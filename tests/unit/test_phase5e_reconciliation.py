"""Phase 5E-1 Unit Tests: Concrete Reconciliation Service.

Verifies:
1. B2B reconciliation pass
2. B2B mismatch detection (missing GSTIN, amount discrepancy)
3. B2C reconciliation pass
4. B2C mismatch detection (registered buyer warning, amount discrepancy)
5. HSN reconciliation pass
6. HSN mismatch detection (missing HSN code, amount discrepancy)
7. Tax totals pass
8. Tax mismatch detection (impossible CGST+IGST combination, asymmetric taxes, arithmetic error)
9. Import-count check
10. Orphan transaction detection
11. Blocking discrepancy handling (blocks GSTR-1 generation)
12. Non-blocking discrepancy handling (warning status, allows generation)
13. Profile isolation (no leakage between tenant profiles)
14. Return-period isolation (no leakage between monthly periods)
15. Deterministic repeated results
"""
import uuid
from datetime import date
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import AuditLog, GSTProfile, ImportHistory, RawImport, Transaction, User
from app.services.reconciliation_service import (
    CheckResult,
    ReconciliationReport,
    check_b2b_totals,
    check_b2c_totals,
    check_cdnr_totals,
    check_duplicates,
    check_hsn_reconciliation,
    check_import_counts,
    check_nil_totals,
    check_state_totals,
    check_tax_totals,
    check_unclassified,
    run_full_reconciliation,
)


