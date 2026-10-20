"""Integration test suite for Phase 6C, 6D & 6E:
GSTR-3B Auto-Draft Bridge, Rule 88A Tax Offset Priority, Cross-Period Credit Notes,
Table 9 Amendments, Zero-Loss Schema Migration Engine & Turnkey Production Deployment.
"""
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
import pytest
from sqlalchemy import create_engine, inspect, text

from app.extensions import db as _db
from app.models import (
    User,
    GSTProfile,
    ImportHistory,
    RawImport,
    Transaction,
    GSTR1Generation,
    TCSReconciliation,
    ReconciliationRecord,
)
from app.services.gstr3b_service import (
    calculate_gst_liability_offset,
    get_gstr3b_table_3_1,
    get_section_52_tcs_summary,
    generate_gstr3b_summary,
    to_dec,
)
from app.services.cdnr_service import link_cross_period_credit_notes
from app.services.gstr1_generator import generate_gstr1
from app.services.migration_service import (
    inspect_schema_drift,
    apply_schema_migrations,
    CANONICAL_COLUMNS,
    CANONICAL_TABLES,
)


@pytest.fixture
def phase6cde_env(app, db):
    """Set up two tenants with users and profiles for Phase 6CDE tests."""
    with app.app_context():
        # Tenant A
        user_a = User(
            username=f'p6cde_user_a_{uuid.uuid4().hex[:6]}',
            email=f'p6cde_a_{uuid.uuid4().hex[:6]}@example.com',
        )
        user_a.set_password('TenantAPass123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=f'27AAAAA{uuid.uuid4().hex[:4].upper()}1Z5',
            legal_name='Tenant A Enterprises Ltd',
            trade_name='Tenant A Retail',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B (for cross-tenant IDOR checks)
        user_b = User(
            username=f'p6cde_user_b_{uuid.uuid4().hex[:6]}',
            email=f'p6cde_b_{uuid.uuid4().hex[:6]}@example.com',
        )
        user_b.set_password('TenantBPass456!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=f'29BBBBB{uuid.uuid4().hex[:4].upper()}1Z8',
            legal_name='Tenant B Corp',
            trade_name='Tenant B Cloud',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(profile_b)
        _db.session.commit()

        return {
            'user_a': user_a,
            'user_b': user_b,
            'user_a_id': user_a.id,
            'user_b_id': user_b.id,
            'profile_a_id': profile_a.id,
            'profile_b_id': profile_b.id,
            'gstin_a': profile_a.gstin,
            'gstin_b': profile_b.gstin,
        }


# ==============================================================================
# 1. Rule 88A / Section 49 Tax Liability Offset Priority Tests (Pure Decimal)
# ==============================================================================

def test_rule_88a_igst_full_exhaustion_first():
    """Verify Rule 88A exhausts IGST ITC against IGST first, then CGST, then SGST."""
    liability = {'igst': '500.00', 'cgst': '300.00', 'sgst': '300.00', 'cess': '0.00'}
    itc = {'igst': '700.00', 'cgst': '100.00', 'sgst': '100.00', 'cess': '0.00'}

    res = calculate_gst_liability_offset(liability, itc)

    # 500 IGST ITC to IGST liability -> 200 IGST ITC remaining
    # 200 IGST ITC to CGST liability -> CGST liability remaining is 100
    # CGST ITC of 100 offsets remaining 100 CGST liability -> CGST liability 0
    # SGST ITC of 100 offsets 100 of SGST liability -> SGST liability remaining 200
    assert res['offset_applied']['igst_from_igst'] == Decimal('500.00')
    assert res['offset_applied']['cgst_from_igst'] == Decimal('200.00')
    assert res['offset_applied']['sgst_from_igst'] == Decimal('0.00')
    assert res['offset_applied']['cgst_from_cgst'] == Decimal('100.00')
    assert res['offset_applied']['sgst_from_sgst'] == Decimal('100.00')

    assert res['liability_payable_cash']['igst'] == Decimal('0.00')
    assert res['liability_payable_cash']['cgst'] == Decimal('0.00')
    assert res['liability_payable_cash']['sgst'] == Decimal('200.00')
    assert res['itc_balance_remaining']['igst'] == Decimal('0.00')
    assert res['itc_balance_remaining']['cgst'] == Decimal('0.00')
    assert res['itc_balance_remaining']['sgst'] == Decimal('0.00')


def test_rule_88a_cgst_cross_utilization_never_sgst():
    """Verify CGST ITC offsets CGST, then remaining IGST, but strictly NEVER SGST."""
    liability = {'igst': '200.00', 'cgst': '100.00', 'sgst': '300.00', 'cess': '0.00'}
    itc = {'igst': '0.00', 'cgst': '500.00', 'sgst': '0.00', 'cess': '0.00'}

    res = calculate_gst_liability_offset(liability, itc)

    # CGST ITC offsets CGST liability (100)
    assert res['offset_applied']['cgst_from_cgst'] == Decimal('100.00')
    # Remaining CGST ITC (400) offsets remaining IGST liability (200)
    assert res['offset_applied']['igst_from_cgst'] == Decimal('200.00')
    # CGST ITC strictly NEVER offsets SGST liability
    assert res['offset_applied']['sgst_from_cgst'] == Decimal('0.00')

    # SGST liability is untouched and must be paid in cash
    assert res['liability_payable_cash']['sgst'] == Decimal('300.00')
    assert res['liability_payable_cash']['cgst'] == Decimal('0.00')
    assert res['liability_payable_cash']['igst'] == Decimal('0.00')

    # Excess CGST ITC (200) carries forward
    assert res['itc_balance_remaining']['cgst'] == Decimal('200.00')


def test_rule_88a_sgst_cross_utilization_never_cgst():
    """Verify SGST ITC offsets SGST, then remaining IGST, but strictly NEVER CGST."""
    liability = {'igst': '150.00', 'cgst': '250.00', 'sgst': '100.00', 'cess': '0.00'}
    itc = {'igst': '0.00', 'cgst': '0.00', 'sgst': '400.00', 'cess': '0.00'}

    res = calculate_gst_liability_offset(liability, itc)

    # SGST ITC offsets SGST liability (100)
    assert res['offset_applied']['sgst_from_sgst'] == Decimal('100.00')
    # Remaining SGST ITC (300) offsets remaining IGST liability (150)
    assert res['offset_applied']['igst_from_sgst'] == Decimal('150.00')
    # SGST ITC strictly NEVER offsets CGST liability
    assert res['offset_applied']['cgst_from_sgst'] == Decimal('0.00')

    # CGST liability is untouched and must be paid in cash
    assert res['liability_payable_cash']['cgst'] == Decimal('250.00')
    assert res['liability_payable_cash']['sgst'] == Decimal('0.00')
    assert res['liability_payable_cash']['igst'] == Decimal('0.00')

    # Excess SGST ITC (150) carries forward
    assert res['itc_balance_remaining']['sgst'] == Decimal('150.00')


def test_rule_88a_cess_isolated_offset():
    """Verify Cess ITC offsets Cess liability only and cannot cross-offset other taxes."""
    liability = {'igst': '100.00', 'cgst': '50.00', 'sgst': '50.00', 'cess': '75.00'}
    itc = {'igst': '0.00', 'cgst': '0.00', 'sgst': '0.00', 'cess': '100.00'}

    res = calculate_gst_liability_offset(liability, itc)

    assert res['offset_applied']['cess_from_cess'] == Decimal('75.00')
    assert res['liability_payable_cash']['cess'] == Decimal('0.00')
    assert res['liability_payable_cash']['igst'] == Decimal('100.00')
    assert res['liability_payable_cash']['cgst'] == Decimal('50.00')
    assert res['liability_payable_cash']['sgst'] == Decimal('50.00')
    assert res['itc_balance_remaining']['cess'] == Decimal('25.00')


def test_rule_88a_pure_decimal_precision():
    """Verify offset handles fractional cents cleanly without floating point inaccuracies."""
    liability = {'igst': '123.45', 'cgst': '67.89', 'sgst': '67.89', 'cess': '10.15'}
    itc = {'igst': '200.00', 'cgst': '50.00', 'sgst': '50.00', 'cess': '10.15'}

    res = calculate_gst_liability_offset(liability, itc)

    assert isinstance(res['liability_payable_cash']['total'], Decimal)
    assert res['offset_applied']['igst_from_igst'] == Decimal('123.45')
    # Remaining IGST ITC is 200 - 123.45 = 76.55, which fully offsets CGST (67.89), leaving 8.66 for SGST
    assert res['offset_applied']['cgst_from_igst'] == Decimal('67.89')
    assert res['offset_applied']['sgst_from_igst'] == Decimal('8.66')
    assert res['liability_payable_cash']['cgst'] == Decimal('0.00')
    # SGST rem liability: 67.89 - 8.66 - 50.00 = 9.23
    assert res['liability_payable_cash']['sgst'] == Decimal('9.23')


# ==============================================================================
# 2. GSTR-3B Table 3.1 Outward & Inward Supplies Computation
# ==============================================================================

def test_gstr3b_table_3_1_a_outward_taxable_with_credit_note_netting(app, phase6cde_env):
    """Test Table 3.1(a) properly aggregates taxable supplies and nets credit notes."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='test_t31.xlsx',
            original_file_name='test_t31.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        # Invoice 1: Taxable 10,000, IGST 1,800
        raw1 = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw1)
        _db.session.flush()

        tx1 = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw1.id,
            invoice_number='INV-001',
            invoice_date=date(2025, 1, 15),
            taxable_value=Decimal('10000.00'),
            igst_amount=Decimal('1800.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            total_tax=Decimal('1800.00'),
            supply_type='B2B',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx1)

        # Invoice 2: Taxable 5,000, CGST 450, SGST 450
        raw2 = RawImport(import_history_id=imp.id, row_number=2, raw_data='{}', status='SUCCESS')
        _db.session.add(raw2)
        _db.session.flush()

        tx2 = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw2.id,
            invoice_number='INV-002',
            invoice_date=date(2025, 1, 16),
            taxable_value=Decimal('5000.00'),
            igst_amount=Decimal('0.00'),
            cgst_amount=Decimal('450.00'),
            sgst_amount=Decimal('450.00'),
            total_tax=Decimal('900.00'),
            supply_type='B2CS',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx2)

        # Credit Note 1 against INV-001: Taxable -2,000, IGST -360
        raw3 = RawImport(import_history_id=imp.id, row_number=3, raw_data='{}', status='SUCCESS')
        _db.session.add(raw3)
        _db.session.flush()

        tx3 = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw3.id,
            invoice_number='CN-001',
            invoice_date=date(2025, 1, 20),
            note_number='CN-001',
            note_type='CREDIT',
            original_invoice_number='INV-001',
            taxable_value=Decimal('2000.00'),
            igst_amount=Decimal('360.00'),
            cgst_amount=Decimal('0.00'),
            sgst_amount=Decimal('0.00'),
            total_tax=Decimal('360.00'),
            supply_type='CDNR',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx3)
        _db.session.commit()

        t31 = get_gstr3b_table_3_1(p_id, '012025')
        row_a = t31['table_3_1']['3.1(a)']

        # Net Taxable: 10,000 + 5,000 - 2,000 = 13,000.00
        assert row_a['taxable_value'] == Decimal('13000.00')
        # Net IGST: 1,800 - 360 = 1,440.00
        assert row_a['igst'] == Decimal('1440.00')
        assert row_a['cgst'] == Decimal('450.00')
        assert row_a['sgst'] == Decimal('450.00')
        assert row_a['total_tax'] == Decimal('2340.00')


def test_gstr3b_table_3_1_b_zero_rated_export_and_sez(app, phase6cde_env):
    """Test Table 3.1(b) contains zero-rated export and SEZ supplies."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='test_export.xlsx',
            original_file_name='test_export.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        raw = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw)
        _db.session.flush()

        tx = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw.id,
            invoice_number='EXP-001',
            invoice_date=date(2025, 1, 10),
            taxable_value=Decimal('25000.00'),
            igst_amount=Decimal('4500.00'),
            total_tax=Decimal('4500.00'),
            supply_type='EXPORT',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx)
        _db.session.commit()

        t31 = get_gstr3b_table_3_1(p_id, '012025')
        row_b = t31['table_3_1']['3.1(b)']

        assert row_b['taxable_value'] == Decimal('25000.00')
        assert row_b['igst'] == Decimal('4500.00')
        assert row_b['cgst'] == Decimal('0.00')
        assert row_b['sgst'] == Decimal('0.00')


def test_gstr3b_table_3_1_c_and_e_nil_exempt_nongst(app, phase6cde_env):
    """Test Table 3.1(c) for Nil/Exempt and 3.1(e) for Non-GST supplies."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='test_nil.xlsx',
            original_file_name='test_nil.xlsx',
            platform_name='Flipkart',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        # Nil rated transaction
        raw1 = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw1)
        _db.session.flush()

        tx_nil = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw1.id,
            invoice_number='NIL-001',
            taxable_value=Decimal('3500.00'),
            total_tax=Decimal('0.00'),
            nil_rated_flag=True,
            supply_type='NIL',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_nil)

        # Non-GST transaction
        raw2 = RawImport(import_history_id=imp.id, row_number=2, raw_data='{}', status='SUCCESS')
        _db.session.add(raw2)
        _db.session.flush()

        tx_nongst = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw2.id,
            invoice_number='NONGST-001',
            taxable_value=Decimal('1200.00'),
            total_tax=Decimal('0.00'),
            non_gst_flag=True,
            supply_type='NONGST',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_nongst)
        _db.session.commit()

        t31 = get_gstr3b_table_3_1(p_id, '012025')
        assert t31['table_3_1']['3.1(c)']['taxable_value'] == Decimal('3500.00')
        assert t31['table_3_1']['3.1(c)']['total_tax'] == Decimal('0.00')
        assert t31['table_3_1']['3.1(e)']['taxable_value'] == Decimal('1200.00')
        assert t31['table_3_1']['3.1(e)']['total_tax'] == Decimal('0.00')


def test_gstr3b_staged_and_deleted_transactions_excluded(app, phase6cde_env):
    """Verify staged (is_staged=True) and soft-deleted (is_deleted=True) rows are excluded."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='test_staged.xlsx',
            original_file_name='test_staged.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        raw1 = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw1)
        _db.session.flush()

        # Staged transaction (should be excluded)
        tx_staged = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw1.id,
            invoice_number='STAGED-001',
            taxable_value=Decimal('99999.00'),
            total_tax=Decimal('18000.00'),
            supply_type='B2B',
            is_deleted=False,
            is_staged=True,
        )
        _db.session.add(tx_staged)

        # Deleted transaction (should be excluded)
        raw2 = RawImport(import_history_id=imp.id, row_number=2, raw_data='{}', status='SUCCESS')
        _db.session.add(raw2)
        _db.session.flush()

        tx_del = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw2.id,
            invoice_number='DEL-001',
            taxable_value=Decimal('88888.00'),
            total_tax=Decimal('16000.00'),
            supply_type='B2B',
            is_deleted=True,
            is_staged=False,
        )
        _db.session.add(tx_del)
        _db.session.commit()

        t31 = get_gstr3b_table_3_1(p_id, '012025')
        assert t31['table_3_1']['3.1(a)']['taxable_value'] == Decimal('0.00')
        assert t31['total_liability']['total'] == Decimal('0.00')


