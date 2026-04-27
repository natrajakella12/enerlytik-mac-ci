// T5 Events — timeline, detail cards, summary table

const RELIABILITY_BADGE = {
    HIGH: '<span style="display:inline-block;padding:1px 5px;font-size:8px;font-family:var(--font-mono);border-radius:3px;background:#22c55e22;color:#22c55e;border:1px solid #22c55e44">HIGH</span>',
    MEDIUM: '<span style="display:inline-block;padding:1px 5px;font-size:8px;font-family:var(--font-mono);border-radius:3px;background:#f9731622;color:#f97316;border:1px solid #f9731644">MED</span>',
    LOW: '<span style="display:inline-block;padding:1px 5px;font-size:8px;font-family:var(--font-mono);border-radius:3px;background:transparent;color:#ef4444;border:1px solid #ef4444">LOW</span>',
    SUPPRESSED: '<span style="display:inline-block;padding:1px 5px;font-size:8px;font-family:var(--font-mono);border-radius:3px;background:#ef4444;color:#fff" title="Event fired on GPS-only data during IoT gap — investigate hardware before acting on this event">SUPPRESSED</span>',
};

const EVENT_NAMES = {
    E1:'Efficiency Regime Change', E2:'Cell Imbalance', E3:'Thermal Stress',
    E4:'Deep Discharge', E5:'Charging Anomaly', E6:'Usage Shift',
    E7:'Voltage Sag', E8:'Capacity Fade', E9:'IR Escalation',
    E10:'BMS Protection Trip', E11:'Self Discharge', E12:'Charge Failure',
    E15:'SOH Decline',
};

