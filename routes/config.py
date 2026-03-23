"""Config and MCP server management routes."""
import os, json, time, hmac, hashlib, secrets, base64, urllib.parse
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify, render_template
from routes.auth import get_current_user
import state
from services.mcp_utils import (
    _load_mcp_registry, _get_mcp_manager, _get_mcp_tools, _redact_key,
    _build_mcp_configs_from_registry, _write_server_accounts,
)
from services.chat_runner import _get_active_provider
import db as _db
from schemas import ApproveToolRequest, SetMcpServerRequest, SetMcpToolRequest, validate_request

try:
    from mcp import ClientSessionGroup
except ImportError:
    ClientSessionGroup = None

bp = Blueprint('config', __name__)

_OAUTH_SECRET = os.environ.get("OAUTH_STATE_SECRET", "todo-app-oauth-state-secret")


def _resolve_oauth_creds(provider: dict) -> tuple[str, str]:
    """Resolve OAuth client_id and client_secret from env vars or direct values."""
    client_id = provider.get("client_id") or os.environ.get(provider.get("client_id_env", ""), "")
    client_secret = provider.get("client_secret") or os.environ.get(provider.get("client_secret_env", ""), "")
    return client_id, client_secret


@bp.route("/api/config", methods=["GET"])
def get_config():
    """Return server config with API keys redacted."""
    user = get_current_user()
    config = _db.get_config(user["id"]) if user else {}
    safe = dict(config)
    # Redact legacy flat keys
    if "anthropic_api_key" in safe and safe["anthropic_api_key"]:
        safe["anthropic_api_key"] = _redact_key(safe["anthropic_api_key"])
    if "openai_compat" in safe and isinstance(safe["openai_compat"], dict):
        oai = dict(safe["openai_compat"])
        if oai.get("api_key"):
            oai["api_key"] = _redact_key(oai["api_key"])
        safe["openai_compat"] = oai
    # Redact provider keys
    if "providers" in safe and isinstance(safe["providers"], dict):
        providers = {}
        for name, prov in safe["providers"].items():
            p = dict(prov)
            if p.get("api_key"):
                p["api_key"] = _redact_key(p["api_key"])
            providers[name] = p
        safe["providers"] = providers
    # Include active provider info
    active_name, active_prov = _get_active_provider()
    safe["_active_provider_name"] = active_name
    safe["_active_provider_type"] = active_prov.get("type", "local")
    return jsonify(safe)


