"""Integration tests for Phase 5E-10B: Cross-Tenant Isolation & Hardening.

Verifies:
1. tenant_query(model, profile_id) strictly isolates records across tenants for:
   - Transaction
   - ImportHistory
   - TCSReconciliation
   - GSTR1Generation
2. Negative cross-tenant operations fail gracefully without side effects or information leakage:
   - find_prior_file_import(tenant_b_id, tenant_a_file_hash) -> None
   - find_existing_transactions(tenant_b_id, tenant_a_fingerprints) -> {}
   - reprocess_import with tenant_b_id on Tenant A's import -> FAILED, leaves Tenant A untouched
   - get_ecom_supplies(tenant_b_id, return_period) -> []
   - load_transactions(tenant_b_id, return_period) -> empty GSTR-1 structure
   - check_import_counts, check_duplicates for Tenant B -> reports 0 of Tenant A data
   - apply_adjustment with tenant_b_id on Tenant A's reconciliation -> returns False
3. Soft-deleted transactions (is_deleted=True) are strictly excluded from queries and business checks.
4. Active profile ownership and web/API boundary enforcement.
"""
from datetime import date, datetime
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import (
    GSTProfile,
    GSTR1Generation,
    ImportHistory,
    RawImport,
    TCSReconciliation,
    Transaction,
    User,
)
from app.services.duplicate_service import (
    find_existing_transactions,
    find_prior_file_import,
)
from app.services.ecom_service import get_ecom_supplies
from app.services.gstr1_generator import load_transactions
from app.services.import_service import reprocess_import
from app.services.reconciliation_service import (
    check_duplicates,
    check_import_counts,
)
from app.services.tcs_service import apply_adjustment
from app.utils.query import tenant_query


