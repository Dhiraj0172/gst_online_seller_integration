import pytest
import os
from app.models.transaction import Transaction
from app.models.import_history import ImportHistory
from app.models.raw_import import RawImport
from app.models.user import User
from app.models.gst_profile import GSTProfile
from app.services.import_service import reprocess_import
from app.extensions import db

def test_reprocess_atomicity_on_invalid_file(app, tmp_path):
    with app.app_context():
        # Setup test data
        test_user = User(username='test_user', email='test@example.com')
        test_user.set_password('test')
        db.session.add(test_user)
        db.session.commit()

        test_profile = GSTProfile(
            user_id=test_user.id,
            legal_name='Test Business',
            gstin='27AAAAA0000A1Z5',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2023-24',
            filing_frequency='monthly'
        )
        db.session.add(test_profile)
        db.session.commit()

        import_history = ImportHistory(
            user_id=test_user.id,
            profile_id=test_profile.id,
            file_name="original.csv",
            original_file_name="original.csv",
            platform_name="Amazon",
            return_period="2023-10",
            financial_year="2023-24",
            processing_status="COMPLETED"
        )
        db.session.add(import_history)
        db.session.commit()
        
        raw_import = RawImport(
            import_history_id=import_history.id,
            sheet_name="sheet1",
            row_number=1,
            raw_data="{}"
        )
        db.session.add(raw_import)
        db.session.commit()

        tx = Transaction(
            import_history_id=import_history.id,
            profile_id=test_profile.id,
            raw_import_id=raw_import.id,
            source_platform="Amazon",
            is_deleted=False
        )
        db.session.add(tx)
        db.session.commit()

        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("invalid,headers,here\n1,2,3")
        import_history.raw_file_path = str(bad_csv)
        db.session.commit()

        original_tx_id = tx.id
        original_ih_id = import_history.id

        assert db.session.get(Transaction, original_tx_id).is_deleted is False

        # Execute
        result = reprocess_import(original_ih_id, test_profile.id, test_user.id)
        
        assert result.status != "COMPLETED"
        
        # Verify
        db.session.expire_all()
        tx_after = db.session.get(Transaction, original_tx_id)
        assert tx_after.is_deleted is False, "Transaction was soft-deleted but reprocess failed! ATOMICITY BROKEN."

        ih_count = ImportHistory.query.filter_by(parent_import_id=original_ih_id).count()
        assert ih_count == 0, "Partial ImportHistory remained after failed reprocess"
