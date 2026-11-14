import os
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_DISPLAY_TZ = ZoneInfo("Asia/Manila")

from flask import Flask, abort, g, jsonify, request, session, url_for

from . import alipay as ali
from .config import database_path, load_keys
from .core import Core


def build_core(**kw):
    _, enc = load_keys()
    return Core(database_path(), enc, public_base_url_env=os.environ.get("PUBLIC_BASE_URL", ""), **kw)


def create_app(core=None, secret_key=None, start_worker=False):
    if core is None:
        core = build_core()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secret_key or load_keys()[0]
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("PUBLIC_BASE_URL", "").startswith("https://")
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    app.core = core
    app.json.ensure_ascii = False          # readable Chinese in JSON (the checkout dialog embeds plan data in the page)

    @app.template_global()
    def asset_url(filename):
        """Version static URLs so browsers/CDNs do not mix new HTML with stale CSS or JS."""
        path = os.path.join(app.static_folder, filename)
        try:
            version = int(os.path.getmtime(path))
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    @app.template_filter("yuan")
    def yuan(cents):
        return ali.format_cents(cents or 0)

    @app.template_filter("dt")
    def dt(ts):
        if not ts:
            return "-"
        return datetime.fromtimestamp(int(ts), tz=_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")

    @app.context_processor
    def inject():
        return {"csrf_token": csrf_token, "user": current_user(), "core": core}

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def current_user():
        if "user" not in g:
            g.user = None
            uid = session.get("uid")
            if uid:
                g.user = core.conn.execute("SELECT id, email, is_admin FROM users WHERE id=?", (uid,)).fetchone()
        return g.user

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and request.endpoint != "notify.alipay":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "CSRF token invalid")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        if request.method != "GET" or request.path.startswith(("/order", "/pay", "/my", "/admin", "/api/orders/")):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    from . import humanize
    humanize.register(app)
    from .admin import bp as admin_bp
    from .web import bp as web_bp, notify_bp
    app.register_blueprint(web_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.current_user = current_user

    if start_worker:
        from .worker import start_thread
        start_thread(core)
    return app
