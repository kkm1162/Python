from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from logexpert.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

# EXE build command example (PyInstaller):
# python -m PyInstaller --noconsole --onefile --icon="LogExpert.ico" --name "LogExpert" main.py