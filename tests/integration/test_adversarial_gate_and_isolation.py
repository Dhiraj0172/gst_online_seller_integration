"""Adversarial Verification Suite: Gate Security & Multi-Tenant Isolation.

Adversarial Stress Testing for Phase 5E-4:
Track 1:
1. Gate Security Adversarial Stress:
   - Attempt to bypass the BLOCKED generation gate via:
     - POST /generate/run with force=true, force=1, enforce_gate=false, bypass=true,
       override=true, query parameters, hidden form fields.
     - POST /api/generate with force=true, enforce_gate=false, bypass=true, etc.
     - POST /generate/regenerate with bypass parameters.
   - Verify that BLOCKED status is unconditionally enforced on the server.
   - Verify that CLEAN and WARNING allow generation as required by frozen rules.
2. Tenant Isolation & IDOR Adversarial Stress:
   - User A logs in and generates returns.
   - User B attempts to access User A's generation via:
     - GET /api/generate/<id_of_A>
     - GET /generate/download/excel/<id_of_A>
     - GET /generate/download/json/<id_of_A>
     - GET /downloads (ensure User B sees zero records of User A)
     - GET /generate/status/<id_of_A>
     - POST /generate/regenerate with generation_id of User A
     - Profile ID spoofing via query params (?profile_id=id_of_A)
   - Verify that all unauthorized cross-tenant requests return 404 Not Found.
   - Verify unauthenticated requests and users without profiles.
"""
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
def adversarial_env(app, db):
    """Set up multi-user adversarial test environment.
    
    Includes:
    - User A: Maharashtra seller with CLEAN (082024), WARNING (092024), and BLOCKED (102024) periods.
    - User B: Karnataka seller attempting cross-tenant access.
    - User C: Registered user with NO GST profile.
    """
    rand_a = f"{random.randint(1000, 9999)}"
    rand_b = f"{random.randint(1000, 9999)}"
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    suffix_c = uuid.uuid4().hex[:6]

    gstin_a = f"27AAAAA{rand_a}A1Z5"
    gstin_b = f"29BBBBB{rand_b}B1Z2"

    with app.app_context():
        # User A
        user_a = User(username=f'adv_user_a_{suffix_a}', email=f'adv_a_{suffix_a}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a,
            legal_name='User A Alpha Industries Pvt Ltd',
            trade_name='Alpha Industries',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # User B (Adversary tenant)
        user_b = User(username=f'adv_user_b_{suffix_b}', email=f'adv_b_{suffix_b}@example.com')
        user_b.set_password('PasswordB1!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b,
            legal_name='User B Beta Retail Pvt Ltd',
            trade_name='Beta Retail',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # User C (User with no profile)
        user_c = User(username=f'adv_user_c_{suffix_c}', email=f'adv_c_{suffix_c}@example.com')
        user_c.set_password('PasswordC1!')
        _db.session.add(user_c)
        _db.session.commit()

        # -------------------------------------------------------------
        # Period 082024: CLEAN data for User A
        # -------------------------------------------------------------
        imp_clean = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='clean_a_082024.xlsx',
            original_file_name='clean_a_082024.xlsx',
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
            invoice_number='INV-ADV-CLEAN-01',
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

        # -------------------------------------------------------------
        # Period 092024: WARNING data for User A (Missing HSN code)
        # -------------------------------------------------------------
        imp_warn = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='092024',
            file_name='warn_a_092024.xlsx',
            original_file_name='warn_a_092024.xlsx',
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
            invoice_number='INV-ADV-WARN-01',
            invoice_date=date(2024, 9, 15),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac=None,  # Triggers missing HSN warning
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

        # -------------------------------------------------------------
        # Period 102024: BLOCKED data for User A (Critical tax discrepancy)
        # -------------------------------------------------------------
        imp_block = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='102024',
            file_name='block_a_102024.xlsx',
            original_file_name='block_a_102024.xlsx',
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

        # Critical failure: impossible tax combination (both CGST and IGST > 0)
        tx_block = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_block.id,
            raw_import_id=raw_block.id,
            invoice_number='INV-ADV-BLOCK-01',
            invoice_date=date(2024, 10, 5),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('180.00'),  # Illegal combination -> Critical failure
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('1360.00'),
            is_deleted=False
        )
        _db.session.add(tx_block)

        # -------------------------------------------------------------
        # User B: Data in 082024 for User B's own profile
        # -------------------------------------------------------------
        imp_b = ImportHistory(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            file_name='b_sales_082024.xlsx',
            original_file_name='b_sales_082024.xlsx',
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
            invoice_date=date(2024, 8, 20),
            supply_type='B2CS',
            place_of_supply='29',
            hsn_sac='8471',
            taxable_value=Decimal('300.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('27.00'),
            sgst_amount=Decimal('27.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('54.00'),
            invoice_value=Decimal('354.00'),
            is_deleted=False
        )
        _db.session.add(tx_b)
        _db.session.commit()

        return {
            'user_a_username': user_a.username,
            'user_a_password': 'PasswordA1!',
            'user_a_id': user_a.id,
            'profile_a_id': profile_a.id,
            'profile_a_gstin': gstin_a,
            'user_b_username': user_b.username,
            'user_b_password': 'PasswordB1!',
            'user_b_id': user_b.id,
            'profile_b_id': profile_b.id,
            'profile_b_gstin': gstin_b,
            'user_c_username': user_c.username,
            'user_c_password': 'PasswordC1!',
            'user_c_id': user_c.id
        }


def _login(client, username, password):
    """Authenticate client session."""
    client.get('/logout', follow_redirects=True)
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


# =============================================================================
# Track 1: Gate Security Adversarial Stress Tests
# =============================================================================

def test_adv_01_post_run_blocked_bypass_form_fields(client, adversarial_env):
    """Adversarial Test 1.1: Attempt to bypass BLOCKED gate via POST /generate/run with bypass parameters.
    
    Tries force=true, force=1, enforce_gate=false, bypass=true, override=true, admin=true, etc.
    Server MUST unconditionally block and redirect to validation errors; NO GSTR1Generation created.
    """
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    bypass_param_sets = [
        {'return_period': '102024', 'force': 'true'},
        {'return_period': '102024', 'force': '1'},
        {'return_period': '102024', 'force': 'True'},
        {'return_period': '102024', 'bypass': 'true'},
        {'return_period': '102024', 'override': 'true'},
        {'return_period': '102024', 'skip_reconciliation': 'true'},
        {'return_period': '102024', 'admin': '1'},
        {'return_period': '102024', 'force': 'true', 'bypass': 'true'}
    ]

    for params in bypass_param_sets:
        res = client.post('/generate/run', data=params, follow_redirects=False)
        assert res.status_code == 302, f"Failed for params: {params} - Expected redirect 302, got {res.status_code}"
        assert '/statement/validation-errors' in res.location, f"Failed for params: {params} - Redirect target was {res.location}"

        # Follow redirect and verify flash message
        res_followed = client.get(res.location)
        assert b"blocked" in res_followed.data.lower() or b"critical" in res_followed.data.lower()

        # Database invariant: ZERO GSTR1Generation records must exist for 102024
        gen = GSTR1Generation.query.filter_by(
            profile_id=adversarial_env['profile_a_id'],
            return_period='102024'
        ).first()
        assert gen is None, f"Security Violation! GSTR1Generation was created despite BLOCKED status with params: {params}"


def test_adv_02_post_run_blocked_bypass_query_params(client, adversarial_env):
    """Adversarial Test 1.2: Attempt to bypass BLOCKED gate via URL query parameters on POST /generate/run.
    
    Tries POST /generate/run?force=true&enforce_gate=false with body return_period=102024.
    """
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    urls_to_test = [
        '/generate/run?force=true',
        '/generate/run?force=1',
        '/generate/run?bypass=true',
        '/generate/run?force=true&bypass=1&override=true',
        '/generate/run?return_period=102024&force=true'
    ]

    for url in urls_to_test:
        res = client.post(url, data={'return_period': '102024'}, follow_redirects=False)
        assert res.status_code == 302, f"Expected 302 redirect for {url}, got {res.status_code}"
        assert '/statement/validation-errors' in res.location

        gen = GSTR1Generation.query.filter_by(
            profile_id=adversarial_env['profile_a_id'],
            return_period='102024'
        ).first()
        assert gen is None, f"Security Violation! Generation occurred via query param bypass: {url}"


def test_adv_03_post_api_generate_blocked_bypass_payloads(client, adversarial_env):
    """Adversarial Test 1.3: Attempt to bypass BLOCKED gate via POST /api/generate JSON payloads.
    
    Tries force=True/true/1, enforce_gate=False/false/0, bypass=True, override=True, skip_reconciliation=True.
    Server MUST return HTTP 403 Forbidden with BLOCKED status and critical failures count.
    """
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    bypass_payloads = [
        {'return_period': '102024', 'force': True},
        {'return_period': '102024', 'force': 'true'},
        {'return_period': '102024', 'force': 1},
        {'return_period': '102024', 'bypass': True},
        {'return_period': '102024', 'override': True},
        {'return_period': '102024', 'skip_reconciliation': True},
        {
            'return_period': '102024',
            'force': True,
            'bypass': True,
            'override': True,
            'role': 'admin'
        }
    ]

    for payload in bypass_payloads:
        res = client.post('/api/generate', json=payload)
        assert res.status_code == 403, f"Bypass succeeded with payload: {payload}, got status {res.status_code}: {res.data}"
        data = res.get_json()
        assert 'BLOCKED' in str(data), f"Expected 'BLOCKED' in response for payload: {payload}"

        # DB invariant
        gen = GSTR1Generation.query.filter_by(
            profile_id=adversarial_env['profile_a_id'],
            return_period='102024'
        ).first()
        assert gen is None, f"Security Violation! Record persisted for BLOCKED period with payload {payload}"


def test_adv_04_post_api_generate_blocked_bypass_query_params_and_headers(client, adversarial_env):
    """Adversarial Test 1.4: Attempt to bypass BLOCKED gate via query params and HTTP headers on /api/generate."""
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    headers_list = [
        {'X-Bypass-Gate': 'true'},
        {'X-Force': 'true'},
        {'X-Admin': 'true'},
        {'X-Enforce-Gate': 'false'}
    ]

    for headers in headers_list:
        res = client.post(
            '/api/generate?force=true&bypass=true',
            json={'return_period': '102024'},
            headers=headers
        )
        assert res.status_code == 403, f"Headers {headers} bypassed server gate!"

    gen = GSTR1Generation.query.filter_by(
        profile_id=adversarial_env['profile_a_id'],
        return_period='102024'
    ).first()
    assert gen is None


def test_adv_05_regenerate_blocked_bypass_attempts(client, adversarial_env):
    """Adversarial Test 1.5: Attempt to bypass BLOCKED gate via POST /generate/regenerate."""
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    bypass_forms = [
        {'return_period': '102024', 'force': 'true'},
        {'return_period': '102024', 'bypass': 'true'}
    ]

    for form in bypass_forms:
        res = client.post('/generate/regenerate', data=form, follow_redirects=False)
        assert res.status_code == 302
        assert '/statement/validation-errors' in res.location

    gen = GSTR1Generation.query.filter_by(
        profile_id=adversarial_env['profile_a_id'],
        return_period='102024'
    ).first()
    assert gen is None


def test_adv_06_clean_period_allows_generation_in_web_and_api(client, adversarial_env):
    """Adversarial Test 1.6: Verify CLEAN reconciliation period (082024) allows generation unconditionally."""
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    # 1. Web route
    res_web = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=False)
    assert res_web.status_code == 302
    assert '/generate' in res_web.location

    gen_web = GSTR1Generation.query.filter_by(
        profile_id=adversarial_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen_web is not None
    assert gen_web.generation_status == 'COMPLETED'
    assert gen_web.total_b2b == 1

    # 2. API route (returns 201)
    res_api = client.post('/api/generate', json={'return_period': '082024'})
    assert res_api.status_code == 201
    data = res_api.get_json()
    assert data.get('error') is False
    assert data.get('data', {}).get('return_period') == '082024'


def test_adv_07_warning_period_allows_generation_in_web_and_api(client, adversarial_env):
    """Adversarial Test 1.7: Verify WARNING reconciliation period (092024) allows generation with warning notice."""
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])

    # 1. Web route
    res_web = client.post('/generate/run', data={'return_period': '092024'}, follow_redirects=False)
    assert res_web.status_code == 302
    assert '/generate' in res_web.location

    # Follow redirect and verify warning flash
    res_web_followed = client.get(res_web.location)
    assert b"warning" in res_web_followed.data.lower() or b"pre-filing review recommended" in res_web_followed.data.lower()

    gen_web = GSTR1Generation.query.filter_by(
        profile_id=adversarial_env['profile_a_id'],
        return_period='092024'
    ).first()
    assert gen_web is not None
    assert gen_web.generation_status == 'COMPLETED'
    assert gen_web.total_b2cs == 1

    # 2. API route
    res_api = client.post('/api/generate', json={'return_period': '092024'})
    assert res_api.status_code == 201


# =============================================================================
# Track 2: Tenant Isolation & IDOR Adversarial Stress Tests
# =============================================================================

@pytest.fixture
def populated_user_a_generation(client, adversarial_env):
    """Generate GSTR-1 for User A in period 082024 and return generation record ID."""
    _login(client, adversarial_env['user_a_username'], adversarial_env['user_a_password'])
    res = client.post('/api/generate', json={'return_period': '082024'})
    assert res.status_code == 201
    gen = GSTR1Generation.query.filter_by(
        profile_id=adversarial_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None
    return gen.id


def test_adv_08_idor_api_get_generation_by_id(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.1: User B attempts IDOR via GET /api/generate/<id_of_A>.
    
    Must return 404 Not Found under all circumstances (with and without spoofed query params).
    """
    gen_a_id = populated_user_a_generation
    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    # Direct IDOR attempt
    res = client.get(f'/api/generate/{gen_a_id}')
    assert res.status_code == 404, f"IDOR Vulnerability! User B accessed User A generation {gen_a_id}: {res.data}"

    # Profile spoofing query parameter
    res_spoofed = client.get(f'/api/generate/{gen_a_id}?profile_id={adversarial_env["profile_a_id"]}')
    assert res_spoofed.status_code == 404, "IDOR Vulnerability! Profile ID query param allowed cross-tenant access!"


def test_adv_09_idor_download_excel(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.2: User B attempts IDOR via GET /generate/download/excel/<id_of_A>.
    
    Must return 404 Not Found. No Excel bytes of User A must leak.
    """
    gen_a_id = populated_user_a_generation
    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    res = client.get(f'/generate/download/excel/{gen_a_id}')
    assert res.status_code == 404, f"IDOR Vulnerability! User B downloaded User A Excel return {gen_a_id}"

    # With spoofed profile_id
    res_spoofed = client.get(f'/generate/download/excel/{gen_a_id}?profile_id={adversarial_env["profile_a_id"]}')
    assert res_spoofed.status_code == 404, "IDOR Vulnerability! Spoofed profile_id allowed Excel download!"


def test_adv_10_idor_download_json(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.3: User B attempts IDOR via GET /generate/download/json/<id_of_A>.
    
    Must return 404 Not Found. No GSTN JSON payload of User A must leak.
    """
    gen_a_id = populated_user_a_generation
    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    res = client.get(f'/generate/download/json/{gen_a_id}')
    assert res.status_code == 404, f"IDOR Vulnerability! User B downloaded User A JSON return {gen_a_id}"

    # With spoofed profile_id
    res_spoofed = client.get(f'/generate/download/json/{gen_a_id}?profile_id={adversarial_env["profile_a_id"]}')
    assert res_spoofed.status_code == 404, "IDOR Vulnerability! Spoofed profile_id allowed JSON download!"


def test_adv_11_downloads_view_zero_leakage(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.4: Ensure GET /downloads contains zero records, GSTIN, or links of User A when viewed by User B."""
    gen_a_id = populated_user_a_generation
    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    res = client.get('/downloads')
    assert res.status_code == 200
    html = res.data.decode('utf-8')

    # Adversarial verification: None of User A's artifacts or identifiers must be present in HTML
    assert f"/generate/download/excel/{gen_a_id}" not in html
    assert f"/generate/download/json/{gen_a_id}" not in html
    assert adversarial_env['profile_a_gstin'] not in html
    assert "User A Alpha Industries" not in html

    # Spoofed profile_id query parameter attempt
    res_spoofed = client.get(f'/downloads?profile_id={adversarial_env["profile_a_id"]}')
    assert res_spoofed.status_code == 200
    html_spoofed = res_spoofed.data.decode('utf-8')
    assert f"/generate/download/excel/{gen_a_id}" not in html_spoofed
    assert adversarial_env['profile_a_gstin'] not in html_spoofed


def test_adv_12_idor_generate_status(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.5: User B attempts IDOR via GET /generate/status/<id_of_A>.
    
    Must return 404 Not Found.
    """
    gen_a_id = populated_user_a_generation
    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    res = client.get(f'/generate/status/{gen_a_id}')
    assert res.status_code == 404, "IDOR Vulnerability on /generate/status/<id>"

    res_spoofed = client.get(f'/generate/status/{gen_a_id}?profile_id={adversarial_env["profile_a_id"]}')
    assert res_spoofed.status_code == 404


def test_adv_13_idor_regenerate_overwrite_prevention(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.6: User B attempts to overwrite User A's generation record via /generate/regenerate.
    
    User B calls POST /generate/regenerate with existing_id=gen_a_id.
    User A's record MUST NOT be updated, modified, or overwritten by User B.
    """
    gen_a_id = populated_user_a_generation
    gen_a_before = GSTR1Generation.query.get(gen_a_id)
    original_profile_id = gen_a_before.profile_id
    original_user_id = gen_a_before.user_id
    original_taxable = gen_a_before.total_taxable_value

    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    # User B attempts to regenerate passing User A's generation id
    client.post('/generate/regenerate', data={'id': str(gen_a_id), 'return_period': '082024'})

    # Verify User A's record was completely untouched
    _db.session.expire_all()
    gen_a_after = GSTR1Generation.query.get(gen_a_id)
    assert gen_a_after.profile_id == original_profile_id == adversarial_env['profile_a_id']
    assert gen_a_after.user_id == original_user_id == adversarial_env['user_a_id']
    assert gen_a_after.total_taxable_value == original_taxable


def test_adv_14_api_generations_list_isolation_under_spoofed_params(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.7: User B queries /api/generations with spoofed profile_id.
    
    Must return 0 records belonging to User A.
    """
    _login(client, adversarial_env['user_b_username'], adversarial_env['user_b_password'])

    # Standard query
    res = client.get('/api/generations')
    assert res.status_code == 200
    data = res.get_json().get('data', [])
    assert not any(g.get('profile_id') == adversarial_env['profile_a_id'] for g in data)

    # Spoofed profile_id query
    res_spoofed = client.get(f'/api/generations?profile_id={adversarial_env["profile_a_id"]}')
    assert res_spoofed.status_code == 200
    data_spoofed = res_spoofed.get_json().get('data', [])
    assert not any(g.get('profile_id') == adversarial_env['profile_a_id'] for g in data_spoofed)


def test_adv_15_unauthenticated_requests_rejection(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.8: Unauthenticated requests to all generation endpoints must be rejected."""
    gen_a_id = populated_user_a_generation
    client.get('/logout', follow_redirects=True)

    endpoints = [
        ('POST', '/generate/run', {'data': {'return_period': '082024'}}),
        ('POST', '/generate/regenerate', {'data': {'return_period': '082024'}}),
        ('POST', '/api/generate', {'json': {'return_period': '082024'}}),
        ('GET', '/api/generations', {}),
        ('GET', f'/api/generate/{gen_a_id}', {}),
        ('GET', f'/generate/download/excel/{gen_a_id}', {}),
        ('GET', f'/generate/download/json/{gen_a_id}', {}),
        ('GET', f'/generate/status/{gen_a_id}', {}),
        ('GET', '/downloads', {}),
    ]

    for method, path, kwargs in endpoints:
        if method == 'POST':
            res = client.post(path, **kwargs)
        else:
            res = client.get(path, **kwargs)

        # Must either redirect to login (302) or return 401 Unauthorized
        assert res.status_code in (302, 401), f"Unauthenticated request to {path} returned {res.status_code}"
        if res.status_code == 302:
            assert '/login' in res.location


def test_adv_16_user_without_profile_handling(client, adversarial_env, populated_user_a_generation):
    """Adversarial Test 2.9: User C (registered user with no GST profile) must receive 404 or redirect, never leak data or 500 error."""
    gen_a_id = populated_user_a_generation
    _login(client, adversarial_env['user_c_username'], adversarial_env['user_c_password'])

    # Attempt to access User A's generation
    res_api = client.get(f'/api/generate/{gen_a_id}')
    assert res_api.status_code == 404

    res_excel = client.get(f'/generate/download/excel/{gen_a_id}')
    assert res_excel.status_code == 404

    res_json = client.get(f'/generate/download/json/{gen_a_id}')
    assert res_json.status_code == 404

    res_dl = client.get('/downloads')
    assert res_dl.status_code == 200
    assert "No Generated Returns Available" in res_dl.data.decode('utf-8')
