"""
PHASE 4 + PHASE 5 GATE — Analytics + Reference + Full Validation
"""
import re, sys
from pathlib import Path

html = Path(__file__).parent.joinpath("passport_final_v2.html").read_text(encoding="utf-8")
ss = [m.start() for m in re.finditer(r"<script>", html)]
se = [m.start() for m in re.finditer(r"</script>", html)]
js = html[ss[-1]:se[-1]] if ss and se else ""
size_kb = len(html) / 1024
PASS = FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; s = "YES"
    else: FAIL += 1; s = "NO"
    print(f"  {name:55s} {s}")

print()
print("PHASE 4 GATE — ANALYTICS + REFERENCE")
print("=" * 62)
print(f"  {'File size:':<55s} {size_kb:.1f} KB")
print()

# ── ANALYTICS ──
print("─── ANALYTICS ───")
check("renderAnalytics() exists", "function renderAnalytics" in js)
check("Coming next sprint label", "Coming next sprint" in js)
check("Custom analytics engine title", "Custom analytics engine" in js)
check("X/Y/Z axis cards (3 cells)", "X axis" in js and "Y axis" in js and "Z overlay" in js)
check("afterRender dispatches analytics", "tab === 'analytics'" in js)

print()
print("─── REFERENCE ───")
check("renderReference() exists", "function renderReference" in js)

# Platform findings
check("BMS imbalance alert finding", "permanently disabled" in js or "800mV" in js)
check("BMS SoC overstatement finding", "5.5%" in js and "overstatement" in js)
check("BMS SoH for LFP finding", "97" in js and "100%" in js and "degraded" in js)
check("Intelligence Applied finding", "corroboration" in js or "two independent" in js)

# Active models
check("Models: Range Ensemble", "Range Predictor" in js and "14.07%" in js)
check("Models: Range Personal", "Personal" in js and "9.3%" in js)
check("Models: Health Scoring", "Health Scoring" in js and "3-layer" in js)
check("Models: Fault Classifier (NMC)", "Fault Classifier" in js and "0.692" in js)
check("Models: Service Forecaster (NMC)", "Service Forecaster" in js and "0.600" in js)
check("Models: DEKF R0 NMC", "7.3% MAPE" in js and "pulse ECM" in js)
check("Models: DEKF R0 LFP", "100% ECM" in js and "thermal proxy" in js.lower())
check("Models: Event Detection", "E1" in js and "CUSUM" in js and "PELT" in js)

# Key formulas
check("Formula: Range (kps × 80)", "km_per_soc_pct" in js and "80" in js)
check("Formula: Efficiency", "SUM(km) / SUM(SoC%)" in js)
check("Formula: SOH_CAP", "SOH_CAP" in js)
check("Formula: Self-baseline", "Self-baseline" in js)
check("Formula: Range correction", "Range correction" in js or "SoC_error" in js)

# Event codes
check("Event E1-E11 all present", all(f"'{e}'" in js or f'"{e}"' in js for e in ["E1","E2","E3","E4","E5","E6","E7","E8","E9","E10","E11"]))
check("Event detection methods shown", "CUSUM" in js and "PELT" in js and "Threshold" in js)

# Severity + tiers
check("SEV-1 through SEV-4", "SEV-1" in js and "SEV-2" in js and "SEV-3" in js and "SEV-4" in js)
check("All 5 tiers defined", all(t in js for t in ["PRIME","STABLE","WATCH","STRESSED","CRITICAL"]))
check("Tier score ranges", "80" in js and "60" in js and "40" in js and "20" in js)

# Key rules
check("Rule 2 (no BMS SoH)", "Rule 2" in js and "BMS SoH" in js)
check("Rule 3 (efficiency formula)", "Rule 3" in js)
check("Rule 14 (LFP R0 thermal)", "Rule 14" in js)
check("Rule 28 (investigation threshold)", "Rule 28" in js)
check("Rule 29 (silent degraders)", "Rule 29" in js)

check("afterRender dispatches reference", "tab === 'reference'" in js)

print()
print("=" * 62)
print("PHASE 5 — FULL VALIDATION")
print("=" * 62)
print()

