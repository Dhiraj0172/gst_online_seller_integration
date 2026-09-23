import json
import re
from typing import Dict, Any, List, Tuple
from decimal import Decimal

class ValidationResult:
    def __init__(self, is_valid: bool, layer: str, errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]]):
        self.is_valid = is_valid
        self.layer = layer
        self.errors = errors
        self.warnings = warnings

    def to_dict(self):
        return {
            "is_valid": self.is_valid,
            "layer": self.layer,
            "errors": self.errors,
            "warnings": self.warnings
        }

class GSTR1Validator:
    """
    Validates GSTR-1 JSON output against official GSTN schema structures.

    Official GSTN schema structures validated:
    - b2b: [{ctin, inv: [{inum, idt, val, pos, rchrg, inv_typ, itms: [{num, itm_det}]}]}]
    - b2cl: [{pos, inv: [{inum, idt, val, itms: [{num, itm_det}]}]}]
    - b2cla: [{pos, inv: [{oinum, oidt, inum, idt, val, itms: [{num, itm_det}]}]}]
    - b2cs: [{sply_ty, pos, typ, rt, txval, iamt, camt, samt, csamt}]
    - cdnr: [{ctin, nt: [{ntty, nt_num, nt_dt, val, pos, itms: [{num, itm_det}]}]}]
    - cdnur: [{ntty, nt_num, nt_dt, val, pos, typ, itms: [{num, itm_det}]}]
    - exp: [{exp_typ, inv: [{inum, idt, val, itms: [{num, itm_det}]}]}]
    - nil: {inv: [{sply_ty, nil_amt, expt_amt, ngsup_amt}]}
    - hsn: {data: [{num, hsn_sc, desc, uqc, qty, val, txval, iamt, camt, samt, csamt}]}
    - doc_issue: {doc_det: [{doc_num, docs: [{num, from, to, totnum, cancel, net_issue}]}]}

    Amendment tables: b2ba, b2cla, b2csa, cdnra, cdnura follow same nested structures as their base tables.
    """
    GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
    DATE_PATTERN = re.compile(r"^(0[1-9]|[12][0-9]|3[01])-(0[1-9]|1[0-2])-\d{4}$")
    VALID_RATES = {0.0, 0.1, 0.25, 1.0, 1.5, 3.0, 5.0, 6.0, 7.5, 12.0, 18.0, 28.0}
    STATE_CODES = {f"{i:02d}" for i in range(1, 39)} | {"96", "97"}
    VALID_NOTE_TYPES = {"C", "D"}
    VALID_SUPPLY_TYPES = {"INTER", "INTRA"}
    VALID_NIL_SUPPLY_TYPES = {"INTRB2B", "INTRAB2B", "INTRB2C", "INTRAB2C"}
    VALID_EXP_TYPES = {"WPAY", "WOPAY"}
    VALID_INV_TYPES = {"R", "DE", "SEZ WP", "SEZ WOP", "CBW"}

    @classmethod
    def validate_gstr1_json(cls, json_data: str) -> ValidationResult:
        # Layer 1: Syntax
        syntax_res, data = cls.validate_syntax(json_data)
        if not syntax_res.is_valid:
            return syntax_res

        # Layer 2: Schema
        schema_res = cls.validate_schema(data)
        if not schema_res.is_valid:
            return schema_res

        # Layer 3: Fields
        field_res = cls.validate_fields(data)
        if not field_res.is_valid:
            return field_res

        # Layer 4: Business Rules
        biz_res = cls.validate_business_rules(data)
        if not biz_res.is_valid:
            return biz_res

        # Layer 5: Cross-tables
        cross_res = cls.validate_cross_tables(data)

        return cross_res

    @classmethod
    def validate_syntax(cls, json_str: str) -> Tuple[ValidationResult, Any]:
        try:
            data = json.loads(json_str)
            return ValidationResult(True, "Syntax", [], []), data
        except json.JSONDecodeError as e:
            return ValidationResult(False, "Syntax", [{
                "field": "json",
                "value": None,
                "rule": "valid_json",
                "message": f"Invalid JSON syntax: {str(e)}",
                "severity": "ERROR",
                "table": None,
                "row": None
            }], []), None

    @classmethod
    def validate_schema(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not isinstance(data, dict):
            errors.append(cls._err(None, None, "type", "Root must be an object", "ERROR"))
            return ValidationResult(False, "Schema", errors, [])

        required_fields = ["gstin", "fp"]
        for f in required_fields:
            if f not in data:
                errors.append(cls._err(f, None, "required", f"Missing required field {f}", "ERROR"))

        # Validate known table structures
        if "b2b" in data and not isinstance(data["b2b"], list):
            errors.append(cls._err("b2b", None, "type", "b2b must be an array", "ERROR"))
        if "b2cl" in data and not isinstance(data["b2cl"], list):
            errors.append(cls._err("b2cl", None, "type", "b2cl must be an array", "ERROR"))
        if "b2cla" in data and not isinstance(data["b2cla"], list):
            errors.append(cls._err("b2cla", None, "type", "b2cla must be an array", "ERROR"))
        if "b2cs" in data and not isinstance(data["b2cs"], list):
            errors.append(cls._err("b2cs", None, "type", "b2cs must be an array", "ERROR"))
        if "cdnr" in data and not isinstance(data["cdnr"], list):
            errors.append(cls._err("cdnr", None, "type", "cdnr must be an array", "ERROR"))
        if "cdnur" in data and not isinstance(data["cdnur"], list):
            errors.append(cls._err("cdnur", None, "type", "cdnur must be an array", "ERROR"))
        if "exp" in data and not isinstance(data["exp"], list):
            errors.append(cls._err("exp", None, "type", "exp must be an array", "ERROR"))
        if "nil" in data and not isinstance(data["nil"], dict):
            errors.append(cls._err("nil", None, "type", "nil must be an object", "ERROR"))
        if "hsn" in data and not isinstance(data["hsn"], dict):
            errors.append(cls._err("hsn", None, "type", "hsn must be an object", "ERROR"))
        if "doc_issue" in data and not isinstance(data["doc_issue"], dict):
            errors.append(cls._err("doc_issue", None, "type", "doc_issue must be an object", "ERROR"))

        # Validate nesting: B2CLA must contain pos/inv structure
        if "b2cla" in data and isinstance(data["b2cla"], list):
            for i, b2cla_entry in enumerate(data["b2cla"]):
                if isinstance(b2cla_entry, dict):
                    if "pos" not in b2cla_entry:
                        errors.append(cls._err("pos", None, "required", "B2CLA entry must have pos", "ERROR", "b2cla", i))
                    if "inv" not in b2cla_entry:
                        errors.append(cls._err("inv", None, "required", "B2CLA entry must have inv array", "ERROR", "b2cla", i))
                    elif not isinstance(b2cla_entry.get("inv"), list):
                        errors.append(cls._err("inv", None, "type", "B2CLA inv must be an array", "ERROR", "b2cla", i))
                else:
                    errors.append(cls._err("b2cla", None, "type", "B2CLA entry must be an object", "ERROR", "b2cla", i))

        # Validate nesting: CDNR must contain ctin/nt structure
        if "cdnr" in data and isinstance(data["cdnr"], list):
            for i, cdnr_entry in enumerate(data["cdnr"]):
                if isinstance(cdnr_entry, dict):
                    if "ctin" not in cdnr_entry:
                        errors.append(cls._err("ctin", None, "required", "CDNR entry must have ctin", "ERROR", "cdnr", i))
                    if "nt" not in cdnr_entry:
                        errors.append(cls._err("nt", None, "required", "CDNR entry must have nt array", "ERROR", "cdnr", i))
                    elif not isinstance(cdnr_entry.get("nt"), list):
                        errors.append(cls._err("nt", None, "type", "CDNR nt must be an array", "ERROR", "cdnr", i))

        # Validate nesting: EXP must contain exp_typ/inv structure
        if "exp" in data and isinstance(data["exp"], list):
            for i, exp_entry in enumerate(data["exp"]):
                if isinstance(exp_entry, dict):
                    if "exp_typ" not in exp_entry:
                        errors.append(cls._err("exp_typ", None, "required", "EXP entry must have exp_typ", "ERROR", "exp", i))
                    if "inv" not in exp_entry:
                        errors.append(cls._err("inv", None, "required", "EXP entry must have inv array", "ERROR", "exp", i))

        # Validate NIL structure
        if "nil" in data and isinstance(data["nil"], dict):
            if "inv" not in data["nil"]:
                errors.append(cls._err("inv", None, "required", "nil must contain inv array", "ERROR", "nil", None))
            elif not isinstance(data["nil"]["inv"], list):
                errors.append(cls._err("inv", None, "type", "nil.inv must be an array", "ERROR", "nil", None))

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "Schema", errors, [])

    @classmethod
    def validate_fields(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []

        if "gstin" in data and data["gstin"] and not cls.GSTIN_PATTERN.match(data["gstin"]):
            errors.append(cls._err("gstin", data["gstin"], "format", "Invalid GSTIN format", "ERROR"))

        if "fp" in data and not re.match(r"^(0[1-9]|1[0-2])\d{4}$", data["fp"]):
            errors.append(cls._err("fp", data["fp"], "format", "Invalid return period format (MMYYYY)", "ERROR"))

        # Validate B2B fields
        if "b2b" in data and isinstance(data["b2b"], list):
            for i, b2b in enumerate(data["b2b"]):
                if "ctin" in b2b and not cls.GSTIN_PATTERN.match(b2b["ctin"]):
                    errors.append(cls._err("ctin", b2b["ctin"], "format", "Invalid recipient GSTIN", "ERROR", "b2b", i))
                for j, inv in enumerate(b2b.get("inv", [])):
                    if "idt" in inv and inv["idt"] and not cls.DATE_PATTERN.match(inv["idt"]):
                        errors.append(cls._err("idt", inv["idt"], "format", "Invalid invoice date format", "ERROR", "b2b", f"{i}.inv.{j}"))
                    if "pos" in inv and str(inv["pos"]) not in cls.STATE_CODES:
                        errors.append(cls._err("pos", inv["pos"], "format", "Invalid POS state code", "ERROR", "b2b", f"{i}.inv.{j}"))
                    if "inv_typ" in inv and inv["inv_typ"] not in cls.VALID_INV_TYPES:
                        errors.append(cls._err("inv_typ", inv["inv_typ"], "value", f"Invalid invoice type. Allowed: {cls.VALID_INV_TYPES}", "ERROR", "b2b", f"{i}.inv.{j}"))
                    cls._validate_itms(inv.get("itms", []), errors, "b2b", f"{i}.inv.{j}")

        # Validate B2CL fields
        if "b2cl" in data and isinstance(data["b2cl"], list):
            for i, b2cl_entry in enumerate(data["b2cl"]):
                if isinstance(b2cl_entry, dict):
                    pos = str(b2cl_entry.get("pos", ""))
                    if pos and pos not in cls.STATE_CODES:
                        errors.append(cls._err("pos", b2cl_entry.get("pos"), "format", "Invalid POS state code", "ERROR", "b2cl", i))
                    for j, inv in enumerate(b2cl_entry.get("inv", [])):
                        if isinstance(inv, dict):
                            if "idt" in inv and inv["idt"] and not cls.DATE_PATTERN.match(inv["idt"]):
                                errors.append(cls._err("idt", inv["idt"], "format", "Invalid invoice date format", "ERROR", "b2cl", f"{i}.inv.{j}"))
                            cls._validate_itms(inv.get("itms", []), errors, "b2cl", f"{i}.inv.{j}")

        # Validate B2CLA fields
        if "b2cla" in data and isinstance(data["b2cla"], list):
            for i, b2cla_entry in enumerate(data["b2cla"]):
                if isinstance(b2cla_entry, dict):
                    pos = str(b2cla_entry.get("pos", ""))
                    if pos and pos not in cls.STATE_CODES:
                        errors.append(cls._err("pos", b2cla_entry.get("pos"), "format", "Invalid POS state code", "ERROR", "b2cla", i))
                    for j, inv in enumerate(b2cla_entry.get("inv", [])):
                        if isinstance(inv, dict):
                            if "oidt" in inv and inv["oidt"] and not cls.DATE_PATTERN.match(inv["oidt"]):
                                errors.append(cls._err("oidt", inv["oidt"], "format", "Invalid original invoice date format", "ERROR", "b2cla", f"{i}.inv.{j}"))
                            if "idt" in inv and inv["idt"] and not cls.DATE_PATTERN.match(inv["idt"]):
                                errors.append(cls._err("idt", inv["idt"], "format", "Invalid invoice date format", "ERROR", "b2cla", f"{i}.inv.{j}"))
                            if "pos" in inv and str(inv["pos"]) not in cls.STATE_CODES:
                                errors.append(cls._err("pos", inv["pos"], "format", "Invalid POS state code", "ERROR", "b2cla", f"{i}.inv.{j}"))
                            cls._validate_itms(inv.get("itms", []), errors, "b2cla", f"{i}.inv.{j}")

        # Validate B2CS fields
        if "b2cs" in data and isinstance(data["b2cs"], list):
            for i, b2cs in enumerate(data["b2cs"]):
                if "pos" in b2cs and str(b2cs["pos"]) not in cls.STATE_CODES:
                    errors.append(cls._err("pos", b2cs["pos"], "format", "Invalid POS state code", "ERROR", "b2cs", i))
                if "rt" in b2cs and float(b2cs["rt"]) not in cls.VALID_RATES:
                    errors.append(cls._err("rt", b2cs["rt"], "value", "Invalid tax rate", "ERROR", "b2cs", i))
                if "sply_ty" in b2cs and b2cs["sply_ty"] not in cls.VALID_SUPPLY_TYPES:
                    errors.append(cls._err("sply_ty", b2cs["sply_ty"], "value", f"Invalid supply type. Allowed: {cls.VALID_SUPPLY_TYPES}", "ERROR", "b2cs", i))

        # Validate CDNR fields - official schema: [{ctin, nt: [{ntty, nt_num, ...}]}]
        if "cdnr" in data and isinstance(data["cdnr"], list):
            for i, cdnr_entry in enumerate(data["cdnr"]):
                if "ctin" in cdnr_entry and not cls.GSTIN_PATTERN.match(cdnr_entry["ctin"]):
                    errors.append(cls._err("ctin", cdnr_entry["ctin"], "format", "Invalid recipient GSTIN", "ERROR", "cdnr", i))
                for j, nt in enumerate(cdnr_entry.get("nt", [])):
                    if "ntty" in nt and nt["ntty"] not in cls.VALID_NOTE_TYPES:
                        errors.append(cls._err("ntty", nt["ntty"], "value", "Note type must be C or D", "ERROR", "cdnr", f"{i}.nt.{j}"))
                    if "nt_dt" in nt and nt["nt_dt"] and not cls.DATE_PATTERN.match(nt["nt_dt"]):
                        errors.append(cls._err("nt_dt", nt["nt_dt"], "format", "Invalid note date format", "ERROR", "cdnr", f"{i}.nt.{j}"))
                    if "pos" in nt and str(nt["pos"]) not in cls.STATE_CODES:
                        errors.append(cls._err("pos", nt["pos"], "format", "Invalid POS state code", "ERROR", "cdnr", f"{i}.nt.{j}"))
                    cls._validate_itms(nt.get("itms", []), errors, "cdnr", f"{i}.nt.{j}")

        # Validate CDNUR fields - official schema: [{ntty, nt_num, ...}] (also tolerate legacy [{nt: [...]}] if present)
        if "cdnur" in data and isinstance(data["cdnur"], list):
            for i, entry in enumerate(data["cdnur"]):
                if isinstance(entry, dict) and "nt" in entry:
                    for j, nt in enumerate(entry.get("nt", [])):
                        if "ntty" in nt and nt["ntty"] not in cls.VALID_NOTE_TYPES:
                            errors.append(cls._err("ntty", nt["ntty"], "value", "Note type must be C or D", "ERROR", "cdnur", f"{i}.nt.{j}"))
                        if "nt_dt" in nt and nt["nt_dt"] and not cls.DATE_PATTERN.match(nt["nt_dt"]):
                            errors.append(cls._err("nt_dt", nt["nt_dt"], "format", "Invalid note date format", "ERROR", "cdnur", f"{i}.nt.{j}"))
                        cls._validate_itms(nt.get("itms", []), errors, "cdnur", f"{i}.nt.{j}")
                elif isinstance(entry, dict):
                    nt = entry
                    if "ntty" in nt and nt["ntty"] not in cls.VALID_NOTE_TYPES:
                        errors.append(cls._err("ntty", nt["ntty"], "value", "Note type must be C or D", "ERROR", "cdnur", i))
                    if "nt_dt" in nt and nt["nt_dt"] and not cls.DATE_PATTERN.match(nt["nt_dt"]):
                        errors.append(cls._err("nt_dt", nt["nt_dt"], "format", "Invalid note date format", "ERROR", "cdnur", i))
                    cls._validate_itms(nt.get("itms", []), errors, "cdnur", i)

        # Validate EXP fields - official schema: [{exp_typ, inv: [{...}]}]
        if "exp" in data and isinstance(data["exp"], list):
            for i, exp_entry in enumerate(data["exp"]):
                if "exp_typ" in exp_entry and exp_entry["exp_typ"] not in cls.VALID_EXP_TYPES:
                    errors.append(cls._err("exp_typ", exp_entry["exp_typ"], "value", f"Invalid export type. Allowed: {cls.VALID_EXP_TYPES}", "ERROR", "exp", i))
                for j, inv in enumerate(exp_entry.get("inv", [])):
                    if "idt" in inv and inv["idt"] and not cls.DATE_PATTERN.match(inv["idt"]):
                        errors.append(cls._err("idt", inv["idt"], "format", "Invalid invoice date format", "ERROR", "exp", f"{i}.inv.{j}"))
                    cls._validate_itms(inv.get("itms", []), errors, "exp", f"{i}.inv.{j}")

        # Validate NIL fields - official schema: {inv: [{sply_ty, nil_amt, expt_amt, ngsup_amt}]}
        if "nil" in data and isinstance(data["nil"], dict):
            for i, nil_entry in enumerate(data["nil"].get("inv", [])):
                if "sply_ty" in nil_entry and nil_entry["sply_ty"] not in cls.VALID_NIL_SUPPLY_TYPES:
                    errors.append(cls._err("sply_ty", nil_entry["sply_ty"], "value", f"Invalid nil supply type. Allowed: {cls.VALID_NIL_SUPPLY_TYPES}", "ERROR", "nil", i))

        # Validate HSN fields
        if "hsn" in data and isinstance(data["hsn"], dict):
            for i, hsn_entry in enumerate(data["hsn"].get("data", [])):
                if "hsn_sc" in hsn_entry:
                    hsn_code = str(hsn_entry["hsn_sc"]).strip()
                    if hsn_code and len(hsn_code) not in (2, 4, 6, 8):
                        errors.append(cls._err("hsn_sc", hsn_code, "format", "HSN code must be 2, 4, 6, or 8 digits", "ERROR", "hsn", i))

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "Field", errors, [])

    @classmethod
    def _validate_itms(cls, itms, errors, table, path_prefix):
        """Validate itms array containing itm_det objects."""
        for k, itm in enumerate(itms):
            det = itm.get("itm_det", {})
            if "rt" in det and float(det["rt"]) not in cls.VALID_RATES:
                errors.append(cls._err("rt", det["rt"], "value", "Invalid tax rate", "ERROR", table, f"{path_prefix}.itms.{k}"))
            if "txval" in det:
                try:
                    float(det["txval"])
                except (ValueError, TypeError):
                    errors.append(cls._err("txval", det["txval"], "type", "Taxable value must be numeric", "ERROR", table, f"{path_prefix}.itms.{k}"))

    @classmethod
    def validate_business_rules(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []
        tolerance = 1.0  # Allow 1 rupee rounding difference

        def check_amounts(val, txval, iamt, camt, samt, csamt, path):
            total_tax = float(iamt or 0) + float(camt or 0) + float(samt or 0) + float(csamt or 0)
            expected_val = float(txval or 0) + total_tax
            if abs(float(val or 0) - expected_val) > tolerance:
                errors.append(cls._err("val", val, "math", f"Invoice value {val} does not match txval + taxes {expected_val}", "ERROR", *path))

        # B2B business rules
        if "b2b" in data:
            for i, b2b in enumerate(data["b2b"]):
                for j, inv in enumerate(b2b.get("inv", [])):
                    val = inv.get("val", 0)
                    txval_sum = sum(float(itm.get("itm_det", {}).get("txval", 0)) for itm in inv.get("itms", []))
                    iamt_sum = sum(float(itm.get("itm_det", {}).get("iamt", 0)) for itm in inv.get("itms", []))
                    camt_sum = sum(float(itm.get("itm_det", {}).get("camt", 0)) for itm in inv.get("itms", []))
                    samt_sum = sum(float(itm.get("itm_det", {}).get("samt", 0)) for itm in inv.get("itms", []))
                    csamt_sum = sum(float(itm.get("itm_det", {}).get("csamt", 0)) for itm in inv.get("itms", []))
                    check_amounts(val, txval_sum, iamt_sum, camt_sum, samt_sum, csamt_sum, ("b2b", f"{i}.inv.{j}"))

                    # IGST vs CGST/SGST rule
                    pos = str(inv.get("pos", ""))
                    supplier_state = data.get("gstin", "")[:2]
                    is_inter = pos != supplier_state
                    if is_inter and (camt_sum > 0 or samt_sum > 0):
                        errors.append(cls._err("tax", None, "rule", "Inter-state supply cannot have CGST/SGST", "ERROR", "b2b", f"{i}.inv.{j}"))
                    if not is_inter and iamt_sum > 0:
                        errors.append(cls._err("tax", None, "rule", "Intra-state supply cannot have IGST", "ERROR", "b2b", f"{i}.inv.{j}"))

        # B2CL business rules
        if "b2cl" in data and isinstance(data["b2cl"], list):
            for i, b2cl_entry in enumerate(data["b2cl"]):
                if isinstance(b2cl_entry, dict):
                    pos = str(b2cl_entry.get("pos", ""))
                    supplier_state = (data.get("gstin") or "")[:2]
                    for j, inv in enumerate(b2cl_entry.get("inv", [])):
                        if isinstance(inv, dict) and inv.get("itms"):
                            val = inv.get("val", 0)
                            txval_sum = sum(float(itm.get("itm_det", {}).get("txval", 0)) for itm in inv.get("itms", []))
                            iamt_sum = sum(float(itm.get("itm_det", {}).get("iamt", 0)) for itm in inv.get("itms", []))
                            camt_sum = sum(float(itm.get("itm_det", {}).get("camt", 0)) for itm in inv.get("itms", []))
                            samt_sum = sum(float(itm.get("itm_det", {}).get("samt", 0)) for itm in inv.get("itms", []))
                            csamt_sum = sum(float(itm.get("itm_det", {}).get("csamt", 0)) for itm in inv.get("itms", []))
                            check_amounts(val, txval_sum, iamt_sum, camt_sum, samt_sum, csamt_sum, ("b2cl", f"{i}.inv.{j}"))
                            inv_pos = str(inv.get("pos") or pos)
                            if supplier_state and inv_pos and inv_pos == supplier_state:
                                errors.append(cls._err("pos", inv_pos, "rule", "Inter-state supply cannot have POS equal to supplier state", "ERROR", "b2cl", f"{i}.inv.{j}"))
                            if camt_sum > 0 or samt_sum > 0:
                                errors.append(cls._err("tax", None, "rule", "B2CL inter-state supply cannot have CGST/SGST", "ERROR", "b2cl", f"{i}.inv.{j}"))

        # B2CLA business rules
        if "b2cla" in data and isinstance(data["b2cla"], list):
            for i, b2cla_entry in enumerate(data["b2cla"]):
                if isinstance(b2cla_entry, dict):
                    pos = str(b2cla_entry.get("pos", ""))
                    supplier_state = (data.get("gstin") or "")[:2]
                    for j, inv in enumerate(b2cla_entry.get("inv", [])):
                        if isinstance(inv, dict) and inv.get("itms"):
                            val = inv.get("val", 0)
                            txval_sum = sum(float(itm.get("itm_det", {}).get("txval", 0)) for itm in inv.get("itms", []))
                            iamt_sum = sum(float(itm.get("itm_det", {}).get("iamt", 0)) for itm in inv.get("itms", []))
                            camt_sum = sum(float(itm.get("itm_det", {}).get("camt", 0)) for itm in inv.get("itms", []))
                            samt_sum = sum(float(itm.get("itm_det", {}).get("samt", 0)) for itm in inv.get("itms", []))
                            csamt_sum = sum(float(itm.get("itm_det", {}).get("csamt", 0)) for itm in inv.get("itms", []))
                            check_amounts(val, txval_sum, iamt_sum, camt_sum, samt_sum, csamt_sum, ("b2cla", f"{i}.inv.{j}"))
                            inv_pos = str(inv.get("pos") or pos)
                            if supplier_state and inv_pos and inv_pos == supplier_state:
                                errors.append(cls._err("pos", inv_pos, "rule", "Inter-state supply cannot have POS equal to supplier state", "ERROR", "b2cla", f"{i}.inv.{j}"))
                            if camt_sum > 0 or samt_sum > 0:
                                errors.append(cls._err("tax", None, "rule", "B2CLA inter-state supply cannot have CGST/SGST", "ERROR", "b2cla", f"{i}.inv.{j}"))

        # B2CS business rules
        if "b2cs" in data:
             for i, b2cs in enumerate(data["b2cs"]):
                 txval = b2cs.get("txval", 0)
                 if float(txval) < 0:
                     errors.append(cls._err("txval", txval, "rule", "B2CS taxable value must be >= 0", "ERROR", "b2cs", i))

        # CDNR business rules - validate note items
        if "cdnr" in data:
            for i, cdnr_entry in enumerate(data["cdnr"]):
                for j, nt in enumerate(cdnr_entry.get("nt", [])):
                    val = nt.get("val", 0)
                    if nt.get("itms"):
                        txval_sum = sum(float(itm.get("itm_det", {}).get("txval", 0)) for itm in nt.get("itms", []))
                        iamt_sum = sum(float(itm.get("itm_det", {}).get("iamt", 0)) for itm in nt.get("itms", []))
                        camt_sum = sum(float(itm.get("itm_det", {}).get("camt", 0)) for itm in nt.get("itms", []))
                        samt_sum = sum(float(itm.get("itm_det", {}).get("samt", 0)) for itm in nt.get("itms", []))
                        csamt_sum = sum(float(itm.get("itm_det", {}).get("csamt", 0)) for itm in nt.get("itms", []))
                        check_amounts(val, txval_sum, iamt_sum, camt_sum, samt_sum, csamt_sum, ("cdnr", f"{i}.nt.{j}"))

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "BusinessRules", errors, [])

    @classmethod
    def validate_cross_tables(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []
        warnings = []

        # HSN totals vs Invoice totals reconciliation
        inv_txval_total = 0.0
        hsn_txval_total = 0.0

        # Sum B2B - official schema: [{ctin, inv: [{itms: [{itm_det: {txval}}]}]}]
        for b2b in data.get("b2b", []):
            for inv in b2b.get("inv", []):
                for itm in inv.get("itms", []):
                    inv_txval_total += float(itm.get("itm_det", {}).get("txval", 0))

        # Sum B2CS - official schema: flat aggregated with txval
        for b2cs in data.get("b2cs", []):
            inv_txval_total += float(b2cs.get("txval", 0))

        # Sum B2CL - official schema: [{pos, inv: [{itms: [{itm_det: {txval}}]}]}]
        for b2cl in data.get("b2cl", []):
            for inv in b2cl.get("inv", []):
                for itm in inv.get("itms", []):
                    inv_txval_total += float(itm.get("itm_det", {}).get("txval", 0))

        # Sum Exports - support both official schema [{exp_typ, inv: [{itms: ...}]}] and flat [{txval: ...}]
        for exp_entry in data.get("exp", []):
            if isinstance(exp_entry, dict) and "inv" in exp_entry:
                for inv in exp_entry.get("inv", []):
                    for itm in inv.get("itms", []):
                        inv_txval_total += float(itm.get("itm_det", {}).get("txval", 0))
            elif isinstance(exp_entry, dict):
                inv_txval_total += float(exp_entry.get("txval", 0))

        # Sum HSN
        if "hsn" in data and "data" in data["hsn"]:
            for hsn_data in data["hsn"]["data"]:
                hsn_txval_total += float(hsn_data.get("txval", 0))
            # Also include B2C HSN data for May 2025+ separate reporting
            for hsn_data in data["hsn"].get("b2c_data", []):
                hsn_txval_total += float(hsn_data.get("txval", 0))

        # If separate B2C mode used top-level hsnb2c and hsn did not contain b2c_data
        if "hsnb2c" in data and "data" in data["hsnb2c"] and not (data.get("hsn", {}).get("b2c_data")):
            for hsn_data in data["hsnb2c"]["data"]:
                hsn_txval_total += float(hsn_data.get("txval", 0))

        # Allow 100 rupee difference for rounding and unmapped items
        if ("hsn" in data and "data" in data["hsn"]) or ("hsnb2c" in data and "data" in data["hsnb2c"]):
            if abs(inv_txval_total - hsn_txval_total) > 100.0:
                warnings.append(cls._err("txval", hsn_txval_total, "reconciliation",
                                       f"HSN taxable value ({hsn_txval_total}) differs significantly from invoice total ({inv_txval_total})",
                                       "WARNING", "hsn", None))

        # Cross-table: Ensure B2CLA does not duplicate invoices reported in B2CL for the same period
        b2cl_inums = set()
        for b2cl in data.get("b2cl", []):
            if isinstance(b2cl, dict):
                for inv in b2cl.get("inv", []):
                    if isinstance(inv, dict) and inv.get("inum"):
                        b2cl_inums.add(inv["inum"])
        for i, b2cla in enumerate(data.get("b2cla", [])):
            if isinstance(b2cla, dict):
                for j, inv in enumerate(b2cla.get("inv", [])):
                    if isinstance(inv, dict) and inv.get("inum") and inv["inum"] in b2cl_inums:
                        warnings.append(cls._err("inum", inv["inum"], "duplicate_invoice",
                                               f"Invoice {inv['inum']} appears in both B2CL and B2CLA",
                                               "WARNING", "b2cla", f"{i}.inv.{j}"))

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "CrossTable", errors, warnings)

    @staticmethod
    def _err(field, value, rule, message, severity, table=None, row=None):
        return {
            "field": field,
            "value": value,
            "rule": rule,
            "message": message,
            "severity": severity,
            "table": table,
            "row": row
        }