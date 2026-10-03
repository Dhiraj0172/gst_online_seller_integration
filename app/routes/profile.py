from flask import render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_required, current_user
from app.models import GSTProfile
from app.utils.state_codes import get_state_name
from app.utils.gstin_validator import validate_gstin
from app.services.audit_service import log_profile_audit
from app.extensions import db
from . import profile_bp
from .auth import is_safe_redirect_url

@profile_bp.route('/profiles')
@login_required
def list_profiles():
    profiles = GSTProfile.query.filter_by(user_id=current_user.id).all()
    return render_template('profile.html', profiles=profiles)

@profile_bp.route('/profiles/create', methods=['GET', 'POST'])
@login_required
def create():
    if request.method == 'POST':
        try:
            gstin = (request.form.get('gstin') or '').strip().upper()
            state_code = request.form.get('state') or gstin[:2] or '27'
            state_name = get_state_name(state_code) or 'Maharashtra'
            legal_name = request.form.get('legal_name') or request.form.get('business_name') or 'Business Entity'
            trade_name = request.form.get('trade_name') or legal_name
            frequency = request.form.get('frequency') or 'Monthly'
            fy = request.form.get('financial_year') or '2024-25'
            
            profile = GSTProfile(
                user_id=current_user.id,
                gstin=gstin,
                legal_name=legal_name,
                trade_name=trade_name,
                state_code=state_code,
                state_name=state_name,
                financial_year=fy,
                filing_frequency=frequency
            )
            db.session.add(profile)
            db.session.flush()

            log_profile_audit(
                user_id=current_user.id,
                profile_id=profile.id,
                action='CREATE',
                old_data=None,
                new_data={
                    "gstin": profile.gstin,
                    "legal_name": profile.legal_name,
                    "trade_name": profile.trade_name,
                    "state_code": profile.state_code,
                    "state_name": profile.state_name,
                    "filing_frequency": profile.filing_frequency,
                    "financial_year": profile.financial_year
                },
                reason=f"Created profile {profile.legal_name} ({profile.gstin})"
            )

            db.session.commit()
            session['active_profile_id'] = profile.id
            flash('Profile created successfully', 'success')
            return redirect(url_for('profile.list_profiles'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(f"Error creating profile: {e}")
            flash('An error occurred while creating the profile. Please check the details and try again.', 'danger')
            
    profiles = GSTProfile.query.filter_by(user_id=current_user.id).all()
    return render_template('profile.html', profiles=profiles, profile=None)

@profile_bp.route('/profiles/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    profile = GSTProfile.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    
    if request.method == 'POST':
        try:
            submitted_gstin = request.form.get('gstin')
            gstin = (submitted_gstin or profile.gstin).strip().upper()

            is_valid, msg = validate_gstin(gstin)
            if not is_valid:
                flash(f'Invalid GSTIN: {msg}', 'danger')
                profiles = GSTProfile.query.filter_by(user_id=current_user.id).all()
                return render_template('profile.html', profiles=profiles, profile=profile), 400

            old_data = {
                "gstin": profile.gstin,
                "legal_name": profile.legal_name,
                "trade_name": profile.trade_name,
                "state_code": profile.state_code,
                "state_name": profile.state_name,
                "filing_frequency": profile.filing_frequency
            }

            profile.gstin = gstin
            profile.legal_name = request.form.get('legal_name') or profile.legal_name
            profile.trade_name = request.form.get('trade_name') or profile.trade_name
            state_code = request.form.get('state') or profile.state_code
            profile.state_code = state_code
            profile.state_name = get_state_name(state_code) or profile.state_name
            profile.filing_frequency = request.form.get('frequency') or profile.filing_frequency

            new_data = {
                "gstin": profile.gstin,
                "legal_name": profile.legal_name,
                "trade_name": profile.trade_name,
                "state_code": profile.state_code,
                "state_name": profile.state_name,
                "filing_frequency": profile.filing_frequency
            }

            log_profile_audit(
                user_id=current_user.id,
                profile_id=profile.id,
                action='UPDATE',
                old_data=old_data,
                new_data=new_data,
                reason=f"Updated profile {profile.legal_name} ({profile.gstin})"
            )

            db.session.commit()
            flash('Profile updated successfully', 'success')
            return redirect(url_for('profile.list_profiles'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(f"Error updating profile {id}: {e}")
            flash('An error occurred while updating the profile. Please try again.', 'danger')
            
    profiles = GSTProfile.query.filter_by(user_id=current_user.id).all()
    return render_template('profile.html', profiles=profiles, profile=profile)

@profile_bp.route('/profiles/<int:id>/delete', methods=['POST'])
@login_required
def delete(id):
    profile = GSTProfile.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    try:
        if str(session.get('active_profile_id')) == str(id):
            session.pop('active_profile_id', None)

        old_data = {
            "gstin": profile.gstin,
            "legal_name": profile.legal_name,
            "trade_name": profile.trade_name,
            "state_code": profile.state_code,
            "state_name": profile.state_name
        }
        log_profile_audit(
            user_id=current_user.id,
            profile_id=profile.id,
            action='DELETE',
            old_data=old_data,
            new_data=None,
            reason=f"Deleted profile {profile.legal_name} ({profile.gstin})"
        )
        db.session.delete(profile)
        db.session.commit()
        flash('Profile deleted successfully', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error deleting profile {id}: {e}")
        flash('An error occurred while deleting the profile. Please try again.', 'danger')
    return redirect(url_for('profile.list_profiles'))

@profile_bp.route('/profiles/<int:id>/select', methods=['POST', 'GET'])
@login_required
def select(id):
    profile = GSTProfile.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    session['active_profile_id'] = profile.id
    flash(f'Active profile set to {profile.legal_name} ({profile.gstin})', 'success')
    target = request.args.get('next') or request.referrer
    if target and is_safe_redirect_url(target):
        return redirect(target)
    return redirect(url_for('main.dashboard'))
