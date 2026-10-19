"""Operational readiness, system health probes, and backup/restore engine.

Workstream 12: Production Readiness, Health Probes & Backup/Restore Drills.
Provides:
  - Fast liveness probe (/healthz)
  - Deep readiness probe (/readyz) verifying DB, GST rules, adapters, storage, and job runner
  - Production security configuration validator
  - Canonical path-traversal containment guard
  - SQLite online backup snapshot creation with SHA-256 manifests and AuditLog
  - Verified restore engine with manifest checksum validation and PRAGMA integrity_check
"""
import os
import sys
import time
import json
import shutil
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, List, Optional, Union

from flask import current_app, has_request_context, request
from sqlalchemy import text, inspect

from app.extensions import db
from app.models.audit_log import AuditLog
from app.services.classification_service import GST_RULE_VERSION
from app.adapters.all_adapters import get_adapter_readiness_matrix

# Global application start time for uptime tracking
APP_START_TIME = time.time()

# Default insecure secret keys that must never be used in production
INSECURE_DEFAULT_SECRETS = {
    'gst-online-seller-dev-key-change-in-production',
    'jwt-secret-change-in-production',
    'dev',
    'development',
    'secret',
    'changeme',
    'password',
    '123456',
    'default',
    '',
}


# ============================================================================
# 1. Health & Readiness Probes
# ============================================================================