@pytest.fixture
def tenant_fixture(app, db):
    """Setup two distinct users with distinct GST profiles and related models."""
    with app.app_context():
        # User A & Profile A
        user_a = User(username='tenant_user_a', email='user_a@example.com')
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
            filing_frequency='monthly',
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # User B & Profile B
        user_b = User(username='tenant_user_b', email='user_b@example.com')
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
            filing_frequency='monthly',
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Seed Data for Profile A
        import_a = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='tenant_a_report.xlsx',
            original_file_name='tenant_a_report.xlsx',
            file_hash='hash_tenant_a_unique_file_123',
            file_size=2048,
            platform_name='Amazon',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=2,
            success_rows=2,
        )
        _db.session.add(import_a)
        _db.session.commit()

        raw_a1 = RawImport(import_history_id=import_a.id, row_number=1, status='SUCCESS')
        raw_a2 = RawImport(import_history_id=import_a.id, row_number=2, status='SUCCESS')
        _db.session.add_all([raw_a1, raw_a2])
        _db.session.commit()

        tx_a1 = Transaction(
            profile_id=profile_a.id,
            import_history_id=import_a.id,
            raw_import_id=raw_a1.id,
            order_id='ORD-A-001',
            invoice_number='INV-A-001',
            invoice_date=date(2024, 8, 10),
            taxable_value=Decimal('10000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('1800.00'),
            total_tax=Decimal('1800.00'),
            invoice_value=Decimal('11800.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            ecommerce_gstin='27AAACE6333D1Z1',
            row_fingerprint='fp_tenant_a_001',
            is_deleted=False,
        )
        tx_a2 = Transaction(
            profile_id=profile_a.id,
            import_history_id=import_a.id,
            raw_import_id=raw_a2.id,
            order_id='ORD-A-002',
            invoice_number='INV-A-002',
            invoice_date=date(2024, 8, 12),
            taxable_value=Decimal('5000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('900.00'),
            total_tax=Decimal('900.00'),
            invoice_value=Decimal('5900.00'),
            supply_type='B2CS',
            place_of_supply='29',
            ecommerce_gstin='27AAACE6333D1Z1',
            row_fingerprint='fp_tenant_a_002',
            is_deleted=False,
        )
        _db.session.add_all([tx_a1, tx_a2])
        _db.session.commit()

        tcs_a = TCSReconciliation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            ecommerce_gstin='27AAACE6333D1Z1',
            state_code='29',
            state_name='Karnataka',
            portal_taxable_value=Decimal('15000.00'),
            portal_tcs=Decimal('150.00'),
            portal_igst_tcs=Decimal('150.00'),
            our_net_taxable_value=Decimal('15000.00'),
            our_calculated_tcs=Decimal('150.00'),
            our_calculated_igst=Decimal('150.00'),
            difference_taxable=Decimal('0.00'),
            difference_tcs=Decimal('0.00'),
            match_status='MATCHED',
        )
        _db.session.add(tcs_a)
        _db.session.commit()

        gen_a = GSTR1Generation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            generation_status='COMPLETED',
            total_b2b=1,
            total_b2cs=1,
            total_taxable_value=15000.0,
            validation_passed=True,
        )
        _db.session.add(gen_a)
        _db.session.commit()

        # Seed Data for Profile B
        import_b = ImportHistory(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            file_name='tenant_b_report.xlsx',
            original_file_name='tenant_b_report.xlsx',
            file_hash='hash_tenant_b_unique_file_456',
            file_size=1024,
            platform_name='Flipkart',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
        )
        _db.session.add(import_b)
        _db.session.commit()

        raw_b1 = RawImport(import_history_id=import_b.id, row_number=1, status='SUCCESS')
        _db.session.add(raw_b1)
        _db.session.commit()

        tx_b1 = Transaction(
            profile_id=profile_b.id,
            import_history_id=import_b.id,
            raw_import_id=raw_b1.id,
            order_id='ORD-B-001',
            invoice_number='INV-B-001',
            invoice_date=date(2024, 8, 20),
            taxable_value=Decimal('8000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('720.00'),
            sgst_amount=Decimal('720.00'),
            total_tax=Decimal('1440.00'),
            invoice_value=Decimal('9440.00'),
            supply_type='B2CS',
            place_of_supply='29',
            ecommerce_gstin='29AAACE6333D1Z2',
            row_fingerprint='fp_tenant_b_001',
            is_deleted=False,
        )
        _db.session.add(tx_b1)
        _db.session.commit()

        tcs_b = TCSReconciliation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            ecommerce_gstin='29AAACE6333D1Z2',
            state_code='29',
            state_name='Karnataka',
            portal_taxable_value=Decimal('8000.00'),
            portal_tcs=Decimal('80.00'),
            portal_cgst_tcs=Decimal('40.00'),
            portal_sgst_tcs=Decimal('40.00'),
            our_net_taxable_value=Decimal('8000.00'),
            our_calculated_tcs=Decimal('80.00'),
            difference_taxable=Decimal('0.00'),
            difference_tcs=Decimal('0.00'),
            match_status='MATCHED',
        )
        _db.session.add(tcs_b)
        _db.session.commit()

        gen_b = GSTR1Generation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            generation_status='COMPLETED',
            total_b2b=0,
            total_b2cs=1,
            total_taxable_value=8000.0,
            validation_passed=True,
        )
        _db.session.add(gen_b)
        _db.session.commit()

        yield {
            'user_a': user_a,
            'user_b': user_b,
            'profile_a': profile_a,
            'profile_b': profile_b,
            'import_a': import_a,
            'import_b': import_b,
            'tx_a1': tx_a1,
            'tx_a2': tx_a2,
            'tx_b1': tx_b1,
            'tcs_a': tcs_a,
            'tcs_b': tcs_b,
            'gen_a': gen_a,
            'gen_b': gen_b,
        }


