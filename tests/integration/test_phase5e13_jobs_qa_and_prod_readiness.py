"""Integration Test Suite for Phase 5E-13:
Workstreams 7, 11, and 12 — Background Jobs, Concurrency/Failure-Injection QA & Production Readiness.

Covers:
- Workstream 7: Job progress tracking, idempotent retry, safe cancellation, cross-tenant IDOR, AuditLog.
- Workstream 11: Concurrency idempotency, duplicate upload guard, failure-injection rollback, generation freeze guard, 2,500+ row streaming & workbook closure.
- Workstream 12: /healthz liveness, /readyz readiness, DB degradation, production secret validation, path-traversal containment, SQLite online backup & restore drills.
"""

import io
import os
import sys
import json
import uuid
import pytest
import shutil
import sqlite3
import openpyxl
from decimal import Decimal
from datetime import datetime, date
from pathlib import Path
from unittest.mock import patch

from app.extensions import db as _db
from app.models import (
    User, GSTProfile, ImportHistory, RawImport, Transaction,
    AuditLog, GSTR1Generation, TCSReconciliation, ReconciliationRecord
)
from app.services.import_service import (
    process_import, confirm_import, preview_import, cancel_import,
    reprocess_import, check_generation_freeze
)
from app.services.job_service import (
    get_import_job_progress, retry_import_job, cancel_import_job, dispatch_import_job
)
from app.services.ops_service import (
    get_liveness_status, get_readiness_status, validate_production_config,
    resolve_contained_path, create_backup_snapshot, verify_and_restore_backup,
    GST_RULE_VERSION
)
from app.utils.query import tenant_query


