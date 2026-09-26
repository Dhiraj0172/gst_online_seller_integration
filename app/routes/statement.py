import csv
import io
from flask import current_app, jsonify, render_template, request, Response, session
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models import AuditLog, GSTProfile, ImportHistory, Transaction
from app.utils.csv_utils import sanitize_csv_value
from . import statement_bp


def get_active_profile_id():
    """Retrieve active profile ID ensuring strict multi-tenant authorization."""
    requested_id = request.args.get('profile_id')
    if requested_id is not None:
        try:
            p = GSTProfile.query.filter_by(id=int(requested_id), user_id=current_user.id).first()
            if p:
                session['active_profile_id'] = p.id
                return p.id
            # If requested profile does not belong to current user, clear session active profile
            session.pop('active_profile_id', None)
        except (ValueError, TypeError):
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
    """Retrieve active return period with query param priority and session persistence.

    Priority:
    1. Query param 'return_period' or 'period'. If explicitly empty (''), returns None.
    2. Session 'return_period'.
    3. Profile's latest ImportHistory return_period if profile_id provided.
    4. Default '012025'.
    """
    # 1. Query parameter priority
    if 'return_period' in request.args:
        val = request.args.get('return_period', '').strip()
        if val:
            session['return_period'] = val
            return val
        return None  # Explicitly empty return period requested

    if 'period' in request.args:
        val = request.args.get('period', '').strip()
        if val:
            session['return_period'] = val
            return val
        return None  # Explicitly empty return period requested

    # 2. Session state
    sess_period = session.get('return_period')
    if sess_period and str(sess_period).strip():
        return str(sess_period).strip()

    # 3. Profile's latest import period if available
    if profile_id:
        latest_imp = (
            ImportHistory.query.filter_by(profile_id=profile_id)
            .order_by(ImportHistory.created_at.desc())
            .first()
        )
        if latest_imp and latest_imp.return_period:
            return latest_imp.return_period

    # 4. Standard default
    return '012025'


@statement_bp.route('/statement')
@login_required
def index():
    profile_id = get_active_profile_id()
    active_profile = GSTProfile.query.filter_by(id=profile_id, user_id=current_user.id).first() if profile_id else None
    return_period = get_active_return_period(profile_id)

    available_periods = []
    if profile_id:
        rows = (
            db.session.query(ImportHistory.return_period)
            .filter_by(profile_id=profile_id)
            .distinct()
            .order_by(ImportHistory.return_period.desc())
            .all()
        )
        available_periods = [r[0] for r in rows if r[0]]

    if return_period and return_period not in available_periods:
        available_periods.insert(0, return_period)

    if not available_periods:
        available_periods = ['012025', '022025', '032025', '042025', '052025']

    return render_template(
        'statement.html',
        active_profile=active_profile,
        active_return_period=return_period,
        available_periods=available_periods,
    )


def get_paginated_transactions(tx_type):
    profile_id = get_active_profile_id()
    return_period = get_active_return_period(profile_id)
    if not profile_id or not return_period:
        return jsonify({
            "data": [],
            "total": 0,
            "pages": 0,
            "page": 1,
            "return_period": return_period,
            "summary": {
                "count": 0,
                "total_taxable": 0.0,
                "total_tax": 0.0,
                "total_invoice_value": 0.0,
            },
        })

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
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
        query = query.filter(Transaction.supply_type.in_(['CDNR', 'CDNRA']))
    elif tx_type == 'CDNUR':
        query = query.filter(Transaction.supply_type.in_(['CDNUR', 'CDNURA']))
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
            "total_tax": float(t.total_tax) if t.total_tax is not None else float((t.cgst_amount or 0) + (t.sgst_amount or 0) + (t.igst_amount or 0) + (t.cess_amount or 0)),
            "invoice_value": float(t.invoice_value) if t.invoice_value else 0.0,
            "tax_rate": float(t.tax_rate) if t.tax_rate else 0.0,
            "hsn_sac": t.hsn_sac or '',
            "supply_type": t.supply_type or '',
            "status": "Valid" if not getattr(t, 'validation_errors', None) else "Warning",
        } for t in paginated.items],
        "total": paginated.total,
        "pages": paginated.pages,
        "page": page,
        "return_period": return_period,
        "summary": summary,
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
    if not profile_id:
        return jsonify({"error": True, "message": "Profile required"}), 404
    t = Transaction.query.filter_by(id=id, profile_id=profile_id, is_deleted=False).first_or_404()

    try:
        old_val = t.taxable_value
        req_data = request.get_json(silent=True) or request.form
        if 'taxable_value' in req_data:
            from decimal import Decimal
            from app.utils.tax_calculator import (
                calculate_cess,
                calculate_cgst,
                calculate_igst,
                calculate_invoice_value,
                calculate_sgst,
                calculate_total_tax,
            )
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
            reason=req_data.get('reason', 'User modified transaction'),
        )
        db.session.add(audit)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error editing transaction {id}: {e}")
        return jsonify({"error": True, "message": "An error occurred while updating the transaction."}), 400


@statement_bp.route('/statement/delete/<int:id>', methods=['POST'])
@login_required
def delete_transaction(id):
    profile_id = get_active_profile_id()
    if not profile_id:
        return jsonify({"error": True, "message": "Profile required"}), 404
    t = Transaction.query.filter_by(id=id, profile_id=profile_id, is_deleted=False).first_or_404()

    try:
        t.is_deleted = True
        req_data = request.get_json(silent=True) or request.form
        reason = req_data.get('reason', 'User deleted transaction')

        audit = AuditLog(
            user_id=current_user.id,
            action="DELETE",
            entity_type="Transaction",
            entity_id=t.id,
            reason=reason,
        )
        db.session.add(audit)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error deleting transaction {id}: {e}")
        return jsonify({"error": True, "message": "An error occurred while deleting the transaction."}), 400


