#!/usr/bin/env python3
"""Export a sanitized LC_VCO evidence bundle for paper/reviewer use."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.safe_bridge import scrub as safe_scrub


STATE_JSONL = PROJECT_ROOT / "paper" / "data" / "lc_vco_evidence_runs.jsonl"
SUMMARY_CSV = PROJECT_ROOT / "paper" / "data" / "lc_vco_evidence_summary.csv"
SPEC_PATH = PROJECT_ROOT / "projects" / "lc_vco_base" / "constraints" / "spec.md"
REPRO_ROOT = PROJECT_ROOT / "paper" / "repro"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default=str(STATE_JSONL))
    p.add_argument("--summary", default=str(SUMMARY_CSV))
    p.add_argument("--spec", default=str(SPEC_PATH))
    p.add_argument("--out-dir", default=None)
    p.add_argument(
        "--pass-only",
        action="store_true",
        help="Export only PASS records. Default includes PASS and FAIL records.",
    )
    return p.parse_args(argv)


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def public_scrub(text: str) -> str:
    text = safe_scrub(text)
    # Do not publish local or remote account-specific paths. Keep enough shape
    # to show the flow used project logs, Maestro results, and transcripts.
    root_variants = {
        str(PROJECT_ROOT),
        str(PROJECT_ROOT).replace("\\", "/"),
    }
    for root in root_variants:
        text = text.replace(root, "<repo>")
    text = re.sub(r"[A-Za-z]:[\\/][^,\s\"']+", "<local-path>", text)
    text = re.sub(
        r"/home/[^/,\s\"']+(?:/[^/,\s\"']+)?/simulation/[^,\s\"']+",
        "<remote-sim-path>",
        text,
    )
    text = re.sub(r"/home/[^/,\s\"']+", "/home/<user>", text)
    return text


def scrub_obj(value: Any) -> Any:
    if isinstance(value, str):
        return public_scrub(value)
    if isinstance(value, list):
        return [scrub_obj(item) for item in value]
    if isinstance(value, dict):
        return {str(key): scrub_obj(item) for key, item in value.items()}
    return value


def copy_text_scrubbed(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8", errors="replace")
    dst.write_text(public_scrub(text), encoding="utf-8")
    return True


def write_readme(out_dir: Path, records: list[dict[str, Any]]) -> None:
    pass_records = [r for r in records if r.get("outcome") == "PASS"]
    variants = sorted({str(r.get("variant")) for r in records})
    models = sorted({str(r.get("model_name")) for r in records})
    inits = sorted({str(r.get("init", "unknown")) for r in records})
    baseline_examples = {}
    for rec in records:
        init = str(rec.get("init", "unknown"))
        baseline_examples.setdefault(init, rec.get("baseline_vars") or {})
    baseline_text = "\n".join(
        f"- `{name}`: `{vars_}`" for name, vars_ in baseline_examples.items()
    )
    body = f"""# LC_VCO Evidence Bundle

Generated: {datetime.now().isoformat(timespec="seconds")}

This bundle contains sanitized artifacts for the LC_VCO 7-point tuning-curve
case study. It intentionally excludes raw stdout logs and raw simulator
waveforms. Transcripts are scrubbed for local and remote account-specific paths.

## Contents

- `spec.md`: benchmark specification used by the agent.
- `summary.csv`: per-run result table.
- `runs.jsonl`: run metadata, final metrics, final design variables, and
  sanitized artifact references.
- `transcripts/`: scrubbed LLM interaction transcripts for included runs.

## Included Runs

- models: {", ".join(models) or "-"}
- variants: {", ".join(variants) or "-"}
- initializations: {", ".join(inits) or "-"}
- pass records: {len(pass_records)}
- total records: {len(records)}

## Initialization Policy

Every live run was reset before optimization. The exact initial design variables
are recorded per row in `runs.jsonl`; representative initializations:

```text
{baseline_text}
```

`all_ones` is a stress initialization: Vctrl remains nominal at 0.4 V and every
other design variable starts from literal `1`.

## Re-run Command Shape

```powershell
.\\.venv\\Scripts\\python.exe scripts\\run_lc_vco_evidence.py --execute
```
"""
    (out_dir / "README.md").write_text(body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = Path(args.state)
    summary_path = Path(args.summary)
    spec_path = Path(args.spec)
    records = load_records(state_path)
    if args.pass_only:
        records = [r for r in records if r.get("outcome") == "PASS"]

    out_dir = (
        Path(args.out_dir)
        if args.out_dir else
        REPRO_ROOT / f"lc_vco_evidence_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    copy_text_scrubbed(spec_path, out_dir / "spec.md")
    copy_text_scrubbed(summary_path, out_dir / "summary.csv")
    public_records = [scrub_obj(record) for record in records]
    (out_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in public_records) + "\n",
        encoding="utf-8",
    )

    transcript_dir = out_dir / "transcripts"
    copied = 0
    for rec in records:
        transcript = rec.get("transcript_path")
        if not transcript:
            continue
        src = Path(transcript)
        dst = transcript_dir / (
            f"{rec.get('model_name')}_{rec.get('variant')}_"
            f"seed{rec.get('seed')}_{src.name}"
        )
        if copy_text_scrubbed(src, dst):
            copied += 1

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "records": len(records),
        "transcripts_copied": copied,
        "source_state": public_scrub(str(state_path)),
        "source_summary": public_scrub(str(summary_path)),
        "source_spec": public_scrub(str(spec_path)),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    write_readme(out_dir, records)
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
