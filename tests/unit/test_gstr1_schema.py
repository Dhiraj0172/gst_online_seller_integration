"""
Focused GSTR-1 Schema and Amendments Tests.

Tests verify:
1. B2B structure correctness
2. B2CS structure correctness (aggregated, sply_ty, tax amounts)
3. B2CL structure correctness (nested inv/itms, pre-Aug 2024)
4. CDNR structure correctness (ctin/nt nesting with itms)
5. CDNUR structure correctness (flat with itms)
6. EXP structure correctness (exp_typ/inv nesting with itms)
7. SEZ structure correctness
8. NIL/EXEMPT/NONGST structure (inv/sply_ty format)
9. HSN pre-May-2025 (combined)
10. HSN May-2025+ (separate B2B/B2C)
11. Documents Issued (Table 13)
12. Amendment structures (b2ba, b2csa, cdnra, cdnura)
13. Validator rejects malformed nesting
14. Generator and validator agree on valid output
15. JSON and Excel represent the same reporting data
16. Edge cases around empty/conditional sections

All tests use exact expected JSON structures per official GSTN schema.
"""
import json
import os
import pytest
import openpyxl
from decimal import Decimal


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def validator():
    from app.services.gstr1_json_validator import GSTR1Validator
    return GSTR1Validator


@pytest.fixture
def json_writer():
    from app.services.gstr1_json_writer import GSTR1JsonWriter
    return GSTR1JsonWriter


@pytest.fixture
def excel_writer():
    from app.services.gstr1_excel_writer import GSTR1ExcelWriter
    return GSTR1ExcelWriter


@pytest.fixture
def gst_rules():
    from app.services.gst_rules import get_rules_for_period, get_hsn_reporting_mode, is_b2cl_applicable
    return {
        "get_rules_for_period": get_rules_for_period,
        "get_hsn_reporting_mode": get_hsn_reporting_mode,
        "is_b2cl_applicable": is_b2cl_applicable
    }


@pytest.fixture
def transform_for_excel():
    from app.services.gstr1_generator import transform_for_excel
    return transform_for_excel


def _minimal_gstr1(**overrides):
    """Build a minimal valid GSTR-1 JSON structure."""
    base = {
        "gstin": "27AABCU9603R1ZM",
        "fp": "012025",
        "gt": 0,
        "cur_gt": 0,
        "b2b": [],
        "b2cs": [],
        "cdnr": [],
        "hsn": {"data": []},
        "doc_issue": {"doc_det": []}
    }
    base.update(overrides)
    return base


# ──────────────────────────────────────────────
# 1. B2B Structure Tests
# ──────────────────────────────────────────────

