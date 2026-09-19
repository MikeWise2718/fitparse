import os
from flask import Flask
from .models import db
from .config import Config


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Ensure upload folder exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Initialize extensions
    db.init_app(app)

    # Create tables
    with app.app_context():
        db.create_all()

    # Register blueprints
    from .routes.main import main_bp
    from .routes.upload import upload_bp
    from .routes.cycling import cycling_bp
    from .routes.running import running_bp
    from .routes.activity import activity_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp, url_prefix='/upload')
    app.register_blueprint(cycling_bp, url_prefix='/cycling')
    app.register_blueprint(running_bp, url_prefix='/running')
    app.register_blueprint(activity_bp, url_prefix='/activity')

    return app
