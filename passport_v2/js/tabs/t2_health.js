// T2 Health — per-battery health verdict, 3-layer scoring, signals, tier history

async function loadT2() {
    if (!STATE.batteryId) {
        setTabContent(emptyState('Select a battery', 'Choose from the dropdown above to view health intelligence'));
        return;
    }
    setTabContent(loadingSkeleton(8));
    const id = STATE.batteryId;
    const [intel, scores, bat] = await Promise.all([
        API.batteryIntelligence(id),
        API.batteryScores(id),
        API.batteryFull(id),
    ]);

    if (!intel && !scores) {
        setTabContent(emptyState(`No data for ${id}`, 'Battery may not be scored yet'));
        return;
    }

    const chem = (intel && intel.chemistry) || (bat && bat.chemistry) || STATE.chemistry;
    const tier = (intel && intel.tier_label_v2) || (scores && scores.current && scores.current.tier_v2) || '—';
    const composite = (intel && intel.composite_score_v2) || (scores && scores.current && scores.current.composite_score_v2) || null;
    const narrative = (intel && (intel.narrative_operator || intel.outcome_narrative)) || '';
    const confidence = (intel && intel.data_confidence) || (scores && scores.current && scores.current.data_confidence) || '';
    const l1 = scores && scores.current ? scores.current.l1_score : null;
    const l2 = scores && scores.current ? scores.current.l2_score : null;
    const l3 = scores && scores.current ? scores.current.l3_score : null;
    const eventPenalty = scores && scores.current ? scores.current.event_penalty : 0;

    let html = '';

    // Section A — Verdict Card
    html += '<div class="card" style="border-left:3px solid ' + (TIER_COLORS[tier]||'#555') + '">';
    html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">';
    html += '<div>';
    html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">`;
    html += `<span style="font-family:var(--font-head);font-size:42px;font-weight:800;color:${TIER_COLORS[tier]||'#888'}">${tier}</span>`;
    html += chemistryBadge(chem);
    html += confidenceBadge(confidence);
    html += '</div>';
    html += `<div style="margin-bottom:6px">${scoreBar(composite)}</div>`;
    if (narrative) html += `<div style="font-size:12px;color:var(--text2);line-height:1.6;max-width:600px;margin-top:6px">${narrative.split('\\n')[0]}</div>`;
    html += '</div>';
    html += '<div style="text-align:right">';
    html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3)">Composite</div>`;
    html += `<div style="font-family:var(--font-head);font-size:36px;font-weight:700;color:${TIER_COLORS[tier]||'#888'}">${composite != null ? Math.round(composite) : '—'}</div>`;
    if (eventPenalty > 0) html += `<div style="font-size:10px;color:#ef4444;font-family:var(--font-mono)">-${eventPenalty} event penalty</div>`;
    html += '</div></div></div>';

    // Retroactive warning
    if (confidence && confidence.includes('RETROACTIVE')) {
        html += '<div class="banner banner-warn">This battery\'s intelligence was produced before the resolution fix. Scores may change after the next rebuild.</div>';
    }

    // Section B — 3-Layer Scoring
    html += '<div class="card">';
    html += '<div class="card-title">3-Layer Scoring Breakdown</div>';
    const layers = [
        { name:'L1 · Self-Baseline', weight:'50%', score:l1, desc:'Efficiency & spread vs own first 8 weeks' },
        { name:'L2 · Physics Thresholds', weight:'30%', score:l2, desc:'Cell spread, temperature, DoD violations' },
        { name:'L3 · Fleet Context', weight:'20%', score:l3, desc:'Percentile rank within peer group' },
    ];
    layers.forEach(l => {
        const s = l.score != null ? Math.round(l.score) : null;
        const tier2 = s >= 80 ? 'PRIME' : s >= 60 ? 'STABLE' : s >= 40 ? 'WATCH' : s >= 20 ? 'STRESSED' : 'CRITICAL';
        const c = s != null ? TIER_COLORS[tier2] : '#555';
        html += `<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #e8e7e3">
            <div style="min-width:180px">
                <div style="font-size:12px;font-weight:500">${l.name}</div>
                <div style="font-size:10px;color:var(--text3)">${l.weight} weight · ${l.desc}</div>
            </div>
            <div style="flex:1">${scoreBar(s)}</div>
        </div>`;
    });
    html += '</div>';

    // Section C — Key Signals (chemistry-specific)
    html += '<div class="card"><div class="card-title">Key Signals</div><div class="g4">';
    if (chem === 'NMC') {
        const r0 = bat && bat.r0_pulse_median_mohm ? (bat.r0_pulse_median_mohm / 14).toFixed(2) : null;
        const soh = intel ? intel.electrochemical_soh : null;
        const spread = bat ? bat.cell_spread_max : null;
        const temp = bat ? bat.temp_max : null;
        html += signalCard('R0 per-cell', r0, 'mΩ', null, null);
        html += signalCard('SOH (ECM)', soh ? soh.toFixed(0) + '%' : '—', '', null, null);
        html += signalCard('Spread Max', spread ? Math.round(spread) : '—', 'mV', null, null);
        html += signalCard('Temp Max', temp ? Math.round(temp) : '—', '°C', null, null);
    } else {
        // LFP signals from weekly features
        const w = scores && scores.weekly && scores.weekly.length > 0 ? scores.weekly[scores.weekly.length-1] : {};
        const w_prev = scores && scores.weekly && scores.weekly.length > 4 ? scores.weekly[scores.weekly.length-5] : {};
        const kps = w.km_per_soc_pct;
        const spread = w.cell_spread_max;
        const temp = w.temp_max;
        const cusum = w.cusum_flag;
        const kps_delta = (kps && w_prev.km_per_soc_pct) ? kps - w_prev.km_per_soc_pct : null;
        const spread_delta = (spread && w_prev.cell_spread_max) ? spread - w_prev.cell_spread_max : null;
        html += signalCard('Efficiency', kps ? (kps*80).toFixed(0) : '—', 'km', kps_delta ? kps_delta*80 : null, kps_delta > 0 ? 'up' : kps_delta < 0 ? 'down' : null);
        html += signalCard('Cell Spread', spread ? Math.round(spread) : '—', 'mV', spread_delta ? Math.round(spread_delta) : null, spread_delta > 0 ? 'up' : spread_delta < 0 ? 'down' : null);
        html += signalCard('Temp Max', temp ? Math.round(temp) : '—', '°C', null, null);
        html += signalCard('CUSUM Flag', cusum ? 'ACTIVE' : 'Clear', '', null, null);
    }
    html += '</div>';
    // Signal context interpretations
    if (chem === 'LFP') {
        const wCtx = scores && scores.weekly && scores.weekly.length > 0 ? scores.weekly[scores.weekly.length-1] : {};
        const kpsCtx = wCtx.km_per_soc_pct;
        const spreadCtx = wCtx.cell_spread_max;
        const tempCtx = wCtx.temp_max;
        let ctx = [];
        if (kpsCtx) {
            const w1 = scores.weekly && scores.weekly.length > 8 ? scores.weekly.slice(0,8) : scores.weekly || [];
            const baseKps = w1.filter(r=>r.km_per_soc_pct).length > 0 ? w1.reduce((s,r)=>s+(r.km_per_soc_pct||0),0)/w1.filter(r=>r.km_per_soc_pct).length : null;
            if (baseKps && kpsCtx < baseKps * 0.9) ctx.push('Efficiency ' + ((1-kpsCtx/baseKps)*100).toFixed(0) + '% below W1-8 baseline — range loss ≈ ' + ((baseKps-kpsCtx)*80).toFixed(0) + 'km vs commissioning');
            else if (baseKps && kpsCtx > baseKps * 1.02) ctx.push('Efficiency above baseline — possibly lighter load or seasonal improvement');
            else ctx.push('Stable efficiency — normal aging pattern');
        }
        if (spreadCtx) {
            if (spreadCtx > 150) ctx.push('Cell imbalance elevated — ' + Math.round(spreadCtx) + 'mV spread. Possible: cell degradation, BMS balancing failure, or temperature gradient');
            else if (spreadCtx > 100) ctx.push('Cell spread at ' + Math.round(spreadCtx) + 'mV — approaching WARNING threshold. Monitor closely');
            else ctx.push('Cell balance healthy — within normal operating range');
        }
        if (tempCtx) {
            if (tempCtx > 45) ctx.push('Thermal stress detected — sustained high temp accelerates SEI layer growth');
            else ctx.push('Temperature within safe operating range');
        }
        if (ctx.length > 0) {
            html += '<div style="padding:0 4px;margin-top:-4px">';
            ctx.forEach(function(c) { html += '<div style="font-size:11px;color:var(--text2);line-height:1.6;padding:3px 0;border-left:2px solid #f9731633;padding-left:8px;margin-bottom:4px">' + c + '</div>'; });
            html += '</div>';
        }
    }
    html += '</div>';

    // Section D — Tier History (weekly feature sparkline)
    html += '<div class="card">';
    html += '<div class="card-title">Weekly Trajectory</div>';
    html += '<div style="position:relative;height:160px"><canvas id="tier-history"></canvas></div>';
    html += '</div>';

    // Section E2 — Behavioural Proxies (LFP only)
    if (chem === 'LFP' && scores && scores.weekly && scores.weekly.length > 0) {
        const wLast = scores.weekly[scores.weekly.length-1];
        const wFirst = scores.weekly[0];
        html += '<div class="card">';
        html += '<div style="font-size:9px;font-family:var(--font-mono);color:#f97316;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">◈ Intelligence Applied</div>';
        html += '<div class="card-title">Behavioural Proxies</div>';
        const proxies = [
            { name:'Charging frequency', value: wLast.charge_cycles_delta, unit:'cycles/wk',
              base: wFirst.charge_cycles_delta,
              interp: function(v,b) { return !v ? '—' : v > (b||v)*1.3 ? 'Opportunity charging detected — frequent partial charges' : v < (b||v)*0.7 ? 'Reduced charging — possible range anxiety or usage drop' : 'Normal charging pattern'; } },
            { name:'Weekly mileage', value: wLast.km_sum || wLast.km_per_week, unit:'km/wk',
              interp: function(v) { return !v ? '—' : v < 50 ? 'Below normal usage — check operator complaint' : v > 300 ? 'High mileage — accelerated wear expected' : 'Normal usage pattern'; } },
            { name:'Depth of discharge', value: wLast.dod_mean, unit:'%',
              interp: function(v) { return !v ? '—' : v > 85 ? 'Deep cycling — stresses cell chemistry' : v < 40 ? 'Shallow cycling — good for longevity' : 'Moderate DoD — normal pattern'; } },
        ];
        proxies.forEach(function(p) {
            const val = p.value;
            const interpText = p.interp(val, p.base);
            html += '<div style="display:flex;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid #e8e7e3">';
            html += '<div style="min-width:160px"><div style="font-size:12px;font-weight:500">' + p.name + '</div></div>';
            html += '<div style="font-family:var(--font-mono);font-size:13px;font-weight:600;min-width:80px">' + (val != null ? (typeof val === 'number' ? val.toFixed(1) : val) : '—') + ' <span style="font-size:10px;color:var(--text3)">' + p.unit + '</span></div>';
            html += '<div style="font-size:11px;color:var(--text2);flex:1;border-left:2px solid #f9731633;padding-left:8px">' + interpText + '</div>';
            html += '</div>';
        });
        html += '</div>';
    }

    // Section E — Data Quality Footer
    const weeks = scores && scores.weekly ? scores.weekly.length : '?';
    html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);padding:8px 0;display:flex;gap:16px;flex-wrap:wrap">
        <span>${weeks} weeks telemetry</span>
        <span>data_confidence: ${confidence || 'UNKNOWN'}</span>
        ${scores && scores.current && scores.current.scoring_mode ? '<span>mode: '+scores.current.scoring_mode+'</span>' : ''}
    </div>`;

    // Section F — Feedback Loop
    html += `<div class="card" style="margin-top:4px">
        <div style="font-size:9px;font-family:var(--font-mono);color:#f97316;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">◈ Intelligence Applied</div>
        <div class="card-title">Field Validation — Intelligence Feedback</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <select id="fb-role" style="flex:1;min-width:140px;padding:6px 8px;border:1px solid var(--bg3);border-radius:4px;font-size:11px;font-family:var(--font-mono);background:var(--bg1)">
            <option value="">Your role...</option>
            <option value="fleet_operator">Fleet Operator</option>
            <option value="service_tech">Service Technician</option>
            <option value="nbfc_analyst">NBFC Analyst</option>
            <option value="oem_engineer">OEM Engineer</option>
            <option value="enerlytik_team">enerlytik Team</option>
          </select>
          <select id="fb-assessment" style="flex:1;min-width:140px;padding:6px 8px;border:1px solid var(--bg3);border-radius:4px;font-size:11px;font-family:var(--font-mono);background:var(--bg1)">
            <option value="">Assessment...</option>
            <option value="agree_tier">Agree with tier</option>
            <option value="too_high">Score too high</option>
            <option value="too_low">Score too low</option>
            <option value="wrong_signals">Wrong signals flagged</option>
            <option value="missed_issue">Missed real issue</option>
          </select>
          <select id="fb-reason" style="flex:1;min-width:140px;padding:6px 8px;border:1px solid var(--bg3);border-radius:4px;font-size:11px;font-family:var(--font-mono);background:var(--bg1)">
            <option value="">Reason code...</option>
            <option value="field_observation">Field observation</option>
            <option value="service_record">Service record</option>
            <option value="operator_report">Operator report</option>
            <option value="physical_inspection">Physical inspection</option>
          </select>
        </div>
        <textarea id="fb-comment" style="width:100%;min-height:48px;padding:8px;border:1px solid var(--bg3);border-radius:4px;font-size:11px;font-family:var(--font-body);background:var(--bg1);resize:vertical" placeholder="Add context — what did you observe in the field?"></textarea>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
          <button onclick="submitFeedback('${id}')" style="padding:6px 16px;border-radius:4px;background:#22c55e;color:#000;font-family:var(--font-mono);font-size:11px;font-weight:600;border:none;cursor:pointer">Submit Feedback</button>
          <button onclick="saveFeedbackJSON('${id}')" style="padding:6px 16px;border-radius:4px;background:var(--bg2);color:var(--text2);font-family:var(--font-mono);font-size:11px;border:1px solid var(--bg3);cursor:pointer">Save as JSON</button>
          <span id="fb-status" style="font-size:11px;display:none"></span>
        </div>
        <div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-top:8px">
          Feedback updates <code>battery_feedback</code> table in production DB. Offline: "Save as JSON" → import via CLI.
        </div>
    </div>`;

    setTabContent(html);

    // Render tier history chart
    if (scores && scores.weekly && scores.weekly.length > 0) {
        const wks = scores.weekly.map(w => 'W' + w.week_number);
        // Use cell_spread_max as inverse health proxy for sparkline
        const spreads = scores.weekly.map(w => w.cell_spread_max || 0);
        const kps_data = scores.weekly.map(w => w.km_per_soc_pct ? w.km_per_soc_pct * 80 : null);

        makeChart('tier-history', 'line', {
            labels: wks,
            datasets: [
                { label:'Range (km)', data:kps_data, borderColor:'#22c55e', borderWidth:1.5, pointRadius:0, tension:0.3 },
                { label:'Spread (mV)', data:spreads, borderColor:'#ef4444', borderWidth:1.5, pointRadius:0, tension:0.3, yAxisID:'y1' },
            ]
        }, {
            legend: true,
            scales: {
                x: { ticks:{color:'#888',font:{size:8},maxTicksLimit:12}, grid:{color:'#e8e7e3'} },
                y: { position:'left', ticks:{color:'#22c55e88',font:{size:9}}, grid:{color:'#e8e7e3'},
                     title:{display:true,text:'km',color:'#22c55e88',font:{size:9}} },
                y1:{ position:'right', ticks:{color:'#ef444488',font:{size:9}}, grid:{display:false},
                     title:{display:true,text:'mV',color:'#ef444488',font:{size:9}} },
            }
        });
    }
}
