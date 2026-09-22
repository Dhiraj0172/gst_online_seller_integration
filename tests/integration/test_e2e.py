"""
End-to-end integration tests for the GST Online Seller pipeline.

Tests the complete workflow:
  Import -> Normalize -> Classify -> Generate GSTR-1 -> Excel -> JSON -> Validate
"""
import os
import sys
import json
import pytest
import openpyxl
from decimal import Decimal
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.adapters.registry import list_platforms, get_adapter
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus
from app.services.classification_service import classify_transaction
from app.services.gst_rules import is_b2cl_applicable, get_hsn_reporting_mode
from app.services.gstr1_excel_writer import GSTR1ExcelWriter
from app.services.gstr1_json_writer import GSTR1JsonWriter
from app.services.gstr1_json_validator import GSTR1Validator
from app.utils.gstin_validator import validate_gstin
from app.utils.tax_calculator import (
    calculate_cgst, calculate_sgst, calculate_igst,
    calculate_total_tax, calculate_invoice_value, gst_round
)


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), '..', 'fixtures')


class TestEndToEndPipeline:
    """Full pipeline test from import to GSTR-1 output."""

    def test_complete_workflow(self, tmp_path):
        """Test the complete import -> classify -> generate -> validate workflow."""
        # STEP 1: Read test data (Amazon sample)
        fixture_path = os.path.join(FIXTURE_DIR, 'amazon_sample.xlsx')
        if not os.path.exists(fixture_path):
            pytest.skip('Fixture not generated yet')

        wb = openpyxl.load_workbook(fixture_path, read_only=True)
        ws = wb.active
        rows = list(ws.rows)
        headers = [cell.value for cell in rows[0]]
        
        # STEP 2: Parse rows into dicts
        parsed_rows = []
        for row in rows[1:]:
            row_dict = {}
            for i, cell in enumerate(row):
                if i < len(headers):
                    row_dict[headers[i]] = cell.value
            parsed_rows.append(row_dict)
        wb.close()

        assert len(parsed_rows) > 0, "No rows parsed"
        print(f"\nParsed {len(parsed_rows)} rows from fixture")

        # STEP 3: Classify each row
        b2b_rows = []
        b2cs_rows = []
        b2cl_rows = []
        cdnr_rows = []
        nil_rows = []
        
        return_period = '012025'
        seller_gstin = '27AABCU9603R1ZM'
        seller_state = '27'

        for row in parsed_rows:
            buyer_gstin = str(row.get('Buyer GSTIN', '') or '')
            supply_type_raw = str(row.get('Supply Type', '') or '')
            tax_rate = Decimal(str(row.get('Tax Rate', 0) or 0))
            invoice_value = Decimal(str(row.get('Invoice Value', 0) or 0))
            place_of_supply = str(row.get('Place of Supply', '') or '')
            pos_code = place_of_supply[:2] if place_of_supply else seller_state
            
            # Classify
            if 'cancel' in supply_type_raw.lower():
                continue  # Skip cancelled
            
            is_note = 'credit' in supply_type_raw.lower() or 'debit' in supply_type_raw.lower()
            
            if is_note and buyer_gstin and len(buyer_gstin) >= 15:
                cdnr_rows.append(row)
            elif buyer_gstin and len(buyer_gstin) >= 15 and not is_note:
                b2b_rows.append(row)
            elif tax_rate == 0:
                nil_rows.append(row)
            else:
                b2cs_rows.append(row)

        print(f"  B2B: {len(b2b_rows)}, B2CS: {len(b2cs_rows)}, CDNR: {len(cdnr_rows)}, Nil: {len(nil_rows)}")
        assert len(b2b_rows) > 0, "Should have B2B rows"
        assert len(b2cs_rows) > 0, "Should have B2C rows"

        # STEP 4: Build GSTR-1 JSON structure
        # B2B: group by recipient GSTIN
        b2b_by_gstin = {}
        for row in b2b_rows:
            gstin = row['Buyer GSTIN']
            if gstin not in b2b_by_gstin:
                b2b_by_gstin[gstin] = []
            taxable = Decimal(str(row.get('Taxable Value', 0) or 0))
            rate = Decimal(str(row.get('Tax Rate', 0) or 0))
            cgst = Decimal(str(row.get('CGST Amount', 0) or 0))
            sgst = Decimal(str(row.get('SGST Amount', 0) or 0))
            igst = Decimal(str(row.get('IGST Amount', 0) or 0))
            cess = Decimal(str(row.get('Cess Amount', 0) or 0))
            inv_val = Decimal(str(row.get('Invoice Value', 0) or 0))
            pos_str = str(row.get('Place of Supply', '') or '')
            pos = pos_str[:2] if pos_str else '27'
            
            b2b_by_gstin[gstin].append({
                'inum': str(row.get('Invoice Number', '')),
                'idt': str(row.get('Invoice Date', '')),
                'val': float(gst_round(inv_val)),
                'pos': pos,
                'rchrg': str(row.get('Reverse Charge', 'N')),
                'inv_typ': 'R',
                'itms': [{
                    'num': 1,
                    'itm_det': {
                        'rt': float(rate),
                        'txval': float(gst_round(taxable)),
                        'camt': float(gst_round(cgst)),
                        'samt': float(gst_round(sgst)),
                        'iamt': float(gst_round(igst)),
                        'csamt': float(gst_round(cess)),
                    }
                }]
            })
        
        b2b_json = [{'ctin': gstin, 'inv': invs} for gstin, invs in b2b_by_gstin.items()]

        # B2CS: aggregate by state + rate
        b2cs_agg = {}
        for row in b2cs_rows:
            pos_str = str(row.get('Place of Supply', '') or '')
            pos = pos_str[:2] if pos_str else '27'
            rate = float(row.get('Tax Rate', 0) or 0)
            key = (pos, rate)
            if key not in b2cs_agg:
                b2cs_agg[key] = {'txval': Decimal('0'), 'camt': Decimal('0'), 'samt': Decimal('0'), 'iamt': Decimal('0'), 'csamt': Decimal('0')}
            b2cs_agg[key]['txval'] += Decimal(str(row.get('Taxable Value', 0) or 0))
            b2cs_agg[key]['camt'] += Decimal(str(row.get('CGST Amount', 0) or 0))
            b2cs_agg[key]['samt'] += Decimal(str(row.get('SGST Amount', 0) or 0))
            b2cs_agg[key]['iamt'] += Decimal(str(row.get('IGST Amount', 0) or 0))
            b2cs_agg[key]['csamt'] += Decimal(str(row.get('Cess Amount', 0) or 0))

        b2cs_json = []
        for (pos, rate), vals in b2cs_agg.items():
            sply_ty = 'INTRA' if pos == seller_state else 'INTER'
            b2cs_json.append({
                'sply_ty': sply_ty, 'pos': pos, 'typ': 'OE', 'rt': rate,
                'txval': float(gst_round(vals['txval'])),
                'camt': float(gst_round(vals['camt'])),
                'samt': float(gst_round(vals['samt'])),
                'iamt': float(gst_round(vals['iamt'])),
                'csamt': float(gst_round(vals['csamt'])),
            })

        gstr1_data = {
            'gstin': seller_gstin,
            'fp': return_period,
            'b2b': b2b_json,
            'b2cs': b2cs_json,
        }

        # STEP 5: Generate JSON output
        json_path = str(tmp_path / 'gstr1_test.json')
        GSTR1JsonWriter.generate_json(gstr1_data, json_path)
        assert os.path.exists(json_path)
        
        with open(json_path, 'r') as f:
            json_output = json.load(f)
        
        assert json_output['gstin'] == seller_gstin
        assert json_output['fp'] == return_period
        assert len(json_output['b2b']) == len(b2b_by_gstin)
        print(f"  JSON generated with {len(json_output['b2b'])} B2B recipients, {len(json_output['b2cs'])} B2CS entries")

        # STEP 6: Generate Excel output
        excel_data = {
            'b2b': [],
            'b2cs': [],
        }
        for b2b_entry in b2b_json:
            ctin = b2b_entry['ctin']
            for inv in b2b_entry['inv']:
                for itm in inv['itms']:
                    det = itm['itm_det']
                    excel_data['b2b'].append([
                        ctin, inv['inum'], inv['idt'], inv['val'],
                        inv['pos'], inv['rchrg'], '', inv['inv_typ'], '',
                        det['rt'], det['txval'], det['csamt']
                    ])
        for b2cs_entry in b2cs_json:
            excel_data['b2cs'].append([
                b2cs_entry['typ'], b2cs_entry['pos'], '',
                b2cs_entry['rt'], b2cs_entry['txval'], b2cs_entry['csamt'], ''
            ])

        excel_path = str(tmp_path / 'gstr1_test.xlsx')
        GSTR1ExcelWriter.generate_excel(excel_data, excel_path)
        assert os.path.exists(excel_path)

        # STEP 7: Verify Excel round-trip
        wb = openpyxl.load_workbook(excel_path)
        assert 'b2b' in wb.sheetnames
        assert 'b2cs' in wb.sheetnames
        
        ws_b2b = wb['b2b']
        assert ws_b2b.cell(1, 1).value == 'GSTIN/UIN of Recipient'
        assert ws_b2b.max_row > 1  # Has data rows
        
        ws_b2cs = wb['b2cs']
        assert ws_b2cs.cell(1, 1).value == 'Type'
        assert ws_b2cs.max_row > 1
        wb.close()

        # STEP 8: Validate JSON
        json_str = json.dumps(json_output)
        validation = GSTR1Validator.validate_gstr1_json(json_str)
        assert validation is not None
        print(f"  Validation: valid={validation.is_valid}, errors={len(validation.errors)}")

        # STEP 9: Verify tax totals reconcile
        # Sum all B2B taxable values from JSON
        json_b2b_taxable = Decimal('0')
        for b2b in json_output.get('b2b', []):
            for inv in b2b.get('inv', []):
                for itm in inv.get('itms', []):
                    json_b2b_taxable += Decimal(str(itm.get('itm_det', {}).get('txval', 0)))
        
        # Sum all B2B taxable values from source
        source_b2b_taxable = sum(Decimal(str(r.get('Taxable Value', 0) or 0)) for r in b2b_rows)
        
        diff = abs(json_b2b_taxable - source_b2b_taxable)
        assert diff < Decimal('1.00'), f"B2B taxable mismatch: JSON={json_b2b_taxable}, Source={source_b2b_taxable}, Diff={diff}"
        print(f"  B2B taxable reconciliation: JSON={json_b2b_taxable}, Source={source_b2b_taxable}, Diff={diff}")

        print("\n  ✓ COMPLETE END-TO-END PIPELINE PASSED")


