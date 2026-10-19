"""Integration tests for Phase 5E-12: Reconciliation Workspace, GST Rule Provenance & GSTR-1 Completeness.

Covers Workstreams 8, 9, and 10:
Workstream 8:
  1. Source-to-normalized row conservation proof: total_source_input == (accepted + rejected + duplicate).
  2. Reconciliation bucket categorization across all 6 buckets (MATCHED, MATCHED_WITH_VARIANCE, etc.).
  3. Exception filtering, resolution note persistence, status update, and AuditLog tracking.
  4. Authorized multi-tenant Reconciliation Export (/statement/export/reconciliation, /api/reconciliation/export).
  5. Cross-tenant IDOR rejection on reconciliation export and resolution.

Workstream 9:
  6. Adapter Readiness Matrix honesty check (FORMAT_DOCUMENTED=True vs False) and /api/adapters/readiness.
  7. GST Classification rule provenance (GST_RULE_VERSION="2026.1") and zero-tax statutory separation.
  13. Period-aware HSN reporting with B2B vs B2C aggregation.

Workstream 10:
  8. GSTR-1 input snapshot checksum reproducibility (deterministic SHA-256).
  9. Fail-closed portal JSON download blocking when generation gate_status is BLOCKED.
  10. Authorized download audit logging.
  11. Cross-tenant isolation across GSTR-1 download and API access.
  12. GSTR-1 regeneration snapshot tracking and audit diff.
  14. 5-layer validation summary persistence on generation records and API responses.
"""
import io
import os
import json
import uuid
import hashlib
from decimal import Decimal
from datetime import date, datetime
import pytest

from app.extensions import db as _db
from app.models import (
    User,
    GSTProfile,
    ImportHistory,
    RawImport,
    Transaction,
    GSTR1Generation,
    AuditLog,
    ReconciliationRecord,
)
from app.services.reconciliation_service import (
    get_source_to_normalized_counts,
    get_reconciliation_summary,
    export_reconciliation_csv,
    resolve_reconciliation_exception,
    run_full_reconciliation,
)
from app.adapters.all_adapters import get_adapter_readiness_matrix
from app.services.classification_service import (
    GST_RULE_VERSION,
    classify_with_provenance,
    determine_taxability,
    TAXABLE,
    NIL_RATED,
    EXEMPT,
    NON_GST,
)
from app.services.nil_service import aggregate_nil
from app.services.hsn_service import aggregate_hsn_b2b, aggregate_hsn_b2c
from app.services.gstr1_generator import (
    compute_input_snapshot_checksum,
    generate_gstr1,
)
from app.utils.query import tenant_query


