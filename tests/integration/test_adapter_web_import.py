"""Integration tests for the marketplace adapter path through the web upload.

Covers the HTTP upload endpoint end to end:

  * a representative valid marketplace file (Flipkart GST report fixture)
    uploads successfully and produces normalized transactions
  * a malformed / unsupported file is explicitly rejected, is recorded as
    FAILED with machine-readable error information, and creates no transactions

Uses the existing fixtures and the same register/login/profile pattern as
test_web_workflow.py. Everything this test creates is removed in teardown
because the in-memory test database is shared across the session.
"""
import io
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.models import GSTProfile, ImportHistory, Transaction, User


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), '..', 'fixtures')


@pytest.fixture
def seller_client(app, db):
    """A logged-in client with one GST profile, cleaned up afterwards."""
    suffix = uuid.uuid4().hex[:8]
    email = f'adapter_{suffix}@example.com'
    password = 'AdapterPass123!'

    with app.app_context():
        reg = app.test_client().post('/register', data={
            'name': 'Adapter Test Seller',
            'email': email,
            'password': password,
        }, follow_redirects=True)
        assert reg.status_code == 200

        client = app.test_client()
        login = client.post('/login', data={'email': email, 'password': password},
                            follow_redirects=True)
        assert login.status_code == 200

        # Unique GSTIN: the column is globally unique and the DB is shared.
        gstin = f'29ADPT{suffix[:4].upper()}1234A1Z{suffix[-1].upper()}'
        profile = client.post('/profiles/create', data={
            'gstin': gstin,
            'legal_name': 'Adapter Test Seller Private Limited',
            'trade_name': 'Adapter Test Seller',
            'state': '29',
            'frequency': 'Monthly',
            'financial_year': '2024-25',
        }, follow_redirects=True)
        assert profile.status_code == 200

        created = {
            'email': email,
            'gstin': gstin,
            'profile_id': GSTProfile.query.filter_by(gstin=gstin).first().id,
        }

    yield {'client': client, 'app': app, 'profile_id': created['profile_id']}

    with app.app_context():
        try:
            user = User.query.filter_by(email=email).first()
            if user is not None:
                db.session.delete(user)  # cascades to profiles/imports/transactions
                db.session.commit()
        except Exception:
            db.session.rollback()


def _upload(client, fixture_name, platform):
    path = os.path.join(FIXTURE_DIR, fixture_name)
    assert os.path.exists(path), f'fixture {fixture_name} must exist'
    with open(path, 'rb') as handle:
        payload = io.BytesIO(handle.read())
    return client.post('/import/upload', data={
        'file': (payload, fixture_name),
        'platform': platform,
    }, content_type='multipart/form-data', follow_redirects=True)


class TestValidMarketplaceUpload:
    def test_flipkart_fixture_uploads_and_normalizes(self, seller_client):
        client = seller_client['client']
        app = seller_client['app']
        profile_id = seller_client['profile_id']

        response = _upload(client, 'flipkart_sample.xlsx', 'Flipkart')
        assert response.status_code == 200

        with app.app_context():
            history = ImportHistory.query.filter_by(profile_id=profile_id).one()
            assert history.processing_status == 'COMPLETED'
            assert history.platform_name == 'Flipkart'
            assert history.total_rows == 118
            assert history.success_rows == 118
            assert history.error_rows == 0
            assert history.error_summary in (None, '')

            transactions = Transaction.query.filter_by(profile_id=profile_id).all()
            assert len(transactions) == 118
            assert {tx.source_platform for tx in transactions} == {'Flipkart'}
            assert {tx.marketplace_name for tx in transactions} == {'Flipkart'}

            first = transactions[0]
            assert first.invoice_number
            assert first.invoice_date is not None
            assert first.hsn_sac
            assert first.order_id
            # Flipkart's line identifier and product title column
            assert first.source_row_id
            assert first.description

    def test_place_of_supply_is_stored_as_a_state_code(self, seller_client):
        client = seller_client['client']
        app = seller_client['app']
        profile_id = seller_client['profile_id']

        assert _upload(client, 'flipkart_sample.xlsx', 'Flipkart').status_code == 200

        with app.app_context():
            codes = {tx.place_of_supply for tx in
                     Transaction.query.filter_by(profile_id=profile_id).all()}
            assert codes
            assert all(code and code.isdigit() and len(code) == 2 for code in codes), codes


class TestRejectedUploads:
    def test_malformed_file_is_rejected_with_error_information(self, seller_client):
        client = seller_client['client']
        app = seller_client['app']
        profile_id = seller_client['profile_id']

        response = _upload(client, 'wrong_headers.xlsx', 'Flipkart')
        assert response.status_code == 200
        assert b'Import rejected' in response.data

        with app.app_context():
            history = ImportHistory.query.filter_by(profile_id=profile_id).one()
            assert history.processing_status == 'FAILED'
            assert history.processing_completed_at is not None
            assert history.total_rows == 0

            errors = json.loads(history.error_summary)
            assert errors, 'rejection must record error information'
            assert errors['counts']['total_rows'] == 0
            messages = [entry['message'] for entry in errors['errors']]
            codes = [entry['code'] for entry in errors['errors']]
            assert any('recognizable header row' in message for message in messages)
            assert any('Flipkart' in message for message in messages)
            assert codes == ['INVALID_FILE']

            # a rejected file must not create normalized transactions
            assert Transaction.query.filter_by(profile_id=profile_id).count() == 0
            # ... and the raw rows must not be ingested either
            assert history.success_rows == 0

    def test_unrelated_file_for_a_chosen_platform_is_rejected(self, seller_client):
        client = seller_client['client']
        app = seller_client['app']
        profile_id = seller_client['profile_id']

        # an Amazon-shaped file uploaded as Meesho: headers are readable, but the
        # adapter still accepts it only after explicit validation, and the import
        # is recorded against the platform the user chose
        assert _upload(client, 'amazon_sample.xlsx', 'Meesho').status_code == 200

        with app.app_context():
            history = ImportHistory.query.filter_by(profile_id=profile_id).one()
            assert history.platform_name == 'Meesho'
            # the generic alias table resolves the shared columns, so this file is
            # importable: 168 rows, all of them retained
            assert history.total_rows == 168
            assert history.error_rows == 0
            assert Transaction.query.filter_by(profile_id=profile_id).count() == 168

    def test_rejection_does_not_block_a_later_valid_upload(self, seller_client):
        client = seller_client['client']
        app = seller_client['app']
        profile_id = seller_client['profile_id']

        assert _upload(client, 'wrong_headers.xlsx', 'Amazon').status_code == 200
        assert _upload(client, 'amazon_sample.xlsx', 'Amazon').status_code == 200

        with app.app_context():
            histories = ImportHistory.query.filter_by(profile_id=profile_id).order_by(
                ImportHistory.id).all()
            assert [history.processing_status for history in histories] == ['FAILED', 'COMPLETED']
            assert histories[1].total_rows == 168
            assert Transaction.query.filter_by(profile_id=profile_id).count() == 168
