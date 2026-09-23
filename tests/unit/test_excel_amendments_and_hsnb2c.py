"""
CRIT-03 + HIGH-07 Regression Tests:
CRIT-03: Excel Amendment Output (b2csa, cdnra, cdnura, expa) + b2csa Sheet Handling
HIGH-07: GSTR-1 JSON Writer hsnb2c Serialization Preservation

Tests verify:
- CRIT-03 A: b2csa transformed into Excel row structure
- CRIT-03 B: cdnra transformed into Excel row structure
- CRIT-03 C: cdnura transformed into Excel row structure
- CRIT-03 D: expa transformed into Excel row structure
- CRIT-03 E: b2csa sheet exists in generated workbook
- CRIT-03 F: Amendment rows preserved from generator to Excel workbook cells
- CRIT-03 G: Existing amendment sheets (b2ba, b2cla) remain intact
- HIGH-07 H: Generator creates json_data["hsnb2c"] for May 2025+ period
- HIGH-07 I: JSON writer preserves hsnb2c without omitting it
- HIGH-07 J: Parsed output JSON contains hsnb2c table
- HIGH-07 K: Existing hsn output remains intact
- HIGH-07 L: Non-hsnb2c periods (pre-May 2025) do NOT contain hsnb2c in JSON
- Cross-layer consistency: Generator -> Transform -> Workbook / Generator -> Writer -> Parsed JSON
"""
import json
import pytest
import openpyxl
from decimal import Decimal
from datetime import datetime

from app.services.gstr1_generator import _build_gstr1_json, transform_for_excel
from app.services.gstr1_excel_writer import GSTR1ExcelWriter, generate_gstr1_excel
from app.services.gstr1_json_writer import GSTR1JsonWriter, generate_gstr1_json


class MockProfile:
    gstin = "27AABCU9603R1ZM"
    state_code = "27"


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
    class MockTx:
        pass

    tx = MockTx()
    tx.classification_status = classification
    tx.gstr1_table = gstr1_table
    tx.supply_type = supply_type or ("INTRA" if place_of_supply == "27" else "INTER")
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


# ══════════════════════════════════════════════
# CRIT-03: Excel Amendment Output Tests
# ══════════════════════════════════════════════

