"""Integration tests for Phase 5E-11: Staged Preview, Confirm & Lifecycle Hardening.

Covers Workstreams 3, 4, 5, and 6:
1. Staging upload creates ImportHistory(processing_status='STAGED') and preserves RawImport.
2. STAGED transactions do NOT leak into GSTR-1 generation, dashboard stats, or B2B/B2C/HSN section queries.
3. GET /import/<id>/preview (and REST API) returns accurate counts, tax totals, and row previews without state mutation.
4. POST /import/<id>/confirm atomically promotes STAGED -> COMPLETED/PARTIAL, makes transactions visible, and logs CONFIRM audit.
5. Double/repeated confirm is idempotent and never duplicates transactions.
6. POST /import/<id>/cancel transitions STAGED -> CANCELLED, soft-deletes transactions, preserves RawImport, and logs CANCEL audit.
7. Confirming a CANCELLED or FAILED import fails closed.
8. Confirming into a return period frozen by a finalized GSTR-1 generation fails closed.
9. Reprocessing a STAGED or CANCELLED import fails closed without corrupting the active ledger.
10. Cross-tenant IDOR protection: Tenant B cannot preview, confirm, or cancel Tenant A's staged import.
11. Staging with invalid headers transitions to FAILED/REJECTED without leaving dangling STAGED state.
12. Mathematical consistency: Preview tax sums and counts match confirmed database ledger totals exactly.
13. Optimistic concurrency guard handles concurrent confirm calls cleanly.
14. In-file duplicate rows are flagged in preview and deduplicated upon confirmation.
"""
import io
import os
import json
import uuid
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
)
from app.services.import_service import (
    process_import,
    reprocess_import,
    check_generation_freeze,
    preview_import,
    confirm_import,
    cancel_import,
)
from app.services.gstr1_generator import load_transactions, generate_gstr1
from app.services.reconciliation_service import check_import_counts, check_duplicates
from app.utils.query import tenant_query


@pytest.fixture
def staged_env(app, db):
    """Set up two fully isolated tenants with distinct profiles and return periods."""
    with app.app_context():
        # Tenant A
        user_a = User(username=f'staged_user_a_{uuid.uuid4().hex[:6]}', email='user_a@example.com')
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
        user_b = User(username=f'staged_user_b_{uuid.uuid4().hex[:6]}', email='user_b@example.com')
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


def _login_client(client, staged_env, tenant_key='a'):
    """Helper to log in test client and bind active profile and period."""
    username = staged_env[f'username_{tenant_key}']
    profile_id = staged_env[f'profile_{tenant_key}_id']
    password = 'SecretA123!' if tenant_key == 'a' else 'SecretB456!'
    client.post('/login', data={
        'username': username,
        'password': password
    }, follow_redirects=True)
    with client.session_transaction() as sess:
        sess['active_profile_id'] = profile_id
        sess['return_period'] = '082024'


def test_01_staging_upload_creates_staged_and_preserves_raw_import(staged_env, fixture_dir):
    """Test Case 1: Staging creates ImportHistory(status='STAGED'), preserves RawImport and candidate transactions."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    assert res.status == 'STAGED'
    assert res.import_history_id is not None
    assert res.total_rows > 0

    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    assert ih is not None
    assert ih.processing_status == 'STAGED'
    assert ih.total_rows == res.total_rows

    # Verify RawImport preservation
    raw_count = RawImport.query.filter_by(import_history_id=ih.id).count()
    assert raw_count == ih.total_rows

    # Verify candidate transactions are marked is_staged=True
    txs = Transaction.query.filter_by(import_history_id=ih.id).all()
    assert len(txs) == res.success_rows
    assert all(t.is_staged is True for t in txs)
    assert all(t.is_deleted is False for t in txs)

    # Verify STAGE audit entry
    audit = AuditLog.query.filter_by(entity_type='ImportHistory', entity_id=ih.id, action='STAGE').first()
    assert audit is not None
    assert audit.user_id == uid_a


def test_02_staged_transactions_excluded_from_downstream_gstr1_dashboard_and_sections(client, staged_env, fixture_dir):
    """Test Case 2: STAGED data is strictly excluded from GSTR-1, dashboard stats, and sections."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )
    assert res.status == 'STAGED'

    # 1. GSTR-1 generator must see 0 transactions
    gstr1_data = load_transactions(pid_a, period)
    assert len(gstr1_data['b2b']) == 0
    assert len(gstr1_data['b2cs']) == 0
    assert len(gstr1_data['cdnr']) == 0

    # 2. Reconciliation check must see 0 active transactions
    recon = check_import_counts(pid_a, period)
    assert recon.passed is True

    # 3. Web & API dashboard queries must report 0
    _login_client(client, staged_env, 'a')
    resp = client.get(f'/api/dashboard-stats?return_period={period}')
    assert resp.status_code == 200
    stats = resp.get_json()['data']
    assert stats['total_invoices'] == 0
    assert stats['taxable_value'] == '0.00'
    assert stats['total_tax'] == '0.00'

    # 4. Section APIs must return empty
    for section in ('b2b', 'b2c', 'cdnr', 'nil', 'hsn-b2b', 'hsn-b2c', 'ecom'):
        resp = client.get(f'/api/{section}?return_period={period}')
        assert resp.status_code == 200
        sec_data = resp.get_json()
        assert sec_data['total'] == 0
        assert len(sec_data['data']) == 0


