"""Operational routes: health probes (/healthz, /readyz) and backup operations.

Workstream 12: Production Readiness, Health Probes & Backup/Restore Drills.
"""
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from app.services import ops_service
from . import ops_bp

@ops_bp.route('/healthz', methods=['GET'])
def healthz():
    """Fast liveness check probe for container runners."""
    payload = ops_service.get_liveness_status()
    return jsonify(payload), 200

@ops_bp.route('/readyz', methods=['GET'])
def readyz():
    """Deep readiness check probe verifying core dependencies (DB, rules, storage, adapters)."""
    is_ready, details = ops_service.get_readiness_status()
    status_code = 200 if is_ready else 503
    return jsonify(details), status_code

@ops_bp.route('/ops/backup', methods=['POST'])
@login_required
def create_backup():
    """Create an online SQLite backup snapshot with SHA-256 manifest and AuditLog."""
    manifest = ops_service.create_backup_snapshot(user_id=current_user.id)
    return jsonify(manifest), 200

@ops_bp.route('/ops/restore', methods=['POST'])
@login_required
def restore_backup():
    """Verify and restore database from backup file and manifest."""
    data = request.get_json(silent=True) or {}
    backup_file = data.get('backup_file')
    manifest_file = data.get('manifest_file')
    if not backup_file or not manifest_file:
        return jsonify({"error": True, "message": "backup_file and manifest_file are required"}), 400
    try:
        res = ops_service.verify_and_restore_backup(
            backup_file=backup_file,
            manifest_file=manifest_file,
            user_id=current_user.id
        )
        return jsonify(res), 200
    except ValueError as ve:
        return jsonify({"error": True, "message": str(ve)}), 400
    except Exception as exc:
        return jsonify({"error": True, "message": str(exc)}), 500
