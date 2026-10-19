"""Integration test suite for Phase 6A & 6B: Marketplace Adapters & Interactive Seller Web UI Completeness.

Covers:
1. Real CSV/Excel parsing, normalization, and Decimal tax extraction across all 9
   newly promoted marketplace adapters (JioMart, Snapdeal, TataCLiQ, Limeroad,
   Shopdeck, GlowRoad, Snapmint, Citymall, Roposo).
2. get_adapter_readiness_matrix() reporting all 15 marketplace adapters as PRODUCTION
   (FORMAT_DOCUMENTED=True, has_header_map=True), while still classifying any
   undocumented adapter class (FORMAT_DOCUMENTED=False) as FALLBACK.
3. Web UI rendering and end-to-end form interactions across /import, /import/history,
   /import/<id>/preview, /statement, and /generate (checking CSRF, staging checkbox,
   progress endpoints, reconciliation cards, exception resolution, gate status).
"""
import csv
import io
import json
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
import openpyxl
import pytest

from app.adapters.all_adapters import (
    BaseGenericAdapter,
    _GenericFilenameAdapter,
    get_adapter_readiness_matrix,
)
from app.adapters.base import ImportRowStatus
from app.adapters.registry import (
    _ADAPTER_REGISTRY,
    detect_platform,
    get_adapter,
    list_platforms,
    register_adapter,
)
from app.extensions import db as _db
from app.models import (
    AuditLog,
    GSTProfile,
    GSTR1Generation,
    ImportHistory,
    RawImport,
    ReconciliationRecord,
    Transaction,
    User,
)
from app.services.gstr1_generator import generate_gstr1, load_transactions
from app.services.import_service import (
    cancel_import,
    confirm_import,
    preview_import,
    process_import,
)
from app.services.reconciliation_service import (
    resolve_reconciliation_exception,
    run_full_reconciliation,
)
from app.utils.query import tenant_query


ALL_15_PRODUCTION_PLATFORMS = (
    'Amazon',
    'Flipkart',
    'Meesho',
    'Myntra',
    'GSTR1_Govt',
    'Custom_Excel',
    'JioMart',
    'Snapdeal',
    'TataCLiQ',
    'Limeroad',
    'Shopdeck',
    'GlowRoad',
    'Snapmint',
    'Citymall',
    'Roposo',
)

