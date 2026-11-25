"""PyInstaller entry script for openscope-gui.exe (stage-220). app.py uses
relative imports, so it cannot be the entry script itself."""
import sys

from openscope_gui.app import main

if __name__ == "__main__":
    sys.exit(main())
