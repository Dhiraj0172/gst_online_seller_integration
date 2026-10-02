import json
from datetime import datetime
from flask import jsonify, request, session, url_for
from flask_login import login_required, current_user
from decimal import Decimal
from sqlalchemy import func
from app.models import GSTProfile, ImportHistory, Transaction, GSTR1Generation, TCSReconciliation, AuditLog
from app import db
from app.adapters.registry import _ADAPTER_REGISTRY
from app.services.reconciliation_service import run_full_reconciliation
from app.services.validation_service import get_pre_filing_review, get_validation_issues
from app.services.gstr1_generator import generate_gstr1, GenerationBlockedError
from app.services.audit_service import log_generation_audit
from . import api_bp

def get_active_profile_id():
    requested_id = request.args.get('profile_id')
    if requested_id is not None:
        try:
            p = GSTProfile.query.filter_by(id=int(requested_id), user_id=current_user.id).first()
            if p:
                session['active_profile_id'] = p.id
                return p.id
        except (ValueError, TypeError):
            pass
        session.pop('active_profile_id', None)

    sess_id = session.get('active_profile_id')
    if sess_id:
        try:
            p = GSTProfile.query.filter_by(id=int(sess_id), user_id=current_user.id).first()
            if p:
                return p.id
        except (ValueError, TypeError):
            pass
        session.pop('active_profile_id', None)

    first_p = GSTProfile.query.filter_by(user_id=current_user.id).first()
    if first_p:
        session['active_profile_id'] = first_p.id
        return first_p.id
    return None

def get_active_return_period(profile_id=None):
    """Retrieve active return period with query param priority and session persistence."""
    if 'return_period' in request.args:
        val = request.args.get('return_period', '').strip()
        if val:
            session['return_period'] = val
            return val
        return None

    if 'period' in request.args:
        val = request.args.get('period', '').strip()
        if val:
            session['return_period'] = val
            return val
        return None

    sess_period = session.get('return_period')
    if sess_period and str(sess_period).strip():
        return str(sess_period).strip()

    if profile_id:
        latest_imp = (
            ImportHistory.query.filter_by(profile_id=profile_id)
            .order_by(ImportHistory.created_at.desc())
            .first()
        )
        if latest_imp and latest_imp.return_period:
            return latest_imp.return_period

    return '012025'

def api_response(data=None, error=False, message="", status=200, **kwargs):
    payload = {
        "success": not error,
        "error": error,
        "message": message,
        "data": data
    }
    payload.update(kwargs)
    return jsonify(payload), status

