# File Duplicator – Duplicate Finder

A fast duplicate-file scanner and cleaner for large directory trees (8 TB+). Available as a **Windows desktop app** or a **web UI** you can run on a NAS via Docker.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![PyQt6](https://img.shields.io/badge/Desktop-PyQt6-green) ![Flask](https://img.shields.io/badge/Web-Flask-lightgrey) ![Docker](https://img.shields.io/badge/NAS-Docker-blue) ![xxhash](https://img.shields.io/badge/hash-xxhash-orange)

---

## Running the App

### Option A – Windows desktop (`.exe`)
Download or build `dist\FileDuplicator.exe` and double-click it. No Python needed.

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

### Building the Windows executable
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "FileDuplicator" \
  --icon "FileDuplicator.ico" --add-data "FileDuplicator.ico;." main.py
```

---

## Features

### Duplicate Detection
- **Three match criteria** – file name, size, and/or content hash (combinable)
- **Recursive or flat scan** – choose whether to walk subdirectories
- **Minimum file size filter** – skip tiny files (0 B → 1 GB threshold)

### Performance (designed for 8 TB+)
- **Progressive hashing** – groups by size → partial hash (first+last 64 KB) → full hash only on true collisions
- **xxhash (xxh128)** – ~10× faster than MD5/SHA
- **Background scanning** – UI stays responsive (desktop: QThread, web: SSE streaming)

### Desktop-only features
- **Double-click** a file → opens Explorer with file selected
- **Right-click context menu** → Show in Explorer / Open folder / Toggle KEEP-DELETE
- **Custom `.ico` icon** in title bar, taskbar, and `.exe`
- **Remembers last directory** between sessions

### Web-only features
- **Browser-based directory picker** – navigate your NAS shares visually
- **Right-click context menu** → Copy full path / Copy directory / Toggle KEEP-DELETE
- **Responsive dark theme** – works on desktop browsers, tablets, and phones
- **Runs headless** – no display server needed (perfect for NAS)

### Both editions
- **Color-coded duplicate groups** – KEEP in green, DELETE in red
- **Automatic suggestions** – oldest file kept, newer copies marked for deletion
- **Bulk actions** – "Select All Newer as Delete" / "Deselect All"
- **Confirmation dialog** – shows file count and reclaimable space before deletion
- **Per-file error reporting** after deletion

---

## How the Scanner Works

| Phase | What happens | Disk reads |
|---|---|---|
| 1. Enumerate | `os.walk()` collects file name + size from metadata | None |
| 2. Group by size | Files with a unique size are discarded | None |
| 3. Partial hash | First + last 64 KB hashed via xxhash | Tiny |
| 4. Full hash | Only true collisions fully hashed (1 MB chunks) | Minimal |

For a typical 8 TB drive, this reads well under 1% of total data.

---

## Project Structure

```
FileDuplicator/
├── main.py                  # Desktop entry point (PyQt6)
├── main_window.py           # Desktop UI
├── scanner.py               # Core scan engine (shared by both editions)
├── requirements.txt         # Desktop dependencies (PyQt6, xxhash)
├── requirements-web.txt     # Web dependencies (Flask, gunicorn, xxhash)
├── FileDuplicator.ico       # App icon
├── generate_icon.py         # Regenerate the .ico via Pillow
├── web/
│   ├── app.py               # Flask server + REST API
│   ├── templates/
│   │   └── index.html       # Web UI page
│   └── static/
│       ├── app.js           # Client-side logic
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