async function loadT5() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view event history')); return; }
    setTabContent(loadingSkeleton(5));
    const id = STATE.batteryId;
    const data = await API.batteryEvents(id);

    if (!data) { setTabContent(emptyState('Failed to load events')); return; }

    const events = data.events || [];
    const weeks = data.weeks || {};
    const totalWeeks = weeks.last || weeks.n || 0;

    let html = '';

    // No events — green health card
    if (events.length === 0) {
        html += `<div class="card" style="border-left:3px solid #22c55e;text-align:center;padding:30px 20px">
            <div style="font-size:36px;color:#22c55e;margin-bottom:8px">&#10003;</div>
            <div style="font-size:16px;font-weight:600;color:#22c55e">No Anomalous Events Detected</div>
            <div style="font-size:13px;color:var(--text2);margin-top:6px">${totalWeeks} weeks of operation — all physics thresholds within normal range</div>
            <div style="font-size:11px;color:var(--text3);margin-top:10px">This battery is operating within all monitored parameters.</div>
        </div>`;
        setTabContent(html); return;
    }

    // Section A — Timeline
    html += '<div class="card"><div class="card-title">Event Timeline — ' + events.length + ' events across ' + totalWeeks + ' weeks</div>';
    html += '<div style="overflow-x:auto;padding:16px 0">';
    html += '<div style="display:flex;align-items:flex-start;gap:0;min-width:' + Math.max(600, totalWeeks * 16) + 'px;position:relative">';

    // Timeline axis
    html += '<div style="position:absolute;top:24px;left:0;right:0;height:2px;background:#e4e2de"></div>';

    // Group events by week for positioning
    const byWeek = {};
    events.forEach(e => {
        const w = e.week_number || 0;
        if (!byWeek[w]) byWeek[w] = [];
        byWeek[w].push(e);
    });

    // Count recurring
    const typeCounts = {};
    events.forEach(e => { const c = e.event_code || e.event_type; typeCounts[c] = (typeCounts[c]||0)+1; });

    // Render nodes
    const sortedWeeks = Object.keys(byWeek).map(Number).sort((a,b) => a-b);
    sortedWeeks.forEach((w, i) => {
        const evts = byWeek[w];
        evts.forEach((e, j) => {
            const code = e.event_code || e.event_type || '?';
            const shortCode = code.replace(/^E\d+_/, '').slice(0, 3);
            const sev = e.severity || '';
            const col = SEV_COLORS[sev] || SEV_COLORS.SEV3 || '#888';
            const size = sev === 'CRITICAL' ? 28 : 22;
            const recurring = typeCounts[code] >= 3;
            const leftPct = totalWeeks > 0 ? (w / totalWeeks * 90 + 5) : (i / Math.max(1, sortedWeeks.length) * 90 + 5);

            html += `<div style="position:absolute;left:${leftPct}%;top:${8 + j*36}px;transform:translateX(-50%);text-align:center;cursor:pointer" onclick="showEventDetail(${JSON.stringify(e).replace(/"/g,'&quot;')})">`;
            html += `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${col};display:flex;align-items:center;justify-content:center;font-family:var(--font-mono);font-size:8px;color:#000;font-weight:600;border:2px solid ${col};position:relative">`;
            html += code.match(/E\d+/) ? code.match(/E\d+/)[0] : shortCode;
            if (recurring && j === evts.length - 1) {
                html += `<span style="position:absolute;top:-6px;right:-8px;background:#ef4444;color:#fff;font-size:7px;padding:1px 4px;border-radius:8px">×${typeCounts[code]}</span>`;
            }
            html += '</div>';
            html += `<div style="font-size:8px;color:var(--text3);margin-top:2px">W${w}</div>`;
            html += '</div>';
        });
    });

    html += '</div></div>';

    // Causal chain hint
    const chainEvents = events.filter(e => e.causal_chain && e.causal_chain !== 'UNKNOWN');
    if (chainEvents.length > 1) {
        const chains = [...new Set(chainEvents.map(e => e.causal_chain))];
        html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-top:6px">Causal chains detected: ${chains.join(', ')}</div>`;
    }
    html += '</div>';

    // Section B — Event Detail (placeholder, populated by click)
    html += '<div id="event-detail"></div>';

    // Section C — Summary Table
    html += '<div class="card"><div class="card-title">Event Summary</div>';
    html += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
    html += '<tr style="font-family:var(--font-mono);font-size:9px;color:var(--text3)"><th style="text-align:left;padding:4px 8px">TYPE</th><th>COUNT</th><th>FIRST</th><th>LAST</th><th>TREND</th><th>SEVERITY</th><th>RELIABILITY</th></tr>';

    // Group by event_code
    const groups = {};
    events.forEach(e => {
        const code = e.event_code || e.event_type;
        if (!groups[code]) groups[code] = { events: [], name: EVENT_NAMES[code] || code };
        groups[code].events.push(e);
    });

    Object.entries(groups).sort((a,b) => b[1].events.length - a[1].events.length).forEach(([code, g]) => {
        const first = g.events[0];
        const last = g.events[g.events.length - 1];
        const firstDev = first.signal_deviation_pct;
        const lastDev = last.signal_deviation_pct;
        let trend = '→ STABLE';
        let trendCol = '#888';
        if (firstDev != null && lastDev != null && lastDev > firstDev) { trend = '↑ WORSENING'; trendCol = '#ef4444'; }
        else if (firstDev != null && lastDev != null && lastDev < firstDev) { trend = '↓ IMPROVING'; trendCol = '#22c55e'; }
        else if (g.events.length >= 3) { trend = '↑ RECURRING'; trendCol = '#f97316'; }

        const worstSev = g.events.reduce((w, e) => {
            const rank = {CRITICAL:4, WARNING:3, HIGH:2, MEDIUM:1, LOW:0};
            return (rank[e.severity]||0) > (rank[w]||0) ? e.severity : w;
        }, g.events[0].severity);
        const sevCol = SEV_COLORS[worstSev] || '#888';

        html += `<tr style="border-top:1px solid #e8e7e3">
            <td style="padding:6px 8px"><span style="font-family:var(--font-mono);font-size:10px;padding:1px 6px;border-radius:3px;background:${sevCol}18;color:${sevCol};border:1px solid ${sevCol}33">${code}</span> <span style="color:var(--text2)">${g.name}</span></td>
            <td style="text-align:center;font-family:var(--font-mono)">${g.events.length}</td>
            <td style="text-align:center;font-size:11px">W${first.week_number||'?'}</td>
            <td style="text-align:center;font-size:11px">W${last.week_number||'?'}</td>
            <td style="text-align:center;font-size:10px;color:${trendCol}">${trend}</td>
            <td style="text-align:center;font-size:10px;color:${sevCol}">${worstSev}</td>
            <td style="text-align:center">${RELIABILITY_BADGE[last.event_reliability] || RELIABILITY_BADGE[first.event_reliability] || ''}</td>
        </tr>`;
    });
    html += '</table></div>';

    // Recurring warnings
    Object.entries(typeCounts).filter(([,c]) => c >= 3).forEach(([code, count]) => {
        const name = EVENT_NAMES[code] || code;
        html += `<div style="font-family:var(--font-mono);font-size:11px;color:#f97316;padding:4px 0">Recurring ${name} — ${count} events detected</div>`;
    });

    setTabContent(html);
}

