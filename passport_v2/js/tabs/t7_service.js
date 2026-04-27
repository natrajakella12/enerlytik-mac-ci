// T7 Service — service scheduling panel (NOT health indicator)

async function loadT7() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view service intelligence')); return; }
    setTabContent(loadingSkeleton(5));
    const id = STATE.batteryId;
    const data = await API.batteryService(id);
    if (!data) { setTabContent(emptyState('Failed to load service data')); return; }

    const chem = data.chemistry || STATE.chemistry;
    const fc = data.forecast;
    const complaints = data.complaints || [];
    const silent = data.silent_degrader;
    const flag = data.forecast_flag;

    let html = '';

    // Mandatory header banner
    html += `<div style="background:#78350f;border:1px solid #92400e;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-family:var(--font-mono)">
        <div style="font-size:12px;font-weight:600;color:#fcd34d">SERVICE SCHEDULING ONLY — this tab does not indicate battery health</div>
        <div style="font-size:10px;color:#fde68a;margin-top:4px;opacity:0.8">Health decisions are made in the Health tab (Track E — electrochemical)</div>
    </div>`;

    // Section A — Survival Forecast
    html += '<div class="card"><div class="card-title">Service Forecast</div>';
    if (chem !== 'NMC') {
        html += '<div style="font-size:12px;color:var(--text3);padding:12px 0;line-height:1.7">Service forecast not available for LFP — complaint history not tracked for e-rickshaw fleet.</div>';
    } else if (fc) {
        const probs = [
            {label:'P(complaint in 30 days)', val:fc.p_complaint_30d, risk: fc.p_complaint_30d > 0.7 ? 'HIGH' : fc.p_complaint_30d > 0.3 ? 'MEDIUM' : 'LOW'},
            {label:'P(complaint in 60 days)', val:fc.p_complaint_60d, risk: fc.p_complaint_60d > 0.7 ? 'HIGH' : fc.p_complaint_60d > 0.3 ? 'MEDIUM' : 'LOW'},
            {label:'P(complaint in 90 days)', val:fc.p_complaint_90d, risk: fc.p_complaint_90d > 0.7 ? 'HIGH' : fc.p_complaint_90d > 0.3 ? 'MEDIUM' : 'LOW'},
        ];
        const riskCols = {HIGH:'#ef4444', MEDIUM:'#f59e0b', LOW:'#22c55e'};

        probs.forEach(p => {
            const pct = Math.round(p.val * 100);
            const col = riskCols[p.risk];
            html += `<div style="margin-bottom:10px">
                <div style="display:flex;justify-content:space-between;margin-bottom:3px">
                    <span style="font-size:11px;color:${col};font-family:var(--font-mono)">${p.risk} risk</span>
                    <span style="font-size:11px;color:var(--text3)">${p.label}</span>
                </div>
                <div style="height:22px;background:#e8e7e3;border-radius:4px;position:relative;overflow:hidden">
                    <div style="width:${pct}%;height:100%;background:${col};border-radius:4px;transition:width 0.5s"></div>
                    <span style="position:absolute;right:8px;top:3px;font-size:11px;font-family:var(--font-mono);font-weight:600;color:var(--text1)">${pct}%</span>
                </div>
            </div>`;
        });

        // Median days
        const med = fc.median_days_to_complaint;
        html += `<div style="text-align:center;padding:16px 0;margin-top:8px;border-top:1px solid #e4e2de">
            <div style="font-family:var(--font-head);font-size:36px;font-weight:700;color:var(--accent)">${med ? Math.round(med) : '—'}</div>
            <div style="font-size:11px;color:var(--text3)">median days to next complaint</div>
        </div>`;

        // Model disclosure
        html += `<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-top:6px">
            Weibull AFT · C-index: 0.600 · ${fc.model_version || 'nmc_survival_v2.1.0'} · Based on complaint trajectory only
        </div>`;

        // Forecast flag
        if (flag) {
            html += `<div style="background:#78350f44;border:1px solid #78350f;border-radius:6px;padding:8px 12px;margin-top:8px;font-family:var(--font-mono);font-size:10px;color:#fcd34d">
                ${flag.data_confidence}: ${flag.forecast_note || ''}
            </div>`;
        }
    } else {
        html += emptyState('No survival forecast available','Model may not have been run for this battery');
    }
    html += '</div>';

    // Section B — Silent Degrader Warning
    if (silent) {
        html += `<div class="card" style="background:#78350f22;border:1px solid #78350f">
            <div class="card-title" style="color:#f59e0b">Silent Degrader Detected</div>
            <div style="font-size:12px;color:#fcd34d;line-height:1.7">
                This battery has <strong>${data.events_count}</strong> telemetry events but low complaint probability.
                Complaint-based forecast may underestimate true risk.
            </div>
            <div style="font-size:11px;color:var(--text3);margin-top:6px">Cross-reference with Health tab for electrochemical state.</div>
        </div>`;
    }

    // Section C — Complaint History
    html += '<div class="card"><div class="card-title">Complaint History</div>';
    if (complaints.length === 0) {
        html += `<div style="display:inline-block;padding:4px 12px;border-radius:20px;font-family:var(--font-mono);font-size:10px;background:#22c55e22;color:#22c55e;border:1px solid #22c55e44">NO_COMPLAINTS</div>`;
        html += '<div style="font-size:12px;color:var(--text3);margin-top:8px">No complaints recorded for this battery.</div>';
    } else {
        // Pattern classification
        const types = complaints.map(c => c.complaint_category);
        const uniqueTypes = [...new Set(types)];
        let pattern = 'RANDOM';
        if (uniqueTypes.length === 1 && complaints.length >= 3) pattern = 'RECURRING';
        if (complaints.length >= 4) {
            const lastDelta = complaints.slice(-2).filter(c => c.delta_mv).map(c => c.delta_mv);
            const firstDelta = complaints.slice(0,2).filter(c => c.delta_mv).map(c => c.delta_mv);
            if (lastDelta.length > 0 && firstDelta.length > 0 && lastDelta[0] > firstDelta[0]) pattern = 'PROGRESSIVE';
        }
        const patCols = {RANDOM:'#888', RECURRING:'#f59e0b', PROGRESSIVE:'#ef4444'};
        html += `<div style="margin-bottom:8px"><span style="padding:3px 10px;border-radius:20px;font-family:var(--font-mono);font-size:10px;background:${patCols[pattern]}22;color:${patCols[pattern]};border:1px solid ${patCols[pattern]}44">${pattern}</span></div>`;

        html += '<table style="width:100%;font-size:11px;border-collapse:collapse">';
        html += '<tr style="font-family:var(--font-mono);font-size:9px;color:var(--text3)"><th style="text-align:left;padding:4px 8px">#</th><th>DATE</th><th>TYPE</th><th>RESOLUTION</th><th>DELTA mV</th></tr>';
        complaints.forEach((c, i) => {
            html += `<tr style="border-top:1px solid #e8e7e3">
                <td style="padding:5px 8px;font-family:var(--font-mono)">${i+1}</td>
                <td style="font-size:10px">${(c.complaint_date||'').slice(0,10)}</td>
                <td style="font-size:10px;color:var(--text2)">${c.diagnosis_category || c.complaint_category || '—'}</td>
                <td style="font-size:10px">${c.resolution_type || '—'}</td>
                <td style="font-family:var(--font-mono);text-align:center">${c.delta_mv != null ? Math.round(c.delta_mv) : '—'}</td>
            </tr>`;
        });
        html += '</table>';
    }
    html += '</div>';

    // Section D — Next Service Recommendation
    html += '<div class="card"><div class="card-title">Next Service Recommendation</div>';
    if (fc && fc.p_complaint_30d > 0.7) {
        html += `<div style="background:#ef444422;border:1px solid #ef444444;border-radius:6px;padding:12px 16px">
            <div style="font-size:13px;font-weight:600;color:#ef4444">HIGH PRIORITY — service recommended within 30 days</div>
            <div style="font-size:11px;color:var(--text3);margin-top:4px">Source: SURVIVAL_MODEL (P30d = ${Math.round(fc.p_complaint_30d*100)}%)</div>
        </div>`;
    } else if (fc && fc.p_complaint_60d > 0.5) {
        html += `<div style="background:#f59e0b22;border:1px solid #f59e0b44;border-radius:6px;padding:12px 16px">
            <div style="font-size:13px;font-weight:600;color:#f59e0b">MEDIUM PRIORITY — schedule service within 60 days</div>
            <div style="font-size:11px;color:var(--text3);margin-top:4px">Source: SURVIVAL_MODEL</div>
        </div>`;
    } else {
        html += `<div style="font-size:12px;color:var(--text3)">No urgent service needed — routine monitoring recommended.</div>`;
    }
    html += '</div>';

    setTabContent(html);
}
