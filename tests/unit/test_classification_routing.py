"""
CRIT-02 Regression Tests: Classification Routing Verification.

Every affected classification variant must:
1. Route to the correct GSTR-1 table.
2. Produce actual JSON objects (not just totals).
3. Never silently disappear from output.
4. Not duplicate into unrelated tables.

Tests cover:
  B2BA → b2ba table
  B2CSA → b2csa table
  CDNRA → cdnra table
  CDNURA → cdnura table
  EXPA → expa table
  SEZ_REGISTERED → exp table (WOPAY)
  SEZ_UNREGISTERED → exp table (WOPAY)
  NIL_REGISTERED → nil table (nil_amt bucket)
  NIL_UNREGISTERED → nil table (nil_amt bucket)
  EXEMPT_REGISTERED → nil table (expt_amt bucket)
  EXEMPT_UNREGISTERED → nil table (expt_amt bucket)
  NONGST_REGISTERED → nil table (ngsup_amt bucket)
  NONGST_UNREGISTERED → nil table (ngsup_amt bucket)

Also includes:
  - Base classification regression (B2B, B2CS, CDNR, CDNUR, EXPORT, SEZ, NIL, EXEMPT, NONGST)
  - Period-specific amendment routing (B2CLA pre/post Aug 2024)
  - No-silent-drop sweep
  - No-duplicate-routing sweep
"""
import pytest
from decimal import Decimal


# ──────────────────────────────────────────────
# MockTx factory — minimal transaction object
# ──────────────────────────────────────────────

def _make_tx(
    classification,
    invoice_number="INV001",
    invoice_date=None,
    invoice_value=Decimal('1180'),
    taxable_value=Decimal('1000'),
    total_tax=Decimal('180'),
    tax_rate=Decimal('18'),
    igst_amount=Decimal('180'),
    cgst_amount=Decimal('0'),
    sgst_amount=Decimal('0'),
    cess_amount=Decimal('0'),
    customer_gstin=None,
    place_of_supply="29",
    amendment_flag=False,
    note_type=None,
    note_number=None,
    note_date=None,
    original_invoice_number=None,
    original_invoice_date=None,
    invoice_type="R",
    reverse_charge="N",
    ecommerce_gstin="",
    hsn_sac="6109",
    uqc="NOS",
    quantity=Decimal('10'),
    description="Test item",
    supply_type=None,
    gstr1_table=None,
):
    """Create a mock transaction object with the given classification."""
    class MockTx:
        pass

    tx = MockTx()
    tx.classification_status = classification
    tx.gstr1_table = gstr1_table
    tx.supply_type = supply_type
    tx.invoice_number = invoice_number
    tx.invoice_date = invoice_date
    tx.invoice_value = invoice_value
    tx.taxable_value = taxable_value
    tx.total_tax = total_tax
    tx.tax_rate = tax_rate
    tx.igst_amount = igst_amount
    tx.cgst_amount = cgst_amount
    tx.sgst_amount = sgst_amount
    tx.cess_amount = cess_amount
    tx.customer_gstin = customer_gstin
    tx.place_of_supply = place_of_supply
    tx.amendment_flag = amendment_flag
    tx.note_type = note_type
    tx.note_number = note_number
    tx.note_date = note_date
    tx.original_invoice_number = original_invoice_number
    tx.original_invoice_date = original_invoice_date
    tx.invoice_type = invoice_type
    tx.reverse_charge = reverse_charge
    tx.ecommerce_gstin = ecommerce_gstin
    tx.hsn_sac = hsn_sac
    tx.uqc = uqc
    tx.quantity = quantity
    tx.description = description
    return tx


class MockProfile:
    gstin = "27AABCU9603R1ZM"
    state_code = "27"


RETURN_PERIOD = "012025"       # Post-Aug 2024 (no B2CL)
RETURN_PERIOD_PRE_AUG = "072024"  # Pre-Aug 2024 (B2CL applicable)