def test_tenant_query_direct_isolation(tenant_fixture):
    """Verify tenant_query returns ONLY the targeted tenant's entities."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']

    # 1. Transaction model isolation
    tx_a_list = tenant_query(Transaction, p_a.id).all()
    tx_b_list = tenant_query(Transaction, p_b.id).all()
    assert len(tx_a_list) == 2
    assert len(tx_b_list) == 1
    assert all(t.profile_id == p_a.id for t in tx_a_list)
    assert all(t.profile_id == p_b.id for t in tx_b_list)
    assert tenant_fixture['tx_b1'].id not in [t.id for t in tx_a_list]
    assert tenant_fixture['tx_a1'].id not in [t.id for t in tx_b_list]

    # 2. ImportHistory model isolation
    imp_a_list = tenant_query(ImportHistory, p_a.id).all()
    imp_b_list = tenant_query(ImportHistory, p_b.id).all()
    assert len(imp_a_list) == 1
    assert len(imp_b_list) == 1
    assert imp_a_list[0].id == tenant_fixture['import_a'].id
    assert imp_b_list[0].id == tenant_fixture['import_b'].id
    assert imp_a_list[0].file_hash == 'hash_tenant_a_unique_file_123'
    assert imp_b_list[0].file_hash == 'hash_tenant_b_unique_file_456'

    # 3. TCSReconciliation model isolation
    tcs_a_list = tenant_query(TCSReconciliation, p_a.id).all()
    tcs_b_list = tenant_query(TCSReconciliation, p_b.id).all()
    assert len(tcs_a_list) == 1
    assert len(tcs_b_list) == 1
    assert tcs_a_list[0].id == tenant_fixture['tcs_a'].id
    assert tcs_b_list[0].id == tenant_fixture['tcs_b'].id

    # 4. GSTR1Generation model isolation
    gen_a_list = tenant_query(GSTR1Generation, p_a.id).all()
    gen_b_list = tenant_query(GSTR1Generation, p_b.id).all()
    assert len(gen_a_list) == 1
    assert len(gen_b_list) == 1
    assert gen_a_list[0].id == tenant_fixture['gen_a'].id
    assert gen_b_list[0].id == tenant_fixture['gen_b'].id

    # 5. Non-existent tenant profile returns empty
    assert tenant_query(Transaction, 999999).all() == []
    assert tenant_query(ImportHistory, 999999).all() == []
    assert tenant_query(TCSReconciliation, 999999).all() == []
    assert tenant_query(GSTR1Generation, 999999).all() == []


def test_find_prior_file_import_negative_isolation(tenant_fixture):
    """Tenant B checking Tenant A's file hash must receive None."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']
    hash_a = tenant_fixture['import_a'].file_hash

    # Tenant B has never imported hash_a
    prior_b = find_prior_file_import(p_b.id, hash_a)
    assert prior_b is None

    # Tenant A checking its own hash finds the import
    prior_a = find_prior_file_import(p_a.id, hash_a)
    assert prior_a is not None
    assert prior_a.id == tenant_fixture['import_a'].id


def test_find_existing_transactions_negative_isolation(tenant_fixture):
    """Tenant B querying Tenant A's row fingerprints must receive empty dict."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']
    fp_a = tenant_fixture['tx_a1'].row_fingerprint

    # Tenant B lookup returns empty
    res_b = find_existing_transactions(p_b.id, [fp_a])
    assert res_b == {}

    # Tenant A lookup returns the transaction
    res_a = find_existing_transactions(p_a.id, [fp_a])
    assert fp_a in res_a
    assert res_a[fp_a].id == tenant_fixture['tx_a1'].id


def test_reprocess_import_cross_tenant_rejection(tenant_fixture):
    """Tenant B attempting to reprocess Tenant A's import must fail and leave data intact."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']
    u_b = tenant_fixture['user_b']
    imp_a = tenant_fixture['import_a']
    tx_a1 = tenant_fixture['tx_a1']

    # Tenant B attempts reprocess with Tenant A's import_history_id
    result = reprocess_import(imp_a.id, p_b.id, u_b.id)
    assert result.status == 'FAILED'
    assert any('unauthorized' in err.lower() or 'not found' in err.lower() for err in result.errors)

    # Ensure Tenant A's import status and transactions were untouched
    _db.session.refresh(imp_a)
    _db.session.refresh(tx_a1)
    assert imp_a.processing_status == 'COMPLETED'
    assert tx_a1.is_deleted is False


