"""
enerlytik — Loan Approval Intelligence API v2.2
v2.1 + fare rates from fleet_context_params + cargo/LCV graceful decline.
"""
import json, math, os, sqlite3, sys, time
from datetime import datetime
from pathlib import Path
from flask import (Blueprint, request, jsonify, Response,
                   stream_with_context, send_from_directory)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.env_config import ENERLYTIK_DB_PATH

DB_PATH = str(ENERLYTIK_DB_PATH)
loan_bp = Blueprint('loan', __name__)

_GROQ_KEY = os.getenv("GROQ_API_KEY", "")
_ENV_FILE = Path(__file__).parent / ".env"
if not _GROQ_KEY and _ENV_FILE.exists():
    for ln in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        if ln.startswith("GROQ_API_KEY="): _GROQ_KEY = ln.split("=", 1)[1].strip()

# ═══════════════════════════════════════════════════════════
# SECTION 1: CONSTANTS
# ═══════════════════════════════════════════════════════════
OPERATIONAL_P50 = {
    'GF_LFP_Pack3001': {'months': 18.4, 'source': 'OBSERVED_COHORT', 'n': 193},
    'GF_LFP_Pack1201': {'months': 36.6, 'source': 'OBSERVED_COHORT', 'n': 49},
    'GF_LFP_Pack3401': {'months': 12.6, 'source': 'OBSERVED_COHORT', 'n': 76},
    'GF_LFP_Pack3301': {'months': 16.8, 'source': 'TRANSFER_PRIOR',
                        'transfer_from': 'GF_LFP_Pack3001', 'n': 8, 'confidence': 'LOW'},
}
COMBINED_P50 = {'GF_LFP_Pack3001': 10.2, 'GF_LFP_Pack1201': 20.3,
                'GF_LFP_Pack3401': 7.0, 'GF_LFP_Pack3301': 9.3}
OEM_FRACTION = 0.562  # Updated from 1,378 service records: CHEMISTRY+BMS=692 / (total-FALSE_ALARM=1232)
WARRANTY_MONTHS = 36
WARRANTY_CLAIM_CERTAINTY = 0.80
LIO_MILEAGE_BANDS = {(0, 12): 0.85, (13, 24): 0.75, (25, 36): 0.60}
FLEET_WARRANTY_STATUS = {
    'GF_LFP_Pack3001': {'pct_commissioned': 0.936, 'age_months': 8.3,
                        'band_floor': 0.85, 'status': 'WITHIN_BAND'},
    'GF_LFP_Pack1201': {'pct_commissioned': 0.893, 'age_months': 7.9,
                        'band_floor': 0.85, 'status': 'WITHIN_BAND'},
    'GF_LFP_Pack3401': {'pct_commissioned': 0.737, 'age_months': 6.4,
                        'band_floor': 0.85, 'status': 'BELOW_BAND', 'claim_flag': True},
}
CITY_NAMES = {'CU': 'Cuttack', 'SR': 'Surat', 'AK': 'Akola', 'AD': 'Ahmedabad'}
_FARE_FALLBACKS = {'CU': 2.50, 'SR': 3.00, 'AK': 2.75, 'AD': 2.75}

def get_city_fares():
    """Read fare rates from fleet_context_params. Falls back to assumed values."""
    conn = sqlite3.connect(DB_PATH)
    fares = {}
    for city in ['CU', 'SR', 'AK', 'AD']:
        row = conn.execute("""
            SELECT param_value, confidence FROM fleet_context_params
            WHERE param_name = ? AND segment_value = ? AND is_active = 1
            ORDER BY layer DESC LIMIT 1
        """, (f'fare_per_km_{city}', city)).fetchone()
        fb = _FARE_FALLBACKS.get(city, 2.50)
        if row:
            fares[city] = {'fare_per_km': float(row[0]), 'working_days': 25,
                           'source': 'fleet_context_params',
                           'disclosure': None if row[1] != 'ESTIMATED'
                               else f'Fare \u20b9{float(row[0])}/km is ESTIMATED for {city}. Confirm with operators.'}
        else:
            fares[city] = {'fare_per_km': fb, 'working_days': 25,
                           'source': 'ASSUMED_UNCONFIRMED',
                           'disclosure': f'Fare \u20b9{fb}/km assumed for {city}. Not in fleet_context_params.'}
    conn.close()
    return fares

PASSENGER_PACKS = ['GF_LFP_Pack3001','GF_LFP_Pack1201','GF_LFP_Pack3401','GF_LFP_Pack3301','GF_LFP_Pack2605']
NON_GF_PREFIXES = ['FGHHL','FGKKL','RGEAK','LCV']

