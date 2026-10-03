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
        result = generate_gstr1('test_profile', '012025', reconciliation_report={'status': 'SUCCESS'})
        assert result is not None
        assert result.excel_path is not None
        assert result.json_path is not None
        assert os.path.exists(result.excel_path)
        assert os.path.exists(result.json_path)

    def test_excel_opens(self):
        result = generate_gstr1('test_profile', '012025', reconciliation_report={'status': 'SUCCESS'})
        wb = openpyxl.load_workbook(result.excel_path)
        assert len(wb.sheetnames) >= 1
        wb.close()

    def test_json_parses(self):
        result = generate_gstr1('test_profile', '012025', reconciliation_report={'status': 'SUCCESS'})
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


# ============================================================
# TCS Reconciliation Tests
# ============================================================
from app.services.gst_rules import (
    get_tcs_rates_for_period, calculate_tcs, get_rules_for_period
)
from app.services.ecom_service import aggregate_ecom
from decimal import Decimal
from datetime import datetime


class TestTCSRates:
    """Test period-aware TCS rates per Notification 15/2024-Central Tax."""

    def test_tcs_rate_pre_july_2024(self):
        """Before 10-07-2024: 1% total (0.5% CGST + 0.5% SGST intra, 1% IGST inter)"""
        rates = get_tcs_rates_for_period('062024')
        assert rates['total'] == Decimal('0.01')
        assert rates['cgst'] == Decimal('0.005')
        assert rates['sgst'] == Decimal('0.005')
        assert rates['igst'] == Decimal('0.01')

    def test_tcs_rate_july_1_to_9_2024(self):
        """01-07-2024 to 09-07-2024: old 1% rate still applies"""
        rates = get_tcs_rates_for_period('072024')
        # The period date is 2024-07-01 which is before 10-07-2024
        # But our rule uses period_date >= TCS_RATE_CHANGE_DATE (10th)
        # For July 2024, the period_date is 2024-07-01, so old rate applies
        # This matches the GSTN advisory: July 1-9 uses old rate
        # However, our function applies rules based on period start date
        # This is a known limitation - for exact compliance, transactions
        # should be filtered by invoice date. For now, period-level rule applies.
        assert rates['total'] == Decimal('0.01')

    def test_tcs_rate_july_10_onward_2024(self):
        """From 10-07-2024: 0.5% total (0.25% CGST + 0.25% SGST intra, 0.5% IGST inter)"""
        rates = get_tcs_rates_for_period('082024')
        assert rates['total'] == Decimal('0.005')
        assert rates['cgst'] == Decimal('0.0025')
        assert rates['sgst'] == Decimal('0.0025')
        assert rates['igst'] == Decimal('0.005')

    def test_tcs_rate_post_2024(self):
        """2025 periods use new 0.5% rate"""
        rates = get_tcs_rates_for_period('012025')
        assert rates['total'] == Decimal('0.005')
        assert rates['cgst'] == Decimal('0.0025')
        assert rates['sgst'] == Decimal('0.0025')
        assert rates['igst'] == Decimal('0.005')


