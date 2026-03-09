"""
File Duplicator – Web edition.

Flask server that wraps the scanner engine and exposes a REST API + SSE progress.
"""

from __future__ import annotations

import os
import json
import uuid
import threading
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from scanner import DuplicateGroup, DuplicateMode, FileEntry, scan_directory

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)

# In-memory store for scan jobs (single-user NAS tool – fine for this use-case)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


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
        "groups": [],
        "error": None,
        "cancelled": False,
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

    def _run():
        try:
            def _progress(phase, cur, tot):
                job["phase"] = phase
                job["current"] = cur
                job["total"] = tot

            groups = scan_directory(
                root,
                by_name=by_name,
                by_size=by_size,
                by_hash=by_hash,
                recursive=recursive,
                min_size=min_size,
                progress=_progress,
                cancelled=lambda: job["cancelled"],
            )
            job["groups"] = _serialise_groups(groups)
            job["status"] = "done"
        except Exception as exc:
            job["error"] = str(exc)
            job["status"] = "error"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/scan/<job_id>/progress")
def api_scan_progress(job_id: str):
    """SSE stream of scan progress."""
    def generate():
        import time
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
                payload["groups"] = job["groups"]
                yield f"data: {json.dumps(payload)}\n\n"
                return
            if job["status"] == "error":
                payload["error"] = job["error"]
                yield f"data: {json.dumps(payload)}\n\n"
                return

            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/scan/<job_id>/cancel", methods=["POST"])
def api_scan_cancel(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        job["cancelled"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "unknown job"}), 404


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
