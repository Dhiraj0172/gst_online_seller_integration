"""
Security Regression Tests for TCS Reconciliation IDOR Prevention.
Verifies that:
- TEST A: Cross-tenant TCS adjustment is rejected and causes zero mutation
- TEST B: Owned TCS adjustment still works as intended
- TEST C: Nonexistent reconciliation ID is safely rejected (no 500, no mutation)
- TEST D: Profile / session mismatch enforces authorization and blocks adjustment
- TEST E: Same-user multi-profile switching allows legitimate adjustment of each profile's records, while foreign user cannot adjust either
- TEST F: Strict database integrity verification proving zero mutation across all fields and audit logs
"""
import pytest
from datetime import datetime
from decimal import Decimal
from app.extensions import db as _db
from app.models import User, GSTProfile, TCSReconciliation, AuditLog


@pytest.fixture
def tcs_security_env(app):
    """Sets up two separate users with profiles and TCS reconciliations."""
    with app.app_context():
        # User A with two profiles
        user_a = User(username='tcs_seller_a', email='tcs_seller_a@example.com')
        user_a.set_password('SecretA123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a1 = GSTProfile(
            user_id=user_a.id,
            gstin='27AAAAA1111A1Z5',
            legal_name='Seller A Maharashtra',
            trade_name='Store A MH',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        profile_a2 = GSTProfile(
            user_id=user_a.id,
            gstin='29AAAAA1111A1Z7',
            legal_name='Seller A Karnataka',
            trade_name='Store A KA',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add_all([profile_a1, profile_a2])
        _db.session.commit()

        # Reconciliation for User A Profile A1
        recon_a1 = TCSReconciliation(
            profile_id=profile_a1.id,
            user_id=user_a.id,
            return_period='082024',
            state_code='27',
            state_name='Maharashtra',
            ecommerce_gstin='27AAPCA0000A1Z1',
            our_net_taxable_value=Decimal('50000.00'),
            our_calculated_tcs=Decimal('250.00'),
            our_calculated_cgst=Decimal('125.00'),
            our_calculated_sgst=Decimal('125.00'),
            our_calculated_igst=Decimal('0.00'),
            portal_taxable_value=Decimal('60000.00'),
            portal_tcs=Decimal('300.00'),
            portal_cgst_tcs=Decimal('150.00'),
            portal_sgst_tcs=Decimal('150.00'),
            portal_igst_tcs=Decimal('0.00'),
            difference_taxable=Decimal('-10000.00'),
            difference_tcs=Decimal('-50.00'),
            difference_cgst=Decimal('-25.00'),
            difference_sgst=Decimal('-25.00'),
            difference_igst=Decimal('0.00'),
            match_status='MISMATCH',
            is_adjusted=False
        )

        # Reconciliation for User A Profile A2
        recon_a2 = TCSReconciliation(
            profile_id=profile_a2.id,
            user_id=user_a.id,
            return_period='082024',
            state_code='29',
            state_name='Karnataka',
            ecommerce_gstin='29AAPCA0000A1Z2',
            our_net_taxable_value=Decimal('40000.00'),
            our_calculated_tcs=Decimal('200.00'),
            our_calculated_cgst=Decimal('0.00'),
            our_calculated_sgst=Decimal('0.00'),
            our_calculated_igst=Decimal('200.00'),
            portal_taxable_value=Decimal('40000.00'),
            portal_tcs=Decimal('200.00'),
            portal_cgst_tcs=Decimal('0.00'),
            portal_sgst_tcs=Decimal('0.00'),
            portal_igst_tcs=Decimal('200.00'),
            difference_taxable=Decimal('0.00'),
            difference_tcs=Decimal('0.00'),
            difference_cgst=Decimal('0.00'),
            difference_sgst=Decimal('0.00'),
            difference_igst=Decimal('0.00'),
            match_status='MATCHED',
            is_adjusted=False
        )
        _db.session.add_all([recon_a1, recon_a2])
        _db.session.commit()

        # User B with one profile
        user_b = User(username='tcs_seller_b', email='tcs_seller_b@example.com')
        user_b.set_password('SecretB456!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin='27BBBBB2222B1Z6',
            legal_name='Seller B Electronics',
            trade_name='Store B MH',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Reconciliation for User B Profile B
        recon_b = TCSReconciliation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            state_code='27',
            state_name='Maharashtra',
            ecommerce_gstin='27AAPCB9999B1Z9',
            our_net_taxable_value=Decimal('10000.00'),
            our_calculated_tcs=Decimal('50.00'),
            portal_taxable_value=Decimal('12000.00'),
            portal_tcs=Decimal('60.00'),
            match_status='MISMATCH',
            is_adjusted=False
        )
        _db.session.add(recon_b)
        _db.session.commit()

        yield {
            'user_a_id': user_a.id,
            'user_b_id': user_b.id,
            'profile_a1_id': profile_a1.id,
            'profile_a2_id': profile_a2.id,
            'profile_b_id': profile_b.id,
            'recon_a1_id': recon_a1.id,
            'recon_a2_id': recon_a2.id,
            'recon_b_id': recon_b.id,
        }

        # Cleanup
        try:
            TCSReconciliation.query.filter(
                TCSReconciliation.profile_id.in_([profile_a1.id, profile_a2.id, profile_b.id])
            ).delete(synchronize_session=False)
            AuditLog.query.filter(
                AuditLog.user_id.in_([user_a.id, user_b.id])
            ).delete(synchronize_session=False)
            _db.session.delete(profile_a1)
            _db.session.delete(profile_a2)
            _db.session.delete(profile_b)
            _db.session.delete(user_a)
            _db.session.delete(user_b)
            _db.session.commit()
        except Exception:
            _db.session.rollback()


def test_cross_tenant_tcs_adjustment_is_rejected(client, tcs_security_env):
    """
    TEST A: CROSS-TENANT TCS ADJUSTMENT IS REJECTED
    User B attempts to adjust User A's reconciliation row.
    Request must be rejected with 404, and User A's row must remain untouched.
    """
    recon_a1_id = tcs_security_env['recon_a1_id']

    # Authenticate as User B
    res_login = client.post('/login', data={'username': 'tcs_seller_b', 'password': 'SecretB456!'}, follow_redirects=True)
    assert res_login.status_code == 200

    # Capture state before attack
    recon_before = _db.session.get(TCSReconciliation, recon_a1_id)
    assert recon_before.is_adjusted is False
    assert recon_before.match_status == 'MISMATCH'

    # Attempt cross-tenant adjustment
    res_adjust = client.post(f'/tcs/adjust/{recon_a1_id}', data={
        'adjustment_type': 'ACCEPT_PORTAL',
        'notes': 'Malicious cross-tenant override attempt'
    })
    assert res_adjust.status_code == 404

    # Verify foreign record is unchanged
    _db.session.expire_all()
    recon_after = _db.session.get(TCSReconciliation, recon_a1_id)
    assert recon_after.is_adjusted is False
    assert recon_after.match_status == 'MISMATCH'
    assert recon_after.our_calculated_tcs == Decimal('250.00')
    assert recon_after.portal_tcs == Decimal('300.00')
    assert recon_after.adjustment_notes is None
    assert recon_after.adjusted_by is None
    assert recon_after.adjusted_at is None


def test_owned_tcs_adjustment_still_works(client, tcs_security_env):
    """
    TEST B: OWNED TCS ADJUSTMENT STILL WORKS
    User A adjusts their own reconciliation row for their active profile.
    Request must succeed and update fields correctly.
    """
    user_a_id = tcs_security_env['user_a_id']
    p_a1 = tcs_security_env['profile_a1_id']
    recon_a1_id = tcs_security_env['recon_a1_id']

    # Authenticate as User A
    res_login = client.post('/login', data={'username': 'tcs_seller_a', 'password': 'SecretA123!'}, follow_redirects=True)
    assert res_login.status_code == 200

    # Ensure profile A1 is active
    client.get(f'/profiles/{p_a1}/select', follow_redirects=True)

    # Perform legitimate adjustment (ACCEPT_PORTAL)
    res_adjust = client.post(f'/tcs/adjust/{recon_a1_id}', data={
        'adjustment_type': 'ACCEPT_PORTAL',
        'notes': 'Legitimate acceptance of portal numbers'
    }, follow_redirects=True)
    assert res_adjust.status_code == 200

    # Verify DB mutation
    _db.session.expire_all()
    recon = _db.session.get(TCSReconciliation, recon_a1_id)
    assert recon.is_adjusted is True
    assert recon.match_status == 'MATCHED'
    assert recon.adjusted_by == user_a_id
    assert recon.our_calculated_tcs == recon.portal_tcs
    assert recon.our_calculated_cgst == recon.portal_cgst_tcs
    assert recon.our_calculated_sgst == recon.portal_sgst_tcs
    assert 'Legitimate acceptance of portal numbers' in recon.adjustment_notes

    # Verify audit log was created
    audits = AuditLog.query.filter_by(
        entity_type='TCSReconciliation', entity_id=recon_a1_id
    ).all()
    assert len(audits) >= 1
    assert audits[-1].action == 'TCS_ADJUSTMENT'
    assert audits[-1].user_id == user_a_id


def test_nonexistent_reconciliation_id(client, tcs_security_env):
    """
    TEST C: NONEXISTENT RECONCILIATION ID
    Adjustment attempt on nonexistent ID must be rejected safely (404, not 500).
    """
    p_a1 = tcs_security_env['profile_a1_id']
    client.post('/login', data={'username': 'tcs_seller_a', 'password': 'SecretA123!'}, follow_redirects=True)
    client.get(f'/profiles/{p_a1}/select', follow_redirects=True)

    # Nonexistent reconciliation ID
    res = client.post('/tcs/adjust/9999999', data={
        'adjustment_type': 'ACCEPT_PORTAL',
        'notes': 'Attempt on nonexistent ID'
    })
    assert res.status_code == 404


def test_profile_session_mismatch(client, tcs_security_env):
    """
    TEST D: PROFILE / SESSION MISMATCH
    User A has active profile A2, but attempts to adjust a reconciliation belonging to profile A1.
    Must be rejected with 404 and record must remain unchanged.
    Also tests User B attempting to adjust Profile A's reconciliation.
    """
    p_a2 = tcs_security_env['profile_a2_id']
    recon_a1_id = tcs_security_env['recon_a1_id']

    # User A logs in and selects profile A2
    client.post('/login', data={'username': 'tcs_seller_a', 'password': 'SecretA123!'}, follow_redirects=True)
    client.get(f'/profiles/{p_a2}/select', follow_redirects=True)

    # Try to adjust recon_a1 (belongs to profile A1, not the currently active profile A2)
    res_mismatch = client.post(f'/tcs/adjust/{recon_a1_id}', data={
        'adjustment_type': 'ACCEPT_PORTAL',
        'notes': 'Attempt adjustment on inactive profile row'
    })
    assert res_mismatch.status_code == 404

    # Record must be unchanged
    _db.session.expire_all()
    recon_a1 = _db.session.get(TCSReconciliation, recon_a1_id)
    assert recon_a1.is_adjusted is False
    assert recon_a1.match_status == 'MISMATCH'

    # Now User B logs in and also attempts
    client.get('/logout', follow_redirects=True)
    client.post('/login', data={'username': 'tcs_seller_b', 'password': 'SecretB456!'}, follow_redirects=True)
    res_user_b = client.post(f'/tcs/adjust/{recon_a1_id}', data={
        'adjustment_type': 'ACCEPT_INTERNAL',
        'notes': 'User B attempt'
    })
    assert res_user_b.status_code == 404

    _db.session.expire_all()
    recon_a1_check = _db.session.get(TCSReconciliation, recon_a1_id)
    assert recon_a1_check.is_adjusted is False


def test_same_user_multi_profile_behavior(client, tcs_security_env):
    """
    TEST E: SAME-USER MULTI-PROFILE BEHAVIOR
    User A can adjust recon_a1 when profile A1 is active, and recon_a2 when profile A2 is active.
    User B cannot adjust either reconciliation.
    """
    user_a_id = tcs_security_env['user_a_id']
    p_a1 = tcs_security_env['profile_a1_id']
    p_a2 = tcs_security_env['profile_a2_id']
    recon_a1_id = tcs_security_env['recon_a1_id']
    recon_a2_id = tcs_security_env['recon_a2_id']

    # 1. User A selects profile A1 and adjusts recon_a1
    client.post('/login', data={'username': 'tcs_seller_a', 'password': 'SecretA123!'}, follow_redirects=True)
    client.get(f'/profiles/{p_a1}/select', follow_redirects=True)
    res_a1 = client.post(f'/tcs/adjust/{recon_a1_id}', data={
        'adjustment_type': 'ACCEPT_INTERNAL',
        'notes': 'Profile A1 internal accepted'
    }, follow_redirects=True)
    assert res_a1.status_code == 200

    _db.session.expire_all()
    r1 = _db.session.get(TCSReconciliation, recon_a1_id)
    assert r1.is_adjusted is True
    assert r1.match_status == 'MATCHED'
    assert r1.adjusted_by == user_a_id

    # 2. User A switches to profile A2 and adjusts recon_a2
    client.get(f'/profiles/{p_a2}/select', follow_redirects=True)
    res_a2 = client.post(f'/tcs/adjust/{recon_a2_id}', data={
        'adjustment_type': 'MANUAL_OVERRIDE',
        'notes': 'Profile A2 manual override'
    }, follow_redirects=True)
    assert res_a2.status_code == 200

    _db.session.expire_all()
    r2 = _db.session.get(TCSReconciliation, recon_a2_id)
    assert r2.is_adjusted is True
    assert r2.match_status == 'MANUALLY_ADJUSTED'
    assert r2.adjustment_notes == 'Profile A2 manual override'

    # 3. User B cannot adjust either
    client.get('/logout', follow_redirects=True)
    client.post('/login', data={'username': 'tcs_seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res_b_a1 = client.post(f'/tcs/adjust/{recon_a1_id}', data={'adjustment_type': 'ACCEPT_PORTAL'})
    assert res_b_a1.status_code == 404

    res_b_a2 = client.post(f'/tcs/adjust/{recon_a2_id}', data={'adjustment_type': 'ACCEPT_PORTAL'})
    assert res_b_a2.status_code == 404


def test_database_integrity_after_unauthorized_request(client, tcs_security_env):
    """
    TEST F: DATABASE INTEGRITY AFTER UNAUTHORIZED REQUEST
    Explicitly snapshot every field of recon_a1 and audit log count before the attack.
    Execute an unauthorized cross-tenant adjust attempt.
    Assert zero mutation across all fields and no audit logs added.
    """
    recon_a1_id = tcs_security_env['recon_a1_id']

    # Snapshot all fields of recon_a1 before request
    recon_before = _db.session.get(TCSReconciliation, recon_a1_id)
    snapshot = {
        'id': recon_before.id,
        'profile_id': recon_before.profile_id,
        'user_id': recon_before.user_id,
        'return_period': recon_before.return_period,
        'import_history_id': recon_before.import_history_id,
        'ecommerce_gstin': recon_before.ecommerce_gstin,
        'state_code': recon_before.state_code,
        'state_name': recon_before.state_name,
        'our_net_taxable_value': recon_before.our_net_taxable_value,
        'our_calculated_tcs': recon_before.our_calculated_tcs,
        'our_calculated_cgst': recon_before.our_calculated_cgst,
        'our_calculated_sgst': recon_before.our_calculated_sgst,
        'our_calculated_igst': recon_before.our_calculated_igst,
        'portal_taxable_value': recon_before.portal_taxable_value,
        'portal_tcs': recon_before.portal_tcs,
        'portal_cgst_tcs': recon_before.portal_cgst_tcs,
        'portal_sgst_tcs': recon_before.portal_sgst_tcs,
        'portal_igst_tcs': recon_before.portal_igst_tcs,
        'difference_taxable': recon_before.difference_taxable,
        'difference_tcs': recon_before.difference_tcs,
        'difference_cgst': recon_before.difference_cgst,
        'difference_sgst': recon_before.difference_sgst,
        'difference_igst': recon_before.difference_igst,
        'match_status': recon_before.match_status,
        'ambiguity_details': recon_before.ambiguity_details,
        'adjustment_notes': recon_before.adjustment_notes,
        'is_adjusted': recon_before.is_adjusted,
        'adjusted_by': recon_before.adjusted_by,
        'adjusted_at': recon_before.adjusted_at,
        'created_at': recon_before.created_at,
    }

    audit_count_before = AuditLog.query.filter_by(
        entity_type='TCSReconciliation', entity_id=recon_a1_id
    ).count()

    # User B logs in and attempts to adjust recon_a1
    client.post('/login', data={'username': 'tcs_seller_b', 'password': 'SecretB456!'}, follow_redirects=True)
    res = client.post(f'/tcs/adjust/{recon_a1_id}', data={
        'adjustment_type': 'ACCEPT_PORTAL',
        'notes': 'Malicious tamper attempt with arbitrary payload'
    })
    assert res.status_code == 404

    # Expire session cache to force fresh DB fetch
    _db.session.expire_all()
    recon_after = _db.session.get(TCSReconciliation, recon_a1_id)

    # Compare every single field before and after
    for field, before_value in snapshot.items():
        after_value = getattr(recon_after, field)
        assert after_value == before_value, f"Field '{field}' mutated! Before: {before_value}, After: {after_value}"

    # Confirm no audit log was created for this entity
    audit_count_after = AuditLog.query.filter_by(
        entity_type='TCSReconciliation', entity_id=recon_a1_id
    ).count()
    assert audit_count_after == audit_count_before, f"AuditLog created during unauthorized attempt! Before: {audit_count_before}, After: {audit_count_after}"


def test_direct_service_apply_adjustment_authorization(app, tcs_security_env):
    """
    Direct service-layer test verifying apply_adjustment() authorization guards:
    - foreign user -> returns False
    - foreign profile -> returns False
    - same user profile mismatch -> returns False
    - authorized owner with matching profile -> returns True
    """
    from app.services.tcs_service import apply_adjustment

    user_a_id = tcs_security_env['user_a_id']
    user_b_id = tcs_security_env['user_b_id']
    p_a1 = tcs_security_env['profile_a1_id']
    p_a2 = tcs_security_env['profile_a2_id']
    p_b = tcs_security_env['profile_b_id']
    recon_a1_id = tcs_security_env['recon_a1_id']

    with app.app_context():
        # 1. Foreign user B attempt
        success_foreign = apply_adjustment(recon_a1_id, 'ACCEPT_PORTAL', 'Foreign user attack', user_id=user_b_id)
        assert success_foreign is False

        # 2. Foreign profile B attempt
        success_foreign_prof = apply_adjustment(recon_a1_id, 'ACCEPT_PORTAL', 'Foreign profile attack', user_id=user_a_id, profile_id=p_b)
        assert success_foreign_prof is False

        # 3. Same user but mismatched profile (profile A2 for row belonging to profile A1)
        success_mismatch = apply_adjustment(recon_a1_id, 'ACCEPT_PORTAL', 'Profile mismatch attack', user_id=user_a_id, profile_id=p_a2)
        assert success_mismatch is False

        # Verify recon_a1 remains unadjusted
        _db.session.expire_all()
        r_check = _db.session.get(TCSReconciliation, recon_a1_id)
        assert r_check.is_adjusted is False

        # 4. Authorized owner and authorized profile
        success_valid = apply_adjustment(recon_a1_id, 'ACCEPT_PORTAL', 'Authorized call', user_id=user_a_id, profile_id=p_a1)
        assert success_valid is True

        _db.session.expire_all()
        r_valid = _db.session.get(TCSReconciliation, recon_a1_id)
        assert r_valid.is_adjusted is True
        assert r_valid.adjusted_by == user_a_id
