from flask import render_template, request, session, flash, redirect, url_for, jsonify, Response, abort, current_app
from flask_login import login_required, current_user
from app.models import TCSReconciliation, GSTProfile, AuditLog
from app.extensions import db
from app.services.tcs_service import import_tcs_report, reconcile_tcs, apply_adjustment, export_reconciliation
from app.utils.date_utils import validate_return_period
from . import tcs_bp
import os
import uuid
import tempfile
from werkzeug.utils import secure_filename

MAX_TCS_FILE_SIZE = 16 * 1024 * 1024  # 16MB limit


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


@tcs_bp.route('/tcs')
@login_required
def index():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('profile.list_profiles'))

    # Get existing reconciliation data for display
    reconciliations = []
    if profile:
        from app.services.tcs_service import reconcile_tcs
        # Just fetch stored data for display
        reconciliations = TCSReconciliation.query.filter_by(
            profile_id=profile.id
        ).order_by(TCSReconciliation.return_period.desc(), TCSReconciliation.state_code).all()

    return render_template('tcs_reconcile.html', reconciliations=reconciliations, profile=profile)


@tcs_bp.route('/tcs/upload', methods=['POST'])
@login_required
def upload():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('tcs.index'))

    return_period = request.form.get('return_period', '').strip()
    if not return_period:
        flash('Return period required', 'danger')
        return redirect(url_for('tcs.index'))

    is_valid_rp, rp_msg = validate_return_period(return_period)
    if not is_valid_rp:
        flash(f'Invalid return period: {rp_msg}', 'danger')
        return redirect(url_for('tcs.index'))

    if 'file' not in request.files:
        flash('No file selected', 'danger')
        return redirect(url_for('tcs.index'))

    file = request.files['file']
    if not file or not file.filename or file.filename.strip() == '':
        flash('No file selected', 'danger')
        return redirect(url_for('tcs.index'))

    filename = secure_filename(file.filename)
    if not filename.lower().endswith('.xlsx'):
        flash('Invalid file format. Only Excel (.xlsx) files are supported for TCS reports.', 'danger')
        return redirect(url_for('tcs.index'))

    # Check request content length if provided
    if request.content_length and request.content_length > MAX_TCS_FILE_SIZE:
        flash('File size exceeds the 16MB limit.', 'danger')
        return redirect(url_for('tcs.index'))

    # Check magic bytes for ZIP/XLSX (PK\x03\x04)
    header = file.read(4)
    file.seek(0)
    if header != b'PK\x03\x04':
        flash('Invalid file content. The file is not a valid Excel (.xlsx) spreadsheet.', 'danger')
        return redirect(url_for('tcs.index'))

    # Safe temp file in system tempdir to isolate tenants and prevent traversal
    temp_dir = tempfile.gettempdir()
    safe_filename = f"tcs_upload_{profile.id}_{return_period}_{uuid.uuid4().hex}.xlsx"
    file_path = os.path.join(temp_dir, safe_filename)

    try:
        bytes_written = 0
        with open(file_path, 'wb') as f_out:
            while chunk := file.read(64 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_TCS_FILE_SIZE:
                    flash('File size exceeds the 16MB limit.', 'danger')
                    return redirect(url_for('tcs.index'))
                f_out.write(chunk)

        # Import the report
        result = import_tcs_report(file_path, profile.id, return_period, current_user.id)

        if result.errors:
            for err in result.errors:
                flash(f'Import error: {err}', 'danger')
        else:
            flash(f'TCS report imported successfully. {result.success_rows} rows parsed.', 'success')
            if result.warnings:
                for warn in result.warnings:
                    flash(warn, 'warning')
    except Exception as exc:
        current_app.logger.exception(f"Error during TCS upload processing: {exc}")
        flash('An error occurred while processing the TCS upload. Please verify the file and try again.', 'danger')
    finally:
        # Guaranteed cleanup on EVERY exit path to prevent stale uploads/artifacts
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

    return redirect(url_for('tcs.index'))


@tcs_bp.route('/tcs/reconcile', methods=['POST'])
@login_required
def reconcile():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('tcs.index'))

    return_period = request.form.get('return_period', '').strip()
    if not return_period:
        flash('Return period required', 'danger')
        return redirect(url_for('tcs.index'))

    is_valid_rp, rp_msg = validate_return_period(return_period)
    if not is_valid_rp:
        flash(f'Invalid return period: {rp_msg}', 'danger')
        return redirect(url_for('tcs.index'))

    include_undated_val = request.form.get('include_undated')
    if include_undated_val is not None:
        include_undated = include_undated_val.lower() in ('true', '1', 'yes', 'on')
    else:
        include_undated = True

    try:
        # Run reconciliation
        result = reconcile_tcs(profile.id, return_period, current_user.id, include_undated=include_undated)

        if result['status'] == 'NO_INTERNAL_DATA':
            flash('No e-commerce transactions found for this period. Import marketplace data first.', 'warning')
        elif result['status'] == 'NO_PORTAL_DATA':
            flash('No portal TCS report imported. Upload a TCS report first.', 'warning')
        else:
            flash(f'Reconciliation completed: {result["matched"]} matched, {result["mismatched"]} mismatched, {result["ambiguous"]} ambiguous, {result["missing_in_source"]} missing in source, {result["missing_in_portal"]} missing in portal.', 'success')
            for warning in result.get('warnings', []):
                flash(warning, 'warning')
    except Exception as exc:
        current_app.logger.exception(f"Error during TCS reconciliation: {exc}")
        flash('An error occurred during reconciliation. Please verify your data and try again.', 'danger')

    return redirect(url_for('tcs.index'))


