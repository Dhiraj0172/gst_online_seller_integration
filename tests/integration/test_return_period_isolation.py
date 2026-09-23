"""
Integration Regression Tests for CRIT-01: Return-Period Transaction Isolation.

Verifies that:
- TEST A: Historical leakage prevention (transactions from 082024 do NOT leak into empty 092024)
- TEST B: Same-period isolation (082024 loads only A; 092024 loads only B)
- TEST C: Empty-period behavior (empty period returns schema-compliant empty structure)
- TEST D: return_period=None behavior (records without valid return period do NOT leak)
- TEST E: Profile/tenant isolation across return periods
- TEST F: Existing normal workflow and soft-delete respect
"""
import pytest
from datetime import date
from decimal import Decimal
from sqlalchemy import text
from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, Transaction, RawImport
from app.services.gstr1_generator import load_transactions, generate_gstr1


@pytest.fixture
def period_isolation_env(app):
    """Sets up test data with multiple profiles, return periods, and transactions."""
    with app.app_context():
        # User 1 with Profile 1
        user1 = User(username='seller_period_1', email='seller_period_1@example.com')
        user1.set_password('Secret123!')
        _db.session.add(user1)
        _db.session.commit()

        profile1 = GSTProfile(
            user_id=user1.id,
            gstin='27AAAAA3333A1Z1',
            legal_name='Seller One Retail',
            trade_name='Store One',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add(profile1)
        _db.session.commit()

        # Import History for Period 082024
        ih_082024 = ImportHistory(
            user_id=user1.id,
            profile_id=profile1.id,
            file_name='sales_082024.csv',
            original_file_name='sales_082024.csv',
            platform_name='AMAZON',
            return_period='082024',
            financial_year='2024-25',
            processing_status='COMPLETED'
        )
        # Import History for Period 092024
        ih_092024 = ImportHistory(
            user_id=user1.id,
            profile_id=profile1.id,
            file_name='sales_092024.csv',
            original_file_name='sales_092024.csv',
            platform_name='AMAZON',
            return_period='092024',
            financial_year='2024-25',
            processing_status='COMPLETED'
        )
        _db.session.add_all([ih_082024, ih_092024])
        _db.session.commit()

        ri_08 = RawImport(import_history_id=ih_082024.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        ri_09 = RawImport(import_history_id=ih_092024.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add_all([ri_08, ri_09])
        _db.session.commit()

        # Invoice A in 082024
        tx_a = Transaction(
            profile_id=profile1.id,
            import_history_id=ih_082024.id,
            raw_import_id=ri_08.id,
            invoice_number='INV-PERIOD-082024-A',
            invoice_date=date(2024, 8, 15),
            taxable_value=Decimal('5000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('900.00'),
            total_tax=Decimal('900.00'),
            invoice_value=Decimal('5900.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=False
        )

        # Invoice B in 092024
        tx_b = Transaction(
            profile_id=profile1.id,
            import_history_id=ih_092024.id,
            raw_import_id=ri_09.id,
            invoice_number='INV-PERIOD-092024-B',
            invoice_date=date(2024, 9, 10),
            taxable_value=Decimal('8000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('1440.00'),
            total_tax=Decimal('1440.00'),
            invoice_value=Decimal('9440.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=False
        )

        # Soft-deleted invoice in 082024
        tx_deleted = Transaction(
            profile_id=profile1.id,
            import_history_id=ih_082024.id,
            raw_import_id=ri_08.id,
            invoice_number='INV-DELETED-082024',
            invoice_date=date(2024, 8, 20),
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('180.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=True
        )

        # User 2 with Profile 2 (for multi-tenant isolation tests)
        user2 = User(username='seller_period_2', email='seller_period_2@example.com')
        user2.set_password('Secret456!')
        _db.session.add(user2)
        _db.session.commit()

        profile2 = GSTProfile(
            user_id=user2.id,
            gstin='27BBBBB4444B1Z2',
            legal_name='Seller Two Electronics',
            trade_name='Store Two',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly'
        )
        _db.session.add(profile2)
        _db.session.commit()

        ih_user2_08 = ImportHistory(
            user_id=user2.id,
            profile_id=profile2.id,
            file_name='seller2_082024.csv',
            original_file_name='seller2_082024.csv',
            platform_name='AMAZON',
            return_period='082024',
            financial_year='2024-25',
            processing_status='COMPLETED'
        )
        _db.session.add(ih_user2_08)
        _db.session.commit()

        ri_u2 = RawImport(import_history_id=ih_user2_08.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(ri_u2)
        _db.session.commit()

        tx_user2 = Transaction(
            profile_id=profile2.id,
            import_history_id=ih_user2_08.id,
            raw_import_id=ri_u2.id,
            invoice_number='INV-USER2-082024',
            invoice_date=date(2024, 8, 12),
            taxable_value=Decimal('15000.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('2700.00'),
            total_tax=Decimal('2700.00'),
            invoice_value=Decimal('17700.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=False
        )

        _db.session.add_all([tx_a, tx_b, tx_deleted, tx_user2])
        _db.session.commit()

        yield {
            'user1_id': user1.id,
            'user2_id': user2.id,
            'profile1_id': profile1.id,
            'profile2_id': profile2.id,
            'ih_082024_id': ih_082024.id,
            'ih_092024_id': ih_092024.id,
            'tx_a_id': tx_a.id,
            'tx_b_id': tx_b.id,
            'tx_deleted_id': tx_deleted.id,
            'tx_user2_id': tx_user2.id
        }

        # Cleanup
        try:
            Transaction.query.filter(Transaction.profile_id.in_([profile1.id, profile2.id])).delete(synchronize_session=False)
            RawImport.query.filter(RawImport.import_history_id.in_([ih_082024.id, ih_092024.id, ih_user2_08.id])).delete(synchronize_session=False)
            ImportHistory.query.filter(ImportHistory.profile_id.in_([profile1.id, profile2.id])).delete(synchronize_session=False)
            _db.session.delete(profile1)
            _db.session.delete(profile2)
            _db.session.delete(user1)
            _db.session.delete(user2)
            _db.session.commit()
        except Exception:
            _db.session.rollback()


def _get_invoice_numbers(gstr1_json):
    """Extract all invoice numbers present in a GSTR-1 json structure."""
    inums = []
    for b2b in gstr1_json.get('b2b', []):
        for inv in b2b.get('inv', []):
            inums.append(inv.get('inum'))
    return inums


def test_historical_leakage_prevention(app, period_isolation_env):
    """
    TEST A: HISTORICAL LEAKAGE PREVENTION
    Profile 1 has Invoice A in 082024.
    Period 072024 has NO transactions.
    Loading 072024 must NOT leak Invoice A from 082024.
    """
    p1 = period_isolation_env['profile1_id']
    with app.app_context():
        # Query 072024 which has no transactions
        data = load_transactions(str(p1), '072024')
        invoices = _get_invoice_numbers(data)

        # Must be empty and must NOT contain INV-PERIOD-082024-A
        assert invoices == [], f"Expected empty invoice list, but got: {invoices}"
        assert 'INV-PERIOD-082024-A' not in invoices
        assert data['b2b'] == []


def test_same_period_isolation(app, period_isolation_env):
    """
    TEST B: SAME-PERIOD ISOLATION
    082024 contains invoice A.
    092024 contains invoice B.
    Loading 082024 returns only A.
    Loading 092024 returns only B.
    """
    p1 = period_isolation_env['profile1_id']
    with app.app_context():
        # Load 082024
        data_08 = load_transactions(str(p1), '082024')
        invoices_08 = _get_invoice_numbers(data_08)
        assert invoices_08 == ['INV-PERIOD-082024-A']
        assert 'INV-PERIOD-092024-B' not in invoices_08

        # Load 092024
        data_09 = load_transactions(str(p1), '092024')
        invoices_09 = _get_invoice_numbers(data_09)
        assert invoices_09 == ['INV-PERIOD-092024-B']
        assert 'INV-PERIOD-082024-A' not in invoices_09


def test_empty_period_behavior(app, period_isolation_env):
    """
    TEST C: EMPTY-PERIOD BEHAVIOR
    Period 102024 has no transactions.
    Must return schema-compliant empty structure with correct fp and empty lists.
    Also generate_gstr1 must handle empty periods gracefully without crashing.
    """
    p1 = period_isolation_env['profile1_id']
    with app.app_context():
        data_10 = load_transactions(str(p1), '102024')
        assert data_10['fp'] == '102024'
        assert data_10['gstin'] == '27AAAAA3333A1Z1'
        assert data_10['b2b'] == []
        assert data_10['b2cs'] == []
        assert data_10['cdnr'] == []
        assert data_10['hsn']['data'] == []
        assert data_10['doc_issue']['doc_det'] == []
        assert _get_invoice_numbers(data_10) == []

        # End-to-end generate_gstr1 for empty period
        gen_res = generate_gstr1(str(p1), '102024')
        assert gen_res.stats['total_invoices'] == 0
        assert gen_res.stats['total_taxable_value'] == 0


def test_return_period_none_does_not_contaminate(app, period_isolation_env):
    """
    TEST D: return_period=None BEHAVIOR
    Create an unlinked transaction (where joined return_period is None)
    and an import with a different period (e.g. '999999').
    Verify that querying a specific period (082024 or 112024) does NOT include either.
    """
    p1 = period_isolation_env['profile1_id']
    u1 = period_isolation_env['user1_id']

    with app.app_context():
        # Create an import history for a different period (999999)
        ih_test = ImportHistory(
            user_id=u1,
            profile_id=p1,
            file_name='other_period.csv',
            original_file_name='other_period.csv',
            platform_name='AMAZON',
            return_period='999999',
            financial_year='2024-25',
            processing_status='COMPLETED'
        )
        _db.session.add(ih_test)
        _db.session.commit()

        ri_test = RawImport(import_history_id=ih_test.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(ri_test)
        _db.session.commit()

        # Transaction 1: attached to period 999999
        tx_other = Transaction(
            profile_id=p1,
            import_history_id=ih_test.id,
            raw_import_id=ri_test.id,
            invoice_number='INV-OTHER-RETURN-PERIOD',
            invoice_date=date(2024, 8, 1),
            taxable_value=Decimal('999.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('179.82'),
            total_tax=Decimal('179.82'),
            invoice_value=Decimal('1178.82'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=False
        )

        # Transaction 2: unlinked transaction (where outer-joined ImportHistory.return_period is None)
        tx_unlinked = Transaction(
            profile_id=p1,
            import_history_id=ih_test.id,
            raw_import_id=ri_test.id,
            invoice_number='INV-UNLINKED-RETURN-PERIOD',
            invoice_date=date(2024, 8, 2),
            taxable_value=Decimal('500.00'),
            tax_rate=Decimal('18.00'),
            igst_amount=Decimal('90.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            supply_type='B2B',
            place_of_supply='29',
            customer_gstin='29AABCT1332L1Z1',
            is_deleted=False
        )
        _db.session.add_all([tx_other, tx_unlinked])
        _db.session.commit()

        # Unlink tx_unlinked's import_history_id so return_period in outer join is None
        _db.session.execute(text(f"UPDATE transactions SET import_history_id = 9999999 WHERE id = {tx_unlinked.id}"))
        _db.session.commit()

        # Verify load_transactions for 082024 does NOT contain either
        data_08 = load_transactions(str(p1), '082024')
        invoices_08 = _get_invoice_numbers(data_08)
        assert 'INV-OTHER-RETURN-PERIOD' not in invoices_08
        assert 'INV-UNLINKED-RETURN-PERIOD' not in invoices_08
        assert invoices_08 == ['INV-PERIOD-082024-A']

        # Verify load_transactions for 112024 (empty period) does NOT contain either
        data_11 = load_transactions(str(p1), '112024')
        invoices_11 = _get_invoice_numbers(data_11)
        assert 'INV-OTHER-RETURN-PERIOD' not in invoices_11
        assert 'INV-UNLINKED-RETURN-PERIOD' not in invoices_11
        assert invoices_11 == []


def test_profile_tenant_isolation_with_period_query(app, period_isolation_env):
    """
    TEST E: PROFILE / TENANT ISOLATION
    User 1 and User 2 both have transactions for 082024.
    User 1 must see only their transactions.
    User 2 must see only their transactions.
    When User 2 queries 092024 (where User 1 has transactions but User 2 does not), User 2 gets empty result.
    """
    p1 = period_isolation_env['profile1_id']
    p2 = period_isolation_env['profile2_id']

    with app.app_context():
        # User 1 queries 082024
        data_u1 = load_transactions(str(p1), '082024')
        invoices_u1 = _get_invoice_numbers(data_u1)
        assert 'INV-PERIOD-082024-A' in invoices_u1
        assert 'INV-USER2-082024' not in invoices_u1

        # User 2 queries 082024
        data_u2 = load_transactions(str(p2), '082024')
        invoices_u2 = _get_invoice_numbers(data_u2)
        assert 'INV-USER2-082024' in invoices_u2
        assert 'INV-PERIOD-082024-A' not in invoices_u2

        # User 2 queries 092024 (User 1 has Invoice B in 092024, User 2 has none)
        data_u2_09 = load_transactions(str(p2), '092024')
        invoices_u2_09 = _get_invoice_numbers(data_u2_09)
        assert invoices_u2_09 == [], f"Cross-tenant leak into empty period: {invoices_u2_09}"
        assert 'INV-PERIOD-092024-B' not in invoices_u2_09


def test_soft_delete_preservation(app, period_isolation_env):
    """
    TEST F: SOFT-DELETE BEHAVIOR PRESERVED
    A soft-deleted transaction in 082024 must not appear in 082024 or any other period.
    """
    p1 = period_isolation_env['profile1_id']
    with app.app_context():
        data_08 = load_transactions(str(p1), '082024')
        invoices_08 = _get_invoice_numbers(data_08)
        assert 'INV-DELETED-082024' not in invoices_08
        assert 'INV-PERIOD-082024-A' in invoices_08
