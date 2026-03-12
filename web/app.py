"""
File Duplicator – Web edition (v2).

Flask server that wraps the scanner engine and exposes a REST API + SSE progress.
Groups are stored server-side and served paginated to avoid browser crashes.
"""

from __future__ import annotations

import csv
import io
import os
import json
import time as _time
import uuid
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from scanner import DuplicateGroup, DuplicateMode, FileEntry, ScanStats, scan_directory

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)

# In-memory store for scan jobs (single-user NAS tool – fine for this use-case)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

PAGE_SIZE = 50  # groups per page


def _human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def _human_elapsed(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Routes – pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes – API
# ---------------------------------------------------------------------------
@app.route("/api/jobs")
def api_jobs():
    """List known jobs so the browser can reconnect after being closed."""
    with _jobs_lock:
        result = []
        for jid, j in _jobs.items():
            result.append({
                "id": jid,
                "status": j["status"],
                "phase": j["phase"],
                "root": j.get("root", ""),
                "started_at": j.get("started_at", ""),
                "group_count": j["summary"].get("group_count", 0) if j["summary"] else 0,
                "finished_at": j.get("finished_at"),
                "elapsed_h": _human_elapsed(j["elapsed"]) if j.get("elapsed") else None,
            })
    return jsonify(result)


@app.route("/api/browse", methods=["GET"])
def api_browse():
    """List subdirectories of the given path (for a directory picker)."""
    path = request.args.get("path", "/")
    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400

    dirs = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.is_dir(follow_symlinks=False):
                dirs.append({"name": entry.name, "path": entry.path})
    except PermissionError:
        pass

    parent = str(Path(path).parent)
    return jsonify({"current": path, "parent": parent, "dirs": dirs})


@app.route("/api/scan", methods=["POST"])
def api_scan_start():
    """Start a scan job.  Returns a job ID for polling progress via SSE."""
    data = request.get_json(force=True)
    root = data.get("root", "")
    if not root or not os.path.isdir(root):
        return jsonify({"error": "Invalid directory"}), 400

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "running",
        "phase": "",
        "current": 0,
        "total": 0,
        "groups": [],          # serialised, sorted, pageable
        "summary": {},
        "error": None,
        "cancelled": False,
        "root": root,
        "started_at": datetime.now().isoformat(),
        "_start_time": _time.monotonic(),
        "finished_at": None,
        "elapsed": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    by_name = data.get("by_name", False)
    by_size = data.get("by_size", True)
    by_hash = data.get("by_hash", True)
    recursive = data.get("recursive", True)

    min_size_map = {
        "0": 0, "1B": 1, "1KB": 1024, "1MB": 1024**2,
        "10MB": 10 * 1024**2, "100MB": 100 * 1024**2, "1GB": 1024**3,
    }
    min_size = min_size_map.get(data.get("min_size", "1KB"), 1024)
    use_sha256 = data.get("use_sha256", False)

    def _run():
        try:
            def _progress(phase, cur, tot):
                job["phase"] = phase
                job["current"] = cur
                job["total"] = tot

            groups, stats = scan_directory(
                root,
                by_name=by_name,
                by_size=by_size,
                by_hash=by_hash,
                recursive=recursive,
                min_size=min_size,
                use_sha256=use_sha256,
                progress=_progress,
                cancelled=lambda: job["cancelled"],
            )

            # Update phase so the UI doesn't appear frozen during
            # serialisation of 100K+ groups
            job["phase"] = "Preparing results\u2026"
            job["current"] = 0
            job["total"] = 0

            # Serialise and sort by file size descending (biggest dupes first)
            serialised = _serialise_groups(groups)
            serialised.sort(
                key=lambda g: g["files"][0]["size"] if g["files"] else 0,
                reverse=True,
            )
            # Re-number after sort
            for i, g in enumerate(serialised):
                g["index"] = i + 1

            # Pre-compute summary
            total_files = sum(g["file_count"] for g in serialised)
            total_waste = 0
            for g in serialised:
                for f in g["files"][1:]:
                    total_waste += f["size"]

            job["groups"] = serialised
            job["summary"] = {
                "group_count": len(serialised),
                "file_count": total_files,
                "reclaimable": total_waste,
                "reclaimable_h": _human_size(total_waste),
                "total_scanned": stats.total_files_scanned,
                "total_scanned_size": stats.total_size_scanned,
                "total_scanned_size_h": _human_size(stats.total_size_scanned),
                "cloud_skipped": stats.cloud_files_skipped,
                "hash_algorithm": stats.hash_algorithm,
            }
            elapsed = _time.monotonic() - job["_start_time"]
            job["finished_at"] = datetime.now().isoformat()
            job["elapsed"] = elapsed
            job["summary"]["finished_at"] = job["finished_at"]
            job["summary"]["elapsed"] = elapsed
            job["summary"]["elapsed_h"] = _human_elapsed(elapsed)
            job["status"] = "done"
        except Exception as exc:
            job["error"] = str(exc)
            job["status"] = "error"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/scan/<job_id>/progress")
def api_scan_progress(job_id: str):
    """SSE stream of scan progress.  On completion sends summary only (not all groups)."""
    def generate():
        import time
        tick = 0
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                yield f"data: {json.dumps({'error': 'unknown job'})}\n\n"
                return

            payload = {
                "status": job["status"],
                "phase": job["phase"],
                "current": job["current"],
                "total": job["total"],
            }
            if job["status"] == "done":
                # Only send summary – groups are fetched via paginated API
                payload["summary"] = job["summary"]
                yield f"data: {json.dumps(payload)}\n\n"
                return
            if job["status"] == "error":
                payload["error"] = job["error"]
                yield f"data: {json.dumps(payload)}\n\n"
                return

            yield f"data: {json.dumps(payload)}\n\n"

            # Send SSE keep-alive comment every ~15s to prevent proxy/browser
            # timeouts during long SHA-256 scans
            tick += 1
            if tick % 37 == 0:  # ~15s at 0.4s intervals
                yield ": keepalive\n\n"

            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/scan/<job_id>/groups")
def api_scan_groups(job_id: str):
    """Paginated group results.  ?offset=0&limit=50&sort=size_desc&search=keyword"""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] != "done":
        return jsonify({"error": "scan not complete"}), 400

    search = request.args.get("search", "").strip().lower()
    sort_by = request.args.get("sort", "size_desc")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", PAGE_SIZE, type=int)
    limit = min(limit, 200)  # cap max per request

    groups = job["groups"]

    # Filter by search keyword (file name or path)
    if search:
        filtered = []
        for g in groups:
            for f in g["files"]:
                if search in f["name"].lower() or search in f["path"].lower():
                    filtered.append(g)
                    break
        groups = filtered

    # Sort
    if sort_by == "size_asc":
        groups = sorted(groups, key=lambda g: g["files"][0]["size"] if g["files"] else 0)
    elif sort_by == "size_desc":
        groups = sorted(groups, key=lambda g: g["files"][0]["size"] if g["files"] else 0, reverse=True)
    elif sort_by == "files_desc":
        groups = sorted(groups, key=lambda g: g["file_count"], reverse=True)
    elif sort_by == "files_asc":
        groups = sorted(groups, key=lambda g: g["file_count"])
    elif sort_by == "name_asc":
        groups = sorted(groups, key=lambda g: g["files"][0]["name"].lower() if g["files"] else "")
    elif sort_by == "name_desc":
        groups = sorted(groups, key=lambda g: g["files"][0]["name"].lower() if g["files"] else "", reverse=True)
    # default: size_desc (already sorted that way from scan)

    page = groups[offset: offset + limit]
    return jsonify({
        "groups": page,
        "total": len(groups),
        "offset": offset,
        "has_more": (offset + limit) < len(groups),
    })


