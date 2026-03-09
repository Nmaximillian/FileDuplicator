"""
File Duplicator – entry point.
"""

import os
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QIcon
from main_window import MainWindow, _icon_path


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("File Duplicator")
    app.setOrganizationName("FileDuplicator")
    app.setStyle("Fusion")

    # App-wide icon (taskbar, alt-tab, etc.)
    ico = _icon_path()
    if os.path.isfile(ico):
        app.setWindowIcon(QIcon(ico))

    # Nice default font
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
