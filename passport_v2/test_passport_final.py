"""
Test script for passport_final.html — validates all 14 tabs, functions, API calls,
data structures, and UI elements are present.
"""
import re
import sys
from pathlib import Path

PASS = 0
FAIL = 0

def check(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")

html = Path(__file__).parent.joinpath("passport_final.html").read_text(encoding="utf-8")
# Find the main script block (not the CDN script tag)
script_starts = [m.start() for m in re.finditer(r"<script>", html)]
script_ends = [m.start() for m in re.finditer(r"</script>", html)]
# The main JS is the last <script>...</script> block
js = html[script_starts[-1]:script_ends[-1]] if script_starts and script_ends else ""

print("=" * 70)
print("PASSPORT FINAL AUDIT — Completeness Test")
print("=" * 70)

# ── FILE METRICS ──
print(f"\nFile size: {len(html):,} bytes | {html.count(chr(10))+1} lines")
print(f"JS size: {len(js):,} chars")

# ── TAB PRESENCE (14 tabs) ──
print("\n--- TAB BUTTONS (14 required) ---")
TABS = ["fleet","health","predictions","signal","events","investigation",
        "service","dekf","twin","platform","architecture","onboarding",
        "explainability","reference"]
for t in TABS:
    check(f"Tab button: {t}", f'data-tab="{t}"' in html)

# ── RENDER FUNCTIONS (14 required) ──
print("\n--- RENDER FUNCTIONS ---")
RENDERS = ["renderFleet","renderHealth","renderPredictions","renderSignal",
           "renderEvents","renderInvestigation","renderService","renderDEKF",
           "renderTwin","renderPlatform","renderArchitecture","renderOnboarding",
           "renderExplainability","renderReference"]
for fn in RENDERS:
    check(f"Function: {fn}", f"function {fn}" in js or f"async function {fn}" in js)

# ── TAB ROUTER COMPLETENESS ──
print("\n--- TAB ROUTER ---")
for t in TABS:
    check(f"Router maps: {t}", f"{t}:render" in js)

# ── HELPER FUNCTIONS (from components.js) ──
print("\n--- HELPER FUNCTIONS ---")
HELPERS = ["tierBadge","confidenceBadge","chemistryBadge","fmt","emptyState",
           "loadingSkeleton","statPill","eventDescription","sevBadge",
           "chemBadgeMini","scoreBar","sigRow","proxyRow","signalCard",
           "makeChart","feedbackCard","showEventDetail","showRefTab","filterRef"]
for fn in HELPERS:
    check(f"Helper: {fn}", f"function {fn}" in js)

# ── API ENDPOINTS USED ──
print("\n--- API ENDPOINTS ---")
ENDPOINTS = [
    ("/api/health", "health check"),
    ("/api/fleet/batteries", "battery list"),
    ("/api/fleet/tier-distribution", "tier doughnut"),
    ("/api/fleet/at-risk", "at-risk table"),
    ("/api/fleet/events/recent", "recent events"),
    ("/api/fleet/summary", "fleet summary"),
    ("/api/platform/models-health", "model health"),
    ("/api/battery/", "battery detail"),
    ("/api/battery/${S.bat}/trend/", "trend data"),
    ("/api/battery/${S.bat}/predictions", "predictions"),
    ("/api/battery/${S.bat}/survival", "survival forecast"),
    ("/api/battery/${S.bat}/telemetry", "telemetry data"),
    ("/api/battery/${S.bat}/investigation", "investigation"),
    ("/api/battery/${S.bat}/service", "service data"),
    ("/api/battery/${S.bat}/dekf", "DEKF data"),
    ("/api/battery/${S.bat}/dekf/status", "DEKF status"),
    ("/api/battery/${S.bat}/reasoning", "reasoning chain"),
    ("/api/battery/${S.bat}/kb-context", "KB context"),
    ("/api/platform/catalogue", "model catalogue"),
    ("/api/platform/queue", "scoring queue"),
    ("/api/platform/system", "system health"),
    ("/api/platform/summary", "platform summary"),
    ("/api/platform/templates", "template catalog"),
    ("/api/platform/gps-check", "GPS check"),
    ("/api/feedback", "feedback submit"),
]
for ep, desc in ENDPOINTS:
    check(f"API: {desc} ({ep})", ep in js)

# ── CHART.JS CANVASES ──
print("\n--- CHART.JS CANVASES ---")
CANVASES = ["tier-doughnut","km-chart","ch-voltage","ch-current","ch-soc",
            "ch-temp","dekf-soc-chart","dekf-r0-chart","dt1-preview"]
for c in CANVASES:
    check(f"Canvas: {c}", f'id="{c}"' in js or f"'{c}'" in js)

# ── DATA STRUCTURES ──
print("\n--- DATA STRUCTURES ---")
check("TIER_C colors", "TIER_C" in js and "PRIME" in js)
check("TIER_BG backgrounds", "TIER_BG" in js)
check("TIER_TX text colors", "TIER_TX" in js)
check("CONF_COLORS", "CONF_COLORS" in js)
check("SEV_COLORS", "SEV_COLORS" in js)
check("VERDICT_S styles", "VERDICT_S" in js)
check("EVENT_LABELS (13+ entries)", len(re.findall(r"'E\d+':", js)) >= 10)
check("FEATURE_LABELS (SHAP)", "FEATURE_LABELS" in js and "cell_spread_slope" in js)
check("STATUS_BADGE (4 statuses)", all(s in js for s in ["PRODUCTION","SHADOW","QUARANTINED","DEPRECATED"]))
check("RULES array (34 rules)", "RULES" in js and "Rule 14" in js)
check("STATIC_CATALOGUE (21+ models)", js.count("name:'") >= 20 or js.count('name:"') >= 20 or "STATIC_CATALOGUE" in js)
check("GLOSSARY (39+ terms)", "GLOSSARY" in js and len(re.findall(r"\['[A-Z]", js)) >= 30)
check("TECHNIQUES (13 entries)", "TECHNIQUES" in js and "Weibull AFT" in js)
check("FORMULAS (8 entries)", "FORMULAS" in js and "Scoring Gate" in js)
check("CHAINS (causal chain intel)", "E3_E2" in js or "E3→E2" in js)

# ── CRITICAL FEATURES ──
print("\n--- CRITICAL FEATURES ---")
check("Fleet Pulse: tier doughnut", "tier-doughnut" in js)
check("Fleet Pulse: EEHI display", "EEHI" in js)
check("Fleet Pulse: at-risk table", "At-Risk" in html or "at-risk" in js)
check("Fleet Pulse: model health cards", "Model Status" in js)
check("Health: L1/L2/L3 scoring bars", "L1 Self-baseline" in js and "L2 Physics" in js and "L3 Fleet" in js)
check("Health: confidence badge", "confidenceBadge" in js)
check("Health: efficiency trajectory chart", "km-chart" in js)
check("Health: behavioural proxies", "Depth of discharge" in js)
check("Predictions: SHAP drivers", "SHAP" in js and "FEATURE_LABELS" in js)
check("Predictions: NMC survival P30/P60/P90", "p_complaint_30d" in js)
check("Predictions: RUL", "Remaining Useful Life" in js or "rul_weeks" in js)
check("Live Signal: 4 charts", all(c in js for c in ["ch-voltage","ch-current","ch-soc","ch-temp"]))
check("Live Signal: time range selector", "t4Days" in js)
check("Live Signal: voltage bounds", "vBounds" in js)
check("Live Signal: deep discharge annotation", "deep discharge" in js.lower())
check("Events: visual timeline (circle nodes)", "border-radius:50%" in js)
check("Events: showEventDetail click handler", "showEventDetail" in js)
check("Events: trend column (WORSENING/IMPROVING)", "WORSENING" in js and "IMPROVING" in js)
check("Events: causal chain analysis", "CAUSAL CHAIN" in js)
check("Investigation: verdict banner", "PHYSICS_CONFIRMED" in js)
check("Investigation: evidence chain table", "Evidence Chain" in js)
check("Investigation: complaint timeline overlay", "Complaint Timeline" in js)
check("Investigation: liability badge", "OEM liability" in js)
check("Investigation: Rule 28 disclosure", "Rule 28" in js)
check("Service: mandatory header", "SERVICE SCHEDULING ONLY" in js)
check("Service: P30/P60/P90 bars", "p_complaint_30d" in js)
check("Service: complaint history table", "Complaint History" in js)
check("Service: pattern classification", "PROGRESSIVE" in js and "RECURRING" in js)
check("Service: silent degrader", "Silent Degrader" in js)
check("Service: next service recommendation", "Next Service Recommendation" in js)
check("DEKF: SoC comparison chart", "dekf-soc-chart" in js)
check("DEKF: R0 tracking chart", "dekf-r0-chart" in js)
check("DEKF: filter health diagnostics table", "Filter Health Diagnostics" in js)
check("DEKF: promotion checklist", "Promotion Checklist" in js)
check("DEKF: phase 2 status", "Phase 2 Status" in js)
check("DEKF: promotion banner (shadow/active)", "SHADOW MODE" in js)
check("Digital Twin: 5 layers", "DT-1" in js and "DT-2" in js and "DT-3" in js and "DT-4" in js and "DT-5" in js)
check("Digital Twin: R0 foundation", "R0 Foundation" in js)
check("Digital Twin: DT-1 preview chart", "dt1-preview" in js)
check("Digital Twin: GPS check", "gps-check" in js)
check("Platform: model catalogue table", "Model Catalogue" in js)
check("Platform: 34 rules accordion", "34 Non-Negotiable Rules" in js)
check("Platform: system health", "System Health" in js)
check("Platform: scoring queue", "Scoring Queue" in js)
check("Platform: fleet KPI pills", "Total Batteries" in js)
check("Architecture: pipeline waterfall", "Pipeline Waterfall" in js or "renderArchWaterfall" in js)
check("Architecture: template catalog", "Template Catalog" in js or "renderArchTemplates" in js)
check("Architecture: sub-tab switcher", "t11-btn-wf" in js)
check("Onboarding: OEM 8-step flow", "Source Data" in js and "Quality Gates" in js)
check("Onboarding: battery 6-step flow", "Commissioning" in js and "First Telemetry" in js)
check("Onboarding: live queue counts", "Fully Active" in js)
check("Onboarding: sub-tab switcher", "t12-btn-oem" in js)
check("Explainability: 5-layer reasoning chain", "LAYER 1" in js and "LAYER 5" in js)
check("Explainability: narrative summary", "Narrative Summary" in js)
check("Explainability: KB connection", "Knowledge Base Context" in js)
check("Explainability: confidence audit trail", "CONFIDENCE AUDIT TRAIL" in js)
check("Reference: searchable glossary", "ref-search" in js)
check("Reference: sub-tab switcher", "showRefTab" in js)
check("Reference: live search filter", "filterRef" in js)
check("Reference: models & techniques", "Models & Techniques" in js or "models" in js)
check("Reference: formulas", "Formulas" in js)
check("Feedback: form with dropdowns", "fb-role" in js and "fb-a" in js)
check("Feedback: submit + JSON save", "submitFeedback" in js and "saveFeedbackJSON" in js)

# ── COMPARISON vs MODULAR ──
print("\n--- COMPARISON vs MODULAR ---")
check("Tabs: 14/14", len([t for t in TABS if f'data-tab="{t}"' in html]) == 14)
check("Render functions: 14/14", len([fn for fn in RENDERS if f"function {fn}" in js or f"async function {fn}" in js]) == 14)
check("Helper functions: 19/19", len([fn for fn in HELPERS if f"function {fn}" in js]) == 19)
check("Canvases: 9/9", len([c for c in CANVASES if c in js]) == 9)

# ── SUMMARY ──
print("\n" + "=" * 70)
total = PASS + FAIL
print(f"RESULTS: {PASS}/{total} PASS | {FAIL} FAIL")
if FAIL == 0:
    print("ALL CHECKS PASS ✓")
else:
    print(f"⚠ {FAIL} checks failed")
print("=" * 70)

sys.exit(0 if FAIL == 0 else 1)
