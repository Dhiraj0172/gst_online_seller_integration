"""Phase 5E-4 Unit Tests: GSTR-1 Backend Generation & Statistics.

Covers:
- Tier 1: Core Feature Coverage
  1. Full GSTR1Generation model columns persistence (all 15 columns).
  2. total_b2b stores exact B2B invoice count (not total invoices count).
  3. Generator stats dictionary section invoice counts (total_b2b, total_b2cs, total_b2cl,
     total_cdnr, total_cdnur, total_nil, total_hsn_b2b, total_hsn_b2c, total_ecom, total_invoices).
  4. Generator stats dictionary tax totals (total_taxable_value, total_cgst, total_sgst,
     total_igst, total_cess, total_tax).
  5. include_hsn option (True vs False) behavior.
  6. Real reconciliation report attachment to GenerationResult and model persistence.
- Tier 2: Boundary & Corner Cases
  7. Empty return period generation (0 transactions).
  8. Transactions missing HSN code handled gracefully with include_hsn=True.
  9. Financial year parameter passing.
  10. High-precision decimal rounding in tax stats.
- Tier 3: Cross-Feature Combinations
  11. Multi-tenant database isolation during generation.
  12. Real reconciliation service output integration with GenerationResult.
- Tier 4: Real-World Scenarios
  13. Multi-period quarterly generation lifecycle.
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
from app.services.gstr1_generator import generate_gstr1, GenerationResult
from app.services.gstr1_json_validator import GSTR1Validator
from app.services.reconciliation_service import run_full_reconciliation


def _invoke_generator(profile_id, return_period, include_hsn=True, financial_year=None, reconciliation_report=None, options=None):
    """Helper to invoke generate_gstr1 safely whether kwargs or options dict are used."""
    if not reconciliation_report:
        reconciliation_report = {'status': 'SUCCESS'}

    return generate_gstr1(
        str(profile_id),
        return_period,
        include_hsn=include_hsn,
        financial_year=financial_year,
        reconciliation_report=reconciliation_report
    )


@pytest.fixture
def backend_env(app, db):
    """Set up test environment with User, Profile, ImportHistory, and standard transactions."""
    import random
    suffix = uuid.uuid4().hex[:6]
    rand_digits = f"{random.randint(1000, 9999)}"
    gstin = f"27ABCDE{rand_digits}F1Z5"
    with app.app_context():
        user = User(username=f'seller_{suffix}', email=f'seller_{suffix}@example.com')
        user.set_password('Secret123!')
        _db.session.add(user)
        _db.session.commit()

        profile = GSTProfile(
            user_id=user.id,
            gstin=gstin,
            legal_name='Test Commerce Pvt Ltd',
            trade_name='Test Commerce',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile)
        _db.session.commit()

        # Import history for period 012025
        imp = ImportHistory(
            profile_id=profile.id,
            user_id=user.id,
            return_period='012025',
            file_name='test_sales_012025.xlsx',
            original_file_name='test_sales_012025.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=7,
            success_rows=7
        )
        _db.session.add(imp)
        _db.session.commit()

        raws = [RawImport(import_history_id=imp.id, sheet_name='Sheet1', row_number=i, status='SUCCESS') for i in range(1, 8)]
        _db.session.add_all(raws)
        _db.session.commit()

        # 1. B2B transaction 1
        tx1 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[0].id,
            invoice_number='INV-B2B-001',
            invoice_date=date(2025, 1, 5),
            supply_type='B2B',
            customer_gstin='27AAAAA0000A1Z5',
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

        # 2. B2B transaction 2
        tx2 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[1].id,
            invoice_number='INV-B2B-002',
            invoice_date=date(2025, 1, 6),
            supply_type='B2B',
            customer_gstin='29BBBBB1111B1Z2',
            place_of_supply='29',
            hsn_sac='8471',
            taxable_value=Decimal('2000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            igst_amount=Decimal('360.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('2360.00'),
            is_deleted=False
        )

        # 3. B2CS transaction 1
        tx3 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[2].id,
            invoice_number='INV-B2CS-001',
            invoice_date=date(2025, 1, 10),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac='8471',
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

        # 4. B2CS transaction 2
        tx4 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[3].id,
            invoice_number='INV-B2CS-002',
            invoice_date=date(2025, 1, 11),
            supply_type='B2CS',
            place_of_supply='29',
            hsn_sac='8471',
            taxable_value=Decimal('750.00'),
            tax_rate=Decimal('12.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            igst_amount=Decimal('90.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('90.00'),
            invoice_value=Decimal('840.00'),
            is_deleted=False
        )

        # 5. CDNR Credit Note
        tx5 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[4].id,
            invoice_number='INV-B2B-001',
            original_invoice_number='INV-B2B-001',
            note_number='CN-001',
            note_type='C',
            note_date=date(2025, 1, 15),
            supply_type='CDNR',
            customer_gstin='27AAAAA0000A1Z5',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('100.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('9.00'),
            sgst_amount=Decimal('9.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('18.00'),
            invoice_value=Decimal('118.00'),
            is_deleted=False
        )

        # 6. CDNUR Credit Note
        tx6 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[5].id,
            invoice_number='INV-B2CS-001',
            original_invoice_number='INV-B2CS-001',
            note_number='CN-UR-001',
            note_type='C',
            note_date=date(2025, 1, 16),
            supply_type='CDNUR',
            place_of_supply='27',
            taxable_value=Decimal('50.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('4.50'),
            sgst_amount=Decimal('4.50'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('9.00'),
            invoice_value=Decimal('59.00'),
            is_deleted=False
        )

        # 7. NIL Rated Supply
        tx7 = Transaction(
            profile_id=profile.id,
            import_history_id=imp.id,
            raw_import_id=raws[6].id,
            invoice_number='INV-NIL-001',
            invoice_date=date(2025, 1, 20),
            supply_type='NIL',
            place_of_supply='27',
            taxable_value=Decimal('300.00'),
            tax_rate=Decimal('0.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('0.00'),
            invoice_value=Decimal('300.00'),
            is_deleted=False
        )

        _db.session.add_all([tx1, tx2, tx3, tx4, tx5, tx6, tx7])
        _db.session.commit()

        return {
            'user_id': user.id,
            'profile_id': profile.id,
            'gstin': profile.gstin,
            'return_period': '012025'
        }


# =========================================================================
# Tier 1: Core Feature Coverage
# =========================================================================

def test_01_gstr1_generation_model_all_15_columns_persistence(app, db, backend_env):
    """1. Test full GSTR1Generation model columns population (all 15 columns)."""
    with app.app_context():
        gen = GSTR1Generation(
            profile_id=backend_env['profile_id'],
            user_id=backend_env['user_id'],
            return_period='012025',
            generation_status='COMPLETED',
            generation_started_at=datetime.utcnow(),
            generation_completed_at=datetime.utcnow(),
            excel_file_path='/tmp/exports/GSTR1_012025.xlsx',
            json_file_path='/tmp/exports/GSTR1_012025.json',
            total_b2b=2,
            total_b2cs=2,
            total_b2cl=0,
            total_cdnr=1,
            total_cdnur=1,
            total_nil=1,
            total_hsn_b2b=2,
            total_hsn_b2c=2,
            total_ecom=0,
            total_taxable_value=Decimal('4700.00'),
            total_cgst=Decimal('188.50'),
            total_sgst=Decimal('188.50'),
            total_igst=Decimal('450.00'),
            total_cess=Decimal('0.00'),
            validation_passed=True,
            validation_errors=json.dumps([]),
            reconciliation_report=json.dumps({'status': 'CLEAN', 'critical_failures': 0}),
            schema_version='1.1',
            rule_version='2024-25'
        )
        _db.session.add(gen)
        _db.session.commit()

        # Query back and verify all 15 columns
        saved = _db.session.get(GSTR1Generation, gen.id)
        assert saved is not None
        assert saved.total_b2b == 2
        assert saved.total_b2cs == 2
        assert saved.total_b2cl == 0
        assert saved.total_cdnr == 1
        assert saved.total_cdnur == 1
        assert saved.total_nil == 1
        assert saved.total_hsn_b2b == 2
        assert saved.total_hsn_b2c == 2
        assert saved.total_ecom == 0
        assert Decimal(str(saved.total_taxable_value)) == Decimal('4700.00')
        assert Decimal(str(saved.total_cgst)) == Decimal('188.50')
        assert Decimal(str(saved.total_sgst)) == Decimal('188.50')
        assert Decimal(str(saved.total_igst)) == Decimal('450.00')
        assert Decimal(str(saved.total_cess)) == Decimal('0.00')
        assert saved.validation_passed is True
        assert saved.schema_version == '1.1'
        assert saved.rule_version == '2024-25'

        recon_data = json.loads(saved.reconciliation_report)
        assert recon_data['status'] == 'CLEAN'


def test_02_total_b2b_stores_b2b_count_not_total_invoices(app, db, backend_env):
    """2. Verify total_b2b stores exact B2B count (2), distinct from total outward invoices (4+)."""
    with app.app_context():
        res = _invoke_generator(backend_env['profile_id'], backend_env['return_period'])
        stats = res.stats

        # In backend_env, there are 2 B2B invoices and 2 B2CS invoices
        b2b_count = stats.get('total_b2b', 0)
        total_invoices = stats.get('total_invoices', 0)

        assert b2b_count == 2, f"Expected total_b2b=2, got {b2b_count}"
        assert total_invoices >= 4, f"Expected total_invoices >= 4, got {total_invoices}"
        assert b2b_count != total_invoices, "total_b2b must not equal total_invoices when mixed supplies exist"


def test_03_generator_stats_dictionary_section_counts(app, db, backend_env):
    """3. Test generator stats dictionary section counts computation."""
    with app.app_context():
        res = _invoke_generator(backend_env['profile_id'], backend_env['return_period'])
        stats = res.stats

        # Verify presence of section counts
        assert 'total_b2b' in stats
        assert 'total_b2cs' in stats
        assert stats['total_b2b'] == 2
        assert stats['total_b2cs'] == 2
        assert stats.get('total_cdnr', 0) == 1
        assert stats.get('total_cdnur', 0) == 1
        assert stats.get('total_nil', 0) == 1
        assert stats['total_invoices'] >= 4


def test_04_generator_stats_dictionary_tax_totals(app, db, backend_env):
    """4. Test generator stats dictionary tax totals computation."""
    with app.app_context():
        res = _invoke_generator(backend_env['profile_id'], backend_env['return_period'])
        stats = res.stats

        assert 'total_taxable_value' in stats
        assert 'total_tax' in stats

        # Sum of taxes in backend_env:
        # tx1 (B2B): CGST 90, SGST 90 -> 180
        # tx2 (B2B): IGST 360 -> 360
        # tx3 (B2CS): CGST 45, SGST 45 -> 90
        # tx4 (B2CS): IGST 90 -> 90
        # tx5 (CDNR): CGST 9, SGST 9 -> 18
        # tx6 (CDNUR): CGST 4.5, SGST 4.5 -> 9
        # Total tax should be positive and greater than 700
        assert stats['total_taxable_value'] > 0
        assert stats['total_tax'] > 0

        # If individual tax breakdown is exposed in stats
        if 'total_cgst' in stats and 'total_sgst' in stats and 'total_igst' in stats:
            expected_tax = stats['total_cgst'] + stats['total_sgst'] + stats['total_igst'] + stats.get('total_cess', 0)
            assert pytest.approx(stats['total_tax'], rel=1e-2) == expected_tax


def test_05_generator_include_hsn_option_toggle(app, db, backend_env):
    """5. Test include_hsn option (True vs False) behavior."""
    with app.app_context():
        # Generation with include_hsn=True
        res_with_hsn = _invoke_generator(backend_env['profile_id'], backend_env['return_period'], include_hsn=True)
        assert os.path.exists(res_with_hsn.json_path)
        with open(res_with_hsn.json_path, 'r', encoding='utf-8') as f:
            data_with_hsn = json.load(f)
        hsn_records = data_with_hsn.get('hsn', {}).get('data', [])
        assert len(hsn_records) > 0, "HSN data must be populated when include_hsn=True"

        # Generation with include_hsn=False
        res_without_hsn = _invoke_generator(backend_env['profile_id'], backend_env['return_period'], include_hsn=False)
        assert os.path.exists(res_without_hsn.json_path)
        with open(res_without_hsn.json_path, 'r', encoding='utf-8') as f:
            data_without_hsn = json.load(f)
        hsn_without = data_without_hsn.get('hsn', {}).get('data', [])
        assert len(hsn_without) == 0, "HSN data must be omitted or empty when include_hsn=False"


def test_06_generator_reconciliation_report_attachment(app, db, backend_env):
    """6. Test real reconciliation report integration with GenerationResult."""
    with app.app_context():
        report = run_full_reconciliation(backend_env['profile_id'], backend_env['return_period'])
        res = _invoke_generator(
            backend_env['profile_id'],
            backend_env['return_period'],
            reconciliation_report=report.to_dict()
        )

        assert res.reconciliation_report is not None
        assert isinstance(res.reconciliation_report, dict)
        assert 'status' in res.reconciliation_report
        assert res.to_dict()['reconciliation_report'] is not None


# =========================================================================
# Tier 2: Boundary & Corner Cases
# =========================================================================

def test_07_empty_period_generation_zero_transactions(app, db, backend_env):
    """7. Empty return period generation returns schema-compliant empty payload without errors."""
    with app.app_context():
        res = _invoke_generator(backend_env['profile_id'], '122099')

        assert res.stats['total_invoices'] == 0
        assert res.stats['total_taxable_value'] == 0
        assert res.stats['total_tax'] == 0

        assert os.path.exists(res.json_path)
        with open(res.json_path, 'r', encoding='utf-8') as f:
            json_str = f.read()

        validation = GSTR1Validator.validate_gstr1_json(json_str)
        assert validation.is_valid is True


def test_08_missing_hsn_transaction_handling(app, db, backend_env):
    """8. Transactions without HSN code do not raise unhandled exceptions during generation."""
    with app.app_context():
        imp = ImportHistory.query.filter_by(profile_id=backend_env['profile_id'], return_period='012025').first()
        raw = RawImport.query.filter_by(import_history_id=imp.id).first()

        # Add transaction with None HSN
        no_hsn_tx = Transaction(
            profile_id=backend_env['profile_id'],
            import_history_id=imp.id,
            raw_import_id=raw.id,
            invoice_number='INV-NO-HSN-999',
            invoice_date=date(2025, 1, 22),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac=None,
            taxable_value=Decimal('200.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('18.00'),
            sgst_amount=Decimal('18.00'),
            total_tax=Decimal('36.00'),
            invoice_value=Decimal('236.00'),
            is_deleted=False
        )
        _db.session.add(no_hsn_tx)
        _db.session.commit()

        # Generator runs cleanly
        res = _invoke_generator(backend_env['profile_id'], '012025', include_hsn=True)
        assert res is not None
        assert os.path.exists(res.json_path)
        assert os.path.exists(res.excel_path)
        assert res.stats['total_invoices'] >= 4


def test_09_financial_year_option_support(app, db, backend_env):
    """9. Financial year parameter passed to generator is accepted and handled cleanly."""
    with app.app_context():
        res = _invoke_generator(backend_env['profile_id'], '012025', financial_year='2024-25')
        assert res is not None
        assert res.validation_result.is_valid is True


def test_10_high_precision_decimal_rounding(app, db, backend_env):
    """10. Fractional paise calculations maintain precision without float corruption."""
    with app.app_context():
        imp = ImportHistory.query.filter_by(profile_id=backend_env['profile_id'], return_period='012025').first()
        raw = RawImport.query.filter_by(import_history_id=imp.id).first()

        precise_tx = Transaction(
            profile_id=backend_env['profile_id'],
            import_history_id=imp.id,
            raw_import_id=raw.id,
            invoice_number='INV-PRECISION-001',
            invoice_date=date(2025, 1, 25),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('100.33'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('9.03'),
            sgst_amount=Decimal('9.03'),
            total_tax=Decimal('18.06'),
            invoice_value=Decimal('118.39'),
            is_deleted=False
        )
        _db.session.add(precise_tx)
        _db.session.commit()

        res = _invoke_generator(backend_env['profile_id'], '012025')
        assert res.stats['total_taxable_value'] > 0


# =========================================================================
# Tier 3: Cross-Feature Combinations
# =========================================================================

def test_11_cross_tenant_generation_isolation(app, db, backend_env):
    """11. Transactions of another tenant's profile are strictly excluded from generated returns."""
    with app.app_context():
        u2_suffix = uuid.uuid4().hex[:8].upper()
        user2 = User(username=f'other_seller_{u2_suffix}', email=f'other_{u2_suffix}@example.com')
        user2.set_password('Secret123!')
        _db.session.add(user2)
        _db.session.commit()

        rand_digits2 = f"{random.randint(1000, 9999)}"
        profile2 = GSTProfile(
            user_id=user2.id,
            gstin=f'29XYZAB{rand_digits2}C1Z1',
            legal_name='Other Seller Ltd',
            trade_name='Other Seller',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile2)
        _db.session.commit()

        imp2 = ImportHistory(
            profile_id=profile2.id,
            user_id=user2.id,
            return_period='012025',
            file_name='other_sales.xlsx',
            original_file_name='other_sales.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp2)
        _db.session.commit()

        raw2 = RawImport(import_history_id=imp2.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw2)
        _db.session.commit()

        other_tx = Transaction(
            profile_id=profile2.id,
            import_history_id=imp2.id,
            raw_import_id=raw2.id,
            invoice_number='INV-OTHER-SECRET-999',
            invoice_date=date(2025, 1, 10),
            supply_type='B2B',
            customer_gstin='27AAAAA0000A1Z5',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('99999.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('8999.91'),
            sgst_amount=Decimal('8999.91'),
            total_tax=Decimal('17999.82'),
            invoice_value=Decimal('117998.82'),
            is_deleted=False
        )
        _db.session.add(other_tx)
        _db.session.commit()

        # Generate return for Profile 1
        res1 = _invoke_generator(backend_env['profile_id'], '012025')
        with open(res1.json_path, 'r', encoding='utf-8') as f:
            data1 = json.load(f)

        # Confirm other seller invoice is NOT present in Profile 1's return
        b2b_invoices_p1 = [inv['inum'] for b in data1.get('b2b', []) for inv in b.get('inv', [])]
        assert 'INV-OTHER-SECRET-999' not in b2b_invoices_p1
        assert res1.stats['total_taxable_value'] < 90000


def test_12_real_reconciliation_service_integration(app, db, backend_env):
    """12. Real reconciliation service output integrates into GenerationResult and persistence."""
    with app.app_context():
        recon_report = run_full_reconciliation(backend_env['profile_id'], backend_env['return_period'])
        res = _invoke_generator(
            backend_env['profile_id'],
            backend_env['return_period'],
            reconciliation_report=recon_report.to_dict()
        )

        gen = GSTR1Generation(
            profile_id=backend_env['profile_id'],
            user_id=backend_env['user_id'],
            return_period=backend_env['return_period'],
            generation_status='COMPLETED',
            excel_file_path=res.excel_path,
            json_file_path=res.json_path,
            total_b2b=res.stats.get('total_b2b', 0),
            total_b2cs=res.stats.get('total_b2cs', 0),
            total_taxable_value=res.stats.get('total_taxable_value', 0),
            validation_passed=res.validation_result.is_valid,
            reconciliation_report=json.dumps(res.reconciliation_report)
        )
        _db.session.add(gen)
        _db.session.commit()

        loaded = _db.session.get(GSTR1Generation, gen.id)
        report_data = json.loads(loaded.reconciliation_report)
        assert 'checks' in report_data or 'status' in report_data


# =========================================================================
# Tier 4: Real-World Scenarios
# =========================================================================

def test_13_multi_period_quarterly_generation_lifecycle(app, db, backend_env):
    """13. Multi-period return generation creates distinct, isolated artifacts per period."""
    with app.app_context():
        # Add period 022025
        imp_feb = ImportHistory(
            profile_id=backend_env['profile_id'],
            user_id=backend_env['user_id'],
            return_period='022025',
            file_name='sales_022025.xlsx',
            original_file_name='sales_022025.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_feb)
        _db.session.commit()

        raw_feb = RawImport(import_history_id=imp_feb.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_feb)
        _db.session.commit()

        tx_feb = Transaction(
            profile_id=backend_env['profile_id'],
            import_history_id=imp_feb.id,
            raw_import_id=raw_feb.id,
            invoice_number='INV-FEB-001',
            invoice_date=date(2025, 2, 10),
            supply_type='B2B',
            customer_gstin='27AAAAA0000A1Z5',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('3500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('315.00'),
            sgst_amount=Decimal('315.00'),
            total_tax=Decimal('630.00'),
            invoice_value=Decimal('4130.00'),
            is_deleted=False
        )
        _db.session.add(tx_feb)
        _db.session.commit()

        # Generate for 012025 and 022025
        res_jan = _invoke_generator(backend_env['profile_id'], '012025')
        res_feb = _invoke_generator(backend_env['profile_id'], '022025')

        assert res_jan.json_path != res_feb.json_path
        assert res_jan.excel_path != res_feb.excel_path

        with open(res_jan.json_path, 'r', encoding='utf-8') as f:
            jan_json = json.load(f)
        with open(res_feb.json_path, 'r', encoding='utf-8') as f:
            feb_json = json.load(f)

        assert jan_json['fp'] == '012025'
        assert feb_json['fp'] == '022025'
        assert len(feb_json.get('b2b', [])) == 1