@bp.route("/api/config", methods=["PUT"])
def put_config():
    """Update server config."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    config = _db.get_config(user["id"])
    # Merge provided fields (legacy + new)
    for key in ("anthropic_api_key", "model", "mcp_servers", "openai_compat", "active_provider", "subagents_enabled", "max_subagents", "auto_approve_all", "system_prompt_strict", "local_cli_enabled"):
        if key in data:
            config[key] = data[key]
    if "providers" in data and isinstance(data["providers"], dict):
        existing = config.get("providers", {})
        for name, prov in data["providers"].items():
            if prov is None:
                existing.pop(name, None)  # Delete provider
            elif name in existing:
                # Merge: don't overwrite api_key with redacted value
                for k, v in prov.items():
                    if k == "api_key" and v and "..." in v:
                        continue  # Skip redacted key
                    existing[name][k] = v
            else:
                existing[name] = prov
        config["providers"] = existing
    if "tokens" in data and isinstance(data["tokens"], dict):
        config.setdefault("tokens", {}).update(data["tokens"])
    _db.save_config(user["id"], **{k: v for k, v in config.items()
                    if k in ("providers", "active_provider", "tokens",
                             "subagents_enabled", "max_subagents",
                             "auto_approve_all", "system_prompt_strict",
                             "local_cli_enabled")})
    return jsonify({"ok": True})


@bp.route("/api/mcp/status")
def mcp_status():
    """Return status of all MCP servers with per-user preferences."""
    user = get_current_user()
    user_id = user["id"] if user else None
    registry = _load_mcp_registry()
    mgr = _get_mcp_manager(user_id)
    connected_status = mgr.get_status() if mgr else {}
    # Load user preferences
    prefs = {}
    auto_approve_all = False
    if user_id and user_id != "local":
        prefs = _db.get_mcp_preferences(user_id)
        config = _db.get_config(user_id)
        auto_approve_all = config.get("auto_approve_all", False)
    # Get user tokens to show which credentials are set
    user_tokens = {}
    if user_id and user_id != "local":
        user_tokens = config.get("tokens", {})
    servers = []
    for name, reg_entry in registry.items():
        pref = prefs.get(name, {})
        cred_fields = reg_entry.get("credential_fields", [])
        acct_fields = reg_entry.get("account_fields", [])
        acct_count = 0
        if acct_fields and user_id and user_id != "local":
            acct_count = len(_db.get_server_accounts(user_id, name))
        entry = {
            "name": name,
            "label": reg_entry.get("label", name),
            "enabled": pref.get("enabled", False),
            "disabled_tools": pref.get("disabled_tools", []),
            "auto_approved_tools": pref.get("auto_approved_tools", []),
            "credential_fields": [
                {**f, "has_value": bool(user_tokens.get(f["key"]))}
                for f in cred_fields
            ],
            "account_fields": acct_fields,
            "account_count": acct_count,
            "oauth_providers": [
                {"id": p["id"], "label": p["label"]}
                for p in reg_entry.get("oauth_providers", [])
            ],
            "bearer_token_key": reg_entry.get("bearer_token", ""),
            "bearer_connected": bool(
                reg_entry.get("bearer_token") and
                user_tokens.get(reg_entry.get("bearer_token"))
            ),
        }
        if name in connected_status:
            entry["connected"] = connected_status[name].get("connected", False)
            entry["tool_count"] = connected_status[name].get("tool_count", 0)
            if "error" in connected_status[name]:
                entry["error"] = connected_status[name]["error"]
            # Include tool names for this server
            if mgr and entry["connected"]:
                prefix = f"mcp__{name}__"
                # Get global excludes from registry
                global_excludes = set(reg_entry.get("exclude_tools", []))
                entry["tools"] = [
                    {"name": t["name"][len(prefix):],
                     "description": (t.get("description") or "")[:120]}
                    for t in mgr.get_tool_definitions()
                    if t["name"].startswith(prefix)
                    and t["name"][len(prefix):] not in global_excludes
                ]
        else:
            entry["connected"] = False
        servers.append(entry)
    return jsonify({
        "servers": servers,
        "available": bool(ClientSessionGroup),
        "auto_approve_all": auto_approve_all,
    })


@bp.route("/api/mcp/reconnect", methods=["POST"])
def mcp_reconnect():
    """Restart MCP server connections for the current user."""
    user = get_current_user()
    user_id = user["id"] if user else None
    uid = user_id or "local"
    with state._mcp_managers_lock:
        old = state._mcp_managers.pop(uid, None)
    if old:
        old.stop()
    # Next call to _get_mcp_manager will lazy-create a fresh one
    mgr = _get_mcp_manager(user_id)
    return jsonify({"ok": True, "connected": mgr is not None})


@bp.route("/api/mcp/approve", methods=["POST"])
@validate_request(ApproveToolRequest)
def mcp_approve(data: ApproveToolRequest):
    """Approve or deny a pending tool execution."""
    with state._approvals_lock:
        pending = state._pending_approvals.get(data.approval_id)
    if not pending:
        return jsonify({"error": "No pending approval with that ID"}), 404

    pending["approved"] = data.approved

    # Persist auto-approval if requested
    if data.approved and data.always_allow:
        user = get_current_user()
        if user:
            _db.set_tool_auto_approved(
                user["id"], pending["server_name"], pending["tool_name"], True
            )

    # Unblock the waiting thread
    pending["event"].set()
    return jsonify({"ok": True})


@bp.route("/api/mcp/servers", methods=["PUT"])
@validate_request(SetMcpServerRequest)
def mcp_set_server(data: SetMcpServerRequest):
    """Enable or disable an MCP server for the current user."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    registry = _load_mcp_registry()
    if data.server not in registry:
        return jsonify({"error": f"Unknown server: {data.server}"}), 400
    _db.set_server_enabled(user["id"], data.server, data.enabled)
    # Reconnect with new server set
    uid = user["id"]
    with state._mcp_managers_lock:
        old = state._mcp_managers.pop(uid, None)
    if old:
        old.stop()
    return jsonify({"ok": True})


@bp.route("/api/mcp/tools", methods=["PUT"])
@validate_request(SetMcpToolRequest)
def mcp_set_tool(data: SetMcpToolRequest):
    """Set tool disabled or auto_approved state."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    if data.disabled is not None:
        _db.set_tool_disabled(user["id"], data.server, data.tool, data.disabled)
    if data.auto_approved is not None:
        _db.set_tool_auto_approved(user["id"], data.server, data.tool, data.auto_approved)
    return jsonify({"ok": True})


@bp.route("/api/mcp/accounts/<server_name>", methods=["GET"])
def mcp_get_accounts(server_name):
    """Get all accounts for a server."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    accounts = _db.get_server_accounts(user["id"], server_name)
    # Redact secrets, add oauth_connected flag
    for acct in accounts:
        cfg = acct["config"]
        if "oauth_token" in cfg:
            cfg["oauth_connected"] = True
            del cfg["oauth_token"]
        for k in list(cfg.keys()):
            if "password" in k.lower() and cfg[k]:
                cfg[k] = "***"
    return jsonify({"accounts": accounts})


