# Extracted from EV_Platform_Explainability/ March 30 2026
# Cost model references Anthropic Claude — update for Groq pricing
"""
performance.py — Timing, token counting, cost estimation, session tracking.
"""

import time
import csv
import io
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
import tiktoken

# Claude Sonnet 4.6 pricing (per 1K tokens)
COST_INPUT_PER_1K  = 0.003
COST_OUTPUT_PER_1K = 0.015

_ENCODER = None

def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        try:
            _ENCODER = tiktoken.encoding_for_model("gpt-4o")  # cl100k_base — closest to Claude
        except Exception:
            _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def count_tokens(text: str) -> int:
    try:
        return len(_get_encoder().encode(text))
    except Exception:
        return len(text) // 4  # fallback estimate


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * COST_INPUT_PER_1K + output_tokens * COST_OUTPUT_PER_1K) / 1000


@dataclass
class QueryResult:
    mode: str                       # "rag" or "direct"
    question: str = ""
    answer: str = ""
    response_time_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    context_size: int = 0           # chunks (RAG) or files (Direct)
    context_label: str = ""         # "6 chunks" or "8 files"
    sources: list = field(default_factory=list)
    context_sent: str = ""
    timestamp: str = ""
    error: Optional[str] = None


@dataclass
class SessionStats:
    total_queries: int = 0
    rag_queries: int = 0
    direct_queries: int = 0
    total_cost: float = 0.0
    rag_total_time_ms: float = 0.0
    direct_total_time_ms: float = 0.0
    rag_total_tokens: int = 0
    direct_total_tokens: int = 0

    def record(self, result: QueryResult):
        self.total_queries += 1
        self.total_cost += result.cost_usd
        if result.mode == "rag":
            self.rag_queries += 1
            self.rag_total_time_ms += result.response_time_ms
            self.rag_total_tokens += result.input_tokens + result.output_tokens
        else:
            self.direct_queries += 1
            self.direct_total_time_ms += result.response_time_ms
            self.direct_total_tokens += result.input_tokens + result.output_tokens

    @property
    def rag_avg_time_ms(self) -> float:
        return self.rag_total_time_ms / self.rag_queries if self.rag_queries else 0

    @property
    def direct_avg_time_ms(self) -> float:
        return self.direct_total_time_ms / self.direct_queries if self.direct_queries else 0


def export_session_csv(history: list[QueryResult]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp", "mode", "question", "response_time_ms",
        "input_tokens", "output_tokens", "cost_usd",
        "context_size", "context_label",
    ])
    for r in history:
        writer.writerow([
            r.timestamp, r.mode, r.question[:120],
            f"{r.response_time_ms:.0f}", r.input_tokens, r.output_tokens,
            f"{r.cost_usd:.5f}", r.context_size, r.context_label,
        ])
    return buf.getvalue()
