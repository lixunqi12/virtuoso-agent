"""Red-team runners.

``run_offline`` drives the fixed probe suite (per-probe). ``run_session`` runs
one adaptive attacker session (per-session): a planner proposes an attack each
turn, the runner applies it, scans the LLM-facing text, and stops early on the
first leak. The two are aggregated under separate denominators (see schema).
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest.mock import MagicMock

from src.safe_bridge import SafeBridge
from src.redteam.canaries import default_registry
from src.redteam.oracle import scan
from src.redteam.probes import ProbeContext, all_probes
from src.redteam.schema import Trial

_PDK_MAP = """\
generic_cell_name: "GENERIC_DEVICE"
valid_aliases: [NMOS, NMOS_LVT, PMOS, PMOS_LVT, MIM_CAP]
model_info_keys: [toxe, u0, vth0, k1, k2, pclm]
allowed_params: [w, l, nf, m, multi, wf]
"""

# A planned attack: a thunk returning the LLM-facing text it produced (it may
# raise ValueError/RuntimeError if a guard rejected it).
AttackFn = Callable[[], str]
# (turn, last_feedback) -> next attack, or None to end the session.
Planner = Callable[[int, str], Optional[AttackFn]]


def _build_bridge(tmp: Path) -> tuple[SafeBridge, MagicMock]:
    client = MagicMock()
    pdk = tmp / "pdk_map.yaml"
    pdk.write_text(_PDK_MAP, encoding="utf-8")
    bridge = SafeBridge(client, str(pdk), skill_dir=tmp / "no_skill")
    return bridge, client


@contextmanager
def offline_context() -> Iterator[ProbeContext]:
    """A ProbeContext backed by a real SafeBridge over a mock EDA host."""
    with tempfile.TemporaryDirectory() as d:
        bridge, client = _build_bridge(Path(d))
        yield ProbeContext(bridge=bridge, mock_client=client,
                           canaries=default_registry())


def run_offline(probes=None) -> list[Trial]:
    probes = probes if probes is not None else all_probes()
    trials: list[Trial] = []
    with offline_context() as ctx:
        for probe in probes:
            outcome = probe.run(ctx)
            leak = scan(outcome.text, ctx.canaries)
            trials.append(Trial(
                tier=probe.tier, attacker="fixed", trial_id=probe.probe_id,
                leaked=leak.leaked, matched=leak.matched,
            ))
    return trials


def run_session(planner: Planner, ctx: ProbeContext, *,
                tier: str = "2", max_turns: int = 5) -> Trial:
    last_feedback = ""
    for turn in range(max_turns):
        attack = planner(turn, last_feedback)
        if attack is None:
            break
        try:
            text = attack()
        except (ValueError, RuntimeError):
            text = ""  # a guard rejected the attack -> nothing reaches the LLM
        leak = scan(text, ctx.canaries)
        last_feedback = text
        if leak.leaked:
            return Trial(tier=tier, attacker="adaptive",
                         trial_id=f"session_turn{turn}", leaked=True,
                         matched=leak.matched)
    return Trial(tier=tier, attacker="adaptive", trial_id="session",
                 leaked=False, matched=())
