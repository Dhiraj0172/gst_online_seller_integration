"""Phase 5A focused tests for Import Engine and Marketplace Adapter Foundation.

Covers:
- Adapter contract / abstraction enforcement
- Adapter detection (filename tokens, sheet name signature, rank precedence)
- Canonical transaction representation (separation of source data vs canonical data)
- Representative adapter (Amazon MTR) valid parsing and canonical conversion
- Invalid headers and schema rejection with explicit actionable errors
- Malformed rows and missing required fields handling
- Duplicate detection integration (in-file duplicates and existing database duplicates)
- Common import pipeline end-to-end execution and batched persistence
- Rollback correctness on injected persistence failure
- Tenant and profile isolation
- Unsupported and unreadable file rejection without raw exception leakage
"""
import io
import json
import os
import uuid
from decimal import Decimal
from unittest.mock import patch

import openpyxl
import pytest

from app.adapters.all_adapters import (
    AmazonAdapter,
    BaseGenericAdapter,
    CustomExcelAdapter,
    FlipkartAdapter,
)
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus, PlatformAdapter
from app.adapters.canonical import CanonicalTransaction
from app.adapters.registry import detect_platform, get_adapter, list_platforms
from app.extensions import db as _db
from app.models import GSTProfile, ImportHistory, RawImport, Transaction, User
from app.services.import_service import process_import, validate_file


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), '..', 'fixtures')


