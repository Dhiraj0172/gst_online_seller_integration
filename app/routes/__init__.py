from flask import Blueprint

auth_bp = Blueprint('auth', __name__)
main_bp = Blueprint('main', __name__)
profile_bp = Blueprint('profile', __name__)
import_bp = Blueprint('imports', __name__)
statement_bp = Blueprint('statement', __name__)
tcs_bp = Blueprint('tcs', __name__)
generate_bp = Blueprint('generate', __name__)
api_bp = Blueprint('api', __name__, url_prefix='/api')

from . import auth, main, profile, import_routes, statement, tcs, generate, api

def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(import_bp)
    app.register_blueprint(statement_bp)
    app.register_blueprint(tcs_bp)
    app.register_blueprint(generate_bp)
    app.register_blueprint(api_bp)