def test_get_ecom_supplies_negative_isolation(tenant_fixture):
    """Tenant B querying ecom supplies for period must not see Tenant A's supplies."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']

    # Both profiles have supplies in 082024
    ecom_a = get_ecom_supplies(p_a.id, '082024')
    ecom_b = get_ecom_supplies(p_b.id, '082024')

    assert len(ecom_a) == 2
    assert len(ecom_b) == 1

    # Verify ID segregation
    a_invoices = {s['invoice_number'] for s in ecom_a}
    b_invoices = {s['invoice_number'] for s in ecom_b}
    assert 'INV-A-001' in a_invoices
    assert 'INV-A-002' in a_invoices
    assert 'INV-B-001' not in a_invoices
    assert 'INV-B-001' in b_invoices
    assert 'INV-A-001' not in b_invoices


def test_load_transactions_negative_isolation(tenant_fixture):
    """load_transactions for Tenant B returns only Tenant B's data structure."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']

    gstr1_a = load_transactions(str(p_a.id), '082024')
    gstr1_b = load_transactions(str(p_b.id), '082024')

    # Profile A has B2B and B2CS
    assert len(gstr1_a.get('b2b', [])) == 1
    assert len(gstr1_a.get('b2cs', [])) == 1

    # Profile B has only B2CS, 0 B2B
    assert len(gstr1_b.get('b2b', [])) == 0
    assert len(gstr1_b.get('b2cs', [])) == 1
    # Check POS in B2CS
    b_items = gstr1_b.get('b2cs', [])
    assert b_items[0]['pos'] == '29'

    # Empty profile
    empty_gstr1 = load_transactions('999999', '082024')
    assert len(empty_gstr1.get('b2b', [])) == 0
    assert len(empty_gstr1.get('b2cs', [])) == 0


def test_reconciliation_checks_negative_isolation(tenant_fixture):
    """Reconciliation checks on Tenant B must not count or inspect Tenant A data."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']

    # check_import_counts
    counts_a = check_import_counts(p_a.id, '082024')
    counts_b = check_import_counts(p_b.id, '082024')

    assert counts_a.passed is True
    assert counts_a.details['imports_count'] == 1
    assert counts_a.details['active_transactions'] == 2

    assert counts_b.passed is True
    assert counts_b.details['imports_count'] == 1
    assert counts_b.details['active_transactions'] == 1

    # check_duplicates
    dups_a = check_duplicates(p_a.id, '082024')
    dups_b = check_duplicates(p_b.id, '082024')

    assert dups_a.passed is True
    assert dups_b.passed is True

    # Inject a duplicate into Profile A only
    raw_dup = RawImport(import_history_id=tenant_fixture['import_a'].id, row_number=3, status='SUCCESS')
    _db.session.add(raw_dup)
    _db.session.flush()

    dup_tx_a = Transaction(
        profile_id=p_a.id,
        import_history_id=tenant_fixture['import_a'].id,
        raw_import_id=raw_dup.id,
        order_id='ORD-A-001-DUP',
        invoice_number='INV-A-001',
        invoice_date=date(2024, 8, 10),
        taxable_value=Decimal('10000.00'),
        tax_rate=Decimal('18.00'),
        igst_amount=Decimal('1800.00'),
        total_tax=Decimal('1800.00'),
        invoice_value=Decimal('11800.00'),
        supply_type='B2B',
        place_of_supply='29',
        row_fingerprint='fp_tenant_a_001',  # Matches tx_a1
        is_deleted=False,
    )
    _db.session.add(dup_tx_a)
    _db.session.commit()

    # Profile A should now fail duplicate check
    dups_a_after = check_duplicates(p_a.id, '082024')
    assert dups_a_after.passed is False

    # Profile B must still pass with 0 duplicates
    dups_b_after = check_duplicates(p_b.id, '082024')
    assert dups_b_after.passed is True


def test_apply_adjustment_negative_isolation(tenant_fixture):
    """Tenant B attempting to adjust Tenant A's TCS record must be denied."""
    p_b = tenant_fixture['profile_b']
    u_b = tenant_fixture['user_b']
    tcs_a = tenant_fixture['tcs_a']

    # Cross-tenant adjustment attempt with profile_id provided
    res = apply_adjustment(
        reconciliation_id=tcs_a.id,
        adjustment_type='ACCEPT_PORTAL',
        notes='Adversarial cross-tenant override',
        user_id=u_b.id,
        profile_id=p_b.id,
    )
    assert res is False

    _db.session.refresh(tcs_a)
    assert tcs_a.adjustment_notes is None or 'Adversarial' not in tcs_a.adjustment_notes


