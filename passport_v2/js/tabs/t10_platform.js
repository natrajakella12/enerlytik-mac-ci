// T10 Platform — model catalogue, scoring queue, rules reference, system health

const STATUS_BADGE = {
    PRODUCTION:`<span style="padding:2px 8px;border-radius:12px;font-size:9px;font-family:var(--font-mono);background:#dcfce7;color:#166534;font-weight:600">PRODUCTION</span>`,
    SHADOW:`<span style="padding:2px 8px;border-radius:12px;font-size:9px;font-family:var(--font-mono);background:#dbeafe;color:#1e40af;font-weight:600">SHADOW</span>`,
    QUARANTINED:`<span style="padding:2px 8px;border-radius:12px;font-size:9px;font-family:var(--font-mono);background:#fee2e2;color:#991b1b;font-weight:600">⊘ QUARANTINED</span>`,
    DEPRECATED:`<span style="padding:2px 8px;border-radius:12px;font-size:9px;font-family:var(--font-mono);background:#f3f4f6;color:#888;text-decoration:line-through">DEPRECATED</span>`,
};

const RULES = [
    {group:'Data Rules (1–8)',rules:[
        '1. battery_id not vehicle_id on all VWF queries','2. Never use BMS SoH for LFP','3. Efficiency = SUM(km)/SUM(SoC%) — never mean of ratios','4. power_w is a BMS register — recompute as V×I','5. Temp-correct all resistance at 0.6%/°C ref 25°C','6. get_connection() SQLite, get_duckdb_connection() DuckDB','7. Every model: .pkl + .json + MODEL_CHANGELOG','8. PELT on raw signals only — never residuals'
    ]},
    {group:'Pipeline Rules (9–16)',rules:[
        '9. LFP and NMC always separate models','10. DuckDB for 30-sec only — never SQLite for high-freq','11. validate_feature_set() before every training','12. soh_cap_weekly BANNED from range models (leakage)','13. load_status: use LOAD_STATUS constants from config','14. R0 for LFP = thermal only, NOT degradation','15. Dual intelligence views: never merge event-driven with absolute-state','16. Relative target: predict % loss from own baseline'
    ]},
    {group:'Architecture Rules (17–21)',rules:[
        '17. NMC thresholds NMC-only, LFP thresholds LFP-only','18. Ensemble diversity: different failure modes, not hyperparams','19. NMC R0: pulse ECM from raw telemetry only — never proxy','20. Dual-track scoring for complaint-linked fleets','21. SWAP under-reporting factor (SUSPENDED)'
    ]},
    {group:'Quality Gates (22–29)',rules:[
        '22. Every ingest writes to ingest_audit_log','23. No downsampling in production ingest','24. Every output carries data_confidence flag','25. Scoring gate: 80% feature completeness minimum','26. Regression tests must pass before commit','27. Unit normalisation: mV not V for cell spread','28. Investigation verdicts require ≥1 signal >1.5σ','29. Survival model forecasts are service scheduling only'
    ]},
    {group:'Digital Twin Rules (30–34)',rules:[
        '30. DT outputs labelled simulated=True — never as measurements','31. DT-1 validates RMSE < 50mV before production','32. DT projections carry P10/P50/P90 bands — never point estimates','33. LFP and NMC run separate DT models','34. DT-3 needs real ambient temp — never BMS cell temp as proxy'
    ]},
];

