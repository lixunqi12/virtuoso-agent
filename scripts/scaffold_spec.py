#!/usr/bin/env python3
"""CLI: generate a new spec Markdown skeleton for a (lib, cell, tb_cell) pair.

Walks the DUT + testbench cellviews over remote host via
``SafeBridge.generate_spec_scaffold`` (no instance properties / model
cards leave remote host), optionally parses a Maestro ``input.scs`` for design
variables + analyses, and writes a 5-section scaffold with
``<TODO>`` placeholders to ``--out``.

Usage::

    python scripts/scaffold_spec.py \\
        --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \\
        --out specs/lc_vco.md \\
        --remote-skill-dir /proj/.../skill \\
        --scs-path /home/<user>/simulation/.../input.scs
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from virtuoso_bridge import VirtuosoClient  # noqa: E402

from src.safe_bridge import SafeBridge  # noqa: E402
from src.spec_scaffold import render_spec_scaffold  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a spec Markdown scaffold from a (lib, cell, tb_cell) "
            "triple. Output is a 5-section skeleton with <TODO> "
            "placeholders; the user fills in topology / eval block / "
            "pass ranges / caveats by hand."
        )
    )
    parser.add_argument("--lib", required=True, help="Virtuoso library name")
    parser.add_argument("--cell", required=True, help="DUT cell name")
    parser.add_argument(
        "--tb-cell", required=True, help="Maestro testbench cell name"
    )
    parser.add_argument(
        "--out", required=True, help="Output Markdown file path"
    )
    parser.add_argument(
        "--pdk-map",
        default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
        help="Path to PDK map YAML (default: config/pdk_map.yaml)",
    )
    parser.add_argument(
        "--remote-skill-dir",
        default=None,
        help=(
            "POSIX path on remote host where safe_*.il SKILL helpers live. "
            "Required for scaffold generation."
        ),
    )
    parser.add_argument(
        "--scs-path",
        default=None,
        help=(
            "POSIX path on remote host to Maestro's ExplorerRun input.scs. "
            "When supplied, discovered design variables + analyses are "
            "baked into the scaffold; otherwise those sections emit "
            "placeholder rows for manual fill-in."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --out if it already exists.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    args = parser.parse_args()

    _MSYS_PREFIX = "C:/msys64"
    for _attr in ("scs_path", "remote_skill_dir"):
        _val = getattr(args, _attr, None)
        if isinstance(_val, str) and _val.startswith(_MSYS_PREFIX + "/"):
            setattr(args, _attr, _val[len(_MSYS_PREFIX):])
    return args


def main() -> int:
    args = parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("scaffold_spec")

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        logger.error(
            "Output file already exists (%s). Re-run with --force to "
            "overwrite.", out_path,
        )
        return 1

    pdk_map_path = Path(args.pdk_map)
    if not pdk_map_path.exists():
        logger.error("PDK map file not found: %s", pdk_map_path)
        return 1

    logger.info("Connecting to Virtuoso bridge...")
    client = VirtuosoClient.from_env()
    bridge = SafeBridge(
        client,
        str(pdk_map_path),
        remote_skill_dir=args.remote_skill_dir,
    )
    logger.info(
        "SafeBridge initialized (remote_skill_dir=%s)",
        args.remote_skill_dir or "<PC fallback>",
    )

    logger.info(
        "Collecting scaffold data for %s/%s (tb=%s)%s",
        args.lib, args.cell, args.tb_cell,
        f", scs={args.scs_path}" if args.scs_path else "",
    )
    scaffold = bridge.generate_spec_scaffold(
        lib=args.lib,
        cell=args.cell,
        tb_cell=args.tb_cell,
        scs_path=args.scs_path,
    )

    markdown = render_spec_scaffold(scaffold)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info(
        "Wrote scaffold (%d chars, %d pins DUT + %d pins TB, "
        "%d desVars, %d analyses) to %s",
        len(markdown),
        len(scaffold.get("dut", {}).get("pins") or []),
        len(scaffold.get("tb", {}).get("pins") or []),
        len(scaffold.get("design_vars") or []),
        len(scaffold.get("analyses") or []),
        out_path,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger("scaffold_spec").error(
            "Scaffold generation crashed (%s: %s)",
            type(exc).__name__, exc,
        )
        raise
