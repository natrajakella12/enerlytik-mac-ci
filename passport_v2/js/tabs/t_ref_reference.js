// T-REF Reference — Glossary, Models & Techniques, Formulas

const GLOSSARY = [
    ['Battery_id','Internal enerlytik identifier mapped from customer serial number. Uses battery_id (not vehicle_id — Rule 1).','All'],
    ['BMS','Battery Management System. Onboard controller reporting SoC/SoH. SoH unreliable for LFP (Rule 2).','Health'],
    ['C-index','Concordance index. Survival model accuracy. 0.5=random, 1.0=perfect. Ours: 0.600.','Predictions'],
    ['Cell spread','Voltage difference between highest and lowest cell in pack (mV). Rising spread = imbalance.','Health'],
    ['Chemistry','Electrode chemistry: LFP (iron phosphate) or NMC (nickel manganese cobalt). Always separate (Rule 9).','All'],
    ['Composite score','0.50×L1 + 0.30×L2 + 0.20×L3. Range 0-100. Higher=healthier.','Health'],
    ['CUSUM','Cumulative Sum. Detects permanent signal shifts from baseline. Applied to raw signals only (Rule 8).','Events'],
    ['data_confidence','Quality flag: HIGH/MEDIUM/LOW/INSUFFICIENT. Every output must carry this (Rule 24).','Platform'],
    ['DEKF','Dual Extended Kalman Filter. Real-time SoC and R0 from 20-sec telemetry. Currently SHADOW.','DEKF'],
    ['Deep discharge','SoC below 10%. Stresses chemistry. Cumulative count tracked.','Health'],
    ['DoD','Depth of Discharge. How deeply battery drains per cycle (%).','Health'],
    ['ECM','Equivalent Circuit Model. R0+RC physics model of battery electrical behaviour.','DEKF'],
    ['Efficiency','km per SoC%. Formula: SUM(km)/SUM(SoC%) — never mean of ratios (Rule 3).','Health'],
    ['E1-E7','Event types: efficiency change, cell imbalance, thermal stress, deep discharge, charging anomaly, usage shift, voltage sag.','Events'],
    ['Gate A','NMC SOH threshold. SOH<80% → IMMEDIATE action.','Investigation'],
    ['INSUFFICIENT_DATA','Scoring gate flag. Battery below minimum weeks/features. Not scored (Rule 25).','Platform'],
    ['Investigation verdict','Signal Triangulation output: PHYSICS_CONFIRMED/PROGRESSIVE/WEAK_SIGNAL/UNCONFIRMED.','Investigation'],
    ['L1 Self-baseline','50% of composite. Compares battery to own Week 1-8 performance.','Health'],
    ['L2 Physics','30% of composite. Absolute physics limits (spread, temp, efficiency).','Health'],
    ['L3 Fleet context','20% of composite. Battery vs cluster peers.','Health'],
    ['LFP','Lithium Iron Phosphate. E-rickshaw. 16S, 105Ah. BMS SoH unreliable.','All'],
    ['MAPE','Mean Absolute Percentage Error. LFP range model: 14.07%.','Predictions'],
    ['NMC','Nickel Manganese Cobalt. 2-wheeler swap. 14S, 40Ah.','All'],
    ['P10/P50/P90','Prediction quantiles. P50=median. P10-P90=confidence band.','Predictions'],
    ['PELT','Changepoint detection. Raw signals only (Rule 8).','Events'],
    ['Pulse ECM','R0 from current pulses in 20-sec data. Primary NMC R0 source (Rule 19).','DEKF'],
    ['R0','Internal resistance (mΩ). NMC: per-cell ÷14. LFP: thermal only, not degradation (Rule 14).','Health'],
    ['Regression anchor','Known-good battery used to validate pipeline after changes.','Platform'],
    ['RUL','Remaining Useful Life. Weeks until replacement threshold.','Predictions'],
    ['Rule 14','LFP R0 is thermal proxy, not degradation signal.','Health'],
    ['Rule 28','Investigation: ≥1 signal >1.5σ required. Event count alone is not a signal.','Investigation'],
    ['Self-baseline','Week 1-8 average. Reference for L1 scoring.','Health'],
    ['SHAP','Feature importance showing each feature contribution to prediction.','Explainability'],
    ['Silent degrader','High telemetry events but low complaints. Survival model underestimates.','Service'],
    ['SOH','State of Health. BMS SOH unreliable for LFP (Rule 2). Use ECM for NMC.','Health'],
    ['Survival model','Weibull AFT. Time-to-complaint. NMC only. C-index 0.600.','Service'],
    ['Tier','PRIME(80+)/STABLE(60-79)/WATCH(40-59)/STRESSED(20-39)/CRITICAL(0-19).','Health'],
    ['Track E','Electrochemical track. Drives health decisions.','Health'],
    ['Track O','Operational/service track. Service scheduling only. Never overrides Track E.','Service'],
    ['VWF','vehicle_weekly_features. Core feature table. Uses battery_id (Rule 1).','Platform'],
    ['Weibull AFT','Accelerated Failure Time. Survival model when Cox PH assumptions violated.','Service'],
];

