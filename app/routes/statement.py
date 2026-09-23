from flask import render_template, request, session, jsonify, Response
from flask_login import login_required, current_user
import csv
import io
from app.models import Transaction, GSTProfile, AuditLog
from app.extensions import db
from app.utils.csv_utils import sanitize_csv_value
from . import statement_bp

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

@statement_bp.route('/statement')
@login_required
def index():
    return render_template('statement.html')

def get_paginated_transactions(tx_type):
    profile_id = get_active_profile_id()
    if not profile_id:
        return jsonify({"data": [], "total": 0, "pages": 0, "page": 1})
        
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    search = request.args.get('search', '').strip()
    
    query = Transaction.query.filter_by(
        profile_id=profile_id,
        is_deleted=False
    )
    
    if tx_type == 'B2B':
        query = query.filter(Transaction.supply_type == 'B2B')
    elif tx_type == 'B2C':
        query = query.filter(Transaction.supply_type.in_(['B2CS', 'B2CL']))
    elif tx_type == 'CDNR':
        query = query.filter(Transaction.supply_type == 'CDNR')
    elif tx_type == 'CDNUR':
        query = query.filter(Transaction.supply_type == 'CDNUR')
    elif tx_type == 'NIL':
        query = query.filter(Transaction.supply_type.in_(['NIL', 'EXEMPT', 'NONGST']))
    elif tx_type in ('HSN', 'HSN_B2B', 'HSN_B2C'):
        query = query.filter(Transaction.hsn_sac.isnot(None))
    elif tx_type == 'ECOM':
        query = query.filter(Transaction.ecommerce_gstin.isnot(None))
        
    if search:
        query = query.filter(
            (Transaction.invoice_number.ilike(f'%{search}%')) |
            (Transaction.customer_gstin.ilike(f'%{search}%')) |
            (Transaction.customer_name.ilike(f'%{search}%'))
        )
        
    paginated = query.order_by(Transaction.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        "data": [{
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
            "invoice_value": float(t.invoice_value) if t.invoice_value else 0.0,
            "tax_rate": float(t.tax_rate) if t.tax_rate else 0.0,
            "supply_type": t.supply_type or '',
            "status": "Valid" if not getattr(t, 'validation_errors', None) else "Warning"
        } for t in paginated.items],
        "total": paginated.total,
        "pages": paginated.pages,
        "page": page
    })

@statement_bp.route('/statement/b2b')
@login_required
def b2b():
    return get_paginated_transactions('B2B')

@statement_bp.route('/statement/b2c')
@login_required
def b2c():
    return get_paginated_transactions('B2C')

@statement_bp.route('/statement/cdnr')
@login_required
def cdnr():
    return get_paginated_transactions('CDNR')

@statement_bp.route('/statement/cdnur')
@login_required
def cdnur():
    return get_paginated_transactions('CDNUR')

@statement_bp.route('/statement/nil')
@login_required
def nil():
    return get_paginated_transactions('NIL')

@statement_bp.route('/statement/hsn-b2b')
@login_required
def hsn_b2b():
    return get_paginated_transactions('HSN_B2B')

@statement_bp.route('/statement/hsn-b2c')
@login_required
def hsn_b2c():
    return get_paginated_transactions('HSN_B2C')

@statement_bp.route('/statement/ecom')
@login_required
def ecom():
    return get_paginated_transactions('ECOM')

@statement_bp.route('/statement/edit/<int:id>', methods=['POST'])
@login_required
def edit_transaction(id):
    profile_id = get_active_profile_id()
    t = Transaction.query.filter_by(id=id, profile_id=profile_id).first_or_404()
    
    try:
        old_val = t.taxable_value
        req_data = request.get_json(silent=True) or request.form
        if 'taxable_value' in req_data:
            from decimal import Decimal
            from app.utils.tax_calculator import calculate_cgst, calculate_sgst, calculate_igst, calculate_cess, calculate_total_tax, calculate_invoice_value
            t.taxable_value = Decimal(str(req_data.get('taxable_value')))
            rate = Decimal(str(t.tax_rate or 0))
            if t.igst_rate and Decimal(str(t.igst_rate)) > 0:
                t.igst_amount = calculate_igst(t.taxable_value, rate)
                t.cgst_amount = Decimal('0.00')
                t.sgst_amount = Decimal('0.00')
            elif (t.cgst_rate and Decimal(str(t.cgst_rate)) > 0) or (t.sgst_rate and Decimal(str(t.sgst_rate)) > 0):
                t.cgst_amount = calculate_cgst(t.taxable_value, rate)
                t.sgst_amount = calculate_sgst(t.taxable_value, rate)
                t.igst_amount = Decimal('0.00')
            if t.cess_rate and Decimal(str(t.cess_rate)) > 0:
                t.cess_amount = calculate_cess(t.taxable_value, t.cess_rate)
            t.total_tax = calculate_total_tax(t.cgst_amount, t.sgst_amount, t.igst_amount, t.cess_amount)
            if 'invoice_value' in req_data:
                t.invoice_value = Decimal(str(req_data.get('invoice_value')))
            else:
                t.invoice_value = calculate_invoice_value(t.taxable_value, t.total_tax)
        
        audit = AuditLog(
            user_id=current_user.id,
            action="UPDATE",
            entity_type="Transaction",
            entity_id=t.id,
            field_name="taxable_value",
            old_value=str(old_val),
            new_value=str(t.taxable_value),
            reason=req_data.get('reason', 'User modified transaction')
        )
        db.session.add(audit)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": True, "message": str(e)}), 400

@statement_bp.route('/statement/delete/<int:id>', methods=['POST'])
@login_required
def delete_transaction(id):
    profile_id = get_active_profile_id()
    t = Transaction.query.filter_by(id=id, profile_id=profile_id).first_or_404()
    
    try:
        t.is_deleted = True
        req_data = request.get_json(silent=True) or request.form
        reason = req_data.get('reason', 'User deleted transaction')
        
        audit = AuditLog(
            user_id=current_user.id,
            action="DELETE",
            entity_type="Transaction",
            entity_id=t.id,
            reason=reason
        )
        db.session.add(audit)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": True, "message": str(e)}), 400

@statement_bp.route('/statement/validation-errors')
@login_required
def validation_errors():
    return render_template('errors.html')

@statement_bp.route('/statement/export/<section>')
@login_required
def export_section(section):
    profile_id = get_active_profile_id()
    if not profile_id:
        return Response('', mimetype="text/csv", headers={"Content-disposition": f"attachment; filename={section}.csv"})
    sec = section.upper()
    
    query = Transaction.query.filter_by(profile_id=profile_id, is_deleted=False)
    if sec == 'B2B':
        query = query.filter(Transaction.supply_type == 'B2B')
    elif sec in ('B2C', 'B2CS'):
        query = query.filter(Transaction.supply_type.in_(['B2CS', 'B2CL']))
    elif sec == 'CDNR':
        query = query.filter(Transaction.supply_type.in_(['CDNR', 'CDNUR']))
        
    txs = query.all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Invoice Number', 'Date', 'Recipient GSTIN', 'Taxable Value', 'Total Tax', 'Invoice Value'])
    for t in txs:
        writer.writerow([
            t.id,
            sanitize_csv_value(t.invoice_number),
            t.invoice_date.strftime('%d-%m-%Y') if t.invoice_date else '',
            sanitize_csv_value(t.customer_gstin or ''),
            float(t.taxable_value or 0),
            float(t.total_tax or 0),
            float(t.invoice_value or 0)
        ])
        
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={section}.csv"}
    )
