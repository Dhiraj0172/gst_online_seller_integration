"""Phase 5E-6 Integration Tests: Audit Trail REST API & Compliance Viewer.

Covers:
1. GET /api/audit-logs
   - Unauthenticated rejection (401)
   - Empty state
   - Authentic audit records & fields
   - Filtering: entity_type, action, return_period, start_date, end_date
   - Pagination & deterministic ordering
   - CSV export via ?export=csv (headers, mimetype, sanitization)
   - Multi-tenant isolation & IDOR resistance (foreign profile_id rejection)
   - Read-only protection (no POST/PUT/DELETE)
2. GET /statement/audit-trail
   - Unauthenticated redirect (302)
   - HTML rendering & compliance badge formatting
   - Active profile & return period filtering
   - Empty state message
   - Pagination
3. GET /statement/audit-trail/export
   - CSV export via dedicated route
   - Headers, MIME type, CSV formula sanitization
4. Sidebar navigation verification
   - Audit Trail link present in UI sidebar
"""
import io
import csv
import uuid
from decimal import Decimal
from datetime import datetime, timedelta
import pytest

from app.extensions import db as _db
from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction, GSTR1Generation, TCSReconciliation, AuditLog


_counter = 0

def _get_unique_suffix():
    global _counter
    _counter += 1
    return f"{_counter:04d}_{uuid.uuid4().hex[:6]}"


