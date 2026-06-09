"""PDK-safe topology intent and operating-point diagnostics.

This module turns a sanitized schematic instance list into a compact,
reviewable circuit-intent artifact.  It deliberately works only from
instance names, generic cell aliases, nets, CDF parameter references, and
safe operating-point scalars.  It must never require model-card contents or
foundry-specific parameter dumps.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable


_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_!]*$")
_GROUND_NETS = {"0", "gnd", "gnd!", "vss", "vss!"}
_SUPPLY_NETS = {"vdd", "vdd!", "vcc", "vcc!"}
_OUTPUT_HINTS = ("out", "vout")
_INPUT_HINTS = ("in", "vin")


def infer_topology_intent(
    instances: Iterable[dict[str, Any]] | None,
    *,
    design_vars: Iterable[dict[str, Any] | str] | None = None,
    dut_pins: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Infer a bounded topology-intent summary from sanitized instances.

    The result is intentionally a plain dict so it can be serialized into
    ``topology_intent.json`` and embedded in generated specs.
    """
    insts = [_normalize_instance(inst) for inst in instances or []]
    insts = [inst for inst in insts if inst]
    desvar_names = _design_var_names(design_vars)
    pin_names = [
        str(pin.get("name", "")).strip()
        for pin in (dut_pins or [])
        if isinstance(pin, dict)
    ]

    roles: dict[str, str] = {}
    signal_path: list[dict[str, Any]] = []
    bias_paths: list[dict[str, Any]] = []
    feedback_paths: list[dict[str, Any]] = []
    compensation: list[dict[str, Any]] = []
    notes: list[str] = []

    nmos = [inst for inst in insts if _is_mos(inst, "n")]
    pmos = [inst for inst in insts if _is_mos(inst, "p")]
    caps = [inst for inst in insts if _cell_contains(inst, "cap")]
    resistors = [inst for inst in insts if _cell_contains(inst, "res")]
    generic = [
        inst for inst in insts
        if not (_is_mos(inst) or _cell_contains(inst, "cap")
                or _cell_contains(inst, "res") or _is_current_source(inst)
                or _is_voltage_source(inst))
    ]

    outputs = _discover_signal_pair(pin_names, insts, _OUTPUT_HINTS)
    inputs = _discover_signal_pair(pin_names, insts, _INPUT_HINTS)
    input_pair = _find_input_pair(nmos, inputs)
    first_stage_nodes = _input_pair_drain_nodes(input_pair)
    tail = _find_tail_device(nmos, input_pair)
    n_bias_diode = _find_diode_for_gate(nmos, _net(tail, "G") if tail else "")
    p_bias_diode = _find_pmos_bias_diode(pmos)
    pmos_loads = _find_pmos_loads(pmos, first_stage_nodes)
    second_stage = _find_second_stage_pmos(pmos, first_stage_nodes, outputs)
    cmfb_actuators = _find_cmfb_actuators(nmos, outputs)
    cmfb_block = _find_cmfb_block(generic)
    compensation = _find_miller_compensation(
        caps, resistors, first_stage_nodes, outputs,
    )

    if input_pair:
        for inst in input_pair:
            roles[_name(inst)] = "input pair"
        signal_path.append({
            "role": "input_pair",
            "instances": _names(input_pair),
            "input_nets": _unique([_net(inst, "G") for inst in input_pair]),
            "source_node": _net(input_pair[0], "S"),
        })
    if pmos_loads:
        for inst in pmos_loads:
            roles[_name(inst)] = "first-stage PMOS load"
        signal_path.append({
            "role": "first_stage_active_load",
            "instances": _names(pmos_loads),
            "output_nodes": _unique([_net(inst, "D") for inst in pmos_loads]),
            "bias_node": _net(pmos_loads[0], "G"),
        })
    if first_stage_nodes:
        signal_path.append({
            "role": "first_stage_outputs",
            "nodes": first_stage_nodes,
        })
    if second_stage:
        for inst in second_stage:
            roles[_name(inst)] = "second-stage PMOS pull-up"
        signal_path.append({
            "role": "second_stage_pullup",
            "instances": _names(second_stage),
            "gate_nodes": _unique([_net(inst, "G") for inst in second_stage]),
            "output_nodes": _unique([_net(inst, "D") for inst in second_stage]),
        })
    if outputs:
        signal_path.append({"role": "outputs", "nodes": outputs})

    if tail:
        roles[_name(tail)] = "tail current source"
        path: dict[str, Any] = {
            "role": "tail_bias",
            "current_source": _name(tail),
            "gate_node": _net(tail, "G"),
            "tail_node": _net(tail, "D"),
        }
        if n_bias_diode:
            roles[_name(n_bias_diode)] = "NMOS bias diode"
            path["bias_diode"] = _name(n_bias_diode)
        bias_paths.append(path)
    if pmos_loads or p_bias_diode:
        path = {
            "role": "pmos_bias",
            "loads": _names(pmos_loads),
            "bias_node": _net(pmos_loads[0], "G") if pmos_loads else "",
        }
        if p_bias_diode:
            roles[_name(p_bias_diode)] = "PMOS bias diode"
            path["bias_diode"] = _name(p_bias_diode)
            path["bias_node"] = path["bias_node"] or _net(p_bias_diode, "G")
        bias_paths.append(path)

    if cmfb_actuators:
        for inst in cmfb_actuators:
            roles[_name(inst)] = "CMFB-controlled NMOS pull-down"
        feedback_paths.append({
            "role": "cmfb_output_actuators",
            "instances": _names(cmfb_actuators),
            "control_node": _net(cmfb_actuators[0], "G"),
            "output_nodes": _unique([_net(inst, "D") for inst in cmfb_actuators]),
        })
    if cmfb_block:
        roles[_name(cmfb_block)] = "CMFB amplifier macro"
        feedback_paths.append({
            "role": "cmfb_amplifier",
            "instance": _name(cmfb_block),
            "pins": _safe_nets(cmfb_block.get("nets", {})),
        })

    if compensation:
        for comp in compensation:
            if comp.get("cap"):
                roles[str(comp["cap"])] = "Miller compensation capacitor"
            if comp.get("resistor"):
                roles[str(comp["resistor"])] = "Miller/nulling resistor"

    if not input_pair:
        notes.append("No differential MOS input pair was confidently inferred.")
    if input_pair and not second_stage:
        notes.append("Input stage found, but no second-stage PMOS pull-up pair was inferred.")
    if feedback_paths and cmfb_block:
        notes.append("CMFB macro internals remain opaque; only pin-level intent is inferred.")

    design_var_roles = _infer_design_var_roles(insts, roles, desvar_names)
    score = _confidence_score(
        bool(input_pair), bool(pmos_loads), bool(second_stage),
        bool(tail), bool(outputs), bool(feedback_paths),
    )
    circuit_class = (
        "fully_differential_two_stage_opamp"
        if input_pair and second_stage and len(outputs) >= 2
        else "unknown"
    )
    confidence = (
        "high" if score >= 5 else "medium" if score >= 3 else "low"
    )

    return {
        "schema": "topology_intent.v1",
        "circuit_class": circuit_class,
        "confidence": confidence,
        "human_review_required": confidence != "high" or bool(cmfb_block),
        "signal_path": signal_path,
        "bias_paths": bias_paths,
        "feedback_paths": feedback_paths,
        "compensation": compensation,
        "device_roles": roles,
        "design_var_roles": design_var_roles,
        "notes": notes,
    }


