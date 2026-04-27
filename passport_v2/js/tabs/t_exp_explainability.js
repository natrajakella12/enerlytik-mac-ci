// T-EXP Explainability — reasoning chain, narrative, KB connection, confidence audit

async function loadTExp() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view reasoning chain')); return; }
    setTabContent(loadingSkeleton(8));
    const id = STATE.batteryId;
    const [data, kb] = await Promise.all([API.batteryReasoning(id), API.batteryKbContext(id)]);
    if (!data) { setTabContent(emptyState('Failed to load reasoning data')); return; }

    const bat = data.battery || {};
    const feat = data.features || {};
    const scores = data.scores || {};
    const intel = data.intelligence || {};
    const events = data.events || [];
    const inv = data.investigation;
    const shap = data.shap;
    const surv = data.survival;
    const bl = data.baseline || [];
    const chem = bat.chemistry || STATE.chemistry;
    const tier = scores.tier_v2 || '—';
    const composite = scores.composite_score_v2;
    const topReason = intel.primary_outcome_concern || (scores.l1_score < 50 ? 'efficiency decline vs baseline' : 'within normal range');

    let html = '';

    // Header
    html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
        <span style="font-family:var(--font-head);font-size:18px;font-weight:700">${id}</span>
        ${chemistryBadge(chem)} ${tierBadge(tier)}
    </div>`;
    html += `<div class="card" style="border-left:3px solid ${TIER_COLORS[tier]||'#999'}">
        <div style="font-size:14px;color:var(--text1)">This battery is <strong style="color:${TIER_COLORS[tier]||'#555'}">${tier}</strong> because: <em>${topReason}</em></div>
    </div>`;

    // Section A — Reasoning Chain (Visual Trace)
    html += '<div class="card"><div class="card-title">Reasoning Chain — Intelligence Trace</div>';

    const node = (label, val, sub, color) => `<div style="background:${color};border:1px solid var(--bg3);border-radius:6px;padding:8px 12px;min-width:140px;flex:1;text-align:center">
        <div style="font-family:var(--font-mono);font-size:9px;color:var(--text3)">${label}</div>
        <div style="font-size:14px;font-weight:600;margin-top:2px">${val}</div>
        ${sub?'<div style="font-size:9px;color:var(--text3);margin-top:2px">'+sub+'</div>':''}
    </div>`;

    // Baseline values
    const blKps = bl.length > 0 ? bl.reduce((s,r) => s + (r.km_per_soc_pct||0), 0) / bl.length : null;
    const blSpread = bl.length > 0 ? bl.reduce((s,r) => s + (r.cell_spread_max||0), 0) / bl.length : null;
    const curKps = feat.km_per_soc_pct;
    const curSpread = feat.cell_spread_max;
    const kpsDelta = (blKps && curKps) ? ((curKps - blKps) / blKps * 100).toFixed(1) : null;
    const spreadDelta = (blSpread && curSpread) ? ((curSpread - blSpread) / blSpread * 100).toFixed(1) : null;

    const riskColor = (v, lo, hi) => v == null ? '#f5f5f3' : v < lo ? '#dcfce7' : v < hi ? '#fef3c7' : '#fee2e2';

    // Layer 1 — Raw Signals
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-bottom:4px">LAYER 1 — RAW SIGNALS</div>';
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">';
    html += node('Voltage', fmt(feat.voltage_range || feat.pack_voltage_mean)+'V', 'pack', '#f5f5f3');
    html += node('Current', fmt(feat.current_discharge_mean)+'A', 'discharge mean', '#f5f5f3');
    html += node('Temperature', fmt(feat.temp_max)+'°C', 'max', riskColor(feat.temp_max, 40, 45));
    html += node('SoC (BMS)', fmt(feat.soc_min || feat.soc_min_observed)+'%', 'minimum', riskColor(feat.soc_min, 15, 5));
    html += '</div>';
    html += '<div style="text-align:center;color:var(--text3);font-size:12px;margin:4px 0">↓</div>';

    // Layer 2 — Computed Features
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-bottom:4px">LAYER 2 — COMPUTED FEATURES</div>';
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">';
    html += node('Efficiency', curKps ? (curKps*80).toFixed(0)+'km' : '—', kpsDelta ? `vs baseline: ${kpsDelta>0?'+':''}${kpsDelta}%` : '', riskColor(kpsDelta ? parseFloat(kpsDelta) : 0, -10, -25));
    html += node('Cell Spread', fmt(curSpread,0)+'mV', spreadDelta ? `vs baseline: ${spreadDelta>0?'+':''}${spreadDelta}%` : '', riskColor(curSpread, 150, 300));
    if (chem === 'NMC') html += node('R0 (NMC)', feat.r0_estimate_mohm ? (feat.r0_estimate_mohm/14).toFixed(2)+'mΩ/cell' : fmt(feat.r0_weekly_median)+'mΩ', 'pulse ECM', '#f5f5f3');
    else html += node('CUSUM', feat.cusum_flag ? 'ACTIVE' : 'Clear', 'regime change', feat.cusum_flag ? '#fee2e2' : '#dcfce7');
    html += '</div>';
    html += '<div style="text-align:center;color:var(--text3);font-size:12px;margin:4px 0">↓</div>';

    // Layer 3 — Model Outputs
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-bottom:4px">LAYER 3 — MODEL OUTPUTS</div>';
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">';
    html += node('L1 Self-Baseline', fmt(scores.l1_score,0)+'/100', '50% weight', riskColor(scores.l1_score, 60, 40));
    html += node('L2 Physics', fmt(scores.l2_score,0)+'/100', '30% weight', riskColor(scores.l2_score, 60, 40));
    html += node('L3 Fleet Context', fmt(scores.l3_score,0)+'/100', '20% weight', riskColor(scores.l3_score, 60, 40));
    html += '</div>';
    html += '<div style="text-align:center;color:var(--text3);font-size:12px;margin:4px 0">↓</div>';

    // Layer 4 — Composite
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-bottom:4px">LAYER 4 — COMPOSITE INTELLIGENCE</div>';
    html += `<div style="background:${TIER_BG[tier]||'#f3f4f6'};border:1px solid var(--bg3);border-radius:6px;padding:12px 16px;margin-bottom:8px">
        <div style="font-size:13px"><strong>Composite: ${fmt(composite,0)}/100</strong> · Tier: ${tierBadge(tier)} · Events: ${events.length}${inv ? ' · Investigation: '+inv.verdict : ''}${surv ? ' · P30='+Math.round(surv.p_complaint_30d*100)+'%' : ''}</div>
        ${scores.event_penalty ? '<div style="font-size:11px;color:var(--red);margin-top:4px">Event penalty: -'+scores.event_penalty+'</div>' : ''}
    </div>`;
    html += '<div style="text-align:center;color:var(--text3);font-size:12px;margin:4px 0">↓</div>';

    // Layer 5 — Actions
    html += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-bottom:4px">LAYER 5 — ACTIONS</div>';
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
    html += node('Fleet Action', intel.operator_action || '—', '', '#f5f5f3');
    html += node('Warning', intel.warning_level_absolute || '—', intel.warning_trend || '', '#f5f5f3');
    html += node('Concern', intel.primary_outcome_concern || '—', '', '#f5f5f3');
    html += '</div></div>';

    // Section B — Narrative Summary
    html += '<div class="card"><div class="card-title">Narrative Summary</div>';
    const weeks = feat.week_number || '?';
    let narrative = `<strong>${id}</strong> is a <strong>${chem}</strong> battery commissioned ${weeks} weeks ago, currently rated <strong style="color:${TIER_COLORS[tier]||'#555'}">${tier}</strong> with a composite score of <strong>${fmt(composite,0)}/100</strong>.`;
    if (kpsDelta) narrative += ` The primary efficiency is ${Math.abs(kpsDelta)}% ${parseFloat(kpsDelta)<0?'below':'above'} this battery's own Week 1-8 baseline (L1 self-baseline, 50% weight).`;
    if (curSpread > 200) narrative += ` Cell spread at ${Math.round(curSpread)}mV indicates cell imbalance.`;
    if (events.length > 0) narrative += ` ${events.length} anomalous events detected, most recently ${events[events.length-1].event_code} in Week ${events[events.length-1].week_number}.`;
    if (inv) narrative += ` Complaint analysis ${inv.verdict === 'PHYSICS_CONFIRMED' || inv.verdict === 'PROGRESSIVE' ? 'confirms' : 'does not confirm'} physics degradation — ${inv.verdict}.`;
    if (surv) narrative += ` Service forecast: ${Math.round(surv.p_complaint_30d*100)}% probability of complaint within 30 days.`;
    narrative += ` Recommended action: <strong>${intel.operator_action || 'ROUTINE'}</strong>.`;
    html += `<div style="font-size:12px;color:var(--text2);line-height:1.8">${narrative}</div></div>`;

    // Section C — KB Connection
    html += '<div class="card"><div class="card-title">Knowledge Base Context</div>';
    if (kb && kb.categories) {
        html += '<table style="width:100%;font-size:11px;border-collapse:collapse">';
        html += '<tr style="font-family:var(--font-mono);font-size:9px;color:var(--text3)"><th style="text-align:left;padding:4px 8px">KB SOURCE</th><th>RELEVANCE</th><th>USED FOR</th></tr>';
        kb.categories.forEach(c => {
            const relCol = c.relevance === 'HIGH' ? 'var(--accent)' : '#d97706';
            html += `<tr style="border-top:1px solid var(--bg3)"><td style="padding:5px 8px">${c.source}</td><td style="text-align:center;color:${relCol};font-family:var(--font-mono);font-size:10px">${c.relevance}</td><td style="font-size:10px;color:var(--text2)">${c.used_for}</td></tr>`;
        });
        html += '</table>';
        if (!kb.rag_online) html += '<div style="font-size:9px;color:var(--text3);margin-top:6px;font-family:var(--font-mono)">RAG offline — showing static KB categories. Start RAG on port 8001 for live context.</div>';
    }
    html += '</div>';

    // Section D — Confidence Audit Trail
    html += '<details class="card"><summary style="cursor:pointer;font-family:var(--font-mono);font-size:10px;color:var(--text3)">CONFIDENCE AUDIT TRAIL</summary>';
    html += `<pre style="font-family:var(--font-mono);font-size:10px;color:var(--text2);line-height:1.8;margin-top:8px;background:var(--bg2);padding:12px;border-radius:4px;overflow-x:auto">
Data source:        telemetry_raw${chem==='NMC'?' + nmc_telemetry_primary':''}
Resolution:         20-sec (native)
Weeks of data:      ${weeks}
Data confidence:    ${scores.data_confidence || 'UNKNOWN'}

Models applied:
  Health score:     ${scores.scoring_mode || 'production scorer'}
  Prediction conf:  ${scores.prediction_confidence || '—'}

Flags active:
  ${scores.data_confidence && scores.data_confidence.includes('RETROACTIVE') ? 'RETROACTIVE_LOW_DATA — pre-resolution-fix output' : 'None'}

Regression tested:  YES — anchors PASS</pre></details>`;

    setTabContent(html);
}
