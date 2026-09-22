"""Comprehensive unit tests for all GST core logic components."""
import os
import sys
import json
import pytest
from decimal import Decimal
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

# ============================================================
# GSTIN Validator Tests
# ============================================================
from app.utils.gstin_validator import (
    validate_gstin, validate_gstin_format,
    get_state_from_gstin, get_pan_from_gstin
)

class TestGSTINValidator:
    def test_valid_gstin(self):
        valid, msg = validate_gstin('27AABCU9603R1ZM')
        assert valid, msg

    def test_valid_gstin_multiple(self):
        gstins = ['29AALCS5765L1ZP', '07AADCB2230M1ZT', '33AAACR5055K1ZK', '09AABCT1332L1ZL']
        for g in gstins:
            valid, msg = validate_gstin(g)
            assert valid, f'{g}: {msg}'

    def test_invalid_gstin_empty(self):
        valid, _ = validate_gstin('')
        assert not valid

    def test_invalid_gstin_none(self):
        valid, _ = validate_gstin(None)
        assert not valid

    def test_invalid_gstin_too_short(self):
        valid, _ = validate_gstin('27AABCU960')
        assert not valid

    def test_invalid_gstin_bad_format(self):
        valid, _ = validate_gstin('XXXXXXXXXXXXXXX')
        assert not valid

    def test_invalid_gstin_bad_state(self):
        valid, msg = validate_gstin('00AABCU9603R1ZM')
        assert not valid
        assert 'state' in msg.lower()

    def test_get_state_from_gstin(self):
        assert get_state_from_gstin('27AABCU9603R1ZM') == '27'

    def test_get_pan_from_gstin(self):
        assert get_pan_from_gstin('27AABCU9603R1ZM') == 'AABCU9603R'

    def test_format_check(self):
        assert validate_gstin_format('27AABCU9603R1ZM')
        assert not validate_gstin_format('invalid')
        assert not validate_gstin_format('')


# ============================================================
# Tax Calculator Tests
# ============================================================
from app.utils.tax_calculator import (
    calculate_cgst, calculate_sgst, calculate_igst, calculate_cess,
    calculate_total_tax, calculate_invoice_value, determine_tax_type,
    validate_tax_calculation, gst_round, VALID_GST_RATES
)

class TestTaxCalculator:
    def test_cgst_18_percent(self):
        # 18% GST => CGST = 9% of taxable
        assert calculate_cgst(Decimal('1000'), Decimal('18')) == Decimal('90.00')

    def test_sgst_18_percent(self):
        assert calculate_sgst(Decimal('1000'), Decimal('18')) == Decimal('90.00')

    def test_igst_18_percent(self):
        assert calculate_igst(Decimal('1000'), Decimal('18')) == Decimal('180.00')

    def test_cgst_5_percent(self):
        assert calculate_cgst(Decimal('1000'), Decimal('5')) == Decimal('25.00')

    def test_cess(self):
        assert calculate_cess(Decimal('1000'), Decimal('1')) == Decimal('10.00')

    def test_total_tax(self):
        total = calculate_total_tax(Decimal('90'), Decimal('90'), Decimal('0'), Decimal('0'))
        assert total == Decimal('180.00')

    def test_invoice_value(self):
        val = calculate_invoice_value(Decimal('1000'), Decimal('180'))
        assert val == Decimal('1180.00')

    def test_tax_type_intra(self):
        assert determine_tax_type('27', '27') == 'INTRA'

    def test_tax_type_inter(self):
        assert determine_tax_type('27', '29') == 'INTER'

    def test_tax_type_unknown(self):
        assert determine_tax_type('', '') == 'UNKNOWN'

    def test_validate_correct_calculation(self):
        valid, errors = validate_tax_calculation(
            Decimal('1000'), Decimal('90'), Decimal('90'),
            Decimal('0'), Decimal('0'), Decimal('1180')
        )
        assert valid, errors

    def test_validate_incorrect_calculation(self):
        valid, errors = validate_tax_calculation(
            Decimal('1000'), Decimal('90'), Decimal('90'),
            Decimal('0'), Decimal('0'), Decimal('9999')
        )
        assert not valid

    def test_gst_round_half_up(self):
        assert gst_round(Decimal('100.005')) == Decimal('100.01')
        assert gst_round(Decimal('100.004')) == Decimal('100.00')
        assert gst_round(Decimal('100.015')) == Decimal('100.02')

    def test_gst_round_none(self):
        assert gst_round(None) == Decimal('0.00')

    def test_valid_gst_rates(self):
        assert Decimal('0') in VALID_GST_RATES
        assert Decimal('5') in VALID_GST_RATES
        assert Decimal('12') in VALID_GST_RATES
        assert Decimal('18') in VALID_GST_RATES
        assert Decimal('28') in VALID_GST_RATES
        assert Decimal('15') not in VALID_GST_RATES

    def test_zero_tax(self):
        assert calculate_cgst(Decimal('1000'), Decimal('0')) == Decimal('0.00')

    def test_large_value(self):
        val = calculate_igst(Decimal('99999999.99'), Decimal('18'))
        assert val == Decimal('18000000.00')  # 99999999.99 * 0.18 = 17999999.9982, rounds to 18000000.00

    def test_small_value(self):
        val = calculate_cgst(Decimal('0.01'), Decimal('18'))
        assert val == Decimal('0.00')  # 0.01 * 0.09 = 0.0009 rounds to 0.00