def detect_fleet_type(pack_model, n_vehicles, nominal_ah=None):
    """Check if passenger e-rickshaw (calibrated) or cargo/LCV (not yet)."""
    if pack_model and any(pack_model.upper().startswith(p) for p in NON_GF_PREFIXES):
        return {'is_passenger': False, 'reason': 'NON_GF_OEM',
                'message': f'Pack {pack_model} is from a different OEM (non-Greenfuel). No survival priors available.'}
    if pack_model and not any(pack_model.startswith(p[:15]) for p in PASSENGER_PACKS):
        return {'is_passenger': False, 'reason': 'UNKNOWN_PACK_TYPE',
                'message': f'Pack {pack_model} not in calibrated passenger e-rickshaw cohort. Cargo/LCV under development.'}
    if nominal_ah and float(nominal_ah) > 130:
        return {'is_passenger': False, 'reason': 'HIGH_CAPACITY_PACK',
                'message': f'Nominal capacity {nominal_ah}Ah exceeds passenger range (105Ah). LCV model not calibrated.'}
    return {'is_passenger': True}
CITY_MULTIPLIERS = {'CU': 1.00, 'SR': 0.88, 'AK': 0.95}
SEASONAL_MULTIPLIERS = {1:1.05,2:0.90,3:0.87,4:1.00,5:1.08,6:1.15,
                        7:1.18,8:1.22,9:1.28,10:1.12,11:1.05,12:1.02}

# ═══════════════════════════════════════════════════════════
# SECTION 2: COMMISSIONING BASELINE STRESS
# ═══════════════════════════════════════════════════════════
def get_commissioning_baseline_stress(pack_model, city_code):
    conn = sqlite3.connect(DB_PATH)
    fleet = conn.execute("""
      SELECT MAX(v.trip_count)*0.90, MAX(v.current_discharge_mean)*0.90, MAX(v.dod_mean)*0.90
      FROM vehicle_weekly_features v JOIN batteries b ON v.battery_id = b.battery_id
      WHERE b.battery_model LIKE 'GF_LFP%' AND v.week_number BETWEEN 1 AND 12
    """).fetchone()
    cohort = conn.execute("""
      SELECT AVG(v.trip_count), AVG(v.current_discharge_mean), AVG(v.dod_mean),
             AVG(v.shallow_charge_pct), AVG(v.km_per_day), COUNT(DISTINCT v.battery_id), COUNT(*)
      FROM vehicle_weekly_features v JOIN batteries b ON v.battery_id = b.battery_id
      WHERE b.battery_model = ? AND b.city_code = ? AND v.week_number BETWEEN 1 AND 12
    """, (pack_model, city_code)).fetchone()
    conn.close()
    if not cohort or not cohort[0] or not fleet or not fleet[0]:
        return {'stress_index': 0.40, 'components': {}, 'raw': {},
                'n_cohort': 0, 'n_weeks': 0, 'data_source': 'FLEET_MEDIAN_FALLBACK',
                'disclosure': 'Insufficient commissioning data. Using fleet median.'}
    p90_t = max(fleet[0], 1); p90_c = max(fleet[1], 0.01); p90_d = max(fleet[2], 0.01)
    tn = min(1.0, (cohort[0] or 0) / p90_t)
    cn = min(1.0, (cohort[1] or 0) / p90_c)
    sn = min(1.0, (cohort[3] or 0) / 100.0)
    dn = min(1.0, (cohort[2] or 0) / p90_d)
    si = round(min(1.0, max(0.0, tn*0.35 + cn*0.30 + sn*0.20 + dn*0.15)), 3)
    return {
        'stress_index': si,
        'components': {'trips_norm': round(tn,3), 'c_rate_norm': round(cn,3),
                       'shallow_norm': round(sn,3), 'dod_norm': round(dn,3)},
        'raw': {'avg_trips': round(cohort[0] or 0,1), 'avg_c_rate': round(cohort[1] or 0,3),
                'avg_dod': round(cohort[2] or 0,3), 'avg_shallow_pct': round(cohort[3] or 0,1),
                'avg_km_day': round(cohort[4] or 0,1)},
        'n_cohort': cohort[5], 'n_weeks': cohort[6],
        'data_source': 'COMMISSIONING_WEEKS_1_12',
        'disclosure': 'Stress baseline from commissioning weeks 1-12. Represents new operator behaviour, not aged fleet.'
    }

# ═══════════════════════════════════════════════════════════
# SECTION 3: TIME OF DAY FACTOR
# ═══════════════════════════════════════════════════════════
def get_time_of_day_factor(pack_model, city_code):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("""
          SELECT CAST(strftime('%H', c.timestamp) AS INTEGER) as hour, COUNT(*) as n
          FROM can_raw_lfp c JOIN batteries b ON c.battery_id = b.battery_id
          WHERE b.battery_model = ? AND b.city_code = ? AND c.current_a < 0
          LIMIT 50000
        """, (pack_model, city_code)).fetchall()
        conn.close()
        if not rows: return 1.0, 'DEFAULT', 'No CAN discharge data'
        total = sum(r[1] for r in rows)
        if total == 0: return 1.0, 'DEFAULT', 'Zero discharge rows'
        peak = sum(r[1] for r in rows if r[0] in range(10,16)) / total
        eve = sum(r[1] for r in rows if r[0] in range(18,22)) / total
        if peak > 0.60: return 1.15, 'HIGH_MIDDAY', f'Peak hrs {peak:.0%}'
        if peak > 0.40: return 1.05, 'MODERATE', f'Peak hrs {peak:.0%}'
        if eve > 0.50: return 0.92, 'EVENING_DOMINANT', f'Evening hrs {eve:.0%}'
        return 1.00, 'MIXED', f'Peak {peak:.0%}, evening {eve:.0%}'
    except Exception as e:
        try: conn.close()
        except: pass
        return 1.0, 'DEFAULT', f'CAN query skipped: {str(e)[:60]}'

