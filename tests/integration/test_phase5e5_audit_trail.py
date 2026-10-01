"""Phase 5E-5 Integration Tests: GSTR-1 Generation Lifecycle Audit Trail.

Covers:
- Test 1: Web generation /generate/run creates AuditLog with action='CREATE', entity_type='GSTR1Generation',
          correct tenant user_id, and expected section summary in new_value.
- Test 2: REST API generation POST /api/generate creates AuditLog with action='CREATE', correct tenant user_id.
- Test 3: Web regeneration /generate/regenerate creates AuditLog with action='REGENERATE',
          old_value containing previous generation metrics, new_value containing updated figures.
- Test 4: Excel download /generate/download/excel/<id> creates AuditLog with action='DOWNLOAD', format='excel'.
- Test 5: JSON download /generate/download/json/<id> creates AuditLog with action='DOWNLOAD', format='json'.
- Test 6: Gate invariant: BLOCKED reconciliation attempt produces ZERO AuditLog entries across web and API.
- Test 7: Multi-tenant isolation: Tenant A cannot download Tenant B's generation (404),
          no audit log is created for unauthorized attempts, and Tenant B's logs record Tenant B's user_id.
- Test 8: Robustness: decimal serialization safety and request context independence in audit service.
"""
import json
import uuid
import random
from datetime import date, datetime
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, GSTR1Generation, AuditLog
from app.services.audit_service import log_generation_audit, extract_generation_summary
from app.services.gstr1_generator import generate_gstr1


@pytest.fixture
def audit_test_env(app, db):
    """Set up multi-user, multi-period environment for audit trail verification."""
    rand_a = f"{random.randint(1000, 9999)}"
    rand_b = f"{random.randint(1000, 9999)}"
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]

    gstin_a = f"27AAAAA{rand_a}A1Z5"
    gstin_b = f"29BBBBB{rand_b}B1Z2"

    with app.app_context():
        # Tenant A
        user_a = User(username=f'audit_user_a_{suffix_a}', email=f'audit_a_{suffix_a}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a,
            legal_name='Audit Tenant A Pvt Ltd',
            trade_name='Audit Tenant A',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B
        user_b = User(username=f'audit_user_b_{suffix_b}', email=f'audit_b_{suffix_b}@example.com')
        user_b.set_password('PasswordB1!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b,
            legal_name='Audit Tenant B Pvt Ltd',
            trade_name='Audit Tenant B',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Clean period 082024 for Tenant A
        imp_a = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name='audit_sales_082024.xlsx',
            original_file_name='audit_sales_082024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=3,
            success_rows=3
        )
        _db.session.add(imp_a)
        _db.session.commit()

        raw_a1 = RawImport(import_history_id=imp_a.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw_a2 = RawImport(import_history_id=imp_a.id, sheet_name='Sheet1', row_number=2, status='SUCCESS')
        raw_a3 = RawImport(import_history_id=imp_a.id, sheet_name='Sheet1', row_number=3, status='SUCCESS')
        _db.session.add_all([raw_a1, raw_a2, raw_a3])
        _db.session.commit()

        tx_a1 = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_a.id,
            raw_import_id=raw_a1.id,
            invoice_number='INV-AUDIT-A-001',
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

        tx_a2 = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_a.id,
            raw_import_id=raw_a2.id,
            invoice_number='INV-AUDIT-A-B2CS-001',
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

        tx_a3 = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_a.id,
            raw_import_id=raw_a3.id,
            invoice_number='INV-AUDIT-A-001',
            original_invoice_number='INV-AUDIT-A-001',
            note_number='CN-AUDIT-001',
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
        _db.session.add_all([tx_a1, tx_a2, tx_a3])

        # Blocked period 102024 for Tenant A (impossible dual taxes triggers critical reconciliation failure)
        imp_block = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='102024',
            file_name='audit_block_102024.xlsx',
            original_file_name='audit_block_102024.xlsx',
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
            invoice_number='INV-AUDIT-BLOCK-001',
            invoice_date=date(2024, 10, 5),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('1000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('90.00'),
            sgst_amount=Decimal('90.00'),
            igst_amount=Decimal('180.00'),  # Conflicting taxes -> BLOCKED
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('1360.00'),
            is_deleted=False
        )
        _db.session.add(tx_block)

        # Pre-create a generation for Tenant B in period 082024
        gen_res_b = generate_gstr1(str(profile_b.id), '082024')
        gen_b = GSTR1Generation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            financial_year='2024-25',
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
            total_tax=Decimal('0.00'),
            validation_passed=True
        )
        _db.session.add(gen_b)
        _db.session.commit()

        return {
            'user_a_id': user_a.id,
            'user_a_username': user_a.username,
            'user_a_password': 'PasswordA1!',
            'profile_a_id': profile_a.id,
            'user_b_id': user_b.id,
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
# Integration Test Cases
# =========================================================================

def test_01_web_generation_creates_audit_log(client, audit_test_env):
    """Test 1: Web generation /generate/run creates AuditLog with action='CREATE', entity_type='GSTR1Generation',

    correct tenant user_id, and expected section summary in new_value.
    """
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    res = client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    assert res.status_code == 200

    gen = GSTR1Generation.query.filter_by(
        profile_id=audit_test_env['profile_a_id'],
        return_period='082024'
    ).order_by(GSTR1Generation.id.desc()).first()
    assert gen is not None

    audit_entry = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen.id,
        action='CREATE'
    ).first()

    assert audit_entry is not None, "AuditLog entry for action='CREATE' must be created"
    assert audit_entry.user_id == audit_test_env['user_a_id'], "AuditLog must record authenticated user_id"
    assert audit_entry.field_name == 'status'
    assert audit_entry.old_value is None

    payload = json.loads(audit_entry.new_value)
    assert payload['return_period'] == '082024'
    assert payload['profile_id'] == audit_test_env['profile_a_id']
    assert payload['generation_status'] == 'COMPLETED'
    assert payload['total_b2b'] == 1
    assert payload['total_b2cs'] == 1
    assert payload['total_cdnr'] == 1
    assert float(payload['total_taxable_value']) == 3200.0
    assert float(payload['total_tax']) == 576.0


def test_02_rest_api_generation_creates_audit_log(client, audit_test_env):
    """Test 2: REST API generation POST /api/generate creates AuditLog with action='CREATE',

    correct tenant user_id.
    """
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    res = client.post('/api/generate', json={'return_period': '082024'})
    assert res.status_code == 201

    resp_json = res.get_json()
    assert resp_json['error'] is False
    gen_id = resp_json['data']['id']

    audit_entry = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen_id,
        action='CREATE'
    ).first()

    assert audit_entry is not None, "AuditLog entry for API POST /api/generate must exist"
    assert audit_entry.user_id == audit_test_env['user_a_id'], "AuditLog must record authenticated API user_id"
    assert audit_entry.field_name == 'status'

    payload = json.loads(audit_entry.new_value)
    assert payload['return_period'] == '082024'
    assert payload['profile_id'] == audit_test_env['profile_a_id']
    assert payload['generation_status'] == 'COMPLETED'
    assert payload['total_b2b'] == 1