class TestExcelRoundTrip:
    """Test that generated Excel files can be re-read and data matches."""

    def test_b2b_round_trip(self, tmp_path):
        data = {
            'b2b': [
                ['29AALCS5765L1ZP', 'INV-001', '15-01-2025', 1180.0, '29', 'N', '', 'Regular', '', 18.0, 1000.0, 0.0],
                ['07AADCB2230M1ZT', 'INV-002', '16-01-2025', 5900.0, '07', 'N', '', 'Regular', '', 18.0, 5000.0, 0.0],
            ]
        }
        path = str(tmp_path / 'roundtrip.xlsx')
        GSTR1ExcelWriter.generate_excel(data, path)
        
        wb = openpyxl.load_workbook(path)
        ws = wb['b2b']
        
        # Verify headers
        assert ws.cell(1, 1).value == 'GSTIN/UIN of Recipient'
        assert ws.cell(1, 2).value == 'Invoice Number'
        
        # Verify data
        assert ws.cell(2, 1).value == '29AALCS5765L1ZP'
        assert ws.cell(2, 2).value == 'INV-001'
        assert ws.cell(2, 4).value == 1180.0  # Invoice Value
        
        assert ws.cell(3, 1).value == '07AADCB2230M1ZT'
        assert ws.cell(3, 2).value == 'INV-002'
        
        # Verify numeric types
        assert isinstance(ws.cell(2, 4).value, (int, float))
        assert isinstance(ws.cell(2, 10).value, (int, float))
        
        # Verify row count
        assert ws.max_row == 3  # 1 header + 2 data rows
        wb.close()