# ═══════════════════════════════════════════════════════════
# SECTION 4: WARRANTY STATUS
# ═══════════════════════════════════════════════════════════
def get_warranty_status(pack_model, tenure_months):
    """Query live warranty status from BHS (Sprint 6 W4) with fallback to constants."""
    conn = sqlite3.connect(DB_PATH)
    # Get fleet-level warranty stats for this pack from live DB
    r = conn.execute("""
        SELECT AVG(pct_of_commissioned) as avg_pct, AVG(age_months) as avg_age,
               SUM(CASE WHEN warranty_status='BELOW_BAND_CLAIM_ELIGIBLE' THEN 1 ELSE 0 END) as n_claim,
               COUNT(*) as n
        FROM battery_health_scores_v2 h
        JOIN batteries b ON h.battery_id = b.battery_id
        WHERE b.battery_model = ? AND h.warranty_status IS NOT NULL
    """, (pack_model,)).fetchone()
    conn.close()

    if not r or not r[3]:
        ws = FLEET_WARRANTY_STATUS.get(pack_model)
        if not ws:
            return {'warranty_remaining_months': 0, 'is_below_band': False,
                    'claim_eligible': False, 'warranty_extension_months': 0,
                    'pct_commissioned': None, 'band_floor': None,
                    'disclosure': f'[PROXY-LiO] No warranty data for {pack_model}'}
        age, pct_c = ws['age_months'], ws['pct_commissioned']
        below = ws['status'] == 'BELOW_BAND'
    else:
        age, pct_c = r[1], r[0] / 100.0 if r[0] else None
        below = r[2] > 0

    remaining = max(0, WARRANTY_MONTHS - (age or 0))
    claim = below and remaining > 0
    ext = round(min(tenure_months, remaining) * OEM_FRACTION * WARRANTY_CLAIM_CERTAINTY, 1) if not claim else 0
    status_str = 'BELOW_BAND_CLAIM_ELIGIBLE' if claim else 'WITHIN_BAND' if not below else 'BELOW_BAND_MONITORING'
    return {
        'warranty_remaining_months': round(remaining, 1),
        'is_below_band': below, 'claim_eligible': claim,
        'warranty_extension_months': ext,
        'pct_commissioned': pct_c,
        'band_floor': 0.85, 'status': status_str,
        'disclosure': f"[PROXY\u2014LiO] {'Below band \u2014 claim eligible' if claim else 'Within warranty band'}"
    }

# ═══════════════════════════════════════════════════════════
# SECTION 5: COMPUTE SURVIVAL
# ═══════════════════════════════════════════════════════════
def compute_survival(pack_model, city_code, stress, tod_factor,
                     charging_profile, tenure_months):
    prior = OPERATIONAL_P50.get(pack_model)
    if not prior:
        return {'viable': False, 'reason': 'DATA_LIMITED',
                'detail': f'{pack_model} not in observed cohort.'}
    city_mult = CITY_MULTIPLIERS.get(city_code)
    if city_mult is None:
        return {'viable': False, 'reason': 'DATA_ANOMALY',
                'detail': f'City {city_code} flagged as anomaly.'}
    op_p50 = prior['months']
    si = stress['stress_index']
    stress_mult = max(0.55, min(1.0, (1.0 - si * 0.35) * ((2.0 - tod_factor) / 1.0)))
    charge_mult = {'overnight_full': 1.15, 'partial_topup': 0.95, 'irregular': 0.78}.get(charging_profile, 1.0)
    month = datetime.now().month
    seasonal_adj = 1.0 - ((SEASONAL_MULTIPLIERS.get(month, 1.0) - 1.0) * 0.20)
    adj_r = round(op_p50 * city_mult * stress_mult * charge_mult * seasonal_adj, 1)
    adj_o = round(adj_r * 1.30, 1)

    ws = get_warranty_status(pack_model, tenure_months)
    warranty_voided = charging_profile == 'irregular'
    w_ext = 0 if warranty_voided else ws['warranty_extension_months']
    adj_with_warranty = round(adj_r + w_ext, 1)
    max_tenure = max(3, round(adj_with_warranty * 0.80))

    return {
        'viable': True,
        'combined_p50': COMBINED_P50.get(pack_model),
        'operational_p50': op_p50,
        'adj_realistic_months': adj_r, 'adj_optimistic_months': adj_o,
        'warranty_extension_months': w_ext,
        'warranty_voided_by_charging': warranty_voided,
        'warranty_status': ws,
        'adj_with_warranty_months': adj_with_warranty,
        'max_viable_tenure_months': max_tenure,
        'adjustments': {'city_multiplier': city_mult, 'stress_multiplier': round(stress_mult,3),
                        'stress_index': si, 'tod_factor': tod_factor,
                        'charging_multiplier': charge_mult,
                        'seasonal_multiplier': round(seasonal_adj,3), 'season_month': month},
        'data_source': prior['source'],
        'transfer_applied': prior['source'] == 'TRANSFER_PRIOR',
        'transfer_from': prior.get('transfer_from'),
        'n_cohort': prior['n'],
        'confidence': prior.get('confidence', 'HIGH') if prior['source'] != 'TRANSFER_PRIOR' else prior.get('confidence', 'LOW')
    }

