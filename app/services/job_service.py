"""Background Job Processing, Monitoring, Retry & Cancellation Service.

Workstream 7: Background Job Progress, Retry, Cancel & Monitoring
Provides unified job lifecycle orchestration for marketplace imports:
- Hybrid Celery/Redis execution with automatic in-process eager fallback.
- Granular progress reporting (processed_rows, total_rows, progress_pct).
- Cooperative cancellation for in-flight jobs.
- Idempotent retries with automatic partial-transaction cleanup.
- Immutable RawImport preservation for statutory auditability.
- Strict multi-tenant isolation and GSTR-1 freeze enforcement.
"""

import os
import uuid
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, Callable

from flask import current_app
from app.extensions import db
from app.models import ImportHistory, Transaction, RawImport, GSTProfile
from app.services.import_service import process_import, check_generation_freeze
from app.utils.query import tenant_query

logger = logging.getLogger(__name__)

__all__ = [
    'get_import_job_progress',
    'retry_import_job',
    'cancel_import_job',
    'dispatch_import_job',
    'is_celery_enabled',
    'JobExecutionError',
    'JobCancelledError',
]


class JobExecutionError(Exception):
    """Raised when job execution fails fatally."""
    pass


class JobCancelledError(Exception):
    """Raised when an in-flight job detects cancellation."""
    pass


def is_celery_enabled() -> bool:
    """Check if Celery & Redis should and can be used for background execution."""
    if not current_app:
        return False
    if current_app.config.get('TESTING') or current_app.config.get('CELERY_ALWAYS_EAGER'):
        return False

    broker_url = current_app.config.get('CELERY_BROKER_URL')
    if not broker_url:
        return False

    try:
        import celery
        # Verify redis ping if using redis broker
        if broker_url.startswith(('redis://', 'rediss://')):
            import redis
            r = redis.from_url(broker_url, socket_timeout=0.5)
            r.ping()
        return True
    except Exception as exc:
        logger.debug("Celery/Redis unavailable (%s); falling back to in-process synchronous eager mode.", exc)
        return False


