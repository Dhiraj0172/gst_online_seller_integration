"""Celery Background Worker Entry Point for GST Online Seller Platform."""
import os
from app import create_app

# Initialize Flask application in production/configured mode
config_name = os.environ.get('FLASK_CONFIG', os.environ.get('FLASK_ENV', 'production'))
app = create_app(config_name)

celery_app = None

try:
    from celery import Celery
    broker_url = app.config.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    backend_url = app.config.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

    celery_app = Celery(
        app.import_name,
        broker=broker_url,
        backend=backend_url,
    )
    celery_app.conf.update(app.config)
except ImportError:
    # Standalone mock worker if celery package is not installed in lightweight environments
    class _MockCelery:
        def __init__(self, name):
            self.name = name
        def start(self):
            print("Celery is not installed. Background worker running in mock mode.")
    celery_app = _MockCelery(app.import_name)

if __name__ == '__main__':
    with app.app_context():
        if celery_app and hasattr(celery_app, 'start'):
            celery_app.start()
