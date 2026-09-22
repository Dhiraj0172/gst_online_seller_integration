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
    GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
    DATE_PATTERN = re.compile(r"^(0[1-9]|[12][0-9]|3[01])-(0[1-9]|1[0-2])-\d{4}$")
    VALID_RATES = {0.0, 0.1, 0.25, 1.0, 1.5, 3.0, 5.0, 6.0, 7.5, 12.0, 18.0, 28.0}
    STATE_CODES = {f"{i:02d}" for i in range(1, 38)}

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

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "Schema", errors, [])

    @classmethod
    def validate_fields(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []
        
        if "gstin" in data and not cls.GSTIN_PATTERN.match(data["gstin"]):
            errors.append(cls._err("gstin", data["gstin"], "format", "Invalid GSTIN format", "ERROR"))
            
        if "fp" in data and not re.match(r"^(0[1-9]|1[0-2])\d{4}$", data["fp"]):
            errors.append(cls._err("fp", data["fp"], "format", "Invalid return period format (MMYYYY)", "ERROR"))

        if "b2b" in data:
            for i, b2b in enumerate(data["b2b"]):
                if "ctin" in b2b and not cls.GSTIN_PATTERN.match(b2b["ctin"]):
                    errors.append(cls._err("ctin", b2b["ctin"], "format", "Invalid recipient GSTIN", "ERROR", "b2b", i))
                for j, inv in enumerate(b2b.get("inv", [])):
                    if "idt" in inv and not cls.DATE_PATTERN.match(inv["idt"]):
                        errors.append(cls._err("idt", inv["idt"], "format", "Invalid invoice date format", "ERROR", "b2b", f"{i}.inv.{j}"))
                    if "pos" in inv and str(inv["pos"]) not in cls.STATE_CODES:
                        errors.append(cls._err("pos", inv["pos"], "format", "Invalid POS state code", "ERROR", "b2b", f"{i}.inv.{j}"))
                    for k, itm in enumerate(inv.get("itms", [])):
                        det = itm.get("itm_det", {})
                        if "rt" in det and float(det["rt"]) not in cls.VALID_RATES:
                            errors.append(cls._err("rt", det["rt"], "value", "Invalid tax rate", "ERROR", "b2b", f"{i}.inv.{j}.itms.{k}"))

        if "cdnr" in data:
             for i, cdnr in enumerate(data["cdnr"]):
                 # Official structure: ctin + nt array
                 if isinstance(cdnr, dict):
                     if "ctin" in cdnr and not cls.GSTIN_PATTERN.match(cdnr["ctin"]):
                         errors.append(cls._err("ctin", cdnr["ctin"], "format", "Invalid recipient GSTIN", "ERROR", "cdnr", i))
                     for j, nt in enumerate(cdnr.get("nt", [])):
                         if "ntty" in nt and nt["ntty"] not in ["C", "D"]:
                             errors.append(cls._err("ntty", nt["ntty"], "value", "Note type must be C or D", "ERROR", "cdnr", f"{i}.nt.{j}"))

        if "cdnur" in data:
             for i, cdnur in enumerate(data["cdnur"]):
                 # Official structure: nt array at top level (no ctin)
                 if isinstance(cdnur, dict):
                     for j, nt in enumerate(cdnur.get("nt", [])):
                         if "ntty" in nt and nt["ntty"] not in ["C", "D"]:
                             errors.append(cls._err("ntty", nt["ntty"], "value", "Note type must be C or D", "ERROR", "cdnur", f"{i}.nt.{j}"))

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "Field", errors, [])

    @classmethod
    def validate_business_rules(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []
        tolerance = 1.0  # Allow 1 rupee rounding difference

        def check_amounts(val, txval, iamt, camt, samt, csamt, path):
            total_tax = float(iamt or 0) + float(camt or 0) + float(samt or 0) + float(csamt or 0)
            expected_val = float(txval or 0) + total_tax
            if abs(float(val or 0) - expected_val) > tolerance:
                errors.append(cls._err("val", val, "math", f"Invoice value {val} does not match txval + taxes {expected_val}", "ERROR", *path))

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

        if "b2cs" in data:
             for i, b2cs in enumerate(data["b2cs"]):
                 txval = b2cs.get("txval", 0)
                 if float(txval) < 0:
                     errors.append(cls._err("txval", txval, "rule", "B2CS taxable value must be >= 0", "ERROR", "b2cs", i))

        is_valid = len(errors) == 0
        return ValidationResult(is_valid, "BusinessRules", errors, [])

    @classmethod
    def validate_cross_tables(cls, data: Dict[str, Any]) -> ValidationResult:
        errors = []
        warnings = []
        
        # Example: HSN totals vs Invoice totals
        inv_txval_total = 0.0
        hsn_txval_total = 0.0
        
        # Sum B2B
        for b2b in data.get("b2b", []):
            for inv in b2b.get("inv", []):
                for itm in inv.get("itms", []):
                    inv_txval_total += float(itm.get("itm_det", {}).get("txval", 0))
                  
        # Sum B2CS
        for b2cs in data.get("b2cs", []):
            inv_txval_total += float(b2cs.get("txval", 0))
          
        # Sum B2CL
        for b2cl in data.get("b2cl", []):
            for inv in b2cl.get("inv", []):
                for itm in inv.get("itms", []):
                    inv_txval_total += float(itm.get("itm_det", {}).get("txval", 0))
                  
        # Sum Exports
        for exp in data.get("exp", []):
            # Export structure is flat list, not nested inv/itms
            inv_txval_total += float(exp.get("txval", 0))
                  
        # Sum HSN - combined mode
        if "hsn" in data and "data" in data["hsn"]:
            for hsn_data in data["hsn"]["data"]:
                hsn_txval_total += float(hsn_data.get("txval", 0))
              
        # Sum HSN - separate B2C mode
        if "hsnb2c" in data and "data" in data["hsnb2c"]:
            for hsn_data in data["hsnb2c"]["data"]:
                hsn_txval_total += float(hsn_data.get("txval", 0))
              
        # Allow 100 rupee difference for rounding and unmapped items
        if abs(inv_txval_total - hsn_txval_total) > 100.0:
            warnings.append(cls._err("txval", hsn_txval_total, "reconciliation", 
                                       f"HSN taxable value ({hsn_txval_total}) differs significantly from invoice total ({inv_txval_total})", 
                                       "WARNING", "hsn", None))

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