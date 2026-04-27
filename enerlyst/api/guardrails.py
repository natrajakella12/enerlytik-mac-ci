"""
enerlytik RAG — Guardrails module.

Three functions used by rag_api.py and the chat backend before/after LLM calls:
  1. check_scope(question)    — gate inbound questions
  2. filter_response(text)    — sanitise outbound LLM text
  3. detect_battery_id(text)  — extract battery IDs for scoped DB queries
"""

import re

# ── Scope checking ────────────────────────────────────────────────────────

# Prompt-injection patterns (case-insensitive)
_INJECTION_RE = re.compile(
    r"\b(ignore\s+(previous|all|above|prior)\s+(instructions?|prompts?|rules?)"
    r"|forget\s+(everything|your|all|previous)"
    r"|pretend\s+(you\s+are|to\s+be|you're)"
    r"|you\s+are\s+now"
    r"|act\s+as\s+(if|a|an|the)"
    r"|\bDAN\b"
    r"|jailbreak"
    r"|do\s+anything\s+now"
    r"|bypass\s+(your|the|all)\s+(restrictions?|rules?|filters?|guardrails?)"
    r"|override\s+(your|the|all)\s+(instructions?|prompts?|rules?)"
    r"|system\s*prompt"
    r"|reveal\s+(your|the)\s+(instructions?|prompts?|rules?))",
    re.IGNORECASE,
)

# Blocked topic patterns
_BLOCKED_PATTERNS = [
    # Weather / climate forecasts (not battery-thermal)
    (re.compile(r"\b(weather\s+forecast|will\s+it\s+rain|temperature\s+tomorrow|weather\s+today)\b", re.I),
     "out_of_scope"),
    # General finance
    (re.compile(r"\b(stock\s*(market|price|ticker)|cryptocurrency|bitcoin|ethereum|forex|mutual\s*fund|sensex|nifty)\b", re.I),
     "general_finance"),
    # Competitor product details / pricing
    (re.compile(r"\b(ather|ola\s+s1|bounce|yulu|hero\s+electric|bajaj\s+chetak|tvs\s+iqube|revolt)\b.*\b(price|cost|buy|specs?|features?|review)\b", re.I),
     "competitor"),
    (re.compile(r"\b(compare|comparison|vs|versus)\b.*\b(ather|ola|bounce|yulu|hero\s+electric|bajaj|tvs|revolt)\b", re.I),
     "competitor"),
    # Code / essay / email writing unrelated to platform
    (re.compile(r"\b(write\s+(me\s+)?(a|an|the)\s+(essay|email|letter|poem|story|song|script|resume|cv))\b", re.I),
     "out_of_scope"),
    (re.compile(r"\b(write\s+(python|java|javascript|c\+\+|code|html|css)\b)", re.I),
     "out_of_scope"),
    # Personal advice
    (re.compile(r"\b(relationship|dating|diet|recipe|workout|medical\s+advice|health\s+tip)\b", re.I),
     "out_of_scope"),
    # Company internals
    (re.compile(r"\benerlytik('s|s)?\s+(investor|revenue|funding|valuation|team|roadmap|salary|employee)\b", re.I),
     "out_of_scope"),
    # General knowledge
    (re.compile(r"\b(who\s+is\s+the\s+president|capital\s+of|world\s+cup|movie|actor|actress)\b", re.I),
     "out_of_scope"),
]

# Allowed topic keywords (if at least one matches, question is allowed even if ambiguous)
_ALLOWED_RE = re.compile(
    r"\b(batter(y|ies)|SOH|SOC|SOP|RUL|degradation|cell\s*(spread|voltage|imbalance)"
    r"|LFP|NMC|NCA|BMS|charging|discharge|thermal|temperature"
    r"|fleet|vehicle|e-?rickshaw|2-?wheeler|3-?wheeler"
    r"|NBFC|loan|EMI|default|repo|collateral|LTV|LGD|NPA"
    r"|health\s*(score|index)|risk\s*(score|tier)|composite"
    r"|range|efficiency|km|mileage|trip|route"
    r"|SHAP|explainab|prediction|forecast"
    r"|driver|eco-?score|behaviour|behavior"
    r"|maintenance|service|warranty|swap"
    r"|CUSUM|Bollinger|PELT|anomaly|signal|alert|event"
    r"|NITI|FAME|subsidy|regulation|policy"
    r"|EV|electric\s*vehicle|Indian\s*(market|fleet|duty)"
    r"|BAT_|GFLP|cluster|tier|prime|stable|watch|stressed|critical"
    r"|enerlytik|platform|dashboard|report|intelligence)\b",
    re.IGNORECASE,
)

