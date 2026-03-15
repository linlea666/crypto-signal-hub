(function () {
    'use strict';

    const REFRESH_INTERVAL = 5 * 60 * 1000;
    const TICKER_INTERVAL = 15 * 1000;
    const ANIMATION_DURATION = 800;

    const FACTOR_KEYS = [
        'technical', 'funding_rate', 'open_interest',
        'long_short_ratio', 'options', 'macro', 'sentiment'
    ];

    const FACTOR_LABELS = {
        'technical': '技术面',
        'funding_rate': '资金费率',
        'open_interest': '持仓量',
        'long_short_ratio': '多空比',
        'options': '期权数据',
        'macro': '宏观环境',
        'sentiment': '市场情绪'
    };

    const FEAR_GREED_THRESHOLDS = [
        { max: 25, color: '#ef4444' },
        { max: 45, color: '#f97316' },
        { max: 55, color: '#eab308' },
        { max: 75, color: '#84cc16' },
        { max: 100, color: '#22c55e' }
    ];

    if (typeof Chart !== 'undefined') {
        Chart.defaults.font.family = "'Inter', sans-serif";
        Chart.defaults.font.size = 12;
        Chart.defaults.color = '#94a3b8';
        Chart.defaults.plugins.legend.display = false;
        Chart.defaults.responsive = true;
        Chart.defaults.maintainAspectRatio = true;
        Chart.defaults.animation = { duration: ANIMATION_DURATION };
    }

    // ── Utility ──

    function formatNumber(n) {
        if (n == null || isNaN(n)) return '—';
        return Number(n).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    function formatPercent(n) {
        if (n == null || isNaN(n)) return '—';
        var v = Number(n);
        var sign = v > 0 ? '+' : '';
        return sign + v.toFixed(2) + '%';
    }

    function animateValue(element, start, end, duration) {
        if (!element) return;
        var startTime = null;
        var delta = end - start;

        function step(ts) {
            if (!startTime) startTime = ts;
            var progress = Math.min((ts - startTime) / duration, 1);
            var eased = 1 - Math.pow(1 - progress, 3);
            element.textContent = formatNumber(start + delta * eased);
            if (progress < 1) requestAnimationFrame(step);
        }

        requestAnimationFrame(step);
    }

    function getFearGreedColor(value) {
        for (var i = 0; i < FEAR_GREED_THRESHOLDS.length; i++) {
            if (value <= FEAR_GREED_THRESHOLDS[i].max) {
                return FEAR_GREED_THRESHOLDS[i].color;
            }
        }
        return FEAR_GREED_THRESHOLDS[FEAR_GREED_THRESHOLDS.length - 1].color;
    }

    function getCanvas(canvasId) {
        var el = document.getElementById(canvasId);
        if (!el || el.tagName !== 'CANVAS') return null;
        return el.getContext('2d');
    }

    function destroyExistingChart(canvasId) {
        var existing = Chart.getChart(canvasId);
        if (existing) existing.destroy();
    }

    // ── Charts ──

    function initRadarChart(canvasId, scores) {
        if (typeof Chart === 'undefined') return null;
        var ctx = getCanvas(canvasId);
        if (!ctx || !scores || !scores.length) return null;

        destroyExistingChart(canvasId);

        var labels = scores.map(function (s) {
            return s.label || FACTOR_LABELS[s.name] || s.name;
        });
        var normalized = scores.map(function (s) {
            return s.max_score ? s.score / s.max_score : 0;
        });

        var positiveData = normalized.map(function (v) { return Math.max(v, 0); });
        var negativeData = normalized.map(function (v) { return Math.min(v, 0); });

        return new Chart(ctx, {
            type: 'radar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Bullish',
                        data: positiveData,
                        backgroundColor: 'rgba(34, 197, 94, 0.2)',
                        borderColor: 'rgba(34, 197, 94, 0.8)',
                        borderWidth: 2,
                        pointBackgroundColor: '#22c55e',
                        pointRadius: 3
                    },
                    {
                        label: 'Bearish',
                        data: negativeData,
                        backgroundColor: 'rgba(239, 68, 68, 0.2)',
                        borderColor: 'rgba(239, 68, 68, 0.8)',
                        borderWidth: 2,
                        pointBackgroundColor: '#ef4444',
                        pointRadius: 3
                    }
                ]
            },
            options: {
                scales: {
                    r: {
                        min: -1,
                        max: 1,
                        ticks: {
                            stepSize: 0.5,
                            color: '#64748b',
                            backdropColor: 'transparent'
                        },
                        grid: { color: '#1e293b' },
                        angleLines: { color: '#1e293b' },
                        pointLabels: {
                            color: '#94a3b8',
                            font: { size: 11 }
                        }
                    }
                },
                plugins: {
                    legend: { display: true, position: 'bottom', labels: { boxWidth: 12, padding: 16 } }
                }
            }
        });
    }

    function initFearGreedGauge(canvasId, value) {
        if (typeof Chart === 'undefined') return null;
        var ctx = getCanvas(canvasId);
        if (!ctx) return null;

        destroyExistingChart(canvasId);

        var val = Math.max(0, Math.min(100, Number(value) || 0));
        var color = getFearGreedColor(val);

        return new Chart(ctx, {
            type: 'doughnut',
            data: {
                datasets: [{
                    data: [val, 100 - val],
                    backgroundColor: [color, '#1e293b'],
                    borderWidth: 0
                }]
            },
            options: {
                cutout: '75%',
                plugins: {
                    tooltip: { enabled: false }
                }
            },
            plugins: [{
                id: 'fearGreedCenter',
                afterDraw: function (chart) {
                    var width = chart.width;
                    var height = chart.height;
                    var drawCtx = chart.ctx;
                    drawCtx.save();
                    drawCtx.textAlign = 'center';
                    drawCtx.textBaseline = 'middle';
                    drawCtx.fillStyle = color;
                    drawCtx.font = "bold 28px 'Inter', sans-serif";
                    drawCtx.fillText(val, width / 2, height / 2 - 8);
                    drawCtx.fillStyle = '#94a3b8';
                    drawCtx.font = "12px 'Inter', sans-serif";
                    var label = val <= 25 ? '极度恐惧' : val <= 45 ? '恐惧' :
                        val <= 55 ? '中性' : val <= 75 ? '贪婪' : '极度贪婪';
                    drawCtx.fillText(label, width / 2, height / 2 + 18);
                    drawCtx.restore();
                }
            }]
        });
    }

    function initScoreGauge(canvasId, score, maxScore) {
        if (typeof Chart === 'undefined') return null;
        var ctx = getCanvas(canvasId);
        if (!ctx) return null;

        destroyExistingChart(canvasId);

        var s = Number(score) || 0;
        var m = Number(maxScore) || 1;
        // Normalize to 0..1 range within [-max, +max]
        var ratio = (s + m) / (2 * m);
        ratio = Math.max(0, Math.min(1, ratio));

        var activeColor = s > 0 ? '#22c55e' : s < 0 ? '#ef4444' : '#64748b';

        return new Chart(ctx, {
            type: 'doughnut',
            data: {
                datasets: [{
                    data: [ratio, 1 - ratio, 1],
                    backgroundColor: [activeColor, '#1e293b', 'transparent'],
                    borderWidth: 0
                }]
            },
            options: {
                circumference: 180,
                rotation: -90,
                cutout: '78%',
                plugins: {
                    tooltip: { enabled: false }
                }
            },
            plugins: [{
                id: 'scoreGaugeCenter',
                afterDraw: function (chart) {
                    var width = chart.width;
                    var height = chart.height;
                    var drawCtx = chart.ctx;
                    var centerY = height * 0.62;
                    drawCtx.save();
                    drawCtx.textAlign = 'center';
                    drawCtx.textBaseline = 'middle';
                    drawCtx.fillStyle = activeColor;
                    drawCtx.font = "bold 24px 'Inter', sans-serif";
                    var display = (s > 0 ? '+' : '') + s.toFixed(0);
                    drawCtx.fillText(display, width / 2, centerY - 6);
                    drawCtx.fillStyle = '#64748b';
                    drawCtx.font = "11px 'Inter', sans-serif";
                    drawCtx.fillText('/ ' + m.toFixed(0), width / 2, centerY + 16);
                    drawCtx.restore();
                }
            }]
        });
    }

    // ── Dashboard Actions ──

    window.triggerAnalysis = function (symbol) {
        var btn = document.querySelector('[onclick*="triggerAnalysis"]');
        if (!btn) return;

        var originalText = btn.textContent;
        btn.textContent = '⏳ 分析中...';
        btn.disabled = true;

        // 空状态进度条
        var progress = document.getElementById('analyze-progress');
        if (progress) progress.style.display = 'block';

        var body = {};
        if (symbol) body.symbol = symbol;

        fetch('/api/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.success) {
                    window.location.reload();
                } else {
                    alert('分析失败: ' + (data.error || '未知错误'));
                }
            })
            .catch(function (e) {
                alert('请求失败: ' + e.message);
            })
            .finally(function () {
                btn.textContent = originalText;
                btn.disabled = false;
                if (progress) progress.style.display = 'none';
            });
    };

    window.refreshHealth = function () {
        var btn = document.getElementById('refreshHealthBtn');
        if (btn) {
            btn.textContent = '⏳ 检测中...';
            btn.disabled = true;
        }

        fetch('/api/health?refresh=true')
            .then(function (resp) { return resp.json(); })
            .then(function (data) { renderHealthBar(data); })
            .catch(function (e) { console.error('健康检查失败', e); })
            .finally(function () {
                if (btn) {
                    btn.textContent = '🔄 刷新';
                    btn.disabled = false;
                }
            });
    };

    window.switchSymbol = function (symbol) {
        if (!symbol) return;
        window.location.href = '/?symbol=' + encodeURIComponent(symbol);
    };

    // ── Health Bar ──

    var STATUS_ICON = { ok: '🟢', degraded: '🟡', error: '🔴', unknown: '⚪' };

    function renderHealthBar(data) {
        var bar = document.getElementById('health-bar');
        if (!bar || !data || !data.probes) return;

        bar.innerHTML = data.probes.map(function (p) {
            var st = typeof p.status === 'object' ? p.status.value : p.status;
            var detail = p.latency_ms > 0
                ? '<span class="mono strip-meta">' + Math.round(p.latency_ms) + 'ms</span>'
                : '<span class="strip-meta">' + (p.message || '').substring(0, 20) + '</span>';
            return '<div class="health-probe">' +
                '<span class="health-dot ' + st + '"></span>' +
                '<span class="health-probe-name">' + p.name + '</span>' +
                detail + '</div>';
        }).join('');
    }

    // ── Auto-Refresh ──

    function startAutoRefresh() {
        if (window.location.pathname !== '/') return;

        setInterval(function () {
            Promise.all([
                fetch('/api/status').then(function (r) { return r.ok ? r.json() : null; }),
                fetch('/api/latest').then(function (r) { return r.ok ? r.json() : null; })
            ])
                .then(function (results) {
                    var statusData = results[0];
                    var latestData = results[1];

                    if (statusData && statusData.health) {
                        renderHealthBar(statusData.health);
                    }

                    if (latestData && latestData.report) {
                        updateDashboardData(latestData.report);
                    }
                })
                .catch(function () { /* silent */ });
        }, REFRESH_INTERVAL);
    }

    // ── 实时价格轮询 ──

    function startTickerPolling() {
        if (window.location.pathname !== '/') return;
        var coinTab = document.querySelector('.coin-tab.active');
        if (!coinTab) return;
        var href = coinTab.getAttribute('href') || '';
        var match = href.match(/symbol=([^&]+)/);
        if (!match) return;
        var symbol = decodeURIComponent(match[1]);

        function poll() {
            fetch('/api/ticker/' + encodeURIComponent(symbol))
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data || !data.price) return;
                    var priceEl = document.getElementById('live-price');
                    var changeEl = document.getElementById('live-change');
                    var tagEl = document.getElementById('price-live-tag');

                    if (priceEl) {
                        var p = Number(data.price);
                        priceEl.textContent = '$' + p.toLocaleString('en-US', {
                            minimumFractionDigits: 2, maximumFractionDigits: 2
                        });
                    }
                    if (changeEl && data.change_pct != null) {
                        var c = Number(data.change_pct);
                        changeEl.textContent = (c >= 0 ? '+' : '') + c.toFixed(2) + '%';
                        changeEl.className = 'strip-change ' + (c >= 0 ? 'positive' : 'negative');
                    }
                    if (tagEl) tagEl.style.display = 'inline';
                })
                .catch(function () { /* silent */ });
        }

        poll();
        setInterval(poll, TICKER_INTERVAL);
    }

    function updateDashboardData(report) {
        if (!report) return;

        var priceEl = document.querySelector('.price');
        if (priceEl && report.snapshot && report.snapshot.price) {
            var newPrice = report.snapshot.price.current;
            var oldPrice = parseFloat(priceEl.textContent.replace(/[$,]/g, '')) || 0;
            if (oldPrice !== newPrice) {
                animateValue(priceEl, oldPrice, newPrice, ANIMATION_DURATION);
            }
        }

        var scoreEl = document.querySelector('.overview-right .score');
        if (scoreEl && report.total_score != null) {
            var sign = report.total_score > 0 ? '+' : '';
            var cls = report.total_score > 0 ? 'bullish' : report.total_score < 0 ? 'bearish' : 'neutral';
            var dirLabel = report.direction === 'bullish' ? '偏多' :
                report.direction === 'bearish' ? '偏空' : '中性';
            scoreEl.className = 'score ' + cls;
            scoreEl.innerHTML = sign + report.total_score.toFixed(0) + '/' +
                report.max_score.toFixed(0) +
                ' <span class="direction-label">' + dirLabel + '</span>';
        }

        var confEl = document.querySelector('.confidence');
        if (confEl && report.confidence != null) {
            confEl.textContent = '信心度 ' + report.confidence.toFixed(0) + '%';
        }

        if (report.scores) {
            var radarCanvas = document.getElementById('radarChart');
            if (radarCanvas) initRadarChart('radarChart', report.scores);

            var gaugeCanvas = document.getElementById('scoreGauge');
            if (gaugeCanvas) initScoreGauge('scoreGauge', report.total_score, report.max_score);
        }

        var tsEl = document.querySelector('.timestamp');
        if (tsEl && report.timestamp) {
            tsEl.textContent = report.timestamp;
        }
    }

    // ── Init ──

    function initChartsFromDOM() {
        if (typeof Chart === 'undefined') return;

        var radarCanvas = document.getElementById('radarChart');
        if (radarCanvas) {
            var rawScores = radarCanvas.getAttribute('data-scores');
            if (rawScores) {
                try {
                    initRadarChart('radarChart', JSON.parse(rawScores));
                } catch (e) { console.error('Radar chart init failed', e); }
            }
        }

        var fgCanvas = document.getElementById('fearGreedGauge');
        if (fgCanvas) {
            var fgValue = fgCanvas.getAttribute('data-value');
            if (fgValue != null) initFearGreedGauge('fearGreedGauge', Number(fgValue));
        }

        var sgCanvas = document.getElementById('scoreGauge');
        if (sgCanvas) {
            var sgScore = sgCanvas.getAttribute('data-score');
            var sgMax = sgCanvas.getAttribute('data-max');
            if (sgScore != null) initScoreGauge('scoreGauge', Number(sgScore), Number(sgMax) || 100);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        initChartsFromDOM();
        startAutoRefresh();
        startTickerPolling();
    });

    window.initRadarChart = initRadarChart;
    window.initFearGreedGauge = initFearGreedGauge;
    window.initScoreGauge = initScoreGauge;
    window.renderHealthBar = renderHealthBar;
    window.formatNumber = formatNumber;
    window.formatPercent = formatPercent;
    window.animateValue = animateValue;
})();
