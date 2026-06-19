"""Fixed adversarial probe suite (Tier 1 / Tier 2), deterministic.

Each probe drives a REAL SafeBridge sanitizer with canary-laden input and
returns the text that would reach the LLM (``""`` if a guard rejected the
attack -- rejection is a successful defense). The runner then scans that text
with the oracle, so the suite measures canary survival through the production
scrubbing path, not through a re-implementation of it.

Tier 1 = direct content exposure (the EDA host hands back canary-laden data).
Tier 2 = injection (canary smuggled through a name / identifier / argument).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from unittest.mock import MagicMock

from src.redteam.canaries import Canary


@dataclass
class ProbeContext:
    bridge: object
    mock_client: MagicMock
    canaries: tuple[Canary, ...]

    def cval(self, asset_class: str) -> str:
        return next(c.value for c in self.canaries if c.asset_class == asset_class)


@dataclass(frozen=True)
class ProbeOutcome:
    text: str       # LLM-facing text the attack produced ("" if rejected)
    rejected: bool   # True if a guard raised (successful defense)


@dataclass(frozen=True)
class Probe:
    probe_id: str
    tier: str
    description: str
    run: Callable[[ProbeContext], "ProbeOutcome"]


def _attack(fn: Callable[[], str]) -> ProbeOutcome:
    """A guard raising (ValueError/RuntimeError) = the attack was rejected."""
    try:
        return ProbeOutcome(text=fn(), rejected=False)
    except (ValueError, RuntimeError):
        return ProbeOutcome(text="", rejected=True)


# --------------------------- Tier 1: direct exposure --------------------------- #

def _t1_read_circuit_canary_cell(ctx: ProbeContext) -> ProbeOutcome:
    def attack() -> str:
        ctx.mock_client.execute_skill.return_value = {
            "instances": [{
                "name": "M1", "cell": ctx.cval("cell"), "lib": "GENERIC_PDK",
                "params": {"w": "1u", "l": "100n"},
            }]
        }
        return json.dumps(ctx.bridge.read_circuit("mylib", "mycell"))
    return _attack(attack)


def _t1_read_circuit_canary_model_param(ctx: ProbeContext) -> ProbeOutcome:
    def attack() -> str:
        ctx.mock_client.execute_skill.return_value = {
            "instances": [{
                "name": "M1", "cell": "NMOS", "lib": "GENERIC_PDK",
                "params": {"w": "1u", "model": ctx.cval("model")},
            }]
        }
        return json.dumps(ctx.bridge.read_circuit("mylib", "mycell"))
    return _attack(attack)


def _t1_read_op_point_canary_key(ctx: ProbeContext) -> ProbeOutcome:
    def attack() -> str:
        ctx.mock_client.execute_skill.return_value = {
            "M1": {"gm": 1e-3, "id": 1e-4, ctx.cval("model"): 1.23},
        }
        return json.dumps(ctx.bridge.read_op_point("mylib", "mycell"))
    return _attack(attack)


def _t1_alias_canary_cell(ctx: ProbeContext) -> ProbeOutcome:
    def attack() -> str:
        return str(ctx.bridge._alias_cell(ctx.cval("cell")))
    return _attack(attack)


# ------------------------------ Tier 2: injection ------------------------------ #

def _t2_read_circuit_name_breakout(ctx: ProbeContext) -> ProbeOutcome:
    def attack() -> str:
        smuggled = f'lib") {ctx.cval("model")} ("'
        return json.dumps(ctx.bridge.read_circuit(smuggled, "cell"))
    return _attack(attack)


def _t2_design_var_name_is_canary(ctx: ProbeContext) -> ProbeOutcome:
    def attack() -> str:
        name = ctx.cval("model")
        if ctx.bridge._is_allowed_param_name(name):
            # would have been written into a command, reaching the prompt
            return f"set design var {name} = 1"
        return ""  # rejected by the whitelist -> nothing reaches the LLM
    return _attack(attack)


def all_probes() -> tuple[Probe, ...]:
    return (
        Probe("t1_circuit_canary_cell", "1",
              "canary cell name returned by read_circuit",
              _t1_read_circuit_canary_cell),
        Probe("t1_circuit_canary_model", "1",
              "canary model param returned by read_circuit",
              _t1_read_circuit_canary_model_param),
        Probe("t1_oppoint_canary_key", "1",
              "canary key returned by read_op_point",
              _t1_read_op_point_canary_key),
        Probe("t1_alias_canary_cell", "1",
              "canary cell passed to _alias_cell",
              _t1_alias_canary_cell),
        Probe("t2_circuit_name_breakout", "2",
              "canary smuggled via a lib-name SKILL breakout",
              _t2_read_circuit_name_breakout),
        Probe("t2_design_var_is_canary", "2",
              "canary used as a design-variable name",
              _t2_design_var_name_is_canary),
    )
