"""Phase 5E-4 Integration Tests: GSTR-1 REST API Suite.

Covers:
- Tier 1: Core Feature Coverage
  1. POST /api/generate with CLEAN status -> 201 Created and generation record persisted.
  2. POST /api/generate with WARNING status -> 201 Created and generation allowed.
  3. POST /api/generate with BLOCKED status -> 403 Forbidden, generation rejected.
  4. POST /api/generate client bypass rejection (force=true,  ignored).
  5. GET /api/generations listing generations scoped to active profile.
  6. GET /api/generations?return_period=MMYYYY filtering by period.
  7. GET /api/generate/<id> details endpoint returning complete generation data.
  8. GET /api/generate/<id> cross-tenant IDOR protection (404 Not Found).
- Tier 2: Boundary & Corner Cases
  9. POST /api/generate with missing or invalid return_period -> 400 Bad Request.
  10. GET /api/generate/<id> for non-existent generation ID -> 404 Not Found.
  11. Unauthenticated requests to all API endpoints -> 401 Unauthorized / redirect.
- Tier 3: Cross-Feature Combinations
  12. Complete API lifecycle: Reconcile -> Generate -> Query List -> Fetch Details.
  13. Tenant isolation across list and detail endpoints simultaneously.
- Tier 4: Real-World Scenarios
  14. Multi-period generation and retrieval via REST API.
"""
import os
import json
import uuid
import random
from datetime import date, datetime
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, GSTR1Generation
from app.services.reconciliation_service import run_full_reconciliation