# ═══════════════════════════════════════════════════════════
# SECTION 6: INCOME PROXY
# ═══════════════════════════════════════════════════════════
# Confirmed cohort km/day from live cross-week aggregate query
COHORT_KM_DAY = {
    ('GF_LFP_Pack3001', 'CU'): 198.1,
    ('GF_LFP_Pack1201', 'CU'): 47.8,
    ('GF_LFP_Pack3401', 'SR'): 161.8,
    ('GF_LFP_Pack3301', 'AK'): 103.3,
}

def compute_income_proxy(pack_model, city_code, n_vehicles):
    avg_km = COHORT_KM_DAY.get((pack_model, city_code))
    if avg_km: src = 'CONFIRMED_COHORT'
    else: avg_km, src = 80.0, 'FLEET_MEDIAN_FALLBACK'
    fares = get_city_fares()
    fc = fares.get(city_code, {'fare_per_km': 2.75, 'working_days': 25, 'source': 'FALLBACK', 'disclosure': None})
    fare, days = fc['fare_per_km'], fc['working_days']
    cons_net = round(avg_km * 0.80 * fare * days * 0.40)
    real_net = round(avg_km * fare * days * 0.50)
    return {'avg_km_per_day': round(avg_km,1), 'fare_per_km': fare, 'working_days': days,
            'source': src, 'fare_source': fc.get('source', 'FALLBACK'),
            'fare_disclosure': fc.get('disclosure'),
            'per_vehicle': {'conservative_net': cons_net, 'realistic_net': real_net},
            'fleet_total': {'conservative': cons_net * n_vehicles, 'realistic': real_net * n_vehicles},
            'n_vehicles': n_vehicles}

# ═══════════════════════════════════════════════════════════
# SECTION 7: SOP GATES
# ═══════════════════════════════════════════════════════════
def check_sop_gates(borrower, survival, income):
    gates = []
    if not survival.get('viable'):
        gates.append({'rule': 211, 'result': 'FAIL', 'note': survival.get('detail', 'DATA_LIMITED')})
        return {'route': 'REFER', 'gates': gates, 'income_coverage': 0}

    # Service profile gate (Rule 216)
    svc = borrower.get('service_profile', 'STANDARD')
    if svc == 'AT_RISK':
        gates.append({'rule': 216, 'result': 'FAIL',
            'note': 'AT_RISK service profile \u2014 hard block. Operator neglect confirmed.'})
        return {'route': 'DECLINE', 'gates': gates, 'income_coverage': 0}
    elif svc == 'NEGLIGENT':
        gates.append({'rule': 216, 'result': 'WARN',
            'note': f'NEGLIGENT service profile \u2014 credit committee review required. +15% survival loading.'})

    tenure = borrower.get('tenure_months', 24)
    loan = borrower.get('loan_amount_inr', 40000)
    emi = (loan / tenure) * 1.15
    net = income['fleet_total']['realistic']
    icr = round(net / max(emi, 1), 2)
    # Rule 212 before 210
    if borrower.get('existing_portfolio_grade') == 'D':
        gates.append({'rule': 212, 'result': 'FAIL',
            'note': f'Existing Grade D portfolio. ICR {icr}\u00d7 viable \u2014 governance block, not asset failure. Resolve Grade D first.'})
        return {'route': 'REFER', 'gates': gates, 'income_coverage': icr, 'monthly_emi': round(emi)}
    if icr < 1.0:
        gates.append({'rule': 210, 'result': 'FAIL',
            'note': f'Income coverage {icr:.2f} < 1.0. EMI \u20b9{emi:,.0f}/mo vs capacity \u20b9{net:,.0f}/mo'})
        return {'route': 'DECLINE', 'gates': gates, 'income_coverage': icr, 'monthly_emi': round(emi)}
    max_t = survival['max_viable_tenure_months']
    route = 'AGENT'
    if tenure > max_t:
        gates.append({'rule': 213, 'result': 'WARN',
            'note': f'Tenure {tenure}mo > max viable {max_t}mo. Agent will recommend reduction.'})
    if survival.get('transfer_applied'):
        gates.append({'rule': 214, 'result': 'INFO',
            'note': f'TRANSFER_PRIOR from {survival["transfer_from"]}. n={survival["n_cohort"]}. Confidence: {survival["confidence"]}'})
    ws = survival.get('warranty_status', {})
    if ws.get('claim_eligible'):
        gates.append({'rule': 215, 'result': 'INFO',
            'note': f'WARRANTY_CLAIM_ELIGIBLE: Pack below LiO band ({ws["pct_commissioned"]:.0%} vs {ws["band_floor"]:.0%}). Collateral protected during claim.'})
    if (icr >= 2.0 and survival['adj_with_warranty_months'] >= tenure * 1.3
        and borrower.get('existing_portfolio_grade') not in ['C', 'D']
        and not survival.get('transfer_applied')):
        gates.append({'rule': 'AUTO', 'result': 'PASS', 'note': 'All gates clear. Auto-approve eligible.'})
        route = 'AUTO_APPROVE'
    elif not any(g['rule'] == 213 for g in gates):
        gates.append({'rule': 'AGENT', 'result': 'PASS', 'note': 'Standard case \u2014 routing to agent.'})
    return {'route': route, 'gates': gates, 'income_coverage': icr, 'monthly_emi': round(emi),
            'max_viable_tenure': max_t}