const TECHNIQUES = [
    {name:'CUSUM',cat:'Detection',status:'PRODUCTION',what:'Detects permanent signal shifts from baseline.',how:'E1 efficiency regime change. Dual CUSUM: fast (2W) + slow (8W).',why:'Sequential data. Detects sustained shifts, not noise.',limit:'8+ weeks baseline needed.',rule:'Rule 8: raw signals only'},
    {name:'PELT',cat:'Detection',status:'PRODUCTION',what:'Changepoint detection in time series.',how:'Cell spread changepoints for E2 events.',why:'Exact linear time. Handles multiple changepoints.',limit:'Sensitive to noise on residuals.',rule:'Rule 8: raw signals only'},
    {name:'Bollinger Bands',cat:'Detection',status:'PRODUCTION',what:'Volatility-based anomaly bands.',how:'Mileage anomaly detection.',why:'Adapts to each battery individual volatility.',limit:'Needs 6+ weeks for stable bands.',rule:null},
    {name:'Isolation Forest',cat:'Detection',status:'PRODUCTION',what:'Unsupervised anomaly detection.',how:'Per-cluster outlier scoring.',why:'No distributional assumptions.',limit:'Skip if cluster <5 batteries.',rule:null},
    {name:'XGBoost',cat:'Prediction',status:'PRODUCTION',what:'Gradient boosted decision trees.',how:'LFP range P50 prediction (14.07% MAPE).',why:'Handles feature interactions, robust to missing data.',limit:'Requires feature engineering.',rule:null},
    {name:'LightGBM',cat:'Prediction',status:'PRODUCTION',what:'Gradient boosting with histogram binning.',how:'T1b fallback range model (16.11% MAPE).',why:'Faster training than XGBoost.',limit:null,rule:null},
    {name:'Ridge Regression',cat:'Prediction',status:'PRODUCTION',what:'L2-regularised linear regression.',how:'T5 per-battery personal models (42 batteries, ~9.3% MAPE).',why:'Small sample per battery — regularisation prevents overfit.',limit:'Linear assumptions.',rule:null},
    {name:'DEKF',cat:'State Estimation',status:'SHADOW',what:'Dual Extended Kalman Filter for SoC + R0.',how:'Online SoC correction and R0 tracking from 20-sec data.',why:'Real-time, accounts for non-linear OCV curve.',limit:'Flat LFP OCV curve challenges convergence.',rule:null},
    {name:'Pulse ECM',cat:'State Estimation',status:'PRODUCTION',what:'R0 from current pulse events in raw telemetry.',how:'NMC R0 ground truth. |ΔI|>3A events identified.',why:'Direct measurement, no model assumptions.',limit:'Needs raw 20-sec data.',rule:'Rule 19'},
    {name:'Weibull AFT',cat:'Survival',status:'PRODUCTION',what:'Time-to-event model for complaint forecasting.',how:'NMC survival v2.1.0. P30/P60/P90 complaint probability.',why:'Cox PH failed (100% event rate). AFT handles this.',limit:'C-index 0.600. Underestimates silent degraders.',rule:null},
    {name:'K-Means',cat:'Clustering',status:'PRODUCTION',what:'Partition batteries into usage clusters.',how:'6 features: mileage, spread, speed, DoD.',why:'Simple, interpretable clusters.',limit:'K must be chosen (silhouette optimised).',rule:null},
    {name:'SHAP',cat:'Explainability',status:'PRODUCTION',what:'Feature importance via Shapley values.',how:'Top 3 features per battery prediction.',why:'Consistent, theoretically grounded.',limit:'Slow for large feature sets.',rule:null},
    {name:'TimeSeriesSplit',cat:'Validation',status:'PRODUCTION',what:'Temporal cross-validation with gap.',how:'5-fold with gap=8 or gap=12 weeks.',why:'Prevents look-ahead leakage.',limit:'Reduces usable training data.',rule:null},
];

