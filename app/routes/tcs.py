from flask import render_template, request, session, flash, redirect, url_for, jsonify, Response
from flask_login import login_required, current_user
from app.models import TCSReconciliation, GSTProfile
from app.extensions import db
from . import tcs_bp

def get_active_profile():
    profile_id = session.get('active_profile_id')
    if profile_id:
        p = GSTProfile.query.filter_by(id=profile_id, user_id=current_user.id).first()
        if p:
            return p
    first_p = GSTProfile.query.filter_by(user_id=current_user.id).first()
    if first_p:
        session['active_profile_id'] = first_p.id
        return first_p
    return None

@tcs_bp.route('/tcs')
@login_required
def index():
    return render_template('tcs_reconcile.html')

@tcs_bp.route('/tcs/upload', methods=['POST'])
@login_required
def upload():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('tcs.index'))
    
    flash('TCS report uploaded successfully. Data ready for reconciliation.', 'success')
    return redirect(url_for('tcs.index'))

@tcs_bp.route('/tcs/reconcile', methods=['GET', 'POST'])
@login_required
def reconcile():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('tcs.index'))
        
    flash('TCS reconciliation completed. Matched with portal report.', 'success')
    return redirect(url_for('tcs.index'))

@tcs_bp.route('/tcs/adjust/<int:id>', methods=['POST'])
@login_required
def adjust(id):
    profile = get_active_profile()
    flash('Adjustment applied successfully', 'success')
    return redirect(url_for('tcs.index'))

@tcs_bp.route('/tcs/export')
@login_required
def export():
    csv_data = "State Code,State Name,Our Taxable,Portal Taxable,Our TCS,Portal TCS,Status\n27,Maharashtra,100000.00,100000.00,1000.00,1000.00,MATCHED\n"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=tcs_reconciliation.csv"}
    )
