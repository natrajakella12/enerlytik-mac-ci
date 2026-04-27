"""
ENERLYTIK — Diagnostic Dashboard API Server
Serves battery_diagnostics, event_chains, and health scores.
Also serves static files from output/ directory.

Run: python -X utf8 passport_server.py
Endpoints: http://localhost:5001
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import requests as http_requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from config.env_config import (
    ENERLYTIK_DB_PATH, ENERLYTIK_OUTPUT_DIR, ENERLYTIK_RAG_DIR,
    ENERLYTIK_PASSPORT_DIR, ENERLYTIK_ENERLYST_UI_DIR, WEB_PORT,
)

# ── PyInstaller / Tauri sidecar path resolution ────────────────────────
# When frozen by PyInstaller, __file__ resolves into sys._MEIPASS (a temp
# extraction dir) where the bundled HTML files do not live. Use the
# executable's directory as the base, and let the Rust shell / start.sh
# override exact locations via env vars.
if getattr(sys, "frozen", False):
    _BUNDLE_BASE = Path(sys.executable).parent.resolve()
else:
    _BUNDLE_BASE = Path(__file__).parent.resolve()


def _resolve_passport_dir():
    """Resolve the directory containing passport_v2 HTML files.
    Env var (set by Rust shell at launch) wins; otherwise probe layouts:
      - Tauri Windows MSI: <install>/resources/passport_v2/
      - Tauri macOS .app : <App>.app/Contents/Resources/passport_v2/
        (binary lives in Contents/MacOS/, so go ../Resources/)
      - Dev / AWS Linux  : <bundle_base>/passport_v2/
    """
    env = os.environ.get("ENERLYTIK_PASSPORT_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    for candidate in (
        _BUNDLE_BASE / "resources" / "passport_v2",
        _BUNDLE_BASE.parent / "Resources" / "resources" / "passport_v2",
        _BUNDLE_BASE.parent / "Resources" / "passport_v2",
        _BUNDLE_BASE / "passport_v2",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    return Path(ENERLYTIK_PASSPORT_DIR)


def _resolve_html(env_var, filename, *, default_subdir="passport_v2"):
    """Return an absolute path to a bundled HTML file.
    Honours `env_var` first; otherwise looks under the resolved passport
    directory; finally falls back to the executable / source root.
    """
    env = os.environ.get(env_var)
    if env and Path(env).is_file():
        return Path(env)
    candidate = _resolve_passport_dir() / filename
    if candidate.is_file():
        return candidate
    if default_subdir:
        candidate = _BUNDLE_BASE / default_subdir / filename
        if candidate.is_file():
            return candidate
        candidate = _BUNDLE_BASE.parent / "Resources" / default_subdir / filename
        if candidate.is_file():
            return candidate
    candidate = _BUNDLE_BASE / filename
    if candidate.is_file():
        return candidate
    candidate = _BUNDLE_BASE.parent / "Resources" / filename
    return candidate


# Override the static import with the frozen-aware resolution.
ENERLYTIK_PASSPORT_DIR = _resolve_passport_dir()

# AWS-prep: dev-fallback API token. WARNING — this token appears in
# handover docs and is therefore compromised. Rotate ENERLYTIK_API_TOKEN
# before any external-facing deployment. Token is visible in browser
# page source — acceptable for demo, not for production. Use short-lived
# tokens in production.
_API_TOKEN_DEV_FALLBACK = "{{ api_token }}"


def _api_token():
    return os.environ.get("ENERLYTIK_API_TOKEN", _API_TOKEN_DEV_FALLBACK)


def _render_html_path(html_path):
    """Read HTML from an absolute path and substitute placeholders.
    Companion to _render_html_with_token for cases where the caller has
    already resolved the file location (e.g. via ENERLYTIK_PASSPORT_HTML).
    """
    try:
        with open(str(html_path), "r", encoding="utf-8") as fh:
            html = fh.read()
    except OSError as e:
        return Response(f"Failed to read {html_path}: {e}", status=500)
    html = html.replace("{{ api_token }}", _api_token())
    demo_mode = os.environ.get("ENERLYTIK_DEMO_MODE", "false")
    html = html.replace("{{ demo_mode }}", demo_mode)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _render_html_with_token(directory, filename):
    """Read HTML and substitute {{ api_token }} placeholder server-side.
    Avoids full Jinja2 rendering so JS template literals (`${var}`) and
    any stray double-braces in scripts cannot trigger UndefinedError.

    No-cache headers applied centrally so every HTML route served via
    this helper (enerlytik_v4, oem_v3, cockpit, ...) defeats browser
    caching of stale UI between deploys.
    """
    path = os.path.join(str(directory), filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
    except OSError as e:
        return Response(f"Failed to read {filename}: {e}", status=500)
    html = html.replace("{{ api_token }}", _api_token())
    demo_mode = os.environ.get("ENERLYTIK_DEMO_MODE", "false")
    html = html.replace("{{ demo_mode }}", demo_mode)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

ENV = os.environ.get("ENV", "dev").lower()

DB = str(ENERLYTIK_DB_PATH)
STATIC_DIR = ENERLYTIK_OUTPUT_DIR
RAG_DIR = ENERLYTIK_RAG_DIR

app = Flask(__name__, static_folder=str(STATIC_DIR))

from RAG.loan_approval_api import loan_bp
app.register_blueprint(loan_bp)


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_json_field(val):
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


# ── Static files ──────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/")
def index():
    return redirect("/enerlytik-v4", code=302)


@app.route("/enerlytik-v4")
def enerlytik_v4_page():
    # AWS-prep: server-side token injection so the HTML file can stay
    # token-agnostic in the repo. Path resolved with frozen-aware lookup
    # (ENERLYTIK_PASSPORT_HTML env var → resolved passport dir → fallback).
    return _render_html_path(_resolve_html(
        "ENERLYTIK_PASSPORT_HTML", "enerlytik_v4.html"))


@app.route("/service-intelligence")
def service_intelligence_page():
    return send_from_directory(
        str(ENERLYTIK_PASSPORT_DIR),
        "service_intelligence.html",
    )


@app.route("/enerlyst-next")
def enerlyst_next_page():
    """Legacy enerlyst UI — kept for rollback (Sprint D)."""
    return send_from_directory(
        str(ENERLYTIK_ENERLYST_UI_DIR),
        "external.html",
    )


@app.route("/oem-v3")
def oem_v3_page():
    """Sprint 3/4/5 OEM v3 surface — served from repo root (not passport_v2/)."""
    return _render_html_path(_resolve_html(
        "ENERLYTIK_OEM_V3_HTML", "oem_v3.html",
        default_subdir=""))


@app.route("/cockpit")
def cockpit_page():
    return _render_html_with_token(ENERLYTIK_PASSPORT_DIR, "cockpit.html")


@app.route("/assets/<path:filename>")
def passport_assets(filename):
    """Static asset route for passport_v2/assets/ (logo, icons, etc).
    HTML pages served by /enerlytik-v4 use src="assets/..." which the
    browser resolves to /assets/... — this route serves them."""
    return send_from_directory(
        str(ENERLYTIK_PASSPORT_DIR / "assets"),
        filename,
    )


# ── Proxy: /api/* → DB API on port 3001 ──────────────
DB_API_PORT = os.environ.get("DB_API_PORT", "3001")
RAG_API_PORT = os.environ.get("RAG_API_PORT", "8001")
# AWS-prep: support split-host deployment via DB_API_HOST / RAG_API_HOST.
_DB_HOST  = os.environ.get("DB_API_HOST",  "localhost")
_RAG_HOST = os.environ.get("RAG_API_HOST", "localhost")
ENERLYST_API_TOKEN = os.environ.get("ENERLYST_API_TOKEN", "natrajthegreat")

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]


@app.route("/api/enerlyst/<path:path>", methods=["GET", "POST"])
def proxy_enerlyst(path):
    """Forward /api/enerlyst/* to the real enerlyst API on port 8001.
    Injects the enerlyst bearer server-side so the browser never holds it.
    Normalises legacy embeds that send `query` instead of `question`.
    """
    url = f"http://{_RAG_HOST}:{RAG_API_PORT}/enerlyst/{path}"
    headers = {"Authorization": f"Bearer {ENERLYST_API_TOKEN}"}
    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if "question" not in body and "query" in body:
                body["question"] = body.pop("query")
            if "question" not in body and "message" in body:
                body["question"] = body.pop("message")
            body.setdefault("audience", "operator")
            resp = http_requests.post(
                url, headers=headers, params=request.args,
                json=body, timeout=60)
        else:
            resp = http_requests.get(
                url, headers=headers, params=request.args, timeout=30)
        return resp.content, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": "enerlyst_unavailable", "detail": str(e)}), 502


@app.route("/api/<path:path>", methods=_PROXY_METHODS)
def proxy_api(path):
    """Forward /api/* requests to FastAPI DB API.
    Methods extended (AWS-prep): GET, POST, PUT, DELETE, PATCH, OPTIONS.
    """
    url = f"http://{_DB_HOST}:{DB_API_PORT}/api/{path}"
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    try:
        if request.method == "GET":
            resp = http_requests.get(
                url, headers=headers, params=request.args, timeout=30)
        elif request.method == "OPTIONS":
            resp = http_requests.options(
                url, headers=headers, params=request.args, timeout=10)
        else:
            resp = http_requests.request(
                method=request.method, url=url, headers=headers,
                params=request.args, data=request.get_data(),
                timeout=30, allow_redirects=False)
        return resp.content, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/static/<path:filename>")
def static_files(filename):
    # Try RAG dir first (logo), then output dir
    rag_file = RAG_DIR / filename
    if rag_file.exists():
        return send_from_directory(str(RAG_DIR), filename)
    return send_from_directory(str(STATIC_DIR), filename)


# ── API: Fleet Summary ───────────────────────────
@app.route("/api/fleet/summary")
def fleet_summary():
    """Forward to db_api — real proxy, no local schema."""
    url = f"http://{_DB_HOST}:{DB_API_PORT}/api/fleet/summary"
    headers = {"Authorization": f"Bearer {_api_token()}"}
    try:
        resp = http_requests.get(
            url, headers=headers,
            params=request.args, timeout=30)
        return resp.content, resp.status_code, \
            {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── API: Battery Diagnostic ──────────────────────
@app.route("/api/battery/<battery_id>/diagnostic")
def battery_diagnostic(battery_id):
    """Forward to db_api — real proxy, no local schema."""
    url = f"http://{_DB_HOST}:{DB_API_PORT}/api/battery/{battery_id}/diagnostic"
    headers = {"Authorization": f"Bearer {_api_token()}"}
    try:
        resp = http_requests.get(
            url, headers=headers,
            params=request.args, timeout=30)
        return resp.content, resp.status_code, \
            {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── API: Fleet Chains ────────────────────────────
@app.route("/api/fleet/chains")
def fleet_chains():
    """Forward to db_api — real proxy, no local schema."""
    url = f"http://{_DB_HOST}:{DB_API_PORT}/api/fleet/chains"
    headers = {"Authorization": f"Bearer {_api_token()}"}
    try:
        resp = http_requests.get(
            url, headers=headers,
            params=request.args, timeout=30)
        return resp.content, resp.status_code, \
            {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── API: Battery list (for sidebar) ──────────────
@app.route("/api/fleet/batteries")
def fleet_batteries():
    """Forward to db_api — real proxy, no local schema."""
    url = f"http://{_DB_HOST}:{DB_API_PORT}/api/fleet/batteries"
    headers = {"Authorization": f"Bearer {_api_token()}"}
    try:
        resp = http_requests.get(
            url, headers=headers,
            params=request.args, timeout=30)
        return resp.content, resp.status_code, \
            {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if ENV == "dev":
        print("*** DEV MODE ***")
    print(f"enerlytik Passport Server  [ENV={ENV}]")
    print(f"  http://localhost:{WEB_PORT}/enerlytik-v4")
    print("  API: /api/fleet/summary, /api/battery/<id>/diagnostic, /api/fleet/chains")
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
