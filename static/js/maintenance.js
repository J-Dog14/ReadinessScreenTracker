// Maintenance page — folder scan, run ingestion via SSE, render summary.
(() => {
    "use strict";

    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    let mode = "auto";
    let isHitter = false;
    let selectedAthlete = null;
    let currentJobId = null;
    let currentEventSource = null;
    let gripUnit = "lbs";

    // ─── Mode toggle ───────────────────────────────────────────────────────
    $$("#mode-chips .chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            $$("#mode-chips .chip").forEach((c) => c.classList.remove("is-active"));
            chip.classList.add("is-active");
            mode = chip.dataset.mode;
            $("#auto-msg").style.display = mode === "auto" ? "" : "none";
            $("#existing-picker").style.display = mode === "existing" ? "" : "none";
            if (mode === "auto") {
                selectedAthlete = null;
                $("#athlete-selected").textContent = "";
                $("#athlete-history").style.display = "none";
            }
        });
    });

    // ─── Test type (Pitcher / Hitter) ──────────────────────────────────────
    $$("#type-chips .chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            $$("#type-chips .chip").forEach((c) => c.classList.remove("is-active"));
            chip.classList.add("is-active");
            isHitter = chip.dataset.type === "hitter";
            $("#hitter-note").style.display = isHitter ? "" : "none";
            // Mark Y/IR90 tiles as skipped when hitter mode is on.
            $$('#file-grid .file-tile:not([data-dynamic])').forEach((tile) => {
                const m = tile.dataset.movement;
                if (m === "Y" || m === "IR90") {
                    if (isHitter) {
                        tile.classList.remove("found", "missing");
                        tile.classList.add("is-skipped");
                        const status = tile.querySelector(".status");
                        if (status) { status.textContent = "skipped"; status.style.color = "var(--muted)"; }
                    } else {
                        tile.classList.remove("is-skipped");
                        tile.classList.add("missing");
                        const status = tile.querySelector(".status");
                        if (status) { status.textContent = "not scanned"; status.style.color = ""; }
                    }
                }
            });
        });
    });

    // ─── Athlete search ────────────────────────────────────────────────────
    let searchTimer = null;
    $("#athlete-search").addEventListener("input", (e) => {
        clearTimeout(searchTimer);
        const q = e.target.value.trim();
        if (q.length < 2) {
            $("#athlete-results").style.display = "none";
            return;
        }
        searchTimer = setTimeout(async () => {
            const res = await fetch(`/api/athletes/search?q=${encodeURIComponent(q)}`);
            const json = await res.json();
            const box = $("#athlete-results");
            box.innerHTML = "";
            if (!json.results || !json.results.length) {
                box.innerHTML = `<div class="item text-muted">No matches</div>`;
            } else {
                json.results.forEach((a) => {
                    const div = document.createElement("div");
                    div.className = "item";
                    div.innerHTML = `${escapeHtml(a.name)} <span class="age-tag">${a.age_group || ""}</span>`;
                    div.addEventListener("click", () => {
                        selectedAthlete = a;
                        $("#athlete-search").value = a.name;
                        $("#athlete-selected").innerHTML = `Selected: <strong>${escapeHtml(a.name)}</strong> <span class="mono text-muted">${a.athlete_uuid}</span>`;
                        box.style.display = "none";
                        if (a.dominant_hand) {
                            $("#grip-dominant").value = a.dominant_hand;
                        }
                        loadAthleteHistory(a.athlete_uuid, a.name);
                    });
                    box.appendChild(div);
                });
            }
            box.style.display = "block";
        }, 200);
    });

    document.addEventListener("click", (e) => {
        if (!e.target.closest(".search-dropdown")) {
            $("#athlete-results").style.display = "none";
        }
    });

    // ─── Grip derived display ─────────────────────────────────────────────
    ["grip-left", "grip-right"].forEach((id) => {
        $("#" + id).addEventListener("input", updateGripDerived);
    });

    function updateGripDerived() {
        const LBS_PER_KG = 2.2046226;
        const lRaw = parseFloat($("#grip-left").value);
        const rRaw = parseFloat($("#grip-right").value);
        const derived = $("#grip-derived");
        if (isNaN(lRaw) && isNaN(rRaw)) { derived.style.display = "none"; return; }
        const lKg = isNaN(lRaw) ? null : (gripUnit === "lbs" ? lRaw / LBS_PER_KG : lRaw);
        const rKg = isNaN(rRaw) ? null : (gripUnit === "lbs" ? rRaw / LBS_PER_KG : rRaw);
        if (lKg !== null && rKg !== null) {
            const avg = (lKg + rKg) / 2;
            const max = Math.max(lKg, rKg);
            const asym = max > 0 ? (100 * Math.abs(lKg - rKg) / max).toFixed(1) : "0.0";
            $("#grip-avg").textContent = avg.toFixed(1);
            $("#grip-max").textContent = max.toFixed(1);
            $("#grip-asym").textContent = asym;
            derived.style.display = "";
        } else {
            derived.style.display = "none";
        }
    }

    // ─── Scan folder ───────────────────────────────────────────────────────
    async function doScan(dir) {
        const res = await fetch(`/api/scan?dir=${encodeURIComponent(dir)}`);
        const json = await res.json();
        const found = json.files || {};
        // Update static tiles (Y, IR90).
        $$('#file-grid .file-tile:not([data-dynamic])').forEach((tile) => {
            const m = tile.dataset.movement;
            if (!m) return;
            const status = tile.querySelector(".status");
            const nameSpan = tile.querySelector(".athlete-name");
            // In hitter mode Y/IR90 are always skipped regardless of what was found.
            if (isHitter && (m === "Y" || m === "IR90")) {
                tile.classList.remove("found", "missing");
                tile.classList.add("is-skipped");
                if (status) { status.textContent = "skipped"; status.style.color = "var(--muted)"; }
                if (nameSpan) nameSpan.textContent = "";
                return;
            }
            if (found[m]) {
                tile.classList.remove("missing", "is-skipped"); tile.classList.add("found");
                status.textContent = "found"; status.style.color = "var(--accent-green)";
                if (nameSpan) nameSpan.textContent = found[m].athlete_name || "";
            } else {
                tile.classList.add("missing"); tile.classList.remove("found", "is-skipped");
                status.textContent = "not found"; status.style.color = "";
                if (nameSpan) nameSpan.textContent = "";
            }
        });
        // Update dynamic tiles (CMJ, PPU) — check for at least one matching key.
        $$('#file-grid .file-tile[data-dynamic]').forEach((tile) => {
            const m = tile.dataset.movement;
            const status = tile.querySelector(".status");
            const nameSpan = tile.querySelector(".athlete-name");
            const matches = Object.keys(found).filter((k) => k.startsWith(m));
            if (matches.length) {
                tile.classList.remove("missing"); tile.classList.add("found");
                status.textContent = `${matches.length} trial(s)`; status.style.color = "var(--accent-green)";
                // Show athlete name from first matching trial (they should all be the same athlete).
                const first = found[matches[0]];
                if (nameSpan) nameSpan.textContent = first?.athlete_name || "";
            } else {
                tile.classList.add("missing"); tile.classList.remove("found");
                status.textContent = "not found"; status.style.color = "";
                if (nameSpan) nameSpan.textContent = "";
            }
        });
        // Auto-populate grip dominant hand from the detected athlete's DB record.
        // Takes the first non-null athlete name from any discovered file, resolves
        // it using the same normalize→exact→fuzzy logic as the pipeline, and sets
        // the dominant hand dropdown if the athlete has a prior grip entry.
        const detectedName = Object.values(found).map((f) => f?.athlete_name).find((n) => n);
        if (detectedName) {
            try {
                const lr = await fetch(`/api/athletes/lookup?name=${encodeURIComponent(detectedName)}`);
                const lj = await lr.json();
                if (lj.athlete?.dominant_hand) {
                    $("#grip-dominant").value = lj.athlete.dominant_hand;
                }
            } catch (_) { /* non-fatal */ }
        }

        return found;
    }

    $("#scan-btn").addEventListener("click", async () => {
        const dir = $("#output-dir").value.trim();
        if (!dir) return;
        $("#scan-btn").disabled = true;
        try {
            await doScan(dir);
        } catch (e) {
            alert("Scan failed: " + e);
        } finally {
            $("#scan-btn").disabled = false;
        }
    });

    // ─── Run ───────────────────────────────────────────────────────────────
    $("#run-btn").addEventListener("click", () => startRun());
    $("#kill-btn").addEventListener("click", () => killRun());

    async function startRun() {
        const outputDir = $("#output-dir").value.trim();
        const powerDir = $("#power-dir").value.trim();
        const fsHz = parseFloat($("#fs-hz").value) || 1000;
        if (!outputDir) {
            alert("Set the output folder path first.");
            return;
        }

        // In auto mode, scan first so tiles show athlete names before the run begins.
        if (mode === "auto") {
            try { await doScan(outputDir); } catch (_) { /* non-fatal */ }
        }

        const body = {
            output_dir: outputDir,
            power_dir: powerDir || outputDir,
            fs_hz: fsHz,
            is_hitter: isHitter,
        };
        if (mode === "existing") {
            if (!selectedAthlete) {
                alert("Pick an existing athlete first, or switch to Auto-detect.");
                return;
            }
            body.athlete_uuid = selectedAthlete.athlete_uuid;
        }

        // Grip payload.
        const LBS_PER_KG = 2.2046226;
        const lRaw = $("#grip-left").value.trim();
        const rRaw = $("#grip-right").value.trim();
        const lVal = lRaw !== "" ? parseFloat(lRaw) : null;
        const rVal = rRaw !== "" ? parseFloat(rRaw) : null;

        if ((lVal !== null) !== (rVal !== null)) {
            const missing = lVal === null ? "left" : "right";
            if (!confirm(`Only the ${missing} hand was entered. Submit anyway with one hand missing?`)) {
                return;
            }
        }
        if (lVal !== null || rVal !== null) {
            const toKg = (v) => v === null ? null : (gripUnit === "lbs" ? v / LBS_PER_KG : v);
            body.grip = {
                left_kg:       toKg(lVal),
                right_kg:      toKg(rVal),
                dominant_hand: $("#grip-dominant").value || null,
                notes:         $("#grip-notes").value.trim() || null,
            };
        }

        $("#run-btn").disabled = true;
        $("#kill-btn").style.display = "";
        $("#terminal").style.display = "";
        $("#terminal").innerHTML = "";
        $("#progress").style.display = "";
        $("#progress-label").textContent = "Starting…";
        $("#result-summary").style.display = "none";
        $("#result-summary").innerHTML = "";

        try {
            const res = await fetch("/api/run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const json = await res.json();
            currentJobId = json.job_id;
            attachStream(currentJobId);
        } catch (e) {
            appendLine("ERROR", "Failed to start: " + e);
            finishRun();
        }
    }

    function attachStream(jobId) {
        currentEventSource = new EventSource(`/api/stream/${jobId}`);
        currentEventSource.addEventListener("log", (e) => {
            const data = JSON.parse(e.data);
            appendLine(data.stage, data.msg);
        });
        currentEventSource.addEventListener("done", (e) => {
            const summary = JSON.parse(e.data);
            renderSummary(summary);
            finishRun();
        });
        currentEventSource.addEventListener("error", () => {
            // Either real error or natural end-of-stream — finish either way.
            finishRun();
        });
    }

    function killRun() {
        if (!currentJobId) return;
        fetch(`/api/kill/${currentJobId}`, { method: "POST" });
    }

    function finishRun() {
        if (currentEventSource) {
            currentEventSource.close();
            currentEventSource = null;
        }
        currentJobId = null;
        $("#run-btn").disabled = false;
        $("#kill-btn").style.display = "none";
        $("#progress-label").textContent = "Idle";
    }

    // ─── Output rendering ─────────────────────────────────────────────────
    function appendLine(stage, msg) {
        const term = $("#terminal");
        const line = document.createElement("div");
        const stageLower = (stage || "").toLowerCase().replace(/[^a-z]/g, "");
        line.innerHTML = `<span class="stage-${stageLower}">[${escapeHtml(stage)}]</span> ${escapeHtml(msg)}`;
        term.appendChild(line);
        term.scrollTop = term.scrollHeight;
        $("#progress-label").textContent = `${stage}: ${msg.slice(0, 80)}`;
    }

    function renderSummary(summary) {
        const div = $("#result-summary");
        if (!summary || (!summary.scores?.length && !summary.rows_inserted && !summary.rows_updated)) {
            div.innerHTML = `<div class="text-muted">No new data ingested.</div>`;
            div.style.display = "";
            return;
        }
        const scoreCards = (summary.scores || []).map((s) => {
            const band = s.band || "INSUFFICIENT_HISTORY";
            const badgeClass =
                band === "READY" ? "green" :
                band === "CAUTION" ? "yellow" :
                band === "FATIGUED" ? "red" : "gray";
            const score = s.composite_score == null ? "—" : s.composite_score.toFixed(1);
            return `
              <div class="card" style="margin-bottom: 0.6rem;">
                <div style="display:flex; align-items:center; justify-content:space-between; gap:1rem;">
                  <div>
                    <div style="font-weight:600; font-size: 1rem;">${escapeHtml(s.name || s.athlete_uuid)}</div>
                    <div class="text-muted" style="font-size: 0.83rem;">${escapeHtml(s.session_date)} · ${s.metrics_used} metrics</div>
                  </div>
                  <div style="display:flex; gap:0.5rem; align-items:center;">
                    <span class="mono" style="font-size: 1.5rem; font-weight: 700;">${score}</span>
                    <span class="badge ${badgeClass}">${band}</span>
                  </div>
                </div>
                <div class="row tight" style="margin-top: 0.6rem; flex-wrap: wrap;">
                  ${subZ("CMJ",  s.cmj_z)}
                  ${subZ("PPU",  s.ppu_z)}
                  ${subZ("Iso",  s.iso_z)}
                  ${subZ("Power", s.power_curve_z)}
                  ${subZ("Grip",  s.grip_z)}
                </div>
                <div style="margin-top: 0.6rem;">
                  <a href="/dashboard?athlete=${encodeURIComponent(s.athlete_uuid)}" class="btn btn-ghost">View dashboard →</a>
                </div>
              </div>
            `;
        }).join("");

        const head = `
          <div class="card" style="background: var(--bg-tertiary);">
            <div class="row" style="gap: 1.5rem;">
              <div><div class="text-muted" style="font-size:0.78rem;">Inserted</div><div class="mono" style="font-size:1.2rem;">${summary.rows_inserted}</div></div>
              <div><div class="text-muted" style="font-size:0.78rem;">Updated</div><div class="mono" style="font-size:1.2rem;">${summary.rows_updated}</div></div>
              <div><div class="text-muted" style="font-size:0.78rem;">Power-curve rows</div><div class="mono" style="font-size:1.2rem;">${summary.power_curve_rows}</div></div>
              <div><div class="text-muted" style="font-size:0.78rem;">Athletes</div><div class="mono" style="font-size:1.2rem;">${(summary.athletes || []).length}</div></div>
            </div>
            ${summary.errors?.length ? `<div style="color: var(--accent-red); margin-top:0.5rem; font-size:0.85rem;">${summary.errors.length} error(s) — see log above</div>` : ""}
          </div>
        `;

        div.innerHTML = head + scoreCards;
        div.style.display = "";
    }

    function subZ(label, z) {
        if (z == null) {
            return `<div class="stat" style="flex:1; min-width:120px;"><div class="stat-label">${label} z</div><div class="stat-value text-muted">—</div></div>`;
        }
        const cls = z >= 0.6 ? "up" : z <= -0.6 ? "down" : "flat";
        const sign = z > 0 ? "+" : "";
        return `<div class="stat" style="flex:1; min-width:120px;"><div class="stat-label">${label} z</div><div class="stat-value">${sign}${z.toFixed(2)}</div><div class="stat-delta ${cls}">${cls === "up" ? "above baseline" : cls === "down" ? "below baseline" : "stable"}</div></div>`;
    }

    function escapeHtml(s) {
        if (s == null) return "";
        return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
    }

    // ─── Athlete history charts ───────────────────────────────────────────
    async function loadAthleteHistory(uuid, name) {
        const panel = $("#athlete-history");
        const empty = $("#hist-empty");
        panel.style.display = "";
        empty.style.display = "none";
        $("#history-athlete-name").textContent = name;
        $("#hist-score").innerHTML = `<p style="color:var(--muted);font-size:0.8rem;padding:1rem">Loading…</p>`;
        ["hist-cmj", "hist-ppu", "hist-iso"].forEach(id => { const el = $(`#${id}`); if (el) el.innerHTML = ""; });

        try {
            const res = await fetch(`/api/dashboard/athlete/${uuid}`);
            const d = await res.json();

            const hasHistory = d.score_history?.length || d.cmj?.timeseries?.length || d.ppu?.timeseries?.length;
            if (!hasHistory) {
                empty.style.display = "";
                $("#hist-score").innerHTML = "";
                return;
            }

            if (d.score_history?.length) {
                const dates  = d.score_history.map(r => r.date);
                const scores = d.score_history.map(r => r.composite_score ?? null);
                const ends   = [dates[0], dates[dates.length - 1]];
                Plotly.newPlot("hist-score", [
                    { x: dates, y: scores, type: "scatter", mode: "lines+markers",
                      line: { color: "#2c99d4", width: 2 }, marker: { size: 7 }, name: "Score" },
                    { x: ends, y: [60, 60], mode: "lines", hoverinfo: "skip",
                      line: { color: "#27ae60", width: 1, dash: "dash" }, showlegend: false },
                    { x: ends, y: [40, 40], mode: "lines", hoverinfo: "skip",
                      line: { color: "#e67e22", width: 1, dash: "dash" }, showlegend: false },
                ], miniLayout("Composite Score", "Score", [0, 100]), { responsive: true, displayModeBar: false });
            }

            if (d.cmj?.timeseries?.length) {
                const ts = d.cmj.timeseries;
                Plotly.newPlot("hist-cmj", [
                    { x: ts.map(r => r.date), y: ts.map(r => r.jump_height ?? null),
                      type: "scatter", mode: "lines+markers",
                      line: { color: "#9b59b6", width: 2 }, marker: { size: 7 }, name: "JH (in)" },
                ], miniLayout("CMJ Jump Height", "inches"), { responsive: true, displayModeBar: false });
            }

            if (d.ppu?.timeseries?.length) {
                const ts = d.ppu.timeseries;
                Plotly.newPlot("hist-ppu", [
                    { x: ts.map(r => r.date), y: ts.map(r => r.jump_height ?? null),
                      type: "scatter", mode: "lines+markers",
                      line: { color: "#e67e22", width: 2 }, marker: { size: 7 }, name: "JH (in)" },
                ], miniLayout("PPU Jump Height", "inches"), { responsive: true, displayModeBar: false });
            }

            const isoTraces = [];
            const palette = { Y: "#27ae60", IR90: "#2c99d4" };
            ["Y", "IR90"].forEach(mv => {
                const grp = d.iso?.[mv];
                if (grp?.data?.length) {
                    isoTraces.push({
                        x: grp.data.map(r => r.date), y: grp.data.map(r => r.avg_force ?? null),
                        type: "scatter", mode: "lines+markers",
                        line: { color: palette[mv], width: 2 }, marker: { size: 7 }, name: mv,
                    });
                }
            });
            if (isoTraces.length) {
                Plotly.newPlot("hist-iso", isoTraces,
                    miniLayout("ISO (Y / IR90)", "Avg Force (N)"),
                    { responsive: true, displayModeBar: false });
            }

        } catch (_) {
            $("#hist-score").innerHTML = `<p style="color:var(--danger);font-size:0.8rem;padding:1rem">Failed to load history.</p>`;
        }
    }

    function miniLayout(title, yTitle, yRange) {
        return {
            title: { text: title, font: { size: 12 }, x: 0.03 },
            margin: { l: 44, r: 12, t: 28, b: 36 },
            xaxis: { type: "category", tickfont: { size: 10 } },
            yaxis: {
                title: yTitle, titlefont: { size: 10 }, tickfont: { size: 10 },
                ...(yRange ? { range: yRange } : {}),
            },
            paper_bgcolor: "transparent",
            plot_bgcolor:  "transparent",
            showlegend: true,
            legend: { font: { size: 10 }, orientation: "h", y: -0.25 },
        };
    }

    // ─── Grip Log ─────────────────────────────────────────────────────────
    let glAthlete = null;
    let glSearchTimer = null;

    // Default date input to today.
    const glDateInput = $("#gl-date");
    if (glDateInput) {
        const today = new Date();
        const yyyy = today.getFullYear();
        const mm   = String(today.getMonth() + 1).padStart(2, "0");
        const dd   = String(today.getDate()).padStart(2, "0");
        glDateInput.value = `${yyyy}-${mm}-${dd}`;
    }

    $("#gl-athlete-search").addEventListener("input", (e) => {
        clearTimeout(glSearchTimer);
        const q = e.target.value.trim();
        if (q.length < 2) { $("#gl-athlete-results").style.display = "none"; return; }
        glSearchTimer = setTimeout(async () => {
            const res  = await fetch(`/api/athletes/search?q=${encodeURIComponent(q)}`);
            const json = await res.json();
            const box  = $("#gl-athlete-results");
            box.innerHTML = "";
            if (!json.results || !json.results.length) {
                box.innerHTML = `<div class="item text-muted">No matches</div>`;
            } else {
                json.results.forEach((a) => {
                    const div = document.createElement("div");
                    div.className = "item";
                    div.innerHTML = `${escapeHtml(a.name)} <span class="age-tag">${a.age_group || ""}</span>`;
                    div.addEventListener("click", () => {
                        glAthlete = a;
                        $("#gl-athlete-search").value = a.name;
                        $("#gl-athlete-selected").innerHTML =
                            `Selected: <strong>${escapeHtml(a.name)}</strong> <span class="mono text-muted">${a.athlete_uuid}</span>`;
                        box.style.display = "none";
                        if (a.dominant_hand) { $("#gl-dominant").value = a.dominant_hand; }
                    });
                    box.appendChild(div);
                });
            }
            box.style.display = "block";
        }, 200);
    });

    document.addEventListener("click", (e) => {
        if (!e.target.closest("#gl-athlete-results") && !e.target.closest("#gl-athlete-search")) {
            $("#gl-athlete-results").style.display = "none";
        }
    });

    $("#gl-save-btn").addEventListener("click", async () => {
        const statusEl = $("#gl-status");
        if (!glAthlete) {
            statusEl.textContent = "Select an athlete first.";
            statusEl.style.color = "var(--accent-red)";
            statusEl.style.display = "";
            return;
        }
        const lRaw = $("#gl-left").value.trim();
        const rRaw = $("#gl-right").value.trim();
        const lVal = lRaw !== "" ? parseFloat(lRaw) : null;
        const rVal = rRaw !== "" ? parseFloat(rRaw) : null;
        if (lVal === null && rVal === null) {
            statusEl.textContent = "Enter at least one grip value.";
            statusEl.style.color = "var(--accent-red)";
            statusEl.style.display = "";
            return;
        }
        const LBS_PER_KG = 2.2046226;
        const toKg = (v) => v === null ? null : v / LBS_PER_KG;
        const payload = {
            athlete_uuid:  glAthlete.athlete_uuid,
            session_date:  $("#gl-date").value || null,
            left_kg:       toKg(lVal),
            right_kg:      toKg(rVal),
            dominant_hand: $("#gl-dominant").value || null,
            notes:         $("#gl-notes").value.trim() || null,
        };

        $("#gl-save-btn").disabled = true;
        statusEl.textContent = "Saving…";
        statusEl.style.color = "var(--muted)";
        statusEl.style.display = "";
        try {
            const res  = await fetch("/api/grip-log", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const json = await res.json();
            if (!res.ok || json.error) {
                statusEl.textContent = "Error: " + (json.error || res.status);
                statusEl.style.color = "var(--accent-red)";
            } else {
                const verb = json.verb === "inserted" ? "Saved" : "Updated";
                statusEl.textContent = `${verb} grip for ${escapeHtml(json.athlete_name)} on ${json.date}.`;
                statusEl.style.color = "var(--accent-green)";
                // Clear fields on success.
                $("#gl-left").value  = "";
                $("#gl-right").value = "";
                $("#gl-notes").value = "";
            }
        } catch (e) {
            statusEl.textContent = "Request failed: " + e;
            statusEl.style.color = "var(--accent-red)";
        } finally {
            $("#gl-save-btn").disabled = false;
        }
    });
})();