@api_bp.route('/dashboard-stats')
@login_required
def dashboard_stats():
    profile_id = get_active_profile_id()
    if not profile_id:
        return api_response({
            'total_invoices': 0,
            'taxable_value': '0.00',
            'total_tax': '0.00',
            'gstr1_status': 'Pending',
            'taxable_value_raw': 0.0,
            'total_tax_raw': 0.0,
            'breakdown': {'b2b': 0, 'b2c': 0, 'cdnr': 0, 'nil': 0},
            'tax_summary': {'cgst': '0.00', 'sgst': '0.00', 'igst': '0.00', 'cess': '0.00'},
            'return_period': None,
            'profile_id': None,
            'stats': {
                'total_invoices': 0,
                'taxable_value': '0.00',
                'total_tax': '0.00',
                'gstr1_status': 'Pending'
            }
        })

    return_period = get_active_return_period(profile_id)
    tx_query = (
        Transaction.query.filter_by(profile_id=profile_id, is_deleted=False)
        .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
        .filter(ImportHistory.return_period == return_period)
    )

    total_inv = tx_query.count()
    tot_taxable = tx_query.with_entities(func.coalesce(func.sum(Transaction.taxable_value), 0)).scalar() or Decimal('0.00')
    tot_cgst = tx_query.with_entities(func.coalesce(func.sum(Transaction.cgst_amount), 0)).scalar() or Decimal('0.00')
    tot_sgst = tx_query.with_entities(func.coalesce(func.sum(Transaction.sgst_amount), 0)).scalar() or Decimal('0.00')
    tot_igst = tx_query.with_entities(func.coalesce(func.sum(Transaction.igst_amount), 0)).scalar() or Decimal('0.00')
    tot_cess = tx_query.with_entities(func.coalesce(func.sum(Transaction.cess_amount), 0)).scalar() or Decimal('0.00')
    tot_tax = tot_cgst + tot_sgst + tot_igst + tot_cess

    stats_block = {
        'total_invoices': total_inv,
        'taxable_value': f"{tot_taxable:,.2f}",
        'total_tax': f"{tot_tax:,.2f}",
        'gstr1_status': 'Pending'
    }

    tax_summary = {
        'cgst': f"{tot_cgst:,.2f}",
        'sgst': f"{tot_sgst:,.2f}",
        'igst': f"{tot_igst:,.2f}",
        'cess': f"{tot_cess:,.2f}"
    }

    breakdown = {
        'b2b': tx_query.filter(Transaction.supply_type.in_(['B2B', 'B2BA'])).count(),
        'b2c': tx_query.filter(Transaction.supply_type.in_(['B2CS', 'B2CSA', 'B2CL', 'B2CLA'])).count(),
        'cdnr': tx_query.filter(Transaction.supply_type.in_(['CDNR', 'CDNRA', 'CDNUR', 'CDNURA'])).count(),
        'nil': tx_query.filter(Transaction.supply_type.in_(['NIL', 'EXEMPT', 'NONGST'])).count(),
    }

    gen_query = GSTR1Generation.query.filter_by(profile_id=profile_id)
    if return_period:
        gen_query = gen_query.filter_by(return_period=return_period)
    latest_gen = gen_query.order_by(GSTR1Generation.created_at.desc()).first()
    if latest_gen and latest_gen.generation_status == 'COMPLETED':
        stats_block['gstr1_status'] = 'Generated'
    else:
        stats_block['gstr1_status'] = 'Pending'

    data = {
        'total_invoices': total_inv,
        'taxable_value': stats_block['taxable_value'],
        'total_tax': stats_block['total_tax'],
        'gstr1_status': stats_block['gstr1_status'],
        'taxable_value_raw': float(tot_taxable),
        'total_tax_raw': float(tot_tax),
        'breakdown': breakdown,
        'tax_summary': tax_summary,
        'return_period': return_period,
        'profile_id': profile_id,
        'stats': stats_block
    }
    return api_response(data)

@api_bp.route('/platforms')
@login_required
def platforms():
    platform_names = sorted(list(_ADAPTER_REGISTRY.keys()))
    return api_response(platform_names)

@api_bp.route('/validate-gstin', methods=['POST'])
@login_required
def validate_gstin():
    gstin = request.json.get('gstin')
    if not gstin or len(gstin) != 15:
        return api_response(error=True, message="Invalid GSTIN", status=400)
    return api_response({"valid": True})

@api_bp.route('/profiles')
@login_required
def profiles():
    profiles = GSTProfile.query.filter_by(user_id=current_user.id).all()
    return api_response([{"id": p.id, "business_name": getattr(p, 'trade_name', None) or p.legal_name} for p in profiles])

