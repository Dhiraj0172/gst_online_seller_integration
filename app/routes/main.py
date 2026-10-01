from flask import render_template, redirect, url_for, session, request
from flask_login import login_required, current_user
from sqlalchemy import func
from app.extensions import db
from app.models import GSTProfile, Transaction, ImportHistory, GSTR1Generation
from . import main_bp

def _get_active_profile():
    requested_id = request.args.get('profile_id') or session.get('active_profile_id')
    profile = None
    if requested_id:
        try:
            profile = GSTProfile.query.filter_by(id=int(requested_id), user_id=current_user.id).first()
        except (ValueError, TypeError):
            pass
        if not profile:
            session.pop('active_profile_id', None)
        else:
            session['active_profile_id'] = profile.id
    if not profile:
        profile = GSTProfile.query.filter_by(user_id=current_user.id).first()
        if profile:
            session['active_profile_id'] = profile.id
    return profile

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

@main_bp.route('/')
@login_required
def index():
    return redirect(url_for('main.dashboard'))

@main_bp.route('/dashboard')
@login_required
def dashboard():
    profile = _get_active_profile()

    stats = {
        'total_invoices': 0,
        'taxable_value': '0.00',
        'total_tax': '0.00',
        'gstr1_status': 'Pending'
    }
    breakdown = {'b2b': 0, 'b2c': 0, 'cdnr': 0, 'nil': 0}
    tax_summary = {'cgst': '0.00', 'sgst': '0.00', 'igst': '0.00', 'cess': '0.00'}

    return_period = None
    if profile:
        return_period = get_active_return_period(profile.id)
        tx_query = (
            Transaction.query.filter_by(profile_id=profile.id, is_deleted=False)
            .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
            .filter(ImportHistory.return_period == return_period)
        )
        total_inv = tx_query.count()
        tot_taxable = tx_query.with_entities(func.coalesce(func.sum(Transaction.taxable_value), 0)).scalar() or 0
        tot_cgst = tx_query.with_entities(func.coalesce(func.sum(Transaction.cgst_amount), 0)).scalar() or 0
        tot_sgst = tx_query.with_entities(func.coalesce(func.sum(Transaction.sgst_amount), 0)).scalar() or 0
        tot_igst = tx_query.with_entities(func.coalesce(func.sum(Transaction.igst_amount), 0)).scalar() or 0
        tot_cess = tx_query.with_entities(func.coalesce(func.sum(Transaction.cess_amount), 0)).scalar() or 0
        tot_tax = tot_cgst + tot_sgst + tot_igst + tot_cess

        stats['total_invoices'] = total_inv
        stats['taxable_value'] = f"{tot_taxable:,.2f}"
        stats['total_tax'] = f"{tot_tax:,.2f}"

        tax_summary['cgst'] = f"{tot_cgst:,.2f}"
        tax_summary['sgst'] = f"{tot_sgst:,.2f}"
        tax_summary['igst'] = f"{tot_igst:,.2f}"
        tax_summary['cess'] = f"{tot_cess:,.2f}"

        breakdown['b2b'] = tx_query.filter(Transaction.supply_type.in_(['B2B', 'B2BA'])).count()
        breakdown['b2c'] = tx_query.filter(Transaction.supply_type.in_(['B2CS', 'B2CSA', 'B2CL', 'B2CLA'])).count()
        breakdown['cdnr'] = tx_query.filter(Transaction.supply_type.in_(['CDNR', 'CDNRA', 'CDNUR', 'CDNURA'])).count()
        breakdown['nil'] = tx_query.filter(Transaction.supply_type.in_(['NIL', 'EXEMPT', 'NONGST'])).count()

        # Phase 5E-4: Query latest GSTR1Generation for active profile to determine real gstr1_status
        gen_query = GSTR1Generation.query.filter_by(profile_id=profile.id)
        rp_filter = request.args.get('return_period') or request.args.get('period')
        if rp_filter:
            gen_query = gen_query.filter_by(return_period=rp_filter)
        latest_gen = gen_query.order_by(GSTR1Generation.created_at.desc()).first()
        if latest_gen and latest_gen.generation_status == 'COMPLETED':
            stats['gstr1_status'] = 'Generated'
        else:
            stats['gstr1_status'] = 'Pending'

    available_periods = []
    if profile:
        rows = (
            db.session.query(ImportHistory.return_period)
            .filter_by(profile_id=profile.id)
            .distinct()
            .order_by(ImportHistory.return_period.desc())
            .all()
        )
        available_periods = [r[0] for r in rows if r[0]]

    return render_template(
        'dashboard.html',
        stats=stats,
        breakdown=breakdown,
        tax_summary=tax_summary,
        active_return_period=return_period,
        available_periods=available_periods
    )

@main_bp.route('/settings')
@login_required
def settings():
    return render_template('profile.html', profiles=GSTProfile.query.filter_by(user_id=current_user.id).all())

@main_bp.route('/downloads')
@login_required
def downloads():
    profile = _get_active_profile()
    generations = []
    if profile:
        generations = GSTR1Generation.query.filter_by(
            profile_id=profile.id
        ).order_by(GSTR1Generation.created_at.desc()).all()
    return render_template('downloads.html', generations=generations)

@main_bp.route('/errors')
@login_required
def errors():
    return redirect(url_for('statement.validation_errors', **request.args))

# Compatibility / alias routes for templates that reference main.*
@main_bp.route('/profile-redirect')
@login_required
def profile():
    return redirect(url_for('profile.list_profiles'))

@main_bp.route('/import-data')
@login_required
def import_data():
    return redirect(url_for('imports.import_page'))

@main_bp.route('/statement-redirect')
@login_required
def statement():
    return redirect(url_for('statement.index'))

@main_bp.route('/tcs-reconcile')
@login_required
def tcs_reconcile():
    return redirect(url_for('tcs.index'))

@main_bp.route('/generate-redirect')
@login_required
def generate():
    return redirect(url_for('generate.index'))

@main_bp.route('/import-history')
@login_required
def import_history():
    return redirect(url_for('imports.history'))
