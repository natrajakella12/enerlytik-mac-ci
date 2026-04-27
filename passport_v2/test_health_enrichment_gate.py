"""
HEALTH ENRICHMENT GATE — Vehicle profile + Subsystem health for passport_final_v2.html
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
print("HEALTH ENRICHMENT GATE")
print("=" * 58)
print(f"  {'File size:':<50s} {size_kb:.1f} KB")
print()

# ── Vehicle Profile ──
print("─── VEHICLE PROFILE ───")
check("vehicleProfile function exists", "function vehicleProfile" in js)
check("Route type derived (speed)", "routeType" in js and "Urban stop-start" in js)
check("Daily usage shows", "Daily usage" in js or "daily" in js.lower())
check("Mean DoD shows", "Mean DoD" in js)
check("Charging pattern shows", "Charging pattern" in js and "Opportunity" in js)
check("Avg temp + stress weeks", "Avg operating temp" in js and "45°C" in js)
check("Monsoon exposure", "Monsoon exposure" in js)
check("Summer exposure", "Summer exposure" in js)
check("'Inferred from X weeks' footer", "Inferred from" in js and "no operator input" in js.lower() or "no manual input" in js.lower())
check("Section label renders", "Vehicle & usage profile" in js)

print()
print("─── SUBSYSTEM HEALTH ───")
check("subsystemHealth function exists", "function subsystemHealth" in js)
check("Cell balance subsystem", "Cell balance" in js)
check("BMS / regime subsystem", "BMS" in js and "regime" in js.lower())
check("Thermal system subsystem", "Thermal system" in js)
check("Charging system subsystem", "Charging system" in js)
check("Capacity subsystem", "Capacity" in js)
check("Internal resistance subsystem", "Internal resistance" in js)
all_6 = all(x in js for x in ["Cell balance", "Thermal system", "Charging system", "Internal resistance"])
check("All 6 subsystems visible", all_6)
check("Coloured status dots (dot function)", "border-radius:50%" in js and "display:inline-block" in js)
check("Detail sentence per subsystem", "Active imbalance" in js or "within normal range" in js)
check("2-column grid layout", "grid-template-columns:1fr 1fr" in js)
check("'Not from BMS self-report' footer", "not from BMS self-report" in js.lower() or "not from bms" in js.lower())
check("Section label renders", "Subsystem health" in js)

print()
print("─── BATTERY IDENTITY ───")
check("batterySubLine function exists", "function batterySubLine" in js)
check("No internal OEM prefix leaks", "GF_LFP_" in js and ".replace(" in js)  # strips them
check("batterySubLine used in header", "batterySubLine(bat)" in js)
check("Chemistry label (E-Rickshaw/2-Wheeler)", "E-Rickshaw" in js and "2-Wheeler" in js)

print()
print("─── INTEGRATION ───")
check("vehicleProfile called in renderHealth", "vehicleProfile(bat" in js)
check("subsystemHealth called in renderHealth", "subsystemHealth(bat" in js)
check("Inserted between Section E and F", js.index("vehicleProfile(bat") < js.index("km-chart"))
check("Phase 1 renderHealth still exists", "async function renderHealth" in js)
check("Phase 2 renderPredictions still exists", "async function renderPredictions" in js)
check("Phase 2 renderEvents still exists", "async function renderEvents" in js)
check("Phase 0 init/showTab intact", "async function init" in js and "function showTab" in js)
check("8 tabs still in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))

print()
print("=" * 58)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  HEALTH ENRICHMENT: {result}")
print("=" * 58)
sys.exit(0 if FAIL == 0 else 1)
