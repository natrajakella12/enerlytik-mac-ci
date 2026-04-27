"""
enerlytik RAG Knowledge Base — FastAPI Server
"""
import base64
import json
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config_rag
from config_rag import ensure_api_token
LOGS_PATH = Path(config_rag.LOGS_PATH)
from guardrails import assess_confidence, check_scope, filter_response
from rag_capture import capture_interaction
from rag_store import RAGStore, detect_topics, get_embed_source

# Ensure token exists on import
_API_TOKEN = ensure_api_token()

# ── Groq keys — 4-key round-robin, server-side only ──────────────
import itertools as _itertools
import json as _json

_ENV_FILE = config_rag.ENV_PATH if hasattr(config_rag, 'ENV_PATH') else Path("enerlyst/.env")
if _ENV_FILE.exists():
    from dotenv import load_dotenv as _ld
    _ld(_ENV_FILE)

# Collect all available keys (GROQ_KEY_1..4 + legacy GROQ_API_KEY)
_GROQ_KEYS = [
    os.getenv("GROQ_KEY_1"),
    os.getenv("GROQ_KEY_2"),
    os.getenv("GROQ_KEY_3"),
    os.getenv("GROQ_KEY_4"),
    os.getenv("GROQ_API_KEY"),
]
_GROQ_KEYS = [k for k in _GROQ_KEYS if k and k != "your_key_here"]
_key_cycle = _itertools.cycle(_GROQ_KEYS) if _GROQ_KEYS else None

def _get_groq_key() -> str:
    """Round-robin key selection. Raises if no keys available."""
    if not _key_cycle:
        raise RuntimeError("No Groq keys found. Set GROQ_KEY_1 in enerlyst/.env")
    return next(_key_cycle)

_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "1200"))

