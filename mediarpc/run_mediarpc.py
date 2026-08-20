"""PyInstaller / launcher entry point for MediaRPC.

A thin bootstrap with an absolute import so PyInstaller (which runs the entry as
the top-level `__main__`, with no parent package) can resolve the package. The
repo root is added to sys.path so `import mediarpc` works when this file is run
directly from inside the package directory. For development you can equivalently
run:  python -m mediarpc
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mediarpc.app import main

if __name__ == "__main__":
    main()
