/**
 * File Duplicator – Web UI logic (v2)
 *
 * Major improvements over v1:
 *  - Paginated group loading (50 at a time) – no more browser crash
 *  - Group-level context menu: Mark All Delete / Keep All / Keep Oldest
 *  - Groups sorted by size descending (biggest waste first)
 *  - All groups expanded by default
 */

// ── State ──
let currentJobId = null;
let eventSource = null;
let loadedGroupCount = 0;
let totalGroupCount = 0;
let summaryData = null;
let currentSort = "size_desc";
let currentSearch = "";
let scanStartTime = null;    // for elapsed timer
let elapsedInterval = null;
let PAGE_SIZE = 50;

// ── Multi-select state ──
let lastClickedRow = null;    // for shift+click range selection on file rows
let selectedGroupDivs = [];   // selected group headers for bulk operations

// ── Multi-directory helpers ──
const scanDirs = [];   // list of selected directories
const dirList = null;  // populated after DOM refs

function renderDirList() {
    const el = $("#dirList");
    if (!el) return;
    el.innerHTML = "";
    if (scanDirs.length === 0) {
        el.innerHTML = '<div class="list-group-item bg-dark text-muted py-1" style="font-size:.85rem">(no directories added yet)</div>';
        return;
    }
    scanDirs.forEach((d, i) => {
        const item = document.createElement("div");
        item.className = "list-group-item bg-dark text-light d-flex align-items-center py-1";
        item.style.fontSize = ".85rem";
        item.innerHTML = `<i class="bi bi-folder-fill text-warning me-2"></i>
            <span class="flex-grow-1 text-truncate">${escHtml(d)}</span>
            <button class="btn btn-sm btn-outline-danger ms-2 py-0 px-1" title="Remove">
                <i class="bi bi-x-lg"></i>
            </button>`;
        item.querySelector("button").addEventListener("click", () => {
            scanDirs.splice(i, 1);
            renderDirList();
        });
        el.appendChild(item);
    });
}

function addDirectory(path) {
    const p = path.trim();
    if (!p) return;
    if (scanDirs.includes(p)) return;  // no duplicates
    scanDirs.push(p);
    renderDirList();
}

// ── DOM refs ──
const $ = (sel) => document.querySelector(sel);
const dirInput = $("#dirInput");
const scanBtn = $("#scanBtn");
const cancelBtn = $("#cancelBtn");
const progressArea = $("#progressArea");
const phaseLabel = $("#phaseLabel");
const progressText = $("#progressText");
const progressBar = $("#progressBar");
const resultsArea = $("#resultsArea");
const summaryBar = $("#summaryBar");
const groupsContainer = $("#groupsContainer");
const loadMoreArea = $("#loadMoreArea");
const loadMoreBtn = $("#loadMoreBtn");
const loadMoreInfo = $("#loadMoreInfo");

// ── Scan ──
scanBtn.addEventListener("click", startScan);
cancelBtn.addEventListener("click", cancelScan);
$("#autoSelectBtn").addEventListener("click", autoSelectNewer);
$("#deselectBtn").addEventListener("click", deselectAll);
$("#expandAllBtn").addEventListener("click", expandAllGroups);
$("#collapseAllBtn").addEventListener("click", collapseAllGroups);
$("#deleteBtn").addEventListener("click", showDeleteConfirm);
$("#confirmDeleteBtn").addEventListener("click", doDelete);
$("#bulkDeleteBtn").addEventListener("click", showBulkDeleteConfirm);
$("#confirmBulkDeleteBtn").addEventListener("click", doBulkDelete);
$("#exportCsvBtn").addEventListener("click", (e) => { e.preventDefault(); exportReport("csv"); });
$("#exportJsonBtn").addEventListener("click", (e) => { e.preventDefault(); exportReport("json"); });
$("#applyRulesBtn").addEventListener("click", applyRules);
loadMoreBtn.addEventListener("click", loadMore);
$("#pageSizeSpin").addEventListener("change", (e) => {
    let v = parseInt(e.target.value, 10);
    if (isNaN(v) || v < 10) v = 10;
    if (v > 5000) v = 5000;
    e.target.value = v;
    PAGE_SIZE = v;
});

// Multi-directory buttons
$("#addDirBtn").addEventListener("click", () => {
    addDirectory(dirInput.value);
    dirInput.value = "";
});
dirInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { addDirectory(dirInput.value); dirInput.value = ""; }
});
$("#clearDirsBtn").addEventListener("click", () => {
    scanDirs.length = 0;
    renderDirList();
});
renderDirList();  // initial render

// ── Directory Rules state ──
const dirRules = [];

function renderRules() {
    const el = $("#rulesList");
    if (!el) return;
    el.innerHTML = "";
    if (dirRules.length === 0) {
        el.innerHTML = '<div class="list-group-item bg-dark text-muted py-1" style="font-size:.85rem">(no rules \u2013 all files treated equally)</div>';
        return;
    }
    dirRules.forEach((r, i) => {
        const icon = r.type === "preserve"
            ? '<i class="bi bi-shield-check text-success me-2"></i>'
            : '<i class="bi bi-trash text-danger me-2"></i>';
        const label = r.type === "preserve" ? "PRESERVE" : "EXPENDABLE";
        const badgeClass = r.type === "preserve" ? "bg-success" : "bg-danger";
        const item = document.createElement("div");
        item.className = "list-group-item bg-dark text-light d-flex align-items-center py-1";
        item.style.fontSize = ".85rem";
        item.innerHTML = `${icon}
            <span class="badge ${badgeClass} me-2" style="font-size:.65rem">${label}</span>
            <span class="flex-grow-1 text-truncate">${escHtml(r.path)}</span>
            <button class="btn btn-sm btn-outline-danger ms-2 py-0 px-1" title="Remove">
                <i class="bi bi-x-lg"></i>
            </button>`;
        item.querySelector("button").addEventListener("click", () => {
            dirRules.splice(i, 1);
            renderRules();
        });
        el.appendChild(item);
    });
}

function addRule(path, type) {
    const p = path.trim();
    if (!p) return;
    if (dirRules.some(r => r.path === p && r.type === type)) return;
    dirRules.push({ path: p, type: type });
    renderRules();
}

function addRuleFromInput(type) {
    const input = $("#ruleInput");
    const path = input.value.trim();
    if (path) {
        addRule(path, type);
        input.value = "";
    } else {
        browseTarget = type;  // "preserve" or "expendable"
        browseTo("/");
        browseModal.show();
    }
}