def _get_section_transactions(tx_type):
    profile_id = get_active_profile_id()
    return_period = get_active_return_period(profile_id)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    if not profile_id or not return_period:
        empty_summary = {
            "count": 0,
            "total_taxable": 0.0,
            "total_tax": 0.0,
            "total_invoice_value": 0.0,
        }
        return api_response(
            [],
            total=0,
            pages=0,
            page=page,
            per_page=per_page,
            return_period=return_period,
            summary=empty_summary
        )

    search = request.args.get('search', '').strip()

    query = (
        db.session.query(Transaction)
        .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
        .filter(
            Transaction.profile_id == profile_id,
            Transaction.is_deleted == False,
            ImportHistory.return_period == return_period,
        )
    )

    if tx_type == 'B2B':
        query = query.filter(Transaction.supply_type.in_(['B2B', 'B2BA']))
    elif tx_type == 'B2C':
        query = query.filter(Transaction.supply_type.in_(['B2CS', 'B2CSA', 'B2CL', 'B2CLA']))
    elif tx_type == 'CDNR':
        query = query.filter(Transaction.supply_type.in_(['CDNR', 'CDNRA', 'CDNUR', 'CDNURA']))
    elif tx_type == 'NIL':
        query = query.filter(Transaction.supply_type.in_(['NIL', 'EXEMPT', 'NONGST']))
    elif tx_type in ('HSN', 'HSN_B2B'):
        query = query.filter(
            Transaction.hsn_sac.isnot(None),
            Transaction.hsn_sac != '',
            func.trim(Transaction.hsn_sac) != '',
            Transaction.supply_type.in_(['B2B', 'B2BA', 'HSN']),
        )
    elif tx_type == 'HSN_B2C':
        query = query.filter(
            Transaction.hsn_sac.isnot(None),
            Transaction.hsn_sac != '',
            func.trim(Transaction.hsn_sac) != '',
            Transaction.supply_type.in_(['B2CS', 'B2CSA', 'B2CL', 'B2CLA', 'HSNB2C']),
        )
    elif tx_type == 'ECOM':
        query = query.filter(Transaction.ecommerce_gstin.isnot(None), Transaction.ecommerce_gstin != '')

    if search:
        query = query.filter(
            (Transaction.invoice_number.ilike(f'%{search}%')) |
            (Transaction.customer_gstin.ilike(f'%{search}%')) |
            (Transaction.customer_name.ilike(f'%{search}%')) |
            (Transaction.place_of_supply.ilike(f'%{search}%')) |
            (Transaction.hsn_sac.ilike(f'%{search}%'))
        )

    summary_row = query.with_entities(
        func.count(Transaction.id),
        func.coalesce(func.sum(Transaction.taxable_value), 0),
        func.coalesce(func.sum(Transaction.total_tax), 0),
        func.coalesce(func.sum(Transaction.invoice_value), 0),
    ).first()

    summary = {
        "count": int(summary_row[0]) if summary_row else 0,
        "total_taxable": float(summary_row[1]) if summary_row else 0.0,
        "total_tax": float(summary_row[2]) if summary_row else 0.0,
        "total_invoice_value": float(summary_row[3]) if summary_row else 0.0,
    }

    paginated = query.order_by(Transaction.id.desc()).paginate(page=page, per_page=per_page, error_out=False)

    items = [{
        "id": t.id,
        "invoice_number": t.invoice_number,
        "invoice_date": t.invoice_date.strftime('%d-%m-%Y') if t.invoice_date else None,
        "customer_gstin": t.customer_gstin or '',
        "customer_name": t.customer_name or '',
        "place_of_supply": t.place_of_supply or '',
        "taxable_value": float(t.taxable_value) if t.taxable_value else 0.0,
        "cgst_amount": float(t.cgst_amount) if t.cgst_amount else 0.0,
        "sgst_amount": float(t.sgst_amount) if t.sgst_amount else 0.0,
        "igst_amount": float(t.igst_amount) if t.igst_amount else 0.0,
        "cess_amount": float(t.cess_amount) if t.cess_amount else 0.0,
        "total_tax": float(t.total_tax) if t.total_tax is not None else float((t.cgst_amount or 0) + (t.sgst_amount or 0) + (t.igst_amount or 0) + (t.cess_amount or 0)),
        "invoice_value": float(t.invoice_value) if t.invoice_value else 0.0,
        "tax_rate": float(t.tax_rate) if t.tax_rate else 0.0,
        "hsn_sac": t.hsn_sac or '',
        "supply_type": t.supply_type or '',
        "ecommerce_gstin": t.ecommerce_gstin or '',
        "status": "Valid" if not getattr(t, 'validation_errors', None) else "Warning",
        "return_period": return_period,
    } for t in paginated.items]

    return api_response(
        items,
        total=paginated.total,
        pages=paginated.pages,
        page=page,
        per_page=per_page,
        return_period=return_period,
        summary=summary
    )

@api_bp.route('/b2b')
@login_required
def b2b():
    return _get_section_transactions('B2B')

@api_bp.route('/b2c')
@login_required
def b2c():
    return _get_section_transactions('B2C')

@api_bp.route('/cdnr')
@login_required
def cdnr():
    return _get_section_transactions('CDNR')

