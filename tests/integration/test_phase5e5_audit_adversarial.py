"""Adversarial stress test suite for Phase 5E-5 GSTR-1 Generation Lifecycle Audit Trail.

Challenges:
1. Blocked period generation attempts: zero AuditLog records created across web, regenerate, and REST API.
2. Cross-tenant IDOR download & hijack attempts: 404 response, zero AuditLog entries created.
3. Multi-iteration regeneration chain integrity: old_value faithfully retains previous_generation_id and figures.
4. Boundary and extreme Decimal JSON serialization without TypeError or numeric corruption.
5. Offline / CLI invocation safety when Flask request context is absent, plus string truncation boundary safety.
"""
import json
import uuid
import random
from datetime import date, datetime
from decimal import Decimal
import pytest

from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, GSTR1Generation, AuditLog
from app.services.audit_service import (
    log_generation_audit,
    extract_generation_summary,
    _safe_json_dumps,
    _json_serial
)
from app.services.gstr1_generator import generate_gstr1
from flask import has_request_context


@pytest.fixture
def audit_adversarial_env(app, db):
    """Set up two isolated tenants with valid and blocked return periods."""
    suffix_a = uuid.uuid4().hex[:6].upper()
    suffix_b = uuid.uuid4().hex[:6].upper()

    gstin_a = f"27ADV{suffix_a[:5]}1Z1"
    gstin_b = f"29ADV{suffix_b[:5]}1Z8"

    with app.app_context():
        # Tenant A
        user_a = User(username=f'adv_user_a_{suffix_a}', email=f'adv_a_{suffix_a}@example.com')
        user_a.set_password('AdvPassA123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=gstin_a,
            legal_name='Adversarial Tenant A Ltd',
            trade_name='Adv Tenant A',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B
        user_b = User(username=f'adv_user_b_{suffix_b}', email=f'adv_b_{suffix_b}@example.com')
        user_b.set_password('AdvPassB123!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=gstin_b,
            legal_name='Adversarial Tenant B Ltd',
            trade_name='Adv Tenant B',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Period 072024: CLEAN data for Tenant A
        imp_clean = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='072024',
            file_name='adv_clean_072024.xlsx',
            original_file_name='adv_clean_072024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=2,
            success_rows=2
        )
        _db.session.add(imp_clean)
        _db.session.commit()

        raw_c1 = RawImport(import_history_id=imp_clean.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        raw_c2 = RawImport(import_history_id=imp_clean.id, sheet_name='Sheet1', row_number=2, status='SUCCESS')
        _db.session.add_all([raw_c1, raw_c2])
        _db.session.commit()

        tx_c1 = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_clean.id,
            raw_import_id=raw_c1.id,
            invoice_number='INV-ADV-A-001',
            invoice_date=date(2024, 7, 5),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('10000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('900.00'),
            sgst_amount=Decimal('900.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('1800.00'),
            invoice_value=Decimal('11800.00'),
            is_deleted=False
        )
        tx_c2 = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_clean.id,
            raw_import_id=raw_c2.id,
            invoice_number='INV-ADV-A-002',
            invoice_date=date(2024, 7, 10),
            supply_type='B2CS',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('2000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('180.00'),
            sgst_amount=Decimal('180.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('360.00'),
            invoice_value=Decimal('2360.00'),
            is_deleted=False
        )
        _db.session.add_all([tx_c1, tx_c2])

        # Period 112024: BLOCKED data for Tenant A (impossible dual intra/inter taxes)
        imp_blocked = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='112024',
            file_name='adv_blocked_112024.xlsx',
            original_file_name='adv_blocked_112024.xlsx',
            platform_name='AMAZON',
            financial_year='2024-25',
            processing_status='COMPLETED',
            total_rows=1,
            success_rows=1
        )
        _db.session.add(imp_blocked)
        _db.session.commit()

        raw_b1 = RawImport(import_history_id=imp_blocked.id, sheet_name='Sheet1', row_number=1, status='SUCCESS')
        _db.session.add(raw_b1)
        _db.session.commit()

        tx_blocked = Transaction(
            profile_id=profile_a.id,
            import_history_id=imp_blocked.id,
            raw_import_id=raw_b1.id,
            invoice_number='INV-ADV-BLOCK-001',
            invoice_date=date(2024, 11, 2),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('5000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('450.00'),
            sgst_amount=Decimal('450.00'),
            igst_amount=Decimal('900.00'),  # Conflicting taxes triggers reconciliation failure
            total_tax=Decimal('1800.00'),
            invoice_value=Decimal('6800.00'),
            is_deleted=False
        )
        _db.session.add(tx_blocked)

        # Pre-create Generation for Tenant A in period 072024
        gen_res_a = generate_gstr1(str(profile_a.id), '072024')
        gen_a = GSTR1Generation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='072024',
            financial_year='2024-25',
            generation_status='COMPLETED',
            generation_started_at=datetime.utcnow(),
            generation_completed_at=datetime.utcnow(),
            excel_file_path=gen_res_a.excel_path,
            json_file_path=gen_res_a.json_path,
            total_b2b=1,
            total_b2cs=1,
            total_taxable_value=Decimal('12000.00'),
            total_cgst=Decimal('1080.00'),
            total_sgst=Decimal('1080.00'),
            total_igst=Decimal('0.00'),
            total_cess=Decimal('0.00'),
            total_tax=Decimal('2160.00'),
            validation_passed=True
        )
        _db.session.add(gen_a)

        # Pre-create Generation for Tenant B in period 072024
        gen_res_b = generate_gstr1(str(profile_b.id), '072024')
        gen_b = GSTR1Generation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='072024',
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
            'user_a_password': 'AdvPassA123!',
            'profile_a_id': profile_a.id,
            'gen_a_id': gen_a.id,
            'user_b_id': user_b.id,
            'user_b_username': user_b.username,
            'user_b_password': 'AdvPassB123!',
            'profile_b_id': profile_b.id,
            'gen_b_id': gen_b.id
        }


def _login(client, username, password):
    client.get('/logout', follow_redirects=True)
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


# =========================================================================
# Challenge 1: Blocked Period Audit Invariant (Zero Audit Logs Created)
# =========================================================================

def test_adversarial_blocked_period_zero_audit_log_matrix(client, audit_adversarial_env):
    """Stress-test generation-gate on a BLOCKED period across multiple routes and bypass vectors.

    Invariant: ZERO AuditLog entries must be written to the database.
    """
    _login(client, audit_adversarial_env['user_a_username'], audit_adversarial_env['user_a_password'])

    # Baseline audit log count for period 112024
    initial_audit_count = AuditLog.query.filter(
        AuditLog.new_value.like('%112024%')
    ).count()
    assert initial_audit_count == 0

    bypass_payloads = [
        {'return_period': '112024'},
        {'return_period': '112024', 'force': '1'},
        {'return_period': '112024', 'force': 'true'},
        {'return_period': '112024', 'bypass': 'true'},
        {'return_period': '112024', 'override': 'true'},
        {'return_period': '112024', 'enforce_gate': '0'},
        {'return_period': '112024', 'skip_reconciliation': '1'},
    ]

    # Vector 1: Web /generate/run with each bypass payload
    for payload in bypass_payloads:
        resp = client.post('/generate/run', data=payload, follow_redirects=False)
        assert resp.status_code == 302, f"Failed on web run payload {payload}"
        assert '/statement/validation-errors' in resp.location

    # Vector 2: Web /generate/regenerate with bypass payloads
    for payload in bypass_payloads:
        resp = client.post('/generate/regenerate', data=payload, follow_redirects=False)
        assert resp.status_code == 302, f"Failed on web regenerate payload {payload}"
        assert '/statement/validation-errors' in resp.location

    # Vector 3: REST API POST /api/generate with JSON payloads and headers
    api_payloads = [
        {'return_period': '112024'},
        {'return_period': '112024', 'force': True},
        {'return_period': '112024', 'bypass': True},
        {'return_period': '112024', 'override': True},
        {'return_period': '112024', 'enforce_gate': False},
        {'return_period': '112024', 'skip_reconciliation': True},
    ]

    for api_p in api_payloads:
        resp = client.post(
            '/api/generate',
            json=api_p,
            headers={'X-Bypass-Gate': 'true', 'X-Force-Generation': '1'}
        )
        assert resp.status_code == 403, f"Failed on API generate payload {api_p}"
        resp_data = resp.get_json()
        assert resp_data['status'] == 'BLOCKED'

    # STRICT INVARIANT ASSERTION: Count of AuditLog entries must still be exactly 0
    final_audit_count = AuditLog.query.filter(
        AuditLog.new_value.like('%112024%')
    ).count()
    assert final_audit_count == 0, (
        f"CRITICAL VIOLATION: {final_audit_count} AuditLog entries were created during blocked generation attempts!"
    )

    # Invariant: No GSTR1Generation record exists for 112024
    gen_record = GSTR1Generation.query.filter_by(
        profile_id=audit_adversarial_env['profile_a_id'],
        return_period='112024'
    ).first()
    assert gen_record is None, "CRITICAL VIOLATION: GSTR1Generation record was created for BLOCKED period!"


# =========================================================================
# Challenge 2: Cross-Tenant Download IDOR & Zero-Audit Invariant
# =========================================================================

def test_adversarial_cross_tenant_idor_zero_audit(client, audit_adversarial_env):
    """Stress-test cross-tenant authorization boundaries and ensure zero audit logs are created for IDOR attempts."""
    gen_a_id = audit_adversarial_env['gen_a_id']
    gen_b_id = audit_adversarial_env['gen_b_id']

    # Step 1: Tenant B logs in and attempts cross-tenant download of Tenant A's generation
    _login(client, audit_adversarial_env['user_b_username'], audit_adversarial_env['user_b_password'])

    # Attempt Excel download
    res_b_excel = client.get(f'/generate/download/excel/{gen_a_id}')
    assert res_b_excel.status_code == 404, "Tenant B must receive 404 when downloading Tenant A's Excel"

    # Attempt JSON download
    res_b_json = client.get(f'/generate/download/json/{gen_a_id}')
    assert res_b_json.status_code == 404, "Tenant B must receive 404 when downloading Tenant A's JSON"

    # Attempt status check
    res_b_status = client.get(f'/generate/status/{gen_a_id}')
    assert res_b_status.status_code == 404, "Tenant B must receive 404 when checking Tenant A's status"

    # Verify ZERO audit logs created for Tenant B attempting gen_a_id
    unauth_b_logs = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen_a_id,
        user_id=audit_adversarial_env['user_b_id']
    ).all()
    assert len(unauth_b_logs) == 0, "Unauthorized Tenant B attempt must produce ZERO AuditLog entries"

    # Step 2: Tenant A logs in and attempts cross-tenant download of Tenant B's generation
    _login(client, audit_adversarial_env['user_a_username'], audit_adversarial_env['user_a_password'])

    res_a_excel = client.get(f'/generate/download/excel/{gen_b_id}')
    assert res_a_excel.status_code == 404, "Tenant A must receive 404 when downloading Tenant B's Excel"

    res_a_json = client.get(f'/generate/download/json/{gen_b_id}')
    assert res_a_json.status_code == 404, "Tenant A must receive 404 when downloading Tenant B's JSON"

    unauth_a_logs = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        entity_id=gen_b_id,
        user_id=audit_adversarial_env['user_a_id']
    ).all()
    assert len(unauth_a_logs) == 0, "Unauthorized Tenant A attempt must produce ZERO AuditLog entries"

    # Step 3: Anonymous unauthenticated client attempt
    client.get('/logout', follow_redirects=True)
    res_anon_excel = client.get(f'/generate/download/excel/{gen_a_id}', follow_redirects=False)
    assert res_anon_excel.status_code == 302, "Anonymous download must redirect to login"

    res_anon_json = client.get(f'/generate/download/json/{gen_a_id}', follow_redirects=False)
    assert res_anon_json.status_code == 302, "Anonymous download must redirect to login"

    anon_logs = AuditLog.query.filter_by(
        entity_type='GSTR1Generation',
        action='DOWNLOAD',
        user_id=None
    ).all()
    assert len(anon_logs) == 0, "Anonymous download attempt must produce ZERO AuditLog entries"

    # Step 4: Cross-tenant regeneration hijack attempt
    # Tenant B attempts to regenerate using generation_id=gen_a_id
    _login(client, audit_adversarial_env['user_b_username'], audit_adversarial_env['user_b_password'])
    client.post('/generate/regenerate', data={'generation_id': gen_a_id, 'return_period': '072024'}, follow_redirects=True)

    # Verify Tenant A's generation was NEVER modified or overwritten
    gen_a_check = _db.session.get(GSTR1Generation, gen_a_id)
    assert gen_a_check.profile_id == audit_adversarial_env['profile_a_id']
    assert gen_a_check.user_id == audit_adversarial_env['user_a_id']
    assert float(gen_a_check.total_taxable_value) == 12000.0

    # Verify Tenant B's audit log never leaked Tenant A's figures in old_value
    b_regen_log = AuditLog.query.filter_by(
        user_id=audit_adversarial_env['user_b_id'],
        action='REGENERATE'
    ).order_by(AuditLog.id.desc()).first()

    if b_regen_log and b_regen_log.old_value:
        old_val = json.loads(b_regen_log.old_value)
        # Must not contain Tenant A's generation id
        assert old_val.get('previous_generation_id') != gen_a_id
        assert old_val.get('profile_id') != audit_adversarial_env['profile_a_id']