$("#addPreserveBtn").addEventListener("click", () => addRuleFromInput("preserve"));
$("#addExpendableBtn").addEventListener("click", () => addRuleFromInput("expendable"));
$("#clearRulesBtn").addEventListener("click", () => {
    dirRules.length = 0;
    renderRules();
});
$("#browseRuleBtn").addEventListener("click", () => {
    browseTarget = "ruleInput";
    const initial = $("#ruleInput").value.trim() || "/";
    browseTo(initial);
    browseModal.show();
});

renderRules();  // initial render

// Sort & search
const sortSelect = $("#sortSelect");
const searchInput = $("#searchInput");
const filterInfo = $("#filterInfo");
let searchDebounce = null;

sortSelect.addEventListener("change", () => {
    currentSort = sortSelect.value;
    reloadResults();
});

searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
        currentSearch = searchInput.value.trim();
        reloadResults();
    }, 400);
});

$("#searchClearBtn").addEventListener("click", () => {
    searchInput.value = "";
    currentSearch = "";
    reloadResults();
});

// Compare
$("#compareBtn").addEventListener("click", openCompareModal);
$("#runCompareBtn").addEventListener("click", runCompare);

function reloadResults() {
    if (!currentJobId) return;
    groupsContainer.innerHTML = "";
    loadedGroupCount = 0;
    loadGroups(currentJobId, 0);
}

async function startScan() {
    if (scanDirs.length === 0) return alert("Add at least one directory.");

    scanBtn.disabled = true;
    cancelBtn.disabled = false;
    progressArea.classList.remove("d-none");
    resultsArea.classList.add("d-none");
    loadMoreArea.classList.add("d-none");
    groupsContainer.innerHTML = "";
    loadedGroupCount = 0;
    totalGroupCount = 0;
    summaryData = null;
    currentSearch = "";
    searchInput.value = "";
    sortSelect.value = "size_desc";
    currentSort = "size_desc";
    progressBar.style.width = "0%";
    phaseLabel.textContent = "";
    progressText.textContent = "";

    // Start elapsed timer
    scanStartTime = Date.now();
    startElapsedTimer();

    const body = {
        roots: [...scanDirs],
        by_name: $("#chkName").checked,
        by_size: $("#chkSize").checked,
        by_hash: $("#chkHash").checked,
        recursive: $("#chkRecursive").checked,
        min_size: $("#minSize").value,
        use_sha256: $("#hashAlgo").value === "sha256",
        save_index: $("#chkSaveIndex").checked,
        rules: dirRules.map(r => ({ path: r.path, type: r.type })),
    };

    try {
        const res = await fetch("/api/scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        currentJobId = data.job_id;
        localStorage.setItem("fd_job_id", data.job_id);
        pollProgress(data.job_id);
    } catch (e) {
        alert("Scan failed: " + e.message);
        scanBtn.disabled = false;
        cancelBtn.disabled = true;
    }
}

// ── Auto-reconnect to running/finished job on page load ──
async function tryReconnect() {
    const savedId = localStorage.getItem("fd_job_id");
    if (!savedId) return;

    try {
        const res = await fetch("/api/jobs");
        const jobs = await res.json();
        const job = jobs.find((j) => j.id === savedId);
        if (!job) { localStorage.removeItem("fd_job_id"); return; }

        currentJobId = savedId;

        if (job.status === "running") {
            // Scan is still running – reconnect to SSE
            if (job.roots && job.roots.length) {
                scanDirs.length = 0;
                job.roots.forEach(r => scanDirs.push(r));
                renderDirList();
            }
            scanBtn.disabled = true;
            cancelBtn.disabled = false;
            progressArea.classList.remove("d-none");
            phaseLabel.textContent = `Reconnected · ${job.phase || "running"}…`;
            pollProgress(savedId);
        } else if (job.status === "done") {
            // Scan finished while we were away – show results
            if (job.roots && job.roots.length) {
                scanDirs.length = 0;
                job.roots.forEach(r => scanDirs.push(r));
                renderDirList();
            }
            const progRes = await fetch(`/api/scan/${savedId}/progress`);
            // Read the SSE stream for summary
            const reader = progRes.body.getReader();
            const decoder = new TextDecoder();
            let text = "";
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                text += decoder.decode(value, { stream: true });
            }
            // Parse last SSE data line
            const lines = text.split("\n").filter((l) => l.startsWith("data: "));
            if (lines.length > 0) {
                const d = JSON.parse(lines[lines.length - 1].replace("data: ", ""));
                if (d.summary) {
                    summaryData = d.summary;
                    totalGroupCount = d.summary.group_count;
                    phaseLabel.textContent = `Reconnected · ${totalGroupCount.toLocaleString()} duplicate groups`;
                    progressArea.classList.remove("d-none");
                    progressBar.style.width = "100%";
                    progressBar.classList.remove("progress-bar-animated");
                    showSummary(d.summary);
                    loadGroups(savedId, 0);
                }
            }
        }
    } catch (e) {
        console.warn("Reconnect failed:", e);
    }
}

tryReconnect();

let sseRetries = 0;
const MAX_SSE_RETRIES = 50;  // ~50 retries × 3s = ~2.5 min of retrying

function pollProgress(jobId) {
    if (eventSource) eventSource.close();
    sseRetries = 0;
    _connectSSE(jobId);
}

function _connectSSE(jobId) {
    eventSource = new EventSource(`/api/scan/${jobId}/progress`);
    eventSource.onmessage = (ev) => {
        sseRetries = 0;  // reset on any successful message
        const d = JSON.parse(ev.data);

        phaseLabel.textContent = d.phase || "";
        if (d.total > 0) {
            const pct = Math.round((d.current / d.total) * 100);
            progressBar.style.width = pct + "%";
            progressBar.classList.remove("progress-bar-animated");
            progressText.textContent = `${d.current.toLocaleString()} / ${d.total.toLocaleString()}`;
        } else {
            progressBar.style.width = "100%";
            progressBar.classList.add("progress-bar-animated");
            progressText.textContent = "";
        }

        if (d.status === "done") {
            eventSource.close();
            stopElapsedTimer();
            summaryData = d.summary;
            totalGroupCount = d.summary.group_count;
            phaseLabel.textContent = `Done · ${totalGroupCount.toLocaleString()} duplicate groups`;
            showSummary(d.summary);
            // Load first page of groups
            loadGroups(jobId, 0);
            scanBtn.disabled = false;
            cancelBtn.disabled = true;
        } else if (d.status === "error") {
            eventSource.close();
            alert("Scan error: " + (d.error || "Unknown"));
            scanBtn.disabled = false;
            cancelBtn.disabled = true;
        }
    };
    eventSource.onerror = () => {
        eventSource.close();
        sseRetries++;
        if (sseRetries <= MAX_SSE_RETRIES) {
            // Connection dropped (common in long scans) — reconnect after a short delay
            console.warn(`SSE connection lost, retrying (${sseRetries}/${MAX_SSE_RETRIES})…`);
            phaseLabel.textContent += " (reconnecting…)";
            setTimeout(() => _connectSSE(jobId), 3000);
        } else {
            // Truly lost — but the scan may still be running server-side
            phaseLabel.textContent = "Connection lost — refresh the page to check results.";
            scanBtn.disabled = false;
            cancelBtn.disabled = true;
        }
    };
}