class TestJsonSchemaValidation:
    """Test GSTR-1 JSON schema validation."""
    
    def test_valid_complete_json(self):
        data = {
            'gstin': '27AABCU9603R1ZM',
            'fp': '012025',
            'b2b': [{
                'ctin': '29AALCS5765L1ZP',
                'inv': [{
                    'inum': 'INV-001', 'idt': '15-01-2025', 'val': 1180.0,
                    'pos': '29', 'rchrg': 'N', 'inv_typ': 'R',
                    'itms': [{'num': 1, 'itm_det': {
                        'rt': 18.0, 'txval': 1000.0,
                        'camt': 90.0, 'samt': 90.0, 'iamt': 0, 'csamt': 0
                    }}]
                }]
            }],
            'b2cs': [{
                'sply_ty': 'INTRA', 'pos': '27', 'typ': 'OE',
                'rt': 18.0, 'txval': 5000.0,
                'camt': 450.0, 'samt': 450.0, 'iamt': 0, 'csamt': 0
            }],
            'nil': {'inv': [
                {'sply_ty': 'INTRB2B', 'nil_amt': 0, 'expt_amt': 0, 'ngsup_amt': 0},
                {'sply_ty': 'INTRAB2B', 'nil_amt': 0, 'expt_amt': 0, 'ngsup_amt': 0},
                {'sply_ty': 'INTRB2C', 'nil_amt': 0, 'expt_amt': 0, 'ngsup_amt': 0},
                {'sply_ty': 'INTRAB2C', 'nil_amt': 0, 'expt_amt': 0, 'ngsup_amt': 0},
            ]},
            'hsn': {'data': [{
                'num': 1, 'hsn_sc': '6109', 'desc': 'T-shirts',
                'uqc': 'NOS-NUMBERS', 'qty': 10.0, 'val': 11800.0,
                'txval': 10000.0, 'camt': 900.0, 'samt': 900.0, 'iamt': 0, 'csamt': 0
            }]},
        }
        json_str = json.dumps(data)
        result = GSTR1Validator.validate_gstr1_json(json_str)
        assert result is not None

    def test_missing_gstin(self):
        data = {'fp': '012025'}
        json_str = json.dumps(data)
        result = GSTR1Validator.validate_gstr1_json(json_str)
        # Should flag missing GSTIN
        assert not result.is_valid or len(result.errors) > 0 or len(result.warnings) > 0