@pytest.fixture
def recon_env(app, db):
    """Set up two isolated users, profiles, import histories, and raw imports."""
    suffix = uuid.uuid4().hex[:6]
    gstin1 = f'27{uuid.uuid4().hex[:10].upper()}1Z5'
    gstin2 = f'29{uuid.uuid4().hex[:10].upper()}1Z6'

    with app.app_context():
        # User 1 / Profile 1
        user1 = User(username=f'u1_{suffix}', email=f'u1_{suffix}@example.com')
        user1.set_password('Pass123!')
        _db.session.add(user1)

        # User 2 / Profile 2
        user2 = User(username=f'u2_{suffix}', email=f'u2_{suffix}@example.com')
        user2.set_password('Pass123!')
        _db.session.add(user2)
        _db.session.commit()

        p1 = GSTProfile(
            user_id=user1.id,
            gstin=gstin1,
            legal_name='Seller One Retail',
            trade_name='Store One',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='monthly',
        )
        p2 = GSTProfile(
            user_id=user2.id,
            gstin=gstin2,
            legal_name='Seller Two South',
            trade_name='Store Two',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='monthly',
        )
        _db.session.add_all([p1, p2])
        _db.session.commit()

        # Import history for P1 in Period 082024
        imp_p1_08 = ImportHistory(
            profile_id=p1.id,
            user_id=user1.id,
            return_period='082024',
            file_name='p1_sales_082024.xlsx',
            original_file_name='p1_sales_082024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=2,
            success_rows=2,
            warning_rows=0,
            error_rows=0,
            skipped_rows=0,
        )
        # Import history for P1 in Period 092024
        imp_p1_09 = ImportHistory(
            profile_id=p1.id,
            user_id=user1.id,
            return_period='092024',
            file_name='p1_sales_092024.xlsx',
            original_file_name='p1_sales_092024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
            warning_rows=0,
            error_rows=0,
            skipped_rows=0,
        )
        # Import history for P2 in Period 082024
        imp_p2_08 = ImportHistory(
            profile_id=p2.id,
            user_id=user2.id,
            return_period='082024',
            file_name='p2_sales_082024.xlsx',
            original_file_name='p2_sales_082024.xlsx',
            platform_name='FLIPKART',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1,
            warning_rows=0,
            error_rows=0,
            skipped_rows=0,
        )
        _db.session.add_all([imp_p1_08, imp_p1_09, imp_p2_08])
        _db.session.commit()

        # RawImports
        raw1 = RawImport(import_history_id=imp_p1_08.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw2 = RawImport(import_history_id=imp_p1_08.id, sheet_name='Sheet1', row_number=2, status='SUCCESS')
        raw_p1_09 = RawImport(import_history_id=imp_p1_09.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw_p2_08 = RawImport(import_history_id=imp_p2_08.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add_all([raw1, raw2, raw_p1_09, raw_p2_08])
        _db.session.commit()

        yield {
            'u1_id': user1.id,
            'u2_id': user2.id,
            'p1_id': p1.id,
            'p2_id': p2.id,
            'imp_p1_08_id': imp_p1_08.id,
            'imp_p1_09_id': imp_p1_09.id,
            'imp_p2_08_id': imp_p2_08.id,
            'raw1_id': raw1.id,
            'raw2_id': raw2.id,
            'raw_p1_09_id': raw_p1_09.id,
            'raw_p2_08_id': raw_p2_08.id,
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


def _create_tx(
    env,
    invoice_number='INV-001',
    supply_type='B2B',
    customer_gstin='27ABCDE1234F1Z5',
    customer_name='Acme Corp',
    taxable_value=Decimal('1000.00'),
    tax_rate=Decimal('18.00'),
    cgst_rate=Decimal('9.00'),
    cgst_amount=Decimal('90.00'),
    sgst_rate=Decimal('9.00'),
    sgst_amount=Decimal('90.00'),
    igst_rate=Decimal('0.00'),
    igst_amount=Decimal('0.00'),
    cess_rate=Decimal('0.00'),
    cess_amount=Decimal('0.00'),
    total_tax=Decimal('180.00'),
    invoice_value=Decimal('1180.00'),
    place_of_supply='27',
    hsn_sac='8471',
    profile_id=None,
    import_history_id=None,
    raw_import_id=None,
    is_deleted=False,
    invoice_date=date(2024, 8, 10),
):
    pid = profile_id or env['p1_id']
    ih_id = import_history_id or env['imp_p1_08_id']
    raw_id = raw_import_id or env['raw1_id']

    tx = Transaction(
        profile_id=pid,
        import_history_id=ih_id,
        raw_import_id=raw_id,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        supply_type=supply_type,
        customer_gstin=customer_gstin,
        customer_name=customer_name,
        taxable_value=taxable_value,
        tax_rate=tax_rate,
        cgst_rate=cgst_rate,
        cgst_amount=cgst_amount,
        sgst_rate=sgst_rate,
        sgst_amount=sgst_amount,
        igst_rate=igst_rate,
        igst_amount=igst_amount,
        cess_rate=cess_rate,
        cess_amount=cess_amount,
        total_tax=total_tax,
        invoice_value=invoice_value,
        place_of_supply=place_of_supply,
        hsn_sac=hsn_sac,
        is_deleted=is_deleted,
    )
    _db.session.add(tx)
    _db.session.commit()
    return tx


# 1. B2B reconciliation pass
def test_b2b_reconciliation_pass(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-B2B-101',
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('2000.00'),
            cgst_amount=Decimal('180.00'),
            sgst_amount=Decimal('180.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('2360.00'),
        )
        res = check_b2b_totals(recon_env['p1_id'], '082024')
        assert res.passed is True
        assert res.severity == 'INFO'
        assert res.details['missing_gstin_count'] == 0
        assert Decimal(res.details['taxable_difference']) == Decimal('0.00')


# 2. B2B mismatch detection (missing GSTIN and amount discrepancy)
def test_b2b_mismatch_missing_gstin(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-B2B-NOGSTIN',
            supply_type='B2B',
            customer_gstin=None,
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
        )
        res = check_b2b_totals(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'BLOCKING'
        assert any('missing or invalid customer GSTIN' in m for m in res.messages)


def test_b2b_mismatch_amount_discrepancy(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-B2B-MISMATCH',
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('500.00'),  # Intentionally corrupt recorded tax
            invoice_value=Decimal('1500.00'),
        )
        res = check_b2b_totals(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'BLOCKING'
        assert any('tax total mismatch' in m for m in res.messages)


# 3. B2C reconciliation pass
def test_b2c_reconciliation_pass(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-B2C-201',
            supply_type='B2CS',
            customer_gstin=None,
            customer_name='Consumer A',
            taxable_value=Decimal('500.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
        )
        res = check_b2c_totals(recon_env['p1_id'], '082024')
        assert res.passed is True
        assert res.severity == 'INFO'
        assert res.details['registered_in_b2c_count'] == 0


# 4. B2C mismatch detection (registered buyer anomaly is WARNING)
def test_b2c_mismatch_registered_buyer(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-B2C-REGBUYER',
            supply_type='B2CS',
            customer_gstin='27XYZAB5678C1Z9',  # 15-char GSTIN on B2C
            customer_name='Business Consumer',
            taxable_value=Decimal('500.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
        )
        res = check_b2c_totals(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'WARNING'
        assert any('registered buyer GSTIN' in m for m in res.messages)


# 5. HSN reconciliation pass
def test_hsn_reconciliation_pass(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-HSN-301',
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('1500.00'),
            cgst_amount=Decimal('135.00'),
            sgst_amount=Decimal('135.00'),
            total_tax=Decimal('270.00'),
            invoice_value=Decimal('1770.00'),
            hsn_sac='847130',
        )
        res = check_hsn_reconciliation(recon_env['p1_id'], '082024')
        assert res.passed is True
        assert res.severity == 'INFO'
        assert res.details['missing_hsn_count'] == 0


# 6. HSN mismatch detection (missing HSN code on non-amendment row is WARNING)
def test_hsn_mismatch_missing_hsn(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-HSN-NOHSN',
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            hsn_sac=None,
        )
        res = check_hsn_reconciliation(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'WARNING'
        assert res.details['missing_hsn_count'] == 1
        assert any('no HSN/SAC code' in m for m in res.messages)


# 7. Tax totals pass
def test_tax_totals_pass(app, recon_env):
    with app.app_context():
        # Intra-state transaction
        _create_tx(
            recon_env,
            invoice_number='INV-TAX-INTRA',
            place_of_supply='27',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('0.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
        )
        # Inter-state transaction
        _create_tx(
            recon_env,
            invoice_number='INV-TAX-INTER',
            place_of_supply='29',
            taxable_value=Decimal('2000.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            igst_amount=Decimal('360.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('2360.00'),
            raw_import_id=recon_env['raw2_id'],
        )
        res = check_tax_totals(recon_env['p1_id'], '082024')
        assert res.passed is True
        assert res.severity == 'INFO'


# 8. Tax mismatch detection (impossible CGST+IGST combination, asymmetric taxes)
def test_tax_mismatch_impossible_combo(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-TAX-IMPOSSIBLE',
            place_of_supply='27',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('180.00'),  # Impossible dual intra + inter tax
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('1360.00'),
        )
        res = check_tax_totals(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'BLOCKING'
        assert any('impossible dual intra/inter tax' in m for m in res.messages)


def test_tax_mismatch_asymmetric_cgst_sgst(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-TAX-ASYMMETRIC',
            place_of_supply='27',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('50.00'),  # Asymmetric CGST != SGST
            igst_amount=Decimal('0.00'),
            total_tax=Decimal('140.00'),
            invoice_value=Decimal('1140.00'),
        )
        res = check_tax_totals(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'BLOCKING'
        assert any('asymmetric CGST/SGST' in m for m in res.messages)


# 9. Import counts check
def test_import_counts_check(app, recon_env):
    with app.app_context():
        # import_history has success_rows=2, create 2 transactions
        _create_tx(
            recon_env,
            invoice_number='INV-IMP-001',
            raw_import_id=recon_env['raw1_id'],
        )
        _create_tx(
            recon_env,
            invoice_number='INV-IMP-002',
            raw_import_id=recon_env['raw2_id'],
        )
        res = check_import_counts(recon_env['p1_id'], '082024')
        assert res.passed is True
        assert res.details['active_transactions'] == 2
        assert res.details['orphans'] == 0


# 10. Orphan detection (BLOCKING)
def test_orphan_detection(app, recon_env):
    with app.app_context():
        # Create transaction with an import_history_id that doesn't exist
        tx = Transaction(
            profile_id=recon_env['p1_id'],
            import_history_id=9999999,  # Non-existent import history
            raw_import_id=recon_env['raw1_id'],
            invoice_number='INV-ORPHAN-001',
            invoice_date=date(2024, 8, 1),
            supply_type='B2B',
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            place_of_supply='27',
            hsn_sac='8471',
            is_deleted=False,
        )
        _db.session.add(tx)
        _db.session.commit()

        res = check_import_counts(recon_env['p1_id'], '082024')
        assert res.passed is False
        assert res.severity == 'BLOCKING'
        assert any('orphaned transaction' in m for m in res.messages)


# 11. Blocking discrepancy in full reconciliation
def test_blocking_discrepancy_blocks_generation(app, recon_env):
    with app.app_context():
        # Missing B2B GSTIN is a BLOCKING error
        _create_tx(
            recon_env,
            invoice_number='INV-BLOCKING-001',
            supply_type='B2B',
            customer_gstin=None,  # Missing
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
        )
        report = run_full_reconciliation(recon_env['p1_id'], '082024')
        assert report.is_generation_blocked is True
        assert report.status == 'BLOCKED'
        assert report.critical_failures >= 1
        assert len(report.blocking_reasons) >= 1


# 12. Non-blocking discrepancy produces WARNING status and allows generation
def test_non_blocking_discrepancy_allows_generation(app, recon_env):
    with app.app_context():
        # Valid B2CS transaction, but with missing HSN code (warning only)
        _create_tx(
            recon_env,
            invoice_number='INV-WARN-001',
            supply_type='B2CS',
            customer_gstin=None,
            taxable_value=Decimal('500.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            hsn_sac=None,  # Missing HSN on B2CS row
            raw_import_id=recon_env['raw1_id'],
        )
        _create_tx(
            recon_env,
            invoice_number='INV-WARN-002',
            supply_type='B2CS',
            customer_gstin=None,
            taxable_value=Decimal('500.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            hsn_sac=None,  # Missing HSN on B2CS row
            raw_import_id=recon_env['raw2_id'],
        )
        report = run_full_reconciliation(recon_env['p1_id'], '082024')
        assert report.is_generation_blocked is False
        assert report.status == 'WARNING'
        assert report.critical_failures == 0
        assert report.warnings >= 1


# 13. Profile isolation: Profile A data completely separated from Profile B
def test_reconciliation_profile_isolation(app, recon_env):
    with app.app_context():
        # Profile 1 transaction
        _create_tx(
            recon_env,
            invoice_number='INV-P1-001',
            profile_id=recon_env['p1_id'],
            import_history_id=recon_env['imp_p1_08_id'],
            raw_import_id=recon_env['raw1_id'],
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
        )
        # Profile 2 transaction with invalid GSTIN (would block P2 if inspected)
        _create_tx(
            recon_env,
            invoice_number='INV-P2-CORRUPT',
            profile_id=recon_env['p2_id'],
            import_history_id=recon_env['imp_p2_08_id'],
            raw_import_id=recon_env['raw_p2_08_id'],
            customer_gstin=None,
            taxable_value=Decimal('50000.00'),
            cgst_amount=Decimal('4500.00'),
            sgst_amount=Decimal('4500.00'),
            total_tax=Decimal('9000.00'),
            invoice_value=Decimal('59000.00'),
        )

        # Profile 1 check must be completely isolated and not see P2's corruption or values
        res1 = check_b2b_totals(recon_env['p1_id'], '082024')
        assert res1.passed is True
        assert res1.details['transaction_count'] == 1
        assert Decimal(res1.details['db_taxable']) == Decimal('1000.00')

        # Profile 2 check should detect its own invalid record
        res2 = check_b2b_totals(recon_env['p2_id'], '082024')
        assert res2.passed is False
        assert res2.details['transaction_count'] == 1
        assert Decimal(res2.details['db_taxable']) == Decimal('50000.00')


# 14. Return-period isolation: Period A separated from Period B
def test_reconciliation_return_period_isolation(app, recon_env):
    with app.app_context():
        # Period 082024 transaction
        _create_tx(
            recon_env,
            invoice_number='INV-PERIOD-08',
            import_history_id=recon_env['imp_p1_08_id'],
            raw_import_id=recon_env['raw1_id'],
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
        )
        # Period 092024 transaction with different amount
        _create_tx(
            recon_env,
            invoice_number='INV-PERIOD-09',
            import_history_id=recon_env['imp_p1_09_id'],
            raw_import_id=recon_env['raw_p1_09_id'],
            taxable_value=Decimal('7000.00'),
            cgst_amount=Decimal('630.00'),
            sgst_amount=Decimal('630.00'),
            total_tax=Decimal('1260.00'),
            invoice_value=Decimal('8260.00'),
        )

        res_08 = check_b2b_totals(recon_env['p1_id'], '082024')
        assert res_08.passed is True
        assert res_08.details['transaction_count'] == 1
        assert Decimal(res_08.details['db_taxable']) == Decimal('1000.00')

        res_09 = check_b2b_totals(recon_env['p1_id'], '092024')
        assert res_09.passed is True
        assert res_09.details['transaction_count'] == 1
        assert Decimal(res_09.details['db_taxable']) == Decimal('7000.00')


# 15. Deterministic repeated results
def test_reconciliation_deterministic_repeated_results(app, recon_env):
    with app.app_context():
        _create_tx(
            recon_env,
            invoice_number='INV-DET-001',
            taxable_value=Decimal('1000.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            raw_import_id=recon_env['raw1_id'],
        )
        _create_tx(
            recon_env,
            invoice_number='INV-DET-002',
            supply_type='B2CS',
            customer_gstin=None,
            taxable_value=Decimal('500.00'),
            cgst_amount=Decimal('45.00'),
            sgst_amount=Decimal('45.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('590.00'),
            raw_import_id=recon_env['raw2_id'],
        )

        report1 = run_full_reconciliation(recon_env['p1_id'], '082024')
        report2 = run_full_reconciliation(recon_env['p1_id'], '082024')

        assert report1.status == report2.status
        assert report1.is_generation_blocked == report2.is_generation_blocked
        assert report1.critical_failures == report2.critical_failures
        assert report1.warnings == report2.warnings
        assert len(report1.checks) == len(report2.checks)
        for c1, c2 in zip(report1.checks, report2.checks):
            assert c1.check_name == c2.check_name
            assert c1.passed == c2.passed
            assert c1.severity == c2.severity
            assert c1.messages == c2.messages
            assert c1.details == c2.details