async function cancelScan() {
    if (!currentJobId) return;
    cancelBtn.disabled = true;
    await fetch(`/api/scan/${currentJobId}/cancel`, { method: "POST" });
    if (eventSource) eventSource.close();
    phaseLabel.textContent = "Cancelled.";
    scanBtn.disabled = false;
}

// ── Summary ──
function showSummary(s) {
    resultsArea.classList.remove("d-none");
    stopElapsedTimer();

    // Scan stats bar (total files scanned, hash algo, elapsed, timestamp)
    const statsBar = $("#scanStatsBar");
    statsBar.classList.remove("d-none");
    let statsHtml = `
        <i class="bi bi-bar-chart me-2"></i>
        Scanned <strong>${s.total_scanned.toLocaleString()}</strong> files
        (<strong>${s.total_scanned_size_h}</strong>)
        &nbsp;•&nbsp; Hash: <strong>${s.hash_algorithm}</strong>
    `;
    if (s.cloud_skipped > 0) {
        statsHtml += ` &nbsp;•&nbsp; ${s.cloud_skipped.toLocaleString()} cloud files skipped`;
    }
    if (s.rules_applied > 0) {
        statsHtml += ` &nbsp;•&nbsp; <i class="bi bi-shield-check"></i> ${s.rules_applied.toLocaleString()} files auto-marked by rules`;
    }
    if (s.elapsed_h) {
        statsHtml += ` &nbsp;•&nbsp; <i class="bi bi-stopwatch"></i> ${s.elapsed_h}`;
    }
    if (s.finished_at) {
        const d = new Date(s.finished_at);
        statsHtml += ` &nbsp;•&nbsp; <i class="bi bi-clock"></i> ${d.toLocaleString()}`;
    }
    statsBar.innerHTML = statsHtml;

    // Duplicate summary bar
    summaryBar.innerHTML = `
        <i class="bi bi-info-circle me-2"></i>
        <strong>${s.group_count.toLocaleString()}</strong>&nbsp;duplicate groups &nbsp;•&nbsp;
        <strong>${s.file_count.toLocaleString()}</strong>&nbsp;duplicate files &nbsp;•&nbsp;
        <strong>${s.reclaimable_h}</strong>&nbsp;reclaimable
    `;

    // Show / hide the File Index download button
    const indexBtn = $("#downloadIndexBtn");
    if (s.index_path) {
        indexBtn.href = `/api/scan/${currentJobId}/export/index`;
        indexBtn.classList.remove("d-none");
    } else {
        indexBtn.classList.add("d-none");
    }
}

