"""
Comprehensive Security and Multi-Tenancy Regression Test Suite.
Verifies CRIT-04 (Cross-tenant session bleed and IDOR elimination)
and CRIT-05 (Destructive GET profile deletion removal).
"""
import pytest
from datetime import date
from decimal import Decimal
from flask import session
from app import create_app
from app.extensions import db as _db
from app.models import User, GSTProfile, Transaction, ImportHistory, GSTR1Generation


@pytest.fixture
def multi_user_env(app):
    """Sets up two separate users with distinct profiles and transactions."""
    with app.app_context():
        # User A
        user_a = User(username='seller_a', email='seller_a@example.com')
        user_a.set_password('SecretA123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a1 = GSTProfile(
            user_id=user_a.id,
            gstin='27AAAAA0000A1Z5',
            legal_name='Seller A Retail',
            trade_name='Seller A Store',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        profile_a2 = GSTProfile(
            user_id=user_a.id,
            gstin='29AAAAA0000A1Z7',
            legal_name='Seller A South',
            trade_name='Seller A Bengaluru',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add_all([profile_a1, profile_a2])
        _db.session.commit()

        import_a = ImportHistory(
            profile_id=profile_a1.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='seller_a_sales.csv',
            original_file_name='seller_a_sales.csv',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED'
        )
        _db.session.add(import_a)
        _db.session.commit()

        tx_a = Transaction(
            profile_id=profile_a1.id,
            import_history_id=import_a.id,
            raw_import_id=1,
            invoice_number='INV-A-CONFIDENTIAL-001',
            invoice_date=date(2024, 8, 15),
            taxable_value=Decimal('50000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('9000.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('9000.00'),
            invoice_value=Decimal('59000.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=False
        )
        _db.session.add(tx_a)
        _db.session.commit()

        gen_a = GSTR1Generation(
            profile_id=profile_a1.id,
            user_id=user_a.id,
            return_period='082024',
            generation_status='COMPLETED',
            excel_file_path='/tmp/gstr1_a.xlsx',
            json_file_path='/tmp/gstr1_a.json',
            total_b2b=1,
            total_taxable_value=50000.0,
            validation_passed=True
        )
        _db.session.add(gen_a)
        _db.session.commit()

        # User B
        user_b = User(username='seller_b', email='seller_b@example.com')
        user_b.set_password('SecretB456!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin='27BBBBB0000B1Z6',
            legal_name='Seller B Electronics',
            trade_name='Seller B Store',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        yield {
            'user_a_id': user_a.id,
            'user_b_id': user_b.id,
            'profile_a1_id': profile_a1.id,
            'profile_a2_id': profile_a2.id,
            'profile_b_id': profile_b.id,
            'tx_a_id': tx_a.id,
            'gen_a_id': gen_a.id
        }

        # Cleanup for in-memory session DB
        try:
            _db.session.delete(user_a)
            _db.session.delete(user_b)
            _db.session.commit()
        except Exception:
            _db.session.rollback()


def test_logout_clears_active_profile_and_prevents_session_bleed(client, multi_user_env):
    """
    User A logs in, selects Profile A, logs out.
    User B logs in on same client.
    User B must NOT inherit User A's active profile or see Profile A in context.
    """
    p_a1 = multi_user_env['profile_a1_id']
    p_b = multi_user_env['profile_b_id']

    # 1. User A logs in and selects Profile A1
    res = client.post('/login', data={'username': 'seller_a', 'password': 'SecretA123!'}, follow_redirects=True)
    assert res.status_code == 200
    res_select = client.get(f'/profiles/{p_a1}/select', follow_redirects=True)
    assert res_select.status_code == 200

    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_a1

    # 2. User A logs out
    res_logout = client.get('/logout', follow_redirects=True)
    assert res_logout.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') is None

    # 3. User B logs in
    res_login_b = client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)
    assert res_login_b.status_code == 200

    dash_html = client.get('/dashboard').get_data(as_text=True)
    assert 'Seller A Retail' not in dash_html
    assert 'Seller A Store' not in dash_html
    assert '27AAAAA0000A1Z5' not in dash_html

    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_b


def test_user_b_cannot_access_user_a_dashboard_data(client, multi_user_env):
    """User B's dashboard must not display User A's turnover or invoices."""
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)
    res = client.get('/dashboard')
    html = res.get_data(as_text=True)

    # User A had 50,000.00 taxable and 9,000.00 tax
    assert '50,000.00' not in html
    assert '9,000.00' not in html
    assert 'Seller A' not in html


def test_user_b_cannot_access_user_a_statement_data(client, multi_user_env):
    """User B cannot fetch User A's transactions in statement table endpoints."""
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    # JSON statement table
    res_b2b = client.get('/statement/b2b')
    assert res_b2b.status_code == 200
    data = res_b2b.get_json()
    assert data['total'] == 0
    assert len(data['data']) == 0

    # CSV export
    res_export = client.get('/statement/export/b2b')
    assert res_export.status_code == 200
    csv_text = res_export.get_data(as_text=True)
    assert 'INV-A-CONFIDENTIAL-001' not in csv_text


def test_user_b_cannot_edit_user_a_transaction(client, multi_user_env):
    """User B POST to /statement/edit/<user_a_tx_id> must return 404 and leave data intact."""
    tx_a_id = multi_user_env['tx_a_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res = client.post(f'/statement/edit/{tx_a_id}', json={'taxable_value': 1.00})
    assert res.status_code == 404

    # Verify DB transaction wasn't modified
    tx = _db.session.get(Transaction, tx_a_id)
    assert tx.taxable_value == Decimal('50000.00')


def test_user_b_cannot_delete_user_a_transaction(client, multi_user_env):
    """User B POST to /statement/delete/<user_a_tx_id> must return 404 and not delete transaction."""
    tx_a_id = multi_user_env['tx_a_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res = client.post(f'/statement/delete/{tx_a_id}', json={'reason': 'Malicious delete'})
    assert res.status_code == 404

    tx = _db.session.get(Transaction, tx_a_id)
    assert tx.is_deleted is False


def test_user_b_cannot_generate_or_download_user_a_returns(client, multi_user_env):
    """User B cannot access User A's GSTR-1 generation status or download files."""
    gen_a_id = multi_user_env['gen_a_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res_status = client.get(f'/generate/status/{gen_a_id}')
    assert res_status.status_code == 404

    res_excel = client.get(f'/generate/download/excel/{gen_a_id}')
    assert res_excel.status_code == 404

    res_json = client.get(f'/generate/download/json/{gen_a_id}')
    assert res_json.status_code == 404


def test_user_b_cannot_access_user_a_via_profile_id_param(client, multi_user_env):
    """Passing User A's profile_id in query string as User B must be rejected and cleared."""
    p_a1 = multi_user_env['profile_a1_id']
    p_b = multi_user_env['profile_b_id']

    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    # 1. Attempt query param on dashboard
    res_dash = client.get(f'/dashboard?profile_id={p_a1}')
    assert res_dash.status_code == 200
    html = res_dash.get_data(as_text=True)
    assert 'Seller A Retail' not in html

    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_b

    # 2. Attempt query param on statement
    res_stmt = client.get(f'/statement/b2b?profile_id={p_a1}')
    assert res_stmt.status_code == 200
    assert res_stmt.get_json()['total'] == 0

    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_b

    # 3. Attempt query param on api
    res_api = client.get(f'/api/dashboard-stats?profile_id={p_a1}')
    assert res_api.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_b


def test_invalid_active_profile_id_falls_back_safely(client, multi_user_env):
    """If session contains a non-existent or corrupted profile ID, it safely clears and falls back."""
    p_b = multi_user_env['profile_b_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    with client.session_transaction() as sess:
        sess['active_profile_id'] = 999999

    res = client.get('/dashboard')
    assert res.status_code == 200

    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_b


def test_profile_deletion_get_rejected(client, multi_user_env):
    """CRIT-05: GET to /profiles/<id>/delete must be rejected with 405 and NOT delete profile."""
    p_b = multi_user_env['profile_b_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res = client.get(f'/profiles/{p_b}/delete')
    assert res.status_code == 405

    # Verify profile still exists
    profile = _db.session.get(GSTProfile, p_b)
    assert profile is not None


def test_cross_user_profile_post_deletion_rejected(client, multi_user_env):
    """User B POST to delete User A's profile must return 404 and NOT delete profile."""
    p_a1 = multi_user_env['profile_a1_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res = client.post(f'/profiles/{p_a1}/delete')
    assert res.status_code == 404

    profile = _db.session.get(GSTProfile, p_a1)
    assert profile is not None


def test_valid_post_profile_deletion(client, multi_user_env):
    """POST to delete own profile deletes the profile successfully."""
    p_b = multi_user_env['profile_b_id']
    client.post('/login', data={'username': 'seller_b', 'password': 'SecretB456!'}, follow_redirects=True)

    res = client.post(f'/profiles/{p_b}/delete', follow_redirects=True)
    assert res.status_code == 200

    profile = _db.session.get(GSTProfile, p_b)
    assert profile is None

    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') is None


def test_csrf_protection_on_profile_deletion():
    """Verify that when WTF_CSRF_ENABLED is True, POST without CSRF token is rejected with 400."""
    app = create_app('testing')
    app.config['WTF_CSRF_ENABLED'] = True
    app.config['SECRET_KEY'] = 'test-csrf-secret-key'

    with app.app_context():
        _db.create_all()
        user = User(username='csrf_user', email='csrf@test.com')
        user.set_password('password123')
        _db.session.add(user)
        _db.session.commit()

        prof = GSTProfile(
            user_id=user.id,
            gstin='27CSRF00000A1Z1',
            legal_name='CSRF Test Entity',
            trade_name='CSRF Entity',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add(prof)
        _db.session.commit()
        prof_id = prof.id

        client = app.test_client()
        # Login
        client.post('/login', data={'username': 'csrf_user', 'password': 'password123'}, follow_redirects=True)

        # POST without CSRF token should be rejected (400 Bad Request)
        res_no_csrf = client.post(f'/profiles/{prof_id}/delete')
        assert res_no_csrf.status_code == 400

        # Profile still exists
        assert _db.session.get(GSTProfile, prof_id) is not None


def test_single_user_multi_profile_switching(client, multi_user_env):
    """Legitimate multi-profile workflow: User A can select between Profile A1 and A2 freely."""
    p_a1 = multi_user_env['profile_a1_id']
    p_a2 = multi_user_env['profile_a2_id']

    client.post('/login', data={'username': 'seller_a', 'password': 'SecretA123!'}, follow_redirects=True)

    # Select A1
    res1 = client.get(f'/profiles/{p_a1}/select', follow_redirects=True)
    assert res1.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_a1

    # Select A2
    res2 = client.get(f'/profiles/{p_a2}/select', follow_redirects=True)
    assert res2.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_a2

    # Query param to switch temporarily
    res_param = client.get(f'/dashboard?profile_id={p_a1}')
    assert res_param.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get('active_profile_id') == p_a1
