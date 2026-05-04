// Maintenance page — folder scan, run ingestion via SSE, render summary.
(() => {
    "use strict";

    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    let mode = "auto";
    let selectedAthlete = null;
    let currentJobId = null;
    let currentEventSource = null;

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
            }
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

    // ─── Scan folder ───────────────────────────────────────────────────────
    $("#scan-btn").addEventListener("click", async () => {
        const dir = $("#output-dir").value.trim();
        if (!dir) return;
        $("#scan-btn").disabled = true;
        try {
            const res = await fetch(`/api/scan?dir=${encodeURIComponent(dir)}`);
            const json = await res.json();
            const found = json.files || {};
            $$("#file-grid .file-tile").forEach((tile) => {
                const m = tile.dataset.movement;
                const status = tile.querySelector(".status");
                if (found[m]) {
                    tile.classList.remove("missing");
                    tile.classList.add("found");
                    status.textContent = "found";
                    status.style.color = "var(--accent-green)";
                } else {
                    tile.classList.add("missing");
                    tile.classList.remove("found");
                    status.textContent = "not found";
                    status.style.color = "";
                }
            });
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

        const body = {
            output_dir: outputDir,
            power_dir: powerDir || outputDir,
            fs_hz: fsHz,
        };
        if (mode === "existing") {
            if (!selectedAthlete) {
                alert("Pick an existing athlete first, or switch to Auto-detect.");
                return;
            }
            body.athlete_uuid = selectedAthlete.athlete_uuid;
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
                <div class="row tight" style="margin-top: 0.6rem;">
                  ${subZ("CMJ",  s.cmj_z)}
                  ${subZ("PPU",  s.ppu_z)}
                  ${subZ("Iso",  s.iso_z)}
                  ${subZ("Power", s.power_curve_z)}
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
})();
