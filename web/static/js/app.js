/**
 * CryptoSignal Hub - 前端交互逻辑
 */

async function triggerAnalysis() {
    const btn = event.target;
    const originalText = btn.textContent;
    btn.textContent = '⏳ 分析中...';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/analyze', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            window.location.reload();
        } else {
            alert('分析失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    } finally {
        btn.textContent = originalText;
        btn.disabled = false;
    }
}

async function refreshHealth() {
    const btn = document.getElementById('refreshHealthBtn');
    if (btn) { btn.textContent = '⏳ 检测中...'; btn.disabled = true; }
    try {
        const resp = await fetch('/api/health?refresh=true');
        const data = await resp.json();
        renderHealthGrid(data);
    } catch (e) {
        console.error('健康检查失败', e);
    } finally {
        if (btn) { btn.textContent = '🔄 刷新'; btn.disabled = false; }
    }
}

function renderHealthGrid(data) {
    const grid = document.getElementById('healthGrid');
    if (!grid || !data || !data.probes) return;

    const statusIcon = {ok: '🟢', degraded: '🟡', error: '🔴', unknown: '⚪'};
    const overallEl = document.querySelector('.health-overall');
    if (overallEl) {
        overallEl.className = `health-overall health-${data.overall}`;
        overallEl.textContent = `${data.ok_count}/${data.total_count} 正常`;
    }

    grid.innerHTML = data.probes.map(p => `
        <div class="health-probe health-probe-${p.status}">
            <div class="probe-status">${statusIcon[p.status] || '⚪'}</div>
            <div class="probe-info">
                <div class="probe-name">${p.name}</div>
                <div class="probe-message">${p.message}</div>
            </div>
            ${p.latency_ms > 0 ? `<div class="probe-latency">${Math.round(p.latency_ms)}ms</div>` : ''}
        </div>
    `).join('');

    const footer = document.querySelector('.health-footer');
    if (footer) footer.textContent = `上次检查: ${data.checked_at?.substring(0, 19) || '-'}`;
}

// 大屏自动刷新（每 5 分钟）
if (window.location.pathname === '/') {
    setInterval(async () => {
        try {
            const resp = await fetch('/api/status');
            if (resp.ok) {
                const data = await resp.json();
                if (data.health) renderHealthGrid(data.health);
            }
        } catch (e) { /* 静默 */ }
    }, 5 * 60 * 1000);
}
