import os
import uuid

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from flask.json.provider import DefaultJSONProvider

__version__ = '0.2.6'


class _IsoJSONProvider(DefaultJSONProvider):
    """Datetimes leave as ISO-8601 with a Z: everything stored is naive UTC, and the browser
    should not have to guess that."""
    sort_keys = False

    def default(self, o):
        import datetime as _dt
        if isinstance(o, _dt.datetime):
            return o.isoformat(timespec='seconds') + ('Z' if o.tzinfo is None else '')
        if isinstance(o, _dt.date):
            return o.isoformat()
        return super().default(o)


def create_app(home=None, testing: bool = False) -> Flask:
    from . import auth
    from .config import make_config
    from .events import event_logger
    from .models import SCHEMA_VERSION, Meta, db

    app = Flask(__name__)
    app.json = _IsoJSONProvider(app)
    app.config.update(make_config(home))
    app.config['TESTING'] = testing
    # Auth cookies are Secure: a login over plain LAN HTTP deliberately does not stick, the
    # supported front door is `tailscale serve` (HTTPS). Browsers accept Secure cookies on
    # http://localhost, so local development still works. Opt out only for LAN debugging.
    app.config['SESSION_COOKIE_SECURE'] = not (testing or os.environ.get('FITMON_INSECURE_COOKIES') == '1')
    # One hop: `tailscale serve` terminates TLS and forwards X-Forwarded-Proto/For.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    event_logger.configure(app.config['FITMON_HOME'])
    db.init_app(app)
    with app.app_context():
        db.create_all()
        if db.session.get(Meta, 'schema_version') is None:
            db.session.add(Meta(key='schema_version', value=str(SCHEMA_VERSION)))
            db.session.commit()

    app.before_request(auth.load_user)
    app.before_request(auth.require_login)

    from .routes.pages import pages_bp
    from .routes.api_core import core_bp
    from .routes.api_auth import auth_bp
    from .routes.api_admin import admin_bp
    from .routes.api_import import import_bp
    from .routes.api_activities import activities_bp
    from .routes.api_trends import trends_bp
    from .routes.api_explorer import explorer_bp
    from .routes.api_sync import sync_bp
    from .routes.api_account import account_bp
    for bp in (pages_bp, core_bp, auth_bp, admin_bp, import_bp, activities_bp,
               trends_bp, explorer_bp, sync_bp, account_bp):
        app.register_blueprint(bp)

    @app.context_processor
    def _inject_version():
        return {'version': __version__, 'min_password_length': auth.MIN_PASSWORD_LENGTH}

    @app.errorhandler(Exception)
    def _handle_error(exc):
        if isinstance(exc, HTTPException):
            if request.path.startswith('/api/'):
                return jsonify({'error': exc.name.lower().replace(' ', '_')}), exc.code
            return exc
        # Never leak a traceback or a path to the client; the detail goes to the event log.
        error_id = uuid.uuid4().hex[:12]
        app.logger.exception('unhandled error %s', error_id)
        event_logger.error('app.unhandled_error', f'{type(exc).__name__}: {exc}', error_id=error_id,
                           path=request.path, method=request.method)
        if request.path.startswith('/api/'):
            return jsonify({'error': 'internal_error', 'error_id': error_id}), 500
        return f'Internal error ({error_id})', 500

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        if request.path.startswith('/api/'):
            resp.headers.setdefault('Cache-Control', 'no-store')
        return resp

    return app
