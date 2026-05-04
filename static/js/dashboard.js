// Dashboard rendering. Talks to /api/dashboard/athlete/<uuid>, drives all charts.
(() => {
    "use strict";

    const sel = document.getElementById("athlete-select");
    if (!sel) return;
    sel.addEventListener("change", () => render(sel.value));
    if (window.__INITIAL_UUID__) {
        render(window.__INITIAL_UUID__);
    } else if (sel.value) {
        render(sel.value);
    }

    // ─── Plotly defaults (dark) ───────────────────────────────────────────
    const layoutBase = {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor:  "rgba(0,0,0,0)",
        font: { family: "DM Sans, sans-serif", color: "#e6edf3", size: 12 },
        xaxis: { gridcolor: "#243044", zerolinecolor: "#243044", linecolor: "#30363d" },
        yaxis: { gridcolor: "#243044", zerolinecolor: "#243044", linecolor: "#30363d" },
        margin: { l: 50, r: 24, t: 28, b: 40 },
        legend: { bgcolor: "rgba(0,0,0,0)", font: { color: "#e6edf3" } },
        hovermode: "closest",
    };
    const config = { displaylogo: false, responsive: true };

    async function render(uuid) {
        if (!uuid) return;
        let data;
        try {
            const res = await fetch(`/api/dashboard/athlete/${encodeURIComponent(uuid)}`);
            data = await res.json();
            if (data.error) throw new Error(data.error);
        } catch (e) {
            console.error("dashboard load failed", e);
            return;
        }

        renderScore(data);
        renderIso(data.iso);
        renderCmjPpu("cmj", data.cmj);
        renderCmjPpu("ppu", data.ppu);
        renderPowerCurves(data.power_curves);
    }

    // ─── Score gauge ───────────────────────────────────────────────────────
    function renderScore(data) {
        const latest = data.latest_score;
        const gauge = document.getElementById("score-gauge");
        const arc = document.getElementById("gauge-arc");
        const num = document.getElementById("gauge-num");
        const band = document.getElementById("gauge-band");
        gauge.classList.remove("band-ready", "band-caution", "band-fatigued", "band-insufficient");

        if (!latest || latest.composite_score == null) {
            num.textContent = "—";
            band.textContent = latest ? "Insufficient history" : "No score yet";
            gauge.classList.add("band-insufficient");
            arc.setAttribute("stroke-dasharray", "0 528");
        } else {
            const score = latest.composite_score;
            num.textContent = score.toFixed(0);
            const cls = latest.band === "READY" ? "band-ready" :
                        latest.band === "CAUTION" ? "band-caution" :
                        latest.band === "FATIGUED" ? "band-fatigued" : "band-insufficient";
            gauge.classList.add(cls);
            band.textContent = `${latest.band} — ${fmtDate(latest.date)}`;
            const circumference = 2 * Math.PI * 84;
            const filled = (score / 100) * circumference;
            arc.setAttribute("stroke-dasharray", `${filled} ${circumference}`);
        }

        renderSubStats(latest);
        renderScoreHistory(data.score_history);
    }

    function renderSubStats(latest) {
        const grid = document.getElementById("sub-stats");
        grid.innerHTML = "";
        const cells = [
            ["CMJ z",        latest && latest.cmj_z],
            ["PPU z",        latest && latest.ppu_z],
            ["Isometric z",  latest && latest.iso_z],
            ["Power curve z", latest && latest.power_curve_z],
        ];
        cells.forEach(([label, z]) => {
            const div = document.createElement("div");
            div.className = "stat";
            if (z == null) {
                div.innerHTML = `<div class="stat-label">${label}</div><div class="stat-value text-muted">—</div>`;
            } else {
                const cls = z >= 0.6 ? "up" : z <= -0.6 ? "down" : "flat";
                const verb = cls === "up" ? "above baseline" : cls === "down" ? "below baseline" : "stable";
                const sign = z > 0 ? "+" : "";
                div.innerHTML = `
                    <div class="stat-label">${label}</div>
                    <div class="stat-value">${sign}${z.toFixed(2)} σ</div>
                    <div class="stat-delta ${cls}">${verb}</div>`;
            }
            grid.appendChild(div);
        });
    }

    function renderScoreHistory(hist) {
        const el = document.getElementById("score-history");
        if (!hist || !hist.length) {
            Plotly.purge(el);
            el.innerHTML = `<div class="text-muted" style="font-size: 0.85rem; padding: 0.5rem 0;">No score history yet.</div>`;
            return;
        }
        const x = hist.map((h) => fmtDate(h.date));
        const y = hist.map((h) => h.composite_score);
        const colors = hist.map((h) => h.band === "READY" ? "#4ade80" :
                                       h.band === "CAUTION" ? "#facc15" :
                                       h.band === "FATIGUED" ? "#f87171" : "#6e7681");
        Plotly.react(el, [
            {
                type: "scatter", mode: "lines+markers", x, y,
                line: { color: "#2c99d4", width: 2 },
                marker: { size: 9, color: colors, line: { color: "#0f1419", width: 1 } },
                name: "Composite score",
                hovertemplate: "%{x}<br>Score: %{y:.1f}<extra></extra>",
            },
            {
                type: "scatter", mode: "lines", x, y: x.map(() => 60),
                line: { color: "#22c55e", dash: "dot", width: 1 }, hoverinfo: "skip", showlegend: false,
            },
            {
                type: "scatter", mode: "lines", x, y: x.map(() => 40),
                line: { color: "#ef4444", dash: "dot", width: 1 }, hoverinfo: "skip", showlegend: false,
            },
        ], {
            ...layoutBase,
            yaxis: { ...layoutBase.yaxis, range: [0, 100], title: "Score" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
            margin: { l: 40, r: 16, t: 8, b: 36 },
        }, config);
    }

    // ─── Isometric time series ─────────────────────────────────────────────
    function renderIso(iso) {
        const palette = { I: "#8be9fd", Y: "#50fa7b", T: "#ffb86c", IR90: "#ff79c6" };
        const traces = Object.entries(iso || {}).map(([m, rows]) => ({
            type: "scatter", mode: "lines+markers",
            x: rows.map((r) => fmtDate(r.date)),
            y: rows.map((r) => r.avg_force),
            name: m,
            line: { color: palette[m] || "#2c99d4", width: 2 },
            marker: { size: 7 },
            hovertemplate: `${m}<br>%{x}<br>Avg Force: %{y:.1f} N<extra></extra>`,
        }));
        Plotly.react("iso-plot", traces, {
            ...layoutBase,
            yaxis: { ...layoutBase.yaxis, title: "Avg Force (N)" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
        }, config);

        const grid = document.getElementById("iso-stats");
        grid.innerHTML = "";
        Object.entries(iso || {}).forEach(([m, rows]) => grid.appendChild(latestPrevDelta(`${m} avg force`, rows.map((r) => r.avg_force))));
    }

    // ─── CMJ / PPU ─────────────────────────────────────────────────────────
    function renderCmjPpu(kind, group) {
        const ts     = group.timeseries || [];
        const scatter = group.scatter || [];
        const peers  = group.peers || [];

        // Split into readiness-screen and athletic-screen rows for distinct markers.
        const rsTsRows  = ts.filter((r) => r.source !== "athletic_screen");
        const athTsRows = ts.filter((r) => r.source === "athletic_screen");

        const jhTraces = [];
        if (rsTsRows.length) {
            jhTraces.push({
                type: "scatter", mode: "lines+markers",
                x: rsTsRows.map((r) => fmtDate(r.date)),
                y: rsTsRows.map((r) => r.jump_height),
                line: { color: "#2c99d4", width: 2 },
                marker: { size: 8, symbol: "circle" },
                name: "Jump height",
                hovertemplate: "%{x}<br>JH: %{y:.2f} in<extra></extra>",
            });
        }
        if (athTsRows.length) {
            jhTraces.push({
                type: "scatter", mode: "markers",
                x: athTsRows.map((r) => fmtDate(r.date)),
                y: athTsRows.map((r) => r.jump_height),
                marker: { size: 10, symbol: "circle-open", color: "#2c99d4", line: { width: 2, color: "#2c99d4" } },
                name: "Athletic screen",
                hovertemplate: "%{x}<br>JH: %{y:.2f} in (athletic screen)<extra></extra>",
            });
        }
        Plotly.react(`${kind}-jh-plot`, jhTraces.length ? jhTraces : [{ type: "scatter", x: [], y: [] }], {
            ...layoutBase,
            title: { text: `Jump height (${kind.toUpperCase()})`, font: { size: 13 }, x: 0, xanchor: "left" },
            yaxis: { ...layoutBase.yaxis, title: "Jump height (in)" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
        }, config);

        const fvEl = `${kind}-fv-plot`;
        const fvTraces = [
            {
                type: "scattergl", mode: "markers",
                x: peers.map((p) => p.force_at_pp),
                y: peers.map((p) => p.vel_at_pp),
                marker: { color: "rgba(139, 233, 253, 0.35)", size: 6, line: { width: 0 } },
                name: "Peers",
                hoverinfo: "skip",
            },
            {
                type: "scatter", mode: "markers",
                x: scatter.map((p) => p.force_at_pp),
                y: scatter.map((p) => p.vel_at_pp),
                marker: {
                    size: 12,
                    color: scatter.map((_, i) => i),
                    colorscale: "Viridis",
                    showscale: false,
                    line: { width: 1, color: "#0f1419" },
                },
                text: scatter.map((p) => fmtDate(p.date)),
                name: "This athlete",
                hovertemplate: "%{text}<br>F: %{x:.1f} N<br>V: %{y:.2f} m/s<extra></extra>",
            },
        ];
        Plotly.react(fvEl, fvTraces, {
            ...layoutBase,
            title: { text: "Force vs Velocity at peak power", font: { size: 13 }, x: 0, xanchor: "left" },
            xaxis: { ...layoutBase.xaxis, title: "Force at PP (N)" },
            yaxis: { ...layoutBase.yaxis, title: "Velocity at PP (m/s)" },
        }, config);

        // Stat grid — use all rows combined (already sorted by date) for latest/prev deltas.
        const grid = document.getElementById(`${kind}-stats`);
        grid.innerHTML = "";
        grid.appendChild(latestPrevDelta("Jump height (in)",  ts.map((r) => r.jump_height)));
        grid.appendChild(latestPrevDelta("W/kg",              ts.map((r) => r.pp_w_per_kg)));
        grid.appendChild(latestPrevDelta("F @ PP (N)",        ts.map((r) => r.force_at_pp)));
        grid.appendChild(latestPrevDelta("V @ PP (m/s)",      ts.map((r) => r.vel_at_pp)));
    }

    // ─── Power-curve trends ────────────────────────────────────────────────
    function renderPowerCurves(curves) {
        const el = document.getElementById("power-plot");
        const cmj = curves?.CMJ || [];
        const ppu = curves?.PPU || [];
        if (!cmj.length && !ppu.length) {
            Plotly.purge(el);
            el.innerHTML = `<div class="text-muted" style="font-size: 0.85rem; padding: 1rem 0;">No power-curve data yet — drop *_Power.txt files into the output folder before running ingestion.</div>`;
            return;
        }
        const traces = [];
        if (cmj.length) {
            traces.push({
                type: "scatter", mode: "lines+markers",
                x: cmj.map((r) => fmtDate(r.date)), y: cmj.map((r) => r.peak_power_w),
                line: { color: "#50fa7b" }, marker: { size: 7 },
                name: "CMJ peak power (W)",
                hovertemplate: "%{x}<br>Peak: %{y:.0f} W<extra>CMJ</extra>",
                yaxis: "y",
            });
            traces.push({
                type: "scatter", mode: "lines+markers",
                x: cmj.map((r) => fmtDate(r.date)), y: cmj.map((r) => r.rpd_max),
                line: { color: "#bd93f9", dash: "dot" }, marker: { size: 6 },
                name: "CMJ RPD max (W/s)",
                yaxis: "y2",
                hovertemplate: "%{x}<br>RPD: %{y:.0f} W/s<extra>CMJ</extra>",
            });
        }
        if (ppu.length) {
            traces.push({
                type: "scatter", mode: "lines+markers",
                x: ppu.map((r) => fmtDate(r.date)), y: ppu.map((r) => r.peak_power_w),
                line: { color: "#ffb86c" }, marker: { size: 7 },
                name: "PPU peak power (W)",
                hovertemplate: "%{x}<br>Peak: %{y:.0f} W<extra>PPU</extra>",
                yaxis: "y",
            });
        }
        Plotly.react(el, traces, {
            ...layoutBase,
            xaxis: { ...layoutBase.xaxis, type: "category" },
            yaxis:  { ...layoutBase.yaxis, title: "Peak power (W)" },
            yaxis2: { ...layoutBase.yaxis, title: "RPD (W/s)", overlaying: "y", side: "right", showgrid: false },
            legend: { ...layoutBase.legend, orientation: "h", y: -0.18 },
        }, config);
    }

    // ─── Stat helpers ─────────────────────────────────────────────────────
    function latestPrevDelta(label, values) {
        const cleaned = (values || []).filter((v) => v != null && !Number.isNaN(v));
        const div = document.createElement("div");
        div.className = "stat";
        if (cleaned.length === 0) {
            div.innerHTML = `<div class="stat-label">${escape(label)}</div><div class="stat-value text-muted">—</div>`;
            return div;
        }
        const latest = cleaned[cleaned.length - 1];
        const prev   = cleaned.length > 1 ? cleaned[cleaned.length - 2] : null;
        const delta  = prev != null ? (latest - prev) : null;
        const cls    = delta == null ? "flat" : (delta > 0.001 ? "up" : (delta < -0.001 ? "down" : "flat"));
        const sign   = delta == null ? "" : (delta > 0 ? "+" : "");
        div.innerHTML = `
            <div class="stat-label">${escape(label)}</div>
            <div class="stat-value">${formatNum(latest)}</div>
            <div class="stat-delta ${cls}">${prev == null ? "first session" : `${sign}${formatNum(delta)} vs prev`}</div>`;
        return div;
    }

    function fmtDate(iso) {
        if (!iso) return "—";
        const [y, m, d] = iso.split("-");
        return `${m}/${d}/${y.slice(2)}`;
    }

    function formatNum(v) {
        if (v == null || Number.isNaN(v)) return "—";
        if (Math.abs(v) >= 100) return v.toFixed(0);
        if (Math.abs(v) >= 10)  return v.toFixed(1);
        return v.toFixed(2);
    }

    function escape(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
    }
})();