const FORMULAS = [
    {name:'Efficiency',formula:'SUM(km) / SUM(SoC%)',unit:'km per SoC%',rule:'Rule 3',note:'Never mean of ratios (Jensen inequality)'},
    {name:'Range Estimate',formula:'km_per_soc_pct × 80',unit:'km',rule:'Rule 4',note:'80% DoD, 105Ah LFP'},
    {name:'R0 (NMC)',formula:'ΔV_pack / ΔI / 14',unit:'mΩ per cell',rule:'Rule 19',note:'Pulse ECM only. 14S correction.'},
    {name:'R0 (LFP)',formula:'V_drop / I_pulse',unit:'mΩ (pack)',rule:'Rule 14',note:'Thermal proxy only — NOT degradation'},
    {name:'Composite Score',formula:'0.50×L1 + 0.30×L2 + 0.20×L3',unit:'0-100',rule:null,note:'L1=self-baseline, L2=physics, L3=fleet'},
    {name:'Investigation Threshold',formula:'|signal_value - baseline_mean| / baseline_std > 1.5',unit:'σ',rule:'Rule 28',note:'≥1 signal >1.5σ required for verdict'},
    {name:'Scoring Gate',formula:'non_null_features / total_features ≥ 0.80',unit:'%',rule:'Rule 25',note:'Below 80% → PENDING_SCORING queue'},
    {name:'Survival P(T≤t)',formula:'1 - S(t) where S = Weibull survival function',unit:'probability',rule:null,note:'C-index 0.600, 27 NMC batteries'},
];

async function loadTRef() {
    let html = '';
    // Search bar
    html += `<div style="margin-bottom:14px"><input id="ref-search" type="text" placeholder="Search glossary, models, formulas..." oninput="filterRef()" style="width:100%;padding:8px 14px;font-family:var(--font-mono);font-size:12px;border:1px solid var(--bg3);border-radius:var(--radius);background:var(--bg1);color:var(--text1);outline:none"></div>`;
    // Sub-tabs
    html += `<div style="display:flex;gap:8px;margin-bottom:14px">
        <button onclick="showRefTab('glossary')" id="ref-btn-g" class="ref-btn active" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--accent);background:var(--accent);color:#000;cursor:pointer;font-weight:600">Glossary</button>
        <button onclick="showRefTab('models')" id="ref-btn-m" class="ref-btn" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--bg3);background:var(--bg1);color:var(--text2);cursor:pointer">Models & Techniques</button>
        <button onclick="showRefTab('formulas')" id="ref-btn-f" class="ref-btn" style="padding:6px 16px;border-radius:6px;font-family:var(--font-mono);font-size:11px;border:1px solid var(--bg3);background:var(--bg1);color:var(--text2);cursor:pointer">Formulas</button>
    </div>`;
    html += '<div id="ref-content"></div>';
    setTabContent(html);
    showRefTab('glossary');
}

