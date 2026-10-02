"""Phase 5E-6 Integration Tests: REST API Suite Completion & Scoping.

Covers all 11 completed REST API endpoints:
1. GET /api/dashboard-stats
2. GET /api/platforms
3. GET /api/b2b
4. GET /api/b2c
5. GET /api/cdnr
6. GET /api/nil
7. GET /api/hsn-b2b
8. GET /api/hsn-b2c
9. GET /api/ecom
10. GET /api/import-history
11. GET /api/tcs-reconciliation

Requirements tested:
- Real repository data returned
- Empty-state behavior
- Current-user scoping & multi-tenant isolation
- Active-profile scoping
- Return-period isolation
- Foreign profile_id / IDOR resistance
- Pagination & deterministic ordering
- JSON structure and schema compliance
- Unauthenticated rejection
- Adversarial parameter resistance (force, bypass, override, , skip_reconciliation)
- Dashboard period scoping & /errors redirect
"""
import uuid
import json
from datetime import date, datetime
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, GSTR1Generation, TCSReconciliation
from app.adapters.registry import _ADAPTER_REGISTRY


_counter = 0

def _get_unique_suffix():
    global _counter
    _counter += 1
    return f"{_counter:04d}_{uuid.uuid4().hex[:6]}"


@pytest.fixture
def phase5e6_test_env(app, db):
    """Set up two tenants with multi-period transactions and imports."""
    suffix_a = _get_unique_suffix()
    suffix_b = _get_unique_suffix()

    with app.app_context():
        # Tenant A
        user_a = User(username=f'p5e6_user_a_{suffix_a}', email=f'p5e6_a_{suffix_a}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=f"27{uuid.uuid4().hex[:10].upper()}1Z5",
            legal_name=f'Tenant A Retail {suffix_a}',
            trade_name=f'Tenant A {suffix_a}',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B
        user_b = User(username=f'p5e6_user_b_{suffix_b}', email=f'p5e6_b_{suffix_b}@example.com')
        user_b.set_password('PasswordB1!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=f"29{uuid.uuid4().hex[:10].upper()}1Z2",
            legal_name=f'Tenant B Retail {suffix_b}',
            trade_name=f'Tenant B {suffix_b}',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Imports for Tenant A: Period 082024
        ih_a_08 = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name=f'sales_082024_{suffix_a}.xlsx',
            original_file_name=f'sales_082024_{suffix_a}.xlsx',
            platform_name='Amazon',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=6,
            success_rows=6
        )
        # Imports for Tenant A: Period 092024
        ih_a_09 = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='092024',
            file_name=f'sales_092024_{suffix_a}.xlsx',
            original_file_name=f'sales_092024_{suffix_a}.xlsx',
            platform_name='Flipkart',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add_all([ih_a_08, ih_a_09])
        _db.session.commit()

        ri_08 = RawImport(import_history_id=ih_a_08.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        ri_09 = RawImport(import_history_id=ih_a_09.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add_all([ri_08, ri_09])
        _db.session.commit()

        # Period 082024 Transactions for Tenant A
        # 1. B2B
        tx_b2b = Transaction(
            profile_id=profile_a.id,
            import_history_id=ih_a_08.id,
            raw_import_id=ri_08.id,
            invoice_number=f'INV-B2B-01-{suffix_a}',
            invoice_date=date(2024, 8, 10),
            customer_name='B2B Customer Corp',
            customer_gstin='27ABCDE1234F1Z5',
            place_of_supply='27',
            taxable_value=Decimal('10000.00'),
            cgst_rate=Decimal('9.00'),
            cgst_amount=Decimal('900.00'),
            sgst_rate=Decimal('9.00'),
            sgst_amount=Decimal('900.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('1800.00'),
            invoice_value=Decimal('11800.00'),
            tax_rate=Decimal('18.00'),
            hsn_sac='84713010',
            supply_type='B2B',
            ecommerce_gstin='27ECOM1234F1Z9',
            is_deleted=False
        )
        # 2. B2CS
        tx_b2c = Transaction(
            profile_id=profile_a.id,
            import_history_id=ih_a_08.id,
            raw_import_id=ri_08.id,
            invoice_number=f'INV-B2C-01-{suffix_a}',
            invoice_date=date(2024, 8, 12),
            customer_name='Retail Buyer',
            customer_gstin='',
            place_of_supply='29',
            taxable_value=Decimal('2000.00'),
            igst_rate=Decimal('18.00'),
            igst_amount=Decimal('360.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('2360.00'),
            tax_rate=Decimal('18.00'),
            hsn_sac='61091000',
            supply_type='B2CS',
            is_deleted=False
        )
        # 3. CDNR
        tx_cdnr = Transaction(
            profile_id=profile_a.id,
            import_history_id=ih_a_08.id,
            raw_import_id=ri_08.id,
            invoice_number=f'CN-01-{suffix_a}',
            invoice_date=date(2024, 8, 15),
            customer_name='B2B Customer Corp',
            customer_gstin='27ABCDE1234F1Z5',
            place_of_supply='27',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            tax_rate=Decimal('18.00'),
            supply_type='CDNR',
            note_type='CREDIT',
            is_deleted=False
        )
        # 4. NIL
        tx_nil = Transaction(
            profile_id=profile_a.id,
            import_history_id=ih_a_08.id,
            raw_import_id=ri_08.id,
            invoice_number=f'INV-NIL-01-{suffix_a}',
            invoice_date=date(2024, 8, 18),
            customer_name='Exempt Buyer',
            place_of_supply='27',
            taxable_value=Decimal('500.00'),
            total_tax=Decimal('0.00'),
            invoice_value=Decimal('500.00'),
            tax_rate=Decimal('0.00'),
            supply_type='NIL',
            nil_rated_flag=True,
            is_deleted=False
        )
        # Period 092024 Transaction for Tenant A
        tx_09 = Transaction(
            profile_id=profile_a.id,
            import_history_id=ih_a_09.id,
            raw_import_id=ri_09.id,
            invoice_number=f'INV-09-01-{suffix_a}',
            invoice_date=date(2024, 9, 5),
            customer_name='Sept Customer',
            place_of_supply='27',
            taxable_value=Decimal('7000.00'),
            cgst_amount=Decimal('630.00'),
            sgst_amount=Decimal('630.00'),
            total_tax=Decimal('1260.00'),
            invoice_value=Decimal('8260.00'),
            tax_rate=Decimal('18.00'),
            hsn_sac='84713010',
            supply_type='B2B',
            is_deleted=False
        )
        _db.session.add_all([tx_b2b, tx_b2c, tx_cdnr, tx_nil, tx_09])
        _db.session.commit()

        # TCS Reconciliation records for Tenant A
        tcs_a = TCSReconciliation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            import_history_id=ih_a_08.id,
            ecommerce_gstin='27ECOM1234F1Z9',
            state_code='27',
            state_name='Maharashtra',
            our_net_taxable_value=Decimal('10000.00'),
            our_calculated_tcs=Decimal('100.00'),
            portal_taxable_value=Decimal('10000.00'),
            portal_tcs=Decimal('100.00'),
            difference_taxable=Decimal('0.00'),
            difference_tcs=Decimal('0.00'),
            match_status='MATCHED'
        )
        _db.session.add(tcs_a)
        _db.session.commit()

        # Generation record for Period 082024
        gen_a = GSTR1Generation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            financial_year='2024-25',
            generation_status='COMPLETED',
            total_b2b=1,
            total_b2cs=1,
            total_cdnr=1,
            total_nil=1,
            total_taxable_value=Decimal('13500.00'),
            total_tax=Decimal('2340.00')
        )
        _db.session.add(gen_a)
        _db.session.commit()

        # User B: Imports and transaction in 082024 (Tenant B)
        ih_b = ImportHistory(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            file_name=f'sales_b_{suffix_b}.xlsx',
            original_file_name=f'sales_b_{suffix_b}.xlsx',
            platform_name='Meesho',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(ih_b)
        _db.session.commit()

        ri_b = RawImport(import_history_id=ih_b.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(ri_b)
        _db.session.commit()

        tx_b = Transaction(
            profile_id=profile_b.id,
            import_history_id=ih_b.id,
            raw_import_id=ri_b.id,
            invoice_number=f'INV-USER-B-{suffix_b}',
            invoice_date=date(2024, 8, 20),
            customer_name='Tenant B Customer',
            place_of_supply='29',
            taxable_value=Decimal('99999.00'),
            total_tax=Decimal('17999.82'),
            invoice_value=Decimal('117998.82'),
            tax_rate=Decimal('18.00'),
            supply_type='B2B',
            is_deleted=False
        )
        _db.session.add(tx_b)
        _db.session.commit()

        u_a_name = user_a.username
        p_a_id = profile_a.id
        u_b_name = user_b.username
        p_b_id = profile_b.id

    return {
        'user_a_username': u_a_name,
        'user_a_password': 'PasswordA1!',
        'profile_a_id': p_a_id,
        'user_b_username': u_b_name,
        'user_b_password': 'PasswordB1!',
        'profile_b_id': p_b_id,
    }


def _login(client, username, password):
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


# ==============================================================================
# 1. Unauthenticated Access Protection
# ==============================================================================

def test_01_unauthenticated_api_endpoints_rejected(client):
    """Unauthenticated requests to all completed REST API endpoints are rejected."""
    endpoints = [
        '/api/dashboard-stats',
        '/api/platforms',
        '/api/b2b',
        '/api/b2c',
        '/api/cdnr',
        '/api/nil',
        '/api/hsn-b2b',
        '/api/hsn-b2c',
        '/api/ecom',
        '/api/import-history',
        '/api/tcs-reconciliation',
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code in (302, 401), f"Endpoint {ep} allowed unauthenticated access with {res.status_code}"


# ==============================================================================
# 2. Dynamic Platforms API
# ==============================================================================

def test_02_platforms_endpoint_dynamic_registry(client, phase5e6_test_env):
    """GET /api/platforms derives list dynamically from adapter registry."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/platforms')
    assert res.status_code == 200
    body = res.get_json()
    assert body['error'] is False
    platforms = body['data']
    assert isinstance(platforms, list)
    # Must have all 16 registered platforms, not merely 3 hardcoded
    assert len(platforms) == len(_ADAPTER_REGISTRY)
    assert len(platforms) >= 16
    for expected in ['Amazon', 'Flipkart', 'Meesho', 'Myntra', 'GSTR1_Govt']:
        assert expected in platforms
    # Check deterministic sorted ordering
    assert platforms == sorted(platforms)


# ==============================================================================
# 3. Dashboard Stats API
# ==============================================================================

def test_03_dashboard_stats_computed_and_period_scoped(client, phase5e6_test_env):
    """GET /api/dashboard-stats returns authentic metrics scoped to return period."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])

    # Period 082024 has 4 transactions for Tenant A
    res_08 = client.get('/api/dashboard-stats?return_period=082024')
    assert res_08.status_code == 200
    data_08 = res_08.get_json()['data']
    assert data_08['return_period'] == '082024'
    assert data_08['total_invoices'] == 4
    assert data_08['gstr1_status'] == 'Generated'
    assert data_08['breakdown']['b2b'] == 1
    assert data_08['breakdown']['b2c'] == 1
    assert data_08['breakdown']['cdnr'] == 1
    assert data_08['breakdown']['nil'] == 1
    assert data_08['taxable_value_raw'] == 13500.0

    # Period 092024 has 1 transaction for Tenant A
    res_09 = client.get('/api/dashboard-stats?return_period=092024')
    assert res_09.status_code == 200
    data_09 = res_09.get_json()['data']
    assert data_09['return_period'] == '092024'
    assert data_09['total_invoices'] == 1
    assert data_09['breakdown']['b2b'] == 1
    assert data_09['breakdown']['b2c'] == 0
    assert data_09['taxable_value_raw'] == 7000.0
    assert data_09['gstr1_status'] == 'Pending'


def test_04_dashboard_stats_empty_state(client, app, db):
    """GET /api/dashboard-stats gracefully handles users with no data."""
    suffix = _get_unique_suffix()
    with app.app_context():
        u = User(username=f'empty_{suffix}', email=f'empty_{suffix}@example.com')
        u.set_password('Secret123!')
        _db.session.add(u)
        _db.session.commit()

        p = GSTProfile(
            user_id=u.id,
            gstin=f"27EMPTY{suffix[:4].upper()}1Z1",
            legal_name='Empty Business',
            trade_name='Empty',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(p)
        _db.session.commit()

    _login(client, f'empty_{suffix}', 'Secret123!')
    res = client.get('/api/dashboard-stats?return_period=082024')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert data['total_invoices'] == 0
    assert data['gstr1_status'] == 'Pending'
    assert data['taxable_value_raw'] == 0.0


# ==============================================================================
# 4. Section APIs: B2B, B2C, CDNR, NIL, HSN-B2B, HSN-B2C, ECOM
# ==============================================================================

def test_05_section_b2b_live_data_and_schema(client, phase5e6_test_env):
    """GET /api/b2b returns real live data with valid schema."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/b2b?return_period=082024')
    assert res.status_code == 200
    body = res.get_json()
    assert body['error'] is False
    items = body['data']
    assert isinstance(items, list)
    assert len(items) == 1
    row = items[0]
    assert 'INV-B2B-01' in row['invoice_number']
    assert row['taxable_value'] == 10000.0
    assert row['supply_type'] == 'B2B'
    assert row['customer_gstin'] == '27ABCDE1234F1Z5'
    assert body['total'] == 1


def test_06_section_b2c_live_data(client, phase5e6_test_env):
    """GET /api/b2c returns live B2CS/B2CL data."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/b2c?return_period=082024')
    assert res.status_code == 200
    body = res.get_json()
    items = body['data']
    assert len(items) == 1
    assert 'INV-B2C-01' in items[0]['invoice_number']
    assert items[0]['supply_type'] == 'B2CS'
    assert items[0]['taxable_value'] == 2000.0


def test_07_section_cdnr_live_data(client, phase5e6_test_env):
    """GET /api/cdnr returns live credit/debit notes."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/cdnr?return_period=082024')
    assert res.status_code == 200
    body = res.get_json()
    items = body['data']
    assert len(items) == 1
    assert 'CN-01' in items[0]['invoice_number']
    assert items[0]['taxable_value'] == 1000.0


def test_08_section_nil_live_data(client, phase5e6_test_env):
    """GET /api/nil returns live exempt/nil rated data."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/nil?return_period=082024')
    assert res.status_code == 200
    body = res.get_json()
    items = body['data']
    assert len(items) == 1
    assert 'INV-NIL-01' in items[0]['invoice_number']
    assert items[0]['taxable_value'] == 500.0


def test_09_section_hsn_b2b_and_b2c(client, phase5e6_test_env):
    """GET /api/hsn-b2b and /api/hsn-b2c return distinct supply type records."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])

    # HSN B2B
    res_b2b = client.get('/api/hsn-b2b?return_period=082024')
    assert res_b2b.status_code == 200
    items_b2b = res_b2b.get_json()['data']
    assert len(items_b2b) == 1
    assert items_b2b[0]['hsn_sac'] == '84713010'
    assert items_b2b[0]['supply_type'] == 'B2B'

    # HSN B2C
    res_b2c = client.get('/api/hsn-b2c?return_period=082024')
    assert res_b2c.status_code == 200
    items_b2c = res_b2c.get_json()['data']
    assert len(items_b2c) == 1
    assert items_b2c[0]['hsn_sac'] == '61091000'
    assert items_b2c[0]['supply_type'] == 'B2CS'


def test_10_section_ecom_live_data(client, phase5e6_test_env):
    """GET /api/ecom returns transactions with ecommerce_gstin."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/ecom?return_period=082024')
    assert res.status_code == 200
    items = res.get_json()['data']
    assert len(items) == 1
    assert items[0]['ecommerce_gstin'] == '27ECOM1234F1Z9'


# ==============================================================================
# 5. Import History & TCS Reconciliation APIs
# ==============================================================================

def test_11_import_history_endpoint(client, phase5e6_test_env):
    """GET /api/import-history returns real ImportHistory records."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/import-history')
    assert res.status_code == 200
    body = res.get_json()
    assert body['error'] is False
    items = body['data']
    assert len(items) == 2
    # Verify ordering (latest first)
    assert items[0]['return_period'] == '092024'
    assert items[1]['return_period'] == '082024'
    assert items[0]['total_rows'] == 1
    assert items[1]['total_rows'] == 6


def test_12_tcs_reconciliation_endpoint(client, phase5e6_test_env):
    """GET /api/tcs-reconciliation returns real TCS records."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/tcs-reconciliation?return_period=082024')
    assert res.status_code == 200
    items = res.get_json()['data']
    assert len(items) == 1
    assert items[0]['ecommerce_gstin'] == '27ECOM1234F1Z9'
    assert items[0]['state_code'] == '27'
    assert items[0]['match_status'] == 'MATCHED'
    assert items[0]['our_net_taxable_value'] == 10000.0


# ==============================================================================
# 6. Pagination & Deterministic Ordering
# ==============================================================================

def test_13_pagination_and_deterministic_ordering(client, phase5e6_test_env):
    """Pagination parameters (page, per_page) slice results predictably."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])

    # Import history has 2 records: page 1 with per_page 1
    res_p1 = client.get('/api/import-history?page=1&per_page=1')
    assert res_p1.status_code == 200
    body_p1 = res_p1.get_json()
    assert len(body_p1['data']) == 1
    assert body_p1['page'] == 1
    assert body_p1['total'] == 2
    first_id = body_p1['data'][0]['id']

    # Page 2
    res_p2 = client.get('/api/import-history?page=2&per_page=1')
    assert res_p2.status_code == 200
    body_p2 = res_p2.get_json()
    assert len(body_p2['data']) == 1
    assert body_p2['page'] == 2
    second_id = body_p2['data'][0]['id']

    assert first_id != second_id


# ==============================================================================
# 7. Multi-Tenant Isolation & IDOR Resistance
# ==============================================================================

def test_14_multi_tenant_cross_access_strictly_isolated(client, phase5e6_test_env):
    """User B cannot see User A's transactions, imports, or TCS records."""
    _login(client, phase5e6_test_env['user_b_username'], phase5e6_test_env['user_b_password'])

    # Section B2B: User B should only see their own 1 transaction (amount 99999.0)
    res_b2b = client.get('/api/b2b?return_period=082024')
    assert res_b2b.status_code == 200
    items_b2b = res_b2b.get_json()['data']
    assert len(items_b2b) == 1
    assert items_b2b[0]['taxable_value'] == 99999.0
    assert not any(t['customer_name'] == 'B2B Customer Corp' for t in items_b2b)

    # Import history: User B should only see their 1 meesho import
    res_ih = client.get('/api/import-history')
    assert res_ih.status_code == 200
    ih_items = res_ih.get_json()['data']
    assert len(ih_items) == 1
    assert ih_items[0]['platform_name'] == 'Meesho'

    # TCS reconciliation: User B has no TCS records
    res_tcs = client.get('/api/tcs-reconciliation')
    assert res_tcs.status_code == 200
    assert len(res_tcs.get_json()['data']) == 0


def test_15_adversarial_foreign_profile_spoofing(client, phase5e6_test_env):
    """User B specifying User A's profile_id is safely ignored and never leaks."""
    _login(client, phase5e6_test_env['user_b_username'], phase5e6_test_env['user_b_password'])
    spoofed_profile = phase5e6_test_env['profile_a_id']

    endpoints = [
        f'/api/dashboard-stats?profile_id={spoofed_profile}&return_period=082024',
        f'/api/b2b?profile_id={spoofed_profile}&return_period=082024',
        f'/api/b2c?profile_id={spoofed_profile}&return_period=082024',
        f'/api/cdnr?profile_id={spoofed_profile}&return_period=082024',
        f'/api/nil?profile_id={spoofed_profile}&return_period=082024',
        f'/api/hsn-b2b?profile_id={spoofed_profile}&return_period=082024',
        f'/api/hsn-b2c?profile_id={spoofed_profile}&return_period=082024',
        f'/api/ecom?profile_id={spoofed_profile}&return_period=082024',
        f'/api/import-history?profile_id={spoofed_profile}',
        f'/api/tcs-reconciliation?profile_id={spoofed_profile}',
    ]

    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200
        body = res.get_json()
        data = body.get('data')
        # Ensure User A's data is never returned to User B
        if isinstance(data, list):
            for row in data:
                assert row.get('profile_id') != spoofed_profile
                if 'customer_name' in row:
                    assert row['customer_name'] != 'B2B Customer Corp'
        elif isinstance(data, dict):
            assert data.get('profile_id') != spoofed_profile


def test_16_adversarial_bypass_flags_ineffective(client, phase5e6_test_env):
    """Forbidden bypass flags (force, bypass, override, ) do not weaken APIs."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/api/dashboard-stats?return_period=082024&force=true&bypass=true&override=true&skip_reconciliation=true')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert data['total_invoices'] == 4
    assert data['return_period'] == '082024'


# ==============================================================================
# 8. Dashboard Web Route Period Scoping & /errors Redirect
# ==============================================================================

def test_17_dashboard_web_route_respects_period_scoping(client, phase5e6_test_env):
    """GET /dashboard reflects active return period in KPIs and surfaces it in HTML."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])

    # Period 082024
    res_08 = client.get('/dashboard?return_period=082024')
    assert res_08.status_code == 200
    html_08 = res_08.data.decode('utf-8')
    assert '082024' in html_08

    # Period 092024
    res_09 = client.get('/dashboard?return_period=092024')
    assert res_09.status_code == 200
    html_09 = res_09.data.decode('utf-8')
    assert '092024' in html_09


def test_18_errors_route_redirects_to_validation_errors(client, phase5e6_test_env):
    """GET /errors redirects cleanly to statement.validation_errors with query args."""
    _login(client, phase5e6_test_env['user_a_username'], phase5e6_test_env['user_a_password'])
    res = client.get('/errors?return_period=082024&severity=ERROR', follow_redirects=False)
    assert res.status_code == 302
    assert '/statement/validation-errors' in res.location
    assert 'return_period=082024' in res.location
    assert 'severity=ERROR' in res.location