# ═══════════════════════════════════════════════════════════
# SECTION 8: SSE ENDPOINT
# ═══════════════════════════════════════════════════════════
def _sse(d): return f"data: {json.dumps(d)}\n\n"

@loan_bp.route('/api/loan/approve', methods=['POST'])
def approve_loan():
    b = request.json
    pack, city = b.get('pack_model',''), b.get('city','')
    charging = b.get('charging_profile', 'overnight_full')
    n_veh = b.get('n_vehicles', 1)
    tenure = b.get('tenure_months', 12)

    def generate():
        # Fleet type gate — cargo/LCV graceful decline
        ft = detect_fleet_type(pack, n_veh, b.get('nominal_ah'))
        if not ft['is_passenger']:
            yield _sse({'step':'sop','text': '// FLEET TYPE CHECK\n  \u2717 ' + ft['reason'] + '\n  ' + ft['message']})
            time.sleep(0.5)
            yield _sse({'step':'decision','decision':'REFER','conditions':[],'covenants':[],
                        'income_coverage':0.0,'survival':None,'income':None,'stress':None,
                        'confidence':'NOT_APPLICABLE','full_reasoning':ft['message'],
                        'fleet_type_block':True,'fleet_type_reason':ft['reason']})
            return

        yield _sse({'step':'sop','text':'// STEP 1 \u2014 SOP GATE CHECK'})
        time.sleep(0.3)
        stress = get_commissioning_baseline_stress(pack, city)
        tod_f, tod_lbl, tod_note = get_time_of_day_factor(pack, city)
        survival = compute_survival(pack, city, stress, tod_f, charging, tenure)
        income = compute_income_proxy(pack, city, n_veh)
        gates = check_sop_gates(b, survival, income)
        sym = {' PASS': '\u2713', 'FAIL': '\u2717', 'WARN': '\u26a0', 'INFO': '\u2139'}
        gl = []
        for g in gates['gates']:
            s = sym.get(g['result'], '\u00b7')
            gl.append(f"  {s} Rule {g['rule']}: {g['note']}")
        gl.append(f"\n  Income coverage: {gates['income_coverage']:.2f}\u00d7")
        gl.append(f"  EMI estimate: \u20b9{gates.get('monthly_emi',0):,}/mo")
        if survival.get('viable'):
            gl.append(f"  Max viable tenure: {survival['max_viable_tenure_months']}mo")
        gl.append(f"\n  // stress baseline: commissioning weeks 1-12 (not week-65 fleet)")
        gl.append(f"  // {stress.get('disclosure','')}")
        gl.append(f"\n  \u2192 Routing to: {gates['route']}")
        yield _sse({'step':'sop','text':'\n'.join(gl)})
        time.sleep(0.8)

        # STEP 2 — INTELLIGENCE ASSEMBLY
        yield _sse({'step':'rag','text':'// STEP 2 \u2014 INTELLIGENCE ASSEMBLY'})
        time.sleep(0.3)
        cn = CITY_NAMES.get(city, city)
        il = _build_intel(survival, stress, tod_lbl, tod_f, tod_note, charging, income, n_veh, pack, cn, b)
        yield _sse({'step':'rag','text':'\n'.join(il)})
        time.sleep(0.8)

        # HARD EXITS
        if gates['route'] in ['DECLINE','REFER'] and any(g['result']=='FAIL' for g in gates['gates']):
            reason = next(g['note'] for g in gates['gates'] if g['result']=='FAIL')
            yield _sse({'step':'reasoning','text':'Hard gate triggered \u2014 agent reasoning bypassed.\n'+reason})
            time.sleep(0.3)
            dec = 'DECLINE' if gates['route']=='DECLINE' else 'REFER'
            conds = ['Manual underwriter review'] if dec=='REFER' else []
            yield _sse({'step':'decision','decision':dec,'conditions':conds,'covenants':[],
                        'income_coverage':gates['income_coverage'],'survival':survival,'income':income,
                        'stress':stress,'confidence':'HIGH','full_reasoning':reason})
            return
        if gates['route'] == 'AUTO_APPROVE':
            r = (f"Auto-approve: {pack} {cn} observed cohort (n={survival['n_cohort']}). "
                 f"Operational P50 {survival['adj_realistic_months']}mo + warranty {survival['warranty_extension_months']}mo "
                 f"= {survival['adj_with_warranty_months']}mo. ICR {gates['income_coverage']:.2f}\u00d7.")
            yield _sse({'step':'reasoning','text':r})
            time.sleep(0.3)
            yield _sse({'step':'decision','decision':'APPROVE','conditions':[],'covenants':['Grade C threshold trigger'],
                        'income_coverage':gates['income_coverage'],'survival':survival,'income':income,
                        'stress':stress,'confidence':'HIGH','full_reasoning':r})
            return

        # STEP 3 — AGENT REASONING
        yield _sse({'step':'reasoning','text':'// STEP 3 \u2014 AGENT REASONING\n'})
        time.sleep(0.5)
        full = ''
        if _GROQ_KEY:
            try:
                import httpx
                hdrs = {"Authorization": f"Bearer {_GROQ_KEY}", "Content-Type": "application/json"}
                payload = {"model": "llama-3.3-70b-versatile", "messages": [
                    {"role": "system", "content": _sys_prompt(survival, stress, gates)},
                    {"role": "user", "content": _usr_prompt(b, survival, stress, tod_lbl, tod_f, income, gates, cn)}
                ], "temperature": 0.3, "max_tokens": 500, "stream": True}
                with httpx.stream("POST", "https://api.groq.com/openai/v1/chat/completions",
                                  json=payload, headers=hdrs, timeout=30.0) as resp:
                    for line in resp.iter_lines():
                        if line.startswith("data: "):
                            d = line[6:]
                            if d.strip() == "[DONE]": break
                            try:
                                delta = json.loads(d)["choices"][0]["delta"].get("content","")
                                if delta: full += delta; yield _sse({'step':'reasoning','text':delta,'append':True})
                            except: continue
            except Exception as e:
                full += f"\n[LLM unavailable: {str(e)[:80]}]"
                yield _sse({'step':'reasoning','text':full,'append':True})
        else:
            full = _rule_reasoning(b, survival, gates, income, cn)
            yield _sse({'step':'reasoning','text':full,'append':True})
        time.sleep(0.5)
        dec = _parse_dec(full)
        conf = 'HIGH' if survival['n_cohort']>30 and not survival.get('transfer_applied') else 'MEDIUM' if survival['n_cohort']>8 else 'LOW'
        if survival.get('transfer_applied'): conf = 'LOW'
        yield _sse({'step':'decision','decision':dec,'conditions':[],'covenants':['Grade C threshold trigger'],
                    'income_coverage':gates['income_coverage'],'survival':survival,'income':income,
                    'stress':stress,'confidence':conf,'full_reasoning':full})
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