@tcs_bp.route('/tcs/adjust/<int:id>', methods=['POST'])
@login_required
def adjust(id):
    profile = get_active_profile()
    if not profile:
        abort(404)

    recon = TCSReconciliation.query.filter_by(id=id, profile_id=profile.id).first_or_404()

    adjustment_type = request.form.get('adjustment_type')
    notes = request.form.get('notes', '')

    if adjustment_type not in ['ACCEPT_PORTAL', 'ACCEPT_INTERNAL', 'MANUAL_OVERRIDE']:
        flash('Invalid adjustment type', 'danger')
        return redirect(url_for('tcs.index'))

    success = apply_adjustment(recon.id, adjustment_type, notes, current_user.id, profile_id=profile.id)

    if success:
        flash('Adjustment applied successfully', 'success')
    else:
        flash('Adjustment failed', 'danger')

    return redirect(url_for('tcs.index'))


@tcs_bp.route('/tcs/export')
@login_required
def export():
    profile = get_active_profile()
    if not profile:
        flash('Active profile required', 'danger')
        return redirect(url_for('tcs.index'))

    return_period = request.args.get('return_period')
    if not return_period:
        flash('Return period required', 'danger')
        return redirect(url_for('tcs.index'))

    csv_data = export_reconciliation(profile.id, return_period)

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename=tcs_reconciliation_{return_period}.csv"}
    )


@tcs_bp.route('/tcs/api/data')
@login_required
def api_data():
    """API endpoint for AJAX data table."""
    profile = get_active_profile()
    if not profile:
        return jsonify({'error': 'No active profile'}), 400

    return_period = request.args.get('return_period')
    if not return_period:
        return jsonify({'error': 'Return period required'}), 400

    reconciliations = TCSReconciliation.query.filter_by(
        profile_id=profile.id,
        return_period=return_period
    ).order_by(TCSReconciliation.state_code).all()

    data = []
    for r in reconciliations:
        data.append({
            'id': r.id,
            'state_code': r.state_code,
            'state_name': r.state_name,
            'ecommerce_gstin': r.ecommerce_gstin,
            'our_net_taxable': float(r.our_net_taxable_value),
            'portal_taxable': float(r.portal_taxable_value),
            'diff_taxable': float(r.difference_taxable),
            'our_tcs': float(r.our_calculated_tcs),
            'portal_tcs': float(r.portal_tcs),
            'diff_tcs': float(r.difference_tcs),
            'our_cgst': float(r.our_calculated_cgst),
            'portal_cgst': float(r.portal_cgst_tcs or 0),
            'diff_cgst': float(r.difference_cgst),
            'our_sgst': float(r.our_calculated_sgst),
            'portal_sgst': float(r.portal_sgst_tcs or 0),
            'diff_sgst': float(r.difference_sgst),
            'our_igst': float(r.our_calculated_igst),
            'portal_igst': float(r.portal_igst_tcs or 0),
            'diff_igst': float(r.difference_igst),
            'match_status': r.match_status,
            'ambiguity_details': r.ambiguity_details,
            'is_adjusted': r.is_adjusted,
            'adjustment_notes': r.adjustment_notes,
        })

    return jsonify({'data': data})