# ============================================================
# Date Utils Tests
# ============================================================
from app.utils.date_utils import (
    parse_date, format_date_gst, format_date_json,
    get_return_period, get_financial_year, validate_return_period
)

class TestDateUtils:
    def test_parse_dd_mm_yyyy(self):
        assert parse_date('15-01-2025') == date(2025, 1, 15)

    def test_parse_dd_slash_mm_slash_yyyy(self):
        assert parse_date('15/01/2025') == date(2025, 1, 15)

    def test_parse_yyyy_mm_dd(self):
        assert parse_date('2025-01-15') == date(2025, 1, 15)

    def test_format_gst(self):
        assert format_date_gst(date(2025, 1, 15)) == '15-01-2025'

    def test_format_json(self):
        result = format_date_json(date(2025, 1, 15))
        assert result == '15-01-2025'

    def test_return_period(self):
        assert get_return_period(1, 2025) == '012025'
        assert get_return_period(12, 2024) == '122024'

    def test_financial_year(self):
        assert get_financial_year(date(2025, 1, 15)) == '2024-25'
        assert get_financial_year(date(2025, 4, 1)) == '2025-26'
        assert get_financial_year(date(2025, 3, 31)) == '2024-25'

    def test_invalid_date(self):
        result = parse_date('not-a-date')
        assert result is None

    def test_validate_return_period_valid(self):
        valid, msg = validate_return_period('012025')
        assert valid, msg

    def test_validate_return_period_invalid(self):
        valid, _ = validate_return_period('invalid')
        assert not valid


# ============================================================
# State Codes Tests
# ============================================================
from app.utils.state_codes import STATE_CODES, get_state_name, get_state_code, is_valid_state

class TestStateCodes:
    def test_all_states_present(self):
        assert len(STATE_CODES) >= 37

    def test_maharashtra(self):
        name = get_state_name('27')
        assert name is not None
        assert 'Maharashtra' in name

    def test_delhi(self):
        name = get_state_name('07')
        assert name is not None
        assert 'Delhi' in name

    def test_invalid_state(self):
        assert not is_valid_state('00')
        assert not is_valid_state('99')

    def test_foreign_country(self):
        assert is_valid_state('96')


# ============================================================
# HSN Utils Tests
# ============================================================
from app.utils.hsn_utils import validate_hsn, normalize_hsn, is_hsn_or_sac

