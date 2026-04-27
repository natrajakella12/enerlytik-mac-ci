// T3 Predictions — range forecast, RUL, SHAP drivers, survival (NMC)

const FEATURE_LABELS = {
    'cell_spread_slope':'Cell spread trend','cell_spread_max':'Cell spread peak',
    'km_per_soc_slope':'Efficiency trend','km_per_soc_pct':'Efficiency',
    'temp_max':'Peak temperature','r0_weekly_median':'Internal resistance',
    'soc_min_observed':'Minimum charge level','soc_min':'Minimum SoC',
    'cumulative_deep_discharge_count':'Deep discharge count','dod_mean':'Depth of discharge',
    'mileage_slope':'Mileage trend','load_normalized_efficiency_residual':'Load-adj efficiency',
    'weeks_since_commission':'Battery age','cycle_life_remaining':'Cycle life remaining',
    'load_x_spread':'Load×spread interaction','reading_count':'Data density',
};

async function loadT3() {
    if (!STATE.batteryId) { setTabContent(emptyState('Select a battery','Choose from dropdown to view predictions')); return; }
    setTabContent(loadingSkeleton(6));
    const id = STATE.batteryId;
    const chem = STATE.chemistry;
    const [pred, surv, intel] = await Promise.all([
        API.batteryPredictions(id),
        chem === 'NMC' ? API.batterySurvival(id) : null,
        API.batteryIntelligence(id),
    ]);

    let html = '';
    const fc = pred && pred.forecast;
    const rul = pred && pred.rul;
    const shap = pred && pred.shap;
    const adv = pred && pred.advanced;

    // Section A — Range Prediction
    html += '<div class="card">';
    html += '<div class="card-title">Range Prediction — 12 Week Horizon</div>';
    if (fc && fc.range_12w_p50) {
        const p50_km = (fc.range_12w_p50 * 80).toFixed(0);
        // Try to get P10/P90 from advanced predictions
        let p10_km = null, p90_km = null;
        if (adv && adv.length > 0) {
            const w12 = adv.filter(r => r.horizon_weeks === 12);
            const p10r = w12.find(r => r.quantile === 'p10');
            const p90r = w12.find(r => r.quantile === 'p90');
            if (p10r) p10_km = (p10r.predicted_range * 80).toFixed(0);
            if (p90r) p90_km = (p90r.predicted_range * 80).toFixed(0);
        }
        if (!p10_km) p10_km = Math.round(p50_km * 0.82);
        if (!p90_km) p90_km = Math.round(p50_km * 1.18);

        html += `<div style="position:relative;height:50px;background:#e8e7e3;border-radius:6px;margin:12px 0;overflow:hidden">`;
        const lo = Math.max(0, p10_km - 10), hi = parseInt(p90_km) + 10;
        const range = hi - lo || 1;
        const p10pct = ((p10_km - lo) / range * 100).toFixed(1);
        const p50pct = ((p50_km - lo) / range * 100).toFixed(1);
        const p90pct = ((p90_km - lo) / range * 100).toFixed(1);
        html += `<div style="position:absolute;left:${p10pct}%;width:${p50pct-p10pct}%;height:100%;background:rgba(245,158,11,0.25)"></div>`;
        html += `<div style="position:absolute;left:${p50pct}%;width:${p90pct-p50pct}%;height:100%;background:rgba(34,197,94,0.2)"></div>`;
        html += `<div style="position:absolute;left:${p50pct}%;top:0;bottom:0;width:2px;background:#fff;z-index:2"></div>`;
        html += `<div style="position:absolute;left:${p10pct}%;bottom:4px;font-size:10px;font-family:var(--font-mono);color:#f59e0b">P10: ${p10_km}km</div>`;
        html += `<div style="position:absolute;left:${p50pct}%;top:4px;font-size:11px;font-family:var(--font-mono);color:#fff;transform:translateX(-50%);font-weight:600">P50: ${p50_km}km</div>`;
        html += `<div style="position:absolute;right:${100-p90pct}%;bottom:4px;font-size:10px;font-family:var(--font-mono);color:#22c55e;text-align:right">P90: ${p90_km}km</div>`;
        html += '</div>';
        html += `<div style="font-family:var(--font-mono);font-size:10px;color:var(--text3)">Model: ${fc.model_version||'—'} · MAPE: ${fc.model_used||'—'} · Scored: ${fc.forecast_date||'—'}</div>`;
        if (chem === 'NMC') html += '<div style="font-size:11px;color:#f59e0b;margin-top:6px">NMC range model WIP — indicative only</div>';
    } else {
        html += emptyState('No range prediction available','Battery may lack sufficient history for forecasting');
    }
    html += '</div>';

    // Section B — RUL
    html += '<div class="g2">';
    if (rul) {
        const weeks = rul.rul_weeks != null ? Math.round(rul.rul_weeks) : null;
        const method = rul.rul_method || '—';
        const conf = rul.confidence_tier || '—';
        html += '<div class="card" style="text-align:center">';
        html += '<div class="card-title">Remaining Useful Life</div>';
        html += `<div style="font-family:var(--font-head);font-size:36px;font-weight:700;color:${weeks && weeks < 20 ? '#ef4444' : weeks && weeks < 52 ? '#f59e0b' : '#22c55e'}">${weeks != null ? weeks + 'w' : '—'}</div>`;
        html += `<div style="font-size:11px;color:var(--text3);margin-top:4px">${method} · ${conf}</div>`;
        html += '</div>';
        html += '<div class="card">';
        html += '<div class="card-title">RUL Assessment</div>';
        if (weeks != null && weeks < 20) {
            html += `<div style="font-size:13px;color:#ef4444;line-height:1.7">At current degradation rate, this battery will need replacement in approximately <strong>${weeks} weeks</strong>.</div>`;
        } else if (weeks != null && weeks > 100) {
            html += '<div style="font-size:13px;color:#22c55e;line-height:1.7">Battery trajectory is stable — no replacement horizon visible within planning window.</div>';
        } else if (weeks != null) {
            html += `<div style="font-size:13px;color:var(--text2);line-height:1.7">Estimated ${weeks} weeks to replacement threshold at current degradation rate.</div>`;
        } else {
            html += '<div style="font-size:13px;color:var(--text3)">Insufficient trajectory data for RUL estimation.</div>';
        }
        html += '</div>';
    } else {
        html += '<div class="card" style="grid-column:span 2">' + emptyState('No RUL estimate available') + '</div>';
    }
    html += '</div>';

    // Section C — SHAP Prediction Drivers
    html += '<div class="card"><div class="card-title">Prediction Drivers</div>';
    if (shap && shap.features && shap.features.length > 0) {
        html += '<div class="g3">';
        shap.features.slice(0, 3).forEach(f => {
            const label = FEATURE_LABELS[f.feature] || f.feature;
            const dir = f.direction === 'negative' ? 'INCREASING RISK ↑' : 'DECREASING RISK ↓';
            const dirCol = f.direction === 'negative' ? '#ef4444' : '#22c55e';
            const impact = f.plain_text || `Impact: ${f.impact_km ? f.impact_km.toFixed(0) + 'km' : '—'}`;
            html += `<div class="card" style="background:#f5f5f3">
                <div style="font-size:12px;font-weight:500;margin-bottom:4px">${label}</div>
                <div style="font-size:10px;color:${dirCol};font-family:var(--font-mono)">${dir}</div>
                <div style="font-size:11px;color:var(--text3);margin-top:4px">${impact}</div>
                <div style="font-size:10px;color:var(--text3);margin-top:2px">Value: ${fmt(f.actual_value)}</div>
            </div>`;
        });
        html += '</div>';
    } else {
        html += '<div style="color:var(--text3);font-size:12px;padding:8px 0">No SHAP attribution available for this battery.</div>';
    }
    html += '</div>';

    // Section E — Survival Forecast (NMC only)
    if (chem === 'NMC' && surv) {
        html += '<div class="card" style="border-left:3px solid #f59e0b">';
        html += '<div class="card-title">Service Forecast (NMC Survival Model)</div>';
        if (surv.forecast) {
            const sf = surv.forecast;
            const probs = [
                { label:'P(complaint 30d)', val:sf.p_complaint_30d },
                { label:'P(complaint 60d)', val:sf.p_complaint_60d },
                { label:'P(complaint 90d)', val:sf.p_complaint_90d },
            ];
            html += '<div style="display:grid;grid-template-columns:1fr auto;gap:16px;align-items:start">';
            html += '<div>';
            probs.forEach(p => {
                const pct = (p.val * 100).toFixed(0);
                const col = pct > 70 ? '#ef4444' : pct > 40 ? '#f59e0b' : '#22c55e';
                html += `<div style="margin-bottom:8px">
                    <div style="font-size:11px;color:var(--text3);margin-bottom:3px">${p.label}</div>
                    <div style="height:20px;background:#e8e7e3;border-radius:4px;position:relative;overflow:hidden">
                        <div style="width:${pct}%;height:100%;background:${col};border-radius:4px"></div>
                        <span style="position:absolute;right:6px;top:2px;font-size:11px;font-family:var(--font-mono);font-weight:600;color:var(--text1)">${pct}%</span>
                    </div>
                </div>`;
            });
            html += '</div>';
            html += `<div style="text-align:center;padding:10px 20px">
                <div style="font-family:var(--font-head);font-size:36px;font-weight:700;color:var(--accent)">${sf.median_days_to_complaint ? Math.round(sf.median_days_to_complaint) : '—'}</div>
                <div style="font-size:11px;color:var(--text3)">median days to complaint</div>
            </div>`;
            html += '</div>';

            // Silent degrader warning
            if (surv.silent_degrader) {
                html += `<div style="background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.3);border-radius:6px;padding:10px 14px;margin-top:10px;font-family:var(--font-mono);font-size:11px;color:#f59e0b">
                    Silent degrader detected — ${surv.events_count} telemetry events, ${surv.complaints_count} complaints. Track E SOH takes precedence over service forecast.
                </div>`;
            }

            html += `<div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);margin-top:8px">Weibull AFT · C-index: 0.600 · 27 batteries · ${sf.model_version||'nmc_survival_v2.1.0'}</div>`;
        } else {
            html += emptyState('No survival forecast available');
        }
        html += '</div>';
    }

    setTabContent(html);
}
