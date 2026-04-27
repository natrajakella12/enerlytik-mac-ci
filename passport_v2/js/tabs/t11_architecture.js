// T11 Architecture — model waterfall + template catalog

async function loadT11() {
    let html = '';
    // Sub-tab switcher
    html += `<div style="display:flex;gap:8px;margin-bottom:14px">
        <button onclick="loadT11Waterfall()" id="t11-btn-wf" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--accent);background:var(--accent);color:#000;cursor:pointer;font-weight:600">Model Waterfall</button>
        <button onclick="loadT11Templates()" id="t11-btn-tp" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--bg3);background:var(--bg1);color:var(--text2);cursor:pointer">Template Catalog</button>
    </div>`;
    html += '<div id="t11-content"></div>';
    setTabContent(html);
    loadT11Waterfall();
}

function loadT11Waterfall() {
    document.getElementById('t11-btn-wf').style.background = 'var(--accent)';
    document.getElementById('t11-btn-wf').style.color = '#000';
    document.getElementById('t11-btn-tp').style.background = 'var(--bg1)';
    document.getElementById('t11-btn-tp').style.color = 'var(--text2)';

    const box = (label, script, status, color, inputs, outputs) => {
        const sBadge = status === 'PRODUCTION' ? '<span style="color:#166534;font-size:8px">● PROD</span>'
            : status === 'SHADOW' ? '<span style="color:#1e40af;font-size:8px">◌ SHADOW</span>'
            : '<span style="color:#d97706;font-size:8px">◇ PENDING</span>';
        return `<div style="background:${color};border:1px solid var(--bg3);border-radius:6px;padding:10px 14px;min-width:180px;position:relative" title="In: ${inputs}\nOut: ${outputs}">
            <div style="font-family:var(--font-mono);font-size:10px;font-weight:600;color:var(--text1)">${label}</div>
            <div style="font-size:9px;color:var(--text3);margin-top:2px">${script}</div>
            <div style="margin-top:4px">${sBadge}</div>
        </div>`;
    };

    const arrow = (style='solid') => `<div style="text-align:center;padding:4px 0;color:var(--text3);font-size:14px">${style==='dashed'?'┊':'↓'}</div>`;

    let h = '<div class="card"><div class="card-title">Intelligence Pipeline Waterfall</div>';

    // Layer 1: Data
    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
    h += box('Raw Telemetry (20-sec)', 'tier0_ingest.py', 'PRODUCTION', '#f5f5f3', 'xlsx/csv files', 'telemetry_raw + DuckDB');
    h += box('Quality Gates (×8)', 'ingest_quality_gates.py', 'PRODUCTION', '#f5f5f3', 'raw files', 'ingest_audit_log');
    h += '</div>';
    h += arrow();

    // Layer 2: Features
    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
    h += box('Weekly Features', '01_aggregate.py', 'PRODUCTION', '#dbeafe', 'telemetry_raw', 'vehicle_weekly_features (181 cols)');
    h += box('NMC Features', 'nmc_feature_engineer.py', 'PRODUCTION', '#dbeafe', 'nmc_telemetry', 'nmc_weekly_features (56 cols)');
    h += box('DEKF SoC/R0', 'dekf_nmc/lfp.py', 'SHADOW', '#fef3c7', 'DuckDB 30-sec', 'dekf_shadow tables');
    h += box('Pulse ECM', 'pulse_ecm.py', 'PRODUCTION', '#dbeafe', 'telemetry_raw', 'nmc_ecm_real');
    h += '</div>';
    h += arrow();

    // Layer 3: Intelligence
    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
    h += box('Cluster Assignment', '02_classify.py', 'PRODUCTION', '#dcfce7', 'VWF', 'battery_usage_clusters');
    h += box('Event Detection', '03_signals_v2.py', 'PRODUCTION', '#dcfce7', 'VWF + clusters', 'vehicle_events (E1-E11)');
    h += box('Health Scoring', '04_score_v3.py', 'PRODUCTION', '#dcfce7', 'VWF + events', 'battery_health_scores_v2');
    h += box('Range P10/P50/P90', 'score_batteries.py', 'PRODUCTION', '#dcfce7', 'VWF (80% gate)', 'range_forecasts');
    h += '</div>';
    h += arrow();

    // Layer 4: Higher intelligence
    h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
    h += box('NMC Dual-Track', 'nmc_phase_b.py', 'PRODUCTION', '#dcfce7', 'NMC features + ECM', 'battery_intelligence');
    h += box('Investigation Engine', 'investigation_engine.py', 'PRODUCTION', '#dcfce7', 'complaints + telemetry', 'battery_investigations');
    h += box('Survival Model', 'train_survival_v2_1.py', 'PRODUCTION', '#dcfce7', 'NMC features', 'nmc_service_forecast');
    h += '</div>';
    h += arrow();

    // Layer 5: Outputs
    h += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
    h += box('Battery Passport', 'gen_passport_nmc.py', 'PRODUCTION', '#0f0800;color:#22c55e', 'all intelligence', 'HTML passports');
    h += box('DB API (3001)', 'db_api.py', 'PRODUCTION', '#0f0800;color:#22c55e', 'production DB', 'REST API');
    h += box('RAG KB (8001)', 'rag_api.py', 'PRODUCTION', '#0f0800;color:#22c55e', 'ChromaDB', 'LLM proxy');
    h += '</div>';

    h += '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-top:12px;padding-top:8px;border-top:1px solid var(--bg3)">';
    h += 'Solid boxes = PRODUCTION · Amber boxes = SHADOW/PENDING · Hover any box for input/output tables';
    h += '</div></div>';

    document.getElementById('t11-content').innerHTML = h;
}

