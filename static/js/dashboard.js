// Dashboard rendering. Talks to /api/dashboard/athlete/<uuid>, drives all charts.
(() => {
    "use strict";

    // ─── Athlete filter (type-to-search) ─────────────────────────────────
    const filterInput = document.getElementById("athlete-filter");
    const dropdown = document.getElementById("athlete-dropdown");
    const athletes = window.__ATHLETES__ || [];
    let activeUuid = window.__INITIAL_UUID__ || (athletes[0] ? athletes[0].athlete_uuid : null);

    function escapeHtml(s) {
        if (s == null) return "";
        return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
    }

    function showDropdown(list) {
        if (!dropdown) return;
        dropdown.innerHTML = "";
        list.forEach((a) => {
            const div = document.createElement("div");
            div.className = "item";
            div.innerHTML = `${escapeHtml(a.name)}${a.age_group ? ` <span class="age-tag">${escapeHtml(a.age_group)}</span>` : ""}`;
            div.addEventListener("click", () => {
                activeUuid = a.athlete_uuid;
                if (filterInput) filterInput.value = a.name;
                dropdown.style.display = "none";
                render(activeUuid);
            });
            dropdown.appendChild(div);
        });
        dropdown.style.display = list.length ? "block" : "none";
    }

    if (filterInput) {
        filterInput.addEventListener("input", () => {
            const q = filterInput.value.toLowerCase().trim();
            if (!q) { dropdown.style.display = "none"; return; }
            const hits = athletes.filter((a) =>
                a.name.toLowerCase().includes(q) ||
                (a.age_group || "").toLowerCase().includes(q)
            );
            showDropdown(hits);
        });
        filterInput.addEventListener("focus", () => {
            const q = filterInput.value.toLowerCase().trim();
            if (q) {
                const hits = athletes.filter((a) =>
                    a.name.toLowerCase().includes(q) ||
                    (a.age_group || "").toLowerCase().includes(q)
                );
                showDropdown(hits);
            }
        });
    }

    document.addEventListener("click", (e) => {
        if (dropdown && !e.target.closest(".search-dropdown")) {
            dropdown.style.display = "none";
        }
    });

    if (activeUuid) render(activeUuid);

    // ISO legacy toggle re-renders iso panel when toggled.
    const isoToggle = document.getElementById("iso-legacy-toggle");
    let _isoData = null;
    if (isoToggle) {
        isoToggle.addEventListener("change", () => {
            if (_isoData) renderIso(_isoData);
        });
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
        renderTodayVsBaseline(data.today_vs_baseline || []);
        _isoData = data.iso;
        renderIso(data.iso);
        renderGrip(data.grip);
        renderCmjPpu("cmj", data.cmj);
        renderMovementStrategy("cmj", data.movement_strategy?.cmj || []);
        renderCmjPpu("ppu", data.ppu);
        renderMovementStrategy("ppu", data.movement_strategy?.ppu || []);
        renderTrialConsistency(data.intra_session);
        renderPowerCurves(data.power_curves);
        renderFlagHeatmap(data.flag_heatmap);
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
            const label = latest.band === "READY" ? "Train" :
                          latest.band === "CAUTION" ? "Monitor" :
                          latest.band === "FATIGUED" ? "Modify" : latest.band;
            band.textContent = `${label} — ${fmtDate(latest.date)}`;
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
            ["CMJ z",         latest && latest.cmj_z],
            ["PPU z",         latest && latest.ppu_z],
            ["Isometric z",   latest && latest.iso_z],
            ["Power curve z", latest && latest.power_curve_z],
            ["Grip z",        latest && latest.grip_z],
        ];
        cells.forEach(([label, z]) => {
            const div = document.createElement("div");
            div.className = "stat";
            if (z == null) {
                div.innerHTML = `<div class="stat-label">${escape(label)}</div><div class="stat-value text-muted">—</div>`;
            } else {
                const cls = z >= 0.6 ? "up" : z <= -0.6 ? "down" : "flat";
                const verb = cls === "up" ? "above baseline" : cls === "down" ? "below baseline" : "stable";
                const sign = z > 0 ? "+" : "";
                div.innerHTML = `
                    <div class="stat-label">${escape(label)}</div>
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
            { type: "scatter", mode: "lines", x, y: x.map(() => 60),
              line: { color: "#22c55e", dash: "dot", width: 1 }, hoverinfo: "skip", showlegend: false },
            { type: "scatter", mode: "lines", x, y: x.map(() => 40),
              line: { color: "#ef4444", dash: "dot", width: 1 }, hoverinfo: "skip", showlegend: false },
        ], {
            ...layoutBase,
            yaxis: { ...layoutBase.yaxis, range: [0, 100], title: "Score" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
            margin: { l: 40, r: 16, t: 8, b: 36 },
        }, config);
    }

    // ─── Today vs Baseline ────────────────────────────────────────────────
    function renderTodayVsBaseline(metrics) {
        const el = document.getElementById("tvb-plot");
        if (!metrics || !metrics.length) {
            el.innerHTML = `<div class="text-muted" style="padding: 1rem 0; font-size: 0.85rem;">No data for today yet.</div>`;
            return;
        }
        const sorted = [...metrics].filter((m) => m.z != null).sort((a, b) => (b.z || 0) - (a.z || 0));
        const labels = sorted.map((m) => m.label);
        const zs = sorted.map((m) => m.z);
        const colors = sorted.map((m) =>
            m.flag === "rise" ? "#4ade80" :
            m.flag === "drop" ? "#f87171" : "#2c99d4"
        );
        const hovertext = sorted.map((m) =>
            `${m.label}<br>Today: ${formatNum(m.today)}<br>` +
            `Baseline mean: ${formatNum(m.mean)}<br>` +
            `SD: ${formatNum(m.sd)}<br>` +
            `z: ${m.z != null ? m.z.toFixed(3) : "—"}<br>` +
            `n history: ${m.n_history}`
        );
        Plotly.react(el, [{
            type: "bar", orientation: "h",
            x: zs, y: labels,
            marker: { color: colors },
            hovertemplate: "%{customdata}<extra></extra>",
            customdata: hovertext,
        }], {
            ...layoutBase,
            xaxis: { ...layoutBase.xaxis, title: "z-score", zeroline: true, zerolinecolor: "#888", zerolinewidth: 1 },
            yaxis: { ...layoutBase.yaxis, automargin: true },
            margin: { l: 160, r: 24, t: 12, b: 40 },
            height: Math.max(300, labels.length * 22 + 60),
        }, config);
    }

    // ─── Isometric time series ─────────────────────────────────────────────
    function renderIso(iso) {
        const showLegacy = isoToggle && isoToggle.checked;
        const palette = { I: "#8be9fd", Y: "#50fa7b", T: "#ffb86c", IR90: "#ff79c6" };
        const traces = [];
        Object.entries(iso || {}).forEach(([m, entry]) => {
            const isLegacy = entry.legacy;
            if (isLegacy && !showLegacy) return;
            const rows = entry.data || entry;  // backward compat if plain array
            traces.push({
                type: "scatter", mode: "lines+markers",
                x: rows.map((r) => fmtDate(r.date)),
                y: rows.map((r) => r.avg_force),
                name: m + (isLegacy ? " (legacy)" : ""),
                line: { color: palette[m] || "#2c99d4", width: 2, dash: isLegacy ? "dot" : "solid" },
                marker: { size: 7 },
                hovertemplate: `${m}<br>%{x}<br>Avg Force: %{y:.1f} N<extra></extra>`,
            });
        });
        Plotly.react("iso-plot", traces.length ? traces : [{ type: "scatter", x: [], y: [] }], {
            ...layoutBase,
            yaxis: { ...layoutBase.yaxis, title: "Avg Force (N)" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
        }, config);

        const grid = document.getElementById("iso-stats");
        grid.innerHTML = "";
        ["Y", "IR90"].forEach((m) => {
            const entry = (iso || {})[m];
            const rows = entry ? (entry.data || entry) : [];
            grid.appendChild(latestPrevDelta(`${m} avg force`, rows.map((r) => r.avg_force)));
        });
    }

    // ─── Grip strength ─────────────────────────────────────────────────────
    function renderGrip(grip) {
        const series = grip?.timeseries || [];
        const card = document.getElementById("grip-card");

        if (!series.length) {
            if (card) card.style.display = "none";
            return;
        }
        if (card) card.style.display = "";

        const x = series.map((r) => fmtDate(r.date));

        // Timeseries — left, right, max
        Plotly.react("grip-ts-plot", [
            {
                type: "scatter", mode: "lines+markers", x, y: series.map((r) => r.left_kg),
                name: "Left (kg)", line: { color: "#8be9fd", width: 2 }, marker: { size: 7 },
                hovertemplate: "%{x}<br>Left: %{y:.1f} kg<extra></extra>",
            },
            {
                type: "scatter", mode: "lines+markers", x, y: series.map((r) => r.right_kg),
                name: "Right (kg)", line: { color: "#50fa7b", width: 2 }, marker: { size: 7 },
                hovertemplate: "%{x}<br>Right: %{y:.1f} kg<extra></extra>",
            },
            {
                type: "scatter", mode: "lines+markers", x, y: series.map((r) => r.max_kg),
                name: "Max (kg)", line: { color: "#bd93f9", width: 2, dash: "dot" }, marker: { size: 5 },
                hovertemplate: "%{x}<br>Max: %{y:.1f} kg<extra></extra>",
            },
        ], {
            ...layoutBase,
            title: { text: "Grip strength over time", font: { size: 13 }, x: 0, xanchor: "left" },
            yaxis: { ...layoutBase.yaxis, title: "kg" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
        }, config);

        // Asymmetry timeseries — color-coded by threshold
        const asymColors = series.map((r) => {
            const v = r.asymmetry_pct;
            if (v == null) return "#6e7681";
            if (v < 10) return "#4ade80";
            if (v < 15) return "#facc15";
            return "#f87171";
        });
        Plotly.react("grip-asym-plot", [{
            type: "scatter", mode: "lines+markers", x,
            y: series.map((r) => r.asymmetry_pct),
            line: { color: "#2c99d4", width: 2 },
            marker: { size: 10, color: asymColors, line: { color: "#0f1419", width: 1 } },
            name: "Asymmetry %",
            hovertemplate: "%{x}<br>Asymmetry: %{y:.1f}%<extra></extra>",
        }], {
            ...layoutBase,
            title: { text: "L/R asymmetry (%)", font: { size: 13 }, x: 0, xanchor: "left" },
            yaxis: { ...layoutBase.yaxis, title: "Asymmetry (%)", rangemode: "tozero" },
            xaxis: { ...layoutBase.xaxis, type: "category" },
            shapes: [
                { type: "line", x0: 0, x1: 1, xref: "paper", y0: 10, y1: 10, line: { color: "#facc15", dash: "dot", width: 1 } },
                { type: "line", x0: 0, x1: 1, xref: "paper", y0: 15, y1: 15, line: { color: "#f87171", dash: "dot", width: 1 } },
            ],
        }, config);

        // Stat grid — latest L, R, max, asymmetry
        const grid = document.getElementById("grip-stats");
        grid.innerHTML = "";
        grid.appendChild(latestPrevDelta("Left kg",      series.map((r) => r.left_kg)));
        grid.appendChild(latestPrevDelta("Right kg",     series.map((r) => r.right_kg)));
        grid.appendChild(latestPrevDelta("Max kg",       series.map((r) => r.max_kg)));
        grid.appendChild(latestPrevDelta("Asymmetry %",  series.map((r) => r.asymmetry_pct)));
    }

    // ─── CMJ / PPU ─────────────────────────────────────────────────────────
    function renderCmjPpu(kind, group) {
        const ts      = group.timeseries || [];
        const scatter = group.scatter || [];
        const peers   = group.peers || [];

        const rsTsRows  = ts.filter((r) => r.source !== "athletic_screen");
        const athTsRows = ts.filter((r) => r.source === "athletic_screen");

        const jhTraces = [];
        if (rsTsRows.length) {
            jhTraces.push({
                type: "scatter", mode: "lines+markers",
                x: rsTsRows.map((r) => fmtDate(r.date)),
                y: rsTsRows.map((r) => r.jump_height),
                line: { color: "#2c99d4", width: 2 }, marker: { size: 8 },
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

        const fvTraces = [
            {
                type: "scattergl", mode: "markers",
                x: peers.map((p) => p.force_at_pp),
                y: peers.map((p) => p.vel_at_pp),
                marker: { color: "rgba(139, 233, 253, 0.35)", size: 6, line: { width: 0 } },
                name: "Peers", hoverinfo: "skip",
            },
            {
                type: "scatter", mode: "markers",
                x: scatter.map((p) => p.force_at_pp),
                y: scatter.map((p) => p.vel_at_pp),
                marker: {
                    size: 12, color: scatter.map((_, i) => i),
                    colorscale: "Viridis", showscale: false,
                    line: { width: 1, color: "#0f1419" },
                },
                text: scatter.map((p) => fmtDate(p.date)),
                name: "This athlete",
                hovertemplate: "%{text}<br>F: %{x:.1f} N<br>V: %{y:.2f} m/s<extra></extra>",
            },
        ];
        Plotly.react(`${kind}-fv-plot`, fvTraces, {
            ...layoutBase,
            title: { text: "Force vs Velocity at peak power", font: { size: 13 }, x: 0, xanchor: "left" },
            xaxis: { ...layoutBase.xaxis, title: "Force at PP (N)" },
            yaxis: { ...layoutBase.yaxis, title: "Velocity at PP (m/s)" },
        }, config);

        const grid = document.getElementById(`${kind}-stats`);
        grid.innerHTML = "";
        grid.appendChild(latestPrevDelta("Jump height (in)", ts.map((r) => r.jump_height)));
        grid.appendChild(latestPrevDelta("W/kg",             ts.map((r) => r.pp_w_per_kg)));
        grid.appendChild(latestPrevDelta("F @ PP (N)",       ts.map((r) => r.force_at_pp)));
        grid.appendChild(latestPrevDelta("V @ PP (m/s)",     ts.map((r) => r.vel_at_pp)));
    }

    // ─── Movement Strategy ─────────────────────────────────────────────────
    function renderMovementStrategy(kind, series) {
        const xs = series.map((r) => fmtDate(r.date));
        const smallLayout = {
            ...layoutBase,
            margin: { l: 46, r: 12, t: 30, b: 36 },
            xaxis: { ...layoutBase.xaxis, type: "category" },
        };

        function miniLine(col, elId, title, yTitle, color) {
            const ys = series.map((r) => r[col]);
            Plotly.react(elId, [{
                type: "scatter", mode: "lines+markers", x: xs, y: ys,
                line: { color: color || "#2c99d4", width: 2 }, marker: { size: 6 },
                hovertemplate: `%{x}<br>${escape(yTitle)}: %{y:.3f}<extra></extra>`,
            }], {
                ...smallLayout,
                title: { text: escape(title), font: { size: 12 }, x: 0, xanchor: "left" },
                yaxis: { ...smallLayout.yaxis, title: yTitle },
            }, config);
        }

        miniLine("mrsi",                  `${kind}-mrsi-plot`,   "mRSI",                       "m·s⁻¹", "#50fa7b");
        miniLine("contraction_time_s",    `${kind}-ct-plot`,     "Contraction time",            "s",     "#ffb86c");

        if (kind === "cmj") {
            miniLine("ecc_con_duration_ratio",  "cmj-ecccon-plot", "Ecc:Con duration ratio", "ratio", "#ff79c6");
            miniLine("eccentric_mean_power_w",  "cmj-eccpow-plot", "Eccentric mean power",   "W",     "#8be9fd");
        }
    }

    // ─── Trial Consistency ─────────────────────────────────────────────────
    function renderTrialConsistency(intra) {
        const el = document.getElementById("trial-consistency-plot");
        const cmjSessions = intra?.cmj || [];
        const ppuSessions = intra?.ppu || [];

        if (!cmjSessions.length && !ppuSessions.length) {
            el.innerHTML = `<div class="text-muted" style="padding: 1rem 0; font-size: 0.85rem;">No multi-trial data yet.</div>`;
            return;
        }

        // Show jump height trial pairs per day as grouped bars.
        const allDates = [...new Set([...cmjSessions.map((s) => s.date), ...ppuSessions.map((s) => s.date)])].sort();
        const cmjByDate = Object.fromEntries(cmjSessions.map((s) => [s.date, s.trials]));
        const ppuByDate = Object.fromEntries(ppuSessions.map((s) => [s.date, s.trials]));

        const traces = [];
        // CMJ trial 1 and trial 2 jump heights.
        traces.push({
            type: "bar", name: "CMJ T1",
            x: allDates.map(fmtDate),
            y: allDates.map((d) => cmjByDate[d]?.[0]?.jump_height ?? null),
            marker: { color: "#50fa7b" },
            hovertemplate: "%{x}<br>CMJ T1 JH: %{y:.2f} in<extra></extra>",
        });
        traces.push({
            type: "bar", name: "CMJ T2",
            x: allDates.map(fmtDate),
            y: allDates.map((d) => cmjByDate[d]?.[1]?.jump_height ?? null),
            marker: { color: "#22c55e" },
            hovertemplate: "%{x}<br>CMJ T2 JH: %{y:.2f} in<extra></extra>",
        });
        traces.push({
            type: "bar", name: "PPU T1",
            x: allDates.map(fmtDate),
            y: allDates.map((d) => ppuByDate[d]?.[0]?.jump_height ?? null),
            marker: { color: "#ffb86c" },
            hovertemplate: "%{x}<br>PPU T1 JH: %{y:.2f} in<extra></extra>",
        });
        traces.push({
            type: "bar", name: "PPU T2",
            x: allDates.map(fmtDate),
            y: allDates.map((d) => ppuByDate[d]?.[1]?.jump_height ?? null),
            marker: { color: "#f59e0b" },
            hovertemplate: "%{x}<br>PPU T2 JH: %{y:.2f} in<extra></extra>",
        });

        Plotly.react(el, traces, {
            ...layoutBase,
            barmode: "group",
            xaxis: { ...layoutBase.xaxis, type: "category" },
            yaxis: { ...layoutBase.yaxis, title: "Jump height (in)" },
            legend: { ...layoutBase.legend, orientation: "h", y: -0.2 },
        }, config);
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
        const xs_cmj = cmj.map((r) => fmtDate(r.date));
        const xs_ppu = ppu.map((r) => fmtDate(r.date));
        const traces = [];

        if (cmj.length) traces.push({
            type: "scatter", mode: "lines+markers", x: xs_cmj, y: cmj.map((r) => r.peak_power_w),
            line: { color: "#50fa7b" }, marker: { size: 7 }, name: "CMJ peak power (W)", yaxis: "y",
            hovertemplate: "%{x}<br>Peak: %{y:.0f} W<extra>CMJ</extra>",
        });
        if (ppu.length) traces.push({
            type: "scatter", mode: "lines+markers", x: xs_ppu, y: ppu.map((r) => r.peak_power_w),
            line: { color: "#ffb86c" }, marker: { size: 7 }, name: "PPU peak power (W)", yaxis: "y",
            hovertemplate: "%{x}<br>Peak: %{y:.0f} W<extra>PPU</extra>",
        });
        if (cmj.length) traces.push({
            type: "scatter", mode: "lines+markers", x: xs_cmj, y: cmj.map((r) => r.auc_j),
            line: { color: "#50fa7b", dash: "longdash" }, marker: { size: 5 },
            name: "CMJ AUC (J)", yaxis: "y", visible: "legendonly",
            hovertemplate: "%{x}<br>AUC: %{y:.0f} J<extra>CMJ</extra>",
        });
        if (ppu.length) traces.push({
            type: "scatter", mode: "lines+markers", x: xs_ppu, y: ppu.map((r) => r.auc_j),
            line: { color: "#ffb86c", dash: "longdash" }, marker: { size: 5 },
            name: "PPU AUC (J)", yaxis: "y", visible: "legendonly",
            hovertemplate: "%{x}<br>AUC: %{y:.0f} J<extra>PPU</extra>",
        });
        if (cmj.length) {
            traces.push({ type: "scatter", mode: "lines+markers", x: xs_cmj, y: cmj.map((r) => r.rpd_max),
                line: { color: "#bd93f9", dash: "dot" }, marker: { size: 6 }, name: "CMJ RPD max (W/s)", yaxis: "y2",
                hovertemplate: "%{x}<br>RPD: %{y:.0f} W/s<extra>CMJ</extra>" });
            traces.push({ type: "scatter", mode: "lines+markers", x: xs_cmj, y: cmj.map((r) => r.rise_slope),
                line: { color: "#8be9fd", dash: "dot" }, marker: { size: 5 }, name: "CMJ rise slope (W/s)", yaxis: "y2", visible: "legendonly",
                hovertemplate: "%{x}<br>Rise: %{y:.0f} W/s<extra>CMJ</extra>" });
        }
        if (ppu.length) {
            traces.push({ type: "scatter", mode: "lines+markers", x: xs_ppu, y: ppu.map((r) => r.rpd_max),
                line: { color: "#ff79c6", dash: "dot" }, marker: { size: 6 }, name: "PPU RPD max (W/s)", yaxis: "y2",
                hovertemplate: "%{x}<br>RPD: %{y:.0f} W/s<extra>PPU</extra>" });
        }
        if (cmj.length) {
            traces.push({ type: "scatter", mode: "lines+markers", x: xs_cmj, y: cmj.map((r) => r.fwhm),
                line: { color: "#f1fa8c" }, marker: { size: 5 }, name: "CMJ FWHM (s)", yaxis: "y3", visible: "legendonly" });
            traces.push({ type: "scatter", mode: "lines+markers", x: xs_cmj, y: cmj.map((r) => r.decay),
                line: { color: "#f1fa8c", dash: "dash" }, marker: { size: 5 }, name: "CMJ decay 90→10 (s)", yaxis: "y3", visible: "legendonly" });
        }
        if (ppu.length) {
            traces.push({ type: "scatter", mode: "lines+markers", x: xs_ppu, y: ppu.map((r) => r.fwhm),
                line: { color: "#ffa07a" }, marker: { size: 5 }, name: "PPU FWHM (s)", yaxis: "y3", visible: "legendonly" });
        }

        Plotly.react(el, traces, {
            ...layoutBase,
            xaxis:  { ...layoutBase.xaxis, type: "category", domain: [0, 0.86] },
            yaxis:  { ...layoutBase.yaxis, title: "Peak power (W)" },
            yaxis2: { ...layoutBase.yaxis, title: "RPD / rise (W/s)", overlaying: "y", side: "right", showgrid: false },
            yaxis3: { ...layoutBase.yaxis, title: "Duration (s)", overlaying: "y", side: "right", position: 1.0, showgrid: false },
            legend: { ...layoutBase.legend, orientation: "h", y: -0.22 },
            margin: { l: 50, r: 95, t: 28, b: 40 },
        }, config);
    }

    // ─── Metric flag heatmap ──────────────────────────────────────────────
    function renderFlagHeatmap(heatmap) {
        const container = document.getElementById("heatmap-inner");
        if (!heatmap || !heatmap.dates || !heatmap.dates.length) {
            container.innerHTML = `<div class="text-muted" style="padding: 1rem 0; font-size: 0.85rem;">Not enough history for heatmap yet.</div>`;
            return;
        }
        const { dates, metrics, cells } = heatmap;
        const colorMap = { rise: "#4ade80", stable: "#2c99d4", drop: "#f87171", insufficient_history: "#6e7681" };

        // Build HTML table.
        let html = `<table class="heatmap-table"><thead><tr><th class="heatmap-metric-label"></th>`;
        dates.forEach((d) => { html += `<th class="heatmap-date">${fmtDate(d)}</th>`; });
        html += `</tr></thead><tbody>`;
        metrics.forEach((metric, mi) => {
            html += `<tr><td class="heatmap-metric-label">${escape(metric)}</td>`;
            dates.forEach((_, di) => {
                const flag = cells[di]?.[mi] || null;
                const color = flag ? (colorMap[flag] || "#333") : "#1e2837";
                const title = flag || "no data";
                html += `<td class="heatmap-cell" style="background:${color};" title="${escape(title)}"></td>`;
            });
            html += `</tr>`;
        });
        html += `</tbody></table>`;
        container.innerHTML = html;
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