# =========================================================================
# Challenge 3: Multi-Iteration Regeneration Chain Integrity
# =========================================================================

def test_adversarial_regeneration_chain_integrity(app, client, audit_adversarial_env):
    """Stress-test a multi-step iterative regeneration chain.

    Verifies old_value faithfully retains previous_generation_id and preceding figures across each link in the chain.
    """
    _login(client, audit_adversarial_env['user_a_username'], audit_adversarial_env['user_a_password'])
    profile_id = audit_adversarial_env['profile_a_id']

    # Step 0: Initial generation (G0)
    client.post('/generate/run', data={'return_period': '072024'}, follow_redirects=True)
    g0 = GSTR1Generation.query.filter_by(profile_id=profile_id, return_period='072024').order_by(GSTR1Generation.id.desc()).first()
    assert g0 is not None
    g0_id = g0.id
    val_step0 = float(g0.total_taxable_value)
    tax_step0 = float(g0.total_tax)

    # Verify CREATE audit log
    log0 = AuditLog.query.filter_by(entity_type='GSTR1Generation', entity_id=g0_id, action='CREATE').first()
    assert log0 is not None
    assert log0.old_value is None

    # Step 1: Add new transaction and Regenerate iteration 1
    with app.app_context():
        imp = ImportHistory.query.filter_by(profile_id=profile_id, return_period='072024').first()
        raw = RawImport(import_history_id=imp.id, sheet_name='Sheet1', row_number=99, status='SUCCESS')
        _db.session.add(raw)
        _db.session.commit()

        tx_new1 = Transaction(
            profile_id=profile_id,
            import_history_id=imp.id,
            raw_import_id=raw.id,
            invoice_number='INV-REGEN-CHAIN-1',
            invoice_date=date(2024, 7, 15),
            supply_type='B2B',
            customer_gstin='27GHIJK5678L1Z9',
            place_of_supply='27',
            hsn_sac='8471',
            taxable_value=Decimal('3000.00'),
            tax_rate=Decimal('18.00'),
            cgst_amount=Decimal('270.00'),
            sgst_amount=Decimal('270.00'),
            igst_amount=Decimal('0.00'),
            cess_amount=Decimal('0.00'),
            total_tax=Decimal('540.00'),
            invoice_value=Decimal('3540.00'),
            is_deleted=False
        )
        _db.session.add(tx_new1)
        _db.session.commit()

    # Trigger Regeneration 1 with explicit generation_id
    res1 = client.post('/generate/regenerate', data={'return_period': '072024', 'generation_id': g0_id}, follow_redirects=True)
    assert res1.status_code == 200

    log1 = AuditLog.query.filter_by(entity_type='GSTR1Generation', entity_id=g0_id, action='REGENERATE').order_by(AuditLog.id.desc()).first()
    assert log1 is not None
    old1 = json.loads(log1.old_value)
    new1 = json.loads(log1.new_value)

    assert old1['previous_generation_id'] == g0_id
    assert float(old1['total_taxable_value']) == val_step0
    assert float(old1['total_tax']) == tax_step0
    assert float(new1['total_taxable_value']) == val_step0 + 3000.0
    assert float(new1['total_tax']) == tax_step0 + 540.0
    assert new1['previous_generation_id'] == g0_id

    val_step1 = float(new1['total_taxable_value'])
    tax_step1 = float(new1['total_tax'])

    # Step 2: Modify existing transaction and Regenerate iteration 2
    with app.app_context():
        tx_mod = Transaction.query.filter_by(profile_id=profile_id, invoice_number='INV-REGEN-CHAIN-1').first()
        tx_mod.taxable_value = Decimal('8000.00')
        tx_mod.cgst_amount = Decimal('720.00')
        tx_mod.sgst_amount = Decimal('720.00')
        tx_mod.total_tax = Decimal('1440.00')
        tx_mod.invoice_value = Decimal('9440.00')
        _db.session.commit()

    # Trigger Regeneration 2
    res2 = client.post('/generate/regenerate', data={'return_period': '072024', 'generation_id': g0_id}, follow_redirects=True)
    assert res2.status_code == 200

    log2 = AuditLog.query.filter_by(entity_type='GSTR1Generation', entity_id=g0_id, action='REGENERATE').order_by(AuditLog.id.desc()).first()
    assert log2 is not None
    assert log2.id != log1.id

    old2 = json.loads(log2.old_value)
    new2 = json.loads(log2.new_value)

    assert old2['previous_generation_id'] == g0_id
    assert float(old2['total_taxable_value']) == val_step1
    assert float(old2['total_tax']) == tax_step1
    assert float(new2['total_taxable_value']) == val_step1 + 5000.0  # +5000 diff
    assert float(new2['total_tax']) == tax_step1 + 900.0

    val_step2 = float(new2['total_taxable_value'])
    tax_step2 = float(new2['total_tax'])

    # Step 3: Trigger Regeneration 3 without generation_id (creates a new generation record G1)
    res3 = client.post('/generate/regenerate', data={'return_period': '072024'}, follow_redirects=True)
    assert res3.status_code == 200

    log3 = AuditLog.query.filter_by(entity_type='GSTR1Generation', action='REGENERATE').order_by(AuditLog.id.desc()).first()
    assert log3 is not None
    assert log3.id != log2.id

    old3 = json.loads(log3.old_value)
    new3 = json.loads(log3.new_value)

    assert old3['previous_generation_id'] == g0_id
    assert float(old3['total_taxable_value']) == val_step2
    assert float(new3['total_taxable_value']) == val_step2
    # Verify the lineage connects g0_id to the new generation
    assert new3['previous_generation_id'] == g0_id