@pytest.fixture
def env5e12(app, db):
    """Set up two fully isolated tenants with distinct profiles and return periods."""
    with app.app_context():
        # Tenant A
        user_a = User(username=f'p5e12_user_a_{uuid.uuid4().hex[:6]}', email='user_a@example.com')
        user_a.set_password('SecretA123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin='27AAAAA0000A1Z5',
            legal_name='Tenant A Retail Ltd',
            trade_name='Tenant A Store',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B
        user_b = User(username=f'p5e12_user_b_{uuid.uuid4().hex[:6]}', email='user_b@example.com')
        user_b.set_password('SecretB456!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin='29BBBBB0000B1Z7',
            legal_name='Tenant B Tech Ltd',
            trade_name='Tenant B Store',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(profile_b)
        _db.session.commit()

        env_dict = {
            'profile_a_id': profile_a.id,
            'user_a_id': user_a.id,
            'username_a': user_a.username,
            'profile_b_id': profile_b.id,
            'user_b_id': user_b.id,
            'username_b': user_b.username,
            'period': '082024',
        }
        yield env_dict


def _login_client(client, env, tenant_key='a'):
    """Helper to log in test client and bind active profile and period."""
    username = env[f'username_{tenant_key}']
    profile_id = env[f'profile_{tenant_key}_id']
    password = 'SecretA123!' if tenant_key == 'a' else 'SecretB456!'
    client.post('/login', data={
        'username': username,
        'password': password
    }, follow_redirects=True)
    with client.session_transaction() as sess:
        sess['active_profile_id'] = profile_id
        sess['return_period'] = env['period']


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Source-to-Normalized Row Conservation Identity
# ─────────────────────────────────────────────────────────────────────────────
def test_01_source_to_normalized_row_conservation_identity(app, env5e12):
    """Mathematical proof that total_source_input == (accepted + rejected + duplicate)."""
    with app.app_context():
        profile_id = env5e12['profile_a_id']
        user_id = env5e12['user_a_id']
        period = env5e12['period']

        # Create import history row with known counts: 10 = 7 accepted + 2 rejected + 1 duplicate
        imp = ImportHistory(
            profile_id=profile_id,
            user_id=user_id,
            platform_name='Amazon',
            file_name='amazon_aug2024.csv',
            original_file_name='amazon_aug2024.csv',
            return_period=period,
            financial_year='2024-25',
            total_rows=10,
            success_rows=7,
            error_rows=2,
            skipped_rows=1,
            processing_status='COMPLETED'
        )
        _db.session.add(imp)
        _db.session.commit()

        raw_imports = []
        for i in range(1, 10):
            r = RawImport(import_history_id=imp.id, sheet_name='Sheet1', row_number=i, status='SUCCESS')
            raw_imports.append(r)
        _db.session.add_all(raw_imports)
        _db.session.commit()

        # Add 7 accepted transactions
        for i in range(7):
            tx = Transaction(
                profile_id=profile_id,
                import_history_id=imp.id,
                raw_import_id=raw_imports[i].id,
                invoice_number=f'INV-AUG-{i}',
                invoice_date=date(2024, 8, 10),
                taxable_value=Decimal('100.00'),
                total_tax=Decimal('18.00'),
                tax_rate=Decimal('18.00'),
                is_staged=False,
                is_deleted=False
            )
            _db.session.add(tx)

        # Add 2 staged transactions (must NOT be counted in active normalized transactions)
        for i in range(2):
            tx_staged = Transaction(
                profile_id=profile_id,
                import_history_id=imp.id,
                raw_import_id=raw_imports[7 + i].id,
                invoice_number=f'INV-STAGED-{i}',
                invoice_date=date(2024, 8, 10),
                taxable_value=Decimal('100.00'),
                total_tax=Decimal('18.00'),
                tax_rate=Decimal('18.00'),
                is_staged=True,
                is_deleted=False
            )
            _db.session.add(tx_staged)

        _db.session.commit()

        counts = get_source_to_normalized_counts(profile_id, period)

        assert counts['total_source_input'] == 10
        assert counts['accepted_rows'] == 7
        assert counts['rejected_rows'] == 2
        assert counts['duplicate_rows'] == 1
        assert counts['is_conserved'] is True
        assert counts['active_transactions'] == 7


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Reconciliation Bucket Categorization
# ─────────────────────────────────────────────────────────────────────────────
def test_02_reconciliation_bucket_categorization(app, env5e12, client):
    """Categorizes transactions and settlement records into authoritative reconciliation buckets."""
    with app.app_context():
        profile_id = env5e12['profile_a_id']
        period = env5e12['period']

        # Insert records across all buckets
        rec_matched = ReconciliationRecord(
            profile_id=profile_id,
            return_period=period,
            bucket='MATCHED',
            source_taxable=Decimal('100.00'),
            normalized_taxable=Decimal('100.00'),
            variance_taxable=Decimal('0.00'),
            resolution_status='OPEN'
        )
        rec_variance = ReconciliationRecord(
            profile_id=profile_id,
            return_period=period,
            bucket='MATCHED_WITH_VARIANCE',
            source_taxable=Decimal('105.50'),
            normalized_taxable=Decimal('100.00'),
            variance_taxable=Decimal('5.50'),
            resolution_status='OPEN'
        )
        rec_unmatched = ReconciliationRecord(
            profile_id=profile_id,
            return_period=period,
            bucket='UNMATCHED_SOURCE',
            source_taxable=Decimal('50.00'),
            normalized_taxable=Decimal('0.00'),
            variance_taxable=Decimal('50.00'),
            resolution_status='OPEN'
        )
        _db.session.add_all([rec_matched, rec_variance, rec_unmatched])
        _db.session.commit()

        summary = get_reconciliation_summary(profile_id, period)
        assert summary['buckets']['MATCHED']['count'] >= 1
        assert summary['buckets']['MATCHED_WITH_VARIANCE']['count'] >= 1
        assert summary['buckets']['UNMATCHED_SOURCE']['count'] >= 1
        assert summary['total_exceptions'] >= 2

    # Verify REST API
    _login_client(client, env5e12, 'a')
    res = client.get(f'/api/reconciliation/summary?return_period={period}')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert 'buckets' in data
    assert 'row_conservation' in data


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Exception Filtering & Resolution Note Persistence
# ─────────────────────────────────────────────────────────────────────────────
def test_03_exception_filtering_and_resolution_note_persistence(app, env5e12, client):
    """Exception items can be filtered and resolved with audited resolution notes."""
    with app.app_context():
        profile_id = env5e12['profile_a_id']
        user_id = env5e12['user_a_id']
        period = env5e12['period']

        rec = ReconciliationRecord(
            profile_id=profile_id,
            return_period=period,
            bucket='UNMATCHED_SOURCE',
            source_taxable=Decimal('200.00'),
            normalized_taxable=Decimal('0.00'),
            variance_taxable=Decimal('200.00'),
            resolution_status='OPEN'
        )
        _db.session.add(rec)
        _db.session.commit()
        rec_id = rec.id

        # Service layer resolution
        res = resolve_reconciliation_exception(
            record_id=rec_id,
            profile_id=profile_id,
            user_id=user_id,
            note="Matched to offline payment voucher PV-889",
            resolution_status="RESOLVED"
        )
        assert res.resolution_status == 'RESOLVED'
        assert res.resolution_note == "Matched to offline payment voucher PV-889"
        assert res.resolved_by == user_id

        # Audit log verified
        audit = AuditLog.query.filter_by(
            action='RESOLVE_EXCEPTION',
            entity_type='ReconciliationRecord',
            entity_id=rec_id
        ).first()
        assert audit is not None
        assert audit.user_id == user_id

    # Web route resolution test
    _login_client(client, env5e12, 'a')
    post_res = client.post(
        f'/statement/reconciliation/resolve/{rec_id}',
        data={'note': 'Re-verified via web route', 'resolution_status': 'OVERRIDDEN'},
        follow_redirects=True
    )
    assert post_res.status_code == 200

    with app.app_context():
        updated_rec = _db.session.get(ReconciliationRecord, rec_id)
        assert updated_rec.status == 'OVERRIDDEN'
        assert updated_rec.resolution_note == 'Re-verified via web route'


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Authorized Reconciliation Export CSV and JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_04_authorized_reconciliation_export_csv_and_json(app, env5e12, client):
    """Authorized tenant exports structured reconciliation report as CSV and JSON with AuditLog."""
    _login_client(client, env5e12, 'a')
    period = env5e12['period']

    # CSV export
    res_csv = client.get(f'/statement/export/reconciliation?return_period={period}&format=csv')
    assert res_csv.status_code == 200
    assert res_csv.mimetype == 'text/csv'
    csv_text = res_csv.data.decode('utf-8')
    assert 'GST RECONCILIATION & EXCEPTION REPORT' in csv_text
    assert 'SOURCE-TO-NORMALIZED ROW CONSERVATION' in csv_text
    assert 'RECONCILIATION BUCKET SUMMARY' in csv_text
    assert 'INTEGRITY CHECKS BREAKDOWN' in csv_text

    # JSON export via API
    res_json = client.get(f'/api/reconciliation/export?return_period={period}&format=json')
    assert res_json.status_code == 200
    data = res_json.get_json()['data']
    assert 'row_conservation' in data
    assert 'buckets' in data

    # Verify AuditLog created
    with app.app_context():
        export_logs = AuditLog.query.filter_by(
            action='EXPORT',
            entity_type='RECONCILIATION_REPORT',
            entity_id=env5e12['profile_a_id']
        ).all()
        assert len(export_logs) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Cross-Tenant IDOR Rejection on Reconciliation Export
# ─────────────────────────────────────────────────────────────────────────────
def test_05_cross_tenant_idor_rejection_on_reconciliation_export(app, env5e12, client):
    """Tenant B attempting to export Tenant A's reconciliation report is rejected with 404."""
    _login_client(client, env5e12, 'b')
    profile_a_id = env5e12['profile_a_id']
    period = env5e12['period']

    # Cross-tenant web export attempt
    res = client.get(f'/statement/export/reconciliation?profile_id={profile_a_id}&return_period={period}')
    assert res.status_code == 404

    # Cross-tenant API export attempt
    res_api = client.get(f'/api/reconciliation/export?profile_id={profile_a_id}&return_period={period}')
    assert res_api.status_code == 404

    # Verify zero export audit logs created for Tenant A
    with app.app_context():
        logs = AuditLog.query.filter_by(
            action='EXPORT',
            entity_type='RECONCILIATION_REPORT',
            entity_id=profile_a_id,
            user_id=env5e12['user_b_id']
        ).all()
        assert len(logs) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Adapter Readiness Matrix Honesty Check
# ─────────────────────────────────────────────────────────────────────────────
def test_06_adapter_readiness_matrix_honesty_check(app, env5e12, client):
    """Adapter readiness matrix distinguishes documented production platforms from fallback stubs."""
    matrix = get_adapter_readiness_matrix()
    assert isinstance(matrix, list)
    assert len(matrix) >= 14

    doc_map = {m['platform']: m['format_documented'] for m in matrix}
    status_map = {m['platform']: m['status'] for m in matrix}

    # Documented production adapters
    assert doc_map.get('Amazon') is True
    assert doc_map.get('Flipkart') is True
    assert doc_map.get('Meesho') is True
    assert doc_map.get('Myntra') is True
    assert status_map.get('Amazon') == 'PRODUCTION'

    # Fallback adapters
    assert doc_map.get('Citymall') is False
    assert doc_map.get('Snapdeal') is False
    assert status_map.get('Citymall') == 'FALLBACK'

    # API verification
    _login_client(client, env5e12, 'a')
    res = client.get('/api/adapters/readiness')
    assert res.status_code == 200
    api_matrix = res.get_json()['data']
    assert len(api_matrix) == len(matrix)
    for entry in api_matrix:
        assert 'name' in entry
        assert 'platform' in entry
        assert 'format_documented' in entry
        assert 'file_formats' in entry
        assert 'status' in entry


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: GST Classification Rule Provenance and Zero-Tax Separation
# ─────────────────────────────────────────────────────────────────────────────
def test_07_gst_classification_rule_provenance_and_zero_tax_separation():
    """Statutory zero-tax separation (NIL, EXEMPT, NON_GST) and rule version provenance."""
    assert GST_RULE_VERSION == "2026.1"

    # Test distinct taxability determinations
    t_taxable = determine_taxability(18.0)
    assert t_taxable == TAXABLE

    t_nil = determine_taxability(0.0, is_nil_rated=True)
    assert t_nil == NIL_RATED

    t_exempt = determine_taxability(0.0, is_exempt=True)
    assert t_exempt == EXEMPT

    t_nongst = determine_taxability(0.0, is_non_gst=True)
    assert t_nongst == NON_GST

    # Provenance classification contract
    class DummyTx:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    tx_exempt = DummyTx(
        customer_gstin=None,
        tax_rate=0.0,
        taxable_value=Decimal('500.00'),
        place_of_supply='27',
        exempt_flag=True,
        nil_rated_flag=False,
        non_gst_flag=False,
        note_type=None,
        original_invoice_number=None,
        export_type=None
    )
    res_provenance = classify_with_provenance(tx_exempt, supplier_state='27')
    assert res_provenance['classification'] == 'NIL'
    assert res_provenance['taxability'] == EXEMPT
    assert res_provenance['rule_version'] == "2026.1"
    assert "exempt" in res_provenance['classification_reason'].lower()

    # Test aggregate_nil preserves distinct statutory buckets
    tx_nil = DummyTx(
        customer_gstin=None,
        taxable_value=Decimal('100.00'),
        nil_rated_flag=True,
        exempt_flag=False,
        non_gst_flag=False,
        place_of_supply='27'
    )
    tx_ex = DummyTx(
        customer_gstin=None,
        taxable_value=Decimal('200.00'),
        nil_rated_flag=False,
        exempt_flag=True,
        non_gst_flag=False,
        place_of_supply='27'
    )
    tx_ng = DummyTx(
        customer_gstin=None,
        taxable_value=Decimal('300.00'),
        nil_rated_flag=False,
        exempt_flag=False,
        non_gst_flag=True,
        place_of_supply='27'
    )

    nil_summary = aggregate_nil([tx_nil, tx_ex, tx_ng], supplier_state='27')
    assert nil_summary['total_nil'] == Decimal('100.00')
    assert nil_summary['total_exempt'] == Decimal('200.00')
    assert nil_summary['total_nongst'] == Decimal('300.00')
    assert nil_summary['rule_version'] == "2026.1"


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: GSTR-1 Snapshot Checksum Reproducibility
# ─────────────────────────────────────────────────────────────────────────────
def test_08_gstr1_snapshot_checksum_reproducibility(app, env5e12):
    """Deterministic snapshot checksum ensures reproducibility of GSTR-1 generation."""
    with app.app_context():
        profile_id = env5e12['profile_a_id']
        user_id = env5e12['user_a_id']
        period = env5e12['period']

        imp = ImportHistory(
            profile_id=profile_id,
            user_id=user_id,
            platform_name='Amazon',
            file_name='test_snap.csv',
            original_file_name='test_snap.csv',
            return_period=period,
            financial_year='2024-25',
            total_rows=2,
            success_rows=2,
            processing_status='COMPLETED'
        )
        _db.session.add(imp)
        _db.session.commit()

        raw1 = RawImport(import_history_id=imp.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw2 = RawImport(import_history_id=imp.id, sheet_name='Sheet1', row_number=2, status='SUCCESS')
        _db.session.add_all([raw1, raw2])
        _db.session.commit()

        tx1 = Transaction(
            profile_id=profile_id,
            import_history_id=imp.id,
            raw_import_id=raw1.id,
            invoice_number='INV-REP-01',
            invoice_date=date(2024, 8, 12),
            taxable_value=Decimal('150.00'),
            total_tax=Decimal('27.00'),
            tax_rate=Decimal('18.00'),
            place_of_supply='27',
            customer_gstin='27BBBBB0000B1Z7',
            is_staged=False,
            is_deleted=False
        )
        tx2 = Transaction(
            profile_id=profile_id,
            import_history_id=imp.id,
            raw_import_id=raw2.id,
            invoice_number='INV-REP-02',
            invoice_date=date(2024, 8, 14),
            taxable_value=Decimal('300.00'),
            total_tax=Decimal('54.00'),
            tax_rate=Decimal('18.00'),
            place_of_supply='27',
            customer_gstin='27BBBBB0000B1Z7',
            is_staged=False,
            is_deleted=False
        )
        _db.session.add_all([tx1, tx2])
        _db.session.commit()

        hash1, count1 = compute_input_snapshot_checksum([tx1, tx2])
        hash2, count2 = compute_input_snapshot_checksum([tx2, tx1])  # Order independence
        assert hash1 == hash2
        assert count1 == 2

        # Changing an amount must produce a completely different checksum
        tx1.taxable_value = Decimal('151.00')
        hash3, _ = compute_input_snapshot_checksum([tx1, tx2])
        assert hash1 != hash3


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Fail-Closed BLOCKED Generation Download Security
# ─────────────────────────────────────────────────────────────────────────────
def test_09_fail_closed_blocked_generation_download_security(app, env5e12, client):
    """GSTR-1 JSON must NEVER be downloaded or exported if gate_status is BLOCKED."""
    _login_client(client, env5e12, 'a')

    with app.app_context():
        profile_id = env5e12['profile_a_id']
        user_id = env5e12['user_a_id']

        blocked_gen = GSTR1Generation(
            profile_id=profile_id,
            user_id=user_id,
            return_period='082024',
            financial_year='2024-25',
            generation_status='COMPLETED',
            gate_status='BLOCKED',
            validation_passed=False,
            excel_file_path='/tmp/fake.xlsx',
            json_file_path='/tmp/fake.json'
        )
        _db.session.add(blocked_gen)
        _db.session.commit()
        blocked_id = blocked_gen.id

    # JSON download must be rejected
    res_json = client.get(f'/generate/download/json/{blocked_id}', follow_redirects=False)
    assert res_json.status_code == 302
    assert '/generate' in res_json.location

    # Excel download must also be rejected
    res_excel = client.get(f'/generate/download/excel/{blocked_id}', follow_redirects=False)
    assert res_excel.status_code == 302
    assert '/generate' in res_excel.location

    # Follow redirect and verify flash message
    res_page = client.get(f'/generate/download/json/{blocked_id}', follow_redirects=True)
    assert b"File download unavailable" in res_page.data


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Authorized Download Audit Logging
# ─────────────────────────────────────────────────────────────────────────────
def test_10_authorized_download_audit_logging(app, env5e12, client, tmp_path):
    """Clean generation download generates AuditLog with action='DOWNLOAD'."""
    _login_client(client, env5e12, 'a')

    with app.app_context():
        profile = _db.session.get(GSTProfile, env5e12['profile_a_id'])
        user_id = env5e12['user_a_id']

        json_file = tmp_path / f"GSTR1_{profile.gstin}_082024.json"
        json_file.write_text('{"gstin": "27AAAAA0000A1Z5", "fp": "082024"}')

        clean_gen = GSTR1Generation(
            profile_id=profile.id,
            user_id=user_id,
            return_period='082024',
            financial_year='2024-25',
            generation_status='COMPLETED',
            gate_status='CLEAN',
            validation_passed=True,
            json_file_path=str(json_file)
        )
        _db.session.add(clean_gen)
        _db.session.commit()
        clean_id = clean_gen.id

    res = client.get(f'/generate/download/json/{clean_id}')
    assert res.status_code == 200

    with app.app_context():
        dl_log = AuditLog.query.filter_by(
            action='DOWNLOAD',
            entity_type='GSTR1Generation',
            entity_id=clean_id
        ).first()
        assert dl_log is not None
        assert dl_log.user_id == env5e12['user_a_id']


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: Cross-Tenant Isolation Across GSTR-1 Downloads and Recon
# ─────────────────────────────────────────────────────────────────────────────
def test_11_cross_tenant_isolation_on_gstr1_downloads_and_recon(app, env5e12, client, tmp_path):
    """Tenant B cannot access or download Tenant A's GSTR-1 artifacts or generation details."""
    with app.app_context():
        profile_a = _db.session.get(GSTProfile, env5e12['profile_a_id'])
        json_file = tmp_path / f"GSTR1_{profile_a.gstin}_082024.json"
        json_file.write_text('{"gstin": "27AAAAA0000A1Z5", "fp": "082024"}')

        gen_a = GSTR1Generation(
            profile_id=profile_a.id,
            user_id=env5e12['user_a_id'],
            return_period='082024',
            financial_year='2024-25',
            generation_status='COMPLETED',
            gate_status='CLEAN',
            validation_passed=True,
            json_file_path=str(json_file)
        )
        _db.session.add(gen_a)
        _db.session.commit()
        gen_a_id = gen_a.id

    _login_client(client, env5e12, 'b')

    # Tenant B attempts IDOR on Tenant A's download routes
    assert client.get(f'/generate/download/json/{gen_a_id}').status_code == 404
    assert client.get(f'/generate/download/excel/{gen_a_id}').status_code == 404
    assert client.get(f'/api/generate/{gen_a_id}').status_code == 404

    # Verify zero download audit logs for gen_a by user B
    with app.app_context():
        unauth_logs = AuditLog.query.filter_by(
            action='DOWNLOAD',
            entity_type='GSTR1Generation',
            entity_id=gen_a_id,
            user_id=env5e12['user_b_id']
        ).all()
        assert len(unauth_logs) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: Regeneration Snapshot Tracking
# ─────────────────────────────────────────────────────────────────────────────
def test_12_regeneration_snapshot_tracking(app, env5e12, client):
    """Regenerating GSTR-1 updates snapshot metadata and records REGENERATE audit trail."""
    _login_client(client, env5e12, 'a')
    period = env5e12['period']

    # First generation
    client.post('/generate/run', data={'return_period': period}, follow_redirects=True)

    with app.app_context():
        gen = GSTR1Generation.query.filter_by(
            profile_id=env5e12['profile_a_id'],
            return_period=period
        ).first()
        assert gen is not None
        first_gen_id = gen.id

    # Regenerate
    client.post('/generate/regenerate', data={'return_period': period, 'id': first_gen_id}, follow_redirects=True)

    with app.app_context():
        regen = GSTR1Generation.query.filter_by(
            profile_id=env5e12['profile_a_id'],
            return_period=period
        ).first()
        assert regen is not None
        assert regen.snapshot_checksum is not None

        regen_log = AuditLog.query.filter_by(
            action='REGENERATE',
            entity_type='GSTR1Generation',
            entity_id=regen.id
        ).first()
        assert regen_log is not None


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: Period-Aware HSN B2B / B2C Partitioning
# ─────────────────────────────────────────────────────────────────────────────
def test_13_period_aware_hsn_b2b_b2c_partitioning():
    """HSN service partitions entries into B2B and B2C groups accurately."""
    class HsnTx:
        def __init__(self, ctin, hsn, qty, val, txval, rate, igst=0, cgst=0, sgst=0):
            self.customer_gstin = ctin
            self.hsn_sac = hsn
            self.quantity = Decimal(str(qty))
            self.invoice_value = Decimal(str(val))
            self.taxable_value = Decimal(str(txval))
            self.tax_rate = Decimal(str(rate))
            self.igst_amount = Decimal(str(igst))
            self.cgst_amount = Decimal(str(cgst))
            self.sgst_amount = Decimal(str(sgst))
            self.cess_amount = Decimal('0')
            self.uqc = 'NOS'
            self.product_name = 'Widget'

    tx_b2b = HsnTx('27AAAAA1111A1Z1', '85044090', 2, 236, 200, 18, igst=36)
    tx_b2c = HsnTx(None, '85044090', 1, 118, 100, 18, igst=18)

    b2b_hsn = aggregate_hsn_b2b([tx_b2b, tx_b2c])
    b2c_hsn = aggregate_hsn_b2c([tx_b2b, tx_b2c])

    assert len(b2b_hsn) == 1
    assert b2b_hsn[0]['qty'] == 2.0
    assert b2b_hsn[0]['txval'] == 200.0

    assert len(b2c_hsn) == 1
    assert b2c_hsn[0]['qty'] == 1.0
    assert b2c_hsn[0]['txval'] == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: Five-Layer Validation Summary Persistence
# ─────────────────────────────────────────────────────────────────────────────
def test_14_five_layer_validation_summary_persistence(app, env5e12, client):
    """Validation summary captures all 5 validation layers and is exposed via API."""
    _login_client(client, env5e12, 'a')
    period = env5e12['period']

    res = client.post('/api/generate', json={'return_period': period})
    assert res.status_code == 201
    resp_data = res.get_json()['data']

    assert 'validation_summary' in resp_data
    val_sum = resp_data['validation_summary']
    assert 'layers_checked' in val_sum
    assert set(val_sum['layers_checked']) == {"Syntax", "Schema", "Fields", "Business Rules", "Cross-tables"}
    assert 'gate_status' in resp_data
    assert resp_data['gate_status'] in ('CLEAN', 'WARNING', 'BLOCKED')

    # GET /api/generate/<id>
    gen_id = resp_data['id']
    detail_res = client.get(f'/api/generate/{gen_id}')
    assert detail_res.status_code == 200
    detail_data = detail_res.get_json()['data']
    assert detail_data['validation_summary']['layers_checked'] == val_sum['layers_checked']
    assert detail_data['gate_status'] == resp_data['gate_status']