def test_03_web_regeneration_creates_audit_log(app, client, audit_test_env):
    """Test 3: Web regeneration /generate/regenerate creates AuditLog with action='REGENERATE',

    old_value containing previous generation metrics, new_value containing updated figures.
    """
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    # Step 1: Initial generation
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    gen1 = GSTR1Generation.query.filter_by(
        profile_id=audit_test_env['profile_a_id'],
        return_period='082024'
    ).order_by(GSTR1Generation.id.desc()).first()
    assert gen1 is not None
    val1 = float(gen1.total_taxable_value)

    # Step 2: Update transaction value
    with app.app_context():
        tx = Transaction.query.filter_by(
            profile_id=audit_test_env['profile_a_id'],
            invoice_number='INV-AUDIT-A-001'
        ).first()
        tx.taxable_value = Decimal('5000.00')
        tx.cgst_amount = Decimal('450.00')
        tx.sgst_amount = Decimal('450.00')
        tx.total_tax = Decimal('900.00')
        tx.invoice_value = Decimal('5900.00')
        _db.session.commit()

    # Step 3: Trigger regeneration
    res = client.post('/generate/regenerate', data={'return_period': '082024', 'generation_id': gen1.id}, follow_redirects=True)
    assert res.status_code == 200

    regen_log = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        action='REGENERATE'
    ).order_by(AuditLog.id.desc()).first()

    assert regen_log is not None, "AuditLog with action='REGENERATE' must be created"
    assert regen_log.user_id == audit_test_env['user_a_id']
    assert regen_log.field_name == 'status'

    old_payload = json.loads(regen_log.old_value)
    new_payload = json.loads(regen_log.new_value)

    assert old_payload['previous_generation_id'] == gen1.id
    assert float(old_payload['total_taxable_value']) == val1
    assert float(new_payload['total_taxable_value']) > val1
    assert float(new_payload['total_taxable_value']) == 5700.0  # 5000 + 500 + 200


def test_04_download_excel_creates_audit_log(client, audit_test_env):
    """Test 4: Excel download /generate/download/excel/<id> creates AuditLog with action='DOWNLOAD', format='excel'."""
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    # Ensure generation exists
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    gen = GSTR1Generation.query.filter_by(
        profile_id=audit_test_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None

    res = client.get(f'/generate/download/excel/{gen.id}')
    assert res.status_code == 200

    download_log = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen.id,
        action='DOWNLOAD'
    ).filter(AuditLog.new_value.like('%excel%')).first()

    assert download_log is not None, "AuditLog with action='DOWNLOAD' for Excel must be recorded"
    assert download_log.user_id == audit_test_env['user_a_id']
    assert download_log.field_name == 'file_download'

    dl_payload = json.loads(download_log.new_value)
    assert dl_payload['format'] == 'excel'
    assert dl_payload['filename'].endswith('.xlsx')
    assert dl_payload['return_period'] == '082024'