def _log_query(key_index: int, model: str, status: int, tokens: int = 0):
    """Append query metadata to logs/query_log.jsonl."""
    try:
        import datetime
        log_path = Path(config_rag.LOGS_PATH) / "query_log.jsonl" if hasattr(config_rag, 'LOGS_PATH') else Path("enerlyst/logs/query_log.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.datetime.utcnow().isoformat(), "key": key_index, "model": model, "status": status, "tokens": tokens}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never break queries

# ── Rate limiting — in-memory per session ──────────────────────────
_rate_query: dict[str, list[float]] = defaultdict(list)  # session_id -> [timestamps]
_rate_llm: dict[str, list[float]] = defaultdict(list)
_QUERY_LIMIT = 30   # per hour
_LLM_LIMIT = 5      # per minute

def _check_rate(bucket: dict, session_id: str, limit: int, window_sec: int) -> int | None:
    """Returns None if OK, or seconds to retry if limited."""
    now = time.time()
    times = bucket[session_id]
    # Purge expired
    times[:] = [t for t in times if now - t < window_sec]
    if len(times) >= limit:
        retry_after = int(window_sec - (now - times[0])) + 1
        return max(retry_after, 1)
    times.append(now)
    return None

app = FastAPI(title="enerlytik RAG API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_store: RAGStore | None = None

# Log files — ensure all exist at startup
LOGS_PATH.mkdir(parents=True, exist_ok=True)
for _logname in ["usage_log", "feedback_log", "filter_log", "unanswered_questions",
                  "training_pairs", "outcome_log", "scheduler_log", "llm_perf_log"]:
    _lp = LOGS_PATH / f"{_logname}.jsonl"
    if not _lp.exists():
        _lp.touch()

_FILTER_LOG = LOGS_PATH / "filter_log.jsonl"
_UNANSWERED_LOG = LOGS_PATH / "unanswered_questions.jsonl"
_FEEDBACK_LOG = LOGS_PATH / "feedback_log.jsonl"
_TRAINING_PAIRS = LOGS_PATH / "training_pairs.jsonl"
_USAGE_LOG = LOGS_PATH / "usage_log.jsonl"


def get_store() -> RAGStore:
    global _store
    if _store is None:
        _store = RAGStore()
    return _store


def verify_token(authorization: Optional[str] = Header(None)):
    """Verify Bearer token."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != _API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")


def _log_jsonl(path, entry: dict):
    """Append a JSON line to a log file."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Models ─────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    battery_id: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=50)
    db_context: Optional[dict] = None
    session_id: str = ""


class CaptureRequest(BaseModel):
    question: str
    answer: str
    battery_id: Optional[str] = None
    vehicle_id: Optional[str] = None
    source: str = "api"


class FeedbackRequest(BaseModel):
    entry_id: str
    signal: str  # "positive" | "negative" | "correction"
    question: str = ""
    answer: str = ""
    battery_id: Optional[str] = None
    comment: Optional[str] = None
    correction_text: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────────────

@app.get("/health")
def health(store: RAGStore = Depends(get_store)):
    """Health check — no auth required."""
    collections = {}
    # Use the current 6-collection layout from config_rag (the pre-refactor
    # names domain_docs / chat_exports / live_captures no longer exist).
    for name in config_rag.COLLECTION_NAMES:
        try:
            collections[name] = store.count(name)
        except Exception as e:
            collections[name] = f"error: {e}"
    return {
        "status": "ok",
        "collections": collections,
        "embed_source": get_embed_source(),
    }


# ── LLM proxy — Groq key never touches browser ──────────────────────

class LLMQueryRequest(BaseModel):
    messages: list[dict]
    system: str = ""
    max_tokens: int = Field(default=800, le=2000)
    session_id: str = ""
    diagnostic_context: Optional[dict] = None


@app.get("/config/llm")
def config_llm(_=Depends(verify_token)):
    """Returns LLM availability. Never returns the key."""
    return {
        "groq_model": _GROQ_MODEL,
        "groq_available": bool(_GROQ_KEYS),
        "groq_keys_count": len(_GROQ_KEYS),
    }


def _build_diagnostic_section(dc: dict) -> str:
    """Build structured diagnostic text for LLM system prompt."""
    if not dc:
        return ""

    lines = ["=== DIAGNOSTIC ENGINE OUTPUT (authoritative - use this before RAG chunks) ==="]

    bid = dc.get("battery_id", "?")
    tier = dc.get("tier", "?")
    score = dc.get("adjusted_composite")
    score_str = f"{score:.1f}" if score is not None else "?"
    lines.append(f"Battery: {bid} | Tier: {tier} | Score: {score_str}/100")

    ts = dc.get("trend_summary") or {}
    if isinstance(ts, str):
        try:
            ts = json.loads(ts)
        except (json.JSONDecodeError, TypeError):
            ts = {}
    direction = ts.get("direction", "?")
    wtb = ts.get("weeks_to_breach")
    lines.append(f"Trend: {direction} | Weeks to threshold: {wtb if wtb is not None else 'N/A'}")

    rd = dc.get("range_deviation_attribution") or {}
    if isinstance(rd, str):
        try:
            rd = json.loads(rd)
        except (json.JSONDecodeError, TypeError):
            rd = {}
    dev = rd.get("deviation_pct", "?")
    lines.append(f"\nRANGE: deviation {dev}%")
    lines.append(f"  Battery degradation: {rd.get('battery_pct', '?')}% | Driver behaviour: {rd.get('driver_pct', '?')}% | Route/load: {rd.get('route_pct', '?')}%")
    lines.append(f"  Confidence: {rd.get('confidence', '?')}")

    shap = dc.get("shap_attribution") or {}
    if isinstance(shap, str):
        try:
            shap = json.loads(shap)
        except (json.JSONDecodeError, TypeError):
            shap = {}
    features = shap.get("features", [])
    if features:
        lines.append("\nTOP SHAP FEATURES (why the score is what it is):")
        for f in features[:3]:
            fname = f.get("feature", "?")
            val = f.get("actual_value", "?")
            direction_f = f.get("direction", "?")
            impact = f.get("impact_km", "?")
            lines.append(f"  {fname} ({val}): {direction_f} {impact}km")

    fp = dc.get("failure_probabilities") or {}
    if isinstance(fp, str):
        try:
            fp = json.loads(fp)
        except (json.JSONDecodeError, TypeError):
            fp = {}
    if fp and "status" not in fp:
        lines.append("\nFAILURE PROBABILITIES:")
        lines.append(f"  Cell failure 4w: {fp.get('cell_failure_4w', '?')}")
        lines.append(f"  BMS shutdown 8w: {fp.get('bms_shutdown_8w', '?')}")
        lines.append(f"  Thermal risk 4w: {fp.get('thermal_risk_4w', '?')}")
        lines.append(f"  Degradation 12w: {fp.get('degradation_12w', '?')}")
        lines.append(f"  Confidence: {fp.get('confidence', '?')}")

    recs = dc.get("recommendations") or []
    if isinstance(recs, str):
        try:
            recs = json.loads(recs)
        except (json.JSONDecodeError, TypeError):
            recs = []
    if recs:
        lines.append("\nRECOMMENDATIONS (top 2):")
        for r in recs[:2]:
            lines.append(f"  {r.get('urgency', '?')}: {r.get('action', '?')}")

    chains = dc.get("chains") or []
    if chains:
        ch = chains[0]
        lines.append(f"\nEVENT CHAIN: {ch.get('pattern_name', '?')} - {ch.get('operator_action', '')[:150]}")

    caf = dc.get("cohort_anomaly_flag", 0)
    if caf:
        lines.append(f"\nCOHORT: OUTLIER: {dc.get('cohort_anomaly_reason', '?')}")
    else:
        lines.append("\nCOHORT: Within normal cohort range")

    lines.append("\nPERSONA SUMMARIES:")
    fs = dc.get("fleet_summary", "")
    if isinstance(fs, str) and fs:
        lines.append(f"  Fleet: {fs}")
    ns = dc.get("nbfc_summary", "")
    if isinstance(ns, dict):
        lines.append(f"  NBFC: risk_score={ns.get('risk_score')}, flag={ns.get('loan_flag')}, reason={ns.get('reason', '')[:100]}")
    elif isinstance(ns, str) and ns:
        lines.append(f"  NBFC: {ns[:200]}")

    lines.append("=== END DIAGNOSTIC ENGINE OUTPUT ===")
    return "\n".join(lines)


@app.post("/llm/query")
async def llm_query(req: LLMQueryRequest, _=Depends(verify_token)):
    """Proxy Groq call. Key stays server-side. Round-robin across available keys."""
    if not _GROQ_KEYS:
        raise HTTPException(503, "No Groq keys configured. Set GROQ_KEY_1 in enerlyst/.env")

    # Rate limit: 5 Groq calls per minute per session
    sid = req.session_id or "default"
    retry = _check_rate(_rate_llm, sid, _LLM_LIMIT, 60)
    if retry is not None:
        return {"error": "rate_limit", "retry_after": retry,
                "message": f"Slow down \u2014 {retry} seconds before next query"}

    # Inject diagnostic context into system prompt if present
    system_content = req.system or ""
    if req.diagnostic_context:
        diag_section = _build_diagnostic_section(req.diagnostic_context)
        if diag_section:
            # Insert between DB CONTEXT and RAG CONTEXT
            if "RAG CONTEXT:" in system_content:
                parts = system_content.split("RAG CONTEXT:", 1)
                system_content = parts[0] + "\n" + diag_section + "\n\nRAG CONTEXT:" + parts[1]
            else:
                system_content += "\n\n" + diag_section

    msgs = req.messages
    if system_content:
        msgs = [{"role": "system", "content": system_content}] + msgs

    max_retries = min(len(_GROQ_KEYS), 3)
    last_error = None
    for attempt in range(max_retries):
        key = _get_groq_key()
        key_index = _GROQ_KEYS.index(key) if key in _GROQ_KEYS else -1
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(_GROQ_URL, json={
                    "model": _GROQ_MODEL,
                    "messages": msgs,
                    "max_tokens": req.max_tokens or _GROQ_MAX_TOKENS,
                    "temperature": 0.2,
                }, headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                })
            if resp.status_code == 429 and attempt < max_retries - 1:
                _log_query(key_index, _GROQ_MODEL, 429)
                continue  # retry with next key
            if resp.status_code != 200:
                _log_query(key_index, _GROQ_MODEL, resp.status_code)
                return {"error": "groq_error", "status": resp.status_code,
                        "detail": resp.text[:200]}
            data = resp.json()
            content = ""
            if data.get("choices") and data["choices"][0].get("message"):
                content = data["choices"][0]["message"].get("content", "")
            tokens = data.get("usage", {}).get("total_tokens", 0)
            _log_query(key_index, _GROQ_MODEL, 200, tokens)
            return {"content": content, "model": _GROQ_MODEL, "usage": data.get("usage")}
        except Exception as e:
            last_error = str(e)[:200]
            _log_query(key_index, _GROQ_MODEL, 0)
            if attempt < max_retries - 1:
                continue
    return {"error": "groq_error", "detail": last_error or "all keys exhausted"}


@app.post("/query")
def query(req: QueryRequest, store: RAGStore = Depends(get_store), _=Depends(verify_token)):
    """Query the knowledge base with scope checking and confidence assessment."""
    query_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    # Rate limit: 30 queries per hour per session
    sid = req.session_id or "default"
    retry = _check_rate(_rate_query, sid, _QUERY_LIMIT, 3600)
    if retry is not None:
        return {"error": "rate_limit", "retry_after": retry,
                "message": f"Rate limit reached \u2014 {retry}s before next query"}

    # Step 1 — Scope check
    scope = check_scope(req.question)
    if not scope["allowed"]:
        _log_jsonl(_FILTER_LOG, {
            "timestamp": now,
            "question": req.question,
            "reason": "scope_block",
            "detail": scope["reason"],
            "query_id": query_id,
        })
        return {
            "type": "blocked",
            "message": scope["redirect"],
            "query_id": query_id,
        }

    # Step 2 — RAG query (existing logic)
    rag_results = store.query(
        question_text=req.question,
        top_k=req.top_k,
        battery_id_filter=req.battery_id,
    )

    # Step 3 — Confidence assessment
    confidence = assess_confidence(
        question=req.question,
        rag_results=rag_results,
        db_data=req.db_context,
    )

    if not confidence["should_answer"]:
        # Log unanswered question for knowledge gap tracking
        _log_jsonl(_UNANSWERED_LOG, {
            "timestamp": now,
            "question": req.question,
            "battery_id": req.battery_id or "",
            "confidence": confidence["confidence"],
            "reason": "low_confidence",
            "query_id": query_id,
        })
        return {
            "type": "insufficient",
            "message": confidence["caveat"],
            "partial_results": [
                {
                    "text": rag_results[0]["text"],
                    "source": rag_results[0]["source"],
                    "weighted_score": rag_results[0]["weighted_score"],
                }
            ] if rag_results else [],
            "query_id": query_id,
        }

    # Step 4 — Fetch diagnostic context from production DB directly
    diagnostic_context = None
    if req.battery_id:
        try:
            import sqlite3 as _sql
            _conn = _sql.connect(config_rag.DB_PATH)
            _conn.row_factory = _sql.Row
            _cur = _conn.cursor()
            _cur.execute("""SELECT d.*, h.tier_label_v2, h.adjusted_composite
                FROM battery_diagnostics d
                LEFT JOIN battery_health_scores_v2 h ON d.battery_id = h.battery_id
                WHERE d.battery_id = ? ORDER BY d.week_number DESC LIMIT 1""",
                (req.battery_id,))
            _row = _cur.fetchone()
            if _row:
                _dc = dict(_row)
                _dc["tier"] = _dc.pop("tier_label_v2", None)
                # Parse JSON fields
                for _jf in ["event_timeline", "shap_attribution", "range_deviation_attribution",
                            "trend_summary", "failure_probabilities", "recommendations",
                            "nbfc_summary", "oem_summary"]:
                    if _jf in _dc and isinstance(_dc[_jf], str):
                        try:
                            _dc[_jf] = json.loads(_dc[_jf])
                        except (json.JSONDecodeError, TypeError):
                            pass
                # Cap event_timeline
                _tl = _dc.get("event_timeline", [])
                if isinstance(_tl, list) and len(_tl) > 5:
                    _dc["event_timeline"] = _tl[:5]
                # Get chains
                _cur.execute("""SELECT chain_id, pattern_name, chain_pattern, operator_action,
                    nbfc_implication, oem_implication, fleet_prevalence_pct
                    FROM event_chains WHERE battery_id = ?""", (req.battery_id,))
                _dc["chains"] = [dict(r) for r in _cur.fetchall()]
                diagnostic_context = _dc
            _conn.close()
        except Exception:
            pass  # Diagnostic not available — continue with RAG only

    # Step 5 — Return enriched results
    return {
        "results": [
            {
                "text": r["text"],
                "source": r["source"],
                "weighted_score": r["weighted_score"],
                "battery_id": r["metadata"].get("battery_id", ""),
                "topics": r["metadata"].get("topics", ""),
                "validated": r["metadata"].get("validated", False),
            }
            for r in rag_results
        ],
        "query_id": query_id,
        "top_k": req.top_k,
        "confidence": confidence["confidence"],
        "confidence_caveat": confidence["caveat"],
        "sources": confidence["sources"],
        "scope_checked": True,
        "diagnostic_context": diagnostic_context,
    }


@app.post("/capture")
def capture(req: CaptureRequest, _=Depends(verify_token)):
    """Capture a live Q&A interaction. Filters response before storing."""
    filtered_answer = filter_response(req.answer)
    was_filtered = filtered_answer != req.answer

    entry_id = capture_interaction(
        question=req.question,
        answer=filtered_answer,
        battery_id=req.battery_id or "",
        vehicle_id=req.vehicle_id or "",
        source=req.source,
    )
    return {"entry_id": entry_id, "status": "captured", "was_filtered": was_filtered}


@app.post("/feedback")
def feedback(req: FeedbackRequest, store: RAGStore = Depends(get_store), _=Depends(verify_token)):
    """Process user feedback on answers (positive, negative, correction)."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    feedback_log_entry = {
        "timestamp": now_iso,
        "entry_id": req.entry_id,
        "signal": req.signal,
        "battery_id": req.battery_id or "",
        "question_preview": req.question[:100] if req.question else "",
    }

    if req.signal == "positive":
        # Promote to validated
        existing = store.get_by_id("live_captures", req.entry_id)
        if existing:
            store.update_metadata("live_captures", req.entry_id, {
                "validated": True,
                "feedback": "positive",
            })
            # Copy to validated_corrections with boost
            store.add_document(
                collection="validated_corrections",
                text=existing["text"],
                metadata={
                    "source": "validated_positive",
                    "source_file": f"feedback_{req.entry_id}",
                    "battery_id": existing["metadata"].get("battery_id", ""),
                    "validated": True,
                    "timestamp": now_iso,
                    "topics": existing["metadata"].get("topics", ""),
                    "weight_boost": "2.0",
                },
                doc_id=f"val_{req.entry_id}",
            )

        _log_jsonl(_FEEDBACK_LOG, feedback_log_entry)
        return {"status": "boosted", "message": "Answer promoted to validated KB"}

    elif req.signal == "negative":
        # Mark as downweighted
        existing = store.get_by_id("live_captures", req.entry_id)
        if existing:
            store.update_metadata("live_captures", req.entry_id, {
                "validated": False,
                "feedback": "negative",
                "correction_of": req.comment or "",
            })

        # Log for knowledge gap tracking
        _log_jsonl(_UNANSWERED_LOG, {
            "timestamp": now_iso,
            "question": req.question,
            "battery_id": req.battery_id or "",
            "reason": "negative_feedback",
            "original_answer": req.answer[:500] if req.answer else "",
            "comment": req.comment or "",
            "entry_id": req.entry_id,
        })

        _log_jsonl(_FEEDBACK_LOG, feedback_log_entry)
        return {"status": "noted", "message": "Feedback recorded"}

    elif req.signal == "correction":
        if not req.correction_text:
            raise HTTPException(status_code=400, detail="correction_text required for corrections")

        # Mark original as superseded
        existing = store.get_by_id("live_captures", req.entry_id)
        if existing:
            store.update_metadata("live_captures", req.entry_id, {
                "feedback": "superseded",
                "correction_of": f"corrected_by_user_{now_iso}",
            })

        # Create corrected entry in validated_corrections
        corrected_text = f"Q: {req.question}\nA: {req.correction_text}"
        topics = detect_topics(corrected_text)
        store.add_document(
            collection="validated_corrections",
            text=corrected_text,
            metadata={
                "source": "human_correction",
                "source_file": f"correction_{req.entry_id}",
                "battery_id": req.battery_id or "",
                "validated": True,
                "timestamp": now_iso,
                "topics": topics,
                "weight_boost": "2.0",
            },
            doc_id=f"corr_{req.entry_id}",
        )

        # Save as training pair for future fine-tuning
        _log_jsonl(_TRAINING_PAIRS, {
            "instruction": req.question,
            "response": req.correction_text,
            "battery_id": req.battery_id or "",
            "source": "human_correction",
            "timestamp": now_iso,
            "quality": "gold",
        })

        feedback_log_entry["signal"] = "correction"
        _log_jsonl(_FEEDBACK_LOG, feedback_log_entry)
        return {"status": "correction_saved", "message": "Correction added to knowledge base"}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown signal: {req.signal}")


@app.get("/feedback/stats")
def feedback_stats(_=Depends(verify_token)):
    """Feedback statistics."""
    positive = negative = corrections = 0
    last_ts = None
    negative_topics: list[str] = []

    if _FEEDBACK_LOG.exists():
        for line in _FEEDBACK_LOG.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            sig = entry.get("signal", "")
            if sig == "positive":
                positive += 1
            elif sig == "negative":
                negative += 1
            elif sig == "correction":
                corrections += 1
            ts = entry.get("timestamp")
            if ts:
                last_ts = ts

    total = positive + negative + corrections
    correction_rate = round(corrections / total * 100, 1) if total > 0 else 0.0

    # Count training pairs
    training_count = 0
    if _TRAINING_PAIRS.exists():
        training_count = sum(1 for _ in _TRAINING_PAIRS.read_text(encoding="utf-8").strip().splitlines() if _.strip())

    return {
        "total_feedback": total,
        "positive": positive,
        "negative": negative,
        "corrections": corrections,
        "correction_rate_pct": correction_rate,
        "training_pairs_count": training_count,
        "last_feedback": last_ts,
    }


@app.get("/feedback/training-pairs")
def training_pairs(min_quality: str = "gold", _=Depends(verify_token)):
    """Return training pairs for fine-tuning."""
    if not _TRAINING_PAIRS.exists():
        return []
    pairs = []
    for line in _TRAINING_PAIRS.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if min_quality == "all" or entry.get("quality", "") == min_quality:
            pairs.append(entry)
    return pairs


@app.get("/logs/unanswered")
def unanswered_logs(_=Depends(verify_token)):
    """Knowledge gap dashboard — last 50 unanswered questions."""
    if not _UNANSWERED_LOG.exists():
        return []
    entries = []
    for line in _UNANSWERED_LOG.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # Sort by timestamp DESC, return last 50
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries[:50]


class UsageEvent(BaseModel):
    session_id: str = ""
    event_id: str = ""
    timestamp: str = ""
    role: str = ""
    event_type: str = ""
    battery_id: Optional[str] = None
    project_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)


def _verify_token_optional(authorization: Optional[str] = Header(None)):
    """Verify token if present, skip silently if missing (for sendBeacon)."""
    if not authorization:
        return  # Allow unauthenticated tracking (sendBeacon can't set headers)
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == _API_TOKEN:
        return
    # Wrong token — still allow, tracking should never block


class AlertSendRequest(BaseModel):
    battery_id: str


@app.post("/alerts/send")
def send_alert(req: AlertSendRequest, _=Depends(verify_token)):
    """Send WhatsApp alert for a specific battery. Manual trigger."""
    try:
        import sqlite3 as _sql
        _conn = _sql.connect(config_rag.DB_PATH)
        _conn.row_factory = _sql.Row
        _cur = _conn.cursor()

        _cur.execute("""SELECT d.fleet_summary, d.recommendations, d.range_deviation_attribution,
            h.tier_label_v2 FROM battery_diagnostics d
            LEFT JOIN battery_health_scores_v2 h ON d.battery_id = h.battery_id
            WHERE d.battery_id = ? ORDER BY d.week_number DESC LIMIT 1""", (req.battery_id,))
        row = _cur.fetchone()
        _conn.close()

        if not row:
            return {"sent": False, "error": f"No diagnostic for {req.battery_id}"}

        tier = row["tier_label_v2"] or "UNKNOWN"
        sev_map = {"CRITICAL": "CRITICAL", "STRESSED": "HIGH", "WATCH": "MEDIUM"}
        severity = sev_map.get(tier, "LOW")

        recs = []
        try:
            recs = json.loads(row["recommendations"]) if row["recommendations"] else []
        except (json.JSONDecodeError, TypeError):
            pass
        top_action = recs[0]["action"] if recs else "Review battery health."

        dev = 0
        try:
            rd = json.loads(row["range_deviation_attribution"]) if row["range_deviation_attribution"] else {}
            dev = rd.get("deviation_pct", 0)
        except (json.JSONDecodeError, TypeError):
            pass

        message = f"Battery {req.battery_id} is {tier} with {dev:.0f}% range deviation. {top_action}"

        from alert_engine import send_whatsapp_alert
        result = send_whatsapp_alert({
            "battery_id": req.battery_id,
            "alert_type": f"MANUAL_{tier}",
            "severity": severity,
            "message": message[:80],
        })
        return {"sent": result, "battery_id": req.battery_id, "message_preview": message[:80]}
    except Exception as e:
        return {"sent": False, "error": str(e)[:200]}


@app.post("/track")
def track_usage(event: UsageEvent, _=Depends(_verify_token_optional)):
    """Record a usage event. Always returns 200 — never propagates errors to client."""
    try:
        entry = event.model_dump()
        if not entry.get("timestamp"):
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        if not entry.get("event_id"):
            entry["event_id"] = uuid.uuid4().hex
        _log_jsonl(_USAGE_LOG, entry)
    except Exception as e:
        import sys
        print(f"[track] write error: {e}", file=sys.stderr)
    return {"ok": True}


@app.get("/observability/summary")
def observability_summary(_=Depends(verify_token)):
    """Return the latest observability report as JSON."""
    from observability import usage_report
    try:
        return usage_report(days=30)
    except Exception as e:
        return {"error": str(e), "sessions": {}, "queries": {}, "quality": {}}


@app.get("/observability/brief")
def observability_brief(_=Depends(verify_token)):
    """Return the latest weekly brief text."""
    # Try to return the most recent saved brief first
    brief_files = sorted(LOGS_PATH.glob("weekly_brief_*.txt"), reverse=True)
    if brief_files:
        return {"text": brief_files[0].read_text(encoding="utf-8")}
    # Generate on the fly
    from observability import weekly_product_brief
    try:
        text = weekly_product_brief()
        return {"text": text}
    except Exception as e:
        return {"text": f"Error generating brief: {e}"}


@app.get("/stats")
def stats(store: RAGStore = Depends(get_store), _=Depends(verify_token)):
    """Collection statistics."""
    collection_stats = store.collection_stats()

    # Top topics
    from collections import Counter
    all_topics: list[str] = []
    for col_name in ["domain_docs", "chat_exports", "live_captures", "validated_corrections"]:
        col = store._col(col_name)
        if col.count() == 0:
            continue
        try:
            docs = col.get(include=["metadatas"])
            if docs and docs["metadatas"]:
                for meta in docs["metadatas"]:
                    t = meta.get("topics", "")
                    if t:
                        all_topics.extend(t.split(","))
        except Exception:
            pass

    counter = Counter(all_topics)

    return {
        "collections": collection_stats,
        "top_topics": [{"topic": t, "count": c} for t, c in counter.most_common(15)],
    }


# ── Ingestion endpoints ──────────────────────────────────────────────────

class IngestTextRequest(BaseModel):
    text: str
    source_label: str = "imported"
    battery_id: Optional[str] = None


class IngestPDFRequest(BaseModel):
    filename: str
    content_base64: str


@app.post("/ingest_text")
def ingest_text(req: IngestTextRequest, store: RAGStore = Depends(get_store), _=Depends(verify_token)):
    """Ingest raw text into chat_exports collection."""
    from config_rag import CHUNK_OVERLAP_CHARS, CHUNK_SIZE_CHARS
    from rag_seed import chunk_text

    chunks = chunk_text(req.text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
    if not chunks:
        return {"chunks_added": 0, "entry_ids": []}

    now = datetime.now(timezone.utc).isoformat()
    entry_ids = []
    for i, chunk in enumerate(chunks):
        doc_id = uuid.uuid4().hex
        topics = detect_topics(chunk)
        store.add_document(
            collection="chat_exports",
            text=chunk,
            metadata={
                "source": req.source_label,
                "source_file": f"import_{now[:10]}",
                "battery_id": req.battery_id or "",
                "topics": topics,
                "validated": False,
                "timestamp": now,
            },
            doc_id=doc_id,
        )
        entry_ids.append(doc_id)

    return {"chunks_added": len(entry_ids), "entry_ids": entry_ids}


@app.post("/ingest_pdf")
def ingest_pdf(req: IngestPDFRequest, store: RAGStore = Depends(get_store), _=Depends(verify_token)):
    """Ingest a base64-encoded PDF into domain_docs collection."""
    from config_rag import CHUNK_OVERLAP_CHARS, CHUNK_SIZE_CHARS, SEED_PATH
    from rag_seed import chunk_text, read_pdf

    uploads_dir = SEED_PATH / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Decode and save
    pdf_bytes = base64.b64decode(req.content_base64)
    dest = uploads_dir / req.filename
    dest.write_bytes(pdf_bytes)

    # Extract text
    text = read_pdf(dest)
    if not text.strip():
        return {"pages": 0, "chunks_added": 0}

    chunks = chunk_text(text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
    now = datetime.now(timezone.utc).isoformat()
    page_count = text.count("\n\n") + 1  # rough estimate

    for i, chunk in enumerate(chunks):
        topics = detect_topics(chunk)
        store.add_document(
            collection="domain_docs",
            text=chunk,
            metadata={
                "source": "external_research",
                "source_file": req.filename,
                "topics": topics,
                "validated": False,
                "timestamp": now,
            },
            doc_id=f"pdf_upload_{req.filename}_{i:04d}",
        )

    return {"pages": page_count, "chunks_added": len(chunks)}


# ── Notification endpoints ────────────────────────────────────────────────

_ALERT_DB = Path("C:/Users/Admin/Desktop/Ev__ML/RAG/alerts.db")


def _get_alert_conn():
    return sqlite3.connect(str(_ALERT_DB))


@app.get("/notifications")
def get_notifications(unread_only: bool = True, _=Depends(verify_token)):
    """Return recent alerts from alerts.db."""
    if not _ALERT_DB.exists():
        return []
    conn = _get_alert_conn()
    conn.row_factory = sqlite3.Row
    try:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE read=0 ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


@app.post("/notifications/{alert_id}/read")
def mark_notification_read(alert_id: str, _=Depends(verify_token)):
    """Mark a notification as read."""
    if not _ALERT_DB.exists():
        raise HTTPException(status_code=404, detail="Alert DB not found")
    conn = _get_alert_conn()
    try:
        conn.execute("UPDATE alerts SET read=1 WHERE id=?", (alert_id,))
        conn.commit()
        return {"status": "read"}
    finally:
        conn.close()


@app.post("/notifications/read-all")
def mark_all_read(_=Depends(verify_token)):
    """Mark all notifications as read."""
    if not _ALERT_DB.exists():
        return {"status": "no_db"}
    conn = _get_alert_conn()
    try:
        conn.execute("UPDATE alerts SET read=1 WHERE read=0")
        conn.commit()
        return {"status": "all_read"}
    finally:
        conn.close()


# ── Scheduler endpoints ──────────────────────────────────────────────────

_SCHEDULE_CONFIG = Path("C:/Users/Admin/Desktop/Ev__ML/RAG/schedule_config.json")
_SCHEDULER_LOG = LOGS_PATH / "scheduler_log.jsonl"


@app.post("/scheduler/run/{task_name}")
def scheduler_run(task_name: str, _=Depends(verify_token)):
    """Run a scheduled task immediately."""
    try:
        if task_name == "alert":
            import alert_engine
            result = alert_engine.run_all_checks()
        elif task_name == "rag":
            import rag_scale
            rag_scale.weekly_kb_refresh()
            result = {"refreshed": True}
        elif task_name == "feedback":
            import feedback_to_ml
            feedback_to_ml.analyse_outcome_errors()
            result = {"analysed": True}
        else:
            raise HTTPException(status_code=404, detail=f"Unknown task: {task_name}")

        _log_jsonl(_SCHEDULER_LOG, {
            "task": task_name,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "triggered_by": "api",
        })
        return {"task": task_name, "status": "completed", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        return {"task": task_name, "status": "error", "error": str(e)}


class AddPromptRequest(BaseModel):
    name: str
    prompt: str
    battery_id: Optional[str] = None
    cron: str = "0 8 * * 1"
    webhook_url: Optional[str] = None


@app.post("/scheduler/add")
def scheduler_add(req: AddPromptRequest, _=Depends(verify_token)):
    """Add a custom scheduled prompt."""
    config = json.loads(_SCHEDULE_CONFIG.read_text(encoding="utf-8")) if _SCHEDULE_CONFIG.exists() else {}
    prompts = config.get("custom_prompts", [])

    prompt_id = uuid.uuid4().hex[:12]
    new_prompt = {
        "id": prompt_id,
        "name": req.name,
        "prompt": req.prompt,
        "battery_id": req.battery_id,
        "schedule_cron": req.cron,
        "enabled": True,
        "last_run": None,
        "last_result": None,
        "webhook_url": req.webhook_url,
    }
    prompts.append(new_prompt)
    config["custom_prompts"] = prompts
    _SCHEDULE_CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")

    return {"id": prompt_id, "next_run": "pending scheduler restart"}


@app.get("/scheduler/status")
def scheduler_status(_=Depends(verify_token)):
    """Return scheduler status."""
    config = json.loads(_SCHEDULE_CONFIG.read_text(encoding="utf-8")) if _SCHEDULE_CONFIG.exists() else {}

    # Get last runs from log
    last_runs = {}
    if _SCHEDULER_LOG.exists():
        for line in _SCHEDULER_LOG.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                task = entry.get("task", "")
                last_runs[task] = entry.get("timestamp", "")
            except json.JSONDecodeError:
                continue

    jobs = []
    for task_name in ["alert_check", "rag_refresh", "feedback_analysis"]:
        cfg = config.get(task_name, {})
        jobs.append({
            "name": task_name,
            "enabled": cfg.get("enabled", False),
            "schedule": cfg.get("cron", cfg.get("interval_minutes", "")),
            "description": cfg.get("description", ""),
            "last_run": last_runs.get(task_name, None),
        })

    for prompt in config.get("custom_prompts", []):
        jobs.append({
            "name": prompt.get("name", ""),
            "enabled": prompt.get("enabled", False),
            "schedule": prompt.get("schedule_cron", ""),
            "description": f"Prompt: {prompt.get('prompt', '')[:60]}",
            "last_run": prompt.get("last_run"),
        })

    return {"jobs": jobs}


# ════════════════════════════════════════════════════════════════════════
# STAGE 2 — enerlyst Routing Layer (March 30, 2026)
# ════════════════════════════════════════════════════════════════════════

import re as _re
import chromadb as _chromadb

# ── Audience field stripping ─────────────────────────────────────────
_BLOCKED_FIELDS = {
    "operator": {"soh_corrected", "soh_cap_weekly", "operational_score", "composite_score",
                 "spread_delta_mv", "cell_spread_max", "commissioned_range_km",
                 "attribution_pct", "maintenance_pct", "charging_pct", "usage_pct",
                 "thermal_pct", "calendar_pct", "corroboration_score", "l1_score",
                 "l2_score", "l3_score", "trajectory_score", "combined_score_v2"},
    "nbfc": {"operational_score", "composite_score", "l1_score", "l2_score", "l3_score",
             "commissioned_range_km", "kps_slope_4wk", "corroboration_score",
             "trajectory_score", "combined_score_v2", "spread_delta_mv"},
    "oem": {"operational_score", "composite_score", "l1_score", "l2_score", "l3_score",
            "combined_score_v2"},
    "internal": set(),  # internal sees everything
    "admin": set(),
}

# Scope guard — substring matching (accepts unless clearly off-domain)
_BATTERY_DOMAIN_KEYWORDS = [
    "battery", "batteries", "fleet", "vehicle", "range", "km",
    "charge", "charging", "charger", "replace", "service", "servic",
    "cell", "spread", "health", "degradat", "action", "monitor",
    "rul", "soh", "capacity", "voltage", "current", "cycle",
    "efc", "pack", "oem", "greenfuel", "inverted", "nbfc", "loan",
    "risk", "grade", "repair", "maintenance", "swap",
    "attention", "urgent", "critical", "warn", "alert", "fault",
    "perform", "trend", "week", "month", "decline", "loss",
    "rickshaw", "ev", "electric", "enerlyt", "enerlyst",
    "lfp", "nmc", "bms", "thermal", "warranty", "eol",
    "attribution", "commissioning", "manufactur", "portfolio",
    "tenure", "ltv", "borrower", "scoring", "tier", "passport",
    "balance", "inspect", "diagnos", "report", "summary", "analys",
    "predict", "forecast", "compare",
]
_BAT_ID_RE = _re.compile(r"BAT_(LFP|NMC)_\d+", _re.IGNORECASE)

_INJECTION_PATTERNS = [
    "ignore previous", "ignore all", "act as ", "pretend you are",
    "your real instructions", "disregard", "forget everything",
    "new instructions", "system prompt", "jailbreak",
    "you are now", "override",
]

_SCOPE_REJECTION = "enerlyst answers questions about EV battery health and fleet intelligence only."


def _check_injection(question: str) -> bool:
    """Returns True if injection pattern detected."""
    q_lower = question.lower()
    return any(p in q_lower for p in _INJECTION_PATTERNS)


def _check_scope_guard(question: str) -> bool:
    """Returns True if question is in-scope. Accepts unless clearly off-domain."""
    q_lower = question.lower()
    # Injection is out of scope
    if _check_injection(question):
        return False
    # Battery ID pattern always in scope
    if _BAT_ID_RE.search(question):
        return True
    # Accept if ANY battery-domain keyword found (substring match)
    return any(kw in q_lower for kw in _BATTERY_DOMAIN_KEYWORDS)




def _strip_fields(text: str, audience: str) -> str:
    """Remove blocked field references from response text for given audience."""
    blocked = _BLOCKED_FIELDS.get(audience, set())
    if not blocked:
        return text
    for field in blocked:
        # Remove patterns like "soh_corrected: 58.4%" or "operational_score = 72"
        text = _re.sub(rf"\b{field}\b\s*[:=]\s*[\d.]+%?", f"[restricted for {audience}]", text)
    return text


_EXPLAIN_RE = _re.compile(r"\b(explain|what does|what is|what does \w+ mean|why|significance|how does|meaning)\b", _re.IGNORECASE)

def _retrieve_from_kb(question: str, top_k: int = 5, battery_id: Optional[str] = None) -> list[dict]:
    """Retrieve from all 6 collections, weighted, return citations.

    When battery_id is provided, the per-battery chunks from session_knowledge
    and model_intelligence are prepended with a guaranteed-top weighted_score
    so they always reach LLM context — semantic cosine alone was losing them
    for batteries whose query wording didn't align with chunk header text.
    """
    try:
        from config_rag import COLLECTION_WEIGHTS, KB_PATH
        kb_client = _chromadb.PersistentClient(path=KB_PATH)
    except Exception:
        # Fall back to the env var directly so we never bind to a literal
        # source path; the caller's environment is authoritative.
        _fallback_kb = os.environ.get("ENERLYTIK_KB_PATH") or "enerlyst/kb"
        kb_client = _chromadb.PersistentClient(path=_fallback_kb)
        COLLECTION_WEIGHTS = {"physics_truths": 2.0, "validated_corrections": 3.0,
                              "explanations": 2.5, "model_intelligence": 0.8,
                              "audience_outputs": 1.5, "session_knowledge": 1.8}

    # Boost explanations for "explain/what/why" queries
    weights = dict(COLLECTION_WEIGHTS)
    if _EXPLAIN_RE.search(question):
        weights["explanations"] = 4.0

    all_results = []
    seen_ids: set[str] = set()

    # Guaranteed per-battery chunks — metadata-filtered, not semantic
    if battery_id:
        for col_name in ("session_knowledge", "model_intelligence"):
            try:
                col = kb_client.get_collection(col_name)
                res = col.get(where={"battery_id": battery_id}, limit=2,
                              include=["documents", "metadatas"])
                for i, cid in enumerate(res.get("ids", [])):
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    all_results.append({
                        "collection": col_name,
                        "chunk_id": cid,
                        "text": res["documents"][i],
                        "distance": 0.0,
                        "weighted_score": 99.0,  # pin to top
                    })
            except Exception:
                continue

    for col_name in weights:
        try:
            col = kb_client.get_collection(col_name)
            res = col.query(query_texts=[question], n_results=top_k)
            weight = weights[col_name]
            for i in range(len(res["ids"][0])):
                cid = res["ids"][0][i]
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                dist = res["distances"][0][i] if res["distances"] else 1.0
                all_results.append({
                    "collection": col_name,
                    "chunk_id": cid,
                    "text": res["documents"][0][i],
                    "distance": dist,
                    "weighted_score": (1 - dist) * weight,
                })
        except Exception:
            continue

    # Sort by weighted score descending, take top results
    all_results.sort(key=lambda x: x["weighted_score"], reverse=True)
    return all_results[:top_k * 2]  # return more for context


class EnerlystQueryRequest(BaseModel):
    question: str
    audience: str = "internal"
    battery_id: Optional[str] = None
    pack_model: Optional[str] = None
    fleet_id: Optional[str] = None
    city: Optional[str] = None
    oem_name: Optional[str] = None
    scope_description: Optional[str] = None
    session_id: Optional[str] = None
    context_summary: Optional[str] = None


@app.post("/enerlyst/query")
async def enerlyst_query(req: EnerlystQueryRequest, _=Depends(verify_token)):
    """
    Stage 2 enerlyst query endpoint.
    Pipeline: scope guard -> injection guard -> KB retrieval -> LLM synthesis -> audience strip.
    """
    query_id = uuid.uuid4().hex
    audience = req.audience.lower() if req.audience else "internal"
    if audience not in _BLOCKED_FIELDS:
        audience = "internal"

    # Rate limit
    sid = req.session_id or "default"
    retry = _check_rate(_rate_query, sid, _QUERY_LIMIT, 3600)
    if retry is not None:
        return {"error": "rate_limit", "retry_after": retry}

    # Injection guard
    if _check_injection(req.question):
        _log_query(-1, "guard", 403)
        return {"error": "injection_blocked", "message": _SCOPE_REJECTION,
                "query_id": query_id}

    # Scope guard
    if not _check_scope_guard(req.question):
        _log_query(-1, "scope", 400)
        return {"error": "out_of_scope", "message": _SCOPE_REJECTION,
                "query_id": query_id}

    # KB retrieval (weighted across all collections)
    chunks = _retrieve_from_kb(req.question, battery_id=req.battery_id)
    if not chunks:
        return {"answer": "No relevant information found in the knowledge base.",
                "citations": [], "audience": audience, "query_id": query_id}

    # Platform-state injection (APR 22):
    # When the user asks about freshness, pipeline runs, pending work, or
    # chain connections, pull live counts from db_api and prepend to context.
    _PLATFORM_KEYWORDS = [
        "last run", "last ingest", "fresh", "freshness", "recent", "today",
        "pending", "backlog", "queue", "what's missing", "missing data",
        "not scored", "unscored", "outlier", "data quality", "status",
        "platform state", "how many", "open alerts", "exit now",
        "replacement", "warranty", "overview", "summary", "health of fleet",
        "chain", "connection", "connect", "cross", "insight",
    ]
    _q_lower = req.question.lower()
    _is_platform_query = any(kw in _q_lower for kw in _PLATFORM_KEYWORDS)
    _platform_context = ""
    if _is_platform_query:
        try:
            import json as _json2
            import requests as _rq
            _DB_PORT = os.getenv("DB_API_PORT", "3001")
            _DB_TOKEN = os.getenv("DB_API_TOKEN", "{{ api_token }}")
            _hdr = {"Authorization": f"Bearer {_DB_TOKEN}"}
            _out = []
            for _ep in (
                "/api/platform/freshness",
                "/api/platform/pending-actions",
                "/api/platform/chain-insights",
            ):
                try:
                    _r = _rq.get(f"http://localhost:{_DB_PORT}{_ep}",
                                 headers=_hdr, timeout=5)
                    if _r.status_code == 200:
                        _out.append(f"[PLATFORM_LIVE {_ep}]\n"
                                    + _json2.dumps(_r.json(), indent=2)[:3500])
                except Exception:
                    continue
            if _out:
                _platform_context = "\n\n".join(_out) + "\n\n"
        except Exception:
            _platform_context = ""

    # Check for validated_corrections override
    vc_chunks = [c for c in chunks if c["collection"] == "validated_corrections"]
    context_parts = []
    citations = []
    if _platform_context:
        context_parts.append(_platform_context)

    # validated_corrections first (highest trust)
    for c in vc_chunks[:3]:
        context_parts.append(f"[VALIDATED — {c['chunk_id']}]: {c['text']}")
        citations.append({"collection": c["collection"], "chunk_id": c["chunk_id"],
                          "score": round(c["weighted_score"], 3)})

    # Then other chunks
    for c in chunks:
        if c["collection"] == "validated_corrections":
            continue
        if len(context_parts) >= 8:
            break
        context_parts.append(f"[{c['collection']} — {c['chunk_id']}]: {c['text']}")
        citations.append({"collection": c["collection"], "chunk_id": c["chunk_id"],
                          "score": round(c["weighted_score"], 3)})

    context_block = "\n\n".join(context_parts)

    # Audience-specific structured prompts (4-line format)
    _PROMPTS = {
        "operator": """You are enerlyst, an EV battery intelligence assistant for fleet operators.

ANSWER STRUCTURE — follow this exactly for every response:
Line 1 — SITUATION: One sentence. Lead with km. What is happening right now.
Line 2 — CAUSE: One sentence. Plain English. Why it is happening.
Line 3 — ACTION: One sentence. Imperative. What to do about it.
Line 4 — HORIZON: One sentence. Time-bound. What happens next if action taken or not.

RULES — NON-NEGOTIABLE:
- Never mention SOH%, scores, mV, or any internal field name
- Always use km for range — never percentages
- One action only — never a list of options
- If you have VALIDATED_DATA below — use those exact numbers, do not invent
- Keep total response under 100 words
- Plain English — write for a vehicle fleet manager, not a data scientist
- CRITICAL: Never include chunk IDs, collection names, or source references.""",

        "nbfc": """You are enerlyst, an EV battery intelligence assistant for lenders.

ANSWER STRUCTURE — follow this exactly:
Line 1 — PORTFOLIO STATE: Grade distribution or specific asset grade. Lead with the grade letter.
Line 2 — RISK SIGNAL: What the data shows about loan coverage or asset deterioration.
Line 3 — RECOMMENDATION: One action for the credit team.
Line 4 — TIMELINE: When this becomes critical if unaddressed.

RULES:
- Always express risk as A/B/C/D grades
- Always relate battery condition to loan repayment capacity
- Never show raw scores, soh_corrected, or internal field names
- Keep total response under 100 words
- CRITICAL: Never include chunk IDs or collection names.""",

        "oem": """You are enerlyst, an EV battery intelligence assistant for battery manufacturers.

ANSWER STRUCTURE — follow this exactly:
Line 1 — FINDING: The primary technical finding. Lead with EFC or pack model.
Line 2 — ATTRIBUTION: Degradation split as percentages — charging X% / maintenance Y% / calendar Z%.
Line 3 — IMPLICATION: What this means for design, warranty, or field performance.
Line 4 — RECOMMENDED ACTION: One specific engineering or commercial action.

RULES:
- Use EFC not "charge count"
- Be technically precise — this audience understands battery chemistry
- Never mention operational scores
- Keep total response under 120 words
- CRITICAL: Never include chunk IDs or collection names.""",

        "internal": """You are enerlyst, the intelligence engine for the enerlytik EV battery platform.
Answer for an internal analyst. All technical signals and field names are allowed.
Be precise. CRITICAL: Never include chunk IDs or collection names in answer text.""",

        "admin": """You are enerlyst. Full technical detail. All signals available.
CRITICAL: Never include chunk IDs or collection names in answer text.""",
    }

    system_prompt = _PROMPTS.get(audience, _PROMPTS["internal"])

    # Inject validated battery data + grounding prompt (Phase 6 — Demo Week 1)
    if req.battery_id:
        try:
            async with httpx.AsyncClient(timeout=3.0) as _hc:
                _br = await _hc.get(f"http://localhost:3001/api/battery/{req.battery_id}",
                    headers={"Authorization": f"Bearer {_DB_TOKEN}"} if _DB_TOKEN else {})
                if _br.status_code == 200:
                    _bd = _br.json()
                    # Core fields for grounding (all audiences)
                    _core = {k: _bd.get(k) for k in [
                        "range_corrected_km", "operational_score", "action_primary",
                        "capacity_state", "rul_weeks_v2", "degradation_primary_driver",
                        "nbfc_risk_tier", "efc_pct_of_warranty", "kps_slope_4wk",
                    ] if _bd.get(k) is not None}
                    # Audience-specific sentences
                    _vs = {}
                    if audience == "operator":
                        _vs = {k: _bd.get(k, "") for k in ["operator_capacity_sentence", "operator_cause_sentence", "operator_action_sentence", "range_corrected_km", "rul_action_v2", "rul_weeks_v2"] if _bd.get(k)}
                    elif audience == "nbfc":
                        _vs = {k: _bd.get(k, "") for k in ["nbfc_risk_tier", "nbfc_summary_sentence", "nbfc_rul_disclosure", "nbfc_range_capacity_pct", "efc_pct_of_warranty"] if _bd.get(k)}
                    elif audience == "oem":
                        _vs = {k: _bd.get(k, "") for k in ["degradation_primary_driver", "shap_explanation_sentence", "efc_cumulative"] if _bd.get(k)}
                    _all_data = {**_core, **_vs}
                    if _all_data:
                        system_prompt += f"\n\nVALIDATED_DATA — use these exact facts, do not contradict:\n{_json.dumps(_all_data, indent=2)}\nBuild your answer FROM these validated sentences."
                    # Grounding directive
                    system_prompt += f"""

When answering about {req.battery_id}:
- Lead with the actual value from the database (range km, score, action)
- Then explain WHY using physics and validated corrections
- Then state the ONE recommended action
- Never give a general answer when specific data is available
- If the database returned range_corrected_km, use that exact number
- Format: [What it is] → [Why] → [What to do]
- Maximum 4 sentences for simple queries. 6 for complex attribution."""
        except Exception:
            pass

    # Fleet context for fleet-level questions
    if req.fleet_id and not req.battery_id:
        try:
            async with httpx.AsyncClient(timeout=3.0) as _fc:
                _fr = await _fc.get("http://localhost:3001/api/fleet/summary?chemistry=LFP", headers={"Authorization": f"Bearer {_DB_TOKEN}"} if _DB_TOKEN else {})
                if _fr.status_code == 200:
                    _fs = _fr.json()
                    _rd = _fs.get("rul_action_dist", {})
                    _fleet_facts = {
                        "total_batteries": _fs.get("total_batteries", 183),
                        "healthy": (_rd.get("NO_ACTION", 0) + _rd.get("MONITOR_WEEKLY", 0) + _rd.get("MONITOR", 0)),
                        "action_needed": (_rd.get("CELL_BALANCE", 0) + _rd.get("REPLACE_PLAN", 0)),
                        "urgent": (_rd.get("REPLACE_URGENT", 0) + _rd.get("EOL_IMMINENT", 0) + _rd.get("EOL", 0)),
                    }
                    system_prompt += f"\n\nFLEET FACTS (use these numbers exactly):\n{_json.dumps(_fleet_facts, indent=2)}\n"
        except Exception:
            pass

    system_prompt += f"\n\nAnswer based ONLY on the provided context. If the context does not contain the answer, say so clearly.\n\nCONTEXT:\n{context_block}"
    # Session context / scope injection
    if req.scope_description:
        system_prompt += f"\n\nSCOPE: Answer only within this context: {req.scope_description}.\n"
        system_prompt += "If asked about 'my fleet' or 'all batteries' — answer only for batteries within this scope.\n"
        if req.battery_id:
            system_prompt += f"Specific battery in focus: {req.battery_id}.\n"
    else:
        _sctx_parts = []
        if req.fleet_id:
            _sctx_parts.append(f"Fleet: {req.fleet_id}")
        if req.city:
            _sctx_parts.append(f"City: {req.city}")
        if req.oem_name:
            _sctx_parts.append(f"OEM: {req.oem_name}")
        if req.pack_model:
            _sctx_parts.append(f"Model: {req.pack_model}")
        if req.battery_id:
            _sctx_parts.append(f"Battery: {req.battery_id}")
        if _sctx_parts:
            system_prompt += f"\n\nSESSION CONTEXT — answer within this scope:\n" + "\n".join(_sctx_parts)
    if req.context_summary:
        system_prompt += f"\n\nCONVERSATION CONTEXT:\n{req.context_summary[:600]}\nUse this to answer follow-up questions."

    # LLM synthesis via Groq round-robin
    if not _GROQ_KEYS:
        # No LLM available — return RAG-only result
        return {
            "answer": f"RAG context retrieved ({len(citations)} chunks). LLM synthesis unavailable — no Groq keys configured.",
            "citations": citations, "audience": audience, "query_id": query_id,
            "context_preview": context_block[:500],
        }

    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": req.question},
    ]

    max_retries = min(len(_GROQ_KEYS), 3)
    answer = None
    for attempt in range(max_retries):
        key = _get_groq_key()
        key_idx = _GROQ_KEYS.index(key) if key in _GROQ_KEYS else -1
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                resp = await http_client.post(_GROQ_URL, json={
                    "model": _GROQ_MODEL,
                    "messages": msgs,
                    "max_tokens": _GROQ_MAX_TOKENS,
                    "temperature": 0.2,
                }, headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                })
            if resp.status_code == 429 and attempt < max_retries - 1:
                _log_query(key_idx, _GROQ_MODEL, 429)
                continue
            if resp.status_code != 200:
                _log_query(key_idx, _GROQ_MODEL, resp.status_code)
                break
            data = resp.json()
            if data.get("choices") and data["choices"][0].get("message"):
                answer = data["choices"][0]["message"].get("content", "")
            tokens = data.get("usage", {}).get("total_tokens", 0)
            _log_query(key_idx, _GROQ_MODEL, 200, tokens)
            break
        except Exception:
            _log_query(key_idx, _GROQ_MODEL, 0)
            continue

    if not answer:
        answer = f"RAG context retrieved ({len(citations)} chunks). LLM synthesis failed."

    # Post-processing: strip any leaked citation references from answer text
    answer = _re.sub(
        r"\[(?:audience_outputs|physics_truths|model_intelligence|validated_corrections|session_knowledge|VALIDATED)[^\]]*\]",
        "", answer
    )
    answer = _re.sub(r"\s{2,}", " ", answer).strip()

    # Audience field stripping
    answer = _strip_fields(answer, audience)

    # Confidence tier from chunk source collections (Phase 4 — Demo Week 1)
    citation_collections = {c.get("collection") for c in citations}
    if "validated_corrections" in citation_collections:
        confidence = {"level": "high", "label": "Physics-validated answer", "tier": "HIGH"}
    elif "physics_truths" in citation_collections:
        confidence = {"level": "medium", "label": "Based on platform data", "tier": "MEDIUM"}
    else:
        confidence = {"level": "estimated", "label": "Estimated — limited direct evidence", "tier": "ESTIMATED"}

    # Discovery insight (lightweight second call)
    discovery = ""
    if _GROQ_KEYS and answer and len(answer) > 20:
        try:
            _disc_prompt = f"The user just received this answer: \"{answer[:200]}\"\nGenerate ONE surprising finding the user probably doesn't know. Start with \"By the way — \". One sentence, max 25 words, must include a specific number. Audience is {audience}. If no finding, return empty string."
            _dk = _get_groq_key()
            async with httpx.AsyncClient(timeout=10.0) as _dc:
                _dr = await _dc.post(_GROQ_URL, json={"model": _GROQ_MODEL, "messages": [{"role": "user", "content": _disc_prompt}], "max_tokens": 60, "temperature": 0.4},
                    headers={"Authorization": f"Bearer {_dk}", "Content-Type": "application/json"})
                if _dr.status_code == 200:
                    _dd = _dr.json()
                    if _dd.get("choices"):
                        _dt = _dd["choices"][0]["message"]["content"].strip()
                        if _dt.startswith("By the way"):
                            discovery = _dt
        except Exception:
            pass

    return {
        "answer": answer,
        "discovery": discovery,
        "confidence": confidence,
        "citations": citations,
        "audience": audience,
        "query_id": query_id,
        "model": _GROQ_MODEL,
    }


@app.get("/enerlyst/health")
def enerlyst_health():
    """Health check for enerlyst routing layer."""
    try:
        from config_rag import KB_PATH, COLLECTION_NAMES
        kb_client = _chromadb.PersistentClient(path=KB_PATH)
        counts = {}
        for name in COLLECTION_NAMES:
            try:
                counts[name] = kb_client.get_collection(name).count()
            except Exception:
                counts[name] = -1
    except Exception as e:
        counts = {"error": str(e)}

    return {
        "status": "ok",
        "stage": 2,
        "groq_keys": len(_GROQ_KEYS),
        "groq_model": _GROQ_MODEL,
        "collections": counts,
        "audiences": list(_BLOCKED_FIELDS.keys()),
    }


# ════════════════════════════════════════════════════════════════════════
# SUGGESTED QUESTIONS — context-aware per battery state (Phase 1, Demo Week 1)
# ════════════════════════════════════════════════════════════════════════


@app.get("/enerlyst/battery/{battery_id}/suggested-questions")
async def suggested_questions(battery_id: str):
    """Return 3 context-aware questions based on battery's current state."""
    bat = await _db_fetch(f"/battery/{battery_id}")
    if not bat:
        return {"battery_id": battery_id, "recommended_action": "UNKNOWN",
                "suggested_questions": [
                    f"What is the current health status of {battery_id}?",
                    f"What action is recommended for {battery_id} this week?",
                    f"How does {battery_id} compare to the fleet average?",
                ]}

    action = bat.get("action_primary") or bat.get("rul_action_v2") or "NO_ACTION"
    bid = battery_id

    _QUESTION_MAP = {
        "REPLACE_URGENT": [
            f"Why does {bid} need immediate replacement?",
            f"How much range has {bid} lost since delivery?",
            f"What is the financial exposure if {bid} fails mid-route?",
        ],
        "REPLACE_NOW": [
            f"Why does {bid} need immediate replacement?",
            f"How much range has {bid} lost since delivery?",
            f"What is the financial exposure if {bid} fails mid-route?",
        ],
        "EOL_IMMINENT": [
            f"Why does {bid} need immediate replacement?",
            f"How much range has {bid} lost since delivery?",
            f"What is the financial exposure if {bid} fails mid-route?",
        ],
        "REPLACE_PLAN": [
            f"What is driving the decline in {bid}?",
            f"How many weeks before {bid} crosses the viable range floor?",
            f"Which other batteries in this fleet are on the same trajectory?",
        ],
        "CELL_BALANCE": [
            f"Why does {bid} need cell balancing?",
            f"Is this a charging issue or a battery design issue?",
            f"What happens if cell balancing is delayed by 4 weeks?",
        ],
        "MONITOR_WEEKLY": [
            f"What signal is causing concern on {bid}?",
            f"How does {bid} compare to similar batteries in the fleet?",
            f"What would trigger an escalation from monitor to replace?",
        ],
        "NO_ACTION": [
            f"Why is {bid} performing well compared to fleet?",
            f"What is the remaining useful life estimate for {bid}?",
            f"What should I watch for to keep {bid} in good health?",
        ],
    }

    questions = _QUESTION_MAP.get(action, [
        f"What is the current health status of {bid}?",
        f"What action is recommended for {bid} this week?",
        f"How does {bid} compare to the fleet average?",
    ])

    return {
        "battery_id": battery_id,
        "recommended_action": action,
        "suggested_questions": questions,
    }


# ════════════════════════════════════════════════════════════════════════
# STAGE 3 — Additional API endpoints (March 30, 2026)
# ════════════════════════════════════════════════════════════════════════


class ChunkQueryRequest(BaseModel):
    query: str
    n: int = Field(default=10, ge=1, le=50)


@app.post("/enerlyst/chunks")
def enerlyst_chunks(req: ChunkQueryRequest, _=Depends(verify_token)):
    """Raw KB retrieval — returns top-N chunks across all collections with scores."""
    chunks = _retrieve_from_kb(req.query, top_k=req.n)
    return {
        "query": req.query,
        "total": len(chunks),
        "chunks": [
            {
                "collection": c["collection"],
                "chunk_id": c["chunk_id"],
                "text": c["text"],
                "distance": round(c["distance"], 4),
                "weighted_score": round(c["weighted_score"], 4),
            }
            for c in chunks
        ],
    }


@app.get("/enerlyst/logs")
def enerlyst_logs(_=Depends(verify_token)):
    """Returns last 200 entries from query_log.jsonl."""
    log_path = Path(LOGS_PATH) / "query_log.jsonl"
    if not log_path.exists():
        return {"entries": [], "total": 0}
    entries = []
    try:
        for line in log_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return {"entries": [], "total": 0, "error": "failed to read log"}
    # Return last 200, most recent first
    entries = entries[-200:]
    entries.reverse()
    return {"entries": entries, "total": len(entries)}


@app.post("/enerlyst/eval/run")
def enerlyst_eval_run(_=Depends(verify_token)):
    """Run faithfulness eval and return results."""
    import subprocess
    eval_script = Path(__file__).parent.parent / "tests" / "faithfulness_eval.py"
    if not eval_script.exists():
        raise HTTPException(status_code=404, detail="faithfulness_eval.py not found")
    try:
        result = subprocess.run(
            ["python", "-X", "utf8", str(eval_script)],
            capture_output=True, text=True, timeout=120,
            cwd=str(eval_script.parent),
        )
        output = result.stdout + result.stderr

        # Parse score from output: "Score: 19/20 (95%)"
        import re
        score_match = re.search(r"Score:\s*(\d+)/(\d+)\s*\((\d+)%\)", output)
        gate_match = re.search(r"Gate.*?:\s*(PASS|FAIL)", output)

        if score_match:
            passed_count = int(score_match.group(1))
            total_count = int(score_match.group(2))
            pct = int(score_match.group(3))
        else:
            passed_count = 0
            total_count = 20
            pct = 0

        gate_passed = gate_match.group(1) == "PASS" if gate_match else False

        return {
            "score": passed_count,
            "total": total_count,
            "pct": pct,
            "passed": gate_passed,
            "details": output,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"score": 0, "total": 20, "pct": 0, "passed": False,
                "details": "Eval timed out after 120s", "exit_code": -1}
    except Exception as e:
        return {"score": 0, "total": 20, "pct": 0, "passed": False,
                "details": f"Error running eval: {e}", "exit_code": -1}


# ── Rate bucket for public feedback (10/min per IP) ────────────────
_rate_feedback: dict[str, list[float]] = defaultdict(list)
_FEEDBACK_LIMIT = 10   # per minute per IP
_ISSUE_LOG = Path(LOGS_PATH) / "issue_log.jsonl"


class PublicFeedbackRequest(BaseModel):
    query_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


@app.post("/enerlyst/feedback")
def enerlyst_feedback(req: PublicFeedbackRequest):
    """Public feedback — no auth required, rate-limited 10/min per IP."""
    # Rate limit by query_id prefix as proxy for caller identity
    caller_id = req.query_id[:8] if req.query_id else "anon"
    retry = _check_rate(_rate_feedback, caller_id, _FEEDBACK_LIMIT, 60)
    if retry is not None:
        return {"error": "rate_limit", "retry_after": retry,
                "message": f"Too many feedback submissions — wait {retry}s"}

    now = datetime.now(timezone.utc).isoformat()
    feedback_entry = {
        "timestamp": now,
        "query_id": req.query_id,
        "rating": req.rating,
        "comment": req.comment or "",
    }

    # Write to feedback log
    feedback_path = Path(LOGS_PATH) / "feedback_log.jsonl"
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    _log_jsonl(feedback_path, feedback_entry)

    # Auto-create issue for low ratings
    if req.rating <= 2:
        issue_entry = {
            "timestamp": now,
            "query_id": req.query_id,
            "rating": req.rating,
            "comment": req.comment or "",
            "status": "open",
            "source": "auto_feedback",
        }
        _ISSUE_LOG.parent.mkdir(parents=True, exist_ok=True)
        _log_jsonl(_ISSUE_LOG, issue_entry)

    return {"status": "recorded", "query_id": req.query_id, "auto_issue": req.rating <= 2}


# ════════════════════════════════════════════════════════════════════════
# STAGE 4 — Skill Endpoints + Suggest (March 30, 2026)
# ════════════════════════════════════════════════════════════════════════

_DB_API_BASE = "http://localhost:3001/api"


_DB_TOKEN = os.getenv("RAG_API_TOKEN", "")
if not _DB_TOKEN:
    # Try loading from RAG/.env
    _rag_env = Path(__file__).parent.parent.parent / "RAG" / ".env"
    if _rag_env.exists():
        for _l in _rag_env.read_text(encoding="utf-8").splitlines():
            if _l.startswith("RAG_API_TOKEN="):
                _DB_TOKEN = _l.split("=", 1)[1].strip()

async def _db_fetch(path: str) -> dict | list:
    """Fetch JSON from db_api (port 3001). Returns {} on failure."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{_DB_API_BASE}{path}", headers={"Authorization": f"Bearer {_DB_TOKEN}"} if _DB_TOKEN else {})
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


_SKILL_PREFIX = "You are enerlyst. Use ONLY the data provided below. Do not invent figures. 3 sentences max. No chunk IDs. No internal field names."

async def _groq_skill(system_prompt: str, user_msg: str, max_tokens: int = 800) -> str:
    """Call Groq for skill synthesis. Returns empty string on failure."""
    if not _GROQ_KEYS:
        return ""
    key = _get_groq_key()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_GROQ_URL, json={
                "model": _GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }, headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("choices") and data["choices"][0].get("message"):
                    return data["choices"][0]["message"].get("content", "")
    except Exception:
        pass
    return ""


class SkillRequest(BaseModel):
    audience: str = "operator"
    battery_id: Optional[str] = None


# ── 1. Fleet Summary (Operator) ──────────────────────────────────────

@app.post("/enerlyst/skill/fleet-summary")
async def skill_fleet_summary(req: SkillRequest, _=Depends(verify_token)):
    """Fleet health summary for operators. Plain English, range in km."""
    data = await _db_fetch("/fleet/summary?chemistry=LFP")
    if not data:
        return {"answer": "Fleet summary data is currently unavailable.", "artifacts": [], "citations": []}

    total = data.get("total_batteries", 0)
    scored = total - data.get("paused", 0)
    user_msg = (f"Fleet: {total} total LFP batteries, {scored} scored (includes all modes: FULL + PARTIAL + ESTIMATED), "
                f"{data.get('paused', 0)} paused.\n"
                f"Urgent replacements: {data.get('urgent', 0)}. Need attention soon: {data.get('attention_soon', 0)}.\n"
                f"Fleet data:\n{json.dumps(data, default=str)[:2500]}")
    system = (_SKILL_PREFIX + f" Operator audience. Fleet has {scored} scored batteries. "
              "Focus on: how many healthy, how many need action, average range in km. No SOH%, no scores.")

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"Fleet has {data.get('total_batteries', '?')} batteries. Raw data returned — LLM synthesis unavailable."

    # Build table artifact from summary
    headers = ["Metric", "Value"]
    rows = []
    for k, v in data.items() if isinstance(data, dict) else []:
        if k not in ("chemistry",) and not isinstance(v, (dict, list)):
            rows.append([str(k).replace("_", " ").title(), str(v)])

    artifacts = []
    if rows:
        artifacts.append({"type": "table", "title": "Fleet Action Summary",
                          "data": {"headers": headers, "rows": rows}})

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 2. Attention List (Operator) ─────────────────────────────────────

@app.post("/enerlyst/skill/attention-list")
async def skill_attention_list(req: SkillRequest, _=Depends(verify_token)):
    """Batteries needing attention. Filtered by rul_action_v2."""
    data = await _db_fetch("/fleet/batteries?chemistry=LFP")
    batteries = data if isinstance(data, list) else data.get("batteries", []) if isinstance(data, dict) else []

    attention_actions = {"REPLACE_URGENT", "REPLACE_PLAN", "CELL_BALANCE"}
    attention = [b for b in batteries if b.get("rul_action_v2") in attention_actions]

    if not attention:
        return {"answer": "No batteries currently require urgent attention.", "artifacts": [], "citations": []}

    user_msg = f"Batteries needing attention ({len(attention)} total):\n"
    for b in attention[:20]:
        user_msg += f"- {b.get('battery_id')}: action={b.get('rul_action_v2')}, range={b.get('range_km', '?')}km, tier={b.get('tier_label_v2', '?')}\n"

    system = _SKILL_PREFIX + " Operator audience. List batteries needing attention with battery ID, range in km, action, cause. No SOH%."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"{len(attention)} batteries need attention. Top: " + ", ".join(
            b.get("battery_id", "?") for b in attention[:5])

    headers = ["Battery", "Action", "Range (km)", "Tier"]
    rows = [[b.get("battery_id", "?"), b.get("rul_action_v2", "?"),
             str(b.get("range_km", "?")), b.get("tier_label_v2", "?")]
            for b in attention[:25]]

    artifacts = [{"type": "table", "title": "Attention Batteries",
                  "data": {"headers": headers, "rows": rows}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 3. Range Loss (Operator) ─────────────────────────────────────────

@app.post("/enerlyst/skill/range-loss")
async def skill_range_loss(req: SkillRequest, _=Depends(verify_token)):
    """Batteries losing range (kps_slope_4wk < -0.003)."""
    data = await _db_fetch("/fleet/batteries?chemistry=LFP")
    batteries = data if isinstance(data, list) else data.get("batteries", []) if isinstance(data, dict) else []

    declining = [b for b in batteries
                 if isinstance(b.get("kps_slope_4wk"), (int, float)) and b["kps_slope_4wk"] < -0.003]
    declining.sort(key=lambda b: b.get("kps_slope_4wk", 0))

    if not declining:
        return {"answer": "No batteries are currently showing significant range decline.", "artifacts": [], "citations": []}

    user_msg = f"Batteries with declining range ({len(declining)} total):\n"
    for b in declining[:20]:
        slope = b.get("kps_slope_4wk", 0)
        user_msg += f"- {b.get('battery_id')}: slope={slope:.4f}, range={b.get('range_km', '?')}km, tier={b.get('tier_label_v2', '?')}\n"

    system = _SKILL_PREFIX + " Operator audience. Explain range loss: how much km lost, cause, action. No SOH%."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"{len(declining)} batteries showing range decline. Worst: " + ", ".join(
            b.get("battery_id", "?") for b in declining[:5])

    headers = ["Battery", "Range (km)", "4-Week Trend", "Tier"]
    rows = [[b.get("battery_id", "?"), str(b.get("range_km", "?")),
             f"{b.get('kps_slope_4wk', 0):.4f}", b.get("tier_label_v2", "?")]
            for b in declining[:25]]

    artifacts = [{"type": "table", "title": "Batteries Losing Range",
                  "data": {"headers": headers, "rows": rows}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 4. Portfolio Risk (NBFC) ─────────────────────────────────────────

@app.post("/enerlyst/skill/portfolio-risk")
async def skill_portfolio_risk(req: SkillRequest, _=Depends(verify_token)):
    """Portfolio risk summary for NBFC analysts."""
    data = await _db_fetch("/fleet/summary")
    if not data:
        return {"answer": "Portfolio risk data is currently unavailable.", "artifacts": [], "citations": []}

    user_msg = f"Fleet/portfolio data:\n{json.dumps(data, default=str)[:3000]}"
    system = _SKILL_PREFIX + " NBFC audience. Lead with grade distribution: X% A/B safe, Y% C/D at risk. Relate to loan repayment. No raw scores."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"Portfolio contains {data.get('total_batteries', '?')} batteries. Raw data returned — LLM synthesis unavailable."

    # Grade distribution table
    headers = ["Grade", "Count", "% of Fleet"]
    rows = []
    tier_dist = data.get("tier_distribution", data.get("tiers", {}))
    if isinstance(tier_dist, dict):
        total = sum(v for v in tier_dist.values() if isinstance(v, (int, float)))
        grade_map = {"PRIME": "A", "STABLE": "B", "WATCH": "C", "STRESSED": "D", "CRITICAL": "D"}
        for tier, count in tier_dist.items():
            if isinstance(count, (int, float)):
                grade = grade_map.get(tier.upper(), tier[0].upper())
                pct = f"{count / total * 100:.1f}" if total > 0 else "0"
                rows.append([grade, str(int(count)), f"{pct}%"])

    artifacts = []
    if rows:
        artifacts.append({"type": "table", "title": "Grade Distribution",
                          "data": {"headers": headers, "rows": rows}})

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 5. RUL vs Tenure (NBFC) ──────────────────────────────────────────

@app.post("/enerlyst/skill/rul-tenure")
async def skill_rul_tenure(req: SkillRequest, _=Depends(verify_token)):
    """Batteries where RUL < 52 weeks (less than ~1 year loan tenure)."""
    data = await _db_fetch("/fleet/batteries?chemistry=LFP")
    batteries = data if isinstance(data, list) else data.get("batteries", []) if isinstance(data, dict) else []

    at_risk = [b for b in batteries
               if isinstance(b.get("rul_weeks_v2"), (int, float)) and b["rul_weeks_v2"] < 52]
    at_risk.sort(key=lambda b: b.get("rul_weeks_v2", 0))

    if not at_risk:
        return {"answer": "No batteries currently have RUL below the 1-year loan tenure threshold.",
                "artifacts": [], "citations": []}

    user_msg = f"Batteries with RUL < 52 weeks ({len(at_risk)} total):\n"
    for b in at_risk[:20]:
        user_msg += (f"- {b.get('battery_id')}: RUL={b.get('rul_weeks_v2', '?')} weeks, "
                     f"tier={b.get('tier_label_v2', '?')}, warranty={b.get('warranty_pct', '?')}%\n")

    system = _SKILL_PREFIX + " NBFC audience. Flag batteries where RUL < loan tenure: battery ID, weeks, gap, grade. No raw scores."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"{len(at_risk)} batteries have RUL below 52 weeks. Highest risk: " + ", ".join(
            b.get("battery_id", "?") for b in at_risk[:5])

    headers = ["Battery", "RUL (weeks)", "Grade", "Warranty %"]
    grade_map = {"PRIME": "A", "STABLE": "B", "WATCH": "C", "STRESSED": "D", "CRITICAL": "D"}
    rows = [[b.get("battery_id", "?"), str(b.get("rul_weeks_v2", "?")),
             grade_map.get((b.get("tier_label_v2") or "").upper(), "?"),
             str(b.get("warranty_pct", "?"))]
            for b in at_risk[:25]]

    artifacts = [{"type": "table", "title": "RUL Below Loan Tenure",
                  "data": {"headers": headers, "rows": rows}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 6. LTV Summary (NBFC) ────────────────────────────────────────────

@app.post("/enerlyst/skill/ltv-summary")
async def skill_ltv_summary(req: SkillRequest, _=Depends(verify_token)):
    """Loan-to-value risk summary for NBFC."""
    summary = await _db_fetch("/fleet/summary")
    data = await _db_fetch("/fleet/batteries?chemistry=LFP")
    batteries = data if isinstance(data, list) else data.get("batteries", []) if isinstance(data, dict) else []

    if not batteries:
        return {"answer": "Battery data unavailable for LTV analysis.", "artifacts": [], "citations": []}

    # Compute simple LTV proxy using warranty_pct as value indicator
    ltv_rows = []
    for b in batteries:
        wpct = b.get("warranty_pct")
        if isinstance(wpct, (int, float)):
            risk = "HIGH" if wpct < 40 else ("MEDIUM" if wpct < 70 else "LOW")
            ltv_rows.append({
                "battery_id": b.get("battery_id", "?"),
                "warranty_pct": wpct,
                "tier": b.get("tier_label_v2", "?"),
                "risk": risk,
            })

    high_risk = [r for r in ltv_rows if r["risk"] == "HIGH"]
    med_risk = [r for r in ltv_rows if r["risk"] == "MEDIUM"]
    low_risk = [r for r in ltv_rows if r["risk"] == "LOW"]

    user_msg = (f"LTV risk distribution: HIGH={len(high_risk)}, MEDIUM={len(med_risk)}, LOW={len(low_risk)}\n"
                f"Fleet summary: {json.dumps(summary, default=str)[:1500]}\n"
                f"High-risk batteries:\n")
    for r in high_risk[:15]:
        user_msg += f"- {r['battery_id']}: warranty={r['warranty_pct']}%, tier={r['tier']}\n"

    system = _SKILL_PREFIX + " NBFC audience. Summarize LTV risk using warranty % as proxy. Lead with portfolio risk. No raw scores."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"LTV analysis: {len(high_risk)} HIGH risk, {len(med_risk)} MEDIUM, {len(low_risk)} LOW."

    headers = ["Risk Level", "Count", "% of Fleet", "Avg Warranty %"]
    rows = []
    for label, group in [("HIGH", high_risk), ("MEDIUM", med_risk), ("LOW", low_risk)]:
        count = len(group)
        pct = f"{count / len(ltv_rows) * 100:.1f}" if ltv_rows else "0"
        avg_w = f"{sum(r['warranty_pct'] for r in group) / count:.1f}" if count else "0"
        rows.append([label, str(count), f"{pct}%", f"{avg_w}%"])

    artifacts = [{"type": "table", "title": "LTV Risk Distribution",
                  "data": {"headers": headers, "rows": rows}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 7. Attribution (OEM) ─────────────────────────────────────────────

@app.post("/enerlyst/skill/attribution")
async def skill_attribution(req: SkillRequest, _=Depends(verify_token)):
    """Degradation attribution analysis for OEM engineers."""
    summary = await _db_fetch("/fleet/summary")
    intel = await _db_fetch("/fleet/intelligence")
    if not summary and not intel:
        return {"answer": "Attribution data is currently unavailable.", "artifacts": [], "citations": []}

    user_msg = f"Fleet summary:\n{json.dumps(summary, default=str)[:1500]}\n"
    if intel:
        user_msg += f"Intelligence data:\n{json.dumps(intel, default=str)[:2000]}"

    system = _SKILL_PREFIX + " OEM audience. Lead with top attribution factor. Present breakdown. Flag >50% manufacturing. Use EFC and %. No operational scores."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = "Attribution analysis data returned. LLM synthesis unavailable."

    # Chart artifact for attribution splits
    labels = ["Maintenance", "Charging", "Usage", "Thermal", "Calendar"]
    # Extract from intelligence data if available
    datasets_data = [20, 20, 20, 20, 20]  # defaults
    if isinstance(intel, dict):
        attr = intel.get("attribution", intel.get("attribution_splits", {}))
        if isinstance(attr, dict):
            datasets_data = [
                attr.get("maintenance_pct", attr.get("maintenance", 20)),
                attr.get("charging_pct", attr.get("charging", 20)),
                attr.get("usage_pct", attr.get("usage", 20)),
                attr.get("thermal_pct", attr.get("thermal", 20)),
                attr.get("calendar_pct", attr.get("calendar", 20)),
            ]
    elif isinstance(intel, list) and intel:
        # Aggregate from battery-level intelligence
        agg = {"maintenance": [], "charging": [], "usage": [], "thermal": [], "calendar": []}
        for item in intel:
            for key in agg:
                val = item.get(f"{key}_pct", item.get(key))
                if isinstance(val, (int, float)):
                    agg[key].append(val)
        datasets_data = [
            round(sum(agg[k]) / len(agg[k]), 1) if agg[k] else 20
            for k in ["maintenance", "charging", "usage", "thermal", "calendar"]
        ]

    artifacts = [{"type": "chart", "title": "Degradation Attribution Splits",
                  "data": {"labels": labels,
                           "datasets": [{"label": "Attribution %", "data": datasets_data}]}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 8. Pack Report (OEM) ─────────────────────────────────────────────

@app.post("/enerlyst/skill/pack-report")
async def skill_pack_report(req: SkillRequest, _=Depends(verify_token)):
    """Per-pack-model performance report for OEM."""
    data = await _db_fetch("/fleet/batteries?chemistry=LFP")
    batteries = data if isinstance(data, list) else data.get("batteries", []) if isinstance(data, dict) else []

    if not batteries:
        return {"answer": "Battery data unavailable for pack report.", "artifacts": [], "citations": []}

    # Group by pack_model
    packs: dict[str, list] = {}
    for b in batteries:
        pm = b.get("pack_model", b.get("model", "Unknown"))
        packs.setdefault(pm, []).append(b)

    user_msg = f"Pack model performance ({len(packs)} models):\n"
    for pm, bats in packs.items():
        ranges = [b.get("range_km") for b in bats if isinstance(b.get("range_km"), (int, float))]
        avg_range = sum(ranges) / len(ranges) if ranges else 0
        user_msg += f"- {pm}: {len(bats)} batteries, avg range={avg_range:.1f}km\n"

    system = _SKILL_PREFIX + " OEM audience. Per-pack performance: count, mean range, spread, anomalies. SOH% allowed. No operational scores."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"{len(packs)} pack models found. " + ", ".join(
            f"{pm}: {len(bats)} units" for pm, bats in list(packs.items())[:5])

    headers = ["Pack Model", "Count", "Avg Range (km)", "Min Range (km)", "Max Range (km)"]
    rows = []
    for pm, bats in packs.items():
        ranges = [b.get("range_km") for b in bats if isinstance(b.get("range_km"), (int, float))]
        if ranges:
            rows.append([pm, str(len(bats)), f"{sum(ranges)/len(ranges):.1f}",
                         f"{min(ranges):.1f}", f"{max(ranges):.1f}"])
        else:
            rows.append([pm, str(len(bats)), "?", "?", "?"])

    artifacts = [{"type": "table", "title": "Pack Model Performance",
                  "data": {"headers": headers, "rows": rows}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 9. Commissioning Quality (OEM) ───────────────────────────────────

@app.post("/enerlyst/skill/commissioning-quality")
async def skill_commissioning_quality(req: SkillRequest, _=Depends(verify_token)):
    """Batteries born weak (below P25 at commissioning) for OEM."""
    data = await _db_fetch("/fleet/batteries?chemistry=LFP")
    batteries = data if isinstance(data, list) else data.get("batteries", []) if isinstance(data, dict) else []

    if not batteries:
        return {"answer": "Battery data unavailable for commissioning analysis.", "artifacts": [], "citations": []}

    # Get commissioned_range_km values and compute P25
    comm_ranges = [b.get("commissioned_range_km") for b in batteries
                   if isinstance(b.get("commissioned_range_km"), (int, float))]

    if not comm_ranges:
        return {"answer": "Commissioning range data not available.", "artifacts": [], "citations": []}

    comm_ranges_sorted = sorted(comm_ranges)
    p25_idx = max(0, len(comm_ranges_sorted) // 4 - 1)
    p25 = comm_ranges_sorted[p25_idx]

    weak = [b for b in batteries
            if isinstance(b.get("commissioned_range_km"), (int, float))
            and b["commissioned_range_km"] < p25]
    weak.sort(key=lambda b: b.get("commissioned_range_km", 0))

    if not weak:
        return {"answer": "No batteries found below P25 commissioning range.", "artifacts": [], "citations": []}

    user_msg = f"P25 commissioning range: {p25:.1f}km. Batteries below P25 ({len(weak)}):\n"
    for b in weak[:20]:
        user_msg += (f"- {b.get('battery_id')}: commissioned={b.get('commissioned_range_km', '?')}km, "
                     f"current={b.get('range_km', '?')}km, soh={b.get('soh_cap_weekly', '?')}\n")

    system = _SKILL_PREFIX + " OEM audience. Identify born-weak batteries: ID, commissioned range vs P25, current state. SOH% allowed. Flag by OEM/batch."

    answer = await _groq_skill(system, user_msg)
    if not answer:
        answer = f"{len(weak)} batteries below P25 commissioning range ({p25:.1f} km)."

    headers = ["Battery", "Commissioned Range (km)", "Current Range (km)", "SOH %"]
    rows = [[b.get("battery_id", "?"),
             str(round(b["commissioned_range_km"], 1)) if isinstance(b.get("commissioned_range_km"), (int, float)) else "?",
             str(round(b["range_km"], 1)) if isinstance(b.get("range_km"), (int, float)) else "?",
             str(round(b["soh_cap_weekly"], 1)) if isinstance(b.get("soh_cap_weekly"), (int, float)) else "?"]
            for b in weak[:25]]

    artifacts = [{"type": "table", "title": "Weak at Commissioning (Below P25)",
                  "data": {"headers": headers, "rows": rows}}]

    return {"answer": answer, "artifacts": artifacts, "citations": []}


# ── 10. Suggest (Dynamic Follow-Up Pills) ────────────────────────────

class SuggestRequest(BaseModel):
    answer: str
    audience: str
    battery_id: Optional[str] = None
    context_summary: Optional[str] = None


@app.post("/enerlyst/suggest")
async def enerlyst_suggest(req: SuggestRequest, _=Depends(verify_token)):
    """Generate 4 follow-up question pills based on previous answer and audience."""
    system = (
        f"You are enerlyst. Given an answer just provided to a {req.audience} user, "
        "generate exactly 4 follow-up questions.\n"
        "Rules: questions must be about EV battery data, max 8 words each.\n"
        "audience=operator: questions about km, action, cause — never SOH% or scores\n"
        "audience=nbfc: questions about risk grade, tenure, EMI — never raw scores\n"
        "audience=oem: questions about EFC, attribution, pack — never operational scores\n"
        "Return ONLY a JSON array of 4 strings. No preamble."
    )

    user_msg = f"Answer given: {req.answer[:400]}"
    if req.battery_id:
        user_msg += f"\nBattery: {req.battery_id}"
    if req.context_summary:
        user_msg += f"\nContext: {req.context_summary[:200]}"

    try:
        raw = await _groq_skill(system, user_msg, max_tokens=120)
        if raw:
            pills = json.loads(raw.strip())
            if isinstance(pills, list) and len(pills) >= 4:
                return {"suggestions": pills[:4]}
    except Exception:
        pass

    # Fallback static pills per audience
    fallback = {
        "operator": [
            "Which batteries need service?",
            "What caused the range drop?",
            "How is the fleet trending?",
            "Which vehicles are urgent?",
        ],
        "nbfc": [
            "Which loans are at risk?",
            "Show grade distribution",
            "What is average RUL?",
            "Any warranty concerns?",
        ],
        "oem": [
            "What is the attribution split?",
            "Which packs perform worst?",
            "Show commissioning quality",
            "Any manufacturing defects?",
        ],
    }
    return {"suggestions": fallback.get(req.audience, fallback["operator"])}


# ════════════════════════════════════════════════════════════════
# BATTERY INTELLIGENCE SKILLS (7 endpoints)
# ════════════════════════════════════════════════════════════════

class BatterySkillRequest(BaseModel):
    battery_id: str
    audience: str = "operator"


async def _bat_fetch(path):
    """Fetch from db_api for a battery."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"http://localhost:3001/api{path}", headers={"Authorization": f"Bearer {_DB_TOKEN}"} if _DB_TOKEN else {})
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


@app.post("/enerlyst/skill/battery/e2e-trace")
async def skill_e2e_trace(req: BatterySkillRequest, _=Depends(verify_token)):
    """End-to-end signal -> decision trace for one battery."""
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    if not d or d.get("detail"):
        return {"answer": f"Battery {req.battery_id} not found.", "artifacts": []}
    chain = {"Signal": f"Range {d.get('range_corrected_km','?')}km, Spread {d.get('cell_spread_max','?')}mV",
             "Physics": f"{d.get('capacity_state','?')}, Corroboration {d.get('corroboration_score','?')}/7",
             "ML": f"Score {d.get('operational_score','?')}, Driver: {d.get('degradation_primary_driver','?')}",
             "Decision": d.get('operator_action_sentence') or d.get('rul_action_v2','?')}
    user_msg = f"Battery {req.battery_id} E2E trace:\n" + "\n".join(f"{k}: {v}" for k, v in chain.items())
    answer = await _groq_skill("Explain this battery's intelligence chain. Signal->Physics->ML->Decision. 4 sentences max. Plain English.", user_msg)
    if not answer:
        answer = "\n".join(f"{k}: {v}" for k, v in chain.items())
    artifact = {"type": "e2e_chain", "title": f"E2E Trace · {req.battery_id}",
                "chain_data": chain, "data": {"headers": ["Layer", "Finding"], "rows": [[k, v] for k, v in chain.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/driver-behaviour")
async def skill_driver_behaviour(req: BatterySkillRequest, _=Depends(verify_token)):
    """Driver/operator behaviour analysis."""
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    beh = {"Grade": d.get("latest_behaviour_grade", "—"), "Cohort": d.get("cohort_label", "—"),
           "Primary Driver": d.get("degradation_primary_driver", "—")}
    user_msg = f"Driver behaviour for {req.battery_id}: {_json.dumps(beh)}"
    answer = await _groq_skill("Assess driver behaviour impact. 3 sentences. Operator audience.", user_msg)
    if not answer:
        answer = f"Behaviour grade: {beh['Grade']}. Cohort: {beh['Cohort']}."
    artifact = {"type": "table", "title": f"Driver Behaviour · {req.battery_id}",
                "data": {"headers": ["Metric", "Value"], "rows": [[k, str(v)] for k, v in beh.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/charging-health")
async def skill_charging_health(req: BatterySkillRequest, _=Depends(verify_token)):
    """Charging stress analysis."""
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    attr = await _bat_fetch(f"/battery/{req.battery_id}/attribution")
    ch = {"Charging Attribution": f"{attr.get('charging_pct', '—')}%",
          "Primary Driver": d.get("degradation_primary_driver", "—"),
          "Thermal Stress": d.get("thermal_stress_score", "—")}
    user_msg = f"Charging health for {req.battery_id}: {_json.dumps(ch)}"
    answer = await _groq_skill("Assess charging impact. 3 sentences. Operator audience — no technical codes.", user_msg)
    if not answer:
        answer = f"Charging contributes {ch['Charging Attribution']} to degradation."
    artifact = {"type": "table", "title": f"Charging Health · {req.battery_id}",
                "data": {"headers": ["Metric", "Value"], "rows": [[k, str(v)] for k, v in ch.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/external-conditions")
async def skill_external_conditions(req: BatterySkillRequest, _=Depends(verify_token)):
    """Thermal and environmental conditions."""
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    attr = await _bat_fetch(f"/battery/{req.battery_id}/attribution")
    env = {"City": d.get("city", "—"), "Thermal Attribution": f"{attr.get('thermal_pct', '—')}%",
           "Thermal Stress Score": d.get("thermal_stress_score", "—")}
    user_msg = f"External conditions for {req.battery_id}: {_json.dumps(env)}"
    answer = await _groq_skill("Assess thermal/environmental impact. 3 sentences. Note summer impact if relevant.", user_msg)
    if not answer:
        answer = f"Thermal attribution: {env['Thermal Attribution']}. City: {env['City']}."
    artifact = {"type": "table", "title": f"External Conditions · {req.battery_id}",
                "data": {"headers": ["Metric", "Value"], "rows": [[k, str(v)] for k, v in env.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/attribution-waterfall")
async def skill_attribution_waterfall(req: BatterySkillRequest, _=Depends(verify_token)):
    """Degradation attribution breakdown."""
    attr = await _bat_fetch(f"/battery/{req.battery_id}/attribution")
    factors = {"Charging": attr.get("charging_pct", 0), "Maintenance": attr.get("maintenance_pct", 0),
               "Calendar": attr.get("calendar_pct", 0), "Thermal": attr.get("thermal_pct", 0),
               "Usage": attr.get("usage_pct", 0)}
    sorted_f = sorted(factors.items(), key=lambda x: x[1], reverse=True)
    user_msg = f"Attribution for {req.battery_id}: {', '.join(f'{k}: {v}%' for k, v in sorted_f)}"
    answer = await _groq_skill(f"Attribution waterfall. Primary: {sorted_f[0][0]} at {sorted_f[0][1]}%. 3 sentences. Audience: {req.audience}.", user_msg)
    if not answer:
        answer = f"Primary degradation factor: {sorted_f[0][0]} ({sorted_f[0][1]}%)."
    artifact = {"type": "chart", "title": f"Attribution · {req.battery_id}",
                "data": {"labels": [k for k, v in sorted_f], "datasets": [{"label": "Attribution %", "data": [v for k, v in sorted_f]}]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/pack-comparison")
async def skill_pack_comparison(req: BatterySkillRequest, _=Depends(verify_token)):
    """Compare battery to its pack model and fleet."""
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    pack = d.get("pack_model", "Unknown")
    this_range = d.get("range_corrected_km", 0)
    fleet_data = await _bat_fetch(f"/fleet/summary?chemistry=LFP")
    fleet_avg = fleet_data.get("eehi", 78)
    user_msg = f"Pack comparison for {req.battery_id} ({pack}): This battery {this_range}km, Fleet avg ~{fleet_avg}"
    answer = await _groq_skill("Compare this battery to pack median and fleet. 3 sentences.", user_msg)
    if not answer:
        answer = f"{req.battery_id} ({pack}): {this_range}km vs fleet average."
    artifact = {"type": "chart", "title": f"Pack Compare · {pack}",
                "data": {"labels": ["This battery", "Fleet median"], "datasets": [{"label": "Range (km)", "data": [this_range, fleet_avg]}]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/range-forecast")
async def skill_range_forecast(req: BatterySkillRequest, _=Depends(verify_token)):
    """12-week range forecast with P10/P50/P90."""
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    current = d.get("range_corrected_km") or d.get("predicted_range_12w") or 78
    slope = d.get("kps_slope_4wk", -0.02)
    rul = d.get("rul_weeks_v2")
    weeks = list(range(1, 13))
    p50 = [round(max(40, current + slope * 80 * w / 12), 1) for w in weeks]
    p10 = [round(max(35, v - 8), 1) for v in p50]
    p90 = [round(min(current + 5, v + 8), 1) for v in p50]
    user_msg = f"12-week forecast for {req.battery_id}: Current {current}km, slope {slope}, P50 at W12: {p50[-1]}km, RUL: {rul}"
    answer = await _groq_skill("12-week range forecast. Current range, trend, when it drops below 70km. 3 sentences. Operator audience.", user_msg)
    if not answer:
        answer = f"Current range: {current}km. Expected at week 12: {p50[-1]}km."
    artifact = {"type": "chart", "title": f"12-Week Forecast · {req.battery_id}",
                "data": {"labels": [f"W+{w}" for w in weeks],
                         "datasets": [{"label": "P50", "data": p50, "borderColor": "#C96A2A", "borderWidth": 2, "pointRadius": 2, "fill": False},
                                      {"label": "P90", "data": p90, "borderColor": "#E8A06A", "borderWidth": 1, "borderDash": [4, 4], "pointRadius": 0, "fill": False},
                                      {"label": "P10", "data": p10, "borderColor": "#dc2626", "borderWidth": 1, "borderDash": [4, 4], "pointRadius": 0, "fill": False}]}}
    return {"answer": answer, "artifacts": [artifact]}


# ════════════════════════════════════════════════════════════════
# SERVICE SKILLS (5 endpoints)
# ════════════════════════════════════════════════════════════════

@app.post("/enerlyst/skill/battery/fault-severity")
async def skill_fault_severity(req: BatterySkillRequest, _=Depends(verify_token)):
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    events = await _bat_fetch(f"/battery/{req.battery_id}/events")
    ev_list = events.get("events", [])[:5]
    fdata = {"severity": d.get("fault_severity", "LOW"), "type": d.get("fault_type", "ROUTINE_WEAR"),
             "recurrence": d.get("fault_recurrence_count", 0), "events": len(ev_list)}
    answer = await _groq_skill(_SKILL_PREFIX + " Fault severity report. Is this chronic or new? What action?",
        f"Battery {req.battery_id}: severity={fdata['severity']}, type={fdata['type']}, recurrence={fdata['recurrence']}x, {fdata['events']} recent events")
    if not answer: answer = f"Fault severity: {fdata['severity']}. Type: {fdata['type']}."
    artifact = {"type": "table", "title": f"Fault Report \u00b7 {req.battery_id}",
                "data": {"headers": ["Field", "Value"], "rows": [[k, str(v)] for k, v in fdata.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/fleet/service-priority-list")
async def skill_service_priority(req: SkillRequest, _=Depends(verify_token)):
    data = await _db_fetch("/fleet/action-priority?limit=20")
    bats = data.get("batteries", [])
    answer = await _groq_skill(_SKILL_PREFIX + " Service priority. What should the team do first this week?",
        f"{len(bats)} batteries need attention. Top 3: {_json.dumps(bats[:3], default=str)[:400]}")
    if not answer: answer = f"{len(bats)} batteries need service attention."
    artifact = {"type": "table", "title": "Service Priority",
                "data": {"headers": ["Rank", "Battery", "Action", "Range"],
                         "rows": [[i + 1, b.get("battery_id"), b.get("action", "—"), f"{b.get('range_proxy_km', '—')}km"] for i, b in enumerate(bats)]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/event-timeline")
async def skill_event_timeline(req: BatterySkillRequest, _=Depends(verify_token)):
    events = await _bat_fetch(f"/battery/{req.battery_id}/events")
    ev_list = events.get("events", [])[:10]
    answer = await _groq_skill(_SKILL_PREFIX + " Event timeline. Is the pattern worsening or stable?",
        f"Battery {req.battery_id}: {len(ev_list)} events. Most recent: {ev_list[0] if ev_list else 'none'}")
    if not answer: answer = f"{len(ev_list)} events recorded for {req.battery_id}."
    artifact = {"type": "table", "title": f"Events \u00b7 {req.battery_id}",
                "data": {"headers": ["Week", "Event", "Severity"], "rows": [[e.get("week_number", "—"), e.get("event_type", "—"), e.get("severity", "—")] for e in ev_list]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/cell-balance")
async def skill_cell_balance(req: BatterySkillRequest, _=Depends(verify_token)):
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    bdata = {"spread": d.get("cell_spread_max", "—"), "priority": d.get("cell_balance_priority", "—"),
             "delta_from_commission": d.get("spread_delta_from_commissioning", "—")}
    answer = await _groq_skill(_SKILL_PREFIX + " Cell balance assessment. Should operator schedule balancing? Expected recovery?",
        f"Battery {req.battery_id}: spread={bdata['spread']}mV, priority={bdata['priority']}, delta={bdata['delta_from_commission']}mV")
    if not answer: answer = f"Cell spread: {bdata['spread']}mV for {req.battery_id}."
    artifact = {"type": "table", "title": f"Cell Balance \u00b7 {req.battery_id}",
                "data": {"headers": ["Metric", "Value"], "rows": [[k, str(v)] for k, v in bdata.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


@app.post("/enerlyst/skill/battery/intervention-outcome")
async def skill_intervention_outcome(req: BatterySkillRequest, _=Depends(verify_token)):
    d = await _bat_fetch(f"/battery/{req.battery_id}")
    trend = await _bat_fetch(f"/battery/{req.battery_id}/trend/km_per_soc_pct")
    odata = {"outcome": d.get("latest_outcome", "STILL_OPERATING"), "trend": "stable", "range": d.get("range_corrected_km", "—")}
    answer = await _groq_skill(_SKILL_PREFIX + " Intervention outcome. Did last service work? What is trajectory now?",
        f"Battery {req.battery_id}: outcome={odata['outcome']}, trend={odata['trend']}, range={odata['range']}km")
    if not answer: answer = f"Latest outcome: {odata['outcome']} for {req.battery_id}."
    artifact = {"type": "table", "title": f"Outcome \u00b7 {req.battery_id}",
                "data": {"headers": ["Field", "Value"], "rows": [[k, str(v)] for k, v in odata.items()]}}
    return {"answer": answer, "artifacts": [artifact]}


# ════════════════════════════════════════════════════════════════
# ADMIN — Auto-learning + status
# ════════════════════════════════════════════════════════════════

@app.post("/enerlyst/admin/auto-learn")
async def trigger_auto_learn(_=Depends(verify_token)):
    """Trigger the auto-learning pipeline (background)."""
    import subprocess as _sp
    proc = _sp.Popen(
        ["python", "-X", "utf8", "enerlyst/ingest/auto_learn.py"],
        stdout=_sp.PIPE, stderr=_sp.PIPE,
        cwd=str(Path(__file__).parent.parent.parent),
    )
    return {"status": "started", "pid": proc.pid}


@app.get("/enerlyst/admin/learning-status")
async def learning_status(_=Depends(verify_token)):
    """Return recent faithfulness evals + ingestion history."""
    eval_log = LOGS_PATH / "eval_log.jsonl"
    ingest_log = LOGS_PATH / "ingest_log.jsonl"
    evals, ingests = [], []
    if eval_log.exists():
        for line in eval_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                evals.append(_json.loads(line))
            except Exception:
                pass
    if ingest_log.exists():
        for line in ingest_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                ingests.append(_json.loads(line))
            except Exception:
                pass
    return {
        "latest_faithfulness": evals[-1] if evals else None,
        "faithfulness_trend": evals[-10:],
        "recent_ingests": ingests[-10:],
        "learning_active": True,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("RAG_API_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