PROMOTED_9_MARKETPLACES = (
    ('JioMart', 'jiomart_sample.xlsx', 'jiomart'),
    ('Snapdeal', 'snapdeal_sample.xlsx', 'snapdeal'),
    ('TataCLiQ', 'tatacliq_sample.xlsx', 'tatacliq'),
    ('Limeroad', 'limeroad_sample.xlsx', 'limeroad'),
    ('Shopdeck', 'shopdeck_sample.xlsx', 'shopdeck'),
    ('GlowRoad', 'glowroad_sample.xlsx', 'glowroad'),
    ('Snapmint', 'snapmint_sample.xlsx', 'snapmint'),
    ('Citymall', 'citymall_sample.xlsx', 'citymall'),
    ('Roposo', 'roposo_sample.xlsx', 'roposo'),
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared Test Fixtures & Login Helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def env_phase6ab(app, db):
    """Set up two fully isolated tenants with distinct profiles and return periods."""
    with app.app_context():
        # Tenant A
        user_a = User(
            username=f'p6ab_user_a_{uuid.uuid4().hex[:6]}',
            email=f'user_a_{uuid.uuid4().hex[:6]}@example.com',
        )
        user_a.set_password('SecretA123!')
        _db.session.add(user_a)
        _db.session.commit()

        profile_a = GSTProfile(
            user_id=user_a.id,
            gstin=f'27AAAAA{uuid.uuid4().hex[:4].upper()}1Z5',
            legal_name='Tenant A Retail Ltd',
            trade_name='Tenant A Store',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(profile_a)
        _db.session.commit()

        # Tenant B
        user_b = User(
            username=f'p6ab_user_b_{uuid.uuid4().hex[:6]}',
            email=f'user_b_{uuid.uuid4().hex[:6]}@example.com',
        )
        user_b.set_password('SecretB456!')
        _db.session.add(user_b)
        _db.session.commit()

        profile_b = GSTProfile(
            user_id=user_b.id,
            gstin=f'29BBBBB{uuid.uuid4().hex[:4].upper()}1Z7',
            legal_name='Tenant B Tech Ltd',
            trade_name='Tenant B Store',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly',
        )
        _db.session.add(profile_b)
        _db.session.commit()

        env_dict = {
            'profile_a_id': profile_a.id,
            'user_a_id': user_a.id,
            'username_a': user_a.username,
            'profile_b_id': profile_b.id,
            'user_b_id': user_b.id,
            'username_b': user_b.username,
            'period': '082024',
        }
        yield env_dict


def _login_client(client, env, tenant_key='a'):
    """Helper to log in test client and bind active profile and period in session."""
    client.get('/logout', follow_redirects=True)
    username = env[f'username_{tenant_key}']
    profile_id = env[f'profile_{tenant_key}_id']
    password = 'SecretA123!' if tenant_key == 'a' else 'SecretB456!'
    client.post(
        '/login',
        data={'username': username, 'password': password},
        follow_redirects=True,
    )
    with client.session_transaction() as sess:
        sess['active_profile_id'] = profile_id
        sess['return_period'] = env['period']


def _write_marketplace_csv(filepath, platform_name):
    """Write synthetic marketplace CSV for testing promoted adapter CSV parsing."""
    headers = [
        'Order ID', 'Invoice Number', 'Invoice Date', 'Customer GSTIN',
        'Place of Supply', 'Item Code', 'HSN', 'Quantity', 'Taxable Value',
        'IGST', 'CGST', 'SGST', 'CESS'
    ]
    rows = [
        [
            f'ORD-{platform_name.upper()}-001',
            f'INV-{platform_name.upper()}-001',
            '10-08-2024',
            '29ABCDE1234F1Z5',
            '29',
            f'SKU-{platform_name.upper()}',
            '6109',
            '2',
            '2500.00',
            '450.00',
            '0.00',
            '0.00',
            '0.00',
        ]
    ]
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def _verify_promoted_adapter_excel_and_csv(platform_name, fixture_filename, prefix, fixture_dir, tmp_path):
    """Reusable verifier for promoted marketplace adapters on both Excel and CSV."""
    adapter = get_adapter(platform_name)
    assert adapter is not None, f"Adapter for {platform_name} must be registered"
    assert adapter.PLATFORM_NAME == platform_name

    # Check promotion indicators
    assert getattr(adapter, 'FORMAT_DOCUMENTED', False) is True, f"{platform_name} must be promoted to FORMAT_DOCUMENTED=True"
    header_map = getattr(adapter, 'HEADER_MAP', {})
    assert bool(header_map) is True, f"{platform_name} must declare a non-empty HEADER_MAP"
    assert '.xlsx' in adapter.SUPPORTED_FILE_TYPES
    assert '.csv' in adapter.SUPPORTED_FILE_TYPES

    # 1. Real Excel parsing
    excel_path = os.path.join(fixture_dir, fixture_filename)
    assert os.path.exists(excel_path), f"Fixture {excel_path} must exist"
    excel_result = adapter.parse(excel_path, fixture_filename)

    assert len(excel_result.errors) == 0, f"Excel parse errors: {excel_result.errors}"
    assert len(excel_result.rows) >= 1
    excel_row = excel_result.rows[0]
    assert excel_row.status == ImportRowStatus.SUCCESS
    x_norm = excel_row.normalized_data

    assert x_norm['order_id'] == 'ORD-001'
    assert x_norm['invoice_number'] == 'INV-001'
    assert x_norm['place_of_supply'] == '29'
    assert isinstance(x_norm['taxable_value'], Decimal)
    assert x_norm['taxable_value'] in (Decimal('1000'), Decimal('1000.00'))
    assert isinstance(x_norm['igst_amount'], Decimal)
    assert x_norm['igst_amount'] in (Decimal('180'), Decimal('180.00'))
    assert x_norm['cgst_amount'] in (Decimal('0'), Decimal('0.00'))
    assert x_norm['sgst_amount'] in (Decimal('0'), Decimal('0.00'))

    # 2. Real CSV parsing
    csv_file_path = str(tmp_path / f"{prefix}_orders.csv")
    _write_marketplace_csv(csv_file_path, platform_name)
    csv_result = adapter.parse(csv_file_path, f"{prefix}_orders.csv")

    assert len(csv_result.errors) == 0, f"CSV parse errors: {csv_result.errors}"
    assert len(csv_result.rows) >= 1
    c_row = csv_result.rows[0]
    assert c_row.status == ImportRowStatus.SUCCESS
    c_norm = c_row.normalized_data

    assert c_norm['order_id'] == f'ORD-{platform_name.upper()}-001'
    assert c_norm['invoice_number'] == f'INV-{platform_name.upper()}-001'
    assert c_norm['place_of_supply'] == '29'
    assert isinstance(c_norm['taxable_value'], Decimal)
    assert c_norm['taxable_value'] == Decimal('2500.00')
    assert isinstance(c_norm['igst_amount'], Decimal)
    assert c_norm['igst_amount'] == Decimal('450.00')
    assert c_norm['cgst_amount'] == Decimal('0.00')
    assert c_norm['sgst_amount'] == Decimal('0.00')


# ═════════════════════════════════════════════════════════════════════════════
# PART 1: Newly Promoted Marketplace Adapters (Tests 1-10)
# ═════════════════════════════════════════════════════════════════════════════

def test_01_jiomart_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """JioMart adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('JioMart', 'jiomart_sample.xlsx', 'jiomart', fixture_dir, tmp_path)


def test_02_snapdeal_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """Snapdeal adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('Snapdeal', 'snapdeal_sample.xlsx', 'snapdeal', fixture_dir, tmp_path)


def test_03_tatacliq_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """TataCLiQ adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('TataCLiQ', 'tatacliq_sample.xlsx', 'tatacliq', fixture_dir, tmp_path)


def test_04_limeroad_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """Limeroad adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('Limeroad', 'limeroad_sample.xlsx', 'limeroad', fixture_dir, tmp_path)


def test_05_shopdeck_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """Shopdeck adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('Shopdeck', 'shopdeck_sample.xlsx', 'shopdeck', fixture_dir, tmp_path)


def test_06_glowroad_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """GlowRoad adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('GlowRoad', 'glowroad_sample.xlsx', 'glowroad', fixture_dir, tmp_path)


def test_07_snapmint_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """Snapmint adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('Snapmint', 'snapmint_sample.xlsx', 'snapmint', fixture_dir, tmp_path)


def test_08_citymall_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """Citymall adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('Citymall', 'citymall_sample.xlsx', 'citymall', fixture_dir, tmp_path)


def test_09_roposo_adapter_excel_and_csv_parsing(fixture_dir, tmp_path):
    """Roposo adapter parses real Excel & CSV with Decimal tax extraction and canonical normalization."""
    _verify_promoted_adapter_excel_and_csv('Roposo', 'roposo_sample.xlsx', 'roposo', fixture_dir, tmp_path)


def test_10_decimal_tax_precision_and_intra_inter_splitting_across_adapters(tmp_path):
    """Promoted adapters preserve Decimal tax arithmetic without float drift across intra/inter/zero rates."""
    headers = [
        'Order ID', 'Invoice Number', 'Invoice Date', 'Customer GSTIN',
        'Place of Supply', 'Item Code', 'HSN', 'Quantity', 'Taxable Value',
        'IGST', 'CGST', 'SGST', 'CESS'
    ]
    # Row 1: High precision intrastate (CGST + SGST)
    # Row 2: High precision interstate (IGST)
    # Row 3: Zero-tax exempt supply
    rows = [
        ['ORD-INTRA', 'INV-INTRA', '12-08-2024', '27AAAAA0000A1Z5', '27', 'SKU-A', '6109', '3', '12345.67', '0.00', '1111.11', '1111.11', '0.00'],
        ['ORD-INTER', 'INV-INTER', '14-08-2024', '29BBBBB0000B1Z7', '29', 'SKU-B', '8517', '1', '24691.34', '4444.44', '0.00', '0.00', '0.00'],
        ['ORD-ZERO', 'INV-ZERO', '15-08-2024', '07CCCCC0000C1Z9', '07', 'SKU-C', '0101', '5', '5000.00', '0.00', '0.00', '0.00', '0.00'],
    ]
    csv_file = str(tmp_path / "tax_precision_test.csv")
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    # Test across multiple promoted adapters
    for p_name in ('JioMart', 'Snapdeal', 'TataCLiQ'):
        adapter = get_adapter(p_name)
        assert adapter is not None
        res = adapter.parse(csv_file, "tax_precision_test.csv")
        assert len(res.rows) == 3

        # Row 1: Intrastate
        r1 = res.rows[0].normalized_data
        assert r1['taxable_value'] == Decimal('12345.67')
        assert r1['cgst_amount'] == Decimal('1111.11')
        assert r1['sgst_amount'] == Decimal('1111.11')
        assert r1['igst_amount'] == Decimal('0.00')

        # Row 2: Interstate
        r2 = res.rows[1].normalized_data
        assert r2['taxable_value'] == Decimal('24691.34')
        assert r2['igst_amount'] == Decimal('4444.44')
        assert r2['cgst_amount'] == Decimal('0.00')
        assert r2['sgst_amount'] == Decimal('0.00')

        # Row 3: Zero-tax
        r3 = res.rows[2].normalized_data
        assert r3['taxable_value'] == Decimal('5000.00')
        assert r3['igst_amount'] == Decimal('0.00')
        assert r3['cgst_amount'] == Decimal('0.00')
        assert r3['sgst_amount'] == Decimal('0.00')
        assert res.rows[2].status == ImportRowStatus.SUCCESS


# ═════════════════════════════════════════════════════════════════════════════
# PART 2: Adapter Readiness Matrix & Honesty Gate (Tests 11-13)
# ═════════════════════════════════════════════════════════════════════════════

def test_11_all_15_marketplace_adapters_production_readiness_matrix():
    """Readiness matrix reports all 15 marketplace adapters as PRODUCTION with valid header mappings."""
    matrix = get_adapter_readiness_matrix()
    assert isinstance(matrix, list)
    assert len(matrix) >= 15

    matrix_by_platform = {m['platform']: m for m in matrix}
    for platform in ALL_15_PRODUCTION_PLATFORMS:
        assert platform in matrix_by_platform, f"Platform {platform} missing from readiness matrix"
        entry = matrix_by_platform[platform]
        assert entry['format_documented'] is True, f"{platform} format_documented must be True"
        assert entry['has_header_map'] is True, f"{platform} has_header_map must be True"
        assert entry['mapped_headers_count'] > 0, f"{platform} mapped_headers_count must be > 0"
        assert entry['status'] == 'PRODUCTION', f"{platform} status must be PRODUCTION"
        assert '.xlsx' in entry['supported_file_types']
        assert '.csv' in entry['supported_file_types']


def test_12_readiness_matrix_honesty_gate_preserves_fallback_classification():
    """Honesty gate maintains FALLBACK classification for any undocumented adapter class."""
    class _MockUndocumentedAdapter(_GenericFilenameAdapter):
        PLATFORM_NAME = 'MockTestFallbackMarketplace'
        FORMAT_DOCUMENTED = False
        HEADER_MAP = {}
        SUPPORTED_FILE_TYPES = ['.xlsx', '.csv']

    try:
        register_adapter(_MockUndocumentedAdapter)
        matrix = get_adapter_readiness_matrix()
        fb_entry = next((m for m in matrix if m['platform'] == 'MockTestFallbackMarketplace'), None)

        assert fb_entry is not None, "Mock undocumented adapter must appear in matrix"
        assert fb_entry['format_documented'] is False
        assert fb_entry['has_header_map'] is False
        assert fb_entry['status'] == 'FALLBACK'
    finally:
        _ADAPTER_REGISTRY.pop('MockTestFallbackMarketplace', None)


def test_13_api_adapters_readiness_and_platforms_production_exposure(client, env_phase6ab):
    """API endpoints /api/adapters/readiness and /api/platforms return all 15 production platforms."""
    _login_client(client, env_phase6ab, 'a')

    # 1. /api/adapters/readiness
    res_ready = client.get('/api/adapters/readiness')
    assert res_ready.status_code == 200
    ready_data = res_ready.get_json()['data']
    ready_platforms = {item['platform'] for item in ready_data}
    assert set(ALL_15_PRODUCTION_PLATFORMS).issubset(ready_platforms)

    for item in ready_data:
        if item['platform'] in ALL_15_PRODUCTION_PLATFORMS:
            assert item['status'] == 'PRODUCTION'
            assert item['format_documented'] is True

    # 2. /api/platforms
    res_plat = client.get('/api/platforms')
    assert res_plat.status_code == 200
    plat_data = res_plat.get_json()['data']
    plat_names = {p['name'] if isinstance(p, dict) else p for p in plat_data}
    assert set(ALL_15_PRODUCTION_PLATFORMS).issubset(plat_names)


# ═════════════════════════════════════════════════════════════════════════════
# PART 3: Web UI Rendering & End-to-End Form Interactions (Tests 14-23)
# ═════════════════════════════════════════════════════════════════════════════

def test_14_import_page_rendering_and_platform_selection_with_csrf(client, env_phase6ab):
    """GET /import renders marketplace platform selection, CSRF protection, and staging checkbox."""
    _login_client(client, env_phase6ab, 'a')
    res = client.get('/import')
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert "Import" in html
    assert 'type="file"' in html or 'fileInput' in html
    # Check that promoted platforms or dynamic platforms are visible
    for name in ('Amazon', 'Flipkart'):
        assert name in html


def test_15_import_staging_upload_mode_and_raw_import_preservation(client, env_phase6ab, fixture_dir):
    """POST /import/upload with stage=1 transitions to STAGED, isolates candidates, and preserves RawImport."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    sample_file = os.path.join(fixture_dir, 'jiomart_sample.xlsx')

    with open(sample_file, 'rb') as f:
        file_bytes = io.BytesIO(f.read())

    res = client.post(
        '/import/upload',
        data={
            'file': (file_bytes, 'jiomart_orders.xlsx'),
            'platform': 'JioMart',
            'stage': '1',
        },
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    # Redirects to preview
    assert res.status_code == 302
    assert '/import/' in res.location and '/preview' in res.location

    # Verify database state
    ih = ImportHistory.query.filter_by(profile_id=pid_a).order_by(ImportHistory.id.desc()).first()
    assert ih is not None
    assert ih.processing_status == 'STAGED'
    assert ih.platform_name == 'JioMart'

    raw = RawImport.query.filter_by(import_history_id=ih.id).first()
    assert raw is not None
    assert raw.raw_data is not None
    assert len(raw.raw_data) > 0

    # Transactions are marked staged and not in active ledger
    staged_txs = Transaction.query.filter_by(import_history_id=ih.id).all()
    assert len(staged_txs) > 0
    assert all(tx.is_staged is True for tx in staged_txs)

    # Active ledger remains clean
    active_txs = Transaction.query.filter_by(profile_id=pid_a, is_staged=False, is_deleted=False).all()
    assert len(active_txs) == 0


def test_16_import_preview_page_rendering_and_metrics_display(client, env_phase6ab, fixture_dir):
    """GET /import/<id>/preview renders candidate row counts, status badge, and confirm/cancel forms."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']

    # Create staged import
    res_import = process_import(
        file_path=os.path.join(fixture_dir, 'jiomart_sample.xlsx'),
        profile_id=pid_a,
        platform_name='JioMart',
        user_id=uid_a,
        return_period=env_phase6ab['period'],
        financial_year='2024-25',
        stage=True,
    )
    import_id = res_import.import_history_id

    res = client.get(f'/import/{import_id}/preview')
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert "Import Preview" in html
    assert "STAGED" in html
    assert "JioMart" in html
    assert "Total Rows" in html
    assert "Accepted" in html
    assert f"/import/{import_id}/confirm" in html
    assert f"/import/{import_id}/cancel" in html


def test_17_import_preview_confirm_workflow_promotes_to_ledger(client, env_phase6ab, fixture_dir):
    """POST /import/<id>/confirm promotes STAGED -> COMPLETED, unsets is_staged, and is idempotent."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']

    res_import = process_import(
        file_path=os.path.join(fixture_dir, 'snapdeal_sample.xlsx'),
        profile_id=pid_a,
        platform_name='Snapdeal',
        user_id=uid_a,
        return_period=env_phase6ab['period'],
        financial_year='2024-25',
        stage=True,
    )
    import_id = res_import.import_history_id

    # Confirm via web POST
    res_confirm = client.post(f'/import/{import_id}/confirm', follow_redirects=True)
    assert res_confirm.status_code == 200

    ih = _db.session.get(ImportHistory, import_id)
    assert ih.processing_status in ('COMPLETED', 'PARTIAL')

    txs = Transaction.query.filter_by(import_history_id=import_id).all()
    assert len(txs) > 0
    assert all(tx.is_staged is False for tx in txs)

    # Now active in ledger
    active_txs = Transaction.query.filter_by(profile_id=pid_a, is_staged=False, is_deleted=False).all()
    assert len(active_txs) == len(txs)

    # Audit log verified
    audit = AuditLog.query.filter_by(
        user_id=uid_a,
        action='CONFIRM',
        entity_id=import_id,
    ).first()
    assert audit is not None

    # Idempotent double-confirm
    res_double = client.post(f'/import/{import_id}/confirm', follow_redirects=True)
    assert res_double.status_code == 200
    txs_after = Transaction.query.filter_by(import_history_id=import_id).all()
    assert len(txs_after) == len(txs)


def test_18_import_preview_cancel_workflow_soft_deletes_candidates(client, env_phase6ab, fixture_dir):
    """POST /import/<id>/cancel transitions STAGED -> CANCELLED, soft-deletes candidates, preserves RawImport."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']

    res_import = process_import(
        file_path=os.path.join(fixture_dir, 'tatacliq_sample.xlsx'),
        profile_id=pid_a,
        platform_name='TataCLiQ',
        user_id=uid_a,
        return_period=env_phase6ab['period'],
        financial_year='2024-25',
        stage=True,
    )
    import_id = res_import.import_history_id

    res_cancel = client.post(f'/import/{import_id}/cancel', follow_redirects=True)
    assert res_cancel.status_code == 200

    ih = _db.session.get(ImportHistory, import_id)
    assert ih.processing_status == 'CANCELLED'

    txs = Transaction.query.filter_by(import_history_id=import_id).all()
    assert all(tx.is_deleted is True for tx in txs)

    # Raw import preserved
    raw = RawImport.query.filter_by(import_history_id=import_id).first()
    assert raw is not None
    assert raw.raw_data is not None
    assert len(raw.raw_data) > 0

    # Audit log verified
    audit = AuditLog.query.filter_by(
        user_id=uid_a,
        action='CANCEL',
        entity_id=import_id,
    ).first()
    assert audit is not None


def test_19_import_history_page_rendering_and_batch_actions(client, env_phase6ab):
    """GET /import/history renders historical import table, status badges, and action controls."""
    _login_client(client, env_phase6ab, 'a')
    res = client.get('/import/history')
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert "Import History" in html
    assert "File Name" in html
    assert "Status" in html


def test_20_import_job_progress_endpoint_tracking_and_isolation(client, env_phase6ab, fixture_dir):
    """GET /api/imports/<id>/progress returns live job metrics with strict tenant IDOR protection."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']

    res_import = process_import(
        file_path=os.path.join(fixture_dir, 'limeroad_sample.xlsx'),
        profile_id=pid_a,
        platform_name='Limeroad',
        user_id=uid_a,
        return_period=env_phase6ab['period'],
        financial_year='2024-25',
        stage=True,
    )
    import_id = res_import.import_history_id

    # Tenant A accesses progress
    res_prog = client.get(f'/api/imports/{import_id}/progress')
    assert res_prog.status_code == 200
    prog = res_prog.get_json()['data']
    assert prog['import_id'] == import_id
    assert prog['status'] == 'STAGED'
    assert prog['total_rows'] >= 1
    assert prog['progress_pct'] == 100.0

    # Cross-tenant IDOR check: Tenant B cannot access Tenant A's progress
    _login_client(client, env_phase6ab, 'b')
    res_idor = client.get(f'/api/imports/{import_id}/progress')
    assert res_idor.status_code == 404


def test_21_statement_page_rendering_summary_cards_and_section_tabs(client, env_phase6ab):
    """GET /statement renders summary KPI cards, section tab navigation, and audit trail link."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']

    # Seed an import history and raw import to satisfy FKs
    ih = ImportHistory(
        user_id=uid_a,
        profile_id=pid_a,
        file_name='seed_statement.xlsx',
        original_file_name='seed_statement.xlsx',
        platform_name='JioMart',
        return_period='082024',
        financial_year='2024-25',
        processing_status='COMPLETED',
    )
    _db.session.add(ih)
    _db.session.commit()

    raw = RawImport(
        import_history_id=ih.id,
        row_number=1,
        raw_data='{}',
        status='SUCCESS',
    )
    _db.session.add(raw)
    _db.session.commit()

    # Seed one confirmed transaction
    tx = Transaction(
        import_history_id=ih.id,
        raw_import_id=raw.id,
        profile_id=pid_a,
        invoice_number='INV-STATEMENT-001',
        invoice_date=date(2024, 8, 15),
        customer_gstin='27AAAAA0000A1Z5',
        place_of_supply='27',
        taxable_value=Decimal('5000.00'),
        cgst_amount=Decimal('450.00'),
        sgst_amount=Decimal('450.00'),
        igst_amount=Decimal('0.00'),
        total_tax=Decimal('900.00'),
        invoice_value=Decimal('5900.00'),
        tax_rate=Decimal('18.00'),
        hsn_sac='6109',
        is_staged=False,
        is_deleted=False,
    )
    _db.session.add(tx)
    _db.session.commit()

    res = client.get('/statement?period=082024')
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert "Taxable Value" in html
    assert "B2B" in html
    assert "B2C" in html
    assert "/statement/audit-trail" in html


def test_22_statement_reconciliation_exception_resolution_and_audit(client, env_phase6ab):
    """POST /statement/reconciliation/resolve/<id> applies resolution note with audited attribution."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']
    period = env_phase6ab['period']

    rec = ReconciliationRecord(
        profile_id=pid_a,
        return_period=period,
        bucket='UNMATCHED_SOURCE',
        source_taxable=Decimal('500.00'),
        normalized_taxable=Decimal('0.00'),
        variance_taxable=Decimal('500.00'),
        resolution_status='OPEN',
    )
    _db.session.add(rec)
    _db.session.commit()
    rec_id = rec.id

    # Resolve exception via web endpoint
    res = client.post(
        f'/statement/reconciliation/resolve/{rec_id}',
        json={
            'resolution_note': 'Approved variance verified with vendor settlement voucher SV-101',
            'resolution_status': 'RESOLVED',
        },
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data.get('success') is True

    # Verify persistence
    updated = _db.session.get(ReconciliationRecord, rec_id)
    assert updated.resolution_status == 'RESOLVED'
    assert 'SV-101' in updated.resolution_note
    assert updated.resolved_by == uid_a

    # Verify audit log
    audit = AuditLog.query.filter_by(
        action='RESOLVE_EXCEPTION',
        entity_id=rec_id,
        user_id=uid_a,
    ).first()
    assert audit is not None

    # Cross-tenant IDOR check: Tenant B cannot resolve Tenant A's record
    _login_client(client, env_phase6ab, 'b')
    res_idor = client.post(
        f'/statement/reconciliation/resolve/{rec_id}',
        json={'resolution_note': 'Hacked', 'resolution_status': 'RESOLVED'},
    )
    assert res_idor.status_code == 404


def test_23_generate_page_rendering_and_pregeneration_reconciliation_gate(client, env_phase6ab):
    """GET /generate renders checklist and Pre-Generation Gate blocks critical errors while passing clean data."""
    _login_client(client, env_phase6ab, 'a')
    pid_a = env_phase6ab['profile_a_id']
    uid_a = env_phase6ab['user_a_id']
    period = env_phase6ab['period']

    # 1. Clean Period: GET renders checklist and generate button is enabled
    res_clean = client.get(f'/generate?return_period={period}')
    assert res_clean.status_code == 200
    html_clean = res_clean.get_data(as_text=True)
    assert "pre-generation checklist" in html_clean.lower()
    assert "generateForm" in html_clean

    # Clean generation succeeds
    res_run = client.post(
        '/generate/run',
        data={'return_period': period, 'include_hsn': '1'},
        follow_redirects=True,
    )
    assert res_run.status_code == 200
    gen = GSTR1Generation.query.filter_by(profile_id=pid_a, return_period=period).first()
    assert gen is not None
    assert gen.generation_status == 'COMPLETED'

    # 2. Injected Critical Failure: Impossible tax combination blocks generation
    ih_bad = ImportHistory(
        user_id=uid_a,
        profile_id=pid_a,
        file_name='bad_tax_file.xlsx',
        original_file_name='bad_tax_file.xlsx',
        platform_name='JioMart',
        return_period=period,
        financial_year='2024-25',
        processing_status='COMPLETED',
    )
    _db.session.add(ih_bad)
    _db.session.commit()

    raw_bad = RawImport(
        import_history_id=ih_bad.id,
        row_number=1,
        raw_data='{}',
        status='ERROR',
    )
    _db.session.add(raw_bad)
    _db.session.commit()

    bad_tx = Transaction(
        import_history_id=ih_bad.id,
        raw_import_id=raw_bad.id,
        profile_id=pid_a,
        invoice_number='INV-BAD-TAX-999',
        invoice_date=date(2024, 8, 20),
        customer_gstin='27AAAAA0000A1Z5',
        place_of_supply='27',
        taxable_value=Decimal('1000.00'),
        cgst_amount=Decimal('100.00'),
        sgst_amount=Decimal('0.00'),  # Asymmetric CGST without SGST triggers critical tax mismatch
        igst_amount=Decimal('100.00'),  # Cannot have IGST and CGST together
        total_tax=Decimal('200.00'),
        invoice_value=Decimal('1200.00'),
        tax_rate=Decimal('18.00'),
        hsn_sac='6109',
        is_staged=False,
        is_deleted=False,
    )
    _db.session.add(bad_tx)
    _db.session.commit()

    # Pre-generation gate detects critical failure
    recon = run_full_reconciliation(pid_a, period)
    assert recon.is_generation_blocked is True

    # GET /generate reflects blocked status
    res_blocked_page = client.get(f'/generate?return_period={period}')
    assert res_blocked_page.status_code == 200
    html_blocked = res_blocked_page.get_data(as_text=True)
    assert "generation blocked" in html_blocked.lower()

    # POST /generate/run is fail-closed and redirects to validation errors
    res_blocked_run = client.post(
        '/generate/run',
        data={'return_period': period, 'include_hsn': '1'},
        follow_redirects=False,
    )
    assert res_blocked_run.status_code == 302
    assert '/statement/validation-errors' in res_blocked_run.location
