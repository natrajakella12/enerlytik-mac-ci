// T8 DEKF — Dual Extended Kalman Filter state estimation panel

async function loadT8() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view DEKF state estimation')); return; }
    setTabContent(loadingSkeleton(5));
    const id = STATE.batteryId;
    const [dekf, status] = await Promise.all([
        API.batteryDekf(id),
        API.batteryDekfStatus(id),
    ]);

    const chem = (status && status.chemistry) || STATE.chemistry;
    const promoted = status && status.promoted;
    const shadowExists = dekf && dekf.data && dekf.data.length > 0;

    let html = '';

    // Promotion status banner (always visible)
    if (promoted) {
        html += `<div style="background:#14532d;border:1px solid #16a34a;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-family:var(--font-mono)">
            <div style="font-size:12px;font-weight:600;color:#4ade80">DEKF ACTIVE — SoC correction live in production</div>
            <div style="font-size:10px;color:#86efac;margin-top:4px">Chemistry: ${chem}</div>
        </div>`;
    } else {
        html += `<div style="background:#eff6ff;border:1px solid #93c5fd;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-family:var(--font-mono)">
            <div style="font-size:12px;font-weight:600;color:#1e40af">SHADOW MODE ACTIVE</div>
            <div style="font-size:10px;color:#1e3a8a;margin-top:4px;line-height:1.6">
                DEKF running in parallel — not influencing any production scores.<br>
                Shadow outputs available for review below.
            </div>
            <div style="font-size:9px;color:#60a5fa;margin-top:6px">To promote: <code style="background:#1e3a5f;padding:2px 6px;border-radius:3px">python pipeline/dekf_promote.py --chemistry ${chem} --confirm</code></div>
        </div>`;
    }

    // Section A — SoC Comparison Chart
    html += '<div class="card"><div class="card-title">SoC Comparison — BMS vs DEKF</div>';
    if (shadowExists) {
        html += '<div style="position:relative;height:160px"><canvas id="dekf-soc-chart"></canvas></div>';
        const pts = dekf.data.slice(-200); // last 200 points
        const labels = pts.map(p => (p.timestamp||'').slice(5,16));
        const bmsSoc = pts.map(p => p.soc_bms || p.pack_soc || null);
        const dekfSoc = pts.map(p => p.soc_dekf || null);
        // Compute mean correction
        const corrections = pts.filter(p => p.soc_bms != null && p.soc_dekf != null).map(p => Math.abs((p.soc_dekf||0) - (p.soc_bms||0)));
        const meanCorr = corrections.length > 0 ? (corrections.reduce((a,b)=>a+b,0)/corrections.length).toFixed(1) : '—';
        html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-top:6px">DEKF corrects BMS by mean ${meanCorr}%</div>`;
        // Defer chart render
        setTimeout(() => {
            const ctx = document.getElementById('dekf-soc-chart');
            if (!ctx) return;
            new Chart(ctx, {
                type:'line', data: { labels, datasets:[
                    {label:'BMS SoC', data:bmsSoc, borderColor:'#888', borderDash:[4,4], borderWidth:1.5, pointRadius:0, fill:false, tension:0.2},
                    {label:'DEKF SoC', data:dekfSoc, borderColor:'#22c55e', borderWidth:1.5, borderDash: promoted ? [] : [2,2], pointRadius:0, fill:false, tension:0.2},
                ]},
                options: {
                    responsive:true, maintainAspectRatio:false, animation:false,
                    plugins:{legend:{display:true,labels:{color:'#888',font:{size:9}}}},
                    scales:{
                        x:{ticks:{color:'#888',font:{size:8},maxTicksLimit:8},grid:{color:'#e8e7e3'}},
                        y:{min:0,max:100,ticks:{color:'#888',font:{size:9}},grid:{color:'#e8e7e3'}},
                    }
                }
            });
        }, 100);
    } else {
        html += emptyState('No DEKF shadow data available','DEKF has not yet processed this battery');
    }
    html += '</div>';

    // Section B — R0 Tracking
    html += '<div class="card"><div class="card-title">' + (chem === 'NMC' ? 'R0 Tracking (NMC)' : 'Thermal R0 (LFP)') + '</div>';
    if (shadowExists) {
        html += '<div style="position:relative;height:140px"><canvas id="dekf-r0-chart"></canvas></div>';
        const pts = dekf.data.slice(-200);
        const labels = pts.map(p => (p.timestamp||'').slice(5,16));
        const r0dekf = pts.map(p => p.r0_dekf_pack_mohm || null);
        if (chem === 'LFP') {
            html += '<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-top:6px">Thermal monitoring only — not a degradation signal (Rule 14)</div>';
        } else {
            html += '<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-top:6px">Per-cell = pack ÷ 14 (14S configuration)</div>';
        }
        setTimeout(() => {
            const ctx = document.getElementById('dekf-r0-chart');
            if (!ctx) return;
            new Chart(ctx, {
                type:'line', data: { labels, datasets:[
                    {label:'DEKF R0 (mΩ)', data:r0dekf, borderColor: chem==='NMC' ? '#f59e0b' : '#888', borderWidth:1.5, pointRadius:0, fill:false, tension:0.2},
                ]},
                options: {
                    responsive:true, maintainAspectRatio:false, animation:false,
                    plugins:{legend:{display:false}},
                    scales:{
                        x:{ticks:{color:'#888',font:{size:8},maxTicksLimit:8},grid:{color:'#e8e7e3'}},
                        y:{ticks:{color:'#888',font:{size:9}},grid:{color:'#e8e7e3'}},
                    }
                }
            });
        }, 150);
    } else {
        html += '<div style="color:var(--text3);font-size:12px;padding:8px 0">No R0 tracking data available in shadow table.</div>';
    }
    html += '</div>';

    // Section C — Filter Health Diagnostics
    html += '<div class="card"><div class="card-title">Filter Health Diagnostics</div>';
    const p2 = status && status.phase2_status ? status.phase2_status : {};
    html += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
    const metrics = [
        ['SoC correction mean', shadowExists ? 'Computing...' : '—', 'PENDING'],
        ['R0 floor hits', '—', 'PENDING'],
        ['OCV source', p2.ocv_fix ? 'BATTERY_SPECIFIC' : 'CHEMISTRY_STANDARD', p2.ocv_fix ? 'GOOD' : 'STANDARD'],
        ['Session resets', '—', 'normal'],
    ];
    if (chem === 'LFP') {
        metrics.push(['Flat region %', '—', 'LFP only']);
        metrics.push(['Coulomb mode %', '—', 'LFP only']);
    }
    metrics.forEach(([name, val, stat]) => {
        const statCol = stat === 'GOOD' ? '#22c55e' : stat === 'PENDING' ? '#888' : '#f59e0b';
        html += `<tr style="border-bottom:1px solid #e8e7e3">
            <td style="padding:6px 8px;color:var(--text2)">${name}</td>
            <td style="font-family:var(--font-mono);text-align:center">${val}</td>
            <td style="text-align:center;font-size:10px;color:${statCol}">${stat}</td>
        </tr>`;
    });
    html += '</table></div>';

    // Section D — Phase 2 Fix Status
    html += '<div class="card"><div class="card-title">Phase 2 Status — ' + chem + '</div>';
    if (chem === 'NMC') {
        html += `<div style="font-size:12px;line-height:2;color:var(--text2)">
            <div>OCV fix applied: <strong>${p2.ocv_fix ? 'YES' : 'NO'}</strong></div>
            <div>R0 decoupling active: <strong>${p2.r0_decoupling ? 'YES' : 'NO'}</strong></div>
        </div>`;
    } else {
        html += `<div style="font-size:12px;line-height:2;color:var(--text2)">
            <div>Hybrid Coulomb/DEKF: <strong>${p2.hybrid_coulomb ? 'YES' : 'NO'}</strong></div>
            <div>Boundary anchoring: <strong>NO</strong></div>
        </div>`;
    }
    html += '</div>';

    // Section E — Promotion Checklist
    html += '<div class="card"><div class="card-title">Promotion Checklist</div>';
    const cl = status && status.checklist ? status.checklist : {};
    const checks = [
        [chem === 'NMC' ? 'NMC SoC correction < 8%' : 'LFP SoC correction < 10%', cl.soc_correction_ok],
        [chem === 'NMC' ? 'NMC R0 MAPE < 25%' : 'LFP drift corrections > 0', cl.r0_mape_ok],
        ['Regression tests pass', cl.regression_tests],
        [`Shadow duration > 4 weeks (${cl.shadow_duration_weeks||0}w)`, (cl.shadow_duration_weeks||0) >= 4],
    ];
    let allPass = true;
    checks.forEach(([label, pass]) => {
        const icon = pass ? '✓' : '✗';
        const col = pass ? '#22c55e' : '#ef4444';
        if (!pass) allPass = false;
        html += `<div style="display:flex;align-items:center;gap:8px;padding:4px 0">
            <span style="color:${col};font-size:14px;font-weight:700">${icon}</span>
            <span style="font-size:12px;color:var(--text2)">${label}</span>
            <span style="font-family:var(--font-mono);font-size:10px;color:${col}">${pass ? 'PASS' : 'FAIL'}</span>
        </div>`;
    });
    if (allPass) {
        html += `<div style="background:#14532d44;border:1px solid #22c55e;border-radius:6px;padding:8px 12px;margin-top:8px;font-family:var(--font-mono);font-size:11px;color:#22c55e">Ready for promotion — run dekf_promote.py --confirm</div>`;
    } else {
        html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-top:8px">Promotion blocked — resolve FAIL items above.</div>`;
    }
    html += '</div>';

    setTabContent(html);
}