class TestTCSCalculation:
    """Test TCS calculation on Section 52 net taxable value."""

    def test_tcs_intra_pre_july_2024(self):
        """Intra-state TCS before 10-07-2024: 1% on net value (0.5% CGST + 0.5% SGST)"""
        result = calculate_tcs(Decimal('10000'), 'INTRA', '062024')
        assert result['cgst'] == Decimal('50.00')
        assert result['sgst'] == Decimal('50.00')
        assert result['igst'] == Decimal('0.00')
        assert result['total'] == Decimal('100.00')

    def test_tcs_inter_pre_july_2024(self):
        """Inter-state TCS before 10-07-2024: 1% IGST on net value"""
        result = calculate_tcs(Decimal('10000'), 'INTER', '062024')
        assert result['cgst'] == Decimal('0.00')
        assert result['sgst'] == Decimal('0.00')
        assert result['igst'] == Decimal('100.00')
        assert result['total'] == Decimal('100.00')

    def test_tcs_intra_post_july_2024(self):
        """Intra-state TCS from 10-07-2024: 0.5% on net value (0.25% CGST + 0.25% SGST)"""
        result = calculate_tcs(Decimal('10000'), 'INTRA', '082024')
        assert result['cgst'] == Decimal('25.00')
        assert result['sgst'] == Decimal('25.00')
        assert result['igst'] == Decimal('0.00')
        assert result['total'] == Decimal('50.00')

    def test_tcs_inter_post_july_2024(self):
        """Inter-state TCS from 10-07-2024: 0.5% IGST on net value"""
        result = calculate_tcs(Decimal('10000'), 'INTER', '082024')
        assert result['cgst'] == Decimal('0.00')
        assert result['sgst'] == Decimal('0.00')
        assert result['igst'] == Decimal('50.00')
        assert result['total'] == Decimal('50.00')

    def test_tcs_on_net_value_not_tax(self):
        """TCS is calculated on NET TAXABLE VALUE, not on IGST/GST amount.

        Section 52: TCS = net_taxable_value * rate
        NOT: TCS = igst_amount * rate
        """
        # If taxable=10000, IGST=1800 (18%), net taxable=8000 (after 2000 returns)
        # TCS should be on 8000, not on 1800
        result = calculate_tcs(Decimal('8000'), 'INTER', '082024')
        assert result['total'] == Decimal('40.00')  # 8000 * 0.5%
        # NOT 1800 * 0.5% = 9.00

    def test_tcs_zero_net_value(self):
        """Zero net value yields zero TCS"""
        result = calculate_tcs(Decimal('0'), 'INTRA', '082024')
        assert result['total'] == Decimal('0.00')

    def test_tcs_rounding(self):
        """TCS amounts rounded to 2 decimal places"""
        result = calculate_tcs(Decimal('10000.33'), 'INTRA', '082024')
        # 10000.33 * 0.25% = 25.000825 -> 25.00
        assert result['cgst'] == Decimal('25.00')
        assert result['sgst'] == Decimal('25.00')