# ── FUNCTIONALITY ──
print("─── FUNCTIONALITY ───")
check("All 8 tab buttons in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))
check("All 8 render functions exist", all(("function render" + t.capitalize() in js or "function render" + t.upper()[:1] + t[1:] in js or "async function render" + t.capitalize() in js or "async function render" + t.upper()[:1] + t[1:] in js) for t in ["Health","Predictions","Events","Deepdive","Service","DEKF","Analytics","Reference"]))
check("All 8 afterRender dispatches", all(f"tab === '{t}'" in js for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))
check("Chemistry toggle LFP/NMC", "setChemistry" in js and 'data-chem="LFP"' in html and 'data-chem="NMC"' in html)
check("OEM filter", "setOEM" in js and "loadOEMs" in js)
check("URL state sync", "syncURL" in js and "replaceState" in js)
check("Refresh button clears cache", "S.cache={}" in js or "S.cache={}" in html)
check("Feedback POST /api/feedback", "submitFeedback" in js and "/api/feedback" in js)
check("Save as JSON fallback", "saveFeedbackJSON" in js)
check("Offline banner", "offline-bar" in html and "setOnline" in js)

print()
print("─── DATA COVERAGE ───")
# Health tab
check("Health: verdict + scorecard + signals", "verdict-alert" in js and "scorecard-cell" in js and "sig-card" in js)
check("Health: KM chart", "km-chart" in js and "drawKmChart" in js)
check("Health: IR sparkline", "ir-chart" in js and "drawIRChart" in js)
check("Health: vehicle profile", "vehicleProfile" in js)
check("Health: subsystem health 6 panels", "subsystemHealth" in js and "Cell balance" in js)
check("Health: proxy signals", "proxyRow" in js and "Load intensity" in js)
# Predictions
check("Predictions: headline sentence", "We believe this battery" in js)
check("Predictions: range context 4-cell", "rangeContext" in js)
check("Predictions: fleet benchmark bar", "fleetBenchmarkBar" in js)
check("Predictions: Based on/Unless/Because panels", "reasonPanel" in js)
check("Predictions: disclaimer always visible", "model disclosure" in js)
check("Predictions: RUL range", "rul_high" in js)
# Events
check("Events: timeline + table + chains", "event-timeline" in js and "detectChains" in js)
check("Events: external conditions", "externalConditions" in js)
# Deepdive
check("Deepdive: verdict + narrative", "VERDICT_CONFIG" in js and "deepNarrative" in js)
check("Deepdive: 4-layer reasoning", "reasoningChain" in js and "Layer 1" in js and "Layer 4" in js)
# Service
check("Service: disclaimer banner", "Service scheduling only" in js)
check("Service: LFP graceful message", "LFP service forecast" in js)
# DEKF
check("DEKF: Rule 14 note", "Rule 14" in js and "thermal proxy" in js)
check("DEKF: promotion checklist", "R0 MAPE" in js and "SoC correction" in js)

print()
print("─── DESIGN ───")
check("Orange accent (var(--accent))", "--accent: #f97316" in html)
check("All section labels with ◈", js.count("◈") >= 10)
check("batterySubLine strips OEM names", "batterySubLine" in js and ".replace(" in js)

print()
print("─── TABS REMOVED — confirm absent ───")
check("Platform tab: ABSENT", 'data-tab="platform"' not in html)
check("Architecture tab: ABSENT", 'data-tab="architecture"' not in html)
check("Onboarding tab: ABSENT", 'data-tab="onboarding"' not in html)
check("Explainability tab: ABSENT", 'data-tab="explainability"' not in html)

print()
print("─── SELF-CONTAINED ───")
check("No imports from js/ folder", 'src="js/' not in html and "src='js/" not in html)
check("No imports from css/ folder", 'href="css/' not in html and "href='css/" not in html)
check("Chart.js CDN only external JS", html.count("<script") == 2)  # CDN + inline
check("Fonts CDN only external CSS", 'googleapis.com' in html and html.count("<link") <= 2)

print()
print("─── PRIOR GATES ───")
check("Phase 0: init/showTab/emptyState/cKey", all(f in js for f in ["function init","function showTab","function emptyState","function cKey"]))
check("Phase 1: renderHealth + all sections", "async function renderHealth" in js and "drawKmChart" in js and "drawIRChart" in js)
check("Phase 2: renderPredictions + renderEvents", "async function renderPredictions" in js and "async function renderEvents" in js)
check("Phase 3: renderDeepdive + renderService + renderDEKF", "async function renderDeepdive" in js and "async function renderService" in js and "async function renderDEKF" in js)

print()
print("=" * 62)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  File size: {size_kb:.1f} KB")
print(f"  PHASE 4 + PHASE 5: {result}")
print("=" * 62)
sys.exit(0 if FAIL == 0 else 1)
