"""Phase 5C-2 focused tests for Meesho Marketplace Adapter Production Hardening.

Covers all 32 required areas:
1. Valid detection
2. Random filename with valid structure
3. Filename-only spoof rejection
4. Amazon cross-platform rejection
5. Flipkart cross-platform rejection
6. Generic workbook rejection
7. Orders sheet with unrelated headers rejection
8. Wrong-sheet Meesho-like rejection
9. Missing invoice header
10. Missing taxable header
11. Empty Orders sheet
12. Corrupt workbook
13. Wrong-sheet validation rejection
14. Valid 40-row parse
15. Canonical mapping
16. Decimal precision
17. Date parsing + detected period
18. GSTIN mapping
19. B2C mapping
20. POS mapping
21. Malformed taxable numeric
22. Malformed other required financial numeric where applicable
23. Negative-value behavior
24. Synthetic return
25. Synthetic RTO
26. Synthetic cancellation
27. Synthetic credit-note scenario
28. In-file duplicate
29. Full-file duplicate re-import
30. Real database persistence & exact taxable reconciliation
31. Batch-boundary rollback (>1000 rows, first flush succeeds, second flush fails)
32. Tenant isolation
33. Registry determinism and order independence
"""
import io
import json
import os
import uuid
from collections import Counter
from decimal import Decimal
from unittest.mock import patch

import openpyxl
import pytest

from app.adapters.base import ImportRowStatus
from app.adapters.canonical import CanonicalTransaction
from app.adapters.meesho import MeeshoAdapter
from app.adapters.registry import (
    _ADAPTER_REGISTRY,
    auto_register_adapters,
    detect_platform,
    get_adapter,
    register_adapter,
)
from app.extensions import db as _db
from app.models import GSTProfile, ImportHistory, RawImport, Transaction, User
from app.services.import_service import process_import

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), '..', 'fixtures')