@pytest.fixture
def api_test_env(app, db):
    """Set up multi-user, multi-period environment with CLEAN, WARNING, and BLOCKED periods."""
    rand_a = f"{random.randint(1000, 9999)}"
    rand_b = f"{random.randint(1000, 9999)}"
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]

    gstin_a = f"27AAAAA{rand_a}A1Z5"
    gstin_b = f"29BBBBB{rand_b}B1Z2"

    with app.app_context():
        # User A
        user_a = User(username=f'api_user_a_{suffix_a}', email=f'api_a_{suffix_a}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a,
            legal_name='User A Enterprise Pvt Ltd',
            trade_name='User A Enterprise',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # User B (for tenant isolation tests)
        user_b = User(username=f'api_user_b_{suffix_b}', email=f'api_b_{suffix_b}@example.com')
        user_b.set_password('PasswordB1!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b,
            legal_name='User B Enterprise Pvt Ltd',
            trade_name='User B Enterprise',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # -----------------------------------------------------------------
        # Period 082024: CLEAN data for User A (reconciliation passes)
        # -----------------------------------------------------------------
        imp_clean = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='clean_082024.xlsx',
            original_file_name='clean_082024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_clean)
        _db.session.commit()

        raw_clean = RawImport(import_history_id=imp_clean.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_clean)
        _db.session.commit()

        tx_clean = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_clean.id,
            raw_import_id=raw_clean.id,
            invoice_number='INV-API-CLEAN-001',
            invoice_date=date(2024, 8, 10),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            is_deleted=False
        )
        _db.session.add(tx_clean)

        # -----------------------------------------------------------------
        # Period 092024: WARNING data for User A (missing HSN code)
        # -----------------------------------------------------------------
        imp_warn = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='092024',
            file_name='warn_092024.xlsx',
            original_file_name='warn_092024.xlsx',
            platform_name='FLIPKART',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_warn)
        _db.session.commit()

        raw_warn = RawImport(import_history_id=imp_warn.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_warn)
        _db.session.commit()

        tx_warn = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_warn.id,
            raw_import_id=raw_warn.id,
            invoice_number='INV-API-WARN-001',
            invoice_date=date(2024, 9, 15),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac=None,  # Warning condition: non-amendment missing HSN
            taxable_value=Decimal('500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            is_deleted=False
        )
        _db.session.add(tx_warn)

        # -----------------------------------------------------------------
        # Period 102024: BLOCKED data for User A (critical tax discrepancy)
        # -----------------------------------------------------------------
        imp_block = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='102024',
            file_name='block_102024.xlsx',
            original_file_name='block_102024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_block)
        _db.session.commit()

        raw_block = RawImport(import_history_id=imp_block.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_block)
        _db.session.commit()

        # Impossible tax combo: both CGST and IGST > 0
        tx_block = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_block.id,
            raw_import_id=raw_block.id,
            invoice_number='INV-API-BLOCK-001',
            invoice_date=date(2024, 10, 5),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('180.00'),  # Impossible tax combo!
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('1360.00'),
            is_deleted=False
        )
        _db.session.add(tx_block)

        # -----------------------------------------------------------------
        # User B Data in 082024
        # -----------------------------------------------------------------
        imp_b = ImportHistory(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            file_name='b_sales.xlsx',
            original_file_name='b_sales.xlsx',
            platform_name='MEESHO',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_b)
        _db.session.commit()

        raw_b = RawImport(import_history_id=imp_b.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_b)
        _db.session.commit()

        tx_b = Transaction(
            profile_id=profile_b.id,
            import_history_id=imp_b.id,
            raw_import_id=raw_b.id,
            invoice_number='INV-USER-B-001',
            invoice_date=date(2024, 8, 12),
            supply_type='B2CS',
            place_of_supply='29',
            hsn_sac='8471',
            taxable_value=Decimal('250.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('22.50'),
            sgst_amount=Decimal('22.50'),
            igst_amount=Decimal('0.00'),
            total_tax=Decimal('45.00'),
            invoice_value=Decimal('295.00'),
            is_deleted=False
        )
        _db.session.add(tx_b)
        _db.session.commit()

        return {
            'user_a_username': user_a.username,
            'user_a_password': 'PasswordA1!',
            'profile_a_id': profile_a.id,
            'user_b_username': user_b.username,
            'user_b_password': 'PasswordB1!',
            'profile_b_id': profile_b.id
        }


def _login(client, username, password):
    """Helper to authenticate client session."""
    client.get('/logout', follow_redirects=True)
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


# =========================================================================
# Tier 1: Core Feature Coverage
# =========================================================================

def test_01_api_generate_clean_status_returns_201_and_record(client, api_test_env):
    """1. POST /api/generate with CLEAN reconciliation status returns 201 Created and persists record."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    res = client.post('/api/generate', json={'return_period': '082024', 'include_hsn': True})
    assert res.status_code == 201, f"Expected 201 Created, got {res.status_code}: {res.data}"

    data = res.get_json()
    resp_data = data.get('data', data)
    assert 'generation_id' in resp_data or 'id' in resp_data
    assert resp_data.get('return_period') == '082024'

    # Database verification
    gen = GSTR1Generation.query.filter_by(
        profile_id=api_test_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None
    assert gen.generation_status == 'COMPLETED'
    assert gen.total_b2b == 1
    assert gen.validation_passed is True


def test_02_api_generate_warning_status_returns_201(client, api_test_env):
    """2. POST /api/generate with WARNING status returns 201 Created and allows generation."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    res = client.post('/api/generate', json={'return_period': '092024'})
    assert res.status_code == 201, f"Expected 201 Created, got {res.status_code}: {res.data}"

    gen = GSTR1Generation.query.filter_by(
        profile_id=api_test_env['profile_a_id'],
        return_period='092024'
    ).first()
    assert gen is not None
    assert gen.generation_status == 'COMPLETED'


def test_03_api_generate_blocked_status_returns_403(client, api_test_env):
    """3. POST /api/generate with BLOCKED status strictly returns 403 Forbidden."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    res = client.post('/api/generate', json={'return_period': '102024'})
    assert res.status_code == 403, f"Expected 403 Forbidden for blocked period, got {res.status_code}"

    data = res.get_json()
    # Verifies error description
    assert data.get('error') is True or 'error' in data or data.get('status') == 'BLOCKED'
    critical_failures = data.get('critical_failures') or data.get('data', {}).get('critical_failures')
    assert critical_failures is not None and critical_failures > 0 or 'BLOCKED' in str(data)

    # Invariant: No GSTR1Generation record is created for blocked generation
    gen = GSTR1Generation.query.filter_by(
        profile_id=api_test_env['profile_a_id'],
        return_period='102024'
    ).first()
    assert gen is None, "BLOCKED period must NEVER persist a GSTR1Generation record"


def test_04_api_generate_client_bypass_rejected(client, api_test_env):
    """4. POST /api/generate client bypass flags (force=true, ) are rejected with 403."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    # Attempt client bypass
    bypass_payload = {
        'return_period': '102024',
        'force': True,

        'bypass': True,
        'override': True,
        'skip_reconciliation': True
    }
    res = client.post('/api/generate', json=bypass_payload)
    assert res.status_code == 403, "Server gate MUST NOT trust client bypass parameters"

    # Verify no record created
    gen = GSTR1Generation.query.filter_by(
        profile_id=api_test_env['profile_a_id'],
        return_period='102024'
    ).first()
    assert gen is None


def test_05_api_get_generations_profile_isolation(client, api_test_env):
    """5. GET /api/generations lists generations scoped strictly to current profile."""
    # First, create a generation for User A
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])
    res_gen = client.post('/api/generate', json={'return_period': '082024'})
    assert res_gen.status_code == 201

    # User A requests /api/generations
    res_a = client.get('/api/generations')
    assert res_a.status_code == 200
    data_a = res_a.get_json()
    items_a = data_a.get('data', data_a)
    assert isinstance(items_a, list)
    assert len(items_a) >= 1
    assert any(g['return_period'] == '082024' for g in items_a)

    # User B logs in and requests /api/generations
    _login(client, api_test_env['user_b_username'], api_test_env['user_b_password'])
    res_b = client.get('/api/generations')
    assert res_b.status_code == 200
    data_b = res_b.get_json()
    items_b = data_b.get('data', data_b)
    # User B must not see User A's generation for 082024
    assert not any(g.get('profile_id') == api_test_env['profile_a_id'] for g in items_b)