# =========================================================================
# Challenge 4: Extreme / Boundary Decimal JSON Serialization Safety
# =========================================================================

def test_adversarial_boundary_decimal_serialization(app, db, audit_adversarial_env):
    """Stress-test JSON serialization with extreme, negative, high-precision, and boundary Decimals."""
    with app.app_context():
        # Sub-test 4A: Boundary values in _json_serial and _safe_json_dumps
        extreme_payload = {
            "astronomical": Decimal('999999999999999999.99'),
            "micro_cent": Decimal('0.00000001'),
            "negative_large": Decimal('-888888888888.88'),
            "exact_zero": Decimal('0.00'),
            "negative_zero": Decimal('-0.00'),
            "high_precision": Decimal('12345.67890123456789'),
            "nested": {
                "level1": {
                    "dec_list": [Decimal('1.11'), Decimal('2.22'), Decimal('-3.33')],
                    "deep_val": Decimal('99.9999')
                }
            },
            "timestamp": datetime(2026, 10, 1, 14, 30, 0),
            "simple_date": date(2026, 10, 1)
        }

        # Must not raise TypeError: Object of type Decimal is not JSON serializable
        serialized = _safe_json_dumps(extreme_payload)
        assert isinstance(serialized, str)

        parsed = json.loads(serialized)
        assert parsed['astronomical'] == 999999999999999999.99
        assert parsed['micro_cent'] == 0.00000001
        assert parsed['negative_large'] == -888888888888.88
        assert parsed['exact_zero'] == 0.0
        assert parsed['nested']['level1']['dec_list'] == [1.11, 2.22, -3.33]
        assert parsed['timestamp'] == '2026-10-01T14:30:00'
        assert parsed['simple_date'] == '2026-10-01'

        # Sub-test 4B: Model-level extreme decimal persistence and audit logging
        gen_extreme = GSTR1Generation(
            profile_id=audit_adversarial_env['profile_a_id'],
            user_id=audit_adversarial_env['user_a_id'],
            return_period='122024',
            financial_year='2024-25',
            generation_status='COMPLETED',
            total_taxable_value=Decimal('9999999999.99'),
            total_cgst=Decimal('899999999.99'),
            total_sgst=Decimal('899999999.99'),
            total_igst=Decimal('0.00'),
            total_cess=Decimal('0.00'),
            total_tax=Decimal('1799999999.98')
        )
        _db.session.add(gen_extreme)
        _db.session.commit()

        # Audit log creation with boundary model
        log_entry = log_generation_audit(
            user_id=audit_adversarial_env['user_a_id'],
            action='CREATE',
            generation=gen_extreme,
            return_period='122024',
            profile_id=audit_adversarial_env['profile_a_id'],
            commit=True
        )

        assert log_entry.id is not None
        log_payload = json.loads(log_entry.new_value)
        assert log_payload['total_taxable_value'] == 9999999999.99
        assert log_payload['total_tax'] == 1799999999.98