@pytest.fixture
def meesho_tenants(app, db):
    """Provide two distinct users and GST profiles for tenant isolation tests."""
    suffix = uuid.uuid4().hex[:8]
    u1_name = f'meesho_user1_{suffix}'
    u2_name = f'meesho_user2_{suffix}'
    u1_email = f'meesho1_{suffix}@example.com'
    u2_email = f'meesho2_{suffix}@example.com'
    gstin1 = f'27AABC{suffix[:4].upper()}1Z5'
    gstin2 = f'29BBED{suffix[:4].upper()}1Z6'

    with app.app_context():
        u1 = User(username=u1_name, email=u1_email)
        u1.set_password('TenantPass1!')
        u2 = User(username=u2_name, email=u2_email)
        u2.set_password('TenantPass2!')
        _db.session.add(u1)
        _db.session.add(u2)
        _db.session.commit()

        p1 = GSTProfile(
            user_id=u1.id,
            gstin=gstin1,
            legal_name='Tenant 1 Seller',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        p2 = GSTProfile(
            user_id=u2.id,
            gstin=gstin2,
            legal_name='Tenant 2 Seller',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(p1)
        _db.session.add(p2)
        _db.session.commit()

        p1_id = p1.id
        p2_id = p2.id
        u1_id = u1.id
        u2_id = u2.id

    return {
        'user1_id': u1_id,
        'user2_id': u2_id,
        'profile1_id': p1_id,
        'profile2_id': p2_id,
        'gstin1': gstin1,
        'gstin2': gstin2,
    }


def _make_meesho_workbook(rows_data, sheet_name='Orders', extra_headers=None):
    """Helper to build an in-memory workbook with Meesho headers."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    headers = [
        'Order ID', 'Invoice Number', 'Invoice Date', 'Ship From State',
        'Buyer GSTIN', 'Seller GSTIN', 'Quantity', 'CGST Rate',
        'SGST Rate', 'IGST Rate', 'Cess Rate', 'Cess Amount',
        'Tax Rate', 'Supply Type', 'Place of Supply', 'Reverse Charge',
        'Sub Order ID', 'Product Name', 'HSN', 'Selling Price',
        'Taxable Amount', 'Total', 'CGST', 'SGST', 'IGST', 'Buyer State'
    ]
    if extra_headers:
        for eh in extra_headers:
            if eh not in headers:
                headers.append(eh)
    # Also collect any unexpected headers in rows_data
    for r in rows_data:
        for k in r:
            if k not in headers:
                headers.append(k)

    ws.append(headers)
    for row in rows_data:
        ws.append([row.get(h, '') for h in headers])
    return wb


# ==============================================================================
# 1. DETECTION TESTS (Requirements 1, 2, 3, 4, 5, 6, 7, 8)
# ==============================================================================

class TestMeeshoDetection:
    """Deterministic Meesho orders report detection."""

    def test_01_valid_meesho_detection(self):
        """Valid Meesho workbook is detected even with generic filename."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        assert os.path.exists(fixture_path)

        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            adapter = detect_platform(wb, 'generic_download_1234.xlsx')
            assert adapter is not None
            assert adapter.PLATFORM_NAME == 'Meesho'
            assert isinstance(adapter, MeeshoAdapter)
        finally:
            wb.close()

        # Pre-read filename hint check
        direct_adapter = MeeshoAdapter()
        assert direct_adapter.detect(None, 'meesho_orders_january.xlsx') is True
        assert direct_adapter.detect(None, 'unrelated_report.xlsx') is False

    def test_02_random_filename_with_valid_structure(self):
        """Meesho workbook with arbitrary random filename is detected by structure."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            adapter = MeeshoAdapter()
            assert adapter.detect(wb, 'totally_random_filename_xyz_9876.xlsx') is True
        finally:
            wb.close()

    def test_03_filename_only_spoof_rejection(self):
        """Generic workbook with 'meesho.xlsx' filename is NOT detected as Meesho."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Sheet1'
        ws.append(['Col A', 'Col B', 'Col C'])
        ws.append(['Val 1', 'Val 2', 'Val 3'])
        try:
            assert MeeshoAdapter().detect(wb, 'meesho.xlsx') is False
            assert MeeshoAdapter().detect(wb, 'meesho_orders_export.xlsx') is False
        finally:
            wb.close()

    def test_04_amazon_cross_platform_rejection(self):
        """Amazon MTR workbook must never be detected as Meesho even if renamed."""
        amazon_path = os.path.join(FIXTURE_DIR, 'amazon_sample.xlsx')
        if os.path.exists(amazon_path):
            wb_amz = openpyxl.load_workbook(amazon_path, data_only=True)
            try:
                assert MeeshoAdapter().detect(wb_amz, 'amazon_sample.xlsx') is False
                assert MeeshoAdapter().detect(wb_amz, 'meesho.xlsx') is False
            finally:
                wb_amz.close()

    def test_05_flipkart_cross_platform_rejection(self):
        """Flipkart GST report workbook must never be detected as Meesho even if renamed."""
        flipkart_path = os.path.join(FIXTURE_DIR, 'flipkart_sample.xlsx')
        if os.path.exists(flipkart_path):
            wb_fk = openpyxl.load_workbook(flipkart_path, data_only=True)
            try:
                assert MeeshoAdapter().detect(wb_fk, 'flipkart_sample.xlsx') is False
                assert MeeshoAdapter().detect(wb_fk, 'meesho.xlsx') is False
            finally:
                wb_fk.close()

    def test_06_generic_workbook_rejection(self):
        """Generic workbook with arbitrary sheet and data is rejected."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Data'
        ws.append(['Header1', 'Header2'])
        ws.append(['100', '200'])
        try:
            assert MeeshoAdapter().detect(wb, 'generic_file.xlsx') is False
        finally:
            wb.close()

    def test_07_orders_sheet_with_unrelated_headers_rejection(self):
        """Orders sheet containing unrelated or generic columns is rejected."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Orders'
        ws.append(['Col A', 'Col B', 'Col C', 'Col D'])
        ws.append(['Val 1', 'Val 2', 'Val 3', 'Val 4'])
        try:
            assert MeeshoAdapter().detect(wb, 'orders.xlsx') is False
            assert MeeshoAdapter().detect(wb, 'meesho.xlsx') is False
        finally:
            wb.close()

    def test_08_wrong_sheet_meesho_like_rejection(self):
        """Workbook with Meesho headers on wrong sheet (e.g. 'Sales') is rejected."""
        rows = [{
            'Order ID': 'ORD-1', 'Sub Order ID': 'SO-1', 'Invoice Number': 'INV-1',
            'Product Name': 'Test Product', 'Selling Price': 100, 'Taxable Amount': 100,
            'Total': 118, 'Place of Supply': '27-Maharashtra', 'Supply Type': 'Regular'
        }]
        wb = _make_meesho_workbook(rows, sheet_name='Sales')
        try:
            assert MeeshoAdapter().detect(wb, 'meesho_sales.xlsx') is False
        finally:
            wb.close()


# ==============================================================================
# 2. SCHEMA VALIDATION TESTS (Requirements 9, 10, 11, 12, 13)
# ==============================================================================

class TestMeeshoSchemaValidation:
    """Meesho schema and structure validation."""

    def test_09_missing_invoice_header(self):
        """Workbook missing 'Invoice Number' column fails validation."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Orders'
        headers = ['Order ID', 'Sub Order ID', 'Taxable Amount', 'Total', 'Product Name']
        ws.append(headers)
        ws.append(['ORD-1', 'SO-1', 1000.0, 1180.0, 'Shirt'])
        try:
            valid, errors = MeeshoAdapter().validate(wb)
            assert valid is False
            assert any('invoice number' in err.lower() for err in errors)
        finally:
            wb.close()

    def test_10_missing_taxable_header(self):
        """Workbook missing 'Taxable Amount' column fails validation."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Orders'
        headers = ['Order ID', 'Sub Order ID', 'Invoice Number', 'Total', 'Product Name']
        ws.append(headers)
        ws.append(['ORD-1', 'SO-1', 'INV-1', 1180.0, 'Shirt'])
        try:
            valid, errors = MeeshoAdapter().validate(wb)
            assert valid is False
            assert any('taxable value' in err.lower() for err in errors)
        finally:
            wb.close()

    def test_11_empty_orders_sheet(self):
        """Orders sheet with headers but zero data rows fails validation."""
        wb = _make_meesho_workbook([])
        try:
            valid, errors = MeeshoAdapter().validate(wb)
            assert valid is False
            assert any('No data rows found beneath the header row' in err for err in errors)
        finally:
            wb.close()

        # Completely blank workbook
        blank_wb = openpyxl.Workbook()
        try:
            valid, errors = MeeshoAdapter().validate(blank_wb)
            assert valid is False
            assert len(errors) > 0
        finally:
            blank_wb.close()

    def test_12_corrupt_workbook(self):
        """Malformed, non-existent or unsupported file paths fail validation."""
        adapter = MeeshoAdapter()

        valid, errors = adapter.validate(None)
        assert valid is False
        assert any('No workbook or data provided' in err for err in errors)

        valid, errors = adapter.validate('corrupt_nonexistent_file_path.xyz')
        assert valid is False
        assert len(errors) > 0

    def test_13_wrong_sheet_validation_rejection(self):
        """Workbook with unrecognized sheet and headers is rejected by validation."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Transactions'
        ws.append(['Col A', 'Col B'])
        ws.append(['Val 1', 'Val 2'])
        try:
            valid, errors = MeeshoAdapter().validate(wb)
            assert valid is False
            assert any("recognizable header row" in err.lower() for err in errors)
        finally:
            wb.close()


# ==============================================================================
# 3. ROW PARSING & CANONICAL MAPPING TESTS (Requirements 14-23)
# ==============================================================================

class TestMeeshoRowParsingAndCanonical:
    """Accurate parsing, Decimal precision, canonical normalization, and error isolation."""

    def test_14_valid_40_row_parse(self):
        """All 40 rows of meesho_sample.xlsx parse cleanly with 0 errors."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            adapter = MeeshoAdapter()
            valid, errors = adapter.validate(wb)
            assert valid is True
            assert errors == []

            result = adapter.parse(wb, 'meesho_sample.xlsx')
            assert result.total_rows == 40
            assert result.success_rows == 40
            assert result.error_rows == 0
            assert result.warning_rows == 0
            assert result.skipped_rows == 0
            assert result.metadata['sheets_parsed'] == ['Orders']
            assert result.metadata['header_rows'] == {'Orders': 1}
        finally:
            wb.close()

    def test_15_canonical_mapping(self):
        """Fields map faithfully to CanonicalTransaction contract."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            result = MeeshoAdapter().parse(wb, 'meesho_sample.xlsx')
            first_row = result.rows[0]
            norm = first_row.normalized_data
            canonical = first_row.canonical_data

            assert norm['source_platform'] == 'Meesho'
            assert norm['marketplace_name'] == 'Meesho'
            assert norm['order_id'] == 'ORD-000401'
            assert norm['sub_order_id'] == 'SO-000001'
            assert norm['order_item_id'] == 'SO-000001'
            assert norm['invoice_number'] == 'INV-2025-0401'
            assert norm['description'] == 'Wooden Shelf Unit'
            assert norm['hsn_sac'] == '6109'
            assert norm['quantity'] == Decimal('10')
            assert norm['unit_price'] == Decimal('14191.7')
            assert norm['taxable_value'] == Decimal('141917')
            assert norm['invoice_value'] == Decimal('149012.85')
            assert norm['igst_amount'] == Decimal('7095.85')
            assert norm['igst_rate'] == Decimal('5')
            assert norm['place_of_supply'] == '07'

            assert isinstance(canonical, CanonicalTransaction)
            assert canonical.order_id == 'ORD-000401'
            assert canonical.order_item_id == 'SO-000001'
            assert canonical.invoice_number == 'INV-2025-0401'
            assert canonical.taxable_value == Decimal('141917')
        finally:
            wb.close()

    def test_16_decimal_precision(self):
        """All financial fields parse as exact Decimal objects with no float drift."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            result = MeeshoAdapter().parse(wb, 'meesho_sample.xlsx')
            # Check row 12 in sheet (first B2C row, 0-indexed position 10)
            row_12 = result.rows[10].normalized_data
            assert isinstance(row_12['taxable_value'], Decimal)
            assert row_12['taxable_value'] == Decimal('9212.04')
            assert isinstance(row_12['invoice_value'], Decimal)
            assert row_12['invoice_value'] == Decimal('11791.42')
            assert isinstance(row_12['cgst_amount'], Decimal)
            assert row_12['cgst_amount'] == Decimal('1289.69')
            assert isinstance(row_12['sgst_amount'], Decimal)
            assert row_12['sgst_amount'] == Decimal('1289.69')
            assert row_12['igst_amount'] == Decimal('0')
        finally:
            wb.close()

    def test_17_date_parsing_and_detected_period(self):
        """Dates are normalized to DD-MM-YYYY and detected_period is 012025."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            result = MeeshoAdapter().parse(wb, 'meesho_sample.xlsx')
            assert result.detected_period == '012025'
            for row in result.rows:
                d = row.normalized_data['invoice_date']
                assert len(d) == 10
                assert d[2] == '-' and d[5] == '-'
                assert d.endswith('-2025')
        finally:
            wb.close()

    def test_18_gstin_mapping(self):
        """Seller GSTIN and B2B buyer GSTINs map accurately."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            result = MeeshoAdapter().parse(wb, 'meesho_sample.xlsx')
            assert result.detected_gstin == '27AABCU9603R1ZM'
            for row in result.rows:
                assert row.normalized_data['seller_gstin'] == '27AABCU9603R1ZM'

            # First 10 rows are B2B
            for i in range(10):
                buyer_gstin = result.rows[i].normalized_data['customer_gstin']
                assert len(buyer_gstin) == 15
                assert buyer_gstin.isalnum()
        finally:
            wb.close()

    def test_19_b2c_mapping(self):
        """All 30 B2C rows handle absent Buyer GSTIN cleanly without error."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            result = MeeshoAdapter().parse(wb, 'meesho_sample.xlsx')
            b2c_rows = result.rows[10:]
            assert len(b2c_rows) == 30
            for row in b2c_rows:
                norm = row.normalized_data
                assert norm['customer_gstin'] == ''
                assert norm['seller_gstin'] == '27AABCU9603R1ZM'
                assert row.status == ImportRowStatus.SUCCESS
                assert len(row.errors) == 0
        finally:
            wb.close()

    def test_20_pos_mapping(self):
        """Place of Supply and Buyer State fallbacks resolve to 2-digit state codes."""
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        try:
            result = MeeshoAdapter().parse(wb, 'meesho_sample.xlsx')
            for row in result.rows:
                pos = row.normalized_data['place_of_supply']
                assert len(pos) == 2
                assert pos.isdigit()
        finally:
            wb.close()

        # Fallback test: Place of Supply column empty, Buyer State populated
        rows = [{
            'Order ID': 'ORD-1', 'Sub Order ID': 'SO-1', 'Invoice Number': 'INV-1',
            'Invoice Date': '15-01-2025', 'Product Name': 'Item', 'Selling Price': 100,
            'Taxable Amount': 100, 'Total': 118, 'Buyer State': 'Karnataka',
            'Supply Type': 'Regular'
        }]
        wb_fallback = _make_meesho_workbook(rows)
        try:
            res = MeeshoAdapter().parse(wb_fallback)
            assert res.rows[0].normalized_data['place_of_supply'] == '29'
        finally:
            wb_fallback.close()

    def test_21_malformed_taxable_numeric(self):
        """Unparseable taxable values become explicit row ERRORs and are NOT coerced to zero."""
        rows = [
            # Row 1: Valid row
            {
                'Order ID': 'ORD-1', 'Sub Order ID': 'SO-1', 'Invoice Number': 'INV-1',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item A', 'Selling Price': 100,
                'Taxable Amount': '1000.00', 'Total': '1180.00', 'Place of Supply': '27-Maharashtra',
            },
            # Row 2: Malformed text taxable amount
            {
                'Order ID': 'ORD-2', 'Sub Order ID': 'SO-2', 'Invoice Number': 'INV-2',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item B', 'Selling Price': 100,
                'Taxable Amount': 'INVALID_AMOUNT', 'Total': '1180.00', 'Place of Supply': '27-Maharashtra',
            },
            # Row 3: Malformed numeric garbage
            {
                'Order ID': 'ORD-3', 'Sub Order ID': 'SO-3', 'Invoice Number': 'INV-3',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item C', 'Selling Price': 100,
                'Taxable Amount': '1,2,3.4x', 'Total': '1180.00', 'Place of Supply': '27-Maharashtra',
            },
        ]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.total_rows == 3
            assert result.success_rows == 1
            assert result.error_rows == 2

            # Row 2 must have explicit error
            r2 = result.rows[1]
            assert r2.status == ImportRowStatus.ERROR
            assert any("Taxable value 'INVALID_AMOUNT' is not a valid numeric amount" in err for err in r2.errors)
            assert r2.normalized_data['taxable_value'] is None

            # Row 3 must have explicit error
            r3 = result.rows[2]
            assert r3.status == ImportRowStatus.ERROR
            assert any("Taxable value '1,2,3.4x' is not a valid numeric amount" in err for err in r3.errors)
            assert r3.normalized_data['taxable_value'] is None
        finally:
            wb.close()

    def test_22_malformed_other_required_financial_numeric(self):
        """Malformed non-empty numeric fields produce row ERRORs; blank optional numerics do not."""
        rows = [
            # Row 1: Malformed invoice_value (Total)
            {
                'Order ID': 'ORD-1', 'Sub Order ID': 'SO-1', 'Invoice Number': 'INV-1',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item A', 'Selling Price': '100',
                'Taxable Amount': '1000.00', 'Total': 'abc_total', 'Place of Supply': '27-Maharashtra',
            },
            # Row 2: Malformed Quantity
            {
                'Order ID': 'ORD-2', 'Sub Order ID': 'SO-2', 'Invoice Number': 'INV-2',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item B', 'Selling Price': '100',
                'Taxable Amount': '1000.00', 'Total': '1180.00', 'Quantity': 'ten_pcs',
                'Place of Supply': '27-Maharashtra',
            },
            # Row 3: Blank optional Cess Amount -> succeeds without error
            {
                'Order ID': 'ORD-3', 'Sub Order ID': 'SO-3', 'Invoice Number': 'INV-3',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item C', 'Selling Price': '100',
                'Taxable Amount': '1000.00', 'Total': '1180.00', 'Cess Amount': '',
                'Place of Supply': '27-Maharashtra',
            },
        ]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.rows[0].status == ImportRowStatus.ERROR
            assert any("Invoice value 'abc_total' is not a valid numeric amount" in err for err in result.rows[0].errors)

            assert result.rows[1].status == ImportRowStatus.ERROR
            assert any("Quantity 'ten_pcs' is not a valid numeric amount" in err for err in result.rows[1].errors)

            assert result.rows[2].status == ImportRowStatus.SUCCESS
            assert result.rows[2].normalized_data['cess_amount'] is None
        finally:
            wb.close()

    def test_23_negative_value_behavior(self):
        """Negative taxable value generates an explicit row error."""
        rows = [{
            'Order ID': 'ORD-1', 'Sub Order ID': 'SO-1', 'Invoice Number': 'INV-1',
            'Invoice Date': '15-01-2025', 'Product Name': 'Item A', 'Selling Price': -100,
            'Taxable Amount': -500.00, 'Total': -590.00, 'Place of Supply': '27-Maharashtra',
        }]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.error_rows == 1
            assert any('negative' in err.lower() for err in result.rows[0].errors)
        finally:
            wb.close()


# ==============================================================================
# 4. SYNTHETIC RETURNS, CANCELLATIONS, AND CREDIT NOTES (Requirements 24-27)
# ==============================================================================

class TestMeeshoSyntheticReturnsAndNotes:
    """Synthetic edge cases for returns, cancellations, and credit notes."""

    def test_24_synthetic_customer_return(self):
        """Supply Type 'Customer Return' correctly sets return_flag=True."""
        rows = [{
            'Order ID': 'ORD-RET-1', 'Sub Order ID': 'SO-RET-1', 'Invoice Number': 'INV-RET-1',
            'Invoice Date': '15-01-2025', 'Product Name': 'Returned Shirt', 'Selling Price': 500,
            'Taxable Amount': 500, 'Total': 590, 'Supply Type': 'Customer Return',
            'Place of Supply': '27-Maharashtra',
        }]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.total_rows == 1
            norm = result.rows[0].normalized_data
            assert norm['return_flag'] is True
            assert norm['cancellation_flag'] is False
        finally:
            wb.close()

    def test_25_synthetic_courier_return_rto(self):
        """Supply Type 'Courier Return / RTO' correctly sets return_flag=True."""
        rows = [{
            'Order ID': 'ORD-RTO-1', 'Sub Order ID': 'SO-RTO-1', 'Invoice Number': 'INV-RTO-1',
            'Invoice Date': '15-01-2025', 'Product Name': 'RTO Shoes', 'Selling Price': 1000,
            'Taxable Amount': 1000, 'Total': 1180, 'Supply Type': 'Courier Return / RTO',
            'Place of Supply': '27-Maharashtra',
        }]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.total_rows == 1
            norm = result.rows[0].normalized_data
            assert norm['return_flag'] is True
            assert norm['cancellation_flag'] is False
        finally:
            wb.close()

    def test_26_synthetic_cancellation(self):
        """Cancelled orders set cancellation_flag=True and return_flag=True."""
        rows = [{
            'Order ID': 'ORD-CAN-1', 'Sub Order ID': 'SO-CAN-1', 'Invoice Number': 'INV-CAN-1',
            'Invoice Date': '15-01-2025', 'Product Name': 'Cancelled Watch', 'Selling Price': 800,
            'Taxable Amount': 800, 'Total': 944, 'Supply Type': 'Cancelled by Customer',
            'Place of Supply': '27-Maharashtra',
        }]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.total_rows == 1
            norm = result.rows[0].normalized_data
            assert norm['cancellation_flag'] is True
            assert norm['return_flag'] is True
        finally:
            wb.close()

    def test_27_synthetic_credit_note_scenario(self):
        """Supply Type 'Credit Note' or invoice 'CN-' correctly identifies note_type='CREDIT'."""
        rows = [{
            'Order ID': 'ORD-CN-1', 'Sub Order ID': 'SO-CN-1', 'Invoice Number': 'CN-2025-001',
            'Invoice Date': '15-01-2025', 'Product Name': 'Returned Item', 'Selling Price': 500,
            'Taxable Amount': 500, 'Total': 590, 'Supply Type': 'Credit Note',
            'Credit Note No': 'CRN-999', 'Place of Supply': '27-Maharashtra',
        }]
        wb = _make_meesho_workbook(rows)
        try:
            result = MeeshoAdapter().parse(wb)
            assert result.total_rows == 1
            norm = result.rows[0].normalized_data
            assert norm['note_type'] == 'CREDIT'
            assert norm['note_number'] == 'CRN-999'
        finally:
            wb.close()


# ==============================================================================
# 5. DUPLICATES, PERSISTENCE & PIPELINE TESTS (Requirements 28-33)
# ==============================================================================

class TestMeeshoDuplicatesAndPersistence:
    """In-file duplicate skip, full-file re-import duplicate check, persistence, rollback & isolation."""

    def test_28_in_file_duplicate(self, app, db, meesho_tenants, tmp_path):
        """Duplicate Sub Order ID in same file is marked SKIPPED."""
        p_id = meesho_tenants['profile1_id']
        u_id = meesho_tenants['user1_id']

        rows = [
            {
                'Order ID': 'ORD-DUP-1', 'Sub Order ID': 'SO-DUP-1', 'Invoice Number': 'INV-DUP-1',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item 1', 'Selling Price': 500,
                'Taxable Amount': 500, 'Total': 590, 'Place of Supply': '27-Maharashtra',
                'Supply Type': 'Regular',
            },
            # Exact duplicate sub_order_id & invoice
            {
                'Order ID': 'ORD-DUP-1', 'Sub Order ID': 'SO-DUP-1', 'Invoice Number': 'INV-DUP-1',
                'Invoice Date': '15-01-2025', 'Product Name': 'Item 1', 'Selling Price': 500,
                'Taxable Amount': 500, 'Total': 590, 'Place of Supply': '27-Maharashtra',
                'Supply Type': 'Regular',
            },
        ]
        wb = _make_meesho_workbook(rows)
        path = str(tmp_path / 'test_meesho_dupes.xlsx')
        wb.save(path)
        wb.close()
        try:
            with app.app_context():
                res = process_import(
                    file_path=path,
                    profile_id=p_id,
                    platform_name='Meesho',
                    user_id=u_id,
                    return_period='012025',
                    financial_year='2024-25',
                    allow_duplicate_file=True,
                )
                assert res.total_rows == 2
                assert res.success_rows == 1
                assert res.skipped_rows == 1
                assert res.status == 'PARTIAL'
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def test_29_full_file_duplicate_reimport(self, app, db, meesho_tenants):
        """Re-importing identical Meesho file without allow_duplicate_file is rejected."""
        p_id = meesho_tenants['profile1_id']
        u_id = meesho_tenants['user1_id']
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')

        with app.app_context():
            res1 = process_import(
                file_path=fixture_path,
                profile_id=p_id,
                platform_name='Meesho',
                user_id=u_id,
                return_period='012025',
                financial_year='2024-25',
                allow_duplicate_file=False,
            )
            assert res1.status == 'COMPLETED'

            # Second import without allow_duplicate_file is rejected as duplicate file
            res2 = process_import(
                file_path=fixture_path,
                profile_id=p_id,
                platform_name='Meesho',
                user_id=u_id,
                return_period='012025',
                financial_year='2024-25',
                allow_duplicate_file=False,
            )
            assert res2.status == 'REJECTED_DUPLICATE_FILE'
            assert 'already imported successfully' in res2.errors[0]

            # Re-import with allow_duplicate_file=True skips all rows and avoids double-counting
            res3 = process_import(
                file_path=fixture_path,
                profile_id=p_id,
                platform_name='Meesho',
                user_id=u_id,
                return_period='012025',
                financial_year='2024-25',
                allow_duplicate_file=True,
            )
            assert res3.status == 'PARTIAL'
            assert res3.total_rows == 40
            assert res3.success_rows == 0
            assert res3.skipped_rows == 40
            assert res3.error_rows == 0

            # Database row count strictly remains 40
            assert Transaction.query.filter_by(profile_id=p_id).count() == 40

    def test_30_real_database_persistence_and_reconciliation(self, app, db, meesho_tenants):
        """Full 40-row fixture imports cleanly into database with exact financial reconciliation."""
        p_id = meesho_tenants['profile1_id']
        u_id = meesho_tenants['user1_id']
        fixture_path = os.path.join(FIXTURE_DIR, 'meesho_sample.xlsx')

        with app.app_context():
            res = process_import(
                file_path=fixture_path,
                profile_id=p_id,
                platform_name='Meesho',
                user_id=u_id,
                return_period='012025',
                financial_year='2024-25',
                allow_duplicate_file=True,
            )
            assert res.status == 'COMPLETED'
            assert res.total_rows == 40
            assert res.success_rows == 40
            assert res.error_rows == 0
            assert res.skipped_rows == 0

            ih = _db.session.get(ImportHistory, res.import_history_id)
            assert ih is not None
            assert ih.processing_status == 'COMPLETED'
            assert ih.total_rows == 40
            assert ih.success_rows == 40

            txs = Transaction.query.filter_by(import_history_id=ih.id).all()
            assert len(txs) == 40

            raws = RawImport.query.filter_by(import_history_id=ih.id).all()
            assert len(raws) == 40

            # Verify GSTR-1 classification breakdown
            table_counts = Counter(t.gstr1_table for t in txs)
            assert table_counts['b2b'] == 10
            assert table_counts['b2cs'] == 30

            # Mathematical reconciliation of taxable total
            total_taxable = sum(t.taxable_value for t in txs)
            assert total_taxable == Decimal('1565780.61')

    def test_31_batch_boundary_rollback(self, app, db, meesho_tenants, tmp_path):
        """Failure after first batch flush (>1000 rows) rolls back cleanly with 0 orphaned records."""
        p1_id = meesho_tenants['profile1_id']
        p2_id = meesho_tenants['profile2_id']
        u1_id = meesho_tenants['user1_id']
        u2_id = meesho_tenants['user2_id']

        # Pre-seed Profile 2 with 1 transaction to confirm unrelated tenant data is unaffected
        p2_row = [{
            'Order ID': 'ORD-P2-01', 'Sub Order ID': 'SO-P2-01', 'Invoice Number': 'INV-P2-01',
            'Invoice Date': '15-01-2025', 'Product Name': 'Item P2', 'Selling Price': 200,
            'Taxable Amount': 200, 'Total': 236, 'Place of Supply': '29-Karnataka',
            'Supply Type': 'Regular',
        }]
        wb_p2 = _make_meesho_workbook(p2_row)
        path_p2 = str(tmp_path / 'test_batch_seed_p2.xlsx')
        wb_p2.save(path_p2)
        wb_p2.close()

        # Generate 1050 valid Meesho rows (>1000 row batch boundary)
        rows_1050 = []
        for i in range(1, 1051):
            rows_1050.append({
                'Order ID': f'ORD-BATCH-{i:05d}',
                'Sub Order ID': f'SO-BATCH-{i:05d}',
                'Invoice Number': f'INV-BATCH-{i:05d}',
                'Invoice Date': '15-01-2025',
                'Product Name': f'Batch Product {i}',
                'Selling Price': 100,
                'Taxable Amount': 100,
                'Total': 118,
                'Place of Supply': '27-Maharashtra',
                'Supply Type': 'Regular',
            })
        wb_batch = _make_meesho_workbook(rows_1050)
        path_batch = str(tmp_path / 'test_batch_1050.xlsx')
        wb_batch.save(path_batch)
        wb_batch.close()

        try:
            with app.app_context():
                # Import profile 2 seed data first
                res_p2 = process_import(path_p2, p2_id, 'Meesho', u2_id, '012025', '2024-25', allow_duplicate_file=True)
                assert res_p2.status == 'COMPLETED'
                p2_tx_count_before = Transaction.query.filter_by(profile_id=p2_id).count()
                assert p2_tx_count_before == 1

                p1_tx_count_before = Transaction.query.filter_by(profile_id=p1_id).count()

                # Hook db.session.flush to succeed on the first batch flush (row 1000)
                # and raise an exception on the second batch flush
                flush_calls = [0]
                orig_flush = _db.session.flush

                def flush_mock(*args, **kwargs):
                    flush_calls[0] += 1
                    if flush_calls[0] > 1:
                        raise RuntimeError('Injected failure crossing 1000-row batch boundary')
                    return orig_flush(*args, **kwargs)

                with patch.object(_db.session, 'flush', side_effect=flush_mock):
                    with pytest.raises(RuntimeError) as exc_info:
                        process_import(
                            path_batch,
                            p1_id,
                            'Meesho',
                            u1_id,
                            '012025',
                            '2024-25',
                            allow_duplicate_file=True,
                        )
                    assert '1000-row batch boundary' in str(exc_info.value)

                # Confirm first flush was executed and second flush raised
                assert flush_calls[0] >= 2

                # Verify ZERO orphaned transactions for Profile 1
                p1_tx_count_after = Transaction.query.filter_by(profile_id=p1_id).count()
                assert p1_tx_count_after == p1_tx_count_before

                # Verify failed ImportHistory record exists and has ZERO raw imports
                failed_ih = ImportHistory.query.filter_by(profile_id=p1_id, processing_status='FAILED').first()
                assert failed_ih is not None
                raw_count = RawImport.query.filter_by(import_history_id=failed_ih.id).count()
                assert raw_count == 0

                # Verify Profile 2 data remains completely intact and unaffected
                p2_tx_count_after = Transaction.query.filter_by(profile_id=p2_id).count()
                assert p2_tx_count_after == p2_tx_count_before
        finally:
            for p in (path_p2, path_batch):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    def test_32_tenant_isolation(self, app, db, meesho_tenants, tmp_path):
        """Transactions imported for Profile 1 are completely isolated from Profile 2."""
        p1_id = meesho_tenants['profile1_id']
        u1_id = meesho_tenants['user1_id']
        p2_id = meesho_tenants['profile2_id']
        u2_id = meesho_tenants['user2_id']

        rows_p1 = [{
            'Order ID': 'ORD-P1-01', 'Sub Order ID': 'SO-P1-01', 'Invoice Number': 'INV-P1-01',
            'Invoice Date': '15-01-2025', 'Product Name': 'Item P1', 'Selling Price': 100,
            'Taxable Amount': 100, 'Total': 118, 'Place of Supply': '27-Maharashtra',
            'Supply Type': 'Regular',
        }]
        rows_p2 = [{
            'Order ID': 'ORD-P2-01', 'Sub Order ID': 'SO-P2-01', 'Invoice Number': 'INV-P2-01',
            'Invoice Date': '15-01-2025', 'Product Name': 'Item P2', 'Selling Price': 200,
            'Taxable Amount': 200, 'Total': 236, 'Place of Supply': '29-Karnataka',
            'Supply Type': 'Regular',
        }]

        wb1 = _make_meesho_workbook(rows_p1)
        path1 = str(tmp_path / 'test_meesho_tenant_p1.xlsx')
        wb1.save(path1)
        wb1.close()

        wb2 = _make_meesho_workbook(rows_p2)
        path2 = str(tmp_path / 'test_meesho_tenant_p2.xlsx')
        wb2.save(path2)
        wb2.close()

        try:
            with app.app_context():
                process_import(path1, p1_id, 'Meesho', u1_id, '012025', '2024-25', allow_duplicate_file=True)
                process_import(path2, p2_id, 'Meesho', u2_id, '012025', '2024-25', allow_duplicate_file=True)

                txs_p1 = Transaction.query.filter_by(profile_id=p1_id).all()
                txs_p2 = Transaction.query.filter_by(profile_id=p2_id).all()

                assert len(txs_p1) == 1
                assert len(txs_p2) == 1
                assert txs_p1[0].invoice_number == 'INV-P1-01'
                assert txs_p2[0].invoice_number == 'INV-P2-01'
                assert txs_p1[0].profile_id == p1_id
                assert txs_p2[0].profile_id == p2_id
        finally:
            for p in (path1, path2):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    def test_33_registry_determinism_and_order_independence(self):
        """app.adapters.meesho.MeeshoAdapter always wins registration deterministically."""
        auto_register_adapters()

        meesho = get_adapter('Meesho')
        assert meesho is not None
        assert meesho.__class__.__module__ == 'app.adapters.meesho'
        assert meesho.__class__.__name__ == 'MeeshoAdapter'

        # Verify all existing registered adapters instantiate properly
        fk = get_adapter('Flipkart')
        assert fk is not None
        assert fk.__class__.__module__ == 'app.adapters.flipkart'

        amz = get_adapter('Amazon')
        assert amz is not None
        assert amz.__class__.__module__ == 'app.adapters.amazon'

        gen = get_adapter('Generic')
        assert gen is not None
        assert gen.__class__.__module__ == 'app.adapters.all_adapters'
        assert gen.__class__.__name__ == '_GenericFilenameAdapter'

        # Test registration order independence: generic cannot overwrite specialized
        from app.adapters.all_adapters import MeeshoAdapter as GenericMeeshoAdapter
        from app.adapters.meesho import MeeshoAdapter as SpecializedMeeshoAdapter

        original = dict(_ADAPTER_REGISTRY)
        try:
            # Case A: Generic registered first, specialized registered second
            _ADAPTER_REGISTRY.clear()
            register_adapter(GenericMeeshoAdapter)
            assert _ADAPTER_REGISTRY['Meesho'] is GenericMeeshoAdapter
            register_adapter(SpecializedMeeshoAdapter)
            assert _ADAPTER_REGISTRY['Meesho'] is SpecializedMeeshoAdapter
            assert get_adapter('Meesho').__class__.__module__ == 'app.adapters.meesho'

            # Case B: Specialized registered first, generic registered second
            _ADAPTER_REGISTRY.clear()
            register_adapter(SpecializedMeeshoAdapter)
            assert _ADAPTER_REGISTRY['Meesho'] is SpecializedMeeshoAdapter
            register_adapter(GenericMeeshoAdapter)
            assert _ADAPTER_REGISTRY['Meesho'] is SpecializedMeeshoAdapter
            assert get_adapter('Meesho').__class__.__module__ == 'app.adapters.meesho'
        finally:
            _ADAPTER_REGISTRY.clear()
            _ADAPTER_REGISTRY.update(original)
            auto_register_adapters()
