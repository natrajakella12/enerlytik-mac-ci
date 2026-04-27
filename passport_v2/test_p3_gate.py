"""
PHASE 3 GATE — Deepdive + Service + DEKF tabs for passport_final_v2.html
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
print("PHASE 3 GATE — DEEPDIVE + SERVICE + DEKF")
print("=" * 62)
print(f"  {'File size:':<55s} {size_kb:.1f} KB")
print()

# ── DEEPDIVE ──
print("─── DEEPDIVE ───")
check("renderDeepdive() exists", "async function renderDeepdive" in js)
check("VERDICT_CONFIG (5+ verdicts)", "VERDICT_CONFIG" in js and "PHYSICS_CONFIRMED" in js and "PROGRESSIVE" in js and "WEAK_SIGNAL" in js and "UNCONFIRMED" in js)
check("Verdict banner with Rule 28", "Rule 28" in js and "verdict requires" in js)
check("deepNarrative function", "function deepNarrative" in js)
check("Complaint timeline renders", "Complaint timeline" in js or "complaint_date" in js)
check("buildEvidenceChain function", "function buildEvidenceChain" in js)
check("Evidence table with sigma/contribution", "FROM BASELINE" in js and "CONTRIBUTION" in js)
check("reasoningChain function (4 layers)", "function reasoningChain" in js)
check("Layer 1 — Raw signals", "Layer 1" in js and "Raw signals" in js)
check("Layer 2 — Computed features", "Layer 2" in js and "Computed" in js)
check("Layer 3 — Model outputs", "Layer 3" in js and "Model" in js)
check("Layer 4 — Composite intelligence", "Layer 4" in js and "Composite" in js)
check("External conditions panel", "External conditions" in js and "Peak Summer" in js)
check("OEM panel (NMC only)", "OEM action panel" in js and "WARRANTY" in js)
check("LFP telemetry-only note", "LFP investigation" in js or "LFP e-rickshaws" in js)
check("API: /investigation endpoint", "/investigation" in js)
check("API: /events endpoint used", "/events" in js)

print()
print("─── SERVICE ───")
check("renderService() exists", "async function renderService" in js)
check("Disclaimer banner first", "Service scheduling only" in js)
check("NMC survival P30/P60/P90 bars", "p_complaint_30d" in js and "P(complaint" in js)
check("Median days to complaint", "median days" in js.lower() or "median_days" in js)
check("Silent degrader warning", "Silent degrader" in js and "Rule 29" in js)
check("Service priority card", "PRIORITY" in js and "schedule inspection" in js)
check("Complaint history table", "complaint_date" in js and "diagnosis_category" in js)
check("LFP graceful message", "LFP service forecast" in js)
check("API: /service endpoint", "/service" in js)

print()
print("─── DEKF ───")
check("renderDEKF() exists", "async function renderDEKF" in js)
check("Shadow banner", "SHADOW MODE ACTIVE" in js)
check("4-track promotion grid (NMC R0/LFP R0/NMC SoC/LFP SoC)", all(x in js for x in ["NMC R0","LFP R0","NMC SoC","LFP SoC"]))
check("PROMOTED badge", "PROMOTED" in js)
check("SHADOW badge", "'SHADOW'" in js)
check("R0 chart canvas", "dekf-r0-chart" in js)
check("drawDEKFR0Chart function", "function drawDEKFR0Chart" in js)
check("Rule 14 note (LFP R0 thermal)", "Rule 14" in js and "thermal proxy" in js)
check("SoC physics explanation (flat OCV)", "flat" in js and "3.2" in js and "3.4V" in js)
check("Promotion checklist (4 items)", "R0 MAPE" in js and "SoC correction" in js and "diverged" in js)
check("Promote command displayed", "dekf_promote.py" in js)
check("API: /dekf endpoint", "/dekf" in js)
check("API: /dekf/status endpoint", "/dekf/status" in js)

print()
print("─── INTEGRATION ───")
check("afterRender dispatches deepdive", "tab === 'deepdive'" in js)
check("afterRender dispatches service", "tab === 'service'" in js)
check("afterRender dispatches dekf", "tab === 'dekf'" in js)
check("Phase 1 renderHealth intact", "async function renderHealth" in js)
check("Phase 2 renderPredictions intact", "async function renderPredictions" in js)
check("Phase 2 renderEvents intact", "async function renderEvents" in js)
check("Phase 0 init/showTab intact", "async function init" in js and "function showTab" in js)
check("8 tabs still in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))

print()
print("=" * 62)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  PHASE 3: {result}")
print("=" * 62)
sys.exit(0 if FAIL == 0 else 1)