@api_bp.route('/nil')
@login_required
def nil():
    return _get_section_transactions('NIL')

@api_bp.route('/hsn-b2b')
@login_required
def hsn_b2b():
    return _get_section_transactions('HSN_B2B')

@api_bp.route('/hsn-b2c')
@login_required
def hsn_b2c():
    return _get_section_transactions('HSN_B2C')

@api_bp.route('/ecom')
@login_required
def ecom():
    return _get_section_transactions('ECOM')

@api_bp.route('/reconciliation')
@login_required
def reconciliation():
    profile_id = get_active_profile_id()
    if not profile_id:
        return api_response(error=True, message="No active GST profile found", status=404)
    return_period = request.args.get('return_period') or session.get('return_period', '012025')
    report = run_full_reconciliation(profile_id, return_period)
    return api_response(report.to_dict())

@api_bp.route('/import-history')
@login_required
def import_history():
    profile_id = get_active_profile_id()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    if not profile_id:
        return api_response([], total=0, pages=0, page=page, per_page=per_page)

    query = ImportHistory.query.filter_by(profile_id=profile_id, user_id=current_user.id)
    return_period = request.args.get('return_period')
    if return_period:
        query = query.filter_by(return_period=return_period)

    paginated = query.order_by(ImportHistory.created_at.desc(), ImportHistory.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    results = []
    for ih in paginated.items:
        results.append({
            "id": ih.id,
            "user_id": ih.user_id,
            "profile_id": ih.profile_id,
            "file_name": ih.file_name,
            "original_file_name": ih.original_file_name,
            "platform_name": ih.platform_name,
            "return_period": ih.return_period,
            "financial_year": ih.financial_year,
            "total_rows": ih.total_rows or 0,
            "success_rows": ih.success_rows or 0,
            "warning_rows": ih.warning_rows or 0,
            "error_rows": ih.error_rows or 0,
            "skipped_rows": ih.skipped_rows or 0,
            "processing_status": ih.processing_status,
            "processing_duration_ms": ih.processing_duration_ms,
            "created_at": ih.created_at.isoformat() if ih.created_at else None,
        })

    return api_response(results, total=paginated.total, pages=paginated.pages, page=page, per_page=per_page)

@api_bp.route('/validation-errors')
@login_required
def validation_errors():
    profile_id = get_active_profile_id()
    if not profile_id:
        return api_response(error=True, message="No active GST profile found", status=404)

    return_period = request.args.get('return_period') or session.get('return_period', '012025')
    severity = request.args.get('severity', '').strip().upper() or None
    search = request.args.get('search', '').strip() or None

    review = get_pre_filing_review(
        profile_id=profile_id,
        return_period=return_period,
        severity=severity,
        search=search,
    )
    if request.args.get('as_list') == 'true' or request.args.get('format') == 'list':
        return api_response(review['issues'])
    return api_response(review)

@api_bp.route('/tcs-reconciliation')
@login_required
def tcs_reconciliation():
    profile_id = get_active_profile_id()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    if not profile_id:
        return api_response([], total=0, pages=0, page=page, per_page=per_page)

    query = TCSReconciliation.query.filter_by(profile_id=profile_id, user_id=current_user.id)
    return_period = request.args.get('return_period')
    if return_period:
        query = query.filter_by(return_period=return_period)

    paginated = query.order_by(
        TCSReconciliation.return_period.desc(),
        TCSReconciliation.state_code.asc(),
        TCSReconciliation.id.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)

    results = []
    for item in paginated.items:
        results.append({
            "id": item.id,
            "profile_id": item.profile_id,
            "user_id": item.user_id,
            "return_period": item.return_period,
            "import_history_id": item.import_history_id,
            "ecommerce_gstin": item.ecommerce_gstin or '',
            "state_code": item.state_code,
            "state_name": item.state_name or '',
            "our_net_taxable_value": float(item.our_net_taxable_value or 0),
            "our_calculated_tcs": float(item.our_calculated_tcs or 0),
            "our_calculated_cgst": float(item.our_calculated_cgst or 0),
            "our_calculated_sgst": float(item.our_calculated_sgst or 0),
            "our_calculated_igst": float(item.our_calculated_igst or 0),
            "portal_taxable_value": float(item.portal_taxable_value or 0),
            "portal_tcs": float(item.portal_tcs or 0),
            "portal_cgst_tcs": float(item.portal_cgst_tcs or 0),
            "portal_sgst_tcs": float(item.portal_sgst_tcs or 0),
            "portal_igst_tcs": float(item.portal_igst_tcs or 0),
            "difference_taxable": float(item.difference_taxable or 0),
            "difference_tcs": float(item.difference_tcs or 0),
            "difference_cgst": float(item.difference_cgst or 0),
            "difference_sgst": float(item.difference_sgst or 0),
            "difference_igst": float(item.difference_igst or 0),
            "match_status": item.match_status,
            "ambiguity_details": item.ambiguity_details,
            "adjustment_notes": item.adjustment_notes,
            "is_adjusted": bool(item.is_adjusted),
            "created_at": item.created_at.isoformat() if item.created_at else None,
        })

    return api_response(results, total=paginated.total, pages=paginated.pages, page=page, per_page=per_page)

@api_bp.route('/audit-logs')
@login_required
def audit_logs():
    profile_id = get_active_profile_id()
    query = AuditLog.query.filter_by(user_id=current_user.id)

    entity_type = request.args.get('entity_type', '').strip()
    action = request.args.get('action', '').strip()
    return_period = request.args.get('return_period', '').strip()
    start_date_str = request.args.get('start_date', '').strip()
    end_date_str = request.args.get('end_date', '').strip()

    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)

    if action:
        query = query.filter(AuditLog.action == action.upper())

    if profile_id:
        gen_ids = db.session.query(GSTR1Generation.id).filter_by(profile_id=profile_id)
        tx_ids = db.session.query(Transaction.id).filter_by(profile_id=profile_id)
        tcs_ids = db.session.query(TCSReconciliation.id).filter_by(profile_id=profile_id)

        profile_condition = (
            (AuditLog.entity_type == 'GSTR1Generation') & (AuditLog.entity_id.in_(gen_ids)) |
            (AuditLog.entity_type == 'Transaction') & (AuditLog.entity_id.in_(tx_ids)) |
            (AuditLog.entity_type == 'TCSReconciliation') & (AuditLog.entity_id.in_(tcs_ids)) |
            (AuditLog.entity_type == 'GSTProfile') & (AuditLog.entity_id == profile_id)
        )
        query = query.filter(profile_condition)

    if return_period:
        rp_gen_ids = db.session.query(GSTR1Generation.id).filter(
            GSTR1Generation.profile_id == profile_id if profile_id else True,
            GSTR1Generation.return_period == return_period
        )
        rp_tx_ids = (
            db.session.query(Transaction.id)
            .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
            .filter(
                Transaction.profile_id == profile_id if profile_id else True,
                ImportHistory.return_period == return_period
            )
        )
        rp_tcs_ids = db.session.query(TCSReconciliation.id).filter(
            TCSReconciliation.profile_id == profile_id if profile_id else True,
            TCSReconciliation.return_period == return_period
        )

        rp_condition = (
            (AuditLog.entity_type == 'GSTR1Generation') & (AuditLog.entity_id.in_(rp_gen_ids)) |
            (AuditLog.entity_type == 'Transaction') & (AuditLog.entity_id.in_(rp_tx_ids)) |
            (AuditLog.entity_type == 'TCSReconciliation') & (AuditLog.entity_id.in_(rp_tcs_ids)) |
            (AuditLog.reason.ilike(f'%{return_period}%')) |
            (AuditLog.new_value.ilike(f'%{return_period}%'))
        )
        query = query.filter(rp_condition)

    if start_date_str:
        try:
            from datetime import datetime as dt
            sd = dt.strptime(start_date_str, '%Y-%m-%d')
            query = query.filter(AuditLog.timestamp >= sd)
        except ValueError:
            pass

    if end_date_str:
        try:
            from datetime import datetime as dt, time as dt_time
            ed = dt.combine(dt.strptime(end_date_str, '%Y-%m-%d').date(), dt_time.max)
            query = query.filter(AuditLog.timestamp <= ed)
        except ValueError:
            pass

    if request.args.get('export') == 'csv':
        all_logs = query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).all()
        import io, csv
        from flask import Response
        from app.utils.csv_utils import sanitize_csv_value

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'Timestamp', 'Action', 'Entity Type', 'Entity ID', 'Field Name', 'Old Value', 'New Value', 'Reason', 'IP Address'])
        for log in all_logs:
            writer.writerow([
                log.id,
                log.timestamp.strftime('%Y-%m-%d %H:%M:%S') if log.timestamp else '',
                sanitize_csv_value(log.action),
                sanitize_csv_value(log.entity_type),
                log.entity_id,
                sanitize_csv_value(log.field_name or ''),
                sanitize_csv_value(log.old_value or ''),
                sanitize_csv_value(log.new_value or ''),
                sanitize_csv_value(log.reason or ''),
                sanitize_csv_value(log.ip_address or ''),
            ])
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=audit_trail.csv"}
        )

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    paginated = query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    results = []
    for log in paginated.items:
        results.append({
            "id": log.id,
            "user_id": log.user_id,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "field_name": log.field_name,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "reason": log.reason,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "ip_address": log.ip_address,
        })

    return api_response(
        results,
        total=paginated.total,
        pages=paginated.pages,
        page=page,
        per_page=per_page,
        return_period=return_period or None
    )


