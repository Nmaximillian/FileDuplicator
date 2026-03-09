"""
Main window – PyQt6 UI for the duplicate-file scanner.
"""

from __future__ import annotations

import os
import locale
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import (
    Qt,
    QThread,
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
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scanner import DuplicateGroup, DuplicateMode, FileEntry, scan_directory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:,.1f} PB"


def _icon_path() -> str:
    """Return the path to the .ico file, works both in dev and PyInstaller bundle."""
    if getattr(sys, '_MEIPASS', None):
        return os.path.join(sys._MEIPASS, 'FileDuplicator.ico')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FileDuplicator.ico')


def _open_in_explorer(file_path: str):
    """Open Windows Explorer and select the given file."""
    try:
        subprocess.Popen(['explorer', '/select,', os.path.normpath(file_path)])
    except Exception:
        pass


def _open_directory(file_path: str):
    """Open the containing directory in Explorer."""
    try:
        folder = os.path.dirname(file_path)
        os.startfile(folder)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker thread – runs the scanner off the UI thread
# ---------------------------------------------------------------------------
class ScanWorker(QThread):
    progress = pyqtSignal(str, int, int)    # phase, current, total
    finished = pyqtSignal(list)             # list[DuplicateGroup]
    error    = pyqtSignal(str)

    def __init__(
        self,
        root: str,
        by_name: bool,
        by_size: bool,
        by_hash: bool,
        recursive: bool,
        min_size: int,
    ):
        super().__init__()
        self.root = root
        self.by_name = by_name
        self.by_size = by_size
        self.by_hash = by_hash
        self.recursive = recursive
        self.min_size = min_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            groups = scan_directory(
                self.root,
                by_name=self.by_name,
                by_size=self.by_size,
                by_hash=self.by_hash,
                recursive=self.recursive,
                min_size=self.min_size,
                progress=lambda phase, cur, tot: self.progress.emit(phase, cur, tot),
                cancelled=lambda: self._cancelled,
            )
            if not self._cancelled:
                self.finished.emit(groups)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Colours for alternating groups (dark-theme friendly)
# ---------------------------------------------------------------------------
GROUP_COLORS = [
    QColor("#1a3a4a"),  # dark teal
    QColor("#3a2a1a"),  # dark amber
    QColor("#1a3a1a"),  # dark green
    QColor("#3a1a2a"),  # dark rose
    QColor("#2a1a3a"),  # dark purple
    QColor("#3a3a1a"),  # dark olive
    QColor("#1a2a3a"),  # dark steel
    QColor("#2a1a2a"),  # dark plum
]

