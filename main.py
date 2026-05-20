#!/usr/bin/env python3
"""Dev-mode shim — allows `python main.py` from the repo root without installing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from wkrt.cli import main

if __name__ == "__main__":
    main()
