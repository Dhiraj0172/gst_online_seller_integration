"""Validation and Pre-Filing Review Service for GST Online Seller Integration.

Provides backend-driven inspection of transaction validation issues and
pre-filing reconciliation status scoped strictly by GST profile and return period.

Guarantees:
- Strict profile-scoped and return-period-scoped queries
- Read-only queries with respect to transaction data
- Soft-deleted transactions (is_deleted=True) are strictly excluded
- Differentiates between BLOCKING errors (which prevent GSTR-1 generation)
  and non-blocking WARNINGs (e.g. missing HSN on non-amendments)
- Reuses the existing Phase 5E-1 reconciliation engine (reconciliation_service.py)
"""
from decimal import Decimal
import json
from typing import Any, Dict, List, Optional

from app.extensions import db
from app.models import ImportHistory, Transaction
from app.services.reconciliation_service import (
    _get_active_transactions,
    run_full_reconciliation,
    ReconciliationReport,
)
from app.utils.gstin_validator import validate_gstin
from app.utils.hsn_utils import is_hsn_or_sac
from app.utils.state_codes import STATE_CODES, is_valid_state
from app.utils.tax_calculator import (
    VALID_GST_RATES,
    calculate_invoice_value,
    calculate_total_tax,
)

VALID_SUPPLY_TYPES = {
    'B2B', 'B2BA', 'B2CS', 'B2CSA', 'B2CL', 'B2CLA',
    'CDNR', 'CDNRA', 'CDNUR', 'CDNURA', 'NIL', 'EXEMPT',
    'NONGST', 'EXPORT', 'EXPA', 'SEZ', 'HSN', 'HSNB2C',
}


def _parse_persisted_errors(raw_val: Any) -> List[str]:
    """Safely parse persisted validation error text or JSON into a list of messages."""
    if not raw_val:
        return []
    if isinstance(raw_val, list):
        return [str(item).strip() for item in raw_val if str(item).strip()]
    if isinstance(raw_val, str):
        val = raw_val.strip()
        if not val:
            return []
        if val.startswith('[') and val.endswith(']'):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except Exception:
                pass
        return [val]
    return [str(raw_val).strip()]


