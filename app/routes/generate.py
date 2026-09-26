import os
from datetime import datetime
from flask import render_template, request, session, flash, redirect, url_for, jsonify, send_file, abort, current_app
from flask_login import login_required, current_user
from app.models import GSTR1Generation, GSTProfile
from app.services.gstr1_generator import generate_gstr1
from app.services.reconciliation_service import run_full_reconciliation
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

@generate_bp.route('/generate/run', methods=['POST', 'GET'])
@login_required
def run():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('profile.list_profiles'))
        
    return_period = request.form.get('return_period') or request.args.get('return_period') or session.get('return_period', '012025')
    force = request.form.get('force') in ('true', '1') or request.args.get('force') in ('true', '1')

    enforce_gate = request.form.get('enforce_gate') in ('true', '1') or request.args.get('enforce_gate') in ('true', '1')

    # Pre-generation reconciliation gate: block generation if critical reconciliation errors exist when enforce_gate is active
    recon_report = run_full_reconciliation(profile.id, return_period)
    if recon_report.is_generation_blocked:
        if enforce_gate and not force:
            flash(
                f"GSTR-1 generation is blocked: {recon_report.critical_failures} critical reconciliation error(s) detected. "
                "Please review and fix validation errors before generating returns.",
                "danger",
            )
            return redirect(url_for('statement.validation_errors', return_period=return_period))
        elif not force:
            flash(
                f"Note: Generated with {recon_report.critical_failures} reconciliation error(s). "
                "Pre-filing review recommended.",
                "warning",
            )
    try:
        gen_result = generate_gstr1(str(profile.id), return_period)
        
        gen = GSTR1Generation(
            profile_id=profile.id,
            user_id=current_user.id,
            return_period=return_period,
            generation_status='COMPLETED',
            excel_file_path=gen_result.excel_path,
            json_file_path=gen_result.json_path,
            total_b2b=gen_result.stats.get('total_invoices', 0),
            total_taxable_value=gen_result.stats.get('total_taxable_value', 0),
            validation_passed=gen_result.validation_result.is_valid if gen_result.validation_result else True,
            generation_started_at=datetime.utcnow(),
            generation_completed_at=datetime.utcnow()
        )
        db.session.add(gen)
        db.session.commit()
        flash('GSTR-1 Excel and JSON generated successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error during GSTR-1 generation: {e}")
        flash('An error occurred during GSTR-1 generation. Please verify your data and try again.', 'danger')
        
    return redirect(url_for('generate.index'))

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
    if gen.excel_file_path and os.path.exists(gen.excel_file_path):
        return send_file(
            gen.excel_file_path,
            as_attachment=True,
            download_name=f"GSTR1_{profile.gstin}_{gen.return_period}.xlsx"
        )
    # If not on disk, regenerate on the fly
    gen_result = generate_gstr1(str(profile.id), gen.return_period)
    return send_file(
        gen_result.excel_path,
        as_attachment=True,
        download_name=f"GSTR1_{profile.gstin}_{gen.return_period}.xlsx"
    )

@generate_bp.route('/generate/download/json/<int:id>')
@login_required
def download_json(id):
    profile = get_active_profile()
    if not profile:
        abort(404)
    gen = GSTR1Generation.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    if gen.json_file_path and os.path.exists(gen.json_file_path):
        return send_file(
            gen.json_file_path,
            as_attachment=True,
            download_name=f"GSTR1_{profile.gstin}_{gen.return_period}.json",
            mimetype="application/json"
        )
    gen_result = generate_gstr1(str(profile.id), gen.return_period)
    return send_file(
        gen_result.json_path,
        as_attachment=True,
        download_name=f"GSTR1_{profile.gstin}_{gen.return_period}.json",
        mimetype="application/json"
    )

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
    return redirect(url_for('generate.run'))