def _build_intel(s, stress, tod_lbl, tod_f, tod_note, charging, income, n_veh, pack, cn, borrower=None):
    if not s.get('viable'): return [f"  \u26a0 {s.get('reason')}: {s.get('detail')}"]
    ws = s.get('warranty_status',{})
    return [
        f"  Pack: {pack} \u00b7 City: {cn} \u00b7 n={s['n_cohort']}",
        f"  Source: {s['data_source']}" + (' [TRANSFER PRIOR]' if s.get('transfer_applied') else ''),
        f"", f"  Combined P50 (reference): {s['combined_p50']} months",
        f"  Operational P50 (OEM failures removed, {OEM_FRACTION:.1%}): {s['operational_p50']} months",
        f"  // OEM_Design failures ({OEM_FRACTION:.1%}) removed from denominator",
        f"  // Remaining risk = operator-borne only",
        f"", f"  Adjustments:",
        f"    City ({cn}): \u00d7{s['adjustments']['city_multiplier']}",
        f"    Stress (commissioning wk1-12): {stress['stress_index']:.3f} \u2192 \u00d7{s['adjustments']['stress_multiplier']}",
        f"      trips={stress['raw']['avg_trips']:.1f} c_rate={stress['raw']['avg_c_rate']:.3f} dod={stress['raw']['avg_dod']:.3f} shallow={stress['raw']['avg_shallow_pct']:.0f}%",
        f"    Time-of-day ({tod_lbl}): \u00d7{tod_f} \u2014 {tod_note}",
        f"    Charging ({charging}): \u00d7{s['adjustments']['charging_multiplier']}",
        f"    Season (month {s['adjustments']['season_month']}): \u00d7{s['adjustments']['seasonal_multiplier']}",
        f"", f"  Adjusted survival: {s['adj_realistic_months']} months",
        f"  Warranty extension (LiO {WARRANTY_MONTHS}mo): +{s['warranty_extension_months']} months",
        f"    // LiO warranty backstops OEM_Design failures ({OEM_FRACTION:.1%})",
        f"    // Status: {ws.get('disclosure','N/A')}" + (f" ({ws.get('pct_commissioned',0):.0%} vs {ws.get('band_floor',0):.0%} floor)" if ws.get('pct_commissioned') else ''),
        *([f"    // \u26a0 Irregular charging may void warranty on charging-related failures"] if s.get('warranty_voided_by_charging') else []),
        f"  Adjusted with warranty: {s['adj_with_warranty_months']} months",
        f"  Max viable tenure: {s['max_viable_tenure_months']} months",
        f"", f"  Income proxy (telemetry):",
        f"    Avg km/day: {income['avg_km_per_day']} ({income['source']})",
        f"    Fare: \u20b9{income['fare_per_km']}/km ({income.get('fare_source','?')})",
        *([ f"    \u26a0 {income['fare_disclosure']}"] if income.get('fare_disclosure') else []),
        f"    Net/vehicle: \u20b9{income['per_vehicle']['realistic_net']:,}/mo",
        f"    Fleet ({n_veh} veh): \u20b9{income['fleet_total']['realistic']:,}/mo",
        f"    ICR: (see gates)",
        *([ f"", f"  Operator profile:",
            f"    Drive profile: {borrower.get('drive_profile','STANDARD') if borrower else 'N/A'}",
            f"    Driver stress tier: {borrower.get('driver_stress_tier','NORMAL') if borrower else 'N/A'}",
            *([ f"    \u26a0 AGGRESSIVE drive profile \u2014 higher operational stress loading"] if borrower and borrower.get('drive_profile')=='AGGRESSIVE' else []),
            *([ f"    \u26a0 HIGH driver stress tier \u2014 above fleet P75 discharge intensity"] if borrower and borrower.get('driver_stress_tier')=='HIGH' else []),
        ] if borrower else [])
    ]

