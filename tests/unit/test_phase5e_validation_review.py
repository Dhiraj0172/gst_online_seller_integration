"""Phase 5E-3 Validation Error Inspection & Pre-Filing Review Tests.

Verifies:
1. Validation endpoint returns real errors from database data.
2. Clean data returns no blocking errors (PASS status, is_generation_blocked=False).
3. Blocking errors are correctly identified (BLOCKING severity).
4. Warnings are distinguishable from blocking errors (WARNING severity).
5. Validation is period-scoped (period A does not leak into period B).
6. Validation is profile-scoped (profile A does not leak into profile B).
7. Deleted records (is_deleted=True) are strictly excluded from validation and review.
8. Another user's transaction cannot appear in validation results (cross-tenant isolation / IDOR).
9. Reconciliation review uses real Phase 5E-1 results (all 10 reconciliation categories present).
10. Blocking review state is accurately reflected (overall status is BLOCKED, is_generation_blocked=True).
11. Warning-only review remains non-blocking (overall status is WARNING, is_generation_blocked=False).
12. Empty review state works correctly (0 transactions produces clean PASS status).
13. Search / filter preserves period, profile, and deleted-row isolation.
14. Affected transaction references are correct (invoice number, ID, customer GSTIN).
15. No fake / static validation data is used.
16. Generation gate prevents GSTR-1 generation when reconciliation is BLOCKED, but allows force bypass.
17. HTML pre-filing review page (/statement/validation-errors) renders real backend-driven review.
"""
from datetime import date
from decimal import Decimal
import uuid
import pytest

from app.extensions import db as _db
from app.models import AuditLog, GSTProfile, ImportHistory, RawImport, Transaction, User
from app.services.validation_service import get_pre_filing_review, get_validation_issues


