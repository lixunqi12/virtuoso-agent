#!/usr/bin/env python3
"""CLI: drive Maestro Setup from a YAML recipe via SafeBridge.

Reads a YAML config describing analyses, outputs, design variables,
per-output pass/fail specs, and zero or more corner-netlist exports, and
applies them in order to the Maestro testbench scoped by ``--lib`` /
``--cell`` / ``--tb-cell``. With ``verify: true`` or ``--verify`` it reads
back a bounded safe summary from Maestro and checks that requested setup
landed.

The PDK-safe wrappers
(:meth:`SafeBridge.set_maestro_analysis`,
:meth:`SafeBridge.add_maestro_output`,
:meth:`SafeBridge.set_maestro_spec`,
:meth:`SafeBridge.create_netlist_for_corner`)
do the input validation; this script is a thin orchestrator.

YAML schema::

    test: "<optional Maestro test name; defaults to tb_cell>"
    session: "<optional Maestro session id; default empty>"
    design_vars:
      Ibias: "1"
      nfin_n: "1"

    analyses:
      - name: tran            # tran / ac / dc / noise / xf / stb / pss / pnoise
        enable: true
        options:
          start: "0"
          stop: "200n"
      - preset: ac_log        # dc_op / ac_log / tran / pss_oscillator / pnoise_phase_noise
        start: "1"
        stop: "100G"
        points_per_dec: "100"
    outputs:
      - name: f_osc
        signal_name: "/Vout"  # OR expr (mutually exclusive)
        output_type: ""       # "" | "signal" | "expr"
        spec:                 # optional
          lt: "21G"
          gt: "19G"
    corner_netlists:
      - corner: typ_25
        output_dir: "~/simulation/corner_typ"

    idempotent_outputs: true  # default true: delete same (test, output) before add
    verify: true

Usage::

    python scripts/configure_maestro_outputs.py \\
        --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \\
        --yaml configs/maestro_outputs.yaml \\
        --remote-skill-dir /proj/.../skill
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from virtuoso_bridge import VirtuosoClient  # noqa: E402

from src.safe_bridge import SafeBridge  # noqa: E402


# R1 R2 (2026-05-14) — YAML hardening caps. Recipe files are user-
# authored config; we still cap parse cost so a malformed file cannot
# stall automation. R1 first-pass caps (64 KiB / depth 5 / list 64 /
# str 1024) were too tight per leader review:
#   * depth 5 — legal ``analyses → tran → options → {start, stop, ...}``
#     is already 4 levels; a user-defined nested sweep blew the budget.
#   * list 64 — PSS / pnoise testbenches commonly produce 100+ outputs.
#   * file 64 KiB — generated recipes (e.g. corner sweeps) easily exceed.
# The relaxed defaults still keep parse cost bounded but accommodate
# realistic Maestro setups. ``_YAML_MAX_ALIAS_COUNT`` is documented for
# completeness — our :class:`_AnchorlessLoader` rejects ALL anchors /
# aliases, so the effective alias count is always 0 (i.e. trivially
# under any positive cap). The constant is retained as policy.
_YAML_MAX_FILE_BYTES = 256 * 1024
_YAML_MAX_DEPTH = 16
_YAML_MAX_ALIAS_COUNT = 32
_YAML_MAX_ITEMS_PER_LIST = 256
_YAML_MAX_STR_LEN = 4096


class _AnchorlessLoader(yaml.SafeLoader):
    """SafeLoader subclass that rejects YAML anchors / aliases entirely.

    Anchors (``&x``) plus aliases (``*x``) are the building block of
    YAML billion-laughs / quadratic-blowup attacks. Maestro Outputs
    Setup recipes do not need either, so we disable both with a single
    error message. PyYAML emits an ``AliasEvent`` when an alias is
    referenced and the anchor name is attached to each
    SCALAR/SEQ/MAP-start event. We inspect the next event *before*
    delegating to ``super().compose_node``, which lets us refuse with
    a sharp error message without ever materializing the aliased
    subtree.
    """

    def compose_node(self, parent, index):  # type: ignore[override]
        # AliasEvent — a ``*name`` reference. Always reject.
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.YAMLError(
                f"YAML alias '*{event.anchor}' is not allowed in Maestro "
                "Outputs Setup recipes (alias/anchor expansion is disabled)."
            )
        # SCALAR / SEQ-start / MAP-start with a ``&name`` attached.
        event = self.peek_event()
        anchor = getattr(event, "anchor", None)
        if anchor is not None:
            raise yaml.YAMLError(
                f"YAML anchor '&{anchor}' is not allowed in Maestro "
                "Outputs Setup recipes (alias/anchor expansion is disabled)."
            )
        return super().compose_node(parent, index)


class _DryRunSkillResult:
    """Successful-shaped result returned by :class:`_DryRunClient`.

    ``virtuoso_bridge.virtuoso.maestro.writer._q`` expects a result
    object with ``.errors`` (falsy for success) and ``.output`` (str).
    """

    errors: tuple = ()
    output: str = ""


class _DryRunClient:
    """No-RPC stand-in for VirtuosoClient used by ``--dry-run``.

    SafeBridge construction runs ``_load_skill_helpers`` which calls
    ``client.execute_skill(...)`` once per safe_*.il helper. Each writer
    method also calls into ``_mae_writer.*`` which itself calls
    ``client.execute_skill(...)``. Returning a success-shaped result for
    every such call lets SafeBridge's input validators fire end-to-end
    against the parsed YAML — which is the whole point of ``--dry-run``
    — without any network or remote-host contact.
    """

    ssh_runner = None  # writer.run_and_wait() inspects this attr

    def execute_skill(self, expr: str, **kwargs: Any) -> _DryRunSkillResult:
        result = _DryRunSkillResult()
        if "safeMaeWriteAndSave" in expr:
            result.output = (
                '{"ok":true,"varsWritten":1,"setupDbWritten":1,'
                '"testScopedWritten":1,"saved":true,"setupDbVars":[]}'
            )
        elif "safeMaeSetupSummary" in expr:
            result.output = '{"ok":true,"session":"dry-run","tests":{},"specsRaw":""}'
        return result


def _coerce_bool(value: Any, *, field: str) -> bool:
    """Strict bool coercion — rejects ``"true"`` / ``"false"`` / 1 / 0.

    R1 fix for P0-5. The previous ``bool(entry.get("enable", True))``
    coerced any truthy string (``"false"``, ``"no"``, ``" "``) to True,
    which would silently flip a disabled analysis back on. SafeBridge's
    own ``isinstance(enable, bool)`` check catches this downstream — but
    only if the CLI hasn't already mangled the type to True/False. The
    rule below mirrors the SafeBridge check at the YAML boundary so the
    error message names the offending field.
    """
    if not isinstance(value, bool):
        raise ValueError(
            f"{field!r} must be a YAML boolean (true/false), not "
            f"{type(value).__name__} (got {value!r})."
        )
    return value


def _coerce_optional_str(value: Any, *, field: str) -> str:
    """Strict optional-string coercion.

    Returns ``""`` if the field is absent (None). For a present value,
    requires ``isinstance(value, str)`` — rejects 0/False/None-derived
    coercions. Fixes the previous ``str(entry.get(...))`` which produced
    the literal string ``"None"`` from a missing key, which then sailed
    past SafeBridge's signal_name regex check as a foreign value.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(
            f"{field!r} must be a string or omitted, not "
            f"{type(value).__name__} (got {value!r})."
        )
    return value