def test_06_api_get_generations_period_filter(client, api_test_env):
    """6. GET /api/generations?return_period=MMYYYY filters generation list by return period."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])
    client.post('/api/generate', json={'return_period': '082024'})
    client.post('/api/generate', json={'return_period': '092024'})

    res_filtered = client.get('/api/generations?return_period=082024')
    assert res_filtered.status_code == 200
    data = res_filtered.get_json()
    items = data.get('data', data)
    assert all(g['return_period'] == '082024' for g in items)


def test_07_api_get_generation_by_id_details(client, api_test_env):
    """7. GET /api/generate/<id> returns complete generation details."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])
    gen_resp = client.post('/api/generate', json={'return_period': '082024'})
    assert gen_resp.status_code == 201

    gen = GSTR1Generation.query.filter_by(
        profile_id=api_test_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None

    res = client.get(f'/api/generate/{gen.id}')
    assert res.status_code == 200
    data = res.get_json()
    detail = data.get('data', data)
    assert detail['id'] == gen.id
    assert detail['return_period'] == '082024'
    assert 'stats' in detail or 'total_b2b' in detail


def test_08_api_get_generation_by_id_cross_tenant_404(client, api_test_env):
    """8. GET /api/generate/<id> cross-tenant IDOR attack returns 404 Not Found."""
    # User A generates return
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])
    client.post('/api/generate', json={'return_period': '082024'})
    gen_a = GSTR1Generation.query.filter_by(profile_id=api_test_env['profile_a_id']).first()
    assert gen_a is not None

    # User B attempts to access User A's generation ID
    _login(client, api_test_env['user_b_username'], api_test_env['user_b_password'])
    res_idor = client.get(f'/api/generate/{gen_a.id}')
    assert res_idor.status_code == 404, "Cross-tenant access must return 404 Not Found"


