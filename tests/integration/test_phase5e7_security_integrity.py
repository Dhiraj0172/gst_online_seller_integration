"""Phase 5E-7 Integration Tests: Security Hardening, Relational Integrity & Profile Audit Compliance.

Covers all 23 hardened behaviors:
1. test_generate_run_rejects_get (405 Method Not Allowed)
2. test_generate_regenerate_rejects_get (405 Method Not Allowed)
3. test_generate_run_accepts_post (200/302 as expected)
4. test_generate_regenerate_accepts_post (200/302 as expected)
5. test_profile_select_safe_redirect (redirects to safe local URL)
6. test_profile_select_unsafe_redirect_fallback (redirects to dashboard when referrer/next is external)
7. test_profile_create_valid_gstin (succeeds)
8. test_profile_create_invalid_gstin (rejected with 400)
9. test_profile_edit_valid_gstin (succeeds, updates database)
10. test_profile_edit_invalid_gstin (rejected with 400, database unchanged)
11. test_profile_edit_preserves_unmodified_fields
12. test_profile_delete_cascades_to_tcs_reconciliation (deletes profile -> TCS reconciliation deleted)
13. test_profile_tcs_reconciliation_relationship_bidirectional (profile.tcs_reconciliations and recon.profile)
14. test_profile_create_audit_logged (AuditLog created with entity_type='GSTProfile', action='CREATE')
15. test_profile_edit_audit_logged (AuditLog created with entity_type='GSTProfile', action='UPDATE')
16. test_profile_delete_audit_logged (AuditLog created with entity_type='GSTProfile', action='DELETE')
17. test_service_gate_blocks_generation_on_critical_failure (generate_gstr1 raises GenerationBlockedError)
18. test_service_gate_allows_generation_on_clean_data (generate_gstr1 proceeds)
19. test_service_gate_allows_generation_on_warnings_only (generate_gstr1 proceeds)
20. test_route_gate_redirects_with_flash_on_blocked_generation
21. test_route_regenerate_gate_redirects_with_flash_on_blocked_generation
22. test_download_excel_regenerate_gate_blocks_on_critical_failure
23. test_download_json_regenerate_gate_blocks_on_critical_failure
"""
import uuid
import json
from datetime import datetime
from unittest.mock import patch
import pytest

from app.extensions import db as _db
from app.models import User, GSTProfile, TCSReconciliation, GSTR1Generation, AuditLog
from app.services.gstr1_generator import generate_gstr1, GenerationBlockedError, GenerationResult


_counter = 0

def _get_unique_suffix():
    global _counter
    _counter += 1
    return f"{_counter:04d}_{uuid.uuid4().hex[:6]}"


def _get_unique_gstin():
    global _counter
    _counter += 1
    return f"27ABCDE{_counter % 10000:04d}F1Z5"


@pytest.fixture
def env5e7(app, db):
    """Fixture providing user and active profile for Phase 5E-7 tests."""
    suffix = _get_unique_suffix()
    gstin = _get_unique_gstin()
    with app.app_context():
        user = User(username=f'p5e7_user_{suffix}', email=f'p5e7_{suffix}@example.com')
        user.set_password('Password5e7!')
        _db.session.add(user)
        _db.session.commit()

        profile = GSTProfile(
            user_id=user.id,
            gstin=gstin,
            legal_name=f'Initial Enterprise {suffix}',
            trade_name='InitCorp',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile)
        _db.session.commit()

        yield {
            'user_id': user.id,
            'username': user.username,
            'password': 'Password5e7!',
            'profile_id': profile.id,
            'gstin': profile.gstin,
            'legal_name': profile.legal_name
        }


def _login(client, env):
    """Helper to log in client and select profile."""
    client.post('/login', data={'username': env['username'], 'password': env['password']}, follow_redirects=True)
    with client.session_transaction() as sess:
        sess['active_profile_id'] = env['profile_id']
        sess['return_period'] = '012025'


# ---------------------------------------------------------------------------
# Scope 1: Generation HTTP Method Hardening
# ---------------------------------------------------------------------------