def _sys_prompt(s, stress, gates):
    return f"""You are enerlystAI, loan intelligence agent for enerlytik.
Max viable tenure = {s.get('max_viable_tenure_months',12)} months.
Operational P50 = {s.get('adj_realistic_months')}mo (OEM failures {OEM_FRACTION:.1%} removed).
Warranty adds {s.get('warranty_extension_months',0)} months.
Combined P50 was {s.get('combined_p50')}mo — do NOT use for tenure.
If tenure > max viable: recommend shorter, not rejection.
Survival based on operational replacement records — batteries replaced when range becomes revenue-unviable, not electrochemical EOL.
DECISION FORMAT: APPROVE / APPROVE_WITH_CONDITIONS (CONDITIONS: / COVENANTS:) / REFER (REASON:) / DECLINE (REASON:)
Under 350 words. Use numbers."""

def _usr_prompt(b, s, stress, tod_lbl, tod_f, income, gates, cn):
    return f"""Borrower: {b.get('borrower_name')} | {b.get('pack_model')} | {cn}
Use: {b.get('use_case')} | Charging: {b.get('charging_profile')} | {b.get('n_vehicles')} veh
Loan: \u20b9{b.get('loan_amount_inr',0):,} | Tenure: {b.get('tenure_months')}mo | Grade: {b.get('existing_portfolio_grade','None')}
P50: combined={s.get('combined_p50')}mo operational={s.get('adj_realistic_months')}mo +warranty={s.get('warranty_extension_months',0)}mo = {s.get('adj_with_warranty_months')}mo
Max tenure: {s.get('max_viable_tenure_months')}mo | Stress: {stress['stress_index']:.3f} (commissioning) | ToD: {tod_lbl} ({tod_f})
ICR: {gates['income_coverage']:.2f}x | EMI: \u20b9{gates.get('monthly_emi',0):,}/mo
Drive profile: {b.get('drive_profile','STANDARD')} | Driver stress tier: {b.get('driver_stress_tier','NORMAL')}
{('NOTE: AGGRESSIVE drive profile — higher operational stress.' if b.get('drive_profile')=='AGGRESSIVE' else 'NOTE: HIGH driver stress tier — above fleet P75 discharge intensity.' if b.get('driver_stress_tier')=='HIGH' else '')}Gates: {' | '.join(g['note'] for g in gates['gates'])}
Assess."""

def _rule_reasoning(b, s, gates, income, cn):
    t = b.get('tenure_months',12); max_t = s['max_viable_tenure_months']; icr = gates['income_coverage']
    lines = [f"Assessment: {b.get('borrower_name')} \u2014 {b.get('pack_model')} in {cn}\n"]
    lines.append(f"Operational P50: {s['adj_realistic_months']}mo + warranty {s['warranty_extension_months']}mo = {s['adj_with_warranty_months']}mo.")
    lines.append(f"Max viable tenure: {max_t}mo. Requested: {t}mo. ICR: {icr:.2f}x.\n")
    if t > max_t:
        lines += [f"Tenure {t}mo exceeds max viable {max_t}mo. Recommend {max_t}mo.",
                  f"\nDECISION: APPROVE_WITH_CONDITIONS",
                  f"  CONDITIONS: Reduce tenure to {max_t} months",
                  f"  COVENANTS: Grade C threshold trigger"]
    elif icr >= 1.5 and s['adj_with_warranty_months'] >= t * 1.2:
        lines += ["Strong coverage.", "\nDECISION: APPROVE\n  COVENANTS: Grade C threshold trigger"]
    elif icr >= 1.0:
        lines += ["Adequate but marginal.", f"\nDECISION: APPROVE_WITH_CONDITIONS",
                  f"  CONDITIONS: Quarterly review", f"  COVENANTS: Grade C threshold trigger"]
    else:
        lines += [f"\nDECISION: DECLINE\n  REASON: ICR {icr} below 1.0"]
    return "\n".join(lines)

