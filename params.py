"""
Adaptive Threshold Engine — params.py v1.0
All platform thresholds resolve via get_param().
Resolution: 9-level segmentation hierarchy.
"""
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(r"C:\Users\Admin\Desktop\Ev__ML\enerlytik_production.db")

def _conn():
    return sqlite3.connect(str(DB_PATH))

_profile_cache = {}

def get_battery_profile(battery_id):
    """Load battery profile (cached per pipeline run). Call clear_profile_cache() between runs."""
    if battery_id in _profile_cache:
        return _profile_cache[battery_id]
    result = _enrich_from_battery_master(battery_id)
    _profile_cache[battery_id] = result
    return result

def clear_profile_cache():
    """Clear between pipeline runs."""
    _profile_cache.clear()

def get_template_modules(template_id):
    """Get active module list from template_registry."""
    if not template_id:
        return ["M0","M1","M2","M3","M4","M6"]
    conn = _conn()
    row = conn.execute("SELECT modules_active_json FROM template_registry WHERE template_id=?",
                       (template_id,)).fetchone()
    conn.close()
    if row and row[0]:
        import json
        try:
            return json.loads(row[0])
        except:
            pass
    return ["M0","M1","M2","M3","M4","M6"]

def get_template_param(template_id, param_name, default=None):
    """Get a specific column value from template_registry."""
    if not template_id:
        return default
    conn = _conn()
    try:
        row = conn.execute(f'SELECT "{param_name}" FROM template_registry WHERE template_id=?',
                           (template_id,)).fetchone()
        conn.close()
        if row and row[0] is not None:
            return row[0]
    except:
        conn.close()
    return default

def _enrich_from_battery_master(battery_id):
    """Single query joining battery_master + battery_usage_profile + batteries."""
    conn = _conn()
    row = conn.execute("""
        SELECT b.chemistry, b.battery_model as pack_model,
               bm.operator_id, bm.cell_chemistry_variant,
               bm.bms_soc_bias_pct, bm.iot_data_quality_flag,
               bup.ownership_model, bup.end_use_type, bup.primary_charge_type,
               bup.usage_intensity_tier, bup.city, bup.template_id
        FROM batteries b
        LEFT JOIN battery_master bm ON b.battery_id = bm.battery_id
        LEFT JOIN battery_usage_profile bup ON b.battery_id = bup.battery_id AND bup.is_current = 1
        WHERE b.battery_id = ?
    """, (battery_id,)).fetchone()
    conn.close()
    if row:
        cols = ["chemistry","pack_model","operator_id","cell_chemistry_variant",
                "bms_soc_bias_pct","iot_data_quality_flag","ownership_model",
                "end_use_type","primary_charge_type","usage_intensity_tier","city","template_id"]
        return dict(zip(cols, row))
    return {}