_REDIRECT_MESSAGES = {
    "out_of_scope": (
        "I focus on battery intelligence for your fleet. "
        "Can I help you with a battery health or operational question?"
    ),
    "injection": (
        "I can only assist with battery health and fleet intelligence."
    ),
    "competitor": (
        "I can tell you how your batteries are performing. "
        "Would you like to see your fleet's current health status?"
    ),
    "general_finance": (
        "I focus on battery intelligence for your fleet. "
        "Can I help you with a battery health or operational question?"
    ),
    "internal": (
        "That's proprietary to enerlytik. I can explain what the "
        "result means and what action to take \u2014 would that help?"
    ),
}


def check_scope(question: str) -> dict:
    """
    Gate inbound questions before RAG retrieval / LLM call.

    Returns:
        {
            "allowed": bool,
            "reason": str,        # "ok" | "injection" | "out_of_scope" | ...
            "redirect": str|None  # redirect message if blocked, else None
        }
    """
    q = question.strip()
    if not q:
        return {"allowed": False, "reason": "empty", "redirect": _REDIRECT_MESSAGES["out_of_scope"]}

    # 1. Prompt injection check (highest priority)
    if _INJECTION_RE.search(q):
        return {"allowed": False, "reason": "injection", "redirect": _REDIRECT_MESSAGES["injection"]}

    # 2. Check blocked patterns
    for pattern, category in _BLOCKED_PATTERNS:
        if pattern.search(q):
            # Exception: if the question also contains allowed battery/fleet terms,
            # let it through (e.g. "How does Ola S1 battery degrade in Delhi?")
            if category == "competitor" and _ALLOWED_RE.search(q):
                continue
            return {"allowed": False, "reason": category, "redirect": _REDIRECT_MESSAGES.get(category, _REDIRECT_MESSAGES["out_of_scope"])}

    # 3. If question matches allowed topics, pass
    if _ALLOWED_RE.search(q):
        return {"allowed": True, "reason": "ok", "redirect": None}

    # 4. Short questions without clear topic — allow with benefit of doubt
    #    (RAG retrieval will naturally scope the answer)
    if len(q.split()) <= 6:
        return {"allowed": True, "reason": "ok_short", "redirect": None}

    # 5. Longer questions with no battery/fleet signal — block
    return {"allowed": False, "reason": "out_of_scope", "redirect": _REDIRECT_MESSAGES["out_of_scope"]}


# ── Response filtering ────────────────────────────────────────────────────

# Technical terms → plain language replacements
_TERM_REPLACEMENTS = [
    ("km_per_soc_pct", "efficiency (km per charge %)"),
    ("cell_spread_mean", "cell voltage imbalance (average)"),
    ("cell_spread_max", "cell voltage imbalance (peak)"),
    ("cell_spread_slope", "cell imbalance trend"),
    ("dod_mean", "depth of discharge"),
    ("temp_max_clean", "peak temperature"),
    ("temp_avg_clean", "average temperature"),
    ("cusum_flag", "degradation trend flag"),
    ("bollinger_breach", "volatility breach"),
    ("thermal_gradient_max", "thermal gradient"),
    ("load_normalized_efficiency_residual", "load-adjusted efficiency"),
    ("current_to_speed_ratio", "power-to-speed ratio"),
    ("soc_corrected_voltage_sag", "voltage sag under load"),
    ("mileage_slope", "mileage trend"),
    ("mileage_mean", "average weekly mileage"),
    ("trip_count", "number of trips"),
    ("charge_cycles_delta", "charging cycles this period"),
    ("km_per_week", "weekly distance"),
    ("km_per_day", "daily distance"),
    ("avg_speed", "average speed"),
    ("km_sum", "total distance"),
    ("soc_sum", "total charge consumed"),
    ("signal_alert_tier", "alert level"),
    ("health_score", "health score"),
    ("risk_score", "risk score"),
    ("value_score", "asset value score"),
    ("composite_score", "overall score"),
    ("quality_tier", "data quality grade"),
    ("vehicle_weekly_features", "weekly performance data"),
    ("battery_health_scores_v2", "health scores table"),
    ("range_predictions_advanced_v2", "range forecasts"),
    ("telemetry_raw", "sensor data"),
]

# IP-sensitive patterns to redact
_REDACT_PATTERNS = [
    # Model file paths
    (re.compile(r"\b[\w/\\]*models/[\w._/\\]+\.pkl\b", re.I), "[model file]"),
    (re.compile(r"\b[\w/\\]*models/[\w._/\\]+\.json\b", re.I), "[model config]"),
    # Code snippets with internal logic
    (re.compile(r"(def\s+\w+\s*\(|import\s+\w+|class\s+\w+)", re.I), ""),
    # Database paths
    (re.compile(r"C:\\[^\s\"']+\.db\b", re.I), "[production database]"),
    (re.compile(r"/[\w/]+\.db\b"), "[production database]"),
    # Feature lists that look like column names (3+ underscored names in sequence)
    (re.compile(r"(\b\w+_\w+(?:_\w+)*\b(?:\s*,\s*)){3,}"), "[feature set] "),
    # Scoring formulas
    (re.compile(r"\b\d+\s*[x×*]\s*\(1\s*-\s*\w+_\w+\)", re.I), "[scoring component]"),
    # Weight values like 0.40, 0.35 in scoring context
    (re.compile(r"\b(WEIGHT_\w+|weight_\w+)\s*=\s*\d+\.\d+", re.I), "[internal weight]"),
    # Config references
    (re.compile(r"\bconfig\.\w+\b", re.I), "[configuration]"),
    # Pipeline script names
    (re.compile(r"\b\d{2}_\w+\.py\b"), "[pipeline stage]"),
    # Sprint references
    (re.compile(r"\bsprint\d+[a-z]?_v\d+\.py\b", re.I), "[training script]"),
]


