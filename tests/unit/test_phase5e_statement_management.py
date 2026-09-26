"""Phase 5E-1 Statement Management & Period Isolation Tests.

Verifies:
1. Period A records appear in period A
2. Period A records do NOT appear in period B
3. Profile A records do NOT appear in profile B (cross-tenant security)
4. Deleted rows are excluded from statement queries
5. Pagination preserves isolation
6. Search preserves isolation
7. Empty or unimported period returns empty result
8. Invalid / unauthorized profile ID cannot leak records (IDOR prevention)
9. Transaction edit validates profile ownership and is_deleted status, creates audit trail
10. Transaction delete performs soft-delete, creates audit trail, excludes from statement
11. CSV export strictly preserves profile and return-period isolation
"""
import uuid
from datetime import date
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import AuditLog, GSTProfile, ImportHistory, RawImport, Transaction, User


@pytest.fixture
def statement_env(app, db):
    """Set up two users with multiple profiles and distinct return periods."""
    suffix = uuid.uuid4().hex[:6]
    gstin_a1 = f'27{uuid.uuid4().hex[:10].upper()}1Z5'
    gstin_b1 = f'29{uuid.uuid4().hex[:10].upper()}1Z6'

    with app.app_context():
        # User A
        user_a = User(username=f'seller_a_{suffix}', email=f'seller_a_{suffix}@example.com')
        user_a.set_password('SecretA123!')
        _db.session.add(user_a)

        # User B
        user_b = User(username=f'seller_b_{suffix}', email=f'seller_b_{suffix}@example.com')
        user_b.set_password('SecretB456!')
        _db.session.add(user_b)
        _db.session.commit()

        # Profile A1 (User A)
        p_a1 = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a1,
            legal_name='Seller A Retail',
            trade_name='Store A',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly',
        )
        # Profile B1 (User B)
        p_b1 = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b1,
            legal_name='Seller B Electronics',
            trade_name='Store B',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='monthly',
        )
        _db.session.add_all([p_a1, p_b1])
        _db.session.commit()

        # Import A in Period 082024
        imp_a_08 = ImportHistory(
            profile_id=p_a1.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='a_082024.xlsx',
            original_file_name='a_082024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=2,
            success_rows=2,
        )
        # Import A in Period 092024
        imp_a_09 = ImportHistory(
            profile_id=p_a1.id,
            user_id=user_a.id,
            return_period='092024',
            file_name='a_092024.xlsx',
            original_file_name='a_092024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
        )
        # Import B in Period 082024
        imp_b_08 = ImportHistory(
            profile_id=p_b1.id,
            user_id=user_b.id,
            return_period='082024',
            file_name='b_082024.xlsx',
            original_file_name='b_082024.xlsx',
            platform_name='FLIPKART',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
        )
        _db.session.add_all([imp_a_08, imp_a_09, imp_b_08])
        _db.session.commit()

        raw_a_08_1 = RawImport(import_history_id=imp_a_08.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw_a_08_2 = RawImport(import_history_id=imp_a_08.id, sheet_name='Sheet1', row_number=2, status='SUCCESS')
        raw_a_09_1 = RawImport(import_history_id=imp_a_09.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw_b_08_1 = RawImport(import_history_id=imp_b_08.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add_all([raw_a_08_1, raw_a_08_2, raw_a_09_1, raw_b_08_1])
        _db.session.commit()

        # Transactions for User A - Period 082024
        tx_a_08_1 = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_08.id,
            raw_import_id=raw_a_08_1.id,
            invoice_number='INV-A-08-001',
            invoice_date=date(2024, 8, 5),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_rate=Decimal('9.00'),
            cgst_amount=Decimal('90.00'),
            sgst_rate=Decimal('9.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        tx_a_08_2 = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_08.id,
            raw_import_id=raw_a_08_2.id,
            invoice_number='INV-A-08-002',
            invoice_date=date(2024, 8, 12),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            customer_name='Beta Corp',
            taxable_value=Decimal('2000.00'),
            tax_rate=Decimal('18.00'),
            cgst_rate=Decimal('9.00'),
            cgst_amount=Decimal('180.00'),
            sgst_rate=Decimal('9.00'),
            sgst_amount=Decimal('180.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('2360.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )

        # Transactions for User A - Period 092024
        tx_a_09_1 = Transaction(
            profile_id=p_a1.id,
            import_history_id=imp_a_09.id,
            raw_import_id=raw_a_09_1.id,
            invoice_number='INV-A-09-001',
            invoice_date=date(2024, 9, 10),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('5000.00'),
            tax_rate=Decimal('18.00'),
            cgst_rate=Decimal('9.00'),
            cgst_amount=Decimal('450.00'),
            sgst_rate=Decimal('9.00'),
            sgst_amount=Decimal('450.00'),
            total_tax=Decimal('900.00'),
            invoice_value=Decimal('5900.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )

        # Transactions for User B - Period 082024 (Confidential)
        tx_b_08_1 = Transaction(
            profile_id=p_b1.id,
            import_history_id=imp_b_08.id,
            raw_import_id=raw_b_08_1.id,
            invoice_number='INV-B-SECRET-001',
            invoice_date=date(2024, 8, 20),
            supply_type='B2B',
            customer_gstin='29ZZZZZ9999Z1Z1',
            customer_name='User B Secret Client',
            taxable_value=Decimal('99999.00'),
            tax_rate=Decimal('18.00'),
            igst_rate=Decimal('18.00'),
            igst_amount=Decimal('17999.82'),
            total_tax=Decimal('17999.82'),
            invoice_value=Decimal('117998.82'),
            place_of_supply='29',
            hsn_sac='8471',
            is_deleted=False,
        )
        _db.session.add_all([tx_a_08_1, tx_a_08_2, tx_a_09_1, tx_b_08_1])
        _db.session.commit()

        yield {
            'user_a_username': user_a.username,
            'user_b_username': user_b.username,
            'user_a_id': user_a.id,
            'user_b_id': user_b.id,
            'p_a1_id': p_a1.id,
            'p_b1_id': p_b1.id,
            'imp_a_08_id': imp_a_08.id,
            'imp_a_09_id': imp_a_09.id,
            'imp_b_08_id': imp_b_08.id,
            'tx_a_08_1_id': tx_a_08_1.id,
            'tx_a_08_2_id': tx_a_08_2.id,
            'tx_a_09_1_id': tx_a_09_1.id,
            'tx_b_08_1_id': tx_b_08_1.id,
        }

        # Teardown
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


# 1. Period A records appear in period A
def test_statement_period_a_records_appear_in_period_a(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    res = client.get('/statement/b2b?return_period=082024')
    assert res.status_code == 200
    data = res.get_json()
    assert data['total'] == 2
    invoices = [row['invoice_number'] for row in data['data']]
    assert 'INV-A-08-001' in invoices
    assert 'INV-A-08-002' in invoices
    assert 'INV-A-09-001' not in invoices


# 2. Period A records do NOT appear in period B
def test_statement_period_a_records_do_not_appear_in_period_b(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    res = client.get('/statement/b2b?return_period=092024')
    assert res.status_code == 200
    data = res.get_json()
    assert data['total'] == 1
    invoices = [row['invoice_number'] for row in data['data']]
    assert 'INV-A-09-001' in invoices
    assert 'INV-A-08-001' not in invoices
    assert 'INV-A-08-002' not in invoices


# 3. Profile A records do NOT appear in Profile B (Cross-tenant isolation)
def test_statement_profile_a_records_do_not_appear_in_profile_b(client, statement_env):
    client.post('/login', data={'username': statement_env['user_b_username'], 'password': 'SecretB456!'}, follow_redirects=True)
    res = client.get('/statement/b2b?return_period=082024')
    assert res.status_code == 200
    data = res.get_json()
    assert data['total'] == 1
    invoices = [row['invoice_number'] for row in data['data']]
    assert 'INV-B-SECRET-001' in invoices
    assert 'INV-A-08-001' not in invoices
    assert 'INV-A-08-002' not in invoices


# 4. Deleted rows are excluded
def test_statement_deleted_rows_are_excluded(app, client, statement_env):
    with app.app_context():
        tx = _db.session.get(Transaction, statement_env['tx_a_08_1_id'])
        tx.is_deleted = True
        _db.session.commit()

    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    res = client.get('/statement/b2b?return_period=082024')
    assert res.status_code == 200
    data = res.get_json()
    assert data['total'] == 1
    invoices = [row['invoice_number'] for row in data['data']]
    assert 'INV-A-08-001' not in invoices
    assert 'INV-A-08-002' in invoices


# 5. Pagination preserves isolation
def test_statement_pagination_preserves_isolation(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    # Page 1 of 1-per-page for Period 082024
    res_p1 = client.get('/statement/b2b?return_period=082024&page=1&per_page=1')
    assert res_p1.status_code == 200
    data_p1 = res_p1.get_json()
    assert data_p1['total'] == 2
    assert len(data_p1['data']) == 1

    # Page 2 of 1-per-page for Period 082024
    res_p2 = client.get('/statement/b2b?return_period=082024&page=2&per_page=1')
    assert res_p2.status_code == 200
    data_p2 = res_p2.get_json()
    assert data_p2['total'] == 2
    assert len(data_p2['data']) == 1

    # Invoices across both pages must only be from Period 082024
    p1_inv = data_p1['data'][0]['invoice_number']
    p2_inv = data_p2['data'][0]['invoice_number']
    assert {p1_inv, p2_inv} == {'INV-A-08-001', 'INV-A-08-002'}
    assert 'INV-A-09-001' not in {p1_inv, p2_inv}
    assert 'INV-B-SECRET-001' not in {p1_inv, p2_inv}


# 6. Search preserves isolation
def test_statement_search_preserves_isolation(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    # Search for "Alpha" (customer name present in both Period 082024 and 092024)
    res = client.get('/statement/b2b?search=Alpha&return_period=082024')
    assert res.status_code == 200
    data = res.get_json()
    assert data['total'] == 1
    assert data['data'][0]['invoice_number'] == 'INV-A-08-001'
    # Period 09 row must NOT bleed into Period 08 search results
    invoices = [r['invoice_number'] for r in data['data']]
    assert 'INV-A-09-001' not in invoices


# 7. Empty period returns empty result
def test_statement_empty_period_returns_empty_result(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    # Explicitly empty return period
    res_empty = client.get('/statement/b2b?return_period=')
    assert res_empty.status_code == 200
    data_empty = res_empty.get_json()
    assert data_empty['total'] == 0
    assert len(data_empty['data']) == 0

    # Unimported period
    res_unimported = client.get('/statement/b2b?return_period=122019')
    assert res_unimported.status_code == 200
    data_unimp = res_unimported.get_json()
    assert data_unimp['total'] == 0
    assert len(data_unimp['data']) == 0


# 8. Invalid / unauthorized profile ID cannot leak records (IDOR defense)
def test_statement_unauthorized_profile_cannot_leak_records(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    # User A maliciously requests User B's profile ID
    res = client.get(f'/statement/b2b?profile_id={statement_env["p_b1_id"]}&return_period=082024')
    assert res.status_code == 200
    data = res.get_json()
    # Must NOT return User B's transactions
    invoices = [r['invoice_number'] for r in data['data']]
    assert 'INV-B-SECRET-001' not in invoices


# 9. Edit transaction validates ownership, is_deleted status, recalculates taxes, creates audit
def test_statement_edit_transaction(app, client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    # 1. Successful edit by owner
    res_edit = client.post(
        f'/statement/edit/{statement_env["tx_a_08_1_id"]}',
        json={'taxable_value': '1500.00', 'reason': 'Audit price correction'},
    )
    assert res_edit.status_code == 200
    assert res_edit.get_json()['success'] is True

    with app.app_context():
        tx = _db.session.get(Transaction, statement_env['tx_a_08_1_id'])
        assert tx.taxable_value == Decimal('1500.00')
        assert tx.cgst_amount == Decimal('135.00')
        assert tx.sgst_amount == Decimal('135.00')
        assert tx.total_tax == Decimal('270.00')
        assert tx.invoice_value == Decimal('1770.00')

        # Verify AuditLog
        audit = AuditLog.query.filter_by(entity_id=tx.id, action='UPDATE').first()
        assert audit is not None
        assert audit.field_name == 'taxable_value'
        assert audit.reason == 'Audit price correction'

    # 2. Cannot edit another user's transaction (IDOR check)
    res_idor = client.post(
        f'/statement/edit/{statement_env["tx_b_08_1_id"]}',
        json={'taxable_value': '1.00'},
    )
    assert res_idor.status_code == 404

    # 3. Cannot edit deleted transaction
    with app.app_context():
        tx = _db.session.get(Transaction, statement_env['tx_a_08_2_id'])
        tx.is_deleted = True
        _db.session.commit()

    res_del_edit = client.post(
        f'/statement/edit/{statement_env["tx_a_08_2_id"]}',
        json={'taxable_value': '500.00'},
    )
    assert res_del_edit.status_code == 404


# 10. Delete transaction performs soft-delete, creates audit trail, excludes from statement
def test_statement_delete_transaction(app, client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    res_del = client.post(
        f'/statement/delete/{statement_env["tx_a_08_1_id"]}',
        json={'reason': 'Duplicate order entry'},
    )
    assert res_del.status_code == 200
    assert res_del.get_json()['success'] is True

    with app.app_context():
        tx = _db.session.get(Transaction, statement_env['tx_a_08_1_id'])
        assert tx.is_deleted is True

        audit = AuditLog.query.filter_by(entity_id=tx.id, action='DELETE').first()
        assert audit is not None
        assert audit.reason == 'Duplicate order entry'

    # Immediately excluded from statement
    res_list = client.get('/statement/b2b?return_period=082024')
    invoices = [r['invoice_number'] for r in res_list.get_json()['data']]
    assert 'INV-A-08-001' not in invoices


# 11. CSV export strictly preserves profile and return-period isolation
def test_statement_export_isolation(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    # Export Period 082024
    res_08 = client.get('/statement/export/b2b?return_period=082024')
    assert res_08.status_code == 200
    csv_08 = res_08.get_data(as_text=True)
    assert 'INV-A-08-001' in csv_08
    assert 'INV-A-08-002' in csv_08
    assert 'INV-A-09-001' not in csv_08
    assert 'INV-B-SECRET-001' not in csv_08

    # Export Period 092024
    res_09 = client.get('/statement/export/b2b?return_period=092024')
    assert res_09.status_code == 200
    csv_09 = res_09.get_data(as_text=True)
    assert 'INV-A-09-001' in csv_09
    assert 'INV-A-08-001' not in csv_09
    assert 'INV-A-08-002' not in csv_09
    assert 'INV-B-SECRET-001' not in csv_09


# 12. Statement HTML page renders with real database context
def test_statement_page_renders_with_real_database_data(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    res = client.get('/statement?return_period=082024')
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'Manage Statement Data' in html
    assert '082024' in html
    assert 'Seller A Retail' in html
    assert 'statementTabs' in html
    assert 'tableBody' in html


# 13. Statement page period display and selection
def test_statement_page_period_display_and_selection(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    # Default without param resolves to active period
    res = client.get('/statement')
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'periodSelect' in html
    assert '082024' in html
    assert '092024' in html

    # With explicit query param
    res_09 = client.get('/statement?return_period=092024')
    assert res_09.status_code == 200
    html_09 = res_09.get_data(as_text=True)
    assert '092024' in html_09


# 14. Summary totals reflect real data from current query
def test_statement_summary_totals_accurate(client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)
    res = client.get('/statement/b2b?return_period=082024')
    assert res.status_code == 200
    data = res.get_json()
    assert 'summary' in data
    summary = data['summary']
    assert summary['count'] == 2
    assert summary['total_taxable'] == 3000.0  # 1000 + 2000
    assert summary['total_tax'] == 540.0       # 180 + 360
    assert summary['total_invoice_value'] == 3540.0  # 1180 + 2360

    # Summary with search filter
    res_search = client.get('/statement/b2b?return_period=082024&search=Alpha')
    assert res_search.status_code == 200
    search_summary = res_search.get_json()['summary']
    assert search_summary['count'] == 1
    assert search_summary['total_taxable'] == 1000.0
    assert search_summary['total_tax'] == 180.0
    assert search_summary['total_invoice_value'] == 1180.0


# 15. Fetch transaction endpoint validates ownership and is_deleted
def test_statement_get_transaction_ownership_and_isolation(app, client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    # 1. Owner can fetch
    res = client.get(f'/statement/transaction/{statement_env["tx_a_08_1_id"]}')
    assert res.status_code == 200
    data = res.get_json()
    assert data['id'] == statement_env['tx_a_08_1_id']
    assert data['invoice_number'] == 'INV-A-08-001'
    assert data['taxable_value'] == 1000.0
    assert data['total_tax'] == 180.0

    # 2. Cannot fetch another user's transaction (IDOR)
    res_idor = client.get(f'/statement/transaction/{statement_env["tx_b_08_1_id"]}')
    assert res_idor.status_code == 404

    # 3. Cannot fetch deleted transaction
    with app.app_context():
        tx = _db.session.get(Transaction, statement_env['tx_a_08_1_id'])
        tx.is_deleted = True
        _db.session.commit()

    res_del = client.get(f'/statement/transaction/{statement_env["tx_a_08_1_id"]}')
    assert res_del.status_code == 404


# 16. Bulk delete soft-deletes only owned transactions and updates statement
def test_statement_bulk_delete_transactions(app, client, statement_env):
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    # User A deletes their 2 Period 082024 transactions
    ids = [statement_env['tx_a_08_1_id'], statement_env['tx_a_08_2_id']]
    res = client.post('/statement/bulk-delete', json={'ids': ids, 'reason': 'Bulk audit cleanup'})
    assert res.status_code == 200
    assert res.get_json()['success'] is True
    assert res.get_json()['deleted_count'] == 2

    # Verify both are soft-deleted
    with app.app_context():
        t1 = _db.session.get(Transaction, statement_env['tx_a_08_1_id'])
        t2 = _db.session.get(Transaction, statement_env['tx_a_08_2_id'])
        assert t1.is_deleted is True
        assert t2.is_deleted is True

        # Verify audit logs
        audits = AuditLog.query.filter_by(action='DELETE', reason='Bulk audit cleanup').all()
        assert len(audits) == 2

    # Transactions disappear from statement
    res_b2b = client.get('/statement/b2b?return_period=082024')
    assert res_b2b.status_code == 200
    assert res_b2b.get_json()['total'] == 0

    # Attempting to bulk-delete User B's transaction does NOT delete it
    res_b_del = client.post('/statement/bulk-delete', json={'ids': [statement_env['tx_b_08_1_id']]})
    assert res_b_del.status_code == 200
    assert res_b_del.get_json()['deleted_count'] == 0
    with app.app_context():
        tb = _db.session.get(Transaction, statement_env['tx_b_08_1_id'])
        assert tb.is_deleted is False


# 17. All supported sections return real database data
def test_statement_all_sections_return_real_data(app, client, statement_env):
    with app.app_context():
        # Create transactions for each supported section
        tx_b2c = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-B2C-001',
            invoice_date=date(2024, 8, 15),
            supply_type='B2CS',
            customer_name='Consumer C',
            taxable_value=Decimal('500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        tx_cdnr = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='CN-CDNR-001',
            invoice_date=date(2024, 8, 16),
            supply_type='CDNR',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('200.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('18.00'),
            sgst_amount=Decimal('18.00'),
            total_tax=Decimal('36.00'),
            invoice_value=Decimal('236.00'),
            place_of_supply='27',
            hsn_sac='8471',
            note_type='C',
            is_deleted=False,
        )
        tx_nil = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-NIL-001',
            invoice_date=date(2024, 8, 17),
            supply_type='NIL',
            taxable_value=Decimal('100.00'),
            tax_rate=Decimal('0.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            total_tax=Decimal('0.00'),
            invoice_value=Decimal('100.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        tx_ecom = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-ECOM-001',
            invoice_date=date(2024, 8, 18),
            supply_type='B2CS',
            ecommerce_gstin='27ECOM123456789',
            taxable_value=Decimal('300.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('27.00'),
            sgst_amount=Decimal('27.00'),
            total_tax=Decimal('54.00'),
            invoice_value=Decimal('354.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        _db.session.add_all([tx_b2c, tx_cdnr, tx_nil, tx_ecom])
        _db.session.commit()

    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    # 1. B2C
    res_b2c = client.get('/statement/b2c?return_period=082024')
    assert res_b2c.status_code == 200
    assert any(r['invoice_number'] == 'INV-B2C-001' for r in res_b2c.get_json()['data'])

    # 2. CDNR
    res_cdnr = client.get('/statement/cdnr?return_period=082024')
    assert res_cdnr.status_code == 200
    assert any(r['invoice_number'] == 'CN-CDNR-001' for r in res_cdnr.get_json()['data'])

    # 3. NIL
    res_nil = client.get('/statement/nil?return_period=082024')
    assert res_nil.status_code == 200
    assert any(r['invoice_number'] == 'INV-NIL-001' for r in res_nil.get_json()['data'])

    # 4. HSN B2B & B2C
    res_hsn = client.get('/statement/hsn-b2b?return_period=082024')
    assert res_hsn.status_code == 200
    assert res_hsn.get_json()['total'] >= 1
    assert all(r['supply_type'] in ('B2B', 'B2BA', 'HSN') for r in res_hsn.get_json()['data'])

    res_hsn_b2c = client.get('/statement/hsn-b2c?return_period=082024')
    assert res_hsn_b2c.status_code == 200
    assert res_hsn_b2c.get_json()['total'] >= 1
    assert any(r['invoice_number'] == 'INV-B2C-001' for r in res_hsn_b2c.get_json()['data'])
    assert all(r['supply_type'] in ('B2CS', 'B2CSA', 'B2CL', 'B2CLA', 'HSNB2C') for r in res_hsn_b2c.get_json()['data'])

    # 5. ECOM
    res_ecom = client.get('/statement/ecom?return_period=082024')
    assert res_ecom.status_code == 200
    assert any(r['invoice_number'] == 'INV-ECOM-001' for r in res_ecom.get_json()['data'])


def test_phase5e21_hsn_b2b_b2c_section_semantics(app, db, client, statement_env):
    """Phase 5E-2.1: Proves strict HSN section semantics:
    1. HSN_B2B returns only B2B transactions with valid HSN (excludes B2C).
    2. HSN_B2C returns only B2C transactions with valid HSN (excludes B2B).
    3. HSN rows with missing or empty HSN are excluded from both sections.
    4. HSN sections remain period-scoped (period A does not leak into period B).
    5. HSN sections remain profile-scoped (user/profile A does not leak into profile B).
    6. Deleted HSN rows (is_deleted=True) remain excluded.
    7. CSV exports for HSN_B2B and HSN_B2C reflect the exact same semantics.
    """
    with app.app_context():
        # Setup specific transactions for Profile A1 in Period 082024
        # 1. B2B with valid HSN
        tx_b2b_with_hsn = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2B-OK',
            invoice_date=date(2024, 8, 20),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('1500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('135.00'),
            sgst_amount=Decimal('135.00'),
            total_tax=Decimal('270.00'),
            invoice_value=Decimal('1770.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        # 2. B2C with valid HSN
        tx_b2c_with_hsn = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2C-OK',
            invoice_date=date(2024, 8, 21),
            supply_type='B2CS',
            customer_name='Consumer X',
            taxable_value=Decimal('800.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('72.00'),
            sgst_amount=Decimal('72.00'),
            total_tax=Decimal('144.00'),
            invoice_value=Decimal('944.00'),
            place_of_supply='27',
            hsn_sac='6109',
            is_deleted=False,
        )
        # 3. B2B without HSN (None)
        tx_b2b_no_hsn = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2B-NOHSN',
            invoice_date=date(2024, 8, 22),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('1200.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('108.00'),
            sgst_amount=Decimal('108.00'),
            total_tax=Decimal('216.00'),
            invoice_value=Decimal('1416.00'),
            place_of_supply='27',
            hsn_sac=None,
            is_deleted=False,
        )
        # 4. B2C without HSN (empty string)
        tx_b2c_no_hsn = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2C-NOHSN',
            invoice_date=date(2024, 8, 23),
            supply_type='B2CS',
            customer_name='Consumer Y',
            taxable_value=Decimal('600.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('54.00'),
            sgst_amount=Decimal('54.00'),
            total_tax=Decimal('108.00'),
            invoice_value=Decimal('708.00'),
            place_of_supply='27',
            hsn_sac='',
            is_deleted=False,
        )
        # 5. Soft-deleted B2B with HSN
        tx_b2b_deleted = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2B-DEL',
            invoice_date=date(2024, 8, 24),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('900.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('81.00'),
            sgst_amount=Decimal('81.00'),
            total_tax=Decimal('162.00'),
            invoice_value=Decimal('1062.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=True,
        )
        # 6. Soft-deleted B2C with HSN
        tx_b2c_deleted = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2C-DEL',
            invoice_date=date(2024, 8, 24),
            supply_type='B2CS',
            customer_name='Consumer Z',
            taxable_value=Decimal('400.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('36.00'),
            sgst_amount=Decimal('36.00'),
            total_tax=Decimal('72.00'),
            invoice_value=Decimal('472.00'),
            place_of_supply='27',
            hsn_sac='6109',
            is_deleted=True,
        )

        # Profile A1 in Period 092024 (different period)
        tx_b2b_p2 = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_09_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2B-P2',
            invoice_date=date(2024, 9, 5),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            customer_name='Alpha Buyer',
            taxable_value=Decimal('2500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('225.00'),
            sgst_amount=Decimal('225.00'),
            total_tax=Decimal('450.00'),
            invoice_value=Decimal('2950.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        tx_b2c_p2 = Transaction(
            profile_id=statement_env['p_a1_id'],
            import_history_id=statement_env['imp_a_09_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2C-P2',
            invoice_date=date(2024, 9, 6),
            supply_type='B2CS',
            customer_name='Consumer Sept',
            taxable_value=Decimal('1100.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('99.00'),
            sgst_amount=Decimal('99.00'),
            total_tax=Decimal('198.00'),
            invoice_value=Decimal('1298.00'),
            place_of_supply='27',
            hsn_sac='6109',
            is_deleted=False,
        )

        # Profile B1 in Period 082024 (different profile / user)
        tx_b2b_tb = Transaction(
            profile_id=statement_env['p_b1_id'],
            import_history_id=statement_env['imp_b_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2B-TB',
            invoice_date=date(2024, 8, 10),
            supply_type='B2B',
            customer_gstin='29AALCS5765L1ZP',
            customer_name='Tenant B Buyer',
            taxable_value=Decimal('3500.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('630.00'),
            total_tax=Decimal('630.00'),
            invoice_value=Decimal('4130.00'),
            place_of_supply='29',
            hsn_sac='8471',
            is_deleted=False,
        )
        tx_b2c_tb = Transaction(
            profile_id=statement_env['p_b1_id'],
            import_history_id=statement_env['imp_b_08_id'],
            raw_import_id=1,
            invoice_number='INV-HSN-B2C-TB',
            invoice_date=date(2024, 8, 11),
            supply_type='B2CS',
            customer_name='Tenant B Consumer',
            taxable_value=Decimal('750.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('135.00'),
            total_tax=Decimal('135.00'),
            invoice_value=Decimal('885.00'),
            place_of_supply='29',
            hsn_sac='6109',
            is_deleted=False,
        )

        _db.session.add_all([
            tx_b2b_with_hsn, tx_b2c_with_hsn, tx_b2b_no_hsn, tx_b2c_no_hsn,
            tx_b2b_deleted, tx_b2c_deleted, tx_b2b_p2, tx_b2c_p2,
            tx_b2b_tb, tx_b2c_tb
        ])
        _db.session.commit()

    # Login as User A
    client.post('/login', data={'username': statement_env['user_a_username'], 'password': 'SecretA123!'}, follow_redirects=True)

    # ── Test 1: HSN_B2B endpoint returns B2B with HSN and strictly excludes B2C ──
    res_b2b_hsn = client.get('/statement/hsn-b2b?return_period=082024')
    assert res_b2b_hsn.status_code == 200
    b2b_data = res_b2b_hsn.get_json()['data']
    b2b_invoices = [r['invoice_number'] for r in b2b_data]

    # Must contain B2B with valid HSN
    assert 'INV-HSN-B2B-OK' in b2b_invoices
    # Must NOT contain B2C with HSN (Rule 1: HSN_B2B excludes B2C)
    assert 'INV-HSN-B2C-OK' not in b2b_invoices
    # Must NOT contain missing HSN rows (Rule 3)
    assert 'INV-HSN-B2B-NOHSN' not in b2b_invoices
    assert 'INV-HSN-B2C-NOHSN' not in b2b_invoices
    # Must NOT contain soft-deleted rows (Rule 6)
    assert 'INV-HSN-B2B-DEL' not in b2b_invoices
    assert 'INV-HSN-B2C-DEL' not in b2b_invoices
    # Must NOT contain other period rows (Rule 4)
    assert 'INV-HSN-B2B-P2' not in b2b_invoices
    # Must NOT contain other profile rows (Rule 5)
    assert 'INV-HSN-B2B-TB' not in b2b_invoices
    # Verify all returned rows have B2B supply type and non-empty HSN
    assert all(r['supply_type'] in ('B2B', 'B2BA', 'HSN') for r in b2b_data)
    assert all(bool(r['hsn_sac'] and r['hsn_sac'].strip()) for r in b2b_data)

    # ── Test 2: HSN_B2C endpoint returns B2C with HSN and strictly excludes B2B ──
    res_b2c_hsn = client.get('/statement/hsn-b2c?return_period=082024')
    assert res_b2c_hsn.status_code == 200
    b2c_data = res_b2c_hsn.get_json()['data']
    b2c_invoices = [r['invoice_number'] for r in b2c_data]

    # Must contain B2C with valid HSN
    assert 'INV-HSN-B2C-OK' in b2c_invoices
    # Must NOT contain B2B with HSN (Rule 2: HSN_B2C excludes B2B)
    assert 'INV-HSN-B2B-OK' not in b2c_invoices
    assert 'INV-A-08-001' not in b2c_invoices
    assert 'INV-A-08-002' not in b2c_invoices
    # Must NOT contain missing HSN rows (Rule 3)
    assert 'INV-HSN-B2B-NOHSN' not in b2c_invoices
    assert 'INV-HSN-B2C-NOHSN' not in b2c_invoices
    # Must NOT contain soft-deleted rows (Rule 6)
    assert 'INV-HSN-B2B-DEL' not in b2c_invoices
    assert 'INV-HSN-B2C-DEL' not in b2c_invoices
    # Must NOT contain other period rows (Rule 4)
    assert 'INV-HSN-B2C-P2' not in b2c_invoices
    # Must NOT contain other profile rows (Rule 5)
    assert 'INV-HSN-B2C-TB' not in b2c_invoices
    # Verify all returned rows have B2C supply type and non-empty HSN
    assert all(r['supply_type'] in ('B2CS', 'B2CSA', 'B2CL', 'B2CLA', 'HSNB2C') for r in b2c_data)
    assert all(bool(r['hsn_sac'] and r['hsn_sac'].strip()) for r in b2c_data)

    # ── Test 3: CSV Export honors exact HSN_B2B vs HSN_B2C semantics ──
    csv_b2b = client.get('/statement/export/hsn-b2b?return_period=082024')
    assert csv_b2b.status_code == 200
    b2b_text = csv_b2b.data.decode('utf-8')
    assert 'INV-HSN-B2B-OK' in b2b_text
    assert 'INV-HSN-B2C-OK' not in b2b_text
    assert 'INV-HSN-B2B-NOHSN' not in b2b_text
    assert 'INV-HSN-B2B-DEL' not in b2b_text

    csv_b2c = client.get('/statement/export/hsn-b2c?return_period=082024')
    assert csv_b2c.status_code == 200
    b2c_text = csv_b2c.data.decode('utf-8')
    assert 'INV-HSN-B2C-OK' in b2c_text
    assert 'INV-HSN-B2B-OK' not in b2c_text
    assert 'INV-HSN-B2C-NOHSN' not in b2c_text
    assert 'INV-HSN-B2C-DEL' not in b2c_text

    # ── Test 4: Return Period Isolation for both sections in Period 092024 ──
    res_b2b_p2 = client.get('/statement/hsn-b2b?return_period=092024')
    assert res_b2b_p2.status_code == 200
    p2_b2b_invs = [r['invoice_number'] for r in res_b2b_p2.get_json()['data']]
    assert 'INV-HSN-B2B-P2' in p2_b2b_invs
    assert 'INV-HSN-B2B-OK' not in p2_b2b_invs
    assert 'INV-HSN-B2C-P2' not in p2_b2b_invs

    res_b2c_p2 = client.get('/statement/hsn-b2c?return_period=092024')
    assert res_b2c_p2.status_code == 200
    p2_b2c_invs = [r['invoice_number'] for r in res_b2c_p2.get_json()['data']]
    assert 'INV-HSN-B2C-P2' in p2_b2c_invs
    assert 'INV-HSN-B2C-OK' not in p2_b2c_invs
    assert 'INV-HSN-B2B-P2' not in p2_b2c_invs

    # ── Test 5: Profile Isolation (cross-tenant security) ──
    client.get('/logout', follow_redirects=True)
    client.post('/login', data={'username': statement_env['user_b_username'], 'password': 'SecretB456!'}, follow_redirects=True)

    res_tb_b2b = client.get('/statement/hsn-b2b?return_period=082024')
    assert res_tb_b2b.status_code == 200
    tb_b2b_invs = [r['invoice_number'] for r in res_tb_b2b.get_json()['data']]
    assert 'INV-HSN-B2B-TB' in tb_b2b_invs
    assert 'INV-HSN-B2B-OK' not in tb_b2b_invs
    assert 'INV-HSN-B2C-TB' not in tb_b2b_invs

    res_tb_b2c = client.get('/statement/hsn-b2c?return_period=082024')
    assert res_tb_b2c.status_code == 200
    tb_b2c_invs = [r['invoice_number'] for r in res_tb_b2c.get_json()['data']]
    assert 'INV-HSN-B2C-TB' in tb_b2c_invs
    assert 'INV-HSN-B2C-OK' not in tb_b2c_invs
    assert 'INV-HSN-B2B-TB' not in tb_b2c_invs
