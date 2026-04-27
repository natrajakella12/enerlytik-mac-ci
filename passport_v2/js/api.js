// api.js — API client for enerlytik passport v2
const API_BASE = 'http://localhost:3001';

async function apiFetch(endpoint) {
    try {
        const res = await fetch(`${API_BASE}${endpoint}`);
        if (res.ok) {
            STATE.apiOnline = true;  // any successful call = online
        }
        if (!res.ok) {
            console.error(`API ${endpoint}: HTTP ${res.status}`);
            return null;
        }
        return await res.json();
    } catch (err) {
        if (STATE.apiOnline) console.warn(`API ${endpoint}:`, err.message);
        return null;
    }
}

const API = {
    health:              ()     => apiFetch('/api/health'),
    fleetBatteries:      (c)    => apiFetch(`/api/fleet/batteries${c ? '?chemistry='+c : ''}`),
    fleetSummary:        (c)    => apiFetch(`/api/fleet/summary${c ? '?chemistry='+c : ''}`),
    fleetTierDist:       (c)    => apiFetch(`/api/fleet/tier-distribution${c ? '?chemistry='+c : ''}`),
    fleetAtRisk:         (c,n)  => apiFetch(`/api/fleet/at-risk?limit=${n||10}${c ? '&chemistry='+c : ''}`),
    fleetEventsRecent:   (c)    => apiFetch(`/api/fleet/events/recent${c ? '?chemistry='+c : ''}`),
    batteryFull:         (id)   => apiFetch(`/api/battery/${id}`),
    batteryIntelligence: (id)   => apiFetch(`/api/battery/${id}/intelligence`),
    batteryScores:       (id)   => apiFetch(`/api/battery/${id}/scores`),
    batteryDiagnostic:   (id)   => apiFetch(`/api/battery/${id}/diagnostic`),
    batteryTimeseries:   (id)   => apiFetch(`/api/battery/${id}/timeseries`),
    batteryEcm:          (id)   => apiFetch(`/api/battery/${id}/ecm`),
    batteryPredictions:  (id)   => apiFetch(`/api/battery/${id}/predictions`),
    batterySurvival:     (id)   => apiFetch(`/api/battery/${id}/survival`),
    batteryTelemetry:    (id,d) => apiFetch(`/api/battery/${id}/telemetry?days=${d||7}`),
    batteryEvents:       (id)   => apiFetch(`/api/battery/${id}/events`),
    batteryInvestigation:(id)   => apiFetch(`/api/battery/${id}/investigation`),
    batteryService:      (id)   => apiFetch(`/api/battery/${id}/service`),
    batteryDekf:         (id)   => apiFetch(`/api/battery/${id}/dekf`),
    batteryDekfStatus:   (id)   => apiFetch(`/api/battery/${id}/dekf/status`),
    batteryReasoning:    (id)   => apiFetch(`/api/battery/${id}/reasoning`),
    batteryKbContext:    (id)   => apiFetch(`/api/battery/${id}/kb-context`),
    gpsCheck:            ()     => apiFetch('/api/platform/gps-check'),
    modelsHealth:        ()     => apiFetch('/api/models/health'),
    apiFetch:            (ep)   => apiFetch(ep),
};

// ── State ──
const STATE = { chemistry: 'LFP', batteryId: null, apiOnline: false };

function setChemistry(chem) {
    STATE.chemistry = chem;
    document.querySelectorAll('.chem-btn').forEach(b => b.classList.toggle('active', b.dataset.chem === chem));
    if (typeof clearCache === 'function') clearCache();
    populateBatteries();
    updateKPIBar();
    loadCurrentTab();
}

function setBattery(id) {
    STATE.batteryId = id;
    loadCurrentTab();
}

async function checkApiHealth() {
    try {
        const r = await fetch(`${API_BASE}/api/health`, { signal: AbortSignal.timeout(3000) });
        STATE.apiOnline = r.ok;
    } catch { STATE.apiOnline = false; }
    return STATE.apiOnline;
}
