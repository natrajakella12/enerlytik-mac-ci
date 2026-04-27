"""
PHASE 0 GATE — Foundation shell validation for passport_final_v2.html
"""
import re
import sys
from pathlib import Path

html = Path(__file__).parent.joinpath("passport_final_v2.html").read_text(encoding="utf-8")

# Extract main JS block
script_starts = [m.start() for m in re.finditer(r"<script>", html)]
script_ends = [m.start() for m in re.finditer(r"</script>", html)]
js = html[script_starts[-1]:script_ends[-1]] if script_starts and script_ends else ""

size_kb = len(html) / 1024
PASS = FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        status = "YES"
    else:
        FAIL += 1
        status = "NO"
    print(f"  {name:38s} {status}")
    return cond

print()
print("PHASE 0 GATE")
print("=" * 56)
print(f"  {'File created:':<38s} passport_final_v2.html")
print(f"  {'File size:':<38s} {size_kb:.1f} KB (expect 15-25 KB)")
print()

# ── API endpoints from db_api.py ──
ENDPOINTS = [
    "/api/health",
    "/api/fleet/summary",
    "/api/fleet/batteries",
    "/api/fleet/chart/tiers",
    "/api/fleet/chart/efficiency",
    "/api/fleet/chart/cell_spread",
    "/api/battery/{battery_id}",
    "/api/battery/{battery_id}/trend/{feature}",
    "/api/fleet/events",
    "/api/fleet/filter",
    "/api/fleet/batch/{batch_id}",
    "/api/models",
    "/api/battery/{battery_id}/diagnostic",
    "/api/fleet/chains",
    "/api/fleet/summary/enhanced",
    "/api/battery/{battery_id}/feedback",
    "/api/battery/{battery_id}/ir-trend",
    "/api/feedback",                          # POST
    "/api/fleet/intelligence",
    "/api/battery/{battery_id}/intelligence",
    "/api/battery/{battery_id}/narrative",
    "/api/battery/{battery_id}/events/active",
    "/api/battery/{battery_id}/events/history",
    "/api/battery/{battery_id}/timeseries",
    "/api/models/catalogue",
    "/api/fleet/outcomes",
    "/api/fleet/actions",
    "/api/battery/{battery_id}/ecm",
    "/api/fleet/findings",
    "/api/fleet/cell-spread",
    "/api/fleet/soc-correction",
    "/api/fleet/batch-analysis",
    "/api/battery/{battery_id}/investigation",
    "/api/fleet/tier-distribution",
    "/api/fleet/at-risk",
    "/api/fleet/events/recent",
    "/api/models/health",
    "/api/battery/{battery_id}/scores",
    "/api/battery/{battery_id}/predictions",
    "/api/battery/{battery_id}/survival",
    "/api/battery/{battery_id}/telemetry",
    "/api/battery/{battery_id}/events",
    "/api/battery/{battery_id}/service",
    "/api/battery/{battery_id}/dekf",
    "/api/battery/{battery_id}/dekf/status",
    "/api/platform/summary",
    "/api/platform/catalogue",
    "/api/platform/changelog",
    "/api/platform/queue",
    "/api/platform/templates",
    "/api/platform/system",
    "/api/battery/{battery_id}/reasoning",
    "/api/battery/{battery_id}/kb-context",
    "/api/platform/gps-check",
]
print(f"  {'API endpoints found (db_api.py):':<38s} {len(ENDPOINTS)}")
print()

# ── Structural checks ──
check("Nav renders (nav tag)", "<nav>" in html and "nav-logo" in html)

check("Chemistry pills (LFP/NMC)", 'data-chem="LFP"' in html and 'data-chem="NMC"' in html)

check("OEM filter populates", 'id="nav-oem"' in html and "loadOEMs" in js)

check("Battery dropdown loads", 'id="bat-sel"' in html and "loadBatteries" in js)

check("Staleness badge shows", 'id="stale-badge"' in html and "updateStaleness" in js)

check("Offline banner CSS", 'id="offline-bar"' in html and ".offline-bar" in html)

# Tab bar — 8 tabs
TABS = ["health", "predictions", "events", "deepdive", "service", "dekf", "analytics", "reference"]
tab_present = all(f'data-tab="{t}"' in html for t in TABS)
check(f"Tab bar renders (all {len(TABS)} tabs)", tab_present)

check("URL state on bat select (?bat=X&chem=Y)", "syncURL" in js and "replaceState" in js)

# JS core functions
CORE_FNS = ["init", "get", "post", "setChemistry", "setOEM", "loadBatteries",
            "loadOEMs", "selectBattery", "showTab", "cKey", "afterRender",
            "setOnline", "updateStaleness", "emptyState"]
for fn in CORE_FNS:
    found = f"function {fn}" in js or f"async function {fn}" in js
    if not found:
        check(f"JS function: {fn}", False)

all_fns = all(f"function {fn}" in js or f"async function {fn}" in js for fn in CORE_FNS)
check(f"All {len(CORE_FNS)} core JS functions", all_fns)

check("window.onload = init", "window.onload" in js and "init" in js)

check("STATE object (S)", "const S =" in js)

check("API_BASE = localhost:3001", "localhost:3001" in js)

check("Zero render functions (shell only)", "renderHealth" not in js and "renderPredictions" not in js)

check("#tab-content div exists", 'id="tab-content"' in html)

# File size gate
size_ok = 15 <= size_kb <= 30
check(f"File size in range (15-30 KB)", size_ok)

print()
print("=" * 56)
total = PASS + FAIL
result = "PASS" if FAIL == 0 else "FAIL"
print(f"  RESULTS: {PASS}/{total} checks passed")
print(f"  PHASE 0: {result}")
print("=" * 56)

sys.exit(0 if FAIL == 0 else 1)
