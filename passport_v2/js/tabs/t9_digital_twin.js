// T9 Digital Twin — DT architecture, R0 foundation, DT-1 through DT-5 roadmap

async function loadT9() {
    const gps = await API.gpsCheck();
    let html = '';

    // Header banner
    html += `<div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-family:var(--font-mono)">
        <div style="font-size:12px;font-weight:600;color:#92400e">SPRINT DT — IN DEVELOPMENT</div>
        <div style="font-size:10px;color:#78350f;margin-top:4px;line-height:1.6">
            R0 state estimation promoted — DT foundation ready<br>
            DT-1 Electrical Twin: BUILDING · DT-2 Degradation: SCHEDULED · DT-3 Thermal: BLOCKED
        </div>
    </div>`;

    // Section A — What's Live Now
    html += '<div class="card"><div class="card-title">R0 Foundation — Live Now</div>';
    html += '<div class="g2">';
    html += `<div style="text-align:center;padding:16px;background:#dcfce7;border-radius:6px">
        <div style="font-size:10px;color:#166534;font-family:var(--font-mono)">NMC</div>
        <div style="font-size:18px;font-weight:700;color:#166534;margin-top:4px">DEKF R0 Promoted</div>
        <div style="font-size:11px;color:#166534;margin-top:4px">7.3% MAPE vs pulse ECM · Per-cell tracking live</div>
    </div>`;
    html += `<div style="text-align:center;padding:16px;background:#dcfce7;border-radius:6px">
        <div style="font-size:10px;color:#166534;font-family:var(--font-mono)">LFP</div>
        <div style="font-size:18px;font-weight:700;color:#166534;margin-top:4px">DEKF R0 Thermal Promoted</div>
        <div style="font-size:11px;color:#166534;margin-top:4px">100% ECM agreement · Thermal monitoring active</div>
    </div>`;
    html += '</div>';
    html += '<div style="font-size:11px;color:var(--text3);margin-top:8px">Digital Twin electrical simulation uses DEKF R0 as its internal resistance parameter. R0 foundation is production-ready.</div>';
    html += '</div>';

    // Section B — DT-1 Preview
    html += '<div class="card"><div class="card-title">DT-1 Preview — Electrical Twin</div>';
    html += `<div style="text-align:center;padding:20px;background:var(--bg2);border-radius:6px;margin-bottom:8px">
        <div style="font-size:11px;color:var(--text3);font-family:var(--font-mono);margin-bottom:8px">SIMULATED EXAMPLE — not real data</div>
        <div style="position:relative;height:120px;max-width:100%"><canvas id="dt1-preview"></canvas></div>
    </div>`;
    html += '<div style="font-size:12px;color:var(--text2);line-height:1.7">DT-1 will simulate voltage/current response for any usage pattern, enabling SoC estimation without BMS dependency.</div>';
    html += '</div>';

    // Section C — Architecture Preview (5 layers)
    const layers = [
        {name:'DT-1 Electrical Twin',status:'BUILDING',color:'#dbeafe',textCol:'#1e40af',input:'DEKF R0 + OCV curve',output:'Simulated V/I response, BMS-free SoC',dep:null,sprint:'Sprint DT-1'},
        {name:'DT-2 Degradation Twin',status:'SCHEDULED',color:'#fef3c7',textCol:'#92400e',input:'DT-1 + historical R0 trajectory',output:'SOH projection, cycle life, 3-pathway degradation',dep:'Post DT-1',sprint:'Sprint DT-2'},
        {name:'DT-3 Thermal Twin',status:'BLOCKED',color:'#fee2e2',textCol:'#991b1b',input:'DT-2 + ambient temp + GPS elevation',output:'Thermal stress prediction, location-aware model',dep:'OpenWeatherMap API + GPS',sprint:'Sprint DT-3'},
        {name:'DT-4 Usage Scenario Engine',status:'SCHEDULED',color:'#fef3c7',textCol:'#92400e',input:'DT-2 + usage pattern parameters',output:'"What-if" battery life under different behaviours',dep:'Post DT-2',sprint:'Sprint DT-4'},
        {name:'DT-5 Fleet Monte Carlo',status:'SCHEDULED',color:'#fef3c7',textCol:'#92400e',input:'DT-4 × fleet size',output:'Fleet replacement planning, risk distribution, capex',dep:'Post DT-4',sprint:'Sprint DT-5'},
    ];

    html += '<div class="card"><div class="card-title">Digital Twin Architecture — 5 Layers</div>';
    layers.forEach((l, i) => {
        const statusBadge = l.status === 'BUILDING' ? '<span style="padding:2px 8px;border-radius:12px;font-size:9px;background:#dbeafe;color:#1e40af;font-family:var(--font-mono)">BUILDING</span>'
            : l.status === 'BLOCKED' ? '<span style="padding:2px 8px;border-radius:12px;font-size:9px;background:#fee2e2;color:#991b1b;font-family:var(--font-mono)">BLOCKED</span>'
            : '<span style="padding:2px 8px;border-radius:12px;font-size:9px;background:#fef3c7;color:#92400e;font-family:var(--font-mono)">SCHEDULED</span>';
        html += `<div style="background:${l.color};border-radius:6px;padding:12px 16px;margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <span style="font-family:var(--font-mono);font-size:12px;font-weight:600;color:${l.textCol}">${l.name}</span>
                ${statusBadge}
            </div>
            <div style="font-size:11px;color:${l.textCol};opacity:0.8;line-height:1.6">
                <strong>In:</strong> ${l.input}<br><strong>Out:</strong> ${l.output}
                ${l.dep ? '<br><strong>Depends:</strong> '+l.dep : ''}
            </div>
        </div>`;
        if (i < layers.length - 1) html += '<div style="text-align:center;color:var(--text3);font-size:10px;margin:2px 0">↓</div>';
    });
    html += '</div>';

    // Section D — DT-3 Unblock Path
    html += `<div class="card" style="border-left:3px solid #dc2626"><div class="card-title">Unblock DT-3 — Thermal Twin</div>
        <div style="font-size:12px;color:var(--text2);line-height:1.8">
            <strong>Required:</strong><br>
            1. Ambient temperature by location — OpenWeatherMap API (free tier)<br>
            2. GPS coordinates per battery session — check gps_raw_30sec in DuckDB<br><br>
            <strong>GPS Data Check:</strong>
        </div>`;
    if (gps) {
        if (gps.available) {
            html += `<div style="background:#dcfce7;border-radius:6px;padding:8px 12px;margin-top:6px;font-family:var(--font-mono);font-size:11px;color:#166534">
                GPS data AVAILABLE — ${(gps.rows||0).toLocaleString()} rows, lat/lon columns present.
                ${gps.sample ? '<br>Sample: '+JSON.stringify(gps.sample[0]) : ''}
            </div>`;
            html += '<div style="font-size:11px;color:var(--accent);margin-top:6px">Action: Connect OpenWeatherMap API to unblock DT-3.</div>';
        } else {
            html += `<div style="background:#fee2e2;border-radius:6px;padding:8px 12px;margin-top:6px;font-family:var(--font-mono);font-size:11px;color:#991b1b">
                GPS data: ${gps.reason || 'Not available'}.
                ${gps.has_latitude === false ? ' lat column missing.' : ''}
                ${gps.has_longitude === false ? ' lon column missing.' : ''}
            </div>`;
        }
    } else {
        html += '<div style="color:var(--text3);font-size:11px;margin-top:6px">Could not check GPS — DuckDB may be locked.</div>';
    }
    html += '</div>';

    setTabContent(html);

    // DT-1 preview chart (simulated)
    setTimeout(() => {
        const ctx = document.getElementById('dt1-preview');
        if (!ctx) return;
        const n = 50;
        const t = Array.from({length:n}, (_,i) => i);
        const measured = t.map(i => 52 - i*0.08 + Math.sin(i*0.3)*0.5 + (Math.random()-0.5)*0.3);
        const simulated = t.map(i => 52 - i*0.08 + Math.sin(i*0.3)*0.5);
        new Chart(ctx, {
            type:'line', data:{labels:t, datasets:[
                {label:'Measured V', data:measured, borderColor:'#555', borderWidth:1.5, pointRadius:0, tension:0.3},
                {label:'ECM Simulation', data:simulated, borderColor:'var(--accent)', borderDash:[4,4], borderWidth:1.5, pointRadius:0, tension:0.3},
            ]},
            options:{responsive:true,maintainAspectRatio:false,animation:false,
                plugins:{legend:{display:true,labels:{color:'#555',font:{size:9}}}},
                scales:{x:{display:false},y:{ticks:{color:'#888',font:{size:9}},grid:{color:'#e4e2de'},title:{display:true,text:'V',color:'#888',font:{size:9}}}}}
        });
    }, 100);
}