# =========================================================================
# Tier 2: Boundary & Corner Cases
# =========================================================================

def test_09_api_generate_missing_return_period_returns_400(client, api_test_env):
    """9. POST /api/generate with missing return_period returns 400 Bad Request."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    res = client.post('/api/generate', json={})
    assert res.status_code == 400, "Missing return_period must return 400 Bad Request"


def test_10_api_get_generation_nonexistent_id_returns_404(client, api_test_env):
    """10. GET /api/generate/<id> for non-existent ID returns 404 Not Found."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    res = client.get('/api/generate/99999999')
    assert res.status_code == 404


def test_11_unauthenticated_api_access_rejected(client):
    """11. Unauthenticated requests to API endpoints are rejected."""
    # POST /api/generate
    res1 = client.post('/api/generate', json={'return_period': '082024'})
    assert res1.status_code in (401, 302)

    # GET /api/generations
    res2 = client.get('/api/generations')
    assert res2.status_code in (401, 302)

    # GET /api/generate/1
    res3 = client.get('/api/generate/1')
    assert res3.status_code in (401, 302)


# =========================================================================
# Tier 3: Cross-Feature Combinations
# =========================================================================

def test_12_complete_api_lifecycle(client, api_test_env):
    """12. Complete API lifecycle: Reconcile -> Generate -> List -> Detail."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    # 1. Check reconciliation endpoint
    recon_res = client.get('/api/reconciliation?return_period=082024')
    assert recon_res.status_code == 200
    recon_data = recon_res.get_json()['data']
    assert recon_data['is_generation_blocked'] is False

    # 2. Trigger generation
    gen_res = client.post('/api/generate', json={'return_period': '082024', 'include_hsn': True})
    assert gen_res.status_code == 201

    # 3. Query generations list
    list_res = client.get('/api/generations')
    assert list_res.status_code == 200
    items = list_res.get_json().get('data', list_res.get_json())
    assert len(items) >= 1
    gen_id = items[0]['id']

    # 4. Fetch detail
    detail_res = client.get(f'/api/generate/{gen_id}')
    assert detail_res.status_code == 200
    assert detail_res.get_json().get('data', detail_res.get_json())['id'] == gen_id


def test_13_tenant_isolation_cross_profile_queries(client, api_test_env):
    """13. Tenant isolation: User A specifying User B's profile_id in query params is ignored."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    # User A tries to pass User B's profile_id
    res = client.get(f"/api/generations?profile_id={api_test_env['profile_b_id']}")
    assert res.status_code == 200
    items = res.get_json().get('data', res.get_json())
    # Should be scoped to User A's authorized profile only
    for item in items:
        assert item.get('profile_id') != api_test_env['profile_b_id']


# =========================================================================
# Tier 4: Real-World Scenarios
# =========================================================================

def test_14_multi_period_generation_and_retrieval(client, api_test_env):
    """14. Seller generates across multiple quarters; list and filter operate accurately."""
    _login(client, api_test_env['user_a_username'], api_test_env['user_a_password'])

    # Generate 082024 and 092024
    res1 = client.post('/api/generate', json={'return_period': '082024'})
    assert res1.status_code == 201

    res2 = client.post('/api/generate', json={'return_period': '092024'})
    assert res2.status_code == 201

    # List all
    all_res = client.get('/api/generations')
    items = all_res.get_json().get('data', all_res.get_json())
    assert len(items) >= 2

    # Filter 082024
    f_res = client.get('/api/generations?return_period=082024')
    f_items = f_res.get_json().get('data', f_res.get_json())
    assert all(i['return_period'] == '082024' for i in f_items)
