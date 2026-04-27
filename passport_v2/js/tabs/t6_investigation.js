// T6 Investigation — signal triangulation panel

const VERDICT_STYLES = {
    PHYSICS_CONFIRMED: {bg:'#fee2e2',text:'#991b1b',border:'#ef4444'},
    PROGRESSIVE:       {bg:'#fef3c7',text:'#92400e',border:'#f59e0b'},
    WEAK_SIGNAL:       {bg:'#fefce8',text:'#713f12',border:'#eab308'},
    UNCONFIRMED:       {bg:'#f9fafb',text:'#374151',border:'#9ca3af'},
};

async function loadT6() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view investigation')); return; }
    setTabContent(loadingSkeleton(6));
    const id = STATE.batteryId;
    const data = await API.batteryInvestigation(id);
    if (!data) { setTabContent(emptyState('Failed to load investigation data')); return; }

    const inv = data.investigation;
    const complaints = data.complaints || [];
    const chem = data.chemistry || STATE.chemistry;
    let html = '';

    // Empty states
    if (!inv) {
        if (chem === 'LFP' || complaints.length === 0) {
            // Case A — no complaints
            html += `<div class="card" style="text-align:center;padding:30px 20px;border-left:3px solid #555">
                <div style="font-size:14px;font-weight:600;color:var(--text2)">No complaint history for this battery</div>
                <div style="font-size:12px;color:var(--text3);margin-top:8px;max-width:500px;margin-left:auto;margin-right:auto;line-height:1.7">
                    Signal Triangulation Engine activates when repeat complaints are detected.<br>
                    <span style="font-family:var(--font-mono);font-size:10px;color:var(--text3)">Complaint → Telemetry pull (±6 weeks) → Physics verdict</span>
                </div>
            </div>`;
        } else {
            // Case B — complaints exist but no investigation
            html += `<div class="card" style="border-left:3px solid #f59e0b;padding:20px">
                <div style="font-size:14px;font-weight:600;color:#f59e0b">Complaints detected — investigation pending</div>
                <div style="font-size:12px;color:var(--text2);margin-top:6px">${complaints.length} complaint(s) on record. Dates: ${complaints.slice(0,3).map(c=>c.complaint_date).join(', ')}${complaints.length>3?'...':''}</div>
                <div style="font-size:11px;color:var(--text3);margin-top:8px;font-family:var(--font-mono)">Investigation will run automatically on next pipeline cycle.</div>
            </div>`;
        }
        setTabContent(html); return;
    }

    // Parse evidence chain
    let evidence = [];
    try { evidence = JSON.parse(inv.evidence_chain_json || '[]'); } catch {}
    let signals = [];
    try { signals = JSON.parse(inv.signals_json || '[]'); } catch {}
    const vs = VERDICT_STYLES[inv.verdict] || VERDICT_STYLES.UNCONFIRMED;

    // Section A — Verdict Banner
    html += `<div class="card" style="background:${vs.bg};border:1px solid ${vs.border};padding:20px">
        <div style="font-family:var(--font-head);font-size:28px;font-weight:700;color:${vs.text}">${inv.verdict}</div>
        <div style="font-size:13px;color:${vs.text};opacity:0.85;margin-top:6px">${inv.hypothesis || ''}</div>
        <div style="font-size:11px;color:${vs.text};opacity:0.6;margin-top:8px;font-family:var(--font-mono)">
            ${inv.n_complaints} complaint windows analysed · ${signals.length} signals confirmed · Pattern: ${inv.classification || '—'}
        </div>
        <div style="margin-top:8px">${confidenceBadge(inv.data_confidence)}</div>
    </div>`;

    // Section B — Complaint Timeline with Signal Overlay
    if (evidence.length > 0) {
        html += '<div class="card"><div class="card-title">Complaint Timeline with Signal Overlay</div>';
        html += '<div style="display:flex;gap:0;overflow-x:auto;padding:16px 0;position:relative">';
        evidence.forEach((w, i) => {
            const sigs = w.signals || [];
            const hasSig = sigs.length > 0;
            const dotCol = hasSig ? '#f59e0b' : '#555';
            const dotSize = hasSig ? 16 : 12;
            html += '<div style="display:flex;flex-direction:column;align-items:center;min-width:110px;position:relative">';
            if (i < evidence.length - 1) html += '<div style="position:absolute;top:7px;left:50%;width:100%;height:2px;background:#e4e2de;z-index:0"></div>';
            html += `<div style="width:${dotSize}px;height:${dotSize}px;border-radius:50%;background:${dotCol};border:2px solid ${dotCol};z-index:1"></div>`;
            html += `<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-top:4px">${(w.complaint_date||'').slice(0,10)}</div>`;
            html += `<div style="font-size:9px;color:var(--text3);margin-top:2px">#${i+1}</div>`;
            if (hasSig) {
                sigs.forEach(s => {
                    const delta = s.delta || s.delta_pct || 0;
                    const sigCol = Math.abs(delta) > 2 ? '#ef4444' : Math.abs(delta) > 1.5 ? '#f59e0b' : '#888';
                    const sigName = s.signal || s.feature || '?';
                    html += `<div style="font-family:var(--font-mono);font-size:8px;color:${sigCol};margin-top:3px;max-width:100px;text-align:center">${sigName}: ${typeof delta === 'number' ? (delta>0?'+':'')+delta.toFixed(1)+'σ' : delta}</div>`;
                });
            } else {
                html += '<div style="font-size:8px;color:#555;margin-top:3px">no signal</div>';
            }
            html += '</div>';
        });
        if (inv.classification === 'CONSISTENT_PATTERN') {
            html += '<div style="position:absolute;bottom:0;left:10%;right:10%;text-align:center;font-family:var(--font-mono);font-size:9px;color:#f59e0b">→ progressive worsening trend →</div>';
        }
        html += '</div></div>';
    }

    // Section C — Evidence Chain Table
    if (evidence.length > 0) {
        html += '<div class="card"><div class="card-title">Evidence Chain</div>';
        html += '<table style="width:100%;font-size:11px;border-collapse:collapse">';
        html += '<tr style="font-family:var(--font-mono);font-size:9px;color:var(--text3)"><th style="text-align:left;padding:4px 8px">COMPLAINT</th><th>DATE</th><th>SIGNALS FIRED</th><th>MAX Δ</th><th>WINDOW VERDICT</th></tr>';
        evidence.forEach((w, i) => {
            const sigs = w.signals || [];
            const sigNames = sigs.map(s => s.signal || s.feature || '?').join(', ') || 'none';
            const maxDelta = sigs.length > 0 ? Math.max(...sigs.map(s => Math.abs(s.delta || s.delta_pct || 0))) : 0;
            const wVerdict = sigs.length > 0 ? '<span style="color:#22c55e">CORROBORATED</span>' : '<span style="color:#888">NO SIGNAL</span>';
            html += `<tr style="border-top:1px solid #e8e7e3">
                <td style="padding:5px 8px;font-family:var(--font-mono)">#${i+1}</td>
                <td style="font-size:10px">${(w.complaint_date||'').slice(0,10)}</td>
                <td style="font-size:10px;color:var(--text2)">${sigNames}</td>
                <td style="font-family:var(--font-mono);text-align:center">${maxDelta > 0 ? maxDelta.toFixed(1)+'σ' : '—'}</td>
                <td>${wVerdict}</td>
            </tr>`;
        });
        html += '</table></div>';
    }

    // Section D — OEM Intelligence Panel
    html += '<div class="g2">';
    html += `<div class="card" style="background:#0f0800;border-color:#22c55e33">
        <div class="card-title" style="color:#22c55e">OEM Recommended Action</div>
        <div style="font-size:12px;color:#22c55e;line-height:1.6">${inv.oem_action || 'No OEM action specified'}</div>
    </div>`;
    html += `<div class="card">
        <div class="card-title">Operator / Environment Finding</div>
        <div style="font-size:12px;color:var(--text2);line-height:1.6">${inv.operator_finding || 'No operator finding recorded'}</div>
    </div>`;
    html += '</div>';

    // Liability badge
    if (inv.verdict === 'PHYSICS_CONFIRMED' || inv.verdict === 'PROGRESSIVE') {
        html += `<div style="display:inline-block;padding:4px 12px;border-radius:20px;font-family:var(--font-mono);font-size:10px;background:#ef444422;color:#ef4444;border:1px solid #ef444444;margin-bottom:10px">PHYSICS CONFIRMED → OEM liability</div>`;
    } else {
        html += `<div style="display:inline-block;padding:4px 12px;border-radius:20px;font-family:var(--font-mono);font-size:10px;background:#55555522;color:#888;border:1px solid #55555544;margin-bottom:10px">UNCONFIRMED → Operator/environment</div>`;
    }

    // Section E — Rule 28 Disclosure
    html += `<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);padding:8px 0;border-top:1px solid #e8e7e3;margin-top:8px">
        Rule 28: Verdict requires ≥1 signal >1.5σ at complaint window. Event count is not a signal. Complaints trigger investigation — telemetry decides verdict.
    </div>`;

    setTabContent(html);
}
