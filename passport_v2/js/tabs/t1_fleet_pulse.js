// T1 Fleet Pulse — fleet KPIs, tier doughnut, at-risk table, recent events, model health
// v2.1 — Intelligence Applied upgrade

const EVENT_LABELS = {
    'E1':'Efficiency regime change','E2':'Cell imbalance','E3':'Thermal stress',
    'E4':'Deep discharge','E5':'Charging anomaly','E6':'Usage shift',
    'E7':'Voltage sag','E9':'IR escalation','E10':'BMS protection trip',
    'E11':'Self discharge','E14':'Resistance acceleration','E15':'Capacity fade',
    'E1_EFFICIENCY_REGIME_CHANGE':'Efficiency regime change',
    'E2_CELL_IMBALANCE':'Cell imbalance','E3_THERMAL_STRESS':'Thermal stress',
    'E4_DEEP_DISCHARGE':'Deep discharge','E5_CHARGING_ANOMALY':'Charging anomaly',
    'E6_USAGE_SHIFT':'Usage shift','E7_VOLTAGE_SAG':'Voltage sag',
};

const SEV_LABELS = {
    'SEV-1':  {label:'CRITICAL', color:'#dc2626'},
    'SEV-2':  {label:'HIGH',     color:'#ea580c'},
    'SEV-3':  {label:'MEDIUM',   color:'#d97706'},
    'SEV-4':  {label:'LOW',      color:'#888'},
    'CRITICAL':{label:'CRITICAL', color:'#dc2626'},
    'WARNING': {label:'HIGH',     color:'#ea580c'},
    'HIGH':    {label:'HIGH',     color:'#ea580c'},
    'MEDIUM':  {label:'MEDIUM',   color:'#d97706'},
    'LOW':     {label:'LOW',      color:'#888'},
};

function eventDescription(code) {
    if (EVENT_LABELS[code]) return EVENT_LABELS[code];
    const match = code && code.match(/^E\d+/);
    if (match && EVENT_LABELS[match[0]]) return EVENT_LABELS[match[0]];
    return 'Unknown event (' + (code||'?') + ')';
}

function sevBadge(sev) {
    const s = SEV_LABELS[sev] || {label:sev||'?', color:'#888'};
    return '<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-family:var(--font-mono);font-size:9px;font-weight:600;background:' + s.color + '15;color:' + s.color + ';border:1px solid ' + s.color + '30">' + s.label + '</span>';
}

function chemBadgeMini(batId) {
    const isNmc = batId && batId.includes('NMC');
    const label = isNmc ? 'NMC' : 'LFP';
    const col = isNmc ? '#1d4ed8' : '#16a34a';
    return '<span style="font-family:var(--font-mono);font-size:8px;color:' + col + ';opacity:0.6;margin-left:4px">' + label + '</span>';
}

