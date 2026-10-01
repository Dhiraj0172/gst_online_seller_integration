"""Audit service for tracking GSTR-1 generation lifecycle events.

Provides centralized audit trail logging for:
- CREATE: New GSTR-1 generation created via web route (/generate/run) or REST API (POST /api/generate).
- REGENERATE: Existing generation regenerated via /generate/regenerate.
- DOWNLOAD: GSTR-1 file downloaded in Excel or JSON format (/generate/download/excel/<id>, /generate/download/json/<id>).

Preserves multi-tenant isolation, decimal serialization safety, and request context safety.
"""
import json
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, Dict, Any, Union
from flask import has_request_context, request
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.gstr1_generation import GSTR1Generation

__all__ = ['log_generation_audit', 'extract_generation_summary']


def _json_serial(obj: Any) -> Any:
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _safe_json_dumps(data: Any) -> str:
    """Serialize data to JSON safely handling Decimal and datetime types."""
    return json.dumps(data, default=_json_serial)


def _get_remote_addr() -> Optional[str]:
    """Safely obtain client IP address if inside a Flask request context."""
    if has_request_context():
        try:
            addr = request.remote_addr
            if addr:
                return addr[:45]
        except Exception:
            return None
    return None


def extract_generation_summary(gen: Union[GSTR1Generation, Dict[str, Any], None]) -> Dict[str, Any]:
    """Extract a JSON-serializable dictionary of generation metrics and counts."""
    if not gen:
        return {}
    if isinstance(gen, dict):
        summary = {}
        for k, v in gen.items():
            if isinstance(v, Decimal):
                summary[k] = float(v)
            elif isinstance(v, (datetime, date)):
                summary[k] = v.isoformat()
            else:
                summary[k] = v
        return summary

    return {
        "generation_id": getattr(gen, 'id', None),
        "profile_id": getattr(gen, 'profile_id', None),
        "return_period": getattr(gen, 'return_period', None),
        "financial_year": getattr(gen, 'financial_year', None),
        "generation_status": getattr(gen, 'generation_status', 'COMPLETED'),
        "total_b2b": getattr(gen, 'total_b2b', 0) or 0,
        "total_b2cs": getattr(gen, 'total_b2cs', 0) or 0,
        "total_b2cl": getattr(gen, 'total_b2cl', 0) or 0,
        "total_cdnr": getattr(gen, 'total_cdnr', 0) or 0,
        "total_cdnur": getattr(gen, 'total_cdnur', 0) or 0,
        "total_nil": getattr(gen, 'total_nil', 0) or 0,
        "total_hsn_b2b": getattr(gen, 'total_hsn_b2b', 0) or 0,
        "total_hsn_b2c": getattr(gen, 'total_hsn_b2c', 0) or 0,
        "total_ecom": getattr(gen, 'total_ecom', 0) or 0,
        "total_taxable_value": float(getattr(gen, 'total_taxable_value', 0) or 0),
        "total_cgst": float(getattr(gen, 'total_cgst', 0) or 0),
        "total_sgst": float(getattr(gen, 'total_sgst', 0) or 0),
        "total_igst": float(getattr(gen, 'total_igst', 0) or 0),
        "total_cess": float(getattr(gen, 'total_cess', 0) or 0),
        "total_tax": float(getattr(gen, 'total_tax', 0) or 0),
        "validation_passed": getattr(gen, 'validation_passed', True) if getattr(gen, 'validation_passed', None) is not None else True,
        "created_at": gen.created_at.isoformat() if getattr(gen, 'created_at', None) else None,
    }