def get_liveness_status() -> Dict[str, Any]:
    """Fast, lightweight liveness check for container orchestrators (/healthz).
    
    Verifies process is responsive without querying external dependencies.
    """
    uptime = time.time() - APP_START_TIME
    return {
        "status": "ok",
        "uptime_seconds": round(uptime, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def get_readiness_status() -> Tuple[bool, Dict[str, Any]]:
    """Deep readiness check for traffic routing (/readyz).
    
    Verifies:
      1. DB connection, latency, and schema integrity
      2. GST_RULE_VERSION == '2026.1'
      3. Adapter readiness count (documented production adapters >= 6)
      4. Storage availability (writable upload/export dirs, disk space metrics)
      5. Job runner mode ('celery' / 'eager')
    
    Returns:
      (is_ready: bool, details: dict)
    """
    checks = {}
    is_ready = True

    # 1. Database Check
    db_check = {"status": "ok"}
    try:
        t0 = time.time()
        db.session.execute(text("SELECT 1"))
        db_check["latency_ms"] = round((time.time() - t0) * 1000, 2)
        
        # Check core schema tables exist
        inspector = inspect(db.engine)
        table_names = set(inspector.get_table_names())
        required_tables = {'users', 'gst_profiles', 'transactions', 'gstr1_generations', 'import_histories'}
        missing = required_tables - table_names
        if missing:
            db_check["status"] = "degraded"
            db_check["warning"] = f"Missing expected tables: {list(missing)}"
        db_check["table_count"] = len(table_names)
    except Exception as exc:
        is_ready = False
        db_check = {
            "status": "error",
            "error": str(exc),
        }
    checks["database"] = db_check

    # 2. GST Rule Engine Version Check
    gst_rules_check = {
        "version": GST_RULE_VERSION,
        "expected": "2026.1",
        "status": "ok" if GST_RULE_VERSION == "2026.1" else "mismatch"
    }
    if gst_rules_check["status"] != "ok":
        is_ready = False
    checks["gst_rules"] = gst_rules_check

    # 3. Adapter Readiness Check
    try:
        adapter_matrix = get_adapter_readiness_matrix()
        prod_adapters = [a for a in adapter_matrix if a.get("status") == "PRODUCTION" or a.get("format_documented")]
        checks["adapters"] = {
            "status": "ok",
            "production_ready_count": len(prod_adapters),
            "total_registered_count": len(adapter_matrix),
            "production_platforms": [a["platform"] for a in prod_adapters],
        }
        if len(prod_adapters) < 6:
            checks["adapters"]["warning"] = f"Expected >= 6 documented adapters, found {len(prod_adapters)}"
    except Exception as exc:
        checks["adapters"] = {
            "status": "error",
            "error": str(exc),
        }
        is_ready = False

    # 4. Storage Availability Check
    upload_folder = Path(current_app.config.get('UPLOAD_FOLDER', 'uploads'))
    generated_folder = Path(current_app.config.get('GENERATED_FOLDER', 'generated'))
    storage_check = {"status": "ok", "directories": {}}
    
    for label, folder in [('upload', upload_folder), ('generated', generated_folder)]:
        folder_info = {"path": str(folder), "exists": folder.exists()}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe_file = folder / f".probe_{os.getpid()}_{int(time.time()*1000)}"
            probe_file.write_text("probe_ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
            folder_info["writable"] = True
        except Exception as exc:
            folder_info["writable"] = False
            folder_info["error"] = str(exc)
            storage_check["status"] = "degraded"
            is_ready = False

        try:
            usage = shutil.disk_usage(folder if folder.exists() else folder.parent)
            folder_info["disk_total_bytes"] = usage.total
            folder_info["disk_free_bytes"] = usage.free
            folder_info["disk_percent_free"] = round((usage.free / usage.total) * 100, 2)
        except Exception:
            pass

        storage_check["directories"][label] = folder_info
    checks["storage"] = storage_check

    # 5. Job Runner Mode Check
    celery_broker = current_app.config.get('CELERY_BROKER_URL', '')
    runner_mode = 'celery' if ('redis://' in celery_broker or 'amqp://' in celery_broker) else 'eager'
    checks["job_runner"] = {
        "status": "ok",
        "mode": runner_mode,
        "broker_configured": bool(celery_broker),
    }

    return is_ready, {
        "status": "ready" if is_ready else "not_ready",
        "ready": is_ready,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


# ============================================================================
# 2. Production Security & Path-Traversal Guards
# ============================================================================

def validate_production_config(app_or_config=None, strict: bool = False) -> Tuple[bool, List[str]]:
    """Validate security settings against production hardening requirements.
    
    Warns or fails if:
      - Running in production with default/weak SECRET_KEY or JWT_SECRET_KEY
      - DEBUG is enabled in production
    """
    if app_or_config is None:
        cfg = current_app.config
    elif hasattr(app_or_config, 'config'):
        cfg = app_or_config.config
    else:
        cfg = app_or_config

    issues = []
    
    secret_key = str(cfg.get('SECRET_KEY', '') or '')
    jwt_key = str(cfg.get('JWT_SECRET_KEY', '') or '')
    is_debug = bool(cfg.get('DEBUG', False))
    is_testing = bool(cfg.get('TESTING', False))
    is_prod_env = os.environ.get('FLASK_CONFIG', '').lower() == 'production' or not is_debug and not is_testing

    if secret_key in INSECURE_DEFAULT_SECRETS or len(secret_key) < 16:
        issues.append("SECRET_KEY is insecure, default, or under 16 characters.")
        
    if jwt_key in INSECURE_DEFAULT_SECRETS or len(jwt_key) < 16:
        issues.append("JWT_SECRET_KEY is insecure, default, or under 16 characters.")

    if is_prod_env and is_debug:
        issues.append("DEBUG mode must be disabled in production.")

    is_valid = len(issues) == 0
    if not is_valid and strict and is_prod_env:
        raise ValueError(f"Production Security Configuration Violation: {'; '.join(issues)}")
        
    return is_valid, issues


def resolve_contained_path(base_dir: Union[str, Path], user_filename: str) -> Path:
    """Canonical path-traversal containment guard.
    
    Guarantees that a user-supplied filename or path fragment cannot escape
    the designated base directory, rejecting:
      - Relative traversal ('../../etc/passwd', '..\\..\\windows\\system32')
      - Null bytes ('\x00')
      - Absolute or drive-rooted paths ('C:\\boot.ini', '/etc/shadow')
      - Special devices / UNC shares
      
    Returns:
      Resolved, verified absolute Path strictly inside base_dir.
    """
    if not user_filename:
        raise ValueError("Filename cannot be empty")
        
    if "\x00" in user_filename:
        raise ValueError("Null byte detected in file path")

    from werkzeug.utils import secure_filename
    clean_name = secure_filename(os.path.basename(user_filename))
    if not clean_name:
        raise ValueError("Filename is invalid or empty after sanitization")

    base_path = Path(base_dir).resolve()
    candidate_path = (base_path / clean_name).resolve()

    try:
        candidate_path.relative_to(base_path)
    except ValueError:
        raise PermissionError(f"Path traversal attempt blocked: '{user_filename}' escapes '{base_dir}'")

    if os.path.commonpath([str(base_path), str(candidate_path)]) != str(base_path):
        raise PermissionError(f"Canonical path traversal violation for '{user_filename}'")

    return candidate_path


# ============================================================================
# 3. SQLite Online Backup & Restore Engine
# ============================================================================

def _compute_sha256(filepath: Union[str, Path]) -> str:
    """Compute deterministic SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _get_sqlite_driver_connection(raw_connection):
    """Safely obtain sqlite3.Connection."""
    if hasattr(raw_connection, 'driver_connection'):
        return raw_connection.driver_connection
    if hasattr(raw_connection, 'connection'):
        return raw_connection.connection
    return raw_connection


def create_backup_snapshot(backup_dir: Optional[Union[str, Path]] = None, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Create a consistent snapshot using SQLite Online Backup API.
    
    Writes:
      1. Consistent .db snapshot file
      2. backup_manifest.json with SHA-256 hash, byte size, timestamp, and metadata
      3. Records AuditLog(action='BACKUP', entity_type='SYSTEM')
      
    Returns:
      Manifest dictionary with snapshot metadata.
    """
    if backup_dir is None:
        backup_dir = Path(current_app.config.get('UPLOAD_FOLDER', 'uploads')).parent / 'backups'
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    db_filename = f"gst_backup_{timestamp_str}_{os.getpid()}.db"
    manifest_filename = f"gst_backup_{timestamp_str}_{os.getpid()}_manifest.json"
    
    target_db_file = backup_path / db_filename
    target_manifest_file = backup_path / manifest_filename

    # Online SQLite Backup
    raw = db.engine.raw_connection()
    try:
        source_conn = _get_sqlite_driver_connection(raw)
        dest_conn = sqlite3.connect(str(target_db_file))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        raw.close()

    # Integrity verification of newly created snapshot
    verify_conn = sqlite3.connect(str(target_db_file))
    try:
        cur = verify_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        check_result = cur.fetchall()
        if not check_result or check_result[0][0] != 'ok':
            raise RuntimeError(f"Snapshot integrity check failed: {check_result}")
            
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cur.fetchall() if not r[0].startswith('sqlite_')]
    finally:
        verify_conn.close()

    # Generate Manifest
    file_size = target_db_file.stat().st_size
    file_sha256 = _compute_sha256(target_db_file)
    
    manifest_data = {
        "backup_file": db_filename,
        "backup_path": str(target_db_file),
        "manifest_path": str(target_manifest_file),
        "sha256": file_sha256,
        "file_size": file_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_dialect": "sqlite",
        "tables": tables,
        "table_count": len(tables),
        "user_id": user_id,
        "gst_rule_version": GST_RULE_VERSION,
        "status": "VERIFIED_CONSISTENT",
    }

    with open(target_manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)

    # Record AuditLog
    try:
        client_ip = request.remote_addr[:45] if has_request_context() and request.remote_addr else None
    except Exception:
        client_ip = None

    audit_entry = AuditLog(
        user_id=user_id,
        entity_type="SYSTEM",
        entity_id=0,
        action="BACKUP",
        field_name="database",
        new_value=json.dumps({
            "backup_file": db_filename,
            "sha256": file_sha256,
            "file_size": file_size,
            "table_count": len(tables),
        }),
        reason=f"SQLite online backup snapshot created successfully ({len(tables)} tables)",
        ip_address=client_ip,
    )
    db.session.add(audit_entry)
    db.session.commit()

    return manifest_data


def verify_and_restore_backup(
    backup_file: Union[str, Path],
    manifest_file: Union[str, Path],
    target_db_path: Optional[Union[str, Path]] = None,
    user_id: Optional[int] = None
) -> Dict[str, Any]:
    """Verify SHA-256 checksum against manifest, restore database, and run PRAGMA integrity_check.
    
    Steps:
      1. Verify SHA-256 matches manifest['sha256']
      2. Verify file_size matches manifest['file_size']
      3. Restore using SQLite online backup to target_db_path or live connection
      4. Run PRAGMA integrity_check on restored database
      5. Record AuditLog(action='RESTORE', entity_type='SYSTEM')
      
    Raises:
      ValueError on checksum or size mismatch
      RuntimeError on failed PRAGMA integrity_check
    """
    backup_path = Path(backup_file)
    manifest_path = Path(manifest_file)

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Checksum & size validation
    expected_hash = manifest.get("sha256")
    expected_size = manifest.get("file_size")
    actual_hash = _compute_sha256(backup_path)
    actual_size = backup_path.stat().st_size

    if expected_hash and actual_hash.lower() != expected_hash.lower():
        raise ValueError(
            f"Backup checksum verification failed! Expected SHA-256: {expected_hash}, calculated: {actual_hash}"
        )
    if expected_size and actual_size != expected_size:
        raise ValueError(
            f"Backup size verification failed! Expected bytes: {expected_size}, actual: {actual_size}"
        )

    # Database Restore Operation
    src_conn = sqlite3.connect(str(backup_path))
    try:
        if target_db_path:
            dest_conn = sqlite3.connect(str(target_db_path))
            try:
                src_conn.backup(dest_conn)
                cur = dest_conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                res = cur.fetchall()
                if not res or res[0][0] != 'ok':
                    raise RuntimeError(f"Restored DB failed integrity check: {res}")
            finally:
                dest_conn.close()
        else:
            # Restore into active application connection
            raw = db.engine.raw_connection()
            try:
                live_conn = _get_sqlite_driver_connection(raw)
                src_conn.backup(live_conn)
                cur = live_conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                res = cur.fetchall()
                if not res or res[0][0] != 'ok':
                    raise RuntimeError(f"Restored live connection failed integrity check: {res}")
            finally:
                raw.close()
    finally:
        src_conn.close()

    # Record AuditLog
    try:
        client_ip = request.remote_addr[:45] if has_request_context() and request.remote_addr else None
    except Exception:
        client_ip = None

    audit_entry = AuditLog(
        user_id=user_id,
        entity_type="SYSTEM",
        entity_id=0,
        action="RESTORE",
        field_name="database",
        new_value=json.dumps({
            "restored_file": backup_path.name,
            "sha256": actual_hash,
            "integrity_check": "ok",
        }),
        reason="Database restored from verified backup snapshot with passing PRAGMA integrity_check",
        ip_address=client_ip,
    )
    db.session.add(audit_entry)
    db.session.commit()

    return {
        "restored": True,
        "integrity_check": "ok",
        "sha256": actual_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