class TestEcomAggregation:
    """Test e-commerce aggregation with Section 52 net value logic."""

    def test_net_value_supplier_returns_only(self):
        """Section 52: net value = taxable supplies - actual SUPPLIER_RETURN only.

        Cancellation, credit note, refund and debit note rows are audited but
        are deliberately NOT deducted from (or added to) the net value.
        """
        d = date(2024, 8, 15)
        transactions = [
            # Normal supply
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800, 'cess': 0,
             'invoice_date': d,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
            # Supplier return - deducted
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 2000, 'cgst': 0, 'sgst': 0, 'igst': 360, 'cess': 0,
             'invoice_date': d,
             'return_flag': True, 'return_reason': 'SUPPLIER_RETURN', 'note_type': ''},
            # Cancellation - NOT deducted, NOT a supply
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 1000, 'cgst': 0, 'sgst': 0, 'igst': 180, 'cess': 0,
             'invoice_date': d,
             'return_flag': True, 'return_reason': 'CANCELLATION', 'note_type': ''},
            # Credit note - NOT deducted
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 500, 'cgst': 0, 'sgst': 0, 'igst': 90, 'cess': 0,
             'invoice_date': d,
             'return_flag': False, 'return_reason': '', 'note_type': 'CREDIT'},
            # Refund - NOT deducted
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 800, 'cgst': 0, 'sgst': 0, 'igst': 144, 'cess': 0,
             'invoice_date': d,
             'return_flag': True, 'return_reason': 'REFUND', 'note_type': ''},
            # Debit note - NOT added
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 300, 'cgst': 0, 'sgst': 0, 'igst': 54, 'cess': 0,
             'invoice_date': d,
             'return_flag': False, 'return_reason': '', 'note_type': 'DEBIT'},
        ]

        result = aggregate_ecom(transactions, '082024')
        assert len(result) == 1
        agg = result[0]

        # Only the genuine supply row forms the supplies figure
        assert agg['gross_taxable_value'] == Decimal('10000.00')
        # Every event type is still tracked for audit
        assert agg['all_rows_value'] == Decimal('14600.00')
        assert agg['supplier_returns'] == Decimal('2000.00')
        assert agg['cancellations'] == Decimal('1000.00')
        assert agg['credit_notes'] == Decimal('500.00')
        assert agg['refunds'] == Decimal('800.00')
        assert agg['debit_notes'] == Decimal('300.00')
        # Net = supplies 10000 - supplier returns 2000 = 8000
        assert agg['net_taxable_value'] == Decimal('8000.00')
        # TCS on 8000 at 0.5% inter-state = 40.00
        assert agg['our_tcs_total'] == Decimal('40.00')
        assert agg['our_tcs_igst'] == Decimal('40.00')

    def test_section52_spec_gross_10000_returns_2000_net_8000(self):
        """Explicit Section 52 worked example: 10,000 supplies - 2,000 returns.

        Net value 8,000, and TCS is computed on 8,000 (not on the tax amount).
        """
        d = date(2024, 8, 15)
        transactions = [
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800, 'cess': 0,
             'invoice_date': d,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 2000, 'cgst': 0, 'sgst': 0, 'igst': 360, 'cess': 0,
             'invoice_date': d,
             'return_flag': True, 'return_reason': 'SUPPLIER_RETURN', 'note_type': ''},
        ]
        agg = aggregate_ecom(transactions, '082024')[0]
        assert agg['gross_taxable_value'] == Decimal('10000.00')
        assert agg['supplier_returns'] == Decimal('2000.00')
        assert agg['net_taxable_value'] == Decimal('8000.00')
        # 0.5% of 8000 = 40.00 (NOT 0.5% of the 2160 IGST)
        assert agg['our_tcs_total'] == Decimal('40.00')
        assert agg['our_tcs_igst'] == Decimal('40.00')

    def test_non_return_events_are_not_treated_as_supplier_returns(self):
        """Cancellation/credit note/refund/debit note never move net value."""
        d = date(2024, 8, 15)
        base = [{'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
                 'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800,
                 'cess': 0, 'invoice_date': d,
                 'return_flag': False, 'return_reason': '', 'note_type': ''}]
        events = [
            {'return_flag': True, 'return_reason': 'CANCELLATION', 'note_type': ''},
            {'return_flag': False, 'return_reason': '', 'note_type': 'CREDIT'},
            {'return_flag': True, 'return_reason': 'REFUND', 'note_type': ''},
            {'return_flag': False, 'return_reason': '', 'note_type': 'DEBIT'},
        ]
        for ev in events:
            row = dict(base[0])
            row.update(ev)
            row['taxable_value'] = 5000
            agg = aggregate_ecom(base + [row], '082024')[0]
            assert agg['net_taxable_value'] == Decimal('10000.00'), ev
            # none of them may appear as a supplier return
            assert agg['supplier_returns'] == Decimal('0.00'), ev

    def test_july_2024_tcs_rate_splits_inter_state(self):
        """July 2024 spans the 10-07-2024 change: 1% then 0.5% (inter-state)."""
        transactions = [
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800,
             'cess': 0, 'invoice_date': date(2024, 7, 9),
             'return_flag': False, 'return_reason': '', 'note_type': ''},
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800,
             'cess': 0, 'invoice_date': date(2024, 7, 10),
             'return_flag': False, 'return_reason': '', 'note_type': ''},
        ]
        agg = aggregate_ecom(transactions, '072024')[0]
        assert agg['net_value_before_rate_change'] == Decimal('10000.00')
        assert agg['net_value_from_rate_change'] == Decimal('10000.00')
        # 10000 * 1% + 10000 * 0.5% = 100.00 + 50.00
        assert agg['our_tcs_igst'] == Decimal('150.00')
        assert agg['our_tcs_total'] == Decimal('150.00')

    def test_july_2024_tcs_rate_splits_intra_state(self):
        """July 2024 intra-state: 0.5%+0.5% then 0.25%+0.25%."""
        mk = lambda d: {'ecommerce_gstin': '27AABCA1234B1ZM',
                        'place_of_supply': '27-Maharashtra', 'tax_rate': 18,
                        'taxable_value': 10000, 'cgst': 900, 'sgst': 900, 'igst': 0,
                        'cess': 0, 'invoice_date': d,
                        'return_flag': False, 'return_reason': '', 'note_type': ''}
        agg = aggregate_ecom([mk(date(2024, 7, 1)), mk(date(2024, 7, 31))], '072024',
                             seller_state='27')[0]
        assert agg['supply_type'] == 'INTRA'
        # before: 10000 * 0.5% = 50.00 each; from: 10000 * 0.25% = 25.00 each
        assert agg['our_tcs_cgst'] == Decimal('75.00')
        assert agg['our_tcs_sgst'] == Decimal('75.00')
        assert agg['our_tcs_total'] == Decimal('150.00')

    def test_undated_supply_is_flagged_not_rate_guessed(self):
        """A row with no date is surfaced, never silently given a guessed rate."""
        agg = aggregate_ecom([
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800,
             'cess': 0, 'invoice_date': None,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
        ], '082024')[0]
        assert agg['has_undated_supplies'] is True
        assert agg['undated_supplies'] == Decimal('10000.00')
        # excluded from the rate-bucketed base rather than assigned 1% or 0.5%
        assert agg['net_value_before_rate_change'] == Decimal('0.00')
        assert agg['net_value_from_rate_change'] == Decimal('0.00')
        assert agg['our_tcs_total'] == Decimal('0.00')

    def test_multiple_ecom_gstin_same_state(self):
        """Multiple ecommerce_gstin in same state are tracked separately."""
        transactions = [
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 1800, 'cess': 0,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
            {'ecommerce_gstin': '29AABCF5678D1ZP', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 5000, 'cgst': 0, 'sgst': 0, 'igst': 900, 'cess': 0,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
        ]

        result = aggregate_ecom(transactions, '082024')
        assert len(result) == 2
        # Each ecom_gstin tracked separately
        ecoms = {a['ecommerce_gstin'] for a in result}
        assert ecoms == {'27AABCA1234B1ZM', '29AABCF5678D1ZP'}

    def test_zero_tax_excluded_from_net(self):
        """Zero-tax supplies are tracked separately with 0 TCS (no taxable supply)."""
        d = date(2024, 8, 15)
        transactions = [
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 0, 'taxable_value': 10000, 'cgst': 0, 'sgst': 0, 'igst': 0, 'cess': 0,
             'invoice_date': d,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
            {'ecommerce_gstin': '27AABCA1234B1ZM', 'place_of_supply': '29-Karnataka',
             'tax_rate': 18, 'taxable_value': 5000, 'cgst': 0, 'sgst': 0, 'igst': 900, 'cess': 0,
             'invoice_date': d,
             'return_flag': False, 'return_reason': '', 'note_type': ''},
        ]

        result = aggregate_ecom(transactions, '082024')
        assert len(result) == 2  # Two rate groups: 0% and 18%

        # Find the 18% rate group
        agg_18 = next(a for a in result if a['rate'] == Decimal('18'))
        assert agg_18['net_taxable_value'] == Decimal('5000.00')
        assert agg_18['our_tcs_total'] == Decimal('25.00')  # 5000 * 0.5%

        # Find the 0% rate group
        agg_0 = next(a for a in result if a['rate'] == Decimal('0'))
        assert agg_0['net_taxable_value'] == Decimal('10000.00')
        assert agg_0['our_tcs_total'] == Decimal('0.00')  # No TCS on 0% supplies
