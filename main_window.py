"""
Main window – PyQt6 UI for the duplicate-file scanner.

Performance notes (C: drive / 8 TB scans):
- Tree is populated in batches via QTimer (never blocks the event loop)
- Display is CAPPED at N groups (configurable) – sorted by wasted space so the
  most impactful duplicates always appear first.
- "Load More" lets the user page through remaining groups on demand.
- Signals and widget updates are blocked during bulk operations.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time as _time
from datetime import datetime

from PyQt6.QtCore import (
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
    QSettings,
)
from PyQt6.QtGui import QColor, QIcon, QAction
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scanner import DuplicateGroup, DuplicateMode, FileEntry, ScanStats, scan_directory, DirectoryRule, apply_directory_rules
from version import __version__

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _icon_path() -> str:
    ext = ".icns" if sys.platform == "darwin" else ".ico"
    name = f"FileDuplicator{ext}"
    if getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _open_in_explorer(file_path: str):
    """Reveal the file in the platform file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", file_path])
        else:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(file_path)])
    except Exception:
        pass


def _open_directory(file_path: str):
    """Open the containing directory in the platform file manager."""
    try:
        folder = os.path.dirname(file_path)
        if sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            os.startfile(folder)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    progress = pyqtSignal(str, int, int)   # phase, current, total
    finished = pyqtSignal(list, object)    # list[DuplicateGroup], ScanStats
    error    = pyqtSignal(str)

    def __init__(
        self,
        roots: list[str],
        by_name: bool,
        by_size: bool,
        by_hash: bool,
        recursive: bool,
        min_size: int,
        use_sha256: bool = False,
        index_path: str | None = None,
    ):
        super().__init__()
        self.roots = roots
        self.by_name = by_name
        self.by_size = by_size
        self.by_hash = by_hash
        self.recursive = recursive
        self.min_size = min_size
        self.use_sha256 = use_sha256
        self.index_path = index_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            groups, stats = scan_directory(
                self.roots,
                by_name=self.by_name,
                by_size=self.by_size,
                by_hash=self.by_hash,
                recursive=self.recursive,
                min_size=self.min_size,
                use_sha256=self.use_sha256,
                index_path=self.index_path,
                progress=lambda phase, cur, tot: self.progress.emit(phase, cur, tot),
                cancelled=lambda: self._cancelled,
            )
            if not self._cancelled:
                self.finished.emit(groups, stats)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Colours (dark-theme friendly)
# ---------------------------------------------------------------------------

GROUP_COLORS = [
    QColor("#1a3a4a"),
    QColor("#3a2a1a"),
    QColor("#1a3a1a"),
    QColor("#3a1a2a"),
    QColor("#2a1a3a"),
    QColor("#3a3a1a"),
    QColor("#1a2a3a"),
    QColor("#2a1a2a"),
]