def log_generation_audit(
    user_id: int,
    action: str,
    generation: GSTR1Generation,
    return_period: Optional[str] = None,
    profile_id: Optional[int] = None,
    format: Optional[str] = None,
    filename: Optional[str] = None,
    previous_generation: Optional[GSTR1Generation] = None,
    old_summary: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    reason: Optional[str] = None,
    commit: bool = False
) -> AuditLog:
    """Record an AuditLog entry for a GSTR1Generation lifecycle event.

    Args:
        user_id: Authenticated user ID (strictly sourced from current_user.id).
        action: Audit action ('CREATE', 'REGENERATE', 'DOWNLOAD').
        generation: GSTR1Generation instance being audited.
        return_period: Return period string (MMYYYY). Defaults to generation.return_period.
        profile_id: GSTProfile ID. Defaults to generation.profile_id.
        format: Download file format ('excel', 'json'). Required for DOWNLOAD action.
        filename: Download filename.
        previous_generation: Prior GSTR1Generation instance for REGENERATE action.
        old_summary: Snapshot dictionary of prior generation metrics for REGENERATE action.
        ip_address: Client IP address (optional override; falls back to request.remote_addr).
        reason: Optional human-readable reason (max 255 chars).
        commit: Whether to commit the transaction immediately (default False).

    Returns:
        The instantiated and persisted AuditLog model.
    """
    if not user_id and generation and getattr(generation, 'user_id', None):
        user_id = generation.user_id

    # Ensure generation has a persisted primary key id
    if generation and getattr(generation, 'id', None) is None:
        db.session.flush()

    gen_id = getattr(generation, 'id', None) if generation else None
    period = return_period or (getattr(generation, 'return_period', None) if generation else None)
    prof_id = profile_id or (getattr(generation, 'profile_id', None) if generation else None)
    normalized_action = action.upper().strip()

    field_name = 'status'
    old_value = None
    new_value = None

    if normalized_action == 'CREATE':
        field_name = 'status'
        summary = extract_generation_summary(generation)
        if period and 'return_period' not in summary:
            summary['return_period'] = period
        if prof_id and 'profile_id' not in summary:
            summary['profile_id'] = prof_id
        new_value = _safe_json_dumps(summary)
        if not reason:
            reason = f"GSTR-1 generation created for period {period}"

    elif normalized_action == 'REGENERATE':
        field_name = 'status'
        # Build old_value payload
        prior_state = {}
        if old_summary:
            prior_state = dict(old_summary)
        elif previous_generation:
            prior_state = extract_generation_summary(previous_generation)

        # Ensure previous_generation_id is explicitly captured
        prev_id = None
        if previous_generation and getattr(previous_generation, 'id', None):
            prev_id = previous_generation.id
        elif 'previous_generation_id' in prior_state and prior_state['previous_generation_id'] is not None:
            prev_id = prior_state['previous_generation_id']
        elif 'generation_id' in prior_state and prior_state['generation_id'] is not None:
            prev_id = prior_state['generation_id']

        prior_state['previous_generation_id'] = prev_id
        old_value = _safe_json_dumps(prior_state)

        # Build new_value payload
        curr_state = extract_generation_summary(generation)
        if prev_id is not None:
            curr_state['previous_generation_id'] = prev_id
        new_value = _safe_json_dumps(curr_state)

        if not reason:
            reason = f"GSTR-1 regenerated for period {period}"

    elif normalized_action == 'DOWNLOAD':
        field_name = 'file_download'
        fmt_str = (format or 'excel').lower()
        dl_payload = {
            "format": fmt_str,
            "filename": filename or f"GSTR1_{period}.{'xlsx' if fmt_str == 'excel' else 'json'}",
            "return_period": period
        }
        new_value = _safe_json_dumps(dl_payload)
        if not reason:
            reason = f"Downloaded GSTR-1 {fmt_str.upper()} for period {period}"

    else:
        # Fallback for generic actions
        field_name = 'status'
        new_value = _safe_json_dumps(extract_generation_summary(generation))
        if not reason:
            reason = f"GSTR-1 {normalized_action} for period {period}"

    # Resolve IP address safely
    resolved_ip = ip_address[:45] if ip_address else _get_remote_addr()

    audit_entry = AuditLog(
        user_id=user_id,
        entity_type='GSTR1Generation',
        entity_id=gen_id,
        action=normalized_action[:20],
        field_name=field_name[:100] if field_name else None,
        old_value=old_value,
        new_value=new_value,
        reason=reason[:255] if reason else None,
        ip_address=resolved_ip
    )

    db.session.add(audit_entry)
    if commit:
        db.session.commit()

    return audit_entry
