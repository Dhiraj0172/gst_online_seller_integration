"""Integration tests for CSV upload, duplicate handling and import quality.

Exercises the real HTTP upload endpoint and the ImportHistory record it writes:
CSV through the adapter pipeline, duplicate rows in one file, re-upload of the
same file, duplicates against already imported data, legitimate non-duplicates
from different sources, and rejected (invalid) rows.

Uses the existing fixtures and the register/login/profile pattern of the other
integration tests; everything created here is removed in teardown because the
in-memory test database is shared across the session.
"""
import csv
import io
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.models import GSTProfile, ImportHistory, RawImport, Transaction, User


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), '..', 'fixtures')

HEADERS = ['Invoice Number', 'Invoice Date', 'Place of Supply', 'Buyer GSTIN',
           'Seller GSTIN', 'HSN/SAC', 'Product Description', 'Quantity',
           'Taxable Value', 'Tax Rate', 'CGST Amount', 'SGST Amount',
           'IGST Amount', 'Invoice Value', 'Supply Type', 'Order Item ID']

ROW = ['INV-1', '15-01-2025', '29-Karnataka', '29AALCS5765L1ZP',
       '27AABCU9603R1ZM', '6109', 'Cotton T-Shirt', '2', '1,000.00', '18',
       '90', '90', '0', '1,180.00', 'Regular', 'OI-1']