# ==============================================================================
# GSTR-1 Generation REST API Suite
# ==============================================================================

@api_bp.route('/generate', methods=['POST'])
@login_required
def api_generate():
    body = request.get_json(silent=True) or {}
    return_period = body.get('return_period')
    if not return_period or not isinstance(return_period, str) or len(return_period) != 6 or not return_period.isdigit():
        return jsonify({
            "error": True,
            "message": "Invalid or missing return_period (expected MMYYYY format)",
            "data": None
        }), 400

    profile_id = get_active_profile_id()
    if not profile_id:
        return jsonify({
            "error": True,
            "message": "No active GST profile found",
            "data": None
        }), 404

    profile = GSTProfile.query.filter_by(id=profile_id, user_id=current_user.id).first()
    if not profile:
        return jsonify({
            "error": True,
            "message": "Active profile not found",
            "data": None
        }), 404

    # Strict server-authoritative gate check: client bypass flags (force) are strictly ignored
    recon_report = run_full_reconciliation(profile.id, return_period)
    if recon_report.is_generation_blocked:
        return jsonify({
            "error": "Generation blocked due to reconciliation errors",
            "critical_failures": recon_report.critical_failures,
            "status": "BLOCKED",
            "message": f"Generation blocked: {recon_report.critical_failures} critical reconciliation error(s) detected.",
            "data": {
                "critical_failures": recon_report.critical_failures,
                "status": "BLOCKED"
            }
        }), 403

    include_hsn = body.get('include_hsn', True)
    if isinstance(include_hsn, str):
        include_hsn = include_hsn.lower() not in ('0', 'false', 'f')

    start_time = datetime.utcnow()
    recon_dict = recon_report.to_dict() if hasattr(recon_report, 'to_dict') else recon_report

    try:
        gen_result = generate_gstr1(
            str(profile.id),
            return_period,
            include_hsn=bool(include_hsn),
            financial_year=profile.financial_year,
            reconciliation_report=recon_dict
        )

        stats = gen_result.stats or {}
        val_passed = gen_result.validation_result.is_valid if gen_result.validation_result else True
        val_errors = json.dumps(gen_result.validation_result.errors) if gen_result.validation_result and hasattr(gen_result.validation_result, 'errors') else "[]"
        recon_json = json.dumps(recon_dict)
        schema_ver = "1.1"
        rule_ver = profile.financial_year or "2024-25"
        completed_time = datetime.utcnow()

        gen = GSTR1Generation(
            profile_id=profile.id,
            user_id=current_user.id,
            return_period=return_period,
            financial_year=rule_ver,
            generation_status='COMPLETED',
            generation_started_at=start_time,
            generation_completed_at=completed_time,
            excel_file_path=gen_result.excel_path,
            json_file_path=gen_result.json_path,
            total_b2b=stats.get('total_b2b', 0),
            total_b2cs=stats.get('total_b2cs', 0),
            total_b2cl=stats.get('total_b2cl', 0),
            total_cdnr=stats.get('total_cdnr', 0),
            total_cdnur=stats.get('total_cdnur', 0),
            total_nil=stats.get('total_nil', 0),
            total_hsn_b2b=stats.get('total_hsn_b2b', 0),
            total_hsn_b2c=stats.get('total_hsn_b2c', 0),
            total_ecom=stats.get('total_ecom', 0),
            total_taxable_value=stats.get('total_taxable_value', 0),
            total_cgst=stats.get('total_cgst', 0),
            total_sgst=stats.get('total_sgst', 0),
            total_igst=stats.get('total_igst', 0),
            total_cess=stats.get('total_cess', 0),
            total_tax=stats.get('total_tax', 0),
            validation_passed=val_passed,
            validation_errors=val_errors,
            reconciliation_report=recon_json,
            schema_version=schema_ver,
            rule_version=rule_ver
        )
        db.session.add(gen)
        db.session.flush()

        log_generation_audit(
            user_id=current_user.id,
            action='CREATE',
            generation=gen,
            return_period=return_period,
            profile_id=profile.id,
            commit=False
        )

        db.session.commit()

        resp_data = {
            "id": gen.id,
            "generation_id": gen.id,
            "profile_id": gen.profile_id,
            "return_period": gen.return_period,
            "financial_year": gen.financial_year,
            "generation_status": gen.generation_status,
            "total_b2b": gen.total_b2b,
            "total_b2cs": gen.total_b2cs,
            "total_b2cl": gen.total_b2cl,
            "total_cdnr": gen.total_cdnr,
            "total_cdnur": gen.total_cdnur,
            "total_nil": gen.total_nil,
            "total_hsn_b2b": gen.total_hsn_b2b,
            "total_hsn_b2c": gen.total_hsn_b2c,
            "total_ecom": gen.total_ecom,
            "total_taxable_value": float(gen.total_taxable_value or 0),
            "total_cgst": float(gen.total_cgst or 0),
            "total_sgst": float(gen.total_sgst or 0),
            "total_igst": float(gen.total_igst or 0),
            "total_cess": float(gen.total_cess or 0),
            "total_tax": float(gen.total_tax or 0),
            "stats": stats,
            "validation_passed": gen.validation_passed,
            "excel_download_url": f"/generate/download/excel/{gen.id}",
            "json_download_url": f"/generate/download/json/{gen.id}",
            "created_at": gen.created_at.isoformat() if gen.created_at else None
        }
        return jsonify({
            "error": False,
            "message": "GSTR-1 generated successfully",
            "data": resp_data
        }), 201

    except GenerationBlockedError as e:
        db.session.rollback()
        return jsonify({
            "error": True,
            "message": str(e),
            "data": {
                "status": "BLOCKED",
                "reconciliation_report": e.reconciliation_report
            }
        }), 403

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "error": True,
            "message": f"Generation failed: {str(e)}",
            "data": None
        }), 500