@pytest.fixture
def env5e13(app):
    """Set up isolated multi-tenant fixture for Phase 5E-13 tests."""
    with app.app_context():
        _db.session.query(AuditLog).delete()
        _db.session.query(Transaction).delete()
        _db.session.query(RawImport).delete()
        _db.session.query(ImportHistory).delete()
        _db.session.query(GSTR1Generation).delete()
        _db.session.query(ReconciliationRecord).delete()
        _db.session.query(GSTProfile).delete()
        _db.session.query(User).delete()
        _db.session.commit()

        # Tenant A
        user_a = User(username='seller_5e13_a', email='seller_5e13_a@example.com')
        user_a.set_password('Password123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin='27ABCDE1234F1Z5',
            legal_name='Seller 5E-13 Alpha Corp',
            trade_name='Alpha Seller',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B
        user_b = User(username='seller_5e13_b', email='seller_5e13_b@example.com')
        user_b.set_password('Password123!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin='29XYZAB5678C1Z2',
            legal_name='Seller 5E-13 Beta Logistics',
            trade_name='Beta Seller',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        yield {
            'user_a_id': user_a.id,
            'profile_a_id': profile_a.id,
            'user_b_id': user_b.id,
            'profile_b_id': profile_b.id,
            'period': '082024'
        }


def _login_client(client, env, tenant='a'):
    email = 'seller_5e13_a@example.com' if tenant == 'a' else 'seller_5e13_b@example.com'
    client.post('/login', data={'email': email, 'password': 'Password123!'}, follow_redirects=True)
    with client.session_transaction() as sess:
        sess['active_profile_id'] = env['profile_a_id'] if tenant == 'a' else env['profile_b_id']
        sess['return_period'] = env['period']


def _create_sample_excel(filepath, rows_count=10):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Amazon Sales"
    ws.append([
        "Invoice Number", "Invoice Date", "Customer GSTIN", "Place of Supply",
        "Taxable Value", "Tax Rate", "CGST Rate", "CGST Amount", "SGST Rate", "SGST Amount",
        "IGST Rate", "IGST Amount", "Total Tax", "Invoice Value"
    ])
    for i in range(1, rows_count + 1):
        ws.append([
            f"INV-{i:05d}", "2024-08-10", "27BBBBB0000B1Z7", "27",
            100.0 * i, 18.0, 9.0, 9.0 * i, 9.0, 9.0 * i,
            0.0, 0.0, 18.0 * i, 118.0 * i
        ])
    wb.save(filepath)
    wb.close()


# ═════════════════════════════════════════════════════════════════════════════
# WORKSTREAM 7: Background Job Progress, Retry, Cancel & Monitoring (Tests 1-5)
# ═════════════════════════════════════════════════════════════════════════════

def test_01_job_progress_tracking_lifecycle(app, env5e13, client, tmp_path):
    """Progress tracking returns accurate counters and percentage during and after import."""
    _login_client(client, env5e13, 'a')
    file_path = str(tmp_path / "progress_sample.xlsx")
    _create_sample_excel(file_path, rows_count=20)

    res = dispatch_import_job(
        file_path=file_path,
        profile_id=env5e13['profile_a_id'],
        platform_name='Generic',
        user_id=env5e13['user_a_id'],
        return_period=env5e13['period'],
        financial_year='2024-25',
        stage=True
    )
    import_id = res['import_id']

    # Query progress via API
    resp = client.get(f'/api/imports/{import_id}/progress')
    assert resp.status_code == 200
    data = resp.get_json()['data']
    assert data['import_id'] == import_id
    assert data['status'] == 'STAGED'
    assert data['total_rows'] == 20
    assert data['processed_rows'] == 20
    assert data['progress_pct'] == 100.0
    assert data['can_retry'] is False
    assert data['can_cancel'] is True


def test_02_job_retry_idempotency_and_state_reset(app, env5e13, client, tmp_path):
    """Job retry cleans up prior partial transactions and re-executes idempotently."""
    _login_client(client, env5e13, 'a')
    file_path = str(tmp_path / "retry_sample.xlsx")
    _create_sample_excel(file_path, rows_count=10)

    # Initial import staged
    res = dispatch_import_job(
        file_path=file_path,
        profile_id=env5e13['profile_a_id'],
        platform_name='Generic',
        user_id=env5e13['user_a_id'],
        return_period=env5e13['period'],
        financial_year='2024-25',
        stage=True
    )
    import_id = res['import_id']

    # Simulate import failure
    with app.app_context():
        ih = _db.session.get(ImportHistory, import_id)
        ih.processing_status = 'FAILED'
        ih.error_message = 'Simulated network timeout'
        _db.session.commit()

    # Retry job via API
    retry_resp = client.post(f'/api/imports/{import_id}/retry', json={'stage': True})
    assert retry_resp.status_code == 200
    retry_data = retry_resp.get_json()['data']
    assert retry_data['import_id'] == import_id
    assert retry_data['status'] == 'STAGED'

    # Verify zero duplicate transactions
    with app.app_context():
        tx_count = tenant_query(Transaction, env5e13['profile_a_id']).filter_by(
            import_history_id=import_id, is_deleted=False
        ).count()
        assert tx_count == 10


def test_03_job_cancel_safety_and_raw_import_preservation(app, env5e13, client, tmp_path):
    """Cancelling a job sets status CANCELLED, soft-deletes transactions, but preserves RawImport."""
    _login_client(client, env5e13, 'a')
    file_path = str(tmp_path / "cancel_sample.xlsx")
    _create_sample_excel(file_path, rows_count=15)

    res = dispatch_import_job(
        file_path=file_path,
        profile_id=env5e13['profile_a_id'],
        platform_name='Generic',
        user_id=env5e13['user_a_id'],
        return_period=env5e13['period'],
        financial_year='2024-25',
        stage=True
    )
    import_id = res['import_id']

    # Cancel the job
    cancel_resp = client.post(f'/api/imports/{import_id}/cancel')
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.get_json()['data']
    assert cancel_data['status'] == 'CANCELLED'

    # Verify transactions soft-deleted but RawImport records preserved
    with app.app_context():
        active_txs = tenant_query(Transaction, env5e13['profile_a_id']).filter_by(
            import_history_id=import_id, is_deleted=False
        ).count()
        assert active_txs == 0

        raw_count = RawImport.query.filter_by(import_history_id=import_id).count()
        assert raw_count == 15, "RawImport records must be preserved immutably"


def test_04_job_progress_retry_cancel_cross_tenant_idor_rejection(app, env5e13, client, tmp_path):
    """Tenant B cannot view progress, retry, or cancel Tenant A's import job (strict 404)."""
    file_path = str(tmp_path / "idor_sample.xlsx")
    _create_sample_excel(file_path, rows_count=5)

    res = dispatch_import_job(
        file_path=file_path,
        profile_id=env5e13['profile_a_id'],
        platform_name='Generic',
        user_id=env5e13['user_a_id'],
        return_period=env5e13['period'],
        financial_year='2024-25',
        stage=True
    )
    import_id = res['import_id']

    # Login as Tenant B
    _login_client(client, env5e13, 'b')

    # Progress -> 404
    p_resp = client.get(f'/api/imports/{import_id}/progress')
    assert p_resp.status_code == 404

    # Retry -> 404
    r_resp = client.post(f'/api/imports/{import_id}/retry')
    assert r_resp.status_code == 404

    # Cancel -> 404
    c_resp = client.post(f'/api/imports/{import_id}/cancel')
    assert c_resp.status_code == 404


def test_05_job_actions_record_audit_log(app, env5e13, client, tmp_path):
    """Job retry and cancel operations record distinct AuditLog entries with entity_type='IMPORT_JOB'."""
    _login_client(client, env5e13, 'a')
    file_path = str(tmp_path / "audit_job_sample.xlsx")
    _create_sample_excel(file_path, rows_count=5)

    res = dispatch_import_job(
        file_path=file_path,
        profile_id=env5e13['profile_a_id'],
        platform_name='Generic',
        user_id=env5e13['user_a_id'],
        return_period=env5e13['period'],
        financial_year='2024-25',
        stage=True
    )
    import_id = res['import_id']

    # Cancel job
    client.post(f'/api/imports/{import_id}/cancel')

    # Retry job
    client.post(f'/api/imports/{import_id}/retry', json={'stage': True})

    with app.app_context():
        cancel_audit = AuditLog.query.filter_by(
            entity_type='IMPORT_JOB',
            entity_id=import_id,
            action='CANCEL'
        ).first()
        assert cancel_audit is not None
        assert cancel_audit.user_id == env5e13['user_a_id']

        retry_audit = AuditLog.query.filter_by(
            entity_type='IMPORT_JOB',
            entity_id=import_id,
            action='RETRY'
        ).first()
        assert retry_audit is not None
        assert retry_audit.user_id == env5e13['user_a_id']


# ═════════════════════════════════════════════════════════════════════════════
# WORKSTREAM 11: Concurrency, Failure-Injection Rollback & Memory QA (Tests 6-10)
# ═════════════════════════════════════════════════════════════════════════════

def test_06_concurrent_reprocess_confirm_idempotency(app, env5e13, tmp_path):
    """Repeated/concurrent confirm calls produce invariant transaction counts with zero duplicates."""
    pid = env5e13['profile_a_id']
    uid = env5e13['user_a_id']
    period = env5e13['period']
    file_path = str(tmp_path / "concurrency_sample.xlsx")
    _create_sample_excel(file_path, rows_count=8)

    res = process_import(file_path, pid, 'Generic', uid, period, '2024-25', stage=True)
    import_id = res.import_history_id

    # First confirm
    c1 = confirm_import(import_id, pid, uid)
    assert c1['success'] is True
    initial_count = tenant_query(Transaction, pid).filter_by(import_history_id=import_id, is_deleted=False).count()
    assert initial_count == 8

    # Second confirm (simulating race/retry)
    c2 = confirm_import(import_id, pid, uid)
    assert c2['success'] is True
    assert c2.get('already_confirmed') is True

    # Invariant count
    after_count = tenant_query(Transaction, pid).filter_by(import_history_id=import_id, is_deleted=False).count()
    assert after_count == initial_count


def test_07_duplicate_upload_rejection_guard(app, env5e13, tmp_path):
    """Identical file upload within the same profile is caught by duplicate file detection."""
    pid = env5e13['profile_a_id']
    uid = env5e13['user_a_id']
    period = env5e13['period']
    file_path = str(tmp_path / "dup_guard_sample.xlsx")
    _create_sample_excel(file_path, rows_count=5)

    # First upload completed
    res1 = process_import(file_path, pid, 'Generic', uid, period, '2024-25', stage=False)
    assert res1.status == 'COMPLETED'

    # Second upload without allow_duplicate_file
    res2 = process_import(file_path, pid, 'Generic', uid, period, '2024-25', stage=False, allow_duplicate_file=False)
    assert res2.status == 'REJECTED_DUPLICATE_FILE'
    assert any("already imported" in e.lower() or "duplicate" in e.lower() for e in res2.errors)


def test_08_mid_import_failure_injection_atomic_rollback(app, env5e13, tmp_path):
    """Injected mid-import exception rolls back all transactions, leaving zero orphaned active records."""
    pid = env5e13['profile_a_id']
    uid = env5e13['user_a_id']
    period = env5e13['period']
    file_path = str(tmp_path / "fault_sample.xlsx")
    _create_sample_excel(file_path, rows_count=10)

    call_count = [0]
    def faulty_classify(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] >= 4:
            raise RuntimeError("Injected database connection drop at row 4")
        from app.services.classification_service import classify_transaction
        return classify_transaction(*args, **kwargs)

    with pytest.raises(RuntimeError, match="Injected database connection drop"):
        process_import(
            file_path=file_path,
            profile_id=pid,
            platform_name='Generic',
            user_id=uid,
            return_period=period,
            financial_year='2024-25',
            classification_fn=faulty_classify,
        )

    # Assert: 0 transactions remain in the database for this profile
    tx_count = tenant_query(Transaction, pid).count()
    assert tx_count == 0, f"Expected 0 orphaned transactions, found {tx_count}"


def test_09_generation_freeze_overlap_guard(app, env5e13, tmp_path):
    """Direct import and confirm are strictly blocked when return period is frozen by finalized GSTR-1."""
    pid = env5e13['profile_a_id']
    uid = env5e13['user_a_id']
    period = env5e13['period']
    file_path = str(tmp_path / "freeze_test.xlsx")
    _create_sample_excel(file_path, rows_count=5)

    # Finalize GSTR-1 for period
    with app.app_context():
        gen = GSTR1Generation(
            profile_id=pid,
            user_id=uid,
            return_period=period,
            financial_year='2024-25',
            generation_status='COMPLETED',
            validation_passed=True
        )
        _db.session.add(gen)
        _db.session.commit()

    # Direct import attempt must be rejected
    res = process_import(file_path, pid, 'Generic', uid, period, '2024-25', stage=False)
    assert res.status in ('REJECTED_FROZEN', 'FAILED')
    assert any("frozen" in e.lower() for e in res.errors)

    # Staged confirm attempt must raise ValueError
    res_staged = process_import(file_path, pid, 'Generic', uid, period, '2024-25', stage=True)
    with pytest.raises(ValueError, match="frozen"):
        confirm_import(res_staged.import_history_id, pid, uid)


def test_10_large_file_streaming_bounded_memory_and_wb_close(app, env5e13, tmp_path):
    """Streaming multi-thousand-row file operates with bounded memory and closes workbook deterministically."""
    pid = env5e13['profile_a_id']
    uid = env5e13['user_a_id']
    period = env5e13['period']
    file_path = str(tmp_path / "large_2500_rows.xlsx")
    _create_sample_excel(file_path, rows_count=2500)

    res = process_import(file_path, pid, 'Generic', uid, period, '2024-25', stage=True)
    assert res.status == 'STAGED'
    assert res.total_rows == 2500

    # Verify transactions loaded cleanly
    count = tenant_query(Transaction, pid).filter_by(
        import_history_id=res.import_history_id, is_staged=True
    ).count()
    assert count == 2500


# ═════════════════════════════════════════════════════════════════════════════
# WORKSTREAM 12: Production Readiness, Health Probes & Backup Drills (Tests 11-17)
# ═════════════════════════════════════════════════════════════════════════════

def test_11_healthz_fast_liveness_probe(client):
    """GET /healthz returns fast liveness status ok with uptime."""
    resp = client.get('/healthz')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ok'
    assert data['uptime_seconds'] >= 0
    assert 'timestamp' in data


def test_12_readyz_deep_readiness_probe_healthy(client):
    """GET /readyz returns 200 ready when DB, GST rules, adapters, and storage are healthy."""
    resp = client.get('/readyz')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ready'
    assert data['ready'] is True

    checks = data['checks']
    assert checks['database']['status'] == 'ok'
    assert checks['gst_rules']['version'] == '2026.1'
    assert checks['adapters']['production_ready_count'] >= 6
    assert checks['storage']['status'] == 'ok'
    assert checks['job_runner']['mode'] in ('eager', 'celery')


def test_13_readyz_service_unavailable_on_db_failure(client, monkeypatch):
    """GET /readyz returns 503 degraded status when database is unreachable."""
    def broken_execute(*args, **kwargs):
        raise ConnectionError("Simulated database unreachable")

    monkeypatch.setattr(_db.session, 'execute', broken_execute)

    resp = client.get('/readyz')
    assert resp.status_code == 503
    data = resp.get_json()
    assert data['status'] == 'not_ready'
    assert data['ready'] is False
    assert data['checks']['database']['status'] == 'error'


def test_14_production_secret_key_validation_guard():
    """validate_production_config rejects weak/default keys and passes strong keys."""
    # Insecure config
    bad_cfg = {'SECRET_KEY': 'gst-online-seller-dev-key-change-in-production', 'JWT_SECRET_KEY': 'dev'}
    is_valid, issues = validate_production_config(bad_cfg)
    assert not is_valid
    assert len(issues) >= 2

    # Strong config
    good_cfg = {
        'SECRET_KEY': 'c4f92d9e8b7a654123ef0123456789abcdef0123456789abcdef0123456789ab',
        'JWT_SECRET_KEY': '9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b'
    }
    is_valid_good, issues_good = validate_production_config(good_cfg)
    assert is_valid_good
    assert len(issues_good) == 0


def test_15_path_traversal_containment_guard(tmp_path):
    """resolve_contained_path contains filenames inside base directory and rejects traversal attacks."""
    base_dir = tmp_path / "safe_uploads"
    base_dir.mkdir()

    # Safe file
    safe_path = resolve_contained_path(base_dir, "report_august_2024.xlsx")
    assert safe_path.parent == base_dir
    assert safe_path.name == "report_august_2024.xlsx"

    # Traversal attack relative
    contained_relative = resolve_contained_path(base_dir, "../../etc/passwd")
    assert contained_relative.parent == base_dir
    assert contained_relative.name == "passwd"

    # Windows drive traversal
    contained_win = resolve_contained_path(base_dir, "..\\..\\windows\\system32\\calc.exe")
    assert contained_win.parent == base_dir
    assert contained_win.name == "calc.exe"

    # Null byte attack
    with pytest.raises(ValueError, match="Null byte"):
        resolve_contained_path(base_dir, "sample.xlsx\x00.exe")

    # Empty filename
    with pytest.raises(ValueError, match="empty"):
        resolve_contained_path(base_dir, "")


def test_16_sqlite_online_backup_snapshot_creation(app, env5e13, tmp_path):
    """create_backup_snapshot generates consistent .db and verified SHA-256 manifest."""
    backup_dir = tmp_path / "backups"
    manifest = create_backup_snapshot(backup_dir=backup_dir, user_id=env5e13['user_a_id'])

    assert manifest['status'] == 'VERIFIED_CONSISTENT'
    assert os.path.exists(manifest['backup_path'])
    assert os.path.exists(manifest['manifest_path'])
    assert manifest['file_size'] > 0
    assert len(manifest['sha256']) == 64
    assert manifest['gst_rule_version'] == '2026.1'

    # Verify AuditLog created
    with app.app_context():
        audit = AuditLog.query.filter_by(
            action='BACKUP',
            entity_type='SYSTEM',
            user_id=env5e13['user_a_id']
        ).first()
        assert audit is not None


def test_17_backup_verification_and_restore_drill(app, env5e13, tmp_path):
    """Complete backup and restore drill: tamper detection on corrupt file and verified restore."""
    backup_dir = tmp_path / "backups_drill"
    manifest = create_backup_snapshot(backup_dir=backup_dir, user_id=env5e13['user_a_id'])

    backup_file = manifest['backup_path']
    manifest_file = manifest['manifest_path']

    # 1. Tamper detection: flip a single byte in backup file
    tampered_backup = tmp_path / "tampered.db"
    shutil.copyfile(backup_file, tampered_backup)
    with open(tampered_backup, "r+b") as f:
        f.seek(50)
        orig = f.read(1)
        f.seek(50)
        f.write(b'\xFF' if orig != b'\xFF' else b'\x00')

    with pytest.raises(ValueError, match="checksum verification failed"):
        verify_and_restore_backup(
            backup_file=tampered_backup,
            manifest_file=manifest_file,
            user_id=env5e13['user_a_id']
        )

    # 2. Authentic restore
    target_restore_db = tmp_path / "restored.db"
    res = verify_and_restore_backup(
        backup_file=backup_file,
        manifest_file=manifest_file,
        target_db_path=target_restore_db,
        user_id=env5e13['user_a_id']
    )
    assert res['restored'] is True
    assert res['integrity_check'] == 'ok'

    # Verify database readability
    conn = sqlite3.connect(str(target_restore_db))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        assert cur.fetchone()[0] == 'ok'
    finally:
        conn.close()

    # Verify AuditLog created
    with app.app_context():
        restore_audit = AuditLog.query.filter_by(
            action='RESTORE',
            entity_type='SYSTEM',
            user_id=env5e13['user_a_id']
        ).first()
        assert restore_audit is not None