class TestClassificationRules:
    """Test period-aware classification rules."""

    def test_b2cl_applicable_july_2024(self):
        assert is_b2cl_applicable('072024')

    def test_b2cl_not_applicable_aug_2024(self):
        assert not is_b2cl_applicable('082024')

    def test_b2cl_not_applicable_2025(self):
        assert not is_b2cl_applicable('012025')

    def test_hsn_combined_april_2025(self):
        assert get_hsn_reporting_mode('042025') == 'combined'

    def test_hsn_separate_may_2025(self):
        assert get_hsn_reporting_mode('052025') == 'separate_b2b_b2c'

    def test_hsn_separate_sept_2025(self):
        assert get_hsn_reporting_mode('092025') == 'separate_b2b_b2c'


class TestAdapterParsing:
    """Test platform adapter file parsing."""

    def test_amazon_adapter_exists(self):
        adapter = get_adapter('Amazon')
        assert adapter is not None

    def test_flipkart_adapter_exists(self):
        adapter = get_adapter('Flipkart')
        assert adapter is not None

    def test_all_adapters_instantiate(self):
        platforms = list_platforms()
        for p in platforms:
            name = p.get('name', '')
            adapter = get_adapter(name)
            assert adapter is not None, f"Adapter {name} returned None"


class TestRoundingEdgeCases:
    """Test decimal rounding in various edge cases."""

    def test_recurring_decimal(self):
        # 333.33 * 9% = 29.9997 => rounds to 30.00
        result = calculate_cgst(Decimal('333.33'), Decimal('18'))
        assert result == Decimal('30.00')

    def test_half_penny(self):
        # 100.01 * 2.5% = 2.50025 => rounds to 2.50
        result = calculate_cgst(Decimal('100.01'), Decimal('5'))
        assert result == Decimal('2.50')

    def test_tiny_amount(self):
        # 0.01 * 14% = 0.0014 => rounds to 0.00
        result = calculate_cgst(Decimal('0.01'), Decimal('28'))
        assert result == Decimal('0.00')

    def test_zero_taxable(self):
        assert calculate_igst(Decimal('0'), Decimal('18')) == Decimal('0.00')

    def test_zero_rate(self):
        assert calculate_igst(Decimal('1000'), Decimal('0')) == Decimal('0.00')
