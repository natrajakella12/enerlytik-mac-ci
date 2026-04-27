"""Customer-facing attribution text library.

Pure string generation — no DB access. Imported by RAG/db_api.py. Every
function returns a plain-English string for one specific audience
(operator / NBFC / OEM). When battery_degradation_attribution already
carries a curated sentence for the audience, the API prefers the stored
sentence; these functions generate when that column is NULL.

Decisions enforced here:
- Operators never see percentages or internal codes.
- NBFC outputs always include `rul_disclaimer()` and grade framing.
- OEM is the only audience that receives raw factor percentages.
- Pack3401 → PACK_GAP_EXCEPTION templates regardless of factor values.
- VEHICLE_ISSUE (sustained ≥3 weeks) → vehicle_context sentence.
- action_sentence uses rul_action_v2 codes only (Rule 207: action_primary deprecated).
"""

_FACTOR_PLAIN = {
    'charging':    'Charging pattern',
    'usage':       'Usage intensity',
    'thermal':     'Thermal exposure',
    'maintenance': 'Cell maintenance',
    'calendar':    'Calendar aging',
}

_PACK3401_FLEET_FINDING = (
    "Pack3401 degrades at 6× the Pack3001 reference rate (−0.0077/wk vs "
    "−0.0013/wk) across 29 fleet batteries. Design-level gap — operator "
    "behaviour signals are within fleet norms."
)


# ── Primary driver detection ────────────────────────────────────────────

def primary_driver_code(factors_norm, attr_row=None, bhs_row=None):
    """Derive a primary driver code from normalised factors.

    Returns one of: charging, maintenance, usage, thermal, calendar.
    Falls back to BDA.primary_driver or bhs.attr_primary_factor if factors
    unavailable.
    """
    if factors_norm:
        ordered = sorted(factors_norm.items(), key=lambda kv: kv[1], reverse=True)
        return ordered[0][0]
    if attr_row and attr_row.get('primary_driver'):
        return attr_row['primary_driver']
    legacy = (bhs_row or {}).get('attr_primary_factor') or ''
    low = legacy.lower()
    if 'cycle' in low or 'charge' in low:
        return 'charging'
    if 'imbal' in low or 'cell' in low or 'balance' in low:
        return 'maintenance'
    if 'dod' in low or 'usage' in low or 'route' in low:
        return 'usage'
    if 'therm' in low or 'heat' in low:
        return 'thermal'
    if 'cal' in low or 'age' in low:
        return 'calendar'
    return 'calendar'


def primary_driver_plain(code):
    """Plain-English label for a driver code."""
    labels = {
        'charging':    'Charging pattern',
        'maintenance': 'Cell imbalance (maintenance gap)',
        'usage':       'Usage intensity',
        'thermal':     'Thermal exposure',
        'calendar':    'Calendar aging (natural)',
    }
    return labels.get(code, code or 'Not determined')


# ── OPERATOR audience ───────────────────────────────────────────────────

_OPERATOR_TEMPLATES = {
    'charging': (
        "This battery is working harder than it needs to because of its "
        "charging pattern. Charging to full overnight every night, then "
        "running the battery low before the next charge, puts extra wear "
        "on the cells. Range is recovering slower than similar batteries "
        "that charge more regularly."),
    'maintenance': (
        "The cells inside this battery have drifted out of balance. This "
        "is normal over time, but a cell balancing service (₹1,500–2,000) "
        "resets the gap and recovers 2–5 km of range. This battery is "
        "overdue for that service."),
    'usage': (
        "This battery is running heavier routes than it was designed for. "
        "The vehicle is drawing more current than the fleet average, which "
        "wears the battery faster. Shorter routes or a mid-day top-up "
        "charge can extend battery life by 8–12 weeks."),
    'thermal': (
        "This battery has been running in hot conditions. Heat accelerates "
        "wear — this is expected in summer and is not a fault. Range "
        "typically recovers by 4–6 km in cooler months. No action needed "
        "now."),
    'calendar': (
        "This battery has aged naturally. At its current age, some range "
        "reduction is expected — the cells are healthy but older. Range at "
        "this age is within the normal band for this pack model."),
}


def operator_narrative(factors_norm, primary_plain, vehicle_issue, bhs_row):
    """Primary driver sentence for the operator surface (no percentages)."""
    if vehicle_issue:
        return (
            "The battery chemistry is in better shape than the range numbers "
            "suggest. The vehicle is using more energy than expected for its "
            "routes — likely the motor, tyres, or load. The battery itself is "
            "healthy. Have the vehicle checked.")
    code = primary_driver_code(factors_norm, None, bhs_row)
    return _OPERATOR_TEMPLATES.get(code, _OPERATOR_TEMPLATES['calendar'])


def pack3401_operator():
    return (
        "This pack model (Pack3401) is showing lower range than similar-age "
        "batteries on Pack3001 packs doing the same routes. This is a known "
        "characteristic of this pack model, not a result of how the vehicle "
        "is used. The manufacturer has been informed.")