@app.route("/api/scan/<job_id>/cancel", methods=["POST"])
def api_scan_cancel(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        job["cancelled"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "unknown job"}), 404


@app.route("/api/compare", methods=["POST"])
def api_compare():
    """Compare two completed scan jobs and return the differences."""
    data = request.get_json(force=True)
    job_a_id = data.get("job_a")
    job_b_id = data.get("job_b")

    with _jobs_lock:
        job_a = _jobs.get(job_a_id)
        job_b = _jobs.get(job_b_id)
    if not job_a or not job_b:
        return jsonify({"error": "One or both jobs not found"}), 404
    if job_a["status"] != "done" or job_b["status"] != "done":
        return jsonify({"error": "Both scans must be complete"}), 400

    # Build file-path sets for each job
    def _file_set(job):
        s = {}  # path → {group_index, hash, size, name}
        for g in job["groups"]:
            for f in g["files"]:
                s[f["path"]] = {"group": g["index"], "hash": f.get("hash", ""), "size": f["size"],
                                "name": f["name"], "size_h": f["size_h"]}
        return s

    def _group_keys(job):
        """Return dict of group_key → group info."""
        d = {}
        for g in job["groups"]:
            paths = tuple(sorted(f["path"] for f in g["files"]))
            d[paths] = g
        return d

    files_a = _file_set(job_a)
    files_b = _file_set(job_b)

    groups_a = _group_keys(job_a)
    groups_b = _group_keys(job_b)

    # Groups only in A (not in B) — these are likely false-positive matches
    only_in_a = []
    for paths_key, grp in groups_a.items():
        if paths_key not in groups_b:
            only_in_a.append(grp)

    only_in_b = []
    for paths_key, grp in groups_b.items():
        if paths_key not in groups_a:
            only_in_b.append(grp)

    sum_a = job_a["summary"]
    sum_b = job_b["summary"]

    return jsonify({
        "job_a": {"id": job_a_id, "hash": sum_a.get("hash_algorithm", ""),
                  "groups": sum_a.get("group_count", 0), "files": sum_a.get("file_count", 0),
                  "reclaimable": sum_a.get("reclaimable_h", "")},
        "job_b": {"id": job_b_id, "hash": sum_b.get("hash_algorithm", ""),
                  "groups": sum_b.get("group_count", 0), "files": sum_b.get("file_count", 0),
                  "reclaimable": sum_b.get("reclaimable_h", "")},
        "only_in_a": only_in_a[:200],  # cap for safety
        "only_in_b": only_in_b[:200],
        "only_in_a_count": len(only_in_a),
        "only_in_b_count": len(only_in_b),
    })


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """Delete a list of file paths."""
    data = request.get_json(force=True)
    paths: list[str] = data.get("paths", [])
    if not paths:
        return jsonify({"error": "No paths given"}), 400

    deleted = []
    errors = []
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
                deleted.append(p)
            else:
                errors.append({"path": p, "error": "File not found"})
        except Exception as exc:
            errors.append({"path": p, "error": str(exc)})

    return jsonify({"deleted": len(deleted), "errors": errors})


