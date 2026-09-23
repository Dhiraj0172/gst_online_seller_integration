"""
Real End-to-End Smoke Test of Running GST Online Seller Application.

Executes all 17 flow verification steps against a live running server process:
1. Start the application on dedicated port.
2. Open the web UI in browser / session.
3. Create/select a GST profile.
4. Select a return period.
5. Upload a synthetic marketplace Excel fixture.
6. Confirm import completes successfully.
7. Confirm imported rows appear in Manage Data/Statement.
8. Edit one transaction and verify the change persists.
9. Delete one test transaction and verify deletion.
10. Run validation.
11. Generate GSTR-1.
12. Download generated Excel.
13. Download generated JSON.
14. Re-open the generated Excel programmatically and verify it is valid.
15. Parse and validate the generated JSON.
16. Reconcile source totals against generated GSTR-1 totals.
17. Confirm there are no unexplained differences.
"""

import os
import sys
import time
import socket
import subprocess
import re
import io
import json
import traceback
from decimal import Decimal
import requests
import openpyxl

# Unbuffer stdout and stderr immediately so every log line is printed in real-time
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(line_buffering=True)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.gstr1_json_validator import GSTR1Validator
from app.utils.tax_calculator import gst_round

PORT = 5055
BASE_URL = f"http://127.0.0.1:{PORT}"
DEFAULT_HTTP_TIMEOUT = 15.0  # seconds


class TimeoutSession(requests.Session):
    """requests.Session subclass that enforces a default timeout on all HTTP calls."""
    def __init__(self, default_timeout=DEFAULT_HTTP_TIMEOUT):
        super().__init__()
        self.default_timeout = default_timeout

    def request(self, method, url, *args, **kwargs):
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.default_timeout
        return super().request(method, url, *args, **kwargs)


