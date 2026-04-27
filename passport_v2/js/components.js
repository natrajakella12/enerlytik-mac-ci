// components.js — Shared UI components for passport v2

// Light-theme tier colors (bg + text for contrast on cream)
const TIER_COLORS = { PRIME:'#16a34a', STABLE:'#1d4ed8', WATCH:'#d97706', STRESSED:'#ea580c', CRITICAL:'#dc2626' };
const TIER_BG     = { PRIME:'#dcfce7', STABLE:'#dbeafe', WATCH:'#fef3c7', STRESSED:'#ffedd5', CRITICAL:'#fee2e2' };
const TIER_TEXT   = { PRIME:'#166534', STABLE:'#1e40af', WATCH:'#92400e', STRESSED:'#9a3412', CRITICAL:'#991b1b' };
const CONF_COLORS = { HIGH:'#16a34a', MEDIUM:'#d97706', LOW:'#ea580c', INSUFFICIENT_DATA:'#dc2626', RETROACTIVE_LOW_DATA:'#dc2626' };
const SEV_COLORS  = { SEV1:'#dc2626', CRITICAL:'#dc2626', SEV2:'#ea580c', WARNING:'#d97706', SEV3:'#1d4ed8', SEV4:'#888' };

// Verdict styles for T6 (light theme)
const VERDICT_BG = { PHYSICS_CONFIRMED:'#fee2e2', PROGRESSIVE:'#fef3c7', WEAK_SIGNAL:'#fefce8', UNCONFIRMED:'#f9fafb' };
const VERDICT_TEXT = { PHYSICS_CONFIRMED:'#991b1b', PROGRESSIVE:'#92400e', WEAK_SIGNAL:'#713f12', UNCONFIRMED:'#374151' };
const VERDICT_BORDER = { PHYSICS_CONFIRMED:'#ef4444', PROGRESSIVE:'#f59e0b', WEAK_SIGNAL:'#eab308', UNCONFIRMED:'#9ca3af' };

function tierBadge(tier) {
    const bg = TIER_BG[tier] || '#f3f4f6';
    const text = TIER_TEXT[tier] || '#555';
    return `<span class="tier-badge" style="background:${bg};color:${text}">${tier || '—'}</span>`;
}

function confidenceBadge(level) {
    if (!level) return '';
    const c = CONF_COLORS[level] || '#555';
    const short = level.replace('RETROACTIVE_LOW_DATA','RETRO').replace('INSUFFICIENT_DATA','INSUF');
    return `<span style="display:inline-block;padding:2px 8px;border-radius:3px;font-family:var(--font-mono);font-size:9px;background:${c}15;color:${c};border:1px solid ${c}33">${short}</span>`;
}

function scoreBar(score, max=100) {
    const pct = Math.min(100, Math.max(0, (score/max)*100));
    const tier = score >= 80 ? 'PRIME' : score >= 60 ? 'STABLE' : score >= 40 ? 'WATCH' : score >= 20 ? 'STRESSED' : 'CRITICAL';
    const c = TIER_COLORS[tier];
    return `<div style="display:flex;align-items:center;gap:8px">
        <div style="flex:1;height:6px;background:var(--bg2);border-radius:3px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${c};border-radius:3px"></div>
        </div>
        <span style="font-family:var(--font-mono);font-size:12px;color:${c};min-width:36px">${score != null ? Math.round(score) : '—'}</span>
    </div>`;
}

function signalCard(label, value, unit, delta, direction) {
    const arrow = direction === 'up' ? '↑' : direction === 'down' ? '↓' : '→';
    const arrowCol = direction === 'up' ? '#ef4444' : direction === 'down' ? '#22c55e' : '#888';
    const deltaStr = delta != null ? `<span style="color:${arrowCol}">${arrow} ${Math.abs(delta).toFixed(1)}${unit||''}</span>` : '';
    return `<div class="card" style="text-align:center">
        <div class="card-title">${label}</div>
        <div style="font-family:var(--font-head);font-size:24px;font-weight:700">${value != null ? (typeof value === 'number' ? value.toFixed(1) : value) : '—'}</div>
        <div style="font-size:11px;color:var(--text3);margin-top:2px">${unit||''} ${deltaStr}</div>
    </div>`;
}

function chemistryBadge(chem) {
    const labels = { NMC:'NMC 14S · 2-Wheeler', LFP:'LFP 16S · E-Rickshaw' };
    return `<span style="font-family:var(--font-mono);font-size:10px;padding:3px 8px;border-radius:3px;background:#1e1e1e;color:var(--text3);border:1px solid #2a2a2a">${labels[chem]||chem}</span>`;
}

function loadingSkeleton(lines=3) {
    return Array(lines).fill(0).map((_,i) =>
        `<div style="height:14px;background:#2a2a2a;border-radius:4px;margin:8px 0;width:${70+Math.random()*30}%;animation:pulse 1.5s ease-in-out infinite"></div>`
    ).join('');
}

function emptyState(msg, sub='') {
    return `<div style="text-align:center;padding:40px 20px;color:var(--text3)">
        <div style="font-size:36px;margin-bottom:8px">○</div>
        <div style="font-size:14px">${msg}</div>
        ${sub ? '<div style="font-size:12px;margin-top:4px;color:var(--text3)">'+sub+'</div>' : ''}
    </div>`;
}

function statPill(label, value, color) {
    return `<div style="text-align:center;padding:6px 14px">
        <div style="font-family:var(--font-mono);font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">${label}</div>
        <div style="font-family:var(--font-head);font-size:18px;font-weight:700;color:${color||'var(--text1)'};margin-top:2px">${value != null ? value : '—'}</div>
    </div>`;
}

function renderCard(title, content) {
    return `<div class="card"><div class="card-title">${title}</div>${content}</div>`;
}

function setTabContent(html) {
    document.getElementById('tab-content').innerHTML = html;
}

function makeChart(canvasId, type, data, opts={}) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    return new Chart(ctx, {
        type,
        data,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: opts.legend !== undefined ? opts.legend : false, labels: { color:'#555', font:{size:10} } } },
            scales: type === 'doughnut' || type === 'pie' ? {} : {
                x: { ticks:{color:'#555',font:{size:9}}, grid:{color:'#e4e2de'} },
                y: { ticks:{color:'#555',font:{size:9}}, grid:{color:'#e4e2de'}, ...(opts.yConfig||{}) },
            },
            ...opts,
        }
    });
}

function fmt(v, d=1) { return v != null ? (typeof v === 'number' ? v.toFixed(d) : v) : '—'; }