# ==============================================================================
# 3. Section 52 TCS Credit Summary Tests
# ==============================================================================

def test_section_52_tcs_summary_calculation(app, phase6cde_env):
    """Test Section 52 calculation computes gross, returns, net, and TCS credits."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='test_tcs.xlsx',
            original_file_name='test_tcs.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        # E-com sale: 20,000
        raw1 = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw1)
        _db.session.flush()

        tx1 = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw1.id,
            invoice_number='ECO-001',
            taxable_value=Decimal('20000.00'),
            ecommerce_gstin='27AAACE1234A1Z5',
            marketplace_name='Amazon',
            supply_type='B2CS',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx1)

        # E-com return: 4,000
        raw2 = RawImport(import_history_id=imp.id, row_number=2, raw_data='{}', status='SUCCESS')
        _db.session.add(raw2)
        _db.session.flush()

        tx2 = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw2.id,
            invoice_number='ECO-RET-001',
            taxable_value=Decimal('4000.00'),
            ecommerce_gstin='27AAACE1234A1Z5',
            marketplace_name='Amazon',
            return_flag=True,
            supply_type='B2CS',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx2)
        _db.session.commit()

        tcs = get_section_52_tcs_summary(p_id, '012025')
        assert tcs['gross_taxable_value'] == Decimal('20000.00')
        assert tcs['returns_taxable_value'] == Decimal('4000.00')
        assert tcs['net_taxable_value'] == Decimal('16000.00')
        # Standard post-July 2024 TCS rate 0.5% (0.25% CGST + 0.25% SGST):
        # 16,000 * 0.0025 = 40.00 CGST, 40.00 SGST, total 80.00
        assert tcs['tcs_credit']['cgst'] == Decimal('40.00')
        assert tcs['tcs_credit']['sgst'] == Decimal('40.00')
        assert tcs['tcs_credit']['total'] == Decimal('80.00')


def test_generate_gstr3b_summary_turnkey(app, phase6cde_env):
    """Test generate_gstr3b_summary integration of Table 3.1 + Section 52 + Rule 88A."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='turnkey_3b.xlsx',
            original_file_name='turnkey_3b.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        raw = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw)
        _db.session.flush()

        tx = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw.id,
            invoice_number='INV-TK-001',
            taxable_value=Decimal('10000.00'),
            cgst_amount=Decimal('900.00'),
            sgst_amount=Decimal('900.00'),
            total_tax=Decimal('1800.00'),
            ecommerce_gstin='27AAACE1234A1Z5',
            supply_type='B2CS',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx)
        _db.session.commit()

        summary = generate_gstr3b_summary(p_id, '012025')
        assert summary['profile_id'] == p_id
        assert summary['return_period'] == '012025'
        assert 'table_3_1' in summary
        assert 'section_52_tcs' in summary
        assert 'tax_offset_ledger' in summary
        assert summary['tax_offset_ledger']['statutory_rule'] == 'Rule 88A & Section 49'


