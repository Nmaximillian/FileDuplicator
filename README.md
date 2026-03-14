# File Duplicator – Duplicate Finder

A fast duplicate-file scanner and cleaner for large directory trees (8 TB+). Available as a **Windows desktop app**, **macOS desktop app**, or a **web UI** you can run on a NAS via Docker.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![PyQt6](https://img.shields.io/badge/Desktop-PyQt6-green) ![Flask](https://img.shields.io/badge/Web-Flask-lightgrey) ![Docker](https://img.shields.io/badge/NAS-Docker-blue) ![xxhash](https://img.shields.io/badge/hash-xxHash%20%7C%20SHA--256-orange)

### Platform Support

![Windows](https://img.shields.io/badge/Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white) ![macOS](https://img.shields.io/badge/macOS-000000?style=for-the-badge&logo=apple&logoColor=white) ![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)

| Platform | Download | How to install |
|----------|----------|----------------|
| **Windows** | `FileDuplicator.exe` | Download and double-click |
| **macOS** | `FileDuplicator.dmg` | Open DMG → drag to Applications ([see Gatekeeper note](#option-a2--macos-desktop-dmg)) |
| **Web / NAS** | Docker image | `docker compose up -d` |

---

## Running the App

### Option A – Windows desktop (`.exe`)
Download `FileDuplicator.exe` from the [latest release](../../releases/latest) and double-click it. No Python needed.

### Option A2 – macOS desktop (`.dmg`)
Download `FileDuplicator.dmg` from the [latest release](../../releases/latest), open it, and drag **FileDuplicator** to your Applications folder.

> **⚠️ macOS Gatekeeper:** Since the app is not signed with an Apple Developer ID, macOS will show a warning on first launch. To open it:
> 1. **Right-click** (or Ctrl+click) the app → click **Open** → click **Open** again, **or**
> 2. Go to **System Settings → Privacy & Security** → click **Open Anyway**, **or**
> 3. Run in Terminal: `xattr -cr /Applications/FileDuplicator.app`

### Option B – Desktop from source
```bash
python -m venv .venv
# Windows:  .\.venv\Scripts\activate
# Linux:    source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

### Option C – Web UI (local)
```bash
pip install -r requirements-web.txt
python -m web.app --port 5000
# Open http://localhost:5000
```

### Option D – Docker on your NAS (recommended for Asustor / Synology / etc.)

```bash
# 1. Build the image
docker compose build

# 2. Edit docker-compose.yml to mount your NAS volumes (see below)

# 3. Start
docker compose up -d

# Open http://<nas-ip>:5000
```

#### Volume mapping for Asustor Flashstor 12 Pro

Edit `docker-compose.yml` and adjust the `volumes:` section:

```yaml
volumes:
  # <host path on NAS>:<path inside container>
  - /volume1:/data/volume1
  - /volume2:/data/volume2
```

Inside the web UI, browse to `/data/volume1/...` to scan your files.

> **Tip:** You can also deploy via **Portainer** (available in ADM's App Central).
> Import the `docker-compose.yml` as a Stack.

### Building from source

**Windows (.exe):**
```bash
pip install pyinstaller pillow
python -c "from generate_icon import generate_icon; generate_icon()"
pyinstaller FileDuplicator.spec
# Output: dist/FileDuplicator.exe
```

**macOS (.app / .dmg):**
```bash
pip install pyinstaller pillow
python -c "from generate_icon import generate_icon; generate_icon()"
python generate_icns.py
pyinstaller FileDuplicator_macOS.spec
# Output: dist/FileDuplicator.app
```

---

## Features

### Duplicate Detection
- **Three match criteria** – file name, size, and/or content hash (combinable)
- **Hash algorithm choice** – xxHash (xxh128) for speed or SHA-256 for cryptographic certainty
- **Recursive or flat scan** – choose whether to walk subdirectories
- **Minimum file size filter** – skip tiny files (0 B → 1 GB threshold)
- **Cloud file detection** – automatically skips OneDrive / iCloud placeholder files on Windows and macOS

### Performance (designed for 8 TB+)
- **Progressive hashing** – groups by size → partial hash (first+last 64 KB) → full hash only on true collisions
- **xxHash (xxh128)** – ~10× faster than SHA-256, reliable for everyday use
- **SHA-256** – cryptographic hash for maximum confidence on critical data
- **Parallel hashing** – batched multi-threaded I/O with size-aware timeouts
- **Paginated results** – handles 100K+ duplicate groups without crashing
- **Background scanning** – UI stays responsive (desktop: QThread, web: SSE streaming)

### Search, Sort & Compare
- **Search / filter** – find groups by file name or path across 100K+ results
- **Sort controls** – sort by size ↑↓, file count ↑↓, or name A→Z / Z→A
- **Compare scans** – run xxHash and SHA-256 scans, then compare them side-by-side to verify results

### Export & Audit
- **Export reports** – download scan results as CSV or JSON for offline review
- **Deletion log export** – after deleting, export a detailed log of what was removed
- **Scan statistics** – total files scanned, size, cloud files skipped, hash algorithm, elapsed time, and timestamp

### Desktop-only features (Windows & macOS)
- **Double-click** a file → opens Explorer (Windows) or Finder (macOS) with file selected
- **Right-click context menu** → Show in Explorer/Finder / Open folder / Toggle KEEP-DELETE
- **Compare JSON reports** – load two exported JSON files and diff them
- **Native icon** – `.ico` on Windows, `.icns` on macOS
- **Remembers last directory** between sessions

### Web-only features
- **Browser-based directory picker** – navigate your NAS shares visually
- **Right-click context menu** → Copy full path / Copy directory / Toggle KEEP-DELETE
- **Auto-reconnect** – close the browser tab and come back later; your scan is still there
- **SSE with heartbeat** – reliable progress streaming even for multi-hour SHA-256 scans
- **Responsive dark theme** – works on desktop browsers, tablets, and phones
- **Runs headless** – no display server needed (perfect for NAS)

### Both editions
- **Color-coded duplicate groups** – KEEP in green, DELETE in red
- **Automatic suggestions** – oldest file kept, newer copies marked for deletion
- **Bulk actions** – "Select All Newer as Delete" / "Deselect All"
- **Confirmation dialog** – shows file count and reclaimable space before deletion
- **Per-file error reporting** after deletion
- **Elapsed time & timestamp** – see how long the scan took and when it finished

---

## How the Scanner Works

| Phase | What happens | Disk reads |
|---|---|---|
| 1. Enumerate | `os.scandir()` collects file name + size, skips cloud placeholders | None |
| 2. Group by size | Files with a unique size are discarded | None |
| 3. Partial hash | First + last 64 KB hashed (xxHash or SHA-256) | Tiny |
| 4. Full hash | Only true collisions fully hashed in 4 MB chunks | Minimal |

For a typical 8 TB drive with ~680K files, this reads well under 1% of total data.

> **Hash algorithm choice:** xxHash (xxh128) is ~10× faster and perfectly reliable.
> SHA-256 is available for extra confidence when dealing with irreplaceable data.

---

## Project Structure

```
FileDuplicator/
├── main.py                  # Desktop entry point (PyQt6)
├── main_window.py           # Desktop UI (search, sort, compare, export)
├── scanner.py               # Core scan engine (shared by both editions)
├── requirements.txt         # Desktop dependencies (PyQt6, xxhash)
├── requirements-web.txt     # Web dependencies (Flask, gunicorn, xxhash)
├── FileDuplicator.ico       # App icon
├── FileDuplicator.spec      # PyInstaller build config (Windows)
├── FileDuplicator_macOS.spec # PyInstaller build config (macOS .app)
├── generate_icon.py         # Regenerate the .ico via Pillow
├── generate_icns.py         # Regenerate the .icns via Pillow + iconutil (macOS)
├── .github/workflows/
│   └── release.yml          # Auto-build Windows + macOS on tag push
├── web/
│   ├── app.py               # Flask server + REST API + SSE + compare
│   ├── templates/
│   │   └── index.html       # Web UI (Bootstrap 5 dark theme)
│   └── static/
│       ├── app.js           # Client-side logic (pagination, sort, search)
│       ├── style.css         # Dark theme styles
│       └── favicon.svg       # Browser tab icon
├── Dockerfile               # Docker image build
├── docker-compose.yml       # One-command deploy with volume mounts
├── .dockerignore
├── LICENSE                  # CC BY-NC-SA 4.0 (non-commercial)
└── dist/
    └── FileDuplicator.exe   # Windows standalone (PyInstaller)
```

---

## License

This project is licensed under **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**.

- ✅ **Free for personal, educational, and hobby use**
- ❌ **Commercial use prohibited** without permission
- 📄 See [LICENSE](LICENSE) file or visit https://creativecommons.org/licenses/by-nc-sa/4.0/

For commercial licensing inquiries, please contact the project maintainers.