# =========================================================================
# Challenge 5: Offline / Non-Request Context & Sizing Boundary Safety
# =========================================================================

def test_adversarial_request_context_safety_and_ip_boundaries(app, db, audit_adversarial_env):
    """Stress-test audit logging outside request context (CLI/cron) and with extreme field lengths."""
    # Sub-test 5A: Standalone execution with zero request context
    assert not has_request_context(), "Precondition: test must execute outside Flask request context"

    gen_record = _db.session.get(GSTR1Generation, audit_adversarial_env['gen_a_id'])

    # Invocation without request context must not throw RuntimeError
    offline_log = log_generation_audit(
        user_id=audit_adversarial_env['user_a_id'],
        action='CREATE',
        generation=gen_record,
        return_period='072024',
        profile_id=audit_adversarial_env['profile_a_id'],
        commit=True
    )
    assert offline_log.id is not None
    assert offline_log.ip_address is None

    # Sub-test 5B: Long IP address string slicing safety (exceeding varchar 45 limit)
    adversarial_long_ip = "2001:0db8:85a3:0000:0000:8a2e:0370:7334, 192.168.1.100, 10.0.0.1, 172.16.0.1"
    assert len(adversarial_long_ip) > 45

    long_ip_log = log_generation_audit(
        user_id=audit_adversarial_env['user_a_id'],
        action='DOWNLOAD',
        generation=gen_record,
        format='excel',
        ip_address=adversarial_long_ip,
        commit=True
    )
    assert long_ip_log.id is not None
    assert len(long_ip_log.ip_address) <= 45
    assert long_ip_log.ip_address == adversarial_long_ip[:45]

    # Sub-test 5C: Long reason string slicing safety (exceeding varchar 255 limit)
    adversarial_long_reason = "REASON_" + ("A" * 300)
    assert len(adversarial_long_reason) > 255

    long_reason_log = log_generation_audit(
        user_id=audit_adversarial_env['user_a_id'],
        action='REGENERATE',
        generation=gen_record,
        reason=adversarial_long_reason,
        commit=True
    )
    assert long_reason_log.id is not None
    assert len(long_reason_log.reason) <= 255
    assert long_reason_log.reason == adversarial_long_reason[:255]

    # Sub-test 5D: Case-insensitive action handling and action truncation
    weird_action_log = log_generation_audit(
        user_id=audit_adversarial_env['user_a_id'],
        action='  regenerate  ',
        generation=gen_record,
        commit=True
    )
    assert weird_action_log.action == 'REGENERATE'

    long_action_log = log_generation_audit(
        user_id=audit_adversarial_env['user_a_id'],
        action='VERY_LONG_CUSTOM_ACTION_EXCEEDING_LIMIT',
        generation=gen_record,
        commit=True
    )
    assert len(long_action_log.action) <= 20
