from flask import jsonify, request, session
from flask_login import login_required, current_user
from app.models import GSTProfile, ImportHistory, Transaction
from app import db
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
    return api_response([])

@api_bp.route('/import-history')
@login_required
def import_history():
    return api_response([])

@api_bp.route('/validation-errors')
@login_required
def validation_errors():
    return api_response([])

@api_bp.route('/tcs-reconciliation')
@login_required
def tcs_reconciliation():
    return api_response([])
