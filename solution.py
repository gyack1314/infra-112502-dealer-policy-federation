#!/usr/bin/env python3
"""Conventional entry-point alias for ``python solution.py``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
