"""Reconciliation Service for GST Online Seller Integration.

Provides comprehensive profile-scoped and return-period-scoped data integrity,
mathematical reconciliation, and pre-filing validation checks.

Guarantees:
- Strictly profile-scoped and return-period-scoped queries
- Read-only with respect to transaction data
- Safe for multi-tenant usage
- Decimal-safe arithmetic
- Clear differentiation between BLOCKING errors and NON-BLOCKING warnings
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.extensions import db
from app.models import GSTProfile, ImportHistory, RawImport, Transaction
from app.services.gstr1_generator import load_transactions
from app.utils.state_codes import STATE_CODES
from app.utils.tax_calculator import (
    calculate_cess,
    calculate_cgst,
    calculate_igst,
    calculate_invoice_value,
    calculate_sgst,
    calculate_total_tax,
)


class CheckResult:
    """Represents the outcome of an individual reconciliation check."""

    def __init__(
        self,
        passed: bool,
        messages: List[str],
        check_name: str = "",
        severity: str = "INFO",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.passed: bool = passed
        self.messages: List[str] = list(messages)
        self.check_name: str = check_name
        self.severity: str = severity  # 'BLOCKING', 'WARNING', 'INFO'
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_name": self.check_name,
            "passed": self.passed,
            "severity": self.severity,
            "messages": self.messages,
            "details": self.details,
        }

    def __repr__(self) -> str:
        status = "PASS" if self.passed else self.severity
        return f"<CheckResult {self.check_name or 'unnamed'}: {status} ({len(self.messages)} messages)>"


class ReconciliationReport:
    """Aggregates all reconciliation checks for a profile and return period."""

    def __init__(self):
        self.checks: List[CheckResult] = []
        self.critical_failures: int = 0
        self.warnings: int = 0
        self.is_generation_blocked: bool = False
        self.blocking_reasons: List[str] = []
        self.status: str = "PASS"  # PASS, WARNING, BLOCKED

    def add_check(self, result: CheckResult, blocking: bool = False):
        self.checks.append(result)
        if not result.passed or result.severity in ("BLOCKING", "WARNING"):
            if blocking or result.severity == "BLOCKING":
                self.critical_failures += 1
                self.is_generation_blocked = True
                self.blocking_reasons.extend(result.messages)
            elif result.severity == "WARNING":
                self.warnings += 1

        if self.critical_failures > 0:
            self.status = "BLOCKED"
        elif self.warnings > 0 and self.status != "BLOCKED":
            self.status = "WARNING"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "is_generation_blocked": self.is_generation_blocked,
            "critical_failures": self.critical_failures,
            "warnings": self.warnings,
            "blocking_reasons": self.blocking_reasons,
            "checks": [c.to_dict() for c in self.checks],
        }

    def __repr__(self) -> str:
        return f"<ReconciliationReport status={self.status} critical={self.critical_failures} warnings={self.warnings}>"


def _get_active_transactions(profile_id: int, return_period: str) -> List[Transaction]:
    """Retrieve all non-deleted transactions strictly scoped to a profile and return period."""
    return (
        db.session.query(Transaction)
        .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
        .filter(
            Transaction.profile_id == int(profile_id),
            Transaction.is_deleted == False,
            ImportHistory.return_period == str(return_period),
        )
        .order_by(Transaction.id.asc())
        .all()
    )


def check_import_counts(profile_id: int, return_period: str) -> CheckResult:
    """Verify import-history consistency, active transaction counts, and orphan relationships.

    Checks:
    - Orphan transactions with invalid import_history_id (BLOCKING)
    - Transactions missing RawImport relationship linkage (WARNING/BLOCKING)
    - Persisted transaction count vs ImportHistory success_rows count
    """
    pid = int(profile_id)
    rp = str(return_period)

    messages = []
    severity = "INFO"
    passed = True

    # 1. Orphaned transactions check (BLOCKING)
    # Transactions for this profile where import_history_id is missing or points to a non-existent import_history
    orphans = (
        db.session.query(Transaction)
        .filter(
            Transaction.profile_id == pid,
            Transaction.is_deleted == False,
            ~Transaction.import_history_id.in_(db.session.query(ImportHistory.id)),
        )
        .all()
    )
    if orphans:
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Found {len(orphans)} orphaned transaction(s) referencing non-existent import_history records."
        )

    # 2. RawImport linkage check
    unlinked_raw = (
        db.session.query(Transaction)
        .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
        .filter(
            Transaction.profile_id == pid,
            Transaction.is_deleted == False,
            ImportHistory.return_period == rp,
            Transaction.raw_import_id.is_(None),
        )
        .count()
    )
    if unlinked_raw > 0:
        passed = False
        if severity != "BLOCKING":
            severity = "WARNING"
        messages.append(
            f"Found {unlinked_raw} transaction(s) missing RawImport relationship linkage."
        )

    # 3. ImportHistory count reconciliation
    imports = (
        ImportHistory.query.filter_by(profile_id=pid, return_period=rp)
        .order_by(ImportHistory.created_at.asc())
        .all()
    )
    active_txs = _get_active_transactions(pid, rp)
    active_count = len(active_txs)

    if not imports and active_count == 0:
        messages.append(f"No imports or transactions recorded for period {rp}.")
        return CheckResult(
            passed=True,
            messages=messages,
            check_name="import_counts",
            severity="INFO",
            details={"imports_count": 0, "active_transactions": 0, "orphans": len(orphans)},
        )

    expected_persisted_rows = 0
    total_skipped = 0
    total_errors = 0

    for imp in imports:
        expected_persisted_rows += (imp.success_rows or 0) + (imp.warning_rows or 0)
        total_skipped += imp.skipped_rows or 0
        total_errors += imp.error_rows or 0

    total_persisted_in_db = (
        db.session.query(Transaction)
        .filter(
            Transaction.profile_id == pid,
            Transaction.import_history_id.in_([imp.id for imp in imports]),
        )
        .count()
    )

    deleted_count = total_persisted_in_db - active_count

    if total_persisted_in_db != expected_persisted_rows and imports:
        if severity != "BLOCKING":
            severity = "WARNING"
        passed = False
        messages.append(
            f"Import history count mismatch: expected {expected_persisted_rows} persisted rows "
            f"across {len(imports)} import(s), but found {total_persisted_in_db} in database."
        )
    else:
        messages.append(
            f"Import counts verified: {len(imports)} import(s), {active_count} active transaction(s), "
            f"{deleted_count} deleted transaction(s)."
        )

    details = {
        "imports_count": len(imports),
        "active_transactions": active_count,
        "deleted_transactions": deleted_count,
        "expected_persisted_rows": expected_persisted_rows,
        "total_persisted_in_db": total_persisted_in_db,
        "skipped_rows": total_skipped,
        "error_rows": total_errors,
        "orphans": len(orphans),
    }
    return CheckResult(passed, messages, check_name="import_counts", severity=severity, details=details)


def check_b2b_totals(profile_id: int, return_period: str) -> CheckResult:
    """Compute B2B totals from persisted transactions and reconcile against GSTR-1 structure."""
    pid = int(profile_id)
    rp = str(return_period)

    b2b_txs = [
        t for t in _get_active_transactions(pid, rp)
        if t.supply_type in ('B2B', 'B2BA')
    ]

    db_taxable = sum((Decimal(str(t.taxable_value or 0)) for t in b2b_txs), Decimal('0.00'))
    db_cgst = sum((Decimal(str(t.cgst_amount or 0)) for t in b2b_txs), Decimal('0.00'))
    db_sgst = sum((Decimal(str(t.sgst_amount or 0)) for t in b2b_txs), Decimal('0.00'))
    db_igst = sum((Decimal(str(t.igst_amount or 0)) for t in b2b_txs), Decimal('0.00'))
    db_cess = sum((Decimal(str(t.cess_amount or 0)) for t in b2b_txs), Decimal('0.00'))
    db_total_tax = sum(
        (
            Decimal(
                str(
                    t.total_tax
                    if t.total_tax is not None
                    else (
                        (t.cgst_amount or 0)
                        + (t.sgst_amount or 0)
                        + (t.igst_amount or 0)
                        + (t.cess_amount or 0)
                    )
                )
            )
            for t in b2b_txs
        ),
        Decimal('0.00'),
    )

    # Generated GSTR-1 B2B representation
    gstr1_data = load_transactions(str(pid), rp)
    gstr1_taxable = Decimal('0.00')
    gstr1_cgst = Decimal('0.00')
    gstr1_sgst = Decimal('0.00')
    gstr1_igst = Decimal('0.00')
    gstr1_cess = Decimal('0.00')

    gstr1_inv_count = 0
    for ctin_group in (gstr1_data.get('b2b', []) + gstr1_data.get('b2ba', [])):
        for inv in ctin_group.get('inv', []):
            gstr1_inv_count += 1
            for itm in inv.get('itms', []):
                det = itm.get('itm_det', {})
                gstr1_taxable += Decimal(str(det.get('txval', 0)))
                gstr1_cgst += Decimal(str(det.get('camt', 0)))
                gstr1_sgst += Decimal(str(det.get('samt', 0)))
                gstr1_igst += Decimal(str(det.get('iamt', 0)))
                gstr1_cess += Decimal(str(det.get('csamt', 0)))

    messages = []
    severity = "INFO"
    passed = True

    # 1. Recipient GSTIN validation (B2B must have 15-char customer GSTIN)
    missing_gstin_invoices = [
        t.invoice_number or f"#{t.id}"
        for t in b2b_txs
        if not t.customer_gstin or len(str(t.customer_gstin).strip()) != 15
    ]
    if missing_gstin_invoices:
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"B2B records with missing or invalid customer GSTIN ({len(missing_gstin_invoices)} records): "
            f"{', '.join(missing_gstin_invoices[:5])}"
        )

    # 2. Taxable total comparison
    diff_taxable = abs(db_taxable - gstr1_taxable)
    if diff_taxable > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"B2B taxable total mismatch: database={db_taxable}, GSTR-1={gstr1_taxable}, "
            f"difference={db_taxable - gstr1_taxable}"
        )

    # 3. Tax totals comparison
    diff_tax = abs(db_total_tax - (gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess))
    if diff_tax > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"B2B tax total mismatch: database={db_total_tax}, GSTR-1={gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess}, "
            f"difference={db_total_tax - (gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess)}"
        )

    if passed and not missing_gstin_invoices:
        messages.append(
            f"B2B totals reconciled successfully: {len(b2b_txs)} transactions, "
            f"taxable={db_taxable}, total_tax={db_total_tax}."
        )

    details = {
        "transaction_count": len(b2b_txs),
        "gstr1_invoice_count": gstr1_inv_count,
        "db_taxable": str(db_taxable),
        "gstr1_taxable": str(gstr1_taxable),
        "taxable_difference": str(db_taxable - gstr1_taxable),
        "db_total_tax": str(db_total_tax),
        "gstr1_total_tax": str(gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess),
        "missing_gstin_count": len(missing_gstin_invoices),
    }
    return CheckResult(passed, messages, check_name="b2b_totals", severity=severity, details=details)


def check_b2c_totals(profile_id: int, return_period: str) -> CheckResult:
    """Compute B2C/B2CS totals accounting for aggregate and invoice records."""
    pid = int(profile_id)
    rp = str(return_period)

    b2c_txs = [
        t for t in _get_active_transactions(pid, rp)
        if t.supply_type in ('B2CS', 'B2CSA', 'B2CL', 'B2CLA')
    ]

    db_taxable = sum((Decimal(str(t.taxable_value or 0)) for t in b2c_txs), Decimal('0.00'))
    db_cgst = sum((Decimal(str(t.cgst_amount or 0)) for t in b2c_txs), Decimal('0.00'))
    db_sgst = sum((Decimal(str(t.sgst_amount or 0)) for t in b2c_txs), Decimal('0.00'))
    db_igst = sum((Decimal(str(t.igst_amount or 0)) for t in b2c_txs), Decimal('0.00'))
    db_cess = sum((Decimal(str(t.cess_amount or 0)) for t in b2c_txs), Decimal('0.00'))
    db_total_tax = sum(
        (
            Decimal(
                str(
                    t.total_tax
                    if t.total_tax is not None
                    else (
                        (t.cgst_amount or 0)
                        + (t.sgst_amount or 0)
                        + (t.igst_amount or 0)
                        + (t.cess_amount or 0)
                    )
                )
            )
            for t in b2c_txs
        ),
        Decimal('0.00'),
    )

    # Generated GSTR-1 B2CS / B2CSA representation
    gstr1_data = load_transactions(str(pid), rp)
    gstr1_taxable = Decimal('0.00')
    gstr1_cgst = Decimal('0.00')
    gstr1_sgst = Decimal('0.00')
    gstr1_igst = Decimal('0.00')
    gstr1_cess = Decimal('0.00')

    # b2cs aggregate entries
    for item in gstr1_data.get('b2cs', []):
        gstr1_taxable += Decimal(str(item.get('txval', 0)))
        gstr1_cgst += Decimal(str(item.get('camt', 0)))
        gstr1_sgst += Decimal(str(item.get('samt', 0)))
        gstr1_igst += Decimal(str(item.get('iamt', 0)))
        gstr1_cess += Decimal(str(item.get('csamt', 0)))

    # b2csa amendment entries
    for item in gstr1_data.get('b2csa', []):
        gstr1_taxable += Decimal(str(item.get('txval', 0)))
        gstr1_cgst += Decimal(str(item.get('camt', 0)))
        gstr1_sgst += Decimal(str(item.get('samt', 0)))
        gstr1_igst += Decimal(str(item.get('iamt', 0)))
        gstr1_cess += Decimal(str(item.get('csamt', 0)))

    # b2cl large invoice entries (if any)
    for pos_group in gstr1_data.get('b2cl', []):
        for inv in pos_group.get('inv', []):
            for itm in inv.get('itms', []):
                det = itm.get('itm_det', {})
                gstr1_taxable += Decimal(str(det.get('txval', 0)))
                gstr1_cgst += Decimal(str(det.get('camt', 0)))
                gstr1_sgst += Decimal(str(det.get('samt', 0)))
                gstr1_igst += Decimal(str(det.get('iamt', 0)))
                gstr1_cess += Decimal(str(det.get('csamt', 0)))

    messages = []
    severity = "INFO"
    passed = True

    # 1. Registered buyer anomaly in B2C (Warning: B2C shouldn't have valid 15-char buyer GSTIN)
    reg_buyers = [
        t.invoice_number or f"#{t.id}"
        for t in b2c_txs
        if t.customer_gstin and len(str(t.customer_gstin).strip()) == 15
    ]
    if reg_buyers:
        passed = False
        if severity != "BLOCKING":
            severity = "WARNING"
        messages.append(
            f"Found {len(reg_buyers)} B2C transaction(s) with registered buyer GSTIN (may belong in B2B): "
            f"{', '.join(reg_buyers[:5])}"
        )

    # 2. Taxable total comparison
    diff_taxable = abs(db_taxable - gstr1_taxable)
    if diff_taxable > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"B2C taxable total mismatch: database={db_taxable}, GSTR-1={gstr1_taxable}, "
            f"difference={db_taxable - gstr1_taxable}"
        )

    # 3. Tax total comparison
    diff_tax = abs(db_total_tax - (gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess))
    if diff_tax > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"B2C tax total mismatch: database={db_total_tax}, GSTR-1={gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess}, "
            f"difference={db_total_tax - (gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess)}"
        )

    if passed and not reg_buyers:
        messages.append(
            f"B2C totals reconciled successfully: {len(b2c_txs)} transactions, "
            f"taxable={db_taxable}, total_tax={db_total_tax}."
        )

    details = {
        "transaction_count": len(b2c_txs),
        "db_taxable": str(db_taxable),
        "gstr1_taxable": str(gstr1_taxable),
        "taxable_difference": str(db_taxable - gstr1_taxable),
        "db_total_tax": str(db_total_tax),
        "gstr1_total_tax": str(gstr1_cgst + gstr1_sgst + gstr1_igst + gstr1_cess),
        "registered_in_b2c_count": len(reg_buyers),
    }
    return CheckResult(passed, messages, check_name="b2c_totals", severity=severity, details=details)


def check_hsn_reconciliation(profile_id: int, return_period: str) -> CheckResult:
    """Reconcile HSN table against HSN-eligible transactions and identify missing codes or exclusions."""
    pid = int(profile_id)
    rp = str(return_period)

    active_txs = _get_active_transactions(pid, rp)

    hsn_eligible = []
    missing_hsn = []
    amendment_excluded = []

    for tx in active_txs:
        is_amendment = bool(
            tx.amendment_flag
            or str(tx.supply_type or '').endswith('A')
            or tx.supply_type in ('B2BA', 'B2CSA', 'CDNRA', 'CDNURA', 'EXPA', 'B2CLA')
        )
        if is_amendment:
            amendment_excluded.append(tx)
        elif not tx.hsn_sac or not str(tx.hsn_sac).strip():
            missing_hsn.append(tx)
        else:
            hsn_eligible.append(tx)

    expected_hsn_taxable = sum((Decimal(str(t.taxable_value or 0)) for t in hsn_eligible), Decimal('0.00'))
    missing_hsn_taxable = sum((Decimal(str(t.taxable_value or 0)) for t in missing_hsn), Decimal('0.00'))

    # Load generated GSTR-1 HSN representation
    gstr1_data = load_transactions(str(pid), rp)
    gstr1_hsn_taxable = Decimal('0.00')

    hsn_struct = gstr1_data.get('hsn', {})
    for item in hsn_struct.get('data', []):
        gstr1_hsn_taxable += Decimal(str(item.get('txval', 0)))

    for item in hsn_struct.get('b2c_data', []):
        gstr1_hsn_taxable += Decimal(str(item.get('txval', 0)))

    messages = []
    severity = "INFO"
    passed = True

    # 1. HSN summary mismatch against HSN-eligible transactions (BLOCKING)
    diff = abs(expected_hsn_taxable - gstr1_hsn_taxable)
    if diff > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"HSN taxable total mismatch: expected from HSN-eligible transactions={expected_hsn_taxable}, "
            f"GSTR-1 HSN table={gstr1_hsn_taxable}, difference={expected_hsn_taxable - gstr1_hsn_taxable}"
        )

    # 2. Non-amendment transactions missing HSN code (NON-BLOCKING / WARNING)
    if missing_hsn:
        passed = False
        if severity != "BLOCKING":
            severity = "WARNING"
        messages.append(
            f"{len(missing_hsn)} transaction(s) with total taxable value {missing_hsn_taxable} have no HSN/SAC code "
            f"and were excluded from the HSN summary table."
        )

    if passed and not missing_hsn:
        messages.append(
            f"HSN reconciliation passed: {len(hsn_eligible)} eligible transaction(s), "
            f"total taxable value {expected_hsn_taxable} matches GSTR-1 HSN table."
        )

    details = {
        "hsn_eligible_count": len(hsn_eligible),
        "missing_hsn_count": len(missing_hsn),
        "amendment_excluded_count": len(amendment_excluded),
        "expected_hsn_taxable": str(expected_hsn_taxable),
        "gstr1_hsn_taxable": str(gstr1_hsn_taxable),
        "missing_hsn_taxable": str(missing_hsn_taxable),
        "difference": str(expected_hsn_taxable - gstr1_hsn_taxable),
    }
    return CheckResult(passed, messages, check_name="hsn_reconciliation", severity=severity, details=details)


def check_tax_totals(profile_id: int, return_period: str) -> CheckResult:
    """Validate arithmetic consistency and detect impossible component combinations in tax data."""
    pid = int(profile_id)
    rp = str(return_period)

    active_txs = _get_active_transactions(pid, rp)

    messages = []
    severity = "INFO"
    passed = True

    impossible_combos = []
    asymmetric_taxes = []
    arithmetic_errors = []

    total_cgst = Decimal('0.00')
    total_sgst = Decimal('0.00')
    total_igst = Decimal('0.00')
    total_cess = Decimal('0.00')
    total_taxable = Decimal('0.00')

    for tx in active_txs:
        txval = Decimal(str(tx.taxable_value or 0))
        cgst = Decimal(str(tx.cgst_amount or 0))
        sgst = Decimal(str(tx.sgst_amount or 0))
        igst = Decimal(str(tx.igst_amount or 0))
        cess = Decimal(str(tx.cess_amount or 0))

        total_taxable += txval
        total_cgst += cgst
        total_sgst += sgst
        total_igst += igst
        total_cess += cess

        # Impossible combo 1: Both CGST and IGST on the same transaction
        if cgst > 0 and igst > 0:
            impossible_combos.append(
                f"Transaction #{tx.id} ({tx.invoice_number or 'no-inv'}): has both CGST ({cgst}) and IGST ({igst})."
            )

        # Impossible combo 2: Intra-state asymmetry (CGST > 0 without SGST or vice versa)
        if (cgst > 0 or sgst > 0) and abs(cgst - sgst) > Decimal('0.05'):
            asymmetric_taxes.append(
                f"Transaction #{tx.id} ({tx.invoice_number or 'no-inv'}): CGST ({cgst}) != SGST ({sgst})."
            )

        # Arithmetic check: total_tax vs cgst + sgst + igst + cess
        computed_tax = calculate_total_tax(cgst, sgst, igst, cess)
        if tx.total_tax is not None:
            rec_tax = Decimal(str(tx.total_tax))
            if abs(rec_tax - computed_tax) > Decimal('0.05'):
                arithmetic_errors.append(
                    f"Transaction #{tx.id} ({tx.invoice_number or 'no-inv'}): recorded total_tax {rec_tax} != computed {computed_tax}."
                )

        # Invoice value check: taxable_value + total_tax vs invoice_value
        if tx.invoice_value is not None:
            rec_inv_val = Decimal(str(tx.invoice_value))
            computed_inv_val = calculate_invoice_value(txval, computed_tax)
            if abs(rec_inv_val - computed_inv_val) > Decimal('0.05'):
                arithmetic_errors.append(
                    f"Transaction #{tx.id} ({tx.invoice_number or 'no-inv'}): recorded invoice_value {rec_inv_val} != computed {computed_inv_val}."
                )

    if impossible_combos:
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Found {len(impossible_combos)} transaction(s) with impossible dual intra/inter tax: "
            f"{'; '.join(impossible_combos[:3])}"
        )

    if asymmetric_taxes:
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Found {len(asymmetric_taxes)} transaction(s) with asymmetric CGST/SGST: "
            f"{'; '.join(asymmetric_taxes[:3])}"
        )

    if arithmetic_errors:
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Found {len(arithmetic_errors)} transaction(s) with tax arithmetic discrepancies: "
            f"{'; '.join(arithmetic_errors[:3])}"
        )

    # Aggregate intra-state check
    if abs(total_cgst - total_sgst) > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Overall intra-state tax asymmetry: Total CGST ({total_cgst}) != Total SGST ({total_sgst})."
        )

    if passed:
        messages.append(
            f"Tax totals validated successfully: Total Taxable={total_taxable}, "
            f"CGST={total_cgst}, SGST={total_sgst}, IGST={total_igst}, Cess={total_cess}."
        )

    details = {
        "transaction_count": len(active_txs),
        "total_taxable": str(total_taxable),
        "total_cgst": str(total_cgst),
        "total_sgst": str(total_sgst),
        "total_igst": str(total_igst),
        "total_cess": str(total_cess),
        "impossible_combos_count": len(impossible_combos),
        "asymmetric_count": len(asymmetric_taxes),
        "arithmetic_errors_count": len(arithmetic_errors),
    }
    return CheckResult(passed, messages, check_name="tax_totals", severity=severity, details=details)


def check_cdnr_totals(profile_id: int, return_period: str) -> CheckResult:
    """Compute and reconcile CDNR and CDNUR (credit/debit notes) totals."""
    pid = int(profile_id)
    rp = str(return_period)

    cdn_txs = [
        t for t in _get_active_transactions(pid, rp)
        if t.supply_type in ('CDNR', 'CDNRA', 'CDNUR', 'CDNURA')
    ]

    db_taxable = sum((Decimal(str(t.taxable_value or 0)) for t in cdn_txs), Decimal('0.00'))
    db_total_tax = sum(
        (
            Decimal(
                str(
                    t.total_tax
                    if t.total_tax is not None
                    else (
                        (t.cgst_amount or 0)
                        + (t.sgst_amount or 0)
                        + (t.igst_amount or 0)
                        + (t.cess_amount or 0)
                    )
                )
            )
            for t in cdn_txs
        ),
        Decimal('0.00'),
    )

    gstr1_data = load_transactions(str(pid), rp)
    gstr1_taxable = Decimal('0.00')

    for ctin_group in (gstr1_data.get('cdnr', []) + gstr1_data.get('cdnra', [])):
        for nt in ctin_group.get('nt', []):
            for itm in nt.get('itms', []):
                gstr1_taxable += Decimal(str(itm.get('itm_det', {}).get('txval', 0)))

    for cdnur_entry in (gstr1_data.get('cdnur', []) + gstr1_data.get('cdnura', [])):
        for itm in cdnur_entry.get('itms', []):
            gstr1_taxable += Decimal(str(itm.get('itm_det', {}).get('txval', 0)))

    messages = []
    severity = "INFO"
    passed = True

    diff = abs(db_taxable - gstr1_taxable)
    if diff > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Credit/Debit note taxable mismatch: database={db_taxable}, GSTR-1={gstr1_taxable}, "
            f"difference={db_taxable - gstr1_taxable}"
        )
    else:
        messages.append(
            f"Credit/Debit notes reconciled: {len(cdn_txs)} note(s), taxable={db_taxable}."
        )

    details = {
        "cdn_count": len(cdn_txs),
        "db_taxable": str(db_taxable),
        "gstr1_taxable": str(gstr1_taxable),
        "db_total_tax": str(db_total_tax),
    }
    return CheckResult(passed, messages, check_name="cdnr_totals", severity=severity, details=details)


def check_nil_totals(profile_id: int, return_period: str) -> CheckResult:
    """Compute and reconcile Nil, Exempt, and Non-GST supplies totals."""
    pid = int(profile_id)
    rp = str(return_period)

    nil_txs = [
        t for t in _get_active_transactions(pid, rp)
        if t.supply_type in ('NIL', 'EXEMPT', 'NONGST')
    ]

    db_taxable = sum((Decimal(str(t.taxable_value or 0)) for t in nil_txs), Decimal('0.00'))

    gstr1_data = load_transactions(str(pid), rp)
    gstr1_nil_val = Decimal('0.00')
    nil_obj = gstr1_data.get('nil', {})
    for item in nil_obj.get('inv', []):
        gstr1_nil_val += Decimal(str(item.get('nil_amt', 0)))
        gstr1_nil_val += Decimal(str(item.get('expt_amt', 0)))
        gstr1_nil_val += Decimal(str(item.get('ngsup_amt', 0)))

    messages = []
    severity = "INFO"
    passed = True

    diff = abs(db_taxable - gstr1_nil_val)
    if diff > Decimal('0.05'):
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Nil/Exempt taxable mismatch: database={db_taxable}, GSTR-1={gstr1_nil_val}, "
            f"difference={db_taxable - gstr1_nil_val}"
        )
    else:
        messages.append(
            f"Nil/Exempt supplies reconciled: {len(nil_txs)} records, total={db_taxable}."
        )

    details = {
        "nil_count": len(nil_txs),
        "db_taxable": str(db_taxable),
        "gstr1_taxable": str(gstr1_nil_val),
    }
    return CheckResult(passed, messages, check_name="nil_totals", severity=severity, details=details)


def check_state_totals(profile_id: int, return_period: str) -> CheckResult:
    """Validate place of supply format and state code integrity across active transactions."""
    pid = int(profile_id)
    rp = str(return_period)

    active_txs = _get_active_transactions(pid, rp)

    invalid_pos = []
    for tx in active_txs:
        pos = str(tx.place_of_supply or '').strip()
        code = pos[:2] if len(pos) >= 2 else pos
        if not code or code not in STATE_CODES:
            invalid_pos.append(f"Transaction #{tx.id} ({tx.invoice_number or 'no-inv'}): invalid POS '{pos}'")

    messages = []
    severity = "INFO"
    passed = True

    if invalid_pos:
        passed = False
        severity = "BLOCKING"
        messages.append(
            f"Found {len(invalid_pos)} transaction(s) with invalid Place of Supply codes: "
            f"{'; '.join(invalid_pos[:5])}"
        )
    else:
        messages.append(f"Place of Supply codes validated for all {len(active_txs)} active transaction(s).")

    details = {
        "transaction_count": len(active_txs),
        "invalid_pos_count": len(invalid_pos),
    }
    return CheckResult(passed, messages, check_name="state_totals", severity=severity, details=details)


def check_duplicates(profile_id: int, return_period: str = None) -> CheckResult:
    """Report active transactions that share the same business fingerprint within profile/period."""
    query = Transaction.query.filter(
        Transaction.profile_id == int(profile_id),
        Transaction.is_deleted.is_(False),
        Transaction.row_fingerprint.isnot(None),
    )
    if return_period is not None:
        query = query.join(ImportHistory, Transaction.import_history_id == ImportHistory.id).filter(
            ImportHistory.return_period == str(return_period)
        )

    rows = query.all()

    groups: Dict[str, List[Transaction]] = {}
    for transaction in rows:
        groups.setdefault(transaction.row_fingerprint, []).append(transaction)

    messages = []
    duplicate_count = 0
    for fingerprint, transactions in groups.items():
        if len(transactions) < 2:
            continue
        duplicate_count += len(transactions) - 1
        ids = ', #'.join(str(transaction.id) for transaction in transactions)
        invoices = ', '.join(sorted({str(transaction.invoice_number or '') for transaction in transactions}))
        messages.append(
            f'Duplicate transactions for the same source line: #{ids} '
            f'(invoice/document {invoices or "n/a"})'
        )

    unf_query = Transaction.query.filter(
        Transaction.profile_id == int(profile_id),
        Transaction.is_deleted.is_(False),
        Transaction.row_fingerprint.is_(None),
    )
    if return_period is not None:
        unf_query = unf_query.join(ImportHistory, Transaction.import_history_id == ImportHistory.id).filter(
            ImportHistory.return_period == str(return_period)
        )
    unfingerprinted = unf_query.count()
    if unfingerprinted:
        messages.append(
            f'{unfingerprinted} transaction(s) predate duplicate fingerprinting and could not be cross-checked'
        )

    passed = not any('Duplicate transactions' in message for message in messages)
    severity = "BLOCKING" if not passed else "INFO"
    return CheckResult(
        passed=passed,
        messages=messages or ["No duplicate transactions detected."],
        check_name="duplicates",
        severity=severity,
        details={"duplicate_count": duplicate_count, "unfingerprinted_count": unfingerprinted},
    )


def check_unclassified(profile_id: int, return_period: str) -> CheckResult:
    """Verify that all active transactions have a valid recognized supply_type classification."""
    pid = int(profile_id)
    rp = str(return_period)

    active_txs = _get_active_transactions(pid, rp)

    valid_types = {
        'B2B', 'B2BA', 'B2CS', 'B2CSA', 'B2CL', 'B2CLA',
        'CDNR', 'CDNRA', 'CDNUR', 'CDNURA', 'NIL', 'EXEMPT',
        'NONGST', 'EXPORT', 'EXPA', 'SEZ', 'HSN', 'HSNB2C'
    }

    unclassified = [
        t for t in active_txs
        if not t.supply_type or str(t.supply_type).strip().upper() not in valid_types
    ]

    if unclassified:
        invs = [t.invoice_number or f"#{t.id}" for t in unclassified[:5]]
        return CheckResult(
            passed=False,
            messages=[
                f"Found {len(unclassified)} unclassified or invalidly classified transaction(s): {', '.join(invs)}"
            ],
            check_name="unclassified",
            severity="BLOCKING",
            details={"unclassified_count": len(unclassified)},
        )

    return CheckResult(
        passed=True,
        messages=[f"All {len(active_txs)} active transaction(s) have valid supply classifications."],
        check_name="unclassified",
        severity="INFO",
        details={"active_count": len(active_txs)},
    )


def run_full_reconciliation(profile_id: int, return_period: str) -> ReconciliationReport:
    """Orchestrate all reconciliation checks and produce an authoritative ReconciliationReport."""
    pid = int(profile_id)
    rp = str(return_period)

    report = ReconciliationReport()

    # 1. Import counts & orphan check
    report.add_check(check_import_counts(pid, rp))

    # 2. Unclassified transactions check
    report.add_check(check_unclassified(pid, rp))

    # 3. Duplicate transactions check
    report.add_check(check_duplicates(pid, rp))

    # 4. Tax totals & impossible combo check
    report.add_check(check_tax_totals(pid, rp))

    # 5. B2B totals check
    report.add_check(check_b2b_totals(pid, rp))

    # 6. B2C totals check
    report.add_check(check_b2c_totals(pid, rp))

    # 7. HSN reconciliation check
    report.add_check(check_hsn_reconciliation(pid, rp))

    # 8. CDNR totals check
    report.add_check(check_cdnr_totals(pid, rp))

    # 9. NIL totals check
    report.add_check(check_nil_totals(pid, rp))

    # 10. Place of supply check
    report.add_check(check_state_totals(pid, rp))

    return report