@bp.route("/api/mcp/accounts/<server_name>", methods=["POST"])
def mcp_add_account(server_name):
    """Add an account for a server."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    registry = _load_mcp_registry()
    if server_name not in registry:
        return jsonify({"error": f"Unknown server: {server_name}"}), 400
    data = request.json or {}
    acct = _db.add_server_account(user["id"], server_name, data)
    # Reconnect to pick up new account
    uid = user["id"]
    with state._mcp_managers_lock:
        old = state._mcp_managers.pop(uid, None)
    if old:
        old.stop()
    return jsonify(acct), 201


@bp.route("/api/mcp/accounts/<server_name>/<account_id>", methods=["PUT"])
def mcp_update_account(server_name, account_id):
    """Update an account."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    # Merge: don't overwrite password with redacted value
    existing = _db.get_server_accounts(user["id"], server_name)
    old = next((a for a in existing if a["id"] == account_id), None)
    if old:
        merged = dict(old["config"])
        for k, v in data.items():
            if "password" in k.lower() and v == "***":
                continue  # Skip redacted
            merged[k] = v
        data = merged
    if not _db.update_server_account(user["id"], account_id, data):
        return jsonify({"error": "Not found"}), 404
    # Reconnect
    uid = user["id"]
    with state._mcp_managers_lock:
        old_mgr = state._mcp_managers.pop(uid, None)
    if old_mgr:
        old_mgr.stop()
    return jsonify({"ok": True})


@bp.route("/api/mcp/accounts/<server_name>/<account_id>", methods=["DELETE"])
def mcp_delete_account(server_name, account_id):
    """Delete an account."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    if not _db.delete_server_account(user["id"], account_id):
        return jsonify({"error": "Not found"}), 404
    # Reconnect
    uid = user["id"]
    with state._mcp_managers_lock:
        old = state._mcp_managers.pop(uid, None)
    if old:
        old.stop()
    return jsonify({"ok": True})


@bp.route("/api/mcp/oauth/start")
def mcp_oauth_start():
    """Start an OAuth flow. Returns {auth_url} for the UI to open in a popup."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    server = request.args.get("server", "")
    provider_id = request.args.get("provider", "")
    account_id = request.args.get("account_id", "")
    registry = _load_mcp_registry()
    if server not in registry:
        return jsonify({"error": f"Unknown server: {server}"}), 400
    providers = registry[server].get("oauth_providers", [])
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        return jsonify({"error": f"Unknown OAuth provider: {provider_id}"}), 400
    client_id, client_secret = _resolve_oauth_creds(provider)
    if not client_id:
        return jsonify({"error": "OAuth client_id not configured (check env vars)"}), 500
    # PKCE support
    code_verifier = ""
    if provider.get("pkce"):
        import secrets as _secrets
        code_verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
    # Build state (signed) — include code_verifier for PKCE
    state_payload = {"user_id": user["id"], "server": server,
                     "provider": provider_id, "account_id": account_id}
    if code_verifier:
        state_payload["code_verifier"] = code_verifier
    state_data = json.dumps(state_payload)
    sig = hmac.new(_OAUTH_SECRET.encode(), state_data.encode(), hashlib.sha256).hexdigest()[:16]
    oauth_state = base64.urlsafe_b64encode(f"{sig}:{state_data}".encode()).decode()
    # Build redirect URI from request host
    domain = os.environ.get("DOMAIN_NAME")
    if domain:
        redirect_uri = f"https://{domain}/api/mcp/oauth/callback"
    else:
        redirect_uri = f"{request.scheme}://{request.host}/api/mcp/oauth/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": oauth_state,
    }
    if code_verifier:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if provider.get("scope"):
        params["scope"] = provider["scope"]
    if provider.get("user_scope"):
        params["user_scope"] = provider["user_scope"]
    if not provider.get("user_scope") and not provider.get("pkce"):
        # Google-style: request offline access for refresh tokens
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    # Provider-specific extra params (e.g., Atlassian audience)
    extra = provider.get("extra_auth_params", {})
    params.update(extra)
    auth_url = provider["auth_uri"] + "?" + urllib.parse.urlencode(params)
    return jsonify({"auth_url": auth_url})


@bp.route("/api/mcp/oauth/callback")
def mcp_oauth_callback():
    """OAuth callback. Exchanges code for tokens, stores in DB, closes popup."""
    try:
        return _handle_oauth_callback()
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        return f"<html><body><h3>Error</h3><pre>{tb}</pre></body></html>", 500