# ──────────────────────────────────────────────
# Helper: run _build_gstr1_json with a single tx
# ──────────────────────────────────────────────

def _generate(tx, return_period=RETURN_PERIOD):
    from app.services.gstr1_generator import _build_gstr1_json
    return _build_gstr1_json(MockProfile(), return_period, [tx])


# ══════════════════════════════════════════════
# 1. Amendment classifications
# ══════════════════════════════════════════════

class TestB2BARouting:
    """B2BA classification must route to the b2ba amendment table."""

    def test_b2ba_routes_to_b2ba_table(self):
        tx = _make_tx("B2BA",
                       customer_gstin="29AALCS5765L1ZP",
                       original_invoice_number="INV-ORIG",
                       invoice_number="INV-REV")
        result = _generate(tx)
        assert "b2ba" in result, "B2BA must produce a b2ba table"
        assert len(result["b2ba"]) > 0
        inv = result["b2ba"][0]["inv"][0]
        assert inv["oinum"] == "INV-ORIG"
        assert inv["inum"] == "INV-REV"
        assert inv["itms"][0]["itm_det"]["txval"] == 1000.0

    def test_b2ba_does_not_appear_in_b2b(self):
        tx = _make_tx("B2BA",
                       customer_gstin="29AALCS5765L1ZP",
                       original_invoice_number="INV-ORIG")
        result = _generate(tx)
        assert len(result.get("b2b", [])) == 0, "B2BA must NOT appear in b2b"

    def test_b2ba_not_silently_dropped(self):
        tx = _make_tx("B2BA",
                       customer_gstin="29AALCS5765L1ZP",
                       original_invoice_number="INV-ORIG")
        result = _generate(tx)
        assert "b2ba" in result, "B2BA must not be silently dropped"


class TestB2CSARouting:
    """B2CSA classification must route to the b2csa amendment table."""

    def test_b2csa_routes_to_b2csa_table(self):
        tx = _make_tx("B2CSA",
                       igst_amount=Decimal('0'),
                       cgst_amount=Decimal('90'),
                       sgst_amount=Decimal('90'),
                       place_of_supply="27")
        result = _generate(tx)
        assert "b2csa" in result, "B2CSA must produce a b2csa table"
        assert len(result["b2csa"]) > 0
        entry = result["b2csa"][0]
        assert entry["sply_ty"] == "INTRA"
        assert entry["txval"] == 1000.0

    def test_b2csa_does_not_appear_in_b2cs(self):
        tx = _make_tx("B2CSA", place_of_supply="27",
                       igst_amount=Decimal('0'), cgst_amount=Decimal('90'), sgst_amount=Decimal('90'))
        result = _generate(tx)
        # b2cs may have empty aggregates or none at all
        b2cs_txval = sum(e.get("txval", 0) for e in result.get("b2cs", []))
        assert b2cs_txval == 0, "B2CSA must NOT contribute to b2cs"


class TestCDNRARouting:
    """CDNRA classification must route to the cdnra amendment table."""

    def test_cdnra_routes_to_cdnra_table(self):
        tx = _make_tx("CDNRA",
                       customer_gstin="29AALCS5765L1ZP",
                       note_type="CREDIT",
                       note_number="CN-REV",
                       original_invoice_number="CN-ORIG")
        result = _generate(tx)
        assert "cdnra" in result, "CDNRA must produce a cdnra table"
        assert len(result["cdnra"]) > 0
        nt = result["cdnra"][0]["nt"][0]
        assert nt["ont_num"] == "CN-ORIG"
        assert nt["nt_num"] == "CN-REV"
        assert nt["ntty"] == "C"

    def test_cdnra_does_not_appear_in_cdnr(self):
        tx = _make_tx("CDNRA",
                       customer_gstin="29AALCS5765L1ZP",
                       note_type="CREDIT",
                       note_number="CN-REV",
                       original_invoice_number="CN-ORIG")
        result = _generate(tx)
        assert len(result.get("cdnr", [])) == 0, "CDNRA must NOT appear in cdnr"