TEXT_COLOR_LIGHT = QColor("#e0e0e0")
TEXT_COLOR_KEEP  = QColor("#81c784")
TEXT_COLOR_DEL   = QColor("#ef9a9a")
TEXT_COLOR_REVIEW = QColor("#ffcc80")
GROUP_HEADER_COLOR = QColor("#ffffff")


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    # How many groups to show at a time
    DEFAULT_DISPLAY_CAP = 500
    BATCH_SIZE = 50      # groups per QTimer tick
    BATCH_DELAY_MS = 5   # ms between ticks

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"File Duplicator v{__version__} – Duplicate Finder")
        self.resize(1200, 750)

        ico = _icon_path()
        if os.path.isfile(ico):
            self.setWindowIcon(QIcon(ico))

        self._worker: ScanWorker | None = None
        self._groups: list[DuplicateGroup] = []   # ALL groups from scan
        self._stats: ScanStats | None = None       # last scan stats
        self._scan_roots: list[str] = []            # directories that were scanned
        self._displayed: int = 0                   # how many groups are in the tree
        self._scan_start_time: float = 0.0          # monotonic start time
        self._scan_elapsed: float = 0.0             # elapsed seconds
        self._scan_finished_at: str = ""            # ISO timestamp
        self._rule_decisions: dict[str, str] = {}    # filepath → keep/delete/review

        self._build_ui()
        self._restore_state()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)

        # --- Directory picker (multi-directory) ---
        dir_group = QGroupBox("Directories")
        dir_outer = QVBoxLayout(dir_group)
        self._dir_list = QListWidget()
        self._dir_list.setMaximumHeight(100)
        self._dir_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._dir_list.setAlternatingRowColors(True)
        dir_outer.addWidget(self._dir_list)
        dir_btn_row = QHBoxLayout()
        self._add_dir_btn = QPushButton("Add…")
        self._add_dir_btn.clicked.connect(self._browse_add)
        dir_btn_row.addWidget(self._add_dir_btn)
        self._remove_dir_btn = QPushButton("Remove selected")
        self._remove_dir_btn.clicked.connect(self._remove_selected_dirs)
        dir_btn_row.addWidget(self._remove_dir_btn)
        self._clear_dirs_btn = QPushButton("Clear all")
        self._clear_dirs_btn.clicked.connect(self._clear_dirs)
        dir_btn_row.addWidget(self._clear_dirs_btn)
        dir_btn_row.addStretch()
        dir_outer.addLayout(dir_btn_row)
        root_layout.addWidget(dir_group)

        # --- Scan options ---
        opts_group = QGroupBox("Scan options")
        opts_layout = QHBoxLayout(opts_group)
        self._chk_name = QCheckBox("By name")
        self._chk_size = QCheckBox("By size"); self._chk_size.setChecked(True)
        self._chk_hash = QCheckBox("By hash (content)"); self._chk_hash.setChecked(True)
        self._chk_recursive = QCheckBox("Recursive"); self._chk_recursive.setChecked(True)
        opts_layout.addWidget(self._chk_name)
        opts_layout.addWidget(self._chk_size)
        opts_layout.addWidget(self._chk_hash)
        opts_layout.addWidget(QLabel("  "))
        opts_layout.addWidget(self._chk_recursive)
        opts_layout.addStretch()
        opts_layout.addWidget(QLabel("Min size:"))
        self._min_size_combo = QComboBox()
        self._min_size_combo.addItems([
            "0 B (all)", "1 B", "1 KB", "1 MB", "10 MB", "100 MB", "1 GB",
        ])
        self._min_size_combo.setCurrentIndex(2)
        opts_layout.addWidget(self._min_size_combo)
        opts_layout.addWidget(QLabel("  Hash:"))
        self._hash_combo = QComboBox()
        self._hash_combo.addItems(["xxHash (fast)", "SHA-256 (paranoid)"])
        self._hash_combo.setCurrentIndex(0)
        self._hash_combo.setToolTip(
            "xxHash is extremely fast and reliable for duplicate detection.\n"
            "SHA-256 is cryptographic – slower but gives absolute certainty."
        )
        opts_layout.addWidget(self._hash_combo)
        opts_layout.addWidget(QLabel("  "))
        self._chk_save_index = QCheckBox("💾 Save file index")
        self._chk_save_index.setToolTip(
            "After scanning, write a CSV index of ALL files found (not just duplicates)\n"
            "to the first scan directory as file_index_YYYYMMDD_HHMMSS.csv.\n"
            "Useful for searching your disk later without re-scanning – like IYF or Everything."
        )
        opts_layout.addWidget(self._chk_save_index)
        root_layout.addWidget(opts_group)

        # --- Directory Rules (optional) ---
        rules_group = QGroupBox("Directory Rules (optional)")
        rules_layout = QVBoxLayout(rules_group)
        rules_help = QLabel(
            "<small>Mark directories as <b>Preserve</b> (always keep files here, "
            "delete matches elsewhere) or <b>Expendable</b> (delete files here "
            "if a copy exists elsewhere).</small>"
        )
        rules_help.setWordWrap(True)
        rules_layout.addWidget(rules_help)
        self._rules_list = QListWidget()
        self._rules_list.setMaximumHeight(80)
        self._rules_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._rules_list.setAlternatingRowColors(True)
        rules_layout.addWidget(self._rules_list)
        rules_btn_row = QHBoxLayout()
        self._add_preserve_btn = QPushButton("+ Preserve")
        self._add_preserve_btn.setToolTip(
            "Files here are canonical \u2014 delete matching copies found elsewhere"
        )
        self._add_preserve_btn.setStyleSheet("color: #81c784;")
        self._add_preserve_btn.clicked.connect(lambda: self._add_rule("preserve"))
        rules_btn_row.addWidget(self._add_preserve_btn)
        self._add_expendable_btn = QPushButton("+ Expendable")
        self._add_expendable_btn.setToolTip(
            "Files here are junk copies \u2014 delete them if a copy exists anywhere else"
        )
        self._add_expendable_btn.setStyleSheet("color: #ef9a9a;")
        self._add_expendable_btn.clicked.connect(lambda: self._add_rule("expendable"))
        rules_btn_row.addWidget(self._add_expendable_btn)
        self._remove_rule_btn = QPushButton("Remove")
        self._remove_rule_btn.clicked.connect(self._remove_selected_rules)
        rules_btn_row.addWidget(self._remove_rule_btn)
        self._clear_rules_btn = QPushButton("Clear")
        self._clear_rules_btn.clicked.connect(self._clear_rules)
        rules_btn_row.addWidget(self._clear_rules_btn)
        rules_btn_row.addStretch()
        rules_layout.addLayout(rules_btn_row)
        root_layout.addWidget(rules_group)

        # --- Action buttons ---
        btn_layout = QHBoxLayout()
        self._scan_btn = QPushButton("🔍  Scan for Duplicates")
        self._scan_btn.setMinimumHeight(38)
        self._scan_btn.setStyleSheet("font-weight: bold; font-size: 14px;")
        self._scan_btn.clicked.connect(self._start_scan)
        btn_layout.addWidget(self._scan_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_scan)
        btn_layout.addWidget(self._cancel_btn)
        root_layout.addLayout(btn_layout)

        # --- Progress ---
        prog_layout = QHBoxLayout()
        self._phase_label = QLabel("")
        prog_layout.addWidget(self._phase_label)
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        prog_layout.addWidget(self._progress, 1)
        root_layout.addLayout(prog_layout)

        # --- Display cap & Load More ---
        cap_layout = QHBoxLayout()
        cap_layout.addWidget(QLabel("Max groups shown:"))
        self._cap_spin = QSpinBox()
        self._cap_spin.setRange(50, 100_000)
        self._cap_spin.setSingleStep(500)
        self._cap_spin.setValue(self.DEFAULT_DISPLAY_CAP)
        cap_layout.addWidget(self._cap_spin)
        self._load_more_btn = QPushButton("Load More Groups")
        self._load_more_btn.setEnabled(False)
        self._load_more_btn.clicked.connect(self._load_more)
        cap_layout.addWidget(self._load_more_btn)
        cap_layout.addStretch()
        root_layout.addLayout(cap_layout)

        # --- Search & Sort ---
        search_sort_layout = QHBoxLayout()
        search_sort_layout.addWidget(QLabel("🔍 Search:"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter by file name or path…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_changed)
        search_sort_layout.addWidget(self._search_edit, 1)
        search_sort_layout.addWidget(QLabel("  Sort:"))
        self._sort_combo = QComboBox()
        self._sort_combo.addItems([
            "Size ↓ (largest)", "Size ↑ (smallest)",
            "File count ↓", "File count ↑",
            "Name A→Z", "Name Z→A",
        ])
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        search_sort_layout.addWidget(self._sort_combo)
        self._filter_label = QLabel("")
        self._filter_label.setStyleSheet("color: #aaa;")
        search_sort_layout.addWidget(self._filter_label)
        root_layout.addLayout(search_sort_layout)

        # --- Results tree ---
        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Action", "File Name", "Path", "Size", "Hash"])
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setAlternatingRowColors(False)
        self._tree.setRootIsDecorated(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        # Override keyPressEvent so Space toggles KEEP/DELETE on all selected
        # file rows at once (not just the focused item).
        _orig_key = self._tree.keyPressEvent

        def _tree_key_press(event):
            if event.key() == Qt.Key.Key_Space:
                selected = [
                    si for si in self._tree.selectedItems()
                    if si.parent() is not None  # file rows only
                ]
                if selected:
                    self._tree.blockSignals(True)
                    for si in selected:
                        if si.checkState(0) == Qt.CheckState.Checked:
                            si.setCheckState(0, Qt.CheckState.Unchecked)
                            si.setText(0, "KEEP")
                            si.setForeground(0, TEXT_COLOR_KEEP)
                        else:
                            si.setCheckState(0, Qt.CheckState.Checked)
                            si.setText(0, "DELETE")
                            si.setForeground(0, TEXT_COLOR_DEL)
                    self._tree.blockSignals(False)
                    return  # consumed
            _orig_key(event)

        self._tree.keyPressEvent = _tree_key_press
        root_layout.addWidget(self._tree, 1)

        # --- Bottom bar ---
        bottom = QHBoxLayout()
        self._summary_label = QLabel("")
        bottom.addWidget(self._summary_label, 1)
        self._select_all_delete = QPushButton("Select All Newer as Delete")
        self._select_all_delete.setToolTip(
            "For each group, mark the newer files for deletion and keep the oldest."
        )
        self._select_all_delete.clicked.connect(self._auto_select_newer)
        bottom.addWidget(self._select_all_delete)
        self._apply_rules_btn = QPushButton("\U0001f4cb Apply Rules")
        self._apply_rules_btn.setToolTip(
            "Re-apply directory rules to the current scan results.\n"
            "Preserve dirs \u2192 keep files; Expendable dirs \u2192 delete if copies exist elsewhere."
        )
        self._apply_rules_btn.clicked.connect(self._apply_rules)
        bottom.addWidget(self._apply_rules_btn)
        self._select_all_keep = QPushButton("Deselect All")
        self._select_all_keep.clicked.connect(self._deselect_all)
        bottom.addWidget(self._select_all_keep)
        self._export_btn = QPushButton("📋  Export Report")
        self._export_btn.setToolTip("Export scan results to CSV or JSON for comparison / audit")
        self._export_btn.clicked.connect(self._export_report)
        bottom.addWidget(self._export_btn)
        self._compare_btn = QPushButton("⚖  Compare Reports")
        self._compare_btn.setToolTip("Compare two exported JSON reports to find differences")
        self._compare_btn.clicked.connect(self._compare_reports)
        bottom.addWidget(self._compare_btn)
        self._delete_btn = QPushButton("🗑  Delete Selected")
        self._delete_btn.setStyleSheet("color: #d32f2f; font-weight: bold;")
        self._delete_btn.clicked.connect(self._delete_selected)
        bottom.addWidget(self._delete_btn)
        root_layout.addLayout(bottom)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)

    # ---------------------------------------------------------- persistence
    def _save_state(self):
        s = QSettings("FileDuplicator", "MainWindow")
        s.setValue("geometry", self.saveGeometry())
        dirs = [self._dir_list.item(i).text() for i in range(self._dir_list.count())]
        s.setValue("last_dirs", dirs)
        # Persist directory rules
        rules = []
        for i in range(self._rules_list.count()):
            data = self._rules_list.item(i).data(Qt.ItemDataRole.UserRole)
            if data:
                rules.append(data)
        s.setValue("last_rules", json.dumps(rules))

    def _restore_state(self):
        s = QSettings("FileDuplicator", "MainWindow")
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        saved = s.value("last_dirs", [])
        if isinstance(saved, str):
            saved = [saved] if saved else []
        for d in saved:
            if d and os.path.isdir(d):
                self._dir_list.addItem(d)
        # Restore directory rules
        saved_rules = s.value("last_rules", "[]")
        try:
            rules = json.loads(saved_rules) if isinstance(saved_rules, str) else []
        except (json.JSONDecodeError, TypeError):
            rules = []
        for r in rules:
            if isinstance(r, dict) and "path" in r and "type" in r:
                prefix = "\U0001f7e2 PRESERVE" if r["type"] == "preserve" else "\U0001f534 EXPENDABLE"
                item = QListWidgetItem(f"{prefix}: {r['path']}")
                item.setData(Qt.ItemDataRole.UserRole, r)
                self._rules_list.addItem(item)

    def closeEvent(self, event):
        self._save_state()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)

    # ---------------------------------------------------------- slots
    def _browse_add(self):
        start = ""
        if self._dir_list.count():
            start = self._dir_list.item(self._dir_list.count() - 1).text()
        d = QFileDialog.getExistingDirectory(self, "Add directory", start)
        if d:
            # Avoid duplicates
            existing = {self._dir_list.item(i).text() for i in range(self._dir_list.count())}
            if d not in existing:
                self._dir_list.addItem(d)

    def _remove_selected_dirs(self):
        for item in reversed(self._dir_list.selectedItems()):
            self._dir_list.takeItem(self._dir_list.row(item))

    def _clear_dirs(self):
        self._dir_list.clear()

    # ---------------------------------------------------------- directory rules
    def _add_rule(self, rule_type: str):
        start = ""
        if self._dir_list.count():
            start = self._dir_list.item(self._dir_list.count() - 1).text()
        d = QFileDialog.getExistingDirectory(self, f"Select {rule_type} directory", start)
        if d:
            prefix = "\U0001f7e2 PRESERVE" if rule_type == "preserve" else "\U0001f534 EXPENDABLE"
            item = QListWidgetItem(f"{prefix}: {d}")
            item.setData(Qt.ItemDataRole.UserRole, {"path": d, "type": rule_type})
            self._rules_list.addItem(item)

    def _remove_selected_rules(self):
        for item in reversed(self._rules_list.selectedItems()):
            self._rules_list.takeItem(self._rules_list.row(item))

    def _clear_rules(self):
        self._rules_list.clear()

    def _get_rules(self) -> list:
        """Return the current list of DirectoryRule objects from the UI."""
        rules = []
        for i in range(self._rules_list.count()):
            data = self._rules_list.item(i).data(Qt.ItemDataRole.UserRole)
            if data:
                rules.append(DirectoryRule(path=data["path"], rule_type=data["type"]))
        return rules

    def _apply_rules(self):
        """Re-apply directory rules to current scan results."""
        rules = self._get_rules()
        if not rules:
            QMessageBox.information(self, "No rules", "Add at least one directory rule first.")
            return
        if not self._groups:
            QMessageBox.information(self, "No results", "Run a scan first.")
            return
        self._rule_decisions = apply_directory_rules(self._groups, rules)
        if not self._rule_decisions:
            QMessageBox.information(
                self, "No matches",
                "No duplicate groups matched any directory rules.",
            )
            return
        # Rebuild tree to reflect rule decisions
        self._displayed = 0
        self._start_batched_populate()
        self._status.showMessage(
            f"\U0001f4cb Rules applied: {len(self._rule_decisions)} files auto-marked.", 10_000
        )

    def _parse_min_size(self) -> int:
        text = self._min_size_combo.currentText()
        if "all" in text:
            return 0
        parts = text.split()
        val = float(parts[0])
        unit = parts[1].upper()
        mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        return int(val * mult.get(unit, 1))

    def _start_scan(self):
        roots = [self._dir_list.item(i).text().strip()
                 for i in range(self._dir_list.count())]
        roots = [r for r in roots if r and os.path.isdir(r)]
        if not roots:
            QMessageBox.warning(self, "No directories", "Please add at least one valid directory.")
            return
        by_name = self._chk_name.isChecked()
        by_size = self._chk_size.isChecked()
        by_hash = self._chk_hash.isChecked()
        if not (by_name or by_size or by_hash):
            QMessageBox.warning(self, "No criteria", "Select at least one duplicate criterion.")
            return

        self._tree.clear()
        self._groups.clear()
        self._stats = None
        self._scan_roots = roots
        self._displayed = 0
        self._progress.setValue(0)
        self._scan_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._summary_label.setText("")
        self._load_more_btn.setEnabled(False)
        self._search_edit.clear()
        self._filter_label.setText("")
        self._scan_start_time = _time.monotonic()

        index_path: str | None = None
        if self._chk_save_index.isChecked():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            index_path = os.path.join(roots[0], f"file_index_{ts}.csv")

        self._worker = ScanWorker(
            roots,
            by_name=by_name,
            by_size=by_size,
            by_hash=by_hash,
            recursive=self._chk_recursive.isChecked(),
            min_size=self._parse_min_size(),
            use_sha256=self._hash_combo.currentIndex() == 1,
            index_path=index_path,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _cancel_scan(self):
        if self._worker:
            self._worker.cancel()
            self._status.showMessage("Cancelling…")
            self._cancel_btn.setEnabled(False)

    @pyqtSlot(str, int, int)
    def _on_progress(self, phase: str, current: int, total: int):
        self._phase_label.setText(phase)
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        else:
            self._progress.setMaximum(0)

    @pyqtSlot(list, object)
    def _on_finished(self, groups: list[DuplicateGroup], stats: ScanStats):
        self._scan_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._groups = groups   # already sorted by wasted space from scanner
        self._stats = stats
        self._displayed = 0

        # Apply directory rules if any are defined
        rules = self._get_rules()
        self._rule_decisions = apply_directory_rules(groups, rules) if rules else {}

        self._scan_elapsed = _time.monotonic() - self._scan_start_time
        self._scan_finished_at = datetime.now().isoformat()

        if not groups:
            self._summary_label.setText(
                f"No duplicates found.  "
                f"Scanned {stats.total_files_scanned:,} files "
                f"({_human_size(stats.total_size_scanned)})  •  "
                f"Hash: {stats.hash_algorithm}  •  "
                f"⏱ {_human_elapsed(self._scan_elapsed)}  •  "
                f"🕐 {datetime.now():%H:%M:%S}"
            )
            self._phase_label.setText("Scan complete – no duplicates.")
            self._progress.setValue(self._progress.maximum() or 1)
        if stats.index_path:
            self._status.showMessage(
                f"No duplicates found.  ·  File index saved → {stats.index_path}", 15_000
            )
        cap = self._cap_spin.value()
        showing = min(cap, len(groups))

        self._summary_label.setText(
            f"Scanned {stats.total_files_scanned:,} files "
            f"({_human_size(stats.total_size_scanned)})  •  "
            f"Found {stats.duplicate_groups:,} duplicate groups  •  "
            f"{stats.duplicate_files:,} duplicate files  •  "
            f"{_human_size(stats.reclaimable_bytes)} reclaimable  •  "
            f"Showing top {showing:,}  •  "
            f"Hash: {stats.hash_algorithm}  •  "
            f"⏱ {_human_elapsed(self._scan_elapsed)}  •  "
            f"🕐 {datetime.now():%H:%M:%S}"
        )
        self._progress.setValue(self._progress.maximum() or 1)
        self._phase_label.setText(
            f"Scan complete – loading {showing:,} of {len(groups):,} groups…"
        )
        status_msg = f"Scan complete – {len(groups):,} groups found."
        if stats.index_path:
            status_msg += f"  ·  File index saved → {stats.index_path}"
        if self._rule_decisions:
            status_msg += f"  ·  📋 {len(self._rule_decisions)} files auto-marked by rules"
        self._status.showMessage(status_msg, 15_000)

        # Start batched display
        self._start_batched_populate()

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        self._scan_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        QMessageBox.critical(self, "Scan error", msg)

    # ---------------------------------------------- batched tree population
    def _start_batched_populate(self):
        """Populate the tree in small batches so the UI stays responsive."""
        self._tree.clear()
        self._displayed = 0

        # Disconnect itemChanged to avoid spurious signals during bulk insert
        try:
            self._tree.itemChanged.disconnect(self._on_item_changed)
        except TypeError:
            pass

        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)

        self._batch_timer = QTimer(self)
        self._batch_timer.setInterval(self.BATCH_DELAY_MS)
        self._batch_timer.timeout.connect(self._populate_next_batch)
        self._batch_timer.start()

    def _populate_next_batch(self):
        groups = self._groups
        cap = self._cap_spin.value()
        limit = min(cap, len(groups))
        start = self._displayed
        end = min(start + self.BATCH_SIZE, limit)

        if start >= end:
            self._finish_populate()
            return

        for idx in range(start, end):
            self._add_group(idx, groups[idx])

        self._displayed = end
        self._phase_label.setText(f"Loading results… {end:,}/{limit:,} groups")

        if end >= limit:
            self._finish_populate()

    def _finish_populate(self):
        self._batch_timer.stop()
        self._tree.setUpdatesEnabled(True)
        self._tree.blockSignals(False)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._phase_label.setText("Scan complete.")

        remaining = len(self._groups) - self._displayed
        if remaining > 0:
            self._load_more_btn.setEnabled(True)
            self._load_more_btn.setText(f"Load More ({remaining:,} remaining)")
        else:
            self._load_more_btn.setEnabled(False)
            self._load_more_btn.setText("All groups loaded")

    def _add_group(self, idx: int, grp: DuplicateGroup):
        color = GROUP_COLORS[idx % len(GROUP_COLORS)]
        mode_label = grp.mode.name.capitalize()

        group_item = QTreeWidgetItem(self._tree)
        group_item.setText(
            1,
            f"[{mode_label}] Group {idx + 1}  –  "
            f"{len(grp.files)} files  •  {_human_size(grp.files[0].size)} each",
        )
        # Expand all groups so the user sees every result immediately
        group_item.setExpanded(True)
        for col in range(5):
            group_item.setBackground(col, color)
            group_item.setForeground(col, GROUP_HEADER_COLOR)

        # Sort files: oldest first → first child = KEEP
        sorted_files = sorted(grp.files, key=lambda f: f.mtime or _safe_mtime(f.path))

        for fi, fe in enumerate(sorted_files):
            child = QTreeWidgetItem(group_item)
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)

            # Check directory rules first, fall back to default (oldest = keep)
            rule = self._rule_decisions.get(fe.path)
            if rule == "keep":
                is_keep, is_review = True, False
            elif rule == "delete":
                is_keep, is_review = False, False
            elif rule == "review":
                is_keep, is_review = True, True
            else:
                is_keep, is_review = (fi == 0), False

            child.setCheckState(0, Qt.CheckState.Unchecked if is_keep else Qt.CheckState.Checked)
            if is_review:
                child.setText(0, "\u26a0 REVIEW")
            else:
                child.setText(0, "KEEP" if is_keep else "DELETE")
            child.setText(1, fe.name)
            child.setText(2, fe.path)
            child.setText(3, _human_size(fe.size))
            child.setText(4, (fe.full_hash[:12] if fe.full_hash
                              else fe.partial_hash[:12] if fe.partial_hash else ""))
            child.setData(0, Qt.ItemDataRole.UserRole, fe.path)
            for col in range(5):
                child.setBackground(col, color)
                child.setForeground(col, TEXT_COLOR_LIGHT)
            if is_review:
                child.setForeground(0, TEXT_COLOR_REVIEW)
            else:
                child.setForeground(0, TEXT_COLOR_KEEP if is_keep else TEXT_COLOR_DEL)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if column == 0 and item.parent() is not None:
            if item.checkState(0) == Qt.CheckState.Checked:
                item.setText(0, "DELETE")
                item.setForeground(0, TEXT_COLOR_DEL)
            else:
                item.setText(0, "KEEP")
                item.setForeground(0, TEXT_COLOR_KEEP)

    # ---------------------------------------------------- Load More
    def _load_more(self):
        """Load the next batch of groups beyond the current cap."""
        old_cap = self._cap_spin.value()
        self._cap_spin.setValue(old_cap + 500)

        # Resume batched populate from where we left off
        self._tree.setUpdatesEnabled(False)
        self._tree.blockSignals(True)

        try:
            self._tree.itemChanged.disconnect(self._on_item_changed)
        except TypeError:
            pass

        self._batch_timer = QTimer(self)
        self._batch_timer.setInterval(self.BATCH_DELAY_MS)
        self._batch_timer.timeout.connect(self._populate_next_batch)
        self._batch_timer.start()

    # ---------------------------------------------- double-click / context
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        if item.parent() is None:
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            _open_in_explorer(path)

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return

        # ---- Group header row (no parent) ----
        if item.parent() is None:
            self._show_group_context_menu(item, pos)
            return

        # ---- Individual file row ----
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path:
            return

        # Collect all selected file-level items (items with a parent)
        selected_file_items = [
            si for si in self._tree.selectedItems()
            if si.parent() is not None and si.data(0, Qt.ItemDataRole.UserRole)
        ]
        if not selected_file_items:
            selected_file_items = [item]

        n = len(selected_file_items)
        suffix = f" ({n} files)" if n > 1 else ""

        menu = QMenu(self)
        _fm = "Finder" if sys.platform == "darwin" else "Explorer"
        act_reveal = menu.addAction(f"📂  Show in {_fm} (select file)")
        act_open_dir = menu.addAction("📁  Open containing folder")
        menu.addSeparator()
        act_keep = menu.addAction(f"✅  Mark as KEEP{suffix}")
        act_delete = menu.addAction(f"🗑  Mark as DELETE{suffix}")

        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen == act_reveal:
            if os.path.exists(path):
                _open_in_explorer(path)
        elif chosen == act_open_dir:
            if os.path.exists(path):
                _open_directory(path)
        elif chosen == act_keep:
            self._tree.blockSignals(True)
            for si in selected_file_items:
                si.setCheckState(0, Qt.CheckState.Unchecked)
                si.setText(0, "KEEP")
                si.setForeground(0, TEXT_COLOR_KEEP)
            self._tree.blockSignals(False)
        elif chosen == act_delete:
            self._tree.blockSignals(True)
            for si in selected_file_items:
                si.setCheckState(0, Qt.CheckState.Checked)
                si.setText(0, "DELETE")
                si.setForeground(0, TEXT_COLOR_DEL)
            self._tree.blockSignals(False)

    def _show_group_context_menu(self, group_item: QTreeWidgetItem, pos):
        """Context menu for a group header – bulk actions for selected groups."""
        # Collect all selected top-level (group) items
        selected_groups = [
            si for si in self._tree.selectedItems()
            if si.parent() is None
        ]
        if not selected_groups:
            selected_groups = [group_item]

        n = len(selected_groups)
        suffix = f" ({n} groups)" if n > 1 else ""

        menu = QMenu(self)
        act_delete_all = menu.addAction(f"🗑  Mark ALL as DELETE{suffix}")
        act_keep_all = menu.addAction(f"✅  Mark ALL as KEEP{suffix}")
        menu.addSeparator()
        act_keep_oldest = menu.addAction(f"📅  Keep oldest, delete rest{suffix}")
        menu.addSeparator()
        act_expand = menu.addAction(f"▶  Expand{suffix}")
        act_collapse = menu.addAction(f"▼  Collapse{suffix}")

        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        for grp in selected_groups:
            gi = self._tree.indexOfTopLevelItem(grp)
            color = GROUP_COLORS[gi % len(GROUP_COLORS)]
            if chosen == act_delete_all:
                self._set_group_action(grp, color, delete_all=True)
            elif chosen == act_keep_all:
                self._set_group_action(grp, color, delete_all=False)
            elif chosen == act_keep_oldest:
                self._set_group_keep_oldest(grp, color)
            elif chosen == act_expand:
                grp.setExpanded(True)
            elif chosen == act_collapse:
                grp.setExpanded(False)

    def _set_group_action(self, group_item: QTreeWidgetItem, color: QColor, *, delete_all: bool):
        """Mark every file in a group as DELETE or KEEP."""
        self._tree.blockSignals(True)
        for ci in range(group_item.childCount()):
            child = group_item.child(ci)
            if delete_all:
                child.setCheckState(0, Qt.CheckState.Checked)
                child.setText(0, "DELETE")
                child.setForeground(0, TEXT_COLOR_DEL)
            else:
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setText(0, "KEEP")
                child.setForeground(0, TEXT_COLOR_KEEP)
        self._tree.blockSignals(False)

    def _set_group_keep_oldest(self, group_item: QTreeWidgetItem, color: QColor):
        """Keep the oldest (first) file, mark the rest as DELETE."""
        self._tree.blockSignals(True)
        for ci in range(group_item.childCount()):
            child = group_item.child(ci)
            if ci == 0:
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setText(0, "KEEP")
                child.setForeground(0, TEXT_COLOR_KEEP)
            else:
                child.setCheckState(0, Qt.CheckState.Checked)
                child.setText(0, "DELETE")
                child.setForeground(0, TEXT_COLOR_DEL)
        self._tree.blockSignals(False)

    # ---------------------------------------------- export report
    def _export_report(self):
        """Let the user export scan results to CSV or JSON."""
        if not self._groups:
            QMessageBox.information(self, "Nothing to export", "Run a scan first.")
            return

        # Gather current action state from tree
        actions = self._gather_actions()

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export scan report",
            f"FileDuplicator_Report_{datetime.now():%Y%m%d_%H%M%S}",
            "CSV Files (*.csv);;JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return

        try:
            if path.lower().endswith(".json"):
                self._export_json(path, actions)
            else:
                self._export_csv(path, actions)
            QMessageBox.information(self, "Export complete", f"Report saved to:\n{path}")
            self._status.showMessage(f"Report exported to {path}", 10_000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _gather_actions(self) -> dict[str, str]:
        """Read the current KEEP/DELETE state of every file row in the tree."""
        actions: dict[str, str] = {}  # path → "KEEP" or "DELETE"
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                p = child.data(0, Qt.ItemDataRole.UserRole)
                if p:
                    actions[p] = child.text(0)  # "KEEP" or "DELETE"
        return actions

    def _export_csv(self, path: str, actions: dict[str, str]):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # Metadata header rows
            w.writerow(["# FileDuplicator Scan Report"])
            w.writerow(["# Date", datetime.now().isoformat()])
            w.writerow(["# Directories", "; ".join(self._scan_roots)])
            if self._stats:
                w.writerow(["# Hash Algorithm", self._stats.hash_algorithm])
                w.writerow(["# Files Scanned", self._stats.total_files_scanned])
                w.writerow(["# Total Size Scanned", _human_size(self._stats.total_size_scanned)])
                w.writerow(["# Duplicate Groups", self._stats.duplicate_groups])
                w.writerow(["# Duplicate Files", self._stats.duplicate_files])
                w.writerow(["# Reclaimable", _human_size(self._stats.reclaimable_bytes)])
            w.writerow([])
            w.writerow(["Group", "Mode", "Action", "File Name", "Path", "Size (bytes)", "Size", "Hash", "Modified"])

            for idx, grp in enumerate(self._groups):
                sorted_files = sorted(grp.files, key=lambda fe: _safe_mtime(fe.path))
                for fe in sorted_files:
                    action = actions.get(fe.path, "")
                    h = fe.full_hash or fe.partial_hash or ""
                    w.writerow([
                        idx + 1,
                        grp.mode.name.capitalize(),
                        action,
                        fe.name,
                        fe.path,
                        fe.size,
                        _human_size(fe.size),
                        h,
                        datetime.fromtimestamp(_safe_mtime(fe.path)).isoformat() if _safe_mtime(fe.path) else "",
                    ])

    def _export_json(self, path: str, actions: dict[str, str]):
        report = {
            "tool": "FileDuplicator",
            "export_date": datetime.now().isoformat(),
            "scan_directories": self._scan_roots,
            "stats": None,
            "groups": [],
        }
        if self._stats:
            report["stats"] = {
                "hash_algorithm": self._stats.hash_algorithm,
                "total_files_scanned": self._stats.total_files_scanned,
                "total_size_scanned": self._stats.total_size_scanned,
                "total_size_scanned_h": _human_size(self._stats.total_size_scanned),
                "duplicate_groups": self._stats.duplicate_groups,
                "duplicate_files": self._stats.duplicate_files,
                "reclaimable_bytes": self._stats.reclaimable_bytes,
                "reclaimable_h": _human_size(self._stats.reclaimable_bytes),
            }
        for idx, grp in enumerate(self._groups):
            sorted_files = sorted(grp.files, key=lambda fe: _safe_mtime(fe.path))
            files = []
            for fe in sorted_files:
                action = actions.get(fe.path, "")
                files.append({
                    "path": fe.path,
                    "name": fe.name,
                    "size": fe.size,
                    "size_h": _human_size(fe.size),
                    "hash": fe.full_hash or fe.partial_hash or "",
                    "mtime": _safe_mtime(fe.path),
                    "action": action,
                })
            report["groups"].append({
                "index": idx + 1,
                "mode": grp.mode.name.capitalize(),
                "file_count": len(grp.files),
                "each_size_h": _human_size(grp.files[0].size) if grp.files else "",
                "files": files,
            })
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    # ---------------------------------------------- bulk selection
    def _auto_select_newer(self):
        self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                if ci == 0:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    child.setText(0, "KEEP")
                    child.setForeground(0, TEXT_COLOR_KEEP)
                else:
                    child.setCheckState(0, Qt.CheckState.Checked)
                    child.setText(0, "DELETE")
                    child.setForeground(0, TEXT_COLOR_DEL)
        self._tree.setUpdatesEnabled(True)
        self._tree.blockSignals(False)

    def _deselect_all(self):
        self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setText(0, "KEEP")
                child.setForeground(0, TEXT_COLOR_KEEP)
        self._tree.setUpdatesEnabled(True)
        self._tree.blockSignals(False)

    # ---------------------------------------------- search & sort
    def _on_search_changed(self, text: str):
        """Filter tree groups by file name or path containing the search text."""
        search = text.strip().lower()
        root = self._tree.invisibleRootItem()
        visible = 0
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            match = False
            if not search:
                match = True
            else:
                for ci in range(group_item.childCount()):
                    child = group_item.child(ci)
                    name = child.text(1).lower()
                    path = child.text(2).lower()
                    if search in name or search in path:
                        match = True
                        break
            group_item.setHidden(not match)
            if match:
                visible += 1

        if search:
            self._filter_label.setText(f"{visible:,} of {root.childCount():,} groups match")
        else:
            self._filter_label.setText("")

    def _on_sort_changed(self, index: int):
        """Re-sort groups and rebuild tree."""
        if not self._groups:
            return

        sort_funcs = {
            0: lambda g: -(g.files[0].size if g.files else 0),      # size desc
            1: lambda g: (g.files[0].size if g.files else 0),       # size asc
            2: lambda g: -len(g.files),                              # file count desc
            3: lambda g: len(g.files),                               # file count asc
            4: lambda g: (g.files[0].name.lower() if g.files else ""),  # name asc
            5: lambda g: (g.files[0].name.lower() if g.files else ""),  # name desc (reversed)
        }
        key_func = sort_funcs.get(index, sort_funcs[0])

        if index == 5:
            self._groups.sort(key=key_func, reverse=True)
        else:
            self._groups.sort(key=key_func)

        # Rebuild tree
        self._displayed = 0
        self._start_batched_populate()

    # ---------------------------------------------- compare reports
    def _compare_reports(self):
        """Compare two exported JSON reports to find differences."""
        file_a, _ = QFileDialog.getOpenFileName(
            self, "Select first report (e.g. xxHash scan)", "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not file_a:
            return
        file_b, _ = QFileDialog.getOpenFileName(
            self, "Select second report (e.g. SHA-256 scan)", "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not file_b:
            return

        try:
            with open(file_a, "r", encoding="utf-8") as f:
                report_a = json.load(f)
            with open(file_b, "r", encoding="utf-8") as f:
                report_b = json.load(f)

            hash_a = report_a.get("stats", {}).get("hash_algorithm", "Scan A") if report_a.get("stats") else "Scan A"
            hash_b = report_b.get("stats", {}).get("hash_algorithm", "Scan B") if report_b.get("stats") else "Scan B"

            groups_a_count = len(report_a.get("groups", []))
            groups_b_count = len(report_b.get("groups", []))

            # Build file-path-set keyed groups
            def group_keys(report):
                d = {}
                for g in report.get("groups", []):
                    paths = tuple(sorted(f["path"] for f in g.get("files", [])))
                    d[paths] = g
                return d

            keys_a = group_keys(report_a)
            keys_b = group_keys(report_b)

            only_a = [keys_a[k] for k in keys_a if k not in keys_b]
            only_b = [keys_b[k] for k in keys_b if k not in keys_a]

            # Build result text
            lines = []
            lines.append(f"═══ Comparison: {hash_a} vs {hash_b} ═══\n")
            lines.append(f"Scan A ({hash_a}):  {groups_a_count:,} groups")
            lines.append(f"Scan B ({hash_b}):  {groups_b_count:,} groups")
            lines.append(f"Delta:  {groups_b_count - groups_a_count:+,} groups\n")

            if only_a:
                lines.append(f"⚠ {len(only_a)} group(s) ONLY in {hash_a} (likely false positives):")
                for g in only_a[:50]:
                    lines.append(f"  Group {g.get('index', '?')} — {g.get('file_count', '?')} files · {g.get('each_size_h', '?')} each")
                    for ff in g.get("files", [])[:5]:
                        lines.append(f"    → {ff.get('name', '')}  —  {ff.get('path', '')}")
                lines.append("")

            if only_b:
                lines.append(f"ℹ {len(only_b)} group(s) ONLY in {hash_b}:")
                for g in only_b[:50]:
                    lines.append(f"  Group {g.get('index', '?')} — {g.get('file_count', '?')} files · {g.get('each_size_h', '?')} each")
                    for ff in g.get("files", [])[:5]:
                        lines.append(f"    → {ff.get('name', '')}  —  {ff.get('path', '')}")
                lines.append("")

            if not only_a and not only_b:
                lines.append("✅ Both scans found identical duplicate groups!")

            result = "\n".join(lines)

            # Show in a scrollable dialog
            from PyQt6.QtWidgets import QDialog, QTextEdit, QDialogButtonBox
            dlg = QDialog(self)
            dlg.setWindowTitle("Compare Reports")
            dlg.resize(900, 600)
            layout = QVBoxLayout(dlg)
            text = QTextEdit()
            text.setReadOnly(True)
            text.setPlainText(result)
            text.setStyleSheet("font-family: monospace; font-size: 12px;")
            layout.addWidget(text)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
            bb.accepted.connect(dlg.accept)
            layout.addWidget(bb)
            dlg.exec()

        except Exception as exc:
            QMessageBox.critical(self, "Compare error", str(exc))

    # ---------------------------------------------- deletion
    def _delete_selected(self):
        to_delete: list[tuple[str, QTreeWidgetItem]] = []
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                if child.checkState(0) == Qt.CheckState.Checked:
                    path = child.data(0, Qt.ItemDataRole.UserRole)
                    if path:
                        to_delete.append((path, child))

        if not to_delete:
            QMessageBox.information(self, "Nothing selected", "No files are marked for deletion.")
            return

        total_size = sum(os.path.getsize(p) for p, _ in to_delete if os.path.exists(p))
        reply = QMessageBox.warning(
            self,
            "Confirm deletion",
            f"Permanently delete {len(to_delete)} file(s)?\n\n"
            f"This will free ~{_human_size(total_size)}.\n\n"
            "This action CANNOT be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted_paths: list[str] = []
        deleted_info: list[dict] = []  # for export
        errors = []
        for p, child in to_delete:
            try:
                # Capture info BEFORE deleting (file won't exist after)
                info = {
                    "path": p,
                    "name": os.path.basename(p),
                    "size": os.path.getsize(p) if os.path.exists(p) else 0,
                }
                # Find matching FileEntry for hash info
                for grp in self._groups:
                    for fe in grp.files:
                        if fe.path == p:
                            info["size"] = fe.size
                            info["size_h"] = _human_size(fe.size)
                            info["hash"] = fe.full_hash or fe.partial_hash or ""
                            info["group_mode"] = grp.mode.name.capitalize()
                            break
                    else:
                        continue
                    break
                if "size_h" not in info:
                    info["size_h"] = _human_size(info["size"])
                    info["hash"] = ""
                    info["group_mode"] = ""

                os.remove(p)
                deleted_paths.append(p)
                deleted_info.append(info)
                parent = child.parent()
                if parent:
                    parent.removeChild(child)
            except Exception as exc:
                errors.append(f"{p}: {exc}")

        # Remove groups with ≤1 file remaining
        for gi in reversed(range(root.childCount())):
            group_item = root.child(gi)
            if group_item.childCount() <= 1:
                root.removeChild(group_item)

        # Update in-memory groups so sort/export/load-more stay consistent
        deleted_set = set(deleted_paths)
        for grp in self._groups:
            grp.files = [fe for fe in grp.files if fe.path not in deleted_set]
        self._groups = [grp for grp in self._groups if len(grp.files) > 1]

        # Show result dialog with Export option
        self._show_deletion_result(len(deleted_paths), total_size, errors, deleted_info)
        self._status.showMessage(f"Deleted {len(deleted_paths)} files.", 10_000)

    def _show_deletion_result(self, count: int, total_size: int, errors: list[str], deleted_info: list[dict]):
        """Show deletion results with an option to export what was deleted."""
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Deletion complete")
        dlg.setIcon(QMessageBox.Icon.Information)

        msg = f"Deleted {count} file(s) ({_human_size(total_size)} freed)."
        if errors:
            msg += f"\n\n{len(errors)} error(s):\n" + "\n".join(errors[:20])
        dlg.setText(msg)

        # Add custom buttons
        export_btn = dlg.addButton("📋  Export Deletion Report", QMessageBox.ButtonRole.ActionRole)
        ok_btn = dlg.addButton(QMessageBox.StandardButton.Ok)
        dlg.setDefaultButton(ok_btn)

        dlg.exec()

        if dlg.clickedButton() == export_btn:
            self._export_deletion_report(deleted_info, errors)

    def _export_deletion_report(self, deleted_info: list[dict], errors: list[str]):
        """Save a CSV/JSON report of what was deleted."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export deletion report",
            f"FileDuplicator_Deleted_{datetime.now():%Y%m%d_%H%M%S}",
            "CSV Files (*.csv);;JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return

        try:
            if path.lower().endswith(".json"):
                report = {
                    "tool": "FileDuplicator",
                    "report_type": "deletion",
                    "deletion_date": datetime.now().isoformat(),
                    "scan_directories": self._scan_roots,
                    "hash_algorithm": self._stats.hash_algorithm if self._stats else "",
                    "files_deleted": len(deleted_info),
                    "total_freed_bytes": sum(d["size"] for d in deleted_info),
                    "total_freed_h": _human_size(sum(d["size"] for d in deleted_info)),
                    "deleted_files": deleted_info,
                    "errors": errors,
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, ensure_ascii=False)
            else:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["# FileDuplicator Deletion Report"])
                    w.writerow(["# Date", datetime.now().isoformat()])
                    w.writerow(["# Directories", "; ".join(self._scan_roots)])
                    w.writerow(["# Hash Algorithm", self._stats.hash_algorithm if self._stats else ""])
                    w.writerow(["# Files Deleted", len(deleted_info)])
                    w.writerow(["# Space Freed", _human_size(sum(d["size"] for d in deleted_info))])
                    if errors:
                        w.writerow(["# Errors", len(errors)])
                    w.writerow([])
                    w.writerow(["File Name", "Path", "Size (bytes)", "Size", "Hash", "Mode"])
                    for d in deleted_info:
                        w.writerow([
                            d["name"],
                            d["path"],
                            d["size"],
                            d.get("size_h", ""),
                            d.get("hash", ""),
                            d.get("group_mode", ""),
                        ])

            QMessageBox.information(self, "Export complete", f"Deletion report saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))
