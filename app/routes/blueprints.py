from . import (
    auth_bp, main_bp, profile_bp, import_bp, statement_bp,
    tcs_bp, generate_bp, api_bp
)

def register_blueprints(app):
    """Register all application blueprints."""
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(import_bp)
    app.register_blueprint(statement_bp)
    app.register_blueprint(tcs_bp)
    app.register_blueprint(generate_bp)
    app.register_blueprint(api_bp)