def get_validation_issues(
    profile_id: int,
    return_period: str,
    severity: Optional[str] = None,
    search: Optional[str] = None,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve granular transaction-level validation issues.

    Strictly scoped to profile_id, return_period, and is_deleted=False.
    Identifies BLOCKING errors vs WARNINGs accurately.
    """
    if not profile_id or not return_period:
        return []

    pid = int(profile_id)
    rp = str(return_period).strip()

    active_txs = _get_active_transactions(pid, rp)
    if not active_txs:
        return []

    # Identify duplicate fingerprints among active transactions
    fp_groups: Dict[str, List[Transaction]] = {}
    for tx in active_txs:
        if tx.row_fingerprint:
            fp_groups.setdefault(tx.row_fingerprint, []).append(tx)

    issues: List[Dict[str, Any]] = []

    for tx in active_txs:
        tx_issues: List[Dict[str, Any]] = []
        inv_no = tx.invoice_number or f"#{tx.id}"
        st = str(tx.supply_type or '').strip().upper()

        # 1. Unclassified or invalid supply classification (BLOCKING)
        if not st or st not in VALID_SUPPLY_TYPES:
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "UNCLASSIFIED_SUPPLY",
                "category": "Classification",
                "message": f"Transaction has missing or invalid supply classification: '{tx.supply_type or 'None'}'.",
                "details": {"raw_supply_type": tx.supply_type},
            })

        # 2. HSN/SAC Code validation (WARNING for non-amendments, INFO/clean otherwise)
        is_amendment = bool(
            tx.amendment_flag
            or str(tx.supply_type or '').endswith('A')
            or tx.supply_type in ('B2BA', 'B2CSA', 'CDNRA', 'CDNURA', 'EXPA', 'B2CLA')
        )
        if not is_amendment and (not tx.hsn_sac or not str(tx.hsn_sac).strip()):
            tx_issues.append({
                "severity": "WARNING",
                "code": "MISSING_HSN",
                "category": "HSN",
                "message": "Non-amendment transaction is missing HSN/SAC code and is excluded from the HSN summary table.",
                "details": {"amendment_flag": tx.amendment_flag},
            })
        elif tx.hsn_sac and str(tx.hsn_sac).strip():
            type_code = is_hsn_or_sac(str(tx.hsn_sac).strip())
            if type_code == 'UNKNOWN':
                tx_issues.append({
                    "severity": "WARNING",
                    "code": "INVALID_HSN_FORMAT",
                    "category": "HSN",
                    "message": f"HSN/SAC code '{tx.hsn_sac}' has an unrecognized format.",
                    "details": {"hsn_sac": tx.hsn_sac},
                })

        # 3. Place of Supply Validation (BLOCKING)
        pos = str(tx.place_of_supply or '').strip()
        pos_code = pos[:2] if len(pos) >= 2 else pos
        if not pos_code or (pos_code not in STATE_CODES and not is_valid_state(pos)):
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "INVALID_POS",
                "category": "Place of Supply",
                "message": f"Invalid place of supply '{tx.place_of_supply}'.",
                "details": {"place_of_supply": tx.place_of_supply},
            })

        # 4. Tax arithmetic & impossible combinations (BLOCKING)
        txval = Decimal(str(tx.taxable_value or 0))
        cgst = Decimal(str(tx.cgst_amount or 0))
        sgst = Decimal(str(tx.sgst_amount or 0))
        igst = Decimal(str(tx.igst_amount or 0))
        cess = Decimal(str(tx.cess_amount or 0))

        # Impossible combination: CGST > 0 and IGST > 0
        if cgst > 0 and igst > 0:
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "IMPOSSIBLE_TAX_COMBO",
                "category": "Tax",
                "message": f"Transaction has both intra-state CGST ({cgst}) and inter-state IGST ({igst}).",
                "details": {"cgst": str(cgst), "igst": str(igst)},
            })

        # Intra-state asymmetry: CGST != SGST
        if (cgst > 0 or sgst > 0) and abs(cgst - sgst) > Decimal('0.05'):
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "ASYMMETRIC_TAX",
                "category": "Tax",
                "message": f"Intra-state tax asymmetry: CGST ({cgst}) != SGST ({sgst}).",
                "details": {"cgst": str(cgst), "sgst": str(sgst)},
            })

        # Tax arithmetic check: total_tax vs cgst + sgst + igst + cess
        computed_tax = calculate_total_tax(cgst, sgst, igst, cess)
        if tx.total_tax is not None:
            rec_tax = Decimal(str(tx.total_tax))
            if abs(rec_tax - computed_tax) > Decimal('0.05'):
                tx_issues.append({
                    "severity": "BLOCKING",
                    "code": "TAX_ARITHMETIC_ERROR",
                    "category": "Tax",
                    "message": f"Recorded total tax ({rec_tax}) does not match sum of tax components ({computed_tax}).",
                    "details": {"recorded_total_tax": str(rec_tax), "computed_tax": str(computed_tax)},
                })

        # Invoice value arithmetic check: taxable_value + total_tax vs invoice_value
        if tx.invoice_value is not None:
            rec_inv_val = Decimal(str(tx.invoice_value))
            computed_inv_val = calculate_invoice_value(txval, computed_tax)
            if abs(rec_inv_val - computed_inv_val) > Decimal('0.05'):
                tx_issues.append({
                    "severity": "BLOCKING",
                    "code": "INVOICE_VALUE_MISMATCH",
                    "category": "Tax",
                    "message": f"Recorded invoice value ({rec_inv_val}) does not match taxable value + taxes ({computed_inv_val}).",
                    "details": {"recorded_invoice_value": str(rec_inv_val), "computed_invoice_value": str(computed_inv_val)},
                })

        # Negative taxable value (unless credit note / CDNR)
        is_note = bool(tx.note_type in ('CREDIT', 'DEBIT') or st.startswith('CDN'))
        if txval < 0 and not is_note:
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "NEGATIVE_TAXABLE_VALUE",
                "category": "Amounts",
                "message": f"Taxable value ({txval}) cannot be negative for regular invoices.",
                "details": {"taxable_value": str(txval)},
            })

        # Standard GST rate check
        if tx.tax_rate is not None:
            try:
                rate = Decimal(str(tx.tax_rate))
                if rate not in VALID_GST_RATES and rate > 0:
                    tx_issues.append({
                        "severity": "BLOCKING",
                        "code": "INVALID_TAX_RATE",
                        "category": "Tax",
                        "message": f"Tax rate {rate}% is not a recognized GST rate.",
                        "details": {"tax_rate": str(rate)},
                    })
            except Exception:
                pass

        # 5. Customer GSTIN validation (BLOCKING for B2B, WARNING for other sections)
        if tx.customer_gstin and str(tx.customer_gstin).strip():
            c_gstin = str(tx.customer_gstin).strip()
            is_g_valid, g_msg = validate_gstin(c_gstin)
            if not is_g_valid:
                sev = "BLOCKING" if st in ('B2B', 'B2BA', 'CDNR', 'CDNRA') else "WARNING"
                tx_issues.append({
                    "severity": sev,
                    "code": "INVALID_GSTIN",
                    "category": "GSTIN",
                    "message": f"Customer GSTIN '{c_gstin}' is invalid: {g_msg}.",
                    "details": {"customer_gstin": c_gstin, "validation_error": g_msg},
                })
        elif st in ('B2B', 'B2BA'):
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "MISSING_B2B_GSTIN",
                "category": "GSTIN",
                "message": "Recipient GSTIN is required for B2B transactions.",
                "details": {},
            })

        # 6. Basic invoice requirements (BLOCKING)
        if not tx.invoice_number or not str(tx.invoice_number).strip():
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "MISSING_INVOICE_NUMBER",
                "category": "Invoice",
                "message": "Invoice number is required.",
                "details": {},
            })
        if not tx.invoice_date:
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "MISSING_INVOICE_DATE",
                "category": "Invoice",
                "message": "Invoice date is required.",
                "details": {},
            })

        # 7. Duplicate transactions (BLOCKING)
        if tx.row_fingerprint and len(fp_groups.get(tx.row_fingerprint, [])) > 1:
            other_ids = [str(o.id) for o in fp_groups[tx.row_fingerprint] if o.id != tx.id]
            tx_issues.append({
                "severity": "BLOCKING",
                "code": "DUPLICATE_TRANSACTION",
                "category": "Duplicate",
                "message": f"Duplicate transaction: shares identical fingerprint with transaction(s) #{', #'.join(other_ids)}.",
                "details": {"duplicate_with_ids": other_ids, "fingerprint": tx.row_fingerprint},
            })

        # 8. Persisted validation errors on the transaction record
        persisted = _parse_persisted_errors(tx.validation_errors)
        for p_err in persisted:
            # Check if this error isn't already captured in messages
            if not any(p_err.lower() in str(i["message"]).lower() for i in tx_issues):
                p_sev = "BLOCKING" if getattr(tx, 'validation_status', None) == 'ERROR' else "WARNING"
                tx_issues.append({
                    "severity": p_sev,
                    "code": "PERSISTED_ERROR",
                    "category": "Validation",
                    "message": p_err,
                    "details": {"source": "persisted"},
                })

        # Attach standard fields to each issue found for this transaction
        for issue in tx_issues:
            issues.append({
                "id": f"{tx.id}_{issue['code']}",
                "transaction_id": tx.id,
                "invoice_number": inv_no,
                "invoice_date": tx.invoice_date.strftime('%Y-%m-%d') if tx.invoice_date else None,
                "severity": issue["severity"],
                "code": issue["code"],
                "category": issue.get("category", "General"),
                "message": issue["message"],
                "section": tx.supply_type or "UNCLASSIFIED",
                "customer_gstin": tx.customer_gstin or "",
                "customer_name": tx.customer_name or "",
                "place_of_supply": tx.place_of_supply or "",
                "hsn_sac": tx.hsn_sac or "",
                "source_platform": tx.source_platform or tx.marketplace_name or "",
                "return_period": rp,
                "taxable_value": float(tx.taxable_value or 0),
                "total_tax": float(tx.total_tax or 0),
                "invoice_value": float(tx.invoice_value or 0),
                "details": issue.get("details", {}),
            })

    # Apply filters
    if severity:
        sev_upper = severity.strip().upper()
        if sev_upper in ("BLOCKING", "WARNING"):
            issues = [i for i in issues if i["severity"] == sev_upper]

    if category:
        cat_lower = category.strip().lower()
        issues = [
            i for i in issues
            if i["category"].lower() == cat_lower or i["code"].lower() == cat_lower
        ]

    if search:
        s = search.strip().lower()
        issues = [
            i for i in issues
            if s in str(i["invoice_number"]).lower()
            or s in str(i["customer_gstin"]).lower()
            or s in str(i["customer_name"]).lower()
            or s in str(i["message"]).lower()
            or s in str(i["code"]).lower()
            or s in str(i["section"]).lower()
            or s in str(i["hsn_sac"]).lower()
            or s in str(i["place_of_supply"]).lower()
        ]

    return issues


def get_pre_filing_review(
    profile_id: int,
    return_period: str,
    severity: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute pre-filing review by coordinating reconciliation and transaction validation.

    Returns authoritative review report with overall status, blocking/warning counts,
    all 10 reconciliation check results, and granular validation issues.
    """
    if not profile_id or not return_period:
        return {
            "status": "PASS",
            "is_generation_blocked": False,
            "profile_id": profile_id,
            "return_period": return_period,
            "blocking_count": 0,
            "warning_count": 0,
            "total_issues": 0,
            "affected_sections": [],
            "issues": [],
            "reconciliation": None,
            "summary": {
                "status": "PASS",
                "is_generation_blocked": False,
                "blocking_count": 0,
                "warning_count": 0,
                "checks_passed": 0,
                "checks_total": 10,
                "total_issues": 0,
                "total_transactions": 0,
            },
        }

    pid = int(profile_id)
    rp = str(return_period).strip()

    # 1. Run authoritative reconciliation checks (Phase 5E-1 source of truth)
    recon_report = run_full_reconciliation(pid, rp)

    # 2. Get unfiltered issues for true totals
    all_issues = get_validation_issues(pid, rp)
    blocking_count = sum(1 for i in all_issues if i["severity"] == "BLOCKING")
    warning_count = sum(1 for i in all_issues if i["severity"] == "WARNING")

    # 3. Determine overall status and generation gate
    if recon_report.is_generation_blocked or blocking_count > 0:
        overall_status = "BLOCKED"
        is_generation_blocked = True
    elif recon_report.status == "WARNING" or warning_count > 0:
        overall_status = "WARNING"
        is_generation_blocked = False
    else:
        overall_status = "PASS"
        is_generation_blocked = False

    # 4. Get filtered issues for view/API display
    display_issues = get_validation_issues(pid, rp, severity=severity, search=search)

    # 5. Aggregate affected sections
    affected_sections = sorted(list({i["section"] for i in all_issues if i["section"]}))

    # 6. Count reconciliation check outcomes
    checks_passed = sum(1 for c in recon_report.checks if c.passed)
    checks_total = len(recon_report.checks)

    active_txs = _get_active_transactions(pid, rp)

    summary = {
        "status": overall_status,
        "is_generation_blocked": is_generation_blocked,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "total_issues": len(all_issues),
        "total_transactions": len(active_txs),
        "critical_reconciliation_failures": recon_report.critical_failures,
        "reconciliation_warnings": recon_report.warnings,
    }

    return {
        "status": overall_status,
        "is_generation_blocked": is_generation_blocked,
        "profile_id": pid,
        "return_period": rp,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "total_issues": len(all_issues),
        "affected_sections": affected_sections,
        "issues": display_issues,
        "reconciliation": recon_report.to_dict(),
        "summary": summary,
    }
