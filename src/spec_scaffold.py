"""Spec scaffold renderer — emit the F-A 5-section Markdown skeleton.

Consumes a scaffold dict produced by ``SafeBridge.generate_spec_scaffold``
and returns a Markdown string with ``<TODO>`` placeholders the user
fills in. The template is intentionally generic: no circuit-specific
names, metrics, or ranges are baked in. The DUT / testbench cell names
and the discovered pin / desVar / analysis lists are the only cell-
specific content that survives into the output.

See ``config/LC_VCO_spec.md`` for a worked example of the same 5-section
structure after a human has filled it in.
"""

from __future__ import annotations

from typing import Any

_SUPPLY_HINT_SUBSTRINGS = ("vdd", "vss", "gnd", "vcc", "vee")


def _classify_pins(
    pins: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Partition pins into (probe_candidates, supply_candidates, other).

    Heuristics:
      - supply_candidates: name (case-insensitive) contains vdd/vss/gnd
        /vcc/vee.
      - probe_candidates: direction is 'output' or 'inputOutput' AND
        the name does not look like a supply.
      - other: everything else (input-only pins, unknown direction).

    These are hints only — the user is expected to review and move
    entries between lists during authoring.
    """
    probes: list[dict[str, str]] = []
    supplies: list[dict[str, str]] = []
    others: list[dict[str, str]] = []
    for pin in pins:
        name = pin.get("name", "")
        direction = (pin.get("direction") or "").lower()
        lname = name.lower()
        is_supply = any(s in lname for s in _SUPPLY_HINT_SUBSTRINGS)
        if is_supply:
            supplies.append(pin)
        elif direction in ("output", "inputoutput"):
            probes.append(pin)
        else:
            others.append(pin)
    return probes, supplies, others


def _fmt_pin_list(pins: list[dict[str, str]]) -> str:
    if not pins:
        return "<none discovered>"
    return ", ".join(
        f"`{p['name']}` ({p.get('direction', 'unknown')})" for p in pins
    )


def _render_desvar_table(design_vars: list[dict[str, str]]) -> str:
    """Render the §3 design-variables table.

    If design_vars is empty (no scs_path supplied or scs parse failed),
    emit a single placeholder row so the format is still legal
    Markdown and visibly needs filling in.
    """
    header = (
        "| Var | Role (device) | Range | Priority |\n"
        "|---|---|---|---|\n"
    )
    if not design_vars:
        return header + (
            "| `<TODO_var_name>` | <TODO role/device> | "
            "<TODO min>–<TODO max> | P1 |\n"
        )
    rows = []
    for entry in design_vars:
        name = entry.get("name", "<TODO>")
        default = entry.get("default", "<TODO>")
        rows.append(
            f"| `{name}` | <TODO role/device> | "
            f"<TODO min>–<TODO max> (default `{default}`) | P1 |"
        )
    return header + "\n".join(rows) + "\n"


def _render_analyses_block(analyses: list[dict[str, Any]]) -> str:
    if not analyses:
        return (
            "_No analyses discovered from Maestro session. "
            "The testbench must declare at least one analysis "
            "(tran/ac/dc/noise/xf/stb) for the agent to run._"
        )
    lines = []
    for entry in analyses:
        name = entry.get("name", "<?>")
        kwargs = entry.get("kwargs") or []
        kw_str = ", ".join(f"`{k}={v}`" for k, v in kwargs) if kwargs else "<no kwargs>"
        lines.append(f"- `{name}`: {kw_str}")
    return "\n".join(lines)


def render_spec_scaffold(scaffold: dict[str, Any]) -> str:
    """Render the scaffold dict to a Markdown string.

    Input must conform to ``SafeBridge.generate_spec_scaffold``'s shape:
    ``{lib, cell, tb_cell, dut, tb, design_vars, analyses}``.
    """
    lib = scaffold.get("lib", "<TODO_lib>")
    cell = scaffold.get("cell", "<TODO_cell>")
    tb_cell = scaffold.get("tb_cell", "<TODO_tb_cell>")
    dut = scaffold.get("dut") or {}
    tb = scaffold.get("tb") or {}
    dut_pins = dut.get("pins") or []
    tb_pins = tb.get("pins") or []
    design_vars = scaffold.get("design_vars") or []
    analyses = scaffold.get("analyses") or []

    dut_probes, dut_supplies, dut_others = _classify_pins(dut_pins)
    _, tb_supplies, _ = _classify_pins(tb_pins)

    desvar_table = _render_desvar_table(design_vars)
    analyses_block = _render_analyses_block(analyses)

    # First probe (if any) becomes the suggested Vdiff path fill-in;
    # otherwise leave the explicit <TODO>.
    probe_paths_hint = (
        ", ".join(f'"/{p["name"]}"' for p in dut_probes[:2])
        if dut_probes
        else '"<TODO_probe_p>", "<TODO_probe_n>"'
    )
    supply_list = (
        ", ".join(f"`{p['name']}`" for p in dut_supplies)
        if dut_supplies
        else "<TODO_supply_nets>"
    )

    sections: list[str] = []

    sections.append(
        f"# {cell} Optimization Spec — <TODO_one_line_summary>\n\n"
        f"> **Platform contract**: circuit-agent reads this file and "
        f"passes its content to the LLM as the target-spec prompt. "
        f"Every number below is authoritative.\n>\n"
        f"> Supporting docs (generic, not circuit-specific):\n"
        f"> `docs/spec_authoring_rules.md` (pass-range rules) · "
        f"`docs/llm_protocol.md` (response format, iteration flow, "
        f"stop conditions).\n"
    )

    sections.append(
        "## 1. Design under test\n\n"
        f"- Library / Cell: `{lib} / {cell}`; "
        f"Testbench cell: `{lib} / {tb_cell}`\n"
        "- Process: <TODO_pdk_label>; VDD = <TODO_V>\n"
        "- Topology: <TODO_one_paragraph_topology_description>\n"
        f"- DUT top-level pins: {_fmt_pin_list(dut_pins)}\n"
        f"- Suggested probes (output-direction pins): "
        f"{_fmt_pin_list(dut_probes)}\n"
        f"- Supply-looking nets: {supply_list}\n"
        "- Target <TODO_top_metric>: **<TODO_value ± tolerance>**\n"
        "<!-- Testbench pins: "
        f"{_fmt_pin_list(tb_pins)} -->\n"
        "<!-- Testbench supply-looking nets: "
        f"{_fmt_pin_list(tb_supplies)} -->\n"
        "<!-- Other DUT pins (input-direction / unknown): "
        f"{_fmt_pin_list(dut_others)} -->\n"
    )

    sections.append(
        "## 2. Machine-readable eval block\n\n"
        "Authoritative structured form for `src/spec_evaluator.py`. "
        "The agent executes against this block; `safeOceanDumpAll` "
        "collects per-signal / per-window stats, and the PC-side "
        "evaluator computes `measurements` + `pass_fail` from them. "
        "See `docs/spec_authoring_rules.md` for tolerance / "
        "sanity-bound rules.\n\n"
        "```yaml\n"
        "signals:\n"
        "  - name: <TODO_signal_name>\n"
        "    kind: <TODO V | Vdiff | Vsum_half | I>\n"
        f"    paths: [{probe_paths_hint}]\n"
        "    bounds: {max_abs: <TODO>, ptp_max: <TODO>}\n"
        "\n"
        "windows:\n"
        "  full: [<TODO_t_start>, <TODO_t_stop>]\n"
        "\n"
        "metrics:\n"
        "  - {name: <TODO_metric_name>, signal: <TODO_signal_name>, "
        "window: full, stat: <TODO ptp|mean|rms|freq_Hz|duty_pct>,\n"
        "     pass: [<TODO_lo>, <TODO_hi>], "
        "sanity: [<TODO_lo>, <TODO_hi>]}\n"
        "```\n"
    )

    sections.append(
        "## 3. Design variables the LLM may adjust\n\n"
        f"{desvar_table}\n"
        "SafeBridge `allowed_params` whitelist: `r, c, w, l, nf, m, "
        "multi, wf, nfin, fingers, idc, vdc` (case-insensitive).\n\n"
        "**Maestro prerequisite**: every var above must exist in the "
        "Maestro \"Design Variables\" pane with a numeric default. "
        "Missing defaults → `SFE-1997` fatal errors.\n\n"
        "**Discovered analyses in Maestro session**:\n\n"
        f"{analyses_block}\n"
    )

    sections.append(
        "## 4. Startup convergence aids\n\n"
        "Unstable-equilibrium circuits (oscillators, latches, Schmitt "
        "triggers) using `skipdc=yes` + `ic` suffer broken bias "
        "networks when spectre silently zeros non-IC'd nodes. Plan "
        "Auto reads spectre's `spectre.fc`, learns equilibrium "
        "values, and patches `ic` so every non-output node is seeded "
        "with its bias value while `perturb_nodes` get an asymmetric "
        "kick. Requires `--auto-bias-ic` AND this block; absent "
        "either, no-op. Delete this section if the DUT has a stable "
        "DC operating point.\n\n"
        "```yaml\n"
        "startup:\n"
        "  warm_start: auto               # auto | none\n"
        "  perturb_nodes:\n"
        "    - {name: <TODO_node_1>, offset_mV: +5}\n"
        "    - {name: <TODO_node_2>, offset_mV: -5}\n"
        "  v_cm_hint_V: <TODO_V>          # fallback if fc parse fails\n"
        "  netlist_path: null             # null = reuse --scs-path\n"
        "```\n"
    )

    sections.append(
        "## 5. Honest caveats\n\n"
        "- <TODO: list modelling simplifications (ideal L/C/R, absent "
        "parasitics) and the consequence on measured metrics>\n"
        "- <TODO: note any window choices that hide startup transients "
        "or other artifacts>\n"
        "- <TODO: any manual prerequisites (e.g. IC file, perturb "
        "nodes) the agent cannot infer>\n"
    )

    return "\n---\n\n".join(sections)
