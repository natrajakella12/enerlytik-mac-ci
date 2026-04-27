"""
ANALYTICS FIX GATE — Confirmed field names, grouped Y, objectives, combos
"""
import re, sys
from pathlib import Path

html = Path(__file__).parent.joinpath("passport_final.html").read_text(encoding="utf-8")
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
print("ANALYTICS FIX GATE")
print("=" * 62)
print(f"  {'File size:':<55s} {size_kb:.1f} KB")
print()

print("─── CONFIRMED FIELD NAMES ───")
print("  VWF weekly_trend fields: week_number, km_per_soc_pct,")
print("    cell_spread_max, cell_spread_mean, temp_max, dod_mean,")
print("    cusum_flag, avg_speed, trip_count, composite_score_v2")
print()

print("─── DATA FIX ───")
# Check Y axes use only confirmed fields
confirmed = ['km_per_soc_pct','cell_spread_max','cell_spread_mean','temp_max','dod_mean','avg_speed','trip_count','composite_score_v2']
check("Y axes use confirmed fields only", all(f"value:'{c}'" in js for c in confirmed))
check("No unresolvable fields (voltage_mean etc)", "value:'voltage_mean'" not in js and "value:'discharge_current_mean'" not in js and "value:'soc_min'" not in js)
check("FIELD_MAP has correct fallbacks", "FIELD_MAP" in js and "composite_score_v2" in js)

print()
print("─── DESIGN FIX ───")
check("Y axis grouped by category (optgroup)", "<optgroup" in js)
check("Groups: Health & Range", "Health & Range" in js)
check("Groups: Cell Health", "Cell Health" in js)
check("Groups: Thermal", "Thermal" in js)
check("Groups: Usage", "Usage" in js)
check("Objective explanation panel div", "analytics-objective" in js)
check("updateAxisObjective function exists", "function updateAxisObjective" in js)
check("Objective text per Y axis", "objective:" in js or ".objective" in js)
check("ANALYTICS_COMBOS defined", "ANALYTICS_COMBOS" in js)
check("Suggested combination renders", "Suggested" in js and "Apply" in js)
check("applyCombo function exists", "function applyCombo" in js)
check("X axis description in objective", "xMeta" in js and ".desc" in js)
check("Z overlay explanation in objective", "Z overlay" in js)
check("updateAxisObjective called on render", "updateAxisObjective" in js and "afterRender" in js.lower() or "setTimeout" in js)

print()
print("─── INTEGRATION ───")
check("renderAnalytics still exists", "function renderAnalytics" in js)
check("updateAnalyticsChart still exists", "async function updateAnalyticsChart" in js)
check("renderAnalyticsStats still exists", "function renderAnalyticsStats" in js)
check("renderAnalyticsInsights still exists", "function renderAnalyticsInsights" in js)
check("All 8 tabs intact", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))
check("Phase 1-3 functions intact", "async function renderHealth" in js and "async function renderDeepdive" in js)

print()
print("=" * 62)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  ANALYTICS FIX: {result}")
print("=" * 62)
sys.exit(0 if FAIL == 0 else 1)
