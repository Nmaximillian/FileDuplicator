# File Duplicator – Duplicate Finder

A fast, standalone desktop app to find and remove duplicate files across large directory trees — tested with 8 TB+ / thousands of files.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![PyQt6](https://img.shields.io/badge/UI-PyQt6-green) ![xxhash](https://img.shields.io/badge/hash-xxhash-orange)

## Running the App

### Option A – Standalone executable (no Python needed)
Download or build `dist\FileDuplicator.exe` and double-click it. No installation required.

### Option B – Run from source
```bash
# 1. Create & activate a virtual environment
python -m venv .venv

# Windows
.\.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python main.py
```

### Building the executable yourself
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "FileDuplicator" \
  --icon "FileDuplicator.ico" --add-data "FileDuplicator.ico;." main.py
# Output: dist\FileDuplicator.exe
```

---

## Features

### Duplicate Detection
- **Three match criteria** – file name, size, and/or content hash (combinable)
- **Recursive or flat scan** – choose whether to walk subdirectories
- **Minimum file size filter** – skip tiny files (0 B → 1 GB threshold, configurable)

### Performance (designed for 8 TB+)
- **Progressive hashing** – groups by size → partial hash (first+last 64 KB) → full hash only on true collisions. Reads a tiny fraction of total disk data in most cases.
- **xxhash (xxh128)** – ~10× faster than MD5/SHA for content hashing
- **Threaded scanning** – background worker thread keeps the UI fully responsive during long scans with a live progress bar

### Results & Navigation
- **Color-coded groups** – each duplicate group has a distinct background; KEEP labels are green, DELETE labels are red
- **Double-click** any file row → opens Windows Explorer with that exact file selected
- **Right-click** any file row → context menu:
  - 📂 Show in Explorer (selects the file)
  - 📁 Open containing folder
  - Toggle Mark as KEEP / DELETE

### Deletion Workflow
- **Automatic suggestions** – oldest file in each group is kept by default; newer copies are marked for deletion
- **"Select All Newer as Delete"** bulk action
- **Confirmation dialog** – shows exact file count and reclaimable space before any files are touched
- **Permanent deletion** with per-file error reporting

### Other
- **Custom icon** – shown in the title bar, taskbar, and Windows Explorer
- **Remembers last directory** between sessions

---

## How the Scanner Works

| Phase | What happens | Disk reads |
|---|---|---|
| 1. Enumerate | `os.walk()` collects file name + size from metadata | None |
| 2. Group by size | Files with a unique size are discarded immediately | None |
| 3. Partial hash | First + last 64 KB read via xxhash | Tiny |
| 4. Full hash | Only files still colliding after phase 3 are fully hashed (1 MB chunks) | Minimal |

For a typical 8 TB drive, phases 3 and 4 together usually read well under 1% of total data.

---

## Project Structure

| File | Purpose |
|---|---|
| `main.py` | App entry point – creates the QApplication and launches the window |
| `main_window.py` | Full PyQt6 UI: directory picker, scan options, results tree, context menus, delete actions |
| `scanner.py` | Core scan engine with 4-phase progressive hashing strategy |
| `generate_icon.py` | Script to regenerate `FileDuplicator.ico` using Pillow |
| `requirements.txt` | Runtime dependencies (`PyQt6`, `xxhash`) |
| `FileDuplicator.ico` | App icon (all standard sizes, 16–256 px) |
| `dist/FileDuplicator.exe` | Standalone Windows executable (built by PyInstaller) |