# ==============================================================================
# 4. Cross-Period Credit Note Linking Tests
# ==============================================================================

def test_cdnr_cross_period_linking_success(app, phase6cde_env):
    """Test credit note in 022025 correctly links to original invoice from 012025."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        # Period 1 (012025) Original Invoice
        imp1 = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='p1.xlsx',
            original_file_name='p1.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp1)
        _db.session.commit()

        raw1 = RawImport(import_history_id=imp1.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw1)
        _db.session.flush()

        tx_orig = Transaction(
            import_history_id=imp1.id,
            profile_id=p_id,
            raw_import_id=raw1.id,
            invoice_number='INV-JAN-100',
            invoice_date=date(2025, 1, 10),
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('50000.00'),
            total_tax=Decimal('9000.00'),
            supply_type='B2B',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_orig)
        _db.session.commit()

        # Period 2 (022025) Credit Note referencing INV-JAN-100
        imp2 = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='p2.xlsx',
            original_file_name='p2.xlsx',
            platform_name='Amazon',
            return_period='022025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp2)
        _db.session.commit()

        raw2 = RawImport(import_history_id=imp2.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw2)
        _db.session.flush()

        tx_cn = Transaction(
            import_history_id=imp2.id,
            profile_id=p_id,
            raw_import_id=raw2.id,
            invoice_number='CN-FEB-200',
            note_number='CN-FEB-200',
            note_type='CREDIT',
            note_date=date(2025, 2, 5),
            original_invoice_number='INV-JAN-100',
            original_invoice_date=date(2025, 1, 10),
            customer_gstin='27ABCDE1234F1Z5',
            taxable_value=Decimal('10000.00'),
            total_tax=Decimal('1800.00'),
            supply_type='CDNR',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_cn)
        _db.session.commit()

        res = link_cross_period_credit_notes(p_id, '022025')
        assert res['total_notes'] == 1
        assert res['cross_period_count'] == 1
        assert res['same_period_count'] == 0
        assert res['unmatched_count'] == 0

        link = res['links'][0]
        assert link['note_number'] == 'CN-FEB-200'
        assert link['original_invoice_number'] == 'INV-JAN-100'
        assert link['original_return_period'] == '012025'
        assert link['match_status'] == 'CROSS_PERIOD_LINKED'
        assert link['is_cross_period'] is True


def test_cdnr_cross_period_tenant_isolation(app, phase6cde_env):
    """Credit note in Tenant A referencing an invoice in Tenant B must NOT link (prevents cross-tenant leak)."""
    with app.app_context():
        p_a = phase6cde_env['profile_a_id']
        u_a = phase6cde_env['user_a_id']
        p_b = phase6cde_env['profile_b_id']
        u_b = phase6cde_env['user_b_id']

        # Tenant B creates invoice INV-SECRET-777
        imp_b = ImportHistory(
            user_id=u_b,
            profile_id=p_b,
            file_name='b.xlsx',
            original_file_name='b.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp_b)
        _db.session.commit()

        raw_b = RawImport(import_history_id=imp_b.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw_b)
        _db.session.flush()

        tx_b = Transaction(
            import_history_id=imp_b.id,
            profile_id=p_b,
            raw_import_id=raw_b.id,
            invoice_number='INV-SECRET-777',
            taxable_value=Decimal('1000.00'),
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_b)
        _db.session.commit()

        # Tenant A tries to reference INV-SECRET-777 in their note
        imp_a = ImportHistory(
            user_id=u_a,
            profile_id=p_a,
            file_name='a.xlsx',
            original_file_name='a.xlsx',
            platform_name='Amazon',
            return_period='022025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp_a)
        _db.session.commit()

        raw_a = RawImport(import_history_id=imp_a.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw_a)
        _db.session.flush()

        tx_a = Transaction(
            import_history_id=imp_a.id,
            profile_id=p_a,
            raw_import_id=raw_a.id,
            invoice_number='CN-A-01',
            note_number='CN-A-01',
            note_type='CREDIT',
            original_invoice_number='INV-SECRET-777',
            taxable_value=Decimal('500.00'),
            supply_type='CDNR',
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_a)
        _db.session.commit()

        # Must report UNMATCHED because invoice does not belong to Tenant A!
        res = link_cross_period_credit_notes(p_a, '022025')
        assert res['cross_period_count'] == 0
        assert res['unmatched_count'] == 1
        assert res['links'][0]['match_status'] == 'UNMATCHED_ORIGINAL'
        assert res['links'][0]['original_invoice_id'] is None


# ==============================================================================
# 5. Table 9 Amendments (b2ba, cdnra) in GSTR-1 Generator
# ==============================================================================

def test_table_9_amendments_b2ba_cdnra_in_gstr1(app, phase6cde_env):
    """Test GSTR-1 generation produces b2ba and cdnra Table 9 amendment structures."""
    with app.app_context():
        p_id = phase6cde_env['profile_a_id']
        u_id = phase6cde_env['user_a_id']

        imp = ImportHistory(
            user_id=u_id,
            profile_id=p_id,
            file_name='amendments.xlsx',
            original_file_name='amendments.xlsx',
            platform_name='Amazon',
            return_period='012025',
            financial_year='2024-25',
            processing_status='COMPLETED',
        )
        _db.session.add(imp)
        _db.session.commit()

        # B2BA amendment
        raw1 = RawImport(import_history_id=imp.id, row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(raw1)
        _db.session.flush()

        tx_b2ba = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw1.id,
            invoice_number='INV-AMEND-1',
            invoice_date=date(2025, 1, 12),
            original_invoice_number='INV-ORIG-1',
            original_invoice_date=date(2024, 12, 10),
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            taxable_value=Decimal('8000.00'),
            cgst_amount=Decimal('720.00'),
            sgst_amount=Decimal('720.00'),
            total_tax=Decimal('1440.00'),
            tax_rate=Decimal('18.00'),
            supply_type='B2BA',
            amendment_flag=True,
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_b2ba)

        # CDNRA amendment
        raw2 = RawImport(import_history_id=imp.id, row_number=2, raw_data='{}', status='SUCCESS')
        _db.session.add(raw2)
        _db.session.flush()

        tx_cdnra = Transaction(
            import_history_id=imp.id,
            profile_id=p_id,
            raw_import_id=raw2.id,
            invoice_number='CN-AMEND-1',
            note_number='CN-AMEND-1',
            note_type='CREDIT',
            note_date=date(2025, 1, 14),
            original_invoice_number='CN-ORIG-1',
            original_invoice_date=date(2024, 12, 15),
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            taxable_value=Decimal('2000.00'),
            cgst_amount=Decimal('180.00'),
            sgst_amount=Decimal('180.00'),
            total_tax=Decimal('360.00'),
            tax_rate=Decimal('18.00'),
            supply_type='CDNRA',
            amendment_flag=True,
            is_deleted=False,
            is_staged=False,
        )
        _db.session.add(tx_cdnra)
        _db.session.commit()

        result = generate_gstr1(str(p_id), '012025')
        assert result is not None
        assert result.stats['total_b2ba'] >= 1
        assert result.stats['total_cdnra'] >= 1

        import json
        with open(result.json_path, 'r', encoding='utf-8') as f:
            gstr1_json = json.load(f)

        assert 'b2ba' in gstr1_json
        assert len(gstr1_json['b2ba']) >= 1
        b2ba_entry = gstr1_json['b2ba'][0]['inv'][0]
        assert b2ba_entry['oinum'] == 'INV-ORIG-1'
        assert b2ba_entry['inum'] == 'INV-AMEND-1'

        assert 'cdnra' in gstr1_json
        assert len(gstr1_json['cdnra']) >= 1
        cdnra_entry = gstr1_json['cdnra'][0]['nt'][0]
        assert cdnra_entry['ont_num'] == 'CN-ORIG-1'
        assert cdnra_entry['nt_num'] == 'CN-AMEND-1'


# ==============================================================================
# 6. Multi-Tenant API Route Tests (IDOR Rejection)
# ==============================================================================

def test_api_gstr3b_summary_success(client, phase6cde_env):
    """Test GET /api/gstr3b/summary returns 200 with complete summary."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(phase6cde_env['user_a_id'])
        sess['active_profile_id'] = phase6cde_env['profile_a_id']

    resp = client.get(f"/api/gstr3b/summary?profile_id={phase6cde_env['profile_a_id']}&return_period=012025")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'table_3_1' in data['data']
    assert 'tax_offset_ledger' in data['data']


