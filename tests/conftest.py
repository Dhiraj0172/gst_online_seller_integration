"""Pytest configuration and shared fixtures."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app import create_app
from app.extensions import db as _db
from app.models.user import User


@pytest.fixture(scope='session')
def app():
    """Create application for testing."""
    app = create_app('testing')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SERVER_NAME'] = 'localhost'
    with app.app_context():
        _db.create_all()
        yield app
        _db.drop_all()


@pytest.fixture(scope='function')
def db(app):
    """Provide a clean database for each test."""
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    """Test client."""
    return app.test_client()


@pytest.fixture
def auth_client(app, db):
    """Authenticated test client."""
    with app.app_context():
        user = User(username='testuser', email='test@test.com')
        user.set_password('testpass123')
        db.session.add(user)
        db.session.commit()
        
        client = app.test_client()
        client.post('/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        }, follow_redirects=True)
        yield client


@pytest.fixture
def fixture_dir():
    """Path to test fixtures directory."""
    return os.path.join(os.path.dirname(__file__), 'fixtures')


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "no_request_context: mark test to run without Flask request context"
    )


@pytest.fixture(autouse=True)
def _push_request_context(request):
    """During tests execution request context has been pushed.

    Honors no_request_context marker to allow testing offline/CLI code outside request context.
    """
    if "app" not in request.fixturenames:
        return

    if request.node.get_closest_marker("no_request_context"):
        return

    app = request.getfixturevalue("app")
    if "live_server" in request.fixturenames:
        app = request.getfixturevalue("live_server").app

    ctx = app.test_request_context()
    ctx.push()

    def teardown():
        ctx.pop()

    request.addfinalizer(teardown)