def test_05_download_json_creates_audit_log(client, audit_test_env):
    """Test 5: JSON download /generate/download/json/<id> creates AuditLog with action='DOWNLOAD', format='json'."""
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    # Ensure generation exists
    client.post('/generate/run', data={'return_period': '082024'}, follow_redirects=True)
    gen = GSTR1Generation.query.filter_by(
        profile_id=audit_test_env['profile_a_id'],
        return_period='082024'
    ).first()
    assert gen is not None

    res = client.get(f'/generate/download/json/{gen.id}')
    assert res.status_code == 200

    download_log = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen.id,
        action='DOWNLOAD'
    ).filter(AuditLog.new_value.like('%json%')).first()

    assert download_log is not None, "AuditLog with action='DOWNLOAD' for JSON must be recorded"
    assert download_log.user_id == audit_test_env['user_a_id']
    assert download_log.field_name == 'file_download'

    dl_payload = json.loads(download_log.new_value)
    assert dl_payload['format'] == 'json'
    assert dl_payload['filename'].endswith('.json')
    assert dl_payload['return_period'] == '082024'


def test_06_gate_invariant_blocked_reconciliation_zero_audit_log(client, audit_test_env):
    """Test 6: Gate invariant: BLOCKED reconciliation attempt produces ZERO AuditLog entries."""
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    audit_count_before = AuditLog.query.filter(
        AuditLog.entity_type == 'GSTR1Generation',
        AuditLog.new_value.like('%102024%')
    ).count()

    # Attempt 1: Web generate run on BLOCKED period
    res_run = client.post('/generate/run', data={'return_period': '102024'}, follow_redirects=False)
    assert res_run.status_code == 302
    assert '/statement/validation-errors' in res_run.location

    # Attempt 2: Web regenerate on BLOCKED period
    res_regen = client.post('/generate/regenerate', data={'return_period': '102024'}, follow_redirects=False)
    assert res_regen.status_code == 302
    assert '/statement/validation-errors' in res_regen.location

    # Attempt 3: REST API generate on BLOCKED period
    res_api = client.post('/api/generate', json={'return_period': '102024'})
    assert res_api.status_code == 403

    audit_count_after = AuditLog.query.filter(
        AuditLog.entity_type == 'GSTR1Generation',
        AuditLog.new_value.like('%102024%')
    ).count()

    assert audit_count_after == audit_count_before, (
        "Server generation gate invariant violated: BLOCKED reconciliation attempt must produce ZERO audit logs"
    )


def test_07_multi_tenant_isolation_unauthorized_download_zero_audit(client, audit_test_env):
    """Test 7: Multi-tenant isolation: Tenant A cannot download Tenant B's generation (404),

    and no audit log is created for unauthorized attempts. Tenant B's generation logs record Tenant B's user_id.
    """
    gen_b_id = audit_test_env['gen_b_id']

    # Step 1: Login Tenant A and attempt cross-tenant IDOR download on Tenant B's record
    _login(client, audit_test_env['user_a_username'], audit_test_env['user_a_password'])

    res_excel = client.get(f'/generate/download/excel/{gen_b_id}')
    assert res_excel.status_code == 404

    res_json = client.get(f'/generate/download/json/{gen_b_id}')
    assert res_json.status_code == 404

    # Verify no audit log exists for Tenant A accessing gen_b_id
    unauth_logs = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen_b_id,
        user_id=audit_test_env['user_a_id']
    ).all()
    assert len(unauth_logs) == 0, "Unauthorized IDOR download attempt must not create any AuditLog entry"

    # Step 2: Login Tenant B and download legitimately
    _login(client, audit_test_env['user_b_username'], audit_test_env['user_b_password'])

    res_b_excel = client.get(f'/generate/download/excel/{gen_b_id}')
    assert res_b_excel.status_code == 200

    b_logs = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen_b_id,
        action='DOWNLOAD'
    ).all()

    assert len(b_logs) > 0
    for b_log in b_logs:
        assert b_log.user_id == audit_test_env['user_b_id'], "AuditLog must strictly record Tenant B user_id"
        assert b_log.user_id != audit_test_env['user_a_id'], "Tenant B generation audit must never record Tenant A"


def test_08_decimal_serialization_and_context_safety(app, db, audit_test_env):
    """Test 8: Verify audit_service helper handles Decimal serialization safely outside request context."""
    with app.app_context():
        # Calling log_generation_audit directly outside request context
        gen = _db.session.get(GSTR1Generation, audit_test_env['gen_b_id'])
        gen.total_taxable_value = Decimal('12345.67')
        gen.total_tax = Decimal('2222.22')

        log = log_generation_audit(
            user_id=audit_test_env['user_b_id'],
            action='CREATE',
            generation=gen,
            return_period='082024',
            profile_id=audit_test_env['profile_b_id'],
            commit=True
        )

        assert log.id is not None
        assert log.ip_address is None  # Outside request context
        parsed = json.loads(log.new_value)
        assert parsed['total_taxable_value'] == 12345.67
        assert parsed['total_tax'] == 2222.22