async function loadT10() {
    setTabContent(loadingSkeleton(8));
    const [summary, catalogue, changelog, queue, system] = await Promise.all([
        API.apiFetch('/api/platform/summary'),
        API.apiFetch('/api/platform/catalogue'),
        API.apiFetch('/api/platform/changelog'),
        API.apiFetch('/api/platform/queue'),
        API.apiFetch('/api/platform/system'),
    ]);

    let html = '';

    // Section A — Fleet KPIs
    const s = summary || {};
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">';
    [{l:'Total Batteries',v:s.total_batteries},{l:'LFP Active',v:s.lfp_active},{l:'NMC Active',v:s.nmc_active},{l:'Models (Prod)',v:s.models_production},{l:'Rules',v:s.rules_count},{l:'Data Quality',v:s.data_quality_pct?s.data_quality_pct+'%':'—'}].forEach(k => {
        html += `<div class="card" style="flex:1;min-width:120px;text-align:center;padding:12px">
            <div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);text-transform:uppercase">${k.l}</div>
            <div style="font-family:var(--font-head);font-size:22px;font-weight:700;margin-top:4px">${k.v??'—'}</div>
        </div>`;
    });
    html += '</div>';

    // Section B — Model Catalogue (static + API overlay)
    const STATIC_CATALOGUE = [
        // LFP Range — Production Ensemble + Components
        {name:'range_t2t1_ensemble',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'14.07% MAPE',scored:'186 bats'},
        {name:'range_t1b_lgbm (fallback)',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'16.11% MAPE',scored:'176 bats'},
        {name:'range_t5_perbat (Ridge)',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'9.3% mean',scored:'104 bats'},
        {name:'conformal_calibration',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'q=0.302',scored:'79.9% cov'},
        {name:'range_x1_fleetprior',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'18.63% MAPE',scored:'Onboarding'},
        {name:'range_x2_templateprior',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'18.67% MAPE',scored:'Onboarding'},
        {name:'range_x3_clusterprior',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'19.81% MAPE',scored:'Onboarding'},
        // LFP Diagnostics
        {name:'rul_xgb',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'14.25% SOH',scored:'186 bats'},
        {name:'fault_probability_rf',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'3h x 2var',scored:'156 bats'},
        {name:'isolation_forest_priority',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'Anomaly',scored:'156 bats'},
        {name:'pca_domain (5 components)',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'5 PCA',scored:'156 bats'},
        {name:'pathway_predictor_rf',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'RF',scored:'156 bats'},
        // ECM + DEKF
        {name:'dekf_lfp_r0',version:'v2.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'100% ECM',scored:'179 bats'},
        {name:'ecm_lfp_batch',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'148 fitted',scored:'3,517 rows'},
        {name:'vb_ecm_params_30sec',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'Thevenin',scored:'DuckDB'},
        {name:'vb_degradation_index',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'VB physics',scored:'6,715 rows'},
        {name:'vb_soc_soh',version:'v1.0',chemistry:'LFP',status:'PRODUCTION',metric:'Coulomb+OCV',scored:'7,273 rows'},
        // Health Scoring (Locked)
        {name:'lfp_health_score',version:'v1.0.0',chemistry:'LFP',status:'PRODUCTION',metric:'0 FN/33',scored:'Locked'},
        {name:'nmc_health_score',version:'v1.0.0',chemistry:'NMC',status:'PRODUCTION',metric:'Dual-track',scored:'Locked'},
        // NMC Production
        {name:'nmc_complaint',version:'v3.1.0',chemistry:'NMC',status:'PRODUCTION',metric:'AUC 0.692',scored:'27 bats'},
        {name:'nmc_survival',version:'v2.1.0',chemistry:'NMC',status:'PRODUCTION',metric:'C=0.600',scored:'27 bats'},
        {name:'dekf_nmc_r0',version:'v2.0.0',chemistry:'NMC',status:'PRODUCTION',metric:'7.3% MAPE',scored:'27 bats'},
        {name:'ecm_nmc_pulse',version:'v2.0.0',chemistry:'NMC',status:'PRODUCTION',metric:'50,631 pulses',scored:'27 bats'},
        {name:'ecm_nmc_ocv',version:'v1.0.0',chemistry:'NMC',status:'PRODUCTION',metric:'R²>0.99: 1',scored:'27 bats'},
        {name:'nmc_dt_electrical',version:'v1.0',chemistry:'NMC',status:'PRODUCTION',metric:'ECM DT',scored:'833 rows'},
        {name:'dmegc_lifecycle',version:'v1.0.0',chemistry:'NMC',status:'PRODUCTION',metric:'DMEGC',scored:'Physics'},
        // Events
        {name:'E1-E7 Events',version:'v1.0.0',chemistry:'BOTH',status:'PRODUCTION',metric:'852 events',scored:'Algorithm'},
        {name:'event_severity_calibrator',version:'v1.0',chemistry:'BOTH',status:'PRODUCTION',metric:'Calibrated',scored:'All events'},
        // Shadow
        {name:'dekf_nmc_soc',version:'v2.0.0',chemistry:'NMC',status:'SHADOW',metric:'10.1% corr',scored:'Shadow'},
        {name:'dekf_lfp_soc',version:'v2.0.0',chemistry:'LFP',status:'SHADOW',metric:'25.1% corr',scored:'Shadow'},
        {name:'lfp_dekf_shadow_v2',version:'v2.0',chemistry:'LFP',status:'SHADOW',metric:'91 bats',scored:'DuckDB'},
        // Quarantined
        {name:'nmc_fault_clf',version:'v4.0.0',chemistry:'NMC',status:'QUARANTINED',metric:'AUC 0.848',scored:'Label-only',reason:'Trained on OEM labels, no telemetry features'},
        {name:'nmc_severity',version:'v1.0.0',chemistry:'NMC',status:'QUARANTINED',metric:'Recall 0.594',scored:'Label-only',reason:'CRITICAL recall below 0.85 gate'},
        {name:'nmc_survival_v2',version:'v2.0.0',chemistry:'NMC',status:'QUARANTINED',metric:'C=0.947',scored:'Label-only',reason:'Overfit on synthetic timing'},
        {name:'nmc_burn_warning',version:'v1.0.0',chemistry:'NMC',status:'QUARANTINED',metric:'Recall 0.980',scored:'Label-only',reason:'No fleet burns to validate'},
    ];
    const models = (catalogue && catalogue.length > 0) ? catalogue : STATIC_CATALOGUE;
    html += '<div class="card">';
    html += '<div class="card-title">Model Catalogue — 98 Named Models · 489 Artifacts</div>';
    html += '<div style="display:flex;gap:12px;margin-bottom:10px;flex-wrap:wrap">';
    html += '<span style="font-family:var(--font-mono);font-size:10px;padding:3px 10px;border-radius:12px;background:#dcfce7;color:#166534">PRODUCTION: 52</span>';
    html += '<span style="font-family:var(--font-mono);font-size:10px;padding:3px 10px;border-radius:12px;background:#dbeafe;color:#1e40af">SHADOW: 3</span>';
    html += '<span style="font-family:var(--font-mono);font-size:10px;padding:3px 10px;border-radius:12px;background:#fee2e2;color:#991b1b">QUARANTINED: 4</span>';
    html += '<span style="font-family:var(--font-mono);font-size:10px;padding:3px 10px;border-radius:12px;background:#f3f4f6;color:#888">DEPRECATED: 39</span>';
    html += '</div>';
    html += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
    html += '<tr style="font-family:var(--font-mono);font-size:9px;color:var(--text3)"><th style="text-align:left;padding:4px 8px">MODEL</th><th>VERSION</th><th>CHEM</th><th>STATUS</th><th>METRIC</th><th>SCORED</th></tr>';
    (models).forEach(m => {
        const badge = STATUS_BADGE[m.status] || m.status;
        const reason = m.reason ? `<div style="font-size:10px;color:#991b1b;padding:4px 8px;background:#fef2f2;border-radius:4px;margin-top:4px">${m.reason}</div>` : '';
        html += `<tr style="border-top:1px solid var(--bg3)">
            <td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">${m.name}</td>
            <td style="font-family:var(--font-mono);font-size:10px;text-align:center">${m.version}</td>
            <td style="text-align:center;font-size:10px">${m.chemistry}</td>
            <td style="text-align:center">${badge}</td>
            <td style="font-family:var(--font-mono);font-size:10px;text-align:center">${m.metric||'—'}</td>
            <td style="font-family:var(--font-mono);font-size:10px;text-align:center">${m.scored||'—'}</td>
        </tr>`;
        if (reason) html += `<tr><td colspan="6" style="padding:2px 8px">${reason}</td></tr>`;
    });
    html += '</table>';
    html += '<details style="margin-top:12px"><summary style="cursor:pointer;font-family:var(--font-mono);font-size:10px;color:var(--text3);padding:4px 0">Show 39 deprecated models (archived iterations)</summary>';
    html += '<div style="font-size:10px;color:var(--text3);padding:8px 0;line-height:2">';
    const deprecated = ['LFP_all_v2.0.0','health_nmc_v1.0.0','nmc_complaint_v1.0.0','nmc_complaint_v2.0.0','nmc_complaint_v3.0.0',
        'nmc_fault_clf_v1.0','xgboost_nmc_health_v2.0','xgboost_nmc_fault_classifier_v1.0','xgboost_nmc_voltage_delta_v1.0',
        'xgboost_p50_12w_v1.2','range_pred_v1.0','range_pred_gbm_v1.0','range_pred_st_v1.0','range_pred_catboost_v1.0',
        'range_pred_sklearn_gbm_v1.0','range_pred_xgboost_v1.0','range_pred_advanced_ensemble_v1.0',
        'dekf_nmc_v1.0','dekf_lfp_v1.0',
        'TPL: range_v2.0.0, v2.1.0, v2.2.0, hybrid_v1.0-v2.0, kps_v1.0, lgbm_v1.0-v1.1, xgb_v3.0-v3.2, ensemble_v1.0, usage_v1.0',
        'v3.0.0: xgboost, lightgbm, catboost, ngboost (experiment suite)',
        'v3.2: delta_range (4 horizons)'];
    deprecated.forEach(function(d) { html += '<span style="padding:2px 8px;border-radius:3px;background:#f3f4f6;margin:2px 4px;display:inline-block;text-decoration:line-through">' + d + '</span> '; });
    html += '</div></details></div>';

    // Section C — Changelog
    html += '<div class="card"><div class="card-title">Recent Changes</div>';
    (changelog||[]).forEach(e => {
        html += `<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--bg3);font-size:11px">
            <span style="font-family:var(--font-mono);color:var(--text3);min-width:80px">${e.date}</span>
            <span style="font-weight:500">${e.version}</span>
            <span style="color:var(--text2);flex:1">${e.summary}</span>
            <span style="font-family:var(--font-mono);font-size:9px;color:var(--text3)">${e.chemistry}</span>
        </div>`;
    });
    html += '</div>';

    // Section D — System Health + Queue
    html += '<div class="g2">';
    const sys = system || {};
    html += `<div class="card"><div class="card-title">System Health</div>
        <div style="font-size:12px;line-height:2">
            <div>SQLite: <strong>${sys.sqlite_size_mb||'?'} MB</strong></div>
            <div>DuckDB: <strong>${sys.duckdb_size_gb||'?'} GB</strong></div>
            <div>Last scored: <strong>${(sys.last_scored||'—').slice(0,16)}</strong></div>
            <div>Last ingest: <strong>${(sys.last_ingest||'—').slice(0,16)}</strong></div>
        </div>
    </div>`;
    html += '<div class="card"><div class="card-title">Scoring Queue</div>';
    if (queue && queue.length > 0) {
        queue.forEach(b => {
            const col = b.status === 'INSUFFICIENT_DATA' ? '#dc2626' : '#d97706';
            html += `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--bg3);font-size:11px">
                <span style="font-family:var(--font-mono)">${b.battery_id}</span>
                <span style="color:${col};font-size:10px">${b.status} · ${b.completeness_pct}%</span>
            </div>`;
        });
    } else {
        html += '<div style="color:var(--text3);font-size:12px">Queue empty — all batteries scored.</div>';
    }
    html += '</div></div>';

    // Section E — Rules Accordion
    html += '<div class="card"><div class="card-title">34 Non-Negotiable Rules</div>';
    RULES.forEach((g,gi) => {
        html += `<details style="margin-bottom:8px"><summary style="cursor:pointer;font-size:12px;font-weight:500;padding:6px 0;border-bottom:1px solid var(--bg3)">${g.group}</summary>`;
        html += '<div style="padding:8px 0">';
        g.rules.forEach(r => {
            html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text2);padding:3px 0;line-height:1.6">${r}</div>`;
        });
        html += '</div></details>';
    });
    html += '</div>';

    setTabContent(html);
}
