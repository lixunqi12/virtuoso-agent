"""Regression test: scripts/run_agent.py --llm argparse choices.

Guards against the 2026-05-12 D4 class bug where new LLM clients landed
in `src/llm_client.py` and were added to `scripts/run_benchmark.py`
CHECKPOINTS, but their CLI string was missing from
`scripts/run_agent.py` argparse `choices=[...]` — causing 15/33 grid
cells to die immediately at `argparse: invalid choice`.

Single-source-of-truth: `scripts.run_agent.LLM_CHOICES` is the canonical
list both the argparse setup AND this test consume.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_agent import LLM_CHOICES, parse_args  # noqa: E402
from scripts.run_benchmark import CHECKPOINTS  # noqa: E402
from src.llm_client import create_llm_client  # noqa: E402


@pytest.mark.parametrize(
    "ckpt", CHECKPOINTS, ids=lambda c: c["name"],
)
def test_every_checkpoint_llm_is_in_argparse_choices(ckpt: dict[str, str]) -> None:
    """Every `llm` key referenced by a benchmark checkpoint must be a
    valid argparse choice — otherwise the grid runner subprocess dies
    at the CLI boundary before any LLM call happens.
    """
    assert ckpt["llm"] in LLM_CHOICES, (
        f"Checkpoint {ckpt['name']!r} uses llm={ckpt['llm']!r} but that "
        f"string is missing from scripts/run_agent.py LLM_CHOICES "
        f"{LLM_CHOICES}. Add it to LLM_CHOICES (and ensure "
        f"src.llm_client.create_llm_client dispatch covers it)."
    )


def test_llm_choices_no_duplicates() -> None:
    assert len(LLM_CHOICES) == len(set(LLM_CHOICES))


def test_llm_choices_includes_d3_additions() -> None:
    """The D3-era additions (openai/mimo/deepseek) are the specific ones
    that were missing on 2026-05-12 — pin them so an over-zealous
    refactor can't silently drop them again.
    """
    for required in ("openai", "mimo", "deepseek"):
        assert required in LLM_CHOICES, (
            f"{required!r} missing from LLM_CHOICES — this is the "
            f"2026-05-12 D4 regression vector."
        )


def test_maestro_test_arg_is_optional_and_distinct_from_tb_cell(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_agent.py",
            "--spec", "spec.md",
            "--lib", "pll",
            "--cell", "LC_VCO",
            "--tb-cell", "LC_VCO_tb",
            "--maestro-test", "pll_LC_VCO_tb_1",
        ],
    )
    args = parse_args()
    assert args.tb_cell == "LC_VCO_tb"
    assert args.maestro_test == "pll_LC_VCO_tb_1"


def test_sweep_results_root_undoes_msys_path_conversion(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_agent.py",
            "--spec", "spec.md",
            "--lib", "pll",
            "--cell", "LC_VCO",
            "--tb-cell", "LC_VCO_tb",
            "--sweep-results-root",
            "C:/msys64/home/u/sim/Interactive.0",
        ],
    )
    args = parse_args()
    assert args.sweep_results_root == "/home/u/sim/Interactive.0"


@pytest.mark.parametrize("llm", LLM_CHOICES)
def test_every_argparse_choice_is_dispatchable(llm: str, monkeypatch) -> None:
    """The reverse direction: every CLI choice must correspond to a real
    factory entry in `create_llm_client`. Catches the dual class of bug
    where a CLI string is registered but the factory raises KeyError.
    """
    # Stub out API keys so the client constructor doesn't try to talk
    # to a network. We're only verifying the factory key is registered.
    for var in (
        "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "KIMI_API_KEY",
        "MINIMAX_API_KEY", "OPENAI_API_KEY", "MIMO_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.setenv(var, "test-stub")

    # `ollama` is local-only and doesn't have an API key path — it may
    # raise on construction for unrelated reasons (no daemon running).
    # Skip the runtime instantiation check for it; the static dispatch
    # entry is verified by inspection in create_llm_client.
    if llm == "ollama":
        # Just verify the factory recognizes the key — accept either
        # KeyError (dict miss) or ValueError (factory's own "unknown
        # provider" raise). It may also raise other things attempting
        # the network — those are fine.
        try:
            create_llm_client(provider=llm)
        except (KeyError, ValueError) as exc:
            pytest.fail(
                f"Factory dispatch missing for llm={llm!r}: {exc}"
            )
        except Exception:
            # Any other exception (connection refused, etc.) is fine —
            # we only care that the dispatch key exists.
            pass
        return

    try:
        create_llm_client(provider=llm)
    except (KeyError, ValueError) as exc:
        pytest.fail(
            f"Factory dispatch missing for llm={llm!r}: {exc}"
        )
    except Exception:
        # Non-dispatch failures (e.g. API library not installed in the
        # test env, model-name validation) are out of scope for this
        # regression test — we're checking the CLI-to-factory plumbing,
        # not full client construction.
        pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
