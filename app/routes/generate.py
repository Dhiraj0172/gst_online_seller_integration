import os
import json
from datetime import datetime
from flask import render_template, request, session, flash, redirect, url_for, jsonify, send_file, abort, current_app
from flask_login import login_required, current_user
from app.models import GSTR1Generation, GSTProfile
from app.services.gstr1_generator import generate_gstr1, GenerationBlockedError
from app.services.reconciliation_service import run_full_reconciliation
from app.services.audit_service import log_generation_audit, extract_generation_summary
from app.extensions import db
from . import generate_bp

def get_active_profile():
    requested_id = request.args.get('profile_id') or session.get('active_profile_id')
    if requested_id:
        try:
            p = GSTProfile.query.filter_by(id=int(requested_id), user_id=current_user.id).first()
            if p:
                session['active_profile_id'] = p.id
                return p
        except (ValueError, TypeError):
            pass
        session.pop('active_profile_id', None)
    first_p = GSTProfile.query.filter_by(user_id=current_user.id).first()
    if first_p:
        session['active_profile_id'] = first_p.id
        return first_p
    return None

@generate_bp.route('/generate')
@login_required
def index():
    profile = get_active_profile()
    generations = []
    recon_report = None
    return_period = request.args.get('return_period') or session.get('return_period', '012025')
    if profile:
        generations = GSTR1Generation.query.filter_by(profile_id=profile.id).order_by(GSTR1Generation.created_at.desc()).all()
        recon_report = run_full_reconciliation(profile.id, return_period)
    return render_template(
        'generate.html',
        generations=generations,
        recon_report=recon_report,
        return_period=return_period,
    )