def _parse_dec(t):
    u = t.upper()
    if 'DECISION: APPROVE_WITH_CONDITIONS' in u: return 'APPROVE_WITH_CONDITIONS'
    if 'DECISION: APPROVE' in u: return 'APPROVE'
    if 'DECISION: DECLINE' in u: return 'DECLINE'
    return 'REFER'

# ═══════════════════════════════════════════════════════════
# SECTION 9-11: ENDPOINTS
# ═══════════════════════════════════════════════════════════
@loan_bp.route('/api/loan/borrowers')
def get_borrowers():
    return jsonify([
        {'id':1,'borrower_name':'Meena Logistics','city':'CU','city_display':'Cuttack',
         'pack_model':'GF_LFP_Pack3001','pack_display':'Pack3001','use_case':'standard_urban',
         'charging_profile':'overnight_full','n_vehicles':12,'loan_amount_inr':480000,
         'tenure_months':14,'existing_portfolio_grade':'None','expected':'APPROVE',
         'scenario_note':'Best cohort \u00b7 overnight charging \u00b7 operational P50 unlocked',
         'service_profile':'STANDARD','nominal_ah':None,
         'warranty_note':'Within LiO band (93.6%)'},
        {'id':2,'borrower_name':'Ravi Transport','city':'SR','city_display':'Surat',
         'pack_model':'GF_LFP_Pack3401','pack_display':'Pack3401','use_case':'quick_commerce',
         'charging_profile':'overnight_full','n_vehicles':8,'loan_amount_inr':320000,
         'tenure_months':10,'existing_portfolio_grade':'None','expected':'APPROVE_WITH_CONDITIONS',
         'scenario_note':'High-risk pack \u00b7 warranty claim active \u00b7 short tenure',
         'service_profile':'STANDARD','nominal_ah':None,
         'warranty_note':'\u26a0 Below LiO band (73.7%)'},
        {'id':3,'borrower_name':'GreenRide Ops','city':'CU','city_display':'Cuttack',
         'pack_model':'GF_LFP_Pack3001','pack_display':'Pack3001','use_case':'standard_urban',
         'charging_profile':'irregular','n_vehicles':4,'loan_amount_inr':160000,
         'tenure_months':11,'existing_portfolio_grade':'B','expected':'APPROVE_WITH_CONDITIONS',
         'scenario_note':'Good pack \u00b7 irregular charging voids warranty',
         'service_profile':'STANDARD','nominal_ah':None,
         'warranty_note':'Within LiO band (93.6%) \u2014 warranty voided by charging'},
        {'id':4,'borrower_name':'Sharma Fleet','city':'CU','city_display':'Cuttack',
         'pack_model':'GF_LFP_Pack1201','pack_display':'Pack1201','use_case':'porter_logistics',
         'charging_profile':'partial_topup','n_vehicles':6,'loan_amount_inr':150000,
         'tenure_months':24,'existing_portfolio_grade':'B','expected':'APPROVE_WITH_CONDITIONS',
         'scenario_note':'Pack1201 best RUL \u00b7 operational P50 36.6mo \u00b7 low km/day',
         'service_profile':'STANDARD','nominal_ah':None,
         'warranty_note':'Within LiO band (89.3%)'},
        {'id':5,'borrower_name':'FastDeliver Co','city':'SR','city_display':'Surat',
         'pack_model':'GF_LFP_Pack3401','pack_display':'Pack3401','use_case':'quick_commerce',
         'charging_profile':'irregular','n_vehicles':5,'loan_amount_inr':200000,
         'tenure_months':6,'existing_portfolio_grade':'D','expected':'REFER',
         'scenario_note':'Grade D portfolio \u00b7 governance block \u00b7 ICR viable',
         'service_profile':'STANDARD','nominal_ah':None,
         'warranty_note':'\u26a0 Below LiO band (73.7%) \u2014 warranty voided by charging'},
    ])

@loan_bp.route('/api/loan/portfolio-monitor')
def portfolio_monitor():
    return jsonify([{
        'borrower':'Ravi Transport','city_display':'Surat','pack':'Pack3401',
        'use_case':'Quick commerce','weeks_since_disbursement':10,
        'batteries_at_risk':3,'total_batteries':8,'breach_probability':0.67,
        'current_collateral_inr':22000,'original_collateral_inr':40000,
        'loan_outstanding_inr':280000,'collateral_gap_inr':54000,
        'alert_level':'COVENANT_BREACH',
        'trigger':'Charging drifted OVERNIGHT \u2192 IRREGULAR (week 7)',
        'stress_index_at_origination':0.32,'stress_index_now':0.74,'stress_drift':'+0.42',
        'warranty_status':'BELOW_BAND \u2014 warranty claim active',
        'oem_attribution_pct':35,'warranty_claim_eligible':True,
        'recommended_action':'Covenant review call this week. 3 of 8 batteries crossed Grade C. '
            'Charging covenant breached week 7. Collateral gap \u20b954,000.'
    }])

@loan_bp.route('/loan-approval')
def loan_approval_page():
    return send_from_directory(str(Path(__file__).parent.parent), 'loan_approval_demo.html')