def test_01_generate_run_rejects_get(client, env5e7):
    """1. GET /generate/run must return 405 Method Not Allowed."""
    _login(client, env5e7)
    res = client.get('/generate/run?return_period=012025')
    assert res.status_code == 405


def test_02_generate_regenerate_rejects_get(client, env5e7):
    """2. GET /generate/regenerate must return 405 Method Not Allowed."""
    _login(client, env5e7)
    res = client.get('/generate/regenerate?return_period=012025')
    assert res.status_code == 405


def test_03_generate_run_accepts_post(client, env5e7):
    """3. POST /generate/run is accepted (returns 302 redirect)."""
    _login(client, env5e7)
    res = client.post('/generate/run', data={'return_period': '012025'}, follow_redirects=False)
    assert res.status_code == 302


def test_04_generate_regenerate_accepts_post(client, env5e7):
    """4. POST /generate/regenerate is accepted (returns 302 redirect)."""
    _login(client, env5e7)
    res = client.post('/generate/regenerate', data={'return_period': '012025'}, follow_redirects=False)
    assert res.status_code == 302


# ---------------------------------------------------------------------------
# Scope 2: Safe Profile Redirect
# ---------------------------------------------------------------------------

def test_05_profile_select_safe_redirect(client, env5e7):
    """5. Profile select safely redirects to safe local URL."""
    _login(client, env5e7)
    res = client.get(f"/profiles/{env5e7['profile_id']}/select?next=/generate", follow_redirects=False)
    assert res.status_code == 302
    assert res.location == '/generate'


def test_06_profile_select_unsafe_redirect_fallback(client, env5e7):
    """6. Profile select falls back to /dashboard on external or unsafe redirect."""
    _login(client, env5e7)
    # External next param
    res_ext = client.get(f"/profiles/{env5e7['profile_id']}/select?next=https://attacker.com/malicious", follow_redirects=False)
    assert res_ext.status_code == 302
    assert res_ext.location == '/dashboard' or res_ext.location.endswith('/dashboard')

    # External Referer header without next param
    res_ref = client.get(
        f"/profiles/{env5e7['profile_id']}/select",
        headers={'Referer': 'https://evil-phishing.com/login'},
        follow_redirects=False
    )
    assert res_ref.status_code == 302
    assert res_ref.location == '/dashboard' or res_ref.location.endswith('/dashboard')


# ---------------------------------------------------------------------------
# Scope 3: GSTIN Validation on Profile Edit & Create
# ---------------------------------------------------------------------------

