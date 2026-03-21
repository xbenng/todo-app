def register_blueprints(app):
    """Register all route blueprints with the Flask app."""
    from routes.auth import bp as auth_bp
    from routes.todos import bp as todos_bp
    from routes.chat import bp as chat_bp
    from routes.config import bp as config_bp
    from routes.history import bp as history_bp
    from routes.terminal import bp as terminal_bp
    from routes.jobs import bp as jobs_bp
    from routes.ea import bp as ea_bp

    for b in [auth_bp, todos_bp, chat_bp, config_bp, history_bp, terminal_bp, jobs_bp, ea_bp]:
        app.register_blueprint(b)