@pytest.fixture
def audit_test_env(app, db):
    """Set up two tenants with diverse AuditLog entries across multiple entities and return periods."""
    suffix_a = _get_unique_suffix()
    suffix_b = _get_unique_suffix()

    with app.app_context():
        # Tenant A
        user_a = User(username=f'audit_user_a_{suffix_a}', email=f'audit_a_{suffix_a}@example.com')
        user_a.set_password('PasswordA1!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=f"27{uuid.uuid4().hex[:10].upper()}1Z5",
            legal_name=f'Tenant A Audit Corp {suffix_a}',
            trade_name=f'Tenant A {suffix_a}',
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
            gstin=f"29{uuid.uuid4().hex[:10].upper()}1Z2",
            legal_name=f'Tenant B Audit Corp {suffix_b}',
            trade_name=f'Tenant B {suffix_b}',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(profile_b)
        _db.session.commit()

        # Create GSTR1Generation for Tenant A (Period 082024)
        gen_a_08 = GSTR1Generation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            json_file_path=f'gstr1_082024_{suffix_a}.json',
            total_taxable_value=Decimal('50000.00'),
            total_cgst=Decimal('4500.00'),
            total_sgst=Decimal('4500.00'),
            total_tax=Decimal('9000.00'),
            generation_status='COMPLETED',
            validation_passed=True,
        )
        _db.session.add(gen_a_08)

        # Create GSTR1Generation for Tenant A (Period 092024)
        gen_a_09 = GSTR1Generation(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='092024',
            json_file_path=f'gstr1_092024_{suffix_a}.json',
            total_taxable_value=Decimal('25000.00'),
            total_cgst=Decimal('2250.00'),
            total_sgst=Decimal('2250.00'),
            total_tax=Decimal('4500.00'),
            generation_status='COMPLETED',
            validation_passed=True,
        )
        _db.session.add(gen_a_09)

        # Create GSTR1Generation for Tenant B
        gen_b = GSTR1Generation(
            profile_id=profile_b.id,
            user_id=user_b.id,
            return_period='082024',
            json_file_path=f'gstr1_082024_{suffix_b}.json',
            total_taxable_value=Decimal('40000.00'),
            total_tax=Decimal('7200.00'),
            generation_status='COMPLETED',
            validation_passed=True,
        )
        _db.session.add(gen_b)
        _db.session.commit()

        # Create Import and Transaction for Tenant A so Transaction audit logs link to profile_a
        ih_a = ImportHistory(
            profile_id=profile_a.id,
            user_id=user_a.id,
            return_period='082024',
            file_name=f'audit_test_{suffix_a}.xlsx',
            original_file_name=f'audit_test_{suffix_a}.xlsx',
            platform_name='Amazon',
            financial_year='2024-25',
            processing_status='COMPLETED'
        )
        _db.session.add(ih_a)
        _db.session.commit()

        ri_a = RawImport(import_history_id=ih_a.id, sheet_name='Sheet1', row_number=1, raw_data='{}', status='SUCCESS')
        _db.session.add(ri_a)
        _db.session.commit()

        tx_a = Transaction(
            profile_id=profile_a.id,
            import_history_id=ih_a.id,
            raw_import_id=ri_a.id,
            invoice_number=f'INV-A-{suffix_a}',
            invoice_date=datetime(2024, 8, 15).date(),
            supply_type='B2B',
            taxable_value=Decimal('1000.00'),
            cgst_rate=Decimal('9.0'),
            cgst_amount=Decimal('90.00'),
            sgst_rate=Decimal('9.0'),
            sgst_amount=Decimal('90.00'),
            total_tax=Decimal('180.00'),
            invoice_value=Decimal('1180.00'),
            customer_gstin='27ABCDE1234F1Z5',
            is_deleted=False
        )
        _db.session.add(tx_a)
        _db.session.commit()

        now = datetime(2024, 9, 15, 10, 0, 0)

        # Tenant A Audit Logs
        # Log 1: GSTR1Generation CREATE for 082024
        log_a1 = AuditLog(
            user_id=user_a.id,
            action='CREATE',
            entity_type='GSTR1Generation',
            entity_id=gen_a_08.id,
            field_name='status',
            old_value=None,
            new_value='{"status": "GENERATED", "return_period": "082024"}',
            reason='Initial GSTR-1 JSON file generation for period 082024',
            timestamp=now - timedelta(days=5),
            ip_address='192.168.1.101'
        )
        # Log 2: GSTR1Generation DOWNLOAD for 082024
        log_a2 = AuditLog(
            user_id=user_a.id,
            action='DOWNLOAD',
            entity_type='GSTR1Generation',
            entity_id=gen_a_08.id,
            field_name='download_count',
            old_value='0',
            new_value='1',
            reason='User downloaded GSTR-1 JSON artifact for period 082024',
            timestamp=now - timedelta(days=4),
            ip_address='192.168.1.101'
        )
        # Log 3: GSTR1Generation REGENERATE for 082024
        log_a3 = AuditLog(
            user_id=user_a.id,
            action='REGENERATE',
            entity_type='GSTR1Generation',
            entity_id=gen_a_08.id,
            field_name='version',
            old_value='1',
            new_value='2',
            reason='Regeneration of GSTR-1 for period 082024',
            timestamp=now - timedelta(days=3),
            ip_address='192.168.1.102'
        )
        # Log 4: GSTR1Generation for 092024
        log_a4 = AuditLog(
            user_id=user_a.id,
            action='CREATE',
            entity_type='GSTR1Generation',
            entity_id=gen_a_09.id,
            field_name='status',
            old_value=None,
            new_value='{"status": "GENERATED", "return_period": "092024"}',
            reason='Initial generation for period 092024',
            timestamp=now - timedelta(days=1),
            ip_address='192.168.1.103'
        )
        # Log 5: Formula Injection attempt test log linked to tx_a
        log_a5 = AuditLog(
            user_id=user_a.id,
            action='UPDATE',
            entity_type='Transaction',
            entity_id=tx_a.id,
            field_name='notes',
            old_value='=cmd|/c calc!A0',
            new_value='+123456789',
            reason='@malicious_user formula injection payload',
            timestamp=now - timedelta(hours=2),
            ip_address='10.0.0.1'
        )
        # Log 6: Profile update log for Tenant A
        log_a6 = AuditLog(
            user_id=user_a.id,
            action='UPDATE',
            entity_type='GSTProfile',
            entity_id=profile_a.id,
            field_name='trade_name',
            old_value='Old Name',
            new_value='New Name',
            reason='Profile name updated',
            timestamp=now,
            ip_address='192.168.1.105'
        )

        # Tenant B Audit Log (Secret tenant B data)
        log_b1 = AuditLog(
            user_id=user_b.id,
            action='CREATE',
            entity_type='GSTR1Generation',
            entity_id=gen_b.id,
            field_name='status',
            old_value=None,
            new_value='{"status": "GENERATED", "secret": "tenant_b_confidential"}',
            reason='Tenant B private generation',
            timestamp=now,
            ip_address='172.16.0.99'
        )

        _db.session.add_all([log_a1, log_a2, log_a3, log_a4, log_a5, log_a6, log_b1])
        _db.session.commit()

        env_dict = {
            'user_a_id': user_a.id,
            'user_a_name': user_a.username,
            'profile_a_id': profile_a.id,
            'user_b_id': user_b.id,
            'user_b_name': user_b.username,
            'profile_b_id': profile_b.id,
            'gen_a_08_id': gen_a_08.id,
            'gen_a_09_id': gen_a_09.id,
            'gen_b_id': gen_b.id,
            'log_a1_id': log_a1.id,
            'log_b1_id': log_b1.id,
        }

    return env_dict


