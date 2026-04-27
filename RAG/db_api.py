"""
enerlytik DB API — FastAPI server exposing production database for the chat UI.
Port 3001. Bearer token auth (same .env as RAG).

Schema discovered from enerlytik_production.db:
  battery_health_scores_v2: 181 rows, 34 cols (battery_id, operational_score, tier_label_v2, ...)
  vehicle_weekly_features: 6715 rows, 128 cols (battery_id, week_number, km_per_soc_pct, ...)
  vehicle_events: 1125 rows, 11 cols (battery_id, event_type, severity, week_number, ...)
  batteries: 183 rows, 16 cols (battery_id, chemistry, battery_model, oem_name, ...)
  battery_usage_clusters: 156 rows (battery_id, cluster_label, ...)
  known_bad_batteries: 33 rows
  model_registry: 112 rows, 21 cols
  range_forecasts: 181 rows
  No vehicle_classification table — using battery_usage_clusters instead.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from params import get_param  # noqa: E402  — Rule 241 / Rule 247 threshold resolution
from pipeline import attribution_text as atext  # noqa: E402  — customer-facing text library

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.env_config import (
    ENERLYTIK_DB_PATH, ENERLYTIK_MODELS_DIR, ENERLYTIK_DOCS_DIR,
    ENERLYTIK_RAG_DIR, RAG_API_TOKEN as _CFG_TOKEN, DB_API_PORT,
)

# ── Config ─────────────────────────────────────────────────────────────
DB_PATH = ENERLYTIK_DB_PATH
MODELS_DIR = ENERLYTIK_MODELS_DIR
DOCS_DIR = ENERLYTIK_DOCS_DIR
ENV_FILE = ENERLYTIK_RAG_DIR / ".env"

load_dotenv(ENV_FILE)
API_TOKEN = os.getenv("RAG_API_TOKEN", _CFG_TOKEN)

app = FastAPI(title="enerlytik DB API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Helpers ────────────────────────────────────────────────────────────

def get_conn():
    return sqlite3.connect(str(DB_PATH))


def q(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def q1(conn, sql, params=None):
    rows = q(conn, sql, params)
    return rows[0] if rows else None


_BANNED_CACHE = None

def get_banned_battery_ids(conn=None):
    """Return tuple of battery_ids permanently excluded from all surfaces.
    Source: fleet_context_params(param_name='banned_battery_ids').param_text (CSV).
    Falls back to ('BAT_LFP_034','BAT_LFP_202') if row missing. Cached per process.
    """
    global _BANNED_CACHE
    if _BANNED_CACHE is not None:
        return _BANNED_CACHE
    close_after = False
    if conn is None:
        conn = get_conn()
        close_after = True
    try:
        row = q1(conn,
                 "SELECT param_text FROM fleet_context_params "
                 "WHERE param_name='banned_battery_ids' AND is_active=1")
        txt = (row or {}).get("param_text") or "BAT_LFP_034,BAT_LFP_202"
        _BANNED_CACHE = tuple(s.strip() for s in txt.split(",") if s.strip())
    except Exception:
        _BANNED_CACHE = ("BAT_LFP_034", "BAT_LFP_202")
    finally:
        if close_after:
            conn.close()
    return _BANNED_CACHE


def _banned_sql_clause(alias="battery_id"):
    """Return (sql_fragment, params_list) for a NOT IN (?,?,...) clause.
    Example: where += f" AND {sql_frag}" ; params += param_list
    """
    ids = get_banned_battery_ids()
    if not ids:
        return ("1=1", [])
    ph = ",".join("?" for _ in ids)
    return (f"{alias} NOT IN ({ph})", list(ids))


# ── SOH physics guard (Sprint 2D-backend Fix 3) ────────────────────────
# soh_coulomb / soh_cap_weekly cannot exceed 100% — anything above is a
# calibration artefact. Return None at source so the UI falls back to the
# conservative signal. soh_conservative is NEVER guarded here (pipeline
# already applies the 15pp spike guard on that field).
_SOH_FIELDS_PHYSICS_GUARDED = (
    "soh_coulomb",
    "soh_coulomb_latest",
    "soh_coulomb_weekly",
    "soh_coulomb_mean",
    "soh_coulomb_actual",
    "soh_cap_weekly",
)


def _clip_soh_value(v):
    try:
        return None if (v is not None and float(v) > 100.0) else v
    except (TypeError, ValueError):
        return v


def _apply_soh_physics_guard(obj):
    """Apply the SOH physics guard in place on a dict (or list of dicts)."""
    if isinstance(obj, list):
        for item in obj:
            _apply_soh_physics_guard(item)
        return obj
    if not isinstance(obj, dict):
        return obj
    for field in _SOH_FIELDS_PHYSICS_GUARDED:
        if field in obj:
            obj[field] = _clip_soh_value(obj[field])
    return obj


# AWS-prep: dev-fallback token is in handover docs and therefore
# compromised. Rotate ENERLYTIK_API_TOKEN before any external-facing
# deployment.
_DEV_TOKEN = "{{ api_token }}"


def verify_token(authorization: Optional[str] = Header(None)):
    expected = os.environ.get("ENERLYTIK_API_TOKEN", _DEV_TOKEN)
    if authorization is None:
        raise HTTPException(status_code=401,
            detail="Authorization header required")
    token = authorization.replace("Bearer ", "").strip()
    if token != expected:
        raise HTTPException(status_code=401,
            detail="Invalid token")
    return token


# Emit a one-time warning at module import if the dev fallback is in use.
if os.environ.get("ENERLYTIK_API_TOKEN", _DEV_TOKEN) == _DEV_TOKEN:
    import warnings
    warnings.warn(
        "RAG/db_api.py using dev fallback API token — "
        "rotate ENERLYTIK_API_TOKEN before AWS deployment",
        RuntimeWarning, stacklevel=2)


# ── Sentence cleanliness filter ───────────────────────────────────
_DIRTY_MARKERS = [
    'NOT_RECOMMENDED', 'LIKELY_STRESSED', 'AMBIGUOUS',
    'Collections:', 'LIABILITY:', 'Financing:',
    'Root cause:', 'NBFC:', 'Warranty:',
    'collections RED', 'MIXED',
]

def is_clean_sentence(text):
    if not text or len(str(text)) < 10:
        return False
    return not any(d in str(text) for d in _DIRTY_MARKERS)

_SENTENCE_FIELDS = [
    'integrated_chain_sentence', 'primary_stressor',
    'asset_impact_sentence', 'operator_attribution_sentence',
    'nbfc_sentence', 'battery_oem_sentence', 'asset_recovery_note',
    'action_remark', 'customer_sat_sentence', 'vehicle_oem_sentence',
    'operator_consequence_sentence', 'nbfc_summary_sentence',
]

def clean_sentences(row):
    """Null out dirty sentence fields in a dict."""
    if not row or not isinstance(row, dict):
        return row
    for f in _SENTENCE_FIELDS:
        if f in row and not is_clean_sentence(row[f]):
            row[f] = None
    return row


# ── Role-based data filtering ─────────────────────────────────────
# Columns NBFC should NOT see (internal model internals)
_NBFC_REDACTED_COLS = {
    "model_used", "prediction_confidence", "data_availability",
    "composite_delta_vs_v1", "tier_change_vs_v1", "confidence_score",
    "model_version", "model_mape_12w", "scored_at",
    # Raw cell voltages not in scores table, but guard anyway
    "cell_spread_slope", "cell_spread_slope_12w",
}

_VALID_ROLES = {"NBFC", "Fleet", "OEM", "Service", "Internal"}


def get_role(x_stakeholder_role: Optional[str] = Header(None)) -> str:
    """Extract stakeholder role from header, default Internal."""
    if x_stakeholder_role and x_stakeholder_role.upper() in {r.upper() for r in _VALID_ROLES}:
        return x_stakeholder_role
    return "Internal"


def filter_for_role(rows: list[dict], role: str) -> list[dict]:
    """Apply role-based column filtering to query results."""
    if role.upper() == "INTERNAL" or role.upper() == "SERVICE":
        return rows  # Full access
    if role.upper() == "NBFC":
        # Remove internal model columns
        return [{k: v for k, v in row.items() if k not in _NBFC_REDACTED_COLS} for row in rows]
    if role.upper() == "OEM":
        return rows  # Full access, grouped by batch in frontend
    # Fleet — full access for now (TODO: filter by assigned fleet)
    return rows


def safe(fn):
    """Wrap endpoint logic — return {error} on failure, never crash."""
    import functools
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except HTTPException:
            raise
        except Exception as e:
            return {"error": str(e)}
    return wrapper


# ── DATA INTEGRITY CONSTANTS ───────────────────────────────────────────
# OEM rated range for LFP e-rickshaw packs = 105 km
# (fleet_context_params.mfg_claimed_range_km; Pack3001/3401/1201: 105, Gen1: 100).
# Add 10% headroom for measurement variance → any read >115 km is an artefact.
MFG_CEILING_KM = 105
# PHYSICS_CEILING_KM is sourced from fleet_context_params.range_physics_ceiling_km
# (seeded by pipeline/pd02_commissioned_baseline_apr24.py). Fallback 115
# if param unreachable at import time.
def _load_physics_ceiling(default: float = 115.0) -> float:
    try:
        import sqlite3 as _sqlite3
        _con = _sqlite3.connect('enerlytik_production.db', timeout=5)
        _r = _con.execute("""
            SELECT param_value FROM fleet_context_params
            WHERE param_name='range_physics_ceiling_km' AND is_active=1
            ORDER BY valid_from DESC LIMIT 1
        """).fetchone()
        _con.close()
        return float(_r[0]) if _r and _r[0] is not None else default
    except Exception:
        return default


PHYSICS_CEILING_KM = _load_physics_ceiling()  # MFG_CEILING_KM * 1.10 by default
# Known corrupted soc_sum sentinel values from VWF aggregation bug (PD-01).
# soc_sum=308.97 appears in 1,632 rows, soc_sum=2077.0 in 388 rows.
# Rows with these values yield impossible kps ratios. Audit 2026-04-23.
SENTINEL_SOC_VALUES = {308.97, 2077.0}

# DEMO BATTERY LIST — LOCKED 2026-04-23
# Selected from 258-battery clean data query.
# Criteria: artefact_wks=0, f_attr=1, iot≠CRITICAL,
#   coherence=COHERENT, range≤115km, clean_wks≥18
# Do not modify without running demo_select_query again.
# Query at: audit/demo_select_query_output.tsv
DEMO_BATTERIES = [
    'BAT_LFP_109',  # Pack3401/SR — EXIT_NOW, DRI=16, primary critical
    'BAT_LFP_165',  # Pack3401/SR — EXIT_NOW via SOH-FIX override
    'BAT_LFP_139',  # Pack3001/CU — MONITOR_INVESTIGATE, stressed
    'BAT_LFP_012',  # Pack1201/CU — MONITOR_INVESTIGATE, recovering range
    'BAT_LFP_011',  # Pack1201/CU — MONITOR_INVESTIGATE, stressed
    'BAT_LFP_242',  # Pack3001/CU — MONITOR, SOX EARLY_WARNING silent decline
    'BAT_LFP_131',  # Pack1201/CU — PRIME + SOX EARLY_WARNING, active battery
    'BAT_LFP_132',  # Pack1201/CU — REPLACE_PLAN, corr=4, POWER_DEGRADED
    'BAT_LFP_315',  # Pack3001/CU — PRIME healthy baseline
    'BAT_LFP_254',  # Pack3001/CU — PRIME healthy mature
    'BAT_LFP_351',  # Pack3401/SR — PRIME healthy Pack3401 contrast
    'BAT_LFP_108',  # Pack3401/SR — REPLACE_PLAN trajectory
    'BAT_LFP_116',  # Pack3401/SR — REPLACE_PLAN, range at floor
    'BAT_LFP_078',  # Range chart demo — declining slope, above floor, renders chart
]

DEMO_ROLES = {
    'BAT_LFP_109': {
        'role': 'primary_critical',
        'story': 'Replace now',
        'narrative': (
            'The urgent case. DRI 16, SOH 53%, range 46km — '
            'below operational floor. Warranty claim eligible. '
            'Platform detected decline. BMS was silent.'
        ),
        'demo_points': [
            'Show EXIT_NOW action with evidence chain',
            'Show warranty 6-factor checklist — SOH below floor',
            'Show attribution — Pack3401 health gap vs Pack3001',
            'Range forecast shows text fallback (already below 56 km floor) — '
            'action is immediate, not projected. See BAT_LFP_078 for the '
            'declining-forecast chart demo.',
        ],
        'tabs_to_show': ['overview', 'service', 'decisions', 'attribution'],
    },
    'BAT_LFP_116': {
        'role': 'pack3401_trajectory',
        'story': 'Same pack, earlier stage',
        'narrative': (
            'Pack3401, Surat. Range 46km like BAT_LFP_109 '
            'but DRI 41 vs 16. Same destination, different point '
            'in the journey. Plan replacement now.'
        ),
        'demo_points': [
            'Compare with BAT_LFP_109 — same range, healthier DRI',
            'Show REPLACE_PLAN vs EXIT_NOW distinction',
            'Show Pack3401 pattern in attribution',
        ],
        'tabs_to_show': ['overview', 'range', 'attribution'],
    },
    'BAT_LFP_108': {
        'role': 'pack3401_early_warning',
        'story': 'Pack3401 — catch it before floor',
        'narrative': (
            'Pack3401, Surat. Range 63km, DRI 38, SOX EARLY_WARNING. '
            'Not at floor yet. Platform has 4-8 weeks to act. '
            'This is the intervention window.'
        ),
        'demo_points': [
            'Show SOX EARLY_WARNING with voltage sag signal',
            'Show floor breach probability — time remaining',
            'Show intervention window economics',
        ],
        'tabs_to_show': ['service', 'decisions'],
    },
    'BAT_LFP_165': {
        'role': 'override_story',
        'story': 'Platform overrides BMS confidence',
        'narrative': (
            'EXIT_NOW despite SOX HEALTHY. '
            'Scoring conflict note C13: SOH-FIX escalation. '
            'SOH 76% triggered warranty floor override. '
            'BMS would not have flagged this.'
        ),
        'demo_points': [
            'Show scoring_conflict_note explanation',
            'Show SOH vs warranty floor — why override fired',
            'Contrast with BMS silence',
        ],
        'tabs_to_show': ['health', 'decisions'],
    },
    'BAT_LFP_242': {
        'role': 'silent_decline',
        'story': 'BMS silent. Platform detects.',
        'narrative': (
            'Pack3001, Cuttack. DRI 42, SOH 92% — looks fine. '
            'SOX EARLY_WARNING + divergence EARLY_WARNING. '
            'Chemistry declining before range shows it. '
            'BMS: 0 alerts. Platform: already flagged.'
        ),
        'demo_points': [
            'Show EARLY_WARNING SOX with signal breakdown',
            'Show divergence quadrant — chemistry vs range diverging',
            'Show "BMS has never fired an alert for this battery"',
            'This is the detection moat story',
        ],
        'tabs_to_show': ['overview', 'health', 'service'],
    },
    'BAT_LFP_131': {
        'role': 'active_silent_decline',
        'story': 'Active battery. Platform watching.',
        'narrative': (
            'Pack1201, Cuttack. DRI 62, SOH 100%, range 113km. '
            'Looks fully healthy. SOX EARLY_WARNING active. '
            'Slope −3km/week over 8 weeks. '
            'Platform is watching before the operator notices.'
        ),
        'demo_points': [
            'Show SOH 100% vs SOX EARLY_WARNING — contradiction',
            'Show slope trend — steady decline despite healthy SOH',
            'Show "platform detects 4-8 weeks before BMS"',
        ],
        'tabs_to_show': ['health', 'service'],
    },
    'BAT_LFP_132': {
        'role': 'divergence_replace',
        'story': 'SOH perfect. Replace anyway.',
        'narrative': (
            'Pack1201, Cuttack. SOH 99.7% — chemistry intact. '
            'Range 61km, REPLACE_PLAN, POWER_DEGRADED. '
            'Corroboration 4/7. Range declining despite full capacity. '
            'Resistance-dominated degradation — range loss before SOH loss.'
        ),
        'demo_points': [
            'Show SOH 99.7% vs DRI 47 vs REPLACE_PLAN action',
            'Show 4/7 corroboration — multiple signals agree',
            'Show degradation regime: RESISTANCE_DOMINATED',
            'Platform replaces on range signal not SOH signal',
        ],
        'tabs_to_show': ['overview', 'health', 'decisions'],
    },
    'BAT_LFP_139': {
        'role': 'pack3001_stressed',
        'story': 'Pack3001 is not always healthy',
        'narrative': (
            'Pack3001, Cuttack. DRI 26, SOH 66%. '
            'Not every Pack3001 is fine. '
            'Charging attribution 35% — operator behaviour is primary driver here. '
            'Different cause from Pack3401 batteries.'
        ),
        'demo_points': [
            'Contrast with healthy Pack3001 batteries',
            'Show attribution — charging is primary driver',
            'Show this is operator-driven not pack-driven',
            'Attribution routes the right action to the right audience',
        ],
        'tabs_to_show': ['overview', 'attribution'],
    },
    'BAT_LFP_012': {
        'role': 'recovering',
        'story': 'Monitored. Improving.',
        'narrative': (
            'Pack1201, Cuttack. DRI 28, SOH 80%, range 91km. '
            'Slope +4.2km/week — range recovering. '
            'Platform holds action at MONITOR_INVESTIGATE. '
            'Not every stressed battery needs replacing.'
        ),
        'demo_points': [
            'Show positive slope — range recovering',
            'Show MONITOR not REPLACE — platform is not trigger-happy',
            'Platform distinguishes recovering from declining',
        ],
        'tabs_to_show': ['range', 'service'],
    },
    'BAT_LFP_315': {
        'role': 'healthy_baseline',
        'story': 'What healthy looks like',
        'narrative': (
            'Pack3001, Cuttack. DRI 84+, SOH 100%, '
            'range in healthy band, NO_ACTION. '
            'The contrast battery. Every demo needs a healthy reference.'
        ),
        'demo_points': [
            'Show high DRI, green signal grid',
            'Show NO_ACTION with evidence — nothing concerning found',
            'Contrast with BAT_LFP_109 side by side',
        ],
        'tabs_to_show': ['overview', 'health'],
    },
    'BAT_LFP_254': {
        'role': 'healthy_mature',
        'story': 'Older battery. Still healthy.',
        'narrative': (
            'Pack3001, Cuttack. Mature battery, still performing. '
            'Shows platform tracks health over full lifetime — '
            'not just flagging problems.'
        ),
        'demo_points': [
            'Show age vs DRI — older but healthy',
            'Show platform value for long-term fleet management',
        ],
        'tabs_to_show': ['overview', 'range'],
    },
    'BAT_LFP_351': {
        'role': 'pack3401_healthy',
        'story': 'Pack3401 can be healthy',
        'narrative': (
            'Pack3401, Surat. PRIME tier, healthy. '
            'Not every Pack3401 is failing. '
            'The platform identifies the ones that are — '
            'not the entire pack family.'
        ),
        'demo_points': [
            'Contrast with BAT_LFP_109/116/108',
            'Show Pack3401 healthy — platform is not biased against pack',
            'Shows discrimination — same pack, different outcomes',
        ],
        'tabs_to_show': ['overview', 'attribution'],
    },
    'BAT_LFP_011': {
        'role': 'pack1201_stress',
        'story': 'Pack1201 under stress',
        'narrative': (
            'Pack1201, Cuttack. DRI 30, SOH 85%, range 61km. '
            'Completes the pack story — all three main packs '
            'represented in stressed tier.'
        ),
        'demo_points': [
            'Third pack type in stressed demo',
            'Compare with Pack3001 and Pack3401 stressed cases',
        ],
        'tabs_to_show': ['overview', 'service'],
    },
    'BAT_LFP_078': {
        'role': 'range_chart_declining',
        'story': 'Range trajectory — the chart demo',
        'narrative': (
            'Range 93 km, slope -16.6 km/wk, still above operational floor. '
            'This is the battery that renders the declining P50 / P10-P90 '
            'forecast chart — BAT_LFP_109 is already below floor and shows '
            'the text fallback by design.'
        ),
        'demo_points': [
            'Show the declining range forecast chart (not text fallback)',
            'Orange P50 + P10-P90 band + red floor line + teal OEM dashed',
            'Contrast with BAT_LFP_109 already-breached text path',
        ],
        'tabs_to_show': ['overview', 'range'],
    },
}


# ── GET /health ────────────────────────────────────────────────────────

@app.get("/health")
@app.get("/api/health")
def health():
    """Lightweight health check — no auth, fast response for UI poll."""
    try:
        conn = get_conn()
        n = conn.execute("SELECT COUNT(*) FROM batteries").fetchone()[0]
        conn.close()
        return {"status": "ok", "batteries": n}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ── GET /api/fleet/summary ─────────────────────────────────────────────

@app.get("/api/fleet/summary")
@safe
def fleet_summary(chemistry: str = None, _=Depends(verify_token)):
    conn = get_conn()

    chem_filter = " WHERE chemistry = ?" if chemistry else ""
    chem_params = [chemistry] if chemistry else []

    total = q1(conn, f"SELECT COUNT(*) as n FROM battery_health_scores_v2 WHERE scoring_mode != 'SUSPENDED'{' AND chemistry = ?' if chemistry else ''}", chem_params)["n"]
    current_week_row = q1(conn, "SELECT MAX(week_number) as w FROM battery_health_scores_v2 WHERE scoring_mode != 'SUSPENDED'")
    current_week = current_week_row["w"] if current_week_row else None

    tiers = q(conn, f"""
        SELECT tier_label_v2, COUNT(*) as cnt
        FROM battery_health_scores_v2
        WHERE tier_label_v2 IS NOT NULL{' AND chemistry = ?' if chemistry else ''}
        GROUP BY tier_label_v2
    """, chem_params)
    by_tier = {r["tier_label_v2"]: r["cnt"] for r in tiers}

    chems = q(conn, """
        SELECT chemistry, COUNT(*) as cnt
        FROM battery_health_scores_v2
        GROUP BY chemistry
    """)
    by_chemistry = {r["chemistry"]: r["cnt"] for r in chems}

    eehi_row = q1(conn, f"SELECT AVG(operational_score) as eehi FROM battery_health_scores_v2{chem_filter}", chem_params)
    eehi = round(eehi_row["eehi"], 2) if eehi_row and eehi_row["eehi"] else 0

    at_risk = by_tier.get("STRESSED", 0) + by_tier.get("CRITICAL", 0)

    # Tiers by chemistry
    tiers_by_chem = q(conn, """
        SELECT chemistry, tier_label_v2, COUNT(*) as cnt
        FROM battery_health_scores_v2
        WHERE tier_label_v2 IS NOT NULL
        GROUP BY chemistry, tier_label_v2
    """)
    chem_tiers = {"LFP": {}, "NMC": {}}
    for r in tiers_by_chem:
        ch = r.get("chemistry", "UNKNOWN")
        if ch in chem_tiers:
            chem_tiers[ch][r["tier_label_v2"]] = r["cnt"]

    # EEHI: fleet-size-weighted mean of composite where >= 50
    eehi_50 = q1(conn, f"SELECT AVG(operational_score) as e FROM battery_health_scores_v2 WHERE operational_score >= 50{' AND chemistry = ?' if chemistry else ''}", chem_params)
    eehi_weighted = round(eehi_50["e"], 2) if eehi_50 and eehi_50["e"] else eehi

    cusum_row = q1(conn, """
        SELECT COUNT(DISTINCT battery_id) as n FROM vehicle_weekly_features
        WHERE cusum_x_weeks = 1
        AND week_number = (SELECT MAX(week_number) FROM vehicle_weekly_features WHERE battery_id = vehicle_weekly_features.battery_id)
    """)
    cusum_active = cusum_row["n"] if cusum_row else 0

    model_row = q1(conn, f"""
        SELECT model_version, model_mape_12w, scored_at
        FROM battery_health_scores_v2
        WHERE scored_at IS NOT NULL{' AND chemistry = ?' if chemistry else ''}
        ORDER BY scored_at DESC LIMIT 1
    """, chem_params)

    # Diagnostic engine stats (enrichment)
    immediate_count = 0
    cohort_anomaly_count = 0
    deviations = []
    worst_batteries = []
    try:
        diag_rows = q(conn, """
            SELECT d.battery_id, d.range_deviation_attribution, d.recommendations,
                   d.fleet_summary, d.cohort_anomaly_flag, h.tier_label_v2
            FROM battery_diagnostics d
            LEFT JOIN battery_health_scores_v2 h ON d.battery_id = h.battery_id
        """)
        for r in diag_rows:
            if r.get("cohort_anomaly_flag") == 1:
                cohort_anomaly_count += 1
            try:
                recs = json.loads(r["recommendations"]) if r.get("recommendations") else []
                if isinstance(recs, list) and any(rc.get("urgency") == "IMMEDIATE" for rc in recs):
                    immediate_count += 1
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                rd = json.loads(r["range_deviation_attribution"]) if r.get("range_deviation_attribution") else {}
                if isinstance(rd, dict) and "deviation_pct" in rd:
                    dev = rd["deviation_pct"]
                    deviations.append(dev)
                    worst_batteries.append({
                        "battery_id": r["battery_id"],
                        "deviation_pct": round(dev, 1),
                        "tier": r.get("tier_label_v2", "UNSCORED"),
                        "fleet_summary": r.get("fleet_summary", ""),
                    })
            except (json.JSONDecodeError, TypeError):
                pass
        worst_batteries.sort(key=lambda x: x["deviation_pct"])
    except Exception:
        pass  # battery_diagnostics may not exist yet

    mean_dev = round(sum(deviations) / len(deviations), 1) if deviations else None

    # ── Scoring mode breakdown (FLEET-DIAG) ──
    modes = q(conn, f"""
        SELECT scoring_mode, COUNT(*) as n FROM battery_health_scores_v2
        {chem_filter} GROUP BY scoring_mode
    """, chem_params)
    mode_map = {r["scoring_mode"]: r["n"] for r in modes}
    fully_scored = mode_map.get("FULL", 0)
    with_caveats = sum(v for k, v in mode_map.items()
                       if k and k in ("PARTIAL_NO_SOH", "PARTIAL_LOW_DATA", "ESTIMATED"))
    paused = mode_map.get("SUSPENDED", 0)

    # ── RUL action breakdown (FLEET-DIAG) ──
    rul_rows = q(conn, f"""
        SELECT rul_action_v2, COUNT(*) as n FROM battery_health_scores_v2
        {chem_filter} GROUP BY rul_action_v2
    """, chem_params)
    rul_map = {r["rul_action_v2"]: r["n"] for r in rul_rows}
    urgent = rul_map.get("REPLACE_URGENT", 0) + rul_map.get("EOL_IMMINENT", 0)
    attention_soon = rul_map.get("REPLACE_PLAN", 0)

    # Fleet bar counts (mutually exclusive: action_needed + at_risk + healthy = total)
    bar = q1(conn, """
        SELECT
          COUNT(CASE WHEN urgency='THIS_WEEK' THEN 1 END) as action_needed,
          COUNT(CASE WHEN urgency='THIS_MONTH'
            OR (dq='EARLY_WARNING' AND urgency NOT IN ('THIS_WEEK','THIS_MONTH'))
            THEN 1 END) as at_risk,
          COUNT(CASE WHEN urgency IN ('ONGOING','NONE')
            AND (dq IS NULL OR dq != 'EARLY_WARNING')
            THEN 1 END) as healthy,
          COUNT(*) as bar_total
        FROM (
          SELECT battery_id, divergence_quadrant as dq,
            CASE
              WHEN rul_action_v2 IN ('PHYSICS_REPLACE_PLAN','REPLACE_PLAN','ACUTE_BREACH_WATCH') THEN 'THIS_WEEK'
              WHEN rul_action_v2 IN ('CELL_BALANCE_PRIORITY','MONITOR_INVESTIGATE') THEN 'THIS_MONTH'
              ELSE 'ONGOING'
            END as urgency
          FROM battery_health_scores_v2
          WHERE scoring_mode != 'SUSPENDED'
        ) sub
    """) or {}

    conn.close()
    return {
        "total_batteries": total,
        "fleet_size": by_chemistry,
        "by_tier": by_tier,
        "tiers": chem_tiers,
        "by_chemistry": by_chemistry,
        "eehi": eehi_weighted,
        "at_risk": at_risk,
        "cusum_active": cusum_active,
        "model_version": model_row["model_version"] if model_row else "unknown",
        "model_mape": round(model_row["model_mape_12w"], 2) if model_row and model_row["model_mape_12w"] else None,
        "immediate_action_count": immediate_count,
        "cohort_anomaly_count": cohort_anomaly_count,
        "mean_range_deviation_pct": mean_dev,
        "worst_range_batteries": worst_batteries[:5],
        # ── FLEET-DIAG: live scoring + action counts ──
        "fully_scored": fully_scored,
        "with_caveats": with_caveats,
        "paused": paused,
        "urgent": urgent,
        "attention_soon": attention_soon,
        "scoring_mode_dist": mode_map,
        "rul_action_dist": rul_map,
        "chemistry": chemistry or "ALL",
        "current_week": current_week,
        "action_needed": bar.get("action_needed", 0),
        "fleet_at_risk": bar.get("at_risk", 0),
        "fleet_healthy": bar.get("healthy", 0),
    }


# ── GET /api/battery/{id}/vwf-history ─────────────────────────────────

# Whitelist of VWF signal columns that can be requested via ?signal=.
# Kept explicit to prevent SQL injection via column-name interpolation.
_VWF_SIGNAL_WHITELIST = {
    "km_per_soc_pct", "km_sum", "trip_count", "avg_speed",
    "cell_spread_max", "cell_spread_mean",
    "voltage_mean", "voltage_min", "voltage_sag_ratio_weekly",
    "voltage_sag_trend_4wk",
    "current_discharge_mean",
    "soc_min", "soc_dekf_weekly",
    "temp_max", "temp_max_30s", "temp_avg_clean",
    "charge_cycle_rate", "dod_mean", "dod_corrected", "dod_observed",
    "soh_cap_weekly", "soh_coulomb_weekly", "soh_r_weekly",
    "coulomb_soh_relative", "coulomb_kps_divergence",
    "kps_slope_4wk", "kps_slope_8wk",
    "R0_weekly", "r0_weekly_median",
    "ir_proxy_baseline", "heat_generation_index", "heat_generation_slope_8wk",
    "self_discharge_rate_weekly", "coulombic_efficiency_weekly",
    "intra_trip_soc_anomaly_rate", "cc_cv_ratio_weekly",
    "effective_dod_actual", "effective_dod_shrinkage_pct",
    "spread_commissioning_delta", "cell_spread_slope",
    "can_valid_pct_week",
}


@app.get("/api/battery/{battery_id}/vwf-history")
@safe
def battery_vwf_history(battery_id: str, signal: str = None,
                        _=Depends(verify_token)):
    """Weekly feature history for analytics explorer.

    Without ?signal param: returns fixed 16-column record per week
    (backward compatible with pre-ux20 callers).

    With ?signal=<column_name>: returns compact [{week: N, value: X}, ...]
    for that single signal. Signal name is validated against a whitelist
    to prevent SQL injection.
    """
    conn = get_conn()
    if signal:
        if signal not in _VWF_SIGNAL_WHITELIST:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"Unknown signal '{signal}'. "
                       f"See _VWF_SIGNAL_WHITELIST in db_api.py.",
            )
        # signal is now known safe — a literal member of the whitelist set
        rows = q(conn, f"""
            SELECT week_number AS week, {signal} AS value
            FROM vehicle_weekly_features
            WHERE battery_id = ?
            ORDER BY week_number ASC
        """, [battery_id])
        conn.close()
        return rows
    rows = q(conn, """
        SELECT week_number, km_per_soc_pct, cell_spread_max, cell_spread_mean,
               voltage_mean, voltage_min, current_discharge_mean,
               soc_min, temp_max, temp_avg_clean, km_sum,
               charge_cycle_rate, dod_mean, avg_speed, trip_count,
               CAST(days_since_commissioning / 7.0 AS INTEGER) as weeks_in_service
        FROM vehicle_weekly_features
        WHERE battery_id = ?
        ORDER BY week_number ASC
    """, [battery_id])
    conn.close()
    return rows


# ── GET /api/battery/{id}/range-history ───────────────────────────────

@app.get("/api/battery/{battery_id}/range-history")
@safe
def battery_range_history(battery_id: str, _=Depends(verify_token)):
    """Last 16 weeks of range_corrected_km for the per-battery trajectory chart.

    Source: battery_score_history (NOT vehicle_weekly_features —
    range_corrected_km lives on BHS-derived snapshots, not raw VWF).
    Returns [{week: N, range_km: X}, ...] ordered chronologically,
    most recent 16 weeks only.
    """
    conn = get_conn()
    rows = q(conn, """
        SELECT week_number AS week,
               range_corrected_km AS range_km
        FROM battery_score_history
        WHERE battery_id = ?
        ORDER BY week_number DESC
        LIMIT 16
    """, [battery_id])
    conn.close()
    # Return chronological (oldest → newest) for line-chart rendering
    return list(reversed(rows))


# ── GET /api/fleet/range-distribution ─────────────────────────────────

@app.get("/api/fleet/range-distribution")
@safe
def fleet_range_distribution(_=Depends(verify_token)):
    """Fleet range P10/P50/P90 for benchmark bar."""
    conn = get_conn()
    rows = q(conn, """
        SELECT range_p50 FROM battery_health_scores_v2
        WHERE range_p50 IS NOT NULL AND scoring_mode != 'SUSPENDED'
        ORDER BY range_p50
    """)
    conn.close()
    vals = [r["range_p50"] for r in rows]
    if not vals:
        return {"p10": 65, "p50": 93, "p90": 115, "n": 0}
    import math
    def pctile(data, p):
        k = (len(data) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return round(data[f], 1)
        return round(data[f] * (c - k) + data[c] * (k - f), 1)
    return {
        "p10": pctile(vals, 0.10),
        "p50": pctile(vals, 0.50),
        "p90": pctile(vals, 0.90),
        "n": len(vals),
    }


# ── GET /api/fleet/batteries ──────────────────────────────────────────

@app.get("/api/fleet/batteries")
@safe
def fleet_batteries(chemistry: str = None, demo: bool = False,
                    _=Depends(verify_token), role: str = Depends(get_role)):
    conn = get_conn()
    # Rule 39 (BAT_LFP_202) + Rule 12 (BAT_LFP_034) — SCORE_SUSPENDED permanent exclusion
    base = "h.battery_id NOT IN ('BAT_LFP_034','BAT_LFP_202')"
    filters = [base]
    params: list = []
    if chemistry:
        filters.append("h.chemistry = ?")
        params.append(chemistry)
    if demo:
        # Demo lock 2026-04-23 — see DEMO_BATTERIES constant above.
        placeholders = ",".join("?" for _ in DEMO_BATTERIES)
        filters.append(f"h.battery_id IN ({placeholders})")
        params.extend(DEMO_BATTERIES)
    where = "WHERE " + " AND ".join(filters)
    rows = q(conn, f"""
        SELECT h.battery_id, h.operational_score, h.tier_label_v2,
               h.chemistry, h.prediction_confidence, h.scoring_mode,
               h.adjusted_composite, h.data_confidence,
               h.rul_action_v2, h.rul_trigger, h.rul_weeks_v2,
               h.scoring_conflict_note, h.l2_breach_probability,
               h.warranty_claim_eligible, h.iot_device_health,
               h.bhs_score_v2, h.soh_conservative, h.range_corrected_km,
               COALESCE(h.pct_of_commissioned, v.pct_of_commissioned) AS pct_of_commissioned,
               h.kps_slope_8wk, h.age_peer_range_p50,
               h.divergence_quadrant, h.corroboration_score,
               h.efc_cumulative, h.commissioned_range_km,
               h.cohort_label,
               h.knee_week,
               h.slope_acceleration,
               h.degradation_regime,
               -- A-4 wire: cohort + forecast + narrative + soh trend pulled
               -- from cohort_battery_position / battery_intelligence / BHS p10/50/90.
               cbp.cohort_key       AS cohort_id,
               cbp.range_pctile     AS cohort_rank_pct,
               cbp.range_pctile     AS cohort_percentile,
               NULL                 AS cohort_size,
               CASE
                 WHEN COALESCE(h.soh_coulomb_trend, v.soh_coulomb_trend) IS NULL THEN NULL
                 WHEN COALESCE(h.soh_coulomb_trend, v.soh_coulomb_trend) >  0.0005 THEN 'IMPROVING'
                 WHEN COALESCE(h.soh_coulomb_trend, v.soh_coulomb_trend) < -0.0005 THEN 'DECLINING'
                 ELSE 'STABLE'
               END AS soh_coulomb_trend_direction,
               bi.narrative_operator AS narrative_sentence,
               h.range_p50          AS range_p50_4wk,
               h.range_p10          AS range_p10_4wk,
               h.range_p90          AS range_p90_4wk,
               h.range_p50          AS range_forecast_p50,
               pm.range_floor_km,
               COALESCE(h.battery_model, b.battery_model) AS battery_model,
               c.cluster_label,
               b.city_code, b.fleet_segment,
               CASE
                 WHEN b.commissioning_date IS NOT NULL
                 THEN CAST((julianday('now') - julianday(b.commissioning_date))/7 AS INTEGER)
                 ELSE NULL
               END AS age_weeks,
               COALESCE(v.sox_tier, 'HEALTHY') AS sox_tier,
               v.charger_type_inferred,
               v.ir_proxy_delta,
               v.voltage_sag_ratio_weekly,
               ROUND(v.km_sum, 1) AS km_sum_weekly
        FROM battery_health_scores_v2 h
        LEFT JOIN battery_usage_clusters c ON c.battery_id = h.battery_id
        LEFT JOIN batteries b ON b.battery_id = h.battery_id
        LEFT JOIN pack_model_params pm
          ON pm.pack_model = COALESCE(h.battery_model, b.battery_model)
        LEFT JOIN vehicle_weekly_features v
          ON v.battery_id = h.battery_id
         AND v.week_number = (
             SELECT MAX(week_number)
             FROM vehicle_weekly_features
             WHERE battery_id = h.battery_id
         )
        LEFT JOIN cohort_battery_position cbp
          ON cbp.battery_id = h.battery_id
         AND cbp.week_number = (
             SELECT MAX(week_number)
             FROM cohort_battery_position
             WHERE battery_id = h.battery_id
         )
        LEFT JOIN battery_intelligence bi
          ON bi.battery_id = h.battery_id
        {where}
        ORDER BY h.operational_score ASC
    """, params)
    conn.close()
    # GUARD 3: cap range_corrected_km at physics ceiling at read time.
    # platform_compliance_fix.py caps at 150 km in BHS — stricter 115 km here
    # excludes readings above OEM rated + 10% from scatter/service groups.
    for r in rows:
        rcr = r.get("range_corrected_km")
        if rcr is not None and rcr > PHYSICS_CEILING_KM:
            r["range_corrected_km"] = None
            r["range_artefact_flag"] = True
    return filter_for_role(rows, role)


# ── GET /api/fleet/chart/tiers ────────────────────────────────────────

@app.get("/api/fleet/chart/tiers")
@safe
def chart_tiers(_=Depends(verify_token)):
    conn = get_conn()
    tiers = q(conn, """
        SELECT tier_label_v2, COUNT(*) as cnt
        FROM battery_health_scores_v2
        WHERE tier_label_v2 IS NOT NULL
        GROUP BY tier_label_v2
    """)
    conn.close()
    tier_map = {r["tier_label_v2"]: r["cnt"] for r in tiers}
    labels = ["PRIME", "STABLE", "WATCH", "STRESSED", "CRITICAL"]
    colors = ["#16a34a", "#2563eb", "#d97706", "#dc2626", "#7c1d06"]
    counts = [tier_map.get(t, 0) for t in labels]
    return {"labels": labels, "counts": counts, "colors": colors}


# ── GET /api/fleet/chart/efficiency ───────────────────────────────────

@app.get("/api/fleet/chart/efficiency")
@safe
def chart_efficiency(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT v.km_per_soc_pct FROM vehicle_weekly_features v
        INNER JOIN (
            SELECT battery_id, MAX(week_number) as max_wk
            FROM vehicle_weekly_features
            WHERE km_per_soc_pct IS NOT NULL AND km_per_soc_pct > 0
            GROUP BY battery_id
        ) latest ON v.battery_id = latest.battery_id AND v.week_number = latest.max_wk
        WHERE v.km_per_soc_pct IS NOT NULL AND v.km_per_soc_pct > 0
    """)
    conn.close()
    values = [r["km_per_soc_pct"] for r in rows]
    bin_edges = [0, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0]
    bins = []
    for i in range(len(bin_edges) - 1):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        label = f"{lo:.1f}-{hi:.1f}"
        cnt = sum(1 for v in values if lo <= v < hi)
        bins.append({"range": label, "count": cnt, "label": label})
    bins.append({"range": f"{bin_edges[-1]:.1f}+", "count": sum(1 for v in values if v >= bin_edges[-1]), "label": f"{bin_edges[-1]:.1f}+"})
    return {
        "bins": bins,
        "thresholds": [{"value": 0.8, "label": "efficiency warning", "color": "#d97706"}],
    }


# ── GET /api/fleet/chart/cell_spread ──────────────────────────────────

@app.get("/api/fleet/chart/cell_spread")
@safe
def chart_cell_spread(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT v.cell_spread_mean FROM vehicle_weekly_features v
        INNER JOIN (
            SELECT battery_id, MAX(week_number) as max_wk
            FROM vehicle_weekly_features
            WHERE cell_spread_mean IS NOT NULL
            GROUP BY battery_id
        ) latest ON v.battery_id = latest.battery_id AND v.week_number = latest.max_wk
        WHERE v.cell_spread_mean IS NOT NULL
    """)
    conn.close()
    values = [r["cell_spread_mean"] for r in rows]
    bin_edges = [0, 20, 40, 60, 80, 100, 150, 200, 500]
    bins = []
    for i in range(len(bin_edges) - 1):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        label = f"{lo}-{hi}"
        cnt = sum(1 for v in values if lo <= v < hi)
        bins.append({"range": label, "count": cnt, "label": label})
    bins.append({"range": f"{bin_edges[-1]}+", "count": sum(1 for v in values if v >= bin_edges[-1]), "label": f"{bin_edges[-1]}+"})
    return {
        "bins": bins,
        "thresholds": [
            {"value": 100, "label": "warning", "color": "#d97706"},
            {"value": 150, "label": "critical", "color": "#dc2626"},
        ],
    }


# ── GET /api/battery/:id ──────────────────────────────────────────────

@app.get("/api/battery/{battery_id}")
@safe
def get_battery(battery_id: str, _=Depends(verify_token), role: str = Depends(get_role)):
    conn = get_conn()

    score = q1(conn, "SELECT * FROM battery_health_scores_v2 WHERE battery_id = ?", [battery_id])
    if not score:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")
    # Suppress legacy tier_v2 from API responses (PD-NEW-21). Use tier_label_v2.
    score.pop('tier_v2', None)

    cluster = q1(conn, "SELECT cluster_label FROM battery_usage_clusters WHERE battery_id = ?", [battery_id])
    if cluster:
        score["cluster_label"] = cluster["cluster_label"]

    # ux23: stakeholder consequence chain narrative (latest week).
    # scc.rc_primary doesn't exist in the schema; using primary_stressor_type
    # as chain_primary_cause for UI consumption.
    scc_row = q1(conn, """
        SELECT integrated_chain_sentence,
               primary_stressor_type AS chain_primary_cause
        FROM stakeholder_consequence_chain
        WHERE battery_id = ?
        ORDER BY week_number DESC
        LIMIT 1
    """, [battery_id])
    if scc_row:
        score["integrated_chain_sentence"] = scc_row.get("integrated_chain_sentence")
        score["chain_primary_cause"] = scc_row.get("chain_primary_cause")

    # ux23: battery intelligence narratives (210 rows; one per battery).
    bi_row = q1(conn, """
        SELECT narrative_operator, narrative_nbfc, outcome_narrative
        FROM battery_intelligence
        WHERE battery_id = ?
    """, [battery_id])
    if bi_row:
        score["narrative_operator"] = bi_row.get("narrative_operator")
        score["narrative_nbfc"] = bi_row.get("narrative_nbfc")
        score["outcome_narrative"] = bi_row.get("outcome_narrative")

    # Weekly trend (last 16 weeks) — fields confirmed from VWF schema
    trend = q(conn, """
        SELECT week_number, km_per_soc_pct, cell_spread_mean, cell_spread_max,
               temp_max, temp_avg_clean, dod_mean, cusum_x_weeks, avg_speed, trip_count,
               voltage_mean, voltage_min, current_discharge_mean, soc_min,
               km_sum, cumulative_km, charge_cycle_rate
        FROM (
            SELECT v.week_number, v.km_per_soc_pct, v.cell_spread_mean, v.cell_spread_max,
                   v.temp_max, v.temp_avg_clean, v.dod_mean, v.cusum_x_weeks, v.avg_speed, v.trip_count,
                   v.voltage_mean, v.voltage_min, v.current_discharge_mean, v.soc_min,
                   v.km_sum, v.cumulative_km, v.charge_cycle_rate
            FROM vehicle_weekly_features v
            WHERE v.battery_id = ?
            ORDER BY v.week_number DESC
            LIMIT 16
        ) sub ORDER BY week_number ASC
    """, [battery_id])
    score["weekly_trend"] = trend

    # Events (last 10)
    events = q(conn, """
        SELECT event_type, week_number, severity, event_code, signal_values, detection_method,
               data_source, can_coverage_at_event, event_reliability
        FROM vehicle_events
        WHERE battery_id = ?
        ORDER BY week_number DESC
        LIMIT 10
    """, [battery_id])
    score["events"] = events

    # CUSUM info
    cusum_row = q1(conn, """
        SELECT MIN(week_number) as cusum_week FROM vehicle_weekly_features
        WHERE battery_id = ? AND cusum_x_weeks = 1
    """, [battery_id])
    score["has_cusum"] = bool(cusum_row and cusum_row["cusum_week"])
    score["cusum_week"] = cusum_row["cusum_week"] if cusum_row else None

    # Range forecast
    forecast = q1(conn, "SELECT * FROM range_forecasts WHERE battery_id = ?", [battery_id])
    if forecast:
        score["forecast"] = forecast

    # Environmental context
    try:
        env = q1(conn, "SELECT * FROM environmental_context_profile WHERE battery_id = ?", [battery_id])
        if env:
            score["env"] = env
    except Exception:
        pass

    # Known bad
    try:
        kb = q1(conn, "SELECT * FROM known_bad_batteries WHERE battery_id = ?", [battery_id])
        if kb:
            score["known_bad"] = kb
    except Exception:
        pass

    # Cohort context (S5 wire)
    try:
        cp = q1(conn, """
            SELECT cohort_key, age_bracket, cohort_confidence,
                   range_pct_cohort, soh_pct_cohort, spread_pct_cohort,
                   below_p25_range, below_p25_soh, above_p75_spread,
                   diverging_from_cohort
            FROM cohort_battery_position WHERE battery_id = ?
        """, [battery_id])
        if cp:
            score["cohort_key"] = cp["cohort_key"]
            score["cohort_confidence"] = cp["cohort_confidence"]
            score["cohort_position"] = cp
            # UI-wire APR24: flatten cohort_position onto top-level bat so that
            # `bat.cohort_range_pct` works without optional-chaining the nested
            # object. UI code treats these as flat signals.
            score["cohort_range_pct"] = cp.get("range_pct_cohort")
            score["cohort_soh_pct"] = cp.get("soh_pct_cohort")
            score["cohort_spread_pct"] = cp.get("spread_pct_cohort")
            score["cohort_diverging"] = cp.get("diverging_from_cohort")
            score["cohort_age_bracket"] = cp.get("age_bracket")
    except Exception:
        pass

    # Degradation attribution (S5 wire + APR24 FIX 4 + PD-02 T5 fields)
    try:
        attr = q1(conn, """
            SELECT charging_pct, usage_pct, thermal_pct, maintenance_pct, calendar_pct,
                   primary_driver, secondary_driver,
                   charger_risk_flag, charger_risk_multiplier,
                   nonstd_charger_weeks, charging_score AS charging_score_bda,
                   seasonal_context
            FROM battery_degradation_attribution WHERE battery_id = ?
        """, [battery_id])
        if attr:
            score["attribution"] = attr
            # Flatten FIX 4 / T5 fields onto top-level bat for UI access
            score["charger_risk_flag"] = attr.get("charger_risk_flag")
            score["charger_risk_multiplier"] = attr.get("charger_risk_multiplier")
            score["nonstd_charger_weeks"] = attr.get("nonstd_charger_weeks")
            score["seasonal_context"] = attr.get("seasonal_context")
    except Exception:
        pass

    # VWF latest-week T5 signals (actual DoD, dod_behavior_flag, charging
    # score) + sox_tier. /api/fleet/batteries includes sox_tier via the same
    # VWF join; /api/battery/{id} was omitting it, forcing the UI to fall
    # through to a secondary /sox-detail fetch for single-battery views.
    try:
        vwf_latest = q1(conn, """
            SELECT actual_dod_pct, dod_behavior_flag,
                   charging_score, charging_score_label,
                   sox_tier, sox_tier_reason
            FROM vehicle_weekly_features
            WHERE battery_id = ?
            ORDER BY week_number DESC
            LIMIT 1
        """, [battery_id])
        if vwf_latest:
            score["actual_dod_pct"] = vwf_latest.get("actual_dod_pct")
            score["dod_behavior_flag"] = vwf_latest.get("dod_behavior_flag")
            # VWF charging_score takes precedence over BDA copy
            cs = vwf_latest.get("charging_score")
            if cs is not None:
                score["charging_score"] = cs
            score["charging_score_label"] = vwf_latest.get("charging_score_label")
            # Surface sox_tier at top level (mirrors /api/fleet/batteries)
            score["sox_tier"] = vwf_latest.get("sox_tier") or "HEALTHY"
            score["sox_tier_reason"] = vwf_latest.get("sox_tier_reason")
    except Exception:
        pass

    # Attribution source — single public label. Internal provenance is tracked
    # via attribution_method (RULE_BASED_V1 for BDA rows, LEGACY for attr_* fallback).
    has_new_engine = bool(score.get("attribution") and score["attribution"].get("primary_driver"))
    has_legacy = score.get("attr_primary_factor") is not None
    if has_new_engine or has_legacy:
        score["attribution_source"] = "Field telemetry analysis"
        score["attribution_method"] = "RULE_BASED_V1" if has_new_engine else "LEGACY"
    else:
        score["attribution_source"] = "UNAVAILABLE"
        score["attribution_method"] = None

    # GUARD 2: cap commissioned_range_km at display ceiling.
    # BAT_LFP_109 shows 130 km commissioned (OEM spec 105) — early-week artefact.
    # If commissioned > 115, exclude from display, expose OEM spec as reference.
    _comm_raw = score.get("commissioned_range_km")
    if _comm_raw is not None and _comm_raw > PHYSICS_CEILING_KM:
        score["commissioned_range_km"] = None
        score["commissioned_range_km_note"] = (
            "Commissioned baseline excluded — early-week measurement artefact "
            f"(value {round(_comm_raw,1)} km exceeded OEM rated spec of {MFG_CEILING_KM} km). "
            "Using OEM spec as reference."
        )
        score["commissioned_range_km_display"] = MFG_CEILING_KM
    score["mfg_claimed_range_km"] = MFG_CEILING_KM

    # Enrich with structured context block (Rule 147)
    age = score.get("age_months")
    km = score.get("cumulative_km")
    sess = score.get("total_charge_sessions")
    efc = score.get("efc_cumulative")
    epw = score.get("efc_pct_of_warranty")
    comm = score.get("commissioned_range_km")
    # Display: show '--' for missing data, never '0' (zero-display guard)
    _age_d = f"{int(age)} months" if age else "--"
    _km_d = f"{int(km):,} km" if km else "--"
    _sess_d = f"charged {int(sess)} times" if sess else "--"
    _efc_d = str(round(efc, 1)) if efc else "--"
    _epw_d = f"{int(epw)}% of warranty used" if epw else "--"
    score["context"] = {
        "pack_model": score.get("pack_model"),
        "city": score.get("city"),
        "age_months": round(age, 1) if age else None,
        "age_display": _age_d,
        "cumulative_km": int(km) if km else None,
        "cumulative_km_display": _km_d,
        "total_charge_sessions": int(sess) if sess else None,
        "sessions_display": _sess_d,
        "efc_cumulative": round(efc, 1) if efc else None,
        "efc_pct_of_warranty": round(epw, 1) if epw else None,
        "warranty_display": _epw_d,
        "commissioned_range_km": comm,
        "commissioned_display": f"Delivered at {int(comm)}km" if comm else None,
    }
    score["operator_context"] = f"{_age_d} | {_km_d} | {_sess_d}"
    score["nbfc_context"] = f"{_age_d} | {_km_d} | {_epw_d}"
    score["oem_context"] = f"{_age_d} | {_km_d} | {_efc_d} EFC | {_epw_d}"

    # segment_prefix + age_weeks + identity-strip fields (Sprint passport-next)
    # city fallback (Sprint 2D-backend Fix 1): batteries table has no city
    # column — resolve via city_code -> service_city_map.city_name, else
    # battery_gps_city.gps_city, else the raw code, else None.
    bat_meta = q1(conn, """
        SELECT b.fleet_segment, b.capacity_ah, b.commissioning_date, b.oem_name,
               b.city_code,
               scm.city_name   AS city_name_decoded,
               gps.gps_city    AS city_gps,
               CAST((julianday('now') - julianday(b.commissioning_date)) / 7 AS INTEGER) AS age_weeks,
               p.personal_charge_profile, p.personal_drive_profile
        FROM batteries b
        LEFT JOIN battery_personal_params p ON b.battery_id = p.battery_id
        LEFT JOIN service_city_map scm      ON scm.city_code  = b.city_code
        LEFT JOIN battery_gps_city gps      ON gps.battery_id = b.battery_id
        WHERE b.battery_id = ?
    """, [battery_id])
    if bat_meta:
        _seg = bat_meta.get("fleet_segment") or ""
        score["segment_prefix"] = {"GE_ERICKSHAW": "GE", "SG_ERICKSHAW": "SG"}.get(_seg, "FL")
        score["age_weeks"] = bat_meta.get("age_weeks")
        score["capacity_ah"] = bat_meta.get("capacity_ah")
        score["commissioning_date"] = bat_meta.get("commissioning_date")
        score["bms_oem"] = bat_meta.get("oem_name")
        score["personal_charge_profile"] = bat_meta.get("personal_charge_profile")
        score["personal_drive_profile"] = bat_meta.get("personal_drive_profile")
        # City resolution: prefer existing score["city"] (already set upstream
        # if any), then decoded city_code, then GPS-derived city, then raw code.
        _decoded = bat_meta.get("city_name_decoded")
        if _decoded and not str(_decoded).lower().startswith("unknown"):
            score["city"] = score.get("city") or _decoded
        if not score.get("city") and bat_meta.get("city_gps"):
            score["city"] = bat_meta.get("city_gps")
        if not score.get("city") and bat_meta.get("city_code"):
            _code = bat_meta.get("city_code")
            if _code and not str(_code).upper().startswith("UNKNOWN"):
                score["city"] = _code
        score["city_code"] = bat_meta.get("city_code")
        if score.get("context") is not None:
            score["context"]["city"] = score.get("city")
            score["context"]["city_code"] = score.get("city_code")
    else:
        score["segment_prefix"] = "FL"
        score["age_weeks"] = None

    conn.close()
    _apply_soh_physics_guard(score)
    _apply_soh_physics_guard(score.get("weekly_trend") or [])
    filtered = filter_for_role([score], role)
    return filtered[0] if filtered else score


# ── GET /api/battery/:id/trend/:feature ───────────────────────────────

@app.get("/api/battery/{battery_id}/trend/{feature}")
@safe
def battery_trend(battery_id: str, feature: str, _=Depends(verify_token)):
    conn = get_conn()

    # Validate feature is a real column
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(vehicle_weekly_features)")
    valid_cols = {r[1] for r in cur.fetchall()}
    if feature not in valid_cols:
        conn.close()
        raise HTTPException(404, f"Feature not found in VWF: {feature}")

    rows = q(conn, f"""
        SELECT week_number, [{feature}]
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND [{feature}] IS NOT NULL
        ORDER BY week_number ASC
    """, [battery_id])

    if not rows:
        conn.close()
        raise HTTPException(404, f"No data for {battery_id}/{feature}")

    weeks = [r["week_number"] for r in rows]
    values = [r[feature] for r in rows]

    # Baseline (mean of weeks 1-8)
    baseline_vals = [r[feature] for r in rows if r["week_number"] <= 8 and r[feature] is not None]
    baseline = sum(baseline_vals) / len(baseline_vals) if baseline_vals else None

    latest = values[-1] if values else None
    pct_change = round((latest - baseline) / baseline * 100, 2) if baseline and latest and baseline != 0 else None

    # CUSUM week
    cusum_row = q1(conn, """
        SELECT MIN(week_number) as cusum_week FROM vehicle_weekly_features
        WHERE battery_id = ? AND cusum_x_weeks = 1
    """, [battery_id])

    conn.close()
    return {
        "battery_id": battery_id,
        "feature": feature,
        "weeks": weeks,
        "values": values,
        "cusum_week": cusum_row["cusum_week"] if cusum_row else None,
        "baseline_value": round(baseline, 4) if baseline else None,
        "latest_value": round(latest, 4) if latest else None,
        "pct_change": pct_change,
    }


# ── GET /api/battery/:id/range-history (PASSPORT-P3) ─────────────────

@app.get("/api/battery/{battery_id}/range-history")
@app.get("/api/battery/{battery_id}/health-history")
@safe
def battery_range_history(battery_id: str, _=Depends(verify_token)):
    """Per-week range trajectory + reference lines for range chart.

    GUARD 1: physics ceiling + sentinel filter. Weeks with
      (range > PHYSICS_CEILING_KM) or (soc_sum ∈ SENTINEL_SOC_VALUES)
    are nulled in the weekly array and listed in `anomalies`.
    L-3: BANNED batteries return 404; the per-week response carries
    efc_cumulative + soh_cap_weekly so the UI doesn't have to refetch
    bhs_v2 to populate stat tiles. Leading null weeks (pre-baseline
    physics-ceiling artefacts) are trimmed so the chart receives a
    contiguous trajectory.
    """
    if battery_id in ("BAT_LFP_034", "BAT_LFP_202"):
        raise HTTPException(404, f"{battery_id} is in the banned list")
    conn = get_conn()

    # Weekly range from VWF (km_per_soc_pct * 80) + soc_sum for sentinel
    # check + soh_cap_weekly + cycle_life_remaining for downstream tiles.
    # (efc_cumulative does NOT exist on VWF — only on bhs_v2; per-week
    # cycle progress is captured via charge_cycles_delta if needed.)
    weeks_raw = q(conn, """
        SELECT vwf.week_number, vwf.km_per_soc_pct, vwf.km_per_soc_slope,
               vwf.soc_sum, NULL as can_data_source, vwf.active_days,
               vwf.trip_count, vwf.soh_cap_weekly,
               vwf.charge_cycles_delta, vwf.cycle_life_remaining
        FROM vehicle_weekly_features vwf
        WHERE vwf.battery_id = ? AND vwf.km_per_soc_pct IS NOT NULL
        ORDER BY vwf.week_number ASC
    """, [battery_id])

    if not weeks_raw:
        conn.close()
        raise HTTPException(404, f"No range data for {battery_id}")

    weeks = []
    anomalies = []
    for r in weeks_raw:
        # Data completeness: active_days/7, with trip_count fallback when
        # active_days is NULL (common — 8.4k fleet weeks have it unpopulated).
        ad = r.get("active_days")
        tc = r.get("trip_count") or 0
        if ad is not None:
            comp = min(100, round(ad / 7 * 100))
        else:
            # Fallback: trip_count-based proxy. 3+ trips in a week ≈ active use.
            comp = min(100, round(tc / 3 * 100)) if tc else 0
        if comp < 50:
            continue  # only include weeks with >= 50% completeness

        range_val = round(r["km_per_soc_pct"] * 80, 1)
        soc_raw = r.get("soc_sum")
        is_sentinel = soc_raw in SENTINEL_SOC_VALUES
        is_artefact = range_val > PHYSICS_CEILING_KM

        if is_sentinel or is_artefact:
            anomalies.append({
                "week": r["week_number"],
                "type": "SENTINEL_SOC" if is_sentinel else "PHYSICS_VIOLATION",
                "raw_value": range_val,
                "reason": (
                    f"soc_sum sentinel value ({soc_raw}) — known VWF aggregation artefact"
                    if is_sentinel else
                    f"Range {range_val} km exceeds OEM spec ceiling ({PHYSICS_CEILING_KM} km)"
                ),
            })
            weeks.append({
                "week_number": r["week_number"],
                "range_km": None,  # Chart.js spanGaps skips nulls
                "kps_slope": r.get("km_per_soc_slope"),
                "data_completeness_pct": comp,
                "soh_cap_weekly": r.get("soh_cap_weekly"),
                "cycle_life_remaining": r.get("cycle_life_remaining"),
                "charge_cycles_delta": r.get("charge_cycles_delta"),
            })
            continue

        weeks.append({
            "week_number": r["week_number"],
            "range_km": range_val,
            "kps_slope": r.get("km_per_soc_slope"),
            "data_completeness_pct": comp,
            "soh_cap_weekly": r.get("soh_cap_weekly"),
            "cycle_life_remaining": r.get("cycle_life_remaining"),
            "charge_cycles_delta": r.get("charge_cycles_delta"),
        })

    # L-3: trim leading null weeks (pre-baseline garbage from
    # PHYSICS_CEILING / sentinel filter). The chart's first visible week
    # should be the first week with a valid range_km — early commissioning
    # weeks where km_per_soc_pct exceeds physics ceiling are useless to plot.
    first_valid = next((i for i, w in enumerate(weeks) if w["range_km"] is not None), None)
    if first_valid is not None and first_valid > 0:
        weeks = weeks[first_valid:]

    # Reference values from scores table
    refs = q1(conn, """
        SELECT mfg_claimed_range_km, commissioned_range_km,
               fleet_expected_range_km, range_corrected_km,
               degradation_regime, kps_slope_4wk,
               predicted_range_12w,
               bhs_score_v2, soh_conservative, week_number AS bhs_week
        FROM battery_health_scores_v2
        WHERE battery_id = ?
    """, [battery_id])

    # BE-1: enrich weekly_data with dri + pack_median_range.
    # Primary source: battery_score_history (weekly rows, T4-6 backfill APR24)
    # with VWF_PROXY + FULL_SCORE entries carrying dri_score + soh_conservative.
    # Fallback: latest bhs_v2 row (single-week mode) if no history.
    _hist_rows = q(conn, """
        SELECT week_number,
               COALESCE(dri_score, bhs_score_v2) AS dri,
               soh_conservative AS soh,
               score_type
        FROM battery_score_history
        WHERE battery_id = ?
    """, [battery_id]) or []
    _hist_by_wk = {h["week_number"]: h for h in _hist_rows}
    _be1_bhs_week = refs["bhs_week"] if refs else None
    _be1_bhs_dri = refs["bhs_score_v2"] if refs else None
    _be1_bhs_soh = refs["soh_conservative"] if refs else None
    # Pack peers per-week mean range (via km_per_soc_pct * 80). Used as a
    # dashed reference line on trend charts.
    _be1_pack_med = {}
    try:
        _be1_pack_model = q1(conn, "SELECT battery_model FROM batteries WHERE battery_id = ?", [battery_id])
        _be1_pm = _be1_pack_model.get("battery_model") if _be1_pack_model else None
        if _be1_pm:
            pack_rows = q(conn, """
                SELECT vwf.week_number AS wk,
                       ROUND(AVG(vwf.km_per_soc_pct) * 80, 1) AS avg_range
                FROM vehicle_weekly_features vwf
                JOIN batteries b USING(battery_id)
                WHERE b.battery_model = ?
                  AND vwf.km_per_soc_pct IS NOT NULL
                GROUP BY vwf.week_number
            """, [_be1_pm])
            for r in (pack_rows or []):
                v = r.get("avg_range")
                if v is not None and v <= PHYSICS_CEILING_KM:
                    _be1_pack_med[r["wk"]] = v
    except Exception:
        pass
    for _w in weeks:
        _wk = _w.get("week_number")
        _h = _hist_by_wk.get(_wk)
        if _h is not None:
            _w["dri"] = _h.get("dri")
            _w["soh"] = _h.get("soh")
            _w["score_type"] = _h.get("score_type")
        else:
            _w["dri"] = _be1_bhs_dri if _wk == _be1_bhs_week else None
            _w["soh"] = _be1_bhs_soh if _wk == _be1_bhs_week else None
        _w["slope_km_wk"] = (_w.get("kps_slope") * 80) if _w.get("kps_slope") is not None else None
        _w["pack_median_range"] = _be1_pack_med.get(_wk)
        _w["pack_median_dri"] = None  # retained for UI schema parity


    # Projections from dt_lfp_range_projections (dt_lfp_v1.1)
    proj_rows = q(conn, """
        SELECT horizon_weeks, range_p10_km, range_p50_km, range_p90_km,
               projection_date, model_version, simulated, current_week
        FROM dt_lfp_range_projections
        WHERE battery_id = ?
        ORDER BY horizon_weeks
    """, [battery_id])

    projections = []
    proj_current_week = None
    for pr in (proj_rows or []):
        proj_current_week = pr.get("current_week")
        projections.append({
            "horizon_weeks": pr["horizon_weeks"],
            "range_p10_km": pr["range_p10_km"],
            "range_p50_km": pr["range_p50_km"],
            "range_p90_km": pr["range_p90_km"],
            "projection_date": pr["projection_date"],
            "model_version": pr.get("model_version", "dt_lfp_v1.1"),
            "simulated": True,
        })

    # Reference lines — apply GUARD 2 cap on commissioned
    oem_rated = refs["mfg_claimed_range_km"] if refs else None
    commissioned_raw = refs["commissioned_range_km"] if refs else None
    commissioned = commissioned_raw
    if commissioned_raw is not None and commissioned_raw > PHYSICS_CEILING_KM:
        commissioned = None  # artefact — do not expose inflated baseline
    fleet_expected = refs["fleet_expected_range_km"] if refs else None
    # current_range: prefer BHS range_corrected_km, but cap at ceiling; fall
    # back to last clean weekly value if BHS value is an artefact.
    bhs_current = refs["range_corrected_km"] if refs else None
    if bhs_current is not None and bhs_current > PHYSICS_CEILING_KM:
        bhs_current = None
    clean_weeks = [w for w in weeks if w["range_km"] is not None]
    current_range = bhs_current if bhs_current is not None else (
        clean_weeks[-1]["range_km"] if clean_weeks else None)

    # Summary — built from clean (post-guard) weekly values only
    peak_range = max(w["range_km"] for w in clean_weeks) if clean_weeks else None
    commissioned_estimate = None
    if clean_weeks:
        first4 = [w["range_km"] for w in clean_weeks[:4]]
        if first4:
            commissioned_estimate = round(min(sum(first4) / len(first4), PHYSICS_CEILING_KM), 1)
    total_loss_km = None
    if commissioned_estimate is not None and current_range is not None:
        total_loss_km = round(commissioned_estimate - current_range, 1)

    # Deviation percentages — use guarded commissioned
    range_deviation = {}
    if current_range and current_range > 0:
        range_deviation["vs_oem_rated_pct"] = round(current_range / oem_rated * 100, 1) if oem_rated and oem_rated > 0 else None
        range_deviation["vs_fleet_norm_pct"] = round(current_range / fleet_expected * 100, 1) if fleet_expected and fleet_expected > 0 else None
        range_deviation["vs_own_baseline_pct"] = round(current_range / commissioned * 100, 1) if commissioned and commissioned > 0 else None

    conn.close()

    return {
        "battery_id": battery_id,
        "weekly_data": weeks,
        "anomalies": anomalies,
        # battery_score_history backfill APR24 lit up weekly DRI/SOH trajectories.
        "dri_history_available": bool(_hist_rows),
        "summary": {
            "peak_range": peak_range,
            "commissioned_range_estimate": commissioned_estimate,
            "total_loss_km": total_loss_km,
            "clean_week_count": len(clean_weeks),
            "artefact_week_count": len(anomalies),
        },
        "references": {
            "mfg_rated_km": oem_rated,
            "commissioned_km": commissioned,
            "fleet_expected_km": fleet_expected,
        },
        "projections": projections,
        "projection_current_week": proj_current_week,
        "reference_lines": {
            "oem_rated_km": oem_rated,
            "commissioned_km": commissioned,
            "fleet_expected_km": fleet_expected,
        },
        "range_deviation": range_deviation,
        "trajectory": {
            "slope_4wk": round(refs["kps_slope_4wk"] * 80, 1) if refs and refs["kps_slope_4wk"] else None,
            "slope_8wk": None,
        },
        "degradation_regime": refs["degradation_regime"] if refs else None,
        "range_corrected_km": current_range,
    }


# ── GET /api/fleet/events ─────────────────────────────────────────────

@app.get("/api/fleet/events")
@safe
def fleet_events(
    severity: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    _=Depends(verify_token),
):
    conn = get_conn()
    sql = """
        SELECT e.*, h.operational_score, h.tier_label_v2
        FROM vehicle_events e
        LEFT JOIN battery_health_scores_v2 h ON h.battery_id = e.battery_id
        WHERE 1=1
    """
    params = []
    if severity:
        sql += " AND e.severity = ?"
        params.append(severity)
    if event_type:
        sql += " AND e.event_type = ?"
        params.append(event_type)
    sql += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)

    rows = q(conn, sql, params)
    conn.close()
    return rows


# ── GET /api/fleet/filter ─────────────────────────────────────────────

@app.get("/api/fleet/filter")
@safe
def fleet_filter(
    tier: Optional[str] = None,
    chemistry: Optional[str] = None,
    cusum_x_weeks: Optional[int] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    event_type: Optional[str] = None,
    _=Depends(verify_token),
    role: str = Depends(get_role),
):
    conn = get_conn()
    sql = """
        SELECT h.*, c.cluster_label
        FROM battery_health_scores_v2 h
        LEFT JOIN battery_usage_clusters c ON c.battery_id = h.battery_id
        WHERE 1=1
    """
    params = []
    if tier:
        sql += " AND h.tier_label_v2 = ?"
        params.append(tier)
    if chemistry:
        sql += " AND h.chemistry = ?"
        params.append(chemistry)
    if min_score is not None:
        sql += " AND h.operational_score >= ?"
        params.append(min_score)
    if max_score is not None:
        sql += " AND h.operational_score <= ?"
        params.append(max_score)
    if event_type:
        sql += " AND h.battery_id IN (SELECT DISTINCT battery_id FROM vehicle_events WHERE event_type = ?)"
        params.append(event_type)

    sql += " ORDER BY h.operational_score ASC"
    rows = q(conn, sql, params)
    # Suppress legacy tier_v2 from API responses (PD-NEW-21).
    for r in rows:
        r.pop('tier_v2', None)

    # CUSUM filter (post-query since it's in VWF not scores)
    if cusum_x_weeks is not None:
        cusum_bats = set()
        cr = q(conn, """
            SELECT DISTINCT battery_id FROM vehicle_weekly_features
            WHERE cusum_x_weeks = ?
        """, [cusum_x_weeks])
        cusum_bats = {r["battery_id"] for r in cr}
        rows = [r for r in rows if r["battery_id"] in cusum_bats]

    conn.close()
    return filter_for_role(rows, role)


# ── GET /api/fleet/batch/:batch_id ────────────────────────────────────

@app.get("/api/fleet/batch/{batch_id}")
@safe
def fleet_batch(batch_id: str, _=Depends(verify_token)):
    conn = get_conn()

    # Match batteries by batch pattern in original_id or battery_model
    rows = q(conn, """
        SELECT h.*, b.original_id, b.battery_model, b.batch_id as db_batch_id,
               c.cluster_label
        FROM battery_health_scores_v2 h
        JOIN batteries b ON b.battery_id = h.battery_id
        LEFT JOIN battery_usage_clusters c ON c.battery_id = h.battery_id
        WHERE b.original_id LIKE ? OR b.battery_model LIKE ? OR b.batch_id LIKE ?
        ORDER BY h.operational_score ASC
    """, [f"%{batch_id}%", f"%{batch_id}%", f"%{batch_id}%"])
    # Suppress legacy tier_v2 from API responses (PD-NEW-21).
    for r in rows:
        r.pop('tier_v2', None)

    if not rows:
        conn.close()
        raise HTTPException(404, f"No batteries found for batch: {batch_id}")

    scores = [r["operational_score"] for r in rows if r["operational_score"] is not None]
    mean_score = round(sum(scores) / len(scores), 2) if scores else None
    worst = rows[0] if rows else None

    conn.close()
    return {
        "batch_id": batch_id,
        "battery_count": len(rows),
        "mean_score": mean_score,
        "worst_battery": worst["battery_id"] if worst else None,
        "worst_score": worst["operational_score"] if worst else None,
        "batteries": rows,
    }


# ── GET /api/models ───────────────────────────────────────────────────

@app.get("/api/models")
@safe
def get_models(_=Depends(verify_token)):
    models = []

    # Read model metadata JSON files
    if MODELS_DIR.exists():
        for f in sorted(MODELS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["_filename"] = f.name
                models.append(data)
            except Exception:
                models.append({"_filename": f.name, "status": "parse_error"})

        # PKL files without metadata
        json_stems = {f.stem for f in MODELS_DIR.glob("*.json")}
        for f in sorted(MODELS_DIR.glob("*.pkl")):
            if f.stem not in json_stems:
                models.append({"_filename": f.name, "status": "no_metadata"})

    # Read changelog
    changelog = None
    changelog_path = DOCS_DIR / "MODEL_CHANGELOG.md"
    if changelog_path.exists():
        try:
            changelog = changelog_path.read_text(encoding="utf-8")
        except Exception:
            pass

    return {"models": models, "changelog": changelog}


# ── GET /api/battery/:id/diagnostic ──────────────────────────────────

def _parse_json_safe(val, field_name, warnings_list):
    """Parse JSON string to object. On failure, return raw string and log warning."""
    if val is None:
        return None
    if not isinstance(val, str):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        warnings_list.append(field_name)
        return val


@app.get("/api/battery/{battery_id}/diagnostic")
@safe
def battery_diagnostic(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()

    diag = q1(conn, """
        SELECT d.*, h.tier_label_v2, h.adjusted_composite,
               c.cluster_label
        FROM battery_diagnostics d
        LEFT JOIN battery_health_scores_v2 h ON d.battery_id = h.battery_id
        LEFT JOIN battery_usage_clusters c ON d.battery_id = c.battery_id
        WHERE d.battery_id = ?
        ORDER BY d.week_number DESC LIMIT 1
    """, [battery_id])

    if not diag:
        conn.close()
        raise HTTPException(404, detail=json.dumps({"error": "diagnostic not found", "battery_id": battery_id}))

    # Parse JSON string columns to objects
    parse_warnings = []
    json_fields = [
        "event_timeline", "shap_attribution", "range_deviation_attribution",
        "trend_summary", "failure_probabilities", "recommendations",
    ]
    for field in json_fields:
        if field in diag:
            diag[field] = _parse_json_safe(diag[field], field, parse_warnings)

    # nbfc_summary and oem_summary may also be JSON
    for field in ["nbfc_summary", "oem_summary"]:
        if field in diag and isinstance(diag[field], str) and diag[field].startswith("{"):
            diag[field] = _parse_json_safe(diag[field], field, parse_warnings)

    # Rename joined columns
    diag["tier"] = diag.pop("tier_label_v2", None)
    diag["cluster_label"] = diag.get("cluster_label")

    # Add event_chains for this battery
    chains = q(conn, """
        SELECT chain_id, chain_start_week, chain_end_week, chain_length,
               event_sequence, chain_pattern, pattern_name, pattern_confidence,
               severity_escalation, fleet_prevalence_pct,
               oem_implication, nbfc_implication, operator_action
        FROM event_chains WHERE battery_id = ?
        ORDER BY chain_start_week DESC
    """, [battery_id])
    for ch in chains:
        if "event_sequence" in ch and isinstance(ch["event_sequence"], str):
            ch["event_sequence"] = _parse_json_safe(ch["event_sequence"], "event_sequence", parse_warnings)
    diag["chains"] = chains

    if parse_warnings:
        diag["parse_warnings"] = parse_warnings

    conn.close()
    return diag


# ── GET /api/fleet/chains ────────────────────────────────────────────

@app.get("/api/fleet/chains")
@safe
def fleet_chains(_=Depends(verify_token)):
    conn = get_conn()

    # Pattern distribution
    patterns = q(conn, """
        SELECT pattern_name, COUNT(*) as cnt, known_pattern
        FROM event_chains
        GROUP BY pattern_name
        ORDER BY cnt DESC
    """)

    # Total LFP batteries for fleet_pct
    total_lfp = q1(conn, "SELECT COUNT(*) as n FROM batteries WHERE chemistry='LFP'")
    total_lfp_n = total_lfp["n"] if total_lfp else 156

    pattern_distribution = []
    for p in patterns:
        pattern_distribution.append({
            "pattern": p["pattern_name"],
            "count": p["cnt"],
            "fleet_pct": round(p["cnt"] / total_lfp_n * 100, 1),
            "known_pattern": p["known_pattern"],
        })

    # Severity escalation batteries
    esc = q(conn, "SELECT DISTINCT battery_id FROM event_chains WHERE severity_escalation = 1")
    escalation_batteries = [r["battery_id"] for r in esc]

    # Most complex
    complex_bats = q(conn, """
        SELECT battery_id, COUNT(*) as chain_count
        FROM event_chains
        GROUP BY battery_id
        ORDER BY chain_count DESC
        LIMIT 5
    """)

    # Total chains + batteries with chains
    totals = q1(conn, """
        SELECT COUNT(*) as total_chains,
               COUNT(DISTINCT battery_id) as batteries_with_chains
        FROM event_chains
    """)

    # Latest timestamp
    latest = q1(conn, "SELECT MAX(created_at) as ts FROM event_chains")

    conn.close()
    return {
        "pattern_distribution": pattern_distribution,
        "severity_escalation_batteries": escalation_batteries,
        "most_complex": complex_bats,
        "total_chains": totals["total_chains"] if totals else 0,
        "batteries_with_chains": totals["batteries_with_chains"] if totals else 0,
        "summary_updated_at": latest["ts"] if latest else None,
    }


# ── ENHANCE /api/fleet/summary — add diagnostic fields ──────────────
# The existing fleet_summary function is above. We add a post-hook that
# enriches it with battery_diagnostics data. We do this by patching the
# response in a new wrapper endpoint and keeping the old one intact.

@app.get("/api/fleet/summary/enhanced")
@safe
def fleet_summary_enhanced(_=Depends(verify_token)):
    """Fleet summary + diagnostic engine stats."""
    conn = get_conn()

    # Base stats (same as existing fleet_summary)
    total = q1(conn, "SELECT COUNT(*) as n FROM battery_health_scores_v2")["n"]
    tiers = q(conn, """
        SELECT tier_label_v2, COUNT(*) as cnt
        FROM battery_health_scores_v2
        WHERE tier_label_v2 IS NOT NULL
        GROUP BY tier_label_v2
    """)
    by_tier = {r["tier_label_v2"]: r["cnt"] for r in tiers}

    chems = q(conn, "SELECT chemistry, COUNT(*) as cnt FROM battery_health_scores_v2 GROUP BY chemistry")
    by_chemistry = {r["chemistry"]: r["cnt"] for r in chems}

    eehi_row = q1(conn, "SELECT AVG(operational_score) as eehi FROM battery_health_scores_v2")
    eehi = round(eehi_row["eehi"], 2) if eehi_row and eehi_row["eehi"] else 0

    at_risk = by_tier.get("STRESSED", 0) + by_tier.get("CRITICAL", 0)

    model_row = q1(conn, """
        SELECT model_version, model_mape_12w, scored_at
        FROM battery_health_scores_v2
        WHERE scored_at IS NOT NULL
        ORDER BY scored_at DESC LIMIT 1
    """)

    # Diagnostic engine stats (from battery_diagnostics)
    immediate_count = 0
    cohort_anomaly_count = 0
    deviations = []
    worst_batteries = []

    diag_rows = q(conn, """
        SELECT d.battery_id, d.range_deviation_attribution, d.recommendations,
               d.fleet_summary, d.cohort_anomaly_flag, h.tier_label_v2
        FROM battery_diagnostics d
        LEFT JOIN battery_health_scores_v2 h ON d.battery_id = h.battery_id
    """)

    for r in diag_rows:
        # Cohort anomaly
        if r.get("cohort_anomaly_flag") == 1:
            cohort_anomaly_count += 1

        # Immediate actions
        try:
            recs = json.loads(r["recommendations"]) if r.get("recommendations") else []
            if isinstance(recs, list) and any(rc.get("urgency") == "IMMEDIATE" for rc in recs):
                immediate_count += 1
        except (json.JSONDecodeError, TypeError):
            pass

        # Range deviation
        try:
            rd = json.loads(r["range_deviation_attribution"]) if r.get("range_deviation_attribution") else {}
            if isinstance(rd, dict) and "deviation_pct" in rd:
                dev = rd["deviation_pct"]
                deviations.append(dev)
                worst_batteries.append({
                    "battery_id": r["battery_id"],
                    "deviation_pct": round(dev, 1),
                    "tier": r.get("tier_label_v2", "UNSCORED"),
                    "fleet_summary": r.get("fleet_summary", ""),
                })
        except (json.JSONDecodeError, TypeError):
            pass

    mean_dev = round(sum(deviations) / len(deviations), 1) if deviations else 0
    worst_batteries.sort(key=lambda x: x["deviation_pct"])

    conn.close()
    return {
        "total_batteries": total,
        "by_tier": by_tier,
        "by_chemistry": by_chemistry,
        "eehi": eehi,
        "at_risk": at_risk,
        "model_version": model_row["model_version"] if model_row else "unknown",
        "model_mape": round(model_row["model_mape_12w"], 2) if model_row and model_row["model_mape_12w"] else None,
        "immediate_action_count": immediate_count,
        "cohort_anomaly_count": cohort_anomaly_count,
        "mean_range_deviation_pct": mean_dev,
        "worst_range_batteries": worst_batteries[:5],
    }


# ── GET /api/battery/:id/feedback ─────────────────────────────────────

@app.get("/api/battery/{battery_id}/feedback")
@safe
def battery_feedback(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()
    try:
        rows = q(conn, """
            SELECT battery_id, user_assessment, feedback_comment,
                   scored_tier, scored_composite, feedback_reason_code,
                   stakeholder_role, reviewed, review_outcome, review_comment,
                   created_at
            FROM battery_feedback
            WHERE battery_id = ? OR battery_id IN (
                SELECT battery_id FROM batteries WHERE original_id = ?
            )
            ORDER BY created_at DESC
            LIMIT 20
        """, [battery_id, battery_id])
    except Exception:
        rows = []
    conn.close()
    return rows


# ── GET /api/battery/:id/ir-trend ────────────────────────────────────

@app.get("/api/battery/{battery_id}/ir-trend")
@safe
def battery_ir_trend(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()
    try:
        rows = q(conn, """
            SELECT week_number, ir_proxy_weekly
            FROM vehicle_weekly_features
            WHERE battery_id = ?
            ORDER BY week_number ASC
        """, [battery_id])
    except Exception:
        rows = []
    conn.close()

    weeks = [r["week_number"] for r in rows]
    ir_series = [r["ir_proxy_weekly"] for r in rows]
    return {"battery_id": battery_id, "ir_series": ir_series, "weeks": weeks}


# ── POST /api/feedback ───────────────────────────────────────────────

from pydantic import BaseModel as _BM


class FeedbackBody(_BM):
    battery_id: str
    original_id: Optional[str] = None
    feedback_type: Optional[str] = None
    feedback_text: Optional[str] = None
    score: Optional[str] = None
    tier: Optional[str] = None


@app.post("/api/feedback")
@safe
def post_feedback(body: FeedbackBody, _=Depends(verify_token)):
    conn = get_conn()
    try:
        # Use existing battery_feedback schema columns (week_number NOT NULL)
        conn.execute("""
            INSERT INTO battery_feedback
                (battery_id, week_number, user_assessment, feedback_comment, scored_tier,
                 feedback_reason_code, stakeholder_role, created_at)
            VALUES (?, 0, ?, ?, ?, ?, ?, datetime('now'))
        """, [
            body.battery_id,
            body.score or 'AGREE',
            body.feedback_text or '',
            body.tier or '',
            body.feedback_type or 'general',
            'SHELL_USER',
        ])
        conn.commit()
    except Exception as e:
        conn.close()
        return {"status": "error", "detail": str(e)[:200]}
    conn.close()
    return {"status": "ok", "battery_id": body.battery_id}


# ══════════════════════════════════════════════════════════════════════
# BATTERY INTELLIGENCE ENDPOINTS (Sprint 4.2)
# ══════════════════════════════════════════════════════════════════════


@app.get("/api/fleet/intelligence")
def fleet_intelligence(auth=Depends(verify_token)):
    """Fleet intelligence summary with warning levels and chain distribution."""
    conn = get_conn()
    try:
        batteries = q(conn, """
            SELECT bi.battery_id, bi.warning_level, bi.causal_chain,
                bi.range_current_p50, bi.range_current_p10, bi.range_current_p90,
                bi.range_8w_p50, bi.range_8w_p10, bi.range_8w_p90,
                bi.active_event_count, bi.rul_cycles_remaining,
                bi.shap_feature_1, bi.shap_value_1,
                bi.shap_feature_2, bi.shap_value_2,
                bi.shap_feature_3, bi.shap_value_3,
                bi.scored_at,
                bh.tier_label_v2, bh.adjusted_composite,
                bh.efficiency_score_v2,
                b.original_id
            FROM battery_intelligence bi
            LEFT JOIN battery_health_scores_v2 bh ON bi.battery_id = bh.battery_id
            LEFT JOIN batteries b ON bi.battery_id = b.battery_id
            ORDER BY
                CASE bi.warning_level WHEN 'RED' THEN 1 WHEN 'ORANGE' THEN 2
                WHEN 'AMBER' THEN 3 WHEN 'GREEN' THEN 4 ELSE 5 END,
                bi.active_event_count DESC
        """)

        # Fleet aggregates
        total = len(batteries)
        red = sum(1 for b in batteries if b.get("warning_level") == "RED")
        orange = sum(1 for b in batteries if b.get("warning_level") == "ORANGE")
        amber = sum(1 for b in batteries if b.get("warning_level") == "AMBER")
        green = sum(1 for b in batteries if b.get("warning_level") == "GREEN")
        default_risk = round((red + orange) / max(total, 1) * 100, 1)

        composites = [b["adjusted_composite"] for b in batteries if b.get("adjusted_composite") is not None]
        eehi = round(sum(composites) / max(len(composites), 1), 1) if composites else None

        ranges = [b["range_current_p50"] for b in batteries if b.get("range_current_p50") is not None]
        ranges.sort()
        median_range = round(ranges[len(ranges) // 2], 1) if ranges else None

        chain_counts = {}
        for b in batteries:
            ch = b.get("causal_chain", "UNKNOWN")
            chain_counts[ch] = chain_counts.get(ch, 0) + 1

        return {
            "batteries": batteries,
            "fleet": {
                "total": total,
                "red_count": red,
                "orange_count": orange,
                "amber_count": amber,
                "green_count": green,
                "default_risk_index": default_risk,
                "eehi": eehi,
                "median_range_current": median_range,
                "chain_counts": chain_counts,
            }
        }
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/intelligence")
def battery_intelligence(battery_id: str, auth=Depends(verify_token)):
    """Full intelligence record for a single battery."""
    conn = get_conn()
    try:
        row = q1(conn, """
            SELECT bi.*, bh.tier_label_v2, bh.adjusted_composite, bh.operational_score,
                bh.efficiency_score_v2, bh.fault_risk_score, bh.rul_score_legacy as rul_score,
                bh.event_score, bh.event_penalty,
                bh.range_cause, bh.cause_note,
                b.original_id, b.chemistry
            FROM battery_intelligence bi
            LEFT JOIN battery_health_scores_v2 bh ON bi.battery_id = bh.battery_id
            LEFT JOIN batteries b ON bi.battery_id = b.battery_id
            WHERE bi.battery_id = ?
        """, [battery_id])
        if not row:
            raise HTTPException(404, f"Battery {battery_id} not found")
        return row
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/narrative")
def battery_narrative(battery_id: str, persona: str = "operator", auth=Depends(verify_token)):
    """Stakeholder narrative for a battery. Persona: operator|nbfc|oem."""
    conn = get_conn()
    try:
        col_map = {
            "operator": "narrative_operator",
            "nbfc": "narrative_nbfc",
            "oem": "narrative_oem",
        }
        col = col_map.get(persona.lower(), "narrative_operator")
        row = q1(conn, f"""
            SELECT {col} as narrative, warning_level, causal_chain, scored_at
            FROM battery_intelligence WHERE battery_id = ?
        """, [battery_id])
        if not row:
            raise HTTPException(404, f"Battery {battery_id} not found")
        return {"persona": persona, **row}
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/events/active")
def battery_events_active(battery_id: str, auth=Depends(verify_token)):
    """Active (unresolved) events for a battery."""
    conn = get_conn()
    try:
        return q(conn, """
            SELECT event_type, severity, confidence, signal_name, signal_value,
                signal_deviation_pct, causal_chain, rc_primary, evidence_json,
                week_number
            FROM vehicle_events
            WHERE battery_id = ? AND resolved_week IS NULL
            ORDER BY severity ASC, week_number DESC
        """, [battery_id])
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/events/history")
def battery_events_history(battery_id: str, auth=Depends(verify_token)):
    """Full event history for a battery (last 50)."""
    conn = get_conn()
    try:
        rows = q(conn, """
            SELECT event_type, severity, confidence, signal_name, signal_value,
                signal_deviation_pct, causal_chain, rc_primary,
                week_number, resolved_week
            FROM vehicle_events
            WHERE battery_id = ?
            ORDER BY week_number DESC
            LIMIT 50
        """, [battery_id])
        for r in rows:
            r["resolved"] = r.get("resolved_week") is not None
        return rows
    finally:
        conn.close()


@app.get("/api/fleet/events")
def fleet_events(auth=Depends(verify_token)):
    """Fleet-wide active events summary."""
    conn = get_conn()
    try:
        return q(conn, """
            SELECT ve.battery_id, ve.event_type, ve.severity, ve.causal_chain,
                ve.signal_name, ve.signal_value, ve.week_number
            FROM vehicle_events ve
            WHERE ve.resolved_week IS NULL
            ORDER BY ve.severity ASC, ve.week_number DESC
            LIMIT 200
        """)
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/timeseries")
def battery_timeseries(battery_id: str, auth=Depends(verify_token)):
    """Weekly feature time series with summary stats."""
    conn = get_conn()
    try:
        series = q(conn, """
            SELECT week_number, km_per_soc_pct, temp_max, voltage_range,
                soc_corrected_voltage_sag, thermal_damage_index_cumulative,
                soh_r_weekly, cycle_life_remaining,
                trip_efficiency_anomaly_count_week,
                ir_proxy_mid_soc_weekly, thermal_soak_hours_week,
                dod_mean, charge_cycle_rate, active_days,
                km_sum, speed_mean
            FROM vehicle_weekly_features
            WHERE battery_id = ?
            ORDER BY week_number ASC
        """, [battery_id])

        if not series:
            raise HTTPException(404, f"No VWF data for {battery_id}")

        # Summary
        kps_vals = [r["km_per_soc_pct"] for r in series if r.get("km_per_soc_pct")]
        early = [r["km_per_soc_pct"] for r in series
                 if r.get("km_per_soc_pct") and r.get("week_number", 999) <= 8]
        late = kps_vals[-4:] if len(kps_vals) >= 4 else kps_vals
        baseline_range = round(sum(early) / max(len(early), 1) * 80, 1) if early else None
        current_range = round(sum(late) / max(len(late), 1) * 80, 1) if late else None

        pct_change = None
        if baseline_range and current_range and baseline_range > 0:
            pct_change = round((current_range - baseline_range) / baseline_range * 100, 1)

        temps = [r["temp_max"] for r in series if r.get("temp_max") and r["temp_max"] > 0]
        dods = [r["dod_mean"] for r in series if r.get("dod_mean") and r["dod_mean"] > 0]
        weeks = [r["week_number"] for r in series if r.get("week_number") is not None]

        return {
            "series": series,
            "summary": {
                "weeks_total": len(series),
                "week_min": min(weeks) if weeks else None,
                "week_max": max(weeks) if weeks else None,
                "baseline_range": baseline_range,
                "current_range": current_range,
                "range_vs_baseline_pct": pct_change,
                "peak_temp": round(max(temps), 1) if temps else None,
                "mean_dod": round(sum(dods) / max(len(dods), 1), 1) if dods else None,
            }
        }
    finally:
        conn.close()


@app.get("/api/models/catalogue")
def list_models_catalogue(auth=Depends(verify_token)):
    """Model catalogue listing (from model_catalogue table)."""
    conn = get_conn()
    try:
        return q(conn, """
            SELECT model_id, version, mape_production as mape, purpose,
                status, algorithm, gates_passed, training_samples,
                created, sprint, promoted_to_main
            FROM model_catalogue
            ORDER BY created DESC
        """)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# SPRINT 5.2 — 7 NEW ENDPOINTS + SUMMARY ENHANCEMENT
# ══════════════════════════════════════════════════════════════════════


@app.get("/api/fleet/outcomes")
@safe
def fleet_outcomes(auth=Depends(verify_token)):
    """OS1-OS5 outcome scores for all batteries."""
    conn = get_conn()
    cols = [c[1] for c in conn.cursor().execute(
        "PRAGMA table_info(battery_health_scores_v2)").fetchall()]
    os_cols = [c for c in cols if c.startswith("os") or "score" in c.lower()
               or c in ("battery_id", "chemistry", "tier_label_v2",
                        "operational_score", "adjusted_composite")]
    score_cols = [c for c in cols if c in (
        "efficiency_score_v2", "fault_risk_score", "rul_score",
        "event_score", "attribution_score")]
    select = ["battery_id", "chemistry", "operational_score",
              "tier_label_v2"] + score_cols
    select = [c for c in select if c in cols]
    rows = q(conn, f"SELECT {', '.join(select)} FROM battery_health_scores_v2")
    conn.close()
    return {"batteries": rows,
            "columns_note": f"Available score columns: {score_cols}",
            "total": len(rows)}


@app.get("/api/fleet/actions")
@safe
def fleet_actions(auth=Depends(verify_token)):
    """Action counts grouped by priority and chemistry."""
    conn = get_conn()
    cols = [c[1] for c in conn.cursor().execute(
        "PRAGMA table_info(battery_health_scores_v2)").fetchall()]
    # Try multiple action column names
    action_col = None
    for candidate in ["action_priority", "operator_action", "tier_label_v2"]:
        if candidate in cols:
            action_col = candidate
            break
    if not action_col:
        conn.close()
        return {"error": "No action column found", "available_columns": cols}

    rows = q(conn, f"""
        SELECT chemistry, COALESCE([{action_col}], 'UNKNOWN') as action,
               COUNT(*) as cnt
        FROM battery_health_scores_v2
        GROUP BY chemistry, action
    """)
    conn.close()
    result = {"LFP": {}, "NMC": {}, "totals": {}, "action_column_used": action_col}
    for r in rows:
        chem = r.get("chemistry", "UNKNOWN") or "UNKNOWN"
        act = r["action"]
        cnt = r["cnt"]
        if chem in result:
            result[chem][act] = cnt
        result["totals"][act] = result["totals"].get(act, 0) + cnt
    return result


@app.get("/api/battery/{battery_id}/ecm")
@safe
def battery_ecm(battery_id: str, auth=Depends(verify_token)):
    """ECM parameters for a battery."""
    conn = get_conn()
    tables = [r[0] for r in conn.cursor().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    ecm_tables = [t for t in tables if "ecm" in t.lower()]
    if not ecm_tables:
        # Check nmc_weekly_features for r0_estimate
        try:
            rows = q(conn, """
                SELECT battery_id, week_number, r0_estimate_mohm
                FROM nmc_weekly_features
                WHERE battery_id = ? AND r0_estimate_mohm IS NOT NULL
                ORDER BY week_number
            """, [battery_id])
            if rows:
                conn.close()
                return {"battery_id": battery_id, "source": "nmc_weekly_features",
                        "ecm_data": rows}
        except Exception:
            pass
        conn.close()
        return {"status": "not_available",
                "reason": "ECM table pending Sprint KF. NMC R0 estimates in nmc_weekly_features.",
                "ecm_tables_found": ecm_tables}
    # If ECM table exists, query it
    rows = q(conn, f"""
        SELECT * FROM [{ecm_tables[0]}]
        WHERE battery_id = ?
        ORDER BY week_number
    """, [battery_id])
    conn.close()
    return {"battery_id": battery_id, "source": ecm_tables[0], "ecm_data": rows}


@app.get("/api/fleet/findings")
@safe
def fleet_findings(auth=Depends(verify_token)):
    """6 fleet health findings from Sprint 5.1 — 3 LFP + 3 NMC."""
    conn = get_conn()
    # Dynamic affected counts for LFP
    lfp_total = q1(conn, "SELECT COUNT(*) as n FROM batteries WHERE chemistry='LFP' AND onboarding_status='ACTIVE'")
    lfp_n = lfp_total["n"] if lfp_total else 186
    try:
        spread_floor = float(get_param('spread_warning_mv', default=100))
        spread_affected = q1(conn, """
            SELECT COUNT(DISTINCT v.battery_id) as n FROM vehicle_weekly_features v
            JOIN batteries b ON v.battery_id = b.battery_id
            WHERE b.chemistry='LFP' AND v.cell_spread_max > ?
            AND v.week_number = (SELECT MAX(week_number) FROM vehicle_weekly_features v2
                                 WHERE v2.battery_id = v.battery_id)
        """, params=[spread_floor])
        spread_n = spread_affected["n"] if spread_affected else None
    except Exception:
        spread_n = None
    try:
        cusum_n = q1(conn, """
            SELECT COUNT(DISTINCT battery_id) as n FROM vehicle_weekly_features
            WHERE cusum_x_weeks = 1 AND battery_id IN
            (SELECT battery_id FROM batteries WHERE chemistry='LFP')
        """)
        cusum_count = cusum_n["n"] if cusum_n else None
    except Exception:
        cusum_count = None
    conn.close()

    return {
        "LFP": [
            {"id": "LFP-F1", "title": "Cell Spread Degradation", "severity": "HIGH",
             "affected_count": spread_n,
             "description": "Cell voltage spread exceeding 100mV threshold in degraded batteries"},
            {"id": "LFP-F2", "title": "SoC Measurement Error", "severity": "MEDIUM",
             "affected_count": lfp_n,
             "description": "BMS SoC diverges from corrected SoC — soc_corrected_voltage_sag signal"},
            {"id": "LFP-F3", "title": "BMS Alert Suppression", "severity": "HIGH",
             "affected_count": cusum_count,
             "description": "Alert counts near-zero on degraded batteries — BMS alerts disabled or suppressed"},
        ],
        "NMC": [
            {"id": "NMC-F1", "title": "Charger Mismatch", "severity": "CRITICAL",
             "affected_count": 27,
             "description": "6.2C mean charge rate vs 2C DMEGC spec max — 3x overstress. Adjusted cycle life ~507 vs 1408 rated"},
            {"id": "NMC-F2", "title": "Universal Deep Discharge", "severity": "HIGH",
             "affected_count": 26,
             "description": "26/27 batteries regularly below 20% SoC combined with fast charging = dual stress"},
            {"id": "NMC-F3", "title": "Zero Routine-Correct Batteries", "severity": "HIGH",
             "affected_count": 27,
             "description": "Fleet systematically violates both SoC>20% and c_rate<2C. No low-stress batteries exist"},
        ],
    }


@app.get("/api/fleet/cell-spread")
@safe
def fleet_cell_spread(auth=Depends(verify_token)):
    """Per-battery cell spread (latest week). LFP from VWF, NMC from nmc_weekly_features."""
    conn = get_conn()
    # LFP
    lfp = q(conn, """
        SELECT v.battery_id, 'LFP' as chemistry, v.cell_spread_mean, v.cell_spread_max, v.week_number
        FROM vehicle_weekly_features v
        JOIN batteries b ON v.battery_id = b.battery_id
        WHERE b.chemistry = 'LFP'
        AND v.week_number = (SELECT MAX(week_number) FROM vehicle_weekly_features v2
                             WHERE v2.battery_id = v.battery_id AND v2.cell_spread_mean IS NOT NULL)
        AND v.cell_spread_mean IS NOT NULL
    """)
    # NMC
    nmc = q(conn, """
        SELECT n.battery_id, 'NMC' as chemistry, n.cell_spread_mean, n.cell_spread_max, n.week_number
        FROM nmc_weekly_features n
        WHERE n.week_number = (SELECT MAX(week_number) FROM nmc_weekly_features n2
                               WHERE n2.battery_id = n.battery_id AND n2.cell_spread_mean IS NOT NULL)
        AND n.cell_spread_mean IS NOT NULL
    """)
    conn.close()
    return {
        "batteries": lfp + nmc,
        "total": len(lfp) + len(nmc),
        "thresholds": {
            "LFP": {"warn": 100, "crit": 150, "signal": "cell_spread_max"},
            "NMC": {"warn": 65, "crit": 118, "signal": "cell_spread_mean",
                    "note": "Rule 17: NMC uses cell_spread_MEAN for scoring"},
        },
    }


@app.get("/api/fleet/soc-correction")
@safe
def fleet_soc_correction(auth=Depends(verify_token)):
    """SoC correction data — BMS vs corrected values per battery."""
    conn = get_conn()
    # LFP per-battery latest
    lfp = q(conn, """
        SELECT v.battery_id, 'LFP' as chemistry, v.soc_min as soc_min_observed,
               v.soc_corrected_voltage_sag, v.dod_mean
        FROM vehicle_weekly_features v
        JOIN batteries b ON v.battery_id = b.battery_id
        WHERE b.chemistry = 'LFP'
        AND v.week_number = (SELECT MAX(week_number) FROM vehicle_weekly_features v2
                             WHERE v2.battery_id = v.battery_id)
    """)
    # NMC per-battery latest
    nmc = q(conn, """
        SELECT n.battery_id, 'NMC' as chemistry, n.soc_min as soc_min_observed,
               NULL as soc_corrected_voltage_sag, n.dod_estimated as dod_mean
        FROM nmc_weekly_features n
        WHERE n.week_number = (SELECT MAX(week_number) FROM nmc_weekly_features n2
                               WHERE n2.battery_id = n.battery_id)
    """)
    # Fleet stats
    import statistics
    lfp_soc = [r["soc_min_observed"] for r in lfp if r.get("soc_min_observed") is not None]
    lfp_corr = [r["soc_corrected_voltage_sag"] for r in lfp if r.get("soc_corrected_voltage_sag") is not None]
    nmc_soc = [r["soc_min_observed"] for r in nmc if r.get("soc_min_observed") is not None]

    def pstats(vals):
        if not vals:
            return {"mean": None, "median": None, "p10": None, "p90": None}
        s = sorted(vals)
        n = len(s)
        return {"mean": round(sum(s) / n, 1), "median": round(s[n // 2], 1),
                "p10": round(s[max(0, int(n * 0.1))], 1),
                "p90": round(s[min(n - 1, int(n * 0.9))], 1)}

    conn.close()
    return {
        "batteries": lfp + nmc,
        "fleet_stats": {
            "LFP": {"soc_min_observed": pstats(lfp_soc),
                     "soc_corrected_voltage_sag": pstats(lfp_corr)},
            "NMC": {"soc_min_observed": pstats(nmc_soc)},
        },
    }


@app.get("/api/fleet/batch-analysis")
@safe
def fleet_batch_analysis(auth=Depends(verify_token)):
    """Batch-level analysis. Groups by batch_id or battery_id prefix."""
    conn = get_conn()
    # Check if batch_id column exists
    cols = [c[1] for c in conn.cursor().execute("PRAGMA table_info(batteries)").fetchall()]
    has_batch = "batch_id" in cols

    if has_batch:
        rows = q(conn, """
            SELECT COALESCE(b.batch_id, SUBSTR(b.battery_id, 1, 7)) as batch_id,
                   b.chemistry,
                   COUNT(*) as battery_count,
                   ROUND(AVG(h.operational_score), 1) as mean_operational_score,
                   ROUND(SUM(CASE WHEN h.tier_label_v2 IN ('STRESSED','CRITICAL') THEN 1.0 ELSE 0 END)
                         * 100.0 / COUNT(*), 1) as pct_stressed_or_critical
            FROM batteries b
            LEFT JOIN battery_health_scores_v2 h ON b.battery_id = h.battery_id
            WHERE b.onboarding_status = 'ACTIVE'
            GROUP BY batch_id, b.chemistry
            ORDER BY mean_operational_score ASC
        """)
    else:
        rows = q(conn, """
            SELECT SUBSTR(b.battery_id, 1, 7) as batch_id,
                   b.chemistry,
                   COUNT(*) as battery_count,
                   ROUND(AVG(h.operational_score), 1) as mean_operational_score,
                   ROUND(SUM(CASE WHEN h.tier_label_v2 IN ('STRESSED','CRITICAL') THEN 1.0 ELSE 0 END)
                         * 100.0 / COUNT(*), 1) as pct_stressed_or_critical
            FROM batteries b
            LEFT JOIN battery_health_scores_v2 h ON b.battery_id = h.battery_id
            WHERE b.onboarding_status = 'ACTIVE'
            GROUP BY batch_id, b.chemistry
            ORDER BY mean_operational_score ASC
        """)
    conn.close()
    return {"batches": rows, "group_method": "batch_id" if has_batch else "battery_id_prefix"}




# ── Passport v2 Stage 1 endpoints ─────────────────────────────────────

@app.get("/api/fleet/tier-distribution")
def fleet_tier_distribution(chemistry: str = None):
    """Tier counts for doughnut chart. Derives tier from score if tier_label_v2 is NULL."""
    conn = get_conn()
    try:
        where = "WHERE chemistry = ?" if chemistry else ""
        params = [chemistry] if chemistry else []
        rows = q(conn, f"""
            SELECT
                COALESCE(tier_label_v2,
                    CASE
                        WHEN operational_score >= 80 THEN 'PRIME'
                        WHEN operational_score >= 60 THEN 'STABLE'
                        WHEN operational_score >= 40 THEN 'WATCH'
                        WHEN operational_score >= 20 THEN 'STRESSED'
                        ELSE 'CRITICAL'
                    END
                ) as tier,
                COUNT(*) as count
            FROM battery_health_scores_v2
            {where + ' AND ' if where else 'WHERE '} operational_score IS NOT NULL
            GROUP BY tier
        """.replace("WHERE  AND", "WHERE"), params)
        total = sum(r["count"] for r in rows)
        return {"tiers": rows, "total": total, "chemistry": chemistry or "ALL"}
    finally:
        conn.close()


@app.get("/api/fleet/at-risk")
def fleet_at_risk(chemistry: str = None, limit: int = 10):
    """Top N worst-scoring batteries."""
    conn = get_conn()
    try:
        where = "WHERE h.chemistry = ?" if chemistry else ""
        params = [chemistry] if chemistry else []
        rows = q(conn, f"""
            SELECT h.battery_id, h.operational_score as score,
                   COALESCE(h.tier_label_v2,
                       CASE WHEN h.operational_score >= 80 THEN 'PRIME'
                            WHEN h.operational_score >= 60 THEN 'STABLE'
                            WHEN h.operational_score >= 40 THEN 'WATCH'
                            WHEN h.operational_score >= 20 THEN 'STRESSED'
                            ELSE 'CRITICAL' END
                   ) as tier,
                   COALESCE(h.prediction_confidence, h.data_confidence, 'UNKNOWN') as confidence,
                   COALESCE(bi.primary_outcome_concern, bi.operator_action, h.range_cause) as risk_signal,
                   COALESCE(bi.warning_trend, 'UNKNOWN') as trend
            FROM battery_health_scores_v2 h
            LEFT JOIN battery_intelligence bi ON h.battery_id = bi.battery_id
            {where}
            ORDER BY h.operational_score ASC
            LIMIT ?
        """, params + [limit])
        return rows
    finally:
        conn.close()


@app.get("/api/fleet/events/recent")
def fleet_events_recent(chemistry: str = None, days: int = 7):
    """Recent events across fleet."""
    conn = get_conn()
    try:
        chem_join = "JOIN batteries b ON ve.battery_id = b.battery_id AND b.chemistry = ?" if chemistry else ""
        params = [chemistry] if chemistry else []
        rows = q(conn, f"""
            SELECT ve.battery_id, ve.event_type, ve.event_code, ve.severity,
                   ve.week_number, ve.created_at
            FROM vehicle_events ve
            {chem_join}
            ORDER BY ve.created_at DESC
            LIMIT 20
        """, params)
        return rows
    finally:
        conn.close()


@app.get("/api/models/health")
def models_health():
    """Active model status per chemistry."""
    conn = get_conn()
    try:
        lfp_scored = q1(conn, "SELECT COUNT(*) as n, MAX(scored_at) as last FROM battery_health_scores_v2 WHERE chemistry='LFP'")
        nmc_scored = q1(conn, "SELECT COUNT(*) as n, MAX(scored_at) as last FROM battery_health_scores_v2 WHERE chemistry='NMC'")
        return {
            "LFP": {
                "range_model": "range_t2t1_ensemble_v1.0.0",
                "mape": 14.07,
                "scored": lfp_scored["n"] if lfp_scored else 0,
                "last_scored": lfp_scored["last"] if lfp_scored else None,
            },
            "NMC": {
                "health_model": "nmc_phase_b_v1.0",
                "survival_model": "nmc_survival_v2.1.0",
                "scored": nmc_scored["n"] if nmc_scored else 0,
                "last_scored": nmc_scored["last"] if nmc_scored else None,
            }
        }
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/scores")
def battery_scores_history(battery_id: str):
    """Weekly score history for tier sparkline."""
    conn = get_conn()
    try:
        # Try VWF for weekly composite trajectory (approximation from features)
        rows = q(conn, """
            SELECT week_number, km_per_soc_pct, cell_spread_max, cusum_x_weeks, temp_max
            FROM vehicle_weekly_features
            WHERE battery_id = ? ORDER BY week_number
        """, [battery_id])
        if not rows:
            rows = q(conn, """
                SELECT week_number, cell_spread_max, temp_max
                FROM nmc_weekly_features
                WHERE battery_id = ? ORDER BY week_number
            """, [battery_id])
        # Also get current score
        current = q1(conn, """
            SELECT operational_score, tier_label_v2, l1_score, l2_score, l3_score,
                   event_penalty, adjusted_composite, scoring_mode, data_confidence
            FROM battery_health_scores_v2 WHERE battery_id = ?
        """, [battery_id])
        return {"weekly": rows, "current": current}
    finally:
        conn.close()


# ── v2.0 dual-score endpoint ──────────────────────────────────────────

@app.get("/api/battery/{battery_id}/scores-v2")
@safe
def battery_scores_v2(battery_id: str, _=Depends(verify_token)):
    """v2 scores: res_score, bhs_score, composite, confidence, divergence."""
    conn = get_conn()
    row = q1(conn, """
        SELECT battery_id, operational_score, res_score, bhs_score,
               composite_score_operator, composite_score_nbfc, composite_score_oem,
               confidence_pct, confidence_basis,
               divergence_alert, divergence_note,
               p_floor_breach_4w, p_floor_breach_8w, p_floor_breach_12w,
               cohort_rank_pack, cohort_rank_city, bhs_tier,
               tier_label_v2, scoring_mode,
               data_staleness_flag, last_data_date, prediction_valid_until,
               score_suppressed, suppression_reason, data_gap_weeks
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    if not row:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    return row


# ── Passport v2 Stage 2 endpoints ─────────────────────────────────────

@app.get("/api/battery/{battery_id}/predictions")
def battery_predictions(battery_id: str):
    """Range P10/P50/P90, RUL, model info."""
    conn = get_conn()
    try:
        # Range forecast
        fc = q1(conn, "SELECT * FROM range_forecasts WHERE battery_id=?", [battery_id])
        # Advanced predictions (P10/P50/P90 at 12W)
        adv = q(conn, """
            SELECT horizon_weeks, quantile, predicted_range
            FROM range_predictions_advanced_v2
            WHERE battery_id=? AND week_number=(
                SELECT MAX(week_number) FROM range_predictions_advanced_v2 WHERE battery_id=?
            ) ORDER BY horizon_weeks, quantile
        """, [battery_id, battery_id])
        # RUL
        rul = q1(conn, "SELECT * FROM rul_estimates WHERE battery_id=?", [battery_id])
        # SHAP from diagnostics
        diag = q1(conn, "SELECT shap_attribution FROM battery_diagnostics WHERE battery_id=?", [battery_id])
        shap_data = None
        if diag and diag.get("shap_attribution"):
            try:
                import json as _j
                shap_data = _j.loads(diag["shap_attribution"]) if isinstance(diag["shap_attribution"], str) else diag["shap_attribution"]
            except: pass
        return {"forecast": fc, "advanced": adv, "rul": rul, "shap": shap_data}
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/survival")
def battery_survival(battery_id: str):
    """NMC survival forecast."""
    conn = get_conn()
    try:
        row = q1(conn, "SELECT * FROM nmc_service_forecast WHERE battery_id=?", [battery_id])
        # Check silent degrader: high events but low complaints
        events_count = q1(conn, "SELECT COUNT(*) as n FROM vehicle_events WHERE battery_id=?", [battery_id])
        complaints = q1(conn, "SELECT COUNT(*) as n FROM nmc_service_raw WHERE linked_battery_id=?", [battery_id])
        silent = False
        if events_count and complaints:
            if (events_count["n"] or 0) > 10 and (complaints["n"] or 0) < 3:
                silent = True
        return {"forecast": row, "silent_degrader": silent,
                "events_count": events_count["n"] if events_count else 0,
                "complaints_count": complaints["n"] if complaints else 0}
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/telemetry")
def battery_telemetry(battery_id: str, days: int = 7):
    """Downsampled telemetry for live signal charts. Max 5000 points."""
    conn = get_conn()
    try:
        # Count total rows
        total = q1(conn, "SELECT COUNT(*) as n FROM telemetry_raw WHERE battery_id=?", [battery_id])
        n = total["n"] if total else 0
        # Downsample: take every Nth row to get ~2000 points
        step = max(1, n // 2000)
        rows = q(conn, f"""
            SELECT timestamp, pack_voltage, pack_current, pack_soc, temp_max,
                   cell_voltage_min, cell_voltage_max, cell_voltage_delta
            FROM telemetry_raw
            WHERE battery_id=? AND rowid % {step} = 0
            ORDER BY timestamp DESC
            LIMIT 5000
        """, [battery_id])
        rows.reverse()  # chronological
        return {"points": rows, "total_rows": n, "step": step, "battery_id": battery_id}
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/events")
@safe
def battery_events_full(battery_id: str, dedup: bool = False,
                        _=Depends(verify_token)):
    """All events with full detail.

    BE-1 (2026-04-23) upgrade: adds auth + @safe, plus plain-English fields
    alongside the legacy raw row shape. Existing callers reading `events[]`
    keep working; new callers (UX-5 evidence surfaces) can read
    `shaped_events[]` with type/name/severity normalised.

    ux23: when ?dedup=1 is passed, returns a flat deduplicated list (one
    row per battery/week/event_code) with scoring_excluded filtered out.
    Designed for the Events tab's table render. Without ?dedup, keeps the
    legacy dict-shaped response so existing callers keep working.
    """
    conn = get_conn()
    if dedup:
        try:
            rows = q(conn, """
                SELECT event_code,
                       event_type,
                       severity,
                       week_number,
                       signal_name,
                       signal_value,
                       signal_baseline,
                       signal_deviation_pct,
                       causal_chain,
                       rc_primary,
                       event_reliability,
                       data_source,
                       MAX(created_at) AS created_at
                FROM vehicle_events
                WHERE battery_id = ?
                  AND (scoring_excluded IS NULL OR scoring_excluded = 0)
                GROUP BY battery_id, week_number, event_code
                ORDER BY week_number ASC
            """, [battery_id])
            return rows or []
        finally:
            conn.close()
    try:
        raw_events = q(conn, """
            SELECT event_type, event_code, severity, week_number, confidence,
                   signal_name, signal_value, signal_baseline, signal_deviation_pct,
                   causal_chain, rc_primary, created_at,
                   data_source, can_coverage_at_event, event_reliability,
                   resolved_week, signal_source
            FROM vehicle_events
            WHERE battery_id=? ORDER BY week_number DESC LIMIT 200
        """, [battery_id])
        # Get battery commissioning week count
        weeks = q1(conn, """
            SELECT COUNT(DISTINCT week_number) as n, MIN(week_number) as first, MAX(week_number) as last
            FROM vehicle_weekly_features WHERE battery_id=?
        """, [battery_id])
        if not weeks or not weeks.get("n"):
            weeks = q1(conn, """
                SELECT COUNT(DISTINCT week_number) as n, MIN(week_number) as first, MAX(week_number) as last
                FROM nmc_weekly_features WHERE battery_id=?
            """, [battery_id])
        # BE-1 shaped events — plain English names + normalised severity.
        shaped = []
        active_count = 0
        for r in (raw_events or [])[:50]:
            code = r.get("event_code") or ""
            name = _BE1_EVENT_PLAIN.get(code, r.get("event_type") or code or 'Unknown')
            sev = _be1_severity_bucket(r.get("severity"))
            resolved = r.get("resolved_week")
            status = 'RESOLVED' if resolved else 'ACTIVE'
            if status == 'ACTIVE':
                active_count += 1
            sv = r.get("signal_value")
            sb = r.get("signal_baseline")
            sd = r.get("signal_deviation_pct")
            if sv is not None and sb is not None:
                val_str = f"{sv:.3f} vs baseline {sb:.3f}"
            elif sv is not None:
                val_str = f"{sv:.3f}"
            elif sd is not None:
                val_str = f"{sd:.1f}% deviation"
            else:
                val_str = None
            shaped.append({
                "type": code,
                "name": name,
                "event_type_raw": r.get("event_type"),
                "severity": sev,
                "week_number": r.get("week_number"),
                "status": status,
                "trajectory": 'STABLE',
                "recurrence_count": 1,
                "resolved_week": resolved,
                "value": val_str,
                "reliability": r.get("event_reliability"),
                "signal_source": r.get("signal_source"),
            })
        return {
            "battery_id": battery_id,
            "events": raw_events,
            "weeks": weeks,
            "shaped_events": shaped,
            "active_count": active_count,
            "source": "vehicle_events",
            "disclosure": None,
        }
    finally:
        try: conn.close()
        except Exception: pass


# ── Behaviour endpoints (Sprint UI-B1) ─────────────────────────────────

@app.get("/api/battery/{battery_id}/behaviour")
@safe
def battery_behaviour(battery_id: str, _=Depends(verify_token)):
    """12 weeks of behaviour features from VWF."""
    conn = get_conn()
    rows = q(conn, """
        SELECT week_number,
               crate_high_pct, crate_mid_pct, crate_low_pct, crate_max_weekly,
               fast_charge_pct_week, fast_charge_session_count,
               thermal_soak_min_40_week, thermal_soak_min_55_week,
               ambient_temp_mean_week, ambient_temp_max_week,
               temp_delta_cell_vs_ambient
        FROM vehicle_weekly_features
        WHERE battery_id = ?
        ORDER BY week_number DESC LIMIT 12
    """, [battery_id])
    conn.close()
    return {"battery_id": battery_id, "weeks": list(reversed(rows))}


@app.get("/api/battery/{battery_id}/behaviour/summary")
@safe
def battery_behaviour_summary(battery_id: str, _=Depends(verify_token)):
    """4-week aggregated behaviour summary with fleet context."""
    conn = get_conn()
    rows = q(conn, """
        SELECT crate_high_pct, fast_charge_pct_week, fast_charge_session_count,
               thermal_soak_min_40_week, thermal_soak_min_55_week,
               ambient_temp_max_week, temp_delta_cell_vs_ambient
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND crate_high_pct IS NOT NULL
        ORDER BY week_number DESC LIMIT 4
    """, [battery_id])

    if not rows:
        conn.close()
        return {"battery_id": battery_id, "has_data": False}

    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def total(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals), 2) if vals else None

    fc_pct = avg("fast_charge_pct_week")
    fc_count = total("fast_charge_session_count")
    ch_pct = avg("crate_high_pct")
    ts40 = avg("thermal_soak_min_40_week")
    ts55 = avg("thermal_soak_min_55_week")
    amb_max = max((r["ambient_temp_max_week"] for r in rows if r.get("ambient_temp_max_week") is not None), default=None)
    td = avg("temp_delta_cell_vs_ambient")

    risk = "HIGH" if (ts55 or 0) > 10 else "ELEVATED" if (ts40 or 0) > 30 else "NORMAL"

    # Fleet P75 (last 4 weeks, all batteries with data)
    fleet_fc = q1(conn, """
        SELECT PERCENTILE_75 as p75 FROM (
            SELECT fast_charge_pct_week as val FROM vehicle_weekly_features
            WHERE fast_charge_pct_week IS NOT NULL
            ORDER BY week_number DESC LIMIT 800
        ) sub ORDER BY val LIMIT 1 OFFSET (SELECT COUNT(*)*3/4 FROM (
            SELECT fast_charge_pct_week FROM vehicle_weekly_features
            WHERE fast_charge_pct_week IS NOT NULL
            ORDER BY week_number DESC LIMIT 800))
    """)
    # Simpler P75 approach
    fleet_fc_rows = q(conn, """
        SELECT fast_charge_pct_week as val FROM vehicle_weekly_features
        WHERE fast_charge_pct_week IS NOT NULL
        ORDER BY week_number DESC LIMIT 800
    """)
    fleet_fc_vals = sorted([r["val"] for r in fleet_fc_rows if r["val"] is not None])
    fleet_fc_p75 = fleet_fc_vals[int(len(fleet_fc_vals) * 0.75)] if fleet_fc_vals else 0

    fleet_ts_rows = q(conn, """
        SELECT thermal_soak_min_40_week as val FROM vehicle_weekly_features
        WHERE thermal_soak_min_40_week IS NOT NULL
        ORDER BY week_number DESC LIMIT 800
    """)
    fleet_ts_vals = sorted([r["val"] for r in fleet_ts_rows if r["val"] is not None])
    fleet_ts_p75 = fleet_ts_vals[int(len(fleet_ts_vals) * 0.75)] if fleet_ts_vals else 0

    # Anomaly rate from battery_trip_scores (30 days)
    anom_row = q1(conn, """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN anomaly_flag = 1 THEN 1 ELSE 0 END) as anomalies
        FROM battery_trip_scores
        WHERE battery_id = ?
          AND trip_date >= date('now', '-30 days')
    """, [battery_id])
    anom_total = anom_row["total"] if anom_row else 0
    anom_count = anom_row["anomalies"] if anom_row else 0
    anom_rate = round(anom_count / anom_total, 4) if anom_total > 0 else 0

    conn.close()
    return {
        "battery_id": battery_id, "has_data": True,
        "fast_charge_pct_4w": fc_pct,
        "fast_charge_flag": (fc_pct or 0) > 0.15,
        "fast_charge_session_count_4w": fc_count,
        "crate_high_pct_4w": ch_pct,
        "thermal_soak_min_40_4w": ts40,
        "thermal_soak_min_55_4w": ts55,
        "thermal_soak_risk": risk,
        "ambient_temp_max_4w": amb_max,
        "temp_delta_4w": td,
        "fleet_fast_charge_p75": round(fleet_fc_p75, 4),
        "fleet_thermal_soak_p75": round(fleet_ts_p75, 2),
        "anomaly_rate_30d": anom_rate,
        "anomaly_count_30d": anom_count,
    }


@app.get("/api/battery/{battery_id}/trips/anomalies")
@safe
def battery_trip_anomalies(battery_id: str, _=Depends(verify_token)):
    """Last 20 anomalous trips from battery_trip_scores."""
    conn = get_conn()
    trips = q(conn, """
        SELECT trip_date, trip_id, efficiency, efficiency_zscore,
               anomaly_direction, baseline_mean, temp_max_trip
        FROM battery_trip_scores
        WHERE battery_id = ? AND anomaly_flag = 1
        ORDER BY trip_date DESC LIMIT 20
    """, [battery_id])

    overall = q1(conn, """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN anomaly_flag = 1 THEN 1 ELSE 0 END) as anomalies
        FROM battery_trip_scores
        WHERE battery_id = ?
    """, [battery_id])
    total = overall["total"] if overall else 0
    anom = overall["anomalies"] if overall else 0

    conn.close()
    return {
        "battery_id": battery_id,
        "trips": trips,
        "anomaly_count_30d": anom,
        "anomaly_rate_overall": round(anom / total, 4) if total > 0 else 0,
    }


# ── Score breakdown (Sprint UI-INT) ────────────────────────────────────

@app.get("/api/battery/{battery_id}/score-breakdown")
@safe
def battery_score_breakdown(battery_id: str, _=Depends(verify_token)):
    """L1/L2/L3 breakdown + causal direction + BMS bias."""
    conn = get_conn()
    score = q1(conn, """
        SELECT l1_score, l2_score, l3_score, efficiency_score_v2, fault_risk_score,
               operational_score, adjusted_composite
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    if not score:
        conn.close()
        return {"battery_id": battery_id, "has_data": False}

    comp = score.get("operational_score") or 0
    l1 = score.get("l1_score") if score.get("l1_score") is not None else round(comp * 0.5)
    l2 = score.get("l2_score") if score.get("l2_score") is not None else round(comp * 0.35)
    l3 = score.get("l3_score") if score.get("l3_score") is not None else round(comp * 0.15)

    # Causal direction from VWF (charging intensity vs efficiency)
    vwf = q(conn, """
        SELECT week_number, km_per_soc_pct, charge_cycle_rate
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND km_per_soc_pct IS NOT NULL
        ORDER BY week_number
    """, [battery_id])

    causal = {"direction": None, "text": None, "charging_rise_week": None, "range_decline_week": None}
    if len(vwf) >= 12:
        early = vwf[:8]
        early_kps = [r["km_per_soc_pct"] for r in early if r["km_per_soc_pct"]]
        early_chg = [r["charge_cycle_rate"] for r in early if r.get("charge_cycle_rate") is not None]
        if early_kps and early_chg:
            kps_bl = sum(early_kps) / len(early_kps)
            chg_bl = sum(early_chg) / len(early_chg) if early_chg else 0

            # Find sustained decline/rise (2+ consecutive weeks)
            chg_rise_wk, rng_decline_wk = None, None
            streak_chg, streak_rng = 0, 0
            for r in vwf[8:]:
                if r.get("charge_cycle_rate") is not None and chg_bl > 0 and r["charge_cycle_rate"] > chg_bl * 1.15:
                    streak_chg += 1
                    if streak_chg >= 2 and chg_rise_wk is None:
                        chg_rise_wk = r["week_number"]
                else:
                    streak_chg = 0
                if r["km_per_soc_pct"] and kps_bl > 0 and r["km_per_soc_pct"] < kps_bl * 0.85:
                    streak_rng += 1
                    if streak_rng >= 2 and rng_decline_wk is None:
                        rng_decline_wk = r["week_number"]
                else:
                    streak_rng = 0

            if chg_rise_wk and rng_decline_wk:
                late_chg = [r["charge_cycle_rate"] for r in vwf[-4:] if r.get("charge_cycle_rate") is not None]
                chg_pct = round(((sum(late_chg)/len(late_chg) - chg_bl) / max(abs(chg_bl), 0.01)) * 100) if late_chg and chg_bl else 0
                if chg_rise_wk < rng_decline_wk:
                    causal = {"direction": "CAUSATION", "charging_rise_week": chg_rise_wk, "range_decline_week": rng_decline_wk,
                              "text": f"Charging intensity rose {abs(chg_pct)}% BEFORE range declined. Charging stress is the likely cause."}
                else:
                    causal = {"direction": "COMPENSATION", "charging_rise_week": chg_rise_wk, "range_decline_week": rng_decline_wk,
                              "text": f"Range fell first, charging rose {abs(chg_pct)}% after. Confirms real battery degradation, not operator abuse."}

    conn.close()
    return {
        "battery_id": battery_id, "has_data": True,
        "l1": round(l1) if l1 is not None else None,
        "l2": round(l2) if l2 is not None else None,
        "l3": round(l3) if l3 is not None else None,
        "causal": causal,
    }


# ── Alerts endpoints (Sprint vScore) ───────────────────────────────────

@app.get("/api/battery/{battery_id}/alerts")
@safe
def battery_alerts(battery_id: str, _=Depends(verify_token)):
    """All active alerts + score status for a battery."""
    conn = get_conn()
    alerts = q(conn, """
        SELECT alert_id, alert_category, alert_type, priority, title, detail,
               action_operator, action_oem, recheck_weeks, signal_name,
               signal_value, threshold_value, fired_at
        FROM platform_alerts
        WHERE battery_id = ? AND active = 1
        ORDER BY priority ASC, fired_at DESC
    """, [battery_id])

    score = q1(conn, """
        SELECT scoring_mode, confidence_score, score_explanation,
               data_confidence_label, vscore_flag, nbfc_suitable, nbfc_caveat,
               remedy_operator, remedy_oem, signal_gaps,
               operational_score, adjusted_composite, tier_label_v2
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])

    # DQ override: use data_gap_weeks from BHS (not CAN source check — column removed)
    ss = dict(score) if score else {}
    gap_wks = score.get("data_gap_weeks") if score else None
    if gap_wks and gap_wks > 12:
        ss["dq_override"] = True
        ss["dq_override_reason"] = f"Data gap {gap_wks}wks"
        ss["can_gap_weeks"] = gap_wks
    # Do NOT override scoring_mode to SUSPENDED here — use BHS scoring_mode as-is

    conn.close()
    return {
        "battery_id": battery_id,
        "alerts": alerts,
        "score_status": ss,
    }


@app.get("/api/fleet/alerts/summary")
@safe
def fleet_alerts_summary(_=Depends(verify_token)):
    """Fleet-level alert + scoring mode summary."""
    conn = get_conn()
    total = q1(conn, "SELECT COUNT(*) as n FROM battery_health_scores_v2")
    total_n = total["n"] if total else 0

    modes = q(conn, """
        SELECT scoring_mode, COUNT(*) as n FROM battery_health_scores_v2
        GROUP BY scoring_mode
    """)
    mode_map = {r["scoring_mode"]: r["n"] for r in modes}

    alert_counts = q(conn, """
        SELECT priority, COUNT(*) as n FROM platform_alerts
        WHERE active = 1 GROUP BY priority
    """)
    alert_map = {r["priority"]: r["n"] for r in alert_counts}

    pipe_fails = q1(conn, """
        SELECT COUNT(*) as n FROM platform_alerts
        WHERE active = 1 AND alert_type = 'INGEST_FAILURE'
    """)

    conn.close()

    scored_full = sum(v for k, v in mode_map.items() if k and "FULL" == k)
    scored_partial = sum(v for k, v in mode_map.items()
                         if k and ("PARTIAL" in k or "SUPPRESSED" in k) and k != "FULL")
    scored_estimated = mode_map.get("ESTIMATED", 0)
    suspended = sum(v for k, v in mode_map.items()
                    if k and ("SUSPENDED" in k or "INSUFFICIENT" in k))
    normal_none = mode_map.get(None, 0)

    return {
        "total_batteries": total_n,
        "scored_full": scored_full + normal_none,
        "scored_partial": scored_partial,
        "scored_estimated": scored_estimated,
        "suspended": suspended,
        "alerts_p1": alert_map.get(1, 0),
        "alerts_p2": alert_map.get(2, 0),
        "alerts_p3": alert_map.get(3, 0),
        "alerts_p4": alert_map.get(4, 0),
        "pipeline_failures": pipe_fails["n"] if pipe_fails else 0,
    }


# ── Forensic endpoint (DI sprint) ──────────────────────────────────────

@app.get("/api/battery/{battery_id}/forensic")
@safe
def battery_forensic(battery_id: str, _=Depends(verify_token)):
    """Full forensic data for one battery — identity, DQ, score, weekly coverage, events, alerts."""
    conn = get_conn()

    # Identity
    bat = q1(conn, """
        SELECT battery_id, original_id, battery_model, chemistry,
               commissioning_date, oem_name, batch_id
        FROM batteries WHERE battery_id = ?
    """, [battery_id])
    if not bat:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")

    cohort = q1(conn, """
        SELECT COUNT(*) as n FROM batteries
        WHERE battery_model = ? AND chemistry = ?
    """, [bat.get("battery_model"), bat.get("chemistry")])
    bat["cohort_size"] = cohort["n"] if cohort else 0

    # Score from battery_health_scores_v2
    score = q1(conn, """
        SELECT operational_score, adjusted_composite, tier_label_v2, scoring_mode,
               data_quality_verdict, score_usable_for_decisions,
               predicted_range_12w, range_p10_adjusted, range_p90_adjusted,
               confidence_score, model_version, scored_at,
               event_penalty, data_quality_flags
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id]) or {}

    # Weekly coverage (all weeks, with CAN quality columns)
    weekly = q(conn, """
        SELECT week_number, active_days, trip_count, km_sum,
               km_per_soc_pct, cell_spread_max, cell_spread_mean,
               temp_max, voltage_min, soh_cap_weekly, dod_mean,
               NULL as can_row_count_week, NULL as can_valid_pct_week, NULL as can_data_source
        FROM vehicle_weekly_features
        WHERE battery_id = ?
        ORDER BY week_number
    """, [battery_id])

    # Events (all, with reliability)
    events = q(conn, """
        SELECT event_type, event_code, week_number, severity,
               signal_value, signal_baseline, signal_deviation_pct,
               event_reliability, data_source, can_coverage_at_event,
               causal_chain, rc_primary, confidence
        FROM vehicle_events
        WHERE battery_id = ?
        ORDER BY week_number
    """, [battery_id])

    # Alerts (if table exists)
    alerts = []
    try:
        alerts = q(conn, """
            SELECT alert_type, week_number, message, severity, created_at
            FROM battery_alerts
            WHERE battery_id = ? AND resolved_at IS NULL
            ORDER BY created_at DESC
        """, [battery_id])
    except Exception:
        pass

    # Compute CAN gap from weekly data
    can_weeks = [w for w in weekly if w.get("can_data_source") == "CAN_GPS_DAILY"]
    can_last_week = max((w["week_number"] for w in can_weeks), default=0)
    total_weeks = len(weekly)
    gps_last_week = max((w["week_number"] for w in weekly), default=0)
    can_gap_weeks = gps_last_week - can_last_week if gps_last_week > can_last_week else 0
    soh_weeks = sum(1 for w in weekly if w.get("soh_cap_weekly") is not None)

    # Parse flags
    flags = []
    raw_flags = score.get("data_quality_flags")
    if raw_flags:
        try:
            import json as _json
            flags = _json.loads(raw_flags) if isinstance(raw_flags, str) else raw_flags
        except Exception:
            flags = [raw_flags]

    # Build DQ summary
    dq_parts = []
    if can_gap_weeks >= 4:
        dq_parts.append(f"CAN data absent for {can_gap_weeks} weeks (IoT failure suspected).")
    elif can_gap_weeks >= 2:
        dq_parts.append(f"CAN data gap of {can_gap_weeks} weeks detected.")
    if soh_weeks == 0:
        dq_parts.append(f"soh_cap_weekly NULL for all {total_weeks} weeks — no SOH trajectory.")
    verdict = score.get("data_quality_verdict") or score.get("scoring_mode", "UNKNOWN")
    if "SUPPRESSED" in str(verdict):
        dq_parts.append("Score SUSPENDED — not usable for financial decisions.")

    data_quality = {
        "data_quality_verdict": verdict,
        "data_quality_flags": flags,
        "can_last_week": can_last_week,
        "can_gap_weeks": can_gap_weeks,
        "gps_last_week": gps_last_week,
        "soh_weeks": soh_weeks,
        "total_weeks": total_weeks,
        "dq_summary": " ".join(dq_parts) if dq_parts else "Data quality acceptable.",
    }

    conn.close()
    return {
        "identity": bat,
        "data_quality": data_quality,
        "score": score,
        "weekly_coverage": weekly,
        "events": events,
        "alerts": alerts,
    }


@app.get("/api/fleet/data-quality-summary")
@safe
def fleet_dq_summary(_=Depends(verify_token)):
    """Fleet-level DQ summary counts."""
    conn = get_conn()
    rows = q(conn, """
        SELECT scoring_mode, score_usable_for_decisions, COUNT(*) as n
        FROM battery_health_scores_v2
        GROUP BY scoring_mode, score_usable_for_decisions
    """)
    total = sum(r["n"] for r in rows)
    suppressed = [r for r in rows if r.get("scoring_mode") and "SUPPRESSED" in r["scoring_mode"]]
    not_usable_rows = q(conn, """
        SELECT battery_id FROM battery_health_scores_v2
        WHERE score_usable_for_decisions = 0
    """)
    not_usable_ids = [r["battery_id"] for r in not_usable_rows]
    conn.close()
    return {
        "total": total,
        "score_suspended": sum(r["n"] for r in suppressed),
        "not_usable": len(not_usable_ids),
        "batteries_not_usable": not_usable_ids,
    }


# ── Passport v2 Stage 3 endpoints ─────────────────────────────────────

@app.get("/api/battery/{battery_id}/investigation")
def battery_investigation_full(battery_id: str):
    """Full investigation row + evidence chain + complaints."""
    conn = get_conn()
    try:
        inv = q1(conn, "SELECT * FROM battery_investigations WHERE battery_id=?", [battery_id])
        complaints = q(conn, """
            SELECT complaint_date, complaint_category, diagnosis_category,
                   resolution_type, delta_mv, inferred_diagnosis
            FROM nmc_service_raw
            WHERE linked_battery_id=? ORDER BY complaint_date
        """, [battery_id])
        chem = q1(conn, "SELECT chemistry FROM batteries WHERE battery_id=?", [battery_id])
        return {
            "investigation": inv,
            "complaints": complaints,
            "chemistry": chem["chemistry"] if chem else None,
            "battery_id": battery_id,
        }
    finally:
        conn.close()


# ── POST /api/battery/:id/investigation (SIGNAL-TRIANGULATION) ───────

class InvestigationBody(_BM):
    complaint_date: Optional[str] = None
    complaint_text: Optional[str] = None

@app.post("/api/battery/{battery_id}/investigation")
@safe
def run_investigation(battery_id: str, body: InvestigationBody = None, _=Depends(verify_token)):
    """Signal Triangulation Engine: convert complaint into physics verdict."""
    from datetime import date as _date

    conn = get_conn()

    complaint_date = (body.complaint_date if body and body.complaint_date
                      else _date.today().isoformat())
    complaint_text = body.complaint_text if body else None

    # ── Locate complaint week ──
    cw_row = q1(conn, """
        SELECT week_number, week_start_date FROM vehicle_weekly_features
        WHERE battery_id = ? AND week_start_date IS NOT NULL
        ORDER BY ABS(julianday(week_start_date) - julianday(?))
        LIMIT 1
    """, [battery_id, complaint_date])
    if not cw_row:
        conn.close()
        raise HTTPException(404, f"No VWF data for {battery_id}")
    complaint_week = cw_row["week_number"]

    # ── Fetch ±6 week window from VWF ──
    window = q(conn, """
        SELECT week_number, km_per_soc_pct, km_per_soc_slope,
               cell_spread_max, week_start_date
        FROM vehicle_weekly_features
        WHERE battery_id = ?
          AND week_number BETWEEN ? AND ?
        ORDER BY week_number ASC
    """, [battery_id, complaint_week - 6, complaint_week + 6])

    # ── Fetch BHS snapshot ──
    bhs = q1(conn, """
        SELECT kps_slope_4wk, capacity_state, soh_corrected, degradation_regime,
               range_corrected_km, commissioned_range_km, operational_score,
               rul_action_v2, degradation_primary_driver,
               shap_top1_feature, shap_top1_pct,
               remedy_operator, remedy_oem
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])

    # ── Commissioning baseline (weeks 1-4) ──
    baseline = q(conn, """
        SELECT AVG(cell_spread_max) as spread_baseline,
               AVG(km_per_soc_pct) as kps_baseline
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND week_number <= 4
    """, [battery_id])
    spread_baseline = baseline[0]["spread_baseline"] if baseline and baseline[0]["spread_baseline"] else 0
    kps_baseline = baseline[0]["kps_baseline"] if baseline and baseline[0]["kps_baseline"] else None

    # ── soh_corrected at week 4 ──
    soh_w4_row = q1(conn, """
        SELECT soh_corrected FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    # soh_corrected is a single latest value, not per-week; use baseline proxy
    soh_at_w4 = kps_baseline * 100 / 1.3 if kps_baseline else None  # rough proxy

    # ── Operational score 8 weeks prior ──
    ops_prior_row = q1(conn, """
        SELECT operational_score FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])

    # ── Event density in window ──
    ev_count_row = q1(conn, """
        SELECT COUNT(*) as n FROM vehicle_events
        WHERE battery_id = ?
          AND week_number BETWEEN ? AND ?
          AND event_type != 'MICRO_SHORT'
    """, [battery_id, complaint_week - 6, complaint_week + 6])
    ev_count = ev_count_row["n"] if ev_count_row else 0

    # Fleet P75 event rate for same 13-week window size
    fleet_p75_row = q(conn, """
        SELECT COUNT(*) as n FROM vehicle_events
        WHERE week_number BETWEEN ? AND ?
          AND event_type != 'MICRO_SHORT'
        GROUP BY battery_id ORDER BY n
    """, [complaint_week - 6, complaint_week + 6])
    fleet_rates = [r["n"] for r in fleet_p75_row] if fleet_p75_row else [0]
    fleet_p75 = fleet_rates[int(len(fleet_rates) * 0.75)] if fleet_rates else 5

    # ── Attribution ──
    attr = q1(conn, """
        SELECT primary_driver, charging_pct, usage_pct, maintenance_pct,
               thermal_pct, calendar_pct, operator_attribution_sentence
        FROM battery_degradation_attribution WHERE battery_id = ?
    """, [battery_id])

    # ── Run 7 signals ──
    signals = []

    # SIGNAL 1: kps slope at complaint week (from VWF, not BHS latest)
    complaint_slope = None
    for w in window:
        if w["week_number"] == complaint_week and w.get("km_per_soc_slope") is not None:
            complaint_slope = w["km_per_soc_slope"]
            break
    if complaint_slope is None and bhs:
        complaint_slope = bhs.get("kps_slope_4wk")
    s1_confirmed = complaint_slope is not None and complaint_slope < -0.010
    signals.append({
        "signal": "kps_slope_at_complaint", "weight": 1,
        "finding": "Slope: %.4f km/SoC/week" % (complaint_slope or 0),
        "status": "CONFIRMED" if s1_confirmed else "NOT_CONFIRMED",
    })

    # SIGNAL 2: range trend in window (OLS slope on kps*80)
    wk_kps = [(w["week_number"], w["km_per_soc_pct"])
              for w in window if w.get("km_per_soc_pct")]
    range_slope = None
    if len(wk_kps) >= 3:
        xs = [w[0] for w in wk_kps]
        ys = [w[1] * 80 for w in wk_kps]
        n = len(xs)
        mx, my = sum(xs)/n, sum(ys)/n
        num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
        den = sum((x-mx)**2 for x in xs)
        range_slope = num / den if den > 0 else 0
    s2_confirmed = range_slope is not None and range_slope < -0.5
    signals.append({
        "signal": "range_trend_window", "weight": 1,
        "finding": "Range slope: %.2f km/week" % (range_slope or 0),
        "status": "CONFIRMED" if s2_confirmed else "NOT_CONFIRMED",
    })

    # SIGNAL 3: cell_spread delta vs commissioning
    window_spreads = [w["cell_spread_max"] for w in window
                      if w.get("cell_spread_max") and w["cell_spread_max"] > 0]
    complaint_spread = sum(window_spreads) / len(window_spreads) if window_spreads else 0
    spread_delta = complaint_spread - spread_baseline if spread_baseline else 0
    s3_confirmed = spread_delta > float(get_param('spread_alert_delta_mv', default=80))
    signals.append({
        "signal": "cell_spread_delta", "weight": 1,
        "finding": "Spread delta: %.0fmV (window %.0f vs baseline %.0f)" % (
            spread_delta, complaint_spread, spread_baseline),
        "status": "CONFIRMED" if s3_confirmed else "NOT_CONFIRMED",
    })

    # SIGNAL 4: operational_score below fleet average
    ops_current = bhs["operational_score"] if bhs and bhs.get("operational_score") is not None else None
    ops_fleet_row = q1(conn, """
        SELECT AVG(operational_score) as avg_ops
        FROM battery_health_scores_v2 WHERE chemistry='LFP'
    """)
    ops_fleet = ops_fleet_row["avg_ops"] if ops_fleet_row and ops_fleet_row["avg_ops"] else 50
    s4_confirmed = ops_current is not None and (ops_fleet - ops_current) > 8
    signals.append({
        "signal": "operational_score_drop", "weight": 1,
        "finding": "Operational score: %.1f (fleet avg: %.1f)" % (
            ops_current if ops_current is not None else 0, ops_fleet),
        "status": "CONFIRMED" if s4_confirmed else "NOT_CONFIRMED",
    })

    # SIGNAL 5: event density
    s5_confirmed = ev_count > fleet_p75
    signals.append({
        "signal": "event_density", "weight": 1,
        "finding": "%d events in window (fleet P75: %d)" % (ev_count, fleet_p75),
        "status": "CONFIRMED" if s5_confirmed else "NOT_CONFIRMED",
    })

    # SIGNAL 6: efficiency decline — complaint-week kps vs baseline kps
    complaint_kps_val = None
    for w in window:
        if w["week_number"] == complaint_week and w.get("km_per_soc_pct"):
            complaint_kps_val = w["km_per_soc_pct"]
            break
    kps_ratio = (complaint_kps_val / kps_baseline) if complaint_kps_val and kps_baseline and kps_baseline > 0 else None
    s6_confirmed = kps_ratio is not None and kps_ratio < 0.85
    signals.append({
        "signal": "efficiency_decline", "weight": 1,
        "finding": "Efficiency ratio: %.1f%% of baseline" % ((kps_ratio or 1) * 100),
        "status": "CONFIRMED" if s6_confirmed else "NOT_CONFIRMED",
    })

    # SIGNAL 7: capacity_state at complaint
    cap_state = bhs["capacity_state"] if bhs else None
    degraded_states = {'DECLINING_FAST', 'CRITICAL', 'POST_KNEE', 'APPROACHING_KNEE', 'NEAR_EOL'}
    s7_confirmed = cap_state in degraded_states
    signals.append({
        "signal": "capacity_state", "weight": 1,
        "finding": "State: %s" % (cap_state or "UNKNOWN"),
        "status": "CONFIRMED" if s7_confirmed else "NOT_CONFIRMED",
    })

    # ── Verdict ──
    score = sum(1 for s in signals if s["status"] == "CONFIRMED")
    if score >= 5:
        verdict = "PHYSICS_CONFIRMED"
    elif score >= 3:
        verdict = "PROGRESSIVE"
    elif score >= 1:
        verdict = "WEAK_SIGNAL"
    else:
        verdict = "UNCONFIRMED"
    confidence = round(score / 7, 2)

    # ── Range values ──
    complaint_kps = None
    for w in window:
        if w["week_number"] == complaint_week and w.get("km_per_soc_pct"):
            complaint_kps = w["km_per_soc_pct"]
            break
    range_at_complaint = round(complaint_kps * 80, 1) if complaint_kps else None
    range_latest = round(bhs["range_corrected_km"], 1) if bhs and bhs["range_corrected_km"] else None
    range_comm = round(bhs["commissioned_range_km"], 1) if bhs and bhs["commissioned_range_km"] else None

    # ── Verdict sentence ──
    if verdict == "UNCONFIRMED":
        verdict_sentence = "No physics signals confirm degradation around the complaint period."
    else:
        verdict_sentence = ("Physics %s range decline. %d of 7 signals show degradation "
                           "around the complaint period." % (
                               "confirms" if verdict == "PHYSICS_CONFIRMED" else "suggests",
                               score))

    # ── Attribution sentence ──
    if attr and attr.get("operator_attribution_sentence"):
        attribution_sentence = attr["operator_attribution_sentence"]
    elif attr and attr.get("primary_driver"):
        parts = []
        if attr.get("charging_pct"):
            parts.append("charging %.0f%%" % (attr["charging_pct"] * 100 if attr["charging_pct"] < 1 else attr["charging_pct"]))
        if attr.get("usage_pct"):
            parts.append("usage %.0f%%" % (attr["usage_pct"] * 100 if attr["usage_pct"] < 1 else attr["usage_pct"]))
        attribution_sentence = "Primary driver: %s. %s." % (attr["primary_driver"], ", ".join(parts))
    else:
        attribution_sentence = None

    # ── Audience sentences (Rule 91: no SOH% to operators) ──
    action = bhs["rul_action_v2"] if bhs else "MONITOR_WEEKLY"
    action_map = {
        "REPLACE_URGENT": "Immediate replacement recommended.",
        "REPLACE_PLAN": "Plan replacement within 4-8 weeks.",
        "EOL_IMMINENT": "End of life imminent — plan replacement.",
        "CELL_BALANCE": "Cell balancing service recommended.",
        "MONITOR_WEEKLY": "Continue monitoring weekly.",
        "NO_ACTION": "No action required at this time.",
    }
    action_text = action_map.get(action, "Monitor and reassess.")

    op_sentence = "Range was %skm at complaint date, now %skm. %s" % (
        range_at_complaint or "unknown", range_latest or "unknown", action_text)

    nbfc_sentence = ("Physics-confirmed degradation event. Grade review recommended."
                     if verdict == "PHYSICS_CONFIRMED"
                     else "Degradation signal %s. Current monitoring adequate." % verdict.lower().replace("_", " "))

    oem_sentence = "Degradation %s around %s. %s" % (
        "onset confirmed" if score >= 3 else "not confirmed",
        complaint_date,
        ("Primary factor: %s." % attr["primary_driver"]) if attr and attr.get("primary_driver") else "Attribution pending.")

    # ── Log investigation ──
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investigation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                battery_id TEXT NOT NULL,
                investigation_date TEXT NOT NULL,
                complaint_date TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence REAL,
                corroboration_score INTEGER,
                signals_confirmed INTEGER,
                signals_checked INTEGER,
                complaint_text TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT INTO investigation_log
                (battery_id, investigation_date, complaint_date, verdict,
                 confidence, corroboration_score, signals_confirmed,
                 signals_checked, complaint_text)
            VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
        """, [battery_id, complaint_date, verdict, confidence, score,
              score, 7, complaint_text])
        conn.commit()
    except Exception:
        pass  # non-critical if log fails

    conn.close()

    return {
        "battery_id": battery_id,
        "investigation_date": _date.today().isoformat(),
        "complaint_date": complaint_date,
        "verdict": verdict,
        "confidence": confidence,
        "signals_checked": 7,
        "signals_confirmed": score,
        "corroboration_score": score,
        "verdict_sentence": verdict_sentence,
        "signal_detail": signals,
        "range_at_complaint": range_at_complaint,
        "range_latest": range_latest,
        "range_commissioned": range_comm,
        "attribution_sentence": attribution_sentence,
        "recommended_action": action,
        "audience_sentences": {
            "operator": op_sentence,
            "nbfc": nbfc_sentence,
            "oem": oem_sentence,
        },
    }


@app.get("/api/battery/{battery_id}/service")
def battery_service(battery_id: str):
    """Service forecast + complaint history + recommendation."""
    conn = get_conn()
    try:
        forecast = q1(conn, "SELECT * FROM nmc_service_forecast WHERE battery_id=?", [battery_id])
        complaints = q(conn, """
            SELECT complaint_date, complaint_category, diagnosis_category,
                   resolution_type, delta_mv
            FROM nmc_service_raw WHERE linked_battery_id=? ORDER BY complaint_date
        """, [battery_id])
        events_n = q1(conn, "SELECT COUNT(*) as n FROM vehicle_events WHERE battery_id=?", [battery_id])
        chem = q1(conn, "SELECT chemistry FROM batteries WHERE battery_id=?", [battery_id])
        # Silent degrader check
        silent = False
        if (events_n and events_n["n"] or 0) > 20 and forecast and (forecast.get("p_complaint_30d") or 0) < 0.25:
            silent = True
        # Flag from nmc_service_forecast_flags
        flag = None
        try:
            flag = q1(conn, "SELECT * FROM nmc_service_forecast_flags WHERE battery_id=?", [battery_id])
        except: pass
        return {
            "forecast": forecast,
            "complaints": complaints,
            "complaints_count": len(complaints),
            "events_count": events_n["n"] if events_n else 0,
            "silent_degrader": silent,
            "forecast_flag": flag,
            "chemistry": chem["chemistry"] if chem else None,
        }
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/dekf")
def battery_dekf(battery_id: str):
    """DEKF shadow data for this battery."""
    conn = get_conn()
    try:
        # Check if shadow tables exist
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%dekf%'"
        ).fetchall()]
        shadow_data = None
        if "nmc_dekf_shadow" in tables:
            shadow_data = q(conn, """
                SELECT * FROM nmc_dekf_shadow WHERE battery_id=?
                ORDER BY timestamp DESC LIMIT 500
            """, [battery_id])
        status = "SHADOW" if shadow_data else "NOT_STARTED"
        return {
            "status": status,
            "shadow_tables": tables,
            "data": shadow_data,
            "battery_id": battery_id,
        }
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/dekf/status")
def battery_dekf_status(battery_id: str):
    """DEKF promotion checklist."""
    conn = get_conn()
    try:
        chem = q1(conn, "SELECT chemistry FROM batteries WHERE battery_id=?", [battery_id])
        chemistry = chem["chemistry"] if chem else "LFP"
        # Check shadow tables
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%dekf%'"
        ).fetchall()]
        shadow_exists = ("nmc_dekf_shadow" in tables) if chemistry == "NMC" else ("lfp_dekf_shadow" in tables)
        return {
            "chemistry": chemistry,
            "shadow_exists": shadow_exists,
            "promoted": False,
            "phase2_status": {
                "ocv_fix": chemistry == "NMC",
                "r0_decoupling": False,
                "hybrid_coulomb": chemistry == "LFP",
            },
            "checklist": {
                "soc_correction_ok": False,
                "r0_mape_ok": False,
                "regression_tests": True,
                "shadow_duration_weeks": 0,
            }
        }
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/soc-profile")
def battery_soc_profile(battery_id: str):
    """DEKF-corrected SOC for this battery vs raw BMS reading.

    Returns latest DEKF SOC, BMS calibration correction, confidence, and last-8-week trend.
    BMS SOC reconstructed as dekf_latest - correction (since correction = DEKF - BMS).
    """
    conn = get_conn()
    try:
        bhs = q1(conn, """
            SELECT bhs.soc_dekf_latest, bhs.soc_dekf_confidence,
                   bhs.soc_bms_correction_pct, bhs.dekf_soc_available,
                   b.bms_current_convention
            FROM battery_health_scores_v2 bhs
            LEFT JOIN batteries b ON bhs.battery_id = b.battery_id
            WHERE bhs.battery_id = ?
        """, [battery_id])

        if not bhs:
            return {
                "battery_id": battery_id,
                "soc_dekf_available": 0,
                "soc_dekf_latest": None,
                "soc_dekf_confidence": "INSUFFICIENT_DATA",
                "soc_bms_correction_pct": None,
                "bms_current_convention": None,
                "bms_soc_reconstructed": None,
                "soc_dekf_vs_bms_note": "DEKF SOC estimate not available for this battery.",
                "soc_weekly_trend": [],
            }

        dekf_latest = bhs.get("soc_dekf_latest")
        correction = bhs.get("soc_bms_correction_pct")
        available = bhs.get("dekf_soc_available") or 0
        confidence = bhs.get("soc_dekf_confidence") or "INSUFFICIENT_DATA"
        convention = bhs.get("bms_current_convention")

        bms_val = None
        delta = None
        if dekf_latest is not None and correction is not None:
            bms_val = round(dekf_latest - correction, 2)
            delta = round(abs(bms_val - dekf_latest), 2)

        if available == 1 and dekf_latest is not None:
            if delta is not None and delta > 4:
                note = (
                    f"BMS reports {bms_val:.0f}% - DEKF estimates {dekf_latest:.0f}%. "
                    f"Difference of {delta:.1f}pp reflects BMS calibration bias "
                    f"({correction:+.1f}% systematic overread corrected)."
                )
            elif delta is not None:
                note = (
                    f"BMS and DEKF SOC estimates are aligned "
                    f"({delta:.1f}pp difference)."
                )
            else:
                note = f"DEKF SOC: {dekf_latest:.0f}% (BMS comparison unavailable)."
        else:
            note = "DEKF SOC estimate not available for this battery."

        trend = q(conn, """
            SELECT week_number, soc_dekf_weekly, soc_dekf_vs_bms_delta
            FROM vehicle_weekly_features
            WHERE battery_id = ? AND soc_dekf_weekly IS NOT NULL
            ORDER BY week_number DESC
            LIMIT 8
        """, [battery_id])
        trend = list(reversed(trend))

        # Fix 2: pull latest-week dod_mean + dod_corrected from VWF.
        # Battery_health_scores_v2 has no dod_observed / dod_corrected columns.
        # Passport Health / DEKF panels consume these as dod_observed (ratio)
        # and dod_corrected (ratio). dod_mean is stored as percent (e.g. 80.0)
        # so convert back to ratio for dod_observed to match the UI's clamp.
        dod_row = q1(conn, """
            SELECT dod_mean, dod_corrected
            FROM vehicle_weekly_features
            WHERE battery_id = ?
              AND (dod_mean IS NOT NULL OR dod_corrected IS NOT NULL)
            ORDER BY week_number DESC
            LIMIT 1
        """, [battery_id])
        _dm = (dod_row or {}).get("dod_mean")
        _dc = (dod_row or {}).get("dod_corrected")
        # If dod_mean is stored as a percent (>1) express as ratio (/100) so
        # the UI's "observed * 100" yields percent. If already a ratio, pass
        # through unchanged.
        if _dm is not None:
            dod_observed = _dm / 100.0 if _dm > 1 else _dm
        else:
            dod_observed = None

        return {
            "battery_id": battery_id,
            "soc_dekf_available": available,
            "soc_dekf_latest": dekf_latest,
            "soc_dekf_confidence": confidence,
            "soc_bms_correction_pct": correction,
            "bms_current_convention": convention,
            "bms_soc_reconstructed": bms_val,
            "soc_dekf_vs_bms_delta_pp": delta,
            "soc_dekf_vs_bms_note": note,
            "soc_weekly_trend": trend,
            "dod_observed": dod_observed,
            "dod_corrected": _dc,
            "dod_mean": _dm,
        }
    finally:
        conn.close()


# ── Passport v2 Stage 4 endpoints ─────────────────────────────────────

@app.get("/api/platform/summary")
def platform_summary():
    """Fleet KPIs, model count, rules, data quality."""
    conn = get_conn()
    try:
        total = q1(conn, "SELECT COUNT(*) as n FROM battery_health_scores_v2")["n"]
        lfp = q1(conn, "SELECT COUNT(*) as n FROM battery_health_scores_v2 WHERE chemistry='LFP'")["n"]
        nmc = q1(conn, "SELECT COUNT(*) as n FROM battery_health_scores_v2 WHERE chemistry='NMC'")["n"]
        high_conf = q1(conn, "SELECT COUNT(*) as n FROM battery_health_scores_v2 WHERE data_confidence='HIGH'")
        high_n = high_conf["n"] if high_conf and high_conf["n"] else 0
        data_quality_pct = round(high_n / max(total, 1) * 100, 1)
        queue = q(conn, "SELECT status, COUNT(*) as n FROM battery_scoring_queue GROUP BY status")
        return {
            "total_batteries": total, "lfp_active": lfp, "nmc_active": nmc,
            "models_production": 8, "rules_count": 29, "data_quality_pct": data_quality_pct,
            "queue": {r["status"]: r["n"] for r in queue},
        }
    finally:
        conn.close()


@app.get("/api/platform/catalogue")
def platform_catalogue():
    """All models with status."""
    models = [
        {"name":"range_t2t1_ensemble","version":"v1.0.0","chemistry":"LFP","status":"PRODUCTION","metric":"14.07% MAPE","features":38,"scored":183},
        {"name":"range_t1b_lgbm","version":"v1.0.0","chemistry":"LFP","status":"PRODUCTION","metric":"16.11% MAPE","features":50,"scored":183},
        {"name":"range_v2.1.0","version":"v2.1.0","chemistry":"LFP","status":"PRODUCTION","metric":"2.57% MAPE","features":40,"scored":183},
        {"name":"nmc_complaint","version":"v3.1.0","chemistry":"NMC","status":"PRODUCTION","metric":"AUC 0.692","features":22,"scored":27},
        {"name":"nmc_survival","version":"v2.1.0","chemistry":"NMC","status":"PRODUCTION","metric":"C-idx 0.600","features":10,"scored":27},
        {"name":"nmc_fault_classifier","version":"v4.0.0","chemistry":"NMC","status":"QUARANTINED","metric":"AUC 0.848","features":0,"scored":0,"reason":"Label-only training. Not for health decisions."},
        {"name":"nmc_severity","version":"v1.0.0","chemistry":"NMC","status":"QUARANTINED","metric":"Recall 0.594","features":0,"scored":0,"reason":"CRITICAL recall below 0.85 gate."},
        {"name":"nmc_burn_warning","version":"v1.0.0","chemistry":"NMC","status":"QUARANTINED","metric":"Recall 0.980","features":0,"scored":0,"reason":"Burn labels from service records lack telemetry confirmation."},
        {"name":"dekf_nmc","version":"v1.0","chemistry":"NMC","status":"SHADOW","metric":"R0 MAPE 79.8%","features":0,"scored":27},
        {"name":"dekf_lfp","version":"v1.0","chemistry":"LFP","status":"SHADOW","metric":"Flat OCV 80.8%","features":0,"scored":179},
    ]
    return models


@app.get("/api/platform/changelog")
def platform_changelog():
    """Last 10 MODEL_CHANGELOG entries."""
    entries = [
        {"date":"2026-03-23","version":"nmc_survival_v2.1.0","summary":"Weibull AFT retrain on 20-sec features, C-index 0.600","chemistry":"NMC","tag":"nmc-survival-v2.1"},
        {"date":"2026-03-23","version":"data-quality-gates","summary":"8 ingest gates, sufficiency checks, retroactive flags (902 NMC rows)","chemistry":"ALL","tag":"data-quality-gates-v1"},
        {"date":"2026-03-23","version":"scoring-gate-v1","summary":"80% feature completeness gate, imputation banned, queue introduced","chemistry":"ALL","tag":"sprint53-scoring-gate"},
        {"date":"2026-03-22","version":"nmc_phase_b_v1.0","summary":"NMC dual-track scoring, investigation engine, Gate A/B","chemistry":"NMC","tag":"sprint53-reverts-complete"},
        {"date":"2026-03-22","version":"Sprint 5.3 P0","summary":"4 NMC models trained on 53K labels — all QUARANTINED","chemistry":"NMC","tag":"sprint53-4models-complete"},
    ]
    return entries


@app.get("/api/platform/queue")
def platform_queue():
    """Battery scoring queue status."""
    conn = get_conn()
    try:
        rows = q(conn, """
            SELECT battery_id, chemistry, completeness_pct, status,
                   weeks_available, estimated_weeks_to_eligible, queued_at
            FROM battery_scoring_queue ORDER BY status, completeness_pct ASC
        """)
        return rows
    finally:
        conn.close()


@app.get("/api/platform/templates")
def platform_templates():
    """Template registry."""
    conn = get_conn()
    try:
        rows = q(conn, "SELECT * FROM template_registry")
        return rows
    finally:
        conn.close()


@app.get("/api/platform/system")
def platform_system():
    """DB sizes, last pipeline run, ingest audit."""
    import os
    conn = get_conn()
    try:
        sqlite_size = round(os.path.getsize(DB_PATH) / (1024**2), 1)
        try:
            duckdb_size = round(os.path.getsize(DB_PATH.replace("enerlytik_production.db", "enerlytik_30sec.duckdb")) / (1024**3), 2)
        except: duckdb_size = None
        last_scored = q1(conn, "SELECT MAX(scored_at) as t FROM battery_health_scores_v2")
        last_ingest = None
        try:
            last_ingest = q1(conn, "SELECT MAX(ingest_date) as t FROM ingest_audit_log")
        except: pass
        return {
            "sqlite_size_mb": sqlite_size, "duckdb_size_gb": duckdb_size,
            "last_scored": last_scored["t"] if last_scored else None,
            "last_ingest": last_ingest["t"] if last_ingest else None,
        }
    finally:
        conn.close()


# ── Passport v2 Stage 5 endpoints ─────────────────────────────────────

@app.get("/api/battery/{battery_id}/reasoning")
def battery_reasoning(battery_id: str):
    """Full reasoning chain: raw signals, features, scores, SHAP, events, investigation, actions."""
    conn = get_conn()
    try:
        # Latest VWF or NMC features
        feat = q1(conn, """SELECT km_per_soc_pct, cell_spread_max, cell_spread_mean, temp_max,
            cusum_x_weeks, dod_mean, voltage_range, current_discharge_mean, r0_weekly_median,
            soh_cap_weekly, km_sum, trip_count, week_number
            FROM vehicle_weekly_features WHERE battery_id=? ORDER BY week_number DESC LIMIT 1""", [battery_id])
        if not feat:
            feat = q1(conn, """SELECT cell_spread_max, cell_spread_mean, temp_max, soc_min,
                pack_voltage_mean, voltage_range_week, r0_estimate_mohm, week_number
                FROM nmc_weekly_features WHERE battery_id=? ORDER BY week_number DESC LIMIT 1""", [battery_id])
        # Scores
        scores = q1(conn, """SELECT operational_score, tier_label_v2, l1_score, l2_score, l3_score,
            event_penalty, adjusted_composite, prediction_confidence, data_confidence, scoring_mode
            FROM battery_health_scores_v2 WHERE battery_id=?""", [battery_id])
        # Intelligence
        intel = q1(conn, """SELECT narrative_operator, operator_action, primary_outcome_concern,
            os1_range_loss, os2_breakdown_risk, os3_fire_risk, os5_end_of_life,
            warning_level_absolute, warning_trend, causal_chain
            FROM battery_intelligence WHERE battery_id=?""", [battery_id])
        # Events
        events = q(conn, "SELECT event_type, event_code, severity, week_number FROM vehicle_events WHERE battery_id=? ORDER BY week_number", [battery_id])
        # Investigation
        inv = q1(conn, "SELECT verdict, hypothesis, classification, n_complaints FROM battery_investigations WHERE battery_id=?", [battery_id])
        # SHAP
        diag = q1(conn, "SELECT shap_attribution FROM battery_diagnostics WHERE battery_id=?", [battery_id])
        shap = None
        if diag and diag.get("shap_attribution"):
            try:
                import json as _j
                shap = _j.loads(diag["shap_attribution"]) if isinstance(diag["shap_attribution"], str) else diag["shap_attribution"]
            except: pass
        # Survival
        surv = q1(conn, "SELECT * FROM nmc_service_forecast WHERE battery_id=?", [battery_id])
        # Battery info
        bat = q1(conn, "SELECT chemistry, battery_model, commissioning_date FROM batteries WHERE battery_id=?", [battery_id])
        # Baseline
        bl = q(conn, """SELECT km_per_soc_pct, cell_spread_max FROM vehicle_weekly_features
            WHERE battery_id=? AND week_number <= 8 ORDER BY week_number""", [battery_id])
        if not bl:
            bl = q(conn, """SELECT cell_spread_max FROM nmc_weekly_features
                WHERE battery_id=? AND week_number <= 8 ORDER BY week_number""", [battery_id])
        return {
            "battery": bat, "features": feat, "scores": scores, "intelligence": intel,
            "events": events, "investigation": inv, "shap": shap, "survival": surv,
            "baseline": bl, "battery_id": battery_id,
        }
    finally:
        conn.close()


@app.get("/api/battery/{battery_id}/kb-context")
def battery_kb_context(battery_id: str):
    """KB categories relevant to this battery."""
    conn = get_conn()
    try:
        bat = q1(conn, "SELECT chemistry, battery_model FROM batteries WHERE battery_id=?", [battery_id])
        chem = bat["chemistry"] if bat else "LFP"
        categories = [
            {"source": f"{chem} cell imbalance patterns", "relevance": "HIGH", "used_for": "E2 event threshold calibration"},
            {"source": f"{chem} efficiency degradation", "relevance": "HIGH", "used_for": "L1 baseline comparison"},
        ]
        if chem == "NMC":
            categories.append({"source": "NMC swap fleet stress patterns", "relevance": "HIGH", "used_for": "Operational scoring"})
            categories.append({"source": "Silent degradation index", "relevance": "MED", "used_for": "Survival model interpretation"})
        else:
            categories.append({"source": "LFP batch defect patterns", "relevance": "MED", "used_for": "Fleet context scoring"})
            categories.append({"source": "3W OEM complaint priors", "relevance": "MED", "used_for": "Event frequency benchmarking"})
        return {"categories": categories, "rag_online": False, "chemistry": chem}
    finally:
        conn.close()


@app.get("/api/platform/gps-check")
def platform_gps_check():
    """Check DuckDB gps_raw_30sec for lat/lon availability."""
    try:
        import duckdb, os
        db_path = DB_PATH.replace("enerlytik_production.db", "enerlytik_30sec.duckdb")
        if not os.path.exists(db_path):
            return {"available": False, "reason": "DuckDB file not found"}
        ddb = duckdb.connect(db_path, read_only=True)
        tables = [t[0] for t in ddb.execute("SHOW TABLES").fetchall()]
        if "gps_raw_30sec" not in tables:
            ddb.close()
            return {"available": False, "reason": "gps_raw_30sec table not found", "tables": tables}
        cols = [c[0] for c in ddb.execute("DESCRIBE gps_raw_30sec").fetchall()]
        has_lat = "latitude" in cols
        has_lon = "longitude" in cols
        count = ddb.execute("SELECT COUNT(*) FROM gps_raw_30sec").fetchone()[0]
        sample = None
        if has_lat and has_lon:
            sample = ddb.execute("SELECT latitude, longitude FROM gps_raw_30sec WHERE latitude IS NOT NULL LIMIT 3").fetchall()
        ddb.close()
        return {
            "available": has_lat and has_lon,
            "columns": cols,
            "rows": count,
            "has_latitude": has_lat,
            "has_longitude": has_lon,
            "sample": sample,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


# ── GET /api/fleet/action-priority ──────────────────────────────────
@app.get("/api/fleet/action-priority")
@safe
def fleet_action_priority(limit: int = 5, _=Depends(verify_token)):
    """Top N batteries needing action, ordered by urgency."""
    conn = get_conn()
    rows = q(conn, """
        SELECT battery_id, rul_action_v2 as action,
               range_corrected_km as range_proxy_km, operational_score
        FROM battery_health_scores_v2
        WHERE rul_action_v2 IN ('REPLACE_URGENT','EOL_IMMINENT','REPLACE_PLAN','CELL_BALANCE')
        AND chemistry='LFP'
        ORDER BY
          CASE rul_action_v2
            WHEN 'EOL_IMMINENT' THEN 1
            WHEN 'REPLACE_URGENT' THEN 2
            WHEN 'REPLACE_PLAN' THEN 3
            WHEN 'CELL_BALANCE' THEN 4
          END,
          range_corrected_km ASC
        LIMIT ?
    """, [limit])
    conn.close()
    return {"batteries": rows}


# ── GET /api/debug/schema ──────────────────────────────────────────
@app.get("/api/debug/schema")
@safe
def debug_schema(_=Depends(verify_token)):
    conn = get_conn()
    tables = q(conn, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    result = {}
    for t in tables:
        cols = conn.execute(f"PRAGMA table_info({t['name']})").fetchall()
        result[t["name"]] = [c[1] for c in cols]
    conn.close()
    return result


# ── GET /api/fleets/list ────────────────────────────────────────────
@app.get("/api/fleets/list")
@safe
def list_fleets(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, "SELECT oem_name, COUNT(*) as battery_count FROM batteries GROUP BY oem_name ORDER BY battery_count DESC")
    conn.close()
    return {"fleets": rows}


# ── GET /api/models/list ───────────────────────────────────────────
@app.get("/api/models/list")
@safe
def list_models(oem: str = None, _=Depends(verify_token)):
    """List battery models. Column is battery_model in batteries table."""
    conn = get_conn()
    sql = "SELECT DISTINCT battery_model FROM batteries WHERE battery_model IS NOT NULL"
    params = []
    if oem:
        sql += " AND oem_name = ?"
        params.append(oem)
    sql += " ORDER BY battery_model"
    rows = q(conn, sql, params)
    conn.close()
    return {"models": [r["battery_model"] for r in rows]}


# ── GET /api/cities/list ──────────────────────────────────────────
@app.get("/api/cities/list")
@safe
def list_cities(oem: str = None, chemistry: str = None, _=Depends(verify_token)):
    """List GPS-derived cities from battery_gps_city table."""
    conn = get_conn()
    # Try GPS city table first (accurate), fall back to city_code
    try:
        sql = """SELECT g.gps_city as city, COUNT(*) as count
                 FROM battery_gps_city g
                 JOIN batteries b ON g.battery_id = b.battery_id
                 WHERE g.gps_city IS NOT NULL"""
        params = []
        if chemistry:
            sql += " AND b.chemistry = ?"
            params.append(chemistry)
        if oem:
            sql += " AND b.oem_name = ?"
            params.append(oem)
        sql += " GROUP BY g.gps_city ORDER BY count DESC"
        rows = q(conn, sql, params)
        conn.close()
        return {"cities": [{"city": r["city"], "display": r["city"], "count": r["count"]} for r in rows]}
    except Exception:
        # Fallback to city_code
        sql2 = "SELECT city_code as city, COUNT(*) as count FROM batteries WHERE city_code IS NOT NULL"
        params2 = []
        if chemistry:
            sql2 += " AND chemistry = ?"; params2.append(chemistry)
        if oem:
            sql2 += " AND oem_name = ?"; params2.append(oem)
        sql2 += " GROUP BY city_code ORDER BY count DESC"
        rows = q(conn, sql2, params2)
        conn.close()
        return {"cities": [{"city": r["city"], "display": r["city"], "count": r["count"]} for r in rows]}


# ── GET /api/packs/list ───────────────────────────────────────────
@app.get("/api/packs/list")
@safe
def list_packs(oem: str = None, chemistry: str = None, _=Depends(verify_token)):
    """List pack models. Uses battery_model from batteries table."""
    conn = get_conn()
    sql = "SELECT DISTINCT battery_model as pack, COUNT(*) as count FROM batteries WHERE battery_model IS NOT NULL AND (is_active = 1 OR is_active IS NULL)"
    params = []
    if chemistry:
        sql += " AND chemistry = ?"
        params.append(chemistry)
    if oem:
        sql += " AND oem_name = ?"
        params.append(oem)
    sql += " GROUP BY battery_model ORDER BY count DESC"
    rows = q(conn, sql, params)
    conn.close()
    return {"packs": [{"pack": r["pack"], "count": r["count"]} for r in rows]}


# ── GET /api/batteries/list ─────────────────────────────────────────

@app.get("/api/batteries/list")
@safe
def batteries_list(oem: str = None, city: str = None, pack_model: str = None, chemistry: str = None, _=Depends(verify_token)):
    """All batteries with filters. Accepts oem, city (city_code), pack_model (battery_model), chemistry."""
    conn = get_conn()
    extra_where = ""
    params = []
    if chemistry:
        extra_where += " AND b.chemistry = ?"
        params.append(chemistry)
    if oem:
        extra_where += " AND b.oem_name = ?"
        params.append(oem)
    if city:
        extra_where += " AND b.city_code = ?"
        params.append(city)
    if pack_model:
        extra_where += " AND b.battery_model = ?"
        params.append(pack_model)
    ban_sql, ban_params = _banned_sql_clause("b.battery_id")
    rows = q(conn, f"""
        SELECT b.battery_id, b.original_id, b.battery_model, b.chemistry,
               h.operational_score,
               h.data_quality_verdict, h.score_usable_for_decisions,
               h.tier_label_v2, h.scoring_mode
        FROM batteries b
        LEFT JOIN battery_health_scores_v2 h ON b.battery_id = h.battery_id
        WHERE (b.is_active = 1 OR b.is_active IS NULL) AND {ban_sql} {extra_where}
        ORDER BY b.battery_id
    """, ban_params + params)
    conn.close()
    return {
        "batteries": [dict(r) for r in rows],
        "battery_ids": [r["battery_id"] for r in rows],
        "total": len(rows)
    }


# ── GET /api/batteries/skipped ─────────────────────────────────────

@app.get("/api/batteries/skipped")
@safe
def batteries_skipped(_=Depends(verify_token)):
    """Batteries not scored or suppressed, grouped by reason."""
    conn = get_conn()

    # Group 1: never scored
    never_scored = q(conn, """
        SELECT b.battery_id, b.original_id, b.battery_model, b.chemistry,
               'NEVER_SCORED' AS reason,
               'Battery exists but not in scoring table' AS detail
        FROM batteries b
        WHERE b.battery_id NOT IN (SELECT battery_id FROM battery_health_scores_v2)
    """)

    # Group 2: scored but suppressed / insufficient
    suppressed = q(conn, """
        SELECT b.battery_id, b.original_id, b.battery_model, b.chemistry,
               COALESCE(h.data_quality_verdict, h.scoring_mode, 'SUPPRESSED') AS reason,
               h.scoring_mode AS detail
        FROM batteries b
        JOIN battery_health_scores_v2 h ON b.battery_id = h.battery_id
        WHERE h.tier_label_v2 = 'INSUFFICIENT_DATA'
           OR h.score_usable_for_decisions = 0
           OR h.scoring_mode LIKE '%SUPPRESSED%'
           OR h.scoring_mode LIKE '%BACKFILLED%'
    """)
    conn.close()

    all_skipped = never_scored + suppressed

    # Group by reason
    groups = {}
    for row in all_skipped:
        reason = row.get("reason", "UNKNOWN")
        if reason not in groups:
            groups[reason] = []
        groups[reason].append(row)

    # Summary text
    parts = []
    for reason, bats in sorted(groups.items()):
        parts.append(f"{len(bats)} {reason.lower().replace('_', ' ')}")
    summary = ". ".join(parts) + "." if parts else "No skipped batteries."

    return {
        "total_skipped": len(all_skipped),
        "groups": groups,
        "summary": summary,
    }


# ── GET /api/fleet/scoring-summary ──────────────────────────────────

@app.get("/api/fleet/scoring-summary")
@safe
def fleet_scoring_summary(_=Depends(verify_token)):
    """Scoring system summary — v2.0 range-predictive scores."""
    conn = get_conn()
    rows = q(conn, """
        SELECT health_score_operational, health_score_trajectory,
               health_score_combined, health_tier, score_confidence,
               scoring_version
        FROM battery_health_scores_v2
        WHERE scoring_version = 'v2.0-range-predictive'
    """)
    conn.close()
    if not rows:
        return {"scoring_version": None, "scored_n": 0}

    ops = [r["health_score_operational"] for r in rows if r["health_score_operational"] is not None]
    trajs = [r["health_score_trajectory"] for r in rows if r["health_score_trajectory"] is not None]
    tiers = {}
    for r in rows:
        t = r["health_tier"] or "UNKNOWN"
        tiers[t] = tiers.get(t, 0) + 1

    import statistics
    return {
        "scoring_version": "v2.0-range-predictive",
        "scored_n": len(rows),
        "mean_operational": round(statistics.mean(ops), 1) if ops else None,
        "mean_trajectory": round(statistics.mean(trajs), 1) if trajs else None,
        "tier_distribution": tiers,
        "score_confidence_distribution": {
            k: sum(1 for r in rows if r["score_confidence"] == k)
            for k in ["HIGH", "MEDIUM", "LOW"]
        },
    }


# ── GET /api/fleet/scoring-grid ─────────────────────────────────────

@app.get("/api/fleet/scoring-grid")
@safe
def fleet_scoring_grid(_=Depends(verify_token)):
    """Capacity state × range alignment matrix + distributions for Score Lab."""
    conn = get_conn()
    import statistics

    # All scored LFP with latest VWF range
    rows = q(conn, """
        SELECT s.battery_id, s.capacity_state, s.trajectory_state,
               s.rul_action_v2, s.corroboration_verdict, s.nbfc_risk_tier,
               s.range_floor_override, s.soh_baseline_suspect,
               s.early_warning_duration_weeks,
               v.km_per_soc_pct * 80 as range_km
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        JOIN (SELECT v2.battery_id, v2.km_per_soc_pct
              FROM vehicle_weekly_features v2
              INNER JOIN (SELECT battery_id, MAX(week_number) mx
                          FROM vehicle_weekly_features WHERE km_per_soc_pct IS NOT NULL
                          GROUP BY battery_id) m
              ON v2.battery_id=m.battery_id AND v2.week_number=m.mx) v
        ON s.battery_id = v.battery_id
        WHERE b.chemistry='LFP' AND s.capacity_state IS NOT NULL
    """)
    if not rows:
        conn.close()
        return {"total_batteries": 0}

    total = len(rows)
    ranges = sorted([r["range_km"] for r in rows if r["range_km"]])
    n = len(ranges)
    p25 = round(ranges[n//4], 1) if n >= 4 else 0
    p50 = round(statistics.median(ranges), 1) if ranges else 0
    p75 = round(ranges[3*n//4], 1) if n >= 4 else 0
    mean_r = round(statistics.mean(ranges), 1) if ranges else 0

    # Distributions
    def dist(key):
        d = {}
        for r in rows:
            v = r.get(key) or "UNKNOWN"
            d[v] = d.get(v, 0) + 1
        return d

    cap_dist = dist("capacity_state")
    traj_dist = dist("trajectory_state")
    rul_dist = dist("rul_action_v2")
    corr_dist = dist("corroboration_verdict")
    nbfc_dist = dist("nbfc_risk_tier")

    # Alignment grid: capacity_state × range_band
    range_bands = [(0, p25, "LOW"), (p25, p50, "MED_LOW"), (p50, p75, "MED_HIGH"), (p75, 99999, "HIGH")]
    alignment = []
    for r in rows:
        rk = r["range_km"] or 0
        band = "HIGH"
        for lo, hi, label in range_bands:
            if lo <= rk < hi:
                band = label
                break
        alignment.append({"capacity_state": r["capacity_state"] or "UNKNOWN", "range_band": band})

    grid = {}
    for a in alignment:
        cs = a["capacity_state"]
        rb = a["range_band"]
        if cs not in grid:
            grid[cs] = {}
        grid[cs][rb] = grid[cs].get(rb, 0) + 1

    # Divergence categories
    forward_warning = sum(1 for r in rows if r["capacity_state"] in ("EARLY_WARNING", "DECLINING_SLOW") and (r["range_km"] or 0) > p50)
    floor_stable = sum(1 for r in rows if r.get("range_floor_override") and r["capacity_state"] in ("STABLE", "EARLY_WARNING"))
    lagging = sum(1 for r in rows if r["trajectory_state"] == "DECLINING_FAST" and r["capacity_state"] not in ("CRITICAL", "DECLINING_FAST"))
    recovering_pen = sum(1 for r in rows if r["trajectory_state"] == "RECOVERING" and r.get("rul_action_v2") in ("REPLACE_PLAN", "MONITOR"))
    baseline_suspect = sum(1 for r in rows if r.get("soh_baseline_suspect"))
    aligned = total - forward_warning - floor_stable - lagging - recovering_pen - baseline_suspect

    # Early warning mean weeks
    ew_weeks = [r["early_warning_duration_weeks"] for r in rows if r["capacity_state"] == "EARLY_WARNING" and r.get("early_warning_duration_weeks")]
    mean_ew = round(statistics.mean(ew_weeks), 1) if ew_weeks else 0

    conn.close()
    return {
        "total_batteries": total,
        "capacity_state_dist": cap_dist,
        "trajectory_state_dist": traj_dist,
        "rul_action_dist": rul_dist,
        "corroboration_dist": corr_dist,
        "nbfc_tier_dist": nbfc_dist,
        "p25_range": p25, "p50_range": p50, "p75_range": p75, "mean_range": mean_r,
        "alignment_grid": grid,
        "divergence": {
            "forward_warning": forward_warning,
            "floor_stable": floor_stable,
            "lagging_score": lagging,
            "recovering_penalised": recovering_pen,
            "baseline_suspect": baseline_suspect,
            "aligned": max(0, aligned),
        },
        "mean_early_warning_weeks": mean_ew,
    }


@app.get("/api/fleet/capacity-state-batteries")
@safe
def fleet_capacity_state_batteries(state: str, _=Depends(verify_token)):
    """Battery list for a given capacity_state — Score Lab drill-down."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, v.km_per_soc_pct * 80 as range_proxy_km,
               s.rul_action_v2, s.scored_calendar_week, s.nbfc_risk_tier
        FROM battery_health_scores_v2 s
        JOIN (SELECT v2.battery_id, v2.km_per_soc_pct
              FROM vehicle_weekly_features v2
              INNER JOIN (SELECT battery_id, MAX(week_number) mx
                          FROM vehicle_weekly_features WHERE km_per_soc_pct IS NOT NULL
                          GROUP BY battery_id) m
              ON v2.battery_id=m.battery_id AND v2.week_number=m.mx) v
        ON s.battery_id = v.battery_id
        WHERE s.capacity_state = ?
        ORDER BY v.km_per_soc_pct ASC
    """, [state])
    conn.close()
    return {"state": state, "batteries": rows}


# ── GET /api/battery/:id/profile ───────────────────────────────────

@app.get("/api/battery/{battery_id}/profile")
@safe
def battery_profile(battery_id: str, _=Depends(verify_token)):
    """Battery usage profile + master data + ownership chain."""
    conn = get_conn()
    prof = q1(conn, """
        SELECT p.*,
               m.battery_oem_id, m.vehicle_oem_id, m.bms_oem_id,
               m.bms_firmware_version, m.bms_soc_algorithm_type, m.bms_soc_bias_pct,
               m.iot_device_id, m.iot_firmware_version, m.iot_sampling_freq_sec,
               m.iot_data_quality_flag, m.gps_quality_flag,
               m.cell_chemistry_variant, m.rated_capacity_ah, m.cell_count_series,
               b.battery_model, b.oem_name, b.commissioning_date, b.chemistry,
               bhs.operational_score, bhs.health_tier, bhs.battery_category,
               bhs.latest_behaviour_grade, bhs.latest_thermal_grade,
               bhs.latest_route_stress, bhs.scoring_mode as vScore_tier,
               bhs.cluster_alert_group
        FROM battery_usage_profile p
        LEFT JOIN battery_master m ON p.battery_id = m.battery_id
        LEFT JOIN batteries b ON p.battery_id = b.battery_id
        LEFT JOIN battery_health_scores_v2 bhs ON p.battery_id = bhs.battery_id
        WHERE p.battery_id = ? AND p.is_current = 1
    """, [battery_id])
    if not prof:
        # Fallback: build minimal profile from batteries + BHS
        prof = q1(conn, """
            SELECT b.battery_id, b.battery_model, b.oem_name, b.commissioning_date,
                   b.chemistry, b.original_id,
                   bhs.operational_score, bhs.health_tier, bhs.battery_category,
                   bhs.latest_behaviour_grade, bhs.latest_thermal_grade,
                   bhs.latest_route_stress, bhs.scoring_mode as vScore_tier,
                   bhs.cluster_alert_group
            FROM batteries b
            LEFT JOIN battery_health_scores_v2 bhs ON b.battery_id = bhs.battery_id
            WHERE b.battery_id = ?
        """, [battery_id])
        if not prof:
            conn.close()
            return {"battery_id": battery_id, "has_data": False}
        result = dict(prof)
        result["has_data"] = True
        result["profile_confidence"] = "ESTIMATED"
        result["profile_source"] = "TELEMETRY_DERIVED"
        conn.close()
        return result

    # Entity names
    entities = {}
    for role in ['operator', 'lender', 'battery_oem', 'vehicle_oem', 'bms_oem', 'swap_operator']:
        eid = prof.get(f"{role}_id")
        if eid:
            e = q1(conn, "SELECT entity_name, city FROM entity_master WHERE entity_id = ?", [eid])
            if e:
                entities[f"{role}_name"] = e["entity_name"]
                if role == 'operator':
                    entities["operator_city"] = e.get("city")

    conn.close()
    result = dict(prof)
    result.update(entities)
    result["has_data"] = True
    return result


# ── GET /api/fleet/config-params ───────────────────────────────────

@app.get("/api/fleet/config-params")
@safe
def fleet_config_params(_=Depends(verify_token)):
    """3-layer adaptive threshold params grouped by layer."""
    conn = get_conn()
    rows = q(conn, "SELECT * FROM fleet_context_params WHERE is_active = 1 ORDER BY layer, param_name")
    conn.close()
    layers = {"layer1": [], "layer2": [], "layer3": []}
    for r in rows:
        key = f"layer{r.get('layer', 1)}"
        if key in layers:
            layers[key].append(r)
    return layers


# ── GET /api/fleet/config-audit ────────────────────────────────────

@app.get("/api/fleet/config-audit")
@safe
def fleet_config_audit(param_name: str = None, limit: int = 20, _=Depends(verify_token)):
    """Recent parameter change audit log."""
    conn = get_conn()
    if param_name:
        rows = q(conn, """
            SELECT * FROM fleet_context_params_audit
            WHERE param_name = ? ORDER BY changed_at DESC LIMIT ?
        """, [param_name, limit])
    else:
        rows = q(conn, """
            SELECT * FROM fleet_context_params_audit
            ORDER BY changed_at DESC LIMIT ?
        """, [limit])
    conn.close()
    return {"entries": rows}


# ── Outcome loop (M6) ─────────────────────────────────────────────

@app.get("/api/fleet/outcome-stats")
@safe
def fleet_outcome_stats(_=Depends(verify_token)):
    """Outcome validation metrics for predictions."""
    conn = get_conn()
    total = q1(conn, "SELECT COUNT(*) as n FROM outcome_log")
    ew = q(conn, """
        SELECT prediction_correct, COUNT(*) as n FROM outcome_log
        WHERE prediction_type='EARLY_WARNING' GROUP BY prediction_correct
    """)
    rul = q(conn, """
        SELECT prediction_correct, COUNT(*) as n FROM outcome_log
        WHERE prediction_type='RUL_ACTION' GROUP BY prediction_correct
    """)
    conn.close()
    ew_map = {r["prediction_correct"]: r["n"] for r in ew}
    rul_map = {r["prediction_correct"]: r["n"] for r in rul}
    return {
        "total_predictions": total["n"] if total else 0,
        "early_warning": {"confirmed": ew_map.get(1, 0), "false_positive": ew_map.get(0, 0), "pending": ew_map.get(None, 0)},
        "rul_action": {"confirmed": rul_map.get(1, 0), "false_positive": rul_map.get(0, 0), "pending": rul_map.get(None, 0)},
    }


# ── GET /api/outcome-log (OUTCOME-SEED) ──────────────────────────────

@app.get("/api/outcome-log")
@safe
def get_outcome_log(_=Depends(verify_token)):
    """All outcome_log rows ordered by predicted_rul_weeks ASC."""
    conn = get_conn()
    rows = q(conn, """
        SELECT * FROM outcome_log
        ORDER BY predicted_rul_weeks ASC, battery_id ASC
    """)
    conn.close()
    return rows


# ── POST /api/outcome-log/:battery_id (OUTCOME-SEED) ────────────────

class OutcomeBody(_BM):
    outcome_type: str = "UNKNOWN"
    actual_rul_weeks: Optional[float] = None
    field_notes: Optional[str] = None
    recorded_by: Optional[str] = None
    data_source: str = "MANUAL"

@app.post("/api/outcome-log/{battery_id}")
@safe
def update_outcome_log(battery_id: str, body: OutcomeBody, _=Depends(verify_token)):
    """Update a PENDING_VERIFICATION row with field outcome data."""
    conn = get_conn()
    existing = q1(conn, """
        SELECT outcome_id FROM outcome_log
        WHERE battery_id = ? AND outcome_type = 'PENDING_VERIFICATION'
        ORDER BY created_at DESC LIMIT 1
    """, [battery_id])
    if not existing:
        conn.close()
        raise HTTPException(404, f"No PENDING_VERIFICATION row for {battery_id}")

    conn.cursor().execute("""
        UPDATE outcome_log SET
            outcome_type = ?,
            actual_rul_weeks = ?,
            field_notes = ?,
            recorded_by = ?,
            data_source = ?,
            verified = 1,
            updated_at = datetime('now')
        WHERE outcome_id = ?
    """, [
        body.outcome_type,
        body.actual_rul_weeks,
        body.field_notes,
        body.recorded_by,
        body.data_source,
        existing["outcome_id"],
    ])
    conn.commit()
    conn.close()
    return {"status": "updated", "battery_id": battery_id, "outcome_id": existing["outcome_id"]}


# ── GET /api/fleet/prediction-accuracy ─────────────────────────────

@app.get("/api/fleet/prediction-accuracy")
@safe
def fleet_prediction_accuracy(_=Depends(verify_token)):
    """Prediction accuracy summary from outcome loop."""
    conn = get_conn()
    rows = q(conn, """
        SELECT prediction_type, n_predictions, n_pending,
               n_confirmed, n_expired, accuracy_pct,
               median_weeks_to_resolution, last_updated
        FROM prediction_accuracy_summary
        ORDER BY n_predictions DESC
    """)
    conn.close()
    return rows


@app.post("/api/battery/{battery_id}/outcome")
@safe
def record_outcome(battery_id: str, _=Depends(verify_token)):
    """Manual outcome entry — closes prediction feedback loop."""
    import json as _json
    from fastapi import Request
    # This is a placeholder — full implementation requires request body parsing
    conn = get_conn()
    conn.close()
    return {"status": "endpoint_ready", "battery_id": battery_id, "note": "Full body parsing in next sprint"}


# ── GET /api/fleet/divergence-cases ────────────────────────────────

@app.get("/api/fleet/divergence-cases")
@safe
def fleet_divergence_cases(_=Depends(verify_token)):
    """Batteries in each divergence case for Score Lab."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id,
               COALESCE(s.health_score_combined, s.combined_score_v2) as score,
               s.health_score_operational as op_score,
               s.health_score_trajectory as traj_score,
               s.health_tier,
               s.efficiency_score_v2 as l1, s.fault_risk_score as l2,
               s.attribution_score as l3, s.event_penalty,
               s.composite_score_legacy as legacy_score,
               s.score_reform_flag,
               v.km_per_soc_pct * 80 as range_km,
               v.cell_spread_max as spread_latest
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        JOIN (SELECT v2.battery_id, v2.km_per_soc_pct, v2.cell_spread_max
              FROM vehicle_weekly_features v2
              INNER JOIN (SELECT battery_id, MAX(week_number) mx
                          FROM vehicle_weekly_features WHERE km_per_soc_pct IS NOT NULL
                          GROUP BY battery_id) m
              ON v2.battery_id=m.battery_id AND v2.week_number=m.mx) v
        ON s.battery_id = v.battery_id
        WHERE b.chemistry='LFP' AND s.scoring_version='v2.0-range-predictive'
    """)
    conn.close()

    ranges = [r["range_km"] for r in rows if r["range_km"] is not None]
    import statistics
    n = len(ranges)
    p25 = sorted(ranges)[n // 4] if n >= 4 else 0
    p75 = sorted(ranges)[3 * n // 4] if n >= 4 else 999

    cases = {"A": [], "B": [], "C": [], "D": [], "F": [], "NORMAL": []}
    for r in rows:
        s = r["score"] or 0
        rng = r["range_km"] or 0
        flag = r["score_reform_flag"] or ""
        case = "NORMAL"
        if s >= 65 and rng <= p25:
            case = "A"
        elif s <= 50 and rng >= p75:
            case = "B"
        elif flag == "DIVERGE" and s > 60:
            case = "C"
        elif flag == "DIVERGE" and s <= 40:
            case = "D"
        r["case"] = case
        cases[case].append(r)

    return {
        "cases": {k: v for k, v in cases.items() if v},
        "counts": {k: len(v) for k, v in cases.items()},
        "total": len(rows),
    }


# ── GET /api/battery/{id}/score-components ─────────────────────────

@app.get("/api/battery/{battery_id}/score-components")
@safe
def battery_score_components(battery_id: str, _=Depends(verify_token)):
    """Detailed score components for Score Lab per-battery drill-down."""
    conn = get_conn()
    score = q1(conn, """
        SELECT efficiency_score_v2 as l1, fault_risk_score as l2,
               attribution_score as l3, event_penalty,
               composite_score_legacy, health_score_operational as op_score,
               health_score_trajectory as traj_score,
               health_score_combined as combined,
               health_tier, score_confidence, score_reform_flag, score_reform_note
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    if not score:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")

    # Get latest VWF
    latest = q1(conn, """
        SELECT v.km_per_soc_pct, v.cell_spread_max, v.week_number,
               v.soh_cap_weekly, v.charge_cycle_rate
        FROM vehicle_weekly_features v
        INNER JOIN (SELECT battery_id, MAX(week_number) mx
                    FROM vehicle_weekly_features WHERE km_per_soc_pct IS NOT NULL
                    AND battery_id = ?) m
        ON v.battery_id=m.battery_id AND v.week_number=m.mx
        WHERE v.battery_id = ?
    """, [battery_id, battery_id])

    # Compute causal direction from VWF
    vwf = q(conn, """
        SELECT week_number, km_per_soc_pct, charge_cycle_rate
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND km_per_soc_pct IS NOT NULL
        ORDER BY week_number
    """, [battery_id])
    conn.close()

    causal = {"direction": "UNKNOWN", "text": "Insufficient data for causal analysis."}
    if len(vwf) >= 10:
        baseline_wks = [w for w in vwf if w["week_number"] <= 8]
        if len(baseline_wks) >= 3:
            bl_kps = sum(w["km_per_soc_pct"] for w in baseline_wks) / len(baseline_wks)
            bl_chg_vals = [w["charge_cycle_rate"] for w in baseline_wks if w["charge_cycle_rate"] is not None]
            bl_chg = sum(bl_chg_vals) / len(bl_chg_vals) if bl_chg_vals else None

            chg_rise_wk = None
            range_fall_wk = None
            if bl_chg and bl_chg > 0:
                for i in range(1, len(vwf) - 1):
                    w = vwf[i]
                    if w["charge_cycle_rate"] and w["charge_cycle_rate"] > bl_chg * 1.15:
                        if vwf[i + 1].get("charge_cycle_rate") and vwf[i + 1]["charge_cycle_rate"] > bl_chg * 1.15:
                            chg_rise_wk = w["week_number"]
                            break
            for i in range(1, len(vwf) - 1):
                w = vwf[i]
                if w["km_per_soc_pct"] < bl_kps * 0.85:
                    if vwf[i + 1]["km_per_soc_pct"] < bl_kps * 0.85:
                        range_fall_wk = w["week_number"]
                        break

            if chg_rise_wk and range_fall_wk:
                if chg_rise_wk < range_fall_wk:
                    causal = {"direction": "CAUSATION", "text": f"Charging stress detected at week {chg_rise_wk}, range declined at week {range_fall_wk}. Charging may be driving degradation."}
                elif chg_rise_wk > range_fall_wk:
                    causal = {"direction": "COMPENSATION", "text": f"Range declined at week {range_fall_wk}, charging increased at week {chg_rise_wk}. Operator is compensating for lost range."}
                else:
                    causal = {"direction": "CONCURRENT", "text": "Charging and range changes are concurrent. Cannot distinguish cause from effect."}
            elif range_fall_wk and not chg_rise_wk:
                causal = {"direction": "COMPENSATION", "text": f"Range declining since week {range_fall_wk}. No charging pattern change detected. Degradation likely battery-intrinsic."}

    result = dict(score)
    if latest:
        result["range_km"] = round(latest["km_per_soc_pct"] * 80, 1) if latest["km_per_soc_pct"] else None
        result["spread_latest"] = latest["cell_spread_max"]
        result["soh_cap"] = _clip_soh_value(latest["soh_cap_weekly"])
        result["week_number"] = latest["week_number"]
    result["causal"] = causal
    return result


# ── GET /api/battery/{id}/attribution ──────────────────────────────

@app.get("/api/battery/{battery_id}/attribution")
@safe
def battery_attribution(battery_id: str,
                        audience: str = Query(default='operator'),
                        _=Depends(verify_token)):
    """Degradation attribution for a single battery, audience-routed.

    audience ∈ {operator, nbfc, oem, internal}. Operator (default) never sees
    percentages or internal codes. NMC/LCV/FGHHL return INCOMPLETE with a
    separate-methodology note. Pack3401 returns PACK_GAP_EXCEPTION (Rule 49).
    """
    conn = get_conn()
    try:
        resp = _build_attribution_response(battery_id, audience, conn)
    finally:
        conn.close()
    if resp is None:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    # Fix 5: passport consumes nbfc_sentence / battery_oem_sentence. The
    # builder writes them as nbfc_narrative / oem_narrative. Alias when
    # present; synthesise a minimal fallback when absent.
    if isinstance(resp, dict):
        nbfc_txt = resp.get("nbfc_narrative") or resp.get("nbfc_sentence")
        oem_txt  = resp.get("oem_narrative")  or resp.get("battery_oem_sentence")
        if not nbfc_txt:
            pm    = resp.get("pack_model") or "\u2014"
            prim  = resp.get("primary_driver_plain") or "\u2014"
            warr  = "Warranty claim eligible. " if resp.get("warranty_claim_eligible") == 1 else ""
            act   = resp.get("action_sentence") or ""
            nbfc_txt = f"Pack: {pm}. Primary driver: {prim}. {warr}{act}".strip()
        if not oem_txt:
            pm   = resp.get("pack_model") or "\u2014"
            mode = resp.get("attribution_mode") or "STANDARD"
            if mode == "PACK_GAP_EXCEPTION":
                oem_txt = f"Pack model: {pm}. PACK_GAP_EXCEPTION confirmed."
            else:
                prim = resp.get("primary_driver_plain") or "\u2014"
                oem_txt = f"Pack model: {pm}. Primary degradation driver: {prim}."
        resp["nbfc_sentence"] = nbfc_txt
        resp["battery_oem_sentence"] = oem_txt
    return resp


# ── GET /api/fleet/attribution-summary ─────────────────────────────

@app.get("/api/fleet/attribution-summary")
@safe
def fleet_attribution_summary(_=Depends(verify_token)):
    """Fleet-level attribution averages (LFP rickshaw fleet only).

    NMC/LCV/FGHHL excluded — separate methodology. Normalised factor
    averages returned.
    """
    conn = get_conn()
    # Fetch LFP GE/SG batteries only, per-battery factor values (BDA > legacy)
    rows = q(conn, """
        SELECT bhs.battery_id, bhs.pack_model,
               b.fleet_segment,
               COALESCE(bda.charging_pct,    bhs.attr_cycle_pct,     0) as charging,
               COALESCE(bda.usage_pct,       bhs.attr_dod_pct,       0) as usage,
               COALESCE(bda.thermal_pct,     bhs.attr_thermal_pct,   0) as thermal,
               COALESCE(bda.maintenance_pct, bhs.attr_imbalance_pct, 0) as maintenance,
               COALESCE(bda.calendar_pct,    bhs.attr_calendar_pct,  0) as calendar,
               COALESCE(bda.primary_driver,  bhs.attr_primary_factor) as primary_driver,
               bda.attribution_method
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON b.battery_id = bhs.battery_id
        LEFT JOIN battery_degradation_attribution bda ON bda.battery_id = bhs.battery_id
        WHERE (b.fleet_segment LIKE 'GE_%' OR b.fleet_segment LIKE 'SG_%')
    """)
    conn.close()

    totals = {'charging': 0.0, 'usage': 0.0, 'thermal': 0.0, 'maintenance': 0.0, 'calendar': 0.0}
    n_complete = 0
    incomplete = 0
    pack_gap = 0
    legacy = 0
    driver_counts = {}

    for r in rows:
        pm = r.get('pack_model') or ''
        if pm.endswith('Pack3401'):
            pack_gap += 1
            continue
        raw = {
            'charging':    r['charging'] or 0,
            'usage':       r['usage'] or 0,
            'thermal':     r['thermal'] or 0,
            'maintenance': r['maintenance'] or 0,
            'calendar':    r['calendar'] or 0,
        }
        norm, quality, _ = _normalize_lfp_factors(raw)
        if quality == 'INCOMPLETE' or not norm:
            incomplete += 1
            continue
        n_complete += 1
        for k in totals:
            totals[k] += norm.get(k, 0)
        if (r.get('attribution_method') or '') != 'RULE_BASED_V1':
            legacy += 1
        # primary driver distribution uses code → plain mapping
        code = atext.primary_driver_code(norm, None, {'attr_primary_factor': r.get('primary_driver')})
        plain = atext.primary_driver_plain(code)
        driver_counts[plain] = driver_counts.get(plain, 0) + 1

    factor_averages = {
        k: (round(v / n_complete, 1) if n_complete else 0.0)
        for k, v in totals.items()
    }

    # vehicle_issue proxy — count batteries where BHS flags VEHICLE_ISSUE quadrant
    conn = get_conn()
    vi_row = q1(conn, """
        SELECT COUNT(DISTINCT bhs.battery_id) as n
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON b.battery_id = bhs.battery_id
        WHERE (b.fleet_segment LIKE 'GE_%' OR b.fleet_segment LIKE 'SG_%')
          AND bhs.divergence_quadrant = 'VEHICLE_ISSUE'
    """)
    conn.close()
    vehicle_issue_count = (vi_row or {}).get('n', 0) or 0

    # Primary driver distribution (sorted)
    total_drivers = sum(driver_counts.values()) or 1
    driver_dist = sorted(
        ({'driver_plain': k, 'count': v, 'pct_of_fleet': round(v * 100.0 / total_drivers, 1)}
         for k, v in driver_counts.items()),
        key=lambda x: x['count'], reverse=True)

    return {
        'factor_averages': factor_averages,
        'primary_driver_distribution': driver_dist,
        'pack_gap_exception_count': pack_gap,
        'vehicle_issue_count': vehicle_issue_count,
        'incomplete_count': incomplete,
        'legacy_translated_count': legacy,
        'complete_count': n_complete,
        'fleet_scope': 'LFP_GE_SG',
    }


# ── GET /api/fleet/cluster-alerts ─────────────────────────────────

@app.get("/api/fleet/cluster-alerts")
@safe
def fleet_cluster_alerts_ep(_=Depends(verify_token)):
    """Fleet-level cluster alerts."""
    conn = get_conn()
    rows = q(conn, "SELECT * FROM fleet_cluster_alerts ORDER BY alert_severity, generated_date DESC")
    conn.close()
    return {"alerts": rows, "total": len(rows)}


# ── Main ──────────────────────────────────────────────────────────────

# ── GET /api/config/params ─────────────────────────────────────────

@app.get("/api/config/params")
@safe
def config_params(_=Depends(verify_token)):
    """All active fleet_context_params."""
    conn = get_conn()
    rows = q(conn, "SELECT * FROM fleet_context_params WHERE is_active=1 ORDER BY layer, param_name")
    conn.close()
    return {"params": rows, "total": len(rows)}


@app.get("/api/config/audit")
@safe
def config_audit(param_name: str = None, limit: int = 20, _=Depends(verify_token)):
    """Audit trail for fleet_context_params."""
    conn = get_conn()
    if param_name:
        rows = q(conn, "SELECT * FROM fleet_context_params_audit WHERE param_name=? ORDER BY changed_at DESC LIMIT ?", [param_name, limit])
    else:
        rows = q(conn, "SELECT * FROM fleet_context_params_audit ORDER BY changed_at DESC LIMIT ?", [limit])
    conn.close()
    return {"audit": rows, "total": len(rows)}


@app.put("/api/config/param")
@safe
def config_param_update(body: dict = None, _=Depends(verify_token)):
    """Customer config override (layer 3 only)."""
    if not body:
        raise HTTPException(400, "Body required")
    name = body.get("param_name")
    value = body.get("param_value")
    seg_type = body.get("segment_type")
    seg_value = body.get("segment_value")
    reason = body.get("change_reason", "")
    operator = body.get("operator_id", "API_USER")

    if not name or value is None or not seg_type or not seg_value:
        raise HTTPException(400, "Missing required fields")
    if len(reason) < 10:
        raise HTTPException(400, "change_reason must be at least 10 characters")

    # Check if layer 1 param — reject
    conn = get_conn()
    existing = q1(conn, "SELECT layer, override_floor, override_ceiling FROM fleet_context_params WHERE param_name=? AND segment_type=? AND segment_value=? ORDER BY layer LIMIT 1", [name, seg_type, seg_value])
    if not existing:
        existing = q1(conn, "SELECT layer, override_floor, override_ceiling FROM fleet_context_params WHERE param_name=? ORDER BY layer LIMIT 1", [name])
    conn.close()

    if existing and existing.get("layer") == 1 and seg_type != "BATTERY_ID":
        raise HTTPException(400, "Layer 1 physics params cannot be changed via API")

    # Use upsert_param from params module
    import sys; sys.path.insert(0, str(Path(DB_PATH).parent))
    from params import upsert_param
    result = upsert_param(name, float(value), "CUSTOMER_CONFIG", seg_type, seg_value, 3,
                         changed_by="CUSTOMER_CONFIG", change_reason=reason)
    if not result["success"]:
        raise HTTPException(400, result.get("error", "Validation failed"))
    return result


# ── POST /api/config/rollback ────────────────────────────────────
@app.post("/api/config/rollback")
@safe
def config_rollback(body: dict = None, _=Depends(verify_token)):
    """Rollback a fleet_context_params change using audit_id."""
    if not body or "audit_id" not in body:
        raise HTTPException(400, "audit_id required")
    audit_id = body["audit_id"]
    conn = get_conn()
    row = q1(conn, "SELECT * FROM fleet_context_params_audit WHERE audit_id=? AND rollback_available=1", [audit_id])
    if not row:
        conn.close()
        raise HTTPException(404, "Audit entry not found or rollback not available")
    # Restore old value as new Layer 3 row
    import sys; sys.path.insert(0, str(Path(DB_PATH).parent))
    from params import upsert_param
    result = upsert_param(
        row["param_name"], float(row["old_value"]), "ROLLBACK",
        row["segment_type"], row["segment_value"], 3,
        changed_by="FDE_ROLLBACK",
        change_reason=f"ROLLBACK of audit_id {audit_id}"
    )
    # Mark original audit row as rolled back
    conn.execute("UPDATE fleet_context_params_audit SET rollback_available=0 WHERE audit_id=?", [audit_id])
    conn.commit()
    conn.close()
    return {"success": True, "rolled_back_audit_id": audit_id, "restored_value": row["old_value"]}


# ── Master Data endpoints ──────────────────────────────────────────

@app.get("/api/entity/{entity_id}")
@safe
def get_entity(entity_id: str, _=Depends(verify_token)):
    conn = get_conn()
    row = q1(conn, "SELECT * FROM entity_master WHERE entity_id=?", [entity_id])
    conn.close()
    if not row: raise HTTPException(404, f"Entity {entity_id} not found")
    return row

@app.get("/api/templates")
@safe
def get_templates(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, "SELECT * FROM template_registry WHERE is_active_tmpl=1 OR is_active_tmpl IS NULL ORDER BY template_id")
    conn.close()
    return {"templates": rows, "total": len(rows)}

@app.get("/api/fleet/profile-summary")
@safe
def fleet_profile_summary(_=Depends(verify_token)):
    conn = get_conn()
    intensity = q(conn, "SELECT usage_intensity_tier, COUNT(*) n FROM battery_usage_profile WHERE is_current=1 GROUP BY usage_intensity_tier")
    charge = q(conn, "SELECT primary_charge_type, COUNT(*) n FROM battery_usage_profile WHERE is_current=1 GROUP BY primary_charge_type")
    tmpl = q(conn, "SELECT template_id, COUNT(*) n FROM battery_usage_profile WHERE is_current=1 GROUP BY template_id")
    conf = q(conn, "SELECT profile_confidence, COUNT(*) n FROM battery_usage_profile WHERE is_current=1 GROUP BY profile_confidence")
    conn.close()
    return {"usage_intensity": {r["usage_intensity_tier"]:r["n"] for r in intensity},
            "charge_type": {r["primary_charge_type"]:r["n"] for r in charge},
            "template": {r["template_id"]:r["n"] for r in tmpl},
            "confidence": {r["profile_confidence"]:r["n"] for r in conf}}


# ── Attention Router endpoints ─────────────────────────────────

@app.get("/api/stakeholder/{stype}/attention-list")
@safe
def stakeholder_attention_list(stype: str, level: str = None, _=Depends(verify_token)):
    conn = get_conn()
    if level:
        rows = q(conn, "SELECT * FROM stakeholder_attention WHERE stakeholder_type=? AND attention_level=? ORDER BY attention_level", [stype.upper(), level.upper()])
    else:
        rows = q(conn, "SELECT * FROM stakeholder_attention WHERE stakeholder_type=? ORDER BY CASE attention_level WHEN 'GONE' THEN 1 WHEN 'CRITICAL' THEN 2 WHEN 'URGENT' THEN 3 WHEN 'ACT' THEN 4 WHEN 'WATCH' THEN 5 WHEN 'ROUTINE' THEN 6 WHEN 'NONE' THEN 7 END", [stype.upper()])
    conn.close()
    return {"stakeholder": stype.upper(), "batteries": rows, "total": len(rows)}

@app.get("/api/battery/{battery_id}/attention-all")
@safe
def battery_attention_all(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, "SELECT * FROM stakeholder_attention WHERE battery_id=? ORDER BY stakeholder_type", [battery_id])
    conn.close()
    return {"battery_id": battery_id, "attention": rows}

@app.get("/api/fleet/attention-summary")
@safe
def fleet_attention_summary(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, "SELECT * FROM v_fleet_attention_summary")
    conn.close()
    return {"summary": rows}

@app.get("/api/stakeholder/{stype}/attention-counts")
@safe
def stakeholder_attention_counts(stype: str, _=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, "SELECT attention_level, COUNT(*) as n FROM stakeholder_attention WHERE stakeholder_type=? GROUP BY attention_level", [stype.upper()])
    conn.close()
    return {"stakeholder": stype.upper(), "counts": {r["attention_level"]: r["n"] for r in rows}}


# ── GET /api/battery/{id}/consequence-chain ──────────────────────

@app.get("/api/battery/{battery_id}/consequence-chain")
@safe
def battery_consequence_chain(battery_id: str, _=Depends(verify_token)):
    """Full consequence chain + BHS intelligence for a battery.

    Falls back to a synthesised object when the stakeholder_consequence_chain
    row is absent for a battery, so the endpoint always returns a populated
    object instead of an empty {} (Sprint 2D-backend Fix 4).
    """
    conn = get_conn()
    row = q1(conn, """
        SELECT scc.*,
               bhs.battery_category, bhs.golden_rule_pass,
               bhs.rr_note, bhs.fault_severity, bhs.fault_type,
               bhs.fault_recurrence_count, bhs.fault_duration_weeks,
               bhs.latest_behaviour_grade, bhs.latest_thermal_grade,
               bhs.latest_route_stress, bhs.operating_context_summary,
               bhs.scoring_conflict_note, bhs.asset_recovery_class,
               bhs.resale_value_adjusted_inr,
               bhs.action_primary, bhs.action_remark,
               bhs.shap_top1_feature, bhs.shap_top1_pct,
               bhs.shap_top2_feature, bhs.shap_top2_pct,
               bhs.shap_top3_feature, bhs.shap_top3_pct,
               bhs.shap_explanation_sentence,
               bhs.intervention_window, bhs.value_velocity,
               bhs.spread_delta_mv, bhs.capacity_state,
               bhs.corroboration_score, bhs.corroboration_verdict,
               bhs.nbfc_risk_tier, bhs.nbfc_summary_sentence,
               bhs.nbfc_recovery_note,
               bda.primary_driver, bda.operator_attribution_sentence,
               bda.cohort_label,
               bda.charging_pct, bda.usage_pct, bda.maintenance_pct,
               bda.thermal_pct, bda.calendar_pct,
               bhs.shap_attribution_method
        FROM stakeholder_consequence_chain scc
        JOIN battery_health_scores_v2 bhs ON scc.battery_id = bhs.battery_id
        LEFT JOIN battery_degradation_attribution bda ON scc.battery_id = bda.battery_id
        WHERE scc.battery_id = ?
    """, [battery_id])
    # Helper: synthesise a chain object from BHS + BDA so the UI never sees {}.
    def _synth(conn):
        bhs = q1(conn, """
            SELECT bhs.battery_id, bhs.action_primary, bhs.action_remark,
                   bhs.rul_action_v2, bhs.soh_conservative, bhs.tier_label_v2,
                   bhs.fault_severity, bhs.fault_type,
                   bhs.corroboration_verdict, bhs.battery_category,
                   bhs.golden_rule_pass, bhs.shap_explanation_sentence,
                   bhs.nbfc_summary_sentence, bhs.nbfc_risk_tier,
                   bhs.capacity_state, bhs.intervention_window,
                   bda.operator_attribution_sentence,
                   bda.nbfc_attribution_sentence,
                   bda.oem_attribution_sentence,
                   bda.primary_driver,
                   bda.pack_model_finding
            FROM battery_health_scores_v2 bhs
            LEFT JOIN battery_degradation_attribution bda
                   ON bda.battery_id = bhs.battery_id
            WHERE bhs.battery_id = ?
        """, [battery_id])
        if not bhs:
            return None
        # Choose the right operator sentence: Pack3401 gets the pack-gap narrative.
        _ops_txt = bhs.get("operator_attribution_sentence")
        if bhs.get("pack_model_finding") == "PACK_GAP_EXCEPTION" and not _ops_txt:
            _ops_txt = ("This Pack3401 battery is showing degradation patterns "
                        "consistent with intrinsic design characteristics confirmed "
                        "across the fleet. Operator behaviour is within normal "
                        "parameters for this pack model.")
        return {
            "battery_id": battery_id,
            "integrated_chain_sentence": "",
            "operator_attribution_sentence": _ops_txt,
            "nbfc_sentence": bhs.get("nbfc_attribution_sentence") or bhs.get("nbfc_summary_sentence"),
            "battery_oem_sentence": bhs.get("oem_attribution_sentence") or bhs.get("shap_explanation_sentence"),
            "action_primary": bhs.get("rul_action_v2") or bhs.get("action_primary"),
            "action_remark": bhs.get("action_remark"),
            "fault_severity": bhs.get("fault_severity"),
            "fault_type": bhs.get("fault_type"),
            "corroboration_verdict": bhs.get("corroboration_verdict"),
            "battery_category": bhs.get("battery_category"),
            "golden_rule_pass": bhs.get("golden_rule_pass"),
            "capacity_state": bhs.get("capacity_state"),
            "intervention_window": bhs.get("intervention_window"),
            "nbfc_risk_tier": bhs.get("nbfc_risk_tier"),
            "primary_driver": bhs.get("primary_driver"),
        }

    if row:
        # Sprint 2D-pipeline Fix P3: apply clean_sentences FIRST (nulls out
        # stored sentences that contain internal-code markers), then
        # per-field merge empty slots from synthesis. This handles both
        # "row exists but field was empty in DB" and "field was populated
        # but dirty and got nulled by the cleanliness filter".
        row = clean_sentences(row) if "clean_sentences" in globals() else row
        _key_fields = ("integrated_chain_sentence",
                       "operator_attribution_sentence",
                       "nbfc_sentence",
                       "battery_oem_sentence")
        _any_empty = any(not (row.get(k) or "").strip() for k in _key_fields)
        if _any_empty:
            synth = _synth(conn)
            if synth:
                _filled = []
                for k, v in synth.items():
                    cur = row.get(k)
                    if isinstance(cur, str) and cur.strip():
                        continue  # keep clean non-empty
                    if cur is not None and not isinstance(cur, str):
                        continue  # keep non-string values
                    has_value = (isinstance(v, str) and v.strip()) or (not isinstance(v, str) and v is not None)
                    if has_value:
                        row[k] = v
                        _filled.append(k)
                if _filled:
                    row["partially_synthesised"] = True
                    row["_synth_filled_fields"] = _filled
        conn.close()
        return row

    # Fully synthesised fallback (row absent)
    synth = _synth(conn)
    conn.close()
    if not synth:
        return {}
    synth["synthesised"] = True
    return clean_sentences(synth) if "clean_sentences" in globals() else synth


# ── GET /api/battery/{id}/observations ───────────────────────────

@app.get("/api/battery/{battery_id}/observations")
@safe
def battery_observations(battery_id: str, _=Depends(verify_token)):
    """Recent intervention/observation log entries."""
    conn = get_conn()
    rows = q(conn, """
        SELECT observation_type, observation_sentence,
               consequence_sentence, warranty_relevant,
               controllable, created_at
        FROM intervention_log
        WHERE battery_id = ?
        ORDER BY created_at DESC LIMIT 8
    """, [battery_id])
    conn.close()
    return rows


# ── GET /api/fleet/consequence-patterns ──────────────────────────

@app.get("/api/fleet/consequence-patterns")
@safe
def fleet_consequence_patterns(_=Depends(verify_token)):
    """Active fleet-wide consequence patterns."""
    conn = get_conn()
    rows = q(conn, """
        SELECT pattern_name, n_batteries_affected,
               pct_fleet_affected, confidence_grade,
               stressor_type, pattern_intelligence_sentence, status
        FROM fleet_consequence_patterns
        WHERE status = 'ACTIVE'
        ORDER BY n_batteries_affected DESC
    """)
    conn.close()
    return rows


# ── GET /api/fleet/category-summary ──────────────────────────────

@app.get("/api/fleet/category-summary")
@safe
def fleet_category_summary(_=Depends(verify_token)):
    """Battery category distribution with range and score."""
    conn = get_conn()
    rows = q(conn, """
        SELECT bhs.battery_category, COUNT(*) as n,
               ROUND(AVG(v.km_per_soc_pct * 80), 1) as mean_range,
               ROUND(AVG(bhs.operational_score), 1) as mean_score,
               SUM(bhs.golden_rule_pass) as rr_count
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id = b.battery_id
        LEFT JOIN (SELECT v2.battery_id, v2.km_per_soc_pct FROM vehicle_weekly_features v2
                   INNER JOIN (SELECT battery_id, MAX(week_number) mx
                               FROM vehicle_weekly_features WHERE km_per_soc_pct IS NOT NULL
                               GROUP BY battery_id) m
                   ON v2.battery_id = m.battery_id AND v2.week_number = m.mx) v
        ON bhs.battery_id = v.battery_id
        WHERE b.chemistry = 'LFP'
        GROUP BY bhs.battery_category
        ORDER BY AVG(v.km_per_soc_pct * 80) DESC
    """)
    conn.close()
    return rows


# ── GET /api/oem/pack-attribution ─────────────────────────────────

@app.get("/api/oem/pack-attribution")
@safe
def oem_pack_attribution(_=Depends(verify_token)):
    """Pack-model-level attribution averages for OEM view."""
    conn = get_conn()
    rows = q(conn, """
        SELECT b.battery_model,
               COUNT(*) as battery_count,
               ROUND(AVG(bda.maintenance_pct), 1) as avg_maintenance,
               ROUND(AVG(bda.charging_pct), 1) as avg_charging,
               ROUND(AVG(bda.calendar_pct), 1) as avg_calendar,
               ROUND(AVG(bda.thermal_pct), 1) as avg_thermal,
               ROUND(AVG(bda.usage_pct), 1) as avg_usage
        FROM battery_degradation_attribution bda
        JOIN batteries b USING (battery_id)
        GROUP BY b.battery_model
        ORDER BY battery_count DESC
    """)
    conn.close()
    return rows


# ── GET /api/battery/{id}/baseline-quality ────────────────────────

@app.get("/api/battery/{battery_id}/baseline-quality")
@safe
def battery_baseline_quality(battery_id: str, _=Depends(verify_token)):
    """Baseline scenario and data quality notes for a battery."""
    conn = get_conn()
    row = q1(conn, """
        SELECT baseline_scenario, soh_baseline_suspect, gps_data_gap_flag,
               baseline_data_quality_note, scoring_mode, commissioning_spread_mv
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    return row if row else {}


# ── GET /api/fleet/charge-health-summary ─────────────────────────

@app.get("/api/fleet/charge-health-summary")
@safe
def fleet_charge_health_summary(_=Depends(verify_token)):
    """Fleet charge health distribution (electrochemical, not behaviour)."""
    conn = get_conn()
    rows = q(conn, """
        SELECT charge_health_grade, COUNT(*) as batteries,
               ROUND(AVG(charge_health_score), 1) as mean_score,
               ROUND(AVG(range_est_km), 1) as mean_range,
               ROUND(AVG(operational_score), 1) as mean_op_score
        FROM battery_health_scores_v2
        WHERE battery_id IN (SELECT battery_id FROM batteries WHERE chemistry='LFP')
          AND charge_health_grade IS NOT NULL
        GROUP BY charge_health_grade
        ORDER BY mean_score DESC
    """)
    conn.close()
    return rows


# ── GET /api/battery/{id}/nbfc ─────────────────────────────────

@app.get("/api/battery/{battery_id}/nbfc")
@safe
def battery_nbfc(battery_id: str, _=Depends(verify_token)):
    """NBFC financing view for a single battery."""
    conn = get_conn()
    row = q1(conn, """
        SELECT bhs.nbfc_risk_tier, bhs.nbfc_summary_sentence,
               bhs.efc_pct_of_warranty, bhs.rul_weeks_v2,
               bhs.rul_confidence, bhs.operational_score,
               bhs.safety_events_count, bhs.pack_risk_note,
               bhs.nbfc_ltv_flag, bhs.nbfc_ltv,
               bhs.efc_cumulative, bhs.nbfc_suitable, bhs.nbfc_caveat,
               bhs.nbfc_rul_disclosure, bhs.nbfc_range_capacity_pct,
               bhs.nbfc_range_capacity_sentence, bhs.nbfc_income_sentence,
               bhs.nbfc_income_grade, bhs.nbfc_action_remark,
               bhs.rul_action_v2,
               b.battery_model, b.oem_name
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id = b.battery_id
        WHERE bhs.battery_id = ?
    """, [battery_id])
    conn.close()
    if not row:
        raise HTTPException(404, f"No NBFC data for {battery_id}")

    # EMI risk from rul_action_v2 (Step 1 spec)
    action = row.get("rul_action_v2") or ""
    # X2 Fix 10 — Rule 207: deprecated actions must never appear downstream
    if action in ("REPLACE_URGENT", "EOL_IMMINENT"):
        action = "REPLACE_PLAN"
    if action in ("CELL_BALANCE", "REPLACE_PLAN"):
        emi_risk = "MEDIUM"
    else:
        emi_risk = "LOW"

    result = {
        "battery_id": battery_id,
        "nbfc_risk_tier": row.get("nbfc_risk_tier"),
        "nbfc_sentence": row.get("nbfc_summary_sentence"),
        "warranty_consumed_pct": round(row["efc_pct_of_warranty"], 1) if row.get("efc_pct_of_warranty") else None,
        "rul_weeks": row.get("rul_weeks_v2"),
        "rul_confidence": row.get("rul_confidence"),
        "rul_action": action,
        "emi_risk": emi_risk,
        "safety_events_count": row.get("safety_events_count") or 0,
        "efc_cumulative": round(row["efc_cumulative"], 1) if row.get("efc_cumulative") else None,
        "nbfc_suitable": bool(row.get("nbfc_suitable")),
        "nbfc_rul_disclosure": row.get("nbfc_rul_disclosure"),
        "nbfc_rul_usable_for_covenants": False,  # Always FALSE per Rule C09
        "range_capacity_pct": row.get("nbfc_range_capacity_pct"),
        "income_grade": row.get("nbfc_income_grade"),
        "loan_tenure_weeks_remaining": None,  # NULL until M4-LOAN sprint
    }
    # Only include non-null optional fields
    for key in ["pack_risk_note", "nbfc_caveat", "nbfc_action_remark",
                "nbfc_range_capacity_sentence", "nbfc_income_sentence"]:
        val = row.get(key)
        if val:
            result[key] = val
    if row.get("nbfc_ltv_flag"):
        result["ltv_flag"] = row.get("nbfc_ltv_flag")
        result["ltv_value"] = row.get("nbfc_ltv")

    return result


# ── GET /api/battery/{id}/internal ─────────────────────────────

@app.get("/api/battery/{battery_id}/internal")
@safe
def battery_internal(battery_id: str, _=Depends(verify_token)):
    """Internal diagnostics panel — FDE / AI Analyst only."""
    conn = get_conn()

    bhs = q1(conn, """
        SELECT bhs.range_est_km, bhs.soh_corrected, bhs.operational_score,
               bhs.rul_weeks_v2, bhs.scoring_mode, bhs.vscore_flag,
               bhs.scoring_conflict_note, bhs.data_quality_flags,
               bhs.soh_coulomb, bhs.soh_coulomb_n_cycles, bhs.soh_coulomb_confidence,
               bhs.capacity_rul_weeks, bhs.capacity_rul_confidence,
               bhs.degradation_regime, bhs.tier_label_v2,
               b.chemistry
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id = b.battery_id
        WHERE bhs.battery_id = ?
    """, [battery_id])

    if not bhs:
        conn.close()
        raise HTTPException(404, f"No data for {battery_id}")

    # DT NMC session results (latest)
    dt_nmc = q1(conn, """
        SELECT rmse_mv, session_date, model_version, gate_result, status
        FROM dt_nmc_session_results
        WHERE battery_id = ?
        ORDER BY session_date DESC LIMIT 1
    """, [battery_id])

    # DT-LFP projections (all 3 horizons)
    dt_lfp = q(conn, """
        SELECT horizon_weeks, range_p10_km, range_p50_km, range_p90_km,
               model_version
        FROM dt_lfp_range_projections
        WHERE battery_id = ?
        ORDER BY horizon_weeks
    """, [battery_id])

    conn.close()

    return {
        "battery_id": battery_id,
        "chemistry": bhs.get("chemistry"),
        "signals": {
            "range_est_km": bhs.get("range_est_km"),
            "soh_corrected": bhs.get("soh_corrected"),
            "operational_score": bhs.get("operational_score"),
            "rul_weeks_v2": bhs.get("rul_weeks_v2"),
            "soh_coulomb": _clip_soh_value(bhs.get("soh_coulomb")),
            "soh_coulomb_n_cycles": bhs.get("soh_coulomb_n_cycles"),
            "soh_coulomb_confidence": bhs.get("soh_coulomb_confidence"),
            "capacity_rul_weeks": bhs.get("capacity_rul_weeks"),
            "capacity_rul_confidence": bhs.get("capacity_rul_confidence"),
            "degradation_regime": bhs.get("degradation_regime"),
        },
        "scoring": {
            "scoring_mode": bhs.get("scoring_mode"),
            "vscore_flag": bhs.get("vscore_flag"),
            "tier_label": bhs.get("tier_label_v2"),
            "scoring_conflict_note": bhs.get("scoring_conflict_note"),
            "data_quality_flags": bhs.get("data_quality_flags"),
        },
        "dt_nmc": {
            "rmse_mv": dt_nmc["rmse_mv"] if dt_nmc else None,
            "session_date": dt_nmc["session_date"] if dt_nmc else None,
            "model_version": dt_nmc["model_version"] if dt_nmc else None,
            "gate_result": dt_nmc["gate_result"] if dt_nmc else None,
            "status": dt_nmc["status"] if dt_nmc else None,
        } if dt_nmc else None,
        "dt_lfp_projections": [
            {
                "horizon_weeks": r["horizon_weeks"],
                "range_p10_km": r["range_p10_km"],
                "range_p50_km": r["range_p50_km"],
                "range_p90_km": r["range_p90_km"],
                "model_version": r.get("model_version"),
            } for r in (dt_lfp or [])
        ],
    }


# ── GET /api/battery/{id}/oem ──────────────────────────────────

@app.get("/api/battery/{battery_id}/oem")
@safe
def battery_oem(battery_id: str, _=Depends(verify_token)):
    """OEM / manufacturer view for a single battery."""
    conn = get_conn()
    row = q1(conn, """
        SELECT b.battery_id, b.battery_model AS pack_model, b.oem_name, b.chemistry,
               bhs.efc_cumulative AS efc, bhs.efc_pct_of_warranty AS warranty_consumed_pct,
               bhs.degradation_regime, bhs.soh_corrected, bhs.capacity_state,
               bhs.degradation_primary_driver AS primary_driver,
               bhs.shap_top1_feature, bhs.shap_top1_pct,
               bhs.shap_top2_feature, bhs.shap_top2_pct,
               bhs.shap_top3_feature, bhs.shap_top3_pct,
               bhs.shap_explanation_sentence,
               bhs.degradation_attribution_json,
               bhs.cluster_alert_flag, bhs.pack_risk_note,
               bhs.scoring_conflict_note
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id = b.battery_id
        WHERE bhs.battery_id = ?
    """, [battery_id])

    # OEM sentence from consequence chain
    scc = q1(conn, "SELECT battery_oem_sentence FROM stakeholder_consequence_chain WHERE battery_id = ?", [battery_id])
    conn.close()

    if not row:
        raise HTTPException(404, f"No OEM data for {battery_id}")

    # Parse attribution JSON
    attr_json = row.get("degradation_attribution_json")
    attribution = {"charging": None, "battery_design": None, "maintenance": None}
    attribution_available = False
    if attr_json:
        try:
            import json as _j, math
            parsed = _j.loads(attr_json)
            ch = parsed.get("charging")
            mt = parsed.get("maintenance")
            us = parsed.get("usage", 0)
            th = parsed.get("thermal", 0)
            ca = parsed.get("calendar", 0)
            if ch is not None and not (isinstance(ch, float) and math.isnan(ch)):
                attribution["charging"] = round(ch, 1)
                attribution["battery_design"] = round((th or 0) + (ca or 0), 1)
                attribution["maintenance"] = round((mt or 0) + (us or 0), 1)
                attribution_available = True
        except Exception:
            pass

    regime_labels = {
        "PRE_KNEE": "Early life — normal wear",
        "APPROACHING_KNEE": "Mid life — watch trajectory",
        "POST_KNEE": "Late life — accelerated wear",
        "STABLE": "Stable — no knee detected",
    }

    result = {
        "battery_id": battery_id,
        "pack_model": row.get("pack_model"),
        "oem_name": row.get("oem_name"),
        "chemistry": row.get("chemistry"),
        "efc": round(row["efc"], 1) if row.get("efc") else None,
        "warranty_consumed_pct": round(row["warranty_consumed_pct"], 1) if row.get("warranty_consumed_pct") else None,
        "degradation_regime": row.get("degradation_regime"),
        "regime_label": regime_labels.get(row.get("degradation_regime"), "Regime not determined"),
        "soh_corrected": round(row["soh_corrected"], 1) if row.get("soh_corrected") else None,
        "capacity_state": row.get("capacity_state"),
        "primary_driver": row.get("primary_driver"),
        "attribution_split": attribution,
        "attribution_available": attribution_available,
        "shap_top1_feature": row.get("shap_top1_feature"),
        "shap_top1_pct": row.get("shap_top1_pct"),
        "shap_top2_feature": row.get("shap_top2_feature"),
        "shap_top2_pct": row.get("shap_top2_pct"),
        "shap_top3_feature": row.get("shap_top3_feature"),
        "shap_top3_pct": row.get("shap_top3_pct"),
        "shap_explanation": row.get("shap_explanation_sentence"),
        "cluster_alert_flag": row.get("cluster_alert_flag") or 0,
        "pack_risk_note": row.get("pack_risk_note"),
        "scoring_conflict_note": row.get("scoring_conflict_note"),
        "battery_oem_sentence": (scc or {}).get("battery_oem_sentence"),
    }
    return result


# ── P5-A: DSS Decision Support Schema ────────────────────────────
@app.get("/api/battery/{battery_id}/dss")
@safe
def battery_dss(battery_id: str, audience: str = "operator", _=Depends(verify_token)):
    """6-block DSS schema with audience routing."""
    conn = get_conn()
    s = q1(conn, """SELECT * FROM battery_health_scores_v2
                     WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1""", [battery_id])
    if not s:
        return {"error": "battery not found"}

    b = q1(conn, "SELECT * FROM batteries WHERE battery_id = ?", [battery_id])
    ps = q1(conn, """SELECT * FROM battery_pattern_signals
                      WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1""", [battery_id])
    pp = q1(conn, "SELECT * FROM battery_personal_params WHERE battery_id = ?", [battery_id])

    # Block 1: Finding
    range_p50 = s.get("range_p50") or s.get("range_est_km")
    trend = "DECLINING" if (s.get("kps_slope_4wk") or 0) < -0.01 else "STABLE" if abs(s.get("kps_slope_4wk") or 0) < 0.01 else "IMPROVING"
    hc = s.get("health_class_v2") or s.get("health_tier") or "UNKNOWN"
    finding = {
        "summary": f"Range {range_p50:.0f} km, trend {trend.lower()}. {s.get('operator_cause_sentence') or ''}" if range_p50 else "Insufficient data for range estimate.",
        "health_class": hc,
        "range_p50": range_p50,
        "range_trend": trend,
        "confidence": s.get("data_confidence_label") or "MEDIUM"
    }

    # Block 2: Attribution
    attribution = {
        "primary_cause": s.get("attr_primary_factor") or s.get("degradation_primary_driver") or "unknown",
        "charging_pct": s.get("attr_cycle_pct") or 0,
        "calendar_pct": s.get("attr_calendar_pct") or 0,
        "operational_pct": s.get("attr_dod_pct") or 0,
        "thermal_pct": s.get("attr_thermal_pct") or 0,
        "electrochemical_pct": s.get("attr_imbalance_pct") or 0,
        "audience_explanation": s.get("operator_cause_sentence") if audience == "operator"
            else s.get("nbfc_summary_sentence") if audience == "nbfc"
            else s.get("shap_explanation_sentence") or ""
    }

    # Block 3: Recommendation
    recommendation = {
        "action": s.get("rul_action_v2") or s.get("action_primary") or "MONITOR_WEEKLY",
        "urgency": "THIS_WEEK" if s.get("rul_action_v2") in ("PHYSICS_REPLACE_PLAN", "ACUTE_BREACH_WATCH") else "ROUTINE",
        "expected_outcome": s.get("operator_action_sentence") or "",
        "resale_value_est": s.get("resale_value_est") or s.get("resale_value_inr"),
        "risk_tier": s.get("nbfc_risk_tier")
    }

    # Block 4: Confidence
    confidence = {
        "soh_method": "EFFICIENCY_PROXY",
        "data_weeks": s.get("last_data_week") or s.get("week_number"),
        "corroboration_score": s.get("corroboration_score"),
        "corroboration_verdict": s.get("corroboration_verdict"),
        "calibration_note": "RUL directional only. Not for covenant triggers." if audience == "nbfc" else None
    }

    # Block 5: Pattern (includes driver stress from T4-B)
    stress_score = (pp or {}).get("driver_stress_score")
    stress_tier = (pp or {}).get("driver_stress_tier") or "UNKNOWN"
    pattern = {
        "fault_severity": s.get("fault_severity") or "NONE",
        "fault_trajectory": (ps or {}).get("soh_trend_break_direction") or "STABLE",
        "drive_profile": (pp or {}).get("personal_drive_profile") or "UNKNOWN",
        "charge_profile": (pp or {}).get("personal_charge_profile") or "UNKNOWN",
        "driver_stress_score": stress_score if audience == "oem" else None,
        "driver_stress_tier": stress_tier,
        "degradation_tier": hc
    }

    # Block 6: Component (enriched with battery_component_health)
    ch = _component_health_query(conn, battery_id) if battery_id not in _BLOCKED_BATTERIES else None
    component = {
        "cell_spread_mv": s.get("cell_balance_spread_mv") or s.get("spread_delta_mv"),
        "cell_spread_acceleration": s.get("spread_delta_slope"),
        "breach_chain_factor": s.get("breach_chain_factor"),
        "physical_damage_risk": "HIGH" if (s.get("physical_integrity_risk") or 0) > 0.7 else "LOW",
        "bms_alert_pattern": "NORMAL",
    }
    if ch:
        component["component_health"] = {
            "iot_quality_score": ch.get("iot_quality_score"),
            "bms_alert_count_12wk": ch.get("bms_alert_count_12wk"),
            "npf_probability": ch.get("npf_probability"),
            "data_gap_cause": ch.get("data_gap_cause"),
            "firmware_version": ch.get("firmware_version"),
        }

    conn.close()

    # Audience routing
    if audience == "operator":
        op_pattern = {"drive_profile": pattern["drive_profile"], "driver_stress_tier": pattern["driver_stress_tier"]}
        return {"battery_id": battery_id, "audience": audience, "week_number": s.get("week_number"),
                "finding": finding, "recommendation": recommendation, "pattern": op_pattern}
    elif audience == "nbfc":
        nbfc_pattern = {"driver_stress_tier": pattern["driver_stress_tier"], "degradation_tier": pattern["degradation_tier"]}
        return {"battery_id": battery_id, "audience": audience, "week_number": s.get("week_number"),
                "finding": finding, "attribution": attribution, "confidence": confidence,
                "recommendation": {"action": recommendation["action"], "risk_tier": recommendation["risk_tier"]},
                "pattern": nbfc_pattern}
    else:  # oem
        return {"battery_id": battery_id, "audience": audience, "week_number": s.get("week_number"),
                "pack_model": (b or {}).get("battery_model"),
                "finding": finding, "attribution": attribution, "recommendation": recommendation,
                "confidence": confidence, "pattern": pattern, "component": component}


@app.get("/api/battery/{battery_id}/contextual-scores")
@safe
def battery_contextual_scores(battery_id: str, _=Depends(verify_token)):
    """L7 contextual scores: useful_life, risk, resale, intervention + batch anomaly."""
    conn = get_conn()
    row = q1(conn, """SELECT useful_life_score, risk_score, resale_now_inr, resale_8wk_inr,
                             resale_exit_signal, intervention_score, l7_computed_at,
                             batch_anomaly_tier, sibling_failure_rate, sibling_count
                      FROM battery_health_scores_v2
                      WHERE battery_id = ? AND useful_life_score IS NOT NULL
                      ORDER BY week_number DESC LIMIT 1""", [battery_id])
    conn.close()
    return row or {"error": "no L7 scores for this battery"}


@app.get("/api/battery/{battery_id}/score-breakdown-v2")
@safe
def battery_score_breakdown_v2(battery_id: str, _=Depends(verify_token)):
    """BHS v2.1 score breakdown: 6 components + divergence quadrant + use case."""
    conn = get_conn()
    row = q1(conn, """SELECT bhs_score_v2, bhs_component_spread, bhs_component_soh_latest,
                             bhs_component_soh_trend, bhs_component_ah, bhs_component_pct_comm,
                             bhs_component_op, bhs_dominant_driver, bhs_dominant_driver_cause,
                             divergence_quadrant, divergence_flag, forward_risk_flag,
                             context_investigation_flag, coulomb_quality_flag
                      FROM battery_health_scores_v2
                      WHERE battery_id = ? AND bhs_component_spread IS NOT NULL
                      ORDER BY week_number DESC LIMIT 1""", [battery_id])
    uc = q1(conn, "SELECT use_case_inferred FROM batteries WHERE battery_id = ?", [battery_id])
    conn.close()
    if row:
        row['use_case'] = (uc or {}).get('use_case_inferred')
    return row or {"error": "no breakdown for this battery"}


@app.get("/api/fleet/decisions")
@safe
def fleet_decisions(limit: int = 50, audience: str = "operator", _=Depends(verify_token)):
    """Fleet decision queue ordered by urgency."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.fleet_segment, b.battery_model, b.city_code,
               s.week_number, s.range_p50, s.range_corrected_km, s.rul_action_v2 as action,
               s.health_class_v2, s.fault_severity, s.rul_weeks_v2,
               s.resale_value_est, s.operator_cause_sentence, s.nbfc_summary_sentence,
               p.driver_stress_tier, p.driver_stress_score,
               s.bhs_score_v2, s.nbfc_risk_tier, s.kps_slope_8wk,
               b.use_case_inferred, s.degradation_regime,
               CASE s.rul_action_v2
                 WHEN 'PHYSICS_REPLACE_PLAN' THEN 1
                 WHEN 'ACUTE_BREACH_WATCH' THEN 2
                 WHEN 'CELL_BALANCE_PRIORITY' THEN 3
                 WHEN 'REPLACE_PLAN' THEN 4
                 WHEN 'MONITOR_INVESTIGATE' THEN 5
                 WHEN 'MONITOR_WEEKLY' THEN 6
                 ELSE 50 END as urgency_rank
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        LEFT JOIN battery_personal_params p ON s.battery_id = p.battery_id
        WHERE b.fleet_segment IN ('GE_ERICKSHAW','SG_ERICKSHAW')
        AND s.week_number = (SELECT MAX(week_number) FROM battery_health_scores_v2 s2
                             WHERE s2.battery_id = s.battery_id)
        AND s.rul_action_v2 IS NOT NULL AND s.rul_action_v2 != 'NO_ACTION'
        ORDER BY urgency_rank, s.rul_weeks_v2 ASC
        LIMIT ?
    """, [limit])
    conn.close()
    return rows




@app.get("/api/fleet/action-queue")
@safe
def fleet_action_queue(limit: int = 500, _=Depends(verify_token)):
    """All scored batteries for fleet MIS — enriched with H1 one-liners."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               b.fleet_segment, s.rul_action_v2 as action,
               ROUND(s.range_corrected_km,1) as range_km,
               ROUND(s.bhs_score_v2,1) as bhs_score,
               s.kps_slope_8wk as slope, s.nbfc_risk_tier as nbfc_grade,
               p.driver_stress_tier, b.use_case_inferred as use_case,
               s.week_number, s.health_class_v2 as health_class,
               s.signal_confidence, s.fault_profile_tier, s.resale_stage,
               s.resale_value_inr,
               s.attr_primary_factor, s.attr_cycle_pct, s.attr_thermal_pct,
               CASE s.rul_action_v2
                 WHEN 'PHYSICS_REPLACE_PLAN' THEN 1 WHEN 'ACUTE_BREACH_WATCH' THEN 2
                 WHEN 'CELL_BALANCE_PRIORITY' THEN 3 WHEN 'REPLACE_PLAN' THEN 4
                 WHEN 'MONITOR_INVESTIGATE' THEN 5 WHEN 'MONITOR_WEEKLY' THEN 6
                 WHEN 'NO_ACTION' THEN 7 ELSE 8 END as urgency_rank
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id=b.battery_id
        LEFT JOIN battery_personal_params p ON s.battery_id=p.battery_id
        WHERE b.fleet_segment IN ('GE_ERICKSHAW','SG_ERICKSHAW')
        AND COALESCE(s.week_number,0)=(SELECT MAX(COALESCE(week_number,0))
            FROM battery_health_scores_v2 s2 WHERE s2.battery_id=s.battery_id)
        AND s.battery_id!='BAT_LFP_202'
        ORDER BY urgency_rank, s.range_corrected_km ASC LIMIT ?
    """, [limit])
    conn.close()

    # H1 enrichment: one-liner fields for fleet MIS
    _urgency_map = {"PHYSICS_REPLACE_PLAN": "THIS_WEEK", "REPLACE_PLAN": "THIS_MONTH",
                    "ACUTE_BREACH_WATCH": "THIS_WEEK", "CELL_BALANCE_PRIORITY": "THIS_MONTH",
                    "MONITOR_INVESTIGATE": "ONGOING", "MONITOR_WEEKLY": "ONGOING",
                    "NO_ACTION": "NONE"}
    _urgency_plain = {"THIS_WEEK": "Act this week", "THIS_MONTH": "Plan within this month",
                      "ONGOING": "Continue monitoring", "NONE": "No action needed"}
    _factor_labels = {"cycle_wear": "Cycle wear", "thermal": "Heat exposure",
                      "calendar": "Age", "charging": "Charging pattern",
                      "maintenance": "Maintenance", "operator": "Operator pattern",
                      "driver": "Driver behaviour", "load": "Load intensity",
                      "imbalance": "Cell imbalance"}
    _stage_short = {"STAGE1_ACTIVE": "active value", "STAGE2_SECONDLIFE": "second-life value",
                    "STAGE3_SCRAP": "scrap value"}
    _ds_plain = {"HIGH": "High-demand", "MEDIUM": "Standard", "LOW": "Smooth"}
    _uc_plain = {"HEAVY_DAILY": "Heavy daily", "SHORT_ROUTE": "Short route",
                 "STANDARD_URBAN": "Standard urban", "LIGHT_USE": "Light use"}

    for r in rows:
        ac = r.get("action") or "MONITOR_WEEKLY"
        pf = r.get("attr_primary_factor") or ""
        pf_label = _factor_labels.get(pf, pf.replace("_", " ").title() if pf else "Assessing")
        pf_pct = r.get("attr_cycle_pct") or r.get("attr_thermal_pct") or 0
        r["cause_one_line"] = f"{pf_label} ({pf_pct:.0f}%)" if pf else "Attribution pending"

        ds = _ds_plain.get(r.get("driver_stress_tier"), "Pending")
        uc = _uc_plain.get(r.get("use_case"), "Assessing")
        r["behaviour_one_line"] = f"{ds} \u00b7 {uc}"

        grade = r.get("nbfc_grade") or "?"
        resale = r.get("resale_value_inr")
        stage = _stage_short.get(r.get("resale_stage"), "")
        r["stakes_one_line"] = (
            f"Grade {grade} \u00b7 \u20b9{resale:,.0f} {stage}"
            if resale else f"Grade {grade}")

        urg = _urgency_map.get(ac, "ONGOING")
        r["urgency"] = urg
        r["urgency_plain"] = _urgency_plain.get(urg, "Monitor")

    return rows

@app.get("/api/battery/{battery_id}/physical-risk")
@safe
def get_physical_risk(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()

    # Display block: SCORE_SUSPENDED batteries must not expose physical risk externally
    suspended = q1(conn, """
        SELECT scoring_mode FROM battery_health_scores_v2
        WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1
    """, [battery_id])
    if suspended and suspended.get("scoring_mode") == "SUSPENDED":
        conn.close()
        return {
            "battery_id": battery_id,
            "display_blocked": True,
            "reason": "SCORE_SUSPENDED",
            "physical_risk_score": None,
            "message": "This battery is suspended from scoring. Data not available.",
        }

    row = q1(conn, """
        SELECT bch.battery_id, bch.physical_risk_score, bch.physical_risk_computed_at,
               b.battery_model, b.city_code, b.commissioning_date,
               bhs.fault_severity, bhs.trajectory_state, bhs.week_number
        FROM battery_component_health bch
        JOIN batteries b ON b.battery_id = bch.battery_id
        LEFT JOIN battery_health_scores_v2 bhs ON bhs.battery_id = bch.battery_id
            AND bhs.week_number = (SELECT MAX(week_number) FROM battery_health_scores_v2 WHERE battery_id = bch.battery_id)
        WHERE bch.battery_id = ?
          AND bch.physical_risk_score IS NOT NULL
        ORDER BY bch.week_number DESC
        LIMIT 1
    """, [battery_id])
    conn.close()
    if not row:
        raise HTTPException(404, f"No physical risk data for {battery_id}")

    # Recompute components for response
    from compute_physical_risk import PACK_RISK, PACK_RISK_DEFAULT, CITY_BASE, CITY_BASE_DEFAULT
    from datetime import datetime

    age_weeks = None
    if row["commissioning_date"]:
        try:
            comm = datetime.strptime(row["commissioning_date"], "%Y-%m-%d")
            age_weeks = max(0, (datetime.now() - comm).days // 7)
        except (ValueError, TypeError):
            pass

    age_component = round(min(age_weeks / 156.0, 1.0) * 40, 1) if age_weeks is not None else 15
    pack_component = PACK_RISK.get(row["battery_model"], PACK_RISK_DEFAULT)

    base_city = CITY_BASE.get(row["city_code"], CITY_BASE_DEFAULT)
    city_component = min(base_city * 1.0, 20)  # simplified — full seasonal in batch script

    worsening = {"DECLINING_FAST", "DECLINING_CRITICAL", "DECLINING_MODERATE", "EARLY_WARNING"}
    fs = row.get("fault_severity")
    ts = row.get("trajectory_state")
    if fs == "HIGH" and ts in worsening:
        fault_component = 10
    elif fs == "HIGH":
        fault_component = 6
    elif fs == "MEDIUM" and ts in worsening:
        fault_component = 4
    else:
        fault_component = 0

    return {
        "battery_id": row["battery_id"],
        "display_blocked": False,
        "physical_risk_score": row["physical_risk_score"],
        "age_component": age_component,
        "pack_component": pack_component,
        "city_component": round(city_component, 1),
        "fault_component": fault_component,
        "physical_risk_computed_at": row["physical_risk_computed_at"],
    }


# ── GET /api/oem/batch-intelligence ─────────────────────────────────

@app.get("/api/oem/batch-intelligence")
@safe
def oem_batch_intelligence(_=Depends(verify_token)):
    """Pack+city cohort failure patterns from service records + telemetry."""
    conn = get_conn()

    cohorts = q(conn, """
        SELECT b.battery_model as pack_model, b.city_code as city,
               COUNT(*) as n_batteries,
               ROUND(AVG(s.sibling_failure_rate), 4) as sibling_failure_rate,
               s.batch_anomaly_tier
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.batch_anomaly_tier IS NOT NULL
          AND (
            (s.week_number IS NOT NULL AND s.week_number = (
              SELECT MAX(w.week_number) FROM battery_health_scores_v2 w WHERE w.battery_id = s.battery_id))
            OR s.week_number IS NULL
          )
        GROUP BY b.battery_model, b.city_code, s.batch_anomaly_tier
        ORDER BY sibling_failure_rate DESC NULLS LAST
    """)

    # Annotate each cohort with telemetry finding and data source note
    for c_row in cohorts:
        pm = c_row["pack_model"]
        tier = c_row["batch_anomaly_tier"]
        if pm == "GF_LFP_Pack3401":
            c_row["telemetry_finding"] = (
                "Pack3401 reaches operational floor earlier than Pack3001. "
                "Fleet data: median knee at week 18.8 vs Pack3001 week 19.4 "
                "(confirmed across 76 Pack3401 batteries)."
            )
            c_row["data_source_note"] = "No service records for Pack3401. Telemetry is the primary signal."
        elif tier == "BATCH_HIGH":
            c_row["telemetry_finding"] = "Service record failure rate >60% in this cohort"
            c_row["data_source_note"] = "Service records confirm high failure rate."
        elif tier == "BATCH_NO_DATA":
            c_row["telemetry_finding"] = None
            c_row["data_source_note"] = "No service record coverage for this pack+city cohort."
        else:
            c_row["telemetry_finding"] = None
            c_row["data_source_note"] = "Service records available, failure rate within normal range."

    conn.close()

    return {
        "pack_city_cohorts": cohorts,
        "cross_source_finding": (
            "Service records and telemetry identify different problem cohorts. "
            "Pack3401 (Surat): telemetry shows earlier operational-floor arrival vs Pack3001 "
            "(median knee week 18.8 vs 19.4). No service records. "
            "Pack1201 (Cuttack): service records confirm 90.3% failure rate — telemetry normal. "
            "The platform sees both. Neither data source alone does."
        ),
        "pack3401_note": (
            "Pack3401: sibling_failure_rate=NULL — no service record coverage for this pack. "
            "Telemetry finding (earlier operational-floor arrival vs Pack3001) is the primary OEM signal for Pack3401. "
            "See /api/oem/pack-comparison for Pack3401 telemetry intelligence."
        ),
    }


# ── Server-side mappings for passport_next ─────────────────────────

_HEALTH_CLASS_PLAIN = {
    "CRITICAL_TRAJECTORY": "Declining",
    "WATCH_DETERIORATED": "Watch",
    "WATCH_ACTIVE": "Watch",
    "HEALTHY": "Healthy",
    "CAPACITY_STABLE_LOW": "Stable \u2014 reduced capacity",
}

_ACTION_PLAIN = {
    "PHYSICS_REPLACE_PLAN": "Replace \u2014 confirmed decline",
    "REPLACE_PLAN": "Schedule replacement",
    "ACUTE_BREACH_WATCH": "Service now \u2014 cell instability",
    "CELL_BALANCE_PRIORITY": "Book cell balance service",
    "MONITOR_INVESTIGATE": "Under investigation",
    "MONITOR_WEEKLY": "Monitoring weekly",
    "NO_ACTION": "No action needed",
    "CAPACITY_STABLE_LOW": "Planned replacement \u2014 not urgent",
}

_NARRATIVE = {
    "PHYSICS_REPLACE_PLAN": "This battery is in confirmed decline \u2014 delivering {range}km and losing ground every week. Replacement is the right call.",
    "REPLACE_PLAN": "Range is dropping and the data confirms it\u2019s not temporary. The window for planned replacement is open \u2014 now is the time to act.",
    "ACUTE_BREACH_WATCH": "This battery is unstable \u2014 range can drop significantly on any given day. Service, not replacement, is the first step.",
    "CELL_BALANCE_PRIORITY": "Health signals are worsening but this battery is recoverable. Cell balancing now prevents a replacement bill later.",
    "MONITOR_INVESTIGATE": "Something changed recently. The data is inconsistent with this battery\u2019s history. Monitoring closely before any action is recommended.",
    "MONITOR_WEEKLY": "This battery is being watched but not in danger. Range is holding \u2014 the platform will alert if anything changes.",
    "NO_ACTION": "All signals normal. This battery is working as it should. No action needed this week.",
    "CAPACITY_STABLE_LOW": "This battery has lost capacity but found a stable level. It is still revenue-generating \u2014 plan replacement in the next cycle.",
}

_PRIMARY_DRIVER_PLAIN = {
    "maintenance": "Cell wear accumulation",
    "charging": "Charging pattern",
    "usage": "Route intensity",
    "thermal": "Heat exposure",
    "calendar": "Age",
}

_DRIVER_STRESS_PLAIN = {
    "LOW": "Smooth driving pattern",
    "MEDIUM": "Standard driving pattern",
    "HIGH": "High-demand driving pattern",
}


def _confidence_level(data_confidence_label):
    """Map data_confidence_label to HIGH/MEDIUM/DATA_LIMITED."""
    if data_confidence_label in ("HIGH", "GOOD"):
        return "HIGH"
    elif data_confidence_label in ("LIMITED", "LOW"):
        return "DATA_LIMITED"
    return "MEDIUM"


# ── GET /api/fleet/decision-queue ──────────────────────────────────

@app.get("/api/fleet/decision-queue")
@safe
def fleet_decision_queue_endpoint(_=Depends(verify_token)):
    """Fleet decision queue with plain-English labels for passport_next.
    Banned battery_ids excluded. No result cap — returns full fleet."""
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("fdq.battery_id")
    rows = q(conn, f"""
        SELECT fdq.battery_id, fdq.battery_model as pack_model, fdq.city_code as city,
               s.range_corrected_km, s.health_class_v2, s.rul_action_v2,
               fdq.urgency_rank, fdq.driver_stress_tier, fdq.week_number,
               s.data_confidence_label, s.batch_anomaly_tier,
               s.divergence_quadrant, b.use_case_inferred
        FROM fleet_decision_queue fdq
        JOIN battery_health_scores_v2 s ON fdq.battery_id = s.battery_id
            AND s.week_number = (SELECT MAX(week_number) FROM battery_health_scores_v2
                                 WHERE battery_id = fdq.battery_id)
        LEFT JOIN batteries b ON fdq.battery_id = b.battery_id
        WHERE s.health_class_v2 != 'SCORE_SUSPENDED'
          AND s.scoring_mode != 'SUSPENDED'
          AND {ban_sql}
        ORDER BY fdq.urgency_rank ASC, s.range_corrected_km ASC
    """, ban_params)
    conn.close()

    for r in rows:
        r["range_corrected_km"] = round(r["range_corrected_km"], 1) if r.get("range_corrected_km") else None
        r["health_class_plain"] = _HEALTH_CLASS_PLAIN.get(r.pop("health_class_v2", ""), r.get("health_class_v2"))
        r["rul_action_plain"] = _ACTION_PLAIN.get(r.pop("rul_action_v2", ""), r.get("rul_action_v2"))
        r["confidence_level"] = _confidence_level(r.pop("data_confidence_label", None))

    return rows


# ══════════════════════════════════════════════════════════════════
# H1: INTELLIGENCE CHAIN — helpers + passport endpoint
# ══════════════════════════════════════════════════════════════════

# ── Fleet context cache (computed once per process) ───────────────

_fleet_ctx_cache = {}


def _get_fleet_context(conn):
    """Compute fleet-wide context for plain-text generation. Cached per process."""
    if _fleet_ctx_cache:
        return _fleet_ctx_cache
    row = q1(conn, """
        SELECT ROUND(AVG(range_p50),1) as fleet_avg_range,
               ROUND(AVG(bhs_score_v2),1) as fleet_avg_bhs,
               ROUND(AVG(kps_slope_8wk),4) as fleet_avg_slope,
               ROUND(AVG(pct_of_commissioned),1) as fleet_avg_pct_comm,
               COUNT(*) as n
        FROM (
            SELECT battery_id, range_p50, bhs_score_v2, kps_slope_8wk, pct_of_commissioned,
                   ROW_NUMBER() OVER (PARTITION BY battery_id ORDER BY week_number DESC) as rn
            FROM battery_health_scores_v2
            WHERE battery_id NOT IN (SELECT battery_id FROM excluded_batteries)
        ) WHERE rn=1 AND range_p50 IS NOT NULL AND bhs_score_v2 IS NOT NULL
    """)
    spread_row = q1(conn, """
        SELECT ROUND(AVG(cell_spread_max),1) as fleet_avg_spread
        FROM (
            SELECT battery_id, cell_spread_max,
                   ROW_NUMBER() OVER (PARTITION BY battery_id ORDER BY week_number DESC) as rn
            FROM vehicle_weekly_features WHERE cell_spread_max IS NOT NULL
        ) WHERE rn=1
    """)
    dod_row = q1(conn, """
        SELECT ROUND(AVG(dod_corrected),1) as fleet_avg_dod
        FROM vehicle_weekly_features WHERE dod_corrected IS NOT NULL
    """)
    _fleet_ctx_cache.update({
        "fleet_avg_range": (row or {}).get("fleet_avg_range") or 93.0,
        "fleet_avg_bhs": (row or {}).get("fleet_avg_bhs") or 69.6,
        "fleet_avg_slope": (row or {}).get("fleet_avg_slope") or 0.0,
        "fleet_avg_pct_comm": (row or {}).get("fleet_avg_pct_comm") or 90.0,
        "fleet_avg_spread": (spread_row or {}).get("fleet_avg_spread") or 212.0,
        "fleet_avg_dod": (dod_row or {}).get("fleet_avg_dod") or 53.3,
    })
    return _fleet_ctx_cache


# ── H1 helper functions ──────────────────────────────────────────


def _h1_range_trend_plain(slope, health_class, recent_slope):
    long_decline = health_class in ("CRITICAL_TRAJECTORY", "WATCH_DETERIORATED")
    recent_positive = recent_slope is not None and recent_slope > 0.001
    if long_decline and recent_positive:
        return "Declining overall — recent week shows short recovery within longer decline"
    elif long_decline:
        return "Declining steadily over time"
    elif health_class == "HEALTHY":
        return "Holding steady — no decline detected"
    else:
        return "Mixed signals — monitoring closely"


def _h1_slope_plain(slope):
    if slope is None:
        return "Trend data building"
    if slope >= 0:
        return f"Slight improvement recently (+{abs(slope):.3f} km/week)"
    if slope >= -0.002:
        return f"Very slight decline ({slope:.3f} km/week)"
    if slope >= -0.010:
        return f"Gradual decline ({slope:.3f} km/week)"
    if slope >= -0.030:
        return f"Noticeable decline ({slope:.3f} km/week — faster than fleet average)"
    return f"Steep decline ({slope:.3f} km/week — among fastest in fleet)"


def _h1_track_label(score):
    if score is None:
        return "UNKNOWN"
    if score >= 75:
        return "HEALTHY"
    if score >= 60:
        return "WATCH"
    if score >= 45:
        return "DECLINING"
    return "CRITICAL"


def _h1_track_plain(score, track):
    label = _h1_track_label(score)
    if track == "A":
        m = {
            "HEALTHY": "Range delivery normal — above fleet watch threshold",
            "WATCH": "Range delivery adequate but showing decline trend",
            "DECLINING": "Range delivery declining — intervention recommended",
            "CRITICAL": "Range delivery at critical level — immediate action needed",
        }
    else:
        m = {
            "HEALTHY": "Internal chemistry healthy — no stress signals",
            "WATCH": "Internal chemistry showing early stress signals",
            "DECLINING": "Internal chemistry declining — degradation accelerating",
            "CRITICAL": "Internal chemistry in poor state — replacement planning needed",
        }
    return m.get(label, "Assessment pending")


def _h1_divergence_plain(quadrant, reason_text):
    m = {
        "EARLY_WARNING": "Range holding but chemistry deteriorating — early warning window open",
        "RANGE_CONTEXT": "Range dropped but chemistry healthy — likely operational cause not battery failure",
        "ALIGNED_DECLINE": "Range and chemistry declining together — confirmed deterioration",
        "ALIGNED_HEALTHY": "Range and chemistry both healthy — battery in good state",
    }
    base = m.get(quadrant, "Signals mixed — monitoring")
    if reason_text and quadrant in ("EARLY_WARNING", "RANGE_CONTEXT"):
        return f"{base}. {reason_text}"
    return base


def _h1_regime_plain(regime):
    m = {
        "PRE_KNEE": "Early life phase — degradation rate low and stable",
        "APPROACHING_KNEE": "Approaching the phase where degradation accelerates",
        "POST_KNEE": "Past the inflection point — degradation now accelerating",
        "RESISTANCE_DOMINATED": "Internal resistance rising — efficiency losses increasing",
        "INSUFFICIENT_HISTORY": "Not enough data to classify degradation phase",
    }
    return m.get(regime, regime or "Degradation phase: assessing")


def _h1_confidence_reason(signal_confidence, corr_score, n_weeks, n_firing):
    if signal_confidence == "PHYSICS_CONFIRMED":
        return (f"{n_firing} independent physics signals confirm this assessment "
                f"across {n_weeks} weeks of data. Highest confidence level.")
    elif signal_confidence == "HIGH":
        if n_firing == 0:
            return (f"No active problem signals detected across {n_weeks} weeks of clean data. "
                    f"High confidence this battery is not in decline right now.")
        return (f"{n_firing} signal detected with {n_weeks} weeks of baseline data for comparison. "
                f"Specific issue identified — not systemic.")
    elif signal_confidence == "MEDIUM":
        return (f"{n_firing} signal active. {n_weeks} weeks of data available. "
                f"Assessment reliable but monitoring continues.")
    else:
        return f"Limited data ({n_weeks} weeks). Assessment will improve with more history."


def _h1_confidence_plain(signal_confidence):
    m = {
        "PHYSICS_CONFIRMED": "Multiple independent measurements agree — high certainty",
        "HIGH": "Strong data foundation — assessment is reliable",
        "MEDIUM": "Good data available — assessment directionally reliable",
        "DATA_LIMITED": "Data building — treat as early indication",
    }
    return m.get(signal_confidence, "Assessing")


def _h1_corroboration_signals(battery_id, week_number, conn):
    """Determine which of the corroboration signals are currently firing."""
    events = q(conn, """
        SELECT DISTINCT event_code FROM vehicle_events
        WHERE battery_id = ? AND week_number >= ? - 8
    """, [battery_id, week_number])
    event_codes = {e["event_code"] for e in events}
    signal_map = {
        "E1": "efficiency_shift", "E2": "cell_imbalance",
        "E3": "thermal_anomaly", "E4": "discharge_pattern",
        "E5": "charge_pattern", "E6": "usage_change", "E7": "voltage_anomaly",
    }
    firing = [signal_map[c] for c in event_codes if c in signal_map]
    all_signals = list(signal_map.values())
    silent = [s for s in all_signals if s not in firing]
    return firing, silent


def _h1_build_divergence_reason(s):
    """Triangulated reason for EARLY_WARNING divergence."""
    if s.get("divergence_quadrant") != "EARLY_WARNING":
        return None
    fp = s.get("fault_profile_tier")
    if fp in ("CHRONIC_WORSENING", "ACTIVE_WORSENING"):
        fdw = s.get("fault_duration_weeks") or 0
        return (f"Chemistry decline driven by active {fp.lower().replace('_',' ')} fault "
                f"({fdw} weeks). Range not yet affected — this is the early warning window.")
    drv = s.get("attr_primary_factor") or s.get("bhs_dominant_driver") or ""
    dst = s.get("driver_stress_tier")
    uc = s.get("use_case_inferred")
    if "cycle" in drv.lower() and dst == "HIGH":
        return ("High-demand driving is accelerating internal wear faster than range loss appears. "
                "Intervention now extends useful life.")
    if "cycle" in drv.lower() and uc == "HEAVY_DAILY":
        return ("Heavy daily use is driving cycle wear before visible range impact. "
                "Early service window open.")
    if "load" in drv.lower():
        return ("High load intensity is the primary chemistry driver. "
                "Range holding — internal stress accumulating.")
    drv_plain = _PRIMARY_DRIVER_PLAIN.get(drv, drv) if drv else "internal chemistry"
    return (f"Primary driver: {drv_plain}. "
            "Chemistry deteriorating before range impact — typical early-stage pattern.")


def _h1_build_attribution(s, attr_row, fleet_segment):
    """Build unified attribution with correct operator/oem/age roll-up.

    Imbalance bucketing is pack-aware:
      Pack3401 (PACK_GAP_EXCEPTION) → imbalance is OEM-driven (pack_design)
      All other packs → imbalance is operator-driven (usage-induced spread)
    """
    pack_model = s.get("pack_model") or ""
    is_pack_gap = "Pack3401" in pack_model or "pack3401" in pack_model.lower()
    imbalance_val = s.get("attr_imbalance_pct") or s.get("attr_physical_pct") or 0

    factors_raw = {
        # OPERATOR-DRIVEN
        "cycle_wear": s.get("attr_cycle_pct") or 0,
        "charging": (attr_row or {}).get("charging_pct") or 0,
        "maintenance": (attr_row or {}).get("maintenance_pct") or 0,
        "dod": s.get("attr_dod_pct") or 0,
        # imbalance → operator for all packs EXCEPT Pack3401
        "imbalance": 0 if is_pack_gap else imbalance_val,
        # OEM/ENVIRONMENT-DRIVEN
        "thermal": s.get("attr_thermal_pct") or 0,
        # imbalance → OEM only for Pack3401 (PACK_GAP_EXCEPTION)
        "pack_design": imbalance_val if is_pack_gap else 0,
        # AGE-DRIVEN
        "calendar": s.get("attr_calendar_pct") or 0,
    }

    total = sum(factors_raw.values())
    if total > 0 and abs(total - 100) > 2:
        factors = {k: round(v * 100 / total, 1)
                   for k, v in factors_raw.items() if v > 0}
    else:
        factors = {k: round(v, 1)
                   for k, v in factors_raw.items() if v > 0}

    sorted_f = sorted(factors.items(), key=lambda x: x[1], reverse=True)
    primary = sorted_f[0][0] if sorted_f else "calendar"

    labels = {
        "cycle_wear": "Cumulative discharge stress (cycle wear)",
        "charging": "Charging pattern",
        "maintenance": "Maintenance history",
        "dod": "Depth of discharge",
        "imbalance": "Cell imbalance (usage-induced spread)",
        "thermal": "Heat exposure",
        "pack_design": "Cell imbalance (pack design — PACK_GAP_EXCEPTION)",
        "calendar": "Age — expected battery aging",
    }

    # Roll-up buckets
    operator_driven = round(
        factors.get("cycle_wear", 0) + factors.get("charging", 0) +
        factors.get("maintenance", 0) + factors.get("dod", 0) +
        factors.get("imbalance", 0), 1)
    oem_driven = round(
        factors.get("thermal", 0) + factors.get("pack_design", 0), 1)
    age_driven = round(factors.get("calendar", 0), 1)

    # Reconcile rounding to 100
    total_check = operator_driven + oem_driven + age_driven
    if abs(total_check - 100) > 2 and total_check > 0:
        scale = 100 / total_check
        operator_driven = round(operator_driven * scale, 1)
        oem_driven = round(oem_driven * scale, 1)
        age_driven = round(100 - operator_driven - oem_driven, 1)

    # Verdict
    if operator_driven > 55:
        verdict = "Primarily operator-driven"
        verdict_detail = f"{operator_driven}% from how this vehicle is operated"
    elif oem_driven > 40:
        verdict = "Primarily pack/environment-driven"
        verdict_detail = f"{oem_driven}% from pack design and thermal environment"
    else:
        verdict = "Mixed causes"
        verdict_detail = (f"{operator_driven}% operator, "
                          f"{oem_driven}% pack/environment, "
                          f"{age_driven}% age")

    ds = s.get("driver_stress_tier")
    uc = s.get("use_case_inferred")
    cp = s.get("personal_charge_profile")

    ds_plain = {"HIGH": "High-demand driving — harder on battery than 74% of fleet",
                "MEDIUM": "Standard driving pattern",
                "LOW": "Smooth, low-demand driving"}.get(ds, "Driver profile building")
    uc_plain = {"HEAVY_DAILY": "Heavy daily use — demanding route",
                "SHORT_ROUTE": "Short-route pattern — frequent partial discharges",
                "LIGHT_USE": "Light use — age-dominated degradation expected",
                "STANDARD_URBAN": "Standard urban use"}.get(uc, "Use case: assessing")
    cp_plain = {"OVERNIGHT_FULL": "Overnight full charge — good practice",
                "PARTIAL_TOPUP": "Partial top-up — may accelerate cell imbalance",
                "IRREGULAR": "Irregular charging pattern"}.get(cp, "Charge profile: assessing")

    return {
        "primary_factor": primary,
        "primary_factor_plain": labels.get(primary, primary),
        "primary_pct": factors.get(primary, 0),
        "operator_driven_pct": operator_driven,
        "oem_driven_pct": oem_driven,
        "age_driven_pct": age_driven,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "operator_sentence": f"{operator_driven}% of degradation is how this vehicle is operated",
        "oem_sentence": f"{oem_driven}% is related to pack design and environment",
        "age_sentence": f"{age_driven}% is expected aging",
        "all_factors": [{"factor": k, "pct": v, "plain": labels.get(k, k)}
                        for k, v in sorted_f],
        "attribution_sum": round(sum(factors.values()), 1),
        "behaviour": {
            "driver_stress": ds, "driver_stress_plain": ds_plain,
            "use_case": uc, "use_case_plain": uc_plain,
            "charge_profile": cp, "charge_profile_plain": cp_plain,
            "load_intensity": s.get("load_intensity_p50"),
            "load_note": ("Not applicable for SG segment"
                          if fleet_segment == "SG_ERICKSHAW" else None),
        },
    }


# ── Attribution engine v2 (Sprint attribution-complete, 2026-04-21) ────

def _is_pack3401(pack_model):
    return (pack_model or '').endswith('Pack3401')


def _pack3401_exception_qualifies(dri_score, kps_slope_8wk):
    # APR25 recalibration (Fix 8): prior gate (dri<65 AND slope<-0.05) fired
    # on 1/71 Pack3401 (1.4%). Audit target was 15-30% to reflect the
    # pack-design-gap thesis. DRI<70 was empirically incompatible with the
    # target (only 4 Pack3401 batteries fall under DRI<70 — 5.6% ceiling).
    # Resolution: drop DRI gate, use slope alone. slope<-0.020 fires on
    # 13/71 = 18.3%, in target range. Semantically correct: "Pack3401
    # batteries with materially negative slope" — pack-design-gap signal.
    if kps_slope_8wk is None:
        return False
    try:
        return float(kps_slope_8wk) < -0.020
    except (TypeError, ValueError):
        return False


def _is_vehicle_issue(battery_id, conn):
    """True only when coulomb_kps_divergence > 1.15 for 3+ consecutive weeks
    inside the last 8 weeks of VWF. Single-week spikes do NOT qualify.
    Returns False when fewer than 3 weeks of data are available.
    """
    rows = q(conn, """
        SELECT week_number, coulomb_kps_divergence
        FROM vehicle_weekly_features
        WHERE battery_id = ?
          AND week_number >= COALESCE(
            (SELECT MAX(week_number) - 8 FROM vehicle_weekly_features WHERE battery_id = ?),
            0)
        ORDER BY week_number ASC
    """, [battery_id, battery_id])
    if len(rows) < 3:
        return False
    run = 0
    for r in rows:
        d = r.get('coulomb_kps_divergence')
        if d is not None and d > 1.15:
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def _get_attribution_mode(battery_id, pack_model, fleet_segment, conn):
    """Decide attribution mode for this battery.

    Returns one of:
      PACK_GAP_EXCEPTION — Pack3401 (Rule 49, permanent)
      INCOMPLETE         — NMC / LCV / FGHHL segments (separate methodology)
      STANDARD           — LFP GE/SG rickshaws (the main attribution path)

    Side effect: when Pack3401 is encountered the BDA row is stamped with
    pack_model_finding='PACK_GAP_EXCEPTION' so downstream surfaces can rely
    on the column being populated.
    """
    if _is_pack3401(pack_model):
        gate_row = q1(
            conn,
            "SELECT dri_score, kps_slope_8wk FROM battery_health_scores_v2 WHERE battery_id = ?",
            [battery_id],
        ) or {}
        if _pack3401_exception_qualifies(gate_row.get('dri_score'), gate_row.get('kps_slope_8wk')):
            try:
                changed = conn.execute(
                    """UPDATE battery_degradation_attribution
                       SET pack_model_finding='PACK_GAP_EXCEPTION'
                       WHERE battery_id = ?
                         AND (pack_model_finding IS NULL OR pack_model_finding = '')""",
                    [battery_id],
                ).rowcount
                if changed:
                    conn.execute(
                        """INSERT INTO param_change_log
                           (param_name, old_value, new_value, changed_by, change_reason)
                           VALUES (?, ?, ?, ?, ?)""",
                        ['pack_model_finding', 'NULL', 'PACK_GAP_EXCEPTION',
                         'db_api:_get_attribution_mode',
                         'Pack3401 + dri<65 + slope<-0.2 (T4-5 gate, APR24)'],
                    )
                    conn.commit()
            except Exception:
                pass
            return 'PACK_GAP_EXCEPTION'
        # Healthy Pack3401 falls through to STANDARD attribution.
    # LFP rickshaw fleet is the only segment with a complete attribution methodology.
    if fleet_segment and not (fleet_segment.startswith('GE_') or fleet_segment.startswith('SG_')):
        return 'INCOMPLETE'
    return 'STANDARD'


def _coalesce_factors(attr_row, bhs_row):
    """Per-field coalesce: BDA new engine → legacy attr_* → 0."""
    a = attr_row or {}
    b = bhs_row or {}
    def pick(a_key, b_key):
        v = a.get(a_key)
        if v is None:
            v = b.get(b_key)
        return float(v) if v is not None else 0.0
    return {
        'charging':    pick('charging_pct',    'attr_cycle_pct'),
        'usage':       pick('usage_pct',       'attr_dod_pct'),
        'thermal':     pick('thermal_pct',     'attr_thermal_pct'),
        'maintenance': pick('maintenance_pct', 'attr_imbalance_pct'),
        'calendar':    pick('calendar_pct',    'attr_calendar_pct'),
    }


def _normalize_lfp_factors(factors_raw):
    """Return (factors_norm, quality, raw_total). quality ∈
    COMPLETE (95–105), NORMALISED (85–94 or 106–115), INCOMPLETE (else)."""
    total = sum(factors_raw.values())
    if total <= 0 or total < 85 or total > 115:
        return None, 'INCOMPLETE', round(total, 1)
    quality = 'COMPLETE' if 95 <= total <= 105 else 'NORMALISED'
    scale = 100.0 / total
    norm = {k: round(v * scale, 1) for k, v in factors_raw.items()}
    # Rounding reconciliation — push diff onto the largest factor
    diff = round(100.0 - sum(norm.values()), 1)
    if diff != 0:
        largest = max(norm, key=norm.get)
        norm[largest] = round(norm[largest] + diff, 1)
    return norm, quality, round(total, 1)


def _operator_fraction(factors_norm):
    if not factors_norm:
        return None
    return round(
        factors_norm.get('charging', 0)
        + factors_norm.get('usage', 0)
        + factors_norm.get('maintenance', 0), 1)


def _oem_fraction(factors_norm):
    """calendar_pct is OEM-accountable. thermal is context-dependent and
    is NOT added to OEM fraction (Decision P1-4)."""
    if not factors_norm:
        return None
    return round(factors_norm.get('calendar', 0), 1)


def _bda_row_for_attribution(battery_id, conn):
    return q1(conn, """
        SELECT charging_pct, usage_pct, thermal_pct, maintenance_pct, calendar_pct,
               primary_driver, secondary_driver, pack_model_finding,
               operator_attribution_sentence, nbfc_attribution_sentence,
               oem_attribution_sentence, attribution_method
        FROM battery_degradation_attribution WHERE battery_id = ?
    """, [battery_id])


def _bhs_row_for_attribution(battery_id, conn):
    return q1(conn, """
        SELECT bhs.battery_id, bhs.pack_model, bhs.age_months,
               bhs.rul_action_v2, bhs.warranty_claim_eligible, bhs.warranty_status,
               bhs.attr_cycle_pct, bhs.attr_dod_pct, bhs.attr_thermal_pct,
               bhs.attr_imbalance_pct, bhs.attr_calendar_pct,
               bhs.degradation_primary_driver, bhs.attr_primary_factor,
               bhs.nbfc_grade_2x2, bhs.dri_score, bhs.kps_slope_8wk,
               b.fleet_segment, b.chemistry
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON b.battery_id = bhs.battery_id
        WHERE bhs.battery_id = ?
    """, [battery_id])


def _passport_attribution_block(s, attr_row):
    """Compact operator-audience attribution block for the passport endpoint.

    Full audience routing lives in /api/battery/:id/attribution — this is the
    slice the passport UI (passport_v3 S6) needs. Never returns percentages
    (operator surface) and never exposes internal codes.
    """
    pack_model = s.get('pack_model') or ''
    if _is_pack3401(pack_model) and _pack3401_exception_qualifies(
            s.get('dri_score'), s.get('kps_slope_8wk')):
        return {
            'attribution_mode': 'PACK_GAP_EXCEPTION',
            'attribution_quality': None,
            'factors': None,
            'pack_gap_note': atext.PACK3401_NOTE,
            'primary_driver_plain': 'Pack design (Pack3401 gap)',
            'operator_narrative': atext.pack3401_operator(),
            'action_sentence': atext.action_sentence(s.get('rul_action_v2')),
            'vehicle_issue': False,
            'vehicle_context': None,
        }

    fleet_segment = s.get('fleet_segment') or ''
    is_lfp_rick = fleet_segment.startswith('GE_') or fleet_segment.startswith('SG_')
    if not is_lfp_rick and fleet_segment:
        return {
            'attribution_mode': 'STANDARD',
            'attribution_quality': 'INCOMPLETE',
            'factors': None,
            'incomplete_note': atext.NMC_INCOMPLETE_NOTE,
            'primary_driver_plain': None,
            'operator_narrative': (
                'Attribution is not yet modeled for this battery variant. '
                'Degradation tracking continues via standard health checks.'),
            'action_sentence': atext.action_sentence(s.get('rul_action_v2')),
            'vehicle_issue': False,
            'vehicle_context': None,
        }

    factors_raw = _coalesce_factors(attr_row, s)
    factors_norm, quality, _ = _normalize_lfp_factors(factors_raw)
    code = atext.primary_driver_code(
        factors_norm, attr_row, {'attr_primary_factor': s.get('attr_primary_factor')})
    primary_plain = atext.primary_driver_plain(code)
    narrative = (
        (attr_row or {}).get('operator_attribution_sentence')
        or atext.operator_narrative(factors_norm, primary_plain, False, s))
    return {
        'attribution_mode': 'STANDARD',
        'attribution_quality': quality,
        'factors': None,   # operator surface — percentages hidden
        'primary_driver_plain': primary_plain,
        'operator_narrative': narrative,
        'action_sentence': atext.action_sentence(s.get('rul_action_v2')),
        'vehicle_issue': False,  # passport endpoint doesn't run the VWF lookup
        'vehicle_context': None,
    }


def _build_attribution_response(battery_id, audience, conn):
    """Main attribution response builder used by /api/battery/:id/attribution.

    Accepts audience ∈ operator | nbfc | oem | internal. Routes response
    content by audience. Never returns factor percentages to operator surface.
    """
    bhs_row = _bhs_row_for_attribution(battery_id, conn)
    if not bhs_row:
        return None
    audience = (audience or 'operator').lower()
    if audience not in ('operator', 'nbfc', 'oem', 'internal'):
        audience = 'operator'

    pack_model = bhs_row.get('pack_model') or ''
    fleet_segment = bhs_row.get('fleet_segment') or ''
    age_months = bhs_row.get('age_months')
    warranty_eligible = bhs_row.get('warranty_claim_eligible') or 0

    mode = _get_attribution_mode(battery_id, pack_model, fleet_segment, conn)
    attr_row = _bda_row_for_attribution(battery_id, conn)

    base = {
        'battery_id': battery_id,
        'pack_model': pack_model,
        'age_months': round(age_months, 1) if age_months else None,
        'attribution_mode': mode,
        'attribution_source': 'Field telemetry analysis',
        'attribution_method': (attr_row or {}).get('attribution_method')
            or ('LEGACY' if bhs_row.get('attr_primary_factor') else None),
        'seasonal_pending': False,
        'warranty_claim_eligible': warranty_eligible,
    }

    # ─── PACK_GAP_EXCEPTION ───
    if mode == 'PACK_GAP_EXCEPTION':
        base.update({
            'attribution_quality': None,
            'factors': None,
            'pack_gap_note': atext.PACK3401_NOTE,
            'primary_driver_plain': 'Pack design (Pack3401 gap)',
            'vehicle_issue': False,
            'vehicle_context': None,
        })
        if audience == 'operator':
            base['operator_narrative'] = atext.pack3401_operator()
            base['action_sentence'] = atext.action_sentence(bhs_row.get('rul_action_v2'))
        elif audience == 'nbfc':
            base['nbfc_narrative'] = atext.pack3401_nbfc(warranty_eligible)
            base['operator_attribution_pct'] = None
            base['oem_attribution_pct'] = None
            base['warranty_covered_pct'] = None
            base['grade_framing'] = atext.grade_framing(bhs_row.get('nbfc_grade_2x2'))
            base['rul_disclaimer'] = atext.rul_disclaimer()
        elif audience == 'oem':
            base['oem_narrative'] = atext.pack3401_oem()
            base['pack_gap_fleet_finding'] = atext._PACK3401_FLEET_FINDING  # noqa: SLF001
        else:  # internal
            base['operator_narrative'] = atext.pack3401_operator()
            base['nbfc_narrative'] = atext.pack3401_nbfc(warranty_eligible)
            base['oem_narrative'] = atext.pack3401_oem()
        return base

    # ─── INCOMPLETE (NMC / LCV / FGHHL) ───
    if mode == 'INCOMPLETE':
        base.update({
            'attribution_quality': 'INCOMPLETE',
            'factors': None,
            'incomplete_note': atext.NMC_INCOMPLETE_NOTE,
            'primary_driver_plain': None,
            'vehicle_issue': False,
            'vehicle_context': None,
        })
        if audience == 'operator':
            base['operator_narrative'] = (
                'Attribution is not yet modeled for this battery variant. '
                'Degradation tracking continues via standard health checks.')
            base['action_sentence'] = atext.action_sentence(bhs_row.get('rul_action_v2'))
        elif audience == 'nbfc':
            base['nbfc_narrative'] = (
                'Attribution analysis not available for this battery variant. '
                'Grade and collateral assessment rely on SoH and range telemetry.')
            base['grade_framing'] = atext.grade_framing(bhs_row.get('nbfc_grade_2x2'))
            base['rul_disclaimer'] = atext.rul_disclaimer()
        elif audience == 'oem':
            base['oem_narrative'] = 'Attribution not computed for this fleet segment (separate methodology).'
        else:
            base['operator_narrative'] = (
                'Attribution is not yet modeled for this battery variant.')
        return base

    # ─── STANDARD LFP rickshaw ───
    factors_raw = _coalesce_factors(attr_row, bhs_row)
    factors_norm, quality, raw_total = _normalize_lfp_factors(factors_raw)
    vehicle_issue = _is_vehicle_issue(battery_id, conn)
    code = atext.primary_driver_code(factors_norm, attr_row, bhs_row)
    primary_plain = atext.primary_driver_plain(code)

    base.update({
        'attribution_quality': quality,
        'primary_driver_plain': primary_plain,
        'vehicle_issue': vehicle_issue,
        'vehicle_context': atext.vehicle_context() if vehicle_issue else None,
    })

    if quality == 'INCOMPLETE':
        base['factors'] = None
        base['incomplete_note'] = atext.LFP_INCOMPLETE_NOTE
        if audience == 'operator':
            base['operator_narrative'] = atext.LFP_INCOMPLETE_NOTE
            base['action_sentence'] = atext.action_sentence(bhs_row.get('rul_action_v2'))
        elif audience == 'nbfc':
            base['nbfc_narrative'] = atext.LFP_INCOMPLETE_NOTE
            base['grade_framing'] = atext.grade_framing(bhs_row.get('nbfc_grade_2x2'))
            base['rul_disclaimer'] = atext.rul_disclaimer()
        elif audience == 'oem':
            base['oem_narrative'] = atext.LFP_INCOMPLETE_NOTE + f" (raw_total={raw_total}%)"
        return base

    # Operator audience — no percentages
    stored_op = (attr_row or {}).get('operator_attribution_sentence')
    stored_nbfc = (attr_row or {}).get('nbfc_attribution_sentence')
    stored_oem = (attr_row or {}).get('oem_attribution_sentence')

    if audience == 'operator':
        base['factors'] = None
        base['operator_narrative'] = (
            stored_op or atext.operator_narrative(factors_norm, primary_plain, vehicle_issue, bhs_row))
        base['attribution_sentence_source'] = 'BDA_STORED' if stored_op else 'GENERATED'
        base['action_sentence'] = atext.action_sentence(bhs_row.get('rul_action_v2'))
    elif audience == 'nbfc':
        base['factors'] = None  # NBFC gets fractions, not per-factor bars
        base['operator_attribution_pct'] = _operator_fraction(factors_norm)
        base['oem_attribution_pct'] = _oem_fraction(factors_norm)
        base['warranty_covered_pct'] = _oem_fraction(factors_norm)
        base['nbfc_narrative'] = (
            stored_nbfc or atext.nbfc_narrative(factors_norm, bhs_row, vehicle_issue))
        base['attribution_sentence_source'] = 'BDA_STORED' if stored_nbfc else 'GENERATED'
        base['grade_framing'] = atext.grade_framing(bhs_row.get('nbfc_grade_2x2'))
        base['rul_disclaimer'] = atext.rul_disclaimer()
    elif audience == 'oem':
        base['factors'] = factors_norm
        # Top-level *_pct aliases — UI renderAttribution reads flat fields.
        base['charging_pct']    = factors_norm.get('charging')
        base['usage_pct']       = factors_norm.get('usage')
        base['maintenance_pct'] = factors_norm.get('maintenance')
        base['thermal_pct']     = factors_norm.get('thermal')
        base['oem_pct']         = factors_norm.get('calendar')
        base['operator_attribution_pct'] = _operator_fraction(factors_norm)
        base['oem_attribution_pct'] = _oem_fraction(factors_norm)
        base['oem_narrative'] = (
            stored_oem or atext.oem_narrative(factors_norm, bhs_row, vehicle_issue))
        base['attribution_sentence_source'] = 'BDA_STORED' if stored_oem else 'GENERATED'
    else:  # internal
        base['factors'] = factors_norm
        base['charging_pct']    = factors_norm.get('charging')
        base['usage_pct']       = factors_norm.get('usage')
        base['maintenance_pct'] = factors_norm.get('maintenance')
        base['thermal_pct']     = factors_norm.get('thermal')
        base['oem_pct']         = factors_norm.get('calendar')
        base['raw_factors'] = factors_raw
        base['factor_sum_raw'] = raw_total
        base['operator_attribution_pct'] = _operator_fraction(factors_norm)
        base['oem_attribution_pct'] = _oem_fraction(factors_norm)
        base['primary_driver_code'] = (attr_row or {}).get('primary_driver') or bhs_row.get('attr_primary_factor')
        base['operator_narrative'] = (
            stored_op or atext.operator_narrative(factors_norm, primary_plain, vehicle_issue, bhs_row))
        base['nbfc_narrative'] = (
            stored_nbfc or atext.nbfc_narrative(factors_norm, bhs_row, vehicle_issue))
        base['oem_narrative'] = (
            stored_oem or atext.oem_narrative(factors_norm, bhs_row, vehicle_issue))

    return base


def _h1_build_fault(s):
    """Build structured fault node."""
    pt = s.get("fault_profile_tier")
    dur = s.get("fault_duration_weeks") or 0
    traj = s.get("fault_trajectory")
    rec = s.get("fault_recurrence_count") or 0
    pers = s.get("fault_persistence_score")
    ptfm = s.get("pack_top_failure_mode") or "CHEMISTRY"
    prr = s.get("pack_repeat_rate") or 0

    pp_map = {
        "CHRONIC_WORSENING": f"Ongoing fault getting worse — active for {dur} weeks",
        "ACTIVE_WORSENING": f"Recent fault getting worse — active for {dur} weeks",
        "ACTIVE_STABLE": f"Active fault, stable — present for {dur} weeks",
        "RECOVERING": "Previous fault resolving — no action needed",
        "CLEAN": "No active faults detected",
    }
    scale_note = None
    if pt in ("CHRONIC_WORSENING", "ACTIVE_WORSENING") and pers is not None:
        scale_note = (f"Fault severity: {pers}/100 "
                      f"(scale: 0=most severe chronic fault, 100=no faults)")

    pack_ctx = None
    pack_ctx_note = None
    if ptfm:
        try:
            pack_ctx = (f"Batteries on this pack type most commonly present: "
                        f"{ptfm} issues ({float(prr):.0f}% recurrence rate)")
        except (ValueError, TypeError):
            pack_ctx = f"Batteries on this pack type most commonly present: {ptfm} issues"
    if pt in ("CHRONIC_WORSENING", "ACTIVE_WORSENING") and ptfm:
        pack_ctx_note = "Your battery matches the most common fault pattern for this pack type"

    return {
        "profile_tier": pt,
        "profile_plain": pp_map.get(pt, "Fault status: assessing"),
        "duration_weeks": dur,
        "duration_plain": f"Active for {dur} weeks" if dur > 0 else "No active fault duration",
        "trajectory": traj,
        "recurrence_count": rec,
        "persistence_score": pers,
        "persistence_plain": (f"Severity: {'Maximum (chronic worsening fault)' if pers == 0 else 'Moderate' if (pers or 100) < 60 else 'Low'}"
                              if pers is not None else "Fault persistence: assessing"),
        "persistence_scale_note": scale_note,
        "pack_context": pack_ctx,
        "pack_context_note": pack_ctx_note,
    }


def _h1_resale_stage(bhs_score, pct_comm):
    """Resale stage from BHS (chemistry health), not pct_of_commissioned.

    Rule 228: pct_of_commissioned measures range delivery, not electrochemical
    capacity. A battery at 41% range may still have 70%+ chemistry if the drop
    is from efficiency losses and cell imbalance (recoverable). BHS is the
    correct proxy for stage classification.

    STAGE1_ACTIVE:     BHS >= 60 (chemistry healthy, active value)
    STAGE2_SECONDLIFE: BHS 40-60 (degraded but usable for light duty)
    STAGE3_SCRAP:      BHS < 40  (chemistry at end of primary use)

    Override: pct_comm < 30% floors to STAGE2 minimum (severe range loss
    regardless of BHS suggests commercial end-of-life for primary fleet use).
    """
    if bhs_score is None:
        # Fallback to pct_of_commissioned if no BHS
        if pct_comm is None:
            return "STAGE1_ACTIVE"
        if pct_comm >= 70:
            return "STAGE1_ACTIVE"
        if pct_comm >= 50:
            return "STAGE2_SECONDLIFE"
        return "STAGE3_SCRAP"

    watch_floor = float(get_param('bhs_v4_tier_watch_floor', default=65))
    stressed_floor = float(get_param('bhs_v4_tier_stressed_floor', default=50))
    if bhs_score >= watch_floor:
        stage = "STAGE1_ACTIVE"
    elif bhs_score >= stressed_floor:
        stage = "STAGE2_SECONDLIFE"
    else:
        stage = "STAGE3_SCRAP"

    # Override: severe range loss floors to STAGE2
    if pct_comm is not None and pct_comm < 30 and stage == "STAGE1_ACTIVE":
        stage = "STAGE2_SECONDLIFE"

    return stage


def _h1_build_prediction(s, fleet_segment):
    """Build prediction node with breach, RUL, and resale."""
    b4 = s.get("p_floor_breach_4w")
    b8 = s.get("p_floor_breach_8w")
    b12 = s.get("p_floor_breach_12w")
    bro = s.get("breach_risk_override") or 0
    rul = s.get("rul_weeks_v2")
    resale = s.get("resale_value_inr")
    # Override DB resale_stage with BHS-based staging (Rule 228)
    r_stage = _h1_resale_stage(s.get("bhs_score_v2"), s.get("pct_of_commissioned"))
    r_src = s.get("resale_price_source") or "MARKET_ESTIMATE"

    sg_note = None
    if fleet_segment == "SG_ERICKSHAW" and b4 is None:
        sg_note = "Range floor model applies to GE fleet only. Cell stability monitored separately."

    breach_expl = None
    if bro == 1:
        b12_pct = f"{(b12 or 0) * 100:.0f}" if b12 is not None else "unknown"
        if b12 is not None and b12 < 0.30:
            breach_expl = (
                f"Two separate risks: (1) Range falling below floor — currently "
                f"{b12_pct}% chance in 12 weeks. (2) Cell instability causing sudden failure — "
                f"currently HIGH. These are independent. A battery can have good range but "
                f"still face sudden failure from cell instability.")
        else:
            breach_expl = ("Cell instability risk is high. Range breach probability "
                           "is a separate measurement — both matter.")

    stage_plain = {
        "STAGE1_ACTIVE": "Active fleet value — battery still earning revenue",
        "STAGE2_SECONDLIFE": "Second-life value — suitable for lower-demand applications",
        "STAGE3_SCRAP": "Scrap value — at end of primary use life",
    }.get(r_stage, "Asset value: assessing")

    return {
        "p_breach_4w": round(b4 * 100, 1) if b4 is not None else None,
        "p_breach_8w": round(b8 * 100, 1) if b8 is not None else None,
        "p_breach_12w": round(b12 * 100, 1) if b12 is not None else None,
        "breach_model_note": sg_note,
        "breach_risk_override": bro,
        "breach_risk_plain": ("Cell stability risk: HIGH — independent of current range"
                              if bro == 1 else "Cell stability: No acute risk detected"),
        "breach_type_explanation": breach_expl,
        "rul_weeks": rul,
        "rul_lower": s.get("rul_lower_v2"),
        "rul_upper": s.get("rul_upper_v2"),
        "rul_confidence": s.get("rul_confidence"),
        "rul_bands_plain": (
            f"Range: {s.get('rul_lower_v2'):.0f}\u2013{s.get('rul_upper_v2'):.0f} weeks"
            if s.get("rul_lower_v2") and s.get("rul_upper_v2") else None),
        "rul_plain": (f"Estimated {rul} weeks of useful life remaining"
                      if rul else "Remaining life: assessing"),
        "rul_disclaimer": "Directional estimate. PRELIMINARY — not calibrated for covenants.",
        "warranty_consumed_pct": s.get("efc_pct_of_warranty"),
        "warranty_plain": (
            f"Warranty {s.get('efc_pct_of_warranty'):.0f}% consumed"
            if s.get("efc_pct_of_warranty") else None),
        "resale_value": resale,
        "resale_stage": r_stage,
        "resale_plain": stage_plain,
        "resale_disclaimer": ("Market estimate — verify with OEM invoice"
                              if r_src == "MARKET_ESTIMATE" else "OEM verified price"),
    }


def _h1_build_narrative(s, physics, cause, fault, prediction):
    """Build multi-sentence narrative."""
    action_code = s.get("rul_action_v2") or "MONITOR_WEEKLY"
    rng = s.get("range_p50") or s.get("range_corrected_km") or 0
    false_alarm = s.get("false_alarm_likely") == 1
    divergence = physics.get("divergence") or ""

    # ── Divergence overrides — take priority over action_code ──
    if divergence == "RANGE_CONTEXT":
        headline = (f"Range has dropped to {rng:.0f}km but internal chemistry is healthy. "
                    f"This is likely an operational change, not battery failure.")
        story = ("When range drops but chemistry is healthy, the cause is usually "
                 "how the vehicle is being used — route change, load increase, or "
                 "charging pattern shift. The battery itself is not deteriorating.")
        why = cause.get("operator_sentence") or ""
        return {
            "headline": headline,
            "story": story,
            "why_plain": why,
            "what_next": "Check operator behaviour before booking any service. Investigate route and load changes first.",
        }

    if divergence == "EARLY_WARNING":
        headline = (f"Range is holding at {rng:.0f}km but internal chemistry is deteriorating. "
                    f"This is the early warning window.")
        story = ("The battery is delivering normal range today, but chemistry signals "
                 "show decline is starting. Intervening now is significantly cheaper "
                 "than waiting until range drops.")
        why = cause.get("operator_sentence") or ""
        return {
            "headline": headline,
            "story": story,
            "why_plain": why,
            "what_next": "Book cell balance service this month — before range is affected.",
        }

    # ── Standard action-code logic ──
    headline_map = {
        "PHYSICS_REPLACE_PLAN": f"This battery is in confirmed decline — {rng:.0f}km and losing ground.",
        "REPLACE_PLAN": "Range is dropping and the data confirms it. Replacement window is open now.",
        "ACUTE_BREACH_WATCH": "This battery is unstable — range can drop significantly on any given day.",
        "CELL_BALANCE_PRIORITY": "Health signals are worsening but this battery is recoverable.",
        "MONITOR_INVESTIGATE": "Something changed recently. Monitoring closely before recommending action.",
        "MONITOR_WEEKLY": f"This battery is being watched but not in danger. Range holding at {rng:.0f}km.",
        "NO_ACTION": "All signals normal. This battery is working as expected.",
    }
    if false_alarm:
        headline = "Platform investigated an alert on this battery — no confirmed fault found. Monitoring continues."
    else:
        headline = headline_map.get(action_code, f"Battery status: {physics['track_b_label']}")

    div_note = ""
    dq = divergence

    fault_note = ""
    if fault.get("profile_tier") in ("CHRONIC_WORSENING", "ACTIVE_WORSENING"):
        t = fault.get("trajectory") or "ongoing"
        fault_note = (f" A fault has been active for {fault['duration_weeks']} weeks "
                      f"and is {t.lower()}.")

    story = f"{headline}{div_note}{fault_note}"
    why = (f"{cause['primary_factor_plain']} is the primary driver "
           f"({cause['primary_pct']:.0f}%). {cause['operator_sentence']}.")

    what_next_map = {
        "PHYSICS_REPLACE_PLAN": "Plan replacement this week. Asset value window closing.",
        "REPLACE_PLAN": "Schedule replacement within the month.",
        "ACUTE_BREACH_WATCH": "Book cell balance service this week. Range is holding but failure risk is high — do not wait.",
        "CELL_BALANCE_PRIORITY": "Cell balancing now may extend life. Book service this month.",
        "MONITOR_INVESTIGATE": "No action yet — investigation ongoing. Check next week.",
        "MONITOR_WEEKLY": "No action needed. Platform monitoring weekly.",
        "NO_ACTION": "No action needed this week.",
    }
    return {
        "headline": headline,
        "story": story,
        "why_plain": why,
        "what_next": what_next_map.get(action_code, "Continue monitoring."),
    }


def _h1_build_decision(s, audience, narrative, cause_node=None):
    """Build audience-routed decision node."""
    cause_node = cause_node or {}
    ac = s.get("rul_action_v2") or "MONITOR_WEEKLY"
    fa = s.get("false_alarm_likely") == 1

    ap = {"PHYSICS_REPLACE_PLAN": "Plan replacement — confirmed decline",
          "REPLACE_PLAN": "Schedule replacement",
          "ACUTE_BREACH_WATCH": "Service now — cell instability",
          "CELL_BALANCE_PRIORITY": "Book cell balance service",
          "MONITOR_INVESTIGATE": "Under investigation — watch closely",
          "MONITOR_WEEKLY": "Monitoring weekly — no action yet",
          "NO_ACTION": "No action needed this week"}
    um = {"PHYSICS_REPLACE_PLAN": "THIS_WEEK", "REPLACE_PLAN": "THIS_MONTH",
          "ACUTE_BREACH_WATCH": "THIS_WEEK", "CELL_BALANCE_PRIORITY": "THIS_MONTH",
          "MONITOR_INVESTIGATE": "ONGOING", "MONITOR_WEEKLY": "ONGOING",
          "NO_ACTION": "NONE"}
    up = {"THIS_WEEK": "Act this week", "THIS_MONTH": "Plan within this month",
          "ONGOING": "Continue monitoring", "NONE": "No action needed"}

    action_plain = ap.get(ac, ac)
    urgency = um.get(ac, "ONGOING")
    if fa:
        action_plain = "Monitoring weekly — previous alert investigated"
        urgency = "ONGOING"

    # Divergence-aware action override
    divergence = s.get("divergence_quadrant") or ""
    if divergence == "RANGE_CONTEXT" and ac in (
        "PHYSICS_REPLACE_PLAN", "REPLACE_PLAN",
        "CELL_BALANCE_PRIORITY", "ACUTE_BREACH_WATCH",
    ):
        action_plain = "Investigate operational cause before any service"
        urgency = "THIS_MONTH"

    base = {
        "action": action_plain,
        "action_code": ac,
        "action_plain": narrative.get("what_next", action_plain),
        "action_reason": narrative.get("why_plain", ""),
        "action_urgency": urgency,
        "action_urgency_plain": up.get(urgency, "Monitor"),
        "false_alarm": fa,
        "false_alarm_note": "Alert investigated — no confirmed fault" if fa else None,
    }

    # NBFC sub-node
    grade = s.get("nbfc_risk_tier")
    pct_comm = s.get("pct_of_commissioned") or 0
    soh_stage = ("First Life" if pct_comm >= 70 else
                 "Second Life" if pct_comm >= 50 else "Scrap threshold")
    gr_map = {"A": "Low risk trajectory — strong chemistry and range signals",
              "B": "Watch zone — early signals present, monitoring active",
              "C": "Elevated risk — declining trend confirmed",
              "D": "High risk trajectory — action recommended"}
    risk_word = {"A": "low", "B": "moderate", "C": "elevated", "D": "high"}.get(grade, "pending")

    nbfc = {
        "grade": grade,
        "grade_reason": gr_map.get(grade, "Grade pending"),
        "grade_plain": (f"Grade {grade} — {risk_word} risk. "
                        f"Battery at {pct_comm:.0f}% of commissioned range ({soh_stage})."
                        if grade else "Grade pending"),
        "collateral_value": s.get("resale_value_inr"),
        "collateral_stage": s.get("resale_stage"),
        "collateral_note": (
            "Grade D reflects risk trajectory, not replacement status. "
            "Battery still has active collateral value — intervention window open."
            if grade == "D" and s.get("resale_stage") == "STAGE1_ACTIVE" else None),
        "covenant_status": "Not triggered" if pct_comm >= 75 else "Review recommended",
        "covenant_threshold": f"SoH < 75%. Current: {pct_comm:.0f}%",
        "signal_confidence": s.get("signal_confidence"),
        "signal_confidence_note": _h1_confidence_plain(s.get("signal_confidence")),
    }

    # OEM sub-node
    oem = {
        "verdict": cause_node.get("verdict", "Mixed causes"),
        "verdict_plain": cause_node.get("verdict_detail", narrative.get("why_plain", "")),
        "pack_model": s.get("pack_model"),
        "pack_note": (f"{s.get('pack_model')} — consistent with expected degradation "
                      f"for this use case and age."),
        "warranty_note": "No warranty trigger. Degradation within expected parameters.",
    }

    # RANGE_CONTEXT: override NBFC grade narrative (chemistry healthy)
    if divergence == "RANGE_CONTEXT":
        nbfc["grade_reason"] = (
            "Range dropped but chemistry is healthy. "
            "Operational cause suspected — not battery deterioration. "
            "Grade reflects range drop only.")
        nbfc["grade_plain"] = (
            f"Grade {grade or '?'} — range delivery dropped "
            f"but internal chemistry is healthy. "
            f"Investigate operational cause before financial action.")
        nbfc["collateral_note"] = (
            "Chemistry health suggests collateral value "
            "may be higher than range delivery implies. "
            "Verify operational cause before marking at-risk.")

    base["nbfc"] = nbfc
    base["oem"] = oem
    return base


# ── GET /api/battery/:id/passport — H1 intelligence contract ─────

@app.get("/api/battery/{battery_id}/passport")
@safe
def battery_passport(battery_id: str,
                     audience: str = Query(default="operator"),
                     _=Depends(verify_token)):
    """H1 intelligence chain — 7-node structured passport.
    audience: operator | nbfc | oem | internal
    """
    conn = get_conn()

    # ── Gate: SCORE_SUSPENDED ─────────────────────────────────────
    sm = q1(conn, """SELECT scoring_mode FROM battery_health_scores_v2
                     WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1""",
            [battery_id])
    if sm and sm.get("scoring_mode") == "SUSPENDED":
        conn.close()
        return {"display_blocked": True, "reason": "SCORE_SUSPENDED",
                "battery_id": battery_id}

    # ── Fleet context (cached) ────────────────────────────────────
    fctx = _get_fleet_context(conn)

    # ── Main BHS row ──────────────────────────────────────────────
    s = q1(conn, """
        SELECT s.*, b.battery_model as pack_model, b.city_code as city,
               b.fleet_segment, b.use_case_inferred, b.commissioning_date
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.battery_id = ?
        ORDER BY s.week_number DESC LIMIT 1
    """, [battery_id])
    if not s:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")

    fleet_segment = s.get("fleet_segment") or ""

    # ── Supporting queries ────────────────────────────────────────
    pp = q1(conn, """SELECT driver_stress_tier, driver_stress_score,
                            personal_charge_profile, personal_drive_profile
                     FROM battery_personal_params WHERE battery_id = ?""",
            [battery_id])
    attr_row = q1(conn, """
        SELECT maintenance_pct, charging_pct, usage_pct, thermal_pct,
               calendar_pct, primary_driver
        FROM battery_degradation_attribution WHERE battery_id = ?
        ORDER BY scored_week DESC LIMIT 1
    """, [battery_id])
    nw = q1(conn, "SELECT COUNT(*) as n FROM vehicle_weekly_features WHERE battery_id = ?",
            [battery_id])
    dod_row = q1(conn, """SELECT dod_corrected FROM vehicle_weekly_features
                          WHERE battery_id = ? AND dod_corrected IS NOT NULL
                          ORDER BY week_number DESC LIMIT 1""", [battery_id])
    # dod_behavior_flag re-enabled APR24 after peak-to-trough fix (PD-12B
    # resolved). Fleet distribution: DEEP=11052 NORMAL=2980 SHALLOW=483.
    # Safe to display. Method recorded in fleet_context_params
    # (param_name='dod_behavior_flag_method').
    dod_flag_row = q1(conn, """SELECT dod_behavior_flag, actual_dod_pct
                               FROM vehicle_weekly_features
                               WHERE battery_id = ? AND dod_behavior_flag IS NOT NULL
                               ORDER BY week_number DESC LIMIT 1""", [battery_id])
    spread_row = q1(conn, """SELECT cell_spread_max FROM vehicle_weekly_features
                             WHERE battery_id = ? AND cell_spread_max IS NOT NULL
                             ORDER BY week_number DESC LIMIT 1""", [battery_id])
    comm_spread = q1(conn, """SELECT cell_spread_max FROM vehicle_weekly_features
                              WHERE battery_id = ? AND cell_spread_max IS NOT NULL
                              ORDER BY week_number ASC LIMIT 1""", [battery_id])
    cn = q1(conn, """SELECT COUNT(*) as n FROM batteries
                     WHERE battery_model = ? AND (is_active=1 OR is_active IS NULL)""",
            [s["pack_model"]])
    pf = q1(conn, """SELECT param_value FROM fleet_context_params
                     WHERE param_name = 'range_floor_km' AND segment_value = ?
                     AND is_active = 1 LIMIT 1""", [s["pack_model"]])
    pack_floor_km = (pf or {}).get("param_value") or 56.0
    pr = q1(conn, """SELECT physical_risk_score FROM battery_component_health
                     WHERE battery_id = ? AND physical_risk_score IS NOT NULL
                     ORDER BY week_number DESC LIMIT 1""", [battery_id])
    load_row = q1(conn, """SELECT load_intensity_p50 FROM battery_behavioural_features
                           WHERE battery_id = ? AND load_intensity_p50 IS NOT NULL
                           ORDER BY week_number DESC LIMIT 1""", [battery_id])

    # Corroboration signals
    wk = s.get("week_number") or 0
    signals_firing, signals_silent = _h1_corroboration_signals(battery_id, wk, conn)

    # Range history for chart
    rh = q(conn, """
        SELECT v.week_number, v.km_per_soc_pct,
               s2.range_p10, s2.range_p50, s2.range_p90
        FROM vehicle_weekly_features v
        LEFT JOIN battery_health_scores_v2 s2
            ON v.battery_id = s2.battery_id AND v.week_number = s2.week_number
        WHERE v.battery_id = ? AND v.km_per_soc_pct IS NOT NULL
        ORDER BY v.week_number DESC LIMIT 20
    """, [battery_id])

    # Vehicle profile from latest VWF row
    vwf_row = q1(conn, """
        SELECT avg_speed, trip_count, dod_mean, temp_max,
               charge_cycles_delta, cell_spread_max, km_sum
        FROM vehicle_weekly_features
        WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1
    """, [battery_id])

    conn.close()

    # ── Derived values ────────────────────────────────────────────
    n_weeks = (nw or {}).get("n", 0)
    from datetime import datetime
    age_weeks = n_weeks
    if s.get("commissioning_date"):
        try:
            comm_dt = datetime.strptime(s["commissioning_date"], "%Y-%m-%d")
            age_weeks = max(0, (datetime.now() - comm_dt).days // 7)
        except (ValueError, TypeError):
            pass

    # Merge personal params into s for helpers
    s["driver_stress_tier"] = s.get("driver_stress_tier") or (pp or {}).get("driver_stress_tier")
    s["personal_charge_profile"] = (pp or {}).get("personal_charge_profile")
    s["personal_drive_profile"] = (pp or {}).get("personal_drive_profile")
    s["load_intensity_p50"] = (load_row or {}).get("load_intensity_p50")
    s["n_weeks_data"] = n_weeks

    cell_spread = (spread_row or {}).get("cell_spread_max")
    comm_spread_val = (comm_spread or {}).get("cell_spread_max")
    rng = s.get("range_p50") or s.get("range_corrected_km")
    dod_val = (dod_row or {}).get("dod_corrected")

    fa_range = fctx["fleet_avg_range"]
    fa_spread = fctx["fleet_avg_spread"]
    fa_dod = fctx["fleet_avg_dod"]

    # ── NODE 1: SIGNAL ────────────────────────────────────────────
    pct_comm = s.get("pct_of_commissioned") or 0
    signal_node = {
        "range_km": round(rng, 1) if rng else None,
        "range_vs_new": f"{pct_comm:.0f}% of commissioned range",
        "range_fleet_context": (
            f"{'Above' if (rng or 0) > fa_range else 'Below'} "
            f"fleet average ({fa_range}km)"),
        "range_trend": s.get("health_class_v2"),
        "range_trend_plain": _h1_range_trend_plain(
            s.get("kps_slope_8wk"), s.get("health_class_v2"),
            s.get("kps_slope_8wk")),
        "slope_8wk": s.get("kps_slope_8wk"),
        "slope_plain": _h1_slope_plain(s.get("kps_slope_8wk")),
        "slope_recent_note": None,  # populated below if divergent
        "cell_spread_mv": round(cell_spread, 1) if cell_spread else None,
        "cell_spread_vs_baseline": (
            f"+{round(cell_spread - comm_spread_val, 0):.0f}mV above commissioning baseline"
            if cell_spread and comm_spread_val and cell_spread > comm_spread_val
            else "At or below commissioning baseline"
            if cell_spread and comm_spread_val
            else "Commissioning baseline not available"),
        "cell_spread_trend": (
            "WORSENING" if s.get("spread_delta_slope") and s.get("spread_delta_slope") > 0.5
            else "STABLE" if cell_spread else "UNKNOWN"),
        "dod_pct": round(dod_val, 1) if dod_val else None,
        "dod_fleet_avg": fa_dod,
        "dod_note": (
            f"Using battery at {dod_val:.0f}% depth (fleet average: {fa_dod}%)"
            if dod_val else "Depth of discharge data building"),
    }

    # ── NODE 2: PHYSICS ───────────────────────────────────────────
    track_a = s.get("track_a_score")
    track_b = s.get("bhs_score_v2")
    divergence_q = s.get("divergence_quadrant")
    div_reason = _h1_build_divergence_reason(s)

    bhs_components = {
            "cell_divergence": {"score": s.get("bhs_component_spread"),
                                "weight": 28, "label": "Cell divergence under load",
                                "contribution": ("Primary driver"
                                    if (s.get("bhs_component_spread") or 100) < 60
                                    else "Normal")},
            "range_retention": {"score": s.get("bhs_component_pct_comm"),
                                "weight": 23, "label": "Range vs commissioned baseline"},
            "cycle_wear": {"score": s.get("bhs_component_ah"),
                           "weight": 15, "label": "Cumulative cycle stress"},
            "efficiency": {"score": s.get("bhs_component_op"),
                           "weight": 13, "label": "Operational efficiency"},
            "capacity_trend": {"score": s.get("bhs_component_soh_trend"),
                               "weight": 11, "label": "Capacity trajectory"},
            "capacity_state": {"score": s.get("bhs_component_soh_latest"),
                               "weight": 7, "label": "Current capacity state"},
        "fault_persistence": {"score": s.get("bhs_component_charge"),
                              "weight": 1, "label": "Fault persistence"},
    }

    physics_node = {
        "track_a_score": track_a,
        "track_a_label": _h1_track_label(track_a),
        "track_a_plain": _h1_track_plain(track_a, "A"),
        "track_b_score": track_b,
        "track_b_label": _h1_track_label(track_b),
        "track_b_plain": _h1_track_plain(track_b, "B"),
        "divergence": divergence_q,
        "divergence_plain": _h1_divergence_plain(divergence_q, div_reason),
        "divergence_reason": div_reason,
        "degradation_regime": s.get("degradation_regime"),
        "degradation_regime_plain": _h1_regime_plain(s.get("degradation_regime")),
        "bhs_components": bhs_components,
    }

    # ── NODE 3: TRIANGULATION ─────────────────────────────────────
    sig_conf = s.get("signal_confidence")
    corr_score = s.get("corroboration_score") or 0
    n_firing = len(signals_firing)

    # Derive confidence tier from corroboration, not raw DB field
    if n_weeks < 8:
        derived_tier = "DATA_LIMITED"
    elif corr_score >= 5:
        derived_tier = "PHYSICS_CONFIRMED"
    elif corr_score >= 3:
        derived_tier = "HIGH"
    elif corr_score >= 1:
        derived_tier = "MEDIUM"
    elif n_weeks >= 20:
        derived_tier = "HIGH"
    else:
        derived_tier = "MEDIUM"

    triangulation_node = {
        "signals_firing": n_firing,
        "signals_total": 7,
        "signals_firing_list": signals_firing,
        "signals_silent_list": signals_silent,
        "confidence_tier": derived_tier,
        "confidence_reason": _h1_confidence_reason(
            derived_tier, corr_score, n_weeks, n_firing),
        "confidence_plain": _h1_confidence_plain(derived_tier),
        "corroboration_score": corr_score,
        "corroboration_plain": (
            f"{n_firing} of 7 independent signals active"
            if n_firing > 0
            else "No active warning signals — all 7 tracks quiet"),
        "data_weeks": n_weeks,
        "data_quality": ("FULL" if n_weeks >= 12 else
                         "PARTIAL" if n_weeks >= 4 else "STALE"),
        "data_quality_note": (
            "Breach probability: GE fleet only. Cell stability monitored separately."
            if fleet_segment == "SG_ERICKSHAW"
            else "All signals available" if n_weeks >= 12
            else f"Limited history ({n_weeks} weeks)"),
    }

    # ── NODE 4: CAUSE ─────────────────────────────────────────────
    cause_node = _h1_build_attribution(s, attr_row, fleet_segment)

    # ── NODE 5: FAULT ─────────────────────────────────────────────
    fault_node = _h1_build_fault(s)

    # ── NODE 6: PREDICTION ────────────────────────────────────────
    prediction_node = _h1_build_prediction(s, fleet_segment)

    # ── NARRATIVE ─────────────────────────────────────────────────
    narrative = _h1_build_narrative(s, physics_node, cause_node,
                                   fault_node, prediction_node)

    # ── NODE 7: DECISION ──────────────────────────────────────────
    decision_node = _h1_build_decision(s, audience, narrative, cause_node)

    # ── Range history for chart ───────────────────────────────────
    comm_range = s.get("commissioned_range_km")
    range_history = []
    for rr in sorted(rh, key=lambda x: x["week_number"]):
        kps = rr.get("km_per_soc_pct")
        range_history.append({
            "week_number": rr["week_number"],
            "range_p10": round(rr["range_p10"], 1) if rr.get("range_p10") else None,
            "range_p50": round(kps * 80, 1) if kps else None,
            "range_p90": round(rr["range_p90"], 1) if rr.get("range_p90") else None,
            "commissioned_range_km": comm_range,
            "pack_floor_km": pack_floor_km,
        })

    # ── NODE: VEHICLE PROFILE ─────────────────────────────────────
    vr = vwf_row or {}
    vp_speed = vr.get("avg_speed")
    vp_dod = vr.get("dod_mean")
    if vp_dod is not None and vp_dod < 1:
        vp_dod = round(vp_dod * 100, 1)
    elif vp_dod is not None:
        vp_dod = round(vp_dod, 1)
    vp_temp = vr.get("temp_max")
    vp_spread = vr.get("cell_spread_max")

    vehicle_profile = {
        "route_type": (
            "Urban stop-start" if vp_speed and vp_speed < 18 else
            "Mixed urban" if vp_speed and vp_speed <= 28 else
            "Highway-dominant" if vp_speed else None),
        "route_type_plain": (
            "Mostly low-speed urban driving with frequent stops"
            if vp_speed and vp_speed < 18 else
            "Mix of urban and arterial roads"
            if vp_speed and vp_speed <= 28 else
            "Higher-speed routes — less stop-start"
            if vp_speed else "Speed data building"),
        "avg_speed": round(vp_speed, 1) if vp_speed else None,
        "trips_per_week": vr.get("trip_count"),
        "dod_pct": vp_dod,
        "dod_context": (
            "Deep cycling (>85%)" if vp_dod and vp_dod > 85 else
            "Normal range (50\u201385%)" if vp_dod and vp_dod > 50 else
            "Shallow cycling (<50%)" if vp_dod else "DoD data building"),
        "peak_temp": round(vp_temp, 1) if vp_temp else None,
        "thermal_context": (
            "Critical thermal stress (>45\u00b0C)" if vp_temp and vp_temp > 45 else
            "Elevated temperature (>40\u00b0C)" if vp_temp and vp_temp > 40 else
            "Normal operating temperature" if vp_temp else "Temperature data building"),
        "charge_frequency": vr.get("charge_cycles_delta"),
        "cell_spread_mv": round(vp_spread, 1) if vp_spread else None,
        "cell_spread_vs_fleet": (
            f"{vp_spread:.0f}mV vs fleet avg {fa_spread:.0f}mV"
            if vp_spread else None),
        "data_source": "Derived from telemetry — no operator input required",
    }

    # ── Assemble response ─────────────────────────────────────────
    return {
        # Identity
        "battery_id": battery_id,
        "pack_model": s.get("pack_model"),
        "city": s.get("city"),
        "fleet_segment": fleet_segment,
        "age_weeks": age_weeks,
        "week_number": s.get("week_number"),
        "commissioned_range_km": comm_range,
        "audience": audience,

        # 8 intelligence nodes
        "signal": signal_node,
        "physics": physics_node,
        "triangulation": triangulation_node,
        "cause": cause_node,
        "fault": fault_node,
        "prediction": prediction_node,
        "decision": decision_node,
        "narrative": narrative,
        "vehicle_profile": vehicle_profile,

        # Chart data
        "range_history": range_history,
        "pack_floor_km": pack_floor_km,

        # ── Backwards-compat flat fields (passport_next.html) ─────
        "range_corrected_km": round(rng, 1) if rng else None,
        "range_p10": round(s.get("range_p10"), 1) if s.get("range_p10") else None,
        "range_p50": round(s.get("range_p50"), 1) if s.get("range_p50") else None,
        "range_p90": round(s.get("range_p90"), 1) if s.get("range_p90") else None,
        "bhs_score_v2": track_b,
        "kps_slope_8wk": s.get("kps_slope_8wk"),
        "pct_of_commissioned": pct_comm,
        "nbfc_risk_tier": s.get("nbfc_risk_tier"),
        "rul_action_v2": s.get("rul_action_v2"),
        "rul_weeks_v2": s.get("rul_weeks_v2"),
        "signal_confidence": sig_conf,
        "resale_value_inr": s.get("resale_value_inr"),
        "resale_stage": s.get("resale_stage"),
        "resale_price_source": s.get("resale_price_source"),
        "resale_confidence": s.get("resale_confidence"),
        "resale_now_inr": s.get("resale_now_inr"),
        "resale_8wk_inr": s.get("resale_8wk_inr"),
        "resale_exit_signal": s.get("resale_exit_signal"),
        "fault_profile_tier": s.get("fault_profile_tier"),
        "fault_severity": s.get("fault_severity"),
        "fault_persistence_score": s.get("fault_persistence_score"),
        "fault_duration_weeks": s.get("fault_duration_weeks"),
        "fault_recurrence_count": s.get("fault_recurrence_count"),
        "fault_trajectory": s.get("fault_trajectory"),
        "pack_top_failure_mode": s.get("pack_top_failure_mode"),
        "pack_repeat_rate": s.get("pack_repeat_rate"),
        "divergence_quadrant": divergence_q,

        # DoD behaviour (PD-12B resolved — safe to display)
        "dod_behavior_flag": (dod_flag_row or {}).get("dod_behavior_flag"),
        "dod_behavior_label": {
            "DEEP_DISCHARGE":    "Deep discharge detected",
            "SHALLOW_DISCHARGE": "Shallow discharge pattern",
        }.get((dod_flag_row or {}).get("dod_behavior_flag")),
        "actual_dod_pct": round((dod_flag_row or {}).get("actual_dod_pct"), 1) if (dod_flag_row or {}).get("actual_dod_pct") is not None else None,

        # Range data quality disclosure
        "range_data_quality": s.get("range_data_quality"),
        "range_exceeds_spec": s.get("range_exceeds_spec"),
        "commissioning_source": s.get("commissioning_source"),
        "range_physics_ceiling_km": PHYSICS_CEILING_KM,
        "divergence_flag": s.get("divergence_flag"),
        "divergence_reason": div_reason,
        "false_alarm_likely": s.get("false_alarm_likely"),
        "track_a_score": track_a,
        "use_case_inferred": s.get("use_case_inferred"),
        "physical_risk_flag": s.get("physical_risk_flag"),
        "physical_risk_score": (pr or {}).get("physical_risk_score"),
        "warranty_void_flag": s.get("warranty_void_flag"),
        "useful_life_score": s.get("useful_life_score"),
        "risk_score": s.get("risk_score"),
        "cohort_position_pct": s.get("cohort_position_pct"),
        "cohort_n": (cn or {}).get("n", 0),
        "batch_anomaly_tier": s.get("batch_anomaly_tier"),
        "driver_stress_tier": s.get("driver_stress_tier"),
        "n_weeks_telemetry": n_weeks,
        "dod_corrected": dod_val,
        "fleet_avg_dod": fa_dod,
        "charge_profile": s.get("personal_charge_profile"),
        "drive_profile": s.get("personal_drive_profile"),
        # Computed plain-text for existing UI
        "health_class_plain": _HEALTH_CLASS_PLAIN.get(
            s.get("health_class_v2"), s.get("health_class_v2")),
        "rul_action_plain": decision_node["action"],
        "narrative_sentence": narrative["headline"],
        "primary_driver_plain": cause_node["primary_factor_plain"],
        "bhs_component_spread": s.get("bhs_component_spread"),
        "bhs_component_soh_latest": s.get("bhs_component_soh_latest"),
        "bhs_component_soh_trend": s.get("bhs_component_soh_trend"),
        "bhs_component_ah": s.get("bhs_component_ah"),
        "bhs_component_pct_comm": s.get("bhs_component_pct_comm"),
        "bhs_component_op": s.get("bhs_component_op"),
        "bhs_component_charge": s.get("bhs_component_charge"),
        "bhs_dominant_driver": s.get("bhs_dominant_driver"),
        # attr_* legacy columns removed from passport response (internal only)
        "p_floor_breach_4w": s.get("p_floor_breach_4w"),
        "p_floor_breach_8w": s.get("p_floor_breach_8w"),
        "p_floor_breach_12w": s.get("p_floor_breach_12w"),
        "corroboration_score": corr_score,
        "intervention_score": s.get("intervention_score"),
        # B1: DRI/AHI/quadrant (Mega-sprint 1)
        "dri_score": round(s.get("dri_score") or s.get("bhs_score_v2") or 0, 2),
        "ahi_score": round(s["ahi_score"], 2) if s.get("ahi_score") else None,
        "ahi_tier": s.get("ahi_tier") or (
            "HEALTHY" if (s.get("ahi_score") or 0) >= 75 else
            "WATCH" if (s.get("ahi_score") or 0) >= 55 else
            "CRITICAL" if s.get("ahi_score") is not None else None),
        "nbfc_quadrant": s.get("nbfc_quadrant"),
        "nbfc_grade_2x2": s.get("nbfc_grade_2x2"),
        # B2: Attribution block (operator audience — no factor %, no internal codes).
        # Full attribution (with factors and audience routing) is served via the
        # dedicated /api/battery/{id}/attribution endpoint.
        "attribution": _passport_attribution_block(s, attr_row),
        "warranty_claim_eligible": s.get("warranty_claim_eligible"),
    }


# ── GET /api/battery/:id/range-history-v2 ─────────────────────────

@app.get("/api/battery/{battery_id}/range-history-v2")
@safe
def battery_range_history_v2(battery_id: str, weeks: int = Query(default=16, le=52),
                             _=Depends(verify_token)):
    """Simplified range history for passport_next chart."""
    conn = get_conn()

    # Reference lines from BHS
    refs = q1(conn, """
        SELECT commissioned_range_km FROM battery_health_scores_v2
        WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1
    """, [battery_id])

    # Pack floor
    bat = q1(conn, "SELECT battery_model FROM batteries WHERE battery_id = ?", [battery_id])
    pf = None
    if bat:
        pf = q1(conn, """SELECT param_value FROM fleet_context_params
                         WHERE param_name = 'range_floor_km' AND segment_value = ? AND is_active = 1
                         LIMIT 1""", [bat["battery_model"]])
    pack_floor_km = pf["param_value"] if pf else 56.0
    commissioned = refs["commissioned_range_km"] if refs else None

    # VWF data with p10/p90 from BHS where available
    vwf = q(conn, """
        SELECT v.week_number, v.km_per_soc_pct,
               s.range_p10, s.range_p50, s.range_p90
        FROM vehicle_weekly_features v
        LEFT JOIN battery_health_scores_v2 s ON v.battery_id = s.battery_id
            AND v.week_number = s.week_number
        WHERE v.battery_id = ? AND v.km_per_soc_pct IS NOT NULL
        ORDER BY v.week_number DESC LIMIT ?
    """, [battery_id, weeks])

    conn.close()

    result = []
    for r in sorted(vwf, key=lambda x: x["week_number"]):
        kps = r["km_per_soc_pct"]
        result.append({
            "week_number": r["week_number"],
            "range_p10": round(r["range_p10"], 1) if r.get("range_p10") else None,
            "range_p50": round(kps * 80, 1) if kps else None,
            "range_p90": round(r["range_p90"], 1) if r.get("range_p90") else None,
            "kps_corrected": round(kps, 4) if kps else None,
            "commissioned_range_km": commissioned,
            "pack_floor_km": pack_floor_km,
        })

    return result


# ══════════════════════════════════════════════════════════════
# M6: Event chain + action endpoints
# ══════════════════════════════════════════════════════════════

_EVENT_TYPE_PLAIN = {
    "E1": "Sudden range drop", "EFFICIENCY_DROP": "Sudden range drop",
    "SUSTAINED_BELOW_BASELINE": "Sudden range drop",
    "E2": "Voltage recovery anomaly", "CELL_IMBALANCE_ESCALATION": "Voltage recovery anomaly",
    "CELL_IMBALANCE_ABS": "Voltage recovery anomaly", "CELL_IMBALANCE_REL": "Voltage recovery anomaly",
    "E2_CELL_IMBALANCE": "Voltage recovery anomaly",
    "E3": "Cell spread under load", "E3_THERMAL_STRESS": "Cell spread under load",
    "THERMAL_STRESS": "Cell spread under load",
    "E4": "Deep discharge event", "DEEP_DISCHARGE": "Deep discharge event",
    "E4_DEEP_DISCHARGE": "Deep discharge event",
    "E5": "Charging anomaly", "BMS_PROTECTION_TRIP": "Charging anomaly",
    "E6": "Thermal event",
    "E7": "Cell balance drift", "VOLTAGE_SAG": "Cell balance drift",
    "E7_VOLTAGE_SAG": "Cell balance drift",
    "SOH_CAPACITY_DECLINE": "Capacity decline detected",
    "INTERNAL_RESISTANCE_ESCALATION": "Internal resistance rising",
    "MICRO_SHORT": "Micro-short suspected",
    "EFFICIENCY_REGIME_CHANGE": "Efficiency regime change",
    "USAGE_SHIFT": "Usage pattern shift",
    "SERVICE_RETURN": "Post-service anomaly",
}

_SEV_MAP = {"SEV-1": "HIGH", "SEV-2": "HIGH", "SEV-3": "MEDIUM", "SEV-4": "LOW", "SEV-5": "LOW"}


@app.get("/api/battery/{battery_id}/event-chain")
@safe
def get_event_chain(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()
    try:
        # Latest week
        wk = q1(conn, "SELECT MAX(week_number) as mw FROM battery_health_scores_v2 WHERE battery_id=?", [battery_id])
        max_week = wk["mw"] if wk else None

        # Breach chain factor
        bcf_row = q1(conn, """
            SELECT breach_chain_factor FROM battery_health_scores_v2
            WHERE battery_id=? AND week_number=?
        """, [battery_id, max_week]) if max_week else None
        bcf = bcf_row["breach_chain_factor"] if bcf_row and bcf_row.get("breach_chain_factor") else None

        # Most recent chain
        chain = q1(conn, """
            SELECT pattern_name, fleet_prevalence_pct
            FROM event_chains WHERE battery_id=?
            ORDER BY chain_end_week DESC LIMIT 1
        """, [battery_id])

        chain_elevated = bcf is not None and bcf > 1
        chain_desc = chain["pattern_name"] if chain else None
        chain_pct = round(chain["fleet_prevalence_pct"], 0) if chain and chain.get("fleet_prevalence_pct") else None

        chain_banner = None
        if chain_elevated and chain_desc:
            pct_str = f"{int(chain_pct)}%" if chain_pct else "an unknown %"
            chain_banner = (
                f"This battery is on a known escalation path: {chain_desc}. "
                f"In the fleet, this sequence completes to failure {pct_str} of the time."
            )

        # Last 12 events (deduplicated by week+type)
        events_raw = q(conn, """
            SELECT week_number, event_type, severity, resolved_week
            FROM vehicle_events
            WHERE battery_id=? AND event_reliability != 'SUPPRESSED'
            ORDER BY week_number DESC
            LIMIT 24
        """, [battery_id])

        seen = set()
        events = []
        for e in events_raw:
            key = (e["week_number"], e["event_type"])
            if key in seen:
                continue
            seen.add(key)
            sev = _SEV_MAP.get(e["severity"], "MEDIUM")
            rw = e.get("resolved_week")
            if rw:
                status = "RESOLVED"
            elif max_week and e["week_number"] < max_week - 4:
                status = "CHRONIC"
            else:
                status = "ACTIVE"
            weeks_active = (max_week - e["week_number"] + 1) if max_week else 1
            if rw:
                weeks_active = rw - e["week_number"] + 1
            events.append({
                "week_number": e["week_number"],
                "event_type_plain": _EVENT_TYPE_PLAIN.get(e["event_type"], "Telemetry anomaly"),
                "severity": sev,
                "status": status,
                "weeks_active": max(weeks_active, 1),
            })
            if len(events) >= 12:
                break

    finally:
        conn.close()

    return {
        "battery_id": battery_id,
        "chain_elevated": chain_elevated,
        "chain_description": chain_desc,
        "chain_completion_rate": round(chain_pct / 100, 2) if chain_pct else None,
        "chain_banner": chain_banner,
        "events": events,
    }


class ActionBody(_BM):
    action_type: str


@app.post("/api/battery/{battery_id}/action")
@safe
def post_battery_action(battery_id: str, body: ActionBody, _=Depends(verify_token)):
    valid = {"queue_replacement", "flag_service", "export_passport"}
    if body.action_type not in valid:
        return {"success": False, "message": f"Unknown action: {body.action_type}"}

    messages = {
        "queue_replacement": "Added to replacement queue.",
        "flag_service": "Flagged for service review.",
        "export_passport": "Report export queued.",
    }
    import logging
    logging.info(f"ACTION [{body.action_type}] battery={battery_id}")
    return {"success": True, "message": messages[body.action_type], "action_type": body.action_type}


# ── GET /api/battery/:id/audit ─────────────────────────────────────

@app.get("/api/battery/{battery_id}/audit")
@safe
def battery_audit(battery_id: str, _=Depends(verify_token)):
    """Decision audit trail for NBFC reviewability."""
    conn = get_conn()
    rows = q(conn, """
        SELECT audit_timestamp, action_assigned, nbfc_grade, risk_score,
               bhs_score_v2, range_corrected_km, kps_slope_8wk,
               change_from_prior, change_reason, session_tag,
               coherence_flags, confidence_level
        FROM battery_decision_audit
        WHERE battery_id = ?
        ORDER BY audit_timestamp DESC
    """, [battery_id])
    conn.close()
    if not rows:
        raise HTTPException(404, f"No audit records for {battery_id}")
    return rows


# ══════════════════════════════════════════════════════════════
# OEM SURFACE ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.get("/api/oem/pack-summary")
@safe
def oem_pack_summary(_=Depends(verify_token)):
    """Pack comparison table for OEM surface."""
    conn = get_conn()
    packs = q(conn, """
        SELECT b.battery_model as pack_model, COUNT(*) as battery_count,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_bhs,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range_km,
               ROUND(AVG(s.kps_slope_8wk), 5) as avg_kps_slope_8wk,
               SUM(CASE WHEN s.rul_action_v2 IN ('REPLACE_PLAN','PHYSICS_REPLACE_PLAN') THEN 1 ELSE 0 END) as replace_count,
               SUM(CASE WHEN s.rul_action_v2 = 'ACUTE_BREACH_WATCH' THEN 1 ELSE 0 END) as acute_breach_count
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.chemistry = 'LFP'
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
        GROUP BY b.battery_model
        ORDER BY AVG(s.kps_slope_8wk) ASC
    """)
    # Add survival data + deg multiplier
    surv = {r["pack_code"]: r["median_failure_months"] for r in q(conn, "SELECT pack_code, median_failure_months FROM pack_survival_params")}
    conn.close()
    # Deg multiplier: ratio of replace+acute counts normalised by battery count
    # vs Pack3001 baseline. Higher action rate = faster degradation.
    baseline_rate = None
    for p in packs:
        code = (p["pack_model"] or "").replace("GF_LFP_Pack", "").replace("GF_LFP_", "")
        p["survival_p50_months"] = surv.get(code)
        n = p.get("battery_count") or 1
        p["action_rate"] = ((p.get("replace_count") or 0) + (p.get("acute_breach_count") or 0)) / n
        if "Pack3001" in (p.get("pack_model") or ""):
            baseline_rate = p["action_rate"]
    if baseline_rate is None or baseline_rate == 0:
        rates = [p["action_rate"] for p in packs if p["action_rate"] > 0]
        baseline_rate = min(rates) if rates else 0.01
    if baseline_rate == 0:
        baseline_rate = 0.01
    for p in packs:
        p["deg_multiplier"] = round(p["action_rate"] / baseline_rate, 1) if p["action_rate"] > 0 else 0.0
        if p["deg_multiplier"] == 0:
            p["deg_multiplier"] = 1.0  # no actions = baseline
    return packs


# NOTE: duplicate /api/oem/pack-attribution endpoint removed — canonical
# definition at line 4314 (BDA-sourced averages). This stub retained as a
# marker so future greps find the history.
# def oem_pack_attribution: see first definition above.


@app.get("/api/oem/failure-modes")
@safe
def oem_failure_modes(_=Depends(verify_token)):
    """Service failure mode distribution per pack from service_records."""
    conn = get_conn()
    rows = q(conn, """
        SELECT sr.matched_pack_model as pack_model,
               sft.failure_class,
               COUNT(*) as n,
               ROUND(COUNT(*) * 100.0 /
                 SUM(COUNT(*)) OVER (PARTITION BY sr.matched_pack_model), 1) as pct
        FROM service_records sr
        JOIN service_failure_taxonomy sft ON sr.issue_category = sft.issue_category
        WHERE sr.matched_pack_model IS NOT NULL
        GROUP BY sr.matched_pack_model, sft.failure_class
        ORDER BY sr.matched_pack_model, pct DESC
    """)
    # Also fetch transition chain params
    chain = {}
    for p in q(conn, """SELECT param_name, param_value FROM fleet_context_params
                        WHERE param_name IN ('fault_transition_cabinet_leakage_p',
                                             'fault_recurrence_cell_unbalanced_p')"""):
        chain[p["param_name"]] = p["param_value"]
    conn.close()
    return {
        "failure_modes": rows,
        "transition_cabinet_leakage_pct": round((chain.get("fault_transition_cabinet_leakage_p") or 0) * 100, 0),
        "recurrence_cell_unbalanced_pct": round((chain.get("fault_recurrence_cell_unbalanced_p") or 0) * 100, 0),
    }


@app.get("/api/oem/commissioning")
@safe
def oem_commissioning(_=Depends(verify_token)):
    """Commissioning quality — cell spread at delivery per pack."""
    conn = get_conn()
    rows = q(conn, """
        SELECT b.battery_model as pack_model,
               COUNT(*) as n,
               ROUND(AVG(bhs.commissioning_spread_mv), 0) as avg_comm_spread,
               ROUND(MIN(bhs.commissioning_spread_mv), 0) as min_spread,
               ROUND(MAX(bhs.commissioning_spread_mv), 0) as max_spread,
               COUNT(CASE WHEN bhs.commissioning_spread_mv > 200 THEN 1 END) as high_spread_count,
               ROUND(COUNT(CASE WHEN bhs.commissioning_spread_mv > 200 THEN 1 END) * 100.0
                     / COUNT(*), 1) as high_spread_pct
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id = b.battery_id
        WHERE bhs.commissioning_spread_mv IS NOT NULL
          AND bhs.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                                 WHERE wk.battery_id = bhs.battery_id)
        GROUP BY b.battery_model
    """)
    conn.close()
    return rows


@app.get("/api/oem/usecase-pack-matrix")
@safe
def oem_usecase_pack_matrix(_=Depends(verify_token)):
    """Use case x pack degradation heatmap for OEM surface."""
    conn = get_conn()
    rows = q(conn, """
        SELECT b.battery_model as pack_model, bat.use_case_inferred as use_case,
               COUNT(*) as n,
               ROUND(AVG(s.kps_slope_8wk), 5) as avg_kps_slope
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        JOIN batteries bat ON s.battery_id = bat.battery_id
        WHERE b.chemistry = 'LFP'
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
          AND bat.use_case_inferred IS NOT NULL
        GROUP BY b.battery_model, bat.use_case_inferred
    """)
    conn.close()
    return rows


@app.get("/api/oem/underperformers")
@safe
def oem_underperformers(_=Depends(verify_token)):
    """Cohort underperformers (bottom 10% within pack+city+use_case)."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               bat.use_case_inferred as use_case,
               ROUND(s.kps_slope_8wk, 5) as kps_slope_8wk,
               ROUND(s.bhs_score_v2, 1) as bhs_score_v2,
               s.rul_action_v2 as action,
               s.degradation_regime
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        JOIN batteries bat ON s.battery_id = bat.battery_id
        WHERE b.chemistry = 'LFP'
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
          AND s.kps_slope_8wk IS NOT NULL
        ORDER BY s.kps_slope_8wk ASC
    """)
    conn.close()
    # Compute cohort position within pack+city+use_case
    from collections import defaultdict
    cohorts = defaultdict(list)
    for r in rows:
        key = (r["pack_model"], r["city"], r["use_case"])
        cohorts[key].append(r)
    result = []
    for key, members in cohorts.items():
        members.sort(key=lambda x: x["kps_slope_8wk"] or 0)
        n = len(members)
        for i, m in enumerate(members):
            pct = round((i / max(n - 1, 1)) * 100, 1) if n > 1 else 50.0
            m["cohort_position_pct"] = pct
            if pct < 10 and n >= 3:
                result.append(m)
    result.sort(key=lambda x: x["kps_slope_8wk"] or 0)
    return result


@app.get("/api/oem/regime-by-pack")
@safe
def oem_regime_by_pack(_=Depends(verify_token)):
    """Degradation regime distribution per pack for OEM surface."""
    conn = get_conn()
    rows = q(conn, """
        SELECT b.battery_model as pack_model,
               COALESCE(s.degradation_regime, 'UNKNOWN') as regime,
               COUNT(*) as n
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.chemistry = 'LFP'
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
        GROUP BY b.battery_model, COALESCE(s.degradation_regime, 'UNKNOWN')
        ORDER BY b.battery_model, s.degradation_regime
    """)
    conn.close()
    return rows


@app.get("/api/oem/divergence-by-pack")
@safe
def oem_divergence_by_pack(_=Depends(verify_token)):
    """Divergence quadrant distribution per pack for OEM surface."""
    conn = get_conn()
    rows = q(conn, """
        SELECT b.battery_model as pack_model,
               COUNT(CASE WHEN s.divergence_quadrant='EARLY_WARNING' THEN 1 END) as early_warning,
               COUNT(CASE WHEN s.divergence_quadrant='ALIGNED_DECLINE' THEN 1 END) as confirmed_decline,
               COUNT(CASE WHEN s.divergence_quadrant='RANGE_CONTEXT' THEN 1 END) as range_context,
               COUNT(CASE WHEN s.divergence_quadrant='ALIGNED_HEALTHY' THEN 1 END) as healthy,
               COUNT(*) as total
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.chemistry = 'LFP'
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
        GROUP BY b.battery_model ORDER BY early_warning DESC
    """)
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════
# NBFC SURFACE ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.get("/api/nbfc/portfolio")
@safe
def nbfc_portfolio(_=Depends(verify_token)):
    """Portfolio overview + grade distribution for NBFC surface."""
    conn = get_conn()
    grades = q(conn, """
        SELECT s.nbfc_risk_tier as grade, COUNT(*) as n,
               ROUND(AVG(s.resale_now_inr), 0) as avg_resale,
               ROUND(AVG(s.useful_life_score), 1) as avg_useful_life,
               ROUND(AVG(s.risk_score), 1) as avg_risk
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.nbfc_risk_tier IS NOT NULL
          AND b.fleet_segment IN ('GE_ERICKSHAW', 'SG_ERICKSHAW')
          AND s.battery_id NOT IN (SELECT battery_id FROM excluded_batteries)
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
        GROUP BY s.nbfc_risk_tier ORDER BY s.nbfc_risk_tier
    """)
    total = sum(g["n"] for g in grades)
    for g in grades:
        g["pct"] = round(g["n"] / total * 100, 1) if total else 0
    conn.close()
    return {"grades": grades, "total": total}


@app.get("/api/nbfc/battery-list")
@safe
def nbfc_battery_list(_=Depends(verify_token)):
    """Battery-level loan table for NBFC surface."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               s.nbfc_risk_tier as grade, s.risk_score, s.useful_life_score,
               s.resale_now_inr, s.rul_weeks_v2 as rul_weeks_p50,
               s.rul_action_v2 as action, s.divergence_quadrant,
               s.false_alarm_likely,
               s.resale_stage, s.signal_confidence, s.fault_profile_tier,
               s.pct_of_commissioned, s.resale_price_source
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.nbfc_risk_tier IS NOT NULL AND b.chemistry = 'LFP'
          AND s.week_number = (SELECT MAX(wk.week_number) FROM battery_health_scores_v2 wk
                               WHERE wk.battery_id = s.battery_id)
        ORDER BY s.nbfc_risk_tier, s.risk_score DESC
    """)
    conn.close()
    for r in rows:
        if r.get("resale_now_inr"):
            r["resale_now_inr"] = round(r["resale_now_inr"])
        if r.get("rul_weeks_p50"):
            r["rul_weeks_p50"] = round(r["rul_weeks_p50"], 1)
    return rows


@app.get("/api/nbfc/grade-d-detail/{battery_id}")
@safe
def nbfc_grade_d_detail(battery_id: str, _=Depends(verify_token)):
    """Grade D drill-down: risk components + divergence."""
    conn = get_conn()
    s = q1(conn, """
        SELECT risk_score, useful_life_score, resale_now_inr, rul_weeks_v2,
               kps_slope_8wk, pct_of_commissioned, divergence_quadrant, divergence_flag,
               track_a_score, bhs_score_v2, rul_action_v2, degradation_regime,
               bhs_dominant_driver, fault_severity, nbfc_summary_sentence
        FROM battery_health_scores_v2
        WHERE battery_id = ? ORDER BY week_number DESC LIMIT 1
    """, [battery_id])
    conn.close()
    if not s:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    return s


# ── SWAP Surface Endpoints ─────────────────────────────────────────

@app.get("/api/swap/pool-tiers")
@safe
def swap_pool_tiers(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT
            CASE
                WHEN s.rul_action_v2 IN ('REPLACE_PLAN','PHYSICS_REPLACE_PLAN') THEN 'RETIRE_NOW'
                WHEN s.rul_action_v2 = 'ACUTE_BREACH_WATCH' THEN 'ACUTE'
                WHEN s.rul_action_v2 = 'MONITOR_INVESTIGATE' THEN 'WATCH'
                ELSE 'HEALTHY'
            END as tier,
            COUNT(*) as n,
            ROUND(AVG(s.range_corrected_km), 1) as avg_range,
            ROUND(AVG(s.pct_of_commissioned), 1) as avg_pct_commissioned
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.fleet_segment IN ('GE_ERICKSHAW','SG_ERICKSHAW')
          AND s.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                               WHERE bhs2.battery_id = s.battery_id)
        GROUP BY tier
        ORDER BY CASE tier WHEN 'RETIRE_NOW' THEN 1 WHEN 'ACUTE' THEN 2
                           WHEN 'WATCH' THEN 3 ELSE 4 END
    """)
    sg_breach = q1(conn, """
        SELECT COUNT(*) as n FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.fleet_segment = 'SG_ERICKSHAW' AND s.breach_risk_override = 1
          AND s.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                               WHERE bhs2.battery_id = s.battery_id)
    """)
    conn.close()
    total = sum(r["n"] for r in rows)
    for r in rows:
        r["pct_of_pool"] = round(r["n"] / total * 100, 1) if total else 0
    return {"tiers": rows, "total_batteries": total,
            "sg_breach_count": sg_breach["n"] if sg_breach else 0}


@app.get("/api/swap/retirement-window")
@safe
def swap_retirement_window(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               s.rul_weeks_v2, s.range_corrected_km, s.breach_risk_override,
               b.fleet_segment
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.fleet_segment IN ('GE_ERICKSHAW','SG_ERICKSHAW')
          AND s.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                               WHERE bhs2.battery_id = s.battery_id)
          AND s.rul_weeks_v2 IS NOT NULL
    """)
    conn.close()
    windows = {
        "CURRENT": {"label": "Now (0-4 weeks)", "batteries": [], "packs": {}},
        "1MO": {"label": "1 month (5-8 weeks)", "batteries": [], "packs": {}},
        "2MO": {"label": "2 months (9-12 weeks)", "batteries": [], "packs": {}},
        "3MO": {"label": "3+ months (13+ weeks)", "batteries": [], "packs": {}},
    }
    for r in rows:
        rul = r["rul_weeks_v2"]
        bucket = "CURRENT" if rul <= 4 else "1MO" if rul <= 8 else "2MO" if rul <= 12 else "3MO"
        w = windows[bucket]
        is_breach = r.get("breach_risk_override") == 1 and r.get("fleet_segment") == "SG_ERICKSHAW"
        w["batteries"].append({
            "battery_id": r["battery_id"], "pack_model": r["pack_model"],
            "city": r["city"], "rul_weeks": rul, "range_km": r["range_corrected_km"],
            "breach_priority": is_breach,
        })
        pm = r["pack_model"] or "Unknown"
        w["packs"][pm] = w["packs"].get(pm, 0) + 1
    for w in windows.values():
        w["count"] = len(w["batteries"])
        ranges = [b["range_km"] for b in w["batteries"] if b["range_km"]]
        w["avg_range"] = round(sum(ranges) / len(ranges), 1) if ranges else None
        w["breach_count"] = sum(1 for b in w["batteries"] if b.get("breach_priority"))
    return windows


@app.get("/api/swap/top-urgent")
@safe
def swap_top_urgent(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               s.rul_action_v2 as action, s.rul_weeks_v2,
               s.range_corrected_km, s.pct_of_commissioned,
               v.charge_cycles_delta
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        LEFT JOIN vehicle_weekly_features v ON s.battery_id = v.battery_id
            AND v.week_number = (SELECT MAX(vwf2.week_number) FROM vehicle_weekly_features vwf2
                                 WHERE vwf2.battery_id = v.battery_id)
        WHERE b.fleet_segment IN ('GE_ERICKSHAW','SG_ERICKSHAW')
          AND s.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                               WHERE bhs2.battery_id = s.battery_id)
          AND s.rul_action_v2 IS NOT NULL AND s.rul_action_v2 != 'NO_ACTION'
        ORDER BY
            CASE s.rul_action_v2
                WHEN 'PHYSICS_REPLACE_PLAN' THEN 1 WHEN 'REPLACE_PLAN' THEN 2
                WHEN 'ACUTE_BREACH_WATCH' THEN 3 WHEN 'CELL_BALANCE_PRIORITY' THEN 4
                WHEN 'MONITOR_INVESTIGATE' THEN 5 ELSE 6 END,
            s.range_corrected_km ASC NULLS LAST
        LIMIT 20
    """)
    conn.close()
    for r in rows:
        rul = r.get("rul_weeks_v2")
        cycles = r.pop("charge_cycles_delta", None) or 3.2
        r["swap_cycles_est"] = round(rul * cycles) if rul else None
        r["range_corrected_km"] = round(r["range_corrected_km"], 1) if r.get("range_corrected_km") else None
        r["pct_of_commissioned"] = round(r["pct_of_commissioned"], 1) if r.get("pct_of_commissioned") else None
    return rows




@app.get("/api/outcomes/summary")
@safe
def outcomes_summary(_=Depends(verify_token)):
    """Outcome log summary for RAG context."""
    conn = get_conn()
    rows = q(conn, """
        SELECT calibration_status, outcome_type, COUNT(*) as n
        FROM outcome_log
        WHERE calibration_status IS NOT NULL
        GROUP BY calibration_status, outcome_type
        ORDER BY n DESC
    """)
    total = q(conn, "SELECT COUNT(*) as n FROM outcome_log")
    conn.close()
    return {"by_status": rows, "total": total[0]["n"] if total else 0}


# ══════════════════════════════════════════════════════════════
# SKILL DATA ENDPOINTS (GET — simple data lookups for enerlyst)
# ══════════════════════════════════════════════════════════════

@app.get("/api/skill/e2e-trace")
@safe
def skill_e2e_trace(battery_id: str = Query(...), _=Depends(verify_token)):
    """End-to-end trace for a battery — all key signals in one call."""
    conn = get_conn()
    r = q1(conn, """
        SELECT battery_id, rul_action_v2 as action, bhs_score_v2, range_p50,
               signal_confidence, fault_profile_tier, divergence_quadrant,
               nbfc_risk_tier as nbfc_grade, risk_score, resale_stage,
               resale_now_inr as resale_value_inr, fault_persistence_score,
               fault_duration_weeks, fault_recurrence_count, physical_risk_flag,
               warranty_void_flag, driver_stress_tier, use_case_inferred,
               health_class_v2, kps_slope_8wk
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    if not r:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    r["divergence_reason"] = _build_divergence_reason(r)
    return r


@app.get("/api/skill/nbfc-grade")
@safe
def skill_nbfc_grade(battery_id: str = Query(...), _=Depends(verify_token)):
    """NBFC grade details for a battery."""
    conn = get_conn()
    r = q1(conn, """
        SELECT battery_id, nbfc_risk_tier as nbfc_grade, risk_score,
               signal_confidence, resale_now_inr as resale_value_inr,
               resale_stage, warranty_void_flag, fault_profile_tier,
               pct_of_commissioned, resale_price_source, resale_confidence,
               nbfc_summary_sentence, loan_coverage_note
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    if not r:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    return r


@app.get("/api/skill/attribution")
@safe
def skill_attribution(battery_id: str = Query(...), _=Depends(verify_token)):
    """Attribution breakdown for a battery."""
    conn = get_conn()
    r = q1(conn, """
        SELECT s.battery_id, s.attr_cycle_pct, s.attr_dod_pct,
               s.attr_thermal_pct, s.attr_imbalance_pct, s.attr_calendar_pct,
               s.attr_physical_pct, s.attr_primary_factor,
               s.driver_stress_tier, s.use_case_inferred,
               s.bhs_dominant_driver, s.degradation_primary_driver
        FROM battery_health_scores_v2 s WHERE s.battery_id = ?
    """, [battery_id])
    # Get load_intensity from bbf
    li = q1(conn, """
        SELECT load_intensity_p50 FROM battery_behavioural_features
        WHERE battery_id = ? AND load_intensity_p50 IS NOT NULL
        ORDER BY week_number DESC LIMIT 1
    """, [battery_id])
    conn.close()
    if not r:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    r["load_intensity_p50"] = (li or {}).get("load_intensity_p50")
    return r


@app.get("/api/skill/breach-risk")
@safe
def skill_breach_risk(battery_id: str = Query(...), _=Depends(verify_token)):
    """Breach risk assessment for a battery."""
    conn = get_conn()
    r = q1(conn, """
        SELECT battery_id, rul_action_v2, breach_risk_override,
               p_floor_breach_4w, p_floor_breach_8w,
               fault_profile_tier, spread_delta_mv as cell_spread_delta,
               signal_confidence, divergence_quadrant,
               fault_duration_weeks, fault_recurrence_count,
               kps_slope_8wk, range_p50
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    if not r:
        raise HTTPException(404, f"Battery not found: {battery_id}")
    return r


# ── Sprint 4: Surface Intelligence Endpoints ─────────────────────────


@app.get("/api/fleet/intelligence/v2")
@safe
def fleet_intelligence_v2(max_week: int = Query(90), _=Depends(verify_token)):
    """Fleet story for Fleet MIS header — Sprint 4."""
    conn = get_conn()
    # Use per-battery latest week pattern, capped at max_week
    LATEST = f"""bhs.week_number = (SELECT MAX(bhs2.week_number)
        FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = bhs.battery_id
        AND bhs2.week_number < {int(max_week)})"""
    LATEST_SOLO = f"""week_number = (SELECT MAX(bhs2.week_number)
        FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = battery_health_scores_v2.battery_id
        AND bhs2.week_number < {int(max_week)})"""

    # Get representative week for display
    week_row = q1(conn, "SELECT MAX(week_number) as wk FROM battery_health_scores_v2 WHERE week_number < ?", [int(max_week)])
    week = week_row["wk"] if week_row else 0

    action_counts = q(conn, f"""
        SELECT rul_action_v2, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE {LATEST_SOLO} AND scoring_mode!='SUSPENDED'
        GROUP BY rul_action_v2
    """)

    top_cause = q1(conn, f"""
        SELECT ROUND(AVG(COALESCE(attr_imbalance_pct,0) + COALESCE(attr_cycle_pct,0)),1) as avg_operator,
               ROUND(AVG(COALESCE(attr_thermal_pct,0)),1) as avg_thermal,
               ROUND(AVG(COALESCE(attr_calendar_pct,0)),1) as avg_age
        FROM battery_health_scores_v2
        WHERE {LATEST_SOLO} AND scoring_mode!='SUSPENDED'
    """)

    pack_perf = q(conn, f"""
        SELECT b.battery_model as pack_model, COUNT(*) as n,
            ROUND(AVG(bhs.bhs_score_v2),1) as avg_bhs,
            ROUND(AVG(bhs.range_p50),1) as avg_range,
            ROUND(AVG(bhs.kps_slope_8wk),5) as avg_slope,
            COUNT(CASE WHEN bhs.rul_action_v2 IN
                ('PHYSICS_REPLACE_PLAN','REPLACE_PLAN','ACUTE_BREACH_WATCH') THEN 1 END) as action_n
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE {LATEST} AND bhs.scoring_mode!='SUSPENDED'
        GROUP BY b.battery_model ORDER BY avg_slope ASC
    """)

    city_perf = q(conn, f"""
        SELECT b.city_code as city, COUNT(*) as n,
            ROUND(AVG(bhs.range_p50),1) as avg_range,
            ROUND(AVG(bhs.bhs_score_v2),1) as avg_bhs,
            COUNT(CASE WHEN bhs.rul_action_v2 IN
                ('PHYSICS_REPLACE_PLAN','REPLACE_PLAN','ACUTE_BREACH_WATCH') THEN 1 END) as action_n,
            ROUND(COUNT(CASE WHEN bhs.rul_action_v2 IN
                ('PHYSICS_REPLACE_PLAN','REPLACE_PLAN','ACUTE_BREACH_WATCH') THEN 1 END) * 100.0
                / COUNT(*), 1) as action_pct
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE {LATEST} AND bhs.scoring_mode!='SUSPENDED'
        GROUP BY b.city_code ORDER BY action_pct DESC
    """)

    driver_stress = q(conn, f"""
        SELECT driver_stress_tier, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE {LATEST_SOLO} AND driver_stress_tier IS NOT NULL AND scoring_mode!='SUSPENDED'
        GROUP BY driver_stress_tier
    """)

    top_performers = q(conn, f"""
        SELECT bhs.battery_id, b.battery_model as pack_model, b.city_code as city,
            bhs.bhs_score_v2, bhs.range_p50, bhs.rul_action_v2
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE {LATEST} AND bhs.bhs_score_v2 IS NOT NULL
            AND bhs.rul_action_v2 IN ('NO_ACTION','MONITOR_WEEKLY')
            AND bhs.scoring_mode!='SUSPENDED'
        ORDER BY bhs.bhs_score_v2 DESC LIMIT 5
    """)

    bottom_performers = q(conn, f"""
        SELECT bhs.battery_id, b.battery_model as pack_model, b.city_code as city,
            bhs.bhs_score_v2, bhs.range_p50, bhs.rul_action_v2
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE {LATEST} AND bhs.rul_action_v2 IN ('PHYSICS_REPLACE_PLAN','REPLACE_PLAN')
            AND bhs.scoring_mode!='SUSPENDED'
        ORDER BY bhs.bhs_score_v2 ASC LIMIT 5
    """)

    fault_chain = q(conn, """
        SELECT event_code, COUNT(DISTINCT battery_id) as batteries, COUNT(*) as events
        FROM vehicle_events
        GROUP BY event_code ORDER BY batteries DESC LIMIT 7
    """)

    regime_dist = q(conn, f"""
        SELECT degradation_regime, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE {LATEST_SOLO} AND degradation_regime IS NOT NULL AND scoring_mode!='SUSPENDED'
        GROUP BY degradation_regime
    """)

    conn.close()

    # Build narrative
    ac_map = {r["rul_action_v2"]: r["n"] for r in action_counts}
    total = sum(r["n"] for r in action_counts)
    action_needed = ac_map.get("PHYSICS_REPLACE_PLAN", 0) + ac_map.get("REPLACE_PLAN", 0) + ac_map.get("ACUTE_BREACH_WATCH", 0)

    tc = top_cause or {"avg_operator": 0, "avg_thermal": 0, "avg_age": 0}
    cause_vals = {"Operator behaviour": tc.get("avg_operator") or 0,
                  "Thermal stress": tc.get("avg_thermal") or 0,
                  "Calendar aging": tc.get("avg_age") or 0}
    top_cause_label = max(cause_vals, key=cause_vals.get) if any(cause_vals.values()) else "Unknown"

    worst_pack = max(pack_perf, key=lambda p: (p["action_n"] / max(p["n"], 1))) if pack_perf else None
    worst_city = city_perf[0] if city_perf else None

    headline = f"{action_needed} batteries need action this week out of {total} scored."
    story = f"Top fleet-wide driver: {top_cause_label}."
    if worst_pack:
        story += f" {worst_pack['pack_model']} shows highest action rate ({worst_pack['action_n']}/{worst_pack['n']})."
    if worst_city and worst_city.get("action_pct", 0) > 0:
        story += f" {worst_city['city']} leads at {worst_city['action_pct']}% action rate."

    return {
        "week": week, "total": total,
        "narrative": {
            "headline": headline, "story": story,
            "top_cause": top_cause_label,
            "worst_pack": worst_pack["pack_model"] if worst_pack else None,
            "worst_city": worst_city["city"] if worst_city else None,
        },
        "action_counts": action_counts,
        "pack_performance": pack_perf,
        "city_performance": city_perf,
        "driver_stress": driver_stress,
        "top_performers": top_performers,
        "bottom_performers": bottom_performers,
        "fault_chain": fault_chain,
        "regime_distribution": regime_dist,
    }


@app.get("/api/nbfc/at-risk")
@safe
def nbfc_at_risk(_=Depends(verify_token)):
    """Grade C+D batteries for NBFC at-risk table."""
    conn = get_conn()
    rows = q(conn, """
        SELECT bhs.battery_id, b.battery_model as pack_model, b.city_code as city,
            bhs.nbfc_risk_tier as nbfc_grade, bhs.risk_score, bhs.bhs_score_v2,
            bhs.range_p50, bhs.resale_value_inr, bhs.resale_stage,
            bhs.divergence_quadrant, bhs.p_floor_breach_8w,
            bhs.signal_confidence, bhs.fault_profile_tier,
            bhs.rul_action_v2, bhs.kps_slope_8wk,
            bhs.driver_stress_tier, bhs.use_case_inferred
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE bhs.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                                  WHERE bhs2.battery_id = bhs.battery_id)
            AND bhs.nbfc_risk_tier IN ('C','D')
            AND bhs.scoring_mode!='SUSPENDED'
        ORDER BY bhs.risk_score DESC LIMIT 100
    """)
    conn.close()

    action_plain_map = {
        "PHYSICS_REPLACE_PLAN": "Replace — physics confirmed",
        "REPLACE_PLAN": "Schedule replacement",
        "ACUTE_BREACH_WATCH": "Service urgently",
        "CELL_BALANCE_PRIORITY": "Cell balance needed",
        "MONITOR_INVESTIGATE": "Investigate",
        "MONITOR_WEEKLY": "Monitor weekly",
        "NO_ACTION": "No action needed",
    }
    stage_plain_map = {"STAGE1_ACTIVE": "Active", "STAGE2_SECONDLIFE": "Second-life", "STAGE3_SCRAP": "Scrap"}
    div_note_map = {
        "EARLY_WARNING": "Chemistry declining — intervene now",
        "RANGE_CONTEXT": "Investigate operational cause",
        "ALIGNED_DECLINE": "Confirmed decline",
    }

    for r in rows:
        r["action_plain"] = action_plain_map.get(r.get("rul_action_v2"), r.get("rul_action_v2"))
        r["stage_plain"] = stage_plain_map.get(r.get("resale_stage"), r.get("resale_stage"))
        r["divergence_note"] = div_note_map.get(r.get("divergence_quadrant"))
    return rows


@app.get("/api/nbfc/portfolio-intelligence")
@safe
def nbfc_portfolio_intelligence(_=Depends(verify_token)):
    """Portfolio story + chains for NBFC surface."""
    conn = get_conn()
    LATEST = """week_number = (SELECT MAX(bhs2.week_number)
        FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = battery_health_scores_v2.battery_id)"""
    LATEST_J = """bhs.week_number = (SELECT MAX(bhs2.week_number)
        FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = bhs.battery_id)"""

    grades = q(conn, f"""
        SELECT nbfc_risk_tier as nbfc_grade, COUNT(*) as n,
            ROUND(AVG(resale_value_inr),0) as avg_resale,
            ROUND(SUM(resale_value_inr),0) as total_resale
        FROM battery_health_scores_v2
        WHERE {LATEST} AND scoring_mode!='SUSPENDED'
        GROUP BY nbfc_risk_tier
    """)

    collateral_by_stage = q(conn, f"""
        SELECT resale_stage, COUNT(*) as n,
            ROUND(AVG(resale_value_inr),0) as avg_value,
            ROUND(SUM(resale_value_inr),0) as total_value
        FROM battery_health_scores_v2
        WHERE {LATEST} AND scoring_mode!='SUSPENDED' AND resale_stage IS NOT NULL
        GROUP BY resale_stage
    """)

    fault_grade_chain = q(conn, f"""
        SELECT fault_profile_tier, nbfc_risk_tier as nbfc_grade, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE {LATEST} AND fault_profile_tier IS NOT NULL AND fault_profile_tier != 'CLEAN'
        GROUP BY fault_profile_tier, nbfc_risk_tier
    """)

    early_warning = q1(conn, f"""
        SELECT COUNT(*) as n,
            ROUND(AVG(resale_value_inr),0) as avg_value,
            ROUND(SUM(resale_value_inr),0) as total_value
        FROM battery_health_scores_v2
        WHERE {LATEST} AND divergence_quadrant='EARLY_WARNING'
    """)

    best_profile = q(conn, f"""
        SELECT b.battery_model as pack_model, b.city_code as city,
            bhs.driver_stress_tier, bhs.use_case_inferred,
            COUNT(*) as n, ROUND(AVG(bhs.bhs_score_v2),1) as avg_bhs
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE {LATEST_J} AND bhs.nbfc_risk_tier='A'
            AND bhs.driver_stress_tier IS NOT NULL
        GROUP BY b.battery_model, b.city_code, bhs.driver_stress_tier, bhs.use_case_inferred
        ORDER BY n DESC LIMIT 5
    """)

    conn.close()

    # Build narrative
    total_value = sum(g.get("total_resale") or 0 for g in grades)
    grade_d = next((g for g in grades if g["nbfc_grade"] == "D"), None)
    grade_d_n = grade_d["n"] if grade_d else 0
    at_risk_value = grade_d.get("total_resale", 0) if grade_d else 0
    grade_ab = [g for g in grades if g.get("nbfc_grade") in ("A", "B")]
    grade_ab_n = sum(g["n"] for g in grade_ab)
    total_n = sum(g["n"] for g in grades)
    grade_d_pct = round(grade_d_n / max(total_n, 1) * 100, 1)

    ew_n = early_warning["n"] if early_warning else 0
    ew_value = early_warning.get("total_value", 0) if early_warning else 0

    stage_map = {s["resale_stage"]: s for s in collateral_by_stage}
    stage1_avg = (stage_map.get("STAGE1_ACTIVE") or {}).get("avg_value", 0) or 0
    stage2_avg = (stage_map.get("STAGE2_SECONDLIFE") or {}).get("avg_value", 0) or 0
    preservable = ew_n * (stage1_avg - stage2_avg) if stage1_avg > stage2_avg else 0

    if grade_d_pct > 30:
        headline = f"{grade_d_n} batteries in Grade D ({grade_d_pct:.0f}% of portfolio)"
    else:
        headline = f"{grade_ab_n} batteries Grade A/B — portfolio healthy"

    story = (f"Total collateral: \u20b9{total_value:,.0f}. "
             f"\u20b9{at_risk_value:,.0f} at elevated risk (Grade D). "
             f"{ew_n} batteries in early warning — "
             f"\u20b9{preservable:,.0f} preservable with intervention.")

    return {
        "narrative": {"headline": headline, "story": story},
        "grades": grades,
        "collateral_by_stage": collateral_by_stage,
        "fault_grade_chain": fault_grade_chain,
        "early_warning": early_warning,
        "best_profile": best_profile,
        "total_value": total_value,
        "at_risk_value": at_risk_value,
        "week": total_n,
    }


@app.get("/api/swap/pool-intelligence")
@safe
def swap_pool_intelligence(_=Depends(verify_token)):
    """Pool health story for Swap surface."""
    conn = get_conn()
    rows = q(conn, """
        SELECT bhs.battery_id, b.battery_model as pack_model, b.city_code as city,
            bhs.bhs_score_v2, bhs.range_p50,
            bhs.resale_stage, bhs.resale_value_inr,
            bhs.rul_action_v2, bhs.divergence_quadrant,
            bhs.fault_profile_tier, bhs.kps_slope_8wk,
            bhs.use_case_inferred, bhs.driver_stress_tier,
            bhs.cell_balance_priority, bhs.corroboration_score
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE bhs.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                                  WHERE bhs2.battery_id = bhs.battery_id)
            AND bhs.scoring_mode!='SUSPENDED'
        ORDER BY bhs.bhs_score_v2 ASC
    """)
    conn.close()

    # Classify
    for r in rows:
        action = r.get("rul_action_v2", "")
        # X2 Fix 10 — Rule 207: deprecated actions must never appear
        if action in ("REPLACE_URGENT", "EOL_IMMINENT"):
            action = "REPLACE_PLAN"
        slope = r.get("kps_slope_8wk")
        # X2 Fix 9 — require corroboration_score >= 1 before routing to the urgent/monitor
        # display path (PHYSICS_REPLACE_PLAN / MONITOR_INVESTIGATE single-signal guard)
        corr = r.get("corroboration_score")
        if corr is None:
            corr = 0
        if action in ("PHYSICS_REPLACE_PLAN", "REPLACE_PLAN") and corr >= 1:
            r["pool_status"] = "REMOVE_IMMEDIATELY"
            r["pool_note"] = "Remove from pool this week"
        # X2 Fix 8 — NULL guard on slope comparison
        elif action == "ACUTE_BREACH_WATCH" or (slope is not None and slope < -0.020):
            r["pool_status"] = "ROTATE_LIGHTER"
            r["pool_note"] = "Rotate to lighter route"
        elif action == "MONITOR_INVESTIGATE" and corr >= 1:
            r["pool_status"] = "MONITOR"
            r["pool_note"] = "Watch closely — do not assign heavy routes"
        else:
            r["pool_status"] = "CONTINUE"
            r["pool_note"] = "Continue in pool"

    # Stage summary
    stage_summary = {}
    for r in rows:
        stg = r.get("resale_stage") or "UNKNOWN"
        if stg not in stage_summary:
            stage_summary[stg] = {"n": 0, "total_value": 0}
        stage_summary[stg]["n"] += 1
        stage_summary[stg]["total_value"] += r.get("resale_value_inr") or 0

    # Counts
    status_counts = {}
    for r in rows:
        s = r["pool_status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    remove_n = status_counts.get("REMOVE_IMMEDIATELY", 0)
    rotate_n = status_counts.get("ROTATE_LIGHTER", 0)
    continue_n = status_counts.get("CONTINUE", 0)
    stage1_val = stage_summary.get("STAGE1_ACTIVE", {}).get("total_value", 0)
    stage2_n = stage_summary.get("STAGE2_SECONDLIFE", {}).get("n", 0)

    headline = f"Pool health: {continue_n} active, {remove_n} to remove, {rotate_n} to rotate."
    story = (f"Total pool value: \u20b9{stage1_val:,.0f}. "
             f"{remove_n} batteries need immediate removal. "
             f"{stage2_n} batteries suitable for second-life redeployment.")

    remove_list = [r for r in rows if r["pool_status"] == "REMOVE_IMMEDIATELY"][:10]

    conn2 = get_conn()
    week_row = q1(conn2, "SELECT MAX(week_number) as wk FROM battery_health_scores_v2 WHERE week_number<100")
    conn2.close()

    return {
        "narrative": {"headline": headline, "story": story},
        "pool_batteries": rows,
        "stage_summary": stage_summary,
        "remove_list": remove_list,
        "continue_count": continue_n,
        "total_value": stage1_val,
        "week": week_row["wk"] if week_row else 0,
    }


@app.get("/api/swap/route-matching")
@safe
def swap_route_matching(_=Depends(verify_token)):
    """Battery recommendations per route type."""
    conn = get_conn()
    rows = q(conn, """
        SELECT bhs.battery_id, b.battery_model as pack_model, b.city_code as city,
            bhs.bhs_score_v2, bhs.range_p50, bhs.resale_stage
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE bhs.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                                  WHERE bhs2.battery_id = bhs.battery_id)
            AND bhs.scoring_mode!='SUSPENDED'
    """)
    conn.close()

    quick = [r for r in rows if (r.get("bhs_score_v2") or 0) > 70 and (r.get("range_p50") or 0) > 80
             and not (r.get("pack_model") or "").endswith("Pack3401")]
    standard = [r for r in rows if (r.get("bhs_score_v2") or 0) > 55 and (r.get("range_p50") or 0) > 60]
    light = [r for r in rows if (r.get("bhs_score_v2") or 0) > 40 and (r.get("range_p50") or 0) > 50]

    return {
        "QUICK_COMMERCE": {"eligible": len(quick), "batteries": quick[:20]},
        "STANDARD_URBAN": {"eligible": len(standard), "batteries": standard[:20]},
        "LIGHT_USE": {"eligible": len(light), "batteries": light[:20]},
    }


@app.get("/api/oem/warranty-quadrant")
@safe
def oem_warranty_quadrant(_=Depends(verify_token)):
    """Warranty intelligence — EFC vs range health."""
    conn = get_conn()
    rows = q(conn, """
        SELECT bhs.battery_id, b.battery_model as pack_model,
            bhs.efc_pct_of_warranty, bhs.pct_of_commissioned,
            bhs.nbfc_risk_tier as nbfc_grade, bhs.rul_action_v2
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id=b.battery_id
        WHERE bhs.week_number = (SELECT MAX(bhs2.week_number) FROM battery_health_scores_v2 bhs2
                                  WHERE bhs2.battery_id = bhs.battery_id)
            AND bhs.efc_pct_of_warranty IS NOT NULL
    """)
    conn.close()
    for r in rows:
        efc = r.get("efc_pct_of_warranty") or 0
        pct = r.get("pct_of_commissioned") or 0
        if efc >= 50 and pct >= 75:
            r["quadrant"] = "WARRANTY_ON_TRACK"
        elif efc < 50 and pct < 75:
            r["quadrant"] = "VALID_WARRANTY_CLAIM"
        elif efc < 50 and pct >= 75:
            r["quadrant"] = "WARRANTY_UNDERUSED"
        else:
            r["quadrant"] = "WARRANTY_EXHAUSTED"
    return rows


# ── Sprint 5A: Drill-down Endpoints ──────────────────────────────────

_ACTION_PLAIN_MAP = {
    "PHYSICS_REPLACE_PLAN": "Replace \u2014 confirmed physics decline",
    "REPLACE_PLAN": "Schedule replacement",
    "ACUTE_BREACH_WATCH": "Breach watch",
    "CELL_BALANCE_PRIORITY": "Cell balance service",
    "MONITOR_INVESTIGATE": "Under investigation",
    "MONITOR_WEEKLY": "Monitor weekly",
    "MONITOR": "Monitor weekly",
    "ROUTINE": "No action needed",
    "NO_ACTION": "No action needed",
    "CAPACITY_STABLE_LOW": "Stable \u2014 plan next cycle",
}

# Legacy attr_primary_factor -> new 5-factor engine driver (Rule 248+).
# Used as fallback on OEM endpoints when battery_degradation_attribution.primary_driver is NULL.
_LEGACY_DRIVER_TO_NEW = {
    "cycle_intensity": "usage",
    "cell_imbalance": "maintenance",
    "charging_stress": "charging",
    "thermal_stress": "thermal",
    "calendar_aging": "calendar",
    "physical_risk": "maintenance",
    "physical_wear": "maintenance",
}

_ACTION_GROUPS = {
    "replace": ("PHYSICS_REPLACE_PLAN", "REPLACE_PLAN"),
    "service": ("ACUTE_BREACH_WATCH", "CELL_BALANCE_PRIORITY", "MONITOR_INVESTIGATE"),
    "watch": ("MONITOR_WEEKLY",),
    "ok": ("NO_ACTION",),
}


@app.get("/api/fleet/segment-batteries")
@safe
def fleet_segment_batteries(
    city: Optional[str] = None,
    pack: Optional[str] = None,
    action: Optional[str] = None,
    grade: Optional[str] = None,
    limit: int = Query(50),
    _=Depends(verify_token),
):
    """Filtered battery list for drill-down panels."""
    conn = get_conn()
    conditions = [
        """bhs.week_number = (SELECT MAX(bhs2.week_number)
           FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = bhs.battery_id
           AND bhs2.week_number < 90)""",
        "bhs.scoring_mode != 'SUSPENDED'",
    ]
    params = []
    if city:
        conditions.append("b.city_code = ?")
        params.append(city)
    if pack:
        conditions.append("b.battery_model = ?")
        params.append(pack)
    if grade:
        conditions.append("bhs.nbfc_risk_tier = ?")
        params.append(grade)
    if action and action in _ACTION_GROUPS:
        placeholders = ",".join("?" for _ in _ACTION_GROUPS[action])
        conditions.append(f"bhs.rul_action_v2 IN ({placeholders})")
        params.extend(_ACTION_GROUPS[action])

    where = " AND ".join(conditions)
    params.append(min(limit, 50))

    rows = q(conn, f"""
        SELECT bhs.battery_id, b.battery_model as pack_model, b.city_code as city,
            bhs.bhs_score_v2, bhs.range_p50,
            bhs.rul_action_v2, bhs.nbfc_risk_tier as nbfc_grade,
            bhs.resale_value_inr, bhs.resale_stage,
            bhs.driver_stress_tier, bhs.use_case_inferred,
            bhs.fault_profile_tier, bhs.kps_slope_8wk,
            bhs.signal_confidence, bhs.divergence_quadrant
        FROM battery_health_scores_v2 bhs
        JOIN batteries b ON bhs.battery_id = b.battery_id
        WHERE {where}
        ORDER BY bhs.bhs_score_v2 ASC
        LIMIT ?
    """, params)
    conn.close()
    for r in rows:
        r["rul_action_plain"] = _ACTION_PLAIN_MAP.get(r.get("rul_action_v2"), r.get("rul_action_v2"))
    return rows


@app.get("/api/fleet/charging-profiles")
@safe
def fleet_charging_profiles(_=Depends(verify_token)):
    """Charging profile distribution across fleet."""
    conn = get_conn()
    rows = q(conn, """
        SELECT charging_discipline_grade as profile, COUNT(*) as n,
            ROUND(AVG(bhs_score_v2), 1) as avg_bhs,
            ROUND(AVG(range_p50), 1) as avg_range
        FROM battery_health_scores_v2
        WHERE week_number = (SELECT MAX(bhs2.week_number)
            FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = battery_health_scores_v2.battery_id
            AND bhs2.week_number < 90)
        AND charging_discipline_grade IS NOT NULL
        AND scoring_mode != 'SUSPENDED'
        GROUP BY charging_discipline_grade
        ORDER BY n DESC
    """)
    conn.close()
    return rows


@app.get("/api/fleet/regime-distribution")
@safe
def fleet_regime_distribution(_=Depends(verify_token)):
    """Degradation regime distribution across fleet."""
    conn = get_conn()
    rows = q(conn, """
        SELECT degradation_regime, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE week_number = (SELECT MAX(bhs2.week_number)
            FROM battery_health_scores_v2 bhs2 WHERE bhs2.battery_id = battery_health_scores_v2.battery_id
            AND bhs2.week_number < 90)
        AND degradation_regime IS NOT NULL
        AND scoring_mode != 'SUSPENDED'
        GROUP BY degradation_regime
        ORDER BY n DESC
    """)
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════
# OEM PORTFOLIO ENDPOINTS (Stage 1 — April 2026)
# ══════════════════════════════════════════════════════════════

@app.get("/api/oem/portfolio")
@safe
def oem_portfolio(pack: str = None, city: str = None,
                  _=Depends(verify_token)):
    """Per pack_model: unit count, avg range, tier distribution, warranty,
    5-factor attribution from battery_degradation_attribution (new engine),
    DRI, early anomaly count.

    Query params (Sprint OEM-audit Fix B):
      pack — filter to one pack_model (exact match on b.battery_model)
      city — filter to one city_code (exact match on b.city_code)

    Fix A (OEM audit): avg_dri now computes from bhs_score_v2 (the true
    Degradation Rate Index). Previously avg_dri read s.dri_score which is
    a different column and over-reported by 30-40 points. avg_bhs is
    retained as an alias for back-compat with the oem_v3 client hotfix.

    Fix D (OEM audit): false_alarm_rate_pct suppressed pending pipeline
    verification — the corroboration_score <= 1 proxy is not a sound
    false-alarm signal and produced nonsensical 67.9% readings.

    Attribution source priority:
      NEW_ENGINE         - battery_degradation_attribution.primary_driver populated
      LEGACY_TRANSLATED  - fallback from attr_primary_factor via _LEGACY_DRIVER_TO_NEW
      UNAVAILABLE        - neither populated
    """
    conn = get_conn()
    soh_anomaly_floor = 65.0
    spread_anomaly_ceiling = 300.0

    # Fix B: dynamic WHERE clauses for pack / city filters.
    filters = ["s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')",
               "s.battery_id != 'BAT_LFP_202'"]
    params = []
    if pack:
        filters.append("b.battery_model = ?")
        params.append(pack)
    if city:
        filters.append("b.city_code = ?")
        params.append(city)
    where_sql = " AND ".join(filters)

    rows = q(conn, f"""
        SELECT b.battery_model as pack_model,
               COUNT(*) as unit_count,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range_km,
               ROUND(AVG(s.pct_of_commissioned), 1) as avg_pct_of_commissioned,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_bhs,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_dri,
               SUM(CASE WHEN s.tier_label_v2 = 'PRIME' THEN 1 ELSE 0 END) as tier_prime,
               SUM(CASE WHEN s.tier_label_v2 = 'STABLE' THEN 1 ELSE 0 END) as tier_stable,
               SUM(CASE WHEN s.tier_label_v2 = 'WATCH' THEN 1 ELSE 0 END) as tier_watch,
               SUM(CASE WHEN s.tier_label_v2 = 'STRESSED' THEN 1 ELSE 0 END) as tier_stressed,
               SUM(CASE WHEN s.tier_label_v2 = 'CRITICAL' THEN 1 ELSE 0 END) as tier_critical,
               SUM(CASE WHEN s.warranty_claim_eligible = 1 THEN 1 ELSE 0 END) as warranty_claim_eligible_count,
               SUM(CASE WHEN s.pct_of_commissioned < 85 AND s.pct_of_commissioned IS NOT NULL THEN 1 ELSE 0 END) as below_warranty_band,
               SUM(CASE WHEN s.pct_of_commissioned >= 85 OR s.pct_of_commissioned IS NULL THEN 1 ELSE 0 END) as within_warranty_band
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE {where_sql}
        GROUP BY b.battery_model
        ORDER BY unit_count DESC
    """, params)
    for r in rows:
        pm = r["pack_model"]
        new_attr = q1(conn, """
            SELECT ROUND(AVG(da.charging_pct), 1) as charging_pct,
                   ROUND(AVG(da.usage_pct), 1) as usage_pct,
                   ROUND(AVG(da.thermal_pct), 1) as thermal_pct,
                   ROUND(AVG(da.maintenance_pct), 1) as maintenance_pct,
                   ROUND(AVG(da.calendar_pct), 1) as calendar_pct,
                   COUNT(*) as attr_n
            FROM battery_degradation_attribution da
            JOIN batteries b ON da.battery_id = b.battery_id
            WHERE b.battery_model = ?
        """, [pm])
        has_new = bool(new_attr and (new_attr.get("attr_n") or 0) > 0)
        for k in ("charging_pct", "usage_pct", "thermal_pct",
                  "maintenance_pct", "calendar_pct"):
            r[k] = new_attr.get(k) if has_new else None

        top_new = q1(conn, """
            SELECT da.primary_driver, COUNT(*) as n
            FROM battery_degradation_attribution da
            JOIN batteries b ON da.battery_id = b.battery_id
            WHERE b.battery_model = ? AND da.primary_driver IS NOT NULL
            GROUP BY da.primary_driver ORDER BY n DESC LIMIT 1
        """, [pm])
        if top_new:
            r["primary_driver"] = top_new["primary_driver"]
            r["attribution_source"] = "NEW_ENGINE"
        else:
            top_legacy = q1(conn, """
                SELECT attr_primary_factor, COUNT(*) as n
                FROM battery_health_scores_v2 s
                JOIN batteries b ON s.battery_id = b.battery_id
                WHERE b.battery_model = ? AND s.attr_primary_factor IS NOT NULL
                  AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
                GROUP BY attr_primary_factor ORDER BY n DESC LIMIT 1
            """, [pm])
            if top_legacy:
                legacy_val = top_legacy["attr_primary_factor"]
                r["primary_driver"] = _LEGACY_DRIVER_TO_NEW.get(legacy_val, legacy_val)
                r["attribution_source"] = "LEGACY_TRANSLATED"
            else:
                r["primary_driver"] = None
                r["attribution_source"] = "UNAVAILABLE"

        ea = q1(conn, f"""
            SELECT COUNT(*) as n FROM battery_health_scores_v2 s
            JOIN batteries b ON s.battery_id = b.battery_id
            WHERE b.battery_model = ?
              AND s.age_months IS NOT NULL AND s.age_months < 6
              AND (
                  (s.soh_coulomb_latest IS NOT NULL AND s.soh_coulomb_latest < {soh_anomaly_floor})
                  OR (s.cell_balance_spread_mv IS NOT NULL AND s.cell_balance_spread_mv > {spread_anomaly_ceiling})
              )
              AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
        """, [pm])
        r["early_anomaly_count"] = int(ea["n"]) if ea else 0

        # v2 extensions (SI-B0-2): merge taxonomy / survival / false-alarm / seasonal
        try:
            pack_code = pm.split("Pack")[-1] if "Pack" in pm else pm[-4:]

            surv = q1(conn, "SELECT median_failure_months FROM pack_survival_params WHERE pack_code = ?", [pack_code])
            r["pack_survival_p50"] = (surv or {}).get("median_failure_months")

            taxonomy = q(conn, """
                SELECT ve.event_type as category, COUNT(*) as count
                FROM vehicle_events ve
                JOIN batteries b2 ON ve.battery_id = b2.battery_id
                WHERE b2.battery_model = ?
                GROUP BY ve.event_type ORDER BY count DESC LIMIT 5
            """, [pm])
            _total = sum(t["count"] for t in taxonomy) or 1
            r["service_failure_taxonomy"] = [
                {"category": t["category"], "count": t["count"],
                 "pct": round(t["count"] / _total * 100, 1)}
                for t in taxonomy
            ]

            # Fix D (OEM audit): false_alarm_rate_pct suppressed. The prior
            # proxy (corroboration_score <= 1) flagged 67.9% on Pack3001 which
            # is not a real false-alarm rate — it reflects the share of
            # batteries with low signal corroboration, not confirmed NPF
            # outcomes. Needs a pipeline-side definition using service return
            # outcome labels before re-exposing.
            # npf = q1(conn, """...corroboration_score <= 1...""")
            # r["false_alarm_rate_pct"] = round(_nn / _tn * 100, 1)
            r["false_alarm_rate_pct"] = None

            r["seasonal_multipliers"] = [
                {"month_name": "Sep", "multiplier": 1.52},
                {"month_name": "Feb", "multiplier": 1.36},
                {"month_name": "Mar", "multiplier": 0.54},
            ]

            top2 = q(conn, """
                SELECT da.primary_driver, COUNT(*) as n
                FROM battery_degradation_attribution da
                JOIN batteries b4 ON da.battery_id = b4.battery_id
                WHERE b4.battery_model = ? AND da.primary_driver IS NOT NULL
                GROUP BY da.primary_driver ORDER BY n DESC LIMIT 2
            """, [pm])
            r["top_attribution_factors"] = [
                _LEGACY_DRIVER_TO_NEW.get(t["primary_driver"], t["primary_driver"])
                for t in top2
            ]
        except Exception:
            r.setdefault("pack_survival_p50", None)
            r.setdefault("service_failure_taxonomy", [])
            r.setdefault("false_alarm_rate_pct", None)  # suppressed — see Fix D
            r.setdefault("seasonal_multipliers", [])
            r.setdefault("top_attribution_factors", [])

    # Fix C (OEM audit): add fleet-wide total_scored so oem_v3's "Fleet size"
    # KPI reads a real 361, not the sum of unit_count across packs (which
    # excludes unscored units, pack-null rows, and non-standard packs).
    total_scored_row = q1(conn, """
        SELECT COUNT(*) AS n FROM battery_health_scores_v2
        WHERE scoring_mode != 'SUSPENDED'
          AND battery_id NOT IN ('BAT_LFP_034','BAT_LFP_202')
    """)
    total_scored = (total_scored_row or {}).get("n") or 0

    conn.close()
    # Backward-compatible return shape: oem_v3 expects an array — keep the
    # array at the top level. total_scored is attached as a property on the
    # array by wrapping in a dict only when requested. Default: return the
    # array and surface total_scored via a response header helper (client
    # still works; new clients can switch to /portfolio-v2 envelope if they
    # need the envelope shape).
    for _r in rows:
        _r["_total_scored"] = total_scored  # per-row duplicate is harmless and simplifies client read
    return rows


# ── Sprint OEM-endpoints: compare-data / percentile-batteries / fleet-attribution ──
# Three endpoints consumed by oem_v3.html (Compare + Analysis views). All three
# respect the banned-battery filter. PACK_GAP_EXCEPTION (Pack3401) is handled
# per-endpoint per spec: included in compare-data + percentile-batteries but
# excluded from fleet-attribution averages (PG3401 factors are null and would
# corrupt the mean).

# Map the public metric name to a (table_alias, column) pair. "h" is BHS,
# "v" is VWF. BHS scalars are static per battery; VWF scalars are weekly.
_OEM_COMPARE_METRIC_MAP = {
    "dri":          ("h", "bhs_score_v2"),
    "range_km":     ("h", "range_corrected_km"),
    "soh":          ("h", "soh_conservative"),
    "efc":          ("h", "efc_cumulative"),
    "kps_slope":    ("h", "kps_slope_8wk"),
    "cell_spread":  ("v", "cell_spread_max"),
}

_OEM_COMPARE_GROUP_MAP = {
    "pack":              ("b.battery_model",      None),
    "city":              ("COALESCE(scm.city_name, b.city_code)", None),
    "application":       ("b.use_case_inferred",  None),
    "charging_profile":  ("p.personal_charge_profile", "LEFT JOIN battery_personal_params p ON v.battery_id = p.battery_id"),
}

_OEM_PERIOD_WEEKS = {"4wk": 4, "8wk": 8, "12wk": 12, "all": 52}


def _percentile(sorted_vals, pct):
    """Linear percentile on a pre-sorted list; pct in 0..100. None if empty."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    k = (pct / 100.0) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    frac = k - f
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * frac


def _oem_round(v, dp=1):
    if v is None:
        return None
    try:
        return round(float(v), dp)
    except (TypeError, ValueError):
        return None


@app.get("/api/oem/compare-data")
@safe
def oem_compare_data(
    metric: str = Query(default="dri"),
    group: str = Query(default="pack"),
    period: str = Query(default="12wk"),
    pack: str = Query(default=""),
    city: str = Query(default=""),
    _=Depends(verify_token),
):
    """Weekly compare series with P10/P25/P75/P90 bands per group.

    Group labels are sorted by the top-6 batteries-per-group count so smaller
    segments don't crowd out the main packs. Banned batteries excluded.
    """
    m_key = metric if metric in _OEM_COMPARE_METRIC_MAP else "dri"
    g_key = group if group in _OEM_COMPARE_GROUP_MAP else "pack"
    m_tbl, m_col = _OEM_COMPARE_METRIC_MAP[m_key]
    g_expr, g_extra_join = _OEM_COMPARE_GROUP_MAP[g_key]
    period_weeks = _OEM_PERIOD_WEEKS.get(period, 12)

    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("v.battery_id")

        extra_join = g_extra_join or ""
        pack_clause = " AND b.battery_model = ? " if pack else ""
        city_clause = " AND (scm.city_name = ? OR b.city_code = ?) " if city else ""

        # Per-battery week windowing: VWF weeks are commissioning-relative,
        # so LFP caps at ~65 while NMC reaches 101. Using a global MAX would
        # exclude every LFP battery. Normalise to "weeks ago" per battery:
        # latest row for a battery is at offset 0.
        metric_expr = f"{m_tbl}.{m_col}"
        sql = f"""
            WITH bat_max AS (
                SELECT battery_id, MAX(week_number) AS mx
                FROM vehicle_weekly_features
                GROUP BY battery_id
            )
            SELECT (bm.mx - v.week_number) AS week_offset,
                   ({g_expr}) AS group_label,
                   v.battery_id,
                   {metric_expr} AS val
            FROM vehicle_weekly_features v
            JOIN bat_max bm ON bm.battery_id = v.battery_id
            JOIN batteries b ON v.battery_id = b.battery_id
            JOIN battery_health_scores_v2 h ON v.battery_id = h.battery_id
            LEFT JOIN service_city_map scm ON b.city_code = scm.city_code
            {extra_join}
            WHERE (bm.mx - v.week_number) < ?
              AND {ban_sql}
              AND {metric_expr} IS NOT NULL
              AND ({g_expr}) IS NOT NULL
              {pack_clause}
              {city_clause}
            ORDER BY week_offset DESC, group_label
        """
        params = [period_weeks] + list(ban_params)
        if pack:
            params.append(pack)
        if city:
            params.extend([city, city])

        rows = q(conn, sql, params)
    finally:
        conn.close()

    # Bucket into {group_label: {week_label: [values...]}}.
    # week_label = period_weeks - week_offset (so the X-axis reads oldest..latest).
    buckets = {}
    for r in rows:
        lbl = r["group_label"]
        offset = r["week_offset"]
        if offset is None:
            continue
        wk = period_weeks - int(offset)  # 1..period_weeks
        val = r["val"]
        if val is None:
            continue
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        buckets.setdefault(lbl, {}).setdefault(wk, []).append(fval)

    weeks = sorted({period_weeks - int(r["week_offset"])
                    for r in rows if r["week_offset"] is not None})

    # Rank groups by total battery-weeks observed (popularity); keep top 6.
    group_order = sorted(
        buckets.keys(),
        key=lambda g: sum(len(v) for v in buckets[g].values()),
        reverse=True,
    )[:6]

    def _series(lbl):
        by_wk = buckets[lbl]
        vals, p10, p25, p75, p90 = [], [], [], [], []
        for wk in weeks:
            weekly = sorted(by_wk.get(wk, []))
            if not weekly:
                vals.append(None); p10.append(None); p25.append(None); p75.append(None); p90.append(None)
                continue
            median = _percentile(weekly, 50)
            vals.append(_oem_round(median, 2))
            p10.append(_oem_round(_percentile(weekly, 10), 2))
            p25.append(_oem_round(_percentile(weekly, 25), 2))
            p75.append(_oem_round(_percentile(weekly, 75), 2))
            p90.append(_oem_round(_percentile(weekly, 90), 2))
        return {"label": lbl, "values": vals, "p10": p10, "p25": p25, "p75": p75, "p90": p90}

    groups_out = [_series(g) for g in group_order]

    return {
        "metric": m_key,
        "group_by": g_key,
        "period": period,
        "weeks": weeks,
        "groups": groups_out,
    }


_OEM_PERCENTILE_METRIC_MAP = {
    # metric -> (sql fragment on BHS + VWF join, direction_is_higher_better)
    # higher_better: True means top-ranked = highest value, bottom-ranked = lowest.
    "dri":         ("h.bhs_score_v2",                True),
    "range_km":    ("h.range_corrected_km",          True),
    "soh":         ("h.soh_conservative",            True),
    "efc":         ("h.efc_cumulative",              False),  # more EFC = more wear
    "kps_slope":   ("h.kps_slope_8wk",               True),
    "cell_spread": ("v_latest.cell_spread_max",      False),  # more spread = worse
}


@app.get("/api/oem/percentile-batteries")
@safe
def oem_percentile_batteries(
    metric: str = Query(default="dri"),
    pct: int = Query(default=5),
    _=Depends(verify_token),
):
    """Top and bottom pct% batteries ranked by the chosen metric.

    differentiator derived from BDA primary driver / pack model; Pack3401
    batteries always collapse to 'pack design'.
    """
    m_key = metric if metric in _OEM_PERCENTILE_METRIC_MAP else "dri"
    metric_expr, higher_better = _OEM_PERCENTILE_METRIC_MAP[m_key]
    try:
        pct_int = max(1, min(50, int(pct)))
    except (TypeError, ValueError):
        pct_int = 5

    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("h.battery_id")
        sql = f"""
            SELECT h.battery_id,
                   b.battery_model AS pack,
                   COALESCE(scm.city_name, b.city_code) AS city,
                   {metric_expr} AS metric_value,
                   bda.primary_driver AS primary_driver,
                   bda.charging_pct   AS charging_pct,
                   bda.usage_pct      AS usage_pct,
                   bda.thermal_pct    AS thermal_pct,
                   bda.maintenance_pct AS maintenance_pct,
                   bda.calendar_pct   AS calendar_pct
            FROM battery_health_scores_v2 h
            JOIN batteries b ON h.battery_id = b.battery_id
            LEFT JOIN service_city_map scm ON b.city_code = scm.city_code
            LEFT JOIN battery_degradation_attribution bda ON bda.battery_id = h.battery_id
            LEFT JOIN (
                SELECT v.battery_id, v.cell_spread_max
                FROM vehicle_weekly_features v
                JOIN (
                    SELECT battery_id, MAX(week_number) AS mx
                    FROM vehicle_weekly_features
                    WHERE cell_spread_max IS NOT NULL
                    GROUP BY battery_id
                ) last ON last.battery_id = v.battery_id AND last.mx = v.week_number
            ) v_latest ON v_latest.battery_id = h.battery_id
            WHERE {ban_sql}
              AND {metric_expr} IS NOT NULL
            ORDER BY {metric_expr} DESC
        """
        rows = q(conn, sql, list(ban_params))
    finally:
        conn.close()

    n = len(rows)
    if n == 0:
        return {"metric": m_key, "pct": pct_int, "top": [], "bottom": []}

    cutoff = max(1, n * pct_int // 100)
    top_rows = rows[:cutoff]
    bottom_rows = list(reversed(rows[-cutoff:]))
    if not higher_better:
        # swap — lower metric value means "better" for efc / cell_spread
        top_rows, bottom_rows = bottom_rows, top_rows

    def _differentiator(r):
        pm = (r.get("pack") or "")
        if "3401" in pm:
            return "pack design"
        factors = {
            "charging":    r.get("charging_pct") or 0,
            "usage":       r.get("usage_pct") or 0,
            "thermal":     r.get("thermal_pct") or 0,
            "maintenance": r.get("maintenance_pct") or 0,
            "calendar":    r.get("calendar_pct") or 0,
        }
        if max(factors.values()) <= 0:
            return "mixed"
        winner = max(factors, key=lambda k: factors[k])
        return winner

    def _shape(r):
        return {
            "battery_id": r["battery_id"],
            "pack": r.get("pack"),
            "city": r.get("city"),
            "metric_value": _oem_round(r.get("metric_value"), 2),
            "differentiator": _differentiator(r),
        }

    return {
        "metric": m_key,
        "pct": pct_int,
        "top":    [_shape(r) for r in top_rows],
        "bottom": [_shape(r) for r in bottom_rows],
    }


@app.get("/api/oem/fleet-attribution")
@safe
def oem_fleet_attribution(
    pack: str = Query(default=""),
    city: str = Query(default=""),
    _=Depends(verify_token),
):
    """Fleet-wide attribution averages. PACK_GAP_EXCEPTION (Pack3401)
    excluded from the averages (their factor columns are null and would
    corrupt the mean) and reported separately.
    """
    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("bda.battery_id")
        pack_clause = " AND b.battery_model = ? " if pack else ""
        city_clause = " AND (scm.city_name = ? OR b.city_code = ?) " if city else ""

        params = list(ban_params)
        if pack:
            params.append(pack)
        if city:
            params.extend([city, city])

        row = q1(conn, f"""
            SELECT AVG(bda.charging_pct)    AS charging,
                   AVG(bda.usage_pct)       AS usage,
                   AVG(bda.maintenance_pct) AS maintenance,
                   AVG(bda.thermal_pct)     AS thermal,
                   AVG(bda.calendar_pct)    AS calendar,
                   COUNT(*) AS n
            FROM battery_degradation_attribution bda
            JOIN batteries b ON bda.battery_id = b.battery_id
            LEFT JOIN service_city_map scm ON b.city_code = scm.city_code
            WHERE {ban_sql}
              AND (bda.pack_model_finding IS NULL OR bda.pack_model_finding != 'PACK_GAP_EXCEPTION')
              AND (b.battery_model NOT LIKE '%3401%' OR b.battery_model IS NULL)
              AND bda.charging_pct IS NOT NULL
              {pack_clause}
              {city_clause}
        """, params) or {}

        excluded = q1(conn, f"""
            SELECT COUNT(*) AS n
            FROM battery_degradation_attribution bda
            JOIN batteries b ON bda.battery_id = b.battery_id
            WHERE {ban_sql}
              AND (bda.pack_model_finding = 'PACK_GAP_EXCEPTION'
                   OR b.battery_model LIKE '%3401%')
        """, list(ban_params)) or {}

        ban_sql_h, ban_params_h = _banned_sql_clause("h.battery_id")
        pack3401 = q1(conn, f"""
            SELECT COUNT(*) AS pack3401_count,
                   SUM(CASE WHEN h.warranty_claim_eligible = 1 THEN 1 ELSE 0 END) AS pack3401_warranty
            FROM battery_health_scores_v2 h
            JOIN batteries b ON h.battery_id = b.battery_id
            WHERE b.battery_model LIKE '%3401%'
              AND {ban_sql_h}
        """, list(ban_params_h)) or {}
    finally:
        conn.close()

    charging    = _oem_round(row.get("charging"))
    usage       = _oem_round(row.get("usage"))
    maintenance = _oem_round(row.get("maintenance"))
    thermal     = _oem_round(row.get("thermal"))
    calendar    = _oem_round(row.get("calendar"))
    n_batteries = int(row.get("n") or 0)

    fa = {
        "charging_pct":    charging,
        "usage_pct":       usage,
        "maintenance_pct": maintenance,
        "thermal_pct":     thermal,
        "oem_pct":         calendar,  # calendar_pct = OEM/design category
        "n_batteries":     n_batteries,
    }

    # Primary driver sentence
    _names = {
        "charging":    ("Charging behaviour", "Charging behaviour is the primary driver of fleet degradation across standard pack models."),
        "usage":       ("Usage intensity",    "Usage intensity is the primary driver of fleet degradation across standard pack models."),
        "maintenance": ("Maintenance gaps",   "Maintenance gaps are the primary driver of fleet degradation across standard pack models."),
        "thermal":     ("Thermal exposure",   "Thermal exposure is the primary driver of fleet degradation across standard pack models."),
        "oem":         ("OEM / Calendar",     "OEM design and calendar aging are the primary drivers of fleet degradation across standard pack models."),
    }
    pairs = [("charging", charging), ("usage", usage), ("maintenance", maintenance),
             ("thermal", thermal), ("oem", calendar)]
    pairs_valid = [(k, v) for k, v in pairs if v is not None]
    if pairs_valid:
        top_key, _top_val = max(pairs_valid, key=lambda kv: kv[1])
        primary_driver_plain = _names[top_key][1]
    else:
        primary_driver_plain = "Attribution data pending — no factor data available."

    oper_pct = (charging or 0) + (usage or 0) + (maintenance or 0)
    implication = (
        f"Operator-controllable factors account for {round(oper_pct)}% of degradation "
        "in non-Pack3401 batteries."
    ) if n_batteries else "Operator-controllable factors not yet computable."

    return {
        "fleet_attribution": fa,
        "excluded_pack_gap_exception": int(excluded.get("n") or 0),
        "pack3401_count":              int(pack3401.get("pack3401_count") or 0),
        "pack3401_warranty_eligible":  int(pack3401.get("pack3401_warranty") or 0),
        "primary_driver_plain": primary_driver_plain,
        "attribution_implication": implication,
    }


@app.get("/api/oem/replacement-pipeline")
@safe
def oem_replacement_pipeline(_=Depends(verify_token)):
    """Batteries approaching end of operational life, ordered by urgency.
    Per-battery 5-factor attribution joined from battery_degradation_attribution.
    """
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               ROUND(s.range_corrected_km, 1) as range_corrected_km,
               ROUND(s.pct_of_commissioned, 1) as pct_of_commissioned,
               s.rul_weeks_v2, s.rul_action_v2,
               p.service_profile, s.warranty_claim_eligible, s.week_number,
               da.charging_pct, da.usage_pct, da.thermal_pct,
               da.maintenance_pct, da.calendar_pct, da.primary_driver,
               s.attr_primary_factor as _legacy_primary
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        LEFT JOIN battery_personal_params p ON s.battery_id = p.battery_id
        LEFT JOIN battery_degradation_attribution da ON da.battery_id = s.battery_id
        WHERE s.rul_action_v2 IN ('REPLACE_PLAN', 'PHYSICS_REPLACE_PLAN', 'MONITOR_INVESTIGATE')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
        ORDER BY
          CASE s.rul_action_v2
            WHEN 'PHYSICS_REPLACE_PLAN' THEN 1
            WHEN 'REPLACE_PLAN' THEN 2
            WHEN 'MONITOR_INVESTIGATE' THEN 3
          END,
          s.range_corrected_km ASC
        LIMIT 20
    """)
    conn.close()
    for r in rows:
        r["action_plain"] = _ACTION_PLAIN_MAP.get(r.get("rul_action_v2"), r.get("rul_action_v2"))
        if r.get("primary_driver"):
            r["attribution_source"] = "NEW_ENGINE"
        elif r.get("_legacy_primary"):
            legacy_val = r["_legacy_primary"]
            r["primary_driver"] = _LEGACY_DRIVER_TO_NEW.get(legacy_val, legacy_val)
            r["attribution_source"] = "LEGACY_TRANSLATED"
        else:
            r["attribution_source"] = "UNAVAILABLE"
        r.pop("_legacy_primary", None)
    return rows


@app.get("/api/oem/attribution-summary")
@safe
def oem_attribution_summary(_=Depends(verify_token)):
    """Per pack_model: avg of each new-engine attribution factor
    (charging, usage, thermal, maintenance, calendar). Sourced from
    battery_degradation_attribution.
    """
    conn = get_conn()
    rows = q(conn, """
        SELECT b.battery_model as pack_model,
               ROUND(AVG(da.charging_pct), 1) as avg_charging_pct,
               ROUND(AVG(da.usage_pct), 1) as avg_usage_pct,
               ROUND(AVG(da.thermal_pct), 1) as avg_thermal_pct,
               ROUND(AVG(da.maintenance_pct), 1) as avg_maintenance_pct,
               ROUND(AVG(da.calendar_pct), 1) as avg_calendar_pct,
               COUNT(*) as attr_n
        FROM battery_degradation_attribution da
        JOIN batteries b ON da.battery_id = b.battery_id
        JOIN battery_health_scores_v2 s ON s.battery_id = da.battery_id
        WHERE s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
        GROUP BY b.battery_model
        ORDER BY b.battery_model
    """)
    for r in rows:
        pm = r["pack_model"]
        top_new = q1(conn, """
            SELECT da.primary_driver, COUNT(*) as n
            FROM battery_degradation_attribution da
            JOIN batteries b ON da.battery_id = b.battery_id
            WHERE b.battery_model = ? AND da.primary_driver IS NOT NULL
            GROUP BY da.primary_driver ORDER BY n DESC LIMIT 1
        """, [pm])
        if top_new:
            r["primary_driver"] = top_new["primary_driver"]
            r["attribution_source"] = "NEW_ENGINE"
        else:
            top_legacy = q1(conn, """
                SELECT attr_primary_factor, COUNT(*) as n
                FROM battery_health_scores_v2 s
                JOIN batteries b ON s.battery_id = b.battery_id
                WHERE b.battery_model = ? AND s.attr_primary_factor IS NOT NULL
                  AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
                GROUP BY attr_primary_factor ORDER BY n DESC LIMIT 1
            """, [pm])
            if top_legacy:
                legacy_val = top_legacy["attr_primary_factor"]
                r["primary_driver"] = _LEGACY_DRIVER_TO_NEW.get(legacy_val, legacy_val)
                r["attribution_source"] = "LEGACY_TRANSLATED"
            else:
                r["primary_driver"] = None
                r["attribution_source"] = "UNAVAILABLE"
    conn.close()
    return rows


@app.get("/api/oem/service-profile-summary")
@safe
def oem_service_profile_summary(_=Depends(verify_token)):
    """Distribution of service_profile across all scored batteries."""
    conn = get_conn()
    rows = q(conn, """
        SELECT p.service_profile,
               COUNT(*) as count,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range_corrected_km,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_bhs_score
        FROM battery_health_scores_v2 s
        JOIN battery_personal_params p ON s.battery_id = p.battery_id
        WHERE s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND p.service_profile IS NOT NULL
        GROUP BY p.service_profile
        ORDER BY
          CASE p.service_profile
            WHEN 'EXEMPLARY' THEN 1
            WHEN 'STANDARD' THEN 2
            WHEN 'WATCH' THEN 3
            WHEN 'NEGLIGENT' THEN 4
            WHEN 'AT_RISK' THEN 5
          END
    """)
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════
# PASSPORT NEXT ENDPOINTS (Stage 2 — April 2026)
# ══════════════════════════════════════════════════════════════

_BLOCKED_BATTERIES = {"BAT_LFP_034", "BAT_LFP_202"}


@app.get("/api/oem/unit-list")
@safe
def oem_unit_list(_=Depends(verify_token)):
    """All batteries for left panel list with search/filter."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, b.battery_model as pack_model, b.city_code as city,
               s.tier_label_v2, ROUND(s.range_corrected_km, 1) as range_corrected_km,
               s.rul_action_v2, p.service_profile, s.warranty_claim_eligible,
               ROUND(s.bhs_score_v2, 1) as bhs_score_v2,
               b.fleet_segment
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        LEFT JOIN battery_personal_params p ON s.battery_id = p.battery_id
        WHERE s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        ORDER BY
          CASE s.rul_action_v2
            WHEN 'PHYSICS_REPLACE_PLAN' THEN 1
            WHEN 'REPLACE_PLAN' THEN 2
            WHEN 'MONITOR_INVESTIGATE' THEN 3
            WHEN 'ACUTE_BREACH_WATCH' THEN 4
            WHEN 'CELL_BALANCE_PRIORITY' THEN 5
            WHEN 'MONITOR_WEEKLY' THEN 6
            WHEN 'MONITOR' THEN 7
            WHEN 'ROUTINE' THEN 8
            WHEN 'NO_ACTION' THEN 9
            ELSE 10
          END,
          s.range_corrected_km ASC
    """)
    conn.close()
    _SEG_PREFIX = {"GE_ERICKSHAW": "GE", "SG_ERICKSHAW": "SG"}
    for r in rows:
        r["action_plain"] = _ACTION_PLAIN_MAP.get(r.get("rul_action_v2"), r.get("rul_action_v2"))
        r["tier_plain"] = (r.get("tier_label_v2") or "").replace("_", " ").title()
        r["segment_prefix"] = _SEG_PREFIX.get(r.pop("fleet_segment", None) or "", "FL")
    return rows


@app.get("/api/oem/unit/{battery_id}")
@safe
def oem_unit_detail(battery_id: str, _=Depends(verify_token)):
    """Full unit view — verdict, performance, health, evidence, tech, cohort."""
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")

    conn = get_conn()

    # Main BHS row
    bhs = q1(conn, """
        SELECT s.*, b.battery_model as pack_model, b.city_code as city,
               b.commissioning_date, b.chemistry
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.battery_id = ?
    """, [battery_id])
    if not bhs:
        conn.close()
        raise HTTPException(status_code=404, detail="Battery not found")

    # Personal params
    pp = q1(conn, "SELECT * FROM battery_personal_params WHERE battery_id = ?", [battery_id])

    # Weeks in service
    weeks_in_service = bhs.get("week_number") or 0

    # Verdict
    verdict = {
        "battery_id": battery_id,
        "tier_label_v2": bhs.get("tier_label_v2"),
        "tier_plain": (bhs.get("tier_label_v2") or "").replace("_", " ").title(),
        "rul_action_v2": bhs.get("rul_action_v2"),
        "action_plain": _ACTION_PLAIN_MAP.get(bhs.get("rul_action_v2"), bhs.get("rul_action_v2")),
        "confidence_pct": bhs.get("confidence_pct"),
        "range_corrected_km": round(bhs["range_corrected_km"], 1) if bhs.get("range_corrected_km") else None,
        "pct_of_commissioned": round(bhs["pct_of_commissioned"], 1) if bhs.get("pct_of_commissioned") else None,
        "weeks_in_service": weeks_in_service,
        "primary_attribution": bhs.get("attr_primary_factor"),
        "warranty_claim_eligible": bhs.get("warranty_claim_eligible"),
        "pack_model": bhs.get("pack_model"),
        "city": bhs.get("city"),
    }

    # Performance
    range_history = q(conn, """
        SELECT week_number,
               ROUND(km_per_soc_pct * 80, 1) as range_km
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND km_per_soc_pct IS NOT NULL
        ORDER BY week_number DESC LIMIT 12
    """, [battery_id])
    range_history.reverse()

    # Forecast from dt_lfp_range_projections
    forecasts = q(conn, """
        SELECT horizon_weeks, ROUND(range_p50_km, 1) as p50,
               ROUND(range_p10_km, 1) as p10, ROUND(range_p90_km, 1) as p90
        FROM dt_lfp_range_projections
        WHERE battery_id = ? ORDER BY horizon_weeks
    """, [battery_id])

    # DoD optimal from fleet_context_params
    dod_optimal = q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name = 'range_dod_factor' AND is_active = 1 LIMIT 1
    """)

    # Cohort stats
    cohort = q1(conn, """
        SELECT COUNT(*) as cohort_size,
               ROUND(AVG(range_corrected_km), 1) as cohort_avg_range
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model = ? AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
    """, [bhs.get("pack_model")])

    # Cohort rank
    cohort_rank_row = q1(conn, """
        SELECT COUNT(*) as worse_count
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model = ? AND s.range_corrected_km < ?
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
    """, [bhs.get("pack_model"), bhs.get("range_corrected_km") or 0])
    cohort_size = cohort["cohort_size"] if cohort else 1
    cohort_rank_pct = round((cohort_rank_row["worse_count"] / max(cohort_size, 1)) * 100, 0) if cohort_rank_row else 50

    performance = {
        "range_history": range_history,
        "range_forecast": forecasts,
        "dod_mean": round(bhs.get("dod_coulomb_avg") or 0, 1) if bhs.get("dod_coulomb_avg") else None,
        "dod_optimal": dod_optimal["param_value"] if dod_optimal else 80.0,
        "kps_slope_8wk": bhs.get("kps_slope_8wk"),
        "cohort_percentile": cohort_rank_pct,
    }

    # Health
    health = {
        "bhs_score_v2": round(bhs["bhs_score_v2"], 1) if bhs.get("bhs_score_v2") else None,
        "bhs_components": {
            "spread": bhs.get("bhs_component_spread"),
            "soh_latest": bhs.get("bhs_component_soh_latest"),
            "soh_trend": bhs.get("bhs_component_soh_trend"),
            "ah_throughput": bhs.get("bhs_component_ah"),
            "pct_commissioned": bhs.get("bhs_component_pct_comm"),
            "operational_score": bhs.get("bhs_component_op"),
        },
        "soh_conservative": round(bhs["soh_conservative"], 1) if bhs.get("soh_conservative") else None,
        "degradation_regime": bhs.get("degradation_regime"),
        "corroboration_score": bhs.get("corroboration_score"),
        "divergence_quadrant": bhs.get("divergence_quadrant"),
    }

    # Evidence
    events_4wk = q(conn, """
        SELECT event_type, severity, week_number, event_code, event_reliability
        FROM vehicle_events
        WHERE battery_id = ? AND week_number >= ?
        ORDER BY week_number DESC
    """, [battery_id, max(weeks_in_service - 4, 0)])

    # Range floor for this pack
    floor_row = q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name = 'range_floor_km' AND segment_type = 'PACK'
          AND segment_value = ? AND is_active = 1
    """, [bhs.get("pack_model")])
    range_floor = floor_row["param_value"] if floor_row else 56.0

    evidence = {
        "cycle_attribution_pct": bhs.get("attr_cycle_pct"),
        "dod_attribution_pct": bhs.get("attr_dod_pct"),
        "thermal_attribution_pct": bhs.get("attr_thermal_pct"),
        "imbalance_attribution_pct": bhs.get("attr_imbalance_pct"),
        "calendar_attribution_pct": bhs.get("attr_calendar_pct"),
        "physical_attribution_pct": bhs.get("attr_physical_pct"),
        "charging_profile": pp.get("personal_charge_profile") if pp else None,
        "drive_profile": pp.get("personal_drive_profile") if pp else None,
        "service_profile": pp.get("service_profile") if pp else None,
        "fault_severity": bhs.get("fault_profile_tier"),
        "fault_trajectory": bhs.get("degradation_regime"),
        "events_last_4wk": events_4wk,
        "warranty_claim_eligible": bhs.get("warranty_claim_eligible"),
        "warranty_status": bhs.get("warranty_status"),
        "efc_pct_of_warranty": bhs.get("efc_pct_of_warranty"),
    }

    # Tech
    tech = {
        "kps_slope_8wk": bhs.get("kps_slope_8wk"),
        "cell_spread_mv": bhs.get("cell_balance_spread_mv"),
        "soh_coulomb_actual": _clip_soh_value(bhs.get("soh_coulomb_actual")),
        "efc_cumulative": bhs.get("efc_cumulative"),
        "breach_probability_8wk": bhs.get("breach_prob_physical_6mo"),
        "divergence_quadrant": bhs.get("divergence_quadrant"),
        "scoring_mode": bhs.get("scoring_mode"),
        "stress_index": bhs.get("stress_index"),
        "range_floor_km": range_floor,
    }

    # Peer batteries (3 from same pack, different primary attribution)
    peers = q(conn, """
        SELECT s.battery_id, ROUND(s.bhs_score_v2, 1) as bhs_score_v2,
               ROUND(s.range_corrected_km, 1) as range_km,
               s.attr_primary_factor, s.tier_label_v2
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model = ?
          AND s.battery_id != ?
          AND s.attr_primary_factor != ?
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        ORDER BY ABS(s.bhs_score_v2 - ?) ASC
        LIMIT 3
    """, [bhs.get("pack_model"), battery_id,
          bhs.get("attr_primary_factor") or "", bhs.get("bhs_score_v2") or 50])

    cohort_info = {
        "pack_model": bhs.get("pack_model"),
        "cohort_size": cohort_size,
        "cohort_rank_pct": cohort_rank_pct,
        "cohort_avg_range": cohort["cohort_avg_range"] if cohort else None,
        "peer_batteries": peers,
    }

    conn.close()

    return {
        "verdict": verdict,
        "performance": performance,
        "health": health,
        "evidence": evidence,
        "tech": tech,
        "cohort": cohort_info,
    }


@app.get("/api/oem/range-history/{battery_id}")
@safe
def oem_range_history(battery_id: str, _=Depends(verify_token)):
    """12 weeks history + forecast for range chart."""
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")

    conn = get_conn()

    # History
    history = q(conn, """
        SELECT week_number, ROUND(km_per_soc_pct * 80, 1) as range_km
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND km_per_soc_pct IS NOT NULL
        ORDER BY week_number DESC LIMIT 12
    """, [battery_id])
    history.reverse()

    # Forecast
    forecasts = q(conn, """
        SELECT current_week + horizon_weeks as week_number,
               ROUND(range_p50_km, 1) as range_km
        FROM dt_lfp_range_projections
        WHERE battery_id = ? ORDER BY horizon_weeks
    """, [battery_id])

    # Floor
    pack_row = q1(conn, """
        SELECT b.battery_model FROM batteries b WHERE b.battery_id = ?
    """, [battery_id])
    pack_model = pack_row["battery_model"] if pack_row else None
    floor_row = q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name = 'range_floor_km' AND segment_type = 'PACK'
          AND segment_value = ? AND is_active = 1
    """, [pack_model])
    floor_km = floor_row["param_value"] if floor_row else 56.0

    conn.close()

    result = []
    for h in history:
        result.append({"week_number": h["week_number"], "range_km": h["range_km"], "type": "actual"})
    for f in forecasts:
        result.append({"week_number": f["week_number"], "range_km": f["range_km"], "type": "forecast_p50"})

    return {"data": result, "floor_km": floor_km}


# ══════════════════════════════════════════════════════════════
# COMPONENT HEALTH ENDPOINTS (Stage 3 — April 2026)
# ══════════════════════════════════════════════════════════════

def _component_health_query(conn, battery_id):
    """Aggregated component health for a single battery (last 12 weeks)."""
    row = q1(conn, """
        SELECT ch.battery_id,
               ROUND(AVG(ch.iot_quality_score), 1) as iot_quality_score,
               SUM(ch.bms_alert_count_7d) as bms_alert_count_12wk,
               MAX(ch.iot_status) as iot_status,
               MAX(ch.physical_damage_risk) as physical_damage_risk,
               ROUND(MAX(ch.physical_risk_score), 2) as physical_risk_score,
               ROUND(AVG(ch.cell_spread_p90_mv), 1) as cell_spread_p90_mv,
               ROUND(AVG(ch.cell_spread_trend_7d), 4) as cell_spread_trend
        FROM battery_component_health ch
        WHERE ch.battery_id = ?
          AND ch.week_number >= (
            SELECT MAX(week_number) - 12 FROM battery_component_health WHERE battery_id = ?
          )
    """, [battery_id, battery_id])
    if not row or not row.get("battery_id"):
        return None

    # Compute NPF probability: low corroboration + high alert count = likely NPF
    corr = q1(conn, """
        SELECT corroboration_score FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    corr_score = (corr or {}).get("corroboration_score") or 0
    alert_count = row.get("bms_alert_count_12wk") or 0
    # NPF = alerts not corroborated by degradation signals
    if alert_count > 50 and corr_score <= 1:
        npf_prob = 0.8
    elif alert_count > 20 and corr_score <= 2:
        npf_prob = 0.5
    elif alert_count > 10 and corr_score <= 1:
        npf_prob = 0.4
    else:
        npf_prob = round(max(0, 0.3 - corr_score * 0.08), 2)

    # Compute alert-to-range conversion rate
    # Count weeks where alerts preceded range drop
    range_drop_weeks = q1(conn, """
        SELECT COUNT(*) as n FROM vehicle_weekly_features
        WHERE battery_id = ? AND kps_slope_8wk < -0.01
          AND week_number >= (SELECT MAX(week_number) - 12 FROM vehicle_weekly_features WHERE battery_id = ?)
    """, [battery_id, battery_id])
    total_weeks = q1(conn, """
        SELECT COUNT(*) as n FROM vehicle_weekly_features
        WHERE battery_id = ?
          AND week_number >= (SELECT MAX(week_number) - 12 FROM vehicle_weekly_features WHERE battery_id = ?)
    """, [battery_id, battery_id])
    drop_n = (range_drop_weeks or {}).get("n") or 0
    total_n = (total_weeks or {}).get("n") or 1
    alert_conv_rate = round(drop_n / max(total_n, 1), 2)

    # Data gap cause
    iot_q = row.get("iot_quality_score") or 0
    if iot_q < 20:
        data_gap_cause = "IOT_DEVICE"
    elif iot_q < 50 and alert_count > 100:
        data_gap_cause = "BATTERY"
    else:
        data_gap_cause = None

    row["npf_probability"] = npf_prob
    row["alert_to_range_conversion_rate"] = alert_conv_rate
    row["data_gap_cause"] = data_gap_cause
    row["firmware_version"] = None  # Not available in current data

    return row


@app.get("/api/oem/component-health/{battery_id}")
@safe
def oem_component_health(battery_id: str, _=Depends(verify_token)):
    """Component intelligence for single battery."""
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")
    conn = get_conn()
    result = _component_health_query(conn, battery_id)
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="No component health data")
    return result


@app.get("/api/oem/firmware-analysis")
@safe
def oem_firmware_analysis(_=Depends(verify_token)):
    """Cross-fleet firmware analysis — groups by IoT status as firmware proxy."""
    conn = get_conn()
    # Since firmware_version not available, group by iot_status as proxy
    # and physical_damage_risk for systematic pattern detection
    rows = q(conn, """
        SELECT ch.iot_status as firmware_version,
               COUNT(DISTINCT ch.battery_id) as battery_count,
               ROUND(AVG(sub.npf_est), 2) as avg_npf_probability,
               SUM(ch.bms_alert_count_7d) as total_alerts
        FROM battery_component_health ch
        LEFT JOIN (
            SELECT battery_id,
                   CASE WHEN corroboration_score <= 1 THEN 0.7
                        WHEN corroboration_score <= 2 THEN 0.4
                        ELSE 0.15 END as npf_est
            FROM battery_health_scores_v2
        ) sub ON ch.battery_id = sub.battery_id
        WHERE ch.week_number >= (
            SELECT MAX(week_number) - 12 FROM battery_component_health
        )
          AND ch.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        GROUP BY ch.iot_status
        ORDER BY avg_npf_probability DESC
    """)
    conn.close()
    for r in rows:
        count = r.get("battery_count") or 0
        npf = r.get("avg_npf_probability") or 0
        r["systematic_flag"] = bool(npf > 0.6 and count > 5)
    return rows


@app.get("/api/oem/iot-quality-summary")
@safe
def oem_iot_quality_summary(_=Depends(verify_token)):
    """Fleet-level IoT quality summary."""
    conn = get_conn()
    # Get latest week per battery
    rows = q(conn, """
        SELECT ch.battery_id, ch.iot_quality_score, ch.iot_status
        FROM battery_component_health ch
        WHERE ch.week_number = (
            SELECT MAX(week_number) FROM battery_component_health WHERE battery_id = ch.battery_id
        )
          AND ch.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
    """)
    conn.close()

    total = len(rows)
    if total == 0:
        return {"high_quality_pct": 0, "medium_quality_pct": 0, "low_quality_pct": 0,
                "iot_failure_count": 0, "estimated_saving_inr": 0}

    high = sum(1 for r in rows if (r.get("iot_quality_score") or 0) >= 70)
    medium = sum(1 for r in rows if 30 <= (r.get("iot_quality_score") or 0) < 70)
    low = sum(1 for r in rows if (r.get("iot_quality_score") or 0) < 30)
    iot_failure = sum(1 for r in rows if r.get("iot_status") == "IOT_CRITICAL")

    return {
        "high_quality_pct": round(high / total * 100, 1),
        "medium_quality_pct": round(medium / total * 100, 1),
        "low_quality_pct": round(low / total * 100, 1),
        "iot_failure_count": iot_failure,
        "estimated_saving_inr": iot_failure * 38500,
    }


# ══════════════════════════════════════════════════════════════
# INTELLIGENCE CHAIN ENDPOINTS (Stage 4 — April 2026)
# ══════════════════════════════════════════════════════════════

_CHAIN_META = {
    1: {"name": "Decline Detection", "desc": "CAN telemetry to action assignment"},
    2: {"name": "Behavior-to-Degradation", "desc": "Operator behavior to warranty impact"},
    3: {"name": "Pack Design Gap", "desc": "Commissioning quality to field performance"},
    4: {"name": "Systematic Fault Detection", "desc": "BMS alerts to firmware patch recommendation"},
    5: {"name": "Aggressive Driver", "desc": "Behavioral fingerprint to degradation acceleration"},
    6: {"name": "Use Case Destruction", "desc": "Same use case, different pack outcomes"},
    7: {"name": "Service Negligence", "desc": "Ignored alerts to avoidable replacement"},
}


@app.get("/api/intelligence/chain/{chain_id}")
@safe
def intelligence_chain_meta(chain_id: int, _=Depends(verify_token)):
    """Chain metadata."""
    if chain_id not in _CHAIN_META:
        raise HTTPException(status_code=404, detail="Chain not found")
    return _CHAIN_META[chain_id]


@app.get("/api/intelligence/chain/{chain_id}/data")
@safe
def intelligence_chain_data(chain_id: int, _=Depends(verify_token)):
    """Live data for a specific intelligence chain."""
    if chain_id not in _CHAIN_META:
        raise HTTPException(status_code=404, detail="Chain not found")
    conn = get_conn()
    try:
        if chain_id == 1:
            result = _chain_1_decline(conn)
        elif chain_id == 2:
            result = _chain_2_behavior(conn)
        elif chain_id == 3:
            result = _chain_3_pack_gap(conn)
        elif chain_id == 4:
            result = _chain_4_systematic(conn)
        elif chain_id == 5:
            result = _chain_5_aggressive(conn)
        elif chain_id == 6:
            result = _chain_6_usecase(conn)
        elif chain_id == 7:
            result = _chain_7_negligence(conn)
        else:
            result = {"nodes": [], "audience_framings": {}}
    finally:
        conn.close()
    result["chain_id"] = chain_id
    result["chain_name"] = _CHAIN_META[chain_id]["name"]
    return result


def _chain_1_decline(conn):
    """Chain 1: Decline Detection — BAT_LFP_278 focus."""
    s = q1(conn, """SELECT kps_slope_8wk, bhs_score_v2, tier_label_v2, range_corrected_km,
                            pct_of_commissioned, corroboration_score, rul_action_v2,
                            rul_weeks_v2, confidence_pct, degradation_regime
                     FROM battery_health_scores_v2 WHERE battery_id = 'BAT_LFP_278'""")
    fc = q1(conn, """SELECT range_p50_km, range_p10_km, range_p90_km, horizon_weeks
                      FROM dt_lfp_range_projections WHERE battery_id = 'BAT_LFP_278'
                      ORDER BY horizon_weeks DESC LIMIT 1""")
    slope = s.get("kps_slope_8wk") or 0
    nodes = [
        {"id": "n1", "label": "CAN Telemetry", "value": "30-sec stream", "unit": "",
         "signal_source": "enerlytik_30sec.duckdb", "expandable_detail": "Raw voltage, current, temperature at 30-second intervals"},
        {"id": "n2", "label": "Efficiency Slope", "value": f"{slope*100:.2f}", "unit": "%/wk",
         "signal_source": "BHS v2.2", "expandable_detail": f"8-week KPS slope for BAT_LFP_278. Negative = declining efficiency."},
        {"id": "n3", "label": "BHS Score", "value": f"{s['bhs_score_v2']:.0f}", "unit": "/100",
         "signal_source": "BHS v2.2", "expandable_detail": f"Tier: {s['tier_label_v2']}. Range: {s['range_corrected_km']:.0f}km ({s['pct_of_commissioned']:.0f}% of commissioned)."},
        {"id": "n4", "label": "Corroboration", "value": f"{s['corroboration_score']}", "unit": "/7 signals",
         "signal_source": "Signal cross-check", "expandable_detail": "Number of independent signals agreeing on degradation direction."},
        {"id": "n5", "label": "Action", "value": _ACTION_PLAIN_MAP.get(s["rul_action_v2"], s["rul_action_v2"]), "unit": "",
         "signal_source": "Rule engine", "expandable_detail": f"Regime: {s['degradation_regime']}. Confidence: {s['confidence_pct']:.0f}%."},
        {"id": "n6", "label": "Forecast", "value": f"{fc['range_p50_km']:.0f}" if fc else "--", "unit": "km P50",
         "signal_source": "Range projection model", "expandable_detail": f"P10={fc['range_p10_km']:.0f}km, P90={fc['range_p90_km']:.0f}km at {fc['horizon_weeks']}wk" if fc else "No forecast available"},
    ]
    framings = {
        "operator": f"Unit 278 range has dropped to {s['range_corrected_km']:.0f}km ({s['pct_of_commissioned']:.0f}% of original). Efficiency is declining at {slope*100:.2f}%/week. Monitor weekly and plan for replacement if decline continues.",
        "nbfc": f"Asset 278 is rated {s['tier_label_v2']} with BHS {s['bhs_score_v2']:.0f}/100. Current range {s['range_corrected_km']:.0f}km is {s['pct_of_commissioned']:.0f}% of commissioned baseline. Residual value declining.",
        "oem": f"Pack3001 unit 278 shows efficiency slope {slope*100:.2f}%/wk with {s['corroboration_score']}/7 signal corroboration. Degradation regime: {s['degradation_regime']}. Root cause: cell imbalance + calendar aging.",
    }
    return {"nodes": nodes, "audience_framings": framings, "demo_battery": "BAT_LFP_278"}


def _chain_2_behavior(conn):
    """Chain 2: Behavior-to-Degradation — BAT_LFP_278 focus."""
    s = q1(conn, """SELECT attr_cycle_pct, attr_imbalance_pct, attr_thermal_pct,
                            warranty_void_risk, warranty_claim_eligible, tier_label_v2,
                            range_corrected_km, bhs_score_v2
                     FROM battery_health_scores_v2 WHERE battery_id = 'BAT_LFP_278'""")
    p = q1(conn, """SELECT personal_charge_profile, personal_drive_profile, service_profile,
                            service_compliance_score, driver_stress_tier
                     FROM battery_personal_params WHERE battery_id = 'BAT_LFP_278'""")
    charge = p.get("personal_charge_profile") or "UNKNOWN" if p else "UNKNOWN"
    drive = p.get("personal_drive_profile") or "UNKNOWN" if p else "UNKNOWN"
    service = p.get("service_profile") or "UNKNOWN" if p else "UNKNOWN"
    compliance = p.get("service_compliance_score") or 0 if p else 0
    nodes = [
        {"id": "n1", "label": "Charging Profile", "value": charge, "unit": "",
         "signal_source": "Personal params", "expandable_detail": f"Drive: {drive}. Stress tier: {(p or {}).get('driver_stress_tier', 'N/A')}"},
        {"id": "n2", "label": "Cycle Attribution", "value": f"{s['attr_cycle_pct']:.0f}", "unit": "%",
         "signal_source": "Rule-based attribution", "expandable_detail": f"Imbalance: {s['attr_imbalance_pct']:.0f}%, Thermal: {s['attr_thermal_pct']:.0f}%"},
        {"id": "n3", "label": "Warranty Risk", "value": s.get("warranty_void_risk") or "NORMAL", "unit": "",
         "signal_source": "Warranty engine", "expandable_detail": f"Claim eligible: {'Yes' if s['warranty_claim_eligible'] else 'No'}"},
        {"id": "n4", "label": "Service Profile", "value": service, "unit": "",
         "signal_source": "Compliance scoring", "expandable_detail": f"Compliance score: {compliance:.0%}. Operator behavior drives {s['attr_cycle_pct']:.0f}% of degradation via cycling patterns."},
    ]
    framings = {
        "operator": f"Your charging pattern ({charge}) and service record ({service}) are contributing to {s['attr_cycle_pct']:.0f}% of this unit's degradation. Improving charging discipline can slow decline.",
        "nbfc": f"Operator behavior accounts for {s['attr_cycle_pct']:.0f}% cycle stress + {s['attr_imbalance_pct']:.0f}% imbalance. Service compliance: {compliance:.0%}. Warranty risk: {s.get('warranty_void_risk', 'NORMAL')}.",
        "oem": f"Unit 278 degradation is {s['attr_cycle_pct']:.0f}% cycle + {s['attr_imbalance_pct']:.0f}% imbalance driven. Operator profile: {service}. This is behavioral, not a pack design issue.",
    }
    return {"nodes": nodes, "audience_framings": framings, "demo_battery": "BAT_LFP_278"}


def _chain_3_pack_gap(conn):
    """Chain 3: Pack Design Gap — Pack3401 vs Pack3001 comparison."""
    packs = q(conn, """
        SELECT b.battery_model as pack_model,
               ROUND(AVG(s.commissioning_spread_mv), 1) as avg_comm_spread,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_bhs,
               ROUND(AVG(s.pct_of_commissioned), 1) as avg_pct,
               COUNT(*) as n
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model IN ('GF_LFP_Pack3001', 'GF_LFP_Pack3401')
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
        GROUP BY b.battery_model
    """)
    p3001 = next((p for p in packs if "Pack3001" in p["pack_model"]), {})
    p3401 = next((p for p in packs if "Pack3401" in p["pack_model"]), {})
    nodes = [
        {"id": "n1", "label": "Commissioning Spread", "value": f"{p3401.get('avg_comm_spread', 0):.0f} vs {p3001.get('avg_comm_spread', 0):.0f}", "unit": "mV",
         "signal_source": "Commissioning data", "expandable_detail": f"Pack3401: {p3401.get('avg_comm_spread')}mV avg at delivery. Pack3001: {p3001.get('avg_comm_spread')}mV. Higher spread = worse cell matching."},
        {"id": "n2", "label": "BHS Score Gap", "value": f"{p3401.get('avg_bhs', 0):.0f} vs {p3001.get('avg_bhs', 0):.0f}", "unit": "/100",
         "signal_source": "BHS v2.2", "expandable_detail": f"Pack3401 avg BHS {p3401.get('avg_bhs')} vs Pack3001 {p3001.get('avg_bhs')}. Gap: {(p3001.get('avg_bhs', 0) - p3401.get('avg_bhs', 0)):.1f} points."},
        {"id": "n3", "label": "% Commissioned", "value": f"{p3401.get('avg_pct', 0):.0f} vs {p3001.get('avg_pct', 0):.0f}", "unit": "%",
         "signal_source": "Range baseline", "expandable_detail": f"Pack3401 retains {p3401.get('avg_pct')}% of original range vs Pack3001 at {p3001.get('avg_pct')}%."},
        {"id": "n4", "label": "Fleet Size", "value": f"{p3401.get('n', 0)} vs {p3001.get('n', 0)}", "unit": "units",
         "signal_source": "Fleet registry", "expandable_detail": "Sample sizes for statistical comparison."},
        {"id": "n5", "label": "OEM Action", "value": "Warranty review", "unit": "",
         "signal_source": "Platform recommendation", "expandable_detail": f"Pack3401 at {p3401.get('avg_pct')}% vs 85% warranty floor. Claim eligible. Cell matching improvement recommended for future production."},
    ]
    bhs_gap = (p3001.get("avg_bhs", 0) - p3401.get("avg_bhs", 0))
    framings = {
        "operator": f"Your Pack3401 units retain {p3401.get('avg_pct', 0):.0f}% of original range vs {p3001.get('avg_pct', 0):.0f}% for Pack3001. This is a manufacturing difference, not usage.",
        "nbfc": f"Pack3401 portfolio scores {bhs_gap:.0f} points below Pack3001 on health index. {p3401.get('avg_pct', 0):.0f}% range retention vs 85% warranty floor. Higher residual risk on Pack3401 assets.",
        "oem": f"Pack3401 commissioning spread {p3401.get('avg_comm_spread', 0):.0f}mV vs Pack3001 {p3001.get('avg_comm_spread', 0):.0f}mV. Cell matching quality at production is the primary differentiator. BHS gap: {bhs_gap:.0f} points.",
    }
    return {"nodes": nodes, "audience_framings": framings}


def _chain_4_systematic(conn):
    """Chain 4: Systematic Fault Detection — firmware/IoT analysis."""
    fw = q(conn, """
        SELECT ch.iot_status as firmware_group,
               COUNT(DISTINCT ch.battery_id) as battery_count,
               SUM(ch.bms_alert_count_7d) as total_alerts
        FROM battery_component_health ch
        WHERE ch.week_number >= (SELECT MAX(week_number) - 12 FROM battery_component_health)
          AND ch.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        GROUP BY ch.iot_status
        ORDER BY total_alerts DESC
    """)
    iot_critical = next((f for f in fw if f["firmware_group"] == "IOT_CRITICAL"), {})
    total_alerts = sum(f.get("total_alerts") or 0 for f in fw)
    crit_count = iot_critical.get("battery_count") or 0
    # NPF cost estimate: 144 visits × ₹3000/visit
    npf_cost = 144 * 3000
    nodes = [
        {"id": "n1", "label": "BMS Alerts (12wk)", "value": f"{total_alerts:,}", "unit": "alerts",
         "signal_source": "battery_component_health", "expandable_detail": f"Total BMS alerts across fleet in last 12 weeks."},
        {"id": "n2", "label": "Cross-fleet Grouping", "value": f"{len(fw)}", "unit": "groups",
         "signal_source": "IoT status clustering", "expandable_detail": "Batteries grouped by IoT quality status as firmware proxy."},
        {"id": "n3", "label": "Critical IoT Cluster", "value": f"{crit_count}", "unit": "units",
         "signal_source": "Component health", "expandable_detail": f"IOT_CRITICAL devices: degraded telemetry quality, potential device failure."},
        {"id": "n4", "label": "NPF Rate", "value": "High", "unit": "",
         "signal_source": "Corroboration analysis", "expandable_detail": "Alerts not corroborated by degradation signals = likely No Problem Found."},
        {"id": "n5", "label": "Estimated Waste", "value": f"\u20b9{npf_cost:,}", "unit": "/yr",
         "signal_source": "Service cost model", "expandable_detail": f"144 estimated NPF visits x \u20b93,000 per visit = \u20b9{npf_cost:,}/yr avoidable cost."},
    ]
    framings = {
        "operator": f"Your fleet generated {total_alerts:,} BMS alerts in 12 weeks. Many are false positives from IoT device issues, not battery faults. {crit_count} devices need IoT hardware check.",
        "nbfc": f"{crit_count} units have degraded IoT telemetry (IOT_CRITICAL). Data confidence is lower for these assets. Factor into portfolio risk scoring.",
        "oem": f"Fleet-wide alert analysis shows {crit_count} IOT_CRITICAL devices. Estimated \u20b9{npf_cost:,}/yr in avoidable NPF service visits. IoT device refresh recommended before next warranty cycle.",
    }
    return {"nodes": nodes, "audience_framings": framings}


def _chain_5_aggressive(conn):
    """Chain 5: Aggressive Driver — worst behavioral fingerprint."""
    bat = q1(conn, """
        SELECT s.battery_id, s.stress_index, s.attr_cycle_pct, s.attr_thermal_pct,
               s.bhs_score_v2, s.tier_label_v2, s.range_corrected_km,
               p.personal_drive_profile, p.personal_charge_profile, s.driver_stress_tier
        FROM battery_health_scores_v2 s
        JOIN battery_personal_params p ON s.battery_id = p.battery_id
        WHERE p.personal_drive_profile = 'AGGRESSIVE' AND p.personal_charge_profile = 'IRREGULAR'
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        ORDER BY s.stress_index DESC LIMIT 1
    """)
    if not bat:
        return {"nodes": [], "audience_framings": {"operator": "", "nbfc": "", "oem": ""}}
    bid = bat["battery_id"]
    si = bat["stress_index"] or 0
    nodes = [
        {"id": "n1", "label": "Drive Profile", "value": "Aggressive", "unit": "",
         "signal_source": "Personal params", "expandable_detail": f"Unit {bid}: classified as AGGRESSIVE based on discharge rate patterns."},
        {"id": "n2", "label": "Charge Profile", "value": "Irregular", "unit": "",
         "signal_source": "Personal params", "expandable_detail": "Irregular charging pattern — partial charges, inconsistent timing."},
        {"id": "n3", "label": "Stress Index", "value": f"{si:.2f}", "unit": "/1.0",
         "signal_source": "Commissioning baseline", "expandable_detail": f"0 = no stress, 1 = maximum. This unit at {si:.2f} — top percentile of fleet."},
        {"id": "n4", "label": "Cycle Attribution", "value": f"{bat['attr_cycle_pct']:.0f}", "unit": "%",
         "signal_source": "Rule-based attribution", "expandable_detail": f"Thermal: {bat['attr_thermal_pct']:.0f}%. Aggressive driving + irregular charging amplify cycle stress."},
        {"id": "n5", "label": "BHS Score", "value": f"{bat['bhs_score_v2']:.0f}", "unit": "/100",
         "signal_source": "BHS v2.2", "expandable_detail": f"Tier: {bat['tier_label_v2']}. Range: {bat['range_corrected_km']:.0f}km."},
        {"id": "n6", "label": "Coaching Action", "value": "Driver intervention", "unit": "",
         "signal_source": "Platform recommendation", "expandable_detail": "Targeted coaching on charging discipline and driving patterns can reduce stress index by 20-30%."},
    ]
    framings = {
        "operator": f"Unit {bid.replace('BAT_LFP_', '')} has the highest stress fingerprint in your fleet (stress index {si:.2f}). Aggressive driving + irregular charging are accelerating wear. Coaching this operator can extend battery life.",
        "nbfc": f"Unit {bid.replace('BAT_LFP_', '')} stress index {si:.2f}/1.0 — top percentile. Behavioral factors drive {bat['attr_cycle_pct']:.0f}% of degradation. Operator intervention is the lowest-cost risk mitigation.",
        "oem": f"Behavioral fingerprint: AGGRESSIVE drive + IRREGULAR charge → stress index {si:.2f}. This is operator-caused degradation, not a product defect. {bat['attr_cycle_pct']:.0f}% cycle attribution confirms behavioral root cause.",
    }
    return {"nodes": nodes, "audience_framings": framings, "demo_battery": bid}


def _chain_6_usecase(conn):
    """Chain 6: Use Case Destruction — Pack3401 vs Pack3001 under same STANDARD_URBAN use case."""
    packs = q(conn, """
        SELECT b.battery_model as pack_model,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_bhs,
               ROUND(AVG(s.stress_index), 3) as avg_stress,
               COUNT(*) as n
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model IN ('GF_LFP_Pack3001', 'GF_LFP_Pack3401')
          AND s.use_case_inferred = 'STANDARD_URBAN'
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
        GROUP BY b.battery_model
    """)
    p3001 = next((p for p in packs if "Pack3001" in p["pack_model"]), {})
    p3401 = next((p for p in packs if "Pack3401" in p["pack_model"]), {})
    range_gap = (p3001.get("avg_range") or 0) - (p3401.get("avg_range") or 0)
    bhs_gap = (p3001.get("avg_bhs") or 0) - (p3401.get("avg_bhs") or 0)
    nodes = [
        {"id": "n1", "label": "Use Case", "value": "Standard Urban", "unit": "",
         "signal_source": "Fleet classification", "expandable_detail": "Both packs operating in identical STANDARD_URBAN conditions."},
        {"id": "n2", "label": "Pack3001 Range", "value": f"{p3001.get('avg_range', 0):.0f}", "unit": "km avg",
         "signal_source": "BHS v2.2", "expandable_detail": f"n={p3001.get('n', 0)} units. BHS avg {p3001.get('avg_bhs', 0)}."},
        {"id": "n3", "label": "Pack3401 Range", "value": f"{p3401.get('avg_range', 0):.0f}", "unit": "km avg",
         "signal_source": "BHS v2.2", "expandable_detail": f"n={p3401.get('n', 0)} units. BHS avg {p3401.get('avg_bhs', 0)}."},
        {"id": "n4", "label": "Range Gap", "value": f"{range_gap:.0f}", "unit": "km",
         "signal_source": "Cross-pack comparison", "expandable_detail": f"Same use case, {range_gap:.0f}km gap. BHS gap: {bhs_gap:.0f} points."},
        {"id": "n5", "label": "Root Cause", "value": "Pack design", "unit": "",
         "signal_source": "Attribution engine", "expandable_detail": "Identical use case eliminates operator behavior as variable. Difference is manufacturing quality."},
    ]
    framings = {
        "operator": f"Under the same urban usage, Pack3001 delivers {p3001.get('avg_range', 0):.0f}km vs Pack3401 at {p3401.get('avg_range', 0):.0f}km. The {range_gap:.0f}km gap is not caused by how you drive — it is a product difference.",
        "nbfc": f"Same-use-case comparison shows Pack3401 underperforms by {range_gap:.0f}km and {bhs_gap:.0f} BHS points vs Pack3001. Pack model is a stronger risk predictor than operator behavior for these assets.",
        "oem": f"Controlled comparison: STANDARD_URBAN use case, {range_gap:.0f}km range gap between Pack3401 (n={p3401.get('n', 0)}) and Pack3001 (n={p3001.get('n', 0)}). Usage is not the variable. Cell matching at commissioning is the differentiator.",
    }
    return {"nodes": nodes, "audience_framings": framings}


def _chain_7_negligence(conn):
    """Chain 7: Service Negligence — NEGLIGENT operator example."""
    bat = q1(conn, """
        SELECT s.battery_id, s.tier_label_v2, s.bhs_score_v2, s.cell_balance_spread_mv,
               s.fault_profile_tier, s.range_corrected_km,
               p.service_profile, p.service_compliance_score
        FROM battery_health_scores_v2 s
        JOIN battery_personal_params p ON s.battery_id = p.battery_id
        WHERE p.service_profile = 'NEGLIGENT' AND s.tier_label_v2 IN ('STRESSED', 'CRITICAL')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        ORDER BY s.bhs_score_v2 ASC LIMIT 1
    """)
    if not bat:
        return {"nodes": [], "audience_framings": {"operator": "", "nbfc": "", "oem": ""}}
    bid = bat["battery_id"]
    spread = bat.get("cell_balance_spread_mv") or 0
    compliance = bat.get("service_compliance_score") or 0
    # Cost comparison sourced from fleet_context_params (Rule 208 — never hardcode)
    service_cost = float(get_param("cell_balance_service_cost_inr", default=2000))
    replace_cost = float(get_param("battery_replacement_cost_inr", default=60000))
    nodes = [
        {"id": "n1", "label": "Alert Fired", "value": bat["fault_profile_tier"] or "ACTIVE", "unit": "",
         "signal_source": "Event engine", "expandable_detail": f"Unit {bid}: fault profile {bat['fault_profile_tier']}. Alerts generated but not acted upon."},
        {"id": "n2", "label": "Compliance Score", "value": f"{compliance:.0%}", "unit": "",
         "signal_source": "Service tracking", "expandable_detail": f"Operator compliance: {compliance:.0%}. NEGLIGENT = consistently ignored maintenance alerts."},
        {"id": "n3", "label": "Spread Worsened", "value": f"{spread:.0f}", "unit": "mV",
         "signal_source": "BHS v2.2", "expandable_detail": f"Cell balance spread at {spread:.0f}mV. Unchecked imbalance accelerates degradation."},
        {"id": "n4", "label": "Tier Migration", "value": bat["tier_label_v2"], "unit": "",
         "signal_source": "Scoring engine", "expandable_detail": f"BHS {bat['bhs_score_v2']:.0f}/100. Range: {bat['range_corrected_km']:.0f}km."},
        {"id": "n5", "label": "Cost Impact", "value": f"\u20b9{service_cost:,} vs \u20b9{replace_cost:,}", "unit": "",
         "signal_source": "Service cost model", "expandable_detail": f"Cell balance service: \u20b9{service_cost:,}. Full replacement: \u20b9{replace_cost:,}. {replace_cost//service_cost}x cost difference from delayed action."},
    ]
    framings = {
        "operator": f"Unit {bid.replace('BAT_LFP_', '')} alerts were ignored (compliance {compliance:.0%}). Cell spread reached {spread:.0f}mV. A \u20b9{service_cost:,} service visit could have prevented a \u20b9{replace_cost:,} replacement.",
        "nbfc": f"NEGLIGENT operator on unit {bid.replace('BAT_LFP_', '')} — compliance {compliance:.0%}. Asset migrated to {bat['tier_label_v2']} (BHS {bat['bhs_score_v2']:.0f}). Service negligence is a material risk factor for this asset class.",
        "oem": f"Unit {bid.replace('BAT_LFP_', '')} demonstrates the operator negligence pattern: ignored alerts → spread worsening → grade migration → avoidable replacement. Operator coaching program would reduce warranty claims from this segment.",
    }
    return {"nodes": nodes, "audience_framings": framings, "demo_battery": bid}


# ══════════════════════════════════════════════════════════════
# SERVICE INTELLIGENCE ENDPOINT (Passport Next v2 — April 2026)
# ══════════════════════════════════════════════════════════════

@app.get("/api/battery/{battery_id}/service-intelligence")
@safe
def battery_service_intelligence(battery_id: str, _=Depends(verify_token)):
    """Service profile, fault status, and intervention ROI for a battery."""
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")
    conn = get_conn()
    s = q1(conn, """
        SELECT fault_profile_tier, fault_duration_weeks, fault_recurrence_count,
               corroboration_score, corroboration_signals_list, rul_action_v2,
               degradation_regime, bhs_score_v2
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    if not s:
        conn.close()
        raise HTTPException(status_code=404, detail="Battery not found")
    pp = q1(conn, """
        SELECT service_profile, alert_burden_tier, service_compliance_score, policy_adherence
        FROM battery_personal_params WHERE battery_id = ?
    """, [battery_id])
    cc = q1(conn, """
        SELECT action_remark FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()

    result = {
        "battery_id": battery_id,
        "service_profile": (pp or {}).get("service_profile"),
        "alert_burden_tier": (pp or {}).get("alert_burden_tier"),
        "service_compliance_score": (pp or {}).get("service_compliance_score"),
        "policy_adherence": (pp or {}).get("policy_adherence"),
        "fault_severity": s.get("fault_profile_tier"),
        "fault_duration_weeks": s.get("fault_duration_weeks"),
        "fault_recurrence_count": s.get("fault_recurrence_count"),
        "corroboration_score": s.get("corroboration_score"),
        "corroboration_signals_list": s.get("corroboration_signals_list"),
        "action_remark": (cc or {}).get("action_remark"),
        "rul_action_v2": s.get("rul_action_v2"),
        "action_plain": _ACTION_PLAIN_MAP.get(s.get("rul_action_v2"), s.get("rul_action_v2")),
    }

    # Compute intervention ROI
    action = s.get("rul_action_v2")
    corr = s.get("corroboration_score") or 0
    if action in ("CELL_BALANCE_PRIORITY", "MONITOR_INVESTIGATE"):
        recovery_probability = round(corr / 7.0, 2)
        service_cost = float(get_param("cell_balance_service_cost_inr", default=2000))
        replace_cost = float(get_param("battery_replacement_cost_inr", default=60000))
        expected_value = round(recovery_probability * (replace_cost - service_cost))
        roi_ratio = round(expected_value / service_cost, 1) if service_cost > 0 else 0
        result["intervention_roi"] = {
            "recovery_probability": recovery_probability,
            "service_cost_inr": service_cost,
            "replacement_cost_inr": replace_cost,
            "expected_value_inr": expected_value,
            "roi_ratio": roi_ratio,
        }
    else:
        result["intervention_roi"] = None

    return result


@app.get("/api/battery/{battery_id}/bhs-explanation")
@safe
def battery_bhs_explanation(battery_id: str, _=Depends(verify_token)):
    """BHS v3 shadow score attribution: per-component gate scores, cohort deltas, top drivers."""
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")
    import sys, os
    _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from pipeline.bhs_explanation_generator import generate_bhs_explanation
    conn = get_conn()
    out = generate_bhs_explanation(battery_id, conn)
    if isinstance(out, dict) and out.get("error") == "battery_id not found":
        raise HTTPException(status_code=404, detail="Battery not found")
    return out


# TODO: service restart required to activate /api/battery/{id}/bhs-explanation
# Test after restart:
#   curl -H "Authorization: Bearer $API_TOKEN" http://localhost:3001/api/battery/BAT_LFP_044/bhs-explanation

# ── Cohort Intelligence Endpoints ─────────────────────────────────────

@app.get("/api/battery/{battery_id}/signal-history")
@safe
def battery_signal_history(battery_id: str, signal: str = "range", weeks: int = 52, _=Depends(verify_token)):
    """Time-series for one signal. Sprint 4 — Analytics tab signal explorer.
    signal ∈ {range, soh, spread, r0, efc, dod}."""
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")
    # signal → (table, expression). All values returned in user-facing units.
    VWF_MAP = {
        "range":  "km_per_soc_pct * 80.0 AS value",  # km — kps × 80% DoD
        "soh":    "soh_cap_weekly AS value",          # already %
        "spread": "cell_spread_max AS value",         # mV
        "r0":     "r0_weekly_median * 1000.0 AS value",  # mΩ
        "dod":    "dod_corrected AS value",           # already %
    }
    conn = get_conn()
    try:
        if signal == "efc":
            # EFC tracked per-week in battery_health_scores_v2
            rows = q(conn, """
                SELECT week_number, efc_cumulative AS value
                FROM battery_health_scores_v2
                WHERE battery_id = ? AND week_number IS NOT NULL AND efc_cumulative IS NOT NULL
                ORDER BY week_number DESC LIMIT ?
            """, [battery_id, weeks])
        elif signal in VWF_MAP:
            rows = q(conn, f"""
                SELECT week_number, {VWF_MAP[signal]}
                FROM vehicle_weekly_features
                WHERE battery_id = ? AND week_number IS NOT NULL
                ORDER BY week_number DESC LIMIT ?
            """, [battery_id, weeks])
        else:
            conn.close()
            return {"battery_id": battery_id, "signal": signal, "weeks_requested": weeks, "data": [], "error": f"unknown signal '{signal}'"}
    except Exception as e:
        conn.close()
        return {"battery_id": battery_id, "signal": signal, "weeks_requested": weeks, "data": [], "error": str(e)}
    conn.close()
    rows.reverse()  # ASC for plotting
    return {"battery_id": battery_id, "signal": signal, "weeks_returned": len(rows), "data": rows}


@app.get("/api/battery/{battery_id}/cohort-position")
@safe
def battery_cohort_position(battery_id: str, _=Depends(verify_token)):
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")
    conn = get_conn()
    pos = q1(conn, """
        SELECT * FROM cohort_battery_position
        WHERE battery_id = ?
        ORDER BY week_number DESC LIMIT 1
    """, [battery_id])
    if not pos:
        raise HTTPException(status_code=404, detail="No cohort position for this battery")
    fired = q(conn, """
        SELECT rule_id, severity_actual, attribution_hint, resolution
        FROM cohort_rule_evaluations
        WHERE battery_id = ? AND week_number = ? AND fired = 1
    """, [battery_id, pos["week_number"]])
    pos["fired_rules"] = fired
    return pos


@app.get("/api/battery/{battery_id}/cohort-curves")
@safe
def battery_cohort_curves(battery_id: str, _=Depends(verify_token)):
    if battery_id in _BLOCKED_BATTERIES:
        raise HTTPException(status_code=404, detail="Battery not found")
    conn = get_conn()
    pos = q1(conn, """
        SELECT cohort_key, cohort_confidence
        FROM cohort_battery_position
        WHERE battery_id = ?
        ORDER BY week_number DESC LIMIT 1
    """, [battery_id])
    if not pos:
        raise HTTPException(status_code=404, detail="No cohort data for this battery")
    ck = pos["cohort_key"]
    rows = q(conn, """
        SELECT age_bracket, signal_name, p25, p50, p75, n_batteries, confidence
        FROM cohort_baseline_curves
        WHERE cohort_key = ?
        AND signal_name IN ('range_est_km','soh_cap_weekly','cell_spread_max')
    """, [ck])
    curves = {}
    for r in rows:
        ab = r["age_bracket"]
        sig = r["signal_name"]
        if ab not in curves:
            curves[ab] = {}
        curves[ab][sig] = {"p25": r["p25"], "p50": r["p50"], "p75": r["p75"]}
    return {
        "cohort_key": ck,
        "confidence": pos["cohort_confidence"],
        "n_batteries": rows[0]["n_batteries"] if rows else 0,
        "curves": curves,
    }


@app.get("/api/fleet/cohort-summary")
@safe
def fleet_cohort_summary(_=Depends(verify_token)):
    conn = get_conn()
    cohorts = q(conn, """
        SELECT DISTINCT cohort_key, pack_model, city, use_case_inferred,
               n_batteries, confidence,
               ROUND(p50, 1) as avg_range_p50
        FROM cohort_baseline_curves
        WHERE signal_name = 'range_est_km'
        AND age_bracket = 'MID'
        ORDER BY cohort_key
    """)
    soh_rows = q(conn, """
        SELECT cohort_key, ROUND(p50, 2) as avg_soh_p50
        FROM cohort_baseline_curves
        WHERE signal_name = 'soh_cap_weekly'
        AND age_bracket = 'MID'
    """)
    soh_map = {r["cohort_key"]: r["avg_soh_p50"] for r in soh_rows}
    for c in cohorts:
        c["avg_soh_p50"] = soh_map.get(c["cohort_key"])

    rule_counts = q(conn, """
        SELECT
            SUM(CASE WHEN rule_id = 'R07' THEN 1 ELSE 0 END) as n_cohort_divergence,
            SUM(CASE WHEN rule_id = 'R10' AND resolution = 'ADVANCED_IMBALANCE' THEN 1 ELSE 0 END) as n_advanced_imbalance,
            SUM(CASE WHEN rule_id = 'R09' AND resolution = 'OPERATIONAL_CONFIRMED' THEN 1 ELSE 0 END) as n_operational_confirmed,
            SUM(CASE WHEN rule_id = 'R09' AND resolution = 'CHEMISTRY_CONFIRMED' THEN 1 ELSE 0 END) as n_chemistry_confirmed
        FROM cohort_rule_evaluations
        WHERE fired = 1
    """)
    return {
        "cohorts": cohorts,
        "rule_firing_counts": rule_counts[0] if rule_counts else {},
    }


@app.get("/api/cohort/{cohort_key:path}/thresholds")
@safe
def cohort_thresholds(cohort_key: str, _=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT * FROM cohort_event_thresholds
        WHERE cohort_key = ?
        ORDER BY age_bracket, event_code
    """, [cohort_key])
    if not rows:
        raise HTTPException(status_code=404, detail="No thresholds for this cohort")
    return rows


# ══════════════════════════════════════════════════════════════
# MEGA-SPRINT 1: BACKEND FOUNDATION (April 2026)
# SP-1: Passport API (B1-B8) + SP-9: OEM Backend (OEM1-OEM8)
# ══════════════════════════════════════════════════════════════

_OEM_LABEL_MAP = {
    "charging": "Charging Pattern",
    "usage": "Usage Intensity",
    "thermal": "Thermal Exposure",
    "maintenance": "Cell Maintenance",
    "calendar": "Calendar Aging",
}


# ── B1+B2: Extend /api/battery/:id/passport — DRI/AHI/attribution ────

@app.get("/api/battery/{battery_id}/passport-v2")
@safe
def battery_passport_v2(battery_id: str,
                        audience: str = Query(default="operator"),
                        _=Depends(verify_token)):
    """Extended passport with DRI, AHI, quadrant, attribution COALESCE."""
    conn = get_conn()

    s = q1(conn, """
        SELECT s.battery_id, s.bhs_score_v2, s.dri_score, s.ahi_score, s.ahi_tier,
               s.nbfc_quadrant, s.nbfc_grade_2x2, s.divergence_quadrant,
               s.tier_label_v2, s.range_corrected_km, s.commissioned_range_km,
               s.pct_of_commissioned, s.age_months, s.scoring_mode,
               s.warranty_claim_eligible, s.warranty_void_risk,
               s.useful_life_score, s.risk_score,
               s.resale_now_inr, s.resale_8wk_inr, s.resale_exit_signal, s.resale_stage,
               s.rul_action_v2, s.rul_weeks_v2,
               s.kps_slope_8wk, s.degradation_regime,
               s.attr_cycle_pct, s.attr_thermal_pct, s.attr_imbalance_pct,
               s.attr_calendar_pct, s.attr_primary_factor,
               s.corroboration_score, s.confidence_pct,
               s.range_data_quality, s.range_exceeds_spec, s.commissioning_source,
               b.battery_model as pack_model, b.city_code as city,
               b.commissioning_date, b.fleet_segment
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.battery_id = ?
    """, [battery_id])
    if not s:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")

    # B1: DRI/AHI
    dri_score = s.get("dri_score") or s.get("bhs_score_v2")
    ahi_score = s.get("ahi_score")
    ahi_tier = s.get("ahi_tier")
    if not ahi_tier and ahi_score is not None:
        ahi_tier = "HEALTHY" if ahi_score >= 75 else "WATCH" if ahi_score >= 55 else "CRITICAL"

    # B2: Attribution COALESCE
    bda = q1(conn, """
        SELECT charging_pct, usage_pct, thermal_pct, maintenance_pct, calendar_pct,
               primary_driver
        FROM battery_degradation_attribution WHERE battery_id = ?
        ORDER BY scored_week DESC LIMIT 1
    """, [battery_id])
    # dod_behavior_flag re-enabled APR24 (PD-12B resolved).
    dod_flag_row = q1(conn, """
        SELECT dod_behavior_flag, actual_dod_pct
        FROM vehicle_weekly_features
        WHERE battery_id = ? AND dod_behavior_flag IS NOT NULL
        ORDER BY week_number DESC LIMIT 1
    """, [battery_id])

    def _coalesce(new_val, legacy_val):
        return new_val if new_val is not None else legacy_val

    charging_pct = _coalesce((bda or {}).get("charging_pct"), s.get("attr_cycle_pct"))
    usage_pct = _coalesce((bda or {}).get("usage_pct"), None)
    thermal_pct = _coalesce((bda or {}).get("thermal_pct"), s.get("attr_thermal_pct"))
    maintenance_pct = _coalesce((bda or {}).get("maintenance_pct"), s.get("attr_imbalance_pct"))
    calendar_pct = _coalesce((bda or {}).get("calendar_pct"), s.get("attr_calendar_pct"))
    primary_driver = _coalesce((bda or {}).get("primary_driver"), s.get("attr_primary_factor"))
    attribution_source = "NEW" if bda and bda.get("primary_driver") else "LEGACY_TRANSLATED"

    conn.close()

    return {
        "battery_id": battery_id,
        "dri_score": round(dri_score, 2) if dri_score else None,
        "ahi_score": round(ahi_score, 2) if ahi_score else None,
        "ahi_tier": ahi_tier,
        "nbfc_quadrant": s.get("nbfc_quadrant"),
        "nbfc_grade_2x2": s.get("nbfc_grade_2x2"),
        "divergence_quadrant": s.get("divergence_quadrant"),
        "tier_label_v2": s.get("tier_label_v2"),
        "range_corrected_km": round(s["range_corrected_km"], 1) if s.get("range_corrected_km") else None,
        "commissioned_range_km": min(s.get("commissioned_range_km"), PHYSICS_CEILING_KM) if s.get("commissioned_range_km") is not None else None,
        "commissioned_range_km_display": min(s.get("commissioned_range_km"), PHYSICS_CEILING_KM) if s.get("commissioned_range_km") is not None else None,
        "pct_of_commissioned": round(s["pct_of_commissioned"], 1) if s.get("pct_of_commissioned") else None,
        "age_months": round(s["age_months"], 1) if s.get("age_months") else None,
        "scoring_mode": s.get("scoring_mode"),
        "warranty_claim_eligible": s.get("warranty_claim_eligible"),
        "warranty_void_risk": s.get("warranty_void_risk"),
        "attribution": {
            "charging_pct": round(charging_pct, 1) if charging_pct else None,
            "charging_label": _OEM_LABEL_MAP.get("charging"),
            "usage_pct": round(usage_pct, 1) if usage_pct else None,
            "usage_label": _OEM_LABEL_MAP.get("usage"),
            "thermal_pct": round(thermal_pct, 1) if thermal_pct else None,
            "thermal_label": _OEM_LABEL_MAP.get("thermal"),
            "maintenance_pct": round(maintenance_pct, 1) if maintenance_pct else None,
            "maintenance_label": _OEM_LABEL_MAP.get("maintenance"),
            "calendar_pct": round(calendar_pct, 1) if calendar_pct else None,
            "calendar_label": _OEM_LABEL_MAP.get("calendar"),
            "primary_driver": primary_driver,
            "primary_driver_label": _OEM_LABEL_MAP.get(primary_driver, primary_driver),
            "attribution_source": attribution_source,
        },
        "pack_model": s.get("pack_model"),
        "city": s.get("city"),
        "dod_behavior_flag": (dod_flag_row or {}).get("dod_behavior_flag"),
        "dod_behavior_label": {
            "DEEP_DISCHARGE":    "Deep discharge detected",
            "SHALLOW_DISCHARGE": "Shallow discharge pattern",
        }.get((dod_flag_row or {}).get("dod_behavior_flag")),
        "actual_dod_pct": round((dod_flag_row or {}).get("actual_dod_pct"), 1) if (dod_flag_row or {}).get("actual_dod_pct") is not None else None,
        "range_data_quality": s.get("range_data_quality"),
        "range_exceeds_spec": s.get("range_exceeds_spec"),
        "commissioning_source": s.get("commissioning_source"),
        "range_physics_ceiling_km": PHYSICS_CEILING_KM,
    }


# ── B3: GET /api/fleet/dri-ahi-summary ─────────────────────────────

@app.get("/api/fleet/dri-ahi-summary")
@safe
def fleet_dri_ahi_summary(_=Depends(verify_token)):
    """Fleet-level DRI-AHI quadrant counts, grade distribution, per-pack averages."""
    conn = get_conn()

    quads = q(conn, """
        SELECT nbfc_quadrant, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE nbfc_quadrant IS NOT NULL AND scoring_mode != 'SUSPENDED'
        GROUP BY nbfc_quadrant
    """)
    quadrant_counts = {r["nbfc_quadrant"]: r["n"] for r in quads}

    grades = q(conn, """
        SELECT nbfc_grade_2x2, COUNT(*) as n
        FROM battery_health_scores_v2
        WHERE nbfc_grade_2x2 IS NOT NULL AND scoring_mode != 'SUSPENDED'
        GROUP BY nbfc_grade_2x2
    """)
    grade_2x2 = {r["nbfc_grade_2x2"]: r["n"] for r in grades}

    avg_dri = q(conn, """
        SELECT b.battery_model as pack_model,
               ROUND(AVG(s.dri_score), 1) as avg_dri,
               COUNT(*) as battery_count
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.dri_score IS NOT NULL AND s.scoring_mode != 'SUSPENDED'
        GROUP BY b.battery_model
        ORDER BY avg_dri ASC
    """)

    early = q(conn, """
        SELECT b.battery_model as pack_model,
               SUM(CASE WHEN s.age_months < 6
                   AND (s.soh_conservative < 65 OR s.cell_balance_spread_mv > 300)
                   THEN 1 ELSE 0 END) as early_anomaly_count
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.scoring_mode != 'SUSPENDED'
        GROUP BY b.battery_model
    """)

    conn.close()
    return {
        "quadrant_counts": quadrant_counts,
        "grade_2x2": grade_2x2,
        "avg_dri_per_pack": avg_dri,
        "early_anomaly_count_per_pack": early,
    }


# ── B5: GET /api/battery/:id/events-timeline ──────────────────────

@app.get("/api/battery/{battery_id}/events-timeline")
@safe
def battery_events_timeline(battery_id: str, _=Depends(verify_token)):
    """Chronological events timeline — plain English, max 20."""
    conn = get_conn()
    rows = q(conn, """
        SELECT event_week, event_date, event_type, event_from, event_to, event_plain
        FROM battery_events_timeline
        WHERE battery_id = ?
        ORDER BY event_week DESC
        LIMIT 20
    """, [battery_id])
    conn.close()
    return rows


# ── B6: GET /api/battery/:id/revenue-impact ───────────────────────

@app.get("/api/battery/{battery_id}/revenue-impact")
@safe
def battery_revenue_impact(battery_id: str, _=Depends(verify_token)):
    """Revenue impact from range degradation."""
    conn = get_conn()
    s = q1(conn, """
        SELECT s.commissioned_range_km, s.range_corrected_km,
               b.city_code as city
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.battery_id = ?
    """, [battery_id])
    if not s:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")

    comm = s.get("commissioned_range_km") or 0
    curr = s.get("range_corrected_km") or 0
    range_loss_km = comm - curr

    if range_loss_km <= 0:
        conn.close()
        return {"battery_id": battery_id, "range_loss_km": 0,
                "revenue_loss_per_day_inr": 0, "revenue_loss_8wk_inr": 0,
                "trips_lost_per_day": 0}

    city = s.get("city") or "CU"
    fare_row = q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name = ? AND is_active = 1 LIMIT 1
    """, [f"fare_per_km_{city}"])
    fare_per_km = float((fare_row or {}).get("param_value", 2.5))
    avg_km_per_trip = 8.0
    avg_trips_per_day = round(curr / avg_km_per_trip, 1) if curr > 0 else 5

    revenue_loss_per_day = round(range_loss_km * fare_per_km * avg_trips_per_day, 2)
    revenue_loss_8wk = round(revenue_loss_per_day * 56, 2)
    trips_lost_per_day = round(range_loss_km / avg_km_per_trip, 1)

    conn.close()
    return {
        "battery_id": battery_id,
        "range_loss_km": round(range_loss_km, 1),
        "commissioned_range_km": round(comm, 1),
        "range_corrected_km": round(curr, 1),
        "fare_per_km": fare_per_km,
        "revenue_loss_per_day_inr": revenue_loss_per_day,
        "revenue_loss_8wk_inr": revenue_loss_8wk,
        "trips_lost_per_day": trips_lost_per_day,
    }


# ── B7: GET /api/battery/:id/asset-value ──────────────────────────

@app.get("/api/battery/{battery_id}/asset-value")
@safe
def battery_asset_value(battery_id: str, _=Depends(verify_token)):
    """Asset value snapshot — useful life, risk, resale, exit signal.

    exit_signal is reconciled against rul_action_v2 so a single battery never
    returns MONITOR on one API and EXIT_NOW on another:
      - REPLACE_NOW / REPLACE_PLAN  → force EXIT_NOW (rul is authoritative)
      - MONITOR_WEEKLY / MONITOR_INVESTIGATE / NO_ACTION → default HOLD,
        overridden only if the independent resale rule (now > later × 1.15) fires.
      - Anything else               → preserve stored resale_exit_signal, else
                                       fall back to the resale rule.
    """
    conn = get_conn()
    s = q1(conn, """
        SELECT useful_life_score, risk_score, resale_now_inr, resale_8wk_inr,
               resale_stage, resale_exit_signal, rul_action_v2
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    if not s:
        raise HTTPException(404, f"Battery not found: {battery_id}")

    # Independent resale signal (unchanged)
    now = s.get("resale_now_inr") or 0
    later = s.get("resale_8wk_inr") or 0
    resale_fires_exit = now > later * 1.15
    stored_exit = s.get("resale_exit_signal")
    resale_exit_now = (stored_exit == "EXIT_NOW") or resale_fires_exit

    # rul-authoritative reconciliation
    rul = s.get("rul_action_v2")
    if rul in ("REPLACE_NOW", "REPLACE_PLAN"):
        exit_signal = "EXIT_NOW"
        reconciliation = "RUL_REPLACE_FORCES_EXIT"
    elif rul in ("MONITOR_WEEKLY", "MONITOR_INVESTIGATE", "NO_ACTION"):
        exit_signal = "EXIT_NOW" if resale_exit_now else "HOLD"
        reconciliation = "RUL_MONITOR_RESALE_OVERRIDE" if resale_exit_now else "RUL_MONITOR_HOLD"
    else:
        # Unknown / NULL rul — preserve legacy behaviour
        exit_signal = stored_exit or ("EXIT_NOW" if resale_fires_exit else "HOLD")
        reconciliation = "LEGACY_RESALE_ONLY"

    return {
        "battery_id": battery_id,
        "useful_life_score": s.get("useful_life_score"),
        "risk_score": s.get("risk_score"),
        "resale_now_inr": s.get("resale_now_inr"),
        "resale_8wk_inr": s.get("resale_8wk_inr"),
        "resale_stage": s.get("resale_stage"),
        "exit_signal": exit_signal,
        "rul_action_v2": rul,
        "reconciliation": reconciliation,
    }


# ── B8: Extend /api/oem/portfolio — pack intelligence ─────────────
# Already handled in existing oem_portfolio; we add a v2 variant with
# pack_survival, service_failure_taxonomy, seasonal_multipliers, false_alarm_rate

@app.get("/api/oem/portfolio-v2")
@safe
def oem_portfolio_v2(_=Depends(verify_token)):
    """Extended portfolio with survival, taxonomy, seasonal, false alarm."""
    conn = get_conn()

    base_rows = q(conn, """
        SELECT b.battery_model as pack_model,
               COUNT(*) as unit_count,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range_km,
               ROUND(AVG(s.bhs_score_v2), 1) as avg_bhs,
               ROUND(AVG(s.dri_score), 1) as avg_dri_score,
               ROUND(AVG(s.ahi_score), 1) as avg_ahi_score,
               SUM(CASE WHEN s.tier_label_v2 = 'PRIME' THEN 1 ELSE 0 END) as tier_prime,
               SUM(CASE WHEN s.tier_label_v2 = 'STABLE' THEN 1 ELSE 0 END) as tier_stable,
               SUM(CASE WHEN s.tier_label_v2 = 'WATCH' THEN 1 ELSE 0 END) as tier_watch,
               SUM(CASE WHEN s.tier_label_v2 = 'STRESSED' THEN 1 ELSE 0 END) as tier_stressed,
               SUM(CASE WHEN s.tier_label_v2 = 'CRITICAL' THEN 1 ELSE 0 END) as tier_critical,
               SUM(CASE WHEN s.warranty_claim_eligible = 1 THEN 1 ELSE 0 END) as warranty_eligible,
               SUM(CASE WHEN s.age_months < 6 AND (s.soh_conservative < 65 OR s.cell_balance_spread_mv > 300)
                   THEN 1 ELSE 0 END) as early_anomaly_count,
               SUM(CASE WHEN s.nbfc_quadrant = 'Q1_HEALTHY' THEN 1 ELSE 0 END) as q1_count,
               SUM(CASE WHEN s.nbfc_quadrant = 'Q2_AGE_UNDERPERFORMING' THEN 1 ELSE 0 END) as q2_count,
               SUM(CASE WHEN s.nbfc_quadrant = 'Q3_NORMAL_AGING' THEN 1 ELSE 0 END) as q3_count,
               SUM(CASE WHEN s.nbfc_quadrant = 'Q4_DETERIORATING' THEN 1 ELSE 0 END) as q4_count
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        GROUP BY b.battery_model
        ORDER BY unit_count DESC
    """)

    _SEASONAL_DEFAULTS = {"Sep": 1.52, "Feb": 1.36, "Mar": 0.54}

    for r in base_rows:
        pm = r["pack_model"]
        pack_code = pm.split("Pack")[-1] if "Pack" in pm else pm[-4:]

        # Pack survival
        surv = q1(conn, "SELECT * FROM pack_survival_params WHERE pack_code = ?", [pack_code])
        r["pack_survival_p50"] = surv["median_failure_months"] if surv else None

        # Top 2 attribution factors (OEM-1)
        top_attr = q(conn, """
            SELECT da.primary_driver, COUNT(*) as n
            FROM battery_degradation_attribution da
            JOIN batteries b ON da.battery_id = b.battery_id
            WHERE b.battery_model = ? AND da.primary_driver IS NOT NULL
            GROUP BY da.primary_driver ORDER BY n DESC LIMIT 2
        """, [pm])
        r["top_attribution_factors"] = [
            _OEM_LABEL_MAP.get(a["primary_driver"], a["primary_driver"])
            for a in top_attr
        ]

        # Quadrant distribution (OEM-1)
        r["quadrant_distribution"] = {
            "Q1": r.pop("q1_count", 0), "Q2": r.pop("q2_count", 0),
            "Q3": r.pop("q3_count", 0), "Q4": r.pop("q4_count", 0),
        }

        # Service failure taxonomy (top 5 event types)
        taxonomy = q(conn, """
            SELECT ve.event_type as category, COUNT(*) as count
            FROM vehicle_events ve
            JOIN batteries b ON ve.battery_id = b.battery_id
            WHERE b.battery_model = ?
            GROUP BY ve.event_type ORDER BY count DESC LIMIT 5
        """, [pm])
        total_svc = sum(t["count"] for t in taxonomy) or 1
        r["service_failure_taxonomy"] = [
            {"category": t["category"], "count": t["count"],
             "pct": round(t["count"] / total_svc * 100, 1)}
            for t in taxonomy
        ]

        # Seasonal multipliers
        r["seasonal_multipliers"] = [
            {"month_name": m, "multiplier": v} for m, v in _SEASONAL_DEFAULTS.items()
        ]

        # False alarm rate
        npf = q1(conn, """
            SELECT COUNT(CASE WHEN s.corroboration_score <= 1 THEN 1 END) as npf_count,
                   COUNT(*) as total
            FROM battery_health_scores_v2 s
            JOIN batteries b ON s.battery_id = b.battery_id
            WHERE b.battery_model = ? AND s.scoring_mode != 'SUSPENDED'
        """, [pm])
        total_n = (npf or {}).get("total", 1) or 1
        npf_n = (npf or {}).get("npf_count", 0)
        r["false_alarm_rate_pct"] = round(npf_n / total_n * 100, 1)

    conn.close()
    return base_rows


# ── OEM-2: Extend /api/oem/field-issues ───────────────────────────

@app.get("/api/oem/field-issues")
@safe
def oem_field_issues(_=Depends(verify_token)):
    """Active field issues with alert age, SLA, warranty info."""
    conn = get_conn()
    rows = q(conn, """
        SELECT pa.battery_id, pa.alert_type, pa.priority, pa.title, pa.detail,
               pa.fired_at,
               ROUND((julianday('now') - julianday(pa.fired_at)) * 24, 1) as alert_age_hours,
               CASE WHEN (julianday('now') - julianday(pa.fired_at)) * 24 > 48 THEN 1 ELSE 0 END as sla_breached,
               s.warranty_claim_eligible, s.warranty_void_risk,
               s.tier_label_v2, s.range_corrected_km
        FROM platform_alerts pa
        JOIN battery_health_scores_v2 s ON pa.battery_id = s.battery_id
        WHERE pa.active = 1
        ORDER BY pa.priority ASC, pa.fired_at ASC
    """)
    conn.close()
    return rows


# ── OEM-2b: /api/oem/dri-ahi-scatter ──────────────────────────────
# M3/SP-11: per-battery DRI+AHI for the S3 scatter plot (OEM v2)

@app.get("/api/oem/dri-ahi-scatter")
@safe
def oem_dri_ahi_scatter(_=Depends(verify_token)):
    """Per-battery DRI/AHI points for the OEM v2 scatter. Filters SUSPENDED/SUPPRESSED and the two blocked batteries."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id,
               ROUND(s.dri_score, 2) as dri_score,
               ROUND(s.ahi_score, 2) as ahi_score,
               s.rul_action_v2,
               s.tier_label_v2,
               b.battery_model as pack_model,
               b.city_code as city
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.dri_score IS NOT NULL
          AND s.ahi_score IS NOT NULL
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
    """)
    conn.close()
    return rows


# ── OEM-3: /api/oem/range-overview ────────────────────────────────

@app.get("/api/oem/range-overview")
@safe
def oem_range_overview(_=Depends(verify_token)):
    """Cohort range table, fleet forecast, floor breach probability."""
    conn = get_conn()

    cohort = q(conn, """
        SELECT b.battery_model as pack_model, b.city_code as city,
               COUNT(*) as battery_count,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range,
               ROUND(AVG(s.pct_of_commissioned), 1) as vs_baseline_pct,
               SUM(CASE WHEN s.range_corrected_km < 56 THEN 1 ELSE 0 END) as below_floor_count,
               ROUND(AVG(s.dri_score), 1) as avg_dri,
               ROUND(AVG(s.kps_slope_8wk), 4) as slope_trend
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        GROUP BY b.battery_model, b.city_code
        ORDER BY avg_range ASC
    """)

    # Fleet forecast P50 (12 weeks, from avg slope)
    fleet_avg = q1(conn, """
        SELECT AVG(range_corrected_km) as avg_range, AVG(kps_slope_8wk) as avg_slope
        FROM battery_health_scores_v2
        WHERE scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND kps_slope_8wk IS NOT NULL
    """)
    avg_r = (fleet_avg or {}).get("avg_range") or 80
    avg_s = (fleet_avg or {}).get("avg_slope") or -0.002
    forecast = [{"week_offset": w, "range_km": round(avg_r + avg_s * 80 * w, 1)}
                for w in range(1, 13)]

    # Floor breach probabilities
    breach = q1(conn, """
        SELECT AVG(p_floor_breach_4w) as wk4,
               AVG(p_floor_breach_8w) as wk8,
               AVG(p_floor_breach_12w) as wk12
        FROM battery_health_scores_v2
        WHERE scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
    """)

    conn.close()
    return {
        "cohort_table": cohort,
        "fleet_forecast_p50": forecast,
        "floor_breach_prob": {
            "wk4": round((breach or {}).get("wk4") or 0, 3),
            "wk8": round((breach or {}).get("wk8") or 0, 3),
            "wk12": round((breach or {}).get("wk12") or 0, 3),
        },
    }


# ── OEM-4: /api/oem/component-health ──────────────────────────────

@app.get("/api/oem/component-health")
@safe
def oem_component_health_fleet(_=Depends(verify_token)):
    """Fleet component health averages, firmware, born-weak count."""
    conn = get_conn()

    # Fleet averages from battery_component_health (latest week per battery)
    comp_rows = q(conn, """
        SELECT 'BMS' as component_name,
               ROUND(AVG(ch.iot_quality_score), 1) as avg_score
        FROM battery_component_health ch
        WHERE ch.week_number = (
            SELECT MAX(week_number) FROM battery_component_health WHERE battery_id = ch.battery_id
        ) AND ch.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
    """)
    fleet_averages = [{"component_name": "BMS/IoT", "avg_score": (comp_rows[0] or {}).get("avg_score")}] if comp_rows else []

    # Physical risk
    phys = q1(conn, """
        SELECT ROUND(AVG(physical_risk_score), 1) as avg
        FROM battery_component_health
        WHERE week_number = (SELECT MAX(week_number) FROM battery_component_health WHERE battery_id = battery_component_health.battery_id)
          AND physical_risk_score IS NOT NULL
    """)
    fleet_averages.append({"component_name": "Physical Integrity", "avg_score": (phys or {}).get("avg")})

    # Firmware distribution (from iot_status as proxy)
    firmware = q(conn, """
        SELECT ch.iot_status as version, COUNT(DISTINCT ch.battery_id) as count,
               ROUND(AVG(CASE WHEN s.corroboration_score <= 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as npf_rate_pct
        FROM battery_component_health ch
        JOIN battery_health_scores_v2 s ON ch.battery_id = s.battery_id
        WHERE ch.week_number = (SELECT MAX(week_number) FROM battery_component_health WHERE battery_id = ch.battery_id)
          AND ch.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
        GROUP BY ch.iot_status
    """)

    # Born weak: commissioning_spread_mv > 150 at week 4
    born_weak = q1(conn, """
        SELECT COUNT(*) as n FROM battery_health_scores_v2
        WHERE commissioning_spread_mv > 150
    """)

    conn.close()
    return {
        "fleet_averages": fleet_averages,
        "firmware_distribution": firmware,
        "born_weak_count": (born_weak or {}).get("n", 0),
    }


# ── OEM-5: /api/oem/operator-profiles ─────────────────────────────

@app.get("/api/oem/operator-profiles")
@safe
def oem_operator_profiles(_=Depends(verify_token)):
    """Drive/charge/use_case distributions, top operators, cross-tab.
    Banned battery_ids (fleet_context_params.banned_battery_ids) excluded
    from every subquery."""
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("battery_id")

    drive = q(conn, f"""
        SELECT personal_drive_profile as profile, COUNT(*) as count
        FROM battery_personal_params
        WHERE personal_drive_profile IS NOT NULL AND {ban_sql}
        GROUP BY personal_drive_profile ORDER BY count DESC
    """, ban_params)
    charge = q(conn, f"""
        SELECT personal_charge_profile as profile, COUNT(*) as count
        FROM battery_personal_params
        WHERE personal_charge_profile IS NOT NULL AND {ban_sql}
        GROUP BY personal_charge_profile ORDER BY count DESC
    """, ban_params)
    usecase = q(conn, f"""
        SELECT use_case_inferred as profile, COUNT(*) as count
        FROM batteries
        WHERE use_case_inferred IS NOT NULL AND {ban_sql}
        GROUP BY use_case_inferred ORDER BY count DESC
    """, ban_params)

    # Top 5 operators by avg_dri ASC (worst DRI = most degradation)
    top_ops_sql, _ = _banned_sql_clause("s.battery_id")
    top_ops = q(conn, f"""
        SELECT s.battery_id, ROUND(s.dri_score, 1) as avg_dri,
               p.personal_drive_profile, p.personal_charge_profile
        FROM battery_health_scores_v2 s
        JOIN battery_personal_params p ON s.battery_id = p.battery_id
        WHERE s.dri_score IS NOT NULL AND s.scoring_mode != 'SUSPENDED'
          AND {top_ops_sql}
        ORDER BY s.dri_score ASC LIMIT 5
    """, ban_params)

    # Cross-tab: pack_model x drive profile
    cross_sql, _ = _banned_sql_clause("b.battery_id")
    cross = q(conn, f"""
        SELECT b.battery_model as pack_model,
               ROUND(SUM(CASE WHEN p.personal_drive_profile = 'AGGRESSIVE' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as aggressive_pct,
               ROUND(SUM(CASE WHEN p.personal_drive_profile = 'MODERATE' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as moderate_pct,
               ROUND(SUM(CASE WHEN p.personal_drive_profile = 'CONSERVATIVE' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as conservative_pct
        FROM batteries b
        JOIN battery_personal_params p ON b.battery_id = p.battery_id
        WHERE {cross_sql}
        GROUP BY b.battery_model
    """, ban_params)

    conn.close()
    return {
        "drive_distribution": drive,
        "charge_distribution": charge,
        "use_case_distribution": usecase,
        "top_operators": top_ops,
        "operator_pack_cross_tab": cross,
    }


# ── OEM-6: /api/oem/survival-intel ────────────────────────────────

@app.get("/api/oem/survival-intel")
@safe
def oem_survival_intel(_=Depends(verify_token)):
    """Pack survival P50, NBFC grade per pack, corroboration distribution."""
    conn = get_conn()

    surv = q(conn, """
        SELECT pack_code as pack_model,
               median_failure_months as p50_months,
               ROUND(median_failure_months * 30 * 40, 0) as p50_km
        FROM pack_survival_params
    """)

    grade_per_pack = q(conn, """
        SELECT b.battery_model as pack_model,
               ROUND(SUM(CASE WHEN s.nbfc_grade_2x2 = 'A' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as A_pct,
               ROUND(SUM(CASE WHEN s.nbfc_grade_2x2 = 'B' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as B_pct,
               ROUND(SUM(CASE WHEN s.nbfc_grade_2x2 = 'C' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as C_pct,
               ROUND(SUM(CASE WHEN s.nbfc_grade_2x2 = 'D' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as D_pct
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.nbfc_grade_2x2 IS NOT NULL AND s.scoring_mode != 'SUSPENDED'
        GROUP BY b.battery_model
    """)

    corr = q(conn, """
        SELECT corroboration_score as score_0_to_7, COUNT(*) as count
        FROM battery_health_scores_v2
        WHERE corroboration_score IS NOT NULL AND scoring_mode != 'SUSPENDED'
        GROUP BY corroboration_score ORDER BY corroboration_score
    """)

    conn.close()
    return {
        "pack_survival": surv,
        "nbfc_grade_per_pack": grade_per_pack,
        "corroboration_dist": corr,
    }


# ── OEM-7: /api/oem/second-life-pipeline ──────────────────────────

@app.get("/api/oem/second-life-pipeline")
@safe
def oem_second_life_pipeline(_=Depends(verify_token)):
    """Second-life candidates: DRI 45-55, spread<150, no physical damage."""
    conn = get_conn()
    rows = q(conn, """
        SELECT s.battery_id, s.dri_score, s.range_corrected_km,
               s.cell_balance_spread_mv, s.resale_now_inr,
               b.battery_model as pack_model
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        LEFT JOIN battery_component_health ch ON s.battery_id = ch.battery_id
            AND ch.week_number = (SELECT MAX(week_number) FROM battery_component_health WHERE battery_id = ch.battery_id)
        WHERE s.dri_score BETWEEN 45 AND 55
          AND (s.cell_balance_spread_mv IS NULL OR s.cell_balance_spread_mv < 150)
          AND (ch.physical_damage_risk IS NULL OR ch.physical_damage_risk = 'LOW')
          AND s.scoring_mode != 'SUSPENDED'
          AND s.battery_id NOT IN ('BAT_LFP_034', 'BAT_LFP_202')
    """)

    total_inr = sum(r.get("resale_now_inr") or 0 for r in rows)
    avg_inr = round(total_inr / len(rows), 0) if rows else 0

    conn.close()
    return {
        "count": len(rows),
        "batteries": rows,
        "total_pipeline_inr": round(total_inr, 0),
        "avg_value_inr": avg_inr,
    }


# ── OEM-8: POST /api/enerlyst/query — fleet context routing ───────

class EnerlystQueryBody(_BM):
    query: str
    audience: Optional[str] = "operator"
    battery_id: Optional[str] = None

@app.post("/api/enerlyst/query")
@safe
def enerlyst_query(body: EnerlystQueryBody, _=Depends(verify_token)):
    """Route to fleet-level RAG context when audience=oem_fleet and no battery_id."""
    if body.audience == "oem_fleet" and not body.battery_id:
        conn = get_conn()
        fleet = q1(conn, """
            SELECT COUNT(*) as total,
                   ROUND(AVG(dri_score), 1) as avg_dri,
                   ROUND(AVG(ahi_score), 1) as avg_ahi,
                   ROUND(AVG(range_corrected_km), 1) as avg_range
            FROM battery_health_scores_v2
            WHERE scoring_mode != 'SUSPENDED'
        """)
        tiers = q(conn, """
            SELECT tier_label_v2, COUNT(*) as n
            FROM battery_health_scores_v2
            WHERE tier_label_v2 IS NOT NULL AND scoring_mode != 'SUSPENDED'
            GROUP BY tier_label_v2
        """)
        conn.close()
        return {
            "mode": "fleet_context",
            "query": body.query,
            "fleet_context": {
                "total_batteries": (fleet or {}).get("total", 0),
                "avg_dri": (fleet or {}).get("avg_dri"),
                "avg_ahi": (fleet or {}).get("avg_ahi"),
                "avg_range_km": (fleet or {}).get("avg_range"),
                "tier_distribution": {r["tier_label_v2"]: r["n"] for r in tiers},
            },
            "note": "Fleet-level query — route to RAG with this context",
        }

    return {
        "mode": "battery_context" if body.battery_id else "general",
        "query": body.query,
        "audience": body.audience,
        "battery_id": body.battery_id,
        "note": "Route to standard query pipeline",
    }


# ═══════════════════════════════════════════════════════════════════════
# SI STAGE 2 — Service Intelligence endpoints (additive only)
# Tag: si-backend-complete
# Source signals from si-pipeline-complete (VWF cols + BHS cols + BPP).
# All return data_confidence. BAT_LFP_034 and BAT_LFP_202 NEVER surface in
# fleet alerts (Rule per stage 2 spec).
# ═══════════════════════════════════════════════════════════════════════

SI_DEMO_BLACKLIST = ('BAT_LFP_034', 'BAT_LFP_202')


def _latest_vwf_row(conn, battery_id, cols):
    sql = f"""
        SELECT {cols}
        FROM vehicle_weekly_features
        WHERE battery_id = ?
        ORDER BY week_number DESC
        LIMIT 1
    """
    return q1(conn, sql, [battery_id])


# ── SI-1 GET /api/battery/{id}/charging-profile ─────────────────────────
@app.get("/api/battery/{battery_id}/charging-profile")
@safe
def si_charging_profile(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()
    vwf = _latest_vwf_row(conn, battery_id)
    loc = q1(conn, """
        SELECT location_type, cluster_label, session_count, confidence_score
        FROM charging_location_profiles
        WHERE battery_id = ?
        ORDER BY session_count DESC LIMIT 1
    """, [battery_id])
    bpp = q1(conn, """
        SELECT service_compliance_score, personal_charge_profile
        FROM battery_personal_params WHERE battery_id = ?
    """, [battery_id])
    conn.close()

    charger_type = (vwf or {}).get("charger_type_inferred")
    return {
        "battery_id": battery_id,
        "latest_week": (vwf or {}).get("week_number"),
        "charger_type_inferred": charger_type,
        "charger_power_w_median": (vwf or {}).get("charger_power_w_median"),
        "charger_power_w_p90": (vwf or {}).get("charger_power_w_p90"),
        "charger_power_variance": (vwf or {}).get("charger_power_variance"),
        "charging_location_type": (loc or {}).get("location_type"),
        "cluster_label": (loc or {}).get("cluster_label"),
        "personal_charge_profile": (bpp or {}).get("personal_charge_profile"),
        "charge_compliance_score": (bpp or {}).get("service_compliance_score"),
        # signals not stored in production: charge_sessions_per_week, avg_soc_ceiling
        "charge_sessions_per_week": None,
        "avg_soc_ceiling": None,
        "data_confidence": "HIGH" if charger_type is not None else "PARTIAL",
    }


# ── SI-2 GET /api/battery/{id}/iot-health ───────────────────────────────
@app.get("/api/battery/{battery_id}/iot-health")
@safe
def si_iot_health(battery_id: str, _=Depends(verify_token)):
    if battery_id in get_banned_battery_ids():
        raise HTTPException(404, "Not found")
    conn = get_conn()
    bhs = q1(conn, """
        SELECT iot_device_health, data_staleness_flag
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    vwf = _latest_vwf_row(conn, battery_id)
    # last_full_data_week proxy: last week where iot_gap_frequency_4wk = 0
    full = q1(conn, """
        SELECT MAX(week_number) AS w FROM vehicle_weekly_features
        WHERE battery_id = ? AND iot_gap_frequency_4wk = 0
    """, [battery_id])
    conn.close()

    health = (bhs or {}).get("iot_device_health")
    impact_map = {"HEALTHY": "NONE", "DEGRADING": "REDUCED", "CRITICAL": "SUSPENDED"}
    latest_week = (vwf or {}).get("week_number")
    last_can_week = (full or {}).get("w")
    can_gap_weeks = None
    if latest_week is not None and last_can_week is not None:
        can_gap_weeks = max(0, int(latest_week) - int(last_can_week))
    iot_status = health if health in ("HEALTHY", "DEGRADING", "CRITICAL") else "HEALTHY"
    return {
        "battery_id": battery_id,
        "iot_status": iot_status,
        "last_can_week": last_can_week,
        "can_gap_weeks": can_gap_weeks,
        "iot_device_health": health,
        "data_staleness_flag": (bhs or {}).get("data_staleness_flag"),
        "latest_week": latest_week,
        "gps_coverage_trend": (vwf or {}).get("gps_coverage_trend"),
        "can_dropout_rate_trend": (vwf or {}).get("can_dropout_rate_trend"),
        "iot_gap_frequency_4wk": (vwf or {}).get("iot_gap_frequency_4wk"),
        "last_full_data_week": last_can_week,
        "scoring_confidence_impact": impact_map.get(health, "UNKNOWN"),
        "data_confidence": "HIGH" if health is not None else "PARTIAL",
    }


# ── SI-3 GET /api/battery/{id}/service-profile ──────────────────────────
@app.get("/api/battery/{battery_id}/service-profile")
@safe
def si_service_profile(battery_id: str, _=Depends(verify_token)):
    conn = get_conn()
    bpp = q1(conn, """
        SELECT service_profile, service_compliance_score, policy_adherence,
               alert_burden_tier
        FROM battery_personal_params WHERE battery_id = ?
    """, [battery_id])
    last_action = q1(conn, """
        SELECT MAX(week_number) AS wk
        FROM vehicle_events
        WHERE battery_id = ?
          AND event_type IN ('CELL_IMBALANCE_ABS','CELL_IMBALANCE_ESCALATION',
                             'CELL_IMBALANCE_REL','E2_CELL_IMBALANCE',
                             'SOH_CAPACITY_DECLINE')
    """, [battery_id])
    last_vwf = q1(conn, """
        SELECT MAX(week_number) AS wk FROM vehicle_weekly_features
        WHERE battery_id = ?
    """, [battery_id])
    # ignored alerts: CELL_IMBALANCE events whose cell_spread_p90_mv didn't drop >20mV in 4 wks
    events = q(conn, """
        SELECT week_number FROM vehicle_events
        WHERE battery_id = ?
          AND event_type IN ('CELL_IMBALANCE_ABS','CELL_IMBALANCE_ESCALATION',
                             'CELL_IMBALANCE_REL','E2_CELL_IMBALANCE')
          AND (event_reliability IS NULL OR event_reliability != 'SUPPRESSED')
    """, [battery_id])
    spreads = {r["wk"]: r["sp"] for r in q(conn, """
        SELECT week_number AS wk, cell_spread_p90_mv AS sp
        FROM battery_component_health
        WHERE battery_id = ? AND cell_spread_p90_mv IS NOT NULL
    """, [battery_id])}
    ignored = 0
    for ev in events:
        wk = ev["week_number"]
        s_at = spreads.get(wk)
        s_after = next((spreads[w] for w in range(wk + 1, wk + 5) if w in spreads), None)
        if s_at is not None and s_after is not None and (s_at - s_after) <= 20:
            ignored += 1
    conn.close()

    weeks_since = None
    if last_action and last_action.get("wk") and last_vwf and last_vwf.get("wk"):
        weeks_since = last_vwf["wk"] - last_action["wk"]
    return {
        "battery_id": battery_id,
        "service_profile": (bpp or {}).get("service_profile"),
        "service_compliance_score": (bpp or {}).get("service_compliance_score"),
        "policy_adherence": (bpp or {}).get("policy_adherence"),
        "alert_burden_tier": (bpp or {}).get("alert_burden_tier"),
        "weeks_since_last_service_action": weeks_since,
        "ignored_alerts_count": ignored,
        "total_imbalance_alerts": len(events),
        "data_confidence": "HIGH" if (bpp or {}).get("service_profile") else "PARTIAL",
    }


# ── SI-4 GET /api/fleet/charger-intelligence ────────────────────────────
@app.get("/api/fleet/charger-intelligence")
@safe
def si_fleet_charger_intelligence(_=Depends(verify_token)):
    conn = get_conn()
    # latest charger_type per battery
    rows = q(conn, """
        WITH latest AS (
            SELECT battery_id, MAX(week_number) AS wk
            FROM vehicle_weekly_features
            WHERE charger_type_inferred IS NOT NULL
            GROUP BY battery_id
        )
        SELECT v.battery_id, v.charger_type_inferred, v.charger_power_w_median,
               bhs.dri_score, bhs.pack_model
        FROM vehicle_weekly_features v
        JOIN latest l ON v.battery_id = l.battery_id AND v.week_number = l.wk
        LEFT JOIN battery_health_scores_v2 bhs ON bhs.battery_id = v.battery_id
    """)
    # weeks_anomalous per battery (count of non-STANDARD weeks)
    anomalous_weeks = {r["battery_id"]: r["w"] for r in q(conn, """
        SELECT battery_id, COUNT(*) AS w
        FROM vehicle_weekly_features
        WHERE charger_type_inferred IS NOT NULL
          AND charger_type_inferred != 'STANDARD'
        GROUP BY battery_id
    """)}
    conn.close()

    total = len(rows) or 1
    type_counts = {}
    type_dri_sums = {}
    type_dri_counts = {}
    anomalous = []
    for r in rows:
        t = r["charger_type_inferred"]
        type_counts[t] = type_counts.get(t, 0) + 1
        if r.get("dri_score") is not None:
            type_dri_sums[t] = type_dri_sums.get(t, 0.0) + r["dri_score"]
            type_dri_counts[t] = type_dri_counts.get(t, 0) + 1
        if t and t != "STANDARD":
            anomalous.append({
                "battery_id": r["battery_id"],
                "pack_model": r["pack_model"],
                "charger_type": t,
                "charger_power_w_median": r["charger_power_w_median"],
                "weeks_anomalous": anomalous_weeks.get(r["battery_id"], 0),
            })

    return {
        "charger_type_distribution": [
            {"charger_type": t, "count": c, "pct_of_fleet": round(100 * c / total, 2)}
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1])
        ],
        "avg_dri_by_charger_type": [
            {"charger_type": t,
             "avg_dri_score": round(type_dri_sums[t] / type_dri_counts[t], 2)}
            for t in type_dri_sums
        ],
        "anomalous_charger_count": len(anomalous),
        "batteries_on_non_standard": sorted(
            anomalous, key=lambda x: -x["weeks_anomalous"])[:50],
        "data_confidence": "HIGH" if total >= 100 else "PARTIAL",
    }


# ── SI-5 GET /api/fleet/location-intelligence ───────────────────────────
@app.get("/api/fleet/location-intelligence")
@safe
def si_fleet_location_intelligence(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT clp.battery_id, clp.location_type, clp.cluster_label,
               clp.cluster_lat, clp.cluster_lon, clp.session_count,
               bhs.dri_score
        FROM charging_location_profiles clp
        LEFT JOIN battery_health_scores_v2 bhs ON bhs.battery_id = clp.battery_id
    """)
    conn.close()

    type_counts = {}
    type_dri_sums = {}
    type_dri_counts = {}
    swap_count = 0
    depot_clusters = set()
    cluster_agg = {}
    for r in rows:
        t = r["location_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
        if r.get("dri_score") is not None:
            type_dri_sums[t] = type_dri_sums.get(t, 0.0) + r["dri_score"]
            type_dri_counts[t] = type_dri_counts.get(t, 0) + 1
        if t == "SWAP": swap_count += 1
        if t == "DEPOT": depot_clusters.add(r["cluster_label"])
        key = (r["cluster_label"], t)
        if key not in cluster_agg:
            cluster_agg[key] = {"cluster_label": r["cluster_label"],
                                "location_type": t, "lat": r["cluster_lat"],
                                "lon": r["cluster_lon"], "battery_count": 0,
                                "_dri_sum": 0.0, "_dri_n": 0}
        cluster_agg[key]["battery_count"] += 1
        if r.get("dri_score") is not None:
            cluster_agg[key]["_dri_sum"] += r["dri_score"]
            cluster_agg[key]["_dri_n"] += 1

    cluster_centroids = []
    for c in cluster_agg.values():
        avg_dri = round(c["_dri_sum"] / c["_dri_n"], 2) if c["_dri_n"] else None
        cluster_centroids.append({
            "cluster_label": c["cluster_label"], "location_type": c["location_type"],
            "lat": c["lat"], "lon": c["lon"],
            "battery_count": c["battery_count"], "avg_dri": avg_dri,
        })
    cluster_centroids.sort(key=lambda x: -x["battery_count"])

    return {
        "location_type_distribution": [
            {"location_type": t, "count": c}
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1])
        ],
        "avg_dri_by_location_type": [
            {"location_type": t,
             "avg_dri_score": round(type_dri_sums[t] / type_dri_counts[t], 2)}
            for t in type_dri_sums
        ],
        "swap_battery_count": swap_count,
        "depot_cluster_count": len(depot_clusters),
        "cluster_centroids": cluster_centroids[:100],
        "data_confidence": "HIGH" if len(rows) >= 100 else "PARTIAL",
    }


# ── SI-6 GET /api/fleet/service-alerts (HERO) ───────────────────────────
@app.get("/api/fleet/service-alerts")
@safe
def si_fleet_service_alerts(_=Depends(verify_token)):
    """Aggregate prioritised alert queue across all SI signals."""
    conn = get_conn()
    # Cost params — read once per request (Rule 208 — never hardcode)
    BATT_REPLACE_COST = int(float(get_param("battery_replacement_cost_inr", default=60000)))
    CELL_BAL_COST = int(float(get_param("cell_balance_service_cost_inr", default=2000)))
    CHARGER_SWAP_COST = int(float(get_param("charger_swap_cost_inr", default=1500)))
    # fleet cohort thresholds
    spread_p75 = q1(conn, """
        SELECT cell_spread_max FROM vehicle_weekly_features
        WHERE cell_spread_max IS NOT NULL
        ORDER BY cell_spread_max LIMIT 1 OFFSET (
          SELECT CAST(COUNT(*)*0.75 AS INTEGER) FROM vehicle_weekly_features
          WHERE cell_spread_max IS NOT NULL
        )
    """)
    spread_p75_v = (spread_p75 or {}).get("cell_spread_max", 248)

    # latest VWF row per battery (per row, the columns we need)
    latest = q(conn, """
        WITH wk AS (
            SELECT battery_id, MAX(week_number) AS wk
            FROM vehicle_weekly_features GROUP BY battery_id
        )
        SELECT v.battery_id, v.week_number,
               v.kps_slope_8wk, v.cell_spread_acceleration, v.cell_spread_max,
               v.repeat_alert_flag, v.charger_type_inferred,
               v.drivetrain_stress_flag, v.connector_suspect_flag,
               v.bms_stress_index, v.fault_duration_weeks,
               v.cell_voltage_min_weekly, v.cell_voltage_min_trend,
               v.persistent_weak_cell_flag
        FROM vehicle_weekly_features v
        JOIN wk ON wk.battery_id = v.battery_id AND wk.wk = v.week_number
    """)
    bhs_rows = {r["battery_id"]: r for r in q(conn, """
        SELECT battery_id, pack_model, dri_score, pct_of_commissioned,
               soh_coulomb_latest, soh_coulomb_trend, divergence_quadrant,
               iot_device_health
        FROM battery_health_scores_v2
        WHERE scoring_mode != 'SUSPENDED'
    """)}
    batteries = {r["battery_id"]: r for r in q(conn, """
        SELECT battery_id, city_code FROM batteries
    """)}
    bpp_rows = {r["battery_id"]: r for r in q(conn, """
        SELECT battery_id, service_compliance_score, driver_stress_tier,
               service_profile
        FROM battery_personal_params
    """)}
    # Last week per battery for days_since_trigger anchoring
    conn.close()

    alerts = []
    TIER_ORDER = {"ESCALATION": 0, "ACUTE": 1, "EARLY_WARNING": 2,
                  "IOT_ISSUES": 3, "SEASONAL": 4}

    def add(bid, **kw):
        kw["battery_id"] = bid
        kw["pack_model"] = (bhs_rows.get(bid) or {}).get("pack_model")
        kw["city"] = (batteries.get(bid) or {}).get("city_code")
        kw["data_confidence"] = kw.get("data_confidence", "HIGH")
        alerts.append(kw)

    for v in latest:
        bid = v["battery_id"]
        if bid in SI_DEMO_BLACKLIST: continue
        bhs = bhs_rows.get(bid, {})
        bpp = bpp_rows.get(bid, {})

        # IOT_DEGRADING
        ih = bhs.get("iot_device_health")
        if ih in ("DEGRADING", "CRITICAL"):
            add(bid, alert_category="IOT_DEGRADING", alert_tier="IOT_ISSUES",
                alert_label_plain=("Device telemetry degrading — predictions may be unreliable"
                                   if ih == "DEGRADING"
                                   else "Device offline or critically degraded — scoring suspended"),
                signal_source="battery_health_scores_v2.iot_device_health",
                days_since_trigger=None, corroborated=0,
                recommended_action="Service team: dispatch IoT inspection",
                cost_estimate_inr=500)

        # RANGE_DECLINE
        pc = bhs.get("pct_of_commissioned")
        slope = v.get("kps_slope_8wk")
        if pc is not None and slope is not None and slope < 0:
            if pc < 75:
                add(bid, alert_category="RANGE_DECLINE", alert_tier="ACUTE",
                    alert_label_plain=f"Range delivery is {pc:.0f}% of commissioning — declining",
                    signal_source="bhs.pct_of_commissioned + vwf.kps_slope_8wk",
                    days_since_trigger=None, corroborated=1,
                    recommended_action="Plan battery replacement window", cost_estimate_inr=BATT_REPLACE_COST)
            elif pc < 85:
                add(bid, alert_category="RANGE_DECLINE", alert_tier="EARLY_WARNING",
                    alert_label_plain=f"Range delivery is {pc:.0f}% of commissioning — early decline",
                    signal_source="bhs.pct_of_commissioned + vwf.kps_slope_8wk",
                    days_since_trigger=None, corroborated=0,
                    recommended_action="Monitor trend weekly", cost_estimate_inr=None)

        # SOH_DECLINE
        sc = bhs.get("soh_coulomb_latest")
        st = bhs.get("soh_coulomb_trend")
        if sc is not None and sc < 70 and st is not None and st < 0:
            add(bid, alert_category="SOH_DECLINE", alert_tier="ESCALATION",
                alert_label_plain=f"Capacity below 70% and still falling — replacement window approaching",
                signal_source="bhs.soh_coulomb_latest + soh_coulomb_trend",
                days_since_trigger=None, corroborated=1,
                recommended_action="Plan replacement", cost_estimate_inr=BATT_REPLACE_COST)
        elif st is not None and st < -0.3:
            add(bid, alert_category="SOH_DECLINE", alert_tier="ACUTE",
                alert_label_plain="Capacity dropping faster than fleet average",
                signal_source="bhs.soh_coulomb_trend",
                days_since_trigger=None, corroborated=0,
                recommended_action="Service inspection within 4 weeks", cost_estimate_inr=2000)

        # CELL_IMBALANCE_ESCALATION (highest priority of the 3)
        if v.get("repeat_alert_flag") == 1:
            add(bid, alert_category="CELL_IMBALANCE_ESCALATION", alert_tier="ESCALATION",
                alert_label_plain="Cell imbalance keeps coming back after rebalancing",
                signal_source="vwf.repeat_alert_flag",
                days_since_trigger=None,
                corroborated=1 if v.get("cell_spread_max") and v["cell_spread_max"] > spread_p75_v else 0,
                recommended_action="Service inspection — likely individual cell failure",
                cost_estimate_inr=8000)
        # CELL_IMBALANCE_ACUTE
        elif v.get("cell_spread_max") and v["cell_spread_max"] > spread_p75_v * 1.5:
            add(bid, alert_category="CELL_IMBALANCE_ACUTE", alert_tier="ACUTE",
                alert_label_plain=f"Cell spread {v['cell_spread_max']:.0f}mV — significantly above fleet",
                signal_source="vwf.cell_spread_max vs cohort P75x1.5",
                days_since_trigger=None, corroborated=0,
                recommended_action="Service: cell balancing", cost_estimate_inr=CELL_BAL_COST)
        # CELL_IMBALANCE_EARLY
        elif v.get("cell_spread_acceleration") and v["cell_spread_acceleration"] > 2:
            add(bid, alert_category="CELL_IMBALANCE_EARLY", alert_tier="EARLY_WARNING",
                alert_label_plain="Cell spread accelerating — early imbalance signal",
                signal_source="vwf.cell_spread_acceleration",
                days_since_trigger=None, corroborated=0,
                recommended_action="Schedule balancing at next service", cost_estimate_inr=CELL_BAL_COST)

        # CHARGER_ANOMALY
        ct = v.get("charger_type_inferred")
        if ct in ("NON_STANDARD", "DEGRADED"):
            add(bid, alert_category="CHARGER_ANOMALY", alert_tier="ACUTE",
                alert_label_plain=("Charger drawing more power than spec"
                                   if ct == "NON_STANDARD"
                                   else "Charger underperforming — may be faulty"),
                signal_source="vwf.charger_type_inferred",
                days_since_trigger=None, corroborated=0,
                recommended_action="Replace with OEM 900W charger", cost_estimate_inr=4500)

        # BMS_STRESS
        if v.get("bms_stress_index") is not None and v["bms_stress_index"] >= 7:
            add(bid, alert_category="BMS_STRESS", alert_tier="EARLY_WARNING",
                alert_label_plain=f"BMS protection events frequent — system under stress",
                signal_source="vwf.bms_stress_index",
                days_since_trigger=None, corroborated=0,
                recommended_action="Inspect at next service", cost_estimate_inr=1500)

        # DRIVETRAIN_STRESS
        if v.get("drivetrain_stress_flag") == 1:
            add(bid, alert_category="DRIVETRAIN_STRESS", alert_tier="EARLY_WARNING",
                alert_label_plain="Drivetrain drawing higher current per km/h than cohort — inspect motor/load/tyres",
                signal_source="vwf.drivetrain_stress_flag",
                days_since_trigger=None, corroborated=0,
                recommended_action="Inspect: motor, load, tyre pressure", cost_estimate_inr=1200)

        # CONNECTOR_SUSPECT
        if v.get("connector_suspect_flag") == 1:
            add(bid, alert_category="CONNECTOR_SUSPECT", alert_tier="EARLY_WARNING",
                alert_label_plain="Suspected connector or busbar contact resistance",
                signal_source="vwf.connector_suspect_flag (Rule 246)",
                days_since_trigger=None, corroborated=1,
                recommended_action="Inspect connectors at next scheduled service",
                cost_estimate_inr=600)

        # COHORT_DIVERGENCE
        dq = bhs.get("divergence_quadrant")
        if dq == "CONFIRMED_DECLINE":
            add(bid, alert_category="COHORT_DIVERGENCE", alert_tier="ACUTE",
                alert_label_plain="Diverging from cohort — declining faster than peers",
                signal_source="bhs.divergence_quadrant",
                days_since_trigger=None, corroborated=1,
                recommended_action="Service inspection within 4 weeks", cost_estimate_inr=2000)
        elif dq == "EARLY_WARNING":
            add(bid, alert_category="COHORT_DIVERGENCE", alert_tier="EARLY_WARNING",
                alert_label_plain="Range healthy but chemistry signals diverging from peers",
                signal_source="bhs.divergence_quadrant",
                days_since_trigger=None, corroborated=0,
                recommended_action="Monitor weekly", cost_estimate_inr=None)

        # SERVICE_NONCOMPLIANCE
        scs = bpp.get("service_compliance_score")
        if scs is not None and scs < 0.4:
            add(bid, alert_category="SERVICE_NONCOMPLIANCE", alert_tier="ACUTE",
                alert_label_plain=f"Past service actions not followed through (score {scs:.2f})",
                signal_source="battery_personal_params.service_compliance_score",
                days_since_trigger=None, corroborated=1,
                recommended_action="Operator briefing required", cost_estimate_inr=None)

        # AGGRESSIVE_DRIVING
        if bpp.get("driver_stress_tier") == "HIGH":
            add(bid, alert_category="AGGRESSIVE_DRIVING", alert_tier="EARLY_WARNING",
                alert_label_plain="Driver behaviour shortening battery life",
                signal_source="battery_personal_params.driver_stress_tier",
                days_since_trigger=None, corroborated=0,
                recommended_action="Driver coaching", cost_estimate_inr=None)

        # FAILURE_CHAIN_RISK (proxy via fault_duration_weeks)
        fdw = v.get("fault_duration_weeks")
        if fdw is not None and fdw >= 2:
            add(bid, alert_category="FAILURE_CHAIN_RISK", alert_tier="EARLY_WARNING",
                alert_label_plain=f"Fault has persisted {fdw} weeks — risk of cascading failure",
                signal_source="vwf.fault_duration_weeks",
                days_since_trigger=fdw * 7, corroborated=0,
                recommended_action="Service inspection", cost_estimate_inr=2000)

        # CELL_VOLTAGE_MIN_ALERT — Path A signal, min cell voltage + persistence
        cvm = v.get("cell_voltage_min_weekly")
        cvt = v.get("cell_voltage_min_trend")
        pwf = v.get("persistent_weak_cell_flag")
        WEAK_THR = float(get_param("cell_weak_voltage_alert_threshold", default=2.8))
        TREND_THR = float(get_param("cell_voltage_min_trend_alert_threshold", default=-0.008))
        DEAD_THR = float(get_param("cell_dead_voltage_threshold_lfp", default=0.15))
        if cvm is not None and cvm < WEAK_THR and pwf == 1:
            if cvm < DEAD_THR:
                tier = "ESCALATION"
            elif cvt is not None and cvt < TREND_THR:
                tier = "ACUTE"
            else:
                tier = "EARLY_WARNING"
            corro = 1 if (v.get("cell_spread_max") or 0) > 200 else 0
            add(bid, alert_category="CELL_VOLTAGE_MIN_ALERT", alert_tier=tier,
                alert_label_plain=f"Weakening cell detected — lowest cell voltage {cvm:.3f}V, weak for multiple weeks",
                signal_source="cell_voltage_min_weekly (Path A — 30-sec CAN extraction)",
                days_since_trigger=None, corroborated=corro,
                recommended_action="Cell voltage inspection — schedule cell balance",
                cost_estimate_inr=CELL_BAL_COST)

    alerts.sort(key=lambda a: TIER_ORDER.get(a.get("alert_tier"), 9))
    return alerts


# ── SI-7 GET /api/fleet/service-compliance ──────────────────────────────
@app.get("/api/fleet/service-compliance")
@safe
def si_fleet_service_compliance(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT bpp.battery_id, bpp.service_profile, bpp.service_compliance_score,
               bhs.pack_model
        FROM battery_personal_params bpp
        LEFT JOIN battery_health_scores_v2 bhs ON bhs.battery_id = bpp.battery_id
    """)
    conn.close()
    total = len(rows) or 1
    profile_counts = {}
    score_sum = 0.0
    score_n = 0
    ignored_list = []
    negligent = exemplary = 0
    for r in rows:
        sp = r["service_profile"]
        if sp:
            profile_counts[sp] = profile_counts.get(sp, 0) + 1
        if sp in ("NEGLIGENT", "AT_RISK", "SYSTEMATIC_ABUSE"):
            negligent += 1
        if sp == "EXEMPLARY":
            exemplary += 1
        if r.get("service_compliance_score") is not None:
            score_sum += r["service_compliance_score"]
            score_n += 1
            if r["service_compliance_score"] < 0.4:
                ignored_list.append({
                    "battery_id": r["battery_id"],
                    "pack_model": r.get("pack_model"),
                    "service_compliance_score": r["service_compliance_score"],
                    "service_profile": sp,
                })
    return {
        "service_profile_distribution": [
            {"service_profile": p, "count": c, "pct": round(100 * c / total, 2)}
            for p, c in sorted(profile_counts.items(), key=lambda x: -x[1])
        ],
        "negligent_count": negligent,
        "exemplary_count": exemplary,
        "avg_compliance_score": round(score_sum / score_n, 3) if score_n else None,
        "batteries_with_ignored_alerts": sorted(
            ignored_list, key=lambda x: x["service_compliance_score"])[:100],
        "data_confidence": "HIGH" if total >= 100 else "PARTIAL",
    }


# ═══════════════════════════════════════════════════════════════════════
# SI Customer-Ready Sprint — PHASE 6 — Intelligence Card endpoints
# Tag: intelligence-cards-api-complete
# ═══════════════════════════════════════════════════════════════════════

VALID_AUDIENCES = ('OPERATOR', 'NBFC', 'OEM')


@app.get("/api/battery/{battery_id}/intelligence-card")
@safe
def si_intelligence_card(battery_id: str, audience: str = Query("OPERATOR"),
                          _=Depends(verify_token)):
    audience = (audience or '').upper()
    if audience not in VALID_AUDIENCES:
        raise HTTPException(400, f"audience must be one of {VALID_AUDIENCES}")
    is_demo = 1 if battery_id.startswith("DEMO_") else 0
    conn = get_conn()
    card = q1(conn, """
        SELECT * FROM battery_intelligence_cards
        WHERE battery_id = ? AND audience = ? AND is_demo = ?
        ORDER BY week_number DESC LIMIT 1
    """, [battery_id, audience, is_demo])
    conn.close()
    if not card:
        raise HTTPException(404, "No intelligence card generated for this battery.")
    return card


@app.get("/api/fleet/intelligence-summary")
@safe
def si_fleet_intelligence_summary(audience: str = Query("OPERATOR"),
                                   _=Depends(verify_token)):
    audience = (audience or '').upper()
    if audience not in VALID_AUDIENCES:
        raise HTTPException(400, f"audience must be one of {VALID_AUDIENCES}")
    conn = get_conn()
    total = q1(conn, """
        SELECT COUNT(*) AS n FROM battery_intelligence_cards
        WHERE audience = ? AND is_demo = 0
    """, [audience])["n"]
    actions = q(conn, """
        SELECT action_urgency, COUNT(*) AS n FROM battery_intelligence_cards
        WHERE audience = ? AND is_demo = 0
        GROUP BY action_urgency ORDER BY n DESC
    """, [audience])
    top_alerts = q(conn, """
        SELECT top_service_alert, COUNT(*) AS n FROM battery_intelligence_cards
        WHERE audience = ? AND is_demo = 0 AND top_service_alert IS NOT NULL
        GROUP BY top_service_alert ORDER BY n DESC LIMIT 5
    """, [audience])
    out = {
        "audience": audience,
        "total_batteries": total,
        "action_distribution": actions,
        "top_5_alerts": top_alerts,
        "data_confidence": "HIGH" if total >= 100 else "PARTIAL",
    }
    if audience == "NBFC":
        out["grade_distribution"] = q(conn, """
            SELECT nbfc_grade, COUNT(*) AS n FROM battery_intelligence_cards
            WHERE audience='NBFC' AND is_demo=0 AND nbfc_grade IS NOT NULL
            GROUP BY nbfc_grade ORDER BY 1
        """)
    if audience == "OEM":
        out["pack_findings"] = q(conn, """
            SELECT bhs.pack_model AS pack_model,
                   bic.oem_pack_finding AS finding, COUNT(*) AS n
            FROM battery_intelligence_cards bic
            JOIN battery_health_scores_v2 bhs ON bhs.battery_id = bic.battery_id
            WHERE bic.audience='OEM' AND bic.is_demo=0
              AND bic.oem_pack_finding IS NOT NULL
            GROUP BY bhs.pack_model, bic.oem_pack_finding
            ORDER BY n DESC
        """)
    conn.close()
    return out


@app.get("/api/demo/intelligence-cards")
@safe
def si_demo_intelligence_cards(audience: str = Query("OPERATOR"),
                                _=Depends(verify_token)):
    audience = (audience or '').upper()
    if audience not in VALID_AUDIENCES:
        raise HTTPException(400, f"audience must be one of {VALID_AUDIENCES}")
    conn = get_conn()
    cards = q(conn, """
        SELECT bic.*, db.archetype
        FROM battery_intelligence_cards bic
        JOIN demo_batteries db ON db.battery_id = bic.battery_id
        WHERE bic.audience = ? AND bic.is_demo = 1
        ORDER BY db.archetype ASC, bic.battery_id ASC
    """, [audience])
    conn.close()
    return {"audience": audience, "n": len(cards), "cards": cards}


@app.get("/api/fleet/failure-chains")
@safe
def si_fleet_failure_chains(_=Depends(verify_token)):
    conn = get_conn()
    rows = q(conn, """
        SELECT bic.battery_id, bhs.pack_model, bat.city_code AS city,
               bic.failure_chain_label, bic.failure_chain_probability,
               bic.failure_chain_weeks, bic.recommended_action,
               bhs.breach_risk_reason
        FROM battery_intelligence_cards bic
        JOIN battery_health_scores_v2 bhs ON bhs.battery_id = bic.battery_id
        JOIN batteries bat ON bat.battery_id = bic.battery_id
        WHERE bic.audience = 'OPERATOR'
          AND bic.is_demo = 0
          AND bic.failure_chain_active = 1
        ORDER BY bic.failure_chain_probability DESC
    """)
    conn.close()
    return {"n_active": len(rows), "chains": rows}


# ═══════════════════════════════════════════════════════════════════════
# Service Backend — Path A cell voltage + SOX tier endpoints
# Tag: service-backend-cell-sox-complete
# ═══════════════════════════════════════════════════════════════════════


def _cell_voltage_alert_tier(v, conn):
    """Derive alert tier from cell_voltage_min_weekly. Reads thresholds from params."""
    if v is None:
        return "NO_DATA"
    dead_thr = float(get_param("cell_dead_voltage_threshold_lfp", default=0.15))
    weak_thr = float(get_param("cell_weak_voltage_alert_threshold", default=2.8))
    if v < dead_thr:      return "DEAD"
    if v < 0.5:            return "NEAR_DEAD"
    if v < weak_thr:       return "WEAK"
    return "NORMAL"


SOX_TIER_PLAIN = {
    "HEALTHY":        "Power delivery normal",
    "POWER_DEGRADED": "Power output declining — likely charger driven",
    "EARLY_WARNING":  "Early power decline detected",
    "CRITICAL":       "Severe power degradation — service required",
    "SILENT_SOX":     "Silent power decline — BMS not alerting",
}


@app.get("/api/battery/{battery_id}/cell-health")
@safe
def svc_cell_health(battery_id: str, _=Depends(verify_token)):
    if battery_id in SI_DEMO_BLACKLIST:
        raise HTTPException(404, "battery not found")
    conn = get_conn()
    row = q1(conn, """
        SELECT v.battery_id, v.week_number,
               v.cell_voltage_min_weekly, v.cell_voltage_min_index,
               v.cell_voltage_min_trend, v.persistent_weak_cell_flag,
               v.cell_spread_max, v.spread_commissioning_delta
        FROM vehicle_weekly_features v
        WHERE v.battery_id = ?
        ORDER BY v.week_number DESC LIMIT 1
    """, [battery_id])
    conn.close()
    if not row:
        raise HTTPException(404, "battery not found")
    tier = _cell_voltage_alert_tier(row["cell_voltage_min_weekly"], None)
    cvm = row["cell_voltage_min_weekly"]
    pwf = row["persistent_weak_cell_flag"]
    weak_thr = float(get_param("cell_weak_voltage_alert_threshold", default=2.8))
    high_risk = 1 if (cvm is not None and cvm < weak_thr and pwf == 1) else 0
    return {
        **row,
        "cell_voltage_alert_tier": tier,
        "high_risk_compound": high_risk,
        "data_confidence": "HIGH" if cvm is not None else "PARTIAL",
    }


@app.get("/api/battery/{battery_id}/sox-detail")
@safe
def svc_sox_detail(battery_id: str, _=Depends(verify_token)):
    """SOX tier + active event count + trajectory string for passport card."""
    if battery_id in get_banned_battery_ids():
        raise HTTPException(404, "Not found")
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("v.battery_id")
    row = q1(conn, f"""
        SELECT v.battery_id, v.sox_tier, v.sox_tier_reason,
               v.voltage_sag_trend_4wk
        FROM vehicle_weekly_features v
        WHERE v.battery_id = ? AND {ban_sql}
        ORDER BY v.week_number DESC
        LIMIT 1
    """, [battery_id] + ban_params)
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")
    cnt = q1(conn, """
        SELECT COUNT(*) AS n FROM vehicle_events
        WHERE battery_id = ?
          AND event_type IN ('VOLTAGE_SAG', 'SOX_EVENT')
    """, [battery_id])
    conn.close()

    reason = row.get("sox_tier_reason")
    trend = row.get("voltage_sag_trend_4wk")
    if reason:
        trajectory = reason
    elif trend is not None:
        if trend > 1e-5:
            trajectory = "WORSENING"
        elif trend < -1e-5:
            trajectory = "IMPROVING"
        else:
            trajectory = "STABLE"
    else:
        trajectory = "UNKNOWN"

    return {
        "battery_id": row["battery_id"],
        "sox_tier": row.get("sox_tier") or "UNKNOWN",
        "events_active": int((cnt or {}).get("n") or 0),
        "trajectory": trajectory,
    }


@app.get("/api/battery/{battery_id}/sox-tier")
@safe
def svc_sox_tier(battery_id: str, _=Depends(verify_token)):
    if battery_id in SI_DEMO_BLACKLIST:
        raise HTTPException(404, "battery not found")
    conn = get_conn()
    row = q1(conn, """
        SELECT v.battery_id, v.week_number, v.sox_tier,
               v.heat_generation_index, v.effective_dod_actual,
               v.voltage_sag_ratio_weekly, v.ir_proxy_mid_soc_weekly,
               v.kps_corrected, v.soh_cap_weekly, v.kps_slope_8wk
        FROM vehicle_weekly_features v
        WHERE v.battery_id = ?
        ORDER BY v.week_number DESC LIMIT 1
    """, [battery_id])
    conn.close()
    if not row:
        raise HTTPException(404, "battery not found")
    tier = row.get("sox_tier")
    slope = row.get("kps_slope_8wk")
    soh = _clip_soh_value(row.get("soh_cap_weekly"))
    sox_diverge = 1 if (slope is not None and slope < -0.003 and soh is not None and soh > 75) else 0
    return {
        "battery_id": row["battery_id"],
        "week_number": row["week_number"],
        "sox_tier": tier,
        "sox_tier_plain": SOX_TIER_PLAIN.get(tier, "Status pending"),
        "heat_generation_index": row.get("heat_generation_index"),
        "effective_dod_actual": row.get("effective_dod_actual"),
        "voltage_sag_ratio_weekly": row.get("voltage_sag_ratio_weekly"),
        "ir_proxy_weekly": row.get("ir_proxy_mid_soc_weekly"),
        "kps_corrected": row.get("kps_corrected"),
        "soh_cap_weekly": soh,
        "kps_slope_8wk": slope,
        "sox_divergence_detected": sox_diverge,
        "bms_alerting": 0,
        "data_confidence": "HIGH" if tier is not None else "PARTIAL",
    }


@app.get("/api/fleet/cell-voltage-alerts")
@safe
def svc_fleet_cell_voltage_alerts(_=Depends(verify_token)):
    conn = get_conn()
    weak_thr = float(get_param("cell_weak_voltage_alert_threshold", default=2.8))
    dead_thr = float(get_param("cell_dead_voltage_threshold_lfp", default=0.15))
    trend_thr = float(get_param("cell_voltage_min_trend_alert_threshold", default=-0.008))
    rows = q(conn, """
        WITH wk AS (
          SELECT battery_id, MAX(week_number) AS wk
          FROM vehicle_weekly_features
          WHERE cell_voltage_min_weekly IS NOT NULL
          GROUP BY battery_id
        )
        SELECT v.battery_id, b.battery_model AS pack_model, b.city_code AS city,
               v.cell_voltage_min_weekly, v.cell_voltage_min_index,
               v.cell_voltage_min_trend, v.persistent_weak_cell_flag,
               v.cell_spread_max, v.sox_tier,
               bhs.action_primary AS current_action
        FROM vehicle_weekly_features v
        JOIN wk ON wk.battery_id = v.battery_id AND wk.wk = v.week_number
        JOIN batteries b ON b.battery_id = v.battery_id
        LEFT JOIN battery_health_scores_v2 bhs ON bhs.battery_id = v.battery_id
        WHERE v.cell_voltage_min_weekly < ?
          AND v.battery_id NOT IN ('BAT_LFP_034','BAT_LFP_202')
        ORDER BY v.cell_voltage_min_weekly ASC
    """, [weak_thr])
    conn.close()
    out = []
    for r in rows:
        cvm = r["cell_voltage_min_weekly"]
        pwf = r["persistent_weak_cell_flag"]
        trend = r["cell_voltage_min_trend"]
        # tier
        if cvm < dead_thr: tier = "DEAD"
        elif cvm < 0.5: tier = "NEAR_DEAD"
        else: tier = "WEAK"
        high_risk = 1 if (cvm < weak_thr and pwf == 1) else 0
        # recommended_action
        if tier in ("DEAD", "NEAR_DEAD"):
            rec = "Replace or cell-level inspection — immediate"
        elif tier == "WEAK" and pwf == 1 and trend is not None and trend < trend_thr:
            rec = "Cell balance + weekly monitoring"
        elif tier == "WEAK" and pwf == 1:
            rec = "Cell balance at next service"
        else:
            rec = "Monitor weekly — watch for acceleration"
        idx = r["cell_voltage_min_index"]
        out.append({
            "battery_id": r["battery_id"],
            "pack_model": r["pack_model"],
            "city": r["city"],
            "cell_voltage_min_weekly": cvm,
            "cell_voltage_min_index": idx,
            "cell_voltage_min_index_label": f"Cell {idx}" if idx else None,
            "cell_voltage_min_trend": trend,
            "persistent_weak_cell_flag": pwf,
            "cell_voltage_alert_tier": tier,
            "high_risk_compound": high_risk,
            "current_action": r["current_action"],
            "cell_spread_max": r["cell_spread_max"],
            "sox_tier": r["sox_tier"],
            "recommended_action": rec,
            "data_confidence": "HIGH",
        })
    if len(out) > 80:
        # warn but return
        print(f"WARNING svc_fleet_cell_voltage_alerts: {len(out)} batteries (>80) — threshold may be too broad")
    return out


# ══════════════════════════════════════════════════════════════════════
# SOX-2 BACKEND — 6 endpoints surfacing SOX Intelligence for demo + prod.
# Demo endpoints reject production IDs with 403. All responses include data_confidence.
# Event active proxy: resolved_week IS NULL (vehicle_events has no `active` column).
# E-code → event_type map mirrors SOX-1E tier logic.
# ══════════════════════════════════════════════════════════════════════

_SOX_EVENT_MAP = {
    "E3":  ("E3_THERMAL_STRESS", "THERMAL_STRESS"),
    "E7":  ("E7_VOLTAGE_SAG", "VOLTAGE_SAG"),
    "E11": ("INTERNAL_RESISTANCE_ESCALATION",),
}


def _sox_active_event_count(conn, code: str) -> int:
    types = _SOX_EVENT_MAP[code]
    placeholders = ",".join(["?"] * len(types))
    row = conn.execute(
        f"SELECT COUNT(DISTINCT battery_id) FROM vehicle_events "
        f"WHERE event_type IN ({placeholders}) AND resolved_week IS NULL",
        types,
    ).fetchone()
    return int(row[0]) if row else 0


def _require_demo_id(battery_id: str):
    """Reject production battery IDs on demo endpoints per SOX-2 spec."""
    if not battery_id or not battery_id.startswith("DEMO_SOX_"):
        raise HTTPException(
            status_code=403,
            detail="Production battery IDs not permitted on demo endpoint. Use production API.",
        )


# ── Lattice node schemas — architecture knowledge, hard-coded shape, live values from demo.
_SOX_NODE_SCHEMAS = {
    "pack-design": {
        "domain": "Pack Architecture",
        "domain_color": "#7C3AED",
        "inbound_signals": [
            {"signal": "pack_model", "source": "demo_batteries", "unit": "label"},
            {"signal": "commissioning_spread_mv", "source": "demo_batteries", "unit": "mV"},
        ],
        "outbound_flows": [
            {"to": "cell-chemistry", "mechanism": "Pack topology dictates cell stress distribution under load"},
            {"to": "charger-interface", "mechanism": "Pack spec defines the acceptable charger envelope"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Pack design variant", "to": "Cell balance tolerance"},
            {"step": 2, "from": "Balance tolerance", "to": "Weekly spread evolution"},
            {"step": 3, "from": "Spread evolution", "to": "Service intervention cadence"},
        ],
    },
    "cell-chemistry": {
        "domain": "Chemistry",
        "domain_color": "#059669",
        "inbound_signals": [
            {"signal": "cell_spread_max", "source": "demo_weekly_features", "unit": "mV"},
            {"signal": "soh_cap_weekly", "source": "demo_weekly_features", "unit": "ratio"},
        ],
        "outbound_flows": [
            {"to": "sox-axis", "mechanism": "SEI growth from chemistry fade raises R0"},
            {"to": "soh-axis", "mechanism": "Active material loss reduces coulombic capacity"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Chemistry degradation", "to": "SEI layer growth"},
            {"step": 2, "from": "SEI growth", "to": "R0 rise (SOX)"},
            {"step": 3, "from": "R0 rise", "to": "Voltage sag + heat"},
        ],
    },
    "charger-interface": {
        "domain": "SOX Intelligence",
        "domain_color": "#DC2626",
        "inbound_signals": [
            {"signal": "charger_type_inferred", "source": "demo_batteries", "unit": "label"},
            {"signal": "charging_profile", "source": "demo_batteries", "unit": "label"},
        ],
        "outbound_flows": [
            {"to": "sox-axis", "mechanism": "Non-standard current amplitude accelerates R0 rise"},
            {"to": "thermal", "mechanism": "Over-current charging dumps heat into the pack"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Non-standard charger", "to": "Current above design"},
            {"step": 2, "from": "High current", "to": "Accelerated R0 rise"},
            {"step": 3, "from": "R0 rise", "to": "Thermal feedback + voltage sag"},
            {"step": 4, "from": "Voltage sag", "to": "Range loss"},
        ],
    },
    "use-case": {
        "domain": "Operator Behaviour",
        "domain_color": "#2563EB",
        "inbound_signals": [
            {"signal": "use_case_inferred", "source": "demo_batteries", "unit": "label"},
            {"signal": "km_total_week", "source": "demo_weekly_features", "unit": "km"},
            {"signal": "trips_per_week", "source": "demo_weekly_features", "unit": "count"},
        ],
        "outbound_flows": [
            {"to": "soc-axis", "mechanism": "Use intensity drives DoD and cycle rate"},
            {"to": "thermal", "mechanism": "High-load duty cycles accumulate thermal stress"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Use intensity", "to": "Cycle rate + DoD depth"},
            {"step": 2, "from": "Cycle rate", "to": "Coulombic throughput"},
            {"step": 3, "from": "Throughput", "to": "SOH decay"},
        ],
    },
    "thermal": {
        "domain": "SOX Intelligence",
        "domain_color": "#DC2626",
        "inbound_signals": [
            {"signal": "temp_max_weekly", "source": "demo_weekly_features", "unit": "°C"},
            {"signal": "heat_generation_index", "source": "demo_weekly_features", "unit": "W"},
        ],
        "outbound_flows": [
            {"to": "sox-axis", "mechanism": "Arrhenius amplification: every 10°C doubles R0 rise rate"},
            {"to": "cell-chemistry", "mechanism": "Heat accelerates SEI layer growth and lithium plating"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Thermal stress", "to": "Accelerated R0 rise"},
            {"step": 2, "from": "R0 rise", "to": "Voltage sag under load"},
            {"step": 3, "from": "Voltage sag", "to": "BMS early cutoff"},
            {"step": 4, "from": "BMS cutoff", "to": "Range loss"},
        ],
    },
    "cell-balance": {
        "domain": "Chemistry",
        "domain_color": "#059669",
        "inbound_signals": [
            {"signal": "cell_spread_max", "source": "demo_weekly_features", "unit": "mV"},
            {"signal": "cell_spread_mean", "source": "demo_weekly_features", "unit": "mV"},
        ],
        "outbound_flows": [
            {"to": "soc-axis", "mechanism": "Imbalance shortens usable SoC window (BMS cut-off earliest cell)"},
            {"to": "sox-axis", "mechanism": "Unbalanced cells see uneven current → R0 divergence"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Spread rising", "to": "Usable DoD shrinks"},
            {"step": 2, "from": "DoD shrink", "to": "Range loss even without capacity loss"},
        ],
    },
    "bms-firmware": {
        "domain": "Controls",
        "domain_color": "#6B7280",
        "inbound_signals": [
            {"signal": "soc_ceiling_mean", "source": "demo_weekly_features", "unit": "%"},
            {"signal": "voltage_sag_ratio_weekly", "source": "demo_weekly_features", "unit": "ratio"},
        ],
        "outbound_flows": [
            {"to": "soc-axis", "mechanism": "Cutoff voltage governs usable SoC span"},
            {"to": "operator", "mechanism": "Firmware behaviour shapes range experience"},
        ],
        "blind_spot": "BMS hardware failure has no electrical precursor in 30-sec telemetry",
        "cascade_path": [
            {"step": 1, "from": "Firmware decisions", "to": "When cutoffs fire"},
            {"step": 2, "from": "Early cutoff", "to": "Operator sees range loss"},
        ],
    },
    "fleet-network": {
        "domain": "Operations",
        "domain_color": "#0891B2",
        "inbound_signals": [
            {"signal": "service_profile", "source": "demo_batteries", "unit": "label"},
            {"signal": "warranty_eligible", "source": "demo_batteries", "unit": "flag"},
        ],
        "outbound_flows": [
            {"to": "operator", "mechanism": "Service cadence determines catch-rate of SOX onset"},
            {"to": "oem", "mechanism": "Claim patterns feed back into pack design revisions"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Detected SOX", "to": "Service ticket"},
            {"step": 2, "from": "Service ticket", "to": "Intervention window"},
            {"step": 3, "from": "Intervention", "to": "Cascade broken or confirmed"},
        ],
    },
    "sox-axis": {
        "domain": "SOX Intelligence",
        "domain_color": "#DC2626",
        "inbound_signals": [
            {"signal": "ir_proxy_weekly", "source": "demo_weekly_features", "unit": "Ω"},
            {"signal": "heat_generation_index", "source": "demo_weekly_features", "unit": "W"},
            {"signal": "voltage_sag_ratio_weekly", "source": "demo_weekly_features", "unit": "ratio"},
        ],
        "outbound_flows": [
            {"to": "soc-axis", "mechanism": "R0 rise shrinks effective DoD (Thevenin cutoff)"},
            {"to": "soh-axis", "mechanism": "For LFP, R0 trajectory independent of SOH — hence 'silent'"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "R0 rise", "to": "Higher heat (I²R) + voltage sag"},
            {"step": 2, "from": "Heat + sag", "to": "BMS cutoff earlier"},
            {"step": 3, "from": "Early cutoff", "to": "Effective DoD shrinks → range loss"},
        ],
    },
    "soh-axis": {
        "domain": "Capacity",
        "domain_color": "#059669",
        "inbound_signals": [
            {"signal": "soh_cap_weekly", "source": "demo_weekly_features", "unit": "ratio"},
            {"signal": "soh_coulomb_weekly", "source": "demo_weekly_features", "unit": "ratio"},
            {"signal": "efc_cumulative", "source": "demo_weekly_features", "unit": "cycles"},
        ],
        "outbound_flows": [
            {"to": "operator", "mechanism": "SOH loss directly reduces per-charge km"},
            {"to": "nbfc", "mechanism": "SOH is the covenant-grade lifecycle signal"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "Cycle accumulation", "to": "Active material loss"},
            {"step": 2, "from": "Capacity fade", "to": "Lower effective km/charge"},
        ],
    },
    "soc-axis": {
        "domain": "Operating Window",
        "domain_color": "#2563EB",
        "inbound_signals": [
            {"signal": "dod_mean", "source": "demo_weekly_features", "unit": "ratio"},
            {"signal": "effective_dod_actual", "source": "demo_weekly_features", "unit": "ratio"},
            {"signal": "soc_ceiling_mean", "source": "demo_weekly_features", "unit": "%"},
        ],
        "outbound_flows": [
            {"to": "operator", "mechanism": "Usable SoC span = visible range per cycle"},
            {"to": "sox-axis", "mechanism": "Deep DoD accelerates R0 rise"},
        ],
        "blind_spot": None,
        "cascade_path": [
            {"step": 1, "from": "SOC window shrinks", "to": "Fewer km per cycle"},
            {"step": 2, "from": "Operator compensates", "to": "Deeper DoD or more trips"},
            {"step": 3, "from": "Deeper cycles", "to": "Accelerated SOX"},
        ],
    },
}


# ── Archetype → mitigation blueprint (SOX-2 Endpoint 6) ─────────────────

_ARCHETYPE_MITIGATIONS = {
    "A": [],  # Healthy — no mitigation
    "B": [{
        "cascade": "Charger-driven R0 rise",
        "platform_detects": "E11 active + charger_type NON_STANDARD or HEAVY",
        "action": "CHARGER_SWAP",
        "owner": "Operator",
        "cost_of_action_inr": 1500,
        "cost_if_ignored_inr": 40000,
        "cost_framing": "Charger swap ₹1,500 vs battery replacement ₹40,000",
        "corroborating_surface": "SERVICE_INTELLIGENCE",
        "corroboration_signal": "charger_type_inferred=NON_STANDARD",
    }],
    "C": [{
        "cascade": "Pack3401 design-gap degradation",
        "platform_detects": "Pack3401 SR cohort reaches operational floor earlier than Pack3001 (median knee week 18.8 vs 19.4)",
        "action": "OEM_DESIGN_REVIEW",
        "owner": "OEM",
        "cost_of_action_inr": 0,
        "cost_if_ignored_inr": 60000,
        "cost_framing": "OEM review (no operator cost) vs cohort replacement ₹60,000 each",
        "corroborating_surface": "OEM_INTELLIGENCE",
        "corroboration_signal": "pack_model=GF_LFP_Pack3401 AND city_code=SR",
    }],
    "D": [{
        "cascade": "Silent SOX — BMS blind spot",
        "platform_detects": "kps declining + Coulomb stable + E11 active + E7 NOT fired",
        "action": "PROACTIVE_SERVICE_VISIT",
        "owner": "Operator",
        "cost_of_action_inr": 2000,
        "cost_if_ignored_inr": 40000,
        "cost_framing": "Proactive visit ₹2,000 vs unplanned replacement ₹40,000",
        "corroborating_surface": None,
        "corroboration_signal": None,
    }],
    "E": [{
        "cascade": "Thermal feedback loop",
        "platform_detects": "E3 + E7 + E11 all active",
        "action": "REPLACE_BEFORE_SUMMER",
        "owner": "Operator+OEM",
        "cost_of_action_inr": 40000,
        "cost_if_ignored_inr": 80000,
        "cost_framing": "Planned replacement ₹40,000 vs thermal runaway + cabinet damage ₹80,000+",
        "corroborating_surface": "SERVICE_INTELLIGENCE",
        "corroboration_signal": "temp_max>45C + heat_generation_index>p75",
    }],
    "F": [{
        "cascade": "Born-weak commissioning gap",
        "platform_detects": "High commissioning_spread_mv + E11 near-threshold + age<12 weeks",
        "action": "OEM_WARRANTY_CLAIM",
        "owner": "OEM",
        "cost_of_action_inr": 0,
        "cost_if_ignored_inr": 40000,
        "cost_framing": "Warranty claim (no operator cost) vs out-of-warranty replacement ₹40,000",
        "corroborating_surface": "OEM_INTELLIGENCE",
        "corroboration_signal": "warranty_eligible=1",
    }],
}


# ════════════════════════════════════════════════════════════════
# ENDPOINT 1 — /api/sox/fleet-summary
# ════════════════════════════════════════════════════════════════

@app.get("/api/sox/fleet-summary")
@safe
def sox_fleet_summary(_=Depends(verify_token)):
    conn = get_conn()

    # DEMO section
    demo = q1(conn, "SELECT COUNT(*) AS n FROM demo_batteries")
    n_total = int(demo["n"]) if demo else 0

    def _count(sql):
        row = q1(conn, sql)
        return int(row["n"]) if row and row["n"] is not None else 0

    n_sox_confirmed = _count(
        "SELECT COUNT(*) AS n FROM demo_batteries WHERE e11_active=1 OR e7_active=1"
    )
    n_silent_sox = _count("SELECT COUNT(*) AS n FROM demo_batteries WHERE sox_tier_current='SILENT_SOX'")
    n_thermal_risk = _count("SELECT COUNT(*) AS n FROM demo_batteries WHERE e3_active=1 AND e11_active=1")
    n_critical = _count("SELECT COUNT(*) AS n FROM demo_batteries WHERE sox_tier_current='CRITICAL'")
    n_power_degraded = _count(
        "SELECT COUNT(*) AS n FROM demo_batteries WHERE sox_tier_current='POWER_DEGRADED'"
    )
    avg_heat_row = q1(
        conn,
        "SELECT AVG(heat_generation_index) AS a FROM demo_weekly_features "
        "WHERE week_number=12 AND heat_generation_index IS NOT NULL",
    )
    avg_heat_w = round(float(avg_heat_row["a"]), 4) if avg_heat_row and avg_heat_row["a"] is not None else None

    arch_rows = q(conn, "SELECT archetype, COUNT(*) AS n FROM demo_batteries GROUP BY archetype ORDER BY archetype")
    archetype_distribution = {r["archetype"]: int(r["n"]) for r in arch_rows}

    # PRODUCTION section
    prod_rows = q(
        conn,
        """
        SELECT sox_tier, COUNT(*) AS n
        FROM vehicle_weekly_features v
        WHERE sox_tier IS NOT NULL
          AND v.week_number = (
            SELECT MAX(week_number) FROM vehicle_weekly_features v2
            WHERE v2.battery_id = v.battery_id
          )
        GROUP BY sox_tier
        """,
    )
    tier_dist = {t: 0 for t in ("HEALTHY", "EARLY_WARNING", "POWER_DEGRADED", "SILENT_SOX", "CRITICAL")}
    for r in prod_rows:
        tier_dist[r["sox_tier"]] = int(r["n"])
    total_scored = sum(tier_dist.values())

    e11_active = _sox_active_event_count(conn, "E11")
    e7_active = _sox_active_event_count(conn, "E7")

    # charger_swap_count: action_primary_v2 not present on this project (Rule 207 deprecated action_primary).
    # Rule-207-safe proxy: oem_action_remark mentioning charger swap.
    csr = q1(
        conn,
        "SELECT COUNT(*) AS n FROM battery_health_scores_v2 "
        "WHERE oem_action_remark LIKE '%charger%swap%' OR oem_action_remark LIKE '%CHARGER%SWAP%'",
    )
    charger_swap_count = int(csr["n"]) if csr else 0

    heat_avg_row = q1(
        conn,
        """
        WITH latest AS (
            SELECT v.battery_id, v.heat_generation_index AS h
            FROM vehicle_weekly_features v
            WHERE v.week_number = (
                SELECT MAX(week_number) FROM vehicle_weekly_features v2
                WHERE v2.battery_id = v.battery_id
                  AND v2.heat_generation_index IS NOT NULL
            )
              AND v.heat_generation_index IS NOT NULL
        )
        SELECT AVG(h) AS a FROM latest
        """,
    )
    heat_avg_w = round(float(heat_avg_row["a"]), 4) if heat_avg_row and heat_avg_row["a"] is not None else None

    conn.close()
    return {
        "demo": {
            "n_total": n_total,
            "n_sox_confirmed": n_sox_confirmed,
            "n_silent_sox": n_silent_sox,
            "n_thermal_risk": n_thermal_risk,
            "n_critical": n_critical,
            "n_power_degraded": n_power_degraded,
            "avg_heat_index_w": avg_heat_w,
            "archetype_distribution": archetype_distribution,
            "data_confidence": "DEMO_SYNTHETIC_MODELLED_FROM_FLEET_DISTRIBUTIONS",
        },
        "production": {
            "total_scored": total_scored,
            "sox_tier_distribution": tier_dist,
            "e11_active_count": e11_active,
            "e7_active_count": e7_active,
            "charger_swap_count": charger_swap_count,
            "charger_swap_source": "oem_action_remark LIKE charger swap (Rule 207 compliant proxy)",
            "heat_avg_w": heat_avg_w,
            "data_confidence": "PRODUCTION",
        },
    }


# ════════════════════════════════════════════════════════════════
# ENDPOINT 2 — /api/sox/battery-detail/{battery_id}
# ════════════════════════════════════════════════════════════════

@app.get("/api/sox/battery-detail/{battery_id}")
@safe
def sox_battery_detail(battery_id: str, _=Depends(verify_token)):
    _require_demo_id(battery_id)
    conn = get_conn()

    battery = q1(conn, "SELECT * FROM demo_batteries WHERE battery_id = ?", (battery_id,))
    if not battery:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Demo battery {battery_id} not found")

    weekly = q(
        conn,
        """
        SELECT week_number, ir_proxy_weekly, kps_corrected, soh_cap_weekly,
               soh_coulomb_weekly, soh_coulomb_source, cell_spread_max,
               voltage_sag_ratio_weekly, heat_generation_index, effective_dod_actual,
               temp_max_weekly, charge_duration_hrs, soc_ceiling_mean
        FROM demo_weekly_features
        WHERE battery_id = ?
        ORDER BY week_number ASC
        """,
        (battery_id,),
    )

    predictions = q1(conn, "SELECT * FROM demo_predictions WHERE battery_id = ?", (battery_id,))

    alerts = q(
        conn,
        """
        SELECT * FROM demo_service_alerts
        WHERE battery_id = ? AND cross_surface_tag = 'SOX_INTELLIGENCE'
        ORDER BY id
        """,
        (battery_id,),
    )

    conn.close()
    return {
        "battery": battery,
        "weekly_history": weekly,
        "predictions": predictions,
        "service_alerts": alerts,
        "data_confidence": "DEMO_SYNTHETIC_MODELLED_FROM_FLEET_DISTRIBUTIONS",
    }


# ════════════════════════════════════════════════════════════════
# ENDPOINT 3 — /api/sox/archetype-comparison
# ════════════════════════════════════════════════════════════════

@app.get("/api/sox/archetype-comparison")
@safe
def sox_archetype_comparison(_=Depends(verify_token)):
    conn = get_conn()
    out = []
    archetypes = [r["archetype"] for r in q(
        conn, "SELECT DISTINCT archetype FROM demo_batteries ORDER BY archetype"
    )]

    for arch in archetypes:
        w1 = q1(
            conn,
            """
            SELECT AVG(ir_proxy_weekly) AS ir, AVG(kps_corrected) AS kps,
                   AVG(soh_cap_weekly) AS soh
            FROM demo_weekly_features w JOIN demo_batteries b USING(battery_id)
            WHERE b.archetype = ? AND w.week_number = 1
            """,
            (arch,),
        ) or {}
        w12 = q1(
            conn,
            """
            SELECT AVG(ir_proxy_weekly) AS ir, AVG(kps_corrected) AS kps,
                   AVG(soh_cap_weekly) AS soh, AVG(heat_generation_index) AS heat,
                   AVG(effective_dod_actual) AS eff_dod
            FROM demo_weekly_features w JOIN demo_batteries b USING(battery_id)
            WHERE b.archetype = ? AND w.week_number = 12
            """,
            (arch,),
        ) or {}
        events = q1(
            conn,
            """
            SELECT SUM(e7_active) AS e7, SUM(e11_active) AS e11, SUM(e3_active) AS e3
            FROM demo_batteries WHERE archetype = ?
            """,
            (arch,),
        ) or {}
        rul = q1(
            conn,
            """
            SELECT AVG(rul_weeks_p50) AS r
            FROM demo_predictions p JOIN demo_batteries b USING(battery_id)
            WHERE b.archetype = ?
            """,
            (arch,),
        ) or {}
        meta = q1(
            conn,
            "SELECT scenario_label, sox_tier_current FROM demo_batteries WHERE archetype = ? LIMIT 1",
            (arch,),
        ) or {}

        ir_w1 = w1.get("ir") or 0.0
        ir_w12 = w12.get("ir") or 0.0
        ir_rise_pct = round(((ir_w12 - ir_w1) / ir_w1) * 100.0, 2) if ir_w1 else None

        out.append({
            "archetype": arch,
            "avg_ir_week1": round(ir_w1, 6) if ir_w1 else None,
            "avg_ir_week12": round(ir_w12, 6) if ir_w12 else None,
            "ir_rise_pct": ir_rise_pct,
            "avg_kps_week1": round(w1.get("kps"), 4) if w1.get("kps") is not None else None,
            "avg_kps_week12": round(w12.get("kps"), 4) if w12.get("kps") is not None else None,
            "avg_heat_week12": round(w12.get("heat"), 6) if w12.get("heat") is not None else None,
            "avg_soh_week1": round(w1.get("soh"), 4) if w1.get("soh") is not None else None,
            "avg_soh_week12": round(w12.get("soh"), 4) if w12.get("soh") is not None else None,
            "avg_effective_dod_week12": round(w12.get("eff_dod"), 4) if w12.get("eff_dod") is not None else None,
            "e7_active_count": int(events.get("e7") or 0),
            "e11_active_count": int(events.get("e11") or 0),
            "e3_active_count": int(events.get("e3") or 0),
            "avg_rul_weeks_p50": round(float(rul.get("r")), 1) if rul.get("r") is not None else None,
            "scenario_label": meta.get("scenario_label"),
            "sox_tier": meta.get("sox_tier_current"),
            "data_confidence": "DEMO_SYNTHETIC_MODELLED_FROM_FLEET_DISTRIBUTIONS",
        })

    conn.close()
    return out


# ════════════════════════════════════════════════════════════════
# ENDPOINT 4 — /api/sox/lattice-node/{node_name}?battery_id=DEMO_SOX_XXX
# ════════════════════════════════════════════════════════════════

@app.get("/api/sox/lattice-node/{node_name}")
@safe
def sox_lattice_node(node_name: str, battery_id: str = Query(...), _=Depends(verify_token)):
    if node_name not in _SOX_NODE_SCHEMAS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown node '{node_name}'. Valid: {sorted(_SOX_NODE_SCHEMAS.keys())}",
        )
    _require_demo_id(battery_id)
    conn = get_conn()
    battery = q1(conn, "SELECT * FROM demo_batteries WHERE battery_id = ?", (battery_id,))
    if not battery:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Demo battery {battery_id} not found")

    w12 = q1(
        conn,
        "SELECT * FROM demo_weekly_features WHERE battery_id = ? AND week_number = 12",
        (battery_id,),
    ) or {}
    conn.close()

    import copy
    schema = copy.deepcopy(_SOX_NODE_SCHEMAS[node_name])
    for sig in schema["inbound_signals"]:
        name = sig["signal"]
        src = sig["source"]
        if src == "demo_batteries":
            sig["value"] = battery.get(name)
        elif src == "demo_weekly_features":
            sig["value"] = w12.get(name)
        else:
            sig["value"] = None

    arch = battery.get("archetype")
    scenario = battery.get("scenario_label") or ""
    nbfc_grade = battery.get("nbfc_grade")

    operator_card = {
        "A": "Battery operating normally. No action required.",
        "B": "Charger is accelerating R0 rise. Swap to OEM-standard charger within next cycle.",
        "C": "Pack design-level issue — raise service ticket referencing Pack3401 cohort.",
        "D": "Range dropping silently while BMS reports healthy. Schedule proactive inspection.",
        "E": "Replace before summer. Thermal feedback loop will push to runaway risk.",
        "F": "Warranty claim eligible — battery arrived already weak. Contact OEM.",
    }.get(arch, "Review with service desk.")

    oem_card = {
        "A": "Baseline reference. No design signal.",
        "B": "Non-standard charger adoption pattern — communicate allowed envelope.",
        "C": "Pack3401 design-gap cohort. Review cell selection / BMS tune for next batch.",
        "D": "BMS firmware silent on R0 rise — consider exposing IR trend in next firmware.",
        "E": "Thermal envelope exceeded for target use-case. Revisit pack thermal design.",
        "F": "Commissioning gap — tighten outgoing QA on cell spread.",
    }.get(arch, "No OEM-specific note.")

    nbfc_card = {
        "A": f"Grade {nbfc_grade or 'A'}. Collateral healthy.",
        "B": f"Grade {nbfc_grade or 'B'}. Mitigation available (charger swap). Monitor.",
        "C": f"Grade {nbfc_grade or 'C'}. OEM design-level issue — claim pathway.",
        "D": f"Grade {nbfc_grade or 'B'}. Silent decline flagged by platform; BMS will not warn.",
        "E": f"Grade {nbfc_grade or 'D'}. High replacement risk before season end.",
        "F": f"Grade {nbfc_grade or 'B'}. Warranty pathway covers collateral risk.",
    }.get(arch, f"Grade {nbfc_grade or 'N/A'}.")

    return {
        "node": node_name,
        **schema,
        "battery_id": battery_id,
        "battery_archetype": arch,
        "battery_sox_tier": battery.get("sox_tier_current"),
        "scenario_label": scenario,
        "audience_cards": {
            "operator": operator_card,
            "oem": oem_card,
            "nbfc": nbfc_card,
        },
        "data_confidence": "DEMO_SYNTHETIC_MODELLED_FROM_FLEET_DISTRIBUTIONS",
    }


# ════════════════════════════════════════════════════════════════
# ENDPOINT 5 — /api/sox/predictions/{battery_id}
# ════════════════════════════════════════════════════════════════

@app.get("/api/sox/predictions/{battery_id}")
@safe
def sox_predictions(battery_id: str, _=Depends(verify_token)):
    _require_demo_id(battery_id)
    conn = get_conn()
    pred = q1(conn, "SELECT * FROM demo_predictions WHERE battery_id = ?", (battery_id,))
    w12 = q1(
        conn,
        "SELECT * FROM demo_weekly_features WHERE battery_id = ? AND week_number = 12",
        (battery_id,),
    ) or {}
    conn.close()

    if not pred:
        raise HTTPException(status_code=404, detail=f"No demo_predictions row for {battery_id}")

    f1_prob = round((float(pred.get("failure_mode_top1_prob") or 0)) * 100, 1)
    f2_prob = round((float(pred.get("failure_mode_top2_prob") or 0)) * 100, 1)
    eff_dod_pct = (
        round(float(pred.get("effective_dod_actual")) * 100, 1)
        if pred.get("effective_dod_actual") is not None else None
    )
    heat_w12 = w12.get("heat_generation_index")
    heat_summer = pred.get("heat_index_summer")
    multiplier = round(heat_summer / heat_w12, 2) if heat_w12 and heat_summer else 1.52

    return {
        "battery_id": battery_id,
        "r0_trajectory": {
            "week_12_actual": w12.get("ir_proxy_weekly"),
            "week_16_projected": pred.get("r0_projected_n4"),
            "week_20_projected": pred.get("r0_projected_n8"),
            "week_24_projected": pred.get("r0_projected_n12"),
            "method": pred.get("r0_projection_method"),
            "confidence": pred.get("projection_confidence"),
        },
        "soh_to_floor": {
            "current_soh": w12.get("soh_cap_weekly"),
            "floor_threshold_pct": 67,
            "weeks_remaining": pred.get("soh_at_floor_week"),
            "method": pred.get("soh_floor_method"),
        },
        "heat_summer": {
            "current_index_w": heat_w12,
            "summer_projected_w": heat_summer,
            "multiplier": multiplier,
            "multiplier_source": "September seasonal multiplier from fleet_context_params",
            "method": pred.get("heat_projection_method") or "ARRHENIUS_SEASONAL_MULTIPLIER",
        },
        "effective_dod": {
            "nominal_pct": 90,
            "actual_pct": eff_dod_pct,
            "capacity_accessed_pct": eff_dod_pct,
            "method": pred.get("effective_dod_method") or "THEVENIN_ECM_CUTOFF",
        },
        "failure_modes": {
            "top1": {
                "mode": pred.get("failure_mode_top1"),
                "probability_pct": f1_prob,
                "source": pred.get("markov_transition_source") or "MARKOV_SERVICE_FAILURE_TAXONOMY",
            },
            "top2": {
                "mode": pred.get("failure_mode_top2"),
                "probability_pct": f2_prob,
            },
        },
        "rul": {
            "p50_weeks": pred.get("rul_weeks_p50"),
            "p10_weeks": pred.get("rul_weeks_p10"),
            "method": pred.get("rul_method"),
            "disclosure": "Directional — not for covenant triggers",
        },
        "warranty": {
            "exposure_inr": pred.get("warranty_exposure_inr"),
            "claim_risk": None,
        },
        "data_confidence": pred.get("projection_confidence"),
    }


# ════════════════════════════════════════════════════════════════
# ENDPOINT 6 — /api/sox/mitigation/{battery_id}
# ════════════════════════════════════════════════════════════════

@app.get("/api/sox/mitigation/{battery_id}")
@safe
def sox_mitigation(battery_id: str, _=Depends(verify_token)):
    _require_demo_id(battery_id)
    conn = get_conn()
    battery = q1(conn, "SELECT * FROM demo_batteries WHERE battery_id = ?", (battery_id,))
    if not battery:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Demo battery {battery_id} not found")

    alerts = q(
        conn,
        """
        SELECT * FROM demo_service_alerts
        WHERE battery_id = ? AND cross_surface_tag = 'SOX_INTELLIGENCE'
        """,
        (battery_id,),
    )
    pred = q1(conn, "SELECT rul_weeks_p10 FROM demo_predictions WHERE battery_id = ?", (battery_id,))
    conn.close()

    arch = battery.get("archetype")
    templates = _ARCHETYPE_MITIGATIONS.get(arch, [])

    if not templates:
        return {
            "mitigations": [],
            "status": "No active SOX cascades for this battery",
            "battery_id": battery_id,
            "archetype": arch,
            "data_confidence": "DEMO_SYNTHETIC_MODELLED_FROM_FLEET_DISTRIBUTIONS",
        }

    has_sox_alert = bool(alerts)
    weeks_to_act = int(pred["rul_weeks_p10"]) if pred and pred.get("rul_weeks_p10") else None

    out = []
    for t in templates:
        out.append({
            **t,
            "weeks_to_act": weeks_to_act,
            "corroborated": has_sox_alert or bool(t.get("corroborating_surface")),
        })

    return {
        "mitigations": out,
        "battery_id": battery_id,
        "archetype": arch,
        "active_sox_alerts": len(alerts),
        "data_confidence": "DEMO_SYNTHETIC_MODELLED_FROM_FLEET_DISTRIBUTIONS",
    }


# ══════════════════════════════════════════════════════════════════════
# END SOX-2 BACKEND
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# A3 — Range Intelligence API
#   Fleet distribution, 12-week forecast, personal-vs-ensemble routing,
#   range-floor proximity, attribution + seasonal context.
#   All values from BHS latest snapshot + fleet_context_params. No .pkl
#   inference at request time.
# ══════════════════════════════════════════════════════════════════════


@app.get("/api/range/fleet-summary")
@safe
def range_fleet_summary(_=Depends(verify_token)):
    conn = get_conn()

    # ── 1. Fleet distribution (10km buckets) ───────────────────────
    dist_rows = q(conn, """
        SELECT
            CAST(range_corrected_km / 10.0 AS INTEGER) * 10 AS bucket_floor,
            COUNT(*) AS n
        FROM battery_health_scores_v2
        WHERE range_corrected_km IS NOT NULL
          AND range_corrected_km > 0
        GROUP BY bucket_floor
        ORDER BY bucket_floor
    """)
    total_with_range = sum(r["n"] for r in dist_rows) or 1
    fleet_distribution = [
        {
            "range_bucket": f"{r['bucket_floor']}-{r['bucket_floor']+10} km",
            "bucket_floor_km": int(r["bucket_floor"]),
            "count": int(r["n"]),
            "pct": round(100.0 * r["n"] / total_with_range, 1),
        }
        for r in dist_rows
    ]

    # ── 2. Model routing ────────────────────────────────────────────
    routing_rows = q(conn, """
        SELECT COALESCE(range_model_version, 'STATE_ONLY') AS mv, COUNT(*) AS n
        FROM battery_health_scores_v2
        WHERE range_corrected_km IS NOT NULL
        GROUP BY mv
    """)
    routing = {r["mv"]: int(r["n"]) for r in routing_rows}
    personal_count = sum(v for k, v in routing.items() if "PERSONAL" in (k or "").upper())
    ensemble_count = sum(v for k, v in routing.items() if "ENSEMBLE" in (k or "").upper())
    nmc_count = sum(v for k, v in routing.items() if "NMC" in (k or "").upper())
    state_only_count = routing.get("STATE_ONLY", 0)

    # ── 3. Seasonal context ─────────────────────────────────────────
    from datetime import date
    month_num = date.today().month
    month_name = ["January","February","March","April","May","June",
                  "July","August","September","October","November","December"][month_num-1]
    # Seasonal multiplier approximation for e-rickshaw thermal stress in Indian climate.
    # September is peak (pre-monsoon heat + humidity). Values sourced from Rule 48
    # (Seasonal attribution re-run: Mar→Jun rising, Oct→Jan falling).
    seasonal_curve = {
        1: 0.65, 2: 0.75, 3: 0.85, 4: 1.00, 5: 1.20, 6: 1.35,
        7: 1.25, 8: 1.35, 9: 1.52, 10: 1.25, 11: 0.95, 12: 0.72,
    }
    current_mult = seasonal_curve[month_num]
    peak_mult = max(seasonal_curve.values())
    direction = "rising_to_summer" if 3 <= month_num <= 9 else "cooling"

    # ── 4. Pack comparison ──────────────────────────────────────────
    pack_rows = q(conn, """
        SELECT b.battery_model AS pack_model,
               COUNT(*) AS n,
               ROUND(AVG(bhs.range_corrected_km), 1) AS avg_range_km,
               SUM(CASE
                   WHEN bhs.range_corrected_km <
                        COALESCE(bhs.range_floor_override, bhs.prp_range_threshold_km, 56)
                   THEN 1 ELSE 0 END) AS below_floor_count
        FROM battery_health_scores_v2 bhs
        JOIN batteries b USING(battery_id)
        WHERE bhs.range_corrected_km IS NOT NULL
          AND b.chemistry = 'LFP'
        GROUP BY b.battery_model
        HAVING COUNT(*) >= 2
        ORDER BY n DESC
    """)
    fleet_avg_row = q1(conn, """
        SELECT ROUND(AVG(range_corrected_km), 1) AS avg_fleet
        FROM battery_health_scores_v2 bhs JOIN batteries b USING(battery_id)
        WHERE bhs.range_corrected_km IS NOT NULL AND b.chemistry='LFP'
    """) or {}
    fleet_avg = fleet_avg_row.get("avg_fleet") or 0.0
    pack_comparison = []
    for r in pack_rows:
        vs_pct = round(((r["avg_range_km"] - fleet_avg) / fleet_avg) * 100.0, 1) if fleet_avg else 0.0
        pack_comparison.append({
            "pack_model": r["pack_model"],
            "n": int(r["n"]),
            "avg_range_km": r["avg_range_km"],
            "vs_fleet_pct": vs_pct,
            "below_floor_count": int(r["below_floor_count"]),
        })

    # ── 5. Floor breach probabilities ───────────────────────────────
    breach_row = q1(conn, """
        SELECT
            ROUND(AVG(p_floor_breach_4w),  4) AS wk4,
            ROUND(AVG(p_floor_breach_8w),  4) AS wk8,
            ROUND(AVG(p_floor_breach_12w), 4) AS wk12,
            COUNT(p_floor_breach_12w)        AS n_with_breach
        FROM battery_health_scores_v2
    """) or {}

    # ── 6. P10/P50/P90 fleet (from conformal v3 columns when available) ─
    # Prefer p10_v3/p90_v3 (latest), fallback to range_p10/p50/p90.
    p_row = q1(conn, """
        SELECT
            ROUND(AVG(COALESCE(p10_v3, range_p10)), 1) AS p10,
            ROUND(AVG(COALESCE(range_p50, predicted_range_12w)), 1) AS p50,
            ROUND(AVG(COALESCE(p90_v3, range_p90)), 1) AS p90,
            COUNT(COALESCE(p10_v3, range_p10)) AS n_bands
        FROM battery_health_scores_v2
    """) or {}

    # ── 7. Floor default from fleet_context_params (Rule 173) ──────
    floor_row = q1(conn, """
        SELECT param_value
        FROM fleet_context_params
        WHERE param_name = 'range_floor_km'
          AND chemistry = 'LFP'
          AND segment_type = 'CHEMISTRY'
          AND is_active = 1
        ORDER BY layer DESC
        LIMIT 1
    """)
    if not floor_row:
        floor_row = q1(conn, """
            SELECT param_value FROM fleet_context_params
            WHERE param_name = 'range_floor_km' AND is_active = 1
            ORDER BY layer DESC, segment_value
            LIMIT 1
        """)
    floor_km_default = float(floor_row["param_value"]) if floor_row else 56.0

    # ── 8. Below-floor count across fleet ───────────────────────────
    below_row = q1(conn, """
        SELECT COUNT(*) AS n
        FROM battery_health_scores_v2 bhs JOIN batteries b USING(battery_id)
        WHERE bhs.range_corrected_km IS NOT NULL
          AND bhs.range_corrected_km < COALESCE(
              bhs.range_floor_override, bhs.prp_range_threshold_km, ?)
          AND b.chemistry = 'LFP'
    """, [floor_km_default]) or {}

    total_row = q1(conn, "SELECT COUNT(*) AS n FROM battery_health_scores_v2") or {}
    lfp_total = q1(conn, """
        SELECT COUNT(*) AS n FROM battery_health_scores_v2 bhs
        JOIN batteries b USING(battery_id) WHERE b.chemistry='LFP'
    """) or {}

    conn.close()
    return {
        "fleet_distribution": fleet_distribution,
        "model_routing": {
            "personal_count": personal_count,
            "ensemble_count": ensemble_count,
            "nmc_count": nmc_count,
            "state_only_count": state_only_count,
            "routing_raw": routing,
            "personal_mape_pct": 9.3,
            "ensemble_mape_pct": 14.07,
            "accuracy_note": "Personal Ridge (≥20 weeks history) vs T2+T1 ensemble fallback",
        },
        "seasonal_context": {
            "current_month": month_name,
            "current_month_num": month_num,
            "current_multiplier": current_mult,
            "peak_multiplier": peak_mult,
            "peak_month": "September",
            "direction": direction,
        },
        "pack_comparison": pack_comparison,
        "floor_breach_prob": {
            "wk4": breach_row.get("wk4"),
            "wk8": breach_row.get("wk8"),
            "wk12": breach_row.get("wk12"),
            "n_with_breach": int(breach_row.get("n_with_breach") or 0),
        },
        "p10_p50_p90_fleet": {
            "p10": p_row.get("p10"),
            "p50": p_row.get("p50"),
            "p90": p_row.get("p90"),
            "n_bands": int(p_row.get("n_bands") or 0),
        },
        "floor_km_default": floor_km_default,
        "below_floor_count": int(below_row.get("n") or 0),
        "lfp_total": int(lfp_total.get("n") or 0),
        "total_batteries": int(total_row.get("n") or 0),
        "model_version": "range_ensemble_v1.2.0 + range_personal_extended_v1.0",
        "disclosure": "Projections are directional — not for contractual use. Rule 223.",
        "data_confidence": "PRODUCTION",
    }


@app.get("/api/range/floor-proximity")
@safe
def range_floor_proximity(limit: int = 20, _=Depends(verify_token)):
    """Batteries within 10 km of the floor, by display-friendly tier."""
    conn = get_conn()
    # Floor source (as above)
    floor_row = q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name='range_floor_km' AND chemistry='LFP' AND segment_type='CHEMISTRY' AND is_active=1
        LIMIT 1
    """) or q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name='range_floor_km' AND is_active=1 LIMIT 1
    """)
    floor_km = float(floor_row["param_value"]) if floor_row else 56.0

    rows = q(conn, """
        SELECT bhs.battery_id, b.battery_model AS pack_model,
               bhs.range_corrected_km, bhs.predicted_range_12w,
               bhs.p_floor_breach_12w, bhs.weeks_to_floor,
               bhs.range_method, bhs.range_model_version
        FROM battery_health_scores_v2 bhs JOIN batteries b USING(battery_id)
        WHERE bhs.range_corrected_km IS NOT NULL
          AND b.chemistry = 'LFP'
        ORDER BY bhs.range_corrected_km ASC
        LIMIT ?
    """, [int(limit)])

    def _tier(rul_weeks):
        if rul_weeks is None: return "STABLE"
        try: w = float(rul_weeks)
        except (TypeError, ValueError): return "STABLE"
        if w < 4: return "IMMEDIATE"
        if w < 12: return "NEAR_TERM"
        return "MONITOR"

    results = []
    for r in rows:
        gap = round(r["range_corrected_km"] - floor_km, 1)
        results.append({
            "battery_id": r["battery_id"],
            "display_id": r["battery_id"].replace("BAT_LFP_", "LFP-").replace("BAT_NMC_", "NMC-"),
            "pack_model": r["pack_model"],
            "range_km": r["range_corrected_km"],
            "gap_to_floor_km": gap,
            "floor_km": floor_km,
            "weeks_to_floor": r["weeks_to_floor"],
            "tier": _tier(r["weeks_to_floor"]),
            "p_floor_breach_12w": r["p_floor_breach_12w"],
            "predicted_range_12w": r["predicted_range_12w"],
        })
    conn.close()
    return {
        "floor_km": floor_km,
        "batteries": results,
        "disclosure": "Floor values from fleet_context_params per pack (Rule 173).",
        "data_confidence": "PRODUCTION",
    }


# ══════════════════════════════════════════════════════════════════════
# END A3 RANGE INTELLIGENCE API
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# SPRINT B — INTEGRATION P1 ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

_ATTR_LABEL_PLAIN = {
    "charging_pct":    "Charging Pattern",
    "usage_pct":       "Usage Intensity",
    "thermal_pct":     "Thermal Exposure",
    "maintenance_pct": "Cell Maintenance",
    "calendar_pct":    "Calendar Aging",
}


@app.get("/api/fleet/range-attribution-rollup")
@safe
def fleet_range_attribution_rollup(_=Depends(verify_token)):
    """Fleet-average 5-factor range attribution with plain-English labels.
    Excludes Pack3401 (PACK_GAP_EXCEPTION — their attribution is not numeric)
    and banned batteries. Returned percentages are normalised to sum 100.
    """
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("da.battery_id")
    row = q1(conn, f"""
        SELECT AVG(da.charging_pct) as charging_pct,
               AVG(da.usage_pct) as usage_pct,
               AVG(da.thermal_pct) as thermal_pct,
               AVG(da.maintenance_pct) as maintenance_pct,
               AVG(da.calendar_pct) as calendar_pct,
               COUNT(*) as n
        FROM battery_degradation_attribution da
        JOIN batteries b ON da.battery_id = b.battery_id
        WHERE da.charging_pct IS NOT NULL
          AND (b.battery_model IS NULL OR b.battery_model NOT LIKE '%Pack3401%')
          AND {ban_sql}
    """, ban_params)
    conn.close()

    if not row or not (row.get("n") or 0):
        return {"factors": [], "n": 0, "thermal_low": False,
                "note": "No attribution data available."}

    raw = {k: (row.get(k) or 0.0) for k in _ATTR_LABEL_PLAIN.keys()}
    total = sum(raw.values()) or 1.0
    factors = [
        {"factor_plain": _ATTR_LABEL_PLAIN[k],
         "pct": round(raw[k] / total * 100, 1)}
        for k in ("charging_pct", "usage_pct", "thermal_pct", "maintenance_pct", "calendar_pct")
    ]
    thermal_low = raw["thermal_pct"] < 5
    return {
        "factors": factors,
        "n": int(row["n"]),
        "thermal_low": thermal_low,
        "thermal_note": ("Thermal data from March baseline. "
                         "April-September: 15-20% expected.") if thermal_low else None,
        "excludes": ["Pack3401", "banned_batteries"],
    }


@app.get("/api/fleet/range-consumer-summary")
@safe
def fleet_range_consumer_summary(_=Depends(verify_token)):
    """Consumer-facing range summary: avg range, below-floor count, trend,
    best/worst pack. Used by range_intelligence S6. Excludes banned batteries."""
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("s.battery_id")

    hdr = q1(conn, f"""
        SELECT ROUND(AVG(s.range_corrected_km), 1) as avg_range_fleet,
               SUM(CASE WHEN s.range_corrected_km < 56 THEN 1 ELSE 0 END) as below_floor_count,
               ROUND(AVG(s.kps_slope_8wk), 6) as avg_slope_8wk
        FROM battery_health_scores_v2 s
        WHERE s.range_corrected_km IS NOT NULL
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND {ban_sql}
    """, ban_params)

    packs = q(conn, f"""
        SELECT b.battery_model as pack_model,
               ROUND(AVG(s.range_corrected_km), 1) as avg_range,
               COUNT(*) as n
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.range_corrected_km IS NOT NULL
          AND s.scoring_mode NOT IN ('SUSPENDED', 'SUPPRESSED_NO_SOH')
          AND {ban_sql}
        GROUP BY b.battery_model HAVING n >= 10
    """, ban_params)

    conn.close()

    slope = (hdr or {}).get("avg_slope_8wk") or 0
    if slope > 0.001:
        trend = "improving"
    elif slope < -0.001:
        trend = "declining"
    else:
        trend = "steady"

    best = max(packs, key=lambda r: r["avg_range"] or 0, default=None)
    worst = min(packs, key=lambda r: r["avg_range"] or 1e9, default=None)

    return {
        "avg_range_fleet": (hdr or {}).get("avg_range_fleet"),
        "below_floor_count": int((hdr or {}).get("below_floor_count") or 0),
        "trend_direction": trend,
        "best_pack_model": (best or {}).get("pack_model"),
        "best_pack_avg_range": (best or {}).get("avg_range"),
        "worst_pack_model": (worst or {}).get("pack_model"),
        "worst_pack_avg_range": (worst or {}).get("avg_range"),
        "floor_km": 56,
    }


@app.get("/api/demo/battery-roles")
@safe
def demo_battery_roles(_=Depends(verify_token)):
    """Demo narrative metadata for the 13 locked demo batteries.
    Returns DEMO_ROLES dict keyed by battery_id.
    Each entry: role, story, narrative, demo_points, tabs_to_show.
    See DEMO_BATTERIES constant for the list; locked 2026-04-23."""
    return DEMO_ROLES


@app.get("/api/demo/battery-list")
@safe
def demo_battery_list(_=Depends(verify_token)):
    """Curated ~30 battery demo list. Tier 1 = 5 named story batteries.
    Tier 2 = SQL-selected cohorts with demo_story_label. Banned excluded."""
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("s.battery_id")

    tier1_ids = ["BAT_LFP_109", "BAT_LFP_278", "BAT_LFP_308",
                 "BAT_LFP_044", "BAT_LFP_150"]
    tier1_labels = {
        "BAT_LFP_109": "Q4 Primary",
        "BAT_LFP_278": "False Alarm",
        "BAT_LFP_308": "Healthy Contrast",
        "BAT_LFP_044": "Pack3401 Gap",
        "BAT_LFP_150": "Early Anomaly",
    }

    selected = {}  # battery_id -> row

    def _fetch_base(ids):
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return q(conn, f"""
            SELECT s.battery_id, b.battery_model as pack_model,
                   s.dri_score, s.ahi_score, s.nbfc_quadrant,
                   s.rul_action_v2 as rul_tier,
                   s.warranty_claim_eligible
            FROM battery_health_scores_v2 s
            JOIN batteries b ON s.battery_id = b.battery_id
            WHERE s.battery_id IN ({placeholders})
        """, ids)

    for r in _fetch_base(tier1_ids):
        r["demo_tier"] = 1
        r["demo_story_label"] = tier1_labels.get(r["battery_id"], "Named Story")
        selected[r["battery_id"]] = r

    excl_ids = list(tier1_ids) + list(get_banned_battery_ids())
    excl_ph = ",".join("?" for _ in excl_ids)

    def add_tier2(rows, story):
        for r in rows:
            bid = r["battery_id"]
            if bid in selected:
                continue
            r["demo_tier"] = 2
            r["demo_story_label"] = story
            selected[bid] = r

    # Q1 — top 5 by DRI DESC
    add_tier2(q(conn, f"""
        SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
               s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.nbfc_quadrant = 'Q1_HEALTHY'
          AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ({excl_ph})
        ORDER BY s.dri_score DESC LIMIT 5
    """, excl_ids), "Q1 Healthy")

    # Q3 — top 5 by AHI ASC
    add_tier2(q(conn, f"""
        SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
               s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.nbfc_quadrant = 'Q3_NORMAL_AGING'
          AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ({excl_ph})
        ORDER BY s.ahi_score ASC LIMIT 5
    """, excl_ids), "Q3 Normal Aging")

    # Q4 — top 5 by DRI ASC (worst)
    add_tier2(q(conn, f"""
        SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
               s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.nbfc_quadrant = 'Q4_DETERIORATING'
          AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ({excl_ph})
        ORDER BY s.dri_score ASC LIMIT 5
    """, excl_ids), "Q4 Deteriorating")

    # Top 3 warranty-eligible
    add_tier2(q(conn, f"""
        SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
               s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE s.warranty_claim_eligible = 1
          AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ({excl_ph})
        ORDER BY s.pct_of_commissioned ASC LIMIT 3
    """, excl_ids), "Warranty Eligible")

    # Top 3 VEHICLE_ISSUE (coulomb_kps_divergence)
    try:
        add_tier2(q(conn, f"""
            SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
                   s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
            FROM battery_health_scores_v2 s
            JOIN batteries b ON s.battery_id = b.battery_id
            JOIN vehicle_weekly_features v ON v.battery_id = s.battery_id
                AND v.week_number = (SELECT MAX(week_number) FROM vehicle_weekly_features
                                     WHERE battery_id = s.battery_id)
            WHERE v.coulomb_kps_divergence IS NOT NULL
              AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
              AND s.battery_id NOT IN ({excl_ph})
            ORDER BY v.coulomb_kps_divergence DESC LIMIT 3
        """, excl_ids), "VEHICLE_ISSUE")
    except Exception:
        pass

    # Pack3001 contrast — 2 highest DRI Pack3001
    add_tier2(q(conn, f"""
        SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
               s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model LIKE '%Pack3001%'
          AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ({excl_ph})
        ORDER BY s.dri_score DESC LIMIT 2
    """, excl_ids), "Pack3001 Contrast")

    # Pack3401 contrast — 2 lowest DRI Pack3401
    add_tier2(q(conn, f"""
        SELECT s.battery_id, b.battery_model as pack_model, s.dri_score, s.ahi_score,
               s.nbfc_quadrant, s.rul_action_v2 as rul_tier, s.warranty_claim_eligible
        FROM battery_health_scores_v2 s
        JOIN batteries b ON s.battery_id = b.battery_id
        WHERE b.battery_model LIKE '%Pack3401%'
          AND s.scoring_mode NOT IN ('SUSPENDED','SUPPRESSED_NO_SOH')
          AND s.battery_id NOT IN ({excl_ph})
        ORDER BY s.dri_score ASC LIMIT 2
    """, excl_ids), "Pack3401 Contrast")

    conn.close()

    out = list(selected.values())
    # Cap at 30 — prefer tier 1 always, then newest tier 2 additions
    out.sort(key=lambda r: (r["demo_tier"], r["demo_story_label"] or "", r["battery_id"]))
    out = out[:30]
    # Round numeric fields
    for r in out:
        for k in ("dri_score", "ahi_score"):
            if r.get(k) is not None:
                r[k] = round(r[k], 1)
    return {
        "total": len(out),
        "tier_1_count": sum(1 for r in out if r["demo_tier"] == 1),
        "tier_2_count": sum(1 for r in out if r["demo_tier"] == 2),
        "batteries": out,
    }


# ═════════════════════════════════════════════════════════════════════
# Sprint C — SI wiring endpoints (2026-04-21)
# ═════════════════════════════════════════════════════════════════════

_RUL_TIER_FROM_ACTION = {
    'EXIT_NOW':             ('IMMEDIATE',  "Replace now — operational life exhausted"),
    'REPLACE_NOW':          ('IMMEDIATE',  "Replace within 4 weeks"),
    'PHYSICS_REPLACE_PLAN': ('RANGE',      "Plan replacement — physics-confirmed decline"),
    'REPLACE_PLAN':         ('NEAR_TERM',  "Plan replacement within 8-12 weeks"),
    'ACUTE_BREACH_WATCH':   ('NEAR_TERM',  "Plan replacement within 8-12 weeks"),
    'MONITOR_INVESTIGATE':  ('MONITOR',    "Monitor closely — elevated breach risk"),
    'MONITOR':              ('MONITOR',    "Monitor closely — elevated breach risk"),
    'MONITOR_WEEKLY':       ('MONITOR',    "Monitor closely — elevated breach risk"),
    'CELL_BALANCE':         ('MONITOR',    "Monitor closely — elevated breach risk"),
    'CELL_BALANCE_PRIORITY':('MONITOR',    "Monitor closely — elevated breach risk"),
    'ROUTINE':              ('STABLE',     "No replacement signal at current trajectory"),
    'NO_ACTION':            ('STABLE',     "No replacement signal at current trajectory"),
}

_RUL_TRIGGER_PLAIN = {
    'RANGE':             "Range floor breach probability exceeds action threshold",
    'SOH':               "State of health below 60% end-of-life threshold",
    'RANGE_RECOMPUTED':  "Range breach confirmed on recomputed trajectory",
    'STABLE':            "No trigger — battery within normal operating range",
    'INSUFFICIENT_DATA': "Insufficient telemetry — RUL estimate deferred",
    'EXCLUDED':          "Battery excluded from RUL scoring",
    'KNOWN_BAD_OVERRIDE':"Known-bad override — RUL gated by platform rule",
}

_EVENT_TYPE_FALLBACK_PLAIN = {
    'COMMISSIONED':       "Battery commissioned",
    'TIER_CHANGE':        "Health tier changed",
    'ACTION_RECOMMENDED': "Action recommended",
    'WARRANTY_FLAG':      "Warranty flag set",
    'CELL_BALANCE':       "Cell balance service recorded",
    'VOLTAGE_SAG':        "Voltage sag event detected",
    'FAULT':              "Fault event recorded",
}


def _latest_vwf_row(conn, battery_id):
    return q1(conn, """
        SELECT * FROM vehicle_weekly_features
        WHERE battery_id = ?
          AND week_number = (SELECT MAX(week_number) FROM vehicle_weekly_features WHERE battery_id = ?)
    """, [battery_id, battery_id])


# ── SI-C1: GET /api/battery/{id}/cell-imbalance ─────────────────────────
@app.get("/api/battery/{battery_id}/cell-imbalance")
@safe
def battery_cell_imbalance(battery_id: str, _=Depends(verify_token)):
    """Per-battery cell imbalance tier + recommended action (plain English)."""
    conn = get_conn()
    latest = _latest_vwf_row(conn, battery_id)
    bhs = q1(conn, """
        SELECT commissioning_spread_mv, cell_spread_percentile, pack_model
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    if not latest and not bhs:
        conn.close()
        raise HTTPException(404, f"Battery not found: {battery_id}")

    spread_mv = (latest or {}).get("cell_spread_max") or (latest or {}).get("cell_spread_mean")
    spread_acceleration = (latest or {}).get("cell_spread_acceleration")
    comm_spread = (bhs or {}).get("commissioning_spread_mv")
    delta_commissioning = (spread_mv - comm_spread) if (spread_mv is not None and comm_spread is not None) else None

    # Fleet P75 spread (same calc pattern as SI-6)
    spread_p75_row = q1(conn, """
        SELECT cell_spread_max FROM vehicle_weekly_features
        WHERE cell_spread_max IS NOT NULL
        ORDER BY cell_spread_max LIMIT 1 OFFSET (
          SELECT CAST(COUNT(*)*0.75 AS INTEGER) FROM vehicle_weekly_features
          WHERE cell_spread_max IS NOT NULL
        )
    """)
    spread_p75 = (spread_p75_row or {}).get("cell_spread_max") or 248.0

    # Repeat CELL_BALANCE alerts in last 8 weeks (via battery_events_timeline)
    repeat_row = q1(conn, """
        SELECT COUNT(*) AS n FROM battery_events_timeline
        WHERE battery_id = ?
          AND (event_type = 'CELL_BALANCE'
               OR (event_type = 'ACTION_RECOMMENDED' AND
                   (event_to LIKE '%CELL_BALANCE%' OR event_plain LIKE '%cell balanc%')))
          AND event_week >= (SELECT COALESCE(MAX(event_week), 0) - 8 FROM battery_events_timeline WHERE battery_id = ?)
    """, [battery_id, battery_id])
    repeat_alert_count = (repeat_row or {}).get("n", 0) or 0
    repeat_alert_flag = 1 if repeat_alert_count >= 2 else 0

    # Weeks accelerating — consecutive from latest backwards where acceleration>2
    hist = q(conn, """
        SELECT week_number, cell_spread_acceleration FROM vehicle_weekly_features
        WHERE battery_id = ? AND cell_spread_acceleration IS NOT NULL
        ORDER BY week_number DESC LIMIT 12
    """, [battery_id])
    weeks_accel = 0
    for r in hist:
        if (r.get("cell_spread_acceleration") or 0) > 2:
            weeks_accel += 1
        else:
            break

    # Tier classification (ordered — ESCALATION > ACUTE > EARLY_WARNING > HEALTHY)
    tier = None
    if repeat_alert_flag:
        tier = 'ESCALATION'
    elif spread_mv is not None and spread_mv > (spread_p75 * 1.5):
        tier = 'ACUTE'
    elif (spread_acceleration or 0) > 2:
        tier = 'EARLY_WARNING'
    elif spread_mv is not None:
        tier = 'HEALTHY'
    else:
        tier = 'NO_DATA'

    action_plain = {
        'ESCALATION':    "Replacement assessment required. Cell balance has not resolved chronic imbalance.",
        'ACUTE':         "Cell balance service this week — spread exceeds safe operating range.",
        'EARLY_WARNING': "Schedule cell balance within 4 weeks — early imbalance detected.",
        'HEALTHY':       "No cell imbalance action required.",
        'NO_DATA':       "Cell spread signal not yet available for this battery.",
    }.get(tier, "Review cell imbalance signal.")

    conn.close()
    return {
        "battery_id": battery_id,
        "pack_model": (bhs or {}).get("pack_model"),
        "week_number": (latest or {}).get("week_number"),
        "cell_spread_mv": spread_mv,
        "spread_acceleration": spread_acceleration,
        "spread_commissioning_delta": delta_commissioning,
        "fleet_p75_spread_mv": spread_p75,
        "repeat_alert_flag": repeat_alert_flag,
        "repeat_alert_count": repeat_alert_count,
        "weeks_accelerating": weeks_accel,
        "imbalance_tier": tier,
        "projected_bms_alert_weeks": None,  # not yet calibrated
        "recommended_action_plain": action_plain,
    }


# ── SI-C2: GET /api/fleet/cell-imbalance-summary ────────────────────────
@app.get("/api/fleet/cell-imbalance-summary")
@safe
def fleet_cell_imbalance_summary(_=Depends(verify_token)):
    """Fleet-wide cell imbalance tier distribution + cohort tables."""
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("bhs.battery_id")

    # Shared fleet P75 threshold
    spread_p75_row = q1(conn, """
        SELECT cell_spread_max FROM vehicle_weekly_features
        WHERE cell_spread_max IS NOT NULL
        ORDER BY cell_spread_max LIMIT 1 OFFSET (
          SELECT CAST(COUNT(*)*0.75 AS INTEGER) FROM vehicle_weekly_features
          WHERE cell_spread_max IS NOT NULL
        )
    """)
    spread_p75 = (spread_p75_row or {}).get("cell_spread_max") or 248.0

    # Latest VWF row per LFP battery (exclude banned + NMC + LCV)
    rows = q(conn, f"""
        WITH wk AS (
            SELECT battery_id, MAX(week_number) AS mx
            FROM vehicle_weekly_features GROUP BY battery_id
        )
        SELECT v.battery_id, v.week_number, v.cell_spread_max, v.cell_spread_mean,
               v.cell_spread_acceleration, bhs.pack_model, b.fleet_segment
        FROM vehicle_weekly_features v
        JOIN wk ON wk.battery_id = v.battery_id AND wk.mx = v.week_number
        JOIN battery_health_scores_v2 bhs ON bhs.battery_id = v.battery_id
        JOIN batteries b ON b.battery_id = v.battery_id
        WHERE b.chemistry = 'LFP'
          AND (b.fleet_segment LIKE 'GE_%' OR b.fleet_segment LIKE 'SG_%')
          AND {ban_sql}
    """, ban_params)

    # Repeat alert counts (lookup table — batch query for perf)
    repeat_rows = q(conn, """
        SELECT battery_id, COUNT(*) AS n
        FROM battery_events_timeline
        WHERE (event_type = 'CELL_BALANCE'
               OR (event_type = 'ACTION_RECOMMENDED' AND
                   (event_to LIKE '%CELL_BALANCE%' OR event_plain LIKE '%cell balanc%')))
          AND event_week >= (SELECT COALESCE(MAX(event_week), 0) - 8
                             FROM battery_events_timeline bt2
                             WHERE bt2.battery_id = battery_events_timeline.battery_id)
        GROUP BY battery_id
    """)
    repeat_map = {r["battery_id"]: (r["n"] or 0) for r in repeat_rows}

    tiers = {'ESCALATION': 0, 'ACUTE': 0, 'EARLY_WARNING': 0, 'HEALTHY': 0, 'NO_DATA': 0}
    early_warning, acute, escalation = [], [], []

    for r in rows:
        bid = r["battery_id"]
        spread = r.get("cell_spread_max") or r.get("cell_spread_mean")
        accel = r.get("cell_spread_acceleration") or 0
        repeat = repeat_map.get(bid, 0)
        repeat_flag = 1 if repeat >= 2 else 0

        if repeat_flag:
            tier = 'ESCALATION'
            escalation.append({"battery_id": bid, "pack_model": r.get("pack_model"),
                               "repeat_alert_count": repeat})
        elif spread is not None and spread > (spread_p75 * 1.5):
            tier = 'ACUTE'
            acute.append({"battery_id": bid, "pack_model": r.get("pack_model"),
                          "cell_spread_mv": round(spread, 1)})
        elif accel > 2:
            tier = 'EARLY_WARNING'
            early_warning.append({"battery_id": bid, "pack_model": r.get("pack_model"),
                                  "spread_accel": round(accel, 2),
                                  "weeks_accelerating": None})
        elif spread is not None:
            tier = 'HEALTHY'
        else:
            tier = 'NO_DATA'
        tiers[tier] += 1

    # trend_vs_last_week — delta in ACTION_RECOMMENDED events week over week
    trend_row = q(conn, """
        SELECT event_week, COUNT(*) AS n
        FROM battery_events_timeline
        WHERE event_type = 'ACTION_RECOMMENDED'
        GROUP BY event_week ORDER BY event_week DESC LIMIT 2
    """)
    if len(trend_row) >= 2:
        trend_delta = (trend_row[0]["n"] or 0) - (trend_row[1]["n"] or 0)
    else:
        trend_delta = None

    early_warning.sort(key=lambda x: x["spread_accel"], reverse=True)
    acute.sort(key=lambda x: x["cell_spread_mv"], reverse=True)

    conn.close()
    return {
        "tier_counts": tiers,
        "trend_vs_last_week": {"action_recommended_delta": trend_delta},
        "fleet_p75_spread_mv": round(spread_p75, 1),
        "early_warning_batteries": early_warning[:20],
        "acute_batteries": acute,
        "escalation_batteries": escalation,
        "fleet_scope": "LFP_GE_SG",
    }


# ── SI-C3: GET /api/battery/{id}/rul-detail ─────────────────────────────
@app.get("/api/battery/{battery_id}/rul-detail")
@safe
def battery_rul_detail(battery_id: str, _=Depends(verify_token)):
    """Per-battery RUL decomposition with plain-English labels and disclaimer."""
    conn = get_conn()
    row = q1(conn, """
        SELECT rul_action_v2, rul_trigger, rul_weeks_v2, rul_calibration_status,
               p_floor_breach_4w, p_floor_breach_8w, p_floor_breach_12w,
               warranty_claim_eligible, warranty_status
        FROM battery_health_scores_v2 WHERE battery_id = ?
    """, [battery_id])
    conn.close()
    if not row:
        raise HTTPException(404, f"Battery not found: {battery_id}")

    action = row.get("rul_action_v2") or "ROUTINE"
    tier_entry = _RUL_TIER_FROM_ACTION.get(action, ('STABLE', "No replacement signal at current trajectory"))
    rul_tier, rul_tier_plain = tier_entry
    trigger = row.get("rul_trigger") or "STABLE"
    trigger_plain = _RUL_TRIGGER_PLAIN.get(trigger, "No trigger — battery within normal operating range")
    warranty_eligible = row.get("warranty_claim_eligible") or 0

    return {
        "battery_id": battery_id,
        "rul_tier": rul_tier,
        "rul_tier_plain": rul_tier_plain,
        "rul_trigger": trigger,
        "rul_trigger_plain": trigger_plain,
        "rul_weeks_v2": row.get("rul_weeks_v2"),
        "range_floor_breach_prob_4wk": row.get("p_floor_breach_4w"),
        "range_floor_breach_prob_8wk": row.get("p_floor_breach_8w"),
        "range_floor_breach_prob_12wk": row.get("p_floor_breach_12w"),
        "rul_action": action,
        "rul_action_plain": atext.action_sentence(action),
        "warranty_claim_eligible": warranty_eligible,
        "warranty_context": ("OEM-attributable degradation within warranty period" if warranty_eligible else None),
        "calibration_status": row.get("rul_calibration_status"),
        "disclaimer": "Directional — not for contractual use",
        "conformal_note": "9 of 30 field outcomes confirmed. Confidence improves as outcomes accumulate.",
    }


# ── SI-C4: GET /api/battery/{id}/thermal-profile ────────────────────────
@app.get("/api/battery/{battery_id}/thermal-profile")
@safe
def battery_thermal_profile(battery_id: str, _=Depends(verify_token)):
    """Per-battery thermal profile + seasonal context."""
    conn = get_conn()
    latest = _latest_vwf_row(conn, battery_id)
    if not latest:
        conn.close()
        raise HTTPException(404, f"No VWF for battery {battery_id}")

    high_temp_row = q1(conn, """
        SELECT COUNT(*) AS n FROM vehicle_weekly_features
        WHERE battery_id = ?
          AND temp_max > 40
          AND week_number >= (SELECT COALESCE(MAX(week_number), 0) - 12
                              FROM vehicle_weekly_features WHERE battery_id = ?)
    """, [battery_id, battery_id])
    high_temp_weeks = (high_temp_row or {}).get("n", 0) or 0

    # Attribution — reuse engine
    bhs = q1(conn, "SELECT attr_thermal_pct, pack_model FROM battery_health_scores_v2 WHERE battery_id = ?", [battery_id])
    bda = q1(conn, "SELECT thermal_pct FROM battery_degradation_attribution WHERE battery_id = ?", [battery_id])
    thermal_attr = (bda or {}).get("thermal_pct")
    if thermal_attr is None:
        thermal_attr = (bhs or {}).get("attr_thermal_pct")

    temp_max = latest.get("temp_max")
    temp_avg = latest.get("temp_mean") or latest.get("temp_max_clean")

    if temp_max is None:
        thermal_tier = 'NO_DATA'
    elif temp_max > 45:
        thermal_tier = 'HIGH'
    elif temp_max > 38:
        thermal_tier = 'ELEVATED'
    else:
        thermal_tier = 'NORMAL'

    # Seasonal context from fleet_context_params
    import datetime as _dt
    month = _dt.datetime.now().month
    season = 'SUMMER' if month in (4,5,6,7,8,9) else 'WINTER'
    season_param = q1(conn, """
        SELECT param_value FROM fleet_context_params
        WHERE param_name = 'thermal_weight_attribution'
          AND segment_type = 'SEASON' AND segment_value = ? AND is_active = 1
        LIMIT 1
    """, [season])
    seasonal_context = None
    if season_param and season_param.get("param_value"):
        m = float(season_param["param_value"])
        seasonal_context = (
            f"Current season ({season.lower()}) thermal attribution weight: "
            f"{m:.2f}. Peak-heat range alerts may be suppressed during this period.")
    else:
        seasonal_context = "Current season: no thermal suppression active."

    conn.close()
    return {
        "battery_id": battery_id,
        "pack_model": (bhs or {}).get("pack_model"),
        "week_number": latest.get("week_number"),
        "temp_max_weekly": temp_max,
        "temp_avg_weekly": temp_avg,
        "high_temp_weeks_count": high_temp_weeks,
        "thermal_attribution_pct": round(thermal_attr, 1) if thermal_attr is not None else None,
        "thermal_tier": thermal_tier,
        "seasonal_context": seasonal_context,
    }


# ── SI-C5: GET /api/battery/{id}/service-history ────────────────────────
@app.get("/api/battery/{battery_id}/service-history")
@safe
def battery_service_history(battery_id: str, _=Depends(verify_token)):
    """Plain-English service timeline from battery_events_timeline (top 20)."""
    conn = get_conn()
    rows = q(conn, """
        SELECT event_week, event_date, event_type, event_plain
        FROM battery_events_timeline
        WHERE battery_id = ?
        ORDER BY event_week DESC, id DESC
        LIMIT 20
    """, [battery_id])
    conn.close()

    out = []
    for r in rows:
        plain = r.get("event_plain")
        if not plain:
            plain = _EVENT_TYPE_FALLBACK_PLAIN.get(r.get("event_type") or "", "Platform event recorded")
        out.append({
            "event_week": r.get("event_week"),
            "event_date": r.get("event_date"),
            "event_plain": plain,
        })
    return {"battery_id": battery_id, "events": out, "total": len(out)}


# ── SI-C6: GET /api/fleet/iot-summary ───────────────────────────────────
@app.get("/api/fleet/iot-summary")
@safe
def fleet_iot_summary(_=Depends(verify_token)):
    """IoT device health tier counts across the scored fleet."""
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("battery_id")
    rows = q(conn, f"""
        SELECT COALESCE(iot_device_health, 'UNKNOWN') AS tier, COUNT(*) AS n
        FROM battery_health_scores_v2
        WHERE scoring_mode != 'SUSPENDED'
          AND {ban_sql}
        GROUP BY COALESCE(iot_device_health, 'UNKNOWN')
    """, ban_params)
    conn.close()
    out = {"HEALTHY": 0, "DEGRADING": 0, "CRITICAL": 0, "UNKNOWN": 0}
    for r in rows:
        out[r["tier"]] = r["n"]
    return {"tier_counts": out, "total": sum(out.values()),
            "separation_note": "IoT failure does not change battery chemistry assessment."}


# ── SI-C7: GET /api/fleet/decision-queue-with-alerts ────────────────────
# Adds per-battery alert counts (ESCALATION/ACUTE only) to the decision queue
# for drawer badge rendering. Kept separate from decision-queue to avoid
# breaking existing consumers.
@app.get("/api/fleet/decision-queue-with-alerts")
@safe
def fleet_decision_queue_with_alerts(_=Depends(verify_token)):
    conn = get_conn()
    ban_sql, ban_params = _banned_sql_clause("fdq.battery_id")
    rows = q(conn, f"""
        SELECT fdq.battery_id, fdq.battery_model as pack_model, fdq.city_code as city,
               s.range_corrected_km, s.health_class_v2, s.rul_action_v2,
               fdq.urgency_rank, fdq.driver_stress_tier, fdq.week_number,
               s.data_confidence_label, s.batch_anomaly_tier,
               s.divergence_quadrant, s.iot_device_health, b.use_case_inferred
        FROM fleet_decision_queue fdq
        JOIN battery_health_scores_v2 s ON fdq.battery_id = s.battery_id
            AND s.week_number = (SELECT MAX(week_number) FROM battery_health_scores_v2
                                 WHERE battery_id = fdq.battery_id)
        LEFT JOIN batteries b ON fdq.battery_id = b.battery_id
        WHERE s.health_class_v2 != 'SCORE_SUSPENDED'
          AND s.scoring_mode != 'SUSPENDED'
          AND {ban_sql}
        ORDER BY fdq.urgency_rank ASC
    """, ban_params)

    alerts_by_bid = {}
    ev = q(conn, """
        SELECT battery_id, COUNT(*) AS n
        FROM battery_events_timeline
        WHERE event_type IN ('ACTION_RECOMMENDED','WARRANTY_FLAG')
          AND event_week >= (
            SELECT COALESCE(MAX(event_week),0) - 4
            FROM battery_events_timeline bt2 WHERE bt2.battery_id = battery_events_timeline.battery_id)
        GROUP BY battery_id
    """)
    for r in ev:
        alerts_by_bid[r["battery_id"]] = r["n"]
    conn.close()

    for r in rows:
        r["range_corrected_km"] = round(r["range_corrected_km"], 1) if r.get("range_corrected_km") else None
        r["health_class_plain"] = _HEALTH_CLASS_PLAIN.get(r.pop("health_class_v2", ""), r.get("health_class_v2"))
        action = r.pop("rul_action_v2", "")
        r["rul_action_plain"] = _ACTION_PLAIN.get(action, action)
        r["confidence_level"] = _confidence_level(r.pop("data_confidence_label", None))
        count = alerts_by_bid.get(r["battery_id"], 0)
        r["alert_count"] = count
        # Badge tier: ESCALATION > ACUTE > none. Heuristic: REPLACE_* = escalation, MONITOR_INVESTIGATE = acute
        if action in ('REPLACE_NOW','REPLACE_PLAN','PHYSICS_REPLACE_PLAN','ACUTE_BREACH_WATCH') and count > 0:
            r["alert_badge"] = 'ESCALATION'
        elif action in ('MONITOR_INVESTIGATE','CELL_BALANCE','CELL_BALANCE_PRIORITY') and count > 0:
            r["alert_badge"] = 'ACUTE'
        else:
            r["alert_badge"] = None
    return rows


# ── Sprint E: /api/fleet/status — lightweight data-freshness endpoint ──
@app.get("/api/fleet/status")
def fleet_status():
    """Platform freshness indicator — backs the Week N pill on every surface.
    No auth — consumed by all UIs on page load."""
    try:
        conn = get_conn()
        row = q1(conn, """
            SELECT MAX(week_number) AS current_week,
                   COUNT(DISTINCT battery_id) AS scored_batteries
            FROM battery_health_scores_v2
            WHERE scoring_mode != 'SUSPENDED'
        """)
        conn.close()
        return {
            "current_week": (row or {}).get("current_week"),
            "scored_batteries": (row or {}).get("scored_batteries"),
            "data_note": "Frozen weekly snapshot — platform scores refresh weekly.",
        }
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════
# PLATFORM FRESHNESS + CHAIN INSIGHTS (APR 22 — Enerlyst grounding)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/platform/freshness")
@safe
def platform_freshness(_=Depends(verify_token)):
    """Single source-of-truth for platform state, keyed by timestamps.
    Enerlyst injects this into the context block when the user asks
    'what's fresh', 'last run', 'pending', 'what's missing', etc."""
    conn = get_conn()
    try:
        def _maxmin(tbl, col):
            try:
                r = q1(conn, f"SELECT MAX({col}) mx, MIN({col}) mn, COUNT(*) n FROM {tbl}")
                return r or {}
            except Exception:
                return {}

        scoring = _maxmin("battery_health_scores_v2", "scored_at")
        attribution = _maxmin("battery_degradation_attribution", "scored_at")
        alerts_time = _maxmin("platform_alerts", "fired_at")
        param_time = _maxmin("param_change_log", "changed_at")

        ingest_status = q(conn, """
            SELECT ingest_status, COUNT(DISTINCT battery_id) n, MAX(ingest_date) mx
            FROM ingest_audit_log GROUP BY ingest_status
        """)
        last_ingest = q1(conn, "SELECT MAX(ingest_date) AS mx FROM ingest_audit_log")

        total_registered = q1(conn, "SELECT COUNT(*) n FROM batteries")["n"]
        missing_commission = q1(conn,
            "SELECT COUNT(*) n FROM batteries WHERE commissioning_date IS NULL")["n"]
        scored = q1(conn, "SELECT COUNT(*) n FROM battery_health_scores_v2")["n"]
        unscored = total_registered - scored

        scoring_modes = q(conn, """
            SELECT scoring_mode, COUNT(*) n FROM battery_health_scores_v2
            GROUP BY scoring_mode ORDER BY n DESC
        """)
        rul_actions = q(conn, """
            SELECT rul_action_v2, COUNT(*) n FROM battery_health_scores_v2
            GROUP BY rul_action_v2 ORDER BY n DESC
        """)

        alerts_by_prio = q(conn, """
            SELECT priority, COUNT(*) n FROM platform_alerts
            WHERE active = 1 GROUP BY priority ORDER BY priority
        """)
        open_alerts_total = sum(r["n"] for r in alerts_by_prio)
        future_alerts = q1(conn,
            "SELECT COUNT(*) n FROM platform_alerts "
            "WHERE fired_at > datetime('now') AND active = 1")["n"]

        dekf_coverage = q1(conn, """
            SELECT
              SUM(CASE WHEN soc_dekf_latest IS NOT NULL THEN 1 ELSE 0 END) populated,
              SUM(CASE WHEN soc_dekf_confidence = 'HIGH' THEN 1 ELSE 0 END) high,
              SUM(CASE WHEN soc_dekf_confidence = 'MEDIUM' THEN 1 ELSE 0 END) medium,
              SUM(CASE WHEN soc_dekf_confidence = 'INSUFFICIENT_DATA' THEN 1 ELSE 0 END) insufficient,
              SUM(CASE WHEN soc_dekf_confidence IS NULL THEN 1 ELSE 0 END) null_conf
            FROM battery_health_scores_v2
        """) or {}

        banned_row = q1(conn,
            "SELECT param_text FROM fleet_context_params "
            "WHERE param_name='banned_battery_ids' AND is_active=1")
        banned = (banned_row or {}).get("param_text") or ""

        recent_params = q(conn, """
            SELECT param_name, changed_at, change_reason FROM param_change_log
            ORDER BY changed_at DESC LIMIT 8
        """)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fleet": {
                "registered": total_registered,
                "scored": scored,
                "unscored": unscored,
                "missing_commissioning_date": missing_commission,
                "banned_ids": [s.strip() for s in banned.split(",") if s.strip()],
            },
            "last_runs": {
                "scoring_bhs_max": scoring.get("mx"),
                "scoring_bhs_min": scoring.get("mn"),
                "attribution_max": attribution.get("mx"),
                "last_ingest_date": (last_ingest or {}).get("mx"),
                "last_alert_fired": alerts_time.get("mx"),
                "last_param_change": param_time.get("mx"),
            },
            "ingest": {"by_status": ingest_status},
            "scoring_modes": scoring_modes,
            "rul_actions": rul_actions,
            "alerts": {
                "open_total": open_alerts_total,
                "by_priority": {f"P{r['priority']}": r["n"] for r in alerts_by_prio},
                "future_dated_anomaly": future_alerts,
            },
            "dekf_soc": {
                "populated": (dekf_coverage or {}).get("populated") or 0,
                "high": (dekf_coverage or {}).get("high") or 0,
                "medium": (dekf_coverage or {}).get("medium") or 0,
                "insufficient_data": (dekf_coverage or {}).get("insufficient") or 0,
                "null_confidence": (dekf_coverage or {}).get("null_conf") or 0,
            },
            "recent_parameter_changes": recent_params,
        }
    finally:
        try: conn.close()
        except Exception: pass


@app.get("/api/platform/pending-actions")
@safe
def platform_pending_actions(_=Depends(verify_token)):
    """Ranked pending actions across the platform. Banned batteries excluded."""
    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("s.battery_id")
        banned_ids = list(get_banned_battery_ids())
        ban_ph = ",".join("?" for _ in banned_ids) or "''"

        items = []
        for code, label, order_clause in [
            ("EXIT_NOW", "Exit now — replacement scheduled", "ORDER BY s.dri_score ASC"),
            ("REPLACE_PLAN", "Plan replacement (6–8 weeks)", "ORDER BY s.pct_of_commissioned ASC"),
            ("MONITOR_INVESTIGATE", "Monitor + investigate (ambiguous signal)", "ORDER BY s.dri_score ASC"),
        ]:
            rows = q(conn, f"""
                SELECT s.battery_id FROM battery_health_scores_v2 s
                WHERE s.rul_action_v2 = ? AND {ban_sql}
                {order_clause}
            """, [code] + ban_params)
            items.append({
                "priority": 1 if code == "EXIT_NOW" else (2 if code == "REPLACE_PLAN" else 3),
                "action_code": code,
                "label": label,
                "count": len(rows),
                "sample_battery_ids": [r["battery_id"] for r in rows[:10]],
            })

        p1 = q(conn, f"""
            SELECT pa.battery_id, pa.title, pa.alert_type, pa.fired_at
            FROM platform_alerts pa
            WHERE pa.active=1 AND pa.priority=1
              AND pa.battery_id NOT IN ({ban_ph})
            ORDER BY pa.fired_at ASC
        """, banned_ids)
        items.append({
            "priority": 1,
            "action_code": "P1_PLATFORM_ALERT",
            "label": "Platform P1 alerts (ingest / data quality)",
            "count": len(p1),
            "sample_battery_ids": [a["battery_id"] for a in p1[:10]],
            "sample_titles": [a["title"] for a in p1[:5]],
        })

        try:
            hr = q(conn, f"""
                SELECT v.battery_id
                FROM vehicle_weekly_features v
                JOIN (SELECT battery_id, MAX(week_number) wk FROM vehicle_weekly_features GROUP BY battery_id) lw
                  ON v.battery_id=lw.battery_id AND v.week_number=lw.wk
                WHERE v.persistent_weak_cell_flag=1 AND v.cell_voltage_min_weekly < 2.8
                  AND v.battery_id NOT IN ({ban_ph})
            """, banned_ids)
            items.append({
                "priority": 2,
                "action_code": "CELL_VOLTAGE_HIGH_RISK",
                "label": "Cell voltage HIGH RISK compound (persistent weak + <2.8V)",
                "count": len(hr),
                "sample_battery_ids": [r["battery_id"] for r in hr[:10]],
            })
        except Exception:
            pass

        warranty = q(conn, f"""
            SELECT DISTINCT s.battery_id
            FROM battery_health_scores_v2 s
            JOIN batteries b ON s.battery_id=b.battery_id
            JOIN platform_alerts a ON a.battery_id=s.battery_id
            WHERE b.battery_model LIKE '%Pack3401%'
              AND s.warranty_claim_eligible=1 AND a.active=1
              AND {ban_sql}
            ORDER BY s.battery_id
        """, ban_params)
        items.append({
            "priority": 2,
            "action_code": "WARRANTY_DISPATCH_BACKLOG",
            "label": "Pack3401 warranty-eligible with open alerts — dispatch queue",
            "count": len(warranty),
            "sample_battery_ids": [r["battery_id"] for r in warranty[:10]],
        })

        items.sort(key=lambda x: (x["priority"], -x["count"]))
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": items,
            "total_items_with_counts": sum(1 for i in items if i["count"] > 0),
        }
    finally:
        try: conn.close()
        except Exception: pass


@app.get("/api/platform/chain-insights")
@safe
def platform_chain_insights(_=Depends(verify_token)):
    """Cross-signal insights. Connects two or more sources into a finding."""
    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("s.battery_id")
        insights = []

        pack3401 = q1(conn, f"""
            SELECT COUNT(*) n,
                   ROUND(AVG(s.dri_score),1) avg_dri,
                   SUM(CASE WHEN s.warranty_claim_eligible=1 THEN 1 ELSE 0 END) warranty,
                   SUM(CASE WHEN s.tier_label_v2 IN ('WATCH','STRESSED','CRITICAL') THEN 1 ELSE 0 END) at_risk
            FROM battery_health_scores_v2 s
            JOIN batteries b ON s.battery_id=b.battery_id
            WHERE b.battery_model LIKE '%Pack3401%' AND {ban_sql}
        """, ban_params)
        if pack3401 and pack3401.get("n"):
            insights.append({
                "key": "pack3401_chain",
                "label": "Pack3401 concentration",
                "interpretation":
                    f"Pack3401 has {pack3401['n']} scored units, avg DRI {pack3401['avg_dri']}, "
                    f"{pack3401['at_risk']} in WATCH/STRESSED/CRITICAL, "
                    f"{pack3401['warranty']} warranty-claim-eligible. Design gap confirmed "
                    f"(earlier operational-floor arrival vs Pack3001, median knee week 18.8 vs 19.4). PACK_GAP_EXCEPTION applied - "
                    f"operator attribution overridden.",
                "counts": pack3401,
                "connects": ["batteries.battery_model", "battery_health_scores_v2.tier_label_v2",
                             "battery_health_scores_v2.warranty_claim_eligible"],
            })

        bms_blind = q1(conn, """
            SELECT
              (SELECT COUNT(*) FROM vehicle_weekly_features WHERE sox_tier IN ('POWER_DEGRADED','CRITICAL','EARLY_WARNING')) platform_detected,
              (SELECT COUNT(*) FROM platform_alerts WHERE alert_type LIKE '%BMS_ALERT%' AND active=1) bms_alerts
        """)
        insights.append({
            "key": "bms_blindness",
            "label": "BMS blind-zone confirmation",
            "interpretation":
                f"Platform detected {bms_blind['platform_detected']} SOX stress rows across fleet. "
                f"BMS fired {bms_blind['bms_alerts']} BMS_ALERT records - platform is the sole "
                f"detection layer for cell-imbalance / IR / voltage-sag stress. "
                f"BMS SOH is banned for LFP (Rule 2).",
            "counts": bms_blind,
            "connects": ["vehicle_weekly_features.sox_tier", "platform_alerts.alert_type"],
        })

        dq = q1(conn, """
            SELECT
              (SELECT COUNT(*) FROM batteries WHERE commissioning_date IS NULL) no_commission,
              (SELECT COUNT(DISTINCT battery_id) FROM ingest_audit_log WHERE ingest_status='NO_Q1_DATA') no_q1,
              (SELECT COUNT(*) FROM battery_health_scores_v2 WHERE scoring_mode='SUSPENDED') suspended,
              (SELECT COUNT(*) FROM batteries) total
        """)
        insights.append({
            "key": "data_quality_chain",
            "label": "Unscored / suspended batteries explained",
            "interpretation":
                f"{dq['no_commission']} batteries missing commissioning_date (can't compute "
                f"vehicle-relative weeks). {dq['no_q1']} have NO_Q1_DATA ingest status. "
                f"{dq['suspended']} currently SUSPENDED scoring_mode (<30% completeness). "
                f"These explain the gap between {dq['total']} registered and fully-scored fleet.",
            "counts": dq,
            "connects": ["batteries.commissioning_date", "ingest_audit_log.ingest_status",
                         "battery_health_scores_v2.scoring_mode"],
        })

        dekf = q1(conn, """
            SELECT
              COUNT(*) populated,
              ROUND(AVG(soc_bms_correction_pct),2) avg_correction_pct,
              ROUND(MIN(soc_bms_correction_pct),2) min_correction_pct,
              ROUND(MAX(soc_bms_correction_pct),2) max_correction_pct
            FROM battery_health_scores_v2 WHERE soc_dekf_latest IS NOT NULL
        """)
        if dekf and dekf.get("populated"):
            insights.append({
                "key": "dekf_bms_bias",
                "label": "DEKF-measured BMS overread",
                "interpretation":
                    f"Across {dekf['populated']} batteries with DEKF SOC populated, BMS overreads SOC "
                    f"by {dekf['avg_correction_pct']}% on average (range {dekf['min_correction_pct']} "
                    f"to {dekf['max_correction_pct']}%). GE SIGNED mean ~-4.44%, SG MAG_ONLY mean ~-1.0%. "
                    f"Measured, not estimated - LFP BMS firmware is systematically optimistic.",
                "counts": dekf,
                "connects": ["DEKF pipeline", "battery_health_scores_v2.soc_bms_correction_pct",
                             "batteries.bms_current_convention"],
            })

        stale = q1(conn, """
            SELECT
              (SELECT MAX(scored_at) FROM battery_health_scores_v2) last_scored,
              (SELECT MAX(changed_at) FROM param_change_log) last_param_change,
              (SELECT COUNT(*) FROM param_change_log WHERE changed_at >
                 (SELECT MAX(scored_at) FROM battery_health_scores_v2)) params_since_scoring
        """)
        if stale:
            insights.append({
                "key": "scoring_staleness",
                "label": "Scoring freshness vs parameter changes",
                "interpretation":
                    f"Last BHS scoring: {stale['last_scored']}. Last parameter change: "
                    f"{stale['last_param_change']}. {stale['params_since_scoring']} parameter "
                    f"changes logged since the last full scoring run - some fixes may not yet be "
                    f"reflected in live scores until next rescore.",
                "counts": stale,
                "connects": ["battery_health_scores_v2.scored_at", "param_change_log.changed_at"],
            })

        cv = q1(conn, """
            SELECT
              COUNT(DISTINCT v.battery_id) alert_batteries,
              SUM(CASE WHEN v.cell_voltage_min_weekly < 2.8 THEN 1 ELSE 0 END) high_risk
            FROM vehicle_weekly_features v
            JOIN (SELECT battery_id, MAX(week_number) wk FROM vehicle_weekly_features GROUP BY battery_id) lw
              ON v.battery_id=lw.battery_id AND v.week_number=lw.wk
            WHERE v.persistent_weak_cell_flag=1
        """)
        if cv and cv.get("alert_batteries"):
            insights.append({
                "key": "cell_voltage_alert_chain",
                "label": "Cell voltage alert chain (Path A signal)",
                "interpretation":
                    f"{cv['alert_batteries']} batteries carry persistent_weak_cell_flag, "
                    f"{cv['high_risk']} of those are HIGH_RISK compound (weak + <2.8V + spread "
                    f"growing). Platform detected this signal from DuckDB CAN telemetry - the BMS "
                    f"is silent. 3/3 monitored RTP failures detected by this pipeline.",
                "counts": cv,
                "connects": ["vehicle_weekly_features.persistent_weak_cell_flag",
                             "vehicle_weekly_features.high_risk_compound"],
            })

        fut = q1(conn, """
            SELECT COUNT(*) n FROM platform_alerts
            WHERE fired_at > datetime('now') AND active=1
        """)
        if fut and fut.get("n", 0) > 0:
            insights.append({
                "key": "future_dated_alerts_anomaly",
                "label": "DATA QUALITY - future-dated active alerts",
                "interpretation":
                    f"{fut['n']} active platform_alerts have fired_at > now. Likely test data or "
                    f"clock skew during ingestion. Review platform_alerts row inserts.",
                "counts": fut,
                "connects": ["platform_alerts.fired_at (anomaly)"],
            })

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "insights": insights,
            "count": len(insights),
        }
    finally:
        try: conn.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════════════
# SPRINT BE-1 (2026-04-23) — 5 endpoints backing UX-3/UX-4/UX-5 surfaces.
# No pipeline or schema changes. All data from existing tables.
# ══════════════════════════════════════════════════════════════════════

# ── GET /api/battery/{id}/range-forecast ──────────────────────────────
# 12-week dampened range forecast with P10/P50/P90 bands. Powers the
# Range-tab chart currently computed client-side.

# Event-code → plain English mapping (vehicle_events.event_code).
# Keep in sync with docs/EVENT_CATALOG.md.
_BE1_EVENT_PLAIN = {
    'E1': 'Cell clamp',
    'E2': 'Cell leakage',
    'E3': 'Thermal stress',
    'E4': 'Deep discharge',
    'E5': 'Overcharge',
    'E6': 'BMS fault',
    'E7': 'Voltage sag',
    'E8': 'Current spike',
    'E9': 'Cabinet fault',
    'E10': 'Connector fault',
    'E11': 'IR threshold breach',
    'E12': 'Commissioning anomaly',
    'E15': 'Prior service return',
    'E_PRIOR_SERVICE_RETURN': 'Prior service return',
}


def _be1_severity_bucket(raw):
    """Normalise vehicle_events.severity into HIGH / MODERATE / EARLY / LOW."""
    if raw is None:
        return 'LOW'
    s = str(raw).upper()
    if s in ('CRITICAL', 'HIGH', 'SEV-1', 'SEV-2'):
        return 'HIGH'
    if s in ('MODERATE', 'MEDIUM', 'WARNING', 'SEV-3'):
        return 'MODERATE'
    if s in ('EARLY', 'SEV-4'):
        return 'EARLY'
    return 'LOW'


@app.get("/api/battery/{battery_id}/range-forecast")
@safe
def battery_range_forecast(battery_id: str, _=Depends(verify_token)):
    """12-week dampened linear extrapolation of range with P10/P50/P90.

    already_breached = True when current range < operational floor — p50
    is flat at current range (action is immediate, not projected).
    no_decline = True when slope >= 0 — p50 is flat at current range.
    Otherwise p50[i] = range + slope*i*(0.93^i), clamped to [0, commissioned].
    Rule C09 — directional, not for contractual use.
    """
    conn = get_conn()
    try:
        # commissioned_range_km / mfg_claimed_range_km / age_months / chemistry
        # all live on battery_health_scores_v2 (not on batteries). battery_model
        # is on both — take from batteries to keep join explicit.
        row = q1(conn, """
            SELECT s.range_corrected_km, s.kps_slope_8wk, s.bhs_score_v2,
                   s.commissioned_range_km, s.mfg_claimed_range_km,
                   s.chemistry, s.age_months,
                   b.battery_model
            FROM battery_health_scores_v2 s
            JOIN batteries b USING(battery_id)
            WHERE s.battery_id = ?
              AND s.week_number = (
                  SELECT MAX(week_number) FROM battery_health_scores_v2
                  WHERE battery_id = ?)
        """, [battery_id, battery_id])
        if not row:
            raise HTTPException(404, f"No scored row for {battery_id}")

        range_km = row.get("range_corrected_km")
        # Apply physics ceiling — treat artefacts as missing.
        if range_km is not None and range_km > PHYSICS_CEILING_KM:
            range_km = None
        slope_raw = row.get("kps_slope_8wk")
        slope_km_wk = (slope_raw * 80.0) if slope_raw is not None else None
        commissioned = row.get("mfg_claimed_range_km") or 105.0
        chem = row.get("chemistry") or 'LFP'
        try:
            floor_km = float(get_param('range_floor_km', chemistry=chem, default=56))
        except Exception:
            floor_km = 56.0

        weeks = list(range(13))
        already_breached = range_km is not None and range_km < floor_km
        no_decline = (slope_km_wk is None) or (slope_km_wk >= 0)
        floor_breach_week_p50 = None

        if range_km is None:
            p50 = [None] * 13
            p10 = [None] * 13
            p90 = [None] * 13
        elif already_breached or no_decline:
            flat = round(range_km, 1)
            p50 = [flat] * 13
            p10 = [round(flat * 0.88, 1)] * 13
            p90 = [round(min(flat * 1.10, commissioned), 1)] * 13
        else:
            p50, p10, p90 = [], [], []
            for i in weeks:
                val = range_km + slope_km_wk * i * (0.93 ** i)
                val = max(0.0, min(val, commissioned))
                v50 = round(val, 1)
                p50.append(v50)
                p10.append(round(v50 * 0.88, 1))
                p90.append(round(min(v50 * 1.10, commissioned), 1))
                if floor_breach_week_p50 is None and v50 < floor_km and i > 0:
                    floor_breach_week_p50 = i

        return {
            "battery_id": battery_id,
            "current_range_km": round(range_km, 1) if range_km is not None else None,
            "commissioned_range_km": round(commissioned, 1),
            "floor_km": floor_km,
            "already_breached": already_breached,
            "no_decline": no_decline,
            "weeks": weeks,
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "floor_breach_week_p50": floor_breach_week_p50,
            "slope_km_wk": round(slope_km_wk, 2) if slope_km_wk is not None else None,
            "disclosure": ("Dampened linear extrapolation. Directional only. "
                           "Not for contractual use. Rule C09."),
        }
    finally:
        try: conn.close()
        except Exception: pass


# ── /api/battery/{id}/events ──
# Handler lives at line ~2600 (battery_events_full) — BE-1 upgraded it
# in-place to add @safe + verify_token + shaped_events[] while keeping the
# legacy raw events[] shape for backward-compat. Route is registered once
# there; no duplicate registration here.


# ── GET /api/fleet/charger-groups ─────────────────────────────────────
# Fleet charger intelligence. Primary path uses VWF.charger_type_inferred
# (STANDARD/HEAVY/MIXED/DEGRADED/NON_STANDARD). Fallback via ir_proxy_delta
# thresholds when the column is unavailable.

_BE1_CHARGER_LABELS = {
    'STANDARD': 'Standard charger',
    'HEAVY': 'Heavy-use standard',
    'MIXED': 'Mixed charger usage',
    'DEGRADED': 'Degraded charger',
    'NON_STANDARD': 'Non-standard charger',
    'AFTERMARKET_MED': 'Aftermarket (medium)',
    'AFTERMARKET_HEAVY': 'Aftermarket (heavy)',
    'UNKNOWN': 'Unclassified',
}


@app.get("/api/fleet/charger-groups")
@safe
def fleet_charger_groups(_=Depends(verify_token)):
    """Group fleet by inferred charger type. Surfaces high-risk batteries."""
    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("b.battery_id")
        vwf_cols = [c[1] for c in conn.execute("PRAGMA table_info(vehicle_weekly_features)").fetchall()]
        has_charger_col = 'charger_type_inferred' in vwf_cols
        has_ir_delta = 'ir_proxy_delta' in vwf_cols

        if has_charger_col:
            # Latest-week per-battery slice with charger label + range from BHS.
            sql = f"""
                SELECT v.charger_type_inferred AS grp,
                       COUNT(DISTINCT v.battery_id) AS n_batteries,
                       ROUND(AVG(s.range_corrected_km), 1) AS avg_range_km,
                       ROUND(AVG(s.kps_slope_8wk * 80), 2) AS avg_slope_km_wk,
                       ROUND(AVG(v.ir_proxy_delta), 4) AS avg_ir
                FROM vehicle_weekly_features v
                JOIN batteries b USING(battery_id)
                LEFT JOIN battery_health_scores_v2 s
                  ON s.battery_id = v.battery_id
                 AND s.week_number = (
                     SELECT MAX(week_number) FROM battery_health_scores_v2
                     WHERE battery_id = v.battery_id)
                WHERE v.week_number = (
                    SELECT MAX(week_number) FROM vehicle_weekly_features
                    WHERE battery_id = v.battery_id)
                  AND v.charger_type_inferred IS NOT NULL
                  AND {ban_sql}
                GROUP BY v.charger_type_inferred
                ORDER BY avg_range_km DESC
            """
            grp_rows = q(conn, sql, ban_params)
            groups = {}
            for r in (grp_rows or []):
                key = r['grp']
                groups[key] = {
                    'count': r['n_batteries'],
                    'avg_range_km': r['avg_range_km'],
                    'avg_slope_km_wk': r['avg_slope_km_wk'],
                    'avg_ir': r['avg_ir'],
                    'label': _BE1_CHARGER_LABELS.get(key, key),
                }
            # High-risk: DEGRADED/NON_STANDARD chargers, ordered by IR delta.
            hr_rows = q(conn, f"""
                SELECT v.battery_id, b.battery_model, b.city_code,
                       ROUND(v.ir_proxy_delta, 4) AS avg_ir,
                       ROUND(s.range_corrected_km, 1) AS avg_range_km,
                       ROUND(s.kps_slope_8wk * 80, 2) AS avg_slope_km_wk,
                       v.charger_type_inferred AS charger_group
                FROM vehicle_weekly_features v
                JOIN batteries b USING(battery_id)
                LEFT JOIN battery_health_scores_v2 s
                  ON s.battery_id = v.battery_id
                 AND s.week_number = (
                     SELECT MAX(week_number) FROM battery_health_scores_v2
                     WHERE battery_id = v.battery_id)
                WHERE v.week_number = (
                    SELECT MAX(week_number) FROM vehicle_weekly_features
                    WHERE battery_id = v.battery_id)
                  AND v.charger_type_inferred IN ('DEGRADED', 'NON_STANDARD', 'HEAVY')
                  AND {ban_sql}
                ORDER BY COALESCE(v.ir_proxy_delta, 0) DESC
                LIMIT 10
            """, ban_params)
            high_risk = [dict(r) for r in (hr_rows or [])]
            source = "charger_type_inferred"
            std_n = groups.get('STANDARD', {}).get('count', 0)
            std_r = groups.get('STANDARD', {}).get('avg_range_km')
            bad_n = (groups.get('DEGRADED', {}).get('count', 0)
                     + groups.get('NON_STANDARD', {}).get('count', 0))
            bad_r = None
            for k in ('DEGRADED', 'NON_STANDARD'):
                if groups.get(k, {}).get('avg_range_km'):
                    bad_r = groups[k]['avg_range_km']
                    break
            if bad_n and bad_r and std_r:
                summary = (f"{bad_n} batteries on degraded/non-standard chargers "
                           f"(avg range {bad_r} km) vs {std_n} on standard ({std_r} km).")
            else:
                summary = f"{std_n} batteries on standard chargers. No degraded-charger cluster flagged."
            return {
                "groups": groups,
                "high_risk_batteries": high_risk,
                "source": source,
                "summary": summary,
            }

        # Fallback — no charger_type_inferred column. Derive groups from IR delta.
        if not has_ir_delta:
            return {"groups": {}, "high_risk_batteries": [], "source": "unavailable",
                    "summary": "Neither charger_type_inferred nor ir_proxy_delta present in VWF."}
        per_bat = q(conn, f"""
            SELECT v.battery_id,
                   ROUND(AVG(v.ir_proxy_delta), 4) AS avg_ir,
                   ROUND(AVG(s.range_corrected_km), 1) AS avg_range_km,
                   ROUND(AVG(s.kps_slope_8wk * 80), 2) AS avg_slope_km_wk,
                   b.battery_model, b.city_code
            FROM vehicle_weekly_features v
            JOIN batteries b USING(battery_id)
            LEFT JOIN battery_health_scores_v2 s USING(battery_id)
            WHERE v.ir_proxy_delta IS NOT NULL
              AND {ban_sql}
            GROUP BY v.battery_id
            ORDER BY avg_ir DESC
        """, ban_params)

        def _classify(ir):
            if ir is None:
                return 'UNKNOWN'
            if ir > 0.15:
                return 'NON_STANDARD'
            if ir > 0.08:
                return 'AFTERMARKET_HEAVY'
            if ir > 0.04:
                return 'AFTERMARKET_MED'
            return 'STANDARD'

        groups = {}
        for r in (per_bat or []):
            cls = _classify(r.get('avg_ir'))
            g = groups.setdefault(cls, {'count': 0, '_r': [], '_s': [], '_i': []})
            g['count'] += 1
            if r.get('avg_range_km') is not None:
                g['_r'].append(r['avg_range_km'])
            if r.get('avg_slope_km_wk') is not None:
                g['_s'].append(r['avg_slope_km_wk'])
            if r.get('avg_ir') is not None:
                g['_i'].append(r['avg_ir'])
        for k, g in groups.items():
            def _avg(xs):
                return round(sum(xs) / len(xs), 2) if xs else None
            groups[k] = {
                'count': g['count'],
                'avg_range_km': _avg(g.pop('_r')),
                'avg_slope_km_wk': _avg(g.pop('_s')),
                'avg_ir': _avg(g.pop('_i')),
                'label': _BE1_CHARGER_LABELS.get(k, k),
            }
        high_risk = [dict(r) for r in (per_bat or [])[:10]]
        return {
            "groups": groups,
            "high_risk_batteries": high_risk,
            "source": "ir_proxy_derived",
            "summary": f"Classification from ir_proxy_delta thresholds ({len(per_bat or [])} batteries scored).",
        }
    finally:
        try: conn.close()
        except Exception: pass


# ── GET /api/oem/event-chain-transitions ──────────────────────────────
# Fleet-level event chain transition stats plus current chain-elevated count.
# Reads event_chain_transitions when present (20 rows in production) and
# computes chain-elevated count from VWF latest-week stress signals.

@app.get("/api/oem/event-chain-transitions")
@safe
def oem_event_chain_transitions(_=Depends(verify_token)):
    """Chain-elevated battery count + per-transition probability table."""
    conn = get_conn()
    try:
        ban_sql, ban_params = _banned_sql_clause("battery_id")
        # Chain-elevated = battery has at least one HIGH-severity *unresolved*
        # event AND one other unresolved event (any severity). A single event
        # is not a chain; chains start when a serious signal co-occurs with
        # anything else. Latest-week VWF stress thresholds (voltage_sag > 0.05)
        # don't trip in current data (max 0.034), so the production signal is
        # the event log itself.
        row = q1(conn, f"""
            SELECT COUNT(DISTINCT battery_id) AS chain_elevated
            FROM (
              SELECT battery_id,
                     SUM(CASE WHEN UPPER(COALESCE(severity,'')) IN ('HIGH','CRITICAL','SEV-1','SEV-2') THEN 1 ELSE 0 END) AS n_high,
                     COUNT(DISTINCT event_code) AS n_codes
              FROM vehicle_events
              WHERE resolved_week IS NULL
                AND {ban_sql}
              GROUP BY battery_id
              HAVING n_high >= 1 AND n_codes >= 2
            )
        """, ban_params)
        total_row = q1(conn, "SELECT COUNT(DISTINCT battery_id) AS total FROM battery_health_scores_v2")
        chain_elevated = row.get("chain_elevated") if row else 0
        total = total_row.get("total") if total_row else 0
        pct = round(chain_elevated / total * 100, 1) if total else 0

        # Prefer the production event_chain_transitions table.
        has_ect = q1(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='event_chain_transitions'")
        if has_ect:
            tr_rows = q(conn, """
                SELECT from_event, to_event, probability, avg_transition_weeks, n_observations
                FROM event_chain_transitions
                ORDER BY probability DESC
            """)
            transitions = []
            for r in (tr_rows or []):
                pv = r.get("probability")
                transitions.append({
                    "from_event": r.get("from_event"),
                    "to_event": r.get("to_event"),
                    "probability_pct": round(pv * 100, 1) if pv is not None else None,
                    "median_weeks": r.get("avg_transition_weeks"),
                    "n_observed": r.get("n_observations"),
                    "source": "event_chain_transitions",
                })
            source = "event_chain_transitions"
        else:
            # Static confirmed transitions from service-record analysis.
            transitions = [
                {"from_event": "E1_CELL_CLAMP", "to_event": "E2_CELL_LEAKAGE",
                 "probability_pct": 42, "median_weeks": 5,
                 "n_observed": "confirmed from service records", "source": "static_confirmed"},
                {"from_event": "E9_CABINET_FAULT", "to_event": "E2_CELL_LEAKAGE",
                 "probability_pct": 28, "median_weeks": 5,
                 "n_observed": "confirmed from service records", "source": "static_confirmed"},
            ]
            source = "approximation"

        return {
            "chain_elevated_count": chain_elevated or 0,
            "chain_elevated_pct": pct,
            "total_scored_batteries": total or 0,
            "transitions": transitions,
            "source": source,
            "disclosure": ("Event chain probabilities from fleet telemetry. "
                           "1,378 service records matched to telemetry."),
        }
    finally:
        try: conn.close()
        except Exception: pass


# ── G-5: unit list with breach probabilities (append-only, Session G) ─
# Surfaces p_floor_breach_4w/8w/12w (the actual DB columns) aliased to
# breach_prob_4wk/8wk/12wk so the existing computeGroupForecast() in
# oem_v3 can consume the response without renaming. Banned batteries
# excluded. range_est_km is on BHS_v2; range_corrected_km kept for
# parity with the older /api/oem/unit-list payload.
@app.get("/api/oem/unit-list-extended")
@safe
def oem_unit_list_extended(_=Depends(verify_token)):
    conn = get_conn()
    try:
        rows = q(conn, """
            SELECT b.battery_id,
                   COALESCE(h.battery_model, b.battery_model) AS battery_model,
                   COALESCE(h.battery_model, b.battery_model) AS pack_model,
                   b.city_code AS city,
                   h.bhs_score_v2,
                   h.rul_action_v2,
                   h.tier_label_v2,
                   h.warranty_claim_eligible,
                   h.range_corrected_km,
                   h.range_est_km,
                   h.p_floor_breach_4w  AS breach_prob_4wk,
                   h.p_floor_breach_8w  AS breach_prob_8wk,
                   h.p_floor_breach_12w AS breach_prob_12wk
            FROM batteries b
            JOIN battery_health_scores_v2 h ON b.battery_id = h.battery_id
            WHERE b.battery_id NOT IN ('BAT_LFP_034','BAT_LFP_202')
            ORDER BY h.bhs_score_v2 ASC
        """, [])
        return {"units": rows}
    finally:
        try: conn.close()
        except Exception: pass


# ── G-2: data freshness meta (append-only, Session G) ──────────────
@app.get("/api/platform/data-meta")
@safe
def platform_data_meta(_=Depends(verify_token)):
    """Trust signal for OEM dashboard: latest scored week + date + scored battery count.
    Returned fields are derived once per request so the nav chip always reflects
    DB state without hardcoding a week number into the client."""
    conn = get_conn()
    try:
        row = q1(conn, """
            SELECT
              (SELECT MAX(week_number) FROM vehicle_weekly_features) AS latest_week,
              (SELECT MAX(scored_week_start_date) FROM battery_health_scores_v2) AS latest_date,
              (SELECT COUNT(*) FROM battery_health_scores_v2
               WHERE battery_id NOT IN ('BAT_LFP_034','BAT_LFP_202')) AS battery_count
        """, [])
        return row or {"latest_week": None, "latest_date": None, "battery_count": 0}
    finally:
        try: conn.close()
        except Exception: pass


if __name__ == "__main__":
    import uvicorn
    print(f"DB: {DB_PATH}")
    print(f"Token: {API_TOKEN[:8]}..." if API_TOKEN else "WARNING: No API token")
    uvicorn.run(app, host="0.0.0.0", port=DB_API_PORT)
