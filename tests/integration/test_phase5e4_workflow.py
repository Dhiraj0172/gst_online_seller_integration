"""Phase 5E-4 Integration Tests: Web Downloads & Generation Workflow.

Covers:
- Tier 1: Core Feature Coverage
  1. GET /downloads with empty generations list (graceful empty state).
  2. GET /downloads with populated generations list (table with periods, dates, totals, download links).
  3. GET /generate/download/excel/<id> returns valid Excel workbook with required sheets.
  4. GET /generate/download/json/<id> returns valid GSTN JSON with schema compliance.
  5. POST /generate/regenerate workflow preserves return_period and re-executes generation.
  6. Server-authoritative gate enforcement on POST /generate/regenerate (blocks BLOCKED periods).
  7. GET /dashboard reflects real gstr1_status ('Pending' vs 'Generated').
- Tier 2: Boundary & Corner Cases
  8. Cross-tenant IDOR protection on Excel download (/generate/download/excel/<id> -> 404).
  9. Cross-tenant IDOR protection on JSON download (/generate/download/json/<id> -> 404).
  10. GET /generate/download/excel/999999 for non-existent ID -> 404.
  11. Tenant isolation on /downloads view (User A cannot see User B's rows).
- Tier 3: Cross-Feature Combinations
  12. Complete Web UI workflow: Upload -> Run Generation -> View Downloads -> Download Excel & JSON.
  13. Regeneration after transaction edits updates totals in downloads table.
- Tier 4: Real-World Scenarios
  14. Multi-period seller downloads center navigation and return retrieval.
"""
import io
import json
import uuid
import random
from datetime import date, datetime
from decimal import Decimal
import pytest
import openpyxl

from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, GSTR1Generation
from app.services.gstr1_generator import generate_gstr1