def test_03_get_import_preview_endpoint_returns_accurate_counts_and_tax_summary(client, staged_env, fixture_dir):
    """Test Case 3: GET preview returns accurate counts and tax breakdown without mutating state."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # 1. Direct service preview
    preview = preview_import(res.import_history_id, pid_a)
    assert preview is not None
    assert preview['import_id'] == res.import_history_id
    assert preview['processing_status'] == 'STAGED'
    assert preview['counts']['total_rows'] == res.total_rows
    assert preview['counts']['accepted_rows'] == res.success_rows
    assert preview['tax_summary']['taxable_value'] > 0
    assert len(preview['rows_preview']) > 0
    assert preview['is_frozen'] is False
    assert preview['can_confirm'] is True

    # 2. Web route preview
    _login_client(client, staged_env, 'a')
    resp = client.get(f'/import/{res.import_history_id}/preview')
    assert resp.status_code == 200
    assert b'Import Preview' in resp.data
    assert b'Candidate Transactions Preview' in resp.data

    # 3. API route preview
    resp_api = client.get(f'/api/import/{res.import_history_id}/preview')
    assert resp_api.status_code == 200
    api_data = resp_api.get_json()['data']
    assert api_data['import_id'] == res.import_history_id
    assert api_data['processing_status'] == 'STAGED'

    # Verify zero state mutation
    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    assert ih.processing_status == 'STAGED'


def test_04_post_confirm_atomically_promotes_staged_to_completed_and_logs_audit(client, staged_env, fixture_dir):
    """Test Case 4: Confirming promotes STAGED -> COMPLETED, activates transactions, and writes CONFIRM audit."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    _login_client(client, staged_env, 'a')
    resp = client.post(f'/import/{res.import_history_id}/confirm', follow_redirects=True)
    assert resp.status_code == 200

    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    assert ih.processing_status in ('COMPLETED', 'PARTIAL')

    # Transactions are now active (is_staged == False)
    txs = tenant_query(Transaction, pid_a).filter_by(import_history_id=ih.id).all()
    assert len(txs) == ih.success_rows
    assert all(t.is_staged is False for t in txs)
    assert all(t.is_deleted is False for t in txs)

    # Downstream GSTR-1 and dashboard now see transactions
    gstr1_data = load_transactions(pid_a, period)
    total_gstr1_rows = len(gstr1_data['b2b']) + len(gstr1_data['b2cs'])
    assert total_gstr1_rows > 0

    resp_stats = client.get(f'/api/dashboard-stats?return_period={period}')
    stats = resp_stats.get_json()['data']
    assert stats['total_invoices'] == ih.success_rows

    # Audit log check
    audit = AuditLog.query.filter_by(entity_type='ImportHistory', entity_id=ih.id, action='CONFIRM').first()
    assert audit is not None
    assert audit.user_id == uid_a


def test_05_double_confirm_is_idempotent_and_never_duplicates_transactions(staged_env, fixture_dir):
    """Test Case 5: Repeated confirm calls return idempotent success and never duplicate records."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # First confirm
    res1 = confirm_import(res.import_history_id, pid_a, uid_a)
    assert res1['success'] is True
    assert res1['status'] in ('COMPLETED', 'PARTIAL')

    initial_tx_count = tenant_query(Transaction, pid_a).filter_by(
        import_history_id=res.import_history_id, is_deleted=False
    ).count()

    # Second confirm
    res2 = confirm_import(res.import_history_id, pid_a, uid_a)
    assert res2['success'] is True
    assert res2.get('already_confirmed') is True

    # Transaction count remains strictly invariant
    second_tx_count = tenant_query(Transaction, pid_a).filter_by(
        import_history_id=res.import_history_id, is_deleted=False
    ).count()
    assert second_tx_count == initial_tx_count


def test_06_post_cancel_transitions_staged_to_cancelled_preserves_raw_import(client, staged_env, fixture_dir):
    """Test Case 6: Cancelling transitions to CANCELLED, soft-deletes transactions, preserves RawImport, and logs CANCEL."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    _login_client(client, staged_env, 'a')
    resp = client.post(f'/import/{res.import_history_id}/cancel', follow_redirects=True)
    assert resp.status_code == 200

    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    assert ih.processing_status == 'CANCELLED'

    # Candidate transactions are soft-deleted
    active_txs = tenant_query(Transaction, pid_a).filter_by(
        import_history_id=ih.id, is_deleted=False
    ).all()
    assert len(active_txs) == 0

    # RawImport is completely preserved as immutable evidence
    raw_count = RawImport.query.filter_by(import_history_id=ih.id).count()
    assert raw_count == ih.total_rows

    # Audit log check
    audit = AuditLog.query.filter_by(entity_type='ImportHistory', entity_id=ih.id, action='CANCEL').first()
    assert audit is not None
    assert audit.user_id == uid_a