def test_api_gstr3b_summary_cross_tenant_idor_rejected(client, phase6cde_env):
    """Test User A requesting User B's profile GSTR-3B summary is rejected with 403."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(phase6cde_env['user_a_id'])
        sess['active_profile_id'] = phase6cde_env['profile_a_id']

    # User A requests User B's profile
    resp = client.get(f"/api/gstr3b/summary?profile_id={phase6cde_env['profile_b_id']}&return_period=012025")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data['error'] is True
    assert "access denied" in data['message'].lower()


def test_api_cdnr_cross_period_links_success(client, phase6cde_env):
    """Test GET /api/cdnr/cross-period-links returns 200."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(phase6cde_env['user_a_id'])
        sess['active_profile_id'] = phase6cde_env['profile_a_id']

    resp = client.get(f"/api/cdnr/cross-period-links?profile_id={phase6cde_env['profile_a_id']}&return_period=012025")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'links' in data['data']


def test_api_cdnr_cross_period_idor_rejected(client, phase6cde_env):
    """Test User A requesting User B's credit note links is rejected with 403."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(phase6cde_env['user_a_id'])
        sess['active_profile_id'] = phase6cde_env['profile_a_id']

    resp = client.get(f"/api/cdnr/cross-period-links?profile_id={phase6cde_env['profile_b_id']}&return_period=012025")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data['error'] is True


def test_api_ops_schema_status_endpoint(client):
    """Test GET /api/ops/schema-status returns 200 and schema status payload."""
    resp = client.get('/api/ops/schema-status')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'dialect' in data['data']
    assert 'is_in_sync' in data['data']


# ==============================================================================
# 7. Zero-Loss Schema Migration Engine Tests
# ==============================================================================

def test_migration_service_inspect_current_db(app):
    """Test inspect_schema_drift reports in-sync status on current application database."""
    with app.app_context():
        res = inspect_schema_drift()
        assert 'is_in_sync' in res
        assert 'missing_tables' in res
        assert 'missing_columns' in res
        assert res['is_in_sync'] is True


def test_migration_service_live_legacy_sqlite_upgrade_zero_data_loss(tmp_path):
    """Simulate a pre-5E11 legacy database, verify drift inspection, upgrade, and 0 data loss."""
    legacy_db_file = tmp_path / "legacy_pre5e11.db"
    legacy_engine = create_engine(f"sqlite:///{legacy_db_file}")

    # Build stripped legacy schema without the Phase 5E-10 to 6C columns/tables
    with legacy_engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                invoice_number VARCHAR(100),
                taxable_value NUMERIC(15, 2),
                is_deleted BOOLEAN DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE import_histories (
                id INTEGER PRIMARY KEY,
                file_name VARCHAR(255),
                processing_status VARCHAR(20)
            )
        """))
        conn.execute(text("""
            CREATE TABLE gstr1_generations (
                id INTEGER PRIMARY KEY,
                return_period VARCHAR(6)
            )
        """))
        # Insert test records
        conn.execute(text("INSERT INTO transactions (id, invoice_number, taxable_value) VALUES (1, 'INV-LEGACY-1', 5000.00)"))
        conn.execute(text("INSERT INTO transactions (id, invoice_number, taxable_value) VALUES (2, 'INV-LEGACY-2', 7500.00)"))
        conn.execute(text("INSERT INTO import_histories (id, file_name, processing_status) VALUES (1, 'old_import.csv', 'COMPLETED')"))
        conn.execute(text("INSERT INTO gstr1_generations (id, return_period) VALUES (1, '012025')"))

    # Step 1: Drift Inspection on legacy database
    drift_before = inspect_schema_drift(legacy_engine)
    assert drift_before['is_in_sync'] is False
    assert 'reconciliation_records' in drift_before['missing_tables']
    assert 'is_staged' in drift_before['missing_columns']['transactions']
    assert 'task_id' in drift_before['missing_columns']['import_histories']
    assert 'processed_rows' in drift_before['missing_columns']['import_histories']
    assert 'error_message' in drift_before['missing_columns']['import_histories']
    assert 'snapshot_checksum' in drift_before['missing_columns']['gstr1_generations']
    assert 'input_count' in drift_before['missing_columns']['gstr1_generations']
    assert 'gate_status' in drift_before['missing_columns']['gstr1_generations']
    assert 'validation_summary' in drift_before['missing_columns']['gstr1_generations']

    # Step 2: Apply zero-loss migration
    mig_result = apply_schema_migrations(legacy_engine)
    assert mig_result['status'] == 'MIGRATED'
    assert mig_result['zero_loss_verified'] is True
    assert 'reconciliation_records' in mig_result['applied_tables']
    assert len(mig_result['applied_columns']) >= 8

    # Step 3: Drift Inspection after migration
    drift_after = inspect_schema_drift(legacy_engine)
    assert drift_after['is_in_sync'] is True
    assert len(drift_after['missing_tables']) == 0
    assert len(drift_after['missing_columns']) == 0

    # Step 4: Verify 100% data preservation and row contents
    with legacy_engine.connect() as conn:
        tx_rows = conn.execute(text("SELECT id, invoice_number, taxable_value, is_staged FROM transactions ORDER BY id")).fetchall()
        assert len(tx_rows) == 2
        assert tx_rows[0][1] == 'INV-LEGACY-1'
        assert float(tx_rows[0][2]) == 5000.00
        # Default for new column is_staged should be 0 (False)
        assert tx_rows[0][3] in (0, False)

        imp_rows = conn.execute(text("SELECT id, file_name, processed_rows FROM import_histories")).fetchall()
        assert len(imp_rows) == 1
        assert imp_rows[0][1] == 'old_import.csv'
        assert imp_rows[0][2] == 0

    # Step 5: Idempotency check: running again on up-to-date schema does nothing and preserves data
    mig_idempotent = apply_schema_migrations(legacy_engine)
    assert mig_idempotent['status'] == 'UP_TO_DATE'
    assert len(mig_idempotent['applied_tables']) == 0
    assert len(mig_idempotent['applied_columns']) == 0