def _save_generation_record(profile, return_period, gen_result, recon_report, existing_gen_id=None, audit_action='CREATE', old_summary=None):
    stats = gen_result.stats or {}
    val_passed = gen_result.validation_result.is_valid if gen_result.validation_result else True
    val_errors = json.dumps(gen_result.validation_result.errors) if gen_result.validation_result and hasattr(gen_result.validation_result, 'errors') else "[]"
    recon_json = json.dumps(recon_report.to_dict() if hasattr(recon_report, 'to_dict') else recon_report)
    schema_ver = "1.1"
    rule_ver = profile.financial_year or "2024-25"
    now = datetime.utcnow()

    gen = None
    if existing_gen_id:
        try:
            gen = GSTR1Generation.query.filter_by(id=int(existing_gen_id), profile_id=profile.id).first()
        except (ValueError, TypeError):
            gen = None

    if gen and not old_summary and audit_action == 'REGENERATE':
        old_summary = extract_generation_summary(gen)
        old_summary['previous_generation_id'] = gen.id

    if gen:
        gen.generation_status = 'COMPLETED'
        gen.generation_completed_at = now
        gen.excel_file_path = gen_result.excel_path
        gen.json_file_path = gen_result.json_path
        gen.total_b2b = stats.get('total_b2b', 0)
        gen.total_b2cs = stats.get('total_b2cs', 0)
        gen.total_b2cl = stats.get('total_b2cl', 0)
        gen.total_cdnr = stats.get('total_cdnr', 0)
        gen.total_cdnur = stats.get('total_cdnur', 0)
        gen.total_nil = stats.get('total_nil', 0)
        gen.total_hsn_b2b = stats.get('total_hsn_b2b', 0)
        gen.total_hsn_b2c = stats.get('total_hsn_b2c', 0)
        gen.total_ecom = stats.get('total_ecom', 0)
        gen.total_taxable_value = stats.get('total_taxable_value', 0)
        gen.total_cgst = stats.get('total_cgst', 0)
        gen.total_sgst = stats.get('total_sgst', 0)
        gen.total_igst = stats.get('total_igst', 0)
        gen.total_cess = stats.get('total_cess', 0)
        gen.total_tax = stats.get('total_tax', 0)
        gen.validation_passed = val_passed
        gen.validation_errors = val_errors
        gen.reconciliation_report = recon_json
        gen.schema_version = schema_ver
        gen.rule_version = rule_ver
        gen.financial_year = rule_ver
    else:
        user_id = current_user.id if (current_user and current_user.is_authenticated) else profile.user_id
        gen = GSTR1Generation(
            profile_id=profile.id,
            user_id=user_id,
            return_period=return_period,
            financial_year=rule_ver,
            generation_status='COMPLETED',
            generation_started_at=now,
            generation_completed_at=now,
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

    audit_user_id = current_user.id if (current_user and current_user.is_authenticated) else profile.user_id
    log_generation_audit(
        user_id=audit_user_id,
        action=audit_action,
        generation=gen,
        return_period=return_period,
        profile_id=profile.id,
        old_summary=old_summary,
        commit=False
    )

    db.session.commit()
    return gen

@generate_bp.route('/generate/run', methods=['POST'])
@login_required
def run():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('profile.list_profiles'))
        
    return_period = request.form.get('return_period') or request.args.get('return_period') or session.get('return_period', '012025')
    include_hsn_val = request.form.get('include_hsn') or request.args.get('include_hsn')
    include_hsn = True if include_hsn_val is None else str(include_hsn_val).lower() not in ('0', 'false', 'f')

    # Pre-generation reconciliation gate: block generation if critical reconciliation errors exist
    recon_report = run_full_reconciliation(profile.id, return_period)
    if recon_report.is_generation_blocked:
        flash(
            f"GSTR-1 generation is blocked: {recon_report.critical_failures} critical reconciliation error(s) detected. "
            "Please review and fix validation errors before generating returns.",
            "danger",
        )
        return redirect(url_for('statement.validation_errors', return_period=return_period))
    elif recon_report.status == 'WARNING':
        flash(
            f"Note: Generated with {recon_report.warnings} reconciliation warning(s). "
            "Pre-filing review recommended.",
            "warning",
        )
    try:
        gen_result = generate_gstr1(
            str(profile.id),
            return_period,
            include_hsn=include_hsn,
            financial_year=profile.financial_year,
            reconciliation_report=recon_report.to_dict()
        )
        _save_generation_record(profile, return_period, gen_result, recon_report)
        flash('GSTR-1 Excel and JSON generated successfully!', 'success')
    except GenerationBlockedError as e:
        db.session.rollback()
        flash(
            f"GSTR-1 generation is blocked: {e.message}",
            "danger",
        )
        return redirect(url_for('statement.validation_errors', return_period=return_period))
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error during GSTR-1 generation: {e}")
        flash('An error occurred during GSTR-1 generation. Please verify your data and try again.', 'danger')
        
    return redirect(url_for('generate.index', return_period=return_period))

@generate_bp.route('/generate/status/<int:id>')
@login_required
def status(id):
    profile = get_active_profile()
    if not profile:
        abort(404)
    gen = GSTR1Generation.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    return jsonify({"status": gen.generation_status})

@generate_bp.route('/generate/download/excel/<int:id>')
@login_required
def download_excel(id):
    profile = get_active_profile()
    if not profile:
        abort(404)
    gen = GSTR1Generation.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    filename = f"GSTR1_{profile.gstin}_{gen.return_period}.xlsx"
    log_generation_audit(
        user_id=current_user.id,
        action='DOWNLOAD',
        generation=gen,
        return_period=gen.return_period,
        profile_id=profile.id,
        format='excel',
        filename=filename,
        commit=True
    )
    if gen.excel_file_path and os.path.exists(gen.excel_file_path):
        return send_file(
            gen.excel_file_path,
            as_attachment=True,
            download_name=filename
        )
    # If not on disk, regenerate on the fly
    try:
        gen_result = generate_gstr1(str(profile.id), gen.return_period, enforce_gate=True)
        return send_file(
            gen_result.excel_path,
            as_attachment=True,
            download_name=filename
        )
    except GenerationBlockedError:
        flash("File download unavailable: GSTR-1 generation is blocked by reconciliation errors.", "danger")
        return redirect(url_for('generate.index'))