class TestCDNURARouting:
    """CDNURA classification must route to the cdnura amendment table."""

    def test_cdnura_routes_to_cdnura_table(self):
        tx = _make_tx("CDNURA",
                       note_type="DEBIT",
                       note_number="DN-REV",
                       original_invoice_number="DN-ORIG")
        result = _generate(tx)
        assert "cdnura" in result, "CDNURA must produce a cdnura table"
        assert len(result["cdnura"]) > 0
        nt = result["cdnura"][0]
        assert nt["ont_num"] == "DN-ORIG"
        assert nt["nt_num"] == "DN-REV"
        assert nt["ntty"] == "D"

    def test_cdnura_does_not_appear_in_cdnur(self):
        tx = _make_tx("CDNURA",
                       note_type="DEBIT",
                       note_number="DN-REV",
                       original_invoice_number="DN-ORIG")
        result = _generate(tx)
        assert len(result.get("cdnur", [])) == 0, "CDNURA must NOT appear in cdnur"


class TestEXPARouting:
    """EXPA classification must route to the expa amendment table."""

    def test_expa_routes_to_expa_table(self):
        tx = _make_tx("EXPA",
                       invoice_number="EXP-REV",
                       original_invoice_number="EXP-ORIG")
        result = _generate(tx)
        assert "expa" in result, "EXPA must produce an expa table"
        assert len(result["expa"]) > 0
        inv = result["expa"][0]["inv"][0]
        assert inv["oinum"] == "EXP-ORIG"
        assert inv["inum"] == "EXP-REV"

    def test_expa_does_not_appear_in_exp(self):
        tx = _make_tx("EXPA",
                       invoice_number="EXP-REV",
                       original_invoice_number="EXP-ORIG")
        result = _generate(tx)
        assert len(result.get("exp", [])) == 0, "EXPA must NOT appear in exp"


# ══════════════════════════════════════════════
# 2. SEZ classifications
# ══════════════════════════════════════════════

class TestSEZRegisteredRouting:
    """SEZ_REGISTERED must route to the exp table as WOPAY."""

    def test_sez_registered_routes_to_exp(self):
        tx = _make_tx("SEZ_REGISTERED",
                       customer_gstin="29AALCS5765L1ZP",
                       invoice_number="SEZ-R001")
        result = _generate(tx)
        assert len(result.get("exp", [])) > 0, "SEZ_REGISTERED must produce an exp table entry"
        exp_entry = result["exp"][0]
        assert exp_entry["exp_typ"] == "WOPAY"
        assert any(inv["inum"] == "SEZ-R001" for inv in exp_entry["inv"])

    def test_sez_registered_not_dropped(self):
        tx = _make_tx("SEZ_REGISTERED", customer_gstin="29AALCS5765L1ZP")
        result = _generate(tx)
        has_exp = len(result.get("exp", [])) > 0
        assert has_exp, "SEZ_REGISTERED must not be silently dropped"


class TestSEZUnregisteredRouting:
    """SEZ_UNREGISTERED must route to the exp table as WOPAY."""

    def test_sez_unregistered_routes_to_exp(self):
        tx = _make_tx("SEZ_UNREGISTERED", invoice_number="SEZ-U001")
        result = _generate(tx)
        assert len(result.get("exp", [])) > 0, "SEZ_UNREGISTERED must produce an exp table entry"
        exp_entry = result["exp"][0]
        assert exp_entry["exp_typ"] == "WOPAY"
        assert any(inv["inum"] == "SEZ-U001" for inv in exp_entry["inv"])


# ══════════════════════════════════════════════
# 3. NIL/Exempt/Non-GST classifications
# ══════════════════════════════════════════════