function showRefTab(tab) {
    ['g','m','f'].forEach(k => {
        const btn = document.getElementById('ref-btn-'+k);
        if (btn) { btn.style.background = 'var(--bg1)'; btn.style.color = 'var(--text2)'; btn.style.borderColor = 'var(--bg3)'; }
    });
    const activeBtn = document.getElementById('ref-btn-'+tab[0]);
    if (activeBtn) { activeBtn.style.background = 'var(--accent)'; activeBtn.style.color = '#000'; activeBtn.style.borderColor = 'var(--accent)'; }
    window._refTab = tab;
    filterRef();
}

function filterRef() {
    const q = (document.getElementById('ref-search')?.value || '').toLowerCase();
    const tab = window._refTab || 'glossary';
    const el = document.getElementById('ref-content');
    if (!el) return;
    let h = '';

    if (tab === 'glossary') {
        const filtered = GLOSSARY.filter(([term, def]) => !q || term.toLowerCase().includes(q) || def.toLowerCase().includes(q));
        filtered.forEach(([term, def, tabs]) => {
            h += `<div style="padding:6px 0;border-bottom:1px solid var(--bg3)" data-search="${term.toLowerCase()} ${def.toLowerCase()}">
                <span style="font-family:var(--font-mono);font-size:12px;font-weight:600">${term}</span>
                <span style="font-size:9px;padding:1px 6px;border-radius:3px;background:var(--bg2);color:var(--text3);margin-left:6px">${tabs}</span>
                <div style="font-size:11px;color:var(--text2);margin-top:2px;line-height:1.6">${def}</div>
            </div>`;
        });
        if (!filtered.length) h += emptyState('No matching terms');
    } else if (tab === 'models') {
        const filtered = TECHNIQUES.filter(t => !q || t.name.toLowerCase().includes(q) || t.cat.toLowerCase().includes(q) || (t.what||'').toLowerCase().includes(q));
        const cats = [...new Set(filtered.map(t => t.cat))];
        cats.forEach(cat => {
            h += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-top:12px;margin-bottom:6px">${cat}</div>`;
            h += '<div class="g2">';
            filtered.filter(t => t.cat === cat).forEach(t => {
                const badge = STATUS_BADGE ? (STATUS_BADGE[t.status] || t.status) : t.status;
                h += `<div class="card"><div style="display:flex;justify-content:space-between;margin-bottom:6px"><span style="font-family:var(--font-mono);font-size:12px;font-weight:600">${t.name}</span>${badge}</div>
                    <div style="font-size:11px;color:var(--text2);line-height:1.7">
                        <div><strong>What:</strong> ${t.what}</div>
                        <div><strong>How:</strong> ${t.how}</div>
                        <div><strong>Why:</strong> ${t.why}</div>
                        ${t.limit ? '<div style="color:var(--text3)"><strong>Limit:</strong> '+t.limit+'</div>' : ''}
                        ${t.rule ? '<div style="color:var(--accent);font-family:var(--font-mono);font-size:10px">'+t.rule+'</div>' : ''}
                    </div>
                </div>`;
            });
            h += '</div>';
        });
    } else {
        const filtered = FORMULAS.filter(f => !q || f.name.toLowerCase().includes(q) || f.formula.toLowerCase().includes(q));
        filtered.forEach(f => {
            h += `<div class="card" style="margin-bottom:8px">
                <div style="font-family:var(--font-mono);font-size:12px;font-weight:600;margin-bottom:4px">${f.name}</div>
                <div style="font-family:var(--font-mono);font-size:14px;color:var(--text1);background:var(--bg2);padding:8px 12px;border-radius:4px;margin-bottom:6px">${f.formula}</div>
                <div style="font-size:11px;color:var(--text2)">Unit: ${f.unit} ${f.rule ? '· <span style="color:var(--accent)">'+f.rule+'</span>' : ''}</div>
                ${f.note ? '<div style="font-size:10px;color:var(--text3);margin-top:2px">'+f.note+'</div>' : ''}
            </div>`;
        });
    }
    el.innerHTML = h;
}
