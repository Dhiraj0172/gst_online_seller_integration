import os
from app import create_app
from app.extensions import db

app = create_app()

if __name__ == '__main__':
    with app.app_context():
        # Ensure database tables are created before running
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, port=port)