def get_import_job_progress(import_id: int, profile_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve comprehensive job progress, row statistics, and status.

    Returns None if import does not exist or violates multi-tenant ownership.
    """
    import_rec = tenant_query(ImportHistory, profile_id).filter_by(id=import_id).first()
    if not import_rec:
        return None

    # Compute duration
    duration_ms = import_rec.processing_duration_ms
    if duration_ms is None and import_rec.processing_started_at:
        end_time = import_rec.processing_completed_at or datetime.utcnow()
        duration_ms = int((end_time - import_rec.processing_started_at).total_seconds() * 1000)

    # Compute processed rows count
    processed = import_rec.processed_rows
    if processed is None or processed == 0:
        processed = (
            (import_rec.success_rows or 0)
            + (import_rec.warning_rows or 0)
            + (import_rec.error_rows or 0)
            + (import_rec.skipped_rows or 0)
        )

    return {
        "import_id": import_rec.id,
        "task_id": import_rec.task_id or str(import_rec.id),
        "status": import_rec.processing_status,
        "total_rows": import_rec.total_rows or 0,
        "processed_rows": processed,
        "progress_pct": import_rec.progress_pct,
        "success_rows": import_rec.success_rows or 0,
        "warning_rows": import_rec.warning_rows or 0,
        "error_rows": import_rec.error_rows or 0,
        "skipped_rows": import_rec.skipped_rows or 0,
        "error_message": import_rec.error_message,
        "processing_duration_ms": duration_ms,
        "file_name": import_rec.file_name,
        "platform_name": import_rec.platform_name,
        "return_period": import_rec.return_period,
        "financial_year": import_rec.financial_year,
        "is_reprocessed": import_rec.is_reprocessed,
        "can_retry": import_rec.can_retry,
        "can_cancel": import_rec.can_cancel,
        "created_at": import_rec.created_at.isoformat() if import_rec.created_at else None,
        "started_at": import_rec.processing_started_at.isoformat() if import_rec.processing_started_at else None,
        "completed_at": import_rec.processing_completed_at.isoformat() if import_rec.processing_completed_at else None,
    }


def retry_import_job(
    import_id: int,
    profile_id: int,
    user_id: int,
    stage: bool = False
) -> Dict[str, Any]:
    """Atomically and idempotently retry an import job.

    1. Validates profile ownership and tenant isolation.
    2. Blocks retry if return period is frozen by a finalized GSTR-1.
    3. Prevents duplicate concurrent execution if job is already PROCESSING.
    4. Purges/cleans any previous partial transactions from this import.
    5. Re-executes import pipeline cleanly without duplicate transactions.
    6. Logs an audited RETRY action in AuditLog.
    """
    import_rec = tenant_query(ImportHistory, profile_id).filter_by(id=import_id).first()
    if not import_rec:
        raise ValueError(f"Import #{import_id} not found or access denied.")

    # Generation freeze check
    freeze = check_generation_freeze(profile_id, import_rec.return_period)
    if freeze:
        raise ValueError(
            f"Cannot retry import: Return period {import_rec.return_period} is frozen "
            f"by finalized GSTR-1 generation #{freeze.id}."
        )

    # Concurrency check
    if import_rec.processing_status == 'PROCESSING':
        return {
            "success": True,
            "status": "PROCESSING",
            "message": f"Import #{import_id} is already currently processing.",
            "already_running": True,
            "import_id": import_rec.id,
        }

    old_status = import_rec.processing_status

    # Clean up prior partial transactions belonging to this import
    tenant_query(Transaction, profile_id).filter_by(
        import_history_id=import_rec.id
    ).delete(synchronize_session=False)

    # Reset counters and mark status PROCESSING
    start_time = datetime.utcnow()
    import_rec.processing_status = 'PROCESSING'
    import_rec.processing_started_at = start_time
    import_rec.processing_completed_at = None
    import_rec.processing_duration_ms = None
    import_rec.processed_rows = 0
    import_rec.success_rows = 0
    import_rec.warning_rows = 0
    import_rec.error_rows = 0
    import_rec.skipped_rows = 0
    import_rec.error_summary = None
    import_rec.warning_summary = None
    import_rec.error_message = None
    db.session.commit()

    # Log RETRY audit record
    from app.models.audit_log import AuditLog
    audit_entry = AuditLog(
        user_id=user_id,
        entity_type='IMPORT_JOB',
        entity_id=import_rec.id,
        action='RETRY',
        field_name='processing_status',
        old_value=old_status,
        new_value='PROCESSING',
        reason=f"Retrying import #{import_rec.id} ({import_rec.file_name})",
    )
    db.session.add(audit_entry)
    db.session.commit()

    # Re-run import pipeline
    file_path = import_rec.raw_file_path
    if file_path and os.path.exists(file_path):
        res = process_import(
            file_path=file_path,
            profile_id=profile_id,
            platform_name=import_rec.platform_name,
            user_id=user_id,
            return_period=import_rec.return_period,
            financial_year=import_rec.financial_year,
            allow_duplicate_file=True,
            import_history_id=import_rec.id,
            stage=stage,
            _internal_commit=True,
        )
        final_status = res.status
    else:
        # Fallback to re-processing directly from preserved RawImport records
        final_status = _reprocess_from_raw_imports(import_rec, profile_id, user_id, stage)

    return {
        "success": True,
        "import_id": import_rec.id,
        "status": final_status,
        "message": f"Import #{import_id} retry completed with status {final_status}.",
        "total_rows": import_rec.total_rows,
        "success_rows": import_rec.success_rows,
    }


def cancel_import_job(
    import_id: int,
    profile_id: int,
    user_id: int
) -> Dict[str, Any]:
    """Cancel an in-flight, pending, or staged import job.

    1. Validates profile ownership and tenant isolation.
    2. Transitions ImportHistory to CANCELLED.
    3. Soft-deletes partial/staged candidate Transaction records.
    4. Preserves RawImport rows immutably as statutory audit evidence.
    5. Logs an audited CANCEL action in AuditLog.
    """
    import_rec = tenant_query(ImportHistory, profile_id).filter_by(id=import_id).first()
    if not import_rec:
        raise ValueError(f"Import #{import_id} not found or access denied.")

    # Idempotent return if already cancelled
    if import_rec.processing_status == 'CANCELLED':
        return {
            "success": True,
            "status": "CANCELLED",
            "message": f"Import #{import_id} is already cancelled.",
            "already_cancelled": True,
            "import_id": import_rec.id,
        }

    # Cannot cancel already completed/partial finalized imports
    if import_rec.processing_status in ('COMPLETED', 'PARTIAL', 'SUPERSEDED'):
        raise ValueError(
            f"Cannot cancel import #{import_id}: Import is already in terminal status '{import_rec.processing_status}'."
        )

    old_status = import_rec.processing_status

    # If Celery task is active, revoke it
    if import_rec.task_id and is_celery_enabled():
        try:
            from celery import current_app as celery_app
            celery_app.control.revoke(import_rec.task_id, terminate=True)
        except Exception as exc:
            logger.warning("Failed to revoke Celery task %s: %s", import_rec.task_id, exc)

    # Transition status to CANCELLED
    import_rec.processing_status = 'CANCELLED'
    import_rec.processing_completed_at = datetime.utcnow()
    if import_rec.processing_started_at:
        import_rec.processing_duration_ms = int(
            (import_rec.processing_completed_at - import_rec.processing_started_at).total_seconds() * 1000
        )

    # Soft-delete partial / candidate transactions (RawImport is preserved immutably)
    tenant_query(Transaction, profile_id).filter_by(
        import_history_id=import_rec.id,
        is_deleted=False
    ).update({
        'is_deleted': True,
        'deleted_at': datetime.utcnow(),
        'deleted_by': user_id
    }, synchronize_session=False)

    # Log CANCEL audit record
    from app.models.audit_log import AuditLog
    audit_entry = AuditLog(
        user_id=user_id,
        entity_type='IMPORT_JOB',
        entity_id=import_rec.id,
        action='CANCEL',
        field_name='processing_status',
        old_value=old_status,
        new_value='CANCELLED',
        reason=f"Cancelled import #{import_rec.id} ({import_rec.file_name})",
    )
    db.session.add(audit_entry)
    db.session.commit()

    return {
        "success": True,
        "status": "CANCELLED",
        "message": f"Import #{import_id} cancelled successfully.",
        "import_id": import_rec.id,
    }


def _reprocess_from_raw_imports(
    import_rec: ImportHistory,
    profile_id: int,
    user_id: int,
    stage: bool
) -> str:
    """Fallback re-processor that parses directly from preserved RawImport rows."""
    from app.adapters.registry import get_adapter
    from app.adapters.all_adapters import BaseGenericAdapter
    from app.services.classification_service import classify_transaction
    from app.services.import_service import _create_transaction_from_normalized

    adapter = get_adapter(import_rec.platform_name) or BaseGenericAdapter()
    raw_rows = RawImport.query.filter_by(
        import_history_id=import_rec.id
    ).order_by(RawImport.row_number.asc()).all()

    profile = db.session.get(GSTProfile, profile_id)
    success_count = 0
    error_count = 0

    for raw_imp in raw_rows:
        try:
            data = json.loads(raw_imp.raw_data) if raw_imp.raw_data else {}
            norm = adapter.normalize(data)
            supply_type = classify_transaction(norm, profile, import_rec.return_period)
            tx = _create_transaction_from_normalized(
                normalized=norm,
                import_history_id=import_rec.id,
                profile_id=profile_id,
                raw_import=raw_imp,
                source_platform=import_rec.platform_name,
                classification=supply_type,
                return_period=import_rec.return_period
            )
            tx.is_staged = stage
            db.session.add(tx)
            success_count += 1
        except Exception:
            error_count += 1

    import_rec.success_rows = success_count
    import_rec.error_rows = error_count
    import_rec.processed_rows = success_count + error_count
    import_rec.processing_status = 'STAGED' if stage else ('COMPLETED' if error_count == 0 else 'PARTIAL')
    import_rec.processing_completed_at = datetime.utcnow()
    db.session.commit()
    return import_rec.processing_status


def dispatch_import_job(
    file_path: str,
    profile_id: int,
    platform_name: str,
    user_id: int,
    return_period: str,
    financial_year: str,
    stage: bool = False,
    allow_duplicate_file: bool = False
) -> Dict[str, Any]:
    """Dispatch an import job either via Celery or deterministic in-process synchronous execution."""
    task_id = str(uuid.uuid4())
    
    # Pre-create ImportHistory in PENDING status
    import_rec = ImportHistory(
        user_id=user_id,
        profile_id=profile_id,
        file_name=os.path.basename(file_path),
        original_file_name=os.path.basename(file_path),
        platform_name=platform_name or 'Generic',
        return_period=return_period,
        financial_year=financial_year,
        processing_status='PENDING',
        task_id=task_id,
        raw_file_path=file_path,
        processing_started_at=datetime.utcnow()
    )
    db.session.add(import_rec)
    db.session.commit()

    if is_celery_enabled():
        try:
            from app.celery_tasks import execute_async_import
            execute_async_import.apply_async(
                args=[import_rec.id, file_path, profile_id, platform_name, user_id, return_period, financial_year, stage, allow_duplicate_file],
                task_id=task_id
            )
            return {
                "success": True,
                "import_id": import_rec.id,
                "task_id": task_id,
                "status": "PENDING",
                "message": "Import job queued successfully."
            }
        except Exception as exc:
            logger.warning("Celery dispatch failed (%s); running eagerly.", exc)

    # In-process synchronous execution (eager fallback)
    res = process_import(
        file_path=file_path,
        profile_id=profile_id,
        platform_name=platform_name,
        user_id=user_id,
        return_period=return_period,
        financial_year=financial_year,
        allow_duplicate_file=allow_duplicate_file,
        import_history_id=import_rec.id,
        stage=stage,
        _internal_commit=True
    )
    import_rec.task_id = task_id
    db.session.commit()
    return {
        "success": True,
        "import_id": import_rec.id,
        "task_id": task_id,
        "status": res.status,
        "message": f"Import job processed synchronously with status {res.status}."
    }