def render_topology_intent_markdown(intent: dict[str, Any] | None) -> str:
    """Render topology intent as compact Markdown for specs/prompts."""
    if not isinstance(intent, dict) or not intent:
        return ""
    lines = [
        f"- Topology hypothesis: `{intent.get('circuit_class', 'unknown')}` "
        f"(confidence: `{intent.get('confidence', 'low')}`)",
        f"- Human review required: `{bool(intent.get('human_review_required', True))}`",
    ]
    for title, key in (
        ("Signal path", "signal_path"),
        ("Bias paths", "bias_paths"),
        ("Feedback paths", "feedback_paths"),
        ("Compensation", "compensation"),
    ):
        entries = intent.get(key)
        if isinstance(entries, list) and entries:
            lines.append(f"- {title}:")
            for entry in entries:
                lines.append(f"  - {_compact_entry(entry)}")
    dvars = intent.get("design_var_roles")
    if isinstance(dvars, dict) and dvars:
        lines.append("- Design-variable roles:")
        for name in sorted(dvars):
            role = dvars[name]
            if isinstance(role, dict):
                text = role.get("role") or role.get("summary") or "unknown"
                insts = role.get("instances")
                suffix = f" ({', '.join(insts)})" if isinstance(insts, list) and insts else ""
                lines.append(f"  - `{name}`: {text}{suffix}")
            else:
                lines.append(f"  - `{name}`: {role}")
    notes = intent.get("notes")
    if isinstance(notes, list) and notes:
        lines.append("- Notes:")
        for note in notes:
            lines.append(f"  - {note}")
    return "\n".join(lines)