class TestExcelAmendmentsOutput:
    """Verify b2csa, cdnra, cdnura, expa survive through transform_for_excel to the workbook."""

    def test_b2csa_transform_and_workbook_cells(self, tmp_path):
        """CRIT-03 A, E, F: b2csa transformed and written into actual workbook cells."""
        tx = _make_tx(
            "B2CSA",
            place_of_supply="27",
            tax_rate=Decimal('18'),
            taxable_value=Decimal('5000'),
            igst_amount=Decimal('0'),
            cgst_amount=Decimal('450'),
            sgst_amount=Decimal('450'),
            cess_amount=Decimal('50')
        )
        json_data = _build_gstr1_json(MockProfile(), "012025", [tx])
        assert "b2csa" in json_data
        assert len(json_data["b2csa"]) == 1

        excel_data = transform_for_excel(json_data)
        assert "b2csa" in excel_data
        assert len(excel_data["b2csa"]) == 1
        row = excel_data["b2csa"][0]
        # Expected row: [typ, pos, "", rt, txval, csamt, ""]
        assert row[0] == "OE"
        assert row[1] == "27"
        assert row[3] == 18.0
        assert row[4] == 5000.0
        assert row[5] == 50.0

        excel_path = str(tmp_path / "test_b2csa.xlsx")
        generate_gstr1_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        assert "b2csa" in wb.sheetnames
        ws = wb["b2csa"]
        assert ws.cell(1, 1).value == "Type"
        assert ws.cell(1, 2).value == "Place Of Supply"
        assert ws.cell(1, 4).value == "Rate"
        assert ws.cell(1, 5).value == "Taxable Value"
        assert ws.cell(1, 6).value == "Cess Amount"

        assert ws.cell(2, 1).value == "OE"
        assert ws.cell(2, 2).value == "27"
        assert ws.cell(2, 4).value == 18.0
        assert ws.cell(2, 5).value == 5000.0
        assert ws.cell(2, 6).value == 50.0
        wb.close()

    def test_cdnra_transform_and_workbook_cells(self, tmp_path):
        """CRIT-03 B, F: cdnra transformed and written into actual workbook cells."""
        tx = _make_tx(
            "CDNRA",
            customer_gstin="29AALCS5765L1ZP",
            note_type="CREDIT",
            note_number="CN002",
            original_invoice_number="CN001",
            invoice_value=Decimal('1180'),
            taxable_value=Decimal('1000'),
            tax_rate=Decimal('18'),
            place_of_supply="29"
        )
        json_data = _build_gstr1_json(MockProfile(), "012025", [tx])
        assert "cdnra" in json_data
        assert len(json_data["cdnra"]) == 1

        excel_data = transform_for_excel(json_data)
        assert "cdnra" in excel_data
        assert len(excel_data["cdnra"]) == 1
        row = excel_data["cdnra"][0]
        # [ctin, ont_num, ont_dt, nt_num, nt_dt, val, pos, "", ntty, rt, txval, csamt]
        assert row[0] == "29AALCS5765L1ZP"
        assert row[1] == "CN001"
        assert row[3] == "CN002"
        assert row[5] == 1180.0
        assert row[6] == "29"
        assert row[8] == "C"
        assert row[9] == 18.0
        assert row[10] == 1000.0

        excel_path = str(tmp_path / "test_cdnra.xlsx")
        generate_gstr1_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        assert "cdnra" in wb.sheetnames
        ws = wb["cdnra"]
        assert ws.cell(1, 1).value == "GSTIN/UIN of Recipient"
        assert ws.cell(1, 2).value == "Original Note Number"
        assert ws.cell(1, 4).value == "Revised Note Number"
        assert ws.cell(1, 6).value == "Note Value"
        assert ws.cell(1, 9).value == "Note Type"

        assert ws.cell(2, 1).value == "29AALCS5765L1ZP"
        assert ws.cell(2, 2).value == "CN001"
        assert ws.cell(2, 4).value == "CN002"
        assert ws.cell(2, 6).value == 1180.0
        assert ws.cell(2, 9).value == "C"
        assert ws.cell(2, 10).value == 18.0
        assert ws.cell(2, 11).value == 1000.0
        wb.close()

    def test_cdnura_transform_and_workbook_cells(self, tmp_path):
        """CRIT-03 C, F: cdnura transformed and written into actual workbook cells."""
        tx = _make_tx(
            "CDNURA",
            note_type="DEBIT",
            note_number="DN002",
            original_invoice_number="DN001",
            invoice_value=Decimal('590'),
            taxable_value=Decimal('500'),
            tax_rate=Decimal('18'),
            place_of_supply="29"
        )
        json_data = _build_gstr1_json(MockProfile(), "012025", [tx])
        assert "cdnura" in json_data
        assert len(json_data["cdnura"]) == 1

        excel_data = transform_for_excel(json_data)
        assert "cdnura" in excel_data
        assert len(excel_data["cdnura"]) == 1
        row = excel_data["cdnura"][0]
        # [ont_num, ont_dt, nt_num, nt_dt, val, pos, typ, ntty, rt, txval, csamt]
        assert row[0] == "DN001"
        assert row[2] == "DN002"
        assert row[4] == 590.0
        assert row[5] == "29"
        assert row[6] == "B2CL"
        assert row[7] == "D"
        assert row[8] == 18.0
        assert row[9] == 500.0

        excel_path = str(tmp_path / "test_cdnura.xlsx")
        generate_gstr1_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        assert "cdnura" in wb.sheetnames
        ws = wb["cdnura"]
        assert ws.cell(1, 1).value == "Original Note Number"
        assert ws.cell(1, 3).value == "Revised Note Number"
        assert ws.cell(1, 5).value == "Note Value"
        assert ws.cell(1, 7).value == "Note Supply Type"
        assert ws.cell(1, 8).value == "Note Type"

        assert ws.cell(2, 1).value == "DN001"
        assert ws.cell(2, 3).value == "DN002"
        assert ws.cell(2, 5).value == 590.0
        assert ws.cell(2, 7).value == "B2CL"
        assert ws.cell(2, 8).value == "D"
        assert ws.cell(2, 9).value == 18.0
        assert ws.cell(2, 10).value == 500.0
        wb.close()

    def test_expa_transform_and_workbook_cells(self, tmp_path):
        """CRIT-03 D, F: expa transformed and written into actual workbook cells."""
        tx = _make_tx(
            "EXPA",
            invoice_number="EXPA002",
            original_invoice_number="EXPA001",
            invoice_value=Decimal('25000'),
            taxable_value=Decimal('25000'),
            tax_rate=Decimal('18'),
            igst_amount=Decimal('4500')
        )
        json_data = _build_gstr1_json(MockProfile(), "012025", [tx])
        assert "expa" in json_data
        assert len(json_data["expa"]) == 1

        excel_data = transform_for_excel(json_data)
        assert "expa" in excel_data
        assert len(excel_data["expa"]) == 1
        row = excel_data["expa"][0]
        # [exp_typ, oinum, oidt, inum, idt, val, sbpcode, sbnum, sbdt, rt, txval]
        assert row[0] == "WPAY"
        assert row[1] == "EXPA001"
        assert row[3] == "EXPA002"
        assert row[5] == 25000.0
        assert row[9] == 18.0
        assert row[10] == 25000.0

        excel_path = str(tmp_path / "test_expa.xlsx")
        generate_gstr1_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        assert "expa" in wb.sheetnames
        ws = wb["expa"]
        assert ws.cell(1, 1).value == "Export Type"
        assert ws.cell(1, 2).value == "Original Invoice Number"
        assert ws.cell(1, 4).value == "Revised Invoice Number"
        assert ws.cell(1, 6).value == "Invoice Value"

        assert ws.cell(2, 1).value == "WPAY"
        assert ws.cell(2, 2).value == "EXPA001"
        assert ws.cell(2, 4).value == "EXPA002"
        assert ws.cell(2, 6).value == 25000.0
        assert ws.cell(2, 10).value == 18.0
        assert ws.cell(2, 11).value == 25000.0
        wb.close()

    def test_existing_amendments_not_regressed(self, tmp_path):
        """CRIT-03 G: Existing amendment sheets b2ba and b2cla remain intact."""
        tx_b2ba = _make_tx("B2BA", customer_gstin="29AALCS5765L1ZP", original_invoice_number="INV01", invoice_number="INV02")
        tx_b2cla = _make_tx("B2CLA", place_of_supply="29", original_invoice_number="LINV01", invoice_number="LINV02", invoice_value=Decimal('300000'), taxable_value=Decimal('300000'))

        # July 2024 has b2cla active
        json_data = _build_gstr1_json(MockProfile(), "072024", [tx_b2ba, tx_b2cla])
        assert "b2ba" in json_data
        assert "b2cla" in json_data

        excel_data = transform_for_excel(json_data)
        assert len(excel_data["b2ba"]) == 1
        assert len(excel_data["b2cla"]) == 1

        excel_path = str(tmp_path / "test_existing_amend.xlsx")
        generate_gstr1_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        assert "b2ba" in wb.sheetnames
        assert "b2cla" in wb.sheetnames
        assert wb["b2ba"].cell(2, 2).value == "INV01"
        assert wb["b2ba"].cell(2, 4).value == "INV02"
        assert wb["b2cla"].cell(2, 1).value == "LINV01"
        assert wb["b2cla"].cell(2, 3).value == "LINV02"
        wb.close()