def get_param(param_name, chemistry=None, pack_model=None, vehicle_type=None,
              operation_type=None, city=None, season=None, behaviour_cohort=None,
              age_cohort=None, battery_id=None, operator_id=None,
              oem_id=None, usage_type=None,
              segment_type=None, segment_value=None,
              layer_max=3, default=None):
    """Resolve parameter using 20-level cascade with chemistry + OEM isolation.
    Resolution: explicit segment → battery → operator+chem → oem+pack → oem+chem → pack → city → season → chemistry → ALL → default
    Chemistry is always a required dimension — LFP and NMC never share a resolution path.
    Explicit (segment_type, segment_value) — when both provided — takes highest priority (after battery_id)
    and is tried at layers 3/2/1 before falling through the standard cascade. Used for BMS_CONVENTION,
    FLEET_SEGMENT, VEHICLE_CATEGORY and other bespoke segmentations that live in fleet_context_params
    but aren't enumerated as explicit cascade candidates.
    """
    # Auto-enrich from battery_master if battery_id provided
    if battery_id and not all([chemistry, pack_model]):
        ctx = _enrich_from_battery_master(battery_id)
        chemistry = chemistry or ctx.get("chemistry")
        pack_model = pack_model or ctx.get("pack_model")
        operator_id = operator_id or ctx.get("operator_id")
        city = city or ctx.get("city")
    if battery_id and not oem_id:
        conn_tmp = _conn()
        oem_row = conn_tmp.execute("SELECT oem_name FROM batteries WHERE battery_id=? LIMIT 1", (battery_id,)).fetchone()
        conn_tmp.close()
        if oem_row: oem_id = oem_row[0]

    chemistry = chemistry or 'LFP'
    if oem_id: oem_id = oem_id.upper()  # normalise — batteries table uses mixed case
    conn = _conn()
    cur = conn.cursor()

    # Chemistry-aware SQL — matches exact chemistry OR 'ALL'
    sql = """SELECT param_value FROM fleet_context_params
        WHERE param_name=? AND segment_type=? AND segment_value=?
        AND chemistry IN (?, 'ALL')
        AND (oem_id=? OR oem_id IS NULL)
        AND layer=? AND is_active=1
        AND (valid_until IS NULL OR valid_until > date('now'))
        ORDER BY CASE WHEN chemistry=? THEN 0 ELSE 1 END,
                 CASE WHEN oem_id IS NOT NULL THEN 0 ELSE 1 END
        LIMIT 1"""

    candidates = []
    # 0: Explicit segment_type + segment_value override (caller-specified, highest after battery)
    if segment_type and segment_value:
        for la in [3, 2, 1]:
            candidates.append((segment_type, str(segment_value), oem_id, la))
            if oem_id:
                candidates.append((segment_type, str(segment_value), None, la))
    # 1-3: Battery-specific (any layer)
    if battery_id:
        for la in [3, 2, 1]:
            candidates.append(('BATTERY_ID', str(battery_id), oem_id, la))
    # 4-6: Operator + chemistry
    if operator_id:
        candidates.append(('OPERATOR_CHEMISTRY', f"{operator_id}:{chemistry}", None, 3))
        candidates.append(('OPERATOR_CHEMISTRY', f"{operator_id}:{chemistry}", None, 2))
        candidates.append(('OPERATOR_ID', str(operator_id), None, 3))
    # 7-8: OEM + pack
    if oem_id and pack_model:
        candidates.append(('OEM_PACK', f"{oem_id}:{pack_model}", oem_id, 2))
        candidates.append(('OEM_PACK', f"{oem_id}:{pack_model}", oem_id, 1))
    # 9-11: OEM + chemistry
    if oem_id:
        candidates.append(('OEM_CHEMISTRY', f"{oem_id}:{chemistry}", oem_id, 2))
        candidates.append(('OEM_CHEMISTRY', f"{oem_id}:{chemistry}", oem_id, 1))
        candidates.append(('OEM_ID', str(oem_id), oem_id, 2))
    # 12-13: Pack (fleet-wide)
    if pack_model:
        candidates.append(('PACK', pack_model, None, 2))
        candidates.append(('PACK', pack_model, None, 1))
    # 14-15: City
    if city and usage_type:
        candidates.append(('CITY_USAGE', f"{city}:{usage_type}", None, 2))
    if city:
        candidates.append(('CITY', city, None, 2))
    # 16: Season
    if season:
        candidates.append(('SEASON', season, None, 2))
    # 17-18: Chemistry fleet-wide
    candidates.append(('CHEMISTRY', chemistry, None, 2))
    candidates.append(('CHEMISTRY', chemistry, None, 1))
    # 19-20: ALL (cross-chemistry fleet default)
    candidates.append(('ALL', 'ALL', None, 1))

    for seg_type, seg_value, oem, layer in candidates:
        row = cur.execute(sql, (param_name, seg_type, seg_value,
                                chemistry, oem, layer, chemistry)).fetchone()
        if row:
            conn.close()
            return row[0]

    conn.close()
    if default is not None:
        import warnings
        warnings.warn(f"get_param('{param_name}'): using default={default} — param not seeded")
    return default


def get_model_param(param_name, model_family, chemistry='LFP',
                    oem_id=None, cast_type=None, default=None):
    """Resolve model training parameter. OEM-specific L3 → fleet L1 → default."""
    conn = _conn()
    candidates = []
    if oem_id:
        candidates += [(oem_id, chemistry, 3), (oem_id, chemistry, 2), (oem_id, 'ALL', 2)]
    candidates += [(None, chemistry, 2), (None, chemistry, 1), (None, 'ALL', 1)]

    sql = """SELECT param_value, param_type FROM model_training_params
        WHERE param_name=? AND model_family=? AND chemistry IN (?,'ALL')
        AND (oem_id=? OR oem_id IS NULL) AND layer=? AND is_active=1
        ORDER BY CASE WHEN oem_id IS NOT NULL THEN 0 ELSE 1 END,
                 CASE WHEN chemistry=? THEN 0 ELSE 1 END
        LIMIT 1"""

    for oem, chem, layer in candidates:
        row = conn.execute(sql, (param_name, model_family, chem, oem, layer, chem)).fetchone()
        if row:
            conn.close()
            val, ptype = row[0], row[1]
            t = cast_type or ptype
            if t == 'int': return int(val)
            if t == 'float': return float(val)
            if t == 'bool': return str(val).lower() == 'true'
            if t == 'list':
                import json; return json.loads(val)
            return val
    conn.close()
    return default


