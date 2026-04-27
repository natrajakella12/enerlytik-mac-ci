"""
enerlytik RAG Knowledge Base — ChromaDB Vector Store
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import chromadb

from config_rag import (
    CHROMA_PATH,
    COLLECTION_NAMES,
    COLLECTION_WEIGHTS,
    EMBED_MODEL,
)

# LLM_PROVIDER placeholder: swap GROQ_API_KEY in .env when ready

logger = logging.getLogger(__name__)

# ── Topic auto-detection ──────────────────────────────────────────────

TOPIC_KEYWORDS = [
    "cell_spread", "cusum", "mileage", "efficiency", "tier", "cluster",
    "scoring", "model_version", "MAPE", "feature", "degradation", "LFP",
    "NMC", "NBFC", "health_score", "range", "prediction", "event",
    "thermal", "charging", "self_baseline", "physics_threshold",
    "fleet_context",
]

_BATTERY_ID_RE = re.compile(r"\bB\d{3}\b")
_VEHICLE_ID_RE = re.compile(r"\b[A-Z]{2}-\d{2}-[A-Z]{2}-\d{4}\b")


def detect_topics(text: str) -> str:
    """Scan text for domain keywords and ID patterns, return comma-separated topics."""
    text_lower = text.lower()
    found = []
    for kw in TOPIC_KEYWORDS:
        if kw.lower() in text_lower:
            found.append(kw)
    if _BATTERY_ID_RE.search(text):
        found.append("battery_id")
    if _VEHICLE_ID_RE.search(text):
        found.append("vehicle_id")
    return ",".join(sorted(set(found))) if found else ""


# ── Embedding ──────────────────────────────────────────────────────────

_st_model = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts using sentence-transformers all-MiniLM-L6-v2 (fully local)."""
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer(EMBED_MODEL)
        logger.info("Loaded sentence-transformers model: %s", EMBED_MODEL)
    embeddings = _st_model.encode(texts, show_progress_bar=False)
    return [e.tolist() for e in embeddings]


def get_embed_source() -> str:
    """Return which embedding backend is active."""
    return f"sentence_transformers/{EMBED_MODEL}"


# ── ChromaDB Store ─────────────────────────────────────────────────────

class RAGStore:
    """Persistent ChromaDB store with weighted multi-collection search."""

    def __init__(self):
        self._client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        self._collections: dict[str, chromadb.Collection] = {}
        for name in COLLECTION_NAMES:
            self._collections[name] = self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        logger.info(
            "RAGStore initialized — collections: %s",
            {n: c.count() for n, c in self._collections.items()},
        )

    def _col(self, name: str) -> chromadb.Collection:
        if name not in self._collections:
            raise ValueError(f"Unknown collection: {name}")
        return self._collections[name]

    def add_document(
        self,
        collection: str,
        text: str,
        metadata: dict,
        doc_id: Optional[str] = None,
    ) -> str:
        """Add a document to a collection. Returns the doc_id."""
        doc_id = doc_id or uuid.uuid4().hex
        meta = {
            "source": metadata.get("source", ""),
            "source_file": metadata.get("source_file", ""),
            "battery_id": metadata.get("battery_id", ""),
            "vehicle_id": metadata.get("vehicle_id", ""),
            "topics": metadata.get("topics", "") or detect_topics(text),
            "validated": metadata.get("validated", False),
            "correction_of": metadata.get("correction_of", ""),
            "timestamp": metadata.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "week_number": metadata.get("week_number", ""),
        }
        embeddings = embed_texts([text])
        self._col(collection).add(
            ids=[doc_id],
            embeddings=embeddings,
            documents=[text],
            metadatas=[meta],
        )
        return doc_id

    def query(
        self,
        question_text: str,
        collections: Optional[list[str]] = None,
        top_k: int = 5,
        battery_id_filter: Optional[str] = None,
    ) -> list[dict]:
        """Search collections with weighted scoring. Returns ranked results."""
        search_collections = collections or COLLECTION_NAMES
        q_embedding = embed_texts([question_text])
        all_results = []

        for col_name in search_collections:
            col = self._col(col_name)
            if col.count() == 0:
                continue

            where_filter = None
            if battery_id_filter:
                where_filter = {"battery_id": battery_id_filter}

            n_results = min(top_k * 2, col.count())
            try:
                results = col.query(
                    query_embeddings=q_embedding,
                    n_results=n_results,
                    where=where_filter,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception:
                # where filter may fail if no matching docs
                if battery_id_filter:
                    continue
                raise

            weight = COLLECTION_WEIGHTS.get(col_name, 1.0)
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i]
                    # ChromaDB cosine distance: 0 = identical, 2 = opposite
                    # Convert to similarity score 0-1
                    score = max(0.0, 1.0 - distance)
                    all_results.append({
                        "doc_id": doc_id,
                        "text": results["documents"][0][i],
                        "source": col_name,
                        "score": round(score, 4),
                        "weighted_score": round(score * weight, 4),
                        "metadata": results["metadatas"][0][i],
                    })

        all_results.sort(key=lambda x: x["weighted_score"], reverse=True)
        return all_results[:top_k]

    def get_by_id(self, collection: str, doc_id: str) -> Optional[dict]:
        """Retrieve a single document by ID."""
        result = self._col(collection).get(ids=[doc_id], include=["documents", "metadatas"])
        if result and result["ids"]:
            return {
                "doc_id": result["ids"][0],
                "text": result["documents"][0],
                "metadata": result["metadatas"][0],
            }
        return None

    def update_metadata(self, collection: str, doc_id: str, metadata_updates: dict) -> bool:
        """Update metadata fields for a document."""
        existing = self.get_by_id(collection, doc_id)
        if not existing:
            return False
        meta = existing["metadata"]
        meta.update(metadata_updates)
        self._col(collection).update(ids=[doc_id], metadatas=[meta])
        return True

    def count(self, collection: str) -> int:
        """Return document count for a collection."""
        return self._col(collection).count()

    def list_unvalidated(self, limit: int = 20) -> list[dict]:
        """List entries from live_captures where validated=false."""
        col = self._col("live_captures")
        if col.count() == 0:
            return []
        try:
            results = col.get(
                where={"validated": False},
                limit=limit,
                include=["documents", "metadatas"],
            )
        except Exception:
            return []
        entries = []
        if results and results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                entries.append({
                    "doc_id": doc_id,
                    "text": results["documents"][i],
                    "metadata": results["metadatas"][i],
                })
        return entries

    def collection_stats(self) -> dict:
        """Return counts and validated counts per collection."""
        stats = {}
        for name in COLLECTION_NAMES:
            col = self._col(name)
            total = col.count()
            validated_count = 0
            if total > 0:
                try:
                    v = col.get(where={"validated": True}, include=[])
                    validated_count = len(v["ids"]) if v and v["ids"] else 0
                except Exception:
                    pass
            stats[name] = {
                "count": total,
                "validated_count": validated_count,
                "validated_pct": round(validated_count / total * 100, 1) if total > 0 else 0.0,
            }
        return stats