def _check_yaml_tree(node: Any, *, depth: int = 0, path: str = "<root>") -> None:
    """Walk parsed YAML tree and enforce depth / list-length / str-length caps.

    Run after ``yaml.load`` (which has already enforced size and alias
    caps); this catches deeply-nested mappings or absurd list lengths
    that the static caps cannot. Caps are taken from the module-level
    ``_YAML_*`` constants — keep them in sync with the docstring above.
    """
    if depth > _YAML_MAX_DEPTH:
        raise ValueError(
            f"YAML nesting depth exceeds {_YAML_MAX_DEPTH} at {path!r}."
        )
    if isinstance(node, dict):
        if len(node) > _YAML_MAX_ITEMS_PER_LIST:
            raise ValueError(
                f"YAML mapping at {path!r} has {len(node)} keys; "
                f"max is {_YAML_MAX_ITEMS_PER_LIST}."
            )
        for k, v in node.items():
            if isinstance(k, str) and len(k) > _YAML_MAX_STR_LEN:
                raise ValueError(
                    f"YAML key at {path!r} too long "
                    f"(len={len(k)}, max={_YAML_MAX_STR_LEN})."
                )
            _check_yaml_tree(v, depth=depth + 1, path=f"{path}.{k!r}")
    elif isinstance(node, list):
        if len(node) > _YAML_MAX_ITEMS_PER_LIST:
            raise ValueError(
                f"YAML list at {path!r} has {len(node)} items; "
                f"max is {_YAML_MAX_ITEMS_PER_LIST}."
            )
        for i, v in enumerate(node):
            _check_yaml_tree(v, depth=depth + 1, path=f"{path}[{i}]")
    elif isinstance(node, str):
        if len(node) > _YAML_MAX_STR_LEN:
            raise ValueError(
                f"YAML scalar at {path!r} too long "
                f"(len={len(node)}, max={_YAML_MAX_STR_LEN})."
            )