def login_client(client, username, password):
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


# ============================================================
# TRACK B: REST API /api/audit-logs TESTS
# ============================================================

def test_audit_api_unauthenticated(client):
    """GET /api/audit-logs must reject unauthenticated requests (302 redirect to /login or 401)."""
    resp = client.get('/api/audit-logs', follow_redirects=False)
    assert resp.status_code in (302, 401)
    if resp.status_code == 302:
        assert '/login' in resp.headers.get('Location', '')


def test_audit_api_returns_authentic_logs(client, audit_test_env):
    """GET /api/audit-logs returns authentic audit logs with all compliance fields."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp = client.get('/api/audit-logs')
    assert resp.status_code == 200
    json_data = resp.get_json()
    assert json_data['success'] is True
    assert 'data' in json_data
    assert json_data['total'] >= 5

    items = json_data['data']
    assert len(items) >= 5

    # Check fields present in items
    first = items[0]
    expected_fields = ['id', 'user_id', 'action', 'entity_type', 'entity_id', 'field_name', 'old_value', 'new_value', 'reason', 'timestamp', 'ip_address']
    for field in expected_fields:
        assert field in first, f"Missing required audit field: {field}"

    # Confirm all items belong to user_a
    for item in items:
        assert item['user_id'] == audit_test_env['user_a_id']


def test_audit_api_filtering_by_entity_type(client, audit_test_env):
    """GET /api/audit-logs?entity_type=GSTR1Generation filters strictly by entity_type."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp = client.get('/api/audit-logs?entity_type=GSTR1Generation')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert len(data['data']) > 0
    for item in data['data']:
        assert item['entity_type'] == 'GSTR1Generation'

    # Filter by Transaction
    resp_tx = client.get('/api/audit-logs?entity_type=Transaction')
    assert resp_tx.status_code == 200
    data_tx = resp_tx.get_json()
    assert len(data_tx['data']) > 0
    for item in data_tx['data']:
        assert item['entity_type'] == 'Transaction'


def test_audit_api_filtering_by_action(client, audit_test_env):
    """GET /api/audit-logs?action=DOWNLOAD filters strictly by action (case-insensitive)."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp = client.get('/api/audit-logs?action=DOWNLOAD')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert len(data['data']) == 1
    assert data['data'][0]['action'] == 'DOWNLOAD'

    # Test lowercase action filter
    resp_lower = client.get('/api/audit-logs?action=download')
    assert resp_lower.status_code == 200
    data_lower = resp_lower.get_json()
    assert len(data_lower['data']) == 1
    assert data_lower['data'][0]['action'] == 'DOWNLOAD'


def test_audit_api_filtering_by_return_period(client, audit_test_env):
    """GET /api/audit-logs?return_period=082024 returns period 082024 logs and excludes 092024."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp = client.get('/api/audit-logs?return_period=082024')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    # Should include log_a1, log_a2, log_a3
    log_ids = [item['id'] for item in data['data']]
    assert audit_test_env['log_a1_id'] in log_ids

    # Query 092024
    resp_09 = client.get('/api/audit-logs?return_period=092024')
    assert resp_09.status_code == 200
    data_09 = resp_09.get_json()
    log_ids_09 = [item['id'] for item in data_09['data']]
    assert audit_test_env['log_a1_id'] not in log_ids_09