@api_bp.route('/generations', methods=['GET'])
@login_required
def api_list_generations():
    profile_id = get_active_profile_id()
    if not profile_id:
        return api_response([])

    return_period = request.args.get('return_period')
    q = GSTR1Generation.query.filter_by(profile_id=profile_id)
    if return_period:
        q = q.filter_by(return_period=return_period)

    generations = q.order_by(GSTR1Generation.created_at.desc()).all()
    results = []
    for g in generations:
        results.append({
            "id": g.id,
            "generation_id": g.id,
            "profile_id": g.profile_id,
            "return_period": g.return_period,
            "financial_year": g.financial_year,
            "generation_status": g.generation_status,
            "total_b2b": g.total_b2b,
            "total_b2cs": g.total_b2cs,
            "total_b2cl": g.total_b2cl,
            "total_cdnr": g.total_cdnr,
            "total_cdnur": g.total_cdnur,
            "total_nil": g.total_nil,
            "total_hsn_b2b": g.total_hsn_b2b,
            "total_hsn_b2c": g.total_hsn_b2c,
            "total_ecom": g.total_ecom,
            "total_taxable_value": float(g.total_taxable_value or 0),
            "total_cgst": float(g.total_cgst or 0),
            "total_sgst": float(g.total_sgst or 0),
            "total_igst": float(g.total_igst or 0),
            "total_cess": float(g.total_cess or 0),
            "total_tax": float(g.total_tax or 0),
            "validation_passed": g.validation_passed,
            "excel_download_url": f"/generate/download/excel/{g.id}",
            "json_download_url": f"/generate/download/json/{g.id}",
            "created_at": g.created_at.isoformat() if g.created_at else None
        })
    return api_response(results)


