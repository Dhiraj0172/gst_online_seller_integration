"""
Phase 3A Remediation Test Suite.

Verifies:
- HIGH-01: Import failure recovery — unexpected errors must transition ImportHistory to FAILED,
           rollback partial Transaction and RawImport rows, and avoid blocking subsequent imports.
- HIGH-03: Login open redirect prevention — strictly reject external, protocol-relative,
           and encoded bypass targets while allowing safe internal paths.
- HIGH-04: CSV formula injection prevention — sanitize formula triggers (=, +, -, @, \\t, \\r)
           across statement and TCS CSV exports while preserving numbers and regular data.
"""
import io
import csv
import json
import pytest
from decimal import Decimal
from datetime import datetime

from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, TCSReconciliation
from app.extensions import db as _db
from app.routes.auth import is_safe_redirect_url
from app.utils.csv_utils import sanitize_csv_value
from app.services.tcs_service import export_reconciliation


@pytest.fixture(autouse=True)
def clean_isolation(app):
    """Ensure clean database and session state before and after each test in Phase 3A."""
    with app.app_context():
        try:
            from flask import g
            g.pop('_login_user', None)
            Transaction.query.delete()
            RawImport.query.delete()
            ImportHistory.query.delete()
            TCSReconciliation.query.delete()
            GSTProfile.query.delete()
            User.query.delete()
            _db.session.commit()
        except Exception:
            _db.session.rollback()
    yield
    with app.app_context():
        try:
            from flask import g
            g.pop('_login_user', None)
            Transaction.query.delete()
            RawImport.query.delete()
            ImportHistory.query.delete()
            TCSReconciliation.query.delete()
            GSTProfile.query.delete()
            User.query.delete()
            _db.session.commit()
        except Exception:
            _db.session.rollback()


# ══════════════════════════════════════════════════════════════════════════
# HIGH-03: Login Open Redirect Prevention Tests
# ══════════════════════════════════════════════════════════════════════════