def _coerce_options_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field!r} must be a mapping if present")
    return dict(value)


def _analysis_from_entry(entry: dict[str, Any]) -> tuple[str, bool, dict[str, Any]]:
    """Normalize raw analysis entry or preset into SafeBridge arguments."""
    enable = (
        _coerce_bool(entry["enable"], field="analyses[].enable")
        if "enable" in entry else True
    )
    options = _coerce_options_mapping(
        entry.get("options"), field="analyses[].options"
    )
    preset = entry.get("preset")
    if preset is None:
        name = _coerce_optional_str(entry.get("name"), field="analyses[].name")
        if not name:
            raise ValueError("'analyses[].name' is required without preset")
        return name, enable, options
    if not isinstance(preset, str) or not preset:
        raise ValueError("'analyses[].preset' must be a non-empty string")

    # Direct scalar keys on the entry are accepted for ergonomic presets:
    # - preset: ac_log
    #   start: "1"
    #   stop: "100G"
    #   points_per_dec: "100"
    direct = {
        k: v for k, v in entry.items()
        if k not in {"name", "preset", "enable", "options"}
    }
    merged = {**options, **direct}

    if preset == "dc_op":
        return "dc", enable, {
            "oppoint": merged.get("oppoint", "rawfile"),
            "detail": merged.get("detail", "all"),
            "maxiters": merged.get("maxiters", "150"),
            "maxsteps": merged.get("maxsteps", "10000"),
        }
    if preset == "ac_log":
        start = merged.get("start")
        stop = merged.get("stop")
        dec = merged.get("points_per_dec", merged.get("dec", 100))
        if start is None or stop is None:
            raise ValueError("preset 'ac_log' requires start and stop")
        return "ac", enable, {"start": start, "stop": stop, "dec": dec}
    if preset == "tran":
        stop = merged.get("stop")
        if stop is None:
            raise ValueError("preset 'tran' requires stop")
        opts: dict[str, Any] = {"stop": stop}
        if "maxstep" in merged:
            opts["maxstep"] = merged["maxstep"]
        return "tran", enable, opts
    if preset == "pss_oscillator":
        if "fund" not in merged:
            raise ValueError("preset 'pss_oscillator' requires fund")
        opts = {
            "fund": merged["fund"],
            "oscillator": merged.get("oscillator", "yes"),
            "autonomous": merged.get("autonomous", "yes"),
        }
        for key in ("harms", "tstab", "errpreset", "fundname", "skipdc", "maxstep"):
            if key in merged:
                opts[key] = merged[key]
        return "pss", enable, opts
    if preset == "pnoise_phase_noise":
        opts = {"noisetype": merged.get("noisetype", "sources")}
        for key in (
            "start", "stop", "dec", "lin", "maxsideband", "relativeharmonic",
            "refsideband", "sweeptype", "output", "input", "oprobe",
            "iprobe", "p", "n",
        ):
            if key in merged:
                opts[key] = merged[key]
        return "pnoise", enable, opts
    raise ValueError(
        "Unknown analysis preset "
        f"{preset!r}; expected dc_op/ac_log/tran/pss_oscillator/"
        "pnoise_phase_noise"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply Maestro Outputs Setup from a YAML recipe via "
            "SafeBridge (PDK-safe write path)."
        )
    )
    parser.add_argument("--lib", required=True, help="Virtuoso library name")
    parser.add_argument("--cell", required=True, help="DUT cell name")
    parser.add_argument(
        "--tb-cell", required=True, help="Maestro testbench cell name"
    )
    parser.add_argument(
        "--yaml", required=True, help="Path to outputs YAML recipe"
    )
    parser.add_argument(
        "--pdk-map",
        default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
        help="Path to PDK map YAML (default: config/pdk_map.yaml)",
    )
    parser.add_argument(
        "--remote-skill-dir",
        default=None,
        help="POSIX path on remote host where safe_*.il SKILL helpers live.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the YAML and run every SafeBridge validator without "
             "contacting any remote host (writers receive a no-RPC client).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After applying a live recipe, read back Maestro setup and verify "
             "requested analyses, outputs, specs, and design variables.",
    )
    parser.add_argument(
        "--report-json",
        default="",
        help="Optional local path for a JSON apply/verify report.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    return parser.parse_args()


def _load_recipe(path: Path) -> dict[str, Any]:
    """Load a Maestro Outputs Setup YAML recipe with hard caps.

    R1 (2026-05-14) hardening:
      * Reject symlinks (the recipe must be a regular file under the
        caller's control; following a symlink into ``/etc/`` is the
        classic config-poisoning vector).
      * Cap file size at ``_YAML_MAX_FILE_BYTES`` (256 KiB) before
        reading so an oversized file never reaches the parser.
      * Parse with :class:`_AnchorlessLoader` which rejects any YAML
        anchor / alias (alias-bomb defense).
      * Walk the parsed tree and enforce depth / list-length / scalar
        length caps via :func:`_check_yaml_tree`.

    The original parser had none of these caps; codex review (P0-4)
    flagged the script as a soft-target for resource-exhaustion via a
    crafted YAML recipe pushed through some upstream automation.
    """
    if path.is_symlink():
        raise ValueError(
            f"YAML recipe must be a regular file, not a symlink: {path.name}"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Cannot stat YAML recipe: {exc}") from exc
    if size > _YAML_MAX_FILE_BYTES:
        raise ValueError(
            f"YAML recipe too large (size={size} bytes, "
            f"max={_YAML_MAX_FILE_BYTES})."
        )
    with open(path, encoding="utf-8") as f:
        recipe = yaml.load(f, Loader=_AnchorlessLoader) or {}  # noqa: S506
    if not isinstance(recipe, dict):
        raise ValueError(
            f"YAML root must be a mapping; got {type(recipe).__name__}"
        )
    for key in ("analyses", "outputs", "corner_netlists"):
        if key in recipe and not isinstance(recipe[key], list):
            raise ValueError(f"YAML key {key!r} must be a list if present")
    if "design_vars" in recipe and not isinstance(recipe["design_vars"], dict):
        raise ValueError("YAML key 'design_vars' must be a mapping if present")
    if "verify" in recipe and not isinstance(recipe["verify"], bool):
        raise ValueError("YAML key 'verify' must be a boolean if present")
    if (
        "idempotent_outputs" in recipe
        and not isinstance(recipe["idempotent_outputs"], bool)
    ):
        raise ValueError(
            "YAML key 'idempotent_outputs' must be a boolean if present"
        )
    _check_yaml_tree(recipe)
    return recipe


def _apply_analyses(
    bridge: SafeBridge,
    items: list[dict[str, Any]],
    test: str | None,
    session: str,
    logger: logging.Logger,
) -> int:
    n = 0
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("Each 'analyses' entry must be a mapping")
        name, enable, options = _analysis_from_entry(entry)
        bridge.set_maestro_analysis(
            analysis=name,
            enable=enable,
            options=options,
            test=test,
            session=session,
        )
        logger.info("  analysis %s applied (enable=%s, opts=%d)",
                    name, enable, len(options))
        n += 1
    return n


def _apply_design_vars(
    bridge: SafeBridge,
    design_vars: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any] | None:
    if not design_vars:
        return None
    result = bridge.write_and_save_maestro(design_vars)
    logger.info(
        "  design_vars written=%s testScopedWritten=%s saved=%s",
        result.get("varsWritten"),
        result.get("testScopedWritten"),
        result.get("saved"),
    )
    return result


def _apply_one_spec_bound(
    bridge: SafeBridge,
    name: str,
    *,
    key: str,
    value: Any,
    test: str | None,
    session: str,
) -> None:
    if key == "lt":
        bridge.set_maestro_spec(name=name, lt=value, test=test, session=session)
    elif key == "gt":
        bridge.set_maestro_spec(name=name, gt=value, test=test, session=session)
    else:
        raise ValueError(f"Unsupported spec bound key {key!r}; use lt/gt")


def _apply_outputs(
    bridge: SafeBridge,
    items: list[dict[str, Any]],
    test: str | None,
    session: str,
    logger: logging.Logger,
    *,
    idempotent: bool,
) -> tuple[int, int]:
    n_out = 0
    n_spec = 0
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("Each 'outputs' entry must be a mapping")
        name = _coerce_optional_str(entry.get("name"), field="outputs[].name")
        if not name:
            raise ValueError("'outputs[].name' is required")
        if idempotent:
            try:
                bridge._delete_maestro_output_remote(name, test=test, session=session)
            except Exception as exc:  # noqa: BLE001 - delete is best-effort
                logger.warning(
                    "  output %s pre-delete skipped (%s: %s)",
                    name, type(exc).__name__, exc,
                )
        bridge.add_maestro_output(
            name=name,
            output_type=_coerce_optional_str(
                entry.get("output_type"), field="outputs[].output_type"
            ),
            signal_name=_coerce_optional_str(
                entry.get("signal_name"), field="outputs[].signal_name"
            ),
            expr=_coerce_optional_str(entry.get("expr"), field="outputs[].expr"),
            test=test,
            session=session,
        )
        logger.info("  output %s added", name)
        n_out += 1
        spec = entry.get("spec")
        if spec:
            if not isinstance(spec, dict):
                raise ValueError(f"'spec' for output {name!r} must be a mapping")
            unknown = set(spec) - {"lt", "gt"}
            if unknown:
                raise ValueError(
                    f"'spec' for output {name!r} has unknown keys: "
                    f"{sorted(unknown)}"
                )
            for bound_key in ("gt", "lt"):
                if bound_key in spec and spec[bound_key] is not None:
                    _apply_one_spec_bound(
                        bridge,
                        name,
                        key=bound_key,
                        value=spec[bound_key],
                        test=test,
                        session=session,
                    )
                    n_spec += 1
            logger.info(
                "    spec applied (lt=%r gt=%r)",
                spec.get("lt"), spec.get("gt"),
            )
    return n_out, n_spec


def _apply_corner_netlists(
    bridge: SafeBridge,
    items: list[dict[str, Any]],
    test: str | None,
    logger: logging.Logger,
) -> int:
    n = 0
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("Each 'corner_netlists' entry must be a mapping")
        corner = _coerce_optional_str(
            entry.get("corner"), field="corner_netlists[].corner"
        )
        output_dir = _coerce_optional_str(
            entry.get("output_dir"), field="corner_netlists[].output_dir"
        )
        if not corner or not output_dir:
            raise ValueError(
                "'corner_netlists[]' requires non-empty corner + output_dir"
            )
        bridge.create_netlist_for_corner(
            corner=corner,
            output_dir=output_dir,
            test=test,
        )
        logger.info("  corner netlist exported for %s", corner)
        n += 1
    return n


def _expected_analysis_names(items: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        name, enable, _options = _analysis_from_entry(entry)
        if enable:
            names.append(name)
    return names


def _verify_readback(
    bridge: SafeBridge,
    *,
    test: str | None,
    analyses: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    design_vars: dict[str, Any],
    writeback: dict[str, Any] | None,
    logger: logging.Logger,
) -> dict[str, Any]:
    summary = bridge.read_maestro_setup_summary(test=test)
    expected_test = test or bridge._scope_tb_cell
    tests = summary.get("tests") if isinstance(summary, dict) else {}
    problems: list[str] = []
    if not isinstance(tests, dict) or expected_test not in tests:
        problems.append(f"test {expected_test!r} not found in readback summary")
        test_summary: dict[str, Any] = {}
    else:
        raw_test_summary = tests[expected_test]
        test_summary = (
            raw_test_summary if isinstance(raw_test_summary, dict) else {}
        )

    if writeback is not None:
        if not writeback.get("saved", False):
            problems.append("writeback.saved is not true")
        if int(writeback.get("testScopedWritten") or 0) <= 0:
            problems.append("writeback.testScopedWritten is not > 0")

    got_analyses = test_summary.get("analyses")
    if not isinstance(got_analyses, dict):
        got_analyses = {}
    for name in _expected_analysis_names(analyses):
        item = got_analyses.get(name)
        if not (isinstance(item, dict) and item.get("enabled") is True):
            problems.append(f"analysis {name!r} is missing or disabled")

    got_output_names: set[str] = set()
    raw_outputs = test_summary.get("outputs")
    if isinstance(raw_outputs, list):
        for item in raw_outputs:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                got_output_names.add(item["name"])
    for entry in outputs:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name not in got_output_names:
                problems.append(f"output {name!r} missing from readback")

    got_vars = test_summary.get("variables")
    if not isinstance(got_vars, dict):
        got_vars = {}
    for name in design_vars:
        if name not in got_vars:
            problems.append(f"design var {name!r} missing from test scope")

    specs_raw = str(summary.get("specsRaw", ""))
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        spec = entry.get("spec")
        name = entry.get("name")
        if not spec:
            continue
        if not isinstance(name, str):
            continue
        if name not in specs_raw:
            problems.append(f"spec for output {name!r} missing from readback")

    report = {
        "ok": not problems,
        "problems": problems,
        "summary": summary,
    }
    if problems:
        for problem in problems:
            logger.error("VERIFY: %s", problem)
        raise RuntimeError(
            "Maestro readback verification failed: "
            + "; ".join(problems)
        )
    logger.info("Maestro readback verification passed for test %s", expected_test)
    return report


def _write_report(path: str, report: dict[str, Any], logger: logging.Logger) -> None:
    if not path:
        return
    out = Path(path)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Wrote JSON report: %s", out)


def _build_bridge(
    pdk_map_path: Path,
    remote_skill_dir: str | None,
    *,
    dry_run: bool,
    logger: logging.Logger,
) -> SafeBridge:
    if dry_run:
        logger.info(
            "Building SafeBridge with _DryRunClient — no remote-host contact."
        )
        return SafeBridge(
            _DryRunClient(),
            str(pdk_map_path),
            remote_skill_dir=remote_skill_dir,
        )
    logger.info("Connecting to Virtuoso bridge...")
    client = VirtuosoClient.from_env()
    return SafeBridge(
        client,
        str(pdk_map_path),
        remote_skill_dir=remote_skill_dir,
    )


def main() -> int:
    args = parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("configure_maestro_outputs")

    yaml_path = Path(args.yaml)
    if not yaml_path.exists():
        logger.error("YAML recipe not found: %s", yaml_path)
        return 1
    pdk_map_path = Path(args.pdk_map)
    if not pdk_map_path.exists():
        logger.error("PDK map file not found: %s", pdk_map_path)
        return 1

    recipe = _load_recipe(yaml_path)
    test_name = recipe.get("test")
    if test_name is not None and not isinstance(test_name, str):
        raise ValueError(
            f"'test' must be a string or omitted, not "
            f"{type(test_name).__name__}"
        )
    session = _coerce_optional_str(recipe.get("session"), field="session")
    analyses = recipe.get("analyses") or []
    outputs = recipe.get("outputs") or []
    design_vars = recipe.get("design_vars") or {}
    corner_netlists = recipe.get("corner_netlists") or []
    idempotent_outputs = recipe.get("idempotent_outputs", True)
    verify = bool(args.verify or recipe.get("verify", False))

    logger.info(
        "Recipe: %d analyses, %d outputs, %d design vars, "
        "%d corner-netlist exports",
        len(analyses), len(outputs), len(design_vars), len(corner_netlists),
    )

    bridge = _build_bridge(
        pdk_map_path,
        args.remote_skill_dir,
        dry_run=args.dry_run,
        logger=logger,
    )
    bridge.set_scope(args.lib, args.cell, tb_cell=args.tb_cell)
    logger.info(
        "SafeBridge scope bound: lib=%s cell=%s tb_cell=%s",
        args.lib, args.cell, args.tb_cell,
    )

    writeback = _apply_design_vars(bridge, design_vars, logger)
    n_an = _apply_analyses(bridge, analyses, test_name, session, logger)
    n_out, n_spec = _apply_outputs(
        bridge,
        outputs,
        test_name,
        session,
        logger,
        idempotent=idempotent_outputs,
    )
    n_corner = _apply_corner_netlists(bridge, corner_netlists, test_name, logger)

    report: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "applied": {
            "design_vars": len(design_vars),
            "analyses": n_an,
            "outputs": n_out,
            "specs": n_spec,
            "corner_netlists": n_corner,
        },
        "writeback": writeback,
    }
    if verify and not args.dry_run:
        report["verify"] = _verify_readback(
            bridge,
            test=test_name,
            analyses=analyses,
            outputs=outputs,
            design_vars=design_vars,
            writeback=writeback,
            logger=logger,
        )
    elif verify and args.dry_run:
        logger.info("[DRY-RUN] verify requested; live readback skipped.")
        report["verify"] = {"ok": None, "skipped": "dry-run"}

    logger.info(
        "%sMaestro Outputs Setup applied: analyses=%d, outputs=%d, "
        "specs=%d, corner_netlists=%d",
        "[DRY-RUN] " if args.dry_run else "",
        n_an, n_out, n_spec, n_corner,
    )
    _write_report(args.report_json, report, logger)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger("configure_maestro_outputs").error(
            "Configuration crashed (%s: %s)",
            type(exc).__name__, exc,
        )
        raise