def upsert_param(param_name, new_value, param_basis, segment_type, segment_value,
                 layer, chemistry='LFP', oem_id=None, sample_n=None,
                 changed_by="SYSTEM_CALIBRATION",
                 change_reason=None, valid_until=None, computed_from=None,
                 override_floor=None, override_ceiling=None):
    """Insert or update a parameter. Always writes audit trail."""
    conn = _conn()
    cur = conn.cursor()

    # Get current value
    existing = cur.execute("""
        SELECT param_value FROM fleet_context_params
        WHERE param_name=? AND segment_type=? AND segment_value=? AND layer=?
    """, (param_name, segment_type, segment_value, layer)).fetchone()
    old_value = existing[0] if existing else None

    # Get physics floor/ceiling from ANY row with this param_name (layer 1 preferred)
    bounds = cur.execute("""
        SELECT override_floor, override_ceiling FROM fleet_context_params
        WHERE param_name=? AND override_floor IS NOT NULL
        ORDER BY layer ASC LIMIT 1
    """, (param_name,)).fetchone()
    floor = override_floor if override_floor is not None else (bounds[0] if bounds else None)
    ceiling = override_ceiling if override_ceiling is not None else (bounds[1] if bounds else None)

    # Validate against physics floors
    if floor is not None and new_value < floor:
        conn.close()
        return {"success": False, "error": f"Value {new_value} below override_floor {floor}"}
    if ceiling is not None and new_value > ceiling:
        conn.close()
        return {"success": False, "error": f"Value {new_value} above override_ceiling {ceiling}"}

    # Confidence from sample_n
    if sample_n is not None:
        confidence = "HIGH" if sample_n >= 30 else "MEDIUM" if sample_n >= 10 else "LOW"
    else:
        confidence = "ESTIMATED"

    now = datetime.now().isoformat()

    # Upsert
    cur.execute("""INSERT OR REPLACE INTO fleet_context_params
        (param_name, param_value, param_basis, layer, segment_type, segment_value,
         computed_from, sample_n, confidence, valid_from, valid_until,
         override_floor, override_ceiling, changed_by, change_reason, changed_at, is_active)
        VALUES (?,?,?,?,?,?,?,?,?,date('now'),?,?,?,?,?,?,1)
    """, (param_name, new_value, param_basis, layer, segment_type, segment_value,
          computed_from, sample_n, confidence, valid_until,
          override_floor or floor, override_ceiling or ceiling,
          changed_by, change_reason, now))

    # Audit
    cur.execute("""INSERT INTO fleet_context_params_audit
        (param_name, segment_type, segment_value, layer,
         old_value, new_value, changed_by, change_reason, changed_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (param_name, segment_type, segment_value, layer,
          old_value, new_value, changed_by, change_reason, now))

    conn.commit()
    conn.close()
    return {"success": True, "old_value": old_value, "new_value": new_value, "param_name": param_name}


def get_range_floor(pack_model=None, city=None, conn=None):
    """Returns operational range floor for given pack_model and city.
    Layer 2 (pack-specific) takes precedence over Layer 1 (chemistry default).
    City adjustment added on top.
    Never returns below 40km (absolute physics minimum for any LFP pack).
    """
    close_conn = False
    if conn is None:
        conn = _conn()
        close_conn = True

    base_floor = None

    # Layer 2: pack-specific floor
    if pack_model:
        row = conn.execute(
            "SELECT param_value FROM fleet_context_params WHERE param_name='range_floor_km' AND segment_type='PACK' AND segment_value=? AND is_active=1",
            (pack_model,)
        ).fetchone()
        if row:
            base_floor = float(row[0])

    # Layer 1 fallback: chemistry default
    if base_floor is None:
        row = conn.execute(
            "SELECT param_value FROM fleet_context_params WHERE param_name='range_floor_km' AND segment_type='CHEMISTRY' AND segment_value='LFP' AND layer=1 AND is_active=1"
        ).fetchone()
        base_floor = float(row[0]) if row else 56.0

    # City adjustment
    city_adj = 0.0
    if city:
        row = conn.execute(
            "SELECT param_value FROM fleet_context_params WHERE param_name='range_floor_city_adj_km' AND segment_type='CITY' AND segment_value=? AND is_active=1",
            (city,)
        ).fetchone()
        if row:
            city_adj = float(row[0])

    if close_conn:
        conn.close()

    return max(40.0, base_floor + city_adj)


def get_dod_rated(vehicle_category=None, chemistry=None, conn=None):
    """Returns rated usable DoD for range formula.
    Priority: vehicle_category > chemistry > 0.80 default.
    """
    close_conn = False
    if conn is None:
        conn = _conn()
        close_conn = True

    result = None

    if vehicle_category:
        row = conn.execute(
            """SELECT param_value FROM fleet_context_params
               WHERE param_name='dod_rated_pct'
               AND segment_type='VEHICLE_CATEGORY' AND segment_value=?
               AND is_active=1 ORDER BY layer DESC LIMIT 1""",
            (vehicle_category,)
        ).fetchone()
        if row:
            result = float(row[0])

    if result is None and chemistry:
        row = conn.execute(
            """SELECT param_value FROM fleet_context_params
               WHERE param_name='dod_rated_pct'
               AND segment_type='CHEMISTRY' AND segment_value=?
               AND layer=1 AND is_active=1 LIMIT 1""",
            (chemistry,)
        ).fetchone()
        if row:
            result = float(row[0])

    if close_conn:
        conn.close()

    return result if result is not None else 0.80
