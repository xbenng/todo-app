"""Authentication routes: register, login, logout, me."""

from flask import Blueprint, request, jsonify
import db as _db
from schemas import RegisterRequest, LoginRequest, validate_request

bp = Blueprint('auth', __name__)


def get_current_user():
    """Extract user from session cookie or Authorization header."""
    token = request.cookies.get("session_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    return _db.get_session_user(token)


def require_user():
    """Get current user or return None (caller should return 401)."""
    return get_current_user()


@bp.route("/api/auth/register", methods=["POST"])
@validate_request(RegisterRequest)
def auth_register(data: RegisterRequest):
    try:
        user = _db.create_user(data.email, data.password, data.name or None)
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return jsonify({"error": "Email already registered"}), 409
        return jsonify({"error": str(exc)}), 500
    token = _db.create_session(user["id"])
    resp = jsonify({"user": user})
    resp.set_cookie("session_token", token, httponly=True, samesite="Lax",
                     max_age=60 * 60 * 24 * 30)  # 30 days
    return resp


@bp.route("/api/auth/login", methods=["POST"])
@validate_request(LoginRequest)
def auth_login(data: LoginRequest):
    user = _db.verify_user(data.email, data.password)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401
    token = _db.create_session(user["id"])
    resp = jsonify({"user": user})
    resp.set_cookie("session_token", token, httponly=True, samesite="Lax",
                     max_age=60 * 60 * 24 * 30)
    return resp


@bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.cookies.get("session_token")
    if token:
        _db.delete_session(token)
    resp = jsonify({"ok": True})
    resp.delete_cookie("session_token")
    return resp


@bp.route("/api/auth/me")
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"user": user})
