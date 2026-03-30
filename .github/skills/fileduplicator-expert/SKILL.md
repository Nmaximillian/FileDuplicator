---
name: fileduplicator-expert
description: 'Expert workflow for the FileDuplicator project (PyQt6 desktop + Flask web + shared scanner engine). Use when implementing features, debugging scan correctness/performance, updating rules/deletion behavior, exporting reports, packaging with PyInstaller, or deploying with Docker/NAS.'
argument-hint: 'Describe your goal, target surface (desktop/web/core/build), and constraints (speed/safety/compatibility).'
user-invocable: true
---

# FileDuplicator Expert

End-to-end workflow for working safely and effectively in this repository.

## When To Use
- Add or modify duplicate-detection behavior in `scanner.py`.
- Change desktop UX in `main_window.py` or startup in `main.py`.
- Change web API/SSE behavior in `web/app.py` or UI in `web/static/app.js`, `web/templates/index.html`, `web/static/style.css`.
- Update export, compare, or delete flows.
- Update release/version/build files (`version.py`, PyInstaller specs, Docker, GitHub Actions).
- Investigate regressions in correctness, responsiveness, or large-scan scalability.

## Architecture Snapshot
- Core engine: `scanner.py`
- Desktop app: `main.py` + `main_window.py` (PyQt6, threaded worker)
- Web app: `web/app.py` (Flask REST + SSE) + static frontend assets
- Shared model primitives: `FileEntry`, `DuplicateGroup`, `ScanStats`, `DirectoryRule`
- Release/build: `FileDuplicator.spec`, `FileDuplicator_macOS.spec`, `.github/workflows/release.yml`
- Container deploy: `Dockerfile`, `docker-compose.yml`
- Version source of truth: `version.py`

## Key Invariants
- Duplicate detection pipeline remains progressive:
1. enumerate files
2. group by size (or name-only mode)
3. partial hash (first+last 64 KB)
4. full hash for partial collisions only
- Cloud placeholder files are skipped (Windows/macOS attribute-based checks).
- Results are sorted by reclaimable waste (largest first).
- Oldest file is default KEEP when no explicit rule override exists.
- Directory rules behavior remains:
1. preserve wins
2. preserve conflicts => review
3. expendable deletes only when a safe copy exists, otherwise keep oldest expendable
- UI paths (desktop/web) stay responsive for very large result sets (batching/pagination).

## Procedure

### 1. Intake And Classification
- Parse requested change and classify target surface:
1. `core-scan` for matching/hash/rules/performance logic
2. `desktop-ui` for Qt interaction and desktop-only behavior
3. `web-api` for Flask endpoints/job lifecycle/SSE/export
4. `web-ui` for browser interaction and rendering
5. `build-release` for packaging/versioning/deployment
- Identify impacted files before edits.

### 2. Trace Data Flow Before Editing
- For scan behavior changes, trace from `scan_directory()` through all call sites.
- For directory-rule changes, trace `apply_directory_rules()` to both desktop and web rendering/deletion paths.
- For web changes, follow:
1. API route in `web/app.py`
2. payload shape
3. consumer logic in `web/static/app.js`
4. expected DOM in `web/templates/index.html`
- For desktop changes, follow:
1. UI control creation/signals in `main_window.py`
2. worker thread payload/progress wiring
3. tree population and action toggles

### 3. Apply Minimal, Cross-Surface-Safe Changes
- Keep shared behavior in `scanner.py` so desktop and web stay consistent.
- If adding fields to serialized groups/files, update all consumers.
- Preserve existing sorting, filtering, and batching defaults unless explicitly requested.
- Maintain cancellation and progress semantics.

### 4. Validate Behavior
- Run relevant paths after changes.
- Desktop run:
```bash
python main.py
```
- Web run:
```bash
python -m web.app --port 5000
```
- Docker run:
```bash
docker compose up -d --build
```
- Perform focused checks based on touched areas.

### 5. Completion Checks
- Correctness:
1. duplicate groups and reclaimable totals are plausible
2. rule actions map correctly to KEEP/DELETE/REVIEW
3. delete actions never remove preserved/review files unintentionally
- UX:
1. desktop remains responsive during scans and large result rendering
2. web pagination/search/sort/load-more still function
3. SSE progress and reconnect behavior still work
- Compatibility:
1. export CSV/JSON schemas remain valid for compare workflow
2. version/build files stay synchronized when release-related changes are made

## Default Invocation Focus
- If the user does not specify a target surface, prioritize scanner correctness and performance first.
- Start with `scanner.py` and only touch desktop/web layers when needed to preserve feature parity.
- For ambiguous requests, favor safe, correctness-preserving behavior over aggressive optimization.

## Decision Branches

### Branch A: Matching Criteria Request
- Name-only requested: use/adjust name grouping path and avoid hash assumptions.
- Size+hash requested: preserve progressive hash pipeline and timeout/batching strategy.
- Hash algorithm request:
1. xxHash for speed
2. SHA-256 for certainty

### Branch B: Rule-Driven Selection Request
- If preserve + neutral/expendable present: preserve KEEP, others DELETE.
- If multiple preserve copies in same group: all REVIEW.
- If only expendable copies exist: keep oldest, delete others.

### Branch C: Performance/Scale Request
- Scanner bottleneck: inspect batching constants, worker usage, and timeout math.
- Desktop bottleneck: inspect batched QTree population and signal blocking.
- Web bottleneck: inspect pagination size, search/sort application, and serialization overhead.

### Branch D: Deletion Safety Request
- Prefer marking/toggling changes first, then explicit deletion confirmation.
- Preserve per-file error collection and user-visible summary.
- Keep deletion report/export integrity.

## File-Specific Guidance
- `scanner.py`: treat as canonical for duplicate logic, hashing, and rule engine.
- `main_window.py`: preserve QThread boundaries and UI-thread-only widget updates.
- `web/app.py`: maintain single-worker in-memory job model assumptions when altering concurrency.
- `web/static/app.js`: keep feature parity with backend payload fields and state transitions.
- `version.py`: bump version for user-visible releases and align release notes/workflow expectations.

## Common Pitfalls To Avoid
- Changing rule semantics in one surface only (desktop or web) and creating divergence.
- Introducing payload fields in web API without updating frontend render logic.
- Breaking oldest-file default keep behavior when reordering files.
- Making large unscoped refactors that alter existing export/compare expectations.
- Altering Docker/Gunicorn worker model without considering in-memory `_jobs` state design.

## Done Criteria
- The change is implemented in all required surfaces.
- No obvious regressions in scan, rule application, deletion, export, or compare flows.
- User-facing behavior and release/build docs remain coherent for the touched scope.

## Example Prompts
- `/fileduplicator-expert Optimize SHA-256 scan time for NAS while preserving correctness.`
- `/fileduplicator-expert Add a new rule type and wire it through desktop + web UI safely.`
- `/fileduplicator-expert Debug why results differ between desktop and web for the same folders.`
- `/fileduplicator-expert Add an export field and update compare logic without breaking old JSON imports.`
- `/fileduplicator-expert Prepare v1.3.1 release: version bump, notes, and packaging sanity check.`
