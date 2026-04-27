"""
enerlytik — Centralized Environment Configuration

All hardcoded paths and ports read from env vars with fallbacks
to current local values. No .env file needed for local dev.

Usage:
    from config.env_config import ENERLYTIK_DB_PATH, DB_API_PORT
"""
import os
from pathlib import Path

# Load RAG/.env first (primary token store), then root .env if present.
# override=False means OS env vars always win over .env file values, so a
# value exported in start_v2.bat or AWS env beats whatever the .env files
# happen to contain.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent.parent / 'RAG' / '.env',
                 override=False)
    _load_dotenv(Path(__file__).parent.parent / '.env',
                 override=False)
except ImportError:
    pass  # python-dotenv missing — env vars must come from OS shell

# ── Base directory ────────────────────────────────────────────────
ENERLYTIK_BASE_DIR = Path(os.getenv(
    "ENERLYTIK_BASE_DIR",
    r"C:\Users\Admin\Desktop\Ev__ML"
))

# ── Database paths ────────────────────────────────────────────────
ENERLYTIK_DB_PATH = Path(os.getenv(
    "ENERLYTIK_DB_PATH",
    str(ENERLYTIK_BASE_DIR / "enerlytik_production.db")
))

ENERLYTIK_DUCKDB_PATH = Path(os.getenv(
    "ENERLYTIK_DUCKDB_PATH",
    r"D:\enerlytik_30sec.duckdb"
))

# ── Directory paths ───────────────────────────────────────────────
ENERLYTIK_RAG_DIR = Path(os.getenv(
    "ENERLYTIK_RAG_DIR",
    str(ENERLYTIK_BASE_DIR / "RAG")
))

ENERLYTIK_MODELS_DIR = Path(os.getenv(
    "ENERLYTIK_MODELS_DIR",
    str(ENERLYTIK_BASE_DIR / "models")
))

ENERLYTIK_DOCS_DIR = Path(os.getenv(
    "ENERLYTIK_DOCS_DIR",
    str(ENERLYTIK_BASE_DIR / "docs")
))

ENERLYTIK_OUTPUT_DIR = Path(os.getenv(
    "ENERLYTIK_OUTPUT_DIR",
    str(ENERLYTIK_BASE_DIR / "output")
))

ENERLYTIK_PASSPORT_DIR = Path(os.getenv(
    "ENERLYTIK_PASSPORT_DIR",
    str(ENERLYTIK_BASE_DIR / "passport_v2")
))

ENERLYTIK_ENERLYST_UI_DIR = Path(os.getenv(
    "ENERLYTIK_ENERLYST_UI_DIR",
    str(ENERLYTIK_BASE_DIR / "enerlyst" / "ui")
))

# ── Service URLs and ports ────────────────────────────────────────
DB_API_URL = os.getenv("DB_API_URL", "http://localhost:3001")
RAG_API_URL = os.getenv("RAG_API_URL", "http://localhost:8001")
WEB_PORT = int(os.getenv("WEB_PORT", "5001"))
DB_API_PORT = int(os.getenv("DB_API_PORT", "3001"))
RAG_API_PORT = int(os.getenv("RAG_API_PORT", "8001"))
ENERLYST_PORT = int(os.getenv("ENERLYST_PORT", "8002"))

# ── API token ─────────────────────────────────────────────────────
# AWS-prep: dev-fallback token is in handover docs and therefore
# compromised. Rotate before any external-facing deployment.
_DEV_TOKEN = "{{ api_token }}"
RAG_API_TOKEN = (os.getenv("RAG_API_TOKEN")
                 or os.getenv("ENERLYTIK_API_TOKEN")
                 or _DEV_TOKEN)
if RAG_API_TOKEN == _DEV_TOKEN:
    import warnings
    warnings.warn(
        "Using dev fallback API token — rotate ENERLYTIK_API_TOKEN before AWS deployment",
        RuntimeWarning, stacklevel=2)