class TestB2BStructure:
    def test_b2b_correct_nesting(self, validator):
        """B2B must follow [{ctin, inv: [{inum, idt, val, pos, rchrg, inv_typ, itms: [{num, itm_det}]}]}]"""
        data = _minimal_gstr1(b2b=[{
            "ctin": "29AALCS5765L1ZP",
            "inv": [{
                "inum": "INV001",
                "idt": "15-01-2025",
                "val": 1180.0,
                "pos": "29",
                "rchrg": "N",
                "inv_typ": "R",
                "itms": [{
                    "num": 1,
                    "itm_det": {
                        "rt": 18.0,
                        "txval": 1000.0,
                        "iamt": 180.0,
                        "camt": 0.0,
                        "samt": 0.0,
                        "csamt": 0.0
                    }
                }]
            }]
        }], gt=1180.0, cur_gt=1180.0)
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"B2B should be valid: {result.errors}"

    def test_b2b_multiple_items(self, validator):
        """B2B invoice can have multiple items at different rates."""
        data = _minimal_gstr1(b2b=[{
            "ctin": "29AALCS5765L1ZP",
            "inv": [{
                "inum": "INV002",
                "idt": "20-01-2025",
                "val": 2460.0,
                "pos": "29",
                "rchrg": "N",
                "inv_typ": "R",
                "itms": [
                    {"num": 1, "itm_det": {"rt": 18.0, "txval": 1000.0, "iamt": 180.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}},
                    {"num": 2, "itm_det": {"rt": 28.0, "txval": 1000.0, "iamt": 280.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}}
                ]
            }]
        }], gt=2460.0, cur_gt=2460.0)
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"Multi-item B2B should be valid: {result.errors}"

    def test_b2b_inv_typ_R_not_Regular(self, validator):
        """inv_typ should use short codes: R, DE, SEZ WP, SEZ WOP - not 'Regular'."""
        data = _minimal_gstr1(b2b=[{
            "ctin": "29AALCS5765L1ZP",
            "inv": [{
                "inum": "INV003",
                "idt": "10-01-2025",
                "val": 1180.0,
                "pos": "29",
                "rchrg": "N",
                "inv_typ": "Regular",
                "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 1000.0, "iamt": 180.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}}]
            }]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        has_inv_typ_error = any(e.get("field") == "inv_typ" for e in result.errors)
        assert has_inv_typ_error, "Validator should reject inv_typ='Regular' (should be 'R')"

    def test_b2b_inter_state_tax_rule(self, validator):
        """Inter-state B2B must have IGST, not CGST/SGST."""
        data = _minimal_gstr1(b2b=[{
            "ctin": "29AALCS5765L1ZP",
            "inv": [{
                "inum": "INV004",
                "idt": "10-01-2025",
                "val": 1180.0,
                "pos": "29",
                "rchrg": "N",
                "inv_typ": "R",
                "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 1000.0, "iamt": 0.0, "camt": 90.0, "samt": 90.0, "csamt": 0.0}}]
            }]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        tax_errors = [e for e in result.errors if e.get("rule") == "rule" and "CGST/SGST" in e.get("message", "")]
        assert len(tax_errors) > 0, "Inter-state supply with CGST/SGST should be flagged"


# ──────────────────────────────────────────────
# 2. B2CS Structure Tests
# ──────────────────────────────────────────────

class TestB2CSStructure:
    def test_b2cs_aggregated_with_sply_ty(self, validator):
        """B2CS must include sply_ty, pos, typ, rt, txval, iamt, camt, samt, csamt."""
        data = _minimal_gstr1(b2cs=[{
            "sply_ty": "INTRA",
            "pos": "27",
            "typ": "OE",
            "rt": 18.0,
            "txval": 5000.0,
            "iamt": 0.0,
            "camt": 450.0,
            "samt": 450.0,
            "csamt": 0.0
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"B2CS with sply_ty should be valid: {result.errors}"

    def test_b2cs_invalid_supply_type(self, validator):
        """B2CS sply_ty must be INTER or INTRA."""
        data = _minimal_gstr1(b2cs=[{
            "sply_ty": "INVALID",
            "pos": "27",
            "typ": "OE",
            "rt": 18.0,
            "txval": 5000.0,
            "iamt": 0.0,
            "camt": 450.0,
            "samt": 450.0,
            "csamt": 0.0
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        sply_errors = [e for e in result.errors if e.get("field") == "sply_ty"]
        assert len(sply_errors) > 0, "Invalid sply_ty should be rejected"

    def test_b2cs_negative_txval_rejected(self, validator):
        """B2CS txval must be >= 0."""
        data = _minimal_gstr1(b2cs=[{
            "sply_ty": "INTRA",
            "pos": "27",
            "typ": "OE",
            "rt": 18.0,
            "txval": -100.0,
            "iamt": 0.0,
            "camt": 0.0,
            "samt": 0.0,
            "csamt": 0.0
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        assert not result.is_valid, "Negative B2CS txval should be rejected"


# ──────────────────────────────────────────────
# 3. B2CL Structure Tests
# ──────────────────────────────────────────────

class TestB2CLStructure:
    def test_b2cl_applicable_pre_aug_2024(self, gst_rules):
        """B2CL should be applicable for periods before August 2024."""
        assert gst_rules["is_b2cl_applicable"]("072024") is True
        assert gst_rules["is_b2cl_applicable"]("062024") is True

    def test_b2cl_not_applicable_post_aug_2024(self, gst_rules):
        """B2CL should NOT be applicable from August 2024 onwards."""
        assert gst_rules["is_b2cl_applicable"]("082024") is False
        assert gst_rules["is_b2cl_applicable"]("012025") is False
        assert gst_rules["is_b2cl_applicable"]("052025") is False


# ──────────────────────────────────────────────
# 4. CDNR Structure Tests
# ──────────────────────────────────────────────

class TestCDNRStructure:
    def test_cdnr_correct_nesting(self, validator):
        """CDNR must follow [{ctin, nt: [{ntty, nt_num, nt_dt, val, pos, itms: [{num, itm_det}]}]}]"""
        data = _minimal_gstr1(cdnr=[{
            "ctin": "29AALCS5765L1ZP",
            "nt": [{
                "ntty": "C",
                "nt_num": "CN001",
                "nt_dt": "20-01-2025",
                "inum": "INV001",
                "idt": "15-01-2025",
                "val": 1180.0,
                "pos": "29",
                "rchrg": "N",
                "inv_typ": "R",
                "itms": [{
                    "num": 1,
                    "itm_det": {
                        "rt": 18.0,
                        "txval": 1000.0,
                        "iamt": 180.0,
                        "camt": 0.0,
                        "samt": 0.0,
                        "csamt": 0.0
                    }
                }]
            }]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"CDNR with nested nt should be valid: {result.errors}"

    def test_cdnr_requires_ctin(self, validator):
        """CDNR must have ctin field."""
        data = _minimal_gstr1(cdnr=[{
            "nt": [{"ntty": "C", "nt_num": "CN001", "nt_dt": "20-01-2025", "val": 100.0, "pos": "29",
                    "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 100.0, "iamt": 18.0, "camt": 0, "samt": 0, "csamt": 0}}]}]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        has_ctin_error = any(e.get("field") == "ctin" for e in result.errors)
        assert has_ctin_error, "CDNR without ctin should be flagged"

    def test_cdnr_requires_nt_array(self, validator):
        """CDNR entry must have nt array."""
        data = _minimal_gstr1(cdnr=[{
            "ctin": "29AALCS5765L1ZP",
            "nt_num": "CN001",
            "ntty": "C"
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        has_nt_error = any(e.get("field") == "nt" for e in result.errors)
        assert has_nt_error, "CDNR without nt array should be flagged"

    def test_cdnr_note_type_validation(self, validator):
        """CDNR note type must be C or D."""
        data = _minimal_gstr1(cdnr=[{
            "ctin": "29AALCS5765L1ZP",
            "nt": [{"ntty": "X", "nt_num": "CN001", "nt_dt": "20-01-2025", "val": 100.0, "pos": "29",
                    "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 100.0, "iamt": 18.0, "camt": 0, "samt": 0, "csamt": 0}}]}]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        ntty_errors = [e for e in result.errors if e.get("field") == "ntty"]
        assert len(ntty_errors) > 0, "Invalid note type should be rejected"


# ──────────────────────────────────────────────
# 5. CDNUR Structure Tests
# ──────────────────────────────────────────────

class TestCDNURStructure:
    def test_cdnur_correct_structure(self, validator):
        """CDNUR must follow [{ntty, nt_num, nt_dt, val, pos, typ, itms: [{num, itm_det}]}]"""
        data = _minimal_gstr1(cdnur=[{
            "ntty": "C",
            "nt_num": "UCN001",
            "nt_dt": "22-01-2025",
            "inum": "INV010",
            "idt": "10-01-2025",
            "val": 590.0,
            "pos": "29",
            "typ": "B2CL",
            "itms": [{
                "num": 1,
                "itm_det": {
                    "rt": 18.0,
                    "txval": 500.0,
                    "iamt": 90.0,
                    "camt": 0.0,
                    "samt": 0.0,
                    "csamt": 0.0
                }
            }]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"CDNUR should be valid: {result.errors}"


# ──────────────────────────────────────────────
# 6. EXP Structure Tests
# ──────────────────────────────────────────────

class TestEXPStructure:
    def test_exp_correct_nesting(self, validator):
        """EXP must follow [{exp_typ, inv: [{inum, idt, val, sbpcode, sbnum, sbdt, itms: [{num, itm_det}]}]}]"""
        data = _minimal_gstr1(exp=[{
            "exp_typ": "WPAY",
            "inv": [{
                "inum": "EXP001",
                "idt": "05-01-2025",
                "val": 10000.0,
                "sbpcode": "",
                "sbnum": "",
                "sbdt": "",
                "itms": [{
                    "num": 1,
                    "itm_det": {
                        "rt": 18.0,
                        "txval": 10000.0,
                        "iamt": 1800.0,
                        "csamt": 0.0
                    }
                }]
            }]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"EXP should be valid: {result.errors}"

    def test_exp_requires_exp_typ(self, validator):
        """EXP entry must have exp_typ."""
        data = _minimal_gstr1(exp=[{
            "inv": [{"inum": "EXP001", "idt": "05-01-2025", "val": 10000.0,
                     "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 10000.0, "iamt": 1800.0, "csamt": 0}}]}]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        has_exp_typ_error = any(e.get("field") == "exp_typ" for e in result.errors)
        assert has_exp_typ_error, "EXP without exp_typ should be flagged"

    def test_exp_invalid_type(self, validator):
        """EXP exp_typ must be WPAY or WOPAY."""
        data = _minimal_gstr1(exp=[{
            "exp_typ": "INVALID",
            "inv": [{"inum": "EXP001", "idt": "05-01-2025", "val": 10000.0,
                     "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 10000.0, "iamt": 1800.0, "csamt": 0}}]}]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        exp_errors = [e for e in result.errors if e.get("field") == "exp_typ"]
        assert len(exp_errors) > 0, "Invalid exp_typ should be rejected"


# ──────────────────────────────────────────────
# 7. SEZ Structure Tests
# ──────────────────────────────────────────────

class TestSEZStructure:
    def test_sez_uses_exp_table_wopay(self):
        """SEZ supplies should be output as EXP with exp_typ=WOPAY."""
        # This tests the classification logic in the generator
        from app.services.gstr1_generator import _add_exp
        exp_groups = {}

        class MockTx:
            invoice_number = "SEZ001"
            invoice_date = None
            invoice_value = 5000
            ecommerce_gstin = None

        item_det = {"rt": 18.0, "txval": 5000.0, "iamt": 900.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}
        _add_exp(exp_groups, MockTx(), item_det, "WOPAY")

        assert "WOPAY" in exp_groups
        assert exp_groups["WOPAY"]["exp_typ"] == "WOPAY"
        assert len(exp_groups["WOPAY"]["inv"]) == 1
        assert exp_groups["WOPAY"]["inv"][0]["inum"] == "SEZ001"


# ──────────────────────────────────────────────
# 8. NIL/EXEMPT/NONGST Structure Tests
# ──────────────────────────────────────────────

class TestNILStructure:
    def test_nil_official_structure(self, validator):
        """NIL must follow {inv: [{sply_ty, nil_amt, expt_amt, ngsup_amt}]}"""
        data = _minimal_gstr1(nil={
            "inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 0.0, "expt_amt": 0.0, "ngsup_amt": 0.0},
                {"sply_ty": "INTRAB2B", "nil_amt": 1000.0, "expt_amt": 0.0, "ngsup_amt": 0.0},
                {"sply_ty": "INTRB2C", "nil_amt": 0.0, "expt_amt": 500.0, "ngsup_amt": 0.0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0.0, "expt_amt": 0.0, "ngsup_amt": 200.0}
            ]
        })
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"NIL with inv array should be valid: {result.errors}"

    def test_nil_four_supply_types(self):
        """NIL output should always include all 4 supply types."""
        from app.services.gstr1_generator import _build_nil_output
        from collections import defaultdict

        nil_data = defaultdict(lambda: {"nil_amt": Decimal('0'), "expt_amt": Decimal('0'), "ngsup_amt": Decimal('0')})
        nil_data["INTRAB2C"]["nil_amt"] = Decimal('500')

        result = _build_nil_output(nil_data)
        assert "inv" in result
        assert len(result["inv"]) == 4
        sply_types = {entry["sply_ty"] for entry in result["inv"]}
        assert sply_types == {"INTRB2B", "INTRAB2B", "INTRB2C", "INTRAB2C"}

    def test_nil_invalid_supply_type(self, validator):
        """NIL sply_ty must be one of INTRB2B, INTRAB2B, INTRB2C, INTRAB2C."""
        data = _minimal_gstr1(nil={
            "inv": [{"sply_ty": "INVALID", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}]
        })
        result = validator.validate_gstr1_json(json.dumps(data))
        sply_errors = [e for e in result.errors if e.get("field") == "sply_ty"]
        assert len(sply_errors) > 0, "Invalid nil sply_ty should be rejected"

    def test_nil_requires_inv_array(self, validator):
        """NIL must have inv array (not flat nil_amt/expt_amt)."""
        data = _minimal_gstr1(nil={"nil_amt": 100, "expt_amt": 200, "ng_amt": 50})
        result = validator.validate_gstr1_json(json.dumps(data))
        inv_errors = [e for e in result.errors if e.get("field") == "inv"]
        assert len(inv_errors) > 0, "NIL without inv array should be rejected"


# ──────────────────────────────────────────────
# 9. HSN Pre-May-2025 Tests
# ──────────────────────────────────────────────

class TestHSNPreMay2025:
    def test_hsn_combined_mode(self, gst_rules):
        """Pre-May 2025 periods should use combined HSN reporting."""
        assert gst_rules["get_hsn_reporting_mode"]("042025") == "combined"
        assert gst_rules["get_hsn_reporting_mode"]("012025") == "combined"
        assert gst_rules["get_hsn_reporting_mode"]("122024") == "combined"

    def test_hsn_combined_structure(self, validator):
        """HSN data in combined mode has single data array."""
        data = _minimal_gstr1(hsn={
            "data": [{
                "num": 1,
                "hsn_sc": "6109",
                "desc": "T-shirts",
                "uqc": "NOS",
                "qty": 100.0,
                "val": 50000.0,
                "txval": 50000.0,
                "iamt": 0.0,
                "camt": 4500.0,
                "samt": 4500.0,
                "csamt": 0.0
            }]
        })
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"Combined HSN should be valid: {result.errors}"

    def test_hsn_combined_output(self):
        """Pre-May-2025 HSN should have data but no b2c_data."""
        from app.services.gstr1_generator import _build_hsn_output

        hsn_agg = {
            ("6109", "NOS", 18.0, "B2B"): {
                "hsn_sc": "6109", "desc": "T-shirts", "uqc": "NOS",
                "qty": Decimal('10'), "val": Decimal('5000'), "txval": Decimal('5000'),
                "iamt": Decimal('0'), "camt": Decimal('450'), "samt": Decimal('450'),
                "csamt": Decimal('0'), "rt": 18.0, "category": "B2B"
            },
            ("6109", "NOS", 18.0, "B2C"): {
                "hsn_sc": "6109", "desc": "T-shirts", "uqc": "NOS",
                "qty": Decimal('20'), "val": Decimal('10000'), "txval": Decimal('10000'),
                "iamt": Decimal('0'), "camt": Decimal('900'), "samt": Decimal('900'),
                "csamt": Decimal('0'), "rt": 18.0, "category": "B2C"
            }
        }

        result = _build_hsn_output(hsn_agg, "combined")
        assert "data" in result
        assert "b2c_data" not in result
        assert len(result["data"]) == 2  # Both B2B and B2C are in same list


# ──────────────────────────────────────────────
# 10. HSN May-2025+ Tests
# ──────────────────────────────────────────────

class TestHSNMay2025Plus:
    def test_hsn_separate_mode(self, gst_rules):
        """May 2025+ periods should use separate B2B/B2C HSN reporting."""
        assert gst_rules["get_hsn_reporting_mode"]("052025") == "separate_b2b_b2c"
        assert gst_rules["get_hsn_reporting_mode"]("062025") == "separate_b2b_b2c"
        assert gst_rules["get_hsn_reporting_mode"]("122025") == "separate_b2b_b2c"

    def test_hsn_separate_output(self):
        """May-2025+ HSN should have both data (B2B) and b2c_data."""
        from app.services.gstr1_generator import _build_hsn_output

        hsn_agg = {
            ("6109", "NOS", 18.0, "B2B"): {
                "hsn_sc": "6109", "desc": "T-shirts B2B", "uqc": "NOS",
                "qty": Decimal('10'), "val": Decimal('5000'), "txval": Decimal('5000'),
                "iamt": Decimal('900'), "camt": Decimal('0'), "samt": Decimal('0'),
                "csamt": Decimal('0'), "rt": 18.0, "category": "B2B"
            },
            ("6109", "NOS", 18.0, "B2C"): {
                "hsn_sc": "6109", "desc": "T-shirts B2C", "uqc": "NOS",
                "qty": Decimal('20'), "val": Decimal('10000'), "txval": Decimal('10000'),
                "iamt": Decimal('0'), "camt": Decimal('900'), "samt": Decimal('900'),
                "csamt": Decimal('0'), "rt": 18.0, "category": "B2C"
            }
        }

        result = _build_hsn_output(hsn_agg, "separate_b2b_b2c")
        assert "data" in result
        assert "b2c_data" in result
        assert len(result["data"]) == 1
        assert len(result["b2c_data"]) == 1
        assert result["data"][0]["desc"] == "T-shirts B2B"
        assert result["b2c_data"][0]["desc"] == "T-shirts B2C"

    def test_hsn_invalid_code_length(self, validator):
        """HSN codes must be 2, 4, 6, or 8 digits."""
        data = _minimal_gstr1(hsn={
            "data": [{
                "num": 1,
                "hsn_sc": "610",  # 3 digits - invalid
                "desc": "Invalid",
                "uqc": "NOS",
                "qty": 1.0, "val": 100.0, "txval": 100.0,
                "iamt": 0.0, "camt": 9.0, "samt": 9.0, "csamt": 0.0
            }]
        })
        result = validator.validate_gstr1_json(json.dumps(data))
        hsn_errors = [e for e in result.errors if e.get("field") == "hsn_sc"]
        assert len(hsn_errors) > 0, "3-digit HSN code should be rejected"


# ──────────────────────────────────────────────
# 11. Documents Issued (Table 13) Tests
# ──────────────────────────────────────────────

class TestDocIssue:
    def test_doc_issue_structure(self, validator):
        """doc_issue must follow {doc_det: [{doc_num, docs: [{num, from, to, totnum, cancel, net_issue}]}]}"""
        data = _minimal_gstr1(doc_issue={
            "doc_det": [{
                "doc_num": 1,
                "docs": [{
                    "num": 1,
                    "from": "INV001",
                    "to": "INV100",
                    "totnum": 100,
                    "cancel": 5,
                    "net_issue": 95
                }]
            }]
        })
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"doc_issue should be valid: {result.errors}"

    def test_doc_issue_built_from_invoices(self):
        """doc_issue should be built from actual tracked document numbers."""
        from app.services.gstr1_generator import _build_doc_issue

        inv_nums = ["INV001", "INV002", "INV003", "INV005"]
        cn_nums = ["CN001", "CN002"]
        dn_nums = ["DN001"]

        result = _build_doc_issue(inv_nums, cn_nums, dn_nums)
        assert "doc_det" in result
        assert len(result["doc_det"]) == 3  # invoices, CN, DN

        inv_doc = next(d for d in result["doc_det"] if d["doc_num"] == 1)
        assert inv_doc["docs"][0]["from"] == "INV001"
        assert inv_doc["docs"][0]["to"] == "INV005"
        assert inv_doc["docs"][0]["totnum"] == 4

    def test_doc_issue_empty_when_no_invoices(self):
        """doc_issue should have empty doc_det when no documents."""
        from app.services.gstr1_generator import _build_doc_issue
        result = _build_doc_issue([], [], [])
        assert result == {"doc_det": []}


# ──────────────────────────────────────────────
# 12. Amendment Structure Tests
# ──────────────────────────────────────────────

class TestAmendments:
    def test_b2ba_structure(self):
        """B2BA should include oinum, oidt (original) and inum, idt (revised)."""
        from app.services.gstr1_generator import _add_b2b_amendment

        class MockTx:
            customer_gstin = "29AALCS5765L1ZP"
            invoice_number = "REV001"
            invoice_date = None
            invoice_value = 1000
            invoice_type = "R"
            reverse_charge = "N"
            ecommerce_gstin = ""
            original_invoice_number = "INV001"
            original_invoice_date = None

        b2ba_groups = {}
        item_det = {"rt": 18.0, "txval": 1000.0, "iamt": 180.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}
        _add_b2b_amendment(b2ba_groups, MockTx(), item_det, "29")

        assert "29AALCS5765L1ZP" in b2ba_groups
        inv = b2ba_groups["29AALCS5765L1ZP"]["inv"][0]
        assert inv["oinum"] == "INV001"
        assert inv["inum"] == "REV001"
        assert "itms" in inv
        assert len(inv["itms"]) == 1

    def test_b2csa_structure(self):
        """B2CSA should include sply_ty, pos, typ, rt, txval, tax amounts."""
        from app.services.gstr1_generator import _add_b2cs_amendment

        class MockTx:
            tax_rate = 18
            igst_amount = 0
            cgst_amount = 90
            sgst_amount = 90
            cess_amount = 0

        b2csa_data = []
        _add_b2cs_amendment(b2csa_data, MockTx(), Decimal('1000'), "27", False, "27")

        assert len(b2csa_data) == 1
        entry = b2csa_data[0]
        assert entry["sply_ty"] == "INTRA"
        assert entry["pos"] == "27"
        assert entry["typ"] == "OE"
        assert entry["rt"] == 18.0

    def test_cdnra_structure(self):
        """CDNRA should include ctin/nt structure with ont_num, ont_dt."""
        from app.services.gstr1_generator import _add_cdnr_amendment

        class MockTx:
            customer_gstin = "29AALCS5765L1ZP"
            note_type = "CREDIT"
            note_number = "CN002"
            note_date = None
            invoice_number = "CN002"
            invoice_date = None
            invoice_value = 500
            reverse_charge = "N"
            original_invoice_number = "CN001"
            original_invoice_date = None

        cdnra_groups = {}
        item_det = {"rt": 18.0, "txval": 500.0, "iamt": 90.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}
        _add_cdnr_amendment(cdnra_groups, MockTx(), item_det, "29")

        assert "29AALCS5765L1ZP" in cdnra_groups
        nt = cdnra_groups["29AALCS5765L1ZP"]["nt"][0]
        assert nt["ont_num"] == "CN001"
        assert nt["nt_num"] == "CN002"
        assert nt["ntty"] == "C"

    def test_cdnura_structure(self):
        """CDNURA should include ont_num, ont_dt for original note reference."""
        from app.services.gstr1_generator import _add_cdnur_amendment

        class MockTx:
            note_type = "DEBIT"
            note_number = "DN002"
            note_date = None
            invoice_number = "DN002"
            invoice_date = None
            invoice_value = 300
            original_invoice_number = "DN001"
            original_invoice_date = None

        cdnura_data = []
        item_det = {"rt": 18.0, "txval": 300.0, "iamt": 54.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}
        _add_cdnur_amendment(cdnura_data, MockTx(), item_det, "29")

        assert len(cdnura_data) == 1
        nt = cdnura_data[0]
        assert nt["ont_num"] == "DN001"
        assert nt["nt_num"] == "DN002"
        assert nt["ntty"] == "D"

    def test_expa_structure(self):
        """EXPA should include oinum, oidt for original invoice reference."""
        from app.services.gstr1_generator import _add_exp_amendment

        class MockTx:
            invoice_number = "EXP002"
            invoice_date = None
            invoice_value = 15000
            original_invoice_number = "EXP001"
            original_invoice_date = None

        expa_groups = {}
        item_det = {"rt": 18.0, "txval": 15000.0, "iamt": 2700.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}
        _add_exp_amendment(expa_groups, MockTx(), item_det, "WPAY")

        assert "WPAY" in expa_groups
        inv = expa_groups["WPAY"]["inv"][0]
        assert inv["oinum"] == "EXP001"
        assert inv["inum"] == "EXP002"


# ──────────────────────────────────────────────
# 13. Validator Rejects Malformed Nesting
# ──────────────────────────────────────────────

class TestValidatorRejectsMalformed:
    def test_rejects_invalid_json_syntax(self, validator):
        """Validator should reject malformed JSON."""
        result = validator.validate_gstr1_json("{invalid json")
        assert not result.is_valid
        assert result.layer == "Syntax"

    def test_rejects_non_dict_root(self, validator):
        """Root must be an object, not an array."""
        result = validator.validate_gstr1_json("[]")
        assert not result.is_valid

    def test_rejects_missing_gstin(self, validator):
        """Missing gstin should be flagged."""
        result = validator.validate_gstr1_json(json.dumps({"fp": "012025"}))
        assert not result.is_valid
        assert any(e.get("field") == "gstin" for e in result.errors)

    def test_rejects_missing_fp(self, validator):
        """Missing fp should be flagged."""
        result = validator.validate_gstr1_json(json.dumps({"gstin": "27AABCU9603R1ZM"}))
        assert not result.is_valid
        assert any(e.get("field") == "fp" for e in result.errors)

    def test_rejects_invalid_gstin_format(self, validator):
        """Invalid GSTIN format should be rejected."""
        data = _minimal_gstr1(gstin="INVALID_GSTIN")
        result = validator.validate_gstr1_json(json.dumps(data))
        assert not result.is_valid

    def test_rejects_invalid_fp_format(self, validator):
        """Invalid return period format should be rejected."""
        data = _minimal_gstr1(fp="2025-01")
        result = validator.validate_gstr1_json(json.dumps(data))
        assert not result.is_valid

    def test_rejects_b2b_without_list(self, validator):
        """b2b must be a list."""
        data = _minimal_gstr1(b2b="not a list")
        result = validator.validate_gstr1_json(json.dumps(data))
        assert not result.is_valid

    def test_rejects_cdnr_flat_list(self, validator):
        """CDNR as flat list without ctin/nt nesting should be rejected."""
        data = _minimal_gstr1(cdnr=[{
            "ntty": "C",
            "nt_num": "CN001",
            "nt_dt": "20-01-2025",
            "val": 100.0,
            "pos": "29"
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        # Should have errors about missing ctin and nt
        structural_errors = [e for e in result.errors if e.get("field") in ("ctin", "nt")]
        assert len(structural_errors) > 0, "Flat CDNR without ctin/nt nesting should be rejected"

    def test_rejects_exp_without_exp_typ(self, validator):
        """EXP without exp_typ should be rejected."""
        data = _minimal_gstr1(exp=[{
            "inv": [{"inum": "EXP001", "idt": "05-01-2025", "val": 10000.0,
                     "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 10000.0, "iamt": 1800.0, "csamt": 0}}]}]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        exp_errors = [e for e in result.errors if e.get("field") == "exp_typ"]
        assert len(exp_errors) > 0

    def test_rejects_nil_without_inv(self, validator):
        """NIL without inv array should be rejected."""
        data = _minimal_gstr1(nil={"nil_amt": 100})
        result = validator.validate_gstr1_json(json.dumps(data))
        assert not result.is_valid

    def test_rejects_invalid_tax_rate(self, validator):
        """Invalid tax rates should be rejected."""
        data = _minimal_gstr1(b2b=[{
            "ctin": "29AALCS5765L1ZP",
            "inv": [{
                "inum": "INV001", "idt": "15-01-2025", "val": 1150.0, "pos": "29", "rchrg": "N", "inv_typ": "R",
                "itms": [{"num": 1, "itm_det": {"rt": 15.0, "txval": 1000.0, "iamt": 150.0, "camt": 0, "samt": 0, "csamt": 0}}]
            }]
        }])
        result = validator.validate_gstr1_json(json.dumps(data))
        rt_errors = [e for e in result.errors if e.get("field") == "rt"]
        assert len(rt_errors) > 0, "Invalid tax rate 15% should be rejected"


# ──────────────────────────────────────────────
# 14. Generator and Validator Agreement Tests
# ──────────────────────────────────────────────

class TestGeneratorValidatorAgreement:
    def test_valid_complete_structure(self, validator):
        """Complete GSTR-1 with all sections should pass validation."""
        data = {
            "gstin": "27AABCU9603R1ZM",
            "fp": "012025",
            "gt": 15000.0,
            "cur_gt": 15000.0,
            "b2b": [{
                "ctin": "29AALCS5765L1ZP",
                "inv": [{
                    "inum": "INV001", "idt": "15-01-2025", "val": 5900.0,
                    "pos": "29", "rchrg": "N", "inv_typ": "R",
                    "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 5000.0, "iamt": 900.0, "camt": 0, "samt": 0, "csamt": 0}}]
                }]
            }],
            "b2cs": [{"sply_ty": "INTRA", "pos": "27", "typ": "OE", "rt": 18.0, "txval": 3000.0, "iamt": 0, "camt": 270.0, "samt": 270.0, "csamt": 0}],
            "cdnr": [{
                "ctin": "29AALCS5765L1ZP",
                "nt": [{"ntty": "C", "nt_num": "CN001", "nt_dt": "20-01-2025", "inum": "INV001", "idt": "15-01-2025",
                        "val": 590.0, "pos": "29", "rchrg": "N", "inv_typ": "R",
                        "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 500.0, "iamt": 90.0, "camt": 0, "samt": 0, "csamt": 0}}]}]
            }],
            "exp": [{"exp_typ": "WPAY", "inv": [{"inum": "EXP001", "idt": "05-01-2025", "val": 5000.0, "sbpcode": "", "sbnum": "", "sbdt": "",
                     "itms": [{"num": 1, "itm_det": {"rt": 0.1, "txval": 5000.0, "iamt": 5.0, "csamt": 0}}]}]}],
            "nil": {"inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}
            ]},
            "hsn": {"data": [
                {"num": 1, "hsn_sc": "6109", "desc": "T-shirts", "uqc": "NOS", "qty": 100.0,
                 "val": 15000.0, "txval": 13500.0, "iamt": 905.0, "camt": 270.0, "samt": 270.0, "csamt": 0.0}
            ]},
            "doc_issue": {"doc_det": [
                {"doc_num": 1, "docs": [{"num": 1, "from": "INV001", "to": "INV001", "totnum": 1, "cancel": 0, "net_issue": 1}]}
            ]}
        }
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"Complete GSTR-1 should pass validation: {result.errors}"

    def test_json_roundtrip(self, json_writer, validator, tmp_path):
        """Generated JSON should round-trip through writer and validator."""
        data = _minimal_gstr1(
            b2b=[{
                "ctin": "29AALCS5765L1ZP",
                "inv": [{
                    "inum": "RT001", "idt": "10-01-2025", "val": 1180.0,
                    "pos": "29", "rchrg": "N", "inv_typ": "R",
                    "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 1000.0, "iamt": 180.0, "camt": 0, "samt": 0, "csamt": 0}}]
                }]
            }],
            nil={"inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}
            ]}
        )

        json_path = str(tmp_path / "test_roundtrip.json")
        json_writer.generate_json(data, json_path)

        with open(json_path, "r") as f:
            json_str = f.read()

        result = validator.validate_gstr1_json(json_str)
        assert result.is_valid, f"Roundtrip JSON should pass validation: {result.errors}"


# ──────────────────────────────────────────────
# 15. JSON and Excel Consistency Tests
# ──────────────────────────────────────────────

class TestJSONExcelConsistency:
    def test_b2b_json_excel_match(self, transform_for_excel):
        """B2B data in JSON and Excel should represent the same values."""
        json_data = _minimal_gstr1(b2b=[{
            "ctin": "29AALCS5765L1ZP",
            "inv": [{
                "inum": "INV001", "idt": "15-01-2025", "val": 1180.0,
                "pos": "29", "rchrg": "N", "inv_typ": "R", "ecom_gstin": "",
                "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 1000.0, "iamt": 180.0, "camt": 0, "samt": 0, "csamt": 0}}]
            }]
        }])

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["b2b"]) == 1
        row = excel_data["b2b"][0]
        assert row[0] == "29AALCS5765L1ZP"  # ctin
        assert row[1] == "INV001"           # inum
        assert row[9] == 18.0               # rt
        assert row[10] == 1000.0            # txval

    def test_cdnr_json_excel_match(self, transform_for_excel):
        """CDNR data in JSON and Excel should represent the same values."""
        json_data = _minimal_gstr1(cdnr=[{
            "ctin": "29AALCS5765L1ZP",
            "nt": [{
                "ntty": "C", "nt_num": "CN001", "nt_dt": "20-01-2025",
                "inum": "INV001", "idt": "15-01-2025", "val": 590.0, "pos": "29",
                "rchrg": "N", "inv_typ": "R",
                "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 500.0, "iamt": 90.0, "camt": 0, "samt": 0, "csamt": 0}}]
            }]
        }])

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["cdnr"]) == 1
        row = excel_data["cdnr"][0]
        assert row[0] == "29AALCS5765L1ZP"  # ctin
        assert row[1] == "CN001"            # nt_num
        assert row[9] == "C"                # ntty

    def test_exp_json_excel_match(self, transform_for_excel):
        """EXP data in JSON and Excel should represent the same values."""
        json_data = _minimal_gstr1(exp=[{
            "exp_typ": "WPAY",
            "inv": [{
                "inum": "EXP001", "idt": "05-01-2025", "val": 10000.0,
                "sbpcode": "INMAA1", "sbnum": "SB001", "sbdt": "10-01-2025",
                "itms": [{"num": 1, "itm_det": {"rt": 0.1, "txval": 10000.0, "iamt": 10.0, "csamt": 0}}]
            }]
        }])

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["exp"]) == 1
        row = excel_data["exp"][0]
        assert row[0] == "WPAY"     # exp_typ
        assert row[1] == "EXP001"   # inum

    def test_nil_json_excel_match(self, transform_for_excel):
        """NIL data in JSON and Excel should represent the same values."""
        json_data = _minimal_gstr1(nil={
            "inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 100.0, "expt_amt": 0.0, "ngsup_amt": 0.0},
                {"sply_ty": "INTRAB2B", "nil_amt": 0.0, "expt_amt": 200.0, "ngsup_amt": 0.0},
                {"sply_ty": "INTRB2C", "nil_amt": 0.0, "expt_amt": 0.0, "ngsup_amt": 300.0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0.0, "expt_amt": 0.0, "ngsup_amt": 0.0}
            ]
        })

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["nil"]) == 4
        assert excel_data["nil"][0][0] == "INTRB2B"
        assert excel_data["nil"][0][1] == 100.0
        assert excel_data["nil"][1][2] == 200.0
        assert excel_data["nil"][2][3] == 300.0

    def test_hsn_b2c_json_excel_match(self, transform_for_excel):
        """HSN B2C data should populate hsnb2c Excel rows for May 2025+."""
        json_data = _minimal_gstr1(hsn={
            "data": [{"num": 1, "hsn_sc": "6109", "desc": "B2B", "uqc": "NOS", "qty": 10, "val": 5000,
                      "txval": 5000, "iamt": 900, "camt": 0, "samt": 0, "csamt": 0}],
            "b2c_data": [{"num": 1, "hsn_sc": "6109", "desc": "B2C", "uqc": "NOS", "qty": 20, "val": 10000,
                          "txval": 10000, "iamt": 0, "camt": 900, "samt": 900, "csamt": 0}]
        })

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["hsn"]) == 1
        assert len(excel_data["hsnb2c"]) == 1
        assert excel_data["hsn"][0][2] == "NOS"
        assert excel_data["hsnb2c"][0][2] == "NOS"

    def test_doc_issue_json_excel_match(self, transform_for_excel):
        """Doc issue data should match between JSON and Excel."""
        json_data = _minimal_gstr1(doc_issue={
            "doc_det": [{
                "doc_num": 1,
                "docs": [{"num": 1, "from": "INV001", "to": "INV050", "totnum": 50, "cancel": 2, "net_issue": 48}]
            }]
        })

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["docs"]) == 1
        row = excel_data["docs"][0]
        assert row[0] == 1           # doc_num
        assert row[1] == "INV001"    # from
        assert row[2] == "INV050"    # to
        assert row[3] == 50          # totnum
        assert row[4] == 2           # cancel

    def test_excel_generation_produces_correct_sheets(self, excel_writer, transform_for_excel, tmp_path):
        """Generated Excel should have sheets matching JSON data."""
        json_data = {
            "gstin": "27AABCU9603R1ZM",
            "fp": "012025",
            "b2b": [{
                "ctin": "29AALCS5765L1ZP",
                "inv": [{"inum": "INV001", "idt": "15-01-2025", "val": 1180.0, "pos": "29", "rchrg": "N", "inv_typ": "R", "ecom_gstin": "",
                         "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 1000.0, "iamt": 180.0, "camt": 0, "samt": 0, "csamt": 0}}]}]
            }],
            "b2cs": [{"sply_ty": "INTRA", "pos": "27", "typ": "OE", "rt": 18.0, "txval": 5000.0, "iamt": 0, "camt": 450, "samt": 450, "csamt": 0}],
            "cdnr": [{"ctin": "29AALCS5765L1ZP", "nt": [{"ntty": "C", "nt_num": "CN001", "nt_dt": "20-01-2025", "inum": "INV001", "idt": "15-01-2025",
                      "val": 590.0, "pos": "29", "rchrg": "N",
                      "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 500.0, "iamt": 90.0, "camt": 0, "samt": 0, "csamt": 0}}]}]}],
            "nil": {"inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}
            ]},
            "hsn": {"data": [{"num": 1, "hsn_sc": "6109", "desc": "T-shirts", "uqc": "NOS", "qty": 100, "val": 6000,
                             "txval": 6000, "iamt": 180, "camt": 450, "samt": 450, "csamt": 0}]},
            "doc_issue": {"doc_det": [{"doc_num": 1, "docs": [{"num": 1, "from": "INV001", "to": "INV001", "totnum": 1, "cancel": 0, "net_issue": 1}]}]}
        }

        excel_data = transform_for_excel(json_data)
        excel_path = str(tmp_path / "test_sheets.xlsx")
        excel_writer.generate_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        assert "b2b" in wb.sheetnames
        assert "b2cs" in wb.sheetnames
        assert "cdnr" in wb.sheetnames
        assert "nil" in wb.sheetnames
        assert "hsn" in wb.sheetnames
        assert "docs" in wb.sheetnames

        # Verify header of b2b sheet
        ws = wb["b2b"]
        assert ws.cell(1, 1).value == "GSTIN/UIN of Recipient"


# ──────────────────────────────────────────────
# 16. Edge Cases
# ──────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_gstr1_valid(self, validator):
        """Empty GSTR-1 with just gstin and fp should be valid."""
        data = {"gstin": "27AABCU9603R1ZM", "fp": "012025"}
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"Empty GSTR-1 should be valid: {result.errors}"

    def test_empty_tables_valid(self, validator):
        """GSTR-1 with empty arrays should be valid."""
        data = _minimal_gstr1()
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"Empty tables should be valid: {result.errors}"

    def test_hsn_cross_table_warning(self, validator):
        """Large HSN vs invoice total discrepancy should produce warning."""
        data = _minimal_gstr1(
            b2b=[{
                "ctin": "29AALCS5765L1ZP",
                "inv": [{"inum": "INV001", "idt": "15-01-2025", "val": 11800.0, "pos": "29", "rchrg": "N", "inv_typ": "R",
                         "itms": [{"num": 1, "itm_det": {"rt": 18.0, "txval": 10000.0, "iamt": 1800.0, "camt": 0, "samt": 0, "csamt": 0}}]}]
            }],
            hsn={"data": [{"num": 1, "hsn_sc": "6109", "desc": "T-shirts", "uqc": "NOS", "qty": 10,
                          "val": 500.0, "txval": 500.0, "iamt": 90.0, "camt": 0, "samt": 0, "csamt": 0}]},
            nil={"inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}
            ]}
        )
        result = validator.validate_gstr1_json(json.dumps(data))
        # Should be valid but with warnings
        assert result.is_valid, f"Should be valid with HSN warning: {result.errors}"
        assert len(result.warnings) > 0, "Should have HSN reconciliation warning"

    def test_nil_all_zeros_valid(self, validator):
        """NIL with all zeros should be valid."""
        data = _minimal_gstr1(nil={
            "inv": [
                {"sply_ty": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
                {"sply_ty": "INTRAB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}
            ]
        })
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid

    def test_doc_issue_empty_valid(self, validator):
        """Empty doc_issue should be valid."""
        data = _minimal_gstr1(doc_issue={"doc_det": []})
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid

    def test_empty_gstin_accepted(self, validator):
        """Empty GSTIN is accepted (for empty returns)."""
        data = {"gstin": "", "fp": "012025"}
        result = validator.validate_gstr1_json(json.dumps(data))
        assert result.is_valid, f"Empty GSTIN should be accepted: {result.errors}"

    def test_period_rules_pre_may_2025(self, gst_rules):
        """Pre-May 2025 should have combined HSN mode."""
        rules = gst_rules["get_rules_for_period"]("012025")
        assert rules["hsn_reporting_mode"] == "combined"
        assert rules["b2cl_applicable"] is False  # Jan 2025 is after Aug 2024

    def test_period_rules_may_2025(self, gst_rules):
        """May 2025 should have separate HSN mode."""
        rules = gst_rules["get_rules_for_period"]("052025")
        assert rules["hsn_reporting_mode"] == "separate_b2b_b2c"
        assert rules["b2cl_applicable"] is False

    def test_transform_handles_empty_json(self, transform_for_excel):
        """transform_for_excel should handle empty JSON gracefully."""
        json_data = {"gstin": "", "fp": "012025"}
        excel_data = transform_for_excel(json_data)
        assert excel_data["b2b"] == []
        assert excel_data["b2cs"] == []
        assert excel_data["cdnr"] == []

    def test_transform_handles_nil_backward_compat(self, transform_for_excel):
        """transform_for_excel should handle old flat nil structure for backward compat."""
        json_data = _minimal_gstr1(nil={"nil_amt": 100, "expt_amt": 200, "ng_amt": 50})
        excel_data = transform_for_excel(json_data)
        assert len(excel_data["nil"]) == 1
        assert excel_data["nil"][0][1] == 100