def format_stage_diagnostics(
    intent: dict[str, Any] | None,
    op_point: dict[str, Any] | None,
) -> str:
    """Create PDK-safe designer-style OP feedback from intent + scalars."""
    if not isinstance(intent, dict) or not isinstance(op_point, dict) or not op_point:
        return ""
    instances = op_point.get("instances")
    if not isinstance(instances, dict):
        instances = op_point
    nodes = op_point.get("nodes") if isinstance(op_point.get("nodes"), dict) else {}
    roles = intent.get("device_roles") if isinstance(intent.get("device_roles"), dict) else {}
    if not roles and not nodes:
        return ""

    lines = ["### Stage-level diagnosis"]
    failures: list[str] = []

    input_pair = _instances_for_role(roles, "input pair")
    if input_pair:
        lines.append(_diagnose_device_group("Input pair", input_pair, instances))

    tail = _instances_for_role(roles, "tail current source")
    if tail:
        text = _diagnose_device_group("Tail current source", tail, instances)
        lines.append(text)
        if "triode" in text.lower() or "cutoff" in text.lower():
            failures.append("tail current-source headroom/conduction is weak")

    first_nodes = _first_stage_nodes(intent)
    first_line = _diagnose_first_stage_nodes(first_nodes, nodes)
    if first_line:
        lines.append(first_line)
        low = first_line.lower()
        if "near vdd" in low or "near ground" in low:
            failures.append("first-stage output common-mode is railing")

    second = _instances_for_role(roles, "second-stage PMOS pull-up")
    if second:
        text = _diagnose_device_group("Second-stage PMOS pull-up", second, instances)
        lines.append(text)
        if "cutoff" in text.lower():
            failures.append("second-stage pull-up devices are off")

    cmfb = _instances_for_role(roles, "CMFB-controlled NMOS pull-down")
    if cmfb:
        text = _diagnose_device_group("CMFB pull-down", cmfb, instances)
        lines.append(text)
        if "cutoff" in text.lower():
            failures.append("CMFB pull-down devices are off")

    cmfb_line = _diagnose_cmfb_nodes(nodes)
    if cmfb_line:
        lines.append(cmfb_line)

    if failures:
        lines.append("- Primary suspected failure: " + "; ".join(_unique(failures)) + ".")
    else:
        lines.append("- Primary suspected failure: not classified from the safe OP scalars.")
    return "\n".join(lines)


def design_var_role_text(intent: dict[str, Any] | None, name: str) -> str | None:
    """Return a short scaffold-table role for a design variable."""
    if not isinstance(intent, dict):
        return None
    roles = intent.get("design_var_roles")
    if not isinstance(roles, dict):
        return None
    entry = roles.get(name)
    if isinstance(entry, dict):
        role = entry.get("role")
        insts = entry.get("instances")
        if isinstance(role, str) and role:
            if isinstance(insts, list) and insts:
                return f"{role} ({', '.join(insts)})"
            return role
    if isinstance(entry, str) and entry:
        return entry
    return None