# ==============================================================================
# 8. Turnkey Production Deployment Package Validation Tests
# ==============================================================================

def test_deployment_wsgi_importable():
    """Verify wsgi.py is importable, exposes Flask app callable, and matches config."""
    import importlib.util
    wsgi_path = os.path.join(os.path.dirname(__file__), "..", "..", "wsgi.py")
    assert os.path.exists(wsgi_path), "wsgi.py must exist in repository root"

    spec = importlib.util.spec_from_file_location("wsgi_module", wsgi_path)
    wsgi_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wsgi_mod)

    assert hasattr(wsgi_mod, 'app'), "wsgi.py must expose 'app' WSGI entrypoint"
    assert wsgi_mod.app is not None
    assert callable(wsgi_mod.app)


def test_deployment_celery_worker_structure():
    """Verify celery_worker.py exists and defines worker entry point."""
    import importlib.util
    worker_path = os.path.join(os.path.dirname(__file__), "..", "..", "celery_worker.py")
    assert os.path.exists(worker_path), "celery_worker.py must exist in repository root"

    spec = importlib.util.spec_from_file_location("celery_worker_module", worker_path)
    worker_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_mod)

    assert hasattr(worker_mod, 'celery_app'), "celery_worker.py must expose celery_app"


def test_deployment_dockerfile_and_compose_specs():
    """Verify Dockerfile and docker-compose.yml adhere to production requirements."""
    base_dir = os.path.join(os.path.dirname(__file__), "..", "..")
    dockerfile_path = os.path.join(base_dir, "Dockerfile")
    compose_path = os.path.join(base_dir, "docker-compose.yml")

    assert os.path.exists(dockerfile_path), "Dockerfile must exist"
    assert os.path.exists(compose_path), "docker-compose.yml must exist"

    with open(dockerfile_path, "r", encoding="utf-8") as f:
        dockerfile_content = f.read()

    # Check non-root user and healthcheck
    assert "useradd" in dockerfile_content or "adduser" in dockerfile_content
    assert "USER gstr" in dockerfile_content
    assert "HEALTHCHECK" in dockerfile_content
    assert "/healthz" in dockerfile_content

    with open(compose_path, "r", encoding="utf-8") as f:
        compose_content = f.read()

    # Check services: web, worker, postgres, redis
    assert "web:" in compose_content
    assert "worker:" in compose_content
    assert "postgres:" in compose_content
    assert "redis:" in compose_content
    # Check health checks on database and cache
    assert "pg_isready" in compose_content
    assert "redis-cli" in compose_content


def test_deployment_env_example_zero_secrets_policy():
    """Verify .env.example defines all required configuration without hardcoded secrets."""
    base_dir = os.path.join(os.path.dirname(__file__), "..", "..")
    env_example_path = os.path.join(base_dir, ".env.example")

    assert os.path.exists(env_example_path), ".env.example must exist"

    with open(env_example_path, "r", encoding="utf-8") as f:
        env_content = f.read()

    # Check essential environment keys
    required_keys = [
        'FLASK_CONFIG',
        'SECRET_KEY',
        'JWT_SECRET_KEY',
        'DATABASE_URL',
        'POSTGRES_USER',
        'POSTGRES_PASSWORD',
        'POSTGRES_DB',
        'CELERY_BROKER_URL',
        'CELERY_RESULT_BACKEND',
    ]
    for key in required_keys:
        assert f"{key}=" in env_content, f"Missing key {key} in .env.example"

    # Zero Hardcoded Secrets verification: Ensure placeholders, not real leaked credentials
    assert "change_me" in env_content or "generate" in env_content.lower()
