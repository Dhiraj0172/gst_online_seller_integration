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
from app.utils.date_utils import parse_date
from app.utils.state_codes import resolve_pos_code
from app.extensions import db
from . import import_bp

ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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
            
            # Process file using the platform adapter
            adapter = get_adapter(platform_name)
            wb = None
            try:
                wb = openpyxl.load_workbook(filepath, data_only=True)
                if not adapter:
                    adapter = detect_platform(wb, filename)
            except Exception:
                pass
                
            if not adapter:
                adapter = BaseGenericAdapter()
                
            parse_result = adapter.parse(wb if wb else filepath)
            
            total_rows = 0
            success_rows = 0
            error_rows = 0
            
            for row in parse_result.rows:
                total_rows += 1
                is_success = (row.status.value == 'SUCCESS')
                if is_success:
                    success_rows += 1
                else:
                    error_rows += 1
                    
                # Save RawImport
                raw_imp = RawImport(
                    import_history_id=import_rec.id,
                    sheet_name=getattr(row, 'sheet_name', 'Sheet1') or 'Sheet1',
                    row_number=row.row_number,
                    raw_data=json.dumps({k: str(v) for k, v in row.raw_data.items() if v is not None}),
                    status=row.status.value,
                    errors=json.dumps(row.errors) if row.errors else None,
                    warnings=json.dumps(row.warnings) if row.warnings else None
                )
                db.session.add(raw_imp)
                db.session.flush()
                
                # Save normalized Transaction if parsed successfully
                norm = row.normalized_data or {}
                if norm and norm.get('invoice_number'):
                    inv_date_str = norm.get('invoice_date')
                    inv_date = parse_date(inv_date_str) if inv_date_str else None
                    
                    # Classify transaction
                    raw_supply = str(norm.get('supply_type') or '').strip().upper()
                    if raw_supply in ('B2B', 'B2CS', 'B2CL', 'CDNR', 'CDNUR', 'NIL', 'EXEMPT', 'NONGST', 'EXPORT', 'SEZ'):
                        supply_type = raw_supply
                    else:
                        supply_type = classify_transaction(norm, profile, return_period)
                    
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
                        total_tax=Decimal(str(norm.get('total_tax', 0) or 0)),
                        invoice_value=Decimal(str(norm.get('invoice_value', 0) or 0)),
                        tax_rate=Decimal(str(norm.get('tax_rate', 0) or 0)),
                        supply_type=supply_type,
                        reverse_charge=norm.get('reverse_charge', 'N'),
                        ecommerce_gstin=norm.get('ecommerce_gstin'),
                        marketplace_name=norm.get('marketplace_name', platform_name),
                        note_type=norm.get('note_type'),
                        note_number=norm.get('note_number')
                    )
                    db.session.add(tx)
            
            # Update ImportHistory
            import_rec.total_rows = total_rows
            import_rec.success_rows = success_rows
            import_rec.error_rows = error_rows
            import_rec.processing_status = 'COMPLETED'
            import_rec.processing_completed_at = datetime.utcnow()
            db.session.commit()
            
            if wb:
                wb.close()
                
            flash(f'File "{filename}" imported successfully! Processed {total_rows} rows ({success_rows} valid).', 'success')
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
    return render_template('import_history.html', history=history_list)

@import_bp.route('/import/<int:id>/detail')
@login_required
def detail(id):
    profile = get_active_profile()
    if not profile:
        return redirect(url_for('profile.list_profiles'))
    import_rec = ImportHistory.query.filter_by(id=id, profile_id=profile.id).first_or_404()
    return render_template('import_history.html', history=[import_rec])

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