class TestNILRegisteredRouting:
    """NIL_REGISTERED must route to nil table, nil_amt bucket, as B2B."""

    def test_nil_registered_routes_to_nil_amt(self):
        tx = _make_tx("NIL_REGISTERED",
                       customer_gstin="29AALCS5765L1ZP",
                       tax_rate=Decimal('0'),
                       igst_amount=Decimal('0'),
                       total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        # Inter-state + registered = INTRB2B
        intrb2b = next(e for e in nil_inv if e["sply_ty"] == "INTRB2B")
        assert intrb2b["nil_amt"] == 1000.0, "NIL_REGISTERED must populate nil_amt"
        assert intrb2b["expt_amt"] == 0.0
        assert intrb2b["ngsup_amt"] == 0.0


class TestNILUnregisteredRouting:
    """NIL_UNREGISTERED must route to nil table, nil_amt bucket, as B2C."""

    def test_nil_unregistered_routes_to_nil_amt(self):
        tx = _make_tx("NIL_UNREGISTERED",
                       tax_rate=Decimal('0'),
                       igst_amount=Decimal('0'),
                       total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        # Inter-state + no GSTIN = INTRB2C
        intrb2c = next(e for e in nil_inv if e["sply_ty"] == "INTRB2C")
        assert intrb2c["nil_amt"] == 1000.0, "NIL_UNREGISTERED must populate nil_amt"


class TestExemptRegisteredRouting:
    """EXEMPT_REGISTERED must route to nil table, expt_amt bucket."""

    def test_exempt_registered_routes_to_expt_amt(self):
        tx = _make_tx("EXEMPT_REGISTERED",
                       customer_gstin="29AALCS5765L1ZP",
                       tax_rate=Decimal('0'),
                       igst_amount=Decimal('0'),
                       total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        intrb2b = next(e for e in nil_inv if e["sply_ty"] == "INTRB2B")
        assert intrb2b["expt_amt"] == 1000.0, "EXEMPT_REGISTERED must populate expt_amt"
        assert intrb2b["nil_amt"] == 0.0
        assert intrb2b["ngsup_amt"] == 0.0


class TestExemptUnregisteredRouting:
    """EXEMPT_UNREGISTERED must route to nil table, expt_amt bucket."""

    def test_exempt_unregistered_routes_to_expt_amt(self):
        tx = _make_tx("EXEMPT_UNREGISTERED",
                       tax_rate=Decimal('0'),
                       igst_amount=Decimal('0'),
                       total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        intrb2c = next(e for e in nil_inv if e["sply_ty"] == "INTRB2C")
        assert intrb2c["expt_amt"] == 1000.0, "EXEMPT_UNREGISTERED must populate expt_amt"


class TestNongstRegisteredRouting:
    """NONGST_REGISTERED must route to nil table, ngsup_amt bucket."""

    def test_nongst_registered_routes_to_ngsup_amt(self):
        tx = _make_tx("NONGST_REGISTERED",
                       customer_gstin="29AALCS5765L1ZP",
                       tax_rate=Decimal('0'),
                       igst_amount=Decimal('0'),
                       total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        intrb2b = next(e for e in nil_inv if e["sply_ty"] == "INTRB2B")
        assert intrb2b["ngsup_amt"] == 1000.0, "NONGST_REGISTERED must populate ngsup_amt"
        assert intrb2b["nil_amt"] == 0.0
        assert intrb2b["expt_amt"] == 0.0


class TestNongstUnregisteredRouting:
    """NONGST_UNREGISTERED must route to nil table, ngsup_amt bucket."""

    def test_nongst_unregistered_routes_to_ngsup_amt(self):
        tx = _make_tx("NONGST_UNREGISTERED",
                       tax_rate=Decimal('0'),
                       igst_amount=Decimal('0'),
                       total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        intrb2c = next(e for e in nil_inv if e["sply_ty"] == "INTRB2C")
        assert intrb2c["ngsup_amt"] == 1000.0, "NONGST_UNREGISTERED must populate ngsup_amt"


# ══════════════════════════════════════════════
# 4. Base classification regression
# ══════════════════════════════════════════════

class TestBaseClassificationRegression:
    """Ensure base classifications still work after CRIT-02 normalization."""

    def test_b2b_still_routes_correctly(self):
        tx = _make_tx("B2B", customer_gstin="29AALCS5765L1ZP")
        result = _generate(tx)
        assert len(result["b2b"]) > 0

    def test_b2cs_still_routes_correctly(self):
        tx = _make_tx("B2CS", place_of_supply="27",
                       igst_amount=Decimal('0'), cgst_amount=Decimal('90'), sgst_amount=Decimal('90'))
        result = _generate(tx)
        assert len(result["b2cs"]) > 0

    def test_cdnr_still_routes_correctly(self):
        tx = _make_tx("CDNR",
                       customer_gstin="29AALCS5765L1ZP",
                       note_type="CREDIT", note_number="CN001")
        result = _generate(tx)
        assert len(result["cdnr"]) > 0

    def test_cdnur_still_routes_correctly(self):
        tx = _make_tx("CDNUR", note_type="CREDIT", note_number="CN001")
        result = _generate(tx)
        assert len(result["cdnur"]) > 0

    def test_export_still_routes_correctly(self):
        tx = _make_tx("EXPORT")
        result = _generate(tx)
        assert len(result["exp"]) > 0
        assert result["exp"][0]["exp_typ"] == "WPAY"

    def test_sez_still_routes_correctly(self):
        tx = _make_tx("SEZ")
        result = _generate(tx)
        assert len(result["exp"]) > 0
        assert result["exp"][0]["exp_typ"] == "WOPAY"

    def test_nil_still_routes_correctly(self):
        tx = _make_tx("NIL", tax_rate=Decimal('0'), igst_amount=Decimal('0'), total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        total_nil = sum(e["nil_amt"] for e in nil_inv)
        assert total_nil == 1000.0

    def test_exempt_still_routes_correctly(self):
        tx = _make_tx("EXEMPT", tax_rate=Decimal('0'), igst_amount=Decimal('0'), total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        total_exempt = sum(e["expt_amt"] for e in nil_inv)
        assert total_exempt == 1000.0

    def test_nongst_still_routes_correctly(self):
        tx = _make_tx("NONGST", tax_rate=Decimal('0'), igst_amount=Decimal('0'), total_tax=Decimal('0'))
        result = _generate(tx)
        nil_inv = result["nil"]["inv"]
        total_nongst = sum(e["ngsup_amt"] for e in nil_inv)
        assert total_nongst == 1000.0


# ══════════════════════════════════════════════
# 5. Period-specific amendment routing
# ══════════════════════════════════════════════

class TestPeriodSpecificAmendments:
    """B2CLA routing depends on period: pre-Aug 2024 → b2cla, post → b2csa."""

    def test_b2cla_pre_aug_routes_to_b2cla(self):
        tx = _make_tx("B2CLA", place_of_supply="29",
                       original_invoice_number="INV-ORIG",
                       invoice_number="INV-REV",
                       invoice_value=Decimal('354000'),
                       taxable_value=Decimal('300000'))
        result = _generate(tx, return_period=RETURN_PERIOD_PRE_AUG)
        assert "b2cla" in result
        assert len(result["b2cla"]) > 0

    def test_b2cla_post_aug_routes_to_b2csa(self):
        tx = _make_tx("B2CLA", place_of_supply="29",
                       original_invoice_number="INV-ORIG",
                       invoice_number="INV-REV")
        result = _generate(tx, return_period=RETURN_PERIOD)
        assert "b2csa" in result
        assert len(result["b2csa"]) > 0


# ══════════════════════════════════════════════
# 6. No-silent-drop sweep
# ══════════════════════════════════════════════

class TestNoSilentDrop:
    """Every affected classification must produce at least one output entry."""

    ALL_AFFECTED = [
        ("B2BA",   {"customer_gstin": "29AALCS5765L1ZP", "original_invoice_number": "ORIG"}),
        ("B2CSA",  {"place_of_supply": "27", "igst_amount": Decimal('0'), "cgst_amount": Decimal('90'), "sgst_amount": Decimal('90')}),
        ("CDNRA",  {"customer_gstin": "29AALCS5765L1ZP", "note_type": "CREDIT", "note_number": "CN", "original_invoice_number": "ORIG"}),
        ("CDNURA", {"note_type": "DEBIT", "note_number": "DN", "original_invoice_number": "ORIG"}),
        ("EXPA",   {"original_invoice_number": "ORIG"}),
        ("SEZ_REGISTERED",   {"customer_gstin": "29AALCS5765L1ZP"}),
        ("SEZ_UNREGISTERED", {}),
        ("NIL_REGISTERED",       {"customer_gstin": "29AALCS5765L1ZP", "tax_rate": Decimal('0'), "igst_amount": Decimal('0'), "total_tax": Decimal('0')}),
        ("NIL_UNREGISTERED",     {"tax_rate": Decimal('0'), "igst_amount": Decimal('0'), "total_tax": Decimal('0')}),
        ("EXEMPT_REGISTERED",    {"customer_gstin": "29AALCS5765L1ZP", "tax_rate": Decimal('0'), "igst_amount": Decimal('0'), "total_tax": Decimal('0')}),
        ("EXEMPT_UNREGISTERED",  {"tax_rate": Decimal('0'), "igst_amount": Decimal('0'), "total_tax": Decimal('0')}),
        ("NONGST_REGISTERED",    {"customer_gstin": "29AALCS5765L1ZP", "tax_rate": Decimal('0'), "igst_amount": Decimal('0'), "total_tax": Decimal('0')}),
        ("NONGST_UNREGISTERED",  {"tax_rate": Decimal('0'), "igst_amount": Decimal('0'), "total_tax": Decimal('0')}),
    ]

    # Expected output table/key for each classification
    EXPECTED_TABLE = {
        "B2BA":   "b2ba",
        "B2CSA":  "b2csa",
        "CDNRA":  "cdnra",
        "CDNURA": "cdnura",
        "EXPA":   "expa",
        "SEZ_REGISTERED":   "exp",
        "SEZ_UNREGISTERED": "exp",
        # NIL/EXEMPT/NONGST all go to "nil" structure
        "NIL_REGISTERED":       "nil",
        "NIL_UNREGISTERED":     "nil",
        "EXEMPT_REGISTERED":    "nil",
        "EXEMPT_UNREGISTERED":  "nil",
        "NONGST_REGISTERED":    "nil",
        "NONGST_UNREGISTERED":  "nil",
    }

    @pytest.mark.parametrize("classification,extra_kwargs", ALL_AFFECTED,
                             ids=[c for c, _ in ALL_AFFECTED])
    def test_classification_not_dropped(self, classification, extra_kwargs):
        tx = _make_tx(classification, **extra_kwargs)
        result = _generate(tx)

        expected_key = self.EXPECTED_TABLE[classification]

        if expected_key == "nil":
            # NIL table is always present; verify non-zero amounts
            nil_inv = result["nil"]["inv"]
            total = sum(
                e["nil_amt"] + e["expt_amt"] + e["ngsup_amt"]
                for e in nil_inv
            )
            assert total > 0, f"{classification} must produce non-zero nil amounts"
        else:
            assert expected_key in result, f"{classification} must produce a {expected_key} table"
            table_data = result[expected_key]
            assert len(table_data) > 0, f"{classification} must have entries in {expected_key}"


# ══════════════════════════════════════════════
# 7. No-duplicate-routing sweep
# ══════════════════════════════════════════════

class TestNoDuplicateRouting:
    """Amendment classifications must NOT also appear in the non-amendment table."""

    def test_b2ba_not_in_b2b_or_b2cs(self):
        tx = _make_tx("B2BA", customer_gstin="29AALCS5765L1ZP",
                       original_invoice_number="ORIG")
        result = _generate(tx)
        assert len(result.get("b2b", [])) == 0
        b2cs_txval = sum(e.get("txval", 0) for e in result.get("b2cs", []))
        assert b2cs_txval == 0

    def test_cdnra_not_in_cdnr(self):
        tx = _make_tx("CDNRA", customer_gstin="29AALCS5765L1ZP",
                       note_type="CREDIT", note_number="CN",
                       original_invoice_number="ORIG")
        result = _generate(tx)
        assert len(result.get("cdnr", [])) == 0

    def test_cdnura_not_in_cdnur(self):
        tx = _make_tx("CDNURA", note_type="DEBIT", note_number="DN",
                       original_invoice_number="ORIG")
        result = _generate(tx)
        assert len(result.get("cdnur", [])) == 0

    def test_expa_not_in_exp(self):
        tx = _make_tx("EXPA", original_invoice_number="ORIG")
        result = _generate(tx)
        assert len(result.get("exp", [])) == 0

    def test_sez_registered_not_in_b2b(self):
        tx = _make_tx("SEZ_REGISTERED", customer_gstin="29AALCS5765L1ZP")
        result = _generate(tx)
        assert len(result.get("b2b", [])) == 0


# ══════════════════════════════════════════════
# 8. Normalizer unit tests
# ══════════════════════════════════════════════

class TestNormalizeClassification:
    """Direct unit tests for _normalize_classification()."""

    def _norm(self, raw):
        from app.services.gstr1_generator import _normalize_classification
        return _normalize_classification(raw)

    # Amendment mappings
    def test_b2ba(self):
        cls, amend, sez, nil = self._norm("B2BA")
        assert cls == "B2B" and amend is True

    def test_b2csa(self):
        cls, amend, sez, nil = self._norm("B2CSA")
        assert cls == "B2CS" and amend is True

    def test_cdnra(self):
        cls, amend, sez, nil = self._norm("CDNRA")
        assert cls == "CDNR" and amend is True

    def test_cdnura(self):
        cls, amend, sez, nil = self._norm("CDNURA")
        assert cls == "CDNUR" and amend is True

    def test_expa(self):
        cls, amend, sez, nil = self._norm("EXPA")
        assert cls == "EXPORT" and amend is True

    # SEZ mappings
    def test_sez_registered(self):
        cls, amend, sez, nil = self._norm("SEZ_REGISTERED")
        assert cls == "SEZ" and amend is False and sez == "WOPAY"

    def test_sez_unregistered(self):
        cls, amend, sez, nil = self._norm("SEZ_UNREGISTERED")
        assert cls == "SEZ" and amend is False and sez == "WOPAY"

    # NIL/Exempt/Non-GST mappings
    def test_nil_registered(self):
        cls, amend, sez, nil_base = self._norm("NIL_REGISTERED")
        assert cls == "NIL" and nil_base == "NIL"

    def test_nil_unregistered(self):
        cls, amend, sez, nil_base = self._norm("NIL_UNREGISTERED")
        assert cls == "NIL" and nil_base == "NIL"

    def test_exempt_registered(self):
        cls, amend, sez, nil_base = self._norm("EXEMPT_REGISTERED")
        assert cls == "EXEMPT" and nil_base == "EXEMPT"

    def test_exempt_unregistered(self):
        cls, amend, sez, nil_base = self._norm("EXEMPT_UNREGISTERED")
        assert cls == "EXEMPT" and nil_base == "EXEMPT"

    def test_nongst_registered(self):
        cls, amend, sez, nil_base = self._norm("NONGST_REGISTERED")
        assert cls == "NONGST" and nil_base == "NONGST"

    def test_nongst_unregistered(self):
        cls, amend, sez, nil_base = self._norm("NONGST_UNREGISTERED")
        assert cls == "NONGST" and nil_base == "NONGST"

    # Passthrough
    def test_b2b_passthrough(self):
        cls, amend, sez, nil_base = self._norm("B2B")
        assert cls == "B2B" and amend is False and sez is None and nil_base is None

    def test_unknown_passthrough(self):
        cls, amend, sez, nil_base = self._norm("UNKNOWN")
        assert cls == "UNKNOWN" and amend is False
