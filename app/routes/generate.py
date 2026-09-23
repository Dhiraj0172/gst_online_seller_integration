import os
from datetime import datetime
from flask import render_template, request, session, flash, redirect, url_for, jsonify, send_file, abort
from flask_login import login_required, current_user
from app.models import GSTR1Generation, GSTProfile
from app.services.gstr1_generator import generate_gstr1
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
    if profile:
        generations = GSTR1Generation.query.filter_by(profile_id=profile.id).order_by(GSTR1Generation.created_at.desc()).all()
    return render_template('generate.html', generations=generations)

@generate_bp.route('/generate/run', methods=['POST', 'GET'])
@login_required
def run():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('profile.list_profiles'))
        
    return_period = request.form.get('return_period') or session.get('return_period', '012025')
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
        flash(f'Error during generation: {str(e)}', 'danger')
        
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
    return render_template('errors.html')

@generate_bp.route('/generate/regenerate', methods=['POST'])
@login_required
def regenerate():
    return redirect(url_for('generate.run'))
