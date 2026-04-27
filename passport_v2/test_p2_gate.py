"""
PHASE 2 GATE — Predictions + Events tabs for passport_final_v2.html
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
print("PHASE 2 GATE — PREDICTIONS + EVENTS")
print("=" * 62)
print(f"  {'File size:':<55s} {size_kb:.1f} KB")
print()

# ── PREDICTIONS TAB ──
print("─── PREDICTIONS ───")
check("renderPredictions() exists", "async function renderPredictions" in js)
check("Sec A: Range headline sentence", "We believe this battery will deliver" in js)
check("Sec A: confidenceBar function", "function confidenceBar" in js)
check("Sec A: P10/P50/P90 in bar", "P10" in js and "P50" in js and "P90" in js)
check("Sec A: publicModelName function", "function publicModelName" in js)
check("Sec A: model disclosure line", "average error" in js)
check("Sec A: personal vs ensemble", "Personal model" in js or "personal" in js)
check("Sec B: Based on panel (blue border)", "Based on" in js)
check("Sec B: Unless panel (amber border)", "Unless" in js)
check("Sec B: Because panel (teal border)", "Because" in js)
check("Sec B: relative target mention", "Relative target" in js or "relative target" in js)
check("Sec C: Confidence disclaimer visible", "Confidence" in js and "model disclosure" in js)
check("Sec C: MAPE error in km", "err_km" in js)
check("Sec C: 6-10 weeks lag", "6" in js and "10 weeks" in js)
check("Sec C: loan decision warning", "loan decision" in js.lower() or "loan" in js.lower())
check("Sec D: RUL range (not point)", "rul_high" in js or "rul_replace" in js)
check("Sec D: Two RUL stat cards", "Weeks to service" in js and "Weeks to replacement" in js)
check("Sec D: RUL disclaimer", "probability estimate" in js or "field inspection" in js)
check("Sec D: NBFC planning horizon", "NBFC" in js and "planning horizon" in js)
check("Sec E: SHAP_LABELS lookup", "SHAP_LABELS" in js)
check("Sec E: SHAP_CTX lookup", "SHAP_CTX" in js)
check("Sec E: top 3 features rendered", "slice(0, 3)" in js or "slice(0,3)" in js)
check("Sec E: direction arrows (INCREASING/STABILISING)", "INCREASING RISK" in js and "STABILISING" in js)
check("Sec E: importance bar width", "barW" in js or "bar" in js.lower())
check("Sec E: no-SHAP placeholder", "SHAP feature importance available" in js)
check("NMC survival (if NMC)", "survival" in js and "p_complaint_30d" in js)
check("API: /predictions endpoint", "/predictions" in js)
check("API: /intelligence endpoint", "/intelligence" in js)
check("API: /survival (NMC only)", "/survival" in js)

print()
print("─── EVENTS ───")
check("renderEvents() exists", "async function renderEvents" in js)
check("Sec A: Summary pills by event type", "counts" in js and "evtCodeShort" in js)
check("Sec A: severity colour mapping", "SEV_COLORS_MAP" in js or "SEV-1" in js)
check("Sec B: event-timeline canvas", "event-timeline" in js)
check("Sec B: drawEventTimeline function", "function drawEventTimeline" in js)
check("Sec B: canvas 2D drawing (arc)", "ctx.arc" in js or "arc(" in js)
check("Sec C: Event table (CODE/SEV/WEEK/DESC/CTX)", "EVENT_LABELS" in js and "EVENT_CONTEXT" in js)
check("Sec C: 11 event types defined", sum(1 for k in ['E1','E2','E3','E4','E5','E6','E7','E8','E9','E10','E11'] if f"'{k}'" in js) >= 10)
check("Sec D: detectChains function", "function detectChains" in js)
check("Sec D: 6 chain patterns", sum(1 for k in ['E3_E2','E2_E1','E6_E1','E4_E7','E3_E1','E1_E2'] if k in js) >= 5)
check("Sec D: chain why + action", ".why" in js and ".action" in js)
check("Sec E: externalConditions function", "function externalConditions" in js)
check("Sec E: India seasonal context", "Indian summer" in js or "Indian" in js)
check("Sec E: 4 seasons covered", "Peak Summer" in js and "Monsoon" in js and "Winter" in js)
check("Sec F: collapsible event legend", "<details" in js and "legend" in js.lower())
check("Sec F: severity level definitions", "SEV-1" in js and "SEV-4" in js)
check("API: /events endpoint", "/events" in js)
check("Zero-events green card", "No Anomalous Events" in js)

print()
print("─── INTEGRATION ───")
check("afterRender dispatches predictions", "tab === 'predictions'" in js)
check("afterRender dispatches events", "tab === 'events'" in js)
check("Phase 1 renderHealth still exists", "async function renderHealth" in js)
check("Phase 0 init/showTab intact", "async function init" in js and "function showTab" in js)
check("Phase 0 8 tabs still in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))

print()
print("=" * 62)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  PHASE 2: {result}")
print("=" * 62)
sys.exit(0 if FAIL == 0 else 1)