function showEventDetail(e) {
    const el = document.getElementById('event-detail');
    if (!el) return;
    const code = e.event_code || e.event_type;
    const name = EVENT_NAMES[code] || code;
    const sevCol = SEV_COLORS[e.severity] || '#888';
    const dev = e.signal_deviation_pct;
    const baseline = e.signal_baseline;
    const value = e.signal_value;

    // Event chain intelligence
    const CHAIN_INTEL = {
        'E3→E2': { why:'Thermal stress often precedes cell imbalance. High temperatures accelerate differential cell aging, leading to voltage spread. This is the most common degradation pathway in Indian summer conditions.', action:'Check charging temperature. Avoid charging in direct sunlight.' },
        'E2→E1': { why:'Cell imbalance reducing effective capacity, causing apparent efficiency drop. The weakest cell limits the pack — not chemistry degradation.', action:'BMS balancing check. May self-resolve with deep cycle.' },
        'E6→E1': { why:'Usage shift (driver avoiding vehicle) often precedes efficiency regime change. Possible: operator detected performance issue before system flagged it.', action:'Interview operator. Check recent complaint history.' },
        'E4→E7': { why:'Deep discharge causes voltage sag in subsequent cycles. Repeated deep discharge damages anode structure.', action:'Set BMS lower cutoff. Operator education on charging habits.' },
        'E1→E2': { why:'Efficiency loss from degradation can manifest as cell imbalance as weaker cells fail to keep up.', action:'Monitor cell spread trend. Escalate if spread exceeds 150mV.' },
        'E3→E1': { why:'Sustained thermal stress degrades electrolyte and SEI layer, reducing overall efficiency.', action:'Reduce high-temp charging. Check ventilation and parking conditions.' },
    };

    let chainHTML = '';
    if (e.causal_chain && e.causal_chain !== 'UNKNOWN') {
        const chainKey = e.causal_chain;
        const intel = CHAIN_INTEL[chainKey];
        if (intel) {
            chainHTML = '<div style="margin-top:10px;padding:10px 12px;background:#fef3c720;border:1px solid #fef3c7;border-radius:6px">' +
                '<div style="font-size:9px;font-family:var(--font-mono);color:#f97316;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:4px">◈ Intelligence Applied — Why This Happened</div>' +
                '<div style="font-size:12px;color:var(--text1);line-height:1.6;margin-bottom:6px">' + intel.why + '</div>' +
                '<div style="font-size:11px;color:var(--accent2);font-weight:500">→ ' + intel.action + '</div></div>';
        }
    }

    // Seasonal context
    const eventWeek = e.week_number || 0;
    let seasonCtx = '';
    if (eventWeek > 0) {
        const monthEst = ((eventWeek % 52) / 52 * 12) | 0;
        const season = monthEst >= 2 && monthEst <= 4 ? 'Summer' : monthEst >= 5 && monthEst <= 8 ? 'Monsoon' : 'Winter/Post-monsoon';
        seasonCtx = '<div style="font-size:10px;color:var(--text3);margin-top:6px;font-family:var(--font-mono)">Season estimate: ' + season + '</div>';
    }

    el.innerHTML = '<div class="card" style="border-left:3px solid ' + sevCol + '">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
            '<div>' +
                '<span style="font-family:var(--font-mono);font-size:12px;padding:2px 8px;border-radius:3px;background:' + sevCol + '22;color:' + sevCol + ';border:1px solid ' + sevCol + '44">' + code + '</span>' +
                '<span style="font-size:14px;font-weight:600;margin-left:8px">' + name + '</span>' +
            '</div>' +
            '<span style="font-family:var(--font-mono);font-size:11px;color:var(--text3)">Week ' + (e.week_number||'?') + '</span>' +
        '</div>' +
        (e.signal_name ? '<div style="font-size:12px;color:var(--text2);margin-bottom:4px">Signal: <strong>' + e.signal_name + '</strong> = ' + fmt(value) + (baseline ? ' (baseline: ' + fmt(baseline) + ')' : '') + '</div>' : '') +
        (dev != null ? '<div style="font-size:12px;color:' + (dev > 0 ? '#ef4444' : '#22c55e') + '">Deviation: ' + (dev > 0 ? '+' : '') + fmt(dev) + '% from baseline</div>' : '') +
        (e.rc_primary ? '<div style="font-size:11px;color:var(--text3);margin-top:4px">Root cause: ' + e.rc_primary + '</div>' : '') +
        (e.causal_chain ? '<div style="font-size:10px;color:var(--text3);margin-top:2px;font-family:var(--font-mono)">Chain: ' + e.causal_chain + '</div>' : '') +
        (e.event_reliability ? '<div style="margin-top:4px">' + (RELIABILITY_BADGE[e.event_reliability]||'') + ' <span style="font-size:10px;color:var(--text3)">CAN coverage: ' + (e.can_coverage_at_event||0).toFixed(1) + '%</span></div>' : '') +
        chainHTML +
        seasonCtx +
    '</div>';
}