# ══════════════════════════════════════════════
# HIGH-07: HSN B2C Serialization Tests
# ══════════════════════════════════════════════

class TestHsnb2cJsonSerialization:
    """Verify hsnb2c is generated and preserved through GSTR1JsonWriter to the final JSON."""

    def test_generator_creates_hsnb2c_for_may_2025(self):
        """HIGH-07 H: Generator creates json_data["hsnb2c"] for May 2025+ period."""
        tx_b2c = _make_tx("B2CS", place_of_supply="27", taxable_value=Decimal('2000'), cgst_amount=Decimal('180'), sgst_amount=Decimal('180'), tax_rate=Decimal('18'))
        tx_b2b = _make_tx("B2B", customer_gstin="29AALCS5765L1ZP", taxable_value=Decimal('3000'), igst_amount=Decimal('540'), tax_rate=Decimal('18'))

        json_data = _build_gstr1_json(MockProfile(), "052025", [tx_b2c, tx_b2b])
        assert "hsnb2c" in json_data, "Generator must produce hsnb2c for May 2025+ period"
        assert "data" in json_data["hsnb2c"]
        assert len(json_data["hsnb2c"]["data"]) == 1
        assert json_data["hsnb2c"]["data"][0]["txval"] == 2000.0

    def test_json_writer_preserves_hsnb2c(self, tmp_path):
        """HIGH-07 I, J: JSON writer serializes hsnb2c and parsed JSON contains it."""
        tx_b2c = _make_tx("B2CS", place_of_supply="27", taxable_value=Decimal('2000'), cgst_amount=Decimal('180'), sgst_amount=Decimal('180'), tax_rate=Decimal('18'))
        json_data = _build_gstr1_json(MockProfile(), "052025", [tx_b2c])

        json_path = str(tmp_path / "test_hsnb2c.json")
        generate_gstr1_json(json_data, json_path)

        with open(json_path, "r", encoding="utf-8") as f:
            written_json = json.load(f)

        assert "hsnb2c" in written_json, "Serialized JSON must retain hsnb2c table"
        assert "data" in written_json["hsnb2c"]
        assert len(written_json["hsnb2c"]["data"]) == 1
        entry = written_json["hsnb2c"]["data"][0]
        assert entry["hsn_sc"] == "6109"
        assert entry["txval"] == 2000.0
        assert entry["camt"] == 180.0
        assert entry["samt"] == 180.0

    def test_hsn_table_intact_alongside_hsnb2c(self, tmp_path):
        """HIGH-07 K: B2B HSN entries remain in hsn.data while B2C HSN entries go to hsnb2c."""
        tx_b2c = _make_tx("B2CS", place_of_supply="27", taxable_value=Decimal('2000'), cgst_amount=Decimal('180'), sgst_amount=Decimal('180'), tax_rate=Decimal('18'))
        tx_b2b = _make_tx("B2B", customer_gstin="29AALCS5765L1ZP", taxable_value=Decimal('3000'), igst_amount=Decimal('540'), tax_rate=Decimal('18'))

        json_data = _build_gstr1_json(MockProfile(), "052025", [tx_b2c, tx_b2b])
        json_path = str(tmp_path / "test_both_hsn.json")
        generate_gstr1_json(json_data, json_path)

        with open(json_path, "r", encoding="utf-8") as f:
            written_json = json.load(f)

        assert "hsn" in written_json
        assert "data" in written_json["hsn"]
        assert len(written_json["hsn"]["data"]) == 1
        assert written_json["hsn"]["data"][0]["txval"] == 3000.0

        assert "hsnb2c" in written_json
        assert len(written_json["hsnb2c"]["data"]) == 1
        assert written_json["hsnb2c"]["data"][0]["txval"] == 2000.0

    def test_pre_may_2025_period_has_no_hsnb2c(self, tmp_path):
        """HIGH-07 L: Pre-May 2025 (combined mode) has no hsnb2c in generator or serialized JSON."""
        tx_b2c = _make_tx("B2CS", place_of_supply="27", taxable_value=Decimal('2000'), cgst_amount=Decimal('180'), sgst_amount=Decimal('180'), tax_rate=Decimal('18'))
        json_data = _build_gstr1_json(MockProfile(), "012025", [tx_b2c])

        assert "hsnb2c" not in json_data, "Pre-May 2025 should not have hsnb2c in generator output"

        json_path = str(tmp_path / "test_no_hsnb2c.json")
        generate_gstr1_json(json_data, json_path)

        with open(json_path, "r", encoding="utf-8") as f:
            written_json = json.load(f)

        assert "hsnb2c" not in written_json, "Pre-May 2025 JSON must not have hsnb2c table"
        assert "hsn" in written_json
        assert len(written_json["hsn"]["data"]) == 1