async function loadT11Templates() {
    document.getElementById('t11-btn-tp').style.background = 'var(--accent)';
    document.getElementById('t11-btn-tp').style.color = '#000';
    document.getElementById('t11-btn-wf').style.background = 'var(--bg1)';
    document.getElementById('t11-btn-wf').style.color = 'var(--text2)';

    const templates = await API.apiFetch('/api/platform/templates');
    let h = '';

    if (templates && templates.length > 0) {
        h += '<div class="g2">';
        templates.forEach(t => {
            const statusCol = t.status === 'ACTIVE' ? '#166534' : '#d97706';
            const statusBg = t.status === 'ACTIVE' ? '#dcfce7' : '#fef3c7';
            h += `<div class="card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <div style="font-family:var(--font-mono);font-size:12px;font-weight:600">${t.template_id}</div>
                    <span style="padding:2px 8px;border-radius:12px;font-size:9px;background:${statusBg};color:${statusCol};font-family:var(--font-mono)">${t.status}</span>
                </div>
                <div style="font-size:12px;color:var(--text2);margin-bottom:6px">${t.display_name||''}</div>
                <div style="font-size:11px;line-height:1.8;color:var(--text3)">
                    Chemistry: <strong>${t.chemistry}</strong> · ${t.capacity_ah||'?'}Ah · ${t.cells_in_series||'?'}S<br>
                    Sampling: ${t.sampling_interval_sec||'?'}s · Models: ${t.models_count||0}
                </div>
                <details style="margin-top:8px"><summary style="font-family:var(--font-mono);font-size:9px;color:var(--text3);cursor:pointer">View JSON</summary>
                    <pre style="font-size:9px;background:var(--bg2);padding:8px;border-radius:4px;margin-top:4px;overflow-x:auto;color:var(--text2)">${JSON.stringify(t, null, 2)}</pre>
                </details>
            </div>`;
        });
        h += '</div>';
    } else {
        h += emptyState('No templates found', 'Template registry may be empty');
    }

    // How templates work
    h += `<div class="card" style="margin-top:12px">
        <div class="card-title">How Templates Work</div>
        <div style="display:flex;gap:16px;font-size:12px;color:var(--text2)">
            <div style="flex:1;padding:8px;background:var(--bg2);border-radius:6px;text-align:center">
                <div style="font-family:var(--font-head);font-size:16px;font-weight:700;color:var(--accent);margin-bottom:4px">1</div>
                New battery onboarded → chemistry + type detected
            </div>
            <div style="flex:1;padding:8px;background:var(--bg2);border-radius:6px;text-align:center">
                <div style="font-family:var(--font-head);font-size:16px;font-weight:700;color:var(--accent);margin-bottom:4px">2</div>
                Template selector matches to best profile
            </div>
            <div style="flex:1;padding:8px;background:var(--bg2);border-radius:6px;text-align:center">
                <div style="font-family:var(--font-head);font-size:16px;font-weight:700;color:var(--accent);margin-bottom:4px">3</div>
                Template drives: models, thresholds, features required
            </div>
        </div>
    </div>`;

    document.getElementById('t11-content').innerHTML = h;
}