@app.route("/api/scan/<job_id>/export/csv")
def api_export_csv(job_id: str):
    """Download all groups as a CSV file."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] != "done":
        return jsonify({"error": "scan not complete"}), 400

    summary = job.get("summary", {})
    groups = job["groups"]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# FileDuplicator Scan Report"])
    w.writerow(["# Date", datetime.now().isoformat()])
    w.writerow(["# Hash Algorithm", summary.get("hash_algorithm", "")])
    w.writerow(["# Files Scanned", summary.get("total_scanned", "")])
    w.writerow(["# Total Size Scanned", summary.get("total_scanned_size_h", "")])
    w.writerow(["# Duplicate Groups", summary.get("group_count", "")])
    w.writerow(["# Duplicate Files", summary.get("file_count", "")])
    w.writerow(["# Reclaimable", summary.get("reclaimable_h", "")])
    w.writerow([])
    w.writerow(["Group", "Mode", "Is Oldest", "File Name", "Path", "Size (bytes)", "Size", "Hash"])

    for g in groups:
        for f in g["files"]:
            w.writerow([
                g["index"],
                g["mode"],
                "Yes" if f.get("is_oldest") else "No",
                f["name"],
                f["path"],
                f["size"],
                f["size_h"],
                f.get("hash", ""),
            ])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="FileDuplicator_Report_{ts}.csv"'},
    )


@app.route("/api/scan/<job_id>/export/json")
def api_export_json(job_id: str):
    """Download all groups as a JSON file."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job["status"] != "done":
        return jsonify({"error": "scan not complete"}), 400

    summary = job.get("summary", {})
    report = {
        "tool": "FileDuplicator",
        "export_date": datetime.now().isoformat(),
        "stats": summary,
        "groups": job["groups"],
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        json.dumps(report, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="FileDuplicator_Report_{ts}.json"'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serialise_groups(groups: list[DuplicateGroup]) -> list[dict]:
    result = []
    for idx, grp in enumerate(groups):
        sorted_files = sorted(grp.files, key=lambda f: _safe_mtime(f.path))
        files = []
        for fi, fe in enumerate(sorted_files):
            files.append({
                "path": fe.path,
                "name": fe.name,
                "size": fe.size,
                "size_h": _human_size(fe.size),
                "hash": (fe.full_hash[:12] if fe.full_hash
                         else fe.partial_hash[:12] if fe.partial_hash
                         else ""),
                "mtime": _safe_mtime(fe.path),
                "is_oldest": fi == 0,
            })
        result.append({
            "index": idx + 1,
            "mode": grp.mode.name.capitalize(),
            "key": grp.key,
            "file_count": len(grp.files),
            "each_size_h": _human_size(grp.files[0].size) if grp.files else "",
            "files": files,
        })
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="File Duplicator – Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=5000, help="Port")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