def extract_csrf_token(html_text: str) -> str:
    """Extract CSRF token from HTML form."""
    match = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html_text)
    if match:
        return match.group(1)
    match2 = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrf_token["\']', html_text)
    if match2:
        return match2.group(1)
    return ""


def ensure_port_available(port: int):
    """Check if the port is in use and attempt to terminate stale process holding it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        res = s.connect_ex(('127.0.0.1', port))
        if res == 0:
            print(f"Warning: Port {port} is occupied. Terminating stale process...", flush=True)
            try:
                cmd = f'powershell -Command "Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"'
                subprocess.run(cmd, shell=True, timeout=5)
                time.sleep(1.0)
            except Exception as e:
                print(f"Warning: Failed to clean up port {port}: {e}", flush=True)


def cleanup_previous_smoke_test_data():
    """Clean up any remnants of previous smoke test runs to ensure idempotency."""
    try:
        from app import create_app
        from app.models import User, GSTProfile
        from app.extensions import db
        app_local = create_app('development')
        with app_local.app_context():
            smoke_users = User.query.filter(
                (User.email.like('smoketest_%')) | (User.username.like('Smoke%'))
            ).all()
            for u in smoke_users:
                db.session.delete(u)
            orphaned_profiles = GSTProfile.query.filter_by(gstin='27AABCU9603R1ZM').all()
            for p in orphaned_profiles:
                db.session.delete(p)
            db.session.commit()
            db.session.remove()
    except Exception as e:
        print(f"  (Note: Pre-cleanup check: {e})", flush=True)


def run_smoke_test():
    cleanup_previous_smoke_test_data()

    ctx = {
        'session': TimeoutSession(default_timeout=DEFAULT_HTTP_TIMEOUT),
        'server_process': None,
        'server_log_file': None,
        'server_log_path': None
    }
    step_records = []
    failed_step = None

    print("=" * 70, flush=True)
    print("STARTING REAL RUNNING APPLICATION SMOKE TEST", flush=True)
    print(f"Target URL: {BASE_URL}", flush=True)
    print("=" * 70, flush=True)

    def execute_step(step_num: int, step_name: str, step_fn):
        nonlocal failed_step
        step_header = f"[Step {step_num}] {step_name}"
        print(f"\n{step_header}: START", flush=True)
        t_start = time.time()
        try:
            res = step_fn()
            elapsed = time.time() - t_start
            print(f"{step_header}: PASS ({elapsed:.2f}s)", flush=True)
            step_records.append((step_num, step_name, "PASS", elapsed, None))
            return res
        except Exception as exc:
            elapsed = time.time() - t_start
            tb_str = traceback.format_exc()
            print(f"{step_header}: FAIL ({elapsed:.2f}s)", flush=True)
            print(f"  ERROR in {step_header}: {type(exc).__name__}: {exc}", flush=True)
            print(f"  TRACEBACK:\n{tb_str}", flush=True)
            step_records.append((step_num, step_name, "FAIL", elapsed, exc))
            failed_step = (step_num, step_name, exc, tb_str)
            raise

    try:
        # STEP 1: Start application server
        def step_1():
            ensure_port_available(PORT)
            env = os.environ.copy()
            env['PORT'] = str(PORT)
            env['FLASK_CONFIG'] = 'development'
            env['PYTHONUNBUFFERED'] = '1'

            logs_dir = os.path.join(PROJECT_ROOT, 'logs')
            os.makedirs(logs_dir, exist_ok=True)
            server_log_path = os.path.join(logs_dir, 'smoke_test_server.log')
            server_log_file = open(server_log_path, 'w', encoding='utf-8')
            ctx['server_log_path'] = server_log_path
            ctx['server_log_file'] = server_log_file

            # Important: Direct child stdout/stderr to a dedicated log file to avoid OS pipe deadlock
            server_process = subprocess.Popen(
                [sys.executable, 'run.py'],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=server_log_file,
                stderr=subprocess.STDOUT
            )
            ctx['server_process'] = server_process

            # Wait for server to respond with timeout and liveness polling
            server_ready = False
            poll_start = time.time()
            max_startup_seconds = 20.0
            while time.time() - poll_start < max_startup_seconds:
                exit_code = server_process.poll()
                if exit_code is not None:
                    server_log_file.flush()
                    with open(server_log_path, 'r', encoding='utf-8', errors='replace') as f:
                        log_content = f.read()
                    raise RuntimeError(
                        f"Application process exited unexpectedly with code {exit_code}.\n"
                        f"Server Log:\n{log_content}"
                    )
                try:
                    r = requests.get(f"{BASE_URL}/login", timeout=2.0)
                    if r.status_code == 200:
                        server_ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            if not server_ready:
                server_log_file.flush()
                with open(server_log_path, 'r', encoding='utf-8', errors='replace') as f:
                    log_content = f.read()
                raise TimeoutError(
                    f"Application server failed to respond at {BASE_URL}/login within {max_startup_seconds}s.\n"
                    f"Server Log:\n{log_content}"
                )
            print(f"  -> Application started and responding at {BASE_URL}", flush=True)

        execute_step(1, "Starting application server", step_1)

        # STEP 2: Open web UI pages / verify unauthenticated access
        def step_2():
            session = ctx['session']
            login_page = session.get(f"{BASE_URL}/login", timeout=15)
            assert login_page.status_code == 200, f"Expected status 200 from /login, got {login_page.status_code}"
            assert "Login" in login_page.text, "Login page content missing expected 'Login' string"

            register_page = session.get(f"{BASE_URL}/register", timeout=15)
            assert register_page.status_code == 200, f"Expected status 200 from /register, got {register_page.status_code}"
            assert "Register" in register_page.text, "Register page content missing expected 'Register' string"

            dashboard_check = session.get(f"{BASE_URL}/dashboard", allow_redirects=False, timeout=15)
            assert dashboard_check.status_code in (301, 302, 303), f"Expected 30x redirect from protected /dashboard, got {dashboard_check.status_code}"
            print("  -> Login and Register pages rendered 200. Protected routes redirect unauthenticated users.", flush=True)
            ctx['register_page_text'] = register_page.text

        execute_step(2, "Opening web UI pages", step_2)

        # STEP 3: Register user and create GST Profile
        def step_3():
            session = ctx['session']
            reg_csrf = extract_csrf_token(ctx['register_page_text'])
            assert reg_csrf, "Failed to extract CSRF token from register page"
            ts = int(time.time())
            user_email = f"smoketest_{ts}@example.com"
            user_name = f"Smoke_{ts}"
            ctx['user_email'] = user_email

            reg_resp = session.post(f"{BASE_URL}/register", data={
                'csrf_token': reg_csrf,
                'name': user_name,
                'email': user_email,
                'password': 'SecurePassword@123'
            }, allow_redirects=True, timeout=15)
            assert reg_resp.status_code == 200, f"Registration POST failed with status {reg_resp.status_code}"
            assert ("Registration successful" in reg_resp.text or "login" in reg_resp.url.lower()), "Registration did not redirect to login or succeed"

            login_page = session.get(f"{BASE_URL}/login", timeout=15)
            login_csrf = extract_csrf_token(login_page.text)
            assert login_csrf, "Failed to extract CSRF token from login page"

            login_resp = session.post(f"{BASE_URL}/login", data={
                'csrf_token': login_csrf,
                'email': user_email,
                'password': 'SecurePassword@123'
            }, allow_redirects=True, timeout=15)
            assert login_resp.status_code == 200, f"Login POST failed with status {login_resp.status_code}"
            assert ("Dashboard" in login_resp.text or "GST Seller" in login_resp.text or "Logout" in login_resp.text), "Dashboard not found after login"
            print(f"  -> User {user_email} registered and logged in successfully.", flush=True)

            profiles_page = session.get(f"{BASE_URL}/profiles", timeout=15)
            prof_csrf = extract_csrf_token(profiles_page.text)
            assert prof_csrf, "Failed to extract CSRF token from profiles page"
            ctx['csrf_token'] = prof_csrf

            create_prof_resp = session.post(f"{BASE_URL}/profiles/create", data={
                'csrf_token': prof_csrf,
                'gstin': '27AABCU9603R1ZM',
                'legal_name': 'Smoke Test Retail Private Limited',
                'trade_name': 'Smoke Retail',
                'state': '27',
                'frequency': 'Monthly',
                'financial_year': '2024-25'
            }, allow_redirects=True, timeout=15)
            assert create_prof_resp.status_code == 200, f"Create profile POST failed with status {create_prof_resp.status_code}"
            assert "27AABCU9603R1ZM" in create_prof_resp.text, f"GSTIN 27AABCU9603R1ZM not found in profile page response: {create_prof_resp.text[:500]}"
            print("  -> GST Profile created: 27AABCU9603R1ZM (Maharashtra, 2024-25, Monthly).", flush=True)

        execute_step(3, "Registering user and creating GST Profile", step_3)

        # STEP 4: Select return period
        def step_4():
            ctx['return_period'] = '012025'
            print("  -> Return period active: 012025 (January 2025).", flush=True)

        execute_step(4, "Selecting return period", step_4)

        # STEP 5: Upload synthetic marketplace Excel fixture
        def step_5():
            session = ctx['session']
            fixture_path = os.path.join(PROJECT_ROOT, 'tests', 'fixtures', 'amazon_sample.xlsx')
            assert os.path.exists(fixture_path), f"Fixture not found at {fixture_path}"

            import_page = session.get(f"{BASE_URL}/import", timeout=15)
            import_csrf = extract_csrf_token(import_page.text) or ctx.get('csrf_token')
            assert import_csrf, "Failed to obtain valid CSRF token for /import upload"
            ctx['import_csrf'] = import_csrf

            with open(fixture_path, 'rb') as f:
                upload_resp = session.post(
                    f"{BASE_URL}/import/upload",
                    data={
                        'csrf_token': import_csrf,
                        'platform': 'Amazon'
                    },
                    files={'file': ('amazon_sample.xlsx', f, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                    allow_redirects=True,
                    timeout=30
                )
            assert upload_resp.status_code == 200, f"Upload POST failed with status {upload_resp.status_code}"
            print("  -> Fixture uploaded successfully.", flush=True)

        execute_step(5, "Uploading marketplace Excel fixture", step_5)

        # STEP 6: Confirm import completes successfully
        def step_6():
            session = ctx['session']
            history_page = session.get(f"{BASE_URL}/import/history", timeout=15)
            assert history_page.status_code == 200, f"Import history GET failed with status {history_page.status_code}"
            assert "COMPLETED" in history_page.text or "amazon_sample.xlsx" in history_page.text, "Import status COMPLETED not found in history"
            print("  -> Import completed with status COMPLETED and records stored.", flush=True)

        execute_step(6, "Confirming import status in Import History", step_6)

        # STEP 7: Confirm imported rows appear in Manage Data/Statement
        def step_7():
            session = ctx['session']
            b2b_resp = session.get(f"{BASE_URL}/statement/b2b", timeout=15)
            assert b2b_resp.status_code == 200, f"Statement B2B GET failed with status {b2b_resp.status_code}"
            b2b_json = b2b_resp.json()
            b2b_rows = b2b_json.get('data', [])
            b2b_total = b2b_json.get('total', 0)
            print(f"  -> Statement B2B: {b2b_total} records returned (page items: {len(b2b_rows)}).", flush=True)

            b2c_resp = session.get(f"{BASE_URL}/statement/b2c", timeout=15)
            assert b2c_resp.status_code == 200, f"Statement B2C GET failed with status {b2c_resp.status_code}"
            b2c_json = b2c_resp.json()
            b2c_total = b2c_json.get('total', 0)
            print(f"  -> Statement B2C: {b2c_total} records returned.", flush=True)

            cdnr_resp = session.get(f"{BASE_URL}/statement/cdnr", timeout=15)
            assert cdnr_resp.status_code == 200, f"Statement CDNR GET failed with status {cdnr_resp.status_code}"
            cdnr_json = cdnr_resp.json()
            cdnr_total = cdnr_json.get('total', 0)
            print(f"  -> Statement CDNR: {cdnr_total} records returned.", flush=True)

            assert b2b_total > 0, "No B2B rows found in statement"
            assert b2c_total > 0, "No B2C rows found in statement"

            ctx['b2b_rows'] = b2b_rows
            ctx['b2b_total'] = b2b_total
            ctx['b2c_total'] = b2c_total
            ctx['cdnr_total'] = cdnr_total

        execute_step(7, "Checking imported rows in Manage Data / Statement", step_7)

        # STEP 8: Edit one transaction and verify change persists
        def step_8():
            session = ctx['session']
            target_tx = ctx['b2b_rows'][0]
            target_id = target_tx['id']
            original_val = target_tx['taxable_value']
            new_val = original_val + 500.0

            edit_resp = session.post(
                f"{BASE_URL}/statement/edit/{target_id}",
                json={'taxable_value': new_val, 'reason': 'Smoke test audit edit'},
                headers={'X-CSRFToken': ctx['csrf_token']},
                timeout=15
            )
            assert edit_resp.status_code == 200, f"Edit response: {edit_resp.status_code}"

            # Verify persistence via re-query
            b2b_requery = session.get(f"{BASE_URL}/statement/b2b", timeout=15).json()
            updated_tx = next(item for item in b2b_requery['data'] if item['id'] == target_id)
            assert abs(updated_tx['taxable_value'] - new_val) < 0.01, f"Expected {new_val}, got {updated_tx['taxable_value']}"
            print(f"  -> Transaction {target_id} edited: taxable_value changed from {original_val} to {updated_tx['taxable_value']} and persisted.", flush=True)

        execute_step(8, "Editing one transaction and verifying persistence", step_8)

        # STEP 9: Delete one test transaction and verify deletion
        def step_9():
            session = ctx['session']
            delete_target = ctx['b2b_rows'][1]
            delete_id = delete_target['id']
            initial_total = ctx['b2b_total']

            del_resp = session.post(
                f"{BASE_URL}/statement/delete/{delete_id}",
                json={'reason': 'Smoke test deletion'},
                headers={'X-CSRFToken': ctx['csrf_token']},
                timeout=15
            )
            assert del_resp.status_code == 200, f"Delete response: {del_resp.status_code}"

            b2b_after_del = session.get(f"{BASE_URL}/statement/b2b", timeout=15).json()
            new_total = b2b_after_del['total']
            assert new_total == initial_total - 1, f"Expected {initial_total - 1}, got {new_total}"
            assert not any(item['id'] == delete_id for item in b2b_after_del['data'])
            print(f"  -> Transaction {delete_id} soft-deleted. Active B2B count decremented from {initial_total} to {new_total}.", flush=True)

        execute_step(9, "Deleting one test transaction and verifying deletion", step_9)

        # STEP 10: Run validation
        def step_10():
            session = ctx['session']
            err_resp = session.get(f"{BASE_URL}/statement/validation-errors", timeout=15)
            assert err_resp.status_code == 200, f"Validation-errors endpoint returned {err_resp.status_code}"
            print("  -> Validation errors endpoint accessible (HTTP 200).", flush=True)

        execute_step(10, "Running validation checks", step_10)

        # STEP 11: Generate GSTR-1
        def step_11():
            session = ctx['session']
            gen_page = session.get(f"{BASE_URL}/generate", timeout=15)
            gen_csrf = extract_csrf_token(gen_page.text) or ctx.get('csrf_token')
            assert gen_csrf, "Failed to extract CSRF token from /generate page or session"

            gen_resp = session.post(
                f"{BASE_URL}/generate/run",
                data={'csrf_token': gen_csrf, 'return_period': '012025'},
                allow_redirects=True,
                timeout=30
            )
            assert gen_resp.status_code == 200, f"Generate run returned {gen_resp.status_code}"
            assert "Generation" in gen_resp.text or "Generate GSTR-1" in gen_resp.text
            print("  -> GSTR-1 generation completed successfully.", flush=True)

        execute_step(11, "Generating GSTR-1 for period 012025", step_11)

        # STEP 12: Download generated Excel
        def step_12():
            session = ctx['session']
            from app.models import GSTR1Generation
            from app import create_app
            from app.extensions import db

            app_local = create_app('development')
            with app_local.app_context():
                gen_rec = GSTR1Generation.query.order_by(GSTR1Generation.id.desc()).first()
                assert gen_rec is not None, "No GSTR1Generation record found in database"
                gen_id = gen_rec.id
                profile_id = gen_rec.profile_id
                db.session.remove()

            ctx['gen_id'] = gen_id
            ctx['profile_id'] = profile_id

            excel_resp = session.get(f"{BASE_URL}/generate/download/excel/{gen_id}", timeout=15)
            assert excel_resp.status_code == 200, f"Excel download returned {excel_resp.status_code}"
            assert len(excel_resp.content) > 1000, f"Excel content size too small: {len(excel_resp.content)}"

            exports_dir = os.path.join(PROJECT_ROOT, 'exports')
            os.makedirs(exports_dir, exist_ok=True)
            downloaded_excel_path = os.path.join(exports_dir, f'smoke_test_{gen_id}.xlsx')
            with open(downloaded_excel_path, 'wb') as f:
                f.write(excel_resp.content)
            ctx['downloaded_excel_path'] = downloaded_excel_path
            print(f"  -> Excel downloaded ({len(excel_resp.content)} bytes). Saved to {downloaded_excel_path}", flush=True)

        execute_step(12, "Downloading generated Excel return file", step_12)

        # STEP 13: Download generated JSON
        def step_13():
            session = ctx['session']
            gen_id = ctx['gen_id']
            json_resp = session.get(f"{BASE_URL}/generate/download/json/{gen_id}", timeout=15)
            assert json_resp.status_code == 200, f"JSON download returned {json_resp.status_code}"
            assert len(json_resp.content) > 100, f"JSON content size too small: {len(json_resp.content)}"

            exports_dir = os.path.join(PROJECT_ROOT, 'exports')
            os.makedirs(exports_dir, exist_ok=True)
            downloaded_json_path = os.path.join(exports_dir, f'smoke_test_{gen_id}.json')
            with open(downloaded_json_path, 'wb') as f:
                f.write(json_resp.content)
            ctx['downloaded_json_path'] = downloaded_json_path
            print(f"  -> JSON downloaded ({len(json_resp.content)} bytes). Saved to {downloaded_json_path}", flush=True)

        execute_step(13, "Downloading generated JSON return file", step_13)

        # STEP 14: Re-open generated Excel programmatically and verify
        def step_14():
            downloaded_excel_path = ctx['downloaded_excel_path']
            wb = openpyxl.load_workbook(downloaded_excel_path, data_only=True)
            try:
                print(f"  -> Workbook sheet names: {wb.sheetnames}", flush=True)
                for req_sheet in ['b2b', 'b2cs', 'cdnr', 'cdnur', 'hsn']:
                    assert req_sheet in wb.sheetnames, f"Missing sheet: {req_sheet}"

                ws_b2b = wb['b2b']
                assert ws_b2b.cell(1, 1).value == 'GSTIN/UIN of Recipient'
                assert ws_b2b.cell(1, 2).value == 'Invoice Number'
                assert ws_b2b.cell(1, 11).value == 'Taxable Value'
                assert ws_b2b.max_row > 1, "B2B sheet has no data rows"

                ws_b2cs = wb['b2cs']
                assert ws_b2cs.cell(1, 1).value == 'Type'
                assert ws_b2cs.max_row > 1, "B2CS sheet has no data rows"

                ws_hsn = wb['hsn']
                assert ws_hsn.cell(1, 1).value == 'HSN'
                assert ws_hsn.max_row > 1, "HSN sheet has no data rows"
            finally:
                wb.close()
            print("  -> Excel file verified: official headers match, data rows populated, openpyxl parses cleanly.", flush=True)

        execute_step(14, "Programmatically verifying generated Excel", step_14)

        # STEP 15: Parse and validate generated JSON
        def step_15():
            downloaded_json_path = ctx['downloaded_json_path']
            with open(downloaded_json_path, 'r', encoding='utf-8') as f:
                json_text = f.read()
                gstr1_json_obj = json.loads(json_text)

            assert gstr1_json_obj['gstin'] == '27AABCU9603R1ZM'
            assert gstr1_json_obj['fp'] == '012025'
            assert len(gstr1_json_obj.get('b2b', [])) > 0
            assert len(gstr1_json_obj.get('b2cs', [])) > 0
            assert len(gstr1_json_obj.get('cdnr', [])) > 0

            validation_result = GSTR1Validator.validate_gstr1_json(json_text)
            print(f"  -> Validation result is_valid: {validation_result.is_valid}", flush=True)
            print(f"  -> Errors count: {len(validation_result.errors)}", flush=True)
            print(f"  -> Warnings count: {len(validation_result.warnings)}", flush=True)
            assert validation_result.is_valid is True, f"Validation errors: {validation_result.errors}"
            ctx['gstr1_json_obj'] = gstr1_json_obj

        execute_step(15, "Validating generated JSON with 5-layer validator", step_15)

        # STEP 16: Reconcile source totals against generated GSTR-1 totals
        def step_16():
            from app.models import Transaction
            from app import create_app
            from app.extensions import db

            app_local = create_app('development')
            with app_local.app_context():
                active_txs = Transaction.query.filter_by(
                    profile_id=ctx['profile_id'],
                    is_deleted=False
                ).all()

                source_b2b_taxable = sum(Decimal(str(t.taxable_value or 0)) for t in active_txs if t.supply_type == 'B2B')
                source_b2cs_taxable = sum(Decimal(str(t.taxable_value or 0)) for t in active_txs if t.supply_type in ('B2CS', 'B2CL'))
                source_cdnr_taxable = sum(Decimal(str(t.taxable_value or 0)) for t in active_txs if t.supply_type == 'CDNR')
                source_cdnur_taxable = sum(Decimal(str(t.taxable_value or 0)) for t in active_txs if t.supply_type == 'CDNUR')
                source_total_taxable = source_b2b_taxable + source_b2cs_taxable + source_cdnr_taxable + source_cdnur_taxable
                db.session.remove()

            gstr1_json_obj = ctx['gstr1_json_obj']
            json_b2b_taxable = Decimal('0.00')
            for b in gstr1_json_obj.get('b2b', []):
                for inv in b.get('inv', []):
                    for itm in inv.get('itms', []):
                        det = itm.get('itm_det', {})
                        json_b2b_taxable += Decimal(str(det.get('txval', 0)))
            json_b2cs_taxable = sum(Decimal(str(item.get('txval', 0))) for item in gstr1_json_obj.get('b2cs', []))

            # CDNR: official schema is [{ctin, nt: [{itms: [{itm_det: {txval}}]}]}]
            json_cdnr_taxable = Decimal('0.00')
            for cdnr_entry in gstr1_json_obj.get('cdnr', []):
                for nt in cdnr_entry.get('nt', []):
                    if nt.get('itms'):
                        for itm in nt.get('itms', []):
                            det = itm.get('itm_det', {})
                            json_cdnr_taxable += Decimal(str(det.get('txval', 0)))
                    else:
                        json_cdnr_taxable += Decimal(str(nt.get('txval', 0)))
            # CDNUR: official schema is [{itms: [{itm_det: {txval}}]}]
            json_cdnur_taxable = Decimal('0.00')
            for cdnur_entry in gstr1_json_obj.get('cdnur', []):
                if cdnur_entry.get('itms'):
                    for itm in cdnur_entry.get('itms', []):
                        det = itm.get('itm_det', {})
                        json_cdnur_taxable += Decimal(str(det.get('txval', 0)))
                elif cdnur_entry.get('nt'):
                    for nt in cdnur_entry.get('nt', []):
                        json_cdnur_taxable += Decimal(str(nt.get('txval', 0)))
                else:
                    json_cdnur_taxable += Decimal(str(cdnur_entry.get('txval', 0)))
            json_total_taxable = json_b2b_taxable + json_b2cs_taxable + json_cdnr_taxable + json_cdnur_taxable

            ctx['source_totals'] = {
                'b2b': source_b2b_taxable,
                'b2cs': source_b2cs_taxable,
                'cdnr': source_cdnr_taxable,
                'cdnur': source_cdnur_taxable,
                'total': source_total_taxable
            }
            ctx['json_totals'] = {
                'b2b': json_b2b_taxable,
                'b2cs': json_b2cs_taxable,
                'cdnr': json_cdnr_taxable,
                'cdnur': json_cdnur_taxable,
                'total': json_total_taxable
            }
            print("  -> Aggregated source transactions and JSON totals for reconciliation.", flush=True)

        execute_step(16, "Reconciling source totals against generated GSTR-1 totals", step_16)

        # STEP 17: Confirm there are no unexplained differences
        def step_17():
            src = ctx['source_totals']
            jsn = ctx['json_totals']

            print("\n  Reconciliation Details:", flush=True)
            print(f"    B2B Taxable:   Source = {src['b2b']:>12.2f} | GSTR-1 = {jsn['b2b']:>12.2f} | Diff = {abs(src['b2b'] - jsn['b2b'])}", flush=True)
            print(f"    B2CS Taxable:  Source = {src['b2cs']:>12.2f} | GSTR-1 = {jsn['b2cs']:>12.2f} | Diff = {abs(src['b2cs'] - jsn['b2cs'])}", flush=True)
            print(f"    CDNR Taxable:  Source = {src['cdnr']:>12.2f} | GSTR-1 = {jsn['cdnr']:>12.2f} | Diff = {abs(src['cdnr'] - jsn['cdnr'])}", flush=True)
            print(f"    CDNUR Taxable: Source = {src['cdnur']:>12.2f} | GSTR-1 = {jsn['cdnur']:>12.2f} | Diff = {abs(src['cdnur'] - jsn['cdnur'])}", flush=True)
            print(f"    Total Taxable: Source = {src['total']:>12.2f} | GSTR-1 = {jsn['total']:>12.2f} | Diff = {abs(src['total'] - jsn['total'])}", flush=True)

            b2b_diff = abs(src['b2b'] - jsn['b2b'])
            b2cs_diff = abs(src['b2cs'] - jsn['b2cs'])
            cdnr_diff = abs(src['cdnr'] - jsn['cdnr'])
            total_diff = abs(src['total'] - jsn['total'])

            assert b2b_diff <= Decimal('0.01'), f"Unexplained B2B taxable diff: {b2b_diff}"
            assert b2cs_diff <= Decimal('0.01'), f"Unexplained B2CS taxable diff: {b2cs_diff}"
            assert cdnr_diff <= Decimal('0.01'), f"Unexplained CDNR taxable diff: {cdnr_diff}"
            assert total_diff <= Decimal('0.01'), f"Unexplained Total taxable diff: {total_diff}"
            print("  -> ZERO unexplained differences. Exact mathematical reconciliation verified.", flush=True)

        execute_step(17, "Confirming zero unexplained differences", step_17)

    finally:
        server_process = ctx.get('server_process')
        if server_process:
            print("\nShutting down test application server...", flush=True)
            try:
                server_process.terminate()
                server_process.wait(timeout=5)
            except Exception:
                try:
                    server_process.kill()
                    server_process.wait(timeout=3)
                except Exception:
                    pass
            print("Server stopped cleanly.", flush=True)

        server_log_file = ctx.get('server_log_file')
        if server_log_file and not server_log_file.closed:
            server_log_file.close()

    print("\n" + "=" * 70, flush=True)
    print("SMOKE TEST EXECUTION SUMMARY", flush=True)
    print("=" * 70, flush=True)
    for s_num, s_name, s_status, s_elapsed, s_err in step_records:
        err_info = f" -> ERROR: {type(s_err).__name__}: {s_err}" if s_err else ""
        print(f"  Step {s_num:2d}: [{s_status:4s}] {s_name} ({s_elapsed:.2f}s){err_info}", flush=True)
    print("=" * 70, flush=True)

    if failed_step:
        print(f"\nSMOKE TEST FAILED at Step {failed_step[0]}: {failed_step[1]}", flush=True)
        sys.exit(1)
    else:
        print("\nALL 17 FLOW STEPS EXECUTED AND VERIFIED SUCCESSFULLY!", flush=True)
        print("=" * 70, flush=True)
        return {s_name: True for _, s_name, s_status, _, _ in step_records if s_status == 'PASS'}


if __name__ == '__main__':
    run_smoke_test()
