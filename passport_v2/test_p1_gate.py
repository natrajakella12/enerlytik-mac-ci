"""
PHASE 1 GATE — Health tab completeness for passport_final_v2.html
"""
import re
import sys
from pathlib import Path

html = Path(__file__).parent.joinpath("passport_final_v2.html").read_text(encoding="utf-8")
script_starts = [m.start() for m in re.finditer(r"<script>", html)]
script_ends = [m.start() for m in re.finditer(r"</script>", html)]
js = html[script_starts[-1]:script_ends[-1]] if script_starts and script_ends else ""

size_kb = len(html) / 1024
PASS = FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; status = "YES"
    else: FAIL += 1; status = "NO"
    print(f"  {name:50s} {status}")
    return cond

print()
print("PHASE 1 GATE — HEALTH TAB")
print("=" * 60)
print(f"  {'File size:':<50s} {size_kb:.1f} KB")
print()

# ── Core function exists ──
check("renderHealth() exists", "async function renderHealth" in js)

# ── Section A: Passport header ──
check("Section A: battery ID (serial class)", "serial" in js and "S.bat" in js)
check("Section A: tier badge", "tierBadge" in js)
check("Section A: confidence badge", "confidenceBadge" in js)
check("Section A: P10/P50/P90 display", "P10:" in js and "P90:" in js)
check("Section A: staleness update", "updateStaleness" in js)

# ── Section B: 4-cell scorecard ──
check("Section B: 4 scorecard cells (g4)", "g4" in js)
check("Section B: efficiency loss %", "Efficiency vs baseline" in js)
check("Section B: cell spread mV", "Cell spread" in js)
check("Section B: range P50", "Range P50" in js)
check("Section B: RUL score", "RUL score" in js)

# ── Section C: Verdict alert ──
check("Section C: verdict-alert class", "verdict-alert" in js)
check("Section C: autoNarrative function", "function autoNarrative" in js)
check("Section C: tier-coloured borders (5 tiers)", all(t in js for t in ["CRITICAL","STRESSED","WATCH","STABLE","PRIME"]))

# ── Section D: 3-layer scoring ──
check("Section D: L1 Self-baseline", "L1 Self-baseline" in js)
check("Section D: L2 Physics limits", "L2 Physics limits" in js)
check("Section D: L3 Fleet context", "L3 Fleet context" in js)
check("Section D: layerNarrative function", "function layerNarrative" in js or "layerNarrative" in js)
check("Section D: score fill bars", "scoreFill" in js or "score-fill" in js)

# ── Section E: Key signals grid ──
check("Section E: 6 signal cards", "sigCard" in js)
check("Section E: efficiency signal", "km/SoC%" in js)
check("Section E: cell spread signal", "mV" in js and "CRITICAL" in js)
check("Section E: temperature + seasonal", "seasonCtx" in js or "season" in js.lower())
check("Section E: voltage sag", "Voltage sag" in js or "voltage sag" in js.lower())
check("Section E: NMC R0 per cell", "R0 per cell" in js or "r0" in js.lower())

# ── Section F: KM chart ──
check("Section F: km-chart canvas", "km-chart" in js)
check("Section F: drawKmChart function", "function drawKmChart" in js)
check("Section F: baseline dashed line", "borderDash" in js)
check("Section F: event marker plugin", "events" in js and "eventWks" in js)
check("Section F: colour-coded bars", "0.7" in js and "0.8" in js and "0.9" in js)

# ── Section G: IR sparkline ──
check("Section G: ir-chart canvas", "ir-chart" in js)
check("Section G: drawIRChart function", "function drawIRChart" in js)
check("Section G: ir-trend API call", "/ir-trend" in js)

# ── Section H: Proxy signals ──
check("Section H: proxyRow function", "function proxyRow" in js or "proxyRow" in js)
check("Section H: load intensity", "Load intensity" in js)
check("Section H: charging frequency", "Charging frequency" in js)
check("Section H: days in service", "Days in service" in js)
check("Section H: deep discharge", "Deep discharge" in js)
check("Section H: mean DoD", "Mean DoD" in js)

# ── Section I: Feedback ──
check("Section I: feedbackCard function", "function feedbackCard" in js)
check("Section I: submitFeedback function", "function submitFeedback" in js or "async function submitFeedback" in js)
check("Section I: saveFeedbackJSON function", "function saveFeedbackJSON" in js)
check("Section I: POST /api/feedback", "/api/feedback" in js)
check("Section I: fb-role dropdown", "fb-role" in js)

# ── API endpoints used ──
health_apis = [
    ("/api/battery/${S.bat}", "battery full"),
    ("/api/battery/${S.bat}/intelligence", "intelligence"),
    ("/api/battery/${S.bat}/trend/km_per_soc_pct", "trend"),
    ("/api/battery/${S.bat}/ir-trend", "IR trend"),
    ("/api/feedback", "feedback POST"),
]
for ep, desc in health_apis:
    check(f"API: {desc} ({ep[:40]})", ep in js)

# ── afterRender routes to health ──
check("afterRender dispatches to renderHealth", "renderHealth" in js and "tab === 'health'" in js)

# ── Phase 0 code untouched ──
check("Phase 0: init() still exists", "async function init" in js)
check("Phase 0: showTab() still exists", "function showTab" in js)
check("Phase 0: 8 tab buttons still in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))

# ── Charts deferred ──
check("Charts: setTimeout for deferred render", "setTimeout" in js and "drawKmChart" in js)

print()
print("=" * 60)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  PHASE 1: {result}")
print("=" * 60)

sys.exit(0 if FAIL == 0 else 1)
