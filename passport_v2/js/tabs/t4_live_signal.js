// T4 Live Signal — telemetry charts (voltage, current, SoC, temperature)

let t4Days = 7;
let t4Charts = [];

async function loadT4() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view live telemetry')); return; }
    setTabContent(loadingSkeleton(8));
    t4Charts.forEach(c => c.destroy()); t4Charts = [];

    const id = STATE.batteryId;
    const data = await API.batteryTelemetry(id, t4Days);

    let html = '';

    // Controls
    html += '<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">';
    html += '<span style="font-family:var(--font-mono);font-size:10px;color:var(--text3)">TIME RANGE</span>';
    [1,3,7,14].forEach(d => {
        const active = d === t4Days;
        html += `<button onclick="t4Days=${d};loadT4()" style="padding:4px 12px;border-radius:4px;font-family:var(--font-mono);font-size:11px;border:1px solid ${active?'var(--accent)':'#e4e2de'};background:${active?'var(--accent)':'#ffffff'};color:${active?'#000':'var(--text3)'};cursor:pointer">${d}d</button>`;
    });
    html += '</div>';

    // Seasonal context annotations
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">';
    const now = new Date();
    const month = now.getMonth(); // 0-indexed
    if (month >= 2 && month <= 4) {
        html += '<div style="font-size:10px;padding:4px 10px;border-radius:4px;background:#fee2e220;color:#dc2626;border:1px solid #fee2e2;font-family:var(--font-mono)">Peak summer — thermal stress risk period</div>';
    } else if (month >= 5 && month <= 8) {
        html += '<div style="font-size:10px;padding:4px 10px;border-radius:4px;background:#fef3c720;color:#d97706;border:1px solid #fef3c7;font-family:var(--font-mono)">Monsoon season — higher humidity, different usage patterns</div>';
    } else if (month >= 10 || month <= 1) {
        html += '<div style="font-size:10px;padding:4px 10px;border-radius:4px;background:#dbeafe20;color:#1d4ed8;border:1px solid #dbeafe;font-family:var(--font-mono)">Winter — lower temps, possible reduced range</div>';
    }
    html += '</div>';

    if (!data || !data.points || data.points.length === 0) {
        html += emptyState('No telemetry data available','Check ingest_audit_log for ingest status');
        setTabContent(html); return;
    }

    const pts = data.points;
    const labels = pts.map(p => { const t = p.timestamp || ''; return t.length > 16 ? t.slice(5,16) : t; });
    const chem = STATE.chemistry;

    // Physics bounds
    const vBounds = chem === 'NMC' ? {lo:35, hi:58.8} : {lo:38.4, hi:57.6};

    html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);margin-bottom:8px">${pts.length.toLocaleString()} data points · step ${data.step} · total ${data.total_rows.toLocaleString()} rows</div>`;

    // Chart 1 — Voltage
    html += '<div class="card"><div class="card-title">Pack Voltage (V)</div><div style="position:relative;height:150px"><canvas id="ch-voltage"></canvas></div></div>';

    // Chart 2 — Current
    html += '<div class="card"><div class="card-title">Current (A)</div><div style="position:relative;height:150px"><canvas id="ch-current"></canvas></div></div>';

    // Chart 3 — SoC
    html += '<div class="card"><div class="card-title">State of Charge (%)</div><div style="position:relative;height:150px"><canvas id="ch-soc"></canvas></div></div>';

    // Chart 4 — Temperature
    html += '<div class="card"><div class="card-title">Temperature (°C)</div><div style="position:relative;height:150px"><canvas id="ch-temp"></canvas></div></div>';

    setTabContent(html);

    // Render charts
    const maxTicks = 8;
    const commonOpts = {
        animation: false,
        plugins: { legend:{display:false} },
        elements: { point:{radius:0}, line:{borderWidth:1.5} },
        scales: {
            x: { ticks:{color:'#888',font:{size:8},maxTicksLimit:maxTicks,maxRotation:0}, grid:{color:'#e8e7e3'} },
        }
    };

    // Voltage
    const voltData = pts.map(p => p.pack_voltage);
    t4Charts.push(new Chart(document.getElementById('ch-voltage'), {
        type:'line', data: { labels, datasets:[
            {data:voltData, borderColor:'#22c55e', fill:false, tension:0.1},
            {data:Array(labels.length).fill(vBounds.lo), borderColor:'#ef444466', borderDash:[4,4], borderWidth:1, pointRadius:0, fill:false},
            {data:Array(labels.length).fill(vBounds.hi), borderColor:'#ef444466', borderDash:[4,4], borderWidth:1, pointRadius:0, fill:false},
        ]},
        options:{...commonOpts, scales:{...commonOpts.scales, y:{ticks:{color:'#22c55e88',font:{size:9}},grid:{color:'#e8e7e3'}}}}
    }));

    // Current
    const currData = pts.map(p => p.pack_current);
    t4Charts.push(new Chart(document.getElementById('ch-current'), {
        type:'line', data: { labels, datasets:[
            {data:currData, borderColor:'#3b82f6', fill:{target:'origin',above:'rgba(34,197,94,0.05)',below:'rgba(239,68,68,0.05)'}, tension:0.1},
        ]},
        options:{...commonOpts, scales:{...commonOpts.scales, y:{ticks:{color:'#3b82f688',font:{size:9}},grid:{color:'#e8e7e3'}}}}
    }));

    // SoC
    const socData = pts.map(p => p.pack_soc);
    t4Charts.push(new Chart(document.getElementById('ch-soc'), {
        type:'line', data: { labels, datasets:[
            {data:socData, borderColor:'#f59e0b', fill:false, tension:0.1},
        ]},
        options:{...commonOpts, scales:{...commonOpts.scales,
            y:{min:0,max:100,ticks:{color:'#f59e0b88',font:{size:9}},grid:{color:'#e8e7e3'}}
        }}
    }));

    // Deep discharge annotation on SoC chart
    const deepDischarges = pts.filter(p => p.pack_soc != null && p.pack_soc < 10).length;
    if (deepDischarges > 0) {
        const ddEl = document.createElement('div');
        ddEl.style.cssText = 'font-size:10px;padding:4px 10px;color:#dc2626;font-family:var(--font-mono);margin:-8px 0 4px';
        ddEl.textContent = '⚠ ' + deepDischarges + ' deep discharge readings (SoC < 10%) — stresses battery chemistry';
        document.getElementById('ch-soc').parentElement.parentElement.appendChild(ddEl);
    }

    // Temperature
    const tempData = pts.map(p => p.temp_max);
    t4Charts.push(new Chart(document.getElementById('ch-temp'), {
        type:'line', data: { labels, datasets:[
            {data:tempData, borderColor:'#ef4444', fill:false, tension:0.1},
            {data:Array(labels.length).fill(45), borderColor:'#ef444466', borderDash:[4,4], borderWidth:1, pointRadius:0, fill:false},
        ]},
        options:{...commonOpts, scales:{...commonOpts.scales, y:{ticks:{color:'#ef444488',font:{size:9}},grid:{color:'#e8e7e3'}}}}
    }));
}