@pytest.fixture
def review_env(app, db):
    """Set up multi-user, multi-profile, multi-period environment for validation tests."""
    suffix = uuid.uuid4().hex[:6]
    gstin_a = f'27{uuid.uuid4().hex[:10].upper()}1Z5'
    gstin_b = f'29{uuid.uuid4().hex[:10].upper()}1Z6'

    with app.app_context():
        # User A
        user_a = User(username=f'val_user_a_{suffix}', email=f'val_a_{suffix}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)

        # User B
        user_b = User(username=f'val_user_b_{suffix}', email=f'val_b_{suffix}@example.com')
        user_b.set_password('PasswordB1!')
        _db.session.add(user_b)
        _db.session.commit()

        # Profile A1 (User A) - Maharashtra
        p_a1 = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a,
            legal_name='Alpha Enterprises',
            trade_name='Alpha Store',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly',
        )

        # Profile B1 (User B) - Karnataka
        p_b1 = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b,
            legal_name='Beta Electronics',
            trade_name='Beta Store',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='monthly',
        )
        _db.session.add_all([p_a1, p_b1])
        _db.session.commit()

        # ----------------------------------------------------
        # Period 082024: Clean Data for User A (Profile A1)
        # ----------------------------------------------------
        imp_a_clean = ImportHistory(
            profile_id=p_a1.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='clean_082024.xlsx',
            original_file_name='clean_082024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
        )
        _db.session.add(imp_a_clean)
        _db.session.commit()

        raw_a_clean = RawImport(import_history_id=imp_a_clean.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_a_clean)
        _db.session.commit()

        tx_clean = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_clean.id,
            raw_import_id=raw_a_clean.id,
            invoice_number='INV-CLEAN-001',
            invoice_date=date(2024, 8, 10),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            customer_name='Clean Buyer',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_rate=Decimal('9.00'),
            cgst_amount=Decimal('90.00'),
            sgst_rate=Decimal('9.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            place_of_supply='27',
            hsn_sac='8471',
            source_platform='AMAZON',
            is_deleted=False,
            row_fingerprint='clean_fp_001',
        )
        _db.session.add(tx_clean)

        # ----------------------------------------------------
        # Period 092024: Warning-Only Data for User A (Missing HSN on non-amendment)
        # ----------------------------------------------------
        imp_a_warn = ImportHistory(
            profile_id=p_a1.id,
            user_id=user_a.id,
            return_period='092024',
            file_name='warn_092024.xlsx',
            original_file_name='warn_092024.xlsx',
            platform_name='FLIPKART',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
        )
        _db.session.add(imp_a_warn)
        _db.session.commit()

        raw_a_warn = RawImport(import_history_id=imp_a_warn.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_a_warn)
        _db.session.commit()

        tx_warn = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_warn.id,
            raw_import_id=raw_a_warn.id,
            invoice_number='INV-WARN-001',
            invoice_date=date(2024, 9, 15),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac=None,  # Missing HSN on non-amendment: should be non-blocking WARNING
            amendment_flag=False,
            taxable_value=Decimal('500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            source_platform='FLIPKART',
            is_deleted=False,
            row_fingerprint='warn_fp_001',
        )
        _db.session.add(tx_warn)

        # ----------------------------------------------------
        # Period 102024: Blocking Error Data for User A
        # Includes: impossible tax combo, invalid POS, unclassified supply, duplicates, deleted row
        # ----------------------------------------------------
        imp_a_err = ImportHistory(
            profile_id=p_a1.id,
            user_id=user_a.id,
            return_period='102024',
            file_name='err_102024.xlsx',
            original_file_name='err_102024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=4,
            success_rows=4,
        )
        _db.session.add(imp_a_err)
        _db.session.commit()

        raw_a_err1 = RawImport(import_history_id=imp_a_err.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw_a_err2 = RawImport(import_history_id=imp_a_err.id, sheet_name='Sheet1', row_number=2, status='SUCCESS')
        raw_a_err3 = RawImport(import_history_id=imp_a_err.id, sheet_name='Sheet1', row_number=3, status='SUCCESS')
        raw_a_err4 = RawImport(import_history_id=imp_a_err.id, sheet_name='Sheet1', row_number=4, status='SUCCESS')
        raw_a_err5 = RawImport(import_history_id=imp_a_err.id, sheet_name='Sheet1', row_number=5, status='SUCCESS')
        _db.session.add_all([raw_a_err1, raw_a_err2, raw_a_err3, raw_a_err4, raw_a_err5])
        _db.session.commit()

        # Error Tx 1: Impossible tax combo (both CGST and IGST > 0)
        tx_err_tax = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_err.id,
            raw_import_id=raw_a_err1.id,
            invoice_number='INV-ERR-TAX-001',
            invoice_date=date(2024, 10, 5),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('180.00'),  # Impossible: both CGST and IGST
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('1360.00'),
            source_platform='AMAZON',
            is_deleted=False,
            row_fingerprint='err_tax_fp_001',
        )

        # Error Tx 2: Invalid Place of Supply ('99')
        tx_err_pos = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_err.id,
            raw_import_id=raw_a_err2.id,
            invoice_number='INV-ERR-POS-002',
            invoice_date=date(2024, 10, 8),
            supply_type='B2CS',
            place_of_supply='99',  # Invalid state code
            hsn_sac='8471',
            taxable_value=Decimal('500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            igst_amount=Decimal('90.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            source_platform='AMAZON',
            is_deleted=False,
            row_fingerprint='err_pos_fp_002',
        )

        # Error Tx 3: Unclassified supply type ('INVALID_TYPE')
        tx_err_unclass = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_err.id,
            raw_import_id=raw_a_err3.id,
            invoice_number='INV-ERR-UNCLASS-003',
            invoice_date=date(2024, 10, 12),
            supply_type='INVALID_TYPE',  # Unclassified supply
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('300.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('27.00'),
            sgst_amount=Decimal('27.00'),
            igst_amount=Decimal('0.00'),
            total_tax=Decimal('54.00'),
            invoice_value=Decimal('354.00'),
            source_platform='AMAZON',
            is_deleted=False,
            row_fingerprint='err_unclass_fp_003',
        )

        # Error Tx 4 & 5: Duplicate fingerprint pair
        tx_err_dup1 = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_err.id,
            raw_import_id=raw_a_err4.id,
            invoice_number='INV-ERR-DUP-004A',
            invoice_date=date(2024, 10, 15),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('200.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('18.00'),
            sgst_amount=Decimal('18.00'),
            total_tax=Decimal('36.00'),
            invoice_value=Decimal('236.00'),
            source_platform='AMAZON',
            is_deleted=False,
            row_fingerprint='shared_duplicate_fp_999',
        )
        tx_err_dup2 = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_err.id,
            raw_import_id=raw_a_err5.id,
            invoice_number='INV-ERR-DUP-004B',
            invoice_date=date(2024, 10, 15),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('200.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('18.00'),
            sgst_amount=Decimal('18.00'),
            total_tax=Decimal('36.00'),
            invoice_value=Decimal('236.00'),
            source_platform='AMAZON',
            is_deleted=False,
            row_fingerprint='shared_duplicate_fp_999',
        )

        # Deleted Transaction with blocking error in period 102024 (MUST BE EXCLUDED)
        tx_deleted = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_err.id,
            raw_import_id=raw_a_err1.id,
            invoice_number='INV-DELETED-999',
            invoice_date=date(2024, 10, 1),
            supply_type='INVALID_DELETED',
            place_of_supply='99',
            taxable_value=Decimal('9999.00'),
            is_deleted=True,  # Soft deleted
            row_fingerprint='deleted_fp_999',
        )

        # ----------------------------------------------------
        # User B: Data for Beta Enterprises (Period 102024)
        # Must NEVER appear in User A results
        # ----------------------------------------------------
        imp_b_10 = ImportHistory(
            profile_id=p_b1.id,
            user_id=user_b.id,
            return_period='102024',
            file_name='b_102024.xlsx',
            original_file_name='b_102024.xlsx',
            platform_name='MEESHO',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
        )
        _db.session.add(imp_b_10)
        _db.session.commit()

        raw_b_10 = RawImport(import_history_id=imp_b_10.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_b_10)
        _db.session.commit()

        tx_user_b = Transaction(
            profile_id=p_b1.id,
            import_history_id=imp_b_10.id,
            raw_import_id=raw_b_10.id,
            invoice_number='INV-USER-B-SECRET-777',
            invoice_date=date(2024, 10, 20),
            supply_type='B2CS',
            place_of_supply='29',
            hsn_sac='8471',
            taxable_value=Decimal('777.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('69.93'),
            sgst_amount=Decimal('69.93'),
            total_tax=Decimal('139.86'),
            invoice_value=Decimal('916.86'),
            source_platform='MEESHO',
            is_deleted=False,
            row_fingerprint='user_b_fp_777',
        )

        _db.session.add_all([
            tx_err_tax, tx_err_pos, tx_err_unclass,
            tx_err_dup1, tx_err_dup2, tx_deleted,
            tx_user_b,
        ])
        _db.session.commit()

        env_data = {
            'user_a_username': user_a.username,
            'user_b_username': user_b.username,
            'user_a_id': user_a.id,
            'user_b_id': user_b.id,
            'p_a1_id': p_a1.id,
            'p_b1_id': p_b1.id,
            'tx_clean_id': tx_clean.id,
            'tx_warn_id': tx_warn.id,
            'tx_err_tax_id': tx_err_tax.id,
            'tx_err_pos_id': tx_err_pos.id,
            'tx_err_unclass_id': tx_err_unclass.id,
            'tx_err_dup1_id': tx_err_dup1.id,
            'tx_err_dup2_id': tx_err_dup2.id,
            'tx_deleted_id': tx_deleted.id,
            'tx_user_b_id': tx_user_b.id,
        }

    yield env_data

    # Teardown
    with app.app_context():
        try:
            _db.session.query(AuditLog).delete()
            _db.session.query(Transaction).delete()
            _db.session.query(RawImport).delete()
            _db.session.query(ImportHistory).delete()
            _db.session.query(GSTProfile).delete()
            _db.session.query(User).delete()
            _db.session.commit()
        except Exception:
            _db.session.rollback()


# -------------------------------------------------------------------------
# Test Cases 1 - 17
# -------------------------------------------------------------------------

def test_01_validation_endpoint_returns_real_errors(client, review_env):
    """1. Validation endpoint returns real errors derived from real database transactions."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=102024')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert data['status'] == 'BLOCKED'
    assert data['blocking_count'] > 0
    assert len(data['issues']) > 0

    codes = [i['code'] for i in data['issues']]
    assert 'IMPOSSIBLE_TAX_COMBO' in codes
    assert 'INVALID_POS' in codes
    assert 'UNCLASSIFIED_SUPPLY' in codes
    assert 'DUPLICATE_TRANSACTION' in codes


def test_02_clean_data_returns_no_blocking_errors(client, review_env):
    """2. Clean data returns no blocking errors (PASS status, is_generation_blocked=False)."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=082024')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert data['status'] == 'PASS'
    assert data['is_generation_blocked'] is False
    assert data['blocking_count'] == 0
    assert data['warning_count'] == 0
    assert len(data['issues']) == 0


def test_03_blocking_errors_are_correctly_identified(client, review_env):
    """3. Blocking errors are correctly identified with severity=BLOCKING."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=102024&severity=BLOCKING')
    assert res.status_code == 200
    data = res.get_json()['data']
    issues = data['issues']
    assert len(issues) > 0
    for issue in issues:
        assert issue['severity'] == 'BLOCKING'


def test_04_warnings_are_distinguishable_from_blocking_errors(client, review_env):
    """4. Warnings are clearly distinguishable from blocking errors and have severity=WARNING."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=092024')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert data['status'] == 'WARNING'
    assert data['is_generation_blocked'] is False
    assert data['blocking_count'] == 0
    assert data['warning_count'] >= 1

    hsn_warnings = [i for i in data['issues'] if i['code'] == 'MISSING_HSN']
    assert len(hsn_warnings) == 1
    assert hsn_warnings[0]['severity'] == 'WARNING'


def test_05_validation_is_period_scoped(client, review_env):
    """5. Validation is period-scoped: errors in period 102024 never appear in period 082024."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Query period 082024
    res_08 = client.get('/api/validation-errors?return_period=082024')
    data_08 = res_08.get_json()['data']
    assert data_08['status'] == 'PASS'
    assert len(data_08['issues']) == 0

    # Query period 102024
    res_10 = client.get('/api/validation-errors?return_period=102024')
    data_10 = res_10.get_json()['data']
    assert data_10['status'] == 'BLOCKED'
    assert len(data_10['issues']) > 0


def test_06_validation_is_profile_scoped(client, review_env):
    """6. Validation is strictly profile-scoped to the active user's authorized profile."""
    # User B logs in
    client.post('/login', data={'username': review_env['user_b_username'], 'password': 'PasswordB1!'}, follow_redirects=True)
    res_b = client.get('/api/validation-errors?return_period=102024')
    assert res_b.status_code == 200
    data_b = res_b.get_json()['data']
    # User B has only 1 clean transaction in 102024
    assert data_b['status'] == 'PASS'
    assert data_b['blocking_count'] == 0

    # User B must not see any issues from User A
    invs_b = [i['invoice_number'] for i in data_b['issues']]
    assert 'INV-ERR-TAX-001' not in invs_b
    assert 'INV-ERR-POS-002' not in invs_b


def test_07_deleted_records_are_excluded(client, review_env):
    """7. Soft-deleted records (is_deleted=True) are strictly excluded from validation issues."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=102024')
    assert res.status_code == 200
    data = res.get_json()['data']
    invs = [i['invoice_number'] for i in data['issues']]
    assert 'INV-DELETED-999' not in invs
    for issue in data['issues']:
        assert issue['transaction_id'] != review_env['tx_deleted_id']


def test_08_cross_tenant_isolation_and_idor_prevention(client, review_env):
    """8. Another user's transaction cannot appear in validation results even if manipulated via URL."""
    # User A logs in
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Attempt IDOR: User A requests profile_id belonging to User B
    res_idor = client.get(f"/api/validation-errors?profile_id={review_env['p_b1_id']}&return_period=102024")
    assert res_idor.status_code == 200
    data_idor = res_idor.get_json()['data']
    # Server-side auth must keep query scoped to User A's authorized profile
    invs = [i['invoice_number'] for i in data_idor['issues']]
    assert 'INV-USER-B-SECRET-777' not in invs


def test_09_reconciliation_review_uses_real_phase5e1_results(client, review_env):
    """9. Reconciliation review uses real Phase 5E-1 results (all 10 categories surfaced)."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/reconciliation?return_period=102024')
    assert res.status_code == 200
    report = res.get_json()['data']

    assert 'status' in report
    assert 'is_generation_blocked' in report
    assert 'checks' in report
    assert len(report['checks']) == 10

    check_names = {c['check_name'] for c in report['checks']}
    expected_categories = {
        'import_counts',
        'unclassified',
        'duplicates',
        'tax_totals',
        'b2b_totals',
        'b2c_totals',
        'hsn_reconciliation',
        'cdnr_totals',
        'nil_totals',
        'state_totals',
    }
    assert expected_categories.issubset(check_names)


def test_10_blocking_review_state_is_accurately_reflected(client, review_env):
    """10. Blocking review state is accurately reflected (status=BLOCKED, is_generation_blocked=True)."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=102024')
    data = res.get_json()['data']
    assert data['status'] == 'BLOCKED'
    assert data['is_generation_blocked'] is True
    assert data['blocking_count'] > 0


def test_11_warning_only_review_remains_non_blocking(client, review_env):
    """11. Warning-only review remains non-blocking (status=WARNING, is_generation_blocked=False)."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=092024')
    data = res.get_json()['data']
    assert data['status'] == 'WARNING'
    assert data['is_generation_blocked'] is False
    assert data['blocking_count'] == 0
    assert data['warning_count'] > 0


def test_12_empty_review_state_works(client, review_env):
    """12. Empty review state for a period with no data returns clean PASS."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=122099')
    assert res.status_code == 200
    data = res.get_json()['data']
    assert data['status'] == 'PASS'
    assert data['is_generation_blocked'] is False
    assert data['blocking_count'] == 0
    assert data['warning_count'] == 0
    assert len(data['issues']) == 0


def test_13_search_filter_remains_isolated(client, review_env):
    """13. Search filter filters within the active period and profile without leaking outside."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Search for specific error invoice in period 102024
    res = client.get('/api/validation-errors?return_period=102024&search=INV-ERR-POS')
    assert res.status_code == 200
    data = res.get_json()['data']
    issues = data['issues']
    assert len(issues) == 1
    assert issues[0]['invoice_number'] == 'INV-ERR-POS-002'
    assert issues[0]['code'] == 'INVALID_POS'

    # Search for User B's secret invoice in period 102024 must return 0 results
    res_leak = client.get('/api/validation-errors?return_period=102024&search=USER-B-SECRET')
    assert res_leak.status_code == 200
    data_leak = res_leak.get_json()['data']
    assert len(data_leak['issues']) == 0


def test_14_affected_transaction_references_are_correct(client, review_env):
    """14. Affected transaction references are correct (invoice_number, transaction_id, GSTIN)."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/api/validation-errors?return_period=102024&search=INV-ERR-TAX-001')
    data = res.get_json()['data']
    assert len(data['issues']) >= 1
    tax_issue = data['issues'][0]
    assert tax_issue['transaction_id'] == review_env['tx_err_tax_id']
    assert tax_issue['invoice_number'] == 'INV-ERR-TAX-001'
    assert tax_issue['customer_gstin'] == '27GHIJK5678L1Z9'
    assert tax_issue['return_period'] == '102024'
    assert tax_issue['section'] == 'B2B'


def test_15_no_fake_static_validation_data(client, review_env):
    """15. No fake or static validation data is returned; output varies directly with DB state."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Clean period has 0 issues
    res_clean = client.get('/api/validation-errors?return_period=082024').get_json()['data']
    # Warning period has warning issues
    res_warn = client.get('/api/validation-errors?return_period=092024').get_json()['data']
    # Error period has blocking issues
    res_err = client.get('/api/validation-errors?return_period=102024').get_json()['data']

    assert res_clean['status'] != res_err['status']
    assert res_warn['status'] != res_err['status']
    assert res_clean['issues'] != res_err['issues']


def test_16_blocked_reconciliation_prevents_normal_generation(client, review_env):
    """1. blocked reconciliation prevents normal generation."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_blocked = client.post('/generate/run', data={'return_period': '102024'}, follow_redirects=False)
    assert res_blocked.status_code == 302
    assert '/statement/validation-errors' in res_blocked.location

def test_17_warning_only_reconciliation_allows_generation(client, review_env):
    """2. warning-only reconciliation allows generation."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_warn = client.post('/generate/run', data={'return_period': '092024'}, follow_redirects=False)
    assert res_warn.status_code == 302
    assert '/generate' in res_warn.location
    assert '/statement/validation-errors' not in res_warn.location

def test_18_clean_reconciliation_allows_generation(client, review_env):
    """3. clean reconciliation allows generation."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_clean = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=False)
    assert res_clean.status_code == 302
    assert '/generate' in res_clean.location
    assert '/statement/validation-errors' not in res_clean.location

def test_19_post_force_true_cannot_bypass_blocked_status(client, review_env):
    """4. POST force=true cannot bypass BLOCKED status."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_blocked = client.post('/generate/run', data={'return_period': '102024', 'force': 'true'}, follow_redirects=False)
    assert res_blocked.status_code == 302
    assert '/statement/validation-errors' in res_blocked.location

def test_20_post_enforce_gate_false_cannot_bypass_blocked_status(client, review_env):
    """5. POST enforce_gate=false cannot bypass BLOCKED status."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_blocked = client.post('/generate/run', data={'return_period': '102024', 'enforce_gate': 'false'}, follow_redirects=False)
    assert res_blocked.status_code == 302
    assert '/statement/validation-errors' in res_blocked.location

def test_21_get_query_parameter_force_true_cannot_bypass_blocked_status(client, review_env):
    """6. GET query parameter force=true cannot bypass BLOCKED status."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_blocked = client.get('/generate/run?return_period=102024&force=true', follow_redirects=False)
    assert res_blocked.status_code == 302
    assert '/statement/validation-errors' in res_blocked.location

def test_22_get_query_parameter_enforce_gate_false_cannot_bypass_blocked_status(client, review_env):
    """7. GET query parameter enforce_gate=false cannot bypass BLOCKED status."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res_blocked = client.get('/generate/run?return_period=102024&enforce_gate=false', follow_redirects=False)
    assert res_blocked.status_code == 302
    assert '/statement/validation-errors' in res_blocked.location

def test_23_hidden_form_field_manipulation_cannot_bypass_server_side_gate(client, review_env):
    """8. hidden/form field manipulation cannot bypass the server-side gate."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Simulate someone adding hidden fields
    res_blocked = client.post('/generate/run', data={'return_period': '102024', 'enforce_gate': '0', 'force': '1', 'bypass': 'true'}, follow_redirects=False)
    assert res_blocked.status_code == 302
    assert '/statement/validation-errors' in res_blocked.location

def test_24_cross_profile_generation_remains_isolated(client, review_env):
    """9. cross-profile generation remains isolated."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Attempting to generate for User B's profile when logged in as User A
    # The generation uses `get_active_profile()` which uses `current_user.id`. 
    # If we pass profile_id in args, it validates it against current_user.
    res = client.post(f"/generate/run?profile_id={review_env['p_b1_id']}&return_period=102024", follow_redirects=False)
    # Since p_b1 is not owned by user_a, it shouldn't allow it. It defaults to the first profile owned by User A.
    # Therefore, it's actually running generation for User A's profile A1 for period 102024 which is BLOCKED.
    assert res.status_code == 302
    assert '/statement/validation-errors' in res.location

def test_25_generation_remains_functional_after_security_hardening(client, review_env):
    """10. generation remains functional after the security hardening."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    # Run clean data generation
    res_clean = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    assert res_clean.status_code == 200
    html = res_clean.data.decode('utf-8')
    assert 'GSTR-1 Excel and JSON generated successfully!' in html

def test_26_html_validation_errors_page_renders_real_review(client, review_env):
    """17. HTML pre-filing review page (/statement/validation-errors) renders real backend review."""
    client.post('/login', data={'username': review_env['user_a_username'], 'password': 'PasswordA1!'}, follow_redirects=True)
    res = client.get('/statement/validation-errors?return_period=102024')
    assert res.status_code == 200
    html = res.data.decode('utf-8')

    # Status banner and counts
    assert 'Pre-Filing Review: Generation Blocked' in html
    assert 'Blocking Issues' in html
    assert 'Reconciliation Integrity Checks' in html

    # Real error records from DB
    assert 'INV-ERR-TAX-001' in html
    assert 'INV-ERR-POS-002' in html
    assert 'IMPOSSIBLE_TAX_COMBO' in html
    assert 'INVALID_POS' in html

    # Excluded deleted row must not appear
    assert 'INV-DELETED-999' not in html

    # Other user's transaction must not appear
    assert 'INV-USER-B-SECRET-777' not in html

