import uuid
import os
from decimal import Decimal
from typing import Dict, Any, Optional, List
from datetime import datetime
from collections import defaultdict

from app.extensions import db
from app.models import Transaction, GSTProfile, ImportHistory
from app.services.gstr1_excel_writer import generate_gstr1_excel
from app.services.gstr1_json_writer import generate_gstr1_json
from app.services.gstr1_json_validator import GSTR1Validator, ValidationResult
from app.services.gst_rules import get_rules_for_period
from app.utils.state_codes import resolve_pos_code


class GenerationBlockedError(Exception):
    """Raised when GSTR-1 generation is blocked due to critical reconciliation errors."""
    def __init__(self, message: str = "Generation blocked due to critical reconciliation errors.", reconciliation_report: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.reconciliation_report = reconciliation_report


class GenerationResult:
    def __init__(self, generation_id: str, excel_path: str, json_path: str, validation_result: ValidationResult, reconciliation_report: Dict[str, Any], stats: Dict[str, Any]):
        self.generation_id = generation_id
        self.excel_path = excel_path
        self.json_path = json_path
        self.validation_result = validation_result
        self.reconciliation_report = reconciliation_report
        self.stats = stats

    def to_dict(self):
        return {
            "generation_id": self.generation_id,
            "excel_path": self.excel_path,
            "json_path": self.json_path,
            "validation_result": self.validation_result.to_dict() if self.validation_result else None,
            "reconciliation_report": self.reconciliation_report,
            "stats": self.stats
        }


def load_transactions(profile_id: str, return_period: str) -> Dict[str, Any]:
    """
    Load transactions for a given profile and return period from the database.
    Returns a standardized dictionary ready for GSTR-1 generation.
    """
    profile = None
    try:
        profile = db.session.get(GSTProfile, int(profile_id))
    except (ValueError, TypeError):
        pass
    if not profile:
        return _empty_gstr1_structure(return_period)

    # Query transactions strictly for this profile and exact return period
    transactions = db.session.query(Transaction).join(
        ImportHistory, Transaction.import_history_id == ImportHistory.id
    ).filter(
        Transaction.profile_id == int(profile_id),
        Transaction.is_deleted == False,
        ImportHistory.return_period == return_period
    ).all()

    if not transactions:
        return _empty_gstr1_structure(return_period, profile.gstin)

    return _build_gstr1_json(profile, return_period, transactions)


def _empty_gstr1_structure(return_period: str, gstin: str = "") -> Dict[str, Any]:
    """Return empty GSTR-1 structure following official GSTN schema."""
    return {
        "gstin": gstin,
        "fp": return_period,
        "gt": 0,
        "cur_gt": 0,
        "b2b": [],
        "b2cs": [],
        "cdnr": [],
        "hsn": {"data": []},
        "doc_issue": {"doc_det": []},
        "_stats_meta": {
            "total_hsn_b2b": 0,
            "total_hsn_b2c": 0,
            "total_ecom": 0
        }
    }


def _normalize_classification(raw):
    """Normalize explicit classification variants to base routing targets.

    The classification_service and external data sources may produce suffixed
    or amendment variants that the main routing if/elif chain in
    _build_gstr1_json does not match.  This function maps them back to the
    base names the router understands, while preserving amendment semantics,
    SEZ export-type hints, and NIL/Exempt/Non-GST category identifiers.

    Returns:
        (classification, is_amendment, sez_exp_typ_override, nil_base_override)

    CRIT-02 FIX — no classification should silently disappear.
    """
    # ── Amendment classifications ──
    # These classification strings directly indicate an amendment transaction.
    _AMENDMENT_MAP = {
        'B2BA':   ('B2B',    True,  None,    None),
        'B2CSA':  ('B2CS',   True,  None,    None),
        'CDNRA':  ('CDNR',   True,  None,    None),
        'CDNURA': ('CDNUR',  True,  None,    None),
        'EXPA':   ('EXPORT', True,  None,    None),
    }

    # ── SEZ sub-types ──
    # Both route to SEZ table but with different exp_typ in GSTN schema.
    _SEZ_MAP = {
        'SEZ_REGISTERED':   ('SEZ', False, 'WOPAY', None),
        'SEZ_UNREGISTERED': ('SEZ', False, 'WOPAY', None),
    }

    # ── NIL/Exempt/Non-GST with recipient suffix ──
    # Strip the _REGISTERED / _UNREGISTERED suffix to match the base
    # routing branch, but preserve the base category for _add_nil().
    _NIL_MAP = {
        'NIL_REGISTERED':       ('NIL',    False, None, 'NIL'),
        'NIL_UNREGISTERED':     ('NIL',    False, None, 'NIL'),
        'EXEMPT_REGISTERED':    ('EXEMPT', False, None, 'EXEMPT'),
        'EXEMPT_UNREGISTERED':  ('EXEMPT', False, None, 'EXEMPT'),
        'NONGST_REGISTERED':    ('NONGST', False, None, 'NONGST'),
        'NONGST_UNREGISTERED':  ('NONGST', False, None, 'NONGST'),
    }

    if raw in _AMENDMENT_MAP:
        return _AMENDMENT_MAP[raw]
    if raw in _SEZ_MAP:
        return _SEZ_MAP[raw]
    if raw in _NIL_MAP:
        return _NIL_MAP[raw]

    # No normalization needed — pass through as-is.
    return (raw, False, None, None)


def _build_gstr1_json(profile: GSTProfile, return_period: str, transactions: list) -> Dict[str, Any]:
    """
    Build GSTR-1 JSON structure from transactions.

    Official GSTN schema reference (from GST Developer Portal / Returns Offline Tool):
    - b2b: [{ctin, inv: [{inum, idt, val, pos, rchrg, inv_typ, itms: [{num, itm_det: {rt, txval, iamt, camt, samt, csamt}}]}]}]
    - b2cl: [{pos, inv: [{inum, idt, val, itms: [{num, itm_det: {rt, txval, iamt, csamt}}]}]}]
    - b2cs: [{sply_ty, pos, typ, rt, txval, iamt, camt, samt, csamt}]
    - cdnr: [{ctin, nt: [{ntty, nt_num, nt_dt, val, pos, rchrg, inv_typ, itms: [{num, itm_det: {rt, txval, iamt, camt, samt, csamt}}]}]}]
    - cdnur: [{ntty, nt_num, nt_dt, val, pos, typ, itms: [{num, itm_det: {rt, txval, iamt, camt, samt, csamt}}]}]
    - exp: [{exp_typ, inv: [{inum, idt, val, sbpcode, sbnum, sbdt, itms: [{num, itm_det: {rt, txval, iamt, csamt}}]}]}]
    - nil: {inv: [{sply_ty, nil_amt, expt_amt, ngsup_amt}]}
    - hsn: {data: [{num, hsn_sc, desc, uqc, qty, val, txval, iamt, camt, samt, csamt}]}
    - doc_issue: {doc_det: [{doc_num, docs: [{num, from, to, totnum, cancel, net_issue}]}]}
    """
    rules = get_rules_for_period(return_period)
    supplier_state = (profile.gstin or "")[:2]

    # Accumulators
    b2b_groups = {}  # keyed by ctin
    b2cs_agg = {}    # keyed by (sply_ty, pos, rt)
    b2cl_groups = {} # keyed by pos
    cdnr_groups = {} # keyed by ctin
    cdnur_data = []
    exp_groups = {}  # keyed by exp_typ
    nil_data = defaultdict(lambda: {"nil_amt": Decimal('0'), "expt_amt": Decimal('0'), "ngsup_amt": Decimal('0')})
    hsn_agg = {}     # keyed by (hsn_sac, uqc, tax_rate)

    # Amendment accumulators
    b2ba_groups = {}  # keyed by ctin
    b2cla_groups = {} # keyed by pos
    b2csa_data = []
    cdnra_groups = {} # keyed by ctin
    cdnura_data = []
    expa_groups = {}  # keyed by exp_typ

    total_taxable = Decimal('0')
    total_tax = Decimal('0')

    # Track invoices for doc_issue
    invoice_numbers = []
    cn_numbers = []
    dn_numbers = []

    for tx in transactions:
        txval = Decimal(str(tx.taxable_value or 0))

        raw_classification = (getattr(tx, 'classification_status', None) or getattr(tx, 'gstr1_table', None) or tx.supply_type or 'UNKNOWN').upper()

        if raw_classification not in ('HSN', 'HSNB2C', 'DOCS'):
            total_taxable += txval
            total_tax += Decimal(str(tx.total_tax or 0))

        # ── CRIT-02 FIX: Normalize explicit classification variants ──
        # The classification_service may produce suffixed/amendment variants
        # (e.g. B2BA, CDNRA, SEZ_REGISTERED, NIL_UNREGISTERED) that must be
        # mapped to the base routing branches used below.  Without this
        # normalization these transactions silently fall through every
        # if/elif branch and are dropped from GSTR-1 output.
        classification, is_amendment_from_class, sez_exp_typ_override, nil_base_override = \
            _normalize_classification(raw_classification)

        # Build item detail following official GSTN itm_det schema
        item_det = {
            "rt": float(tx.tax_rate or 0),
            "txval": float(txval),
            "iamt": float(tx.igst_amount or 0),
            "camt": float(tx.cgst_amount or 0),
            "samt": float(tx.sgst_amount or 0),
            "csamt": float(tx.cess_amount or 0)
        }

        pos_val = tx.place_of_supply or resolve_pos_code(tx.place_of_supply, tx.customer_gstin, profile.state_code)
        is_amendment = (getattr(tx, 'amendment_flag', False) or False) or is_amendment_from_class

        # Determine if inter/intra state
        is_inter = str(pos_val) != supplier_state

        # Build invoice/detail based on classification
        if classification == 'B2B':
            if is_amendment:
                _add_b2b_amendment(b2ba_groups, tx, item_det, pos_val)
            else:
                _add_b2b(b2b_groups, tx, item_det, pos_val)
                if tx.invoice_number:
                    invoice_numbers.append(tx.invoice_number)

        elif classification == 'B2CS':
            if is_amendment:
                _add_b2cs_amendment(b2csa_data, tx, txval, pos_val, is_inter, supplier_state)
            else:
                _add_b2cs(b2cs_agg, tx, txval, pos_val, is_inter, supplier_state)

        elif classification in ('B2CL', 'B2CLA'):
            # B2CL only applicable for periods before Aug 2024
            is_b2cl_amendment = is_amendment or (classification == 'B2CLA')
            if rules.get('b2cl_applicable', False):
                if is_b2cl_amendment:
                    _add_b2cl_amendment(b2cla_groups, tx, item_det, pos_val)
                else:
                    _add_b2cl(b2cl_groups, tx, item_det, pos_val)
                    if tx.invoice_number:
                        invoice_numbers.append(tx.invoice_number)
            else:
                if is_b2cl_amendment:
                    _add_b2cs_amendment(b2csa_data, tx, txval, pos_val, is_inter, supplier_state)
                else:
                    _add_b2cs(b2cs_agg, tx, txval, pos_val, is_inter, supplier_state)

        elif classification in ('CDNR', 'CDNUR'):
            is_registered = classification == 'CDNR'
            if is_amendment:
                if is_registered:
                    _add_cdnr_amendment(cdnra_groups, tx, item_det, pos_val)
                else:
                    _add_cdnur_amendment(cdnura_data, tx, item_det, pos_val)
            else:
                if is_registered:
                    _add_cdnr(cdnr_groups, tx, item_det, pos_val)
                else:
                    _add_cdnur(cdnur_data, tx, item_det, pos_val)
                # Track note numbers for doc_issue
                if tx.note_type and tx.note_number:
                    if tx.note_type.upper().startswith('C'):
                        cn_numbers.append(tx.note_number)
                    else:
                        dn_numbers.append(tx.note_number)
        elif classification in ('EXPORT', 'SEZ'):
            exp_typ = sez_exp_typ_override or ("WPAY" if classification == 'EXPORT' else "WOPAY")
            if is_amendment:
                _add_exp_amendment(expa_groups, tx, item_det, exp_typ)
            else:
                _add_exp(exp_groups, tx, item_det, exp_typ)
                if tx.invoice_number:
                    invoice_numbers.append(tx.invoice_number)

        elif classification in ('NIL', 'EXEMPT', 'NONGST'):
            nil_category = nil_base_override or classification
            meta = {}
            if getattr(tx, 'source_metadata', None):
                try:
                    import json
                    meta = json.loads(tx.source_metadata)
                except Exception:
                    pass
            sply_ty_override = meta.get('nil_sply_ty') or getattr(tx, 'uqc', None)
            if sply_ty_override in ("INTRB2B", "INTRAB2B", "INTRB2C", "INTRAB2C"):
                if meta.get('nil_rated_supplies') is not None and (meta.get('nil_rated_supplies') != '0' or meta.get('exempt_supplies') != '0' or meta.get('non_gst_supplies') != '0'):
                    nil_data[sply_ty_override]["nil_amt"] += Decimal(str(meta.get('nil_rated_supplies') or 0))
                    nil_data[sply_ty_override]["expt_amt"] += Decimal(str(meta.get('exempt_supplies') or 0))
                    nil_data[sply_ty_override]["ngsup_amt"] += Decimal(str(meta.get('non_gst_supplies') or 0))
                else:
                    if nil_category == 'NIL':
                        nil_data[sply_ty_override]["nil_amt"] += txval
                    elif nil_category == 'EXEMPT':
                        nil_data[sply_ty_override]["expt_amt"] += txval
                    elif nil_category == 'NONGST':
                        nil_data[sply_ty_override]["ngsup_amt"] += txval
            else:
                _add_nil(nil_data, nil_category, txval, is_inter, tx.customer_gstin)

        # HSN aggregation (for all non-amendment transactions with valid HSN code)
        if not is_amendment and classification != 'B2CLA' and getattr(tx, 'hsn_sac', None):
            _add_hsn(hsn_agg, tx, txval, raw_classification)

    # Build HSN output based on period rules
    hsn_reporting_mode = rules.get('hsn_reporting_mode', 'combined')
    hsn_output = _build_hsn_output(hsn_agg, hsn_reporting_mode)

    # Build NIL output following official GSTN schema
    nil_output = _build_nil_output(nil_data)

    # Build doc_issue from tracked document numbers
    doc_issue = _build_doc_issue(invoice_numbers, cn_numbers, dn_numbers)

    # Build final structure
    json_data = {
        "gstin": profile.gstin,
        "fp": return_period,
        "gt": float(total_taxable + total_tax),
        "cur_gt": float(total_taxable + total_tax),
        "b2b": list(b2b_groups.values()),
        "b2cs": list(b2cs_agg.values()),
        "cdnr": list(cdnr_groups.values()),
        "cdnur": cdnur_data,
        "exp": list(exp_groups.values()),
        "nil": nil_output,
        "hsn": hsn_output,
        "doc_issue": doc_issue
    }

    # Backward compatibility: also expose hsnb2c if separate mode is active
    if hsn_reporting_mode == 'separate_b2b_b2c' and "b2c_data" in hsn_output:
        json_data["hsnb2c"] = {"data": hsn_output["b2c_data"]}

    # Add B2CL if applicable (pre-Aug 2024 only)
    if b2cl_groups and rules.get('b2cl_applicable', False):
        json_data["b2cl"] = list(b2cl_groups.values())

    # Add amendment tables if they have data
    if b2ba_groups:
        json_data["b2ba"] = list(b2ba_groups.values())
    if b2cla_groups:
        json_data["b2cla"] = list(b2cla_groups.values())
    if b2csa_data:
        json_data["b2csa"] = b2csa_data
    if cdnra_groups:
        json_data["cdnra"] = list(cdnra_groups.values())
    if cdnura_data:
        json_data["cdnura"] = cdnura_data
    if expa_groups:
        json_data["expa"] = list(expa_groups.values())

    hsn_b2b_count = sum(1 for v in hsn_agg.values() if v.get("category") == "B2B")
    hsn_b2c_count = sum(1 for v in hsn_agg.values() if v.get("category") != "B2B")
    ecom_count = sum(1 for tx in transactions if getattr(tx, 'ecommerce_gstin', None))
    json_data["_stats_meta"] = {
        "total_hsn_b2b": hsn_b2b_count,
        "total_hsn_b2c": hsn_b2c_count,
        "total_ecom": ecom_count
    }

    return json_data


# ──────────────────────────────────────────────
# B2B: Official GSTN schema
# [{ctin, inv: [{inum, idt, val, pos, rchrg, inv_typ, ecom_gstin, itms: [{num, itm_det: {rt, txval, iamt, camt, samt, csamt}}]}]}]
# ──────────────────────────────────────────────

def _add_b2b(b2b_groups, tx, item_det, pos_val):
    """Add a B2B transaction following official GSTN b2b schema."""
    ctin = tx.customer_gstin
    if ctin not in b2b_groups:
        b2b_groups[ctin] = {"ctin": ctin, "inv": []}

    inv_num = tx.invoice_number
    existing_inv = None
    for inv in b2b_groups[ctin]["inv"]:
        if inv["inum"] == inv_num:
            existing_inv = inv
            break

    itm_entry = {"num": len((existing_inv or {}).get("itms", [])) + 1, "itm_det": item_det}

    if existing_inv:
        existing_inv["itms"].append(itm_entry)
    else:
        # inv_typ: R=Regular, SEZ WP=SEZ with payment, SEZ WOP=SEZ without payment, DE=Deemed Export
        inv_typ = tx.invoice_type or "R"
        # Normalize common variations
        if inv_typ.upper() in ("REGULAR", "REG"):
            inv_typ = "R"

        b2b_groups[ctin]["inv"].append({
            "inum": inv_num,
            "idt": format_date_gst(tx.invoice_date) if tx.invoice_date else "",
            "val": float(tx.invoice_value or 0),
            "pos": pos_val,
            "rchrg": tx.reverse_charge or "N",
            "inv_typ": inv_typ,
            "ecom_gstin": tx.ecommerce_gstin or "",
            "itms": [{"num": 1, "itm_det": item_det}]
        })


# ──────────────────────────────────────────────
# B2CS: Official GSTN schema
# [{sply_ty, pos, typ, rt, txval, iamt, camt, samt, csamt}]
# Aggregated by (sply_ty, pos, rt)
# ──────────────────────────────────────────────

def _add_b2cs(b2cs_agg, tx, txval, pos_val, is_inter, supplier_state):
    """Add a B2CS transaction following official GSTN b2cs schema (aggregated)."""
    sply_ty = "INTER" if is_inter else "INTRA"
    rt = float(tx.tax_rate or 0)
    key = (sply_ty, str(pos_val), rt)

    if key not in b2cs_agg:
        b2cs_agg[key] = {
            "sply_ty": sply_ty,
            "pos": str(pos_val),
            "typ": "OE",
            "rt": rt,
            "txval": 0.0,
            "iamt": 0.0,
            "camt": 0.0,
            "samt": 0.0,
            "csamt": 0.0
        }

    b2cs_agg[key]["txval"] += float(txval)
    b2cs_agg[key]["iamt"] += float(tx.igst_amount or 0)
    b2cs_agg[key]["camt"] += float(tx.cgst_amount or 0)
    b2cs_agg[key]["samt"] += float(tx.sgst_amount or 0)
    b2cs_agg[key]["csamt"] += float(tx.cess_amount or 0)


# ──────────────────────────────────────────────
# B2CL: Official GSTN schema (pre-Aug 2024 only)
# [{pos, inv: [{inum, idt, val, itms: [{num, itm_det: {rt, txval, iamt, csamt}}]}]}]
# ──────────────────────────────────────────────

def _add_b2cl(b2cl_groups, tx, item_det, pos_val):
    """Add a B2CL transaction following official GSTN b2cl schema."""
    pos = str(pos_val)
    if pos not in b2cl_groups:
        b2cl_groups[pos] = {"pos": pos, "inv": []}

    inv_num = tx.invoice_number
    existing_inv = None
    for inv in b2cl_groups[pos]["inv"]:
        if inv["inum"] == inv_num:
            existing_inv = inv
            break

    # B2CL only has IGST (inter-state)
    b2cl_item = {
        "rt": item_det["rt"],
        "txval": item_det["txval"],
        "iamt": item_det["iamt"],
        "csamt": item_det["csamt"]
    }
    itm_entry = {"num": len((existing_inv or {}).get("itms", [])) + 1, "itm_det": b2cl_item}

    if existing_inv:
        existing_inv["itms"].append(itm_entry)
    else:
        b2cl_groups[pos]["inv"].append({
            "inum": inv_num,
            "idt": format_date_gst(tx.invoice_date) if tx.invoice_date else "",
            "val": float(tx.invoice_value or 0),
            "ecom_gstin": tx.ecommerce_gstin or "",
            "itms": [{"num": 1, "itm_det": b2cl_item}]
        })


# ──────────────────────────────────────────────
# CDNR: Official GSTN schema
# [{ctin, nt: [{ntty, nt_num, nt_dt, val, pos, rchrg, inv_typ, itms: [{num, itm_det: {rt, txval, iamt, camt, samt, csamt}}]}]}]
# ──────────────────────────────────────────────

def _add_cdnr(cdnr_groups, tx, item_det, pos_val):
    """Add a CDNR transaction following official GSTN cdnr schema."""
    ctin = tx.customer_gstin or ""
    if ctin not in cdnr_groups:
        cdnr_groups[ctin] = {"ctin": ctin, "nt": []}

    nt_num = tx.note_number or tx.invoice_number or ""
    existing_nt = None
    for nt in cdnr_groups[ctin]["nt"]:
        if nt["nt_num"] == nt_num:
            existing_nt = nt
            break

    itm_entry = {"num": len((existing_nt or {}).get("itms", [])) + 1, "itm_det": item_det}

    if existing_nt:
        existing_nt["itms"].append(itm_entry)
    else:
        ntty = "C"
        if tx.note_type:
            ntty = "D" if tx.note_type.upper().startswith("D") else "C"

        cdnr_groups[ctin]["nt"].append({
            "ntty": ntty,
            "nt_num": nt_num,
            "nt_dt": format_date_gst(tx.note_date or tx.invoice_date) if (tx.note_date or tx.invoice_date) else "",
            "inum": tx.original_invoice_number or "",
            "idt": format_date_gst(tx.original_invoice_date) if tx.original_invoice_date else "",
            "val": float(tx.invoice_value or 0),
            "pos": pos_val,
            "rchrg": tx.reverse_charge or "N",
            "inv_typ": "R",
            "itms": [{"num": 1, "itm_det": item_det}]
        })


# ──────────────────────────────────────────────
# CDNUR: Official GSTN schema
# [{ntty, nt_num, nt_dt, val, pos, typ, itms: [{num, itm_det: {rt, txval, iamt, camt, samt, csamt}}]}]
# ──────────────────────────────────────────────

def _add_cdnur(cdnur_data, tx, item_det, pos_val):
    """Add a CDNUR transaction following official GSTN cdnur schema."""
    nt_num = tx.note_number or tx.invoice_number or ""

    # Check if note already exists
    existing_nt = None
    for nt in cdnur_data:
        if nt["nt_num"] == nt_num:
            existing_nt = nt
            break

    itm_entry = {"num": len((existing_nt or {}).get("itms", [])) + 1, "itm_det": item_det}

    if existing_nt:
        existing_nt["itms"].append(itm_entry)
    else:
        ntty = "C"
        if tx.note_type:
            ntty = "D" if tx.note_type.upper().startswith("D") else "C"

        cdnur_data.append({
            "ntty": ntty,
            "nt_num": nt_num,
            "nt_dt": format_date_gst(tx.note_date or tx.invoice_date) if (tx.note_date or tx.invoice_date) else "",
            "inum": tx.original_invoice_number or "",
            "idt": format_date_gst(tx.original_invoice_date) if tx.original_invoice_date else "",
            "val": float(tx.invoice_value or 0),
            "pos": pos_val,
            "typ": "B2CL",
            "itms": [{"num": 1, "itm_det": item_det}]
        })


# ──────────────────────────────────────────────
# EXP: Official GSTN schema
# [{exp_typ, inv: [{inum, idt, val, sbpcode, sbnum, sbdt, itms: [{num, itm_det: {rt, txval, iamt, csamt}}]}]}]
# ──────────────────────────────────────────────

def _add_exp(exp_groups, tx, item_det, exp_typ):
    """Add an Export/SEZ transaction following official GSTN exp schema."""
    if exp_typ not in exp_groups:
        exp_groups[exp_typ] = {"exp_typ": exp_typ, "inv": []}

    inv_num = tx.invoice_number
    existing_inv = None
    for inv in exp_groups[exp_typ]["inv"]:
        if inv["inum"] == inv_num:
            existing_inv = inv
            break

    # Exports only have IGST
    exp_item = {
        "rt": item_det["rt"],
        "txval": item_det["txval"],
        "iamt": item_det["iamt"],
        "csamt": item_det["csamt"]
    }
    itm_entry = {"num": len((existing_inv or {}).get("itms", [])) + 1, "itm_det": exp_item}

    if existing_inv:
        existing_inv["itms"].append(itm_entry)
    else:
        exp_groups[exp_typ]["inv"].append({
            "inum": inv_num,
            "idt": format_date_gst(tx.invoice_date) if tx.invoice_date else "",
            "val": float(tx.invoice_value or 0),
            "sbpcode": "",
            "sbnum": "",
            "sbdt": "",
            "itms": [{"num": 1, "itm_det": exp_item}]
        })


# ──────────────────────────────────────────────
# NIL/EXEMPT/NONGST: Official GSTN schema
# {inv: [{sply_ty, nil_amt, expt_amt, ngsup_amt}]}
# sply_ty: INTRB2B, INTRAB2B, INTRB2C, INTRAB2C
# ──────────────────────────────────────────────

def _add_nil(nil_data, classification, txval, is_inter, customer_gstin):
    """Add NIL/Exempt/Non-GST amounts following official GSTN nil schema."""
    has_gstin = bool(customer_gstin and len(str(customer_gstin)) == 15)

    if is_inter and has_gstin:
        sply_ty = "INTRB2B"
    elif not is_inter and has_gstin:
        sply_ty = "INTRAB2B"
    elif is_inter and not has_gstin:
        sply_ty = "INTRB2C"
    else:
        sply_ty = "INTRAB2C"

    if classification == 'NIL':
        nil_data[sply_ty]["nil_amt"] += txval
    elif classification == 'EXEMPT':
        nil_data[sply_ty]["expt_amt"] += txval
    elif classification == 'NONGST':
        nil_data[sply_ty]["ngsup_amt"] += txval


def _build_nil_output(nil_data):
    """Build official GSTN nil output structure."""
    inv = []
    # Always include all four supply types for completeness
    for sply_ty in ["INTRB2B", "INTRAB2B", "INTRB2C", "INTRAB2C"]:
        amounts = nil_data.get(sply_ty, {"nil_amt": Decimal('0'), "expt_amt": Decimal('0'), "ngsup_amt": Decimal('0')})
        inv.append({
            "sply_ty": sply_ty,
            "nil_amt": float(amounts["nil_amt"]),
            "expt_amt": float(amounts["expt_amt"]),
            "ngsup_amt": float(amounts["ngsup_amt"])
        })
    return {"inv": inv}


# ──────────────────────────────────────────────
# HSN: Official GSTN schema
# {data: [{num, hsn_sc, desc, uqc, qty, val, txval, iamt, camt, samt, csamt}]}
# May 2025+: separate b2b and b2c HSN reporting
# ──────────────────────────────────────────────

def _add_hsn(hsn_agg, tx, txval, classification):
    """Aggregate HSN data, tracking B2B vs B2C category for period-aware reporting."""
    is_b2b_type = classification in (
        'B2B', 'B2BA', 'CDNR', 'CDNRA', 'EXPORT', 'SEZ', 'SEZ_REGISTERED',
        'NIL_REGISTERED', 'EXEMPT_REGISTERED', 'NONGST_REGISTERED', 'HSN'
    ) or (bool(getattr(tx, 'customer_gstin', None) and len(str(tx.customer_gstin)) == 15) and classification not in ('B2CS', 'CDNUR', 'B2CL', 'B2CLA', 'B2CSA', 'HSNB2C'))
    category = 'B2B' if is_b2b_type else 'B2C'

    hsn_key = (tx.hsn_sac or "", tx.uqc or "OTH", float(tx.tax_rate or 0), category)
    if hsn_key not in hsn_agg:
        hsn_agg[hsn_key] = {
            "hsn_sc": hsn_key[0],
            "desc": tx.description or "",
            "uqc": hsn_key[1],
            "qty": Decimal('0'),
            "val": Decimal('0'),
            "txval": Decimal('0'),
            "iamt": Decimal('0'),
            "camt": Decimal('0'),
            "samt": Decimal('0'),
            "csamt": Decimal('0'),
            "rt": hsn_key[2],
            "category": category
        }
    hsn_agg[hsn_key]["qty"] += Decimal(str(tx.quantity or 0))
    hsn_agg[hsn_key]["val"] += Decimal(str(tx.invoice_value or txval or 0))
    hsn_agg[hsn_key]["txval"] += txval
    hsn_agg[hsn_key]["iamt"] += Decimal(str(tx.igst_amount or 0))
    hsn_agg[hsn_key]["camt"] += Decimal(str(tx.cgst_amount or 0))
    hsn_agg[hsn_key]["samt"] += Decimal(str(tx.sgst_amount or 0))
    hsn_agg[hsn_key]["csamt"] += Decimal(str(tx.cess_amount or 0))


def _build_hsn_output(hsn_agg, hsn_reporting_mode):
    """
    Build HSN output based on period rules.
    - 'combined': Single hsn.data list (pre-May 2025)
    - 'separate_b2b_b2c': Separate hsn.data (B2B) and hsnb2c (B2C) (May 2025+)
    """
    def _hsn_entry(v, num):
        return {
            "num": num,
            "hsn_sc": v["hsn_sc"],
            "desc": v["desc"],
            "uqc": v["uqc"],
            "qty": float(v["qty"]),
            "val": float(v["val"]),
            "txval": float(v["txval"]),
            "iamt": float(v["iamt"]),
            "camt": float(v["camt"]),
            "samt": float(v["samt"]),
            "csamt": float(v["csamt"])
        }

    if hsn_reporting_mode == 'separate_b2b_b2c':
        b2b_items = []
        b2c_items = []
        for v in hsn_agg.values():
            entry = _hsn_entry(v, 0)
            if v.get("category") == "B2B":
                entry["num"] = len(b2b_items) + 1
                b2b_items.append(entry)
            else:
                entry["num"] = len(b2c_items) + 1
                b2c_items.append(entry)
        return {"data": b2b_items, "b2c_data": b2c_items}
    else:
        items = []
        for i, v in enumerate(hsn_agg.values(), 1):
            entry = _hsn_entry(v, i)
            items.append(entry)
        return {"data": items}


# ──────────────────────────────────────────────
# Documents Issued (Table 13): Official GSTN schema
# {doc_det: [{doc_num, docs: [{num, from, to, totnum, cancel, net_issue}]}]}
# ──────────────────────────────────────────────

def _build_doc_issue(invoice_numbers, cn_numbers, dn_numbers):
    """Build doc_issue from actual tracked document numbers."""
    doc_det = []

    def _add_doc_series(doc_num, numbers, doc_det_list):
        if not numbers:
            return
        sorted_nums = sorted(set(numbers))
        doc_det_list.append({
            "doc_num": doc_num,
            "docs": [{
                "num": 1,
                "from": sorted_nums[0],
                "to": sorted_nums[-1],
                "totnum": len(sorted_nums),
                "cancel": 0,
                "net_issue": len(sorted_nums)
            }]
        })

    _add_doc_series(1, invoice_numbers, doc_det)  # Invoices for outward supply
    _add_doc_series(5, cn_numbers, doc_det)       # Credit Notes
    _add_doc_series(4, dn_numbers, doc_det)       # Debit Notes

    return {"doc_det": doc_det}


# ──────────────────────────────────────────────
# Amendment structures
# ──────────────────────────────────────────────

def _add_b2b_amendment(b2ba_groups, tx, item_det, pos_val):
    """Add B2B amendment (b2ba) following official GSTN schema."""
    ctin = tx.customer_gstin
    if ctin not in b2ba_groups:
        b2ba_groups[ctin] = {"ctin": ctin, "inv": []}

    inv_typ = tx.invoice_type or "R"
    if inv_typ.upper() in ("REGULAR", "REG"):
        inv_typ = "R"

    b2ba_groups[ctin]["inv"].append({
        "oinum": tx.original_invoice_number or tx.invoice_number or "",
        "oidt": format_date_gst(tx.original_invoice_date or tx.invoice_date) if (tx.original_invoice_date or tx.invoice_date) else "",
        "inum": tx.invoice_number or "",
        "idt": format_date_gst(tx.invoice_date) if tx.invoice_date else "",
        "val": float(tx.invoice_value or 0),
        "pos": pos_val,
        "rchrg": tx.reverse_charge or "N",
        "inv_typ": inv_typ,
        "itms": [{"num": 1, "itm_det": item_det}]
    })


def _add_b2cl_amendment(b2cla_groups, tx, item_det, pos_val):
    """Add B2CL amendment (b2cla) following official GSTN schema."""
    pos = str(pos_val)
    if pos not in b2cla_groups:
        b2cla_groups[pos] = {"pos": pos, "inv": []}

    inv_num = tx.invoice_number or ""
    orig_inv_num = tx.original_invoice_number or inv_num

    existing_inv = None
    for inv in b2cla_groups[pos]["inv"]:
        if inv["inum"] == inv_num and inv.get("oinum") == orig_inv_num:
            existing_inv = inv
            break

    # B2CL only has IGST (inter-state)
    b2cl_item = {
        "rt": item_det["rt"],
        "txval": item_det["txval"],
        "iamt": item_det["iamt"],
        "csamt": item_det["csamt"]
    }
    itm_entry = {"num": len((existing_inv or {}).get("itms", [])) + 1, "itm_det": b2cl_item}

    if existing_inv:
        existing_inv["itms"].append(itm_entry)
    else:
        b2cla_groups[pos]["inv"].append({
            "oinum": orig_inv_num,
            "oidt": format_date_gst(tx.original_invoice_date or tx.invoice_date) if (tx.original_invoice_date or tx.invoice_date) else "",
            "inum": inv_num,
            "idt": format_date_gst(tx.invoice_date) if tx.invoice_date else "",
            "val": float(tx.invoice_value or 0),
            "ecom_gstin": tx.ecommerce_gstin or "",
            "itms": [{"num": 1, "itm_det": b2cl_item}]
        })


def _add_b2cs_amendment(b2csa_data, tx, txval, pos_val, is_inter, supplier_state):
    """Add B2CS amendment (b2csa) following official GSTN schema."""
    sply_ty = "INTER" if is_inter else "INTRA"
    b2csa_data.append({
        "sply_ty": sply_ty,
        "pos": str(pos_val),
        "typ": "OE",
        "rt": float(tx.tax_rate or 0),
        "txval": float(txval),
        "iamt": float(tx.igst_amount or 0),
        "camt": float(tx.cgst_amount or 0),
        "samt": float(tx.sgst_amount or 0),
        "csamt": float(tx.cess_amount or 0)
    })


def _add_cdnr_amendment(cdnra_groups, tx, item_det, pos_val):
    """Add CDNR amendment (cdnra) following official GSTN schema."""
    ctin = tx.customer_gstin or ""
    if ctin not in cdnra_groups:
        cdnra_groups[ctin] = {"ctin": ctin, "nt": []}

    ntty = "C"
    if tx.note_type:
        ntty = "D" if tx.note_type.upper().startswith("D") else "C"

    cdnra_groups[ctin]["nt"].append({
        "ntty": ntty,
        "ont_num": tx.original_invoice_number or "",
        "ont_dt": format_date_gst(tx.original_invoice_date) if tx.original_invoice_date else "",
        "nt_num": tx.note_number or tx.invoice_number or "",
        "nt_dt": format_date_gst(tx.note_date or tx.invoice_date) if (tx.note_date or tx.invoice_date) else "",
        "val": float(tx.invoice_value or 0),
        "pos": pos_val,
        "itms": [{"num": 1, "itm_det": item_det}]
    })


def _add_cdnur_amendment(cdnura_data, tx, item_det, pos_val):
    """Add CDNUR amendment (cdnura) following official GSTN schema."""
    ntty = "C"
    if tx.note_type:
        ntty = "D" if tx.note_type.upper().startswith("D") else "C"

    cdnura_data.append({
        "ntty": ntty,
        "ont_num": tx.original_invoice_number or "",
        "ont_dt": format_date_gst(tx.original_invoice_date) if tx.original_invoice_date else "",
        "nt_num": tx.note_number or tx.invoice_number or "",
        "nt_dt": format_date_gst(tx.note_date or tx.invoice_date) if (tx.note_date or tx.invoice_date) else "",
        "val": float(tx.invoice_value or 0),
        "pos": pos_val,
        "typ": "B2CL",
        "itms": [{"num": 1, "itm_det": item_det}]
    })


def _add_exp_amendment(expa_groups, tx, item_det, exp_typ):
    """Add Export amendment (expa) following official GSTN schema."""
    if exp_typ not in expa_groups:
        expa_groups[exp_typ] = {"exp_typ": exp_typ, "inv": []}

    exp_item = {
        "rt": item_det["rt"],
        "txval": item_det["txval"],
        "iamt": item_det["iamt"],
        "csamt": item_det["csamt"]
    }

    expa_groups[exp_typ]["inv"].append({
        "oinum": tx.original_invoice_number or tx.invoice_number or "",
        "oidt": format_date_gst(tx.original_invoice_date or tx.invoice_date) if (tx.original_invoice_date or tx.invoice_date) else "",
        "inum": tx.invoice_number or "",
        "idt": format_date_gst(tx.invoice_date) if tx.invoice_date else "",
        "val": float(tx.invoice_value or 0),
        "sbpcode": "",
        "sbnum": "",
        "sbdt": "",
        "itms": [{"num": 1, "itm_det": exp_item}]
    })


# ──────────────────────────────────────────────
# Utility functions
# ──────────────────────────────────────────────

def format_date_gst(d) -> str:
    """Format date to DD-MM-YYYY."""
    if d is None:
        return ""
    if hasattr(d, 'strftime'):
        return d.strftime('%d-%m-%Y')
    return str(d)


def transform_for_excel(json_data: Dict[str, Any]) -> Dict[str, list]:
    """
    Transforms the JSON structure into row-based lists for Excel generation.
    Must produce rows consistent with the JSON data for reconciliation.
    """
    excel_data = {
        "b2b": [],
        "b2ba": [],
        "b2cl": [],
        "b2cla": [],
        "b2cs": [],
        "b2csa": [],
        "cdnr": [],
        "cdnra": [],
        "cdnur": [],
        "cdnura": [],
        "exp": [],
        "expa": [],
        "nil": [],
        "hsn": [],
        "hsnb2c": [],
        "docs": []
    }

    # B2B: follows {ctin, inv: [{..., itms: [{itm_det}]}]} structure
    if "b2b" in json_data:
        for b2b in json_data["b2b"]:
            ctin = b2b.get("ctin", "")
            for inv in b2b.get("inv", []):
                inum = inv.get("inum", "")
                idt = inv.get("idt", "")
                val = inv.get("val", 0)
                pos = inv.get("pos", "")
                rchrg = inv.get("rchrg", "N")
                inv_typ = inv.get("inv_typ", "R")
                ecom_gstin = inv.get("ecom_gstin", "")
                for itm in inv.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    csamt = det.get("csamt", 0)
                    excel_data["b2b"].append([
                        ctin, inum, idt, val, pos, rchrg, "", inv_typ, ecom_gstin, rt, txval, csamt
                    ])

    # B2BA: amendment rows
    if "b2ba" in json_data:
        for b2ba in json_data["b2ba"]:
            ctin = b2ba.get("ctin", "")
            for inv in b2ba.get("inv", []):
                for itm in inv.get("itms", []):
                    det = itm.get("itm_det", {})
                    excel_data["b2ba"].append([
                        ctin,
                        inv.get("oinum", ""), inv.get("oidt", ""),
                        inv.get("inum", ""), inv.get("idt", ""),
                        inv.get("val", 0), inv.get("pos", ""),
                        inv.get("rchrg", "N"), "",
                        inv.get("inv_typ", "R"), "",
                        det.get("rt", 0), det.get("txval", 0), det.get("csamt", 0)
                    ])

    # B2CS: flat aggregated rows
    if "b2cs" in json_data:
        for b2cs in json_data["b2cs"]:
            typ = b2cs.get("typ", "OE")
            pos = b2cs.get("pos", "")
            rt = b2cs.get("rt", 0)
            txval = b2cs.get("txval", 0)
            csamt = b2cs.get("csamt", 0)
            excel_data["b2cs"].append([
                typ, pos, "", rt, txval, csamt, ""
            ])

    # B2CSA: flat aggregated rows
    if "b2csa" in json_data:
        for b2csa in json_data["b2csa"]:
            typ = b2csa.get("typ", "OE")
            pos = b2csa.get("pos", "")
            rt = b2csa.get("rt", 0)
            txval = b2csa.get("txval", 0)
            csamt = b2csa.get("csamt", 0)
            excel_data["b2csa"].append([
                typ, pos, "", rt, txval, csamt, ""
            ])

    # B2CL: follows {pos, inv: [{..., itms: [{itm_det}]}]} structure
    if "b2cl" in json_data:
        for b2cl in json_data["b2cl"]:
            pos = b2cl.get("pos", "")
            for inv in b2cl.get("inv", []):
                inum = inv.get("inum", "")
                idt = inv.get("idt", "")
                val = inv.get("val", 0)
                ecom_gstin = inv.get("ecom_gstin", "")
                for itm in inv.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    csamt = det.get("csamt", 0)
                    excel_data["b2cl"].append([
                        inum, idt, val, pos, "", rt, txval, csamt, ecom_gstin
                    ])

    # B2CLA: follows {pos, inv: [{..., itms: [{itm_det}]}]} structure
    if "b2cla" in json_data:
        for b2cla in json_data["b2cla"]:
            pos = b2cla.get("pos", "")
            for inv in b2cla.get("inv", []):
                oinum = inv.get("oinum", "")
                oidt = inv.get("oidt", "")
                inum = inv.get("inum", "")
                idt = inv.get("idt", "")
                val = inv.get("val", 0)
                ecom_gstin = inv.get("ecom_gstin", "")
                for itm in inv.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    csamt = det.get("csamt", 0)
                    excel_data["b2cla"].append([
                        oinum, oidt, inum, idt, val, pos, "", rt, txval, csamt, ecom_gstin
                    ])

    # CDNR: follows {ctin, nt: [{..., itms: [{itm_det}]}]} structure
    if "cdnr" in json_data:
        for cdnr_entry in json_data["cdnr"]:
            ctin = cdnr_entry.get("ctin", "")
            for nt in cdnr_entry.get("nt", []):
                nt_num = nt.get("nt_num", "")
                nt_dt = nt.get("nt_dt", "")
                inum = nt.get("inum", "")
                idt = nt.get("idt", "")
                val = nt.get("val", 0)
                pos = nt.get("pos", "")
                rchrg = nt.get("rchrg", "N")
                ntty = nt.get("ntty", "C")
                for itm in nt.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    csamt = det.get("csamt", 0)
                    excel_data["cdnr"].append([
                        ctin, nt_num, nt_dt, inum, idt, val, pos, rchrg, "", ntty, "", rt, txval, csamt
                    ])

    # CDNRA: follows {ctin, nt: [{..., itms: [{itm_det}]}]} structure
    if "cdnra" in json_data:
        for cdnra_entry in json_data["cdnra"]:
            ctin = cdnra_entry.get("ctin", "")
            for nt in cdnra_entry.get("nt", []):
                ont_num = nt.get("ont_num", "")
                ont_dt = nt.get("ont_dt", "")
                nt_num = nt.get("nt_num", "")
                nt_dt = nt.get("nt_dt", "")
                val = nt.get("val", 0)
                pos = nt.get("pos", "")
                ntty = nt.get("ntty", "C")
                for itm in nt.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    csamt = det.get("csamt", 0)
                    excel_data["cdnra"].append([
                        ctin, ont_num, ont_dt, nt_num, nt_dt, val, pos, "", ntty, rt, txval, csamt
                    ])

    # CDNUR: follows [{..., itms: [{itm_det}]}] structure
    if "cdnur" in json_data:
        for nt in json_data["cdnur"]:
            nt_num = nt.get("nt_num", "")
            nt_dt = nt.get("nt_dt", "")
            inum = nt.get("inum", "")
            idt = nt.get("idt", "")
            val = nt.get("val", 0)
            pos = nt.get("pos", "")
            ntty = nt.get("ntty", "C")
            for itm in nt.get("itms", []):
                det = itm.get("itm_det", {})
                rt = det.get("rt", 0)
                txval = det.get("txval", 0)
                csamt = det.get("csamt", 0)
                excel_data["cdnur"].append([
                    nt_num, nt_dt, inum, idt, val, pos, "", ntty, "", rt, txval, csamt
                ])

    # CDNURA: follows [{..., itms: [{itm_det}]}] structure
    if "cdnura" in json_data:
        for nt in json_data["cdnura"]:
            ont_num = nt.get("ont_num", "")
            ont_dt = nt.get("ont_dt", "")
            nt_num = nt.get("nt_num", "")
            nt_dt = nt.get("nt_dt", "")
            val = nt.get("val", 0)
            pos = nt.get("pos", "")
            typ = nt.get("typ", "B2CL")
            ntty = nt.get("ntty", "C")
            for itm in nt.get("itms", []):
                det = itm.get("itm_det", {})
                rt = det.get("rt", 0)
                txval = det.get("txval", 0)
                csamt = det.get("csamt", 0)
                excel_data["cdnura"].append([
                    ont_num, ont_dt, nt_num, nt_dt, val, pos, typ, ntty, rt, txval, csamt
                ])

    # EXP: follows {exp_typ, inv: [{..., itms: [{itm_det}]}]} structure
    if "exp" in json_data:
        for exp_entry in json_data["exp"]:
            exp_typ = exp_entry.get("exp_typ", "")
            for inv in exp_entry.get("inv", []):
                inum = inv.get("inum", "")
                idt = inv.get("idt", "")
                val = inv.get("val", 0)
                sbpcode = inv.get("sbpcode", "")
                sbnum = inv.get("sbnum", "")
                sbdt = inv.get("sbdt", "")
                for itm in inv.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    excel_data["exp"].append([
                        exp_typ, inum, idt, val, sbpcode, sbnum, sbdt, rt, txval
                    ])

    # EXPA: follows {exp_typ, inv: [{..., itms: [{itm_det}]}]} structure
    if "expa" in json_data:
        for expa_entry in json_data["expa"]:
            exp_typ = expa_entry.get("exp_typ", "")
            for inv in expa_entry.get("inv", []):
                oinum = inv.get("oinum", "")
                oidt = inv.get("oidt", "")
                inum = inv.get("inum", "")
                idt = inv.get("idt", "")
                val = inv.get("val", 0)
                sbpcode = inv.get("sbpcode", "")
                sbnum = inv.get("sbnum", "")
                sbdt = inv.get("sbdt", "")
                for itm in inv.get("itms", []):
                    det = itm.get("itm_det", {})
                    rt = det.get("rt", 0)
                    txval = det.get("txval", 0)
                    excel_data["expa"].append([
                        exp_typ, oinum, oidt, inum, idt, val, sbpcode, sbnum, sbdt, rt, txval
                    ])

    # NIL: follows {inv: [{sply_ty, nil_amt, expt_amt, ngsup_amt}]} structure
    if "nil" in json_data:
        nil = json_data["nil"]
        if "inv" in nil:
            for nil_entry in nil["inv"]:
                excel_data["nil"].append([
                    nil_entry.get("sply_ty", ""),
                    nil_entry.get("nil_amt", 0),
                    nil_entry.get("expt_amt", 0),
                    nil_entry.get("ngsup_amt", 0)
                ])
        else:
            # Backward compatibility: old flat structure
            excel_data["nil"].append([
                "Nil/Exempt/Non-GST",
                nil.get("nil_amt", 0),
                nil.get("expt_amt", 0),
                nil.get("ng_amt", nil.get("ngsup_amt", 0))
            ])

    # HSN: follows {data: [{hsn_sc, desc, uqc, qty, val, txval, ...}]} structure
    if "hsn" in json_data and "data" in json_data["hsn"]:
        for h in json_data["hsn"]["data"]:
            excel_data["hsn"].append([
                h.get("hsn_sc", ""),
                h.get("desc", ""),
                h.get("uqc", "NOS"),
                h.get("qty", 0),
                h.get("val", 0),
                h.get("txval", 0),
                h.get("iamt", 0),
                h.get("camt", 0),
                h.get("samt", 0),
                h.get("csamt", 0),
                h.get("rt", 0) if "rt" in h else ""
            ])

    # HSN B2C (May 2025+ separate reporting)
    if "hsn" in json_data and "b2c_data" in json_data["hsn"]:
        for h in json_data["hsn"]["b2c_data"]:
            excel_data["hsnb2c"].append([
                h.get("hsn_sc", ""),
                h.get("desc", ""),
                h.get("uqc", "NOS"),
                h.get("qty", 0),
                h.get("val", 0),
                h.get("txval", 0),
                h.get("iamt", 0),
                h.get("camt", 0),
                h.get("samt", 0),
                h.get("csamt", 0),
                h.get("rt", 0) if "rt" in h else ""
            ])

    # Documents Issued: follows {doc_det: [{doc_num, docs: [{from, to, totnum, cancel, net_issue}]}]}
    if "doc_issue" in json_data:
        doc_issue = json_data["doc_issue"]
        for doc_det in doc_issue.get("doc_det", []):
            doc_num = doc_det.get("doc_num", "")
            for doc in doc_det.get("docs", []):
                excel_data["docs"].append([
                    doc_num,
                    doc.get("from", ""),
                    doc.get("to", ""),
                    doc.get("totnum", 0),
                    doc.get("cancel", 0)
                ])

    return excel_data


def generate_gstr1(
    profile_id: str,
    return_period: str,
    db_session: Any = None,
    include_hsn: bool = True,
    financial_year: Optional[str] = None,
    reconciliation_report: Optional[Dict[str, Any]] = None
) -> GenerationResult:
    """
    Main GSTR-1 generator service.
    Steps: load transactions -> reconcile -> generate Excel -> generate JSON -> validate output -> return
    """
    initial_recon_report = reconciliation_report
    generation_id = str(uuid.uuid4())

    # 1. Load transactions
    json_data = load_transactions(profile_id, return_period)
    _stats_meta = json_data.pop("_stats_meta", {})

    # 2. Options handling (include_hsn)
    if not include_hsn:
        json_data["hsn"] = {"data": []}
        if "hsnb2c" in json_data:
            json_data["hsnb2c"] = {"data": []}

    # 3. Reconciliation report
    recon_obj = None
    if reconciliation_report is not None:
        if hasattr(reconciliation_report, 'to_dict'):
            recon_obj = reconciliation_report
            reconciliation_report = reconciliation_report.to_dict()
    else:
        try:
            from app.services.reconciliation_service import run_full_reconciliation
            recon_obj = run_full_reconciliation(int(profile_id), return_period)
            if recon_obj is None:
                raise GenerationBlockedError("Failed to obtain authoritative reconciliation state: returned None")
            reconciliation_report = recon_obj.to_dict() if hasattr(recon_obj, 'to_dict') else recon_obj
            if reconciliation_report is None:
                raise GenerationBlockedError("Failed to obtain authoritative reconciliation state: no valid dictionary")
        except GenerationBlockedError:
            raise
        except Exception as e:
            raise GenerationBlockedError(f"Failed to obtain authoritative reconciliation state: {str(e)}")

    # Service-layer reconciliation gate check
    is_blocked = False
    
    if recon_obj is not None:
        is_blocked = getattr(recon_obj, 'is_generation_blocked', False)
    if not is_blocked and isinstance(reconciliation_report, dict):
        is_blocked = bool(
            reconciliation_report.get('is_generation_blocked', False)
            or reconciliation_report.get('status') == 'BLOCKED'
            or (reconciliation_report.get('critical_failures', 0) > 0)
        )
    if is_blocked:
        raise GenerationBlockedError(
            f"GSTR-1 generation blocked: Reconciliation report has critical errors for profile {profile_id}, period {return_period}.",
            reconciliation_report=reconciliation_report
        )

    # 4. Section invoice counts
    total_b2b = sum(len(b.get("inv", [])) for b in json_data.get("b2b", [])) + sum(len(b.get("inv", [])) for b in json_data.get("b2ba", []))
    total_b2cs = len(json_data.get("b2cs", [])) + len(json_data.get("b2csa", []))
    total_b2cl = sum(len(b.get("inv", [])) for b in json_data.get("b2cl", [])) + sum(len(b.get("inv", [])) for b in json_data.get("b2cla", []))
    total_cdnr = sum(len(b.get("nt", [])) for b in json_data.get("cdnr", [])) + sum(len(b.get("nt", [])) for b in json_data.get("cdnra", []))
    total_cdnur = len(json_data.get("cdnur", [])) + len(json_data.get("cdnura", []))
    total_exp = sum(len(b.get("inv", [])) for b in json_data.get("exp", [])) + sum(len(b.get("inv", [])) for b in json_data.get("expa", []))

    nil_section = json_data.get("nil", {})
    if isinstance(nil_section, dict):
        nil_rows = nil_section.get("inv", [])
    elif isinstance(nil_section, list):
        nil_rows = nil_section
    else:
        nil_rows = []
    total_nil = sum(1 for row in nil_rows if (row.get("nil_amt", 0) or 0) > 0 or (row.get("expt_amt", 0) or 0) > 0 or (row.get("ngsup_amt", 0) or 0) > 0)

    total_invoices = total_b2b + total_b2cs + total_b2cl + total_cdnr + total_cdnur + total_exp + total_nil

    if include_hsn:
        total_hsn_b2b = _stats_meta.get("total_hsn_b2b", 0)
        total_hsn_b2c = _stats_meta.get("total_hsn_b2c", 0)
        if "b2c_data" in json_data.get("hsn", {}):
            total_hsn_b2b = len(json_data["hsn"].get("data", []))
            total_hsn_b2c = len(json_data["hsn"].get("b2c_data", []))
    else:
        total_hsn_b2b = 0
        total_hsn_b2c = 0

    total_ecom = _stats_meta.get("total_ecom", 0)

    # 5. Tax totals calculation
    total_taxable_value = Decimal('0.00')
    total_cgst = Decimal('0.00')
    total_sgst = Decimal('0.00')
    total_igst = Decimal('0.00')
    total_cess = Decimal('0.00')

    # B2B & B2BA
    for tbl in ("b2b", "b2ba"):
        for b in json_data.get(tbl, []):
            for inv in b.get("inv", []):
                for itm in inv.get("itms", []):
                    d = itm.get("itm_det", {})
                    total_taxable_value += Decimal(str(d.get("txval", 0)))
                    total_cgst += Decimal(str(d.get("camt", 0)))
                    total_sgst += Decimal(str(d.get("samt", 0)))
                    total_igst += Decimal(str(d.get("iamt", 0)))
                    total_cess += Decimal(str(d.get("csamt", 0)))

    # B2CS & B2CSA
    for tbl in ("b2cs", "b2csa"):
        for row in json_data.get(tbl, []):
            total_taxable_value += Decimal(str(row.get("txval", 0)))
            total_cgst += Decimal(str(row.get("camt", 0)))
            total_sgst += Decimal(str(row.get("samt", 0)))
            total_igst += Decimal(str(row.get("iamt", 0)))
            total_cess += Decimal(str(row.get("csamt", 0)))

    # B2CL & B2CLA
    for tbl in ("b2cl", "b2cla"):
        for b in json_data.get(tbl, []):
            for inv in b.get("inv", []):
                for itm in inv.get("itms", []):
                    d = itm.get("itm_det", {})
                    total_taxable_value += Decimal(str(d.get("txval", 0)))
                    total_igst += Decimal(str(d.get("iamt", 0)))
                    total_cess += Decimal(str(d.get("csamt", 0)))

    # CDNR & CDNRA
    for tbl in ("cdnr", "cdnra"):
        for b in json_data.get(tbl, []):
            for nt in b.get("nt", []):
                for itm in nt.get("itms", []):
                    d = itm.get("itm_det", {})
                    total_taxable_value += Decimal(str(d.get("txval", 0)))
                    total_cgst += Decimal(str(d.get("camt", 0)))
                    total_sgst += Decimal(str(d.get("samt", 0)))
                    total_igst += Decimal(str(d.get("iamt", 0)))
                    total_cess += Decimal(str(d.get("csamt", 0)))

    # CDNUR & CDNURA
    for tbl in ("cdnur", "cdnura"):
        for nt in json_data.get(tbl, []):
            for itm in nt.get("itms", []):
                d = itm.get("itm_det", {})
                total_taxable_value += Decimal(str(d.get("txval", 0)))
                total_cgst += Decimal(str(d.get("camt", 0)))
                total_sgst += Decimal(str(d.get("samt", 0)))
                total_igst += Decimal(str(d.get("iamt", 0)))
                total_cess += Decimal(str(d.get("csamt", 0)))

    # EXP & EXPA
    for tbl in ("exp", "expa"):
        for b in json_data.get(tbl, []):
            for inv in b.get("inv", []):
                for itm in inv.get("itms", []):
                    d = itm.get("itm_det", {})
                    total_taxable_value += Decimal(str(d.get("txval", 0)))
                    total_igst += Decimal(str(d.get("iamt", 0)))
                    total_cess += Decimal(str(d.get("csamt", 0)))

    # NIL
    for row in nil_rows:
        nil_val = Decimal(str(row.get("nil_amt", 0))) + Decimal(str(row.get("expt_amt", 0))) + Decimal(str(row.get("ngsup_amt", 0)))
        total_taxable_value += nil_val

    total_tax = total_cgst + total_sgst + total_igst + total_cess

    stats = {
        "total_b2b": total_b2b,
        "total_b2cs": total_b2cs,
        "total_b2cl": total_b2cl,
        "total_cdnr": total_cdnr,
        "total_cdnur": total_cdnur,
        "total_exp": total_exp,
        "total_nil": total_nil,
        "total_hsn_b2b": total_hsn_b2b,
        "total_hsn_b2c": total_hsn_b2c,
        "total_ecom": total_ecom,
        "total_invoices": total_invoices,
        "total_taxable_value": float(total_taxable_value),
        "total_cgst": float(total_cgst),
        "total_sgst": float(total_sgst),
        "total_igst": float(total_igst),
        "total_cess": float(total_cess),
        "total_tax": float(total_tax),
    }

    # 6. Output directories
    output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "exports", generation_id)
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, f"GSTR1_{profile_id}_{return_period}.json")
    excel_path = os.path.join(output_dir, f"GSTR1_{profile_id}_{return_period}.xlsx")

    # 7. Generate JSON
    generate_gstr1_json(json_data, json_path)

    # 8. Generate Excel
    excel_data = transform_for_excel(json_data)
    if not include_hsn:
        excel_data["hsn"] = []
        excel_data["hsnb2c"] = []
    generate_gstr1_excel(excel_data, excel_path)

    # 9. Validate output
    with open(json_path, "r", encoding="utf-8") as f:
        json_str = f.read()
    validation_result = GSTR1Validator.validate_gstr1_json(json_str)

    return GenerationResult(
        generation_id=generation_id,
        excel_path=excel_path,
        json_path=json_path,
        validation_result=validation_result,
        reconciliation_report=reconciliation_report,
        stats=stats
    )
