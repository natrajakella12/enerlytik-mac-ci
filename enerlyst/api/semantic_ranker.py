# Extracted from enerlytik-studio/ March 30 2026
# Paths and schema references are STALE — update before use
# composite_score_v2 -> operational_score | 183 batteries -> 222
"""
semantic_ranker.py — Semantic file selection using sentence-transformers.

On first call, embeds all indexable files and caches to models/file_embeddings.pkl.
On subsequent calls, loads from cache (fast).
"""

import os
import re
import pickle
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer

BASE_DIR    = Path("C:/Users/Admin/Desktop/Ev__ML")
CACHE_PATH  = Path(__file__).parent.parent / "models" / "file_embeddings.pkl"
MODEL_NAME  = "all-MiniLM-L6-v2"

INCLUDE_EXTENSIONS = {".py", ".md", ".txt"}
EXCLUDE_DIRS = {
    "__pycache__", ".git", "archive", "2kWh_Field data",
    "output", "output_batch50", "output_batch2", "data", "figures",
    "models", "schema", "sql", "utils",
}

MAX_FILE_PREVIEW = 2000   # chars per file for embedding representation
MAX_RETURN       = 10     # max files to return

_model      = None
_cache      = None   # dict: {rel_path: {"embedding": np.array, "preview": str}}


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _collect_files() -> list[Path]:
    files = []
    for root, dirs, filenames in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fname in filenames:
            p = Path(root) / fname
            if p.suffix.lower() in INCLUDE_EXTENSIONS:
                files.append(p)
    return sorted(files)


def _file_preview(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:MAX_FILE_PREVIEW]
    except Exception:
        return ""


def build_cache(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    if CACHE_PATH.exists() and not force:
        with open(CACHE_PATH, "rb") as f:
            _cache = pickle.load(f)
        return _cache

    model  = _get_model()
    files  = _collect_files()
    result = {}

    previews  = []
    rel_paths = []
    for p in files:
        preview = _file_preview(p)
        if preview.strip():
            rel_paths.append(str(p.relative_to(BASE_DIR)).replace("\\", "/"))
            previews.append(preview)

    embeddings = model.encode(previews, show_progress_bar=False, normalize_embeddings=True)

    for rel, emb, preview in zip(rel_paths, embeddings, previews):
        result[rel] = {"embedding": emb, "preview": preview[:500]}

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(result, f)

    _cache = result
    return _cache


def rank_files(question: str, top_n: int = MAX_RETURN) -> list[dict]:
    """
    Returns list of {filepath, score, preview} sorted by semantic similarity desc.
    """
    cache = build_cache()
    model = _get_model()

    q_emb = model.encode([question], normalize_embeddings=True)[0]

    scores = []
    for rel_path, data in cache.items():
        sim = float(np.dot(q_emb, data["embedding"]))
        scores.append({"filepath": rel_path, "score": round(sim, 4), "preview": data["preview"]})

    scores.sort(key=lambda x: -x["score"])

    # Always ensure CLAUDE.md and PROGRESS.md are in top results if not already
    top_paths = {s["filepath"] for s in scores[:top_n]}
    anchors   = ["CLAUDE.md", "PROGRESS.md"]
    for anchor in anchors:
        if anchor not in top_paths and anchor in cache:
            scores.append({"filepath": anchor, "score": 0.5, "preview": cache[anchor]["preview"]})

    return scores[:top_n]


def invalidate_cache():
    global _cache
    _cache = None
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()


class SemanticRanker:
    """Class-based interface wrapping module-level functions."""

    def build_cache(self, force: bool = False) -> dict:
        return build_cache(force=force)

    def rank_files(self, question: str, top_k: int = MAX_RETURN) -> list[dict]:
        return rank_files(question, top_n=top_k)

    def invalidate_cache(self):
        invalidate_cache()

    def stats(self) -> dict:
        cache = build_cache()
        return {"files_indexed": len(cache), "cache_path": str(CACHE_PATH)}