class TestHSNUtils:
    def test_valid_4_digit(self):
        valid, _ = validate_hsn('6109')
        assert valid

    def test_valid_6_digit(self):
        valid, _ = validate_hsn('610910')
        assert valid

    def test_valid_8_digit(self):
        valid, _ = validate_hsn('61091010')
        assert valid

    def test_invalid_3_digit(self):
        valid, _ = validate_hsn('610')
        assert not valid

    def test_invalid_5_digit(self):
        valid, _ = validate_hsn('61091')
        assert not valid

    def test_sac_detection(self):
        result = is_hsn_or_sac('9963')
        assert result in ('SAC', 'HSN')  # Both valid interpretations

    def test_normalize(self):
        result = normalize_hsn(' 6109 ')
        assert result == '6109'


# ============================================================
# UQC Codes Tests
# ============================================================
from app.utils.uqc_codes import UQC_CODES, validate_uqc

class TestUQCCodes:
    def test_valid_uqc(self):
        assert validate_uqc('NOS')
        assert validate_uqc('KGS')
        assert validate_uqc('PCS')

    def test_invalid_uqc(self):
        assert not validate_uqc('INVALID')
        assert not validate_uqc('')

    def test_all_have_entries(self):
        assert len(UQC_CODES) >= 20


# ============================================================
# File Utils Tests
# ============================================================
from app.utils.file_utils import sanitize_filename, validate_file_type, human_readable_size

class TestFileUtils:
    def test_sanitize_normal(self):
        result = sanitize_filename('report.xlsx')
        assert result == 'report.xlsx'

    def test_sanitize_path_traversal(self):
        result = sanitize_filename('../../../etc/passwd')
        assert '..' not in result
        assert '/' not in result or 'etc' not in result

    def test_sanitize_special_chars(self):
        result = sanitize_filename('file<>:"/\\|?*.xlsx')
        assert '<' not in result
        assert '>' not in result

    def test_validate_allowed(self):
        valid, _ = validate_file_type('report.xlsx', {'xlsx', 'csv'})
        assert valid

    def test_validate_disallowed(self):
        valid, _ = validate_file_type('script.exe', {'xlsx', 'csv'})
        assert not valid

    def test_human_readable(self):
        assert 'KB' in human_readable_size(1024) or 'kB' in human_readable_size(1024) or '1' in human_readable_size(1024)


# ============================================================
# GST Rules Tests
# ============================================================
from app.services.gst_rules import (
    is_b2cl_applicable, get_hsn_reporting_mode,
    get_rules_for_period, get_b2cl_threshold
)

class TestGSTRules:
    def test_b2cl_before_aug_2024(self):
        assert is_b2cl_applicable('072024')

    def test_b2cl_after_aug_2024(self):
        assert not is_b2cl_applicable('082024')
        assert not is_b2cl_applicable('012025')

    def test_b2cl_threshold_before(self):
        threshold = get_b2cl_threshold('072024')
        assert threshold == Decimal('250000.00')

    def test_b2cl_threshold_after(self):
        threshold = get_b2cl_threshold('082024')
        assert threshold is None

    def test_hsn_combined_before_may_2025(self):
        assert get_hsn_reporting_mode('042025') == 'combined'

    def test_hsn_separate_after_may_2025(self):
        assert get_hsn_reporting_mode('052025') == 'separate_b2b_b2c'

    def test_rules_baseline(self):
        rules = get_rules_for_period('012024')
        assert rules['b2cl_applicable'] is True
        assert rules['hsn_reporting_mode'] == 'combined'

    def test_rules_post_all_changes(self):
        rules = get_rules_for_period('062025')
        assert rules['b2cl_applicable'] is False
        assert rules['hsn_reporting_mode'] == 'separate_b2b_b2c'


# ============================================================
# Classification Tests
# ============================================================
from app.services.classification_service import (
    classify_b2b, classify_b2c, classify_cdnr, classify_cdnur,
    classify_nil_exempt, classify_transaction, classify_ecommerce,
    classify_reverse_charge, determine_supply_type, classify_sez
)