TEXT_COLOR_LIGHT = QColor("#e0e0e0")
TEXT_COLOR_KEEP  = QColor("#81c784")   # green accent for KEEP
TEXT_COLOR_DEL   = QColor("#ef9a9a")   # red accent for DELETE
GROUP_HEADER_COLOR = QColor("#ffffff")


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("File Duplicator – Duplicate Finder")
        self.resize(1200, 750)

        # Set window icon
        ico = _icon_path()
        if os.path.isfile(ico):
            self.setWindowIcon(QIcon(ico))

        self._worker: ScanWorker | None = None
        self._groups: list[DuplicateGroup] = []

        self._build_ui()
        self._restore_state()

    # ----- UI construction --------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)

        # --- Directory picker ---
        dir_group = QGroupBox("Directory")
        dir_layout = QHBoxLayout(dir_group)
        self._dir_edit = QLineEdit()
        self._dir_edit.setPlaceholderText("Choose a folder to scan…")
        dir_layout.addWidget(self._dir_edit, 1)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self._browse)
        dir_layout.addWidget(self._browse_btn)
        root_layout.addWidget(dir_group)

        # --- Options ---
        opts_group = QGroupBox("Scan options")
        opts_layout = QHBoxLayout(opts_group)

        self._chk_name = QCheckBox("By name")
        self._chk_size = QCheckBox("By size")
        self._chk_size.setChecked(True)
        self._chk_hash = QCheckBox("By hash (content)")
        self._chk_hash.setChecked(True)
        self._chk_recursive = QCheckBox("Recursive")
        self._chk_recursive.setChecked(True)

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
        self._min_size_combo.setCurrentIndex(2)  # 1 KB default
        opts_layout.addWidget(self._min_size_combo)

        root_layout.addWidget(opts_group)

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
        root_layout.addWidget(self._tree, 1)

        # --- Bottom action bar ---
        bottom = QHBoxLayout()
        self._summary_label = QLabel("")
        bottom.addWidget(self._summary_label, 1)

        self._select_all_delete = QPushButton("Select All Newer as Delete")
        self._select_all_delete.setToolTip(
            "For each group, mark the newer files for deletion and keep the oldest."
        )
        self._select_all_delete.clicked.connect(self._auto_select_newer)
        bottom.addWidget(self._select_all_delete)

        self._select_all_keep = QPushButton("Deselect All")
        self._select_all_keep.clicked.connect(self._deselect_all)
        bottom.addWidget(self._select_all_keep)

        self._delete_btn = QPushButton("🗑  Delete Selected")
        self._delete_btn.setStyleSheet("color: #d32f2f; font-weight: bold;")
        self._delete_btn.clicked.connect(self._delete_selected)
        bottom.addWidget(self._delete_btn)

        root_layout.addLayout(bottom)

        # --- Status bar ---
        self._status = QStatusBar()
        self.setStatusBar(self._status)

    # ----- State persistence ------------------------------------------------
    def _save_state(self):
        s = QSettings("FileDuplicator", "MainWindow")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("last_dir", self._dir_edit.text())

    def _restore_state(self):
        s = QSettings("FileDuplicator", "MainWindow")
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        last = s.value("last_dir", "")
        if last:
            self._dir_edit.setText(last)

    def closeEvent(self, event):
        self._save_state()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)

    # ----- Slots ------------------------------------------------------------
    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select directory", self._dir_edit.text())
        if d:
            self._dir_edit.setText(d)

    def _parse_min_size(self) -> int:
        text = self._min_size_combo.currentText()
        if "all" in text:
            return 0
        parts = text.split()
        val = float(parts[0])
        unit = parts[1].upper()
        multiplier = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        return int(val * multiplier.get(unit, 1))

    def _start_scan(self):
        root = self._dir_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Invalid directory", "Please choose a valid directory.")
            return

        by_name = self._chk_name.isChecked()
        by_size = self._chk_size.isChecked()
        by_hash = self._chk_hash.isChecked()
        if not (by_name or by_size or by_hash):
            QMessageBox.warning(self, "No criteria", "Select at least one duplicate criterion.")
            return

        self._tree.clear()
        self._groups.clear()
        self._progress.setValue(0)
        self._scan_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._summary_label.setText("")

        self._worker = ScanWorker(
            root,
            by_name=by_name,
            by_size=by_size,
            by_hash=by_hash,
            recursive=self._chk_recursive.isChecked(),
            min_size=self._parse_min_size(),
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
            self._progress.setMaximum(0)  # indeterminate

    @pyqtSlot(list)
    def _on_finished(self, groups: list[DuplicateGroup]):
        self._scan_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._groups = groups
        self._populate_tree(groups)
        total_dupes = sum(len(g.files) - 1 for g in groups)
        total_waste = sum(
            sum(f.size for f in g.files[1:]) for g in groups
        )
        self._summary_label.setText(
            f"Found {len(groups)} duplicate groups  •  "
            f"{total_dupes} duplicate files  •  "
            f"{_human_size(total_waste)} reclaimable"
        )
        self._progress.setValue(self._progress.maximum() or 1)
        self._phase_label.setText("Scan complete.")
        self._status.showMessage(f"Scan complete – {len(groups)} groups found.", 10_000)

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        self._scan_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        QMessageBox.critical(self, "Scan error", msg)

    # ----- Tree population --------------------------------------------------
    def _populate_tree(self, groups: list[DuplicateGroup]):
        self._tree.clear()
        self._tree.setUpdatesEnabled(False)

        for idx, grp in enumerate(groups):
            color = GROUP_COLORS[idx % len(GROUP_COLORS)]
            mode_label = grp.mode.name.capitalize()

            group_item = QTreeWidgetItem(self._tree)
            group_item.setText(
                1,
                f"[{mode_label}] Group {idx + 1}  –  "
                f"{len(grp.files)} files  •  {_human_size(grp.files[0].size)} each",
            )
            group_item.setExpanded(True)
            group_item.setFirstColumnSpanned(False)
            for col in range(5):
                group_item.setBackground(col, color)
                group_item.setForeground(col, GROUP_HEADER_COLOR)

            # Sort files: oldest first (so first = "original")
            sorted_files = sorted(grp.files, key=lambda f: _safe_mtime(f.path))

            for fi, fe in enumerate(sorted_files):
                child = QTreeWidgetItem(group_item)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # First file defaults to KEEP, rest default to DELETE
                if fi == 0:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    child.setText(0, "KEEP")
                else:
                    child.setCheckState(0, Qt.CheckState.Checked)
                    child.setText(0, "DELETE")
                child.setText(1, fe.name)
                child.setText(2, fe.path)
                child.setText(3, _human_size(fe.size))
                child.setText(4, fe.full_hash[:12] if fe.full_hash else fe.partial_hash[:12] if fe.partial_hash else "")
                child.setData(0, Qt.ItemDataRole.UserRole, fe.path)
                for col in range(5):
                    child.setBackground(col, color)
                    child.setForeground(col, TEXT_COLOR_LIGHT)
                # Highlight the action label
                child.setForeground(0, TEXT_COLOR_KEEP if fi == 0 else TEXT_COLOR_DEL)

        self._tree.setUpdatesEnabled(True)

        # Update check-state labels when user toggles
        self._tree.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if column == 0 and item.parent() is not None:
            if item.checkState(0) == Qt.CheckState.Checked:
                item.setText(0, "DELETE")
                item.setForeground(0, TEXT_COLOR_DEL)
            else:
                item.setText(0, "KEEP")
                item.setForeground(0, TEXT_COLOR_KEEP)

    # ----- Double-click & right-click --------------------------------------
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """Double-click a file row → open Explorer with file selected."""
        if item.parent() is None:
            return  # group header, ignore
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            _open_in_explorer(path)

    def _on_context_menu(self, pos):
        """Right-click context menu on file rows."""
        item = self._tree.itemAt(pos)
        if item is None or item.parent() is None:
            return  # nothing or group header
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path:
            return

        menu = QMenu(self)
        act_reveal = menu.addAction("📂  Show in Explorer (select file)")
        act_open_dir = menu.addAction("📁  Open containing folder")
        menu.addSeparator()
        if item.checkState(0) == Qt.CheckState.Checked:
            act_toggle = menu.addAction("✅  Mark as KEEP")
        else:
            act_toggle = menu.addAction("🗑  Mark as DELETE")

        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen == act_reveal:
            if os.path.exists(path):
                _open_in_explorer(path)
        elif chosen == act_open_dir:
            if os.path.exists(path):
                _open_directory(path)
        elif chosen == act_toggle:
            if item.checkState(0) == Qt.CheckState.Checked:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setCheckState(0, Qt.CheckState.Checked)

    # ----- Bulk selection helpers -------------------------------------------
    def _auto_select_newer(self):
        """Mark all but the oldest file in each group for deletion."""
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                if ci == 0:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                else:
                    child.setCheckState(0, Qt.CheckState.Checked)

    def _deselect_all(self):
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                group_item.child(ci).setCheckState(0, Qt.CheckState.Unchecked)

    # ----- Deletion ---------------------------------------------------------
    def _delete_selected(self):
        to_delete: list[str] = []
        root = self._tree.invisibleRootItem()
        for gi in range(root.childCount()):
            group_item = root.child(gi)
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                if child.checkState(0) == Qt.CheckState.Checked:
                    path = child.data(0, Qt.ItemDataRole.UserRole)
                    if path:
                        to_delete.append(path)

        if not to_delete:
            QMessageBox.information(self, "Nothing selected", "No files are marked for deletion.")
            return

        total_size = sum(os.path.getsize(p) for p in to_delete if os.path.exists(p))
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

        deleted = 0
        errors = []
        for p in to_delete:
            try:
                os.remove(p)
                deleted += 1
            except Exception as exc:
                errors.append(f"{p}: {exc}")

        # Remove deleted items from tree
        for gi in reversed(range(root.childCount())):
            group_item = root.child(gi)
            for ci in reversed(range(group_item.childCount())):
                child = group_item.child(ci)
                path = child.data(0, Qt.ItemDataRole.UserRole)
                if path in to_delete and os.path.exists(path) is False:
                    group_item.removeChild(child)
            # Remove group if only 1 or 0 files remain
            if group_item.childCount() <= 1:
                root.removeChild(group_item)

        msg = f"Deleted {deleted} file(s)."
        if errors:
            msg += f"\n\n{len(errors)} error(s):\n" + "\n".join(errors[:20])
        QMessageBox.information(self, "Deletion complete", msg)
        self._status.showMessage(f"Deleted {deleted} files.", 10_000)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0
