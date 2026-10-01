import json
from datetime import datetime
from flask import jsonify, request, session, url_for
from flask_login import login_required, current_user
from app.models import GSTProfile, ImportHistory, Transaction, GSTR1Generation
from app import db
from app.services.reconciliation_service import run_full_reconciliation
from app.services.validation_service import get_pre_filing_review, get_validation_issues
from app.services.gstr1_generator import generate_gstr1
from app.services.audit_service import log_generation_audit
from . import api_bp

def get_active_profile_id():
    requested_id = request.args.get('profile_id') or session.get('active_profile_id')
    if requested_id:
        try:
            p = GSTProfile.query.filter_by(id=int(requested_id), user_id=current_user.id).first()
            if p:
                session['active_profile_id'] = p.id
                return p.id
        except (ValueError, TypeError):
            pass
        session.pop('active_profile_id', None)
    first_p = GSTProfile.query.filter_by(user_id=current_user.id).first()
    if first_p:
        session['active_profile_id'] = first_p.id
        return first_p.id
    return None

def api_response(data=None, error=False, message="", status=200):
    return jsonify({
        "error": error,
        "message": message,
        "data": data
    }), status

@api_bp.route('/dashboard-stats')
@login_required
def dashboard_stats():
    return api_response({"stats": "dummy"})

@api_bp.route('/platforms')
@login_required
def platforms():
    return api_response(["Amazon", "Flipkart", "Meesho"])

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

@api_bp.route('/b2b')
@login_required
def b2b():
    return api_response([])

@api_bp.route('/b2c')
@login_required
def b2c():
    return api_response([])

@api_bp.route('/cdnr')
@login_required
def cdnr():
    return api_response([])

@api_bp.route('/nil')
@login_required
def nil():
    return api_response([])

@api_bp.route('/hsn-b2b')
@login_required
def hsn_b2b():
    return api_response([])

@api_bp.route('/hsn-b2c')
@login_required
def hsn_b2c():
    return api_response([])

@api_bp.route('/ecom')
@login_required
def ecom():
    return api_response([])

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
    return api_response([])

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
    return api_response([])


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

    # Strict server-authoritative gate check: client bypass flags (force, enforce_gate) are strictly ignored
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
