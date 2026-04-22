"""Plan Auto — startup convergence aid for unstable-equilibrium circuits.

Stage 1 rev 10 (2026-04-19).

Rationale
---------
spectre's ``skipdc=yes`` lets a transient skip DC and start from an ``ic``
statement, which is the only way to break the symmetric equilibrium of
LC-VCO / ring-VCO / SRAM / Schmitt-like circuits. The catch: spectre silently
zeroes every non-IC'd node, including bias-mirror gates and tail sources, so
the transient starts with a broken bias network. The circuit eventually
re-equilibrates but only AFTER the small tank-ring has decayed — result is
``V_diff = 0, V_cm = VDD`` flat output.

Plan Auto fixes this by reading ``spectre.fc`` (spectre's own post-tran
equilibrium snapshot, written via ``writefinal="spectre.fc"``) and rewriting
``input.scs``'s ``ic`` line so EVERY node gets its equilibrium value except
for a small list of ``perturb_nodes`` that get a deliberate asymmetric
kick. The mechanism is fully spec-driven — see LC_VCO_spec.md §9.

Scope
-----
This module only:
  (a) parses the ``startup:`` yaml block out of the spec markdown,
  (b) decides whether to invoke the SKILL-side patcher each iteration,
  (c) computes per-node IC values from v_cm + offset_mV.

The heavy lifting (reading ``spectre.fc``, rewriting ``input.scs``) lives in
the SKILL-side helper ``skill/safe_patch_netlist.il``. This Python module
never parses netlists or touches files on remote host directly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerturbNode:
    """One node to perturb away from the symmetric equilibrium."""
    name: str                 # e.g. "Vout_n" or "I0.Vout_n"
    offset_mV: float          # signed; +/- creates differential kick


@dataclass
class StartupConfig:
    """Parsed `startup:` section from the spec markdown."""
    warm_start: str = "none"              # "auto" | "none"
    perturb_nodes: list[PerturbNode] = field(default_factory=list)
    v_cm_hint_V: float = 0.4
    netlist_path: str | None = None       # None = use --scs-path

    @property
    def enabled(self) -> bool:
        return self.warm_start == "auto" and bool(self.perturb_nodes)


_YAML_FENCE_RE = re.compile(
    r"```ya?ml\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE
)


def parse_startup_from_spec(spec_text: str) -> StartupConfig:
    """Extract a `startup:` block from a Markdown spec.

    Scans every fenced yaml block in the spec for a top-level
    ``startup:`` key. Returns a ``StartupConfig`` with defaults
    (``warm_start="none"``) when no block is found — so the absence
    of §9 in the spec silently disables Plan Auto.

    The parser is intentionally minimal (no PyYAML dependency) so it
    works in environments where the virtuoso-agent venv is minimal.
    Only the keys documented in §9 of the spec are recognized; all
    other keys under ``startup:`` are ignored with a warning.
    """
    if not isinstance(spec_text, str):
        return StartupConfig()

    for fence_match in _YAML_FENCE_RE.finditer(spec_text):
        yaml_block = fence_match.group(1)
        if "startup:" not in yaml_block:
            continue
        parsed = _parse_startup_block(yaml_block)
        if parsed is not None:
            return parsed
    return StartupConfig()


def _parse_startup_block(yaml_text: str) -> StartupConfig | None:
    """Minimal yaml-subset parser for the `startup:` section.

    Handles only the flat structure documented in spec §9:
        startup:
          warm_start: auto
          perturb_nodes:
            - name: Vout_n
              offset_mV: +5
          v_cm_hint_V: 0.4
          netlist_path: null

    Returns None if the block is malformed (e.g. ``startup:`` present
    but no child keys).
    """
    lines = yaml_text.splitlines()
    in_startup = False
    startup_indent = 0
    warm_start = "none"
    v_cm_hint_V = 0.4
    netlist_path: str | None = None
    perturb_nodes: list[PerturbNode] = []
    current_node: dict[str, Any] | None = None

    for raw_line in lines:
        # Strip trailing comment
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        # Only treat ``startup:`` as a section header when nothing
        # follows the colon on the same line (i.e. it's a mapping key,
        # not a scalar/list assignment like ``startup: [0, 2e-7]`` that
        # can appear as a *window* name inside a different yaml fence).
        if stripped == "startup:" or stripped.rstrip() == "startup:":
            in_startup = True
            startup_indent = indent
            continue
        if not in_startup:
            continue
        if indent <= startup_indent and stripped != "":
            # De-indented out of startup section.
            in_startup = False
            continue

        # Perturb list item start. Two accepted forms:
        #   - name: foo               (block-style, continues on next line)
        #   - {name: foo, offset_mV: +5}   (flow-style, self-contained)
        # F-A (2026-04-22) compressed the spec's perturb_nodes to the
        # flow-style form. Without the inline branch below, "- {name: ..."
        # got split on the first colon and produced a useless
        # current_node={"{name": "..."} — Plan Auto then silently
        # disabled because no PerturbNode finalized.
        if stripped.startswith("- "):
            if current_node is not None:
                pn = _finalize_perturb(current_node)
                if pn:
                    perturb_nodes.append(pn)
                current_node = None
            kv = stripped[2:].strip()
            if kv.startswith("{") and kv.endswith("}"):
                inline_node: dict[str, Any] = {}
                for part in kv[1:-1].split(","):
                    if ":" not in part:
                        continue
                    k, v = part.split(":", 1)
                    inline_node[k.strip()] = v.strip()
                pn = _finalize_perturb(inline_node)
                if pn:
                    perturb_nodes.append(pn)
                # current_node stays None so a block-style sibling key
                # on a subsequent line does NOT accidentally merge into
                # this flow-style entry.
                continue
            current_node = {}
            if ":" in kv:
                k, v = kv.split(":", 1)
                current_node[k.strip()] = v.strip()
            continue

        # Subsequent key of current list item (deeper indent than "- ")
        if current_node is not None and indent > startup_indent + 4:
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                current_node[k.strip()] = v.strip()
            continue

        # Close out pending perturb item when we hit a sibling key
        if current_node is not None:
            pn = _finalize_perturb(current_node)
            if pn:
                perturb_nodes.append(pn)
            current_node = None

        # Top-level keys of startup:
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip()

        if key == "warm_start":
            if val.lower() in {"auto", "none"}:
                warm_start = val.lower()
            else:
                logger.warning(
                    "Plan Auto: unknown warm_start=%r; treating as 'none'", val
                )
        elif key == "v_cm_hint_V":
            try:
                v_cm_hint_V = float(val)
            except ValueError:
                logger.warning(
                    "Plan Auto: bad v_cm_hint_V=%r; using default 0.4", val
                )
        elif key == "netlist_path":
            if val and val.lower() not in {"null", "none", "~", ""}:
                netlist_path = val.strip("'\"")
        elif key == "perturb_nodes":
            # handled by list items below
            pass

    # Final list item flush.
    if current_node is not None:
        pn = _finalize_perturb(current_node)
        if pn:
            perturb_nodes.append(pn)

    if warm_start == "none" and not perturb_nodes:
        # Nothing parsed out of this fence — return None so
        # ``parse_startup_from_spec`` keeps scanning subsequent
        # yaml fences instead of short-circuiting on this block.
        return None

    return StartupConfig(
        warm_start=warm_start,
        perturb_nodes=perturb_nodes,
        v_cm_hint_V=v_cm_hint_V,
        netlist_path=netlist_path,
    )


def _finalize_perturb(node_dict: dict[str, Any]) -> PerturbNode | None:
    name = node_dict.get("name")
    offset = node_dict.get("offset_mV")
    if not isinstance(name, str) or not name:
        return None
    try:
        # Handle "+5" / "-10" / "5" / "5.0"
        off_val = float(str(offset).lstrip("+"))
    except (TypeError, ValueError):
        logger.warning(
            "Plan Auto: perturb_node %s has bad offset_mV=%r; defaulting 0",
            name, offset,
        )
        off_val = 0.0
    return PerturbNode(name=name, offset_mV=off_val)


class PlanAuto:
    """Orchestrator: decides when + how to call the SKILL patcher.

    Holds the parsed ``StartupConfig`` + a feature flag (``--auto-bias-ic``
    on the CLI). When either is off, every public method degrades to a
    no-op with a single info log. When both are on, ``patch_after_run``
    invokes ``bridge.patch_netlist_ic(...)`` and logs the outcome.

    Per-iter patch semantics
    ------------------------
    The patcher reads the latest ``spectre.fc`` (written by each tran
    run at ``writefinal="spectre.fc"``) and rewrites input.scs's
    ``ic`` line. Re-patching every iter (rather than once) means that
    as the LLM's design_vars change the equilibrium bias point, the
    next iter's ic values track that drift.
    """

    def __init__(
        self,
        config: StartupConfig,
        scs_path: str | None,
        enabled_flag: bool,
    ):
        self.config = config
        self.scs_path = scs_path
        self.enabled_flag = enabled_flag

    @property
    def active(self) -> bool:
        return (
            self.enabled_flag
            and self.config.enabled
            and self.scs_path is not None
        )

    def describe(self) -> str:
        if not self.enabled_flag:
            return "Plan Auto: disabled (--auto-bias-ic not set)"
        if not self.config.enabled:
            return "Plan Auto: disabled (spec startup.warm_start != auto)"
        if self.scs_path is None:
            return "Plan Auto: disabled (--scs-path missing)"
        nodes = ", ".join(
            f"{p.name}{p.offset_mV:+g}mV" for p in self.config.perturb_nodes
        )
        return f"Plan Auto: ACTIVE (perturb={nodes}, scs={self.scs_path})"

    def patch_after_run(
        self,
        bridge: Any,
        iteration: int,
    ) -> dict[str, Any]:
        """Invoke the SKILL-side patcher using this run's spectre.fc.

        Returns a status dict:
            {"patched": bool, "reason": str, "numBiasNodes": int}
        Best-effort: any failure logs a warning and returns
        ``{"patched": False, "reason": "..."}``; it MUST NOT raise into
        the agent loop. The next iter will re-attempt.
        """
        if not self.active:
            return {"patched": False, "reason": "inactive"}

        scs_path = self.scs_path
        fc_path = self._infer_fc_path(scs_path)
        perturb_spec = [
            {"name": p.name, "offset_mV": p.offset_mV}
            for p in self.config.perturb_nodes
        ]

        try:
            result = bridge.patch_netlist_ic(
                scs_path=scs_path,
                fc_path=fc_path,
                perturb_nodes=perturb_spec,
                v_cm_hint_V=self.config.v_cm_hint_V,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Plan Auto iter %d: patch_netlist_ic raised (%s: %s); "
                "leaving ic line unchanged for next iter.",
                iteration, type(exc).__name__, exc,
            )
            return {"patched": False, "reason": f"{type(exc).__name__}"}

        if not result.get("ok"):
            logger.warning(
                "Plan Auto iter %d: patcher returned not-ok (%s)",
                iteration, result.get("error", "unknown"),
            )
            return {"patched": False, "reason": str(result.get("error"))}

        nbias = int(result.get("numBiasNodes", 0))
        vcm = result.get("vcmMeasured")
        logger.info(
            "Plan Auto iter %d: ic line patched — %d bias node(s) + "
            "%d perturb node(s), V_cm from fc = %s V",
            iteration, nbias, len(perturb_spec), vcm,
        )
        return {
            "patched": True,
            "reason": "ok",
            "numBiasNodes": nbias,
            "vcmMeasured": vcm,
        }

    @staticmethod
    def _infer_fc_path(scs_path: str) -> str:
        """``input.scs`` and ``spectre.fc`` both sit in ``.../netlist/``."""
        p = Path(scs_path)
        return str(p.with_name("spectre.fc")).replace("\\", "/")