// ── Paginated group loading ──
async function loadGroups(jobId, offset) {
    try {
        loadMoreBtn.disabled = true;
        loadMoreBtn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span> Loading…`;

        const params = new URLSearchParams({
            offset: String(offset), limit: String(PAGE_SIZE),
            sort: currentSort,
        });
        if (currentSearch) params.set("search", currentSearch);
        const res = await fetch(`/api/scan/${jobId}/groups?${params}`);
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        for (const g of data.groups) {
            groupsContainer.appendChild(buildGroup(g, true));  // all groups expanded
            loadedGroupCount++;
        }

        // Update load-more area
        if (data.has_more) {
            const remaining = data.total - loadedGroupCount;
            loadMoreArea.classList.remove("d-none");
            loadMoreBtn.disabled = false;
            loadMoreBtn.innerHTML = `<i class="bi bi-arrow-down-circle"></i> Load More`;
            loadMoreInfo.textContent = `Showing ${loadedGroupCount.toLocaleString()} of ${data.total.toLocaleString()} groups (${remaining.toLocaleString()} remaining)`;
        } else {
            loadMoreArea.classList.add("d-none");
            loadMoreInfo.textContent = "";
        }

        // Update filter info
        if (currentSearch) {
            filterInfo.textContent = `Showing ${data.total.toLocaleString()} matching groups`;
        } else {
            filterInfo.textContent = "";
        }
    } catch (e) {
        alert("Failed to load groups: " + e.message);
        loadMoreBtn.disabled = false;
        loadMoreBtn.innerHTML = `<i class="bi bi-arrow-down-circle"></i> Load More`;
    }
}

function loadMore() {
    if (!currentJobId) return;
    loadGroups(currentJobId, loadedGroupCount);
}

// ── Build a single group ──
function buildGroup(group, expanded) {
    const div = document.createElement("div");
    div.className = "dup-group";
    div.dataset.groupIndex = group.index;

    // Header
    const header = document.createElement("div");
    header.className = "dup-group-header";
    header.innerHTML = `
        <i class="bi ${expanded ? "bi-chevron-down" : "bi-chevron-right"} toggle-icon"></i>
        <span class="badge bg-secondary">${group.mode}</span>
        <span class="group-title">Group ${group.index} &nbsp;–&nbsp; ${group.file_count} files &nbsp;•&nbsp; ${group.each_size_h} each</span>
    `;

    // Left-click: toggle expand/collapse
    header.addEventListener("click", (e) => {
        // Don't toggle if right-click handled
        if (e.button !== 0) return;
        // Ctrl/Cmd+click or Shift+click: toggle group selection instead of expand
        if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            toggleGroupSelection(div);
            return;
        }
        if (e.shiftKey) {
            e.preventDefault();
            shiftSelectGroups(div);
            return;
        }
        const body = div.querySelector(".dup-group-body");
        const icon = header.querySelector(".toggle-icon");
        if (body.style.display === "none") {
            body.style.display = "";
            icon.className = "bi bi-chevron-down toggle-icon";
        } else {
            body.style.display = "none";
            icon.className = "bi bi-chevron-right toggle-icon";
        }
    });

    // Right-click on header: group context menu
    header.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        showGroupContextMenu(e.clientX, e.clientY, div);
    });

    div.appendChild(header);

    // Body table
    const body = document.createElement("div");
    body.className = "dup-group-body";
    if (!expanded) body.style.display = "none";

    let html = `<table class="dup-table">
        <thead><tr>
            <th style="width:40px"></th>
            <th>Action</th>
            <th>File Name</th>
            <th>Path</th>
            <th>Size</th>
            <th class="hash-cell">Hash</th>
        </tr></thead><tbody>`;

    for (let i = 0; i < group.files.length; i++) {
        const f = group.files[i];
        let checked, actionClass, actionText, ruleBadge = "";
        if (f.rule_action === "keep") {
            checked = ""; actionClass = "action-keep"; actionText = "KEEP";
            ruleBadge = ' <span class="badge bg-success rule-badge" title="Preserve rule applied">R</span>';
        } else if (f.rule_action === "delete") {
            checked = "checked"; actionClass = "action-delete"; actionText = "DELETE";
            ruleBadge = ' <span class="badge bg-danger rule-badge" title="Expendable rule applied">R</span>';
        } else if (f.rule_action === "review") {
            checked = ""; actionClass = "action-review"; actionText = "\u26a0 REVIEW";
            ruleBadge = ' <span class="badge bg-warning rule-badge" title="Conflicting rules \u2013 review manually">\u26a0</span>';
        } else {
            checked = f.is_oldest ? "" : "checked";
            actionClass = f.is_oldest ? "action-keep" : "action-delete";
            actionText = f.is_oldest ? "KEEP" : "DELETE";
        }
        html += `<tr data-path="${escHtml(f.path)}" data-group="${group.index}">
            <td><input type="checkbox" class="dup-check" data-path="${escHtml(f.path)}" data-size="${f.size}" ${checked}></td>
            <td class="action-label ${actionClass}">${actionText}${ruleBadge}</td>
            <td>${escHtml(f.name)}</td>
            <td class="path-cell" title="${escHtml(f.path)}">${escHtml(f.path)}</td>
            <td style="white-space:nowrap">${f.size_h}</td>
            <td class="hash-cell" style="font-family:monospace;font-size:0.8rem">${f.hash}</td>
        </tr>`;
    }

    html += "</tbody></table>";
    body.innerHTML = html;
    div.appendChild(body);

    // Wire checkbox → action label
    body.querySelectorAll(".dup-check").forEach((cb) => {
        cb.addEventListener("change", () => {
            const label = cb.closest("tr").querySelector(".action-label");
            if (cb.checked) {
                label.textContent = "DELETE";
                label.className = "action-label action-delete";
            } else {
                label.textContent = "KEEP";
                label.className = "action-label action-keep";
            }
        });
    });

    // Right-click on file rows: file context menu
    body.addEventListener("contextmenu", (e) => {
        const tr = e.target.closest("tr[data-path]");
        if (!tr) return;
        e.preventDefault();
        // If right-clicked row is not in current selection, select just it
        if (!tr.classList.contains("row-selected")) {
            clearRowSelection();
            tr.classList.add("row-selected");
        }
        const selectedRows = Array.from(body.closest("#groupsContainer")
            .querySelectorAll("tr.row-selected[data-path]"));
        showFileContextMenu(e.clientX, e.clientY, tr, selectedRows);
    });

    // Left-click on file rows: selection (Ctrl/Shift)
    body.addEventListener("click", (e) => {
        const tr = e.target.closest("tr[data-path]");
        if (!tr) return;
        // Ignore clicks on checkboxes themselves
        if (e.target.tagName === "INPUT") return;
        if (e.ctrlKey || e.metaKey) {
            tr.classList.toggle("row-selected");
            lastClickedRow = tr;
        } else if (e.shiftKey && lastClickedRow) {
            selectRowRange(lastClickedRow, tr);
        } else {
            clearRowSelection();
            tr.classList.add("row-selected");
            lastClickedRow = tr;
        }
    });

    return div;
}

// ── Group context menu (right-click on header) ──
function showGroupContextMenu(x, y, groupDiv) {
    removeContextMenu();

    // If right-clicked group is not selected, select just it
    if (!selectedGroupDivs.includes(groupDiv)) {
        clearGroupSelection();
        selectGroup(groupDiv);
    }
    const targets = selectedGroupDivs.length > 0 ? [...selectedGroupDivs] : [groupDiv];
    const n = targets.length;
    const suffix = n > 1 ? ` (${n} groups)` : "";

    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    menu.style.left = x + "px";
    menu.style.top = y + "px";

    // Mark ALL as DELETE
    const delAll = menuItem("bi-trash3", `Mark ALL as DELETE${suffix}`, "text-danger");
    delAll.addEventListener("click", () => {
        targets.forEach(g => setGroupAction(g, "delete"));
        removeContextMenu();
    });
    menu.appendChild(delAll);

    // Mark ALL as KEEP
    const keepAll = menuItem("bi-check-circle", `Mark ALL as KEEP${suffix}`, "text-success");
    keepAll.addEventListener("click", () => {
        targets.forEach(g => setGroupAction(g, "keep"));
        removeContextMenu();
    });
    menu.appendChild(keepAll);

    menu.appendChild(menuSep());

    // Keep oldest, delete rest
    const keepOldest = menuItem("bi-clock-history", `Keep oldest, delete rest${suffix}`, "text-warning");
    keepOldest.addEventListener("click", () => {
        targets.forEach(g => setGroupKeepOldest(g));
        removeContextMenu();
    });
    menu.appendChild(keepOldest);

    menu.appendChild(menuSep());

    // Expand all
    const expandItem = menuItem("bi-arrows-expand", `Expand${suffix}`);
    expandItem.addEventListener("click", () => {
        targets.forEach(g => {
            const body = g.querySelector(".dup-group-body");
            const icon = g.querySelector(".toggle-icon");
            if (body) body.style.display = "";
            if (icon) icon.className = "bi bi-chevron-down toggle-icon";
        });
        removeContextMenu();
    });
    menu.appendChild(expandItem);

    // Collapse all
    const collapseItem = menuItem("bi-arrows-collapse", `Collapse${suffix}`);
    collapseItem.addEventListener("click", () => {
        targets.forEach(g => {
            const body = g.querySelector(".dup-group-body");
            const icon = g.querySelector(".toggle-icon");
            if (body) body.style.display = "none";
            if (icon) icon.className = "bi bi-chevron-right toggle-icon";
        });
        removeContextMenu();
    });
    menu.appendChild(collapseItem);

    document.body.appendChild(menu);
    adjustMenuPosition(menu);

    setTimeout(() => {
        document.addEventListener("click", removeContextMenu, { once: true });
    }, 10);
}

function setGroupAction(groupDiv, action) {
    const checks = groupDiv.querySelectorAll(".dup-check");
    checks.forEach((cb) => {
        cb.checked = action === "delete";
        cb.dispatchEvent(new Event("change"));
    });
}

function setGroupKeepOldest(groupDiv) {
    const checks = groupDiv.querySelectorAll(".dup-check");
    checks.forEach((cb, i) => {
        cb.checked = i !== 0; // first = oldest = KEEP
        cb.dispatchEvent(new Event("change"));
    });
}

// ── File context menu (right-click on row) ──
function showFileContextMenu(x, y, tr, selectedRows) {
    removeContextMenu();
    const path = tr.dataset.path;
    const targets = selectedRows && selectedRows.length > 1 ? selectedRows : [tr];
    const n = targets.length;
    const suffix = n > 1 ? ` (${n} files)` : "";

    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    menu.style.left = x + "px";
    menu.style.top = y + "px";

    // Copy full path (always single item)
    const copyItem = menuItem("bi-clipboard", "Copy full path");
    copyItem.addEventListener("click", () => {
        navigator.clipboard.writeText(path).catch(() => {});
        removeContextMenu();
    });
    menu.appendChild(copyItem);

    // Copy directory (always single item)
    const dir = path.substring(0, path.lastIndexOf("/")) || path.substring(0, path.lastIndexOf("\\"));
    const copyDirItem = menuItem("bi-folder", "Copy directory path");
    copyDirItem.addEventListener("click", () => {
        navigator.clipboard.writeText(dir).catch(() => {});
        removeContextMenu();
    });
    menu.appendChild(copyDirItem);

    menu.appendChild(menuSep());

    // Mark as KEEP (all selected)
    const keepItem = menuItem("bi-check-circle", `Mark as KEEP${suffix}`, "text-success");
    keepItem.addEventListener("click", () => {
        targets.forEach(r => {
            const cb = r.querySelector(".dup-check");
            if (cb) { cb.checked = false; cb.dispatchEvent(new Event("change")); }
        });
        removeContextMenu();
    });
    menu.appendChild(keepItem);

    // Mark as DELETE (all selected)
    const delItem = menuItem("bi-trash3", `Mark as DELETE${suffix}`, "text-danger");
    delItem.addEventListener("click", () => {
        targets.forEach(r => {
            const cb = r.querySelector(".dup-check");
            if (cb) { cb.checked = true; cb.dispatchEvent(new Event("change")); }
        });
        removeContextMenu();
    });
    menu.appendChild(delItem);

    document.body.appendChild(menu);
    adjustMenuPosition(menu);

    setTimeout(() => {
        document.addEventListener("click", removeContextMenu, { once: true });
    }, 10);
}

// ── Context menu helpers ──
function menuItem(icon, text, colorClass) {
    const d = document.createElement("div");
    d.className = "ctx-menu-item" + (colorClass ? " " + colorClass : "");
    d.innerHTML = `<i class="bi ${icon}"></i> ${text}`;
    return d;
}

function menuSep() {
    const d = document.createElement("div");
    d.className = "ctx-menu-sep";
    return d;
}

function removeContextMenu() {
    document.querySelectorAll(".ctx-menu").forEach((m) => m.remove());
}

function adjustMenuPosition(menu) {
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + "px";
    if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + "px";
}

// ── Bulk actions ──
function autoSelectNewer() {
    document.querySelectorAll(".dup-group").forEach((grp) => {
        const checks = grp.querySelectorAll(".dup-check");
        checks.forEach((cb, i) => {
            cb.checked = i !== 0;
            cb.dispatchEvent(new Event("change"));
        });
    });
}

function deselectAll() {
    document.querySelectorAll(".dup-check").forEach((cb) => {
        cb.checked = false;
        cb.dispatchEvent(new Event("change"));
    });
}

function expandAllGroups() {
    document.querySelectorAll(".dup-group").forEach((grp) => {
        const body = grp.querySelector(".dup-group-body");
        const icon = grp.querySelector(".toggle-icon");
        if (body) body.style.display = "";
        if (icon) icon.className = "bi bi-chevron-down toggle-icon";
    });
}

function collapseAllGroups() {
    document.querySelectorAll(".dup-group").forEach((grp) => {
        const body = grp.querySelector(".dup-group-body");
        const icon = grp.querySelector(".toggle-icon");
        if (body) body.style.display = "none";
        if (icon) icon.className = "bi bi-chevron-right toggle-icon";
    });
}

// ── Delete ──
function showDeleteConfirm() {
    const paths = getSelectedPaths();
    if (paths.length === 0) return alert("No files selected for deletion.");

    let totalSize = 0;
    document.querySelectorAll(".dup-check:checked").forEach((cb) => {
        totalSize += parseInt(cb.dataset.size || "0");
    });

    $("#deleteMsg").innerHTML = `
        Permanently delete <strong>${paths.length.toLocaleString()}</strong> file(s)?<br>
        This will free approximately <strong>${humanSize(totalSize)}</strong>.
    `;
    new bootstrap.Modal($("#deleteModal")).show();
}

async function doDelete() {
    const paths = getSelectedPaths();
    bootstrap.Modal.getInstance($("#deleteModal")).hide();

    // Collect file info BEFORE deleting (for deletion report)
    const fileInfoBefore = [];
    document.querySelectorAll(".dup-check:checked").forEach((cb) => {
        const tr = cb.closest("tr");
        if (!tr) return;
        fileInfoBefore.push({
            path: cb.dataset.path,
            name: tr.querySelector("td:nth-child(3)")?.textContent || "",
            size: parseInt(cb.dataset.size || "0"),
            size_h: tr.querySelector("td:nth-child(5)")?.textContent || "",
            hash: tr.querySelector(".hash-cell")?.textContent || "",
        });
    });

    // Show progress
    const btn = $("#deleteBtn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span> Deleting…`;

    try {
        const res = await fetch("/api/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ paths, job_id: currentJobId }),
        });
        const data = await res.json();

        if (data.error) {
            alert("Delete error: " + data.error);
            return;
        }

        // Remove deleted rows from the DOM
        const deletedSet = new Set(paths);
        document.querySelectorAll("tr[data-path]").forEach((tr) => {
            if (deletedSet.has(tr.dataset.path)) tr.remove();
        });

        // Remove groups with ≤1 file remaining
        document.querySelectorAll(".dup-group").forEach((grp) => {
            if (grp.querySelectorAll("tr[data-path]").length <= 1) grp.remove();
        });

        // Update summary stats to reflect the deletion
        if (summaryData) {
            const remainingGroups = document.querySelectorAll(".dup-group").length;
            const remainingFiles = document.querySelectorAll("tr[data-path]").length;
            summaryData.group_count = remainingGroups + (loadedGroupCount < totalGroupCount ? totalGroupCount - loadedGroupCount : 0);
            summaryData.file_count = remainingFiles;
            summaryData.reclaimable = Math.max(0, summaryData.reclaimable - fileInfoBefore.reduce((s, f) => s + f.size, 0));
            summaryData.reclaimable_h = humanSize(summaryData.reclaimable);
            summaryBar.innerHTML = `
                <i class="bi bi-info-circle me-2"></i>
                <strong>${summaryData.group_count.toLocaleString()}</strong>&nbsp;duplicate groups &nbsp;•&nbsp;
                <strong>${summaryData.file_count.toLocaleString()}</strong>&nbsp;duplicate files &nbsp;•&nbsp;
                <strong>${summaryData.reclaimable_h}</strong>&nbsp;reclaimable
            `;
        }

        // Show deletion result modal
        const totalFreed = fileInfoBefore.reduce((s, f) => s + f.size, 0);
        lastDeletionInfo = { files: fileInfoBefore, count: data.deleted, freed: totalFreed, errors: data.errors || [] };
        showDeletionResult(data, totalFreed);
    } catch (e) {
        alert("Delete failed: " + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-trash3"></i> Delete Selected`;
    }
}

let lastDeletionInfo = null;

function showDeletionResult(data, totalFreed) {
    let body = `<p>Deleted <strong>${data.deleted}</strong> file(s) (${humanSize(totalFreed)} freed).</p>`;
    if (data.errors && data.errors.length > 0) {
        body += `<p class="text-warning">${data.errors.length} error(s):</p><ul class="small">`;
        data.errors.slice(0, 10).forEach((e) => {
            body += `<li>${escHtml(e.path)}: ${escHtml(e.error)}</li>`;
        });
        body += `</ul>`;
    }
    $("#deletionResultBody").innerHTML = body;
    new bootstrap.Modal($("#deletionResultModal")).show();
}

function exportDeletionReport(format) {
    if (!lastDeletionInfo || !lastDeletionInfo.files.length) return alert("No deletion data to export.");
    const info = lastDeletionInfo;
    const ts = new Date().toISOString();

    if (format === "json") {
        const report = {
            tool: "FileDuplicator",
            report_type: "deletion",
            deletion_date: ts,
            files_deleted: info.count,
            total_freed_bytes: info.freed,
            total_freed_h: humanSize(info.freed),
            deleted_files: info.files,
            errors: info.errors,
        };
        downloadBlob(JSON.stringify(report, null, 2), `FileDuplicator_Deleted_${ts.slice(0,10)}.json`, "application/json");
    } else {
        let csv = "# FileDuplicator Deletion Report\n";
        csv += `# Date,${ts}\n`;
        csv += `# Files Deleted,${info.count}\n`;
        csv += `# Space Freed,${humanSize(info.freed)}\n`;
        csv += "\nFile Name,Path,Size (bytes),Size,Hash\n";
        for (const f of info.files) {
            csv += `${csvEsc(f.name)},${csvEsc(f.path)},${f.size},${csvEsc(f.size_h)},${csvEsc(f.hash)}\n`;
        }
        downloadBlob(csv, `FileDuplicator_Deleted_${ts.slice(0,10)}.csv`, "text/csv");
    }
}

function csvEsc(s) {
    if (!s) return "";
    if (s.includes(",") || s.includes('"') || s.includes("\n")) return '"' + s.replace(/"/g, '""') + '"';
    return s;
}

function downloadBlob(content, filename, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function getSelectedPaths() {
    return Array.from(document.querySelectorAll(".dup-check:checked"))
        .map((cb) => cb.dataset.path);
}

// ── Export ──
function exportReport(format) {
    if (!currentJobId) return alert("No scan results to export.");
    window.open(`/api/scan/${currentJobId}/export/${format}`, "_blank");
}

// ── Apply Rules (post-scan) ──
async function applyRules() {
    if (!currentJobId) return alert("No scan results. Run a scan first.");
    if (dirRules.length === 0) return alert("Add at least one directory rule first.");

    const btn = $("#applyRulesBtn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span> Applying…`;

    try {
        const res = await fetch(`/api/scan/${currentJobId}/apply-rules`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rules: dirRules.map(r => ({ path: r.path, type: r.type })) }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        // Refresh summary and groups
        if (data.summary) {
            summaryData = data.summary;
            showSummary(data.summary);
        }
        reloadResults();

        const count = data.rules_applied || 0;
        if (count > 0) {
            phaseLabel.textContent = `\u2705 Rules applied: ${count.toLocaleString()} files auto-marked.`;
        } else {
            phaseLabel.textContent = "No files matched the directory rules.";
        }
    } catch (e) {
        alert("Apply rules failed: " + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-shield-check"></i> Apply Rules`;
    }
}

// ── Browse modal ──
const browseModal = new bootstrap.Modal($("#browseModal"));
let browseCurrent = "/";
let browseTarget = "dir";  // "dir" | "preserve" | "expendable" | "ruleInput"

$("#browseBtn").addEventListener("click", () => {
    browseTarget = "dir";
    const initial = dirInput.value.trim() || "/";
    browseTo(initial);
    browseModal.show();
});

$("#browseGoBtn").addEventListener("click", () => {
    browseTo($("#browsePathInput").value.trim() || "/");
});

$("#browsePathInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") browseTo($("#browsePathInput").value.trim() || "/");
});

$("#browseSelectBtn").addEventListener("click", () => {
    if (browseTarget === "preserve" || browseTarget === "expendable") {
        addRule(browseCurrent, browseTarget);
    } else if (browseTarget === "ruleInput") {
        $("#ruleInput").value = browseCurrent;
    } else {
        addDirectory(browseCurrent);
    }
    dirInput.value = "";
    browseModal.hide();
});

async function browseTo(path) {
    try {
        const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        browseCurrent = data.current;
        $("#browsePathInput").value = data.current;

        const list = $("#browseDirList");
        list.innerHTML = "";

        // Parent
        if (data.parent !== data.current) {
            const item = document.createElement("a");
            item.href = "#";
            item.className = "list-group-item list-group-item-action bg-dark text-warning";
            item.innerHTML = `<i class="bi bi-arrow-up-circle"></i> ..  (parent)`;
            item.addEventListener("click", (e) => { e.preventDefault(); browseTo(data.parent); });
            list.appendChild(item);
        }

        for (const d of data.dirs) {
            const item = document.createElement("a");
            item.href = "#";
            item.className = "list-group-item list-group-item-action bg-dark text-light";
            item.innerHTML = `<i class="bi bi-folder-fill text-warning"></i> ${escHtml(d.name)}`;
            item.addEventListener("click", (e) => { e.preventDefault(); browseTo(d.path); });
            list.appendChild(item);
        }

        if (data.dirs.length === 0) {
            const item = document.createElement("div");
            item.className = "list-group-item bg-dark text-muted";
            item.textContent = "(no subdirectories)";
            list.appendChild(item);
        }
    } catch (e) {
        alert("Browse error: " + e.message);
    }
}

// ── Multi-select helpers ──

// --- Group selection ---
function selectGroup(groupDiv) {
    if (!selectedGroupDivs.includes(groupDiv)) {
        selectedGroupDivs.push(groupDiv);
        groupDiv.querySelector(".dup-group-header").classList.add("group-selected");
    }
}

function deselectGroup(groupDiv) {
    selectedGroupDivs = selectedGroupDivs.filter(g => g !== groupDiv);
    groupDiv.querySelector(".dup-group-header").classList.remove("group-selected");
}

function toggleGroupSelection(groupDiv) {
    if (selectedGroupDivs.includes(groupDiv)) {
        deselectGroup(groupDiv);
    } else {
        selectGroup(groupDiv);
    }
}

function clearGroupSelection() {
    selectedGroupDivs.forEach(g => {
        const h = g.querySelector(".dup-group-header");
        if (h) h.classList.remove("group-selected");
    });
    selectedGroupDivs = [];
}

function shiftSelectGroups(groupDiv) {
    const allGroups = Array.from(document.querySelectorAll(".dup-group"));
    const lastIdx = selectedGroupDivs.length > 0
        ? allGroups.indexOf(selectedGroupDivs[selectedGroupDivs.length - 1])
        : 0;
    const curIdx = allGroups.indexOf(groupDiv);
    const [start, end] = lastIdx <= curIdx ? [lastIdx, curIdx] : [curIdx, lastIdx];
    for (let i = start; i <= end; i++) {
        selectGroup(allGroups[i]);
    }
}

// --- Row selection ---
function clearRowSelection() {
    document.querySelectorAll("tr.row-selected").forEach(r => r.classList.remove("row-selected"));
}

function selectRowRange(fromRow, toRow) {
    const allRows = Array.from(document.querySelectorAll("tr[data-path]"));
    const fromIdx = allRows.indexOf(fromRow);
    const toIdx = allRows.indexOf(toRow);
    if (fromIdx < 0 || toIdx < 0) return;
    const [start, end] = fromIdx <= toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
    for (let i = start; i <= end; i++) {
        allRows[i].classList.add("row-selected");
    }
}

// Clear selections when clicking outside
document.addEventListener("click", (e) => {
    // Clear group selection if clicking outside groups and not holding Ctrl/Shift
    if (!e.ctrlKey && !e.metaKey && !e.shiftKey) {
        if (!e.target.closest(".dup-group-header") && !e.target.closest(".ctx-menu")) {
            clearGroupSelection();
        }
    }
});

// ── Spacebar toggles KEEP / DELETE on selected (or focused) rows ──
document.addEventListener("keydown", (e) => {
    // Ignore if user is typing in an input/textarea/select
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key !== " ") return;
    e.preventDefault();  // prevent page scroll

    // Gather rows: selected rows first, fallback to hovered row
    let rows = Array.from(document.querySelectorAll("tr.row-selected[data-path]"));
    if (rows.length === 0) {
        // Check if there's a hovered row
        const hovered = document.querySelector("tr[data-path]:hover");
        if (hovered) rows = [hovered];
    }
    if (rows.length === 0) return;

    rows.forEach((tr) => {
        const cb = tr.querySelector(".dup-check");
        if (!cb) return;
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event("change"));
    });
});

// ── Utilities ──
function humanSize(n) {
    for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
        if (Math.abs(n) < 1024) return n.toFixed(1) + " " + unit;
        n /= 1024;
    }
    return n.toFixed(1) + " PB";
}