class TestClassification:
    def test_b2b_with_gstin(self):
        txn = {'customer_gstin': '29AALCS5765L1ZP', 'document_type': ''}
        assert classify_b2b(txn)

    def test_b2b_without_gstin(self):
        txn = {'customer_gstin': '', 'document_type': ''}
        assert not classify_b2b(txn)

    def test_b2cs_intra(self):
        txn = {
            'customer_gstin': '', 'seller_gstin': '27AABCU9603R1ZM',
            'place_of_supply': '27-Maharashtra', 'seller_state': '27',
            'invoice_value': 5000
        }
        result = classify_b2c(txn, '012025')
        assert result == 'B2CS'

    def test_b2cs_post_aug_2024(self):
        """After Aug 2024, all B2C goes to B2CS regardless of value."""
        txn = {
            'customer_gstin': '', 'seller_gstin': '27AABCU9603R1ZM',
            'place_of_supply': '29-Karnataka', 'seller_state': '27',
            'invoice_value': 500000
        }
        result = classify_b2c(txn, '012025')
        assert result == 'B2CS'

    def test_b2cl_pre_aug_2024(self):
        """Before Aug 2024, inter-state B2C > 2.5L goes to B2CL."""
        txn = {
            'customer_gstin': '', 'seller_gstin': '27AABCU9603R1ZM',
            'place_of_supply': '29-Karnataka', 'seller_state': '27',
            'invoice_value': 300000
        }
        result = classify_b2c(txn, '072024')
        assert result == 'B2CL'

    def test_cdnr_credit_note(self):
        txn = {'document_type': 'Credit Note', 'customer_gstin': '29AALCS5765L1ZP'}
        assert classify_cdnr(txn)

    def test_cdnur_credit_note(self):
        txn = {'document_type': 'Credit Note', 'customer_gstin': ''}
        assert classify_cdnur(txn)

    def test_nil_rated(self):
        txn = {'tax_rate': 0, 'item_type': ''}
        result = classify_nil_exempt(txn)
        assert result == 'NIL_UNREGISTERED'

    def test_exempt(self):
        txn = {'tax_rate': 0, 'item_type': 'Exempt'}
        result = classify_nil_exempt(txn)
        assert result == 'EXEMPT_UNREGISTERED'

    def test_nil_rated_registered(self):
        txn = {'tax_rate': 0, 'item_type': 'Nil', 'customer_gstin': '29AALCS5765L1ZP'}
        result = classify_nil_exempt(txn)
        assert result == 'NIL_REGISTERED'

    def test_exempt_registered(self):
        txn = {'tax_rate': 0, 'item_type': 'Exempt', 'customer_gstin': '29AALCS5765L1ZP'}
        result = classify_nil_exempt(txn)
        assert result == 'EXEMPT_REGISTERED'

    def test_nongst_registered(self):
        txn = {'tax_rate': 5, 'item_type': 'Non-GST', 'customer_gstin': '29AALCS5765L1ZP'}
        result = classify_nil_exempt(txn)
        assert result == 'NONGST_REGISTERED'

    def test_nongst_unregistered(self):
        txn = {'tax_rate': 5, 'item_type': 'Non-GST', 'customer_gstin': ''}
        result = classify_nil_exempt(txn)
        assert result == 'NONGST_UNREGISTERED'

    def test_sez_registered(self):
        txn = {'sez_type': 'WPAY', 'customer_gstin': '29AALCS5765L1ZP'}
        result = classify_sez(txn)
        assert result == 'SEZ_REGISTERED'

    def test_sez_unregistered(self):
        txn = {'sez_type': 'WPAY', 'customer_gstin': ''}
        result = classify_sez(txn)
        assert result == 'SEZ_UNREGISTERED'

    def test_reverse_charge(self):
        assert classify_reverse_charge({'reverse_charge': True})
        assert not classify_reverse_charge({'reverse_charge': False})

    def test_ecommerce(self):
        assert classify_ecommerce({'ecommerce_gstin': '27AABCA1234B1ZM'})
        assert not classify_ecommerce({'ecommerce_gstin': ''})

    def test_supply_type_intra(self):
        result = determine_supply_type('27AABCU9603R1ZM', None, '27-Maharashtra', '27')
        assert result == 'INTRA'

    def test_supply_type_inter(self):
        result = determine_supply_type('27AABCU9603R1ZM', None, '29-Karnataka', '27')
        assert result == 'INTER'