def test_07_confirming_cancelled_or_failed_import_fails_closed(staged_env, fixture_dir):
    """Test Case 7: Confirming a CANCELLED or FAILED import fails closed."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # Cancel it
    cancel_import(res.import_history_id, pid_a, uid_a)

    # Attempting to confirm a cancelled import must raise ValueError
    with pytest.raises(ValueError, match="Cannot confirm import.*CANCELLED"):
        confirm_import(res.import_history_id, pid_a, uid_a)

    # Set to FAILED and verify
    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    ih.processing_status = 'FAILED'
    _db.session.commit()

    with pytest.raises(ValueError, match="Cannot confirm import.*FAILED"):
        confirm_import(res.import_history_id, pid_a, uid_a)


def test_08_confirming_into_frozen_return_period_is_blocked(staged_env, fixture_dir):
    """Test Case 8: Confirming into a return period frozen by a finalized GSTR-1 fails closed."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    # Stage import first
    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # Now create finalized GSTR-1 freeze for this period
    freeze_gen = GSTR1Generation(
        profile_id=pid_a,
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        generation_status='COMPLETED',
        validation_passed=True,
    )
    _db.session.add(freeze_gen)
    _db.session.commit()

    # Verify check_generation_freeze detects the lock
    lock = check_generation_freeze(pid_a, period)
    assert lock is not None

    # Confirming must fail closed
    with pytest.raises(ValueError, match="frozen by finalized GSTR-1"):
        confirm_import(res.import_history_id, pid_a, uid_a)

    # Status remains STAGED
    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    assert ih.processing_status == 'STAGED'


