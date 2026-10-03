import os
import uuid
import json
import hashlib
from decimal import Decimal
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import render_template, redirect, url_for, flash, request, session, current_app, send_file, jsonify
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
    platforms = list_platforms()
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
            )

            if res.status == 'REJECTED_UNREADABLE':
                for err in res.errors:
                    flash(f'Import rejected: {err}', 'danger')
                return redirect(url_for('imports.history'))

            if res.status == 'REJECTED_INVALID':
                adapter = get_adapter(platform_name) or BaseGenericAdapter()
                plat_name = getattr(adapter, 'PLATFORM_NAME', platform_name) or platform_name
                for message in res.validation_errors[:3]:
                    flash(f'Import rejected ({plat_name}): {message}', 'danger')
                return redirect(url_for('imports.history'))

            if res.status == 'REJECTED_DUPLICATE_FILE':
                msg = res.errors[0] if res.errors else 'The exact same file has already been imported for this profile'
                flash(f'Import rejected: {msg}', 'danger')
                return redirect(url_for('imports.history'))

            if res.status == 'FAILED':
                flash('An error occurred while processing the uploaded file. Please verify the file and try again.', 'danger')
                return redirect(url_for('imports.import_page'))

            summary = f'Processed {res.total_rows} rows: {res.success_rows} imported'
            if res.warning_rows:
                summary += f', {res.warning_rows} with warnings'
            if res.error_rows:
                summary += f', {res.error_rows} rejected'
            if res.skipped_rows:
                summary += f', {res.skipped_rows} duplicates skipped'
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
            flash('An error occurred while processing the uploaded file. Please verify the file and try again.', 'danger')
            return redirect(url_for('imports.import_page'))
            
    flash('Invalid file format. Please upload an Excel (.xlsx/.xls) or CSV file.', 'danger')
    return redirect(url_for('imports.import_page'))

@import_bp.route('/import/preview', methods=['POST', 'GET'])
@login_required
def preview():
    return redirect(url_for('imports.history'))

@import_bp.route('/import/confirm', methods=['POST'])
@login_required
def confirm():
    return redirect(url_for('imports.history'))

@import_bp.route('/import/history')
@login_required
def history():
    profile = get_active_profile()
    if not profile:
        flash('Please select an active profile first', 'warning')
        return redirect(url_for('profile.list_profiles'))
        
    history_list = ImportHistory.query.filter_by(profile_id=profile.id).order_by(ImportHistory.created_at.desc()).all()
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
    import_rec = ImportHistory.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    return render_template('import_history.html', history=[import_rec], reports=_import_reports([import_rec]))

@import_bp.route('/import/<int:id>/reprocess', methods=['POST'])
@login_required
def reprocess(id):
    profile = get_active_profile()
    if not profile:
        return redirect(url_for('profile.list_profiles'))
    import_rec = ImportHistory.query.filter_by(id=id, profile_id=profile.id).first_or_404()

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

@import_bp.route('/import/<int:id>/download-original')
@login_required
def download_original(id):
    profile = get_active_profile()
    if not profile:
        return redirect(url_for('profile.list_profiles'))
    import_rec = ImportHistory.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    if import_rec.raw_file_path and os.path.exists(import_rec.raw_file_path):
        return send_file(import_rec.raw_file_path, as_attachment=True, download_name=import_rec.file_name)
    flash('Original file not found on disk', 'warning')
    return redirect(url_for('imports.history'))
