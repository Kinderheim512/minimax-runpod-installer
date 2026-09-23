"""Entry point for the packaged OpenFoxForge.exe (PyInstaller target)."""

import sys

from launcher.gui import main

if __name__ == "__main__":
    sys.exit(main())