class TestLoginRedirectSecurity:
    """Test unit validation and HTTP response behavior for open redirect prevention."""

    @pytest.mark.parametrize("target,expected", [
        # Safe internal targets
        ("/dashboard", True),
        ("/statement", True),
        ("/statement?period=012025", True),
        ("/statement?period=012025&page=2#summary", True),
        ("/profiles/1/edit", True),
        ("/import/history", True),

        # Unsafe targets
        ("", False),
        (None, False),
        ("   ", False),
        ("dashboard", False),  # Relative path without leading slash
        ("https://attacker.example", False),
        ("http://attacker.example", False),
        ("https://attacker.example/dashboard", False),
        ("http://attacker.example/dashboard", False),
        ("//attacker.example", False),
        ("//attacker.example/dashboard", False),
        ("///attacker.example", False),
        ("////attacker.example", False),
        ("\\\\attacker.example", False),
        ("/\\attacker.example", False),
        ("\\attacker.example", False),
        ("/%5cattacker.example", False),
        ("/%2f%2fattacker.example", False),
        ("javascript:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
    ])
    def test_is_safe_redirect_url_unit(self, target, expected):
        """Verify redirect validation rejects external/protocol-relative/malicious URLs."""
        assert is_safe_redirect_url(target) == expected

    def test_login_redirect_internal_path(self, client, app):
        """Normal internal relative path in 'next' is honored upon successful login."""
        username = 'redirect_user1'
        password = 'SecretPass123!'
        with app.app_context():
            u = User(username=username, email='red1@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

        resp = client.post('/login?next=/statement?period=012025', data={
            'username': username,
            'password': password
        })
        assert resp.status_code == 302
        assert resp.headers['Location'] == '/statement?period=012025'

    def test_login_redirect_no_next_param(self, client, app):
        """When next parameter is absent, redirects to default dashboard."""
        username = 'redirect_user2'
        password = 'SecretPass123!'
        with app.app_context():
            u = User(username=username, email='red2@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

        resp = client.post('/login', data={
            'username': username,
            'password': password
        })
        assert resp.status_code == 302
        assert resp.headers['Location'] == '/dashboard'

    @pytest.mark.parametrize("malicious_target", [
        "https://attacker.example",
        "http://attacker.example/phish",
        "//attacker.example",
        "///attacker.example",
        "\\attacker.example",
        "/%5cattacker.example",
        "/%2f%2fattacker.example",
        "javascript:alert(1)",
    ])
    def test_login_rejects_external_and_malicious_next(self, client, app, malicious_target):
        """Unsafe next destinations fall back to internal dashboard."""
        username = f'red_user_{abs(hash(malicious_target))}'
        password = 'SecretPass123!'
        with app.app_context():
            u = User(username=username, email=f'{username}@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

        resp = client.post(f'/login?next={malicious_target}', data={
            'username': username,
            'password': password
        })
        assert resp.status_code == 302
        assert resp.headers['Location'] == '/dashboard', f"Failed to reject malicious target {malicious_target}"


# ══════════════════════════════════════════════════════════════════════════
# HIGH-04: CSV Formula Injection Prevention Tests
# ══════════════════════════════════════════════════════════════════════════

class TestCsvFormulaInjectionPrevention:
    """Test sanitization of formula triggers in CSV exports."""

    @pytest.mark.parametrize("input_val,expected_output", [
        ("=1+1", "'=1+1"),
        ("+CMD('calc')", "'+CMD('calc')"),
        ("-10", "'-10"),
        ("@SUM(A1:A10)", "'@SUM(A1:A10)"),
        ("\tmalicious_tab", "'\tmalicious_tab"),
        ("\rmalicious_cr", "'\rmalicious_cr"),
        ("  =leading_spaces", "'  =leading_spaces"),
        ("  +leading_plus", "'  +leading_plus"),
        ("  @leading_at", "'  @leading_at"),
        ("INV-123", "INV-123"),
        ("GSTIN27AAAAA0000A1Z5", "GSTIN27AAAAA0000A1Z5"),
        ("ABC123", "ABC123"),
        ("1000", "1000"),
        (1000, 1000),
        (1000.50, 1000.50),
        (-10, -10),
        (Decimal("1000.00"), Decimal("1000.00")),
        (Decimal("-50.25"), Decimal("-50.25")),
        ("", ""),
        (None, ""),
    ])
    def test_sanitize_csv_value_unit(self, input_val, expected_output):
        """Verify formula triggers are escaped with single quote, numbers and safe text preserved."""
        assert sanitize_csv_value(input_val) == expected_output

    def test_statement_csv_export_sanitizes_dangerous_fields(self, app):
        """Exporting statement CSV sanitizes formula injections in invoice_number and customer_gstin."""
        username = 'csv_seller'
        password = 'Pass123!'
        with app.app_context():
            u = User(username=username, email='csv_seller@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

            prof = GSTProfile(
                user_id=u.id,
                gstin='27ABCDE1234F1Z5',
                legal_name='CSV Test Store',
                state_code='27',
                state_name='Maharashtra',
                financial_year='2024-25',
                filing_frequency='monthly'
            )
            _db.session.add(prof)
            _db.session.commit()

            imp = ImportHistory(
                profile_id=prof.id,
                user_id=u.id,
                file_name='test.csv',
                original_file_name='test.csv',
                file_hash='hash1',
                file_size=100,
                platform_name='Generic',
                return_period='012025',
                financial_year='2024-25',
                processing_status='COMPLETED'
            )
            _db.session.add(imp)
            _db.session.commit()

            # Create a RawImport (required FK for Transaction.raw_import_id)
            raw_imp = RawImport(
                import_history_id=imp.id,
                row_number=1,
                raw_data='{}',
                status='SUCCESS'
            )
            _db.session.add(raw_imp)
            _db.session.flush()

            # Malicious invoice number and GSTIN containing formula triggers
            tx = Transaction(
                profile_id=prof.id,
                import_history_id=imp.id,
                raw_import_id=raw_imp.id,
                invoice_number='=1+1',
                customer_gstin='+CMD(calc)',
                supply_type='B2B',
                taxable_value=Decimal('500.00'),
                total_tax=Decimal('90.00'),
                invoice_value=Decimal('590.00'),
                is_deleted=False
            )
            _db.session.add(tx)
            _db.session.commit()
            prof_id = prof.id

        client = app.test_client()
        client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)

        with client.session_transaction() as sess:
            sess['active_profile_id'] = prof_id

        resp = client.get('/statement/export/B2B')
        assert resp.status_code == 200
        content = resp.data.decode('utf-8')
        rows = list(csv.reader(io.StringIO(content)))

        assert len(rows) >= 2
        data_row = rows[1]
        # Invoice number was '=1+1', must be sanitized to ''=1+1'
        assert data_row[1] == "'=1+1"
        # Customer GSTIN was '+CMD(calc)', must be sanitized to "'+CMD(calc)'"
        assert data_row[3] == "'+CMD(calc)"
        # Amounts must remain numbers
        assert float(data_row[4]) == 500.0
        assert float(data_row[5]) == 90.0
        assert float(data_row[6]) == 590.0

    def test_tcs_csv_export_sanitizes_dangerous_fields(self, app):
        """Exporting TCS reconciliation CSV sanitizes formula triggers in text fields."""
        with app.app_context():
            u = User(username='tcs_csv_user', email='tcs_csv@test.com')
            u.set_password('Pass123!')
            _db.session.add(u)
            _db.session.commit()

            prof = GSTProfile(
                user_id=u.id,
                gstin='27ABCDE5678F1Z9',
                legal_name='TCS CSV Entity',
                state_code='27',
                state_name='Maharashtra',
                financial_year='2024-25',
                filing_frequency='monthly'
            )
            _db.session.add(prof)
            _db.session.commit()

            recon = TCSReconciliation(
                profile_id=prof.id,
                user_id=u.id,
                return_period='012025',
                state_code='27',
                state_name='Maharashtra',
                ecommerce_gstin='@SUM(A1:A10)',
                our_net_taxable_value=Decimal('1000.00'),
                portal_taxable_value=Decimal('1000.00'),
                difference_taxable=Decimal('-10.00'),
                our_calculated_tcs=Decimal('10.00'),
                portal_tcs=Decimal('10.00'),
                difference_tcs=Decimal('0.00'),
                match_status='MISMATCH',
                ambiguity_details='\talert("tab")',
                is_adjusted=True,
                adjustment_notes='-10'
            )
            _db.session.add(recon)
            _db.session.commit()
            prof_id = prof.id

            csv_text = export_reconciliation(prof_id, '012025')
            rows = list(csv.reader(io.StringIO(csv_text)))

            assert len(rows) >= 2
            data_row = rows[1]
            # ecommerce_gstin was '@SUM(A1:A10)'
            assert data_row[2] == "'@SUM(A1:A10)"
            # difference_taxable is a number: -10.00
            assert data_row[5] == "-10.00"
            # ambiguity_details was '\talert("tab")'
            assert data_row[19] == "'\talert(\"tab\")"
            # adjustment_notes was '-10'
            assert data_row[21] == "'-10"


# ══════════════════════════════════════════════════════════════════════════
# HIGH-01: Import Failure Recovery Tests
# ══════════════════════════════════════════════════════════════════════════

class TestImportFailureRecovery:
    """Verify that unexpected exceptions during import transition ImportHistory to FAILED

    and roll back any partial database writes without blocking subsequent imports.
    """

    def test_unexpected_error_transitions_import_history_to_failed(self, app, monkeypatch):
        """An unhandled exception in processing sets processing_status='FAILED' and rolls back rows."""
        username = 'fail_user'
        password = 'Pass123!'
        with app.app_context():
            u = User(username=username, email='fail@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

            prof = GSTProfile(
                user_id=u.id,
                gstin='27ABCDE9999F1Z1',
                legal_name='Fail Test Co',
                state_code='27',
                state_name='Maharashtra',
                financial_year='2024-25',
                filing_frequency='monthly'
            )
            _db.session.add(prof)
            _db.session.commit()
            prof_id = prof.id

        client = app.test_client()
        client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)
        with client.session_transaction() as sess:
            sess['active_profile_id'] = prof_id
            sess['return_period'] = '012025'

        # Create valid CSV content
        csv_content = (
            "Invoice Number,Invoice Date,Customer GSTIN,Place of Supply,Taxable Value,Rate,CGST,SGST,IGST,Total Value\n"
            "INV-FAIL-01,01-01-2025,27AAAAA0000A1Z5,27,1000,18,90,90,0,1180\n"
        )
        data = {
            'file': (io.BytesIO(csv_content.encode('utf-8')), 'test_fail.csv'),
            'platform': 'Generic'
        }

        # Monkeypatch classify_transaction to raise an unexpected runtime error
        def mock_crash(*args, **kwargs):
            raise RuntimeError("Simulated unexpected crash during row normalization")

        monkeypatch.setattr('app.routes.import_routes.classify_transaction', mock_crash)

        resp = client.post('/import/upload', data=data, content_type='multipart/form-data', follow_redirects=False)
        assert resp.status_code == 302

        with app.app_context():
            # Verify ImportHistory record exists and transitioned to FAILED
            rec = ImportHistory.query.filter_by(file_name='test_fail.csv', profile_id=prof_id).first()
            assert rec is not None, "ImportHistory record must be preserved"
            assert rec.processing_status == 'FAILED', f"Expected FAILED status, got {rec.processing_status}"
            assert rec.processing_completed_at is not None
            assert rec.error_summary is not None
            summary = json.loads(rec.error_summary)
            assert any('Simulated unexpected crash' in str(err) for err in summary.get('errors', []))

            # Verify no partial Transaction rows exist
            tx_count = Transaction.query.filter_by(import_history_id=rec.id).count()
            assert tx_count == 0, f"Expected 0 transactions after failure rollback, found {tx_count}"

            # Verify no partial RawImport rows exist
            raw_count = RawImport.query.filter_by(import_history_id=rec.id).count()
            assert raw_count == 0, f"Expected 0 raw_imports after failure rollback, found {raw_count}"

            # Verify subsequent import of the same file is NOT blocked as duplicate
            # find_prior_file_import only looks at COMPLETED/PARTIAL imports
            from app.services.duplicate_service import find_prior_file_import
            prior = find_prior_file_import(prof_id, rec.file_hash, 'Generic')
            assert prior is None, "A FAILED import must not be treated as a prior completed import"

    def test_successful_import_still_completes(self, app):
        """A normal import (no crash) transitions to COMPLETED/PARTIAL as before."""
        username = 'success_user'
        password = 'Pass123!'
        with app.app_context():
            u = User(username=username, email='success@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

            prof = GSTProfile(
                user_id=u.id,
                gstin='27ABCDE8888F1Z2',
                legal_name='Success Test Co',
                state_code='27',
                state_name='Maharashtra',
                financial_year='2024-25',
                filing_frequency='monthly'
            )
            _db.session.add(prof)
            _db.session.commit()
            prof_id = prof.id

        client = app.test_client()
        client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)
        with client.session_transaction() as sess:
            sess['active_profile_id'] = prof_id
            sess['return_period'] = '012025'

        csv_content = (
            "Invoice Number,Invoice Date,Customer GSTIN,Place of Supply,Taxable Value,Rate,CGST,SGST,IGST,Total Value\n"
            "INV-OK-01,01-01-2025,27AAAAA0000A1Z5,27,1000,18,90,90,0,1180\n"
        )
        data = {
            'file': (io.BytesIO(csv_content.encode('utf-8')), 'test_success.csv'),
            'platform': 'Generic'
        }

        resp = client.post('/import/upload', data=data, content_type='multipart/form-data', follow_redirects=False)
        assert resp.status_code == 302

        with app.app_context():
            rec = ImportHistory.query.filter_by(file_name='test_success.csv', profile_id=prof_id).first()
            assert rec is not None
            assert rec.processing_status in ('COMPLETED', 'PARTIAL'), \
                f"Expected COMPLETED or PARTIAL, got {rec.processing_status}"
            assert rec.processing_completed_at is not None

            # Transactions were created
            tx_count = Transaction.query.filter_by(import_history_id=rec.id).count()
            assert tx_count >= 1, f"Expected at least 1 transaction, found {tx_count}"

    def test_handled_validation_failure_preserves_existing_behavior(self, app):
        """An invalid file (e.g. empty CSV) follows the existing FAILED path, not the new catch-all."""
        username = 'valid_fail_user'
        password = 'Pass123!'
        with app.app_context():
            u = User(username=username, email='validfail@test.com')
            u.set_password(password)
            _db.session.add(u)
            _db.session.commit()

            prof = GSTProfile(
                user_id=u.id,
                gstin='27ABCDE7777F1Z3',
                legal_name='Validation Fail Co',
                state_code='27',
                state_name='Maharashtra',
                financial_year='2024-25',
                filing_frequency='monthly'
            )
            _db.session.add(prof)
            _db.session.commit()
            prof_id = prof.id

        client = app.test_client()
        client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)
        with client.session_transaction() as sess:
            sess['active_profile_id'] = prof_id
            sess['return_period'] = '012025'

        # Empty CSV (only whitespace) triggers a handled validation failure
        csv_content = "   \n   \n"
        data = {
            'file': (io.BytesIO(csv_content.encode('utf-8')), 'test_empty.csv'),
            'platform': 'Generic'
        }

        resp = client.post('/import/upload', data=data, content_type='multipart/form-data', follow_redirects=False)
        assert resp.status_code == 302

        with app.app_context():
            rec = ImportHistory.query.filter_by(file_name='test_empty.csv', profile_id=prof_id).first()
            assert rec is not None
            assert rec.processing_status == 'FAILED', f"Expected FAILED, got {rec.processing_status}"
            # The error_summary should indicate the handled failure (not UNEXPECTED_ERROR)
            assert rec.error_summary is not None
            summary = json.loads(rec.error_summary)
            error_codes = [e.get('code') for e in summary.get('errors', []) if isinstance(e, dict)]
            assert 'UNEXPECTED_ERROR' not in error_codes, \
                "Handled validation failures must NOT use the unexpected error path"
            assert rec.processing_completed_at is not None
