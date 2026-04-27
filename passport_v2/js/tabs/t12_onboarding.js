// T12 Onboarding — new OEM dataset + new battery flows

async function loadT12() {
    const queue = await API.apiFetch('/api/platform/queue');
    const pending = queue ? queue.filter(b => b.status === 'PENDING_SCORING').length : 0;
    const insuf = queue ? queue.filter(b => b.status === 'INSUFFICIENT_DATA').length : 0;
    const active = (await API.apiFetch('/api/platform/summary'))?.total_batteries || 0;

    let html = '';

    // Live counts
    html += `<div class="card" style="display:flex;gap:20px;justify-content:center;padding:14px">
        <div style="text-align:center"><div style="font-family:var(--font-head);font-size:22px;font-weight:700;color:var(--accent)">${active}</div><div style="font-size:10px;color:var(--text3)">Fully Active</div></div>
        <div style="text-align:center"><div style="font-family:var(--font-head);font-size:22px;font-weight:700;color:#d97706">${pending}</div><div style="font-size:10px;color:var(--text3)">Pending Scoring</div></div>
        <div style="text-align:center"><div style="font-family:var(--font-head);font-size:22px;font-weight:700;color:#dc2626">${insuf}</div><div style="font-size:10px;color:var(--text3)">Insufficient Data</div></div>
    </div>`;

    // Sub-tab switcher
    html += `<div style="display:flex;gap:8px;margin-bottom:14px">
        <button onclick="loadT12OEM()" id="t12-btn-oem" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--accent);background:var(--accent);color:#000;cursor:pointer;font-weight:600">New OEM Dataset</button>
        <button onclick="loadT12Battery()" id="t12-btn-bat" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--bg3);background:var(--bg1);color:var(--text2);cursor:pointer">New Battery</button>
    </div>`;
    html += '<div id="t12-content"></div>';
    setTabContent(html);
    loadT12OEM();
}

function _step(num, title, actions, output, fail, color) {
    return `<div class="card" style="border-left:3px solid ${color}">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <div style="width:28px;height:28px;border-radius:50%;background:${color};color:#fff;display:flex;align-items:center;justify-content:center;font-family:var(--font-head);font-size:14px;font-weight:700;flex-shrink:0">${num}</div>
            <div style="font-size:13px;font-weight:600">${title}</div>
        </div>
        <div style="font-size:11px;color:var(--text2);line-height:1.7;padding-left:38px">
            ${actions.map(a => `<div>• ${a}</div>`).join('')}
            ${output ? `<div style="margin-top:4px;color:var(--accent);font-family:var(--font-mono);font-size:10px">→ ${output}</div>` : ''}
            ${fail ? `<div style="margin-top:4px;color:#dc2626;font-family:var(--font-mono);font-size:10px">✗ ${fail}</div>` : ''}
        </div>
    </div>`;
}

function loadT12OEM() {
    document.getElementById('t12-btn-oem').style.background = 'var(--accent)';
    document.getElementById('t12-btn-oem').style.color = '#000';
    document.getElementById('t12-btn-bat').style.background = 'var(--bg1)';
    document.getElementById('t12-btn-bat').style.color = 'var(--text2)';

    const grey='#999', blue='#1d4ed8', green='#16a34a';
    document.getElementById('t12-content').innerHTML = [
        _step(1,'Source Data',['xlsx/csv files received from OEM','File format validation','Serial number extraction → battery_id mapping'],'serial_number_map populated','Format not recognised → halt, request schema',grey),
        _step(2,'Ingest Quality Gates (×8)',['Gate 1: Source validation','Gate 2: Resolution ≥20-sec check','Gate 3: No downsampling','Gate 4: Voltage/current sanity bounds','Gate 5: Timestamp gap detection','Gate 6: Duplicate removal','Gate 7: Minimum data sufficiency (2+ weeks)','Gate 8: Row count reconciliation'],'telemetry_raw + ingest_audit_log entry','Any gate FAIL → halt, do not ingest degraded data',grey),
        _step(3,'Feature Engineering',['01_aggregate.py computes 38 LFP / 32 NMC features','Trajectory slopes, rolling 4-week windows','Cumulative lifetime features, volatility'],'vehicle_weekly_features or nmc_weekly_features','Missing source columns → feature gaps logged',blue),
        _step(4,'Scoring Eligibility',['data_sufficiency.py: min 6 weeks + 80% feature coverage','ELIGIBLE → proceed | PENDING → queue | INSUFFICIENT → flag'],'battery_scoring_queue entry','Below 50% features → INSUFFICIENT_DATA, monitor until improved',blue),
        _step(5,'Model Selection',['Template engine: chemistry + battery_model → template','Template selects: which models, which thresholds, which features'],'Template assigned from template_registry',null,blue),
        _step(6,'First Scoring Run',['Health score (L1 self-baseline + L2 physics + L3 fleet)','Range P10/P50/P90 (if LFP)','Event detection E1–E11','Tier assignment: PRIME → CRITICAL'],'battery_health_scores_v2 + battery_intelligence','Score with data_confidence: MEDIUM (new battery, baseline establishing)',green),
        _step(7,'Battery Passport',['All intelligence sections populated','data_confidence: MEDIUM (new)','Investigation engine eligible after 2+ complaints'],'Passport HTML generated',null,green),
        _step(8,'Production State',['Weekly pipeline runs automatically','Continuous improvement: MEDIUM → HIGH at Week 8+','DEKF running in shadow mode'],'Fully active battery in fleet',null,green),
    ].join('');
}

function loadT12Battery() {
    document.getElementById('t12-btn-bat').style.background = 'var(--accent)';
    document.getElementById('t12-btn-bat').style.color = '#000';
    document.getElementById('t12-btn-oem').style.background = 'var(--bg1)';
    document.getElementById('t12-btn-oem').style.color = 'var(--text2)';

    const grey='#999', blue='#1d4ed8', green='#16a34a';
    document.getElementById('t12-content').innerHTML = [
        _step(1,'Commissioning',['battery_id assigned in batteries table','Chemistry, OEM, model registered','Commissioning date logged (first 10km or first telemetry)'],'batteries table entry',null,grey),
        _step(2,'First Telemetry',['20-sec data starts flowing to telemetry_raw','ingest_audit_log entry created automatically','Resolution validated: must be ≤35 sec'],'Telemetry ingested, audit logged','Resolution >60s → halt, investigate source',grey),
        _step(3,'Weeks 1–8: Baseline Period',['data_confidence: LOW → MEDIUM','Self-baseline establishing (efficiency, cell spread)','Not yet eligible for full scoring','Enters battery_scoring_queue as PENDING'],'Baseline features accumulating','Insufficient weeks → remain in queue',blue),
        _step(4,'Scoring Eligibility (Week 6+)',['Rolling windows (4W) now available','80% feature coverage reached','Automatic scoring triggered by queue processor','First tier assignment'],'battery_health_scores_v2 row created','Feature coverage <80% → remains PENDING',blue),
        _step(5,'Full Intelligence (Week 8+)',['L1 self-baseline fully active','L2 + L3 scoring layers complete','data_confidence: HIGH','Investigation engine eligible (if complaints exist)','Passport fully populated'],'Complete battery intelligence',null,green),
        _step(6,'Ongoing Operations',['Weekly pipeline runs: features → scores → events → intelligence','DEKF running in shadow (SoC correction)','Regression anchors updated if this battery becomes notable','Compare mode available for fleet benchmarking'],'Continuous monitoring',null,green),
    ].join('');
}