# ══════════════════════════════════════════════
# Cross-Layer Boundary Verification
# ══════════════════════════════════════════════

class TestCrossLayerConsistency:
    """End-to-end multi-amendment verification across generator, Excel, and JSON."""

    def test_all_amendments_survive_full_excel_pipeline(self, tmp_path):
        """All 5 amendment tables (b2ba, b2cla, b2csa, cdnra, cdnura, expa) survive to final workbook."""
        tx_b2ba = _make_tx("B2BA", customer_gstin="29AALCS5765L1ZP", original_invoice_number="INV01", invoice_number="INV02")
        tx_b2cla = _make_tx("B2CLA", place_of_supply="29", original_invoice_number="LINV01", invoice_number="LINV02", invoice_value=Decimal('300000'), taxable_value=Decimal('300000'))
        tx_b2csa = _make_tx("B2CSA", place_of_supply="27", taxable_value=Decimal('1000'), cgst_amount=Decimal('90'), sgst_amount=Decimal('90'), tax_rate=Decimal('18'))
        tx_cdnra = _make_tx("CDNRA", customer_gstin="29AALCS5765L1ZP", note_type="CREDIT", note_number="CN02", original_invoice_number="CN01")
        tx_cdnura = _make_tx("CDNURA", note_type="DEBIT", note_number="DN02", original_invoice_number="DN01")
        tx_expa = _make_tx("EXPA", original_invoice_number="EXP01", invoice_number="EXP02")

        # Period 072024 has b2cl applicable, so b2cla routes to b2cla
        json_data = _build_gstr1_json(MockProfile(), "072024", [tx_b2ba, tx_b2cla, tx_b2csa, tx_cdnra, tx_cdnura, tx_expa])
        excel_data = transform_for_excel(json_data)

        excel_path = str(tmp_path / "test_full_amendments.xlsx")
        generate_gstr1_excel(excel_data, excel_path)

        wb = openpyxl.load_workbook(excel_path)
        for expected_sheet in ["b2ba", "b2cla", "b2csa", "cdnra", "cdnura", "expa"]:
            assert expected_sheet in wb.sheetnames, f"Sheet {expected_sheet} must exist in workbook"
            ws = wb[expected_sheet]
            assert ws.max_row >= 2, f"Sheet {expected_sheet} must have data rows"
        wb.close()