async function loadT1() {
    setTabContent(loadingSkeleton(6));
    const chem = STATE.chemistry;
    const [tierData, atRisk, events, models, summary] = await Promise.all([
        API.fleetTierDist(chem),
        API.fleetAtRisk(chem, 5),
        API.fleetEventsRecent(chem),
        API.modelsHealth(),
        API.fleetSummary(chem),
    ]);

    let html = '';

    // Section 0 — Fleet Intelligence Header (from bat-intel)
    const total = summary ? (typeof summary.fleet_size === 'object' ? (summary.fleet_size[chem]||0) : summary.total_batteries || summary.total || 0) : 0;
    const eehi = summary ? (summary.eehi || summary.EEHI) : null;
    const avgRange = summary ? (summary.avg_range_km || summary.fleet_avg_range) : null;
    const lastScored = summary ? (summary.last_scored || summary.last_score_date) : null;

    html += '<div class="card" style="border-left:3px solid #f97316">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">';
    html += '<div>';
    html += '<div style="font-size:9px;font-family:var(--font-mono);color:#f97316;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:4px">◈ Fleet Intelligence — ' + chem + '</div>';
    html += '<div style="display:flex;align-items:baseline;gap:16px;flex-wrap:wrap">';
    if (eehi != null) {
        html += '<div><span style="font-family:var(--font-head);font-size:32px;font-weight:800;color:#f97316">' + (typeof eehi === 'number' ? eehi.toFixed(1) : eehi) + '</span><span style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-left:4px">EEHI</span></div>';
    }
    html += '<span style="font-family:var(--font-mono);font-size:12px;color:var(--text2)">' + total + ' batteries</span>';
    if (avgRange != null) html += '<span style="font-family:var(--font-mono);font-size:12px;color:var(--text2)">Avg range: ' + (typeof avgRange === 'number' ? avgRange.toFixed(0) : avgRange) + ' km</span>';
    html += '</div>';
    html += '</div>';
    // Data source + staleness
    html += '<div style="text-align:right">';
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3)">enerlytik_production.db</div>';
    if (lastScored) {
        var days = Math.floor((Date.now() - new Date(lastScored).getTime()) / 86400000);
        var col = days > 14 ? '#d97706' : days > 7 ? '#f59e0b' : '#16a34a';
        html += '<div style="font-family:var(--font-mono);font-size:10px;color:' + col + '">' + days + 'd since last score</div>';
    }
    // Seasonal context
    var month = new Date().getMonth();
    var season = month >= 2 && month <= 4 ? 'Peak Summer' : month >= 5 && month <= 8 ? 'Monsoon' : month >= 10 || month <= 1 ? 'Winter' : 'Post-monsoon';
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-top:2px">Season: ' + season + '</div>';
    html += '</div></div></div>';

    // Section A — Tier Distribution + Fleet Health Summary
    html += '<div class="g2" style="margin-bottom:12px">';
    html += '<div class="card" style="position:relative;min-height:260px">';
    html += '<div class="card-title">Tier Distribution</div>';
    html += '<div style="position:relative;height:220px"><canvas id="tier-doughnut"></canvas></div>';
    html += '</div>';

    // Section B — At Risk (enhanced with click-through)
    html += '<div class="card">';
    html += '<div class="card-title">Top 5 At-Risk Batteries</div>';
    if (atRisk && atRisk.length > 0) {
        html += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
        html += '<tr style="color:var(--text3);font-family:var(--font-mono);font-size:9px"><th style="text-align:left;padding:4px 8px">BATTERY</th><th>TIER</th><th>SCORE</th><th>RISK SIGNAL</th><th>TREND</th></tr>';
        atRisk.forEach(function(b) {
            var trend = b.trend === 'ACCELERATING' ? '<span style="color:#ef4444">↑ ACCEL</span>' : b.trend === 'IMPROVING' ? '<span style="color:#22c55e">↓ IMPROV</span>' : '<span style="color:#888">→</span>';
            html += '<tr style="border-top:1px solid #e4e2de;cursor:pointer" onclick="setBattery(\'' + b.battery_id + '\');setTab(\'t2\')">';
            html += '<td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">' + b.battery_id + '</td>';
            html += '<td style="text-align:center">' + tierBadge(b.tier) + '</td>';
            html += '<td style="text-align:center;font-family:var(--font-mono)">' + fmt(b.score,0) + '</td>';
            html += '<td style="font-size:11px;color:var(--text2)">' + (b.risk_signal||'—') + '</td>';
            html += '<td style="text-align:center;font-size:10px">' + trend + '</td>';
            html += '</tr>';
        });
        html += '</table>';
    } else {
        html += emptyState('No at-risk batteries', 'All batteries within normal range');
    }
    html += '</div></div>';

    // Section C — Recent Events (enhanced with descriptions + context)
    html += '<div class="card">';
    html += '<div style="font-size:9px;font-family:var(--font-mono);color:#f97316;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">◈ Intelligence Applied</div>';
    html += '<div class="card-title">Recent Events</div>';
    if (events && events.length > 0) {
        // Event type summary counts
        var evtCounts = {};
        events.forEach(function(e) {
            var code = (e.event_code || e.event_type || '?').replace(/_.*/, '');
            evtCounts[code] = (evtCounts[code]||0) + 1;
        });
        html += '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">';
        Object.keys(evtCounts).sort().forEach(function(code) {
            var col = code === 'E1' || code === 'E2' || code === 'E3' ? '#ea580c' : '#d97706';
            html += '<span style="font-family:var(--font-mono);font-size:9px;padding:2px 8px;border-radius:10px;background:' + col + '12;color:' + col + ';border:1px solid ' + col + '25">' + code + ' ×' + evtCounts[code] + '</span>';
        });
        html += '</div>';

        html += '<div style="max-height:240px;overflow-y:auto">';
        events.forEach(function(e) {
            var code = e.event_code || e.event_type || '?';
            var desc = eventDescription(code);
            html += '<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid #e8e7e3;font-size:11px;align-items:center">';
            html += '<span style="font-family:var(--font-mono);color:var(--text1);min-width:100px;cursor:pointer" onclick="setBattery(\'' + e.battery_id + '\');setTab(\'t5\')">' + e.battery_id + chemBadgeMini(e.battery_id) + '</span>';
            html += '<span style="color:var(--text3);font-size:10px;min-width:32px;font-family:var(--font-mono)">W' + (e.week_number||'?') + '</span>';
            html += '<span style="min-width:70px">' + sevBadge(e.severity) + '</span>';
            html += '<span style="color:var(--text2);font-size:11px;flex:1">' + desc + '</span>';
            html += '</div>';
        });
        html += '</div>';
    } else {
        html += emptyState('No recent events');
    }
    html += '</div>';

    // Section D — Model Health (enhanced with key metrics)
    html += '<div class="g2">';
    if (models) {
        ['LFP','NMC'].forEach(function(c) {
            var m = models[c] || {};
            html += '<div class="card">';
            html += '<div class="card-title">' + c + ' Model Status</div>';
            html += '<div style="font-size:12px;line-height:2">';
            if (c === 'LFP') {
                html += '<div>Active: <strong style="color:var(--accent)">range_t2t1_ensemble v1.0.0</strong></div>';
                html += '<div>MAPE: <strong style="color:var(--accent)">' + (m.mape||'14.07') + '%</strong> · Fallback: T1b LGBM 16.11%</div>';
                html += '<div>Per-battery: <strong>104</strong> Ridge models (mean 9.3%)</div>';
            } else {
                html += '<div>Complaint: <strong style="color:#1d4ed8">nmc_complaint v3.1.0</strong> · AUC 0.692</div>';
                html += '<div>Survival: <strong style="color:#1d4ed8">nmc_survival v2.1.0</strong> · C-index 0.600</div>';
                html += '<div>DEKF R0: <strong>dekf_nmc_r0 v2.0</strong> · 7.3% vs ECM</div>';
            }
            html += '<div>Scored: <strong>' + (m.scored||0) + '</strong> batteries</div>';
            html += '<div style="font-size:10px;color:var(--text3)">Last: ' + (m.last_scored ? m.last_scored.slice(0,16) : '—') + '</div>';
            html += '</div></div>';
        });
    }
    html += '</div>';

    // Section E — Fleet Intelligence Summary
    if (summary) {
        var fleetSummaryText = summary.fleet_summary || summary.summary_text;
        if (fleetSummaryText) {
            html += '<div class="card">';
            html += '<div style="font-size:9px;font-family:var(--font-mono);color:#f97316;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">◈ Intelligence Applied — Fleet Summary</div>';
            html += '<div style="font-size:12px;color:var(--text2);line-height:1.7">' + fleetSummaryText + '</div>';
            html += '</div>';
        }
    }

    setTabContent(html);

    // Render doughnut
    if (tierData && tierData.tiers) {
        var labels = tierData.tiers.map(function(t) { return t.tier; });
        var counts = tierData.tiers.map(function(t) { return t.count; });
        var colors = labels.map(function(t) { return TIER_COLORS[t] || '#555'; });
        makeChart('tier-doughnut', 'doughnut', {
            labels: labels, datasets: [{ data: counts, backgroundColor: colors, borderWidth: 0 }]
        }, {
            legend: true,
            cutout: '60%',
            plugins: {
                legend: { position:'bottom', labels:{color:'#888',font:{size:10,family:'IBM Plex Mono'},padding:12} }
            }
        });
    }
}