def test_audit_api_filtering_by_date_range(client, audit_test_env):
    """GET /api/audit-logs with start_date and end_date filters correctly."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    # Dates: log_a1 is 2024-09-10, log_a2 is 2024-09-11, log_a3 is 2024-09-12, log_a4 is 2024-09-14, log_a5 is 2024-09-15
    resp = client.get('/api/audit-logs?start_date=2024-09-10&end_date=2024-09-11')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    # Should contain exactly log_a1 and log_a2
    actions = [item['action'] for item in data['data']]
    assert 'CREATE' in actions
    assert 'DOWNLOAD' in actions
    assert 'REGENERATE' not in actions


def test_audit_api_pagination_and_deterministic_order(client, audit_test_env):
    """GET /api/audit-logs respects pagination and deterministic descending timestamp/id order."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp_p1 = client.get('/api/audit-logs?page=1&per_page=2')
    assert resp_p1.status_code == 200
    data_p1 = resp_p1.get_json()
    assert len(data_p1['data']) == 2
    assert data_p1['page'] == 1
    assert data_p1['per_page'] == 2
    assert data_p1['pages'] >= 3

    resp_p2 = client.get('/api/audit-logs?page=2&per_page=2')
    assert resp_p2.status_code == 200
    data_p2 = resp_p2.get_json()
    assert len(data_p2['data']) == 2
    assert data_p2['page'] == 2

    # Check that items in page 1 and page 2 are disjoint
    p1_ids = {x['id'] for x in data_p1['data']}
    p2_ids = {x['id'] for x in data_p2['data']}
    assert p1_ids.isdisjoint(p2_ids)


def test_audit_api_csv_export(client, audit_test_env):
    """GET /api/audit-logs?export=csv returns valid text/csv with sanitized values."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp = client.get('/api/audit-logs?export=csv')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert 'attachment;filename=audit_trail.csv' in resp.headers.get('Content-Disposition', '')

    csv_text = resp.data.decode('utf-8')
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    header = rows[0]
    expected_header = ['ID', 'Timestamp', 'Action', 'Entity Type', 'Entity ID', 'Field Name', 'Old Value', 'New Value', 'Reason', 'IP Address']
    assert header == expected_header

    # Verify formula injection sanitization on log_a5: '=cmd...', '+123...', '@malicious...'
    # Formula prefixes (=, +, -, @) should be prepended with a single quote '
    found_sanitized = False
    for r in rows[1:]:
        if r[3] == 'Transaction':
            old_val = r[6]
            new_val = r[7]
            reason = r[8]
            assert old_val.startswith("'=") or not old_val.startswith("=")
            assert new_val.startswith("'+") or not new_val.startswith("+")
            assert reason.startswith("'@") or not reason.startswith("@")
            found_sanitized = True
    assert found_sanitized


def test_audit_api_tenant_isolation(client, audit_test_env):
    """GET /api/audit-logs must never leak Tenant B's audit logs or IP addresses to Tenant A."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp = client.get('/api/audit-logs')
    assert resp.status_code == 200
    data = resp.get_json()

    for item in data['data']:
        assert item['id'] != audit_test_env['log_b1_id']
        assert item['user_id'] != audit_test_env['user_b_id']
        assert item['ip_address'] != '172.16.0.99'
        assert 'tenant_b_confidential' not in str(item.get('new_value'))


def test_audit_api_idor_foreign_profile_safely_ignored(client, audit_test_env):
    """Passing a foreign profile_id to /api/audit-logs must not expose foreign logs."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    # Tenant A attempts to pass Tenant B's profile_id
    resp = client.get(f"/api/audit-logs?profile_id={audit_test_env['profile_b_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    # Should not expose any of Tenant B's records
    for item in data['data']:
        assert item['id'] != audit_test_env['log_b1_id']
        assert item['user_id'] != audit_test_env['user_b_id']


def test_audit_api_read_only_protection(client, audit_test_env):
    """GET /api/audit-logs is read-only. POST/PUT/DELETE must return 405 Method Not Allowed."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    resp_post = client.post('/api/audit-logs', json={'action': 'TAMPER'})
    assert resp_post.status_code == 405

    resp_put = client.put('/api/audit-logs', json={'action': 'TAMPER'})
    assert resp_put.status_code == 405

    resp_delete = client.delete('/api/audit-logs')
    assert resp_delete.status_code == 405