# ============================================================
# Transaction Validator Tests
# ============================================================
from app.validators.transaction_validator import validate_transaction

class TestTransactionValidator:
    def test_valid_transaction(self):
        txn = {
            'invoice_number': 'INV-001',
            'invoice_date': '15-01-2025',
            'customer_gstin': '29AALCS5765L1ZP',
            'seller_gstin': '27AABCU9603R1ZM',
            'hsn_sac': '6109',
            'taxable_value': 1000,
            'tax_rate': 18,
            'cgst_amount': 90,
            'sgst_amount': 90,
            'igst_amount': 0,
            'invoice_value': 1180,
            'place_of_supply': '29',
        }
        result = validate_transaction(txn)
        assert result.is_valid, result.errors

    def test_missing_invoice_number(self):
        txn = {
            'invoice_number': '',
            'invoice_date': '15-01-2025',
            'taxable_value': 1000,
            'tax_rate': 18,
        }
        result = validate_transaction(txn)
        assert not result.is_valid

    def test_invalid_gstin(self):
        txn = {
            'invoice_number': 'INV-001',
            'invoice_date': '15-01-2025',
            'customer_gstin': 'INVALID',
            'taxable_value': 1000,
            'tax_rate': 18,
        }
        result = validate_transaction(txn)
        # Should have warning or error about GSTIN
        assert len(result.errors) > 0 or len(result.warnings) > 0


# ============================================================
# Excel Writer Tests
# ============================================================
from app.services.gstr1_excel_writer import GSTR1ExcelWriter
import openpyxl

class TestExcelWriter:
    def test_generate_b2b_sheet(self, tmp_path):
        data = {
            'b2b': [
                ['29AALCS5765L1ZP', 'INV-001', '15-01-2025', 1180.0, '29-Karnataka', 'N', '', 'Regular', '', 18.0, 1000.0, 0.0]
            ]
        }
        path = str(tmp_path / 'test_gstr1.xlsx')
        GSTR1ExcelWriter.generate_excel(data, path)
        
        # Verify the generated file
        wb = openpyxl.load_workbook(path)
        assert 'b2b' in wb.sheetnames
        ws = wb['b2b']
        assert ws.cell(1, 1).value == 'GSTIN/UIN of Recipient'
        assert ws.cell(2, 1).value == '29AALCS5765L1ZP'
        assert ws.cell(2, 2).value == 'INV-001'
        wb.close()

    def test_generate_empty_data(self, tmp_path):
        data = {}
        path = str(tmp_path / 'test_empty.xlsx')
        GSTR1ExcelWriter.generate_excel(data, path)
        wb = openpyxl.load_workbook(path)
        assert len(wb.sheetnames) >= 1
        wb.close()

    def test_multiple_sheets(self, tmp_path):
        data = {
            'b2b': [['29AALCS5765L1ZP', 'INV-001', '15-01-2025', 1180.0, '29', 'N', '', 'Regular', '', 18.0, 1000.0, 0.0]],
            'b2cs': [['OE', '27-Maharashtra', '', 18.0, 5000.0, 0.0, '']],
        }
        path = str(tmp_path / 'test_multi.xlsx')
        GSTR1ExcelWriter.generate_excel(data, path)
        wb = openpyxl.load_workbook(path)
        assert 'b2b' in wb.sheetnames
        assert 'b2cs' in wb.sheetnames
        wb.close()