def _handle_oauth_callback():
    code = request.args.get("code", "")
    state_b64 = request.args.get("state", "")
    error = request.args.get("error", "")
    if error:
        return f"<html><body><h3>OAuth Error: {error}</h3><script>window.close()</script></body></html>"
    if not code or not state_b64:
        return "Missing code or state", 400
    # Validate state
    try:
        state_raw = base64.urlsafe_b64decode(state_b64).decode()
        sig, state_data = state_raw.split(":", 1)
        expected_sig = hmac.new(_OAUTH_SECRET.encode(), state_data.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return "Invalid state signature", 400
        oauth_state = json.loads(state_data)
    except Exception:
        return "Invalid state", 400
    user_id = oauth_state["user_id"]
    server = oauth_state["server"]
    provider_id = oauth_state["provider"]
    account_id = oauth_state.get("account_id", "")
    # Look up OAuth provider config
    registry = _load_mcp_registry()
    providers = registry.get(server, {}).get("oauth_providers", [])
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        return "Unknown provider", 400
    client_id, client_secret = _resolve_oauth_creds(provider)
    domain = os.environ.get("DOMAIN_NAME")
    if domain:
        redirect_uri = f"https://{domain}/api/mcp/oauth/callback"
    else:
        redirect_uri = f"{request.scheme}://{request.host}/api/mcp/oauth/callback"
    # Exchange code for tokens
    import requests as _requests
    token_payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    # Include PKCE code_verifier if present in state
    code_verifier = oauth_state.get("code_verifier")
    if code_verifier:
        token_payload["code_verifier"] = code_verifier
    resp = _requests.post(provider["token_uri"], data=token_payload)
    if resp.status_code != 200:
        return f"<html><body><h3>Token exchange failed</h3><pre>{resp.text}</pre><script>setTimeout(()=>window.close(),5000)</script></body></html>"
    token_data = resp.json()
    # Extract the access token — support nested paths like "authed_user.access_token"
    token_path = provider.get("token_path", "access_token")
    access_token = token_data
    for key in token_path.split("."):
        access_token = access_token.get(key) if isinstance(access_token, dict) else None
    if not access_token:
        return f"<html><body><h3>No access token in response</h3><pre>{json.dumps(token_data, indent=2)}</pre></body></html>", 400
    # Check if this provider stores as a credential (e.g., Slack xoxp)
    store_as = provider.get("store_as")
    store_as_oauth = provider.get("store_as_oauth")
    if store_as:
        # Store directly as a user credential token (raw access token)
        config = _db.get_config(user_id)
        tokens = config.get("tokens", {})
        tokens[store_as] = access_token
        _db.save_config(user_id, tokens=tokens)
    elif store_as_oauth:
        # Store full oauth_token object as a credential (supports refresh)
        oauth_token = {
            "token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": provider["token_uri"],
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": provider.get("scope", "").split(),
            "expiry": (datetime.now(timezone.utc) +
                       timedelta(seconds=token_data.get("expires_in", 3600))).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        config = _db.get_config(user_id)
        tokens = config.get("tokens", {})
        tokens[store_as_oauth] = oauth_token
        _db.save_config(user_id, tokens=tokens)
    else:
        # Standard flow: build oauth_token and store on an account
        oauth_token = {
            "token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": provider["token_uri"],
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": provider.get("scope", "").split(),
            "expiry": (datetime.now(timezone.utc) +
                       timedelta(seconds=token_data.get("expires_in", 3600))).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        if account_id:
            accounts = _db.get_server_accounts(user_id, server)
            acct = next((a for a in accounts if a["id"] == account_id), None)
            if acct:
                cfg = dict(acct["config"])
                cfg["oauth_token"] = oauth_token
                _db.update_server_account(user_id, account_id, cfg)
        else:
            template = dict(provider.get("account_template", {}))
            # Try to get email for template substitution
            email = ""
            try:
                info_resp = _requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                           headers={"Authorization": f"Bearer {access_token}"})
                if info_resp.status_code == 200:
                    email = info_resp.json().get("email", "")
            except Exception:
                pass
            if email:
                for k, v in template.items():
                    if isinstance(v, str) and "{email}" in v:
                        template[k] = v.replace("{email}", email)
            template["oauth_token"] = oauth_token
            _db.add_server_account(user_id, server, template)
    # Reconnect MCP
    with state._mcp_managers_lock:
        old = state._mcp_managers.pop(user_id, None)
    if old:
        old.stop()
    return render_template("oauth_complete.html")