@pytest.fixture
def workflow_test_env(app, db):
    """Set up multi-user, multi-period environment with transactions and generations."""
    rand_a = f"{random.randint(1000, 9999)}"
    rand_b = f"{random.randint(1000, 9999)}"
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]

    gstin_a = f"27AAAAA{rand_a}A1Z5"
    gstin_b = f"29BBBBB{rand_b}B1Z2"

    with app.app_context():
        # User A
        user_a = User(username=f'wf_user_a_{suffix_a}', email=f'wf_a_{suffix_a}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a,
            legal_name='Workflow Seller A Pvt Ltd',
            trade_name='Workflow Seller A',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # User B
        user_b = User(username=f'wf_user_b_{suffix_b}', email=f'wf_b_{suffix_b}@example.com')
        user_b.set_password('PasswordB1!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b,
            legal_name='Workflow Seller B Pvt Ltd',
            trade_name='Workflow Seller B',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Clean period 082024 for User A
        imp_a = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='wf_sales_082024.xlsx',
            original_file_name='wf_sales_082024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_a)
        _db.session.commit()

        raw_a = RawImport(import_history_id=imp_a.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_a)
        _db.session.commit()

        tx_a = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_a.id,
            raw_import_id=raw_a.id,
            invoice_number='INV-WF-A-001',
            invoice_date=date(2024, 8, 10),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('2500.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('225.00'),
            sgst_amount=Decimal('225.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('450.00'),
            invoice_value=Decimal('2950.00'),
            is_deleted=False
        )
        _db.session.add(tx_a)

        tx_a_b2cs = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_a.id,
            raw_import_id=raw_a.id,
            invoice_number='INV-WF-A-B2CS-001',
            invoice_date=date(2024, 8, 12),
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
        _db.session.add(tx_a_b2cs)

        tx_a_cdnr = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_a.id,
            raw_import_id=raw_a.id,
            invoice_number='INV-WF-A-001',
            original_invoice_number='INV-WF-A-001',
            note_number='CN-WF-001',
            note_type='C',
            note_date=date(2024, 8, 15),
            supply_type='CDNR',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('200.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('18.00'),
            sgst_amount=Decimal('18.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('36.00'),
            invoice_value=Decimal('236.00'),
            is_deleted=False
        )
        _db.session.add(tx_a_cdnr)

        # Blocked period 102024 for User A
        imp_block = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='102024',
            file_name='wf_block_102024.xlsx',
            original_file_name='wf_block_102024.xlsx',
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

        tx_block = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_block.id,
            raw_import_id=raw_block.id,
            invoice_number='INV-WF-BLOCK-001',
            invoice_date=date(2024, 10, 5),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('180.00'),  # Impossible tax combo
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('1360.00'),
            is_deleted=False
        )
        _db.session.add(tx_block)

        # Pre-create a generation for User B in period 082024
        gen_res_b = generate_gstr1(str(profile_b.id), '082024')
        gen_b = GSTR1Generation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            generation_status='COMPLETED',
            generation_started_at=datetime.utcnow(),
            generation_completed_at=datetime.utcnow(),
            excel_file_path=gen_res_b.excel_path,
            json_file_path=gen_res_b.json_path,
            total_b2b=0,
            total_b2cs=0,
            total_taxable_value=Decimal('0.00'),
            total_cgst=Decimal('0.00'),
            total_sgst=Decimal('0.00'),
            total_igst=Decimal('0.00'),
            total_cess=Decimal('0.00'),
            validation_passed=True
        )
        _db.session.add(gen_b)
        _db.session.commit()

        return {
            'user_a_username': user_a.username,
            'user_a_password': 'PasswordA1!',
            'profile_a_id': profile_a.id,
            'user_b_username': user_b.username,
            'user_b_password': 'PasswordB1!',
            'profile_b_id': profile_b.id,
            'gen_b_id': gen_b.id
        }


def _login(client, username, password):
    """Authenticate client session."""
    client.get('/logout', follow_redirects=True)
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


# =========================================================================
# Tier 1: Core Feature Coverage
# =========================================================================

def test_01_downloads_empty_state(client, workflow_test_env):
    """1. GET /downloads renders graceful empty state when user has no generations."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    res = client.get('/downloads')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert 'Downloads' in html or 'downloads' in html
    # In empty state, no download link for User B's return should appear
    assert f"/generate/download/excel/{workflow_test_env['gen_b_id']}" not in html


def test_02_downloads_populated_state(client, workflow_test_env):
    """2. GET /downloads lists returns with periods, totals, and download links."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    # Run generation for User A
    gen_post = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    assert gen_post.status_code == 200

    # View downloads
    res = client.get('/downloads')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert '082024' in html
    assert 'download' in html.lower()


def test_03_download_excel_endpoint_structure(client, workflow_test_env):
    """3. GET /generate/download/excel/<id> returns valid Excel workbook with required sheets."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)

    gen = GSTR1Generation.query.filter_by(
        profile_id=workflow_test_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None

    res = client.get(f'/generate/download/excel/{gen.id}')
    assert res.status_code == 200
    assert len(res.data) > 0
    assert 'spreadsheet' in res.content_type or 'excel' in res.content_type or 'octet-stream' in res.content_type

    # Verify Excel workbook structure using openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(res.data))
    for expected_sheet in ['b2b', 'b2cs', 'cdnr', 'hsn']:
        assert expected_sheet in wb.sheetnames, f"Expected sheet '{expected_sheet}' in {wb.sheetnames}"
    wb.close()


def test_04_download_json_endpoint_structure(client, workflow_test_env):
    """4. GET /generate/download/json/<id> returns valid GSTN JSON conforming to schema."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)

    gen = GSTR1Generation.query.filter_by(
        profile_id=workflow_test_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None

    res = client.get(f'/generate/download/json/{gen.id}')
    assert res.status_code == 200
    assert 'application/json' in res.content_type

    payload = json.loads(res.data.decode('utf-8'))
    assert 'gstin' in payload
    assert payload['fp'] == '082024'
    assert 'b2b' in payload
    assert 'b2cs' in payload
    assert 'hsn' in payload


def test_05_generate_regenerate_workflow_preserves_period(client, workflow_test_env):
    """5. POST /generate/regenerate preserves return_period and triggers generation."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    res = client.post('/generate/regenerate', data={'return_period': '082024'}, follow_redirects=False)
    # Should redirect either to generate run or directly to generate view
    assert res.status_code in (302, 200)

    # Follow redirect to verify successful generation completion
    res_final = client.post('/generate/regenerate', data={'return_period': '082024'}, follow_redirects=True)
    assert res_final.status_code == 200

    gen = GSTR1Generation.query.filter_by(
        profile_id=workflow_test_env['profile_a_id'],
        return_period='082024'
    ).order_by(GSTR1Generation.created_at.desc()).first()
    assert gen is not None
    assert gen.generation_status == 'COMPLETED'


def test_06_regenerate_gate_enforcement_blocks_critical(client, workflow_test_env):
    """6. Server-authoritative gate on /generate/regenerate blocks BLOCKED return periods."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    res = client.post('/generate/regenerate', data={'return_period': '102024'}, follow_redirects=False)
    assert res.status_code == 302
    assert '/statement/validation-errors' in res.location or '/generate' in res.location

    # Verify no completed generation for blocked period 102024
    gen = GSTR1Generation.query.filter_by(
        profile_id=workflow_test_env['profile_a_id'],
        return_period='102024',
        generation_status='COMPLETED'
    ).first()
    assert gen is None, "Regenerate must NOT allow generation for BLOCKED period"


def test_07_dashboard_generation_status_reflection(client, workflow_test_env):
    """7. GET /dashboard reflects real gstr1_status ('Pending' vs 'Generated')."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    # Initially before generation
    res_before = client.get('/dashboard')
    assert res_before.status_code == 200
    html_before = res_before.data.decode('utf-8')
    assert 'Pending' in html_before

    # Now generate GSTR-1
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)

    # After generation
    res_after = client.get('/dashboard')
    assert res_after.status_code == 200
    html_after = res_after.data.decode('utf-8')
    assert 'Generated' in html_after


# =========================================================================
# Tier 2: Boundary & Corner Cases
# =========================================================================

def test_08_download_excel_cross_tenant_idor_404(client, workflow_test_env):
    """8. User A requesting User B's Excel download URL gets 404 Not Found."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    res = client.get(f"/generate/download/excel/{workflow_test_env['gen_b_id']}")
    assert res.status_code == 404, "Cross-tenant Excel download must return 404 Not Found"


def test_09_download_json_cross_tenant_idor_404(client, workflow_test_env):
    """9. User A requesting User B's JSON download URL gets 404 Not Found."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    res = client.get(f"/generate/download/json/{workflow_test_env['gen_b_id']}")
    assert res.status_code == 404, "Cross-tenant JSON download must return 404 Not Found"


def test_10_download_nonexistent_generation_id_404(client, workflow_test_env):
    """10. Requesting non-existent generation ID returns 404 Not Found."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    assert client.get('/generate/download/excel/99999999').status_code == 404
    assert client.get('/generate/download/json/99999999').status_code == 404


def test_11_downloads_view_tenant_isolation(client, workflow_test_env):
    """11. /downloads view strictly isolates records between tenants."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    res_a = client.get('/downloads')
    assert res_a.status_code == 200
    html_a = res_a.data.decode('utf-8')

    # User B's generation ID should not appear in User A's downloads table
    assert f"/generate/download/excel/{workflow_test_env['gen_b_id']}" not in html_a
    assert f"/generate/download/json/{workflow_test_env['gen_b_id']}" not in html_a


# =========================================================================
# Tier 3: Cross-Feature Combinations
# =========================================================================

def test_12_complete_web_generation_and_download_flow(client, workflow_test_env):
    """12. Complete Web UI workflow: View -> Run -> View Downloads -> Download files."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    # 1. View generate page
    gen_page = client.get('/generate?return_period=082024')
    assert gen_page.status_code == 200

    # 2. Run generation
    run_res = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    assert run_res.status_code == 200

    # 3. View downloads page
    dl_page = client.get('/downloads')
    assert dl_page.status_code == 200
    assert '082024' in dl_page.data.decode('utf-8')

    gen = GSTR1Generation.query.filter_by(
        profile_id=workflow_test_env['profile_a_id'],
        return_period='082024'
    ).first()

    # 4. Download Excel
    excel_res = client.get(f'/generate/download/excel/{gen.id}')
    assert excel_res.status_code == 200
    assert len(excel_res.data) > 0

    # 5. Download JSON
    json_res = client.get(f'/generate/download/json/{gen.id}')
    assert json_res.status_code == 200
    assert json.loads(json_res.data.decode('utf-8'))['fp'] == '082024'


def test_13_regeneration_after_transaction_edit(app, db, client, workflow_test_env):
    """13. Modifying transactions and regenerating return reflects updated figures."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    with app.app_context():
        # Initial generation
        client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
        gen1 = GSTR1Generation.query.filter_by(
            profile_id=workflow_test_env['profile_a_id'],
            return_period='082024'
        ).order_by(GSTR1Generation.created_at.desc()).first()
        val1 = float(gen1.total_taxable_value)

        # Edit transaction taxable value and taxes proportionally
        tx = Transaction.query.filter_by(
            profile_id=workflow_test_env['profile_a_id'],
            invoice_number='INV-WF-A-001',
            supply_type='B2B'
        ).first()
        tx.taxable_value = Decimal('5000.00')
        tx.cgst_amount = Decimal('450.00')
        tx.sgst_amount = Decimal('450.00')
        tx.total_tax = Decimal('900.00')
        tx.invoice_value = Decimal('5900.00')
        _db.session.commit()

        # Regenerate
        client.post('/generate/regenerate', data={'return_period': '082024'}, follow_redirects=True)
        gen2 = GSTR1Generation.query.filter_by(
            profile_id=workflow_test_env['profile_a_id'],
            return_period='082024'
        ).order_by(GSTR1Generation.id.desc()).first()

        assert gen2 is not None
        _db.session.refresh(gen2)
        # Updated value is reflected
        assert float(gen2.total_taxable_value) > val1


# =========================================================================
# Tier 4: Real-World Scenarios
# =========================================================================

def test_14_multi_period_seller_downloads_center(client, workflow_test_env):
    """14. Seller manages returns across multiple periods; downloads center lists all."""
    _login(client, workflow_test_env['user_a_username'], workflow_test_env['user_a_password'])

    # Add transaction in period 072024
    imp_jul = ImportHistory(
        profile_id=workflow_test_env['profile_a_id'],
        user_id=GSTR1Generation.query.filter_by(profile_id=workflow_test_env['profile_a_id']).first().user_id if GSTR1Generation.query.filter_by(profile_id=workflow_test_env['profile_a_id']).first() else 1,
        return_period='072024',
        file_name='sales_072024.xlsx',
        original_file_name='sales_072024.xlsx',
        platform_name='AMAZON',
        financial_year='2024-25',
        processing_status='COMPLETED',
        total_rows=1,
        success_rows=1
    )
    _db.session.add(imp_jul)
    _db.session.commit()

    raw_jul = RawImport(import_history_id=imp_jul.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
    _db.session.add(raw_jul)
    _db.session.commit()

    tx_jul = Transaction(
        profile_id=workflow_test_env['profile_a_id'],
        import_history_id=imp_jul.id,
        raw_import_id=raw_jul.id,
        invoice_number='INV-JUL-001',
        invoice_date=date(2024, 7, 10),
        supply_type='B2B',
        customer_gstin='27GHIJK5678L1Z9',
        place_of_supply='27',
        hsn_sac='8471',
        taxable_value=Decimal('1500.00'),
        tax_rate=Decimal('18.00'),
        cgst_amount=Decimal('135.00'),
        sgst_amount=Decimal('135.00'),
        total_tax=Decimal('270.00'),
        invoice_value=Decimal('1770.00'),
        is_deleted=False
    )
    _db.session.add(tx_jul)
    _db.session.commit()

    # Generate 072024 and 082024
    client.post('/generate/run', data={'return_period': '072024'}, follow_redirects=True)
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)

    # Downloads center lists both periods
    res = client.get('/downloads')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert '072024' in html
    assert '082024' in html