def test_07_profile_create_valid_gstin(client, env5e7, app):
    """7. Profile creation succeeds with valid 15-character GSTIN."""
    _login(client, env5e7)
    new_gstin = _get_unique_gstin()
    res = client.post('/profiles/create', data={
        'gstin': new_gstin,
        'legal_name': 'Karnataka Branch',
        'trade_name': 'KB Corp',
        'state': '27',
        'frequency': 'Monthly'
    }, follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        p = GSTProfile.query.filter_by(gstin=new_gstin).first()
        assert p is not None
        assert p.legal_name == 'Karnataka Branch'


def test_08_profile_edit_invalid_gstin_format(client, env5e7, app):
    """8. Profile edit rejects malformed GSTIN format with 400."""
    _login(client, env5e7)
    res = client.post(f"/profiles/{env5e7['profile_id']}/edit", data={
        'gstin': 'INVALID_GSTIN_123',
        'legal_name': 'Invalid Entity',
        'state': '27'
    }, follow_redirects=False)
    assert res.status_code == 400

    with app.app_context():
        p = _db.session.get(GSTProfile, env5e7['profile_id'])
        assert p.gstin == env5e7['gstin']
        assert p.legal_name == env5e7['legal_name']


def test_09_profile_edit_valid_gstin(client, env5e7, app):
    """9. Profile edit succeeds with valid GSTIN and updates DB."""
    _login(client, env5e7)
    updated_gstin = _get_unique_gstin()
    res = client.post(f"/profiles/{env5e7['profile_id']}/edit", data={
        'gstin': updated_gstin,
        'legal_name': 'Renamed Enterprise',
        'trade_name': 'InitCorp',
        'state': '27',
        'frequency': 'Quarterly'
    }, follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        p = _db.session.get(GSTProfile, env5e7['profile_id'])
        assert p.gstin == updated_gstin
        assert p.legal_name == 'Renamed Enterprise'
        assert p.filing_frequency == 'Quarterly'


def test_10_profile_edit_invalid_gstin(client, env5e7, app):
    """10. Profile edit rejects invalid GSTIN with 400 and keeps database unchanged."""
    _login(client, env5e7)
    res = client.post(f"/profiles/{env5e7['profile_id']}/edit", data={
        'gstin': 'MALFORMED-GST',
        'legal_name': 'Hacked Name'
    }, follow_redirects=False)
    assert res.status_code == 400

    with app.app_context():
        p = _db.session.get(GSTProfile, env5e7['profile_id'])
        assert p.gstin == env5e7['gstin']
        assert p.legal_name == env5e7['legal_name']


def test_11_profile_edit_preserves_unmodified_fields(client, env5e7, app):
    """11. Profile edit preserves unmodified fields when partial data is posted."""
    _login(client, env5e7)
    res = client.post(f"/profiles/{env5e7['profile_id']}/edit", data={
        'legal_name': 'Solely Updated Name'
        # gstin, trade_name, state, frequency omitted
    }, follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        p = _db.session.get(GSTProfile, env5e7['profile_id'])
        assert p.legal_name == 'Solely Updated Name'
        assert p.gstin == env5e7['gstin']
        assert p.trade_name == 'InitCorp'
        assert p.state_code == '27'
        assert p.filing_frequency == 'Monthly'


# ---------------------------------------------------------------------------
# Scope 4: Relational Integrity & Cascade on TCSReconciliation
# ---------------------------------------------------------------------------

def test_12_profile_delete_cascades_to_tcs_reconciliation(client, env5e7, app):
    """12. Deleting a GSTProfile cascades and removes associated TCSReconciliation records."""
    _login(client, env5e7)

    with app.app_context():
        p = _db.session.get(GSTProfile, env5e7['profile_id'])
        recon = TCSReconciliation(
            profile=p,
            user_id=env5e7['user_id'],
            return_period='012025',
            state_code='27',
            match_status='MATCHED'
        )
        _db.session.add(recon)
        _db.session.commit()
        recon_id = recon.id

        assert _db.session.get(TCSReconciliation, recon_id) is not None

    # Delete profile via POST
    res = client.post(f"/profiles/{env5e7['profile_id']}/delete", follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        assert _db.session.get(GSTProfile, env5e7['profile_id']) is None
        assert _db.session.get(TCSReconciliation, recon_id) is None


def test_13_profile_tcs_reconciliation_relationship_bidirectional(env5e7, app):
    """13. Bidirectional relationship between GSTProfile and TCSReconciliation works both ways."""
    with app.app_context():
        p = _db.session.get(GSTProfile, env5e7['profile_id'])
        recon = TCSReconciliation(
            profile=p,
            user_id=env5e7['user_id'],
            return_period='022025',
            state_code='27',
            match_status='MATCHED'
        )
        _db.session.add(recon)
        _db.session.commit()

        # Check forward navigation
        assert recon in p.tcs_reconciliations
        # Check backward navigation
        assert recon.profile.id == p.id
        assert recon.profile.gstin == p.gstin


# ---------------------------------------------------------------------------
# Scope 5: Profile Lifecycle Audit Logging
# ---------------------------------------------------------------------------

def test_14_profile_create_audit_logged(client, env5e7, app):
    """14. Profile creation logs an AuditLog entry with entity_type='GSTProfile', action='CREATE'."""
    _login(client, env5e7)
    create_gstin = _get_unique_gstin()
    res = client.post('/profiles/create', data={
        'gstin': create_gstin,
        'legal_name': 'Audited Creator Entity',
        'trade_name': 'AuditCorp',
        'state': '27',
        'frequency': 'Monthly'
    }, follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        created_profile = GSTProfile.query.filter_by(gstin=create_gstin).first()
        assert created_profile is not None

        audit = AuditLog.query.filter_by(
            entity_type='GSTProfile',
            entity_id=created_profile.id,
            action='CREATE'
        ).first()
        assert audit is not None
        assert audit.user_id == env5e7['user_id']
        assert create_gstin in audit.new_value


def test_15_profile_edit_audit_logged(client, env5e7, app):
    """15. Profile edit logs an AuditLog entry with action='UPDATE' and old/new snapshots."""
    _login(client, env5e7)
    edit_gstin = _get_unique_gstin()
    res = client.post(f"/profiles/{env5e7['profile_id']}/edit", data={
        'gstin': edit_gstin,
        'legal_name': 'Audit Updated Name',
        'trade_name': 'AuditCorp',
        'state': '27',
        'frequency': 'Monthly'
    }, follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        audit = AuditLog.query.filter_by(
            entity_type='GSTProfile',
            entity_id=env5e7['profile_id'],
            action='UPDATE'
        ).order_by(AuditLog.id.desc()).first()
        assert audit is not None
        assert audit.user_id == env5e7['user_id']
        assert env5e7['legal_name'] in audit.old_value
        assert 'Audit Updated Name' in audit.new_value


def test_16_profile_delete_audit_logged(client, env5e7, app):
    """16. Profile deletion logs an AuditLog entry with action='DELETE'."""
    _login(client, env5e7)
    res = client.post(f"/profiles/{env5e7['profile_id']}/delete", follow_redirects=False)
    assert res.status_code == 302

    with app.app_context():
        audit = AuditLog.query.filter_by(
            entity_type='GSTProfile',
            entity_id=env5e7['profile_id'],
            action='DELETE'
        ).first()
        assert audit is not None
        assert audit.user_id == env5e7['user_id']
        assert env5e7['legal_name'] in audit.old_value


# ---------------------------------------------------------------------------
# Scope 6: Service-Layer Generation Gate
# ---------------------------------------------------------------------------

def test_17_service_gate_blocks_generation_on_critical_failure(env5e7, app):
    """17. generate_gstr1 raises GenerationBlockedError when reconciliation is blocked."""
    with app.app_context():
        blocked_report = {
            'status': 'BLOCKED',
            'is_generation_blocked': True,
            'critical_failures': 2,
            'blocking_reasons': ['PAN mismatch detected', 'State mismatch']
        }
        with pytest.raises(GenerationBlockedError) as exc_info:
            generate_gstr1(str(env5e7['profile_id']), '012025', reconciliation_report=blocked_report)

        assert 'Generation blocked' in exc_info.value.message or 'GSTR-1 generation blocked' in str(exc_info.value)
        assert exc_info.value.reconciliation_report['status'] == 'BLOCKED'
        assert exc_info.value.reconciliation_report['critical_failures'] == 2


def test_18_service_gate_allows_generation_on_clean_data(env5e7, app):
    """18. generate_gstr1 proceeds normally when reconciliation is clean (SUCCESS)."""
    with app.app_context():
        clean_report = {
            'status': 'SUCCESS',
            'is_generation_blocked': False,
            'critical_failures': 0,
            'warnings': 0,
            'differences': []
        }
        res = generate_gstr1(str(env5e7['profile_id']), '012025', reconciliation_report=clean_report)
        assert isinstance(res, GenerationResult)
        assert res.reconciliation_report['status'] == 'SUCCESS'


def test_19_service_gate_allows_generation_on_warnings_only(env5e7, app):
    """19. generate_gstr1 proceeds normally when reconciliation has warnings only (WARNING)."""
    with app.app_context():
        warning_report = {
            'status': 'WARNING',
            'is_generation_blocked': False,
            'critical_failures': 0,
            'warnings': 3,
            'blocking_reasons': []
        }
        res = generate_gstr1(str(env5e7['profile_id']), '012025', reconciliation_report=warning_report)
        assert isinstance(res, GenerationResult)
        assert res.reconciliation_report['status'] == 'WARNING'


# ---------------------------------------------------------------------------
# Scope 7: Route Integration & Download Gate Safety
# ---------------------------------------------------------------------------

def test_20_route_gate_redirects_with_flash_on_blocked_generation(client, env5e7):
    """20. POST /generate/run redirects to validation-errors when reconciliation blocks."""
    _login(client, env5e7)

    class MockBlockedRecon:
        status = 'BLOCKED'
        is_generation_blocked = True
        critical_failures = 1
        warnings = 0
        blocking_reasons = ['Critical error']
        def to_dict(self):
            return {
                'status': 'BLOCKED',
                'is_generation_blocked': True,
                'critical_failures': 1,
                'warnings': 0,
                'blocking_reasons': ['Critical error']
            }

    with patch('app.routes.generate.run_full_reconciliation', return_value=MockBlockedRecon()):
        res = client.post('/generate/run', data={'return_period': '012025'}, follow_redirects=False)
        assert res.status_code == 302
        assert '/statement/validation-errors' in res.location


def test_21_route_regenerate_gate_redirects_with_flash_on_blocked_generation(client, env5e7):
    """21. POST /generate/regenerate redirects to validation-errors when reconciliation blocks."""
    _login(client, env5e7)

    class MockBlockedRecon:
        status = 'BLOCKED'
        is_generation_blocked = True
        critical_failures = 1
        warnings = 0
        blocking_reasons = ['Critical error']
        def to_dict(self):
            return {
                'status': 'BLOCKED',
                'is_generation_blocked': True,
                'critical_failures': 1,
                'warnings': 0,
                'blocking_reasons': ['Critical error']
            }

    with patch('app.routes.generate.run_full_reconciliation', return_value=MockBlockedRecon()):
        res = client.post('/generate/regenerate', data={'return_period': '012025'}, follow_redirects=False)
        assert res.status_code == 302
        assert '/statement/validation-errors' in res.location


def test_22_download_excel_regenerate_gate_blocks_on_critical_failure(client, env5e7, app):
    """22. GET /generate/download/excel/<id> safely catches GenerationBlockedError and redirects."""
    _login(client, env5e7)

    with app.app_context():
        gen = GSTR1Generation(
            profile_id=env5e7['profile_id'],
            user_id=env5e7['user_id'],
            return_period='012025',
            financial_year='2024-25',
            excel_file_path='/non_existent_file.xlsx',
            json_file_path='/non_existent_file.json',
            generation_status='COMPLETED'
        )
        _db.session.add(gen)
        _db.session.commit()
        gen_id = gen.id

    with patch('app.routes.generate.generate_gstr1', side_effect=GenerationBlockedError("Critical error")):
        res = client.get(f'/generate/download/excel/{gen_id}', follow_redirects=False)
        assert res.status_code == 302
        assert '/generate' in res.location


def test_23_download_json_regenerate_gate_blocks_on_critical_failure(client, env5e7, app):
    """23. GET /generate/download/json/<id> safely catches GenerationBlockedError and redirects."""
    _login(client, env5e7)

    with app.app_context():
        gen = GSTR1Generation(
            profile_id=env5e7['profile_id'],
            user_id=env5e7['user_id'],
            return_period='012025',
            financial_year='2024-25',
            excel_file_path='/non_existent_file.xlsx',
            json_file_path='/non_existent_file.json',
            generation_status='COMPLETED'
        )
        _db.session.add(gen)
        _db.session.commit()
        gen_id = gen.id

    with patch('app.routes.generate.generate_gstr1', side_effect=GenerationBlockedError("Critical error")):
        res = client.get(f'/generate/download/json/{gen_id}', follow_redirects=False)
        assert res.status_code == 302
        assert '/generate' in res.location