@api_bp.route('/generate/<int:id>', methods=['GET'])
@login_required
def api_get_generation(id):
    profile_id = get_active_profile_id()
    if not profile_id:
        return api_response(error=True, message="Active profile not found", status=404)

    gen = GSTR1Generation.query.filter_by(id=id, profile_id=profile_id).first()
    if not gen:
        return api_response(error=True, message="Generation record not found", status=404)

    stats = {
        "total_b2b": gen.total_b2b,
        "total_b2cs": gen.total_b2cs,
        "total_b2cl": gen.total_b2cl,
        "total_cdnr": gen.total_cdnr,
        "total_cdnur": gen.total_cdnur,
        "total_nil": gen.total_nil,
        "total_hsn_b2b": gen.total_hsn_b2b,
        "total_hsn_b2c": gen.total_hsn_b2c,
        "total_ecom": gen.total_ecom,
        "total_invoices": (gen.total_b2b or 0) + (gen.total_b2cs or 0) + (gen.total_b2cl or 0) + (gen.total_cdnr or 0) + (gen.total_cdnur or 0) + (gen.total_nil or 0),
        "total_taxable_value": float(gen.total_taxable_value or 0),
        "total_cgst": float(gen.total_cgst or 0),
        "total_sgst": float(gen.total_sgst or 0),
        "total_igst": float(gen.total_igst or 0),
        "total_cess": float(gen.total_cess or 0),
        "total_tax": float(gen.total_tax or 0),
    }

    val_errors = []
    if gen.validation_errors:
        try:
            val_errors = json.loads(gen.validation_errors)
        except Exception:
            pass

    recon_report = {}
    if gen.reconciliation_report:
        try:
            recon_report = json.loads(gen.reconciliation_report)
        except Exception:
            pass

    detail = {
        "id": gen.id,
        "generation_id": gen.id,
        "profile_id": gen.profile_id,
        "return_period": gen.return_period,
        "financial_year": gen.financial_year,
        "generation_status": gen.generation_status,
        "generation_started_at": gen.generation_started_at.isoformat() if gen.generation_started_at else None,
        "generation_completed_at": gen.generation_completed_at.isoformat() if gen.generation_completed_at else None,
        "excel_file_path": gen.excel_file_path,
        "json_file_path": gen.json_file_path,
        "excel_download_url": f"/generate/download/excel/{gen.id}",
        "json_download_url": f"/generate/download/json/{gen.id}",
        "stats": stats,
        "total_b2b": gen.total_b2b,
        "total_b2cs": gen.total_b2cs,
        "total_b2cl": gen.total_b2cl,
        "total_cdnr": gen.total_cdnr,
        "total_cdnur": gen.total_cdnur,
        "total_nil": gen.total_nil,
        "total_hsn_b2b": gen.total_hsn_b2b,
        "total_hsn_b2c": gen.total_hsn_b2c,
        "total_ecom": gen.total_ecom,
        "total_taxable_value": float(gen.total_taxable_value or 0),
        "total_cgst": float(gen.total_cgst or 0),
        "total_sgst": float(gen.total_sgst or 0),
        "total_igst": float(gen.total_igst or 0),
        "total_cess": float(gen.total_cess or 0),
        "total_tax": float(gen.total_tax or 0),
        "validation_passed": gen.validation_passed,
        "validation_errors": val_errors,
        "reconciliation_report": recon_report,
        "schema_version": gen.schema_version,
        "rule_version": gen.rule_version,
        "created_at": gen.created_at.isoformat() if gen.created_at else None
    }
    return api_response(detail)
