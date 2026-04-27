# Extracted from enerlytik-studio/ March 30 2026
# Paths and schema references are STALE — update before use
# composite_score_v2 -> operational_score | 183 batteries -> 222
"""
router.py — Smart routing: decides which engine to use based on mode + question complexity.

Modes:
  smart  — auto-selects based on question complexity heuristics
  fast   — always uses RAG (pre-indexed, low latency)
  deep   — always uses multi-step agentic reasoning
"""

import re

COMPLEXITY_SIGNALS = [
    r"\bwhy\b.*\band\b",           # compound why questions
    r"\bcompare\b",
    r"\bdifference\b",
    r"\bhow does.{30,}work\b",     # long "how does X work" questions
    r"\bexplain.{30,}\b",          # long explain questions
    r"\bwhat.*and.*why\b",
    r"\brelationship\b",
    r"\binteract\b",
    r"\btrade.?off\b",
    r"\bwhen should\b",
    r"\bwhy.*instead\b",
]


def is_complex(question: str) -> bool:
    q = question.lower()
    if len(q.split()) > 25:
        return True
    for pattern in COMPLEXITY_SIGNALS:
        if re.search(pattern, q):
            return True
    return False


def route(mode: str, question: str) -> str:
    """
    Returns resolved engine: 'rag', 'direct', or 'deep'.
    """
    if mode == "fast":
        return "rag"
    if mode == "deep":
        return "deep"
    # Smart mode: use complexity heuristic
    if is_complex(question):
        return "deep"
    return "rag"