def filter_response(response_text: str) -> str:
    """
    Sanitise LLM output before it reaches the user.
    Replaces technical column names with plain language and redacts IP-sensitive content.
    """
    text = response_text

    # Apply term replacements (case-insensitive, whole-word where possible)
    for technical, plain in _TERM_REPLACEMENTS:
        # Use word-boundary matching for terms with underscores
        pattern = re.compile(re.escape(technical), re.IGNORECASE)
        text = pattern.sub(plain, text)

    # Apply redaction patterns
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)

    # Clean up any double spaces or empty brackets left by redaction
    text = re.sub(r"  +", " ", text)
    text = re.sub(r"\[\]\s*", "", text)

    return text.strip()


# ── Confidence assessment ─────────────────────────────────────────────────

def assess_confidence(
    question: str,
    rag_results: list[dict],
    db_data: dict | None = None,
) -> dict:
    """
    Assess whether the system has enough context to answer reliably.

    Returns:
        {
            "confidence": float (0.0-1.0),
            "should_answer": bool,
            "caveat": str | None,
            "sources": list[str],
        }
    """
    if not rag_results:
        return {
            "confidence": 0.0,
            "should_answer": False,
            "caveat": "I don't have enough information to answer this reliably. This question has been logged for our team to address.",
            "sources": [],
        }

    # Collect sources
    sources = list({r.get("source", "") for r in rag_results if r.get("source")})

    # Score based on top result relevance
    top_score = rag_results[0].get("weighted_score", 0.0) if rag_results else 0.0

    # Bonus for validated results
    validated_count = sum(1 for r in rag_results if r.get("metadata", {}).get("validated", False))
    validated_bonus = min(0.15, validated_count * 0.03)

    # Bonus for DB data available
    db_bonus = 0.1 if db_data else 0.0

    # Multi-source bonus
    source_bonus = min(0.1, (len(sources) - 1) * 0.05) if len(sources) > 1 else 0.0

    confidence = min(1.0, top_score + validated_bonus + db_bonus + source_bonus)

    # Decision thresholds
    if confidence >= 0.45:
        should_answer = True
        caveat = None
        if confidence < 0.6:
            caveat = "This answer is based on limited context and may not be fully accurate."
    elif confidence >= 0.25:
        should_answer = True
        caveat = "I have partial information on this. The answer may be incomplete."
    else:
        should_answer = False
        caveat = "I don't have enough information to answer this reliably. This question has been logged for our team to address."

    return {
        "confidence": round(confidence, 3),
        "should_answer": should_answer,
        "caveat": caveat,
        "sources": sources,
    }


# ── Battery ID extraction ─────────────────────────────────────────────────

_BATTERY_ID_PATTERNS = [
    # BAT_LFP_001 .. BAT_LFP_171, BAT_NMC_001 .. BAT_NMC_027
    re.compile(r"\b(BAT_(?:LFP|NMC)_\d{3})\b", re.I),
    # GFLP serial numbers
    re.compile(r"\b(GFLP\w+)\b"),
    # Loose references like "battery 53", "LFP 101", "NMC 006"
    re.compile(r"\b(?:battery|bat|lfp|nmc)\s*#?\s*(\d{1,3})\b", re.I),
]


def detect_battery_id(text: str) -> str | None:
    """
    Extract a battery_id from user text for scoped DB queries.

    Returns the first matched battery ID normalised to BAT_LFP_NNN or BAT_NMC_NNN format,
    or None if no battery reference found.
    """
    for pattern in _BATTERY_ID_PATTERNS:
        m = pattern.search(text)
        if m:
            raw = m.group(1).upper()
            # Already in BAT_XXX_NNN format
            if raw.startswith("BAT_"):
                return raw
            # GFLP serial — return as-is for lookup
            if raw.startswith("GFLP"):
                return raw
            # Numeric only — need context to decide LFP vs NMC
            try:
                num = int(raw)
                if num <= 171:
                    # Check if "NMC" appears nearby
                    if re.search(r"\bNMC\b", text, re.I):
                        return f"BAT_NMC_{num:03d}"
                    return f"BAT_LFP_{num:03d}"
            except ValueError:
                pass
    return None