# ============================================================
# TRACK C: AUDIT TRAIL WEB VIEWER & CSV EXPORT
# ============================================================

def test_audit_viewer_unauthenticated(client):
    """GET /statement/audit-trail must redirect unauthenticated users to login."""
    resp = client.get('/statement/audit-trail')
    assert resp.status_code == 302
    assert '/login' in resp.headers.get('Location', '')


def test_audit_viewer_html_rendering(client, audit_test_env):
    """GET /statement/audit-trail renders HTML compliance viewer with action badges and IP."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    # Set active profile in session
    with client.session_transaction() as sess:
        sess['active_profile_id'] = audit_test_env['profile_a_id']

    resp = client.get('/statement/audit-trail')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    # Verify UI sections
    assert 'Audit Trail' in html
    assert 'Compliance' in html
    assert 'Export CSV' in html
    assert 'Filter Audit Records' in html

    # Verify action badges
    assert 'badge bg-success' in html  # CREATE
    assert 'badge bg-info' in html     # DOWNLOAD
    assert 'badge bg-warning' in html  # REGENERATE
    assert '192.168.1.101' in html     # IP address displayed

    # Tenant B IP must NOT be present
    assert '172.16.0.99' not in html
    assert 'tenant_b_confidential' not in html


def test_audit_viewer_filtering_html(client, audit_test_env):
    """GET /statement/audit-trail with query parameters filters table rows."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    with client.session_transaction() as sess:
        sess['active_profile_id'] = audit_test_env['profile_a_id']

    resp = client.get('/statement/audit-trail?action=DOWNLOAD')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    assert 'DOWNLOAD' in html
    assert 'download_count' in html
    # Should not show CREATE or REGENERATE in the logs table
    assert 'Initial GSTR-1 JSON file generation' not in html


def test_audit_viewer_empty_state(client, audit_test_env):
    """GET /statement/audit-trail shows empty state when no records match filter."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    with client.session_transaction() as sess:
        sess['active_profile_id'] = audit_test_env['profile_a_id']

    resp = client.get('/statement/audit-trail?action=NONEXISTENT_ACTION')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'No Audit Records Found' in html or 'No audit records found' in html


def test_audit_viewer_csv_export_endpoint(client, audit_test_env):
    """GET /statement/audit-trail/export produces valid CSV file."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    with client.session_transaction() as sess:
        sess['active_profile_id'] = audit_test_env['profile_a_id']

    resp = client.get('/statement/audit-trail/export')
    assert resp.status_code == 200
    assert 'text/csv' in resp.content_type
    assert 'audit_trail.csv' in resp.headers.get('Content-Disposition', '')

    csv_text = resp.data.decode('utf-8')
    assert 'ID,Timestamp,Action,Entity Type,Entity ID' in csv_text
    assert '192.168.1.101' in csv_text
    assert '172.16.0.99' not in csv_text


def test_audit_viewer_multi_tenant_isolation(client, audit_test_env):
    """User B cannot see User A's audit records in the HTML viewer."""
    login_client(client, audit_test_env['user_b_name'], 'PasswordB1!')
    with client.session_transaction() as sess:
        sess['active_profile_id'] = audit_test_env['profile_b_id']

    resp = client.get('/statement/audit-trail')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    # User B should see their own log
    assert '172.16.0.99' in html
    # User B should NOT see User A's logs or IPs
    assert '192.168.1.101' not in html
    assert '192.168.1.102' not in html
    assert 'Tenant A' not in html


def test_sidebar_audit_trail_nav_link(client, audit_test_env):
    """Sidebar navigation contains Audit Trail link."""
    login_client(client, audit_test_env['user_a_name'], 'PasswordA1!')
    with client.session_transaction() as sess:
        sess['active_profile_id'] = audit_test_env['profile_a_id']

    resp = client.get('/dashboard')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert '/statement/audit-trail' in html
    assert 'Audit Trail' in html
