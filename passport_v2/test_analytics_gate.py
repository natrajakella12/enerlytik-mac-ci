"""
ANALYTICS GATE — Custom axis chart for passport_final.html
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
print("ANALYTICS GATE")
print("=" * 62)
print(f"  {'File size:':<55s} {size_kb:.1f} KB")
print()

check("renderAnalytics() replaces stub", "function renderAnalytics" in js and "Coming next sprint" not in js)
check("ANALYTICS_AXES constant defined", "ANALYTICS_AXES" in js)
check("X axis selector (4 options)", "week_number" in js and "cumulative_km" in js and "cycle_count" in js and "weeks_in_service" in js)
check("Y axis selector (10 options)", sum(1 for a in ["km_per_soc_pct","cell_spread_max","temp_max","voltage_mean","discharge_current_mean","soc_min","charge_cycles","r0_weekly_median","composite_score","dod_mean"] if a in js) >= 9)
check("Z overlay selector (optional)", "ax-z" in js and "None" in js)
check("Chart type selector (line/bar/scatter)", "ax-type" in js and "'line'" in js and "'bar'" in js and "'scatter'" in js)
check("updateAnalyticsChart function exists", "async function updateAnalyticsChart" in js)
check("Chart renders (new Chart)", "new Chart(canvas" in js or "_analyticsChart = new Chart" in js)
check("Line chart type", "type:'line'" in js or "type: 'line'" in js or "'line'" in js)
check("Bar chart type", "'bar'" in js)
check("Scatter chart type", "'scatter'" in js)
check("Z overlay as dashed line", "borderDash" in js and "y2" in js)
check("Stats bar (5 stats)", "renderAnalyticsStats" in js and "Current" in js and "Mean" in js and "Min" in js and "Max" in js and "Trend" in js)
check("Automated insights render", "renderAnalyticsInsights" in js)
check("Threshold insight for cell_spread", "150mV" in js and "CRITICAL threshold" in js)
check("Trend insight (>5% change)", "5" in js and "increased" in js and "decreased" in js)
check("Spike detection", "spike" in js.lower() or "Unusual" in js)
check("Data table (collapsible)", "renderAnalyticsTable" in js and "<details" in js)
check("Preferences saved to localStorage", "localStorage.setItem" in js and "analytics_" in js)
check("Preferences restored on return", "localStorage.getItem" in js and "analytics_" in js)
check("Reset button clears preferences", "function resetAnalytics" in js and "localStorage.removeItem" in js)
check("afterRender calls updateAnalyticsChart", "updateAnalyticsChart" in js and "analytics" in js)

print()
print("─── INTEGRATION ───")
check("Phase 1 renderHealth intact", "async function renderHealth" in js)
check("Phase 2 renderPredictions intact", "async function renderPredictions" in js)
check("Phase 2 renderEvents intact", "async function renderEvents" in js)
check("Phase 3 renderDeepdive intact", "async function renderDeepdive" in js)
check("Phase 3 renderService intact", "async function renderService" in js)
check("Phase 3 renderDEKF intact", "async function renderDEKF" in js)
check("Phase 4 renderReference intact", "function renderReference" in js)
check("8 tabs in HTML", all(f'data-tab="{t}"' in html for t in ["health","predictions","events","deepdive","service","dekf","analytics","reference"]))

print()
print("=" * 62)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  ANALYTICS: {result}")
print("=" * 62)
sys.exit(0 if FAIL == 0 else 1)