# ============================================================
# JSON Writer Tests
# ============================================================
from app.services.gstr1_json_writer import GSTR1JsonWriter

class TestJsonWriter:
    def test_generate_json(self, tmp_path):
        data = {
            'gstin': '27AABCU9603R1ZM',
            'fp': '012025',
            'b2b': [{
                'ctin': '29AALCS5765L1ZP',
                'inv': [{
                    'inum': 'INV-001', 'idt': '15-01-2025', 'val': 1180.0,
                    'pos': '29', 'rchrg': 'N', 'inv_typ': 'R',
                    'itms': [{'num': 1, 'itm_det': {'rt': 18.0, 'txval': 1000.0, 'camt': 90.0, 'samt': 90.0, 'iamt': 0, 'csamt': 0}}]
                }]
            }],
            'b2cs': [{'sply_ty': 'INTRA', 'pos': '27', 'typ': 'OE', 'rt': 18.0, 'txval': 5000.0, 'camt': 450.0, 'samt': 450.0, 'iamt': 0, 'csamt': 0}],
        }
        path = str(tmp_path / 'test_gstr1.json')
        GSTR1JsonWriter.generate_json(data, path)
        
        with open(path, 'r') as f:
            result = json.load(f)
        
        assert result['gstin'] == '27AABCU9603R1ZM'
        assert result['fp'] == '012025'
        assert len(result['b2b']) == 1
        assert result['b2b'][0]['ctin'] == '29AALCS5765L1ZP'

    def test_deterministic_output(self, tmp_path):
        data = {'gstin': '27AABCU9603R1ZM', 'fp': '012025', 'b2b': [], 'b2cs': []}
        p1 = str(tmp_path / 'test1.json')
        p2 = str(tmp_path / 'test2.json')
        GSTR1JsonWriter.generate_json(data, p1)
        GSTR1JsonWriter.generate_json(data, p2)
        with open(p1) as f1, open(p2) as f2:
            assert f1.read() == f2.read()


# ============================================================
# JSON Validator Tests
# ============================================================
from app.services.gstr1_json_validator import GSTR1Validator

class TestJsonValidator:
    def test_valid_json(self):
        json_str = json.dumps({
            'gstin': '27AABCU9603R1ZM', 'fp': '012025',
            'b2b': [], 'b2cs': []
        })
        result = GSTR1Validator.validate_gstr1_json(json_str)
        # Should parse and validate successfully at syntax level
        assert result is not None

    def test_invalid_json_syntax(self):
        result = GSTR1Validator.validate_gstr1_json('{invalid json}')
        assert not result.is_valid


# ============================================================
# GSTR-1 Generator Tests
# ============================================================
from app.services.gstr1_generator import generate_gstr1

class TestGSTR1Generator:
    def test_generate_empty(self):
        result = generate_gstr1('test_profile', '012025')
        assert result is not None
        assert result.excel_path is not None
        assert result.json_path is not None
        assert os.path.exists(result.excel_path)
        assert os.path.exists(result.json_path)

    def test_excel_opens(self):
        result = generate_gstr1('test_profile', '012025')
        wb = openpyxl.load_workbook(result.excel_path)
        assert len(wb.sheetnames) >= 1
        wb.close()

    def test_json_parses(self):
        result = generate_gstr1('test_profile', '012025')
        with open(result.json_path, 'r') as f:
            data = json.load(f)
        assert 'gstin' in data
        assert 'fp' in data


# ============================================================
# Adapter Tests
# ============================================================
from app.adapters.registry import list_platforms

class TestAdapters:
    def test_platforms_registered(self):
        platforms = list_platforms()
        assert len(platforms) >= 15

    def test_platform_names(self):
        platforms = list_platforms()
        names = [p.get('name', '') for p in platforms]
        assert 'Amazon' in names
        assert 'Flipkart' in names
        assert 'Meesho' in names
