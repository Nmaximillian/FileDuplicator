/**
 * File Duplicator – Web UI logic
 */

// ── State ──
let currentJobId = null;
let scanGroups = [];
let eventSource = null;

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

// ── Scan ──
scanBtn.addEventListener("click", startScan);
cancelBtn.addEventListener("click", cancelScan);
$("#autoSelectBtn").addEventListener("click", autoSelectNewer);
$("#deselectBtn").addEventListener("click", deselectAll);
$("#deleteBtn").addEventListener("click", showDeleteConfirm);
$("#confirmDeleteBtn").addEventListener("click", doDelete);

async function startScan() {
    const root = dirInput.value.trim();
    if (!root) return alert("Enter a directory path.");

    scanBtn.disabled = true;
    cancelBtn.disabled = false;
    progressArea.classList.remove("d-none");
    resultsArea.classList.add("d-none");
    groupsContainer.innerHTML = "";
    scanGroups = [];
    progressBar.style.width = "0%";

    const body = {
        root,
        by_name: $("#chkName").checked,
        by_size: $("#chkSize").checked,
        by_hash: $("#chkHash").checked,
        recursive: $("#chkRecursive").checked,
        min_size: $("#minSize").value,
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
        pollProgress(data.job_id);
    } catch (e) {
        alert("Scan failed: " + e.message);
        scanBtn.disabled = false;
        cancelBtn.disabled = true;
    }
}

function pollProgress(jobId) {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/api/scan/${jobId}/progress`);
    eventSource.onmessage = (ev) => {
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
            scanGroups = d.groups || [];
            renderResults(scanGroups);
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
        scanBtn.disabled = false;
        cancelBtn.disabled = true;
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

// ── Render results ──
function renderResults(groups) {
    resultsArea.classList.remove("d-none");
    groupsContainer.innerHTML = "";

    let totalDupes = 0;
    let totalWaste = 0;
    for (const g of groups) {
        totalDupes += g.files.length - 1;
        for (let i = 1; i < g.files.length; i++) totalWaste += g.files[i].size;
    }

    summaryBar.innerHTML = `
        <i class="bi bi-info-circle me-2"></i>
        <strong>${groups.length}</strong>&nbsp;duplicate groups &nbsp;•&nbsp;
        <strong>${totalDupes}</strong>&nbsp;duplicate files &nbsp;•&nbsp;
        <strong>${humanSize(totalWaste)}</strong>&nbsp;reclaimable
    `;

    for (const g of groups) {
        groupsContainer.appendChild(buildGroup(g));
    }
}

function buildGroup(group) {
    const div = document.createElement("div");
    div.className = "dup-group";

    // Header
    const header = document.createElement("div");
    header.className = "dup-group-header";
    header.innerHTML = `
        <i class="bi bi-chevron-down toggle-icon"></i>
        <span class="badge bg-secondary">${group.mode}</span>
        Group ${group.index} &nbsp;–&nbsp; ${group.file_count} files &nbsp;•&nbsp; ${group.each_size_h} each
    `;
    header.addEventListener("click", () => {
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
    div.appendChild(header);

    // Body table
    const body = document.createElement("div");
    body.className = "dup-group-body";

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
        const checked = f.is_oldest ? "" : "checked";
        const actionClass = f.is_oldest ? "action-keep" : "action-delete";
        const actionText = f.is_oldest ? "KEEP" : "DELETE";
        html += `<tr data-path="${escHtml(f.path)}" data-group="${group.index}">
            <td><input type="checkbox" class="dup-check" data-path="${escHtml(f.path)}" data-size="${f.size}" ${checked}></td>
            <td class="action-label ${actionClass}">${actionText}</td>
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

    // Right-click context menu
    body.addEventListener("contextmenu", (e) => {
        const tr = e.target.closest("tr[data-path]");
        if (!tr) return;
        e.preventDefault();
        showContextMenu(e.clientX, e.clientY, tr);
    });

    return div;
}

// ── Context menu ──
function showContextMenu(x, y, tr) {
    removeContextMenu();
    const path = tr.dataset.path;
    const cb = tr.querySelector(".dup-check");

    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    menu.style.left = x + "px";
    menu.style.top = y + "px";

    // Copy path
    const copyItem = menuItem("bi-clipboard", "Copy full path");
    copyItem.addEventListener("click", () => {
        navigator.clipboard.writeText(path).catch(() => {});
        removeContextMenu();
    });
    menu.appendChild(copyItem);

    // Copy directory
    const dir = path.substring(0, path.lastIndexOf("/")) || path.substring(0, path.lastIndexOf("\\"));
    const copyDirItem = menuItem("bi-folder", "Copy directory path");
    copyDirItem.addEventListener("click", () => {
        navigator.clipboard.writeText(dir).catch(() => {});
        removeContextMenu();
    });
    menu.appendChild(copyDirItem);

    menu.appendChild(menuSep());

    // Toggle keep/delete
    if (cb) {
        const isChecked = cb.checked;
        const toggleItem = menuItem(
            isChecked ? "bi-check-circle" : "bi-trash3",
            isChecked ? "Mark as KEEP" : "Mark as DELETE"
        );
        toggleItem.addEventListener("click", () => {
            cb.checked = !isChecked;
            cb.dispatchEvent(new Event("change"));
            removeContextMenu();
        });
        menu.appendChild(toggleItem);
    }

    document.body.appendChild(menu);

    // Adjust if off-screen
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + "px";
    if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + "px";

    setTimeout(() => {
        document.addEventListener("click", removeContextMenu, { once: true });
    }, 10);
}

function menuItem(icon, text) {
    const d = document.createElement("div");
    d.className = "ctx-menu-item";
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

// ── Delete ──
function showDeleteConfirm() {
    const paths = getSelectedPaths();
    if (paths.length === 0) return alert("No files selected for deletion.");

    let totalSize = 0;
    document.querySelectorAll(".dup-check:checked").forEach((cb) => {
        totalSize += parseInt(cb.dataset.size || "0");
    });

    $("#deleteMsg").innerHTML = `
        Permanently delete <strong>${paths.length}</strong> file(s)?<br>
        This will free approximately <strong>${humanSize(totalSize)}</strong>.
    `;
    new bootstrap.Modal($("#deleteModal")).show();
}

async function doDelete() {
    const paths = getSelectedPaths();
    bootstrap.Modal.getInstance($("#deleteModal")).hide();

    try {
        const res = await fetch("/api/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ paths }),
        });
        const data = await res.json();

        // Remove deleted rows from the DOM
        const deletedSet = new Set(paths);
        document.querySelectorAll("tr[data-path]").forEach((tr) => {
            if (deletedSet.has(tr.dataset.path)) tr.remove();
        });

        // Remove groups with ≤1 file remaining
        document.querySelectorAll(".dup-group").forEach((grp) => {
            if (grp.querySelectorAll("tr[data-path]").length <= 1) grp.remove();
        });

        let msg = `Deleted ${data.deleted} file(s).`;
        if (data.errors && data.errors.length > 0) {
            msg += `\n${data.errors.length} error(s):\n` +
                data.errors.map((e) => `${e.path}: ${e.error}`).join("\n");
        }
        alert(msg);
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

function getSelectedPaths() {
    return Array.from(document.querySelectorAll(".dup-check:checked"))
        .map((cb) => cb.dataset.path);
}

// ── Browse modal ──
const browseModal = new bootstrap.Modal($("#browseModal"));
let browseCurrent = "/";

$("#browseBtn").addEventListener("click", () => {
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
    dirInput.value = browseCurrent;
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