def test_soft_deleted_transaction_isolation(tenant_fixture):
    """Soft deleted transactions are excluded from tenant queries and duplicate detection."""
    p_a = tenant_fixture['profile_a']
    tx_a1 = tenant_fixture['tx_a1']

    # Soft delete tx_a1
    tx_a1.is_deleted = True
    tx_a1.deleted_at = datetime.utcnow()
    _db.session.commit()

    # 1. tenant_query with is_deleted=False
    active_txs = tenant_query(Transaction, p_a.id).filter(
        Transaction.is_deleted == False
    ).all()
    assert tx_a1.id not in [t.id for t in active_txs]
    assert len(active_txs) == 1

    # 2. find_existing_transactions ignores soft-deleted
    found = find_existing_transactions(p_a.id, [tx_a1.row_fingerprint])
    assert tx_a1.row_fingerprint not in found

    # 3. get_ecom_supplies ignores soft-deleted
    supplies = get_ecom_supplies(p_a.id, '082024')
    assert tx_a1.invoice_number not in [s['invoice_number'] for s in supplies]


def test_web_routes_cross_tenant_isolation(client, tenant_fixture):
    """Web and API routes reject cross-tenant IDOR attacks with 404 or empty tenant payload."""
    p_a = tenant_fixture['profile_a']
    p_b = tenant_fixture['profile_b']
    imp_a = tenant_fixture['import_a']
    tcs_a = tenant_fixture['tcs_a']

    # 1. Login as User B
    client.post('/login', data={
        'username': 'tenant_user_b',
        'password': 'SecretB456!',
    }, follow_redirects=True)

    # Set active profile to Profile B
    client.get(f'/profile/select/{p_b.id}', follow_redirects=True)

    # 2. Attempt to view import detail of Profile A -> 404
    resp = client.get(f'/import/{imp_a.id}/detail')
    assert resp.status_code == 404

    # 3. Attempt to reprocess import of Profile A -> 404
    resp = client.post(f'/import/{imp_a.id}/reprocess')
    assert resp.status_code == 404

    # 4. Attempt to download original file of Profile A -> 404
    resp = client.get(f'/import/{imp_a.id}/download-original')
    assert resp.status_code == 404

    # 5. Attempt to adjust TCS reconciliation of Profile A -> 404
    resp = client.post(f'/tcs/adjust/{tcs_a.id}', data={
        'adjustment_type': 'ACCEPT_PORTAL',
        'notes': 'Malicious adjustment'
    })
    assert resp.status_code == 404

    # 6. Call API /api/b2b attempting to access Profile A's data
    resp = client.get(f'/api/b2b?return_period=082024&profile_id={p_a.id}')
    assert resp.status_code == 200
    data = resp.get_json()
    # Should contain 0 transactions because Profile A is unauthorized for User B
    assert data.get('total') == 0
    assert len(data.get('data', [])) == 0

    # 7. Call API /api/dashboard-stats attempting to inspect Profile A's data
    resp = client.get(f'/api/dashboard-stats?return_period=082024&profile_id={p_a.id}')
    assert resp.status_code == 200
    stats = resp.get_json().get('data', {})
    # User B has only 1 invoice (B2CS), while Profile A had 2 invoices (including B2B)
    assert stats.get('total_invoices') == 1
    assert stats.get('breakdown', {}).get('b2b') == 0
    assert stats.get('breakdown', {}).get('b2c') == 1
