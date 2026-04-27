"""
enerlyst — RAG Knowledge Base Configuration
Canonical config for the enerlytik EV Intelligence Platform RAG layer.
All paths are relative to project root — works on Windows, Linux, and AWS.
"""
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── Path Resolution (portable — no hardcoded Windows paths) ──────────
ENERLYST_ROOT = Path(__file__).parent.parent.resolve()
PROJECT_ROOT = ENERLYST_ROOT.parent.resolve()

# In a PyInstaller-frozen build (Tauri sidecar) __file__ resolves into the
# temporary _MEIPASS extraction dir — never useful for locating bundled
# resources. Use the executable's directory as the bundle base.
if getattr(sys, "frozen", False):
    _BUNDLE_BASE = Path(sys.executable).parent.resolve()
else:
    _BUNDLE_BASE = PROJECT_ROOT


def _resolve_kb_path():
    """Resolve the ChromaDB persistent directory.
    Order of precedence:
      1. ENERLYTIK_KB_PATH env var (set by Tauri Rust shell or AWS start.sh)
      2. <bundle_base>/resources/data/RAG/kb (Tauri MSI install layout)
      3. <bundle_base>/../Resources/data/RAG/kb (Tauri macOS .app layout —
         binary is in Contents/MacOS/, resources in Contents/Resources/)
      4. <bundle_base>/RAG/kb (AWS layout where RAG/ ships next to the exe)
      5. <enerlyst_root>/kb (developer source tree)
    """
    env = os.environ.get("ENERLYTIK_KB_PATH")
    if env and Path(env).is_dir():
        return env
    for candidate in (
        _BUNDLE_BASE / "resources" / "data" / "RAG" / "kb",
        _BUNDLE_BASE / "resources" / "RAG" / "kb",
        _BUNDLE_BASE.parent / "Resources" / "data" / "RAG" / "kb",
        _BUNDLE_BASE.parent / "Resources" / "RAG" / "kb",
        _BUNDLE_BASE / "RAG" / "kb",
        ENERLYST_ROOT / "kb",
    ):
        if candidate.is_dir():
            return str(candidate)
    # No directory found — return the env var value if set (caller decides
    # how to handle missing dir) else the canonical dev path.
    return env or str(ENERLYST_ROOT / "kb")


# All paths derived from roots
KB_PATH = _resolve_kb_path()
DB_PATH = os.getenv("ENERLYTIK_DB_PATH",
                    str(PROJECT_ROOT / "enerlytik_production.db"))
ALERTS_DB_PATH = str(ENERLYST_ROOT / "logs" / "alerts.db")
ENV_PATH = ENERLYST_ROOT / ".env"
LOGS_PATH = str(ENERLYST_ROOT / "logs")
SOURCES_PATH = str(ENERLYST_ROOT / "sources")
INBOX_PATH = str(ENERLYST_ROOT / "ingest" / "inbox")
INBOX_PROCESSED = str(ENERLYST_ROOT / "ingest" / "inbox" / "processed")

# Legacy compatibility — RAG/ code that imports config_rag
RAG_BASE = ENERLYST_ROOT
CHROMA_PATH = Path(KB_PATH)
SEED_PATH = ENERLYST_ROOT / "sources"
PRODUCTION_DB = Path(DB_PATH)
ENV_FILE = ENV_PATH

# ── Embedding ─────────────────────────────────────────────────────────
EMBED_MODEL = "all-MiniLM-L6-v2"   # sentence-transformers, fully local

# ── LLM Provider ─────────────────────────────────────────────────────
LLM_PROVIDER = "groq"
GROQ_MODEL = "llama-3.3-70b-versatile"  # upgraded from llama3-8b-8192
GROQ_MAX_TOKENS = 1200
GROQ_TEMPERATURE = 0.2

# ── Canonical 5-Collection Schema ────────────────────────────────────
# Replaces old 4-collection schema (domain_docs/chat_exports/
# live_captures/validated_corrections) which had 88.8% invisible chunks.
#
# Migration:
#   domain_docs      -> split into physics_truths + model_intelligence
#   chat_exports     -> renamed session_knowledge
#   live_captures    -> merged into session_knowledge
#   sprint_learnings -> merged into session_knowledge
#   validated_corrections -> kept (highest trust, weight 3.0)
#   physics_truths   -> new (from rebuild, now registered)
#   model_intelligence -> new (from rebuild, now registered)
#   audience_outputs -> new (from rebuild, now registered)

COLLECTIONS = {
    "physics_truths": {
        "weight": 2.0,
        "description": "LFP/NMC physics constants, chemistry rules, validated equations",
        "sources": ["sources/physics/"],
    },
    "validated_corrections": {
        "weight": 3.0,
        "description": "C01-C20 validated corrections, highest trust layer",
        "sources": ["sources/validated/"],
    },
    "explanations": {
        "weight": 2.5,
        "description": "Audience-routed term glossary, FAQ answers, platform explanations",
        "sources": ["sources/explanations/"],
    },
    "model_intelligence": {
        "weight": 0.8,
        "description": "Model catalogue, scoring architecture, platform docs",
        "sources": ["sources/platform/"],
    },
    "audience_outputs": {
        "weight": 1.5,
        "description": "Operator/NBFC/OEM sentence patterns, audience routing",
        "sources": ["sources/audience/"],
    },
    "session_knowledge": {
        "weight": 1.8,
        "description": "Chat exports, sprint Q&A, investigation findings, live per-battery intel",
        "sources": ["sources/sessions/"],
    },
}

# Flat lists for backward compatibility with rag_store.py
COLLECTION_NAMES = list(COLLECTIONS.keys())
COLLECTION_WEIGHTS = {k: v["weight"] for k, v in COLLECTIONS.items()}

# ── Chunking ──────────────────────────────────────────────────────────
CHUNK_SIZE_CHARS = 1600       # ~400 tokens
CHUNK_OVERLAP_CHARS = 200     # ~50 tokens

# ── API ───────────────────────────────────────────────────────────────
API_PORT = 8001

# ── Rate Limits ───────────────────────────────────────────────────────
RATE_LIMIT_QUERIES_PER_HOUR = 30
RATE_LIMIT_LLM_PER_MINUTE = 5


# ── API Token Management ─────────────────────────────────────────────
def ensure_api_token() -> str:
    """Load or generate the RAG API token. Never overwrites existing .env."""
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
        token = os.getenv("RAG_API_TOKEN") or os.getenv("ENERLYST_API_TOKEN")
        if token:
            return token

    # Generate new token
    token = secrets.token_hex(16)
    ENV_PATH.write_text(f"ENERLYST_API_TOKEN={token}\n", encoding="utf-8")
    return token


def load_api_token() -> str:
    """Load the API token from .env (must already exist)."""
    load_dotenv(ENV_PATH)
    token = os.getenv("RAG_API_TOKEN") or os.getenv("ENERLYST_API_TOKEN", "")
    if not token:
        raise RuntimeError(f"ENERLYST_API_TOKEN not found in {ENV_PATH}")
    return token
