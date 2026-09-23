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
            
            # Process file using the platform adapter. Excel is handed to the
            # adapter as a loaded workbook, CSV as a path (the adapter reads it
            # with the shared CSV reader so both paths normalize identically).
            adapter = get_adapter(platform_name)
            is_csv = filename.lower().endswith('.csv')
            wb = None
            source = filepath
            if not is_csv:
                try:
                    wb = openpyxl.load_workbook(filepath, data_only=True)
                    source = wb
                except Exception as exc:
                    import_rec.processing_status = 'FAILED'
                    import_rec.error_summary = json.dumps([
                        f'Unsupported or unreadable file: {exc}'
                    ])
                    import_rec.processing_completed_at = datetime.utcnow()
                    db.session.commit()
                    flash(f'Import rejected: unsupported or unreadable file ({exc}). '
                          f'Upload an Excel (.xlsx/.xls) or CSV marketplace export.', 'danger')
                    return redirect(url_for('imports.history'))

            if not adapter:
                adapter = detect_platform(source, filename)
            if not adapter:
                adapter = BaseGenericAdapter()

            # CSV sources are read by the shared CSV reader: a read/decode
            # failure is an unreadable source, not a header mismatch.
            if filepath.lower().endswith('.csv'):
                csv_check = read_csv_rows(filepath)
                if csv_check.errors:
                    import_rec.processing_status = 'FAILED'
                    import_rec.error_summary = json.dumps({
                        'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                                   'error_rows': 0, 'skipped_rows': 0},
                        'errors': [{'code': 'UNREADABLE_SOURCE',
                                    'message': csv_check.errors[0]}],
                    })
                    import_rec.processing_completed_at = datetime.utcnow()
                    db.session.commit()
                    flash(f'Import rejected: {csv_check.errors[0]}', 'danger')
                    return redirect(url_for('imports.history'))

            # Fail explicitly on malformed / unsupported files instead of
            # importing rows that the adapter cannot understand.
            is_valid, validation_errors = adapter.validate(source)
            if not is_valid:
                import_rec.processing_status = 'FAILED'
                import_rec.error_summary = json.dumps({
                    'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                               'error_rows': 0, 'skipped_rows': 0},
                    'errors': [{'code': 'INVALID_FILE', 'message': message}
                               for message in validation_errors],
                })
                import_rec.processing_completed_at = datetime.utcnow()
                db.session.commit()
                if wb:
                    wb.close()
                for message in validation_errors[:3]:
                    flash(f'Import rejected ({adapter.PLATFORM_NAME}): {message}', 'danger')
                return redirect(url_for('imports.history'))

            # Duplicate source file: same content already imported for this profile
            allow_duplicate_file = str(request.form.get('allow_duplicate_file', '')).lower() in (
                '1', 'true', 'yes', 'on'
            )
            prior_import = find_prior_file_import(profile.id, file_hash, platform_name)
            if prior_import is not None and not allow_duplicate_file:
                report = file_duplicate_report(prior_import)
                import_rec.processing_status = 'FAILED'
                import_rec.error_summary = json.dumps({
                    'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                               'error_rows': 0, 'skipped_rows': 0},
                    'errors': [report],
                })
                import_rec.processing_completed_at = datetime.utcnow()
                db.session.commit()
                if wb:
                    wb.close()
                flash(f'Import rejected: {report["message"]}', 'danger')
                return redirect(url_for('imports.history'))

            parse_result = adapter.parse(source, filename)
            if parse_result.errors:
                import_rec.processing_status = 'FAILED'
                import_rec.error_summary = json.dumps({
                    'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                               'error_rows': 0, 'skipped_rows': 0},
                    'errors': [{'code': 'UNREADABLE_SOURCE', 'message': message}
                               for message in parse_result.errors],
                })
                import_rec.processing_completed_at = datetime.utcnow()
                db.session.commit()
                if wb:
                    wb.close()
                flash(f'Import rejected: {parse_result.errors[0]}', 'danger')
                return redirect(url_for('imports.history'))

            total_rows = 0
            success_rows = 0
            error_rows = 0
            warning_rows = 0
            skipped_rows = 0
            rejections = []
            duplicates = []
            warning_report = []

            # Existing fingerprints for this profile, so a re-export of already
            # imported rows is reported instead of double-counted.
            fingerprints = []
            for candidate in parse_result.rows:
                candidate_norm = candidate.normalized_data or {}
                fingerprints.append(build_fingerprint(candidate_norm, profile.id, platform_name))
            existing_by_fingerprint = find_existing_transactions(profile.id, fingerprints)

            seen_in_file = {}

            for row in parse_result.rows:
                total_rows += 1
                norm = row.normalized_data or {}
                fingerprint = build_fingerprint(norm, profile.id, platform_name)

                # ---- duplicate detection (never silently discarded) ----
                duplicate_entry = None
                if fingerprint in seen_in_file:
                    first_row = seen_in_file[fingerprint]
                    duplicate_entry = duplicate_report_entry(
                        row.row_number, DUPLICATE_IN_FILE,
                        f'Duplicate of row {first_row} in this file',
                        duplicate_of_row=first_row,
                    )
                elif fingerprint in existing_by_fingerprint:
                    prior_tx = existing_by_fingerprint[fingerprint]
                    duplicate_entry = duplicate_report_entry(
                        row.row_number, DUPLICATE_EXISTING,
                        f'Already imported: transaction #{prior_tx.id} '
                        f'(import #{prior_tx.import_history_id})',
                        duplicate_of_transaction_id=prior_tx.id,
                        duplicate_of_import_id=prior_tx.import_history_id,
                    )

                if duplicate_entry is not None:
                    skipped_rows += 1
                    duplicates.append(duplicate_entry)
                    raw_imp = RawImport(
                        import_history_id=import_rec.id,
                        sheet_name=getattr(row, 'sheet_name', '') or _first_parsed_sheet(parse_result),
                        row_number=row.row_number,
                        raw_data=json.dumps({k: str(v) for k, v in row.raw_data.items() if v is not None}),
                        status='SKIPPED',
                        errors=json.dumps([duplicate_entry]),
                        warnings=json.dumps(row.warnings) if row.warnings else None,
                    )
                    db.session.add(raw_imp)
                    db.session.flush()
                    continue

                seen_in_file.setdefault(fingerprint, row.row_number)

                if row.status.value == 'SUCCESS':
                    success_rows += 1
                elif row.status.value == 'WARNING':
                    warning_rows += 1
                else:
                    error_rows += 1
                    rejections.append(duplicate_report_entry(
                        row.row_number, 'ROW_REJECTED', '; '.join(row.errors),
                    ))
                for warning in row.warnings:
                    warning_report.append(duplicate_report_entry(
                        row.row_number, 'ROW_WARNING', warning,
                    ))

                # Save RawImport
                raw_imp = RawImport(
                    import_history_id=import_rec.id,
                    sheet_name=getattr(row, 'sheet_name', '') or _first_parsed_sheet(parse_result),
                    row_number=row.row_number,
                    raw_data=json.dumps({k: str(v) for k, v in row.raw_data.items() if v is not None}),
                    status=row.status.value,
                    errors=json.dumps(row.errors) if row.errors else None,
                    warnings=json.dumps(row.warnings) if row.warnings else None
                )
                db.session.add(raw_imp)
                db.session.flush()

                # A rejected (ERROR) row carries no usable data: keep the raw row
                # and the reason, and do not create a transaction from it.
                if row.status.value == 'ERROR':
                    continue

                # Save normalized Transaction if parsed successfully
                if norm and norm.get('invoice_number'):
                    inv_date_str = norm.get('invoice_date')
                    inv_date = parse_date(inv_date_str) if inv_date_str else None

                    # Classify transaction
                    raw_supply = str(norm.get('supply_type') or '').strip().upper()
                    if raw_supply in ('B2B', 'B2CS', 'B2CL', 'CDNR', 'CDNUR', 'NIL', 'EXEMPT', 'NONGST', 'EXPORT', 'SEZ'):
                        supply_type = raw_supply
                    else:
                        supply_type = classify_transaction(norm, profile, return_period)

                    total_tax = (
                        Decimal(str(norm.get('cgst_amount', 0) or 0))
                        + Decimal(str(norm.get('sgst_amount', 0) or 0))
                        + Decimal(str(norm.get('igst_amount', 0) or 0))
                        + Decimal(str(norm.get('cess_amount', 0) or 0))
                    )
                    tx = Transaction(
                        import_history_id=import_rec.id,
                        profile_id=profile.id,
                        raw_import_id=raw_imp.id,
                        source_platform=platform_name,
                        source_row_id=str(row.row_number),
                        order_id=norm.get('order_id'),
                        invoice_number=norm.get('invoice_number'),
                        invoice_date=inv_date,
                        customer_name=norm.get('customer_name'),
                        customer_gstin=norm.get('customer_gstin'),
                        place_of_supply=resolve_pos_code(norm.get('place_of_supply'), norm.get('customer_gstin'), profile.state_code),
                        seller_gstin=profile.gstin,
                        item_code=norm.get('item_code'),
                        hsn_sac=norm.get('hsn_sac'),
                        description=norm.get('description'),
                        quantity=Decimal(str(norm.get('quantity', 1) or 1)),
                        uqc=norm.get('uqc', 'NOS'),
                        taxable_value=Decimal(str(norm.get('taxable_value', 0) or 0)),
                        cgst_rate=Decimal(str(norm.get('cgst_rate', 0) or 0)),
                        cgst_amount=Decimal(str(norm.get('cgst_amount', 0) or 0)),
                        sgst_rate=Decimal(str(norm.get('sgst_rate', 0) or 0)),
                        sgst_amount=Decimal(str(norm.get('sgst_amount', 0) or 0)),
                        igst_rate=Decimal(str(norm.get('igst_rate', 0) or 0)),
                        igst_amount=Decimal(str(norm.get('igst_amount', 0) or 0)),
                        cess_rate=Decimal(str(norm.get('cess_rate', 0) or 0)),
                        cess_amount=Decimal(str(norm.get('cess_amount', 0) or 0)),
                        total_tax=total_tax,
                        invoice_value=Decimal(str(norm.get('invoice_value', 0) or 0)),
                        tax_rate=Decimal(str(norm.get('tax_rate', 0) or 0)),
                        supply_type=supply_type,
                        reverse_charge=norm.get('reverse_charge', 'N'),
                        ecommerce_gstin=norm.get('ecommerce_gstin'),
                        marketplace_name=norm.get('marketplace_name', platform_name),
                        note_type=norm.get('note_type'),
                        note_number=norm.get('note_number'),
                        row_fingerprint=fingerprint,
                    )
                    db.session.add(tx)

            # Update ImportHistory
            counts = {
                'total_rows': total_rows,
                'success_rows': success_rows,
                'warning_rows': warning_rows,
                'error_rows': error_rows,
                'skipped_rows': skipped_rows,
                'duplicate_rows': skipped_rows,
            }
            import_rec.total_rows = total_rows
            import_rec.success_rows = success_rows
            import_rec.error_rows = error_rows
            import_rec.warning_rows = warning_rows
            import_rec.skipped_rows = skipped_rows
            import_rec.processing_status = 'COMPLETED' if not (
                error_rows or warning_rows or skipped_rows
            ) else 'PARTIAL'
            error_summary, warning_summary = duplicate_envelope(
                rejections, duplicates, warning_report, counts
            )
            import_rec.error_summary = error_summary if rejections else None
            import_rec.warning_summary = warning_summary if (duplicates or warning_report) else None
            import_rec.processing_completed_at = datetime.utcnow()
            db.session.commit()

            if wb:
                wb.close()

            summary = f'Processed {total_rows} rows: {success_rows} imported'
            if warning_rows:
                summary += f', {warning_rows} with warnings'
            if error_rows:
                summary += f', {error_rows} rejected'
            if skipped_rows:
                summary += f', {skipped_rows} duplicates skipped'
            flash(f'File "{filename}" imported: {summary}.', 'success')
            return redirect(url_for('imports.history'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error processing upload: {str(e)}', 'danger')
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
    flash(f'Import {import_rec.file_name} reprocessed successfully', 'success')
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
