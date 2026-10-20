"""WSGI Production Entry Point for GST Online Seller Platform."""
import os
from app import create_app
from app.extensions import db

# Determine active configuration mode (production by default)
config_name = os.environ.get('FLASK_CONFIG', os.environ.get('FLASK_ENV', 'production'))
app = create_app(config_name)

if __name__ == '__main__':
    with app.app_context():
        # Ensure database tables exist
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