def _csv_bytes(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode('utf-8')


@pytest.fixture
def seller_client(app, db):
    suffix = uuid.uuid4().hex[:8]
    email = f'quality_{suffix}@example.com'
    password = 'QualityPass123!'

    with app.app_context():
        assert app.test_client().post('/register', data={
            'name': 'Quality Test Seller', 'email': email, 'password': password,
        }, follow_redirects=True).status_code == 200

        client = app.test_client()
        assert client.post('/login', data={'email': email, 'password': password},
                           follow_redirects=True).status_code == 200

        gstin = f'29QLTY{suffix[:4].upper()}1234A1Z{suffix[-1].upper()}'
        assert client.post('/profiles/create', data={
            'gstin': gstin, 'legal_name': 'Quality Test Seller Private Limited',
            'trade_name': 'Quality Test Seller', 'state': '29',
            'frequency': 'Monthly', 'financial_year': '2024-25',
        }, follow_redirects=True).status_code == 200

        profile_id = GSTProfile.query.filter_by(gstin=gstin).first().id

    yield {'client': client, 'app': app, 'profile_id': profile_id}

    with app.app_context():
        try:
            user = User.query.filter_by(email=email).first()
            if user is not None:
                db.session.delete(user)
                db.session.commit()
        except Exception:
            db.session.rollback()


def _upload(client, content, file_name, platform='Flipkart', extra=None):
    data = {'file': (io.BytesIO(content), file_name), 'platform': platform}
    if extra:
        data.update(extra)
    return client.post('/import/upload', data=data, content_type='multipart/form-data',
                       follow_redirects=True)


def _histories(app, profile_id):
    with app.app_context():
        return (ImportHistory.query.filter_by(profile_id=profile_id)
                .order_by(ImportHistory.id.asc()).all())


def _transactions(app, profile_id):
    with app.app_context():
        return Transaction.query.filter_by(profile_id=profile_id).all()


class TestCsvUpload:
    def test_valid_csv_uploads_and_normalizes(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        response = _upload(client, _csv_bytes([HEADERS, ROW]), 'flipkart_orders.csv')
        assert response.status_code == 200

        history = _histories(app, profile_id)[0]
        assert history.processing_status == 'COMPLETED'
        assert history.total_rows == 1
        assert history.success_rows == 1
        assert history.error_rows == 0
        assert history.skipped_rows == 0
        assert history.error_summary is None

        transactions = _transactions(app, profile_id)
        assert len(transactions) == 1
        transaction = transactions[0]
        assert transaction.invoice_number == 'INV-1'
        assert transaction.place_of_supply == '29'
        assert transaction.taxable_value == 1000
        assert transaction.row_fingerprint

    def test_utf8_bom_csv_uploads(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        payload = b'\xef\xbb\xbf' + _csv_bytes([HEADERS, ROW])
        assert _upload(client, payload, 'bom_orders.csv').status_code == 200
        history = _histories(app, profile_id)[0]
        assert history.processing_status == 'COMPLETED'
        assert history.total_rows == 1
        assert _transactions(app, profile_id)[0].invoice_number == 'INV-1'

    def test_semicolon_delimited_csv_uploads(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=';')
        writer.writerow(HEADERS)
        writer.writerow(ROW)
        assert _upload(client, buffer.getvalue().encode('utf-8'), 'semi.csv').status_code == 200
        assert _histories(app, profile_id)[0].total_rows == 1

    def test_malformed_csv_is_rejected_with_error_information(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        import openpyxl
        buffer = io.BytesIO()
        openpyxl.Workbook().save(buffer)          # an .xlsx wearing a .csv name
        assert _upload(client, buffer.getvalue(), 'not_really.csv').status_code == 200

        history = _histories(app, profile_id)[0]
        assert history.processing_status == 'FAILED'
        payload = json.loads(history.error_summary)
        assert payload['errors'][0]['code'] == 'UNREADABLE_SOURCE'
        assert 'not text CSV' in payload['errors'][0]['message']
        assert _transactions(app, profile_id) == []

    def test_invalid_rows_reject_but_keep_the_rest(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        negative = list(ROW)
        negative[0] = 'INV-2'
        negative[8] = '-500.00'                    # impossible negative taxable value
        bad_date = list(ROW)
        bad_date[0] = 'INV-3'
        bad_date[1] = '99-99-9999'
        good = list(ROW)
        good[0] = 'INV-4'

        response = _upload(client, _csv_bytes([HEADERS, negative, bad_date, good]),
                           'mixed_quality.csv')
        assert response.status_code == 200

        history = _histories(app, profile_id)[0]
        assert history.total_rows == 3
        assert history.success_rows == 1
        assert history.error_rows == 2
        assert history.processing_status == 'PARTIAL'

        payload = json.loads(history.error_summary)
        messages = ' | '.join(entry['message'] for entry in payload['errors'])
        codes = {entry['code'] for entry in payload['errors']}
        assert codes == {'ROW_REJECTED'}
        assert 'negative' in messages
        assert 'not a recognized date' in messages
        assert payload['counts']['error_rows'] == 2

        # rejected rows are kept as raw rows with their reasons, never dropped
        with app.app_context():
            raw_rows = RawImport.query.filter_by(import_history_id=history.id).all()
            statuses = sorted(raw.status for raw in raw_rows)
            assert statuses == ['ERROR', 'ERROR', 'SUCCESS']
            errors = [raw for raw in raw_rows if raw.status == 'ERROR']
            assert all(raw.errors for raw in errors)

        # only the valid row becomes a transaction
        transactions = _transactions(app, profile_id)
        assert [transaction.invoice_number for transaction in transactions] == ['INV-4']


class TestDuplicateHandling:
    def test_duplicate_rows_within_one_file_are_marked_skipped(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        assert _upload(client, _csv_bytes([HEADERS, ROW, ROW]), 'dupes.csv').status_code == 200

        history = _histories(app, profile_id)[0]
        assert history.total_rows == 2
        assert history.success_rows == 1
        assert history.skipped_rows == 1
        assert history.processing_status == 'PARTIAL'

        payload = json.loads(history.warning_summary)
        assert payload['duplicates'][0]['code'] == 'DUPLICATE_IN_FILE'
        assert payload['duplicates'][0]['row'] == 3
        assert payload['duplicates'][0]['duplicate_of_row'] == 2
        assert payload['counts']['duplicate_rows'] == 1

        # the duplicate is retained as a raw row, and creates no transaction
        with app.app_context():
            raw_rows = RawImport.query.filter_by(import_history_id=history.id).order_by(
                RawImport.row_number).all()
            assert [raw.status for raw in raw_rows] == ['SUCCESS', 'SKIPPED']
            assert 'Duplicate of row 2' in json.loads(raw_rows[1].errors)[0]['message']
        assert len(_transactions(app, profile_id)) == 1

    def test_reupload_of_the_same_file_is_rejected(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        payload = _csv_bytes([HEADERS, ROW])
        assert _upload(client, payload, 'orders.csv').status_code == 200
        first_id = _histories(app, profile_id)[0].id

        response = _upload(client, payload, 'orders.csv')
        assert response.status_code == 200
        assert b'Import rejected' in response.data

        histories = _histories(app, profile_id)
        assert [history.processing_status for history in histories] == ['COMPLETED', 'FAILED']
        report = json.loads(histories[1].error_summary)
        assert report['errors'][0]['code'] == 'DUPLICATE_FILE'
        assert report['errors'][0]['duplicate_of_import_id'] == first_id
        assert report['counts']['total_rows'] == 0
        # no second import of the rows
        assert len(_transactions(app, profile_id)) == 1

    def test_reupload_with_override_marks_every_row_as_duplicate(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        payload = _csv_bytes([HEADERS, ROW])
        assert _upload(client, payload, 'orders.csv').status_code == 200
        assert _upload(client, payload, 'orders.csv',
                       extra={'allow_duplicate_file': '1'}).status_code == 200

        histories = _histories(app, profile_id)
        assert history_statuses(histories) == ['COMPLETED', 'PARTIAL']
        second = histories[1]
        assert second.total_rows == 1
        assert second.skipped_rows == 1
        report = json.loads(second.warning_summary)
        assert report['duplicates'][0]['code'] == 'DUPLICATE_EXISTING'
        assert report['duplicates'][0]['duplicate_of_transaction_id'] is not None
        assert len(_transactions(app, profile_id)) == 1

    def test_duplicate_against_existing_data_from_a_later_export(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        assert _upload(client, _csv_bytes([HEADERS, ROW]), 'january.csv').status_code == 200

        new_row = list(ROW)
        new_row[0] = 'INV-2'
        new_row[15] = 'OI-2'
        assert _upload(client, _csv_bytes([HEADERS, ROW, new_row]),
                       'february_reexport.csv').status_code == 200

        histories = _histories(app, profile_id)
        second = histories[1]
        assert second.total_rows == 2
        assert second.success_rows == 1
        assert second.skipped_rows == 1
        report = json.loads(second.warning_summary)
        assert report['duplicates'][0]['code'] == 'DUPLICATE_EXISTING'
        assert report['duplicates'][0]['duplicate_of_import_id'] == histories[0].id
        assert sorted(transaction.invoice_number
                      for transaction in _transactions(app, profile_id)) == ['INV-1', 'INV-2']

    def test_same_invoice_number_from_a_different_source_is_not_a_duplicate(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        payload = _csv_bytes([HEADERS, ROW])
        # same rows, different marketplace/source: a legitimate distinct record
        assert _upload(client, payload, 'flipkart_orders.csv', platform='Flipkart').status_code == 200
        assert _upload(client, payload, 'meesho_orders.csv', platform='Meesho').status_code == 200

        histories = _histories(app, profile_id)
        assert history_statuses(histories) == ['COMPLETED', 'COMPLETED']
        assert all(history.skipped_rows == 0 for history in histories)
        transactions = _transactions(app, profile_id)
        assert len(transactions) == 2
        assert {transaction.invoice_number for transaction in transactions} == {'INV-1'}
        assert {transaction.source_platform for transaction in transactions} == {'Flipkart', 'Meesho'}

    def test_import_history_page_renders_the_report(self, seller_client):
        client, app, profile_id = (seller_client['client'], seller_client['app'],
                                   seller_client['profile_id'])
        assert _upload(client, _csv_bytes([HEADERS, ROW, ROW]), 'dupes.csv').status_code == 200

        page = client.get('/import/history')
        assert page.status_code == 200
        text = page.data.decode('utf-8')
        assert 'PARTIAL' in text
        assert 'DUPLICATE_IN_FILE' in text
        assert 'Duplicate of row 2' in text


def history_statuses(histories):
    return [history.processing_status for history in histories]
