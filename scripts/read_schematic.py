#!/usr/bin/env python3
"""Read-only schematic extractor CLI.

Pulls a single (lib, cell) schematic out of Virtuoso through SafeBridge
(PDK-sanitized) and emits it in an LLM-friendly format. Never calls
set_params / simulate / Maestro writeback.

Default Markdown output is byte-for-byte identical to the topology
section CircuitAgent feeds the LLM — it is exactly
``CircuitAgent._format_topology(circuit)`` with no additions. Pass
``--with-summary`` to append a human-oriented ``### Summary`` tally
(NOT sent to the agent).

Usage:
    python scripts/read_schematic.py --lib pllLib --cell LC_VCO
    python scripts/read_schematic.py --lib pllLib --cell LC_VCO \\
        --format both --output ./out/lc_vco
    python scripts/read_schematic.py --lib pllLib --cell LC_VCO \\
        --remote-skill-dir /project/.../skill
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from virtuoso_bridge import VirtuosoClient

from src.agent import CircuitAgent
from src.safe_bridge import SafeBridge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only schematic extractor. Emits a sanitized, "
            "LLM-friendly dump of a single (lib, cell)."
        )
    )
    parser.add_argument("--lib", required=True, help="Virtuoso library name")
    parser.add_argument("--cell", required=True, help="Cell name to read")
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "both"],
        default="markdown",
        help=(
            "Output format. 'markdown' matches exactly what the agent "
            "feeds the LLM; 'json' is the raw sanitized structure for "
            "audit/diff; 'both' writes both files. Default: markdown."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output path. For --format markdown|json: a file path "
            "(or '-' for stdout, default). For --format both: REQUIRED "
            "directory path; <cell>.md and <cell>.json are written "
            "there. 'both' does not support stdout."
        ),
    )
    parser.add_argument(
        "--with-summary",
        action="store_true",
        help=(
            "Append a human-oriented '### Summary' cell-type tally to "
            "the Markdown output. Disabled by default so that the "
            "Markdown matches byte-for-byte what the agent feeds the "
            "LLM. Does not affect JSON output."
        ),
    )
    parser.add_argument(
        "--pdk-map",
        default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
        help="Path to PDK map YAML",
    )
    parser.add_argument(
        "--remote-skill-dir",
        default=None,
        help=(
            "POSIX path on the remote host where safe_*.il SKILL helpers "
            "live. If omitted, SafeBridge falls back to a local PC-side "
            "skill dir (will NOT load on remote host)."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: config/.env)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    return parser.parse_args()


def _render_markdown(circuit: dict) -> str:
    """Return EXACTLY what CircuitAgent.run() feeds to the LLM.

    This must stay byte-for-byte identical to the topology string used
    in agent.py's first-turn prompt. Any addendum (e.g. summary tally)
    belongs to a separate, explicitly opt-in artifact — never mixed
    into the default payload.
    """
    return CircuitAgent._format_topology(circuit)


def _render_summary(circuit: dict) -> str:
    """One-line cell-type tally, appended to Markdown as a global hint
    before LLM reasoning (e.g. '3 NMOS, 2 PMOS, 1 inductor')."""
    tally: dict[str, int] = {}
    for inst in circuit.get("instances", []):
        cell = inst.get("cell", "?")
        tally[cell] = tally.get(cell, 0) + 1
    if not tally:
        return ""
    parts = [f"{n} {c}" for c, n in sorted(tally.items())]
    return "\n\n### Summary\n" + ", ".join(parts)


def _write(out_path: Path, text: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("read_schematic")

    # --format both has strict output semantics: a real directory is
    # required and stdout is not supported. Validate up front so we
    # don't silently write into a directory literally named '-' or
    # dump generated files into an un-ignored default location.
    if args.format == "both":
        if args.output is None or args.output == "-":
            logger.error(
                "--format both requires an explicit --output DIR "
                "(stdout and default location are not supported)."
            )
            return 2

    env_file = args.env_file or str(PROJECT_ROOT / "config" / ".env")
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("Loaded env from %s", env_path)

    pdk_map_path = Path(args.pdk_map)
    if not pdk_map_path.exists():
        logger.error("PDK map file not found: %s", pdk_map_path)
        return 1

    logger.info("Connecting to Virtuoso bridge (read-only)...")
    client = VirtuosoClient.from_env()

    # Stage 1 rev 2 (2026-04-18): the spectre parameter was dropped from
    # SafeBridge along with bridge.simulate(); read_schematic never used
    # it anyway.
    bridge = SafeBridge(
        client,
        str(pdk_map_path),
        remote_skill_dir=args.remote_skill_dir,
    )
    # Lock to the single (lib, cell) — same scope guard the agent uses.
    bridge.set_scope(args.lib, args.cell)
    logger.info("Scope bound: %s/%s", args.lib, args.cell)

    logger.info("Reading circuit (sanitized)...")
    circuit = bridge.read_circuit(args.lib, args.cell)

    # Default Markdown: EXACTLY what agent.run() sends to the LLM.
    # Summary is human-only and strictly opt-in via --with-summary.
    md_text = _render_markdown(circuit)
    if args.with_summary:
        md_text += _render_summary(circuit)
    md_text += "\n"
    json_text = json.dumps(circuit, indent=2, ensure_ascii=False) + "\n"

    fmt = args.format
    out = args.output

    if fmt == "both":
        # Validated above: out is a real directory path.
        out_dir = Path(out)
        md_path = out_dir / f"{args.cell}.md"
        json_path = out_dir / f"{args.cell}.json"
        _write(md_path, md_text)
        _write(json_path, json_text)
        logger.info("Wrote %s", md_path)
        logger.info("Wrote %s", json_path)
        return 0

    payload = md_text if fmt == "markdown" else json_text
    if out is None or out == "-":
        sys.stdout.write(payload)
        return 0

    _write(Path(out), payload)
    logger.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
