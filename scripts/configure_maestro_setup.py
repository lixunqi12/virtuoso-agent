#!/usr/bin/env python3
"""Canonical CLI entrypoint for generic Maestro setup recipes.

This wraps ``configure_maestro_outputs.py`` after that script grew beyond
outputs-only setup. Keep the old filename for backward compatibility; use this
entrypoint for new recipes that include design variables and verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from configure_maestro_outputs import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