function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
}

// ── Elapsed timer ──
function startElapsedTimer() {
    stopElapsedTimer();
    elapsedInterval = setInterval(() => {
        if (!scanStartTime) return;
        const sec = Math.floor((Date.now() - scanStartTime) / 1000);
        progressText.textContent = progressText.textContent.replace(/ +\|.*/, "") +
            " | " + formatElapsed(sec);
    }, 1000);
}

function stopElapsedTimer() {
    if (elapsedInterval) {
        clearInterval(elapsedInterval);
        elapsedInterval = null;
    }
}

function formatElapsed(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

// ── Compare scans ──
async function openCompareModal() {
    try {
        const res = await fetch("/api/jobs");
        const jobs = await res.json();
        const done = jobs.filter((j) => j.status === "done");

        const selA = $("#compareJobA");
        const selB = $("#compareJobB");
        selA.innerHTML = "";
        selB.innerHTML = "";

        for (const j of done) {
            const label = `${j.root || "(multi)"} · ${j.elapsed_h || "?"} · ${j.group_count.toLocaleString()} groups (${j.id})`;
            selA.innerHTML += `<option value="${j.id}">${escHtml(label)}</option>`;
            selB.innerHTML += `<option value="${j.id}">${escHtml(label)}</option>`;
        }

        // Pre-select different jobs if possible
        if (done.length >= 2) {
            selA.selectedIndex = 0;
            selB.selectedIndex = 1;
        }

        $("#compareResults").classList.add("d-none");
        new bootstrap.Modal($("#compareModal")).show();
    } catch (e) {
        alert("Failed to load jobs: " + e.message);
    }
}

async function runCompare() {
    const jobA = $("#compareJobA").value;
    const jobB = $("#compareJobB").value;
    if (!jobA || !jobB) return alert("Select two scans.");
    if (jobA === jobB) return alert("Select two different scans.");

    const btn = $("#runCompareBtn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span> Comparing…`;

    try {
        const res = await fetch("/api/compare", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ job_a: jobA, job_b: jobB }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        // Build summary table
        let html = `<table class="table table-sm table-dark table-bordered">
            <thead><tr><th></th><th>Scan A (${escHtml(data.job_a.hash)})</th><th>Scan B (${escHtml(data.job_b.hash)})</th><th>Delta</th></tr></thead>
            <tbody>
            <tr><td>Groups</td><td>${data.job_a.groups.toLocaleString()}</td><td>${data.job_b.groups.toLocaleString()}</td>
                <td class="${data.job_a.groups !== data.job_b.groups ? "text-warning" : ""}">${(data.job_b.groups - data.job_a.groups).toLocaleString()}</td></tr>
            <tr><td>Files</td><td>${data.job_a.files.toLocaleString()}</td><td>${data.job_b.files.toLocaleString()}</td>
                <td class="${data.job_a.files !== data.job_b.files ? "text-warning" : ""}">${(data.job_b.files - data.job_a.files).toLocaleString()}</td></tr>
            <tr><td>Reclaimable</td><td>${data.job_a.reclaimable}</td><td>${data.job_b.reclaimable}</td><td>—</td></tr>
            </tbody></table>`;

        if (data.only_in_a_count > 0) {
            html += `<h6 class="text-warning mt-3">⚠ ${data.only_in_a_count} group(s) only in Scan A (${escHtml(data.job_a.hash)}) — likely false positives:</h6>`;
            html += buildCompareGroupList(data.only_in_a);
        }
        if (data.only_in_b_count > 0) {
            html += `<h6 class="text-info mt-3">ℹ ${data.only_in_b_count} group(s) only in Scan B (${escHtml(data.job_b.hash)}):</h6>`;
            html += buildCompareGroupList(data.only_in_b);
        }
        if (data.only_in_a_count === 0 && data.only_in_b_count === 0) {
            html += `<div class="alert alert-success mt-3"><i class="bi bi-check-circle"></i> Both scans found identical duplicate groups!</div>`;
        }

        $("#compareSummary").innerHTML = html;
        $("#compareResults").classList.remove("d-none");
    } catch (e) {
        alert("Compare failed: " + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-arrow-left-right"></i> Compare`;
    }
}

function buildCompareGroupList(groups) {
    let html = `<div style="max-height:400px;overflow-y:auto">`;
    for (const g of groups) {
        html += `<div class="card bg-dark border-secondary mb-2"><div class="card-body py-2 px-3">`;
        html += `<strong>Group ${g.index}</strong> — ${g.file_count} files · ${g.each_size_h} each<br>`;
        for (const f of g.files) {
            html += `<small class="text-muted d-block ms-3">${escHtml(f.name)} — ${escHtml(f.path)}</small>`;
        }
        html += `</div></div>`;
    }
    html += `</div>`;
    return html;
}

// ── Bulk Delete ALL Duplicates ──

function showBulkDeleteConfirm() {
    if (!currentJobId || !summaryData) return alert("No scan results available.");
    const msg = `You are about to delete <strong>${summaryData.file_count.toLocaleString()}</strong> duplicate files ` +
        `across <strong>${summaryData.group_count.toLocaleString()}</strong> groups.<br><br>` +
        `This will free approximately <strong>${summaryData.reclaimable_h}</strong> of disk space.<br><br>` +
        `The <strong>oldest</strong> file in each group will be kept.`;
    $("#bulkDeleteMsg").innerHTML = msg;
    new bootstrap.Modal($("#bulkDeleteModal")).show();
}

function doBulkDelete() {
    bootstrap.Modal.getInstance($("#bulkDeleteModal")).hide();

    // Show progress modal
    const progressModal = new bootstrap.Modal($("#bulkDeleteProgressModal"));
    progressModal.show();

    const bar = $("#bulkDeleteProgressBar");
    const countEl = $("#bulkDeleteCount");
    const deletedEl = $("#bulkDeleteDeleted");
    const freedEl = $("#bulkDeleteFreed");
    const errorsEl = $("#bulkDeleteErrors");

    // Reset
    bar.style.width = "0%";
    bar.textContent = "0%";
    bar.className = "progress-bar progress-bar-striped progress-bar-animated bg-danger";
    countEl.textContent = "0 / 0";
    deletedEl.textContent = "0";
    freedEl.textContent = "0 B";
    errorsEl.textContent = "0";

    fetchSSEBulkDelete(currentJobId, bar, countEl, deletedEl, freedEl, errorsEl, progressModal);
}

async function fetchSSEBulkDelete(jobId, bar, countEl, deletedEl, freedEl, errorsEl, progressModal) {
    try {
        const resp = await fetch(`/api/scan/${jobId}/delete-all-duplicates`, { method: "POST" });
        if (!resp.ok) {
            const err = await resp.json();
            progressModal.hide();
            alert("Error: " + (err.error || "Unknown error"));
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let lastData = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop();  // keep incomplete line

            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    const data = JSON.parse(line.slice(6));
                    lastData = data;

                    if (data.status === "done") {
                        bar.style.width = "100%";
                        bar.classList.remove("progress-bar-animated");
                        bar.classList.replace("bg-danger", "bg-success");
                        bar.textContent = "Complete!";
                        countEl.textContent = `${data.total.toLocaleString()} / ${data.total.toLocaleString()}`;
                        deletedEl.textContent = data.deleted.toLocaleString();
                        freedEl.textContent = data.freed_h;
                        errorsEl.textContent = data.error_count.toLocaleString();

                        // Wait a moment then show result and refresh groups
                        setTimeout(() => {
                            progressModal.hide();
                            showBulkDeleteResult(data);
                            // Refresh groups from server (server-side groups are now updated)
                            reloadResults();
                        }, 1000);
                    } else {
                        const pct = data.total > 0 ? Math.round((data.current / data.total) * 100) : 0;
                        bar.style.width = pct + "%";
                        bar.textContent = pct + "%";
                        countEl.textContent = `${data.current.toLocaleString()} / ${data.total.toLocaleString()}`;
                        deletedEl.textContent = data.deleted.toLocaleString();
                        freedEl.textContent = data.freed_h;
                        errorsEl.textContent = data.error_count.toLocaleString();
                    }
                }
            }
        }
    } catch (e) {
        progressModal.hide();
        alert("Bulk delete failed: " + e.message);
    }
}

function showBulkDeleteResult(data) {
    let html = `<div class="mb-3">`;
    html += `<div class="d-flex gap-3 mb-3 justify-content-center">`;
    html += `<div class="text-center"><div class="fs-3 text-success fw-bold">${data.deleted.toLocaleString()}</div><div class="text-muted small">Files Deleted</div></div>`;
    html += `<div class="text-center"><div class="fs-3 text-info fw-bold">${data.freed_h}</div><div class="text-muted small">Space Freed</div></div>`;
    html += `<div class="text-center"><div class="fs-3 fw-bold">${data.kept.toLocaleString()}</div><div class="text-muted small">Files Kept</div></div>`;
    if (data.error_count > 0) {
        html += `<div class="text-center"><div class="fs-3 text-warning fw-bold">${data.error_count.toLocaleString()}</div><div class="text-muted small">Errors</div></div>`;
    }
    html += `</div>`;

    if (data.error_count > 0) {
        html += `<div class="alert alert-warning py-2 small"><strong>Errors (first ${data.errors.length}):</strong><ul class="mb-0 mt-1">`;
        for (const err of data.errors) {
            html += `<li>${escHtml(err.path)}: ${escHtml(err.error)}</li>`;
        }
        html += `</ul></div>`;
    }

    if (data.error_count === 0) {
        html += `<div class="alert alert-success py-2"><i class="bi bi-check-circle-fill"></i> All duplicate files were successfully deleted!</div>`;
    }
    html += `</div>`;

    $("#deletionResultBody").innerHTML = html;
    new bootstrap.Modal($("#deletionResultModal")).show();
}