@generate_bp.route('/generate/download/json/<int:id>')
@login_required
def download_json(id):
    profile = get_active_profile()
    if not profile:
        abort(404)
    gen = GSTR1Generation.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    filename = f"GSTR1_{profile.gstin}_{gen.return_period}.json"
    log_generation_audit(
        user_id=current_user.id,
        action='DOWNLOAD',
        generation=gen,
        return_period=gen.return_period,
        profile_id=profile.id,
        format='json',
        filename=filename,
        commit=True
    )
    if gen.json_file_path and os.path.exists(gen.json_file_path):
        return send_file(
            gen.json_file_path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/json"
        )
    try:
        gen_result = generate_gstr1(str(profile.id), gen.return_period, enforce_gate=True)
        return send_file(
            gen_result.json_path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/json"
        )
    except GenerationBlockedError:
        flash("File download unavailable: GSTR-1 generation is blocked by reconciliation errors.", "danger")
        return redirect(url_for('generate.index'))

@generate_bp.route('/generate/validation/<int:id>')
@login_required
def validation(id):
    profile = get_active_profile()
    if profile:
        gen = GSTR1Generation.query.filter_by(id=id, profile_id=profile.id).first()
        if gen and gen.return_period:
            return redirect(url_for('statement.validation_errors', return_period=gen.return_period))
    return redirect(url_for('statement.validation_errors'))

@generate_bp.route('/generate/regenerate', methods=['POST'])
@login_required
def regenerate():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('profile.list_profiles'))

    existing_id = request.form.get('id') or request.form.get('generation_id') or request.args.get('id') or request.args.get('generation_id')
    return_period = request.form.get('return_period') or request.args.get('return_period')

    previous_gen = None
    if existing_id:
        try:
            previous_gen = GSTR1Generation.query.filter_by(id=int(existing_id), profile_id=profile.id).first()
            if previous_gen and not return_period:
                return_period = previous_gen.return_period
        except (ValueError, TypeError):
            pass

    if not return_period:
        return_period = session.get('return_period', '012025')

    if not previous_gen and return_period:
        previous_gen = GSTR1Generation.query.filter_by(
            profile_id=profile.id,
            return_period=return_period
        ).order_by(GSTR1Generation.created_at.desc()).first()

    old_summary = extract_generation_summary(previous_gen) if previous_gen else None
    if previous_gen and old_summary:
        old_summary['previous_generation_id'] = previous_gen.id

    include_hsn_val = request.form.get('include_hsn') or request.args.get('include_hsn')
    include_hsn = True if include_hsn_val is None else str(include_hsn_val).lower() not in ('0', 'false', 'f')

    # Pre-generation reconciliation gate: block generation if critical reconciliation errors exist
    recon_report = run_full_reconciliation(profile.id, return_period)
    if recon_report.is_generation_blocked:
        flash(
            f"GSTR-1 generation is blocked: {recon_report.critical_failures} critical reconciliation error(s) detected. "
            "Please review and fix validation errors before generating returns.",
            "danger",
        )
        return redirect(url_for('statement.validation_errors', return_period=return_period))
    elif recon_report.status == 'WARNING':
        flash(
            f"Note: Generated with {recon_report.warnings} reconciliation warning(s). "
            "Pre-filing review recommended.",
            "warning",
        )

    try:
        gen_result = generate_gstr1(
            str(profile.id),
            return_period,
            include_hsn=include_hsn,
            financial_year=profile.financial_year,
            reconciliation_report=recon_report.to_dict()
        )
        _save_generation_record(
            profile,
            return_period,
            gen_result,
            recon_report,
            existing_gen_id=existing_id,
            audit_action='REGENERATE',
            old_summary=old_summary
        )
        flash('GSTR-1 regenerated successfully!', 'success')
    except GenerationBlockedError as e:
        db.session.rollback()
        flash(
            f"GSTR-1 generation is blocked: {e.message}",
            "danger",
        )
        return redirect(url_for('statement.validation_errors', return_period=return_period))
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error during GSTR-1 regeneration: {e}")
        flash('An error occurred during GSTR-1 generation. Please verify your data and try again.', 'danger')

    return redirect(url_for('generate.index', return_period=return_period))

