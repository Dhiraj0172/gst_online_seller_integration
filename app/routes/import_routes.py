import os
import uuid
import json
import hashlib
from decimal import Decimal
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import render_template, redirect, url_for, flash, request, session, current_app, send_file, jsonify, abort
from flask_login import login_required, current_user
import openpyxl

from app.models import ImportHistory, GSTProfile, RawImport, Transaction
from app.adapters.registry import get_adapter, detect_platform, list_platforms
from app.adapters.all_adapters import BaseGenericAdapter
from app.services.classification_service import classify_transaction
from app.services.import_service import process_import
from app.services.duplicate_service import (
    DUPLICATE_EXISTING, DUPLICATE_IN_FILE, build_fingerprint, duplicate_envelope,
    duplicate_report_entry, file_duplicate_report, find_existing_transactions,
    find_prior_file_import,
)
from app.utils.csv_utils import read_csv_rows
from app.utils.date_utils import parse_date
from app.utils.state_codes import resolve_pos_code
from app.utils.query import tenant_query
from app.extensions import db
from . import import_bp

ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls'}


def _first_parsed_sheet(parse_result) -> str:
    sheets = parse_result.metadata.get('sheets_parsed') or []
    return sheets[0] if sheets else 'Sheet1'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

@import_bp.route('/import')
@login_required
def import_page():
    profile = get_active_profile()
    if not profile:
        flash('Please create a GST profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))
    from app.adapters.all_adapters import get_adapter_readiness_matrix
    platforms = get_adapter_readiness_matrix(include_internal=False)
    for p in platforms:
        if 'supported_files' not in p:
            p['supported_files'] = p.get('supported_file_types', ['.xlsx', '.csv'])
    return render_template('import.html', platforms=platforms)

@import_bp.route('/import/upload', methods=['POST'])
@login_required
def upload():
    profile = get_active_profile()
    if not profile:
        flash('Please create or select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))
        
    if 'file' not in request.files:
        flash('No file part provided', 'danger')
        return redirect(url_for('imports.import_page'))
        
    file = request.files['file']
    if file.filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('imports.import_page'))
        
    if file and allowed_file(file.filename):
        wb = None
        import_rec_id = None
        try:
            filename = secure_filename(file.filename)
            unique_filename = f"{uuid.uuid4()}_{filename}"
            
            upload_dir = current_app.config.get('UPLOAD_FOLDER', os.path.join(current_app.root_path, '..', 'uploads'))
            os.makedirs(upload_dir, exist_ok=True)
            filepath = os.path.join(upload_dir, unique_filename)
            
            file.save(filepath)
            
            # Compute SHA256 hash
            sha256_hash = hashlib.sha256()
            with open(filepath, "rb") as f:
                for byte_block in iter(lambda: f.read(65536), b""):
                    sha256_hash.update(byte_block)
            file_hash = sha256_hash.hexdigest()
            file_size = os.path.getsize(filepath)
            
            platform_name = request.form.get('platform') or 'Generic'
            return_period = session.get('return_period', '012025')
            
            # Create ImportHistory record
            import_rec = ImportHistory(
                user_id=current_user.id,
                profile_id=profile.id,
                file_name=filename,
                original_file_name=filename,
                file_hash=file_hash,
                file_size=file_size,
                platform_name=platform_name,
                return_period=return_period,
                financial_year=profile.financial_year or '2024-25',
                processing_status='PROCESSING',
                raw_file_path=filepath,
                processing_started_at=datetime.utcnow()
            )
            db.session.add(import_rec)
            db.session.commit()
            import_rec_id = import_rec.id
            
            # Delegate import orchestration to common import engine
            allow_duplicate_file = str(request.form.get('allow_duplicate_file', '')).lower() in (
                '1', 'true', 'yes', 'on'
            )
            stage_mode = str(request.form.get('stage', '')).lower() in (
                '1', 'true', 'yes', 'on'
            ) or request.form.get('action') == 'preview' or 'preview' in request.form

            res = process_import(
                file_path=filepath,
                profile_id=profile.id,
                platform_name=platform_name,
                user_id=current_user.id,
                return_period=return_period,
                financial_year=profile.financial_year or '2024-25',
                allow_duplicate_file=allow_duplicate_file,
                classification_fn=classify_transaction,
                import_history_id=import_rec.id,
                stage=stage_mode,
            )

            is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json or request.args.get('format') == 'json'

            if res.status == 'REJECTED_UNREADABLE':
                if is_ajax:
                    return jsonify({'success': False, 'status': res.status, 'errors': res.errors, 'message': f'Import rejected: {res.errors[0] if res.errors else "File unreadable"}'}), 400
                for err in res.errors:
                    flash(f'Import rejected: {err}', 'danger')
                return redirect(url_for('imports.history'))

            if res.status == 'REJECTED_INVALID':
                adapter = get_adapter(platform_name) or BaseGenericAdapter()
                plat_name = getattr(adapter, 'PLATFORM_NAME', platform_name) or platform_name
                if is_ajax:
                    return jsonify({'success': False, 'status': res.status, 'errors': res.validation_errors, 'message': f'Import rejected ({plat_name})'}), 400
                for message in res.validation_errors[:3]:
                    flash(f'Import rejected ({plat_name}): {message}', 'danger')
                return redirect(url_for('imports.history'))

            if res.status == 'REJECTED_DUPLICATE_FILE':
                msg = res.errors[0] if res.errors else 'The exact same file has already been imported for this profile'
                if is_ajax:
                    return jsonify({'success': False, 'status': res.status, 'errors': res.errors, 'message': msg}), 400
                flash(f'Import rejected: {msg}', 'danger')
                return redirect(url_for('imports.history'))

            if res.status == 'FAILED':
                if is_ajax:
                    return jsonify({'success': False, 'status': res.status, 'message': 'An error occurred while processing the uploaded file.'}), 500
                flash('An error occurred while processing the uploaded file. Please verify the file and try again.', 'danger')
                return redirect(url_for('imports.import_page'))

            if res.status == 'STAGED':
                msg = f'File "{filename}" staged for preview. Review candidate transactions and confirm when ready.'
                if is_ajax:
                    return jsonify({
                        'success': True,
                        'import_id': res.import_history_id or import_rec.id,
                        'status': 'STAGED',
                        'message': msg,
                        'redirect_url': url_for('imports.preview_view', id=res.import_history_id or import_rec.id)
                    })
                flash(msg, 'info')
                return redirect(url_for('imports.preview_view', id=res.import_history_id))

            summary = f'Processed {res.total_rows} rows: {res.success_rows} imported'
            if res.warning_rows:
                summary += f', {res.warning_rows} with warnings'
            if res.error_rows:
                summary += f', {res.error_rows} rejected'
            if res.skipped_rows:
                summary += f', {res.skipped_rows} duplicates skipped'
            if is_ajax:
                return jsonify({
                    'success': True,
                    'import_id': res.import_history_id or import_rec.id,
                    'status': res.status,
                    'message': f'File "{filename}" imported: {summary}.',
                    'redirect_url': url_for('imports.history')
                })
            flash(f'File "{filename}" imported: {summary}.', 'success')
            return redirect(url_for('imports.history'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(f"Error processing import upload: {e}")
            if wb:
                try:
                    wb.close()
                except Exception:
                    pass
            if import_rec_id is not None:
                try:
                    failed_rec = db.session.get(ImportHistory, import_rec_id)
                    if failed_rec:
                        failed_rec.processing_status = 'FAILED'
                        failed_rec.error_summary = json.dumps({
                            'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                                       'error_rows': 1, 'skipped_rows': 0},
                            'errors': [{'code': 'UNEXPECTED_ERROR', 'message': str(e)}],
                        })
                        failed_rec.processing_completed_at = datetime.utcnow()
                        db.session.commit()
                except Exception as commit_exc:
                    db.session.rollback()
                    current_app.logger.error(
                        f"Failed to update import record {import_rec_id} to FAILED: {commit_exc}"
                    )
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
                return jsonify({'success': False, 'status': 'FAILED', 'message': str(e)}), 500
            flash('An error occurred while processing the uploaded file. Please verify the file and try again.', 'danger')
            return redirect(url_for('imports.import_page'))
            
    flash('Invalid file format. Please upload an Excel (.xlsx/.xls) or CSV file.', 'danger')
    return redirect(url_for('imports.import_page'))

@import_bp.route('/import/<int:id>/preview', methods=['GET'])
@import_bp.route('/import/preview', methods=['GET', 'POST'])
@login_required
def preview_view(id=None):
    profile = get_active_profile()
    if not profile:
        flash('Please select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))

    target_id = id or request.args.get('id', type=int) or request.form.get('id', type=int)
    if not target_id:
        return redirect(url_for('imports.history'))

    from app.services.import_service import preview_import
    preview_data = preview_import(target_id, profile.id)
    if not preview_data:
        abort(404)

    if request.is_json or request.args.get('format') == 'json':
        return jsonify(preview_data)

    return render_template('import_preview.html', preview=preview_data)


@import_bp.route('/import/<int:id>/confirm', methods=['POST'])
@import_bp.route('/import/confirm', methods=['POST'])
@login_required
def confirm_view(id=None):
    profile = get_active_profile()
    if not profile:
        if request.is_json:
            return jsonify({"error": True, "message": "No active profile"}), 404
        flash('Please select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))

    target_id = id or request.form.get('id', type=int)
    if not target_id:
        if request.is_json:
            return jsonify({"error": True, "message": "Import ID required"}), 400
        flash('Import ID required to confirm', 'danger')
        return redirect(url_for('imports.history'))

    from app.services.import_service import confirm_import
    try:
        res = confirm_import(target_id, profile.id, current_user.id)
        if request.is_json:
            return jsonify(res)
        flash(res['message'], 'success')
        return redirect(url_for('imports.history'))
    except ValueError as e:
        if request.is_json:
            status_code = 403 if "frozen" in str(e).lower() else 400
            return jsonify({"error": True, "message": str(e)}), status_code
        flash(str(e), 'danger')
        return redirect(url_for('imports.preview_view', id=target_id))


@import_bp.route('/import/<int:id>/cancel', methods=['POST'])
@import_bp.route('/import/cancel', methods=['POST'])
@login_required
def cancel_view(id=None):
    profile = get_active_profile()
    if not profile:
        if request.is_json:
            return jsonify({"error": True, "message": "No active profile"}), 404
        flash('Please select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))

    target_id = id or request.form.get('id', type=int)
    if not target_id:
        if request.is_json:
            return jsonify({"error": True, "message": "Import ID required"}), 400
        flash('Import ID required to cancel', 'danger')
        return redirect(url_for('imports.history'))

    from app.services.import_service import cancel_import
    try:
        res = cancel_import(target_id, profile.id, current_user.id)
        if request.is_json:
            return jsonify(res)
        flash(res['message'], 'info')
        return redirect(url_for('imports.history'))
    except ValueError as e:
        if request.is_json:
            return jsonify({"error": True, "message": str(e)}), 400
        flash(str(e), 'danger')
        return redirect(url_for('imports.preview_view', id=target_id))


preview = preview_view
confirm = confirm_view
cancel = cancel_view


@import_bp.route('/import/history')
@login_required
def history():
    profile = get_active_profile()
    if not profile:
        flash('Please select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))
        
    history_list = tenant_query(ImportHistory, profile.id).order_by(ImportHistory.created_at.desc()).all()
    return render_template('import_history.html', history=history_list, reports=_import_reports(history_list))


def _import_reports(history_list):
    """Machine-readable import summaries, decoded for human-readable display."""
    reports = {}
    for item in history_list:
        report = {'counts': {}, 'errors': [], 'duplicates': [], 'warnings': []}
        try:
            errors = json.loads(item.error_summary) if item.error_summary else None
        except (TypeError, ValueError):
            errors = None
        try:
            warnings = json.loads(item.warning_summary) if item.warning_summary else None
        except (TypeError, ValueError):
            warnings = None

        if isinstance(errors, dict):
            report['counts'].update(errors.get('counts') or {})
            report['errors'] = errors.get('errors') or []
        elif isinstance(errors, list):
            report['errors'] = [{'code': 'IMPORT_ERROR', 'message': str(message)}
                                for message in errors]
        if isinstance(warnings, dict):
            report['duplicates'] = warnings.get('duplicates') or []
            report['warnings'] = warnings.get('warnings') or []
        elif isinstance(warnings, list):
            report['warnings'] = [{'code': 'IMPORT_WARNING', 'message': str(message)}
                                  for message in warnings]
        reports[item.id] = report
    return reports

@import_bp.route('/import/<int:id>/detail')
@login_required
def detail(id):
    profile = get_active_profile()
    if not profile:
        return redirect(url_for('profile.list_profiles'))
    import_rec = tenant_query(ImportHistory, profile.id).filter_by(id=id).first_or_404()
    return render_template('import_history.html', history=[import_rec], reports=_import_reports([import_rec]))

@import_bp.route('/import/<int:id>/reprocess', methods=['POST'])
@login_required
def reprocess(id):
    profile = get_active_profile()
    if not profile:
        return redirect(url_for('profile.list_profiles'))
    import_rec = tenant_query(ImportHistory, profile.id).filter_by(id=id).first_or_404()
    
    from app.services.import_service import reprocess_import
    from app.services.audit_service import log_import_audit

    try:
        res = reprocess_import(id, profile.id, current_user.id)
        if res.status in ('COMPLETED', 'PARTIAL'):
            status_text = 'successfully' if res.status == 'COMPLETED' else 'with partial success'
            flash(f'Import {import_rec.file_name} reprocessed {status_text}.', 'success')
            log_import_audit(current_user.id, res.import_history_id or id, action='REPROCESS', commit=True)
        else:
            err_msg = res.errors[0] if res.errors else 'Unknown error'
            flash(f'Reprocess failed: {err_msg}', 'danger')
            log_import_audit(current_user.id, res.import_history_id or id, action='REPROCESS', reason=f"FAILED: {err_msg}", commit=True)
    except Exception as e:
        flash(f'An unexpected error occurred during reprocess: {str(e)}', 'danger')
        log_import_audit(current_user.id, id, action='REPROCESS', reason=f"FAILED: {str(e)}", commit=True)

    return redirect(url_for('imports.history'))
 
@import_bp.route('/import/<int:id>/retry', methods=['POST'])
@login_required
def retry_view(id):
    profile = get_active_profile()
    if not profile:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': True, 'message': 'No active profile'}), 404
        flash('Please select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))

    from app.services.job_service import retry_import_job
    try:
        stage = str(request.form.get('stage', '')).lower() in ('1', 'true', 'yes', 'on')
        res = retry_import_job(import_id=id, profile_id=profile.id, user_id=current_user.id, stage=stage)
        msg = res.get('message', f'Import #{id} retried successfully.')
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': msg, 'data': res})
        flash(msg, 'success')
        return redirect(url_for('imports.history'))
    except Exception as exc:
        err_msg = str(exc)
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': True, 'message': err_msg}), 400
        flash(f"Retry failed: {err_msg}", 'danger')
        return redirect(url_for('imports.history'))

retry = retry_view

@import_bp.route('/import/<int:id>/download-original')
@login_required
def download_original(id):
    profile = get_active_profile()
    if not profile:
        return redirect(url_for('profile.list_profiles'))
    import_rec = tenant_query(ImportHistory, profile.id).filter_by(id=id).first_or_404()
    if import_rec.raw_file_path and os.path.exists(import_rec.raw_file_path):
        return send_file(import_rec.raw_file_path, as_attachment=True, download_name=import_rec.file_name)
    flash('Original file not found on disk', 'warning')
    return redirect(url_for('imports.history'))