# Action sentence map — rul_action_v2 codes (Rule 207: action_primary deprecated)
_ACTION_TEMPLATES = {
    'REPLACE_NOW':         "Replace this battery — it has reached end of useful life.",
    'REPLACE_PLAN':        "Plan battery replacement in the next 8–12 weeks.",
    'PHYSICS_REPLACE_PLAN':"Plan battery replacement in the next 8–12 weeks — physics-driven decision.",
    'ACUTE_BREACH_WATCH':  "Floor breach detected — review this battery this week.",
    'CELL_BALANCE':        "Book a cell balancing service this week — ₹1,500–2,000, recovers 2–5 km.",
    'CELL_BALANCE_PRIORITY':"Cell balancing service is overdue — book this week.",
    'CHARGER_SWAP':        "Replace the charger — ₹3,000, likely to recover 4–8 km.",
    'ROUTE_SHORTEN':       "Reduce route length by 20% or add a mid-day charge stop.",
    'MONITOR_INVESTIGATE': "Investigate next visit — range below expected but trajectory unclear.",
    'MONITOR':             "Check this battery's range weekly. No action needed yet.",
    'MONITOR_WEEKLY':      "Check this battery's range weekly. No action needed yet.",
    'ROUTINE':             "This battery is performing well. Continue current routine.",
    'NO_ACTION':           "This battery is performing well. Continue current routine.",
}


def action_sentence(rul_action_v2):
    if not rul_action_v2:
        return _ACTION_TEMPLATES['ROUTINE']
    return _ACTION_TEMPLATES.get(rul_action_v2, f"Review operator dashboard for current action. ({rul_action_v2})")


def vehicle_context():
    """Teal info sentence shown when VEHICLE_ISSUE is sustained."""
    return (
        "Coulomb vs kWh-per-SoC divergence is sustained. The vehicle is "
        "using more energy than its battery is delivering — likely motor, "
        "tyres, or load. Battery chemistry is not the primary issue.")


# ── NBFC audience ───────────────────────────────────────────────────────

def nbfc_narrative(factors_norm, bhs_row, vehicle_issue):
    op_pct = round(
        (factors_norm or {}).get('charging', 0) +
        (factors_norm or {}).get('usage', 0) +
        (factors_norm or {}).get('maintenance', 0), 1)
    oem_pct = round((factors_norm or {}).get('calendar', 0), 1)
    warranty_eligible = (bhs_row or {}).get('warranty_claim_eligible') or 0

    if vehicle_issue:
        return (
            "Battery chemistry is stable. Range shortfall is vehicle-related "
            "(motor or load), not battery degradation. Collateral value is "
            "not at risk from battery chemistry.")
    if warranty_eligible:
        return (
            f"Degradation pace is above warranty floor for this battery's age. "
            f"{oem_pct}% of degradation is OEM-attributable — this portion is "
            f"warranty-covered, reducing effective collateral risk. Platform "
            f"has generated an evidence pack for claim submission.")
    return (
        f"Degradation analysis: {op_pct}% attributable to operator usage and "
        f"maintenance. {oem_pct}% attributable to pack design and calendar "
        f"aging — covered by manufacturer warranty during warranty period. "
        f"Effective borrower risk: {op_pct}% of degradation exposure.")


def pack3401_nbfc(warranty_eligible):
    head = (
        "This pack model (Pack3401) shows a design-level performance gap "
        "confirmed across 29 batteries in the fleet. Degradation rate is 6× "
        "the Pack3001 reference pack.")
    tail = (
        " Warranty claim active — OEM has been notified. Effective loan risk "
        "for this asset should include warranty recovery probability in "
        "collateral valuation."
        if warranty_eligible
        else " OEM investigation active — warranty recovery probability should be "
             "factored into collateral valuation when claim opens.")
    return head + tail


def grade_framing(grade):
    g = (grade or '').upper()[:1]
    return {
        'A': "Battery is in the top tier of the fleet. Loan risk is low.",
        'B': "Battery is aging normally. No covenant concern at current trajectory.",
        'C': "Battery is declining. Review covenant position at next payment cycle.",
        'D': "Battery has breached floor. Collateral value has declined materially.",
    }.get(g, "Grade not yet assigned — refer to SoH and range telemetry.")


def rul_disclaimer():
    return (
        "Remaining useful life estimates are directional. Not for use as "
        "covenant triggers until conformal calibration completes (9 of 30 "
        "field outcomes confirmed).")


# ── OEM audience ────────────────────────────────────────────────────────

def oem_narrative(factors_norm, bhs_row, vehicle_issue):
    age = (bhs_row or {}).get('age_months')
    age_s = f"{round(age)} months" if age else "—"
    f = factors_norm or {}
    parts = (
        f"Degradation decomposition — {age_s} field operation: "
        f"Charging pattern {f.get('charging', 0)}% | "
        f"Usage intensity {f.get('usage', 0)}% | "
        f"Thermal exposure {f.get('thermal', 0)}% | "
        f"Cell maintenance {f.get('maintenance', 0)}% | "
        f"Calendar aging {f.get('calendar', 0)}%.")
    code = primary_driver_code(f, None, bhs_row)
    parts += f" Primary driver: {primary_driver_plain(code)}."
    if vehicle_issue:
        parts += (
            " Note: vehicle shows sustained coulomb/KPS divergence >1.15 — "
            "operational stress signal is partially vehicle-attributable.")
    return parts


def pack3401_oem():
    return (
        "Pack3401 — Attribution not computed. This battery's range decline "
        "cannot be explained by operator behaviour signals. Cell maintenance "
        "and usage indicators are within fleet norms. Intrinsic pack "
        "characteristic is the most likely explanation. " + _PACK3401_FLEET_FINDING)


# ── Helpers used by the API layer ───────────────────────────────────────

def factor_plain(code):
    return _FACTOR_PLAIN.get(code, code)


PACK3401_NOTE = (
    "Pack3401 shows intrinsic design gap confirmed across fleet. Degradation "
    "rate 6× Pack3001 (−0.0077/wk vs −0.0013/wk). Attribution not computed — "
    "operator behaviour signals are within fleet norms. OEM investigation "
    "active.")

NMC_INCOMPLETE_NOTE = "NMC attribution uses separate methodology."

LFP_INCOMPLETE_NOTE = "Attribution signal incomplete for this battery."
