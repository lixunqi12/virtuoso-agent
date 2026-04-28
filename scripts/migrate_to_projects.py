"""One-shot migration: scatter the legacy flat layout into projects/.

Layout source -> destination::

  config/LC_VCO_spec.md          -> projects/lc_vco_base/constraints/spec.md
  config/LC_VCO_40G_spec.md      -> projects/lc_vco_40g/constraints/spec.md
  config/delay_test_spec.md      -> projects/cobi_delay/constraints/spec.md
  config/matching_test_spec.md   -> projects/cobi_matching/constraints/spec.md

  specs_work/netlist.scrubbed.sp        -> projects/cobi_matching/circuit/
  specs_work/netlist.scrubbed.sp        -> projects/cobi_delay/circuit/   (copy 2)
  specs_work/edge_close_new.scrubbed.sp -> projects/cobi_matching/circuit/

  logs/* (Apr 19 - Apr 22 verify_*/run_*) -> projects/_legacy/logs/agent/

  config/logs/hspice_transcript_20260426_*.jsonl -> projects/cobi_delay/logs/hspice/
  config/logs/hspice_transcript_20260427_*.jsonl -> projects/cobi_matching/logs/hspice/

The migration is COPY-only. Source files are untouched. After the
operator confirms the new layout works, they may delete the legacy
locations by hand (or pass ``--prune`` to delete the source side
once each individual file has been verified copied).

Run with ``--dry-run`` to see what would be copied.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parent.parent

# Make ``src.project`` importable when running this script directly.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.project import LEGACY_PROJECT, Project  # noqa: E402


@dataclass
class CopyTask:
    src: Path
    dst: Path
    label: str

    def render(self) -> str:
        try:
            r_src = self.src.relative_to(REPO_ROOT)
        except ValueError:
            r_src = self.src
        try:
            r_dst = self.dst.relative_to(REPO_ROOT)
        except ValueError:
            r_dst = self.dst
        return f"  [{self.label}] {r_src} -> {r_dst}"


def _project(name: str) -> Project:
    return Project.from_repo(name, repo_root=REPO_ROOT)


def _legacy() -> Project:
    return Project.from_repo(LEGACY_PROJECT, repo_root=REPO_ROOT)


def build_plan() -> list[CopyTask]:
    tasks: list[CopyTask] = []

    # 1. Spec files -> constraints/spec.md per project
    spec_map = [
        ("config/LC_VCO_spec.md",       "lc_vco_base"),
        ("config/LC_VCO_40G_spec.md",   "lc_vco_40g"),
        ("config/delay_test_spec.md",   "cobi_delay"),
        ("config/matching_test_spec.md", "cobi_matching"),
    ]
    for rel, proj_name in spec_map:
        src = REPO_ROOT / rel
        if src.is_file():
            dst = _project(proj_name).spec_file
            tasks.append(CopyTask(src=src, dst=dst, label="spec"))

    # 2. specs_work/ scrubbed netlists.
    #    netlist.scrubbed.sp is the matching_test cell — same DUT under
    #    both delay and matching specs, so each project gets its own copy
    #    (independent project rule: no sharing).
    #    edge_close_new.scrubbed.sp is the matching project's testbench.
    netlist = REPO_ROOT / "specs_work" / "netlist.scrubbed.sp"
    if netlist.is_file():
        for proj_name in ("cobi_delay", "cobi_matching"):
            dst = _project(proj_name).circuit_dir / "netlist.sp"
            tasks.append(CopyTask(src=netlist, dst=dst, label="circuit"))
    edge_close = REPO_ROOT / "specs_work" / "edge_close_new.scrubbed.sp"
    if edge_close.is_file():
        dst = _project("cobi_matching").circuit_dir / "edge_close_new_tb.sp"
        tasks.append(CopyTask(src=edge_close, dst=dst, label="circuit"))

    # 3. config/logs/ hspice transcripts -> per-project logs/hspice/
    #    Bucket by date in the filename: 20260426 -> cobi_delay,
    #    20260427/20260428 -> cobi_matching. (Verified by sampling
    #    transcripts: same design_vars across both, but spec mtimes
    #    place the cutover at end of 4/26.)
    transcripts_dir = REPO_ROOT / "config" / "logs"
    if transcripts_dir.is_dir():
        for f in sorted(transcripts_dir.glob("hspice_transcript_*.jsonl")):
            stem = f.stem  # hspice_transcript_YYYYMMDD_HHMMSS
            try:
                date_part = stem.split("_")[2]
            except IndexError:
                continue
            if date_part.startswith("20260426"):
                proj = "cobi_delay"
            else:
                proj = "cobi_matching"
            dst = _project(proj).logs_hspice_dir / f.name
            tasks.append(CopyTask(src=f, dst=dst, label="hspice-log"))

    # 4. Legacy logs/ -> projects/_legacy/logs/agent/
    #    Everything in logs/ pre-projects-layout: .log + verify_*.log + watchdog.
    #    Skip .gitkeep and _tmp_parse.py (the latter is hand-cleaned).
    logs_dir = REPO_ROOT / "logs"
    if logs_dir.is_dir():
        for f in sorted(logs_dir.iterdir()):
            if not f.is_file():
                continue
            if f.name in (".gitkeep", "_tmp_parse.py"):
                continue
            dst = _legacy().logs_agent_dir / f.name
            tasks.append(CopyTask(src=f, dst=dst, label="legacy-log"))

    return tasks


def _file_eq(a: Path, b: Path) -> bool:
    if not (a.is_file() and b.is_file()):
        return False
    if a.stat().st_size != b.stat().st_size:
        return False
    return _hash(a) == _hash(b)


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def execute(tasks: list[CopyTask], dry_run: bool) -> tuple[int, int]:
    """Execute (or just print) the plan. Returns (copied, skipped_existing)."""
    copied = 0
    skipped = 0
    for t in tasks:
        if t.dst.exists() and _file_eq(t.src, t.dst):
            skipped += 1
            print(f"= SKIP (already in place) {t.render()}")
            continue
        print(("DRY-RUN " if dry_run else "COPY    ") + t.render())
        if dry_run:
            continue
        t.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(t.src, t.dst)
        copied += 1
    return copied, skipped


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migrate flat layout to projects/<name>/ structure (copy-only).",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan without copying anything.")
    args = ap.parse_args()

    tasks = build_plan()
    if not tasks:
        print("Nothing to migrate (no source files found).")
        return 0

    print(f"Migration plan: {len(tasks)} file(s).\n")
    copied, skipped = execute(tasks, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\nDRY-RUN done. {len(tasks)} would-copy, 0 changed on disk.")
    else:
        print(f"\nDone. copied={copied}, already-in-place={skipped}.")
        print("Source files left in place. Delete them by hand once verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
