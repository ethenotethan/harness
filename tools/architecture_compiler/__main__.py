"""``python3 -m tools.architecture_compiler <service-root> [--check] [--quiet]``"""
from __future__ import annotations

import sys

from .core import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
