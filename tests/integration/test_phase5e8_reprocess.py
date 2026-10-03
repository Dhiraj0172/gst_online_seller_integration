import pytest
from app.models.import_history import ImportHistory
from app.models.transaction import Transaction
from app.models.raw_import import RawImport
from app.models.audit_log import AuditLog
from app.models.gstr1_generation import GSTR1Generation
from app.models.user import User
from app.models.gst_profile import GSTProfile
from app.services.import_service import reprocess_import, ImportProcessingResult
import os
from datetime import datetime

def _setup_test_data(db):
    user = User.query.filter_by(username='testuser').first()
    if not user:
        user = User(username='testuser', email='t@t.com')
        user.set_password('testpass')
        db.session.add(user)
        db.session.commit()
    profile = GSTProfile.query.filter_by(gstin='29ABCDE1234F1Z5').first()
    if not profile:
        profile = GSTProfile(user_id=user.id, gstin='29ABCDE1234F1Z5', legal_name='Test', trade_name='Test', state_code='29', state_name='Karnataka', financial_year='2026-27', filing_frequency='MONTHLY')
        db.session.add(profile)
        db.session.commit()
    return user, profile

def test_reprocess_valid_owned_import(app, db):
    with app.app_context():
        user, profile = _setup_test_data(db)

        ih = ImportHistory(
            user_id=user.id, profile_id=profile.id, file_name='test.xlsx',
            original_file_name='test.xlsx', platform_name='Custom Excel',
            return_period='102026', financial_year='2026-27',
            processing_status='COMPLETED',
            raw_file_path=os.path.join(app.root_path, '..', 'tests', 'fixtures', 'custom_excel_sample.xlsx')
        )
        db.session.add(ih)
        db.session.flush()

        ri = RawImport(import_history_id=ih.id, row_number=1, status='SUCCESS')
        db.session.add(ri)
        db.session.flush()

        tx = Transaction(import_history_id=ih.id, profile_id=profile.id, raw_import_id=ri.id,
                         order_id='ORD-001', taxable_value=1000.0)
        db.session.add(tx)
        db.session.commit()

        old_ih_id = ih.id
        old_ri_id = ri.id
        old_tx_id = tx.id

        res = reprocess_import(old_ih_id, profile.id, user.id)
        assert res.status in ('SUCCESS', 'COMPLETED', 'PARTIAL')

        old_tx = db.session.get(Transaction, old_tx_id)
        assert old_tx.is_deleted == True

        old_ri = db.session.get(RawImport, old_ri_id)
        assert old_ri is not None

        new_ih = db.session.get(ImportHistory, res.import_history_id)
        assert new_ih.is_reprocessed == True
        assert new_ih.parent_import_id == old_ih_id

        new_txs = Transaction.query.filter_by(import_history_id=new_ih.id).all()
        assert len(new_txs) > 0
        assert new_txs[0].is_deleted == False

def test_reprocess_generation_freeze_blocks(app, db):
    with app.app_context():
        user, profile = _setup_test_data(db)
        ih = ImportHistory(
            user_id=user.id, profile_id=profile.id, file_name='test.xlsx', original_file_name='test.xlsx',
            platform_name='Custom Excel', return_period='102026', financial_year='2026-27',
            processing_status='COMPLETED', raw_file_path='dummy'
        )
        db.session.add(ih)
        db.session.flush()

        gen = GSTR1Generation(
            user_id=user.id, profile_id=profile.id, return_period='102026', financial_year='2026-27',
            generation_status='COMPLETED', validation_passed=True
        )
        db.session.add(gen)
        db.session.commit()

        res = reprocess_import(ih.id, profile.id, user.id)
        assert res.status == 'FAILED'
        assert "A finalized GSTR-1 generation exists" in res.errors[0]

def test_reprocess_unauthorized_cross_profile(app, db):
    with app.app_context():
        user, profile = _setup_test_data(db)
        ih = ImportHistory(
            user_id=user.id, profile_id=profile.id, file_name='t.xlsx', original_file_name='t.xlsx',
            platform_name='Custom Excel', return_period='102026', financial_year='2026-27',
            processing_status='COMPLETED', raw_file_path='dummy'
        )
        db.session.add(ih)
        db.session.commit()

        res = reprocess_import(ih.id, 9999, user.id)
        assert res.status == 'FAILED'
        assert "not found or unauthorized" in res.errors[0]

def test_reprocess_atomic_rollback_on_failure(app, db):
    with app.app_context():
        user, profile = _setup_test_data(db)
        ih = ImportHistory(
            user_id=user.id, profile_id=profile.id, file_name='test.xlsx',
            original_file_name='test.xlsx', platform_name='Custom Excel',
            return_period='102026', financial_year='2026-27',
            processing_status='COMPLETED',
            raw_file_path=os.path.join(app.root_path, '..', 'tests', 'fixtures', 'missing_file.xlsx')
        )
        db.session.add(ih)
        db.session.flush()

        tx = Transaction(import_history_id=ih.id, profile_id=profile.id, raw_import_id=1,
                         order_id='ORD-001', taxable_value=1000.0)
        db.session.add(tx)
        db.session.commit()

        res = reprocess_import(ih.id, profile.id, user.id)
        assert res.status == 'FAILED' or res.status.startswith('REJECTED')

        old_tx = db.session.get(Transaction, tx.id)
        assert old_tx.is_deleted == False

def test_reprocess_partial_success(app, db):
    with app.app_context():
        user, profile = _setup_test_data(db)
        from app.services.import_service import process_import

        file_path = os.path.join(app.root_path, '..', 'tests', 'fixtures', 'custom_excel_sample.xlsx')

        # 1. First import. It might be COMPLETED or PARTIAL depending on previous tests.
        res1 = process_import(file_path, profile.id, 'Custom Excel', user.id, '112026', '2026-27', allow_duplicate_file=True)
        assert res1.status in ('COMPLETED', 'PARTIAL')

        # 2. Second import. This will definitively be PARTIAL because it duplicates res1.
        res2 = process_import(file_path, profile.id, 'Custom Excel', user.id, '112026', '2026-27', allow_duplicate_file=True)
        assert res2.status == 'PARTIAL'
        assert res2.skipped_rows > 0

        # 3. Reprocess the second import. Should also yield PARTIAL and successfully commit.
        res3 = reprocess_import(res2.import_history_id, profile.id, user.id)
        assert res3.status == 'PARTIAL', f"Expected PARTIAL but got {res3.status}"
        assert res3.skipped_rows > 0

        # Verify atomicity (commit)
        new_ih = db.session.get(ImportHistory, res3.import_history_id)
        assert new_ih is not None
        assert new_ih.is_reprocessed == True
        assert new_ih.processing_status == 'PARTIAL'
