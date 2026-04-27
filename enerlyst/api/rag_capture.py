"""
enerlytik RAG Knowledge Base — Live interaction capture
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from config_rag import LOGS_PATH
from rag_store import RAGStore, detect_topics

logger = logging.getLogger(__name__)


def capture_interaction(
    question: str,
    answer: str,
    battery_id: str = "",
    vehicle_id: str = "",
    source: str = "api",
) -> str:
    """
    Capture a live Q&A interaction into the RAG knowledge base.

    1. Store in 'live_captures' collection (validated=False)
    2. Append to daily JSONL log
    3. Return entry_id
    """
    store = RAGStore()
    entry_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    combined = f"Q: {question}\n\nA: {answer}"
    topics = detect_topics(combined)

    # Store in ChromaDB
    store.add_document(
        collection="live_captures",
        text=combined,
        metadata={
            "source": "live_capture",
            "source_file": f"api_{source}",
            "battery_id": battery_id,
            "vehicle_id": vehicle_id,
            "topics": topics,
            "validated": False,
            "timestamp": now.isoformat(),
        },
        doc_id=entry_id,
    )

    # Append to daily JSONL log
    LOGS_PATH.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_PATH / f"{now.strftime('%Y-%m-%d')}.jsonl"
    log_entry = {
        "entry_id": entry_id,
        "timestamp": now.isoformat(),
        "question": question,
        "answer": answer,
        "battery_id": battery_id,
        "vehicle_id": vehicle_id,
        "topics": topics,
        "source": source,
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    logger.info("Captured interaction %s (topics: %s)", entry_id, topics)
    return entry_id
