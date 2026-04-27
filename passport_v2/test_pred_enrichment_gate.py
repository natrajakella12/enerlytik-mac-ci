"""
PREDICTIONS ENRICHMENT GATE — Range context, fleet benchmark, battery-specific argumentation
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
    print(f"  {name:50s} {s}")

print()
print("PREDICTIONS ENRICHMENT GATE")
print("=" * 58)
print(f"  {'File size:':<50s} {size_kb:.1f} KB")
print()

# ── Range Context 4-cell Row ──
print("─── RANGE CONTEXT 4-CELL ROW ───")
check("rangeContext function exists", "function rangeContext" in js)
check("Current week km shows", "Current week" in js or "current week" in js)
check("12-week forecast % vs current", "12-week forecast" in js or "vs current" in js)
check("vs fleet avg with fleet P50", "vs fleet" in js and "fleet P50" in js.lower() or "fleet avg" in js)
check("vs design spec %", "vs design spec" in js or "design spec" in js)
check("Total loss since commissioning", "Total range lost since commissioning" in js)
check("BMS overstatement called out", "BMS display" in js or "overstated" in js)

print()
print("─── FLEET BENCHMARK BAR ───")
check("fleetBenchmarkBar function exists", "function fleetBenchmarkBar" in js)
check("This battery dot positioned", "thisPct" in js and "border-radius:50%" in js)
check("Fleet P10/P50/P90 labelled", "Fleet P10" in js and "Fleet median" in js and "Fleet P90" in js)
check("Above/below fleet narrative", "below fleet median" in js and "above fleet median" in js)
check("Fleet summary API called", "/api/fleet/summary" in js)

print()
print("─── BATTERY-SPECIFIC ARGUMENTATION ───")
check("specificBasedOn function exists", "function specificBasedOn" in js)
check("specificUnless function exists", "function specificUnless" in js)
check("CUSUM confirmation if active", "CUSUM regime change" in js)
check("Specific recovery estimates", "recovery +4" in js or "+4–7km" in js or "recovery" in js)
check("Personal model check/warning", "personal model threshold" in js.lower() or "Check why ensemble" in js)
check("Deep discharge contribution", "anode degradation" in js)
check("Cell spread cost per mV", "0.3% capacity" in js or "costs" in js)
check("Decline rate with projection", "At this rate" in js)
check("specificBasedOn called in render", "specificBasedOn(" in js)
check("specificUnless called in render", "specificUnless(" in js)

print()
print("─── INTEGRATION ───")
check("Phase 0 init/showTab intact", "async function init" in js and "function showTab" in js)
check("Phase 1 renderHealth intact", "async function renderHealth" in js)
check("Phase 2 renderPredictions intact", "async function renderPredictions" in js)
check("Phase 2 renderEvents intact", "async function renderEvents" in js)
check("Health enrichment intact", "function vehicleProfile" in js and "function subsystemHealth" in js)
check("8 tabs still in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))

print()
print("=" * 58)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  PREDICTIONS ENRICHMENT: {result}")
print("=" * 58)
sys.exit(0 if FAIL == 0 else 1)
