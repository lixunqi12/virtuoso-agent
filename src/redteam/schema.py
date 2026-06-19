"""Trial records + ASR aggregation for the red-team extraction experiment.

Per the paper review (P2a), fixed-probe and adaptive denominators are kept
strictly separate: fixed-probe trials are counted per-probe, adaptive trials
per-session, and there is no merged total ASR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

_MODES = ("fixed", "adaptive")


@dataclass(frozen=True)
class Trial:
    """One red-team attempt.

    ``attacker`` is ``"fixed"`` (deterministic probe, counted per-probe) or
    ``"adaptive"`` (LLM planner session, counted per-session). ``matched``
    holds the oracle tokens that leaked (local audit only; never serialized
    into tracked output).
    """

    tier: str
    attacker: str
    trial_id: str
    leaked: bool
    matched: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModeSummary:
    trials: int = 0
    leaks: int = 0

    @property
    def asr(self) -> float:
        return 0.0 if self.trials == 0 else self.leaks / self.trials


@dataclass(frozen=True)
class TierSummary:
    tier: str
    fixed: ModeSummary = field(default_factory=ModeSummary)
    adaptive: ModeSummary = field(default_factory=ModeSummary)


def summarize(trials: Iterable[Trial]) -> dict[str, TierSummary]:
    """Aggregate trials into per-tier, per-attacker-mode ASR.

    The ``fixed`` (per-probe) and ``adaptive`` (per-session) tallies are
    independent; they are never combined into one denominator.
    """
    # tier -> {mode: [trials, leaks]}
    acc: dict[str, dict[str, list[int]]] = {}
    for t in trials:
        if t.attacker not in _MODES:
            raise ValueError(
                f"unknown attacker mode {t.attacker!r}; expected one of {_MODES}"
            )
        modes = acc.setdefault(t.tier, {m: [0, 0] for m in _MODES})
        modes[t.attacker][0] += 1
        if t.leaked:
            modes[t.attacker][1] += 1
    return {
        tier: TierSummary(
            tier=tier,
            fixed=ModeSummary(trials=m["fixed"][0], leaks=m["fixed"][1]),
            adaptive=ModeSummary(trials=m["adaptive"][0], leaks=m["adaptive"][1]),
        )
        for tier, m in acc.items()
    }