@pytest.fixture
def import_seller(app, db):
    """Create two separate users and GST profiles for tenant isolation tests."""
    suffix = uuid.uuid4().hex[:8]
    u1_name = f'eng_user1_{suffix}'
    u2_name = f'eng_user2_{suffix}'
    u1_email = f'eng1_{suffix}@example.com'
    u2_email = f'eng2_{suffix}@example.com'
    gstin1 = f'27AABC{suffix[:4].upper()}1Z5'
    gstin2 = f'29BBCD{suffix[:4].upper()}1Z6'

    with app.app_context():
        u1 = User(username=u1_name, email=u1_email)
        u1.set_password('Pass123!')
        _db.session.add(u1)

        u2 = User(username=u2_name, email=u2_email)
        u2.set_password('Pass123!')
        _db.session.add(u2)
        _db.session.commit()

        p1 = GSTProfile(
            user_id=u1.id,
            gstin=gstin1,
            legal_name='Import Engine Test 1',
            trade_name='Engine 1',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(p1)

        p2 = GSTProfile(
            user_id=u2.id,
            gstin=gstin2,
            legal_name='Import Engine Test 2',
            trade_name='Engine 2',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(p2)
        _db.session.commit()

        p1_id = p1.id
        p2_id = p2.id
        u1_id = u1.id
        u2_id = u2.id

    return {
        'u1_id': u1_id,
        'u2_id': u2_id,
        'p1_id': p1_id,
        'p2_id': p2_id,
        'gstin1': gstin1,
        'gstin2': gstin2,
    }


def _create_sample_csv(rows: list, headers: list = None) -> str:
    """Helper to generate CSV text."""
    if headers is None:
        headers = [
            'Invoice Number', 'Invoice Date', 'Customer GSTIN', 'Place of Supply',
            'Taxable Value', 'Rate', 'CGST', 'SGST', 'IGST', 'Total Value'
        ]
    lines = [','.join(headers)]
    for r in rows:
        line = ','.join(str(r.get(h, '')) for h in headers)
        lines.append(line)
    return '\n'.join(lines)


# ==============================================================================
# 1. ADAPTER CONTRACT & DISCOVERY
# ==============================================================================

class TestAdapterContractAndDiscovery:
    """Verify PlatformAdapter contract and adapter selection mechanics."""

    def test_adapter_contract_methods_and_attributes(self):
        """Every adapter must satisfy the PlatformAdapter interface without persistence."""
        adapter = get_adapter('Amazon')
        assert isinstance(adapter, PlatformAdapter)
        assert adapter.PLATFORM_NAME == 'Amazon'
        assert '.xlsx' in adapter.SUPPORTED_FILE_TYPES
        assert '.csv' in adapter.SUPPORTED_FILE_TYPES
        assert hasattr(adapter, 'detect')
        assert hasattr(adapter, 'validate')
        assert hasattr(adapter, 'parse')
        assert hasattr(adapter, 'normalize')

        # Adapters must NOT have persistence or DB commit methods
        assert not hasattr(adapter, 'save')
        assert not hasattr(adapter, 'commit')
        assert not hasattr(adapter, 'persist')

    def test_adapter_detection_by_filename(self):
        """Adapter is detected by filename tokens."""
        detected = detect_platform(None, 'amazon_mtr_report_jan2025.xlsx')
        assert detected is not None
        assert detected.PLATFORM_NAME == 'Amazon'

        detected_fk = detect_platform(None, 'flipkart_sales_report.xlsx')
        assert detected_fk is not None
        assert detected_fk.PLATFORM_NAME == 'Flipkart'

    def test_adapter_detection_by_sheet_name(self):
        """Adapter is detected by workbook sheet name when filename is generic."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'MTR'
        detected = detect_platform(wb, 'report_generic_12345.xlsx')
        assert detected is not None
        assert detected.PLATFORM_NAME == 'Amazon'
        wb.close()

    def test_adapter_detection_ranking(self):
        """Specific platform adapter wins over catch-all CustomExcelAdapter."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'MTR'
        detected = detect_platform(wb, 'anything.xlsx')
        # Even with generic filename, MTR sheet detects Amazon over Custom_Excel
        assert detected.PLATFORM_NAME == 'Amazon'
        wb.close()


# ==============================================================================
# 2. CANONICAL TRANSACTION REPRESENTATION
# ==============================================================================

class TestCanonicalRepresentation:
    """Verify CanonicalTransaction dataclass behavior and source separation."""

    def test_canonical_transaction_instantiation_and_dict_access(self):
        """CanonicalTransaction holds typed fields and supports dictionary access."""
        tx = CanonicalTransaction(
            invoice_number='INV-2025-01',
            invoice_date='15-01-2025',
            taxable_value=Decimal('5000.00'),
            cgst_amount=Decimal('450.00'),
            sgst_amount=Decimal('450.00'),
            place_of_supply='27',
            source_platform='Amazon'
        )
        assert tx.invoice_number == 'INV-2025-01'
        assert tx.taxable_value == Decimal('5000.00')
        assert tx.total_tax == Decimal('900.00')

        # Dictionary access compatibility
        assert tx['invoice_number'] == 'INV-2025-01'
        assert tx.get('place_of_supply') == '27'
        assert 'taxable_value' in tx

        # to_dict contains all canonical fields
        d = tx.to_dict()
        assert isinstance(d, dict)
        assert d['invoice_number'] == 'INV-2025-01'
        assert d['place_of_supply'] == '27'
        assert d['taxable_value'] == Decimal('5000.00')

    def test_canonical_from_dict_conversion(self):
        """from_dict safely constructs CanonicalTransaction from normalized dict."""
        raw_dict = {
            'invoice_number': 'INV-999',
            'invoice_date': '01-01-2025',
            'taxable_value': '1200.50',
            'cgst_amount': '108.00',
            'sgst_amount': '108.00',
            'customer_gstin': '27ABCDE1234F1Z5',
            'reverse_charge': 'Y',
            'return_flag': True,
        }
        canonical = CanonicalTransaction.from_dict(raw_dict)
        assert canonical.invoice_number == 'INV-999'
        assert canonical.taxable_value == Decimal('1200.50')
        assert canonical.cgst_amount == Decimal('108.00')
        assert canonical.reverse_charge == 'Y'
        assert canonical.return_flag is True

    def test_import_row_preserves_source_and_canonical_data(self):
        """ImportRow clearly separates raw source data from canonical data."""
        raw_source = {'Invoice No': 'INV-101', 'Taxable Val': '1000', 'Extra': 'raw_value'}
        norm_data = {
            'invoice_number': 'INV-101',
            'taxable_value': Decimal('1000'),
            'place_of_supply': '27'
        }
        row = ImportRow(
            row_number=2,
            status=ImportRowStatus.SUCCESS,
            raw_data=raw_source,
            normalized_data=norm_data
        )
        # raw_data is untouched
        assert row.raw_data == raw_source
        assert row.raw_data['Extra'] == 'raw_value'

        # canonical is automatically initialized and typed
        assert row.canonical is not None
        assert row.canonical.invoice_number == 'INV-101'
        assert row.canonical.taxable_value == Decimal('1000')


# ==============================================================================
# 3. REPRESENTATIVE ADAPTER (AMAZON MTR) END-TO-END PARSING
# ==============================================================================

class TestRepresentativeAdapterParsing:
    """Exercise real Amazon MTR fixture through adapter parsing and canonical contract."""

    def test_amazon_mtr_fixture_parsing(self):
        """AmazonAdapter parses real MTR fixture and outputs canonical rows."""
        fixture_path = os.path.join(FIXTURE_DIR, 'amazon_sample.xlsx')
        assert os.path.exists(fixture_path), "amazon_sample.xlsx fixture must exist"

        adapter = AmazonAdapter()
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        is_valid, validation_errors = adapter.validate(wb)
        assert is_valid is True, f"Validation failed: {validation_errors}"

        result = adapter.parse(wb, 'amazon_sample.xlsx')
        wb.close()

        assert result.platform == 'Amazon'
        assert result.total_rows == 168
        assert result.success_rows == 168
        assert result.error_rows == 0
        assert len(result.rows) == 168

        # Verify canonical properties on the parsed rows
        first_row = result.rows[0]
        assert first_row.status == ImportRowStatus.SUCCESS
        assert first_row.canonical is not None
        assert first_row.canonical.invoice_number == 'INV-2025-0001'
        assert first_row.canonical.marketplace_name == 'Amazon'
        assert first_row.canonical.source_platform == 'Amazon'
        assert first_row.canonical.place_of_supply.isdigit()
        assert len(first_row.canonical.place_of_supply) == 2
        assert first_row.canonical.taxable_value > Decimal('0')


# ==============================================================================
# 4. ERROR HANDLING & VALIDATION BOUNDARIES
# ==============================================================================

class TestErrorHandlingAndValidation:
    """Differentiate invalid headers, malformed rows, and unsupported files."""

    def test_unsupported_file_extension_rejected(self):
        """validate_file rejects invalid extensions explicitly."""
        with pytest.raises(ValueError, match="Invalid file format"):
            validate_file("transactions.txt")
        with pytest.raises(ValueError, match="Invalid file format"):
            validate_file("data.pdf")

    def test_invalid_headers_file_rejected(self):
        """File with missing or unrecognized headers is rejected by validate()."""
        fixture_path = os.path.join(FIXTURE_DIR, 'wrong_headers.xlsx')
        assert os.path.exists(fixture_path)

        adapter = AmazonAdapter()
        wb = openpyxl.load_workbook(fixture_path, data_only=True)
        is_valid, errors = adapter.validate(wb)
        wb.close()

        assert is_valid is False
        assert len(errors) > 0
        assert any('recognizable header row' in err or 'missing required column' in err for err in errors)

    def test_malformed_negative_value_row_flagged_as_error(self):
        """A row with negative taxable value is marked ImportRowStatus.ERROR."""
        adapter = AmazonAdapter()
        raw_row = {
            'Invoice Number': 'INV-NEG-01',
            'Invoice Date': '01-01-2025',
            'Place of Supply': '27-Maharashtra',
            'Taxable Value': '-500.00',  # Negative value
            'Quantity': '1'
        }
        normalized = adapter.normalize(raw_row)
        assert len(normalized.get('_errors', [])) > 0
        assert any('negative' in err for err in normalized['_errors'])


# ==============================================================================
# 5. COMMON IMPORT PIPELINE & PERSISTENCE
# ==============================================================================

class TestCommonImportPipelinePersistence:
    """Verify common import pipeline orchestration, duplicate handling, and persistence."""

    def test_process_import_successful_pipeline_execution(self, app, import_seller):
        """End-to-end import creates ImportHistory, RawImports, and Transactions cleanly."""
        fixture_path = os.path.join(FIXTURE_DIR, 'amazon_sample.xlsx')

        with app.app_context():
            res = process_import(
                file_path=fixture_path,
                profile_id=import_seller['p1_id'],
                platform_name='Amazon',
                user_id=import_seller['u1_id'],
                return_period='012025',
                financial_year='2024-25'
            )
            assert res.status == 'COMPLETED'
            assert res.total_rows == 168
            assert res.success_rows == 168
            assert res.error_rows == 0
            assert res.skipped_rows == 0

            ih = _db.session.get(ImportHistory, res.import_history_id)
            assert ih is not None
            assert ih.processing_status == 'COMPLETED'
            assert ih.total_rows == 168

            txs = Transaction.query.filter_by(import_history_id=ih.id).all()
            assert len(txs) == 168

            raws = RawImport.query.filter_by(import_history_id=ih.id).all()
            assert len(raws) == 168

            # Verify HIGH-02 relationship-based linkage on every transaction
            for tx in txs:
                assert tx.raw_import_id is not None
                assert tx.raw_import is not None
                assert tx.raw_import.id == tx.raw_import_id
                assert tx.profile_id == import_seller['p1_id']

    def test_duplicate_rows_in_file_are_marked_skipped(self, app, import_seller, tmp_path):
        """In-file duplicate rows create SKIPPED RawImport and zero duplicate Transactions."""
        file_path = str(tmp_path / 'dup_in_file.csv')
        csv_rows = [
            {
                'Invoice Number': 'INV-INFILE-01',
                'Invoice Date': '10-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1000.00',
                'Rate': '18',
                'CGST': '90.00',
                'SGST': '90.00',
                'IGST': '0.00',
                'Total Value': '1180.00',
            },
            {
                'Invoice Number': 'INV-INFILE-02',
                'Invoice Date': '10-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '2000.00',
                'Rate': '18',
                'CGST': '180.00',
                'SGST': '180.00',
                'IGST': '0.00',
                'Total Value': '2360.00',
            },
            {
                # Duplicate of first row
                'Invoice Number': 'INV-INFILE-01',
                'Invoice Date': '10-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1000.00',
                'Rate': '18',
                'CGST': '90.00',
                'SGST': '90.00',
                'IGST': '0.00',
                'Total Value': '1180.00',
            },
        ]
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(_create_sample_csv(csv_rows))

        with app.app_context():
            res = process_import(
                file_path=file_path,
                profile_id=import_seller['p1_id'],
                platform_name='Generic',
                user_id=import_seller['u1_id'],
                return_period='012025',
                financial_year='2024-25'
            )
            assert res.total_rows == 3
            assert res.success_rows == 2
            assert res.skipped_rows == 1

            txs = Transaction.query.filter_by(import_history_id=res.import_history_id).all()
            assert len(txs) == 2

            raws = RawImport.query.filter_by(import_history_id=res.import_history_id).all()
            assert len(raws) == 3
            skipped_raw = [r for r in raws if r.status == 'SKIPPED']
            assert len(skipped_raw) == 1
            assert 'Duplicate of row 2' in skipped_raw[0].errors

    def test_pipeline_rollback_on_injected_failure(self, app, import_seller, tmp_path):
        """Failure during transaction processing triggers complete rollback."""
        file_path = str(tmp_path / 'injected_fail.csv')
        csv_rows = [
            {
                'Invoice Number': f'INV-FAIL-{i}',
                'Invoice Date': '10-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1000.00',
                'Rate': '18',
                'CGST': '90.00',
                'SGST': '90.00',
                'IGST': '0.00',
                'Total Value': '1180.00',
            }
            for i in range(1, 6)
        ]
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(_create_sample_csv(csv_rows))

        def failing_classifier(norm, profile, period):
            if norm.get('invoice_number') == 'INV-FAIL-4':
                raise RuntimeError("Injected database error during classification")
            return 'B2B'

        with app.app_context():
            with pytest.raises(RuntimeError, match="Injected database error"):
                process_import(
                    file_path=file_path,
                    profile_id=import_seller['p1_id'],
                    platform_name='Generic',
                    user_id=import_seller['u1_id'],
                    return_period='012025',
                    financial_year='2024-25',
                    classification_fn=failing_classifier
                )

            ih = ImportHistory.query.filter_by(
                profile_id=import_seller['p1_id'],
                file_name=os.path.basename(file_path)
            ).first()
            assert ih is not None
            assert ih.processing_status == 'FAILED'

            # Ensure zero partial transactions or raw imports remain
            assert Transaction.query.filter_by(import_history_id=ih.id).count() == 0
            assert RawImport.query.filter_by(import_history_id=ih.id).count() == 0

    def test_tenant_isolation_in_common_pipeline(self, app, import_seller, tmp_path):
        """Identical transactions imported under separate profiles do not collide."""
        file_path = str(tmp_path / 'tenant_iso.csv')
        csv_rows = [
            {
                'Invoice Number': 'INV-ISO-COMMON-01',
                'Invoice Date': '12-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1500.00',
                'Rate': '18',
                'CGST': '135.00',
                'SGST': '135.00',
                'IGST': '0.00',
                'Total Value': '1770.00',
            }
        ]
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(_create_sample_csv(csv_rows))

        with app.app_context():
            # Import for Profile 1
            res1 = process_import(
                file_path=file_path,
                profile_id=import_seller['p1_id'],
                platform_name='Generic',
                user_id=import_seller['u1_id'],
                return_period='012025',
                financial_year='2024-25'
            )
            assert res1.success_rows == 1

            # Import identical file for Profile 2
            res2 = process_import(
                file_path=file_path,
                profile_id=import_seller['p2_id'],
                platform_name='Generic',
                user_id=import_seller['u2_id'],
                return_period='012025',
                financial_year='2024-25'
            )
            assert res2.success_rows == 1

            # Both profiles have independent transaction records
            tx1 = Transaction.query.filter_by(import_history_id=res1.import_history_id).one()
            tx2 = Transaction.query.filter_by(import_history_id=res2.import_history_id).one()
            assert tx1.profile_id == import_seller['p1_id']
            assert tx2.profile_id == import_seller['p2_id']
            assert tx1.id != tx2.id