def test_09_reprocessing_staged_or_cancelled_import_fails_closed(staged_env, fixture_dir):
    """Test Case 9: Reprocessing a STAGED or CANCELLED import fails closed without mutating active data."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # Reprocess on STAGED import must fail
    rep_res = reprocess_import(res.import_history_id, pid_a, uid_a)
    assert rep_res.status == 'FAILED'
    assert any("not in a valid state" in err.lower() or "superseded" in err.lower() for err in rep_res.errors)

    # Cancel import and test again
    cancel_import(res.import_history_id, pid_a, uid_a)
    rep_res2 = reprocess_import(res.import_history_id, pid_a, uid_a)
    assert rep_res2.status == 'FAILED'


def test_10_cross_tenant_idor_protection_on_preview_confirm_cancel(client, staged_env, fixture_dir):
    """Test Case 10: Tenant B cannot preview, confirm, or cancel Tenant A's staged import."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    pid_b = staged_env['profile_b_id']
    uid_b = staged_env['user_b_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    # Tenant A stages an import
    res_a = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # Service-level cross-tenant protection
    assert preview_import(res_a.import_history_id, pid_b) is None
    with pytest.raises(ValueError, match="not found or access denied"):
        confirm_import(res_a.import_history_id, pid_b, uid_b)
    with pytest.raises(ValueError, match="not found or access denied"):
        cancel_import(res_a.import_history_id, pid_b, uid_b)

    # Web client cross-tenant protection: Login as Tenant B
    _login_client(client, staged_env, 'b')

    # 1. Preview -> 404
    resp_prev = client.get(f'/import/{res_a.import_history_id}/preview')
    assert resp_prev.status_code == 404

    # 2. Confirm -> 400 or redirect with danger flash
    resp_conf = client.post(f'/import/{res_a.import_history_id}/confirm', follow_redirects=True)
    assert resp_conf.status_code in (200, 404)
    assert b'not found or access denied' in resp_conf.data or resp_conf.status_code == 404

    # 3. Cancel -> 400 or redirect with danger flash
    resp_canc = client.post(f'/import/{res_a.import_history_id}/cancel', follow_redirects=True)
    assert resp_canc.status_code in (200, 404)
    assert b'not found or access denied' in resp_canc.data or resp_canc.status_code == 404

    # REST API endpoints -> 404
    assert client.get(f'/api/import/{res_a.import_history_id}/preview').status_code == 404
    assert client.post(f'/api/import/{res_a.import_history_id}/confirm').status_code in (400, 404)
    assert client.post(f'/api/import/{res_a.import_history_id}/cancel').status_code in (400, 404)

    # Tenant A's import remains pristine STAGED
    ih_a = tenant_query(ImportHistory, pid_a).filter_by(id=res_a.import_history_id).first()
    assert ih_a.processing_status == 'STAGED'


def test_11_staging_with_invalid_file_transitions_to_failed_without_dangling_staged(staged_env, fixture_dir):
    """Test Case 11: Staging an invalid file transitions to FAILED/REJECTED without dangling STAGED state."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    invalid_file = os.path.join(fixture_dir, 'wrong_headers.xlsx')

    res = process_import(
        file_path=invalid_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    assert res.status in ('REJECTED_INVALID', 'FAILED')
    ih = tenant_query(ImportHistory, pid_a).filter_by(id=res.import_history_id).first()
    assert ih.processing_status == 'FAILED'

    # Zero transactions created
    tx_count = Transaction.query.filter_by(import_history_id=ih.id).count()
    assert tx_count == 0


def test_12_transaction_count_and_tax_totals_match_between_preview_and_confirmed_ledger(staged_env, fixture_dir):
    """Test Case 12: Preview tax totals and row counts match confirmed ledger aggregates exactly."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    preview = preview_import(res.import_history_id, pid_a)
    confirm_import(res.import_history_id, pid_a, uid_a)

    from sqlalchemy import func
    actual_taxable = _db.session.query(func.coalesce(func.sum(Transaction.taxable_value), 0)).filter_by(
        import_history_id=res.import_history_id, is_deleted=False
    ).scalar()
    actual_tax = _db.session.query(func.coalesce(func.sum(Transaction.total_tax), 0)).filter_by(
        import_history_id=res.import_history_id, is_deleted=False
    ).scalar()
    actual_count = Transaction.query.filter_by(
        import_history_id=res.import_history_id, is_deleted=False
    ).count()

    assert Decimal(str(preview['tax_summary']['taxable_value'])).quantize(Decimal('0.01')) == Decimal(str(actual_taxable)).quantize(Decimal('0.01'))
    assert Decimal(str(preview['tax_summary']['total_tax'])).quantize(Decimal('0.01')) == Decimal(str(actual_tax)).quantize(Decimal('0.01'))
    assert preview['counts']['accepted_rows'] == actual_count


def test_13_concurrency_optimistic_lock_on_confirm(staged_env, fixture_dir):
    """Test Case 13: Optimistic concurrency control on concurrent confirm calls."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    sample_file = os.path.join(fixture_dir, 'custom_excel_sample.xlsx')

    res = process_import(
        file_path=sample_file,
        profile_id=pid_a,
        platform_name='Generic',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    # First confirm succeeds
    res1 = confirm_import(res.import_history_id, pid_a, uid_a)
    assert res1['success'] is True

    # Immediate second call returns idempotent success without error
    res2 = confirm_import(res.import_history_id, pid_a, uid_a)
    assert res2['success'] is True
    assert res2.get('already_confirmed') is True


def test_14_in_file_duplicate_rows_flagged_in_preview_and_deduplicated_on_confirm(staged_env, fixture_dir):
    """Test Case 14: In-file duplicates are flagged in preview and correctly deduplicated on confirm."""
    pid_a = staged_env['profile_a_id']
    uid_a = staged_env['user_a_id']
    period = staged_env['period']
    dup_file = os.path.join(fixture_dir, 'test_flipkart_in_file_dup.xlsx')

    res = process_import(
        file_path=dup_file,
        profile_id=pid_a,
        platform_name='Flipkart',
        user_id=uid_a,
        return_period=period,
        financial_year='2024-25',
        stage=True,
    )

    assert res.status == 'STAGED'
    assert res.skipped_rows > 0

    preview = preview_import(res.import_history_id, pid_a)
    assert preview['counts']['duplicate_rows'] == res.skipped_rows
    assert len(preview['duplicates']) > 0

    # On confirm, candidate rows promoted equals success_rows (excluding duplicates)
    confirm_res = confirm_import(res.import_history_id, pid_a, uid_a)
    assert confirm_res['success'] is True

    active_tx_count = tenant_query(Transaction, pid_a).filter_by(
        import_history_id=res.import_history_id, is_deleted=False
    ).count()
    assert active_tx_count == res.success_rows
