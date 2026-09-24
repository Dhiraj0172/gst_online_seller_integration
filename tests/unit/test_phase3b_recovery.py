"""
Focused tests for Phase 3B:
- HIGH-05: TCS date handling (timestamp parsing, undated handling, no guessed rates, no double counting)
- HIGH-06: TCS upload security (auth, tenant/profile scoping, .xlsx only, magic bytes, size limit, cleanup)
- MED-01: Exception disclosure (generic safe messages, no raw traceback/exception details leaked)
"""
import io
import os
import tempfile
import pytest
import openpyxl
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db as _db
from app.models import User, GSTProfile, Transaction, ImportHistory, RawImport, TCSReconciliation
from app.utils.date_utils import parse_date, validate_return_period
from app.services.ecom_service import get_ecom_supplies, aggregate_ecom
from app.services.tcs_service import reconcile_tcs, import_tcs_report


# ==============================================================================
# HIGH-05: Date Parsing and TCS Undated Transaction Handling
# ==============================================================================

class TestHigh05DateHandling:
    """Tests for parse_date hardening and undated transaction preservation."""

    def test_parse_date_various_timestamp_formats(self):
        """Valid timestamps in various formats parse accurately to date."""
        assert parse_date('2024-08-15 14:30:00') == date(2024, 8, 15)
        assert parse_date('15/08/2024 09:15:30') == date(2024, 8, 15)
        assert parse_date('2024-08-15T18:45:00') == date(2024, 8, 15)
        assert parse_date('2024-08-15T18:45:00Z') == date(2024, 8, 15)
        assert parse_date('2024-08-15T18:45:00+05:30') == date(2024, 8, 15)
        assert parse_date('15-08-2024') == date(2024, 8, 15)
        assert parse_date('2024/08/15') == date(2024, 8, 15)
        assert parse_date(datetime(2024, 8, 15, 12, 0)) == date(2024, 8, 15)
        assert parse_date(date(2024, 8, 15)) == date(2024, 8, 15)

    def test_parse_date_invalid_and_null_inputs(self):
        """NULL, empty, or unparseable representations return None safely."""
        assert parse_date(None) is None
        assert parse_date('') is None
        assert parse_date('   ') is None
        assert parse_date('None') is None
        assert parse_date('nan') is None
        assert parse_date('NULL') is None
        assert parse_date('invalid-date-string') is None
        assert parse_date(12345) is None

    def test_undated_transactions_in_ecom_supplies(self, app, db):
        """get_ecom_supplies with include_undated=True includes transactions with NULL date."""
        with app.app_context():
            user = User(username='undated_user', email='undated@test.com')
            user.set_password('pass123')
            db.session.add(user)
            db.session.commit()

            profile = GSTProfile(
                user_id=user.id, gstin='27AABCU9603R1ZM', legal_name='Test Seller',
                state_code='27', state_name='Maharashtra',
                financial_year='2024-25', filing_frequency='monthly'
            )
            db.session.add(profile)
            db.session.commit()

            ih = ImportHistory(
                user_id=user.id, profile_id=profile.id, platform_name='Amazon',
                return_period='082024', financial_year='2024-25',
                processing_status='COMPLETED', file_name='test.xlsx',
                original_file_name='test.xlsx', file_hash='h1', file_size=10
            )
            db.session.add(ih)
            db.session.commit()

            ri = RawImport(import_history_id=ih.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
            db.session.add(ri)
            db.session.commit()

            # Transaction 1: Dated August 2024
            tx1 = Transaction(
                profile_id=profile.id, import_history_id=ih.id, raw_import_id=ri.id,
                invoice_number='DATED-1', invoice_date=date(2024, 8, 10),
                taxable_value=Decimal('5000'), ecommerce_gstin='27ECO0000001Z1',
                place_of_supply='27', tax_rate=Decimal('18'), is_deleted=False
            )
            # Transaction 2: Undated (invoice_date = None) linked to same ImportHistory
            tx2 = Transaction(
                profile_id=profile.id, import_history_id=ih.id, raw_import_id=ri.id,
                invoice_number='UNDATED-1', invoice_date=None,
                taxable_value=Decimal('3000'), ecommerce_gstin='27ECO0000001Z1',
                place_of_supply='27', tax_rate=Decimal('18'), is_deleted=False
            )
            db.session.add_all([tx1, tx2])
            db.session.commit()

            # Without include_undated: only dated transaction
            dated_only = get_ecom_supplies(profile.id, '082024', include_undated=False)
            assert len(dated_only) == 1
            assert dated_only[0]['invoice_number'] == 'DATED-1'

            # With include_undated: both transactions
            with_undated = get_ecom_supplies(profile.id, '082024', include_undated=True)
            assert len(with_undated) == 2
            inv_nums = {t['invoice_number'] for t in with_undated}
            assert inv_nums == {'DATED-1', 'UNDATED-1'}

            # Aggregation: undated row does not get a guessed TCS rate
            agg = aggregate_ecom(with_undated, '082024', seller_state='27')[0]
            assert agg['has_undated_supplies'] is True
            assert agg['undated_supplies'] == Decimal('3000.00')
            # Only the dated 5000 gets TCS calculated (at 0.5% INTRA: 0.25% CGST + 0.25% SGST = 12.50 each)
            assert agg['net_taxable_value'] == Decimal('5000.00')
            assert agg['our_tcs_cgst'] == Decimal('12.50')
            assert agg['our_tcs_sgst'] == Decimal('12.50')
            assert agg['our_tcs_total'] == Decimal('25.00')

            # Reconcile_tcs with include_undated=True surfaces undated rows explicitly
            recon_res = reconcile_tcs(profile.id, '082024', user_id=user.id, include_undated=True)
            assert recon_res['status'] == 'NO_PORTAL_DATA'  # No portal report imported yet, but shows undated warning if transactions exist


# ==============================================================================
# HIGH-06: TCS Upload Security
# ==============================================================================

class TestHigh06UploadSecurity:
    """Security tests for /tcs/upload endpoint."""

    @pytest.fixture
    def authed_client(self, app, db):
        import uuid
        s = uuid.uuid4().hex[:8]
        with app.app_context():
            user = User(username=f'sec_user_{s}', email=f'sec_{s}@example.com')
            user.set_password('secpass123')
            db.session.add(user)
            db.session.commit()

            profile = GSTProfile(
                user_id=user.id, gstin=f'27AABCU{s[:4].upper()}1ZM', legal_name='Sec Test',
                state_code='27', state_name='Maharashtra',
                financial_year='2024-25', filing_frequency='monthly'
            )
            db.session.add(profile)
            db.session.commit()

            client = app.test_client()
            client.post('/login', data={'username': f'sec_user_{s}', 'password': 'secpass123'}, follow_redirects=True)
            client.get(f'/profiles/{profile.id}/select', follow_redirects=True)
            yield client, user, profile

    def test_upload_requires_authentication(self, app, db):
        """Unauthenticated POST to /tcs/upload is redirected to login."""
        # Clear any SQLAlchemy identity-map state from previous tests so
        # that Flask-Login's user_loader cannot accidentally resolve a stale
        # User reference.
        db.session.expire_all()
        with app.test_client() as fresh_client:
            # Explicitly logout to clear any session state
            fresh_client.get('/logout', follow_redirects=True)
            res = fresh_client.post('/tcs/upload', data={'return_period': '082024'})
            assert res.status_code in (301, 302)
            assert '/login' in res.headers.get('Location', '')

    def test_upload_rejects_invalid_return_period(self, authed_client):
        """POST with invalid return period format is rejected."""
        client, _, _ = authed_client
        res = client.post('/tcs/upload', data={
            'return_period': 'invalid',
            'file': (io.BytesIO(b'dummy'), 'test.xlsx')
        }, content_type='multipart/form-data', follow_redirects=True)
        assert res.status_code == 200
        assert b'Invalid return period' in res.data

    def test_upload_rejects_non_xlsx_extension(self, authed_client):
        """Files without .xlsx extension (.csv, .xls, .pdf) are rejected."""
        client, _, _ = authed_client
        for ext in ['test.csv', 'test.xls', 'test.pdf', 'test.exe']:
            res = client.post('/tcs/upload', data={
                'return_period': '082024',
                'file': (io.BytesIO(b'dummy content'), ext)
            }, content_type='multipart/form-data', follow_redirects=True)
            assert b'Only Excel (.xlsx) files are supported' in res.data

    def test_upload_rejects_non_zip_magic_bytes(self, authed_client):
        """Files named .xlsx but with invalid content (magic bytes != PK\\x03\\x04) are rejected."""
        client, _, _ = authed_client
        res = client.post('/tcs/upload', data={
            'return_period': '082024',
            'file': (io.BytesIO(b'NOT A REAL XLSX FILE HEADER'), 'test.xlsx')
        }, content_type='multipart/form-data', follow_redirects=True)
        assert b'The file is not a valid Excel (.xlsx) spreadsheet' in res.data

    def test_upload_cleans_up_temp_file_on_every_exit_path(self, authed_client, tmp_path):
        """Temporary upload file is cleanly unlinked on success, error, or invalid content."""
        client, _, profile = authed_client
        temp_dir = tempfile.gettempdir()

        # Clean any stale artifacts before test execution to ensure test isolation
        for f in os.listdir(temp_dir):
            if f.startswith('tcs_upload_'):
                try:
                    os.remove(os.path.join(temp_dir, f))
                except OSError:
                    pass

        def make_valid_xlsx():
            w = openpyxl.Workbook()
            s = w.active
            s.append(['State Code', 'State Name', 'Taxable Value', 'TCS Amount', 'CGST TCS', 'SGST TCS', 'IGST TCS'])
            s.append(['27', 'Maharashtra', 10000, 50, 25, 25, 0])
            b = io.BytesIO()
            w.save(b)
            w.close()
            b.seek(0)
            return b

        # 1. Exit Path: Success (valid xlsx workbook)
        res = client.post('/tcs/upload', data={
            'return_period': '082024',
            'file': (make_valid_xlsx(), 'valid_tcs.xlsx')
        }, content_type='multipart/form-data', follow_redirects=True)
        assert res.status_code == 200
        assert b'TCS report imported successfully' in res.data

        # Verify valid TCS data remains persisted correctly
        persisted = TCSReconciliation.query.filter_by(profile_id=profile.id, return_period='082024').all()
        assert len(persisted) == 1
        assert persisted[0].state_code == '27'
        assert persisted[0].portal_taxable_value == Decimal('10000')

        # Verify no files with prefix 'tcs_upload_' remain in system tempdir
        stale_files = [f for f in os.listdir(temp_dir) if f.startswith('tcs_upload_')]
        assert len(stale_files) == 0, f"Stale temp upload files found: {stale_files}"

        # 2. Exit Path: Invalid workbook (magic bytes PK\x03\x04 but invalid xlsx body)
        corrupt_buf = io.BytesIO(b'PK\x03\x04\x00\x00invalid_corrupt_zip_stream_bytes')
        res_corrupt = client.post('/tcs/upload', data={
            'return_period': '082024',
            'file': (corrupt_buf, 'corrupt.xlsx')
        }, content_type='multipart/form-data', follow_redirects=True)
        assert res_corrupt.status_code == 200
        stale_files = [f for f in os.listdir(temp_dir) if f.startswith('tcs_upload_')]
        assert len(stale_files) == 0, f"Stale temp files found after corrupt upload: {stale_files}"

        # 3. Exit Path: Parser failure (simulated parser exception)
        with patch('app.services.tcs_service.TCSReportParser.parse_file', side_effect=RuntimeError("Simulated parser failure")):
            res_err = client.post('/tcs/upload', data={
                'return_period': '082024',
                'file': (make_valid_xlsx(), 'valid_tcs.xlsx')
            }, content_type='multipart/form-data', follow_redirects=True)
            assert res_err.status_code == 200

        stale_files = [f for f in os.listdir(temp_dir) if f.startswith('tcs_upload_')]
        assert len(stale_files) == 0, f"Stale temp files found after parser failure: {stale_files}"


# ==============================================================================
# MED-01: Exception Disclosure
# ==============================================================================

class TestMed01ExceptionDisclosure:
    """Verify that unexpected server exceptions return safe messages without raw leakage."""

    @pytest.fixture
    def authed_client(self, app, db):
        import uuid
        s = uuid.uuid4().hex[:8]
        with app.app_context():
            user = User(username=f'med_user_{s}', email=f'med_{s}@example.com')
            user.set_password('medpass123')
            db.session.add(user)
            db.session.commit()

            profile = GSTProfile(
                user_id=user.id, gstin=f'27AABCU{s[:4].upper()}2ZM', legal_name='Med Test',
                state_code='27', state_name='Maharashtra',
                financial_year='2024-25', filing_frequency='monthly'
            )
            db.session.add(profile)
            db.session.commit()

            ih = ImportHistory(
                user_id=user.id, profile_id=profile.id, platform_name='Manual',
                return_period='082024', financial_year='2024-25',
                processing_status='COMPLETED', file_name='test.xlsx',
                original_file_name='test.xlsx', file_hash=f'h_med_{s}', file_size=10
            )
            db.session.add(ih)
            db.session.commit()

            ri = RawImport(import_history_id=ih.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
            db.session.add(ri)
            db.session.commit()

            client = app.test_client()
            client.post('/login', data={'username': f'med_user_{s}', 'password': 'medpass123'}, follow_redirects=True)
            client.get(f'/profiles/{profile.id}/select', follow_redirects=True)
            yield client, user, profile, ih, ri

    def test_statement_edit_exception_returns_safe_message(self, authed_client):
        """When transaction edit raises unexpected exception, raw details are not exposed."""
        client, _, profile, ih, ri = authed_client
        tx = Transaction(
            profile_id=profile.id, import_history_id=ih.id, raw_import_id=ri.id,
            invoice_number='INV-MED1', taxable_value=Decimal('1000')
        )
        _db.session.add(tx)
        _db.session.commit()
        tx_id = tx.id

        with patch('app.routes.statement.AuditLog', side_effect=RuntimeError("Internal DB Secret Crash")):
            res = client.post(f'/statement/edit/{tx_id}', json={'taxable_value': 2000})
            assert res.status_code == 400
            data = res.get_json()
            assert data['error'] is True
            assert 'Internal DB Secret Crash' not in data['message']
            assert 'An error occurred while updating the transaction.' in data['message']

    def test_statement_delete_exception_returns_safe_message(self, authed_client):
        """When transaction delete raises unexpected exception, raw details are not exposed."""
        client, _, profile, ih, ri = authed_client
        tx = Transaction(
            profile_id=profile.id, import_history_id=ih.id, raw_import_id=ri.id,
            invoice_number='INV-MED2', taxable_value=Decimal('1000')
        )
        _db.session.add(tx)
        _db.session.commit()
        tx_id = tx.id

        with patch('app.routes.statement.AuditLog', side_effect=RuntimeError("Internal Delete Secret Crash")):
            res = client.post(f'/statement/delete/{tx_id}', json={'reason': 'test'})
            assert res.status_code == 400
            data = res.get_json()
            assert data['error'] is True
            assert 'Internal Delete Secret Crash' not in data['message']
            assert 'An error occurred while deleting the transaction.' in data['message']

    def test_tcs_reconcile_exception_returns_safe_message(self, authed_client):
        """When reconcile raises unexpected exception, flash does not leak raw details."""
        client, _, _, _, _ = authed_client
        with patch('app.routes.tcs.reconcile_tcs', side_effect=RuntimeError("Sensitive Stack Trace Secret")):
            res = client.post('/tcs/reconcile', data={'return_period': '082024'}, follow_redirects=True)
            assert res.status_code == 200
            assert b'Sensitive Stack Trace Secret' not in res.data
            assert b'An error occurred during reconciliation' in res.data

    def test_tcs_reconcile_rejects_invalid_return_period(self, authed_client):
        """Reconcile route validates return_period format, not just emptiness."""
        client, _, _, _, _ = authed_client
        for bad_rp in ['abc', '13-2024', '002024', '132024', 'ABCDEF', '../08']:
            res = client.post('/tcs/reconcile', data={'return_period': bad_rp}, follow_redirects=True)
            assert res.status_code == 200
            assert b'Invalid return period' in res.data, f"Expected rejection for '{bad_rp}'"

    def test_generate_exception_returns_safe_message(self, authed_client):
        """When GSTR-1 generation raises, raw exception is not exposed to user."""
        client, _, profile, _, _ = authed_client
        with patch('app.routes.generate.generate_gstr1', side_effect=RuntimeError("Internal generation secret")):
            res = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
            assert res.status_code == 200
            assert b'Internal generation secret' not in res.data

    def test_profile_create_exception_returns_safe_message(self, authed_client):
        """When profile creation raises, raw exception is not exposed to user."""
        client, _, _, _, _ = authed_client
        with patch('app.routes.profile.db.session.commit', side_effect=RuntimeError("DB internal secret")):
            res = client.post('/profiles/create', data={
                'gstin': '27AABCU9603R1ZM', 'legal_name': 'Fail Test',
                'state_code': '27', 'state_name': 'Maharashtra',
                'financial_year': '2024-25', 'filing_frequency': 'monthly'
            }, follow_redirects=True)
            assert res.status_code == 200
            assert b'DB internal secret' not in res.data

    def test_tcs_upload_exception_cleans_up_and_returns_safe_message(self, authed_client):
        """When import_tcs_report raises an unexpected exception, the temp file is cleaned
        and the user sees a safe error message without raw details."""
        client, _, _, _, _ = authed_client
        temp_dir = tempfile.gettempdir()
        for f in os.listdir(temp_dir):
            if f.startswith('tcs_upload_'):
                try:
                    os.remove(os.path.join(temp_dir, f))
                except OSError:
                    pass

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['State Code', 'State Name', 'Taxable Value', 'TCS Amount', 'CGST TCS', 'SGST TCS', 'IGST TCS'])
        ws.append(['27', 'Maharashtra', 10000, 50, 25, 25, 0])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        buf.seek(0)

        with patch('app.routes.tcs.import_tcs_report', side_effect=RuntimeError("Disk failure secret")):
            res = client.post('/tcs/upload', data={
                'return_period': '082024',
                'file': (buf, 'valid_tcs.xlsx')
            }, content_type='multipart/form-data', follow_redirects=True)
            assert res.status_code == 200
            assert b'Disk failure secret' not in res.data
            assert b'An error occurred while processing the TCS upload' in res.data

        # Verify cleanup
        stale = [f for f in os.listdir(temp_dir) if f.startswith('tcs_upload_')]
        assert len(stale) == 0, f"Stale temp files found: {stale}"