@statement_bp.route('/statement/transaction/<int:id>', methods=['GET'])
@login_required
def get_transaction(id):
    profile_id = get_active_profile_id()
    if not profile_id:
        return jsonify({"error": True, "message": "Profile required"}), 404
    t = Transaction.query.filter_by(id=id, profile_id=profile_id, is_deleted=False).first_or_404()
    return jsonify({
        "id": t.id,
        "invoice_number": t.invoice_number,
        "invoice_date": t.invoice_date.strftime('%Y-%m-%d') if t.invoice_date else '',
        "customer_gstin": t.customer_gstin or '',
        "customer_name": t.customer_name or '',
        "place_of_supply": t.place_of_supply or '',
        "taxable_value": float(t.taxable_value) if t.taxable_value else 0.0,
        "tax_rate": float(t.tax_rate) if t.tax_rate else 0.0,
        "cgst_amount": float(t.cgst_amount) if t.cgst_amount else 0.0,
        "sgst_amount": float(t.sgst_amount) if t.sgst_amount else 0.0,
        "igst_amount": float(t.igst_amount) if t.igst_amount else 0.0,
        "cess_amount": float(t.cess_amount) if t.cess_amount else 0.0,
        "total_tax": float(t.total_tax) if t.total_tax is not None else float((t.cgst_amount or 0) + (t.sgst_amount or 0) + (t.igst_amount or 0) + (t.cess_amount or 0)),
        "invoice_value": float(t.invoice_value) if t.invoice_value else 0.0,
        "supply_type": t.supply_type or '',
        "hsn_sac": t.hsn_sac or '',
    })


@statement_bp.route('/statement/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_transactions():
    profile_id = get_active_profile_id()
    if not profile_id:
        return jsonify({"error": True, "message": "Profile required"}), 404
    req_data = request.get_json(silent=True) or request.form
    ids = req_data.get('ids', [])
    reason = req_data.get('reason', 'User bulk-deleted transactions')
    if not ids or not isinstance(ids, list):
        return jsonify({"error": True, "message": "No transaction IDs provided"}), 400

    try:
        txs = Transaction.query.filter(
            Transaction.id.in_(ids),
            Transaction.profile_id == profile_id,
            Transaction.is_deleted == False
        ).all()
        for t in txs:
            t.is_deleted = True
            audit = AuditLog(
                user_id=current_user.id,
                action="DELETE",
                entity_type="Transaction",
                entity_id=t.id,
                reason=reason,
            )
            db.session.add(audit)
        db.session.commit()
        return jsonify({"success": True, "deleted_count": len(txs)})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error bulk deleting transactions: {e}")
        return jsonify({"error": True, "message": "An error occurred while deleting transactions."}), 400


@statement_bp.route('/statement/validation-errors')
@login_required
def validation_errors():
    return render_template('errors.html')


@statement_bp.route('/statement/export/<section>')
@login_required
def export_section(section):
    profile_id = get_active_profile_id()
    return_period = get_active_return_period(profile_id)
    if not profile_id or not return_period:
        return Response('', mimetype="text/csv", headers={"Content-disposition": f"attachment; filename={section}.csv"})

    sec = section.upper()
    sec_norm = sec.replace('-', '_')

    query = (
        db.session.query(Transaction)
        .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
        .filter(
            Transaction.profile_id == profile_id,
            Transaction.is_deleted == False,
            ImportHistory.return_period == return_period,
        )
    )
    if sec == 'B2B':
        query = query.filter(Transaction.supply_type.in_(['B2B', 'B2BA']))
    elif sec in ('B2C', 'B2CS'):
        query = query.filter(Transaction.supply_type.in_(['B2CS', 'B2CSA', 'B2CL', 'B2CLA']))
    elif sec in ('CDNR', 'CDNRA'):
        query = query.filter(Transaction.supply_type.in_(['CDNR', 'CDNRA']))
    elif sec in ('CDNUR', 'CDNURA'):
        query = query.filter(Transaction.supply_type.in_(['CDNUR', 'CDNURA']))
    elif sec in ('NIL', 'EXEMPT'):
        query = query.filter(Transaction.supply_type.in_(['NIL', 'EXEMPT', 'NONGST']))
    elif sec_norm in ('HSN_B2C', 'HSNB2C'):
        query = query.filter(
            Transaction.hsn_sac.isnot(None),
            Transaction.hsn_sac != '',
            func.trim(Transaction.hsn_sac) != '',
            Transaction.supply_type.in_(['B2CS', 'B2CSA', 'B2CL', 'B2CLA', 'HSNB2C']),
        )
    elif sec_norm in ('HSN_B2B', 'HSN') or sec.startswith('HSN'):
        query = query.filter(
            Transaction.hsn_sac.isnot(None),
            Transaction.hsn_sac != '',
            func.trim(Transaction.hsn_sac) != '',
            Transaction.supply_type.in_(['B2B', 'B2BA', 'HSN']),
        )
    elif sec == 'ECOM':
        query = query.filter(Transaction.ecommerce_gstin.isnot(None), Transaction.ecommerce_gstin != '')

    search = request.args.get('search', '').strip()
    if search:
        query = query.filter(
            (Transaction.invoice_number.ilike(f'%{search}%')) |
            (Transaction.customer_gstin.ilike(f'%{search}%')) |
            (Transaction.customer_name.ilike(f'%{search}%')) |
            (Transaction.place_of_supply.ilike(f'%{search}%')) |
            (Transaction.hsn_sac.ilike(f'%{search}%'))
        )

    txs = query.order_by(Transaction.id.asc()).all()

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
            float(t.invoice_value or 0),
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={section}.csv"},
    )