def _normalize_instance(inst: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(inst, dict):
        return {}
    name = inst.get("instName") or inst.get("name")
    cell = inst.get("cell")
    if not isinstance(name, str) or not name:
        return {}
    if not isinstance(cell, str):
        cell = ""
    nets = inst.get("nets") if isinstance(inst.get("nets"), dict) else {}
    params = inst.get("params") if isinstance(inst.get("params"), dict) else {}
    return {
        "name": name,
        "cell": cell,
        "nets": {str(k): str(v) for k, v in nets.items()},
        "params": {str(k): str(v) for k, v in params.items()},
    }


def _design_var_names(design_vars: Iterable[dict[str, Any] | str] | None) -> set[str]:
    names: set[str] = set()
    for item in design_vars or []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
    return names


def _name(inst: dict[str, Any] | None) -> str:
    return str((inst or {}).get("name", ""))


def _names(instances: Iterable[dict[str, Any]]) -> list[str]:
    return [_name(inst) for inst in instances if _name(inst)]


def _cell_contains(inst: dict[str, Any], needle: str) -> bool:
    return needle.lower() in str(inst.get("cell", "")).lower()


def _is_mos(inst: dict[str, Any], polarity: str | None = None) -> bool:
    cell = str(inst.get("cell", "")).upper()
    if polarity == "n":
        return cell.startswith("NMOS")
    if polarity == "p":
        return cell.startswith("PMOS")
    return cell.startswith(("NMOS", "PMOS"))


def _is_current_source(inst: dict[str, Any]) -> bool:
    return str(inst.get("cell", "")).upper() in {"ISRC", "IDC"}


def _is_voltage_source(inst: dict[str, Any]) -> bool:
    return str(inst.get("cell", "")).upper() in {"VSRC", "VDC", "VPWL"}


def _net(inst: dict[str, Any] | None, pin: str) -> str:
    nets = (inst or {}).get("nets")
    if not isinstance(nets, dict):
        return ""
    return str(nets.get(pin, ""))


def _safe_nets(nets: dict[str, Any]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for pin, net in nets.items():
        pin_s = str(pin)
        net_s = str(net)
        if _safe_net_name(net_s):
            safe[pin_s] = net_s
    return safe


def _safe_net_name(name: str) -> bool:
    if not name:
        return False
    return bool(_SAFE_NAME_RE.fullmatch(name))


def _is_ground(net: str) -> bool:
    return net.lower() in _GROUND_NETS


def _is_supply(net: str) -> bool:
    return net.lower() in _SUPPLY_NETS


def _discover_signal_pair(
    pin_names: list[str],
    insts: list[dict[str, Any]],
    hints: tuple[str, ...],
) -> list[str]:
    candidates: list[str] = []
    for name in pin_names:
        lname = name.lower()
        if any(hint in lname for hint in hints) and not _is_supply(lname):
            candidates.append(name)
    if len(candidates) < 2:
        for inst in insts:
            for net in inst.get("nets", {}).values():
                lname = str(net).lower()
                if any(hint in lname for hint in hints) and not _is_supply(lname):
                    candidates.append(str(net))
    return _ordered_diff_pair(_unique(candidates))


def _ordered_diff_pair(names: list[str]) -> list[str]:
    if len(names) < 2:
        return names
    p = [name for name in names if name.lower().endswith("_p")]
    n = [name for name in names if name.lower().endswith("_n")]
    if p and n:
        return [p[0], n[0]]
    return names[:2]


def _find_input_pair(
    nmos: list[dict[str, Any]], inputs: list[str],
) -> list[dict[str, Any]]:
    in_set = set(inputs)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for inst in nmos:
        gate = _net(inst, "G")
        source = _net(inst, "S")
        if gate in in_set and source:
            by_source.setdefault(source, []).append(inst)
    for group in by_source.values():
        if len(group) >= 2:
            return sorted(group, key=_name)[:2]
    # Fallback for common names when the DUT pin list is unavailable.
    candidates = [
        inst for inst in nmos
        if _net(inst, "G").lower() in {"vin_p", "vin_n", "inp", "inn", "vip", "vin"}
    ]
    by_source.clear()
    for inst in candidates:
        by_source.setdefault(_net(inst, "S"), []).append(inst)
    for group in by_source.values():
        if len(group) >= 2:
            return sorted(group, key=_name)[:2]
    return []


def _input_pair_drain_nodes(input_pair: list[dict[str, Any]]) -> list[str]:
    return _unique([_net(inst, "D") for inst in input_pair if _net(inst, "D")])


def _find_tail_device(
    nmos: list[dict[str, Any]], input_pair: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not input_pair:
        return None
    source_node = _net(input_pair[0], "S")
    for inst in nmos:
        if _name(inst) in set(_names(input_pair)):
            continue
        if _net(inst, "D") == source_node and _is_ground(_net(inst, "S")):
            return inst
    return None


def _find_diode_for_gate(
    mos: list[dict[str, Any]], gate_net: str,
) -> dict[str, Any] | None:
    if not gate_net:
        return None
    for inst in mos:
        if _net(inst, "G") == gate_net and _net(inst, "D") == gate_net:
            return inst
    return None


def _find_pmos_bias_diode(pmos: list[dict[str, Any]]) -> dict[str, Any] | None:
    for inst in pmos:
        if _net(inst, "G") and _net(inst, "G") == _net(inst, "D"):
            return inst
    return None


def _find_pmos_loads(
    pmos: list[dict[str, Any]], first_stage_nodes: list[str],
) -> list[dict[str, Any]]:
    nodes = set(first_stage_nodes)
    loads = [
        inst for inst in pmos
        if _net(inst, "D") in nodes and _is_supply(_net(inst, "S"))
    ]
    if len(loads) >= 2:
        return sorted(loads, key=_name)[:2]
    return loads


def _find_second_stage_pmos(
    pmos: list[dict[str, Any]], first_stage_nodes: list[str], outputs: list[str],
) -> list[dict[str, Any]]:
    nodes = set(first_stage_nodes)
    out_set = set(outputs)
    stage = [
        inst for inst in pmos
        if _net(inst, "G") in nodes and _net(inst, "D") in out_set
        and _is_supply(_net(inst, "S"))
    ]
    return sorted(stage, key=_name)


def _find_cmfb_actuators(
    nmos: list[dict[str, Any]], outputs: list[str],
) -> list[dict[str, Any]]:
    out_set = set(outputs)
    by_gate: dict[str, list[dict[str, Any]]] = {}
    for inst in nmos:
        if _net(inst, "D") in out_set and _is_ground(_net(inst, "S")):
            by_gate.setdefault(_net(inst, "G"), []).append(inst)
    for gate, group in by_gate.items():
        if gate and len(group) >= 2:
            return sorted(group, key=_name)
    return []


def _find_cmfb_block(generic: list[dict[str, Any]]) -> dict[str, Any] | None:
    for inst in generic:
        text = " ".join([_name(inst), *inst.get("nets", {}).keys(), *inst.get("nets", {}).values()])
        if "cmfb" in text.lower():
            return inst
    return None


def _find_miller_compensation(
    caps: list[dict[str, Any]],
    resistors: list[dict[str, Any]],
    first_stage_nodes: list[str],
    outputs: list[str],
) -> list[dict[str, Any]]:
    first_set = set(first_stage_nodes)
    out_set = set(outputs)
    entries: list[dict[str, Any]] = []
    for cap in caps:
        cap_nets = set(cap.get("nets", {}).values())
        first = sorted(cap_nets & first_set)
        other = sorted(cap_nets - first_set)
        if not first or not other:
            continue
        bridge_node = other[0]
        match_res = None
        for res in resistors:
            res_nets = set(res.get("nets", {}).values())
            if bridge_node in res_nets and res_nets & out_set:
                match_res = res
                break
        entries.append({
            "role": "miller_rc" if match_res else "miller_capacitor",
            "cap": _name(cap),
            "resistor": _name(match_res) if match_res else None,
            "first_stage_node": first[0],
            "bridge_node": bridge_node,
            "output_node": sorted(set(match_res.get("nets", {}).values()) & out_set)[0]
            if match_res else None,
        })
    return entries


def _infer_design_var_roles(
    insts: list[dict[str, Any]], device_roles: dict[str, str], names: set[str],
) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    for inst in insts:
        inst_name = _name(inst)
        inst_role = device_roles.get(inst_name, _fallback_role(inst))
        for key, value in inst.get("params", {}).items():
            if value not in names:
                continue
            role = _param_role(value, key, inst_role)
            entry = roles.setdefault(value, {"role": role, "instances": []})
            if inst_name and inst_name not in entry["instances"]:
                entry["instances"].append(inst_name)
    for inst in insts:
        if not _is_current_source(inst):
            continue
        for key, value in inst.get("params", {}).items():
            if value in names:
                role = "bias current source"
                entry = roles.setdefault(value, {"role": role, "instances": []})
                if _name(inst) not in entry["instances"]:
                    entry["instances"].append(_name(inst))
    return roles


def _fallback_role(inst: dict[str, Any]) -> str:
    cell = str(inst.get("cell", "")).upper()
    if cell.startswith("NMOS"):
        return "NMOS sizing"
    if cell.startswith("PMOS"):
        return "PMOS sizing"
    if "CAP" in cell:
        return "capacitor value"
    if "RES" in cell:
        return "resistor value"
    if cell in {"ISRC", "IDC"}:
        return "current source"
    return "device parameter"


def _param_role(var: str, param: str, inst_role: str) -> str:
    lower_var = var.lower()
    lower_param = param.lower()
    if lower_var.startswith("i") or "ibias" in lower_var or lower_param == "idc":
        return f"{inst_role} current"
    if lower_param in {"r", "res", "resistance"}:
        return f"{inst_role} resistance"
    if lower_param in {"c", "cap", "capacitance"}:
        return f"{inst_role} capacitance"
    if "finger" in lower_param or "nfin" in lower_param or lower_param in {"m", "multi", "nf"}:
        return f"{inst_role} size"
    return f"{inst_role} parameter"


def _confidence_score(*conditions: bool) -> int:
    return sum(1 for cond in conditions if cond)


def _compact_entry(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return str(entry)
    role = _safe_role_label(entry.get("role", "entry"))
    parts = [f"role={role}"]
    for key in (
        "instances", "instance", "nodes", "input_nets", "output_nodes",
        "gate_nodes", "source_node", "tail_node", "bias_node",
        "control_node", "cap", "resistor", "first_stage_node",
        "output_node",
    ):
        if key in entry and entry[key] not in (None, "", [], {}):
            parts.append(f"{key}={entry[key]}")
    return ", ".join(parts)


def _safe_role_label(role: Any) -> str:
    """Render inferred role ids as LLM-safe prose labels.

    Internal role ids can legitimately contain strings such as
    ``pmos_bias``. The final LLM feedback gate treats ``pmos_*`` /
    ``nmos_*`` tokens as foundry/model-shaped, so avoid replaying those
    machine ids verbatim in prompt-facing Markdown.
    """
    text = str(role)
    explicit = {
        "pmos_bias": "p-channel bias",
        "nmos_bias": "n-channel bias",
    }
    if text in explicit:
        return explicit[text]
    if text.lower().startswith("pmos_"):
        return "p-channel " + text.split("_", 1)[1].replace("_", " ")
    if text.lower().startswith("nmos_"):
        return "n-channel " + text.split("_", 1)[1].replace("_", " ")
    return text


def _instances_for_role(roles: dict[str, str], role: str) -> list[str]:
    return [name for name, text in roles.items() if text == role]


def _op_for(instances: dict[str, Any], name: str) -> dict[str, Any]:
    for key in (name, f"/I0/{name}"):
        raw = instances.get(key)
        if isinstance(raw, dict):
            return raw
    for key, raw in instances.items():
        if isinstance(key, str) and key.rsplit("/", 1)[-1] == name and isinstance(raw, dict):
            return raw
    return {}


def _diagnose_device_group(
    label: str, names: list[str], instances: dict[str, Any],
) -> str:
    summaries: list[str] = []
    for name in names:
        op = _op_for(instances, name)
        if not op:
            summaries.append(f"{name}: no OP data")
            continue
        region = op.get("region_label") or _region_label(op.get("region"))
        id_v = _fmt_si(op.get("id") if "id" in op else op.get("ids"), "A")
        gm = _num(op.get("gm"))
        gds = _num(op.get("gds"))
        ratio = gm / gds if gm is not None and gds and gds > 0 else None
        summaries.append(
            f"{name}: {region}, id={id_v}, gm/gds={_fmt_plain(ratio)}"
        )
    return f"- {label}: " + "; ".join(summaries)


def _region_label(value: Any) -> str:
    labels = {0: "cutoff", 1: "triode", 2: "saturation", 3: "subthreshold", 4: "breakdown"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return labels.get(int(value), f"region{int(value)}")
    return "unknown"


def _first_stage_nodes(intent: dict[str, Any]) -> list[str]:
    for entry in intent.get("signal_path", []) or []:
        if isinstance(entry, dict) and entry.get("role") == "first_stage_outputs":
            nodes = entry.get("nodes")
            if isinstance(nodes, list):
                return [str(node) for node in nodes]
    return []


def _diagnose_first_stage_nodes(nodes: list[str], op_nodes: dict[str, Any]) -> str:
    values = [_node_value(op_nodes, node) for node in nodes]
    values = [value for value in values if value is not None]
    if not values:
        return ""
    vdd = _infer_vdd(op_nodes)
    cm = sum(values) / len(values)
    state = "mid-supply"
    if vdd is not None and cm > 0.85 * vdd:
        state = "near VDD"
    elif vdd is not None and cm < 0.15 * vdd:
        state = "near ground"
    return (
        f"- First-stage output nodes {', '.join(nodes)}: "
        f"common-mode={_fmt_si(cm, 'V')} ({state})."
    )


def _diagnose_cmfb_nodes(nodes: dict[str, Any]) -> str:
    sense = _node_value(nodes, "net5")
    ref = _node_value(nodes, "net1")
    cmfb_out = _node_value(nodes, "cmfb_out")
    parts: list[str] = []
    if sense is not None and ref is not None:
        parts.append(f"sense-ref={_fmt_si(sense - ref, 'V')}")
    if cmfb_out is not None:
        parts.append(f"cmfb_out={_fmt_si(cmfb_out, 'V')}")
    if not parts:
        return ""
    return "- CMFB nodes: " + ", ".join(parts) + "."


def _node_value(nodes: dict[str, Any], name: str) -> float | None:
    for key in (name, f"/{name}", f"/I0/{name}"):
        value = _num(nodes.get(key))
        if value is not None:
            return value
    leaf = name.rsplit("/", 1)[-1]
    for key, raw in nodes.items():
        if isinstance(key, str) and key.rsplit("/", 1)[-1] == leaf:
            value = _num(raw)
            if value is not None:
                return value
    return None


def _infer_vdd(nodes: dict[str, Any]) -> float | None:
    for name in ("vdd!", "vdd", "/vdd!", "/I0/vdd!"):
        value = _node_value(nodes, name)
        if value is not None and value > 0:
            return value
    values = [_num(v) for v in nodes.values()]
    values = [v for v in values if v is not None and v > 0]
    return max(values) if values else None


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        return value if math.isfinite(value) else None
    return None


def _fmt_plain(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 100:
        return f"{value:.3g}"
    return f"{value:.2f}"


def _fmt_si(value: Any, unit: str = "") -> str:
    number = _num(value)
    if number is None:
        return "-"
    prefixes = [
        (1e-12, "p"), (1e-9, "n"), (1e-6, "u"), (1e-3, "m"),
        (1.0, ""), (1e3, "k"), (1e6, "M"), (1e9, "G"),
    ]
    abs_v = abs(number)
    scale, prefix = 1.0, ""
    for candidate_scale, candidate_prefix in prefixes:
        if abs_v < candidate_scale * 1000:
            scale, prefix = candidate_scale, candidate_prefix
            break
    return f"{number / scale:.3g}{prefix}{unit}"


def _unique(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
