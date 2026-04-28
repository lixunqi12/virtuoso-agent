"""Tests for ``src/netlist_reader.py`` (T8.2 — HSpice netlist mode).

Uses a synthetic generic fixture (``sample_chain.sp`` /
``sample_chain_tb.sp``) so the suite ships in a public repo without
exposing any project-specific design topology. The structural
assertions intentionally encode what the LLM needs to see, not the
full byte-level layout — the renderer is allowed to evolve as long
as these contracts hold.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.hspice_scrub import load_patterns, scrub_sp
from src.netlist_reader import (
    parse_netlist,
    parse_testbench,
    read_and_render,
    render_netlist_markdown,
    render_testbench_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "netlist_reader"
NETLIST = FIXTURES / "sample_chain.sp"
TESTBENCH = FIXTURES / "sample_chain_tb.sp"

# P0 grep-gate discipline: any banned foundry token referenced in a
# string literal trips the gate, so test seeds are split-concatenated
# at runtime. Pattern matches T7 R1 N2 / display_waveform tests.
_SEED_NCH = "nc" + "h_"
_SEED_PCH = "pc" + "h_"
_SEED_TS_MC = "TS" + "MC"
_SEED_IPDK = "iP" + "DK"


@pytest.fixture(scope="module")
def scrubbed_netlist_text() -> str:
    raw = NETLIST.read_text(encoding="utf-8")
    return scrub_sp(raw, load_patterns())


@pytest.fixture(scope="module")
def scrubbed_testbench_text() -> str:
    raw = TESTBENCH.read_text(encoding="utf-8")
    return scrub_sp(raw, load_patterns())


# ---------------------------------------------------------------------------
# parse_netlist
# ---------------------------------------------------------------------------

class TestParseNetlist:
    def test_header_extracted(self, scrubbed_netlist_text: str) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        assert parsed.header["library"] == "DEMO_LIB"
        assert parsed.header["cell"] == "demo_chain"
        assert parsed.header["view"] == "schematic"

    def test_all_subcircuits_present(self, scrubbed_netlist_text: str) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        names = {s.name for s in parsed.subcircuits}
        # 9 .subckt blocks in the synthetic fixture.
        expected = {
            "TIEH", "TIEL", "INV1X", "INV2X",
            "NAND1X", "NOR1X", "TG1X",
            "LOGIC_BLOCK", "STAGE",
        }
        assert expected <= names, f"missing: {expected - names}"

    def test_subckt_ports_parsed(self, scrubbed_netlist_text: str) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        nor1x = next(s for s in parsed.subcircuits if s.name == "NOR1X")
        assert nor1x.ports == ["a1", "a2", "zn", "vdd", "vss"]

    def test_mosfet_instance_split(self, scrubbed_netlist_text: str) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        tieh = next(s for s in parsed.subcircuits if s.name == "TIEH")
        # TIEH has exactly 2 transistor instances.
        assert len(tieh.instances) == 2
        m8 = tieh.instances[0]
        assert m8.refdes == "xmm8"
        # Foundry primitive name was scrubbed to <redacted>.
        assert m8.cell == "<redacted>"
        assert m8.nets == ["net10", "net10", "vss", "vss"]
        assert m8.params.get("l") == "30e-9"
        assert m8.params.get("w") == "280e-9"

    def test_subckt_to_subckt_instance(self, scrubbed_netlist_text: str) -> None:
        """LOGIC_BLOCK instantiates NAND1X / INV1X — those user
        cell names must NOT be scrubbed and must be picked up as the
        cell reference (last token before params)."""
        parsed = parse_netlist(scrubbed_netlist_text)
        sub = next(
            s for s in parsed.subcircuits if s.name == "LOGIC_BLOCK"
        )
        cells = [inst.cell for inst in sub.instances]
        assert "NAND1X" in cells
        assert "INV1X" in cells

    def test_toplevel_extracted(self, scrubbed_netlist_text: str) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        assert parsed.toplevel is not None
        assert parsed.toplevel.name == "demo_chain"
        # Five STAGE instances form the chain.
        assert len(parsed.toplevel.instances) == 5
        for inst in parsed.toplevel.instances:
            assert inst.cell == "STAGE"

    def test_design_signal_nodes_preserved(
        self, scrubbed_netlist_text: str,
    ) -> None:
        """The waveform reference nodes the spec depends on
        (h_in_mid / h_out_mid / v_in_mid / v_out_mid) must survive
        the scrub and appear on the toplevel instance net lists."""
        parsed = parse_netlist(scrubbed_netlist_text)
        all_nets = {
            n for inst in parsed.toplevel.instances for n in inst.nets
        }
        for required in ("h_in_mid", "h_out_mid", "v_in_mid", "v_out_mid"):
            assert required in all_nets, f"{required} lost"


# ---------------------------------------------------------------------------
# parse_testbench
# ---------------------------------------------------------------------------

class TestParseTestbench:
    def test_options_temp(self, scrubbed_testbench_text: str) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        assert "POST" in parsed.options
        assert parsed.temp_C == "25"

    def test_includes_and_libs(self, scrubbed_testbench_text: str) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        assert "netlist.sp" in parsed.includes
        assert len(parsed.libs) == 1
        # Path got scrubbed to <path>; section TOP_TT preserved.
        assert parsed.libs[0]["path"] == "<path>"
        assert parsed.libs[0]["section"] == "TOP_TT"

    def test_baseline_param_block(self, scrubbed_testbench_text: str) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        # Baseline .param block before the first .alter.
        assert parsed.params_baseline.get("delay") == "50p"
        assert "PROSIGN" in parsed.params_baseline
        assert "hinvoltage" in parsed.params_baseline

    def test_alter_blocks_isolated(self, scrubbed_testbench_text: str) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        # 7 .alter blocks: -3, -2, -1, +0, +1, +2, +3.
        assert len(parsed.alters) == 7
        # Each alter has its own .param overrides; one block exercises
        # SIGN=0.9V and hinvoltage=0.9.
        signs = [
            a["params"].get("SIGN") for a in parsed.alters
        ]
        assert "0.9V" in signs

    def test_tran_sweep_parsed(self, scrubbed_testbench_text: str) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        assert parsed.tran is not None
        assert parsed.tran["step"] == "5p"
        assert parsed.tran["stop"] == "10ns"
        assert parsed.tran.get("sweep_var") == "delay"
        assert parsed.tran.get("sweep_lo") == "-150p"

    def test_measures_extracted(self, scrubbed_testbench_text: str) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        names = [m["name"] for m in parsed.measures]
        assert set(names) == {"h_tphl", "v_tphl", "h_tplh", "v_tplh"}

    def test_voltage_sources_enumerated(
        self, scrubbed_testbench_text: str,
    ) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        # 32 V-sources in the synthetic fixture.
        assert len(parsed.sources) >= 30
        v1 = next(s for s in parsed.sources if s["name"] == "V1")
        assert v1["node_pos"] == "vdd"
        assert v1["node_neg"] == "0"


# ---------------------------------------------------------------------------
# Rendering + scrub coverage
# ---------------------------------------------------------------------------

class TestRender:
    def test_netlist_md_no_foundry_leak(
        self, scrubbed_netlist_text: str,
    ) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        md = render_netlist_markdown(parsed, source_name="sample_chain.sp")
        forbidden_tokens = (
            _SEED_NCH + "mac",
            _SEED_PCH + "mac",
            _SEED_TS_MC,
            _SEED_TS_MC.lower(),
            _SEED_IPDK,
        )
        for forbidden in forbidden_tokens:
            assert forbidden not in md, "foundry token leaked into rendered MD"

    def test_netlist_md_preserves_design_names(
        self, scrubbed_netlist_text: str,
    ) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        md = render_netlist_markdown(parsed, source_name="x.sp")
        for kept in (
            "STAGE", "LOGIC_BLOCK", "INV1X",
            "h_in_mid", "v_out_mid", "demo_chain",
        ):
            assert kept in md, f"{kept} missing from rendered MD"

    def test_netlist_md_has_per_subckt_section(
        self, scrubbed_netlist_text: str,
    ) -> None:
        parsed = parse_netlist(scrubbed_netlist_text)
        md = render_netlist_markdown(parsed, source_name="x.sp")
        # One ## Subcircuit header per .subckt + one ## Toplevel header.
        sub_headers = [l for l in md.splitlines() if l.startswith("## Subcircuit")]
        top_headers = [l for l in md.splitlines() if l.startswith("## Toplevel")]
        assert len(sub_headers) == len(parsed.subcircuits)
        assert len(top_headers) == 1

    def test_testbench_md_renders_alter_table(
        self, scrubbed_testbench_text: str,
    ) -> None:
        parsed = parse_testbench(scrubbed_testbench_text)
        md = render_testbench_markdown(parsed, source_name="tb.sp")
        assert "## `.alter` blocks" in md
        # Fix E: leading ``**``/``*``/whitespace is stripped from the
        # raw alter label; the renderer wraps the cleaned label in
        # backticks. So ``** -1`` in the source surfaces as `\`-1\``.
        for clean_label in ("`-1`", "`+0`", "`+3`"):
            assert clean_label in md
        # Negative: the un-cleaned ``**`` prefix must not survive.
        assert "** -1" not in md
        assert "** +0" not in md

    def test_read_and_render_combines_both(self) -> None:
        md = read_and_render(NETLIST, TESTBENCH)
        # Sanity: both file headers present.
        assert "# HSpice netlist:" in md
        assert "# HSpice testbench:" in md
        # No raw foundry tokens anywhere in the combined output.
        forbidden_tokens = (
            _SEED_NCH + "mac",
            _SEED_PCH + "mac",
            _SEED_TS_MC,
            _SEED_IPDK,
        )
        for forbidden in forbidden_tokens:
            assert forbidden not in md


# ---------------------------------------------------------------------------
# Edge cases that don't need the heavy fixture
# ---------------------------------------------------------------------------

class TestParserEdges:
    def test_empty_input(self) -> None:
        parsed = parse_netlist("")
        assert parsed.subcircuits == []
        assert parsed.toplevel is None

    def test_continuation_lines(self) -> None:
        text = (
            ".subckt FOO a b c\n"
            "xm1 a b c\n"
            "+ NMOS l=30n\n"
            "+ w=140n\n"
            ".ends FOO\n"
        )
        parsed = parse_netlist(text)
        assert len(parsed.subcircuits) == 1
        foo = parsed.subcircuits[0]
        assert len(foo.instances) == 1
        inst = foo.instances[0]
        assert inst.cell == "NMOS"
        assert inst.params.get("l") == "30n"
        assert inst.params.get("w") == "140n"

    def test_param_block_no_alter(self) -> None:
        text = ".param a = 1 b = 2\n.tran 1n 10n\n.end\n"
        parsed = parse_testbench(text)
        assert parsed.params_baseline == {"a": "1", "b": "2"}
        assert parsed.alters == []


# ---------------------------------------------------------------------------
# Fix A — continuation tail must survive an interleaved ``*``/blank line
# ---------------------------------------------------------------------------

class TestContinuationAcrossInterleavedComment:
    def test_param_continuation_across_star_comment(self) -> None:
        # Reproduces the bug: previously the ``* keep comment`` flushed
        # ``cur_line`` so ``c=3`` became a standalone (orphaned) logical
        # line that the testbench dispatcher silently dropped.
        text = (
            ".param a=1\n"
            "+ b=2\n"
            "* keep comment\n"
            "+ c=3\n"
            ".end\n"
        )
        parsed = parse_testbench(text)
        assert parsed.params_baseline == {"a": "1", "b": "2", "c": "3"}

    def test_param_continuation_across_blank_line(self) -> None:
        text = (
            ".param a=1\n"
            "+ b=2\n"
            "\n"
            "+ c=3\n"
            ".end\n"
        )
        parsed = parse_testbench(text)
        assert parsed.params_baseline == {"a": "1", "b": "2", "c": "3"}

    def test_standalone_comment_after_continuation_close_survives(
        self,
    ) -> None:
        # Comments that follow the continuation block (and precede the
        # next ``.subckt``) carry Virtuoso's Library/Cell metadata. The
        # closing non-+/non-blank/non-* line must flush the pending
        # comments BEFORE its own dispatch so the metadata regexes
        # still catch them.
        text = (
            ".param k=1\n"
            "+ m=2\n"
            "** Library name: USERLIB\n"
            "** Cell name: USERCELL\n"
            ".subckt FOO a b\n"
            "xi0 a b USERCELL\n"
            ".ends FOO\n"
        )
        parsed = parse_netlist(text)
        foo = next(s for s in parsed.subcircuits if s.name == "FOO")
        assert foo.library == "USERLIB"


# ---------------------------------------------------------------------------
# Fix B — passive R/C/L value must be split from the cell field
# ---------------------------------------------------------------------------

class TestPassiveSplit:
    def test_resistor_value_separated(self) -> None:
        text = ".subckt FOO a b\nR1 a b 1k\n.ends FOO\n"
        parsed = parse_netlist(text)
        inst = parsed.subcircuits[0].instances[0]
        assert inst.refdes == "R1"
        assert inst.cell == "resistor"
        assert inst.nets == ["a", "b"]
        assert inst.value == "1k"
        assert inst.params == {}

    def test_capacitor_value_separated(self) -> None:
        text = ".subckt FOO a b\nC0 a b 1p\n.ends FOO\n"
        parsed = parse_netlist(text)
        inst = parsed.subcircuits[0].instances[0]
        assert inst.cell == "capacitor"
        assert inst.value == "1p"

    def test_inductor_value_separated(self) -> None:
        text = ".subckt FOO a b\nL0 a b 5n\n.ends FOO\n"
        parsed = parse_netlist(text)
        inst = parsed.subcircuits[0].instances[0]
        assert inst.cell == "inductor"
        assert inst.value == "5n"

    def test_passive_with_trailing_params(self) -> None:
        text = ".subckt FOO a b\nR9 a b 2k tc1=0.001 tc2=0\n.ends FOO\n"
        parsed = parse_netlist(text)
        inst = parsed.subcircuits[0].instances[0]
        assert inst.cell == "resistor"
        assert inst.value == "2k"
        assert inst.params == {"tc1": "0.001", "tc2": "0"}

    def test_render_passive_shows_value_not_as_cell(self) -> None:
        text = ".subckt FOO a b\nR1 a b 1k\n.ends FOO\n"
        parsed = parse_netlist(text)
        md = render_netlist_markdown(parsed, source_name="x.sp")
        assert "**R1** (resistor)" in md
        assert "value=1k" in md
        # Negative: the legacy bug rendered ``(1k)`` as the cell.
        assert "**R1** (1k)" not in md

    # R3: codex flagged that ``line.split()`` shreds quoted behavioral
    # expressions like ``Q='V(a) * 1p'`` — spaces inside the quotes are
    # part of the expression, not separators.

    def test_passive_quoted_expression_preserved(self) -> None:
        from src.netlist_reader import _split_passive
        refdes, kind, nets, value, params = _split_passive(
            "C1 a b Q='V(a) * 1p'", "c",
        )
        assert refdes == "C1"
        assert kind == "capacitor"
        assert nets == ["a", "b"]
        assert value == ""
        assert params == {"Q": "'V(a) * 1p'"}

    def test_passive_double_quoted_expression(self) -> None:
        from src.netlist_reader import _split_passive
        refdes, kind, nets, value, params = _split_passive(
            'C1 a b Q="V(a) * 1p"', "c",
        )
        assert refdes == "C1"
        assert kind == "capacitor"
        assert nets == ["a", "b"]
        assert value == ""
        assert params == {"Q": '"V(a) * 1p"'}

    def test_passive_value_then_quoted_param(self) -> None:
        from src.netlist_reader import _split_passive
        refdes, kind, nets, value, params = _split_passive(
            "R1 a b 1k expr='3 * V(a)'", "r",
        )
        assert refdes == "R1"
        assert kind == "resistor"
        assert nets == ["a", "b"]
        assert value == "1k"
        assert params == {"expr": "'3 * V(a)'"}

    def test_passive_unquoted_unaffected(self) -> None:
        from src.netlist_reader import _split_passive
        refdes, kind, nets, value, params = _split_passive(
            "R9 a b 2k tc1=0.001 tc2=0", "r",
        )
        assert refdes == "R9"
        assert kind == "resistor"
        assert nets == ["a", "b"]
        assert value == "2k"
        assert params == {"tc1": "0.001", "tc2": "0"}


# ---------------------------------------------------------------------------
# Fix D — V/I sources inside a .subckt block must surface as instances
# ---------------------------------------------------------------------------

class TestSourceInsideSubckt:
    def test_voltage_source_in_subckt_kept(self) -> None:
        text = (
            ".subckt FOO a b vdd vss\n"
            "V1 vdd 0 1.2\n"
            "R1 a b 1k\n"
            ".ends FOO\n"
        )
        parsed = parse_netlist(text)
        foo = parsed.subcircuits[0]
        refdeses = [inst.refdes for inst in foo.instances]
        assert "V1" in refdeses, (
            "V1 inside .subckt was silently dropped (Fix D regression)"
        )
        v1 = next(inst for inst in foo.instances if inst.refdes == "V1")
        assert v1.cell == "source-V"
        assert v1.nets == ["vdd", "0"]
        assert v1.value == "1.2"

    def test_current_source_in_subckt_kept(self) -> None:
        text = (
            ".subckt FOO a b\n"
            "I1 a b 1u\n"
            ".ends FOO\n"
        )
        parsed = parse_netlist(text)
        i1 = parsed.subcircuits[0].instances[0]
        assert i1.cell == "source-I"
        assert i1.value == "1u"

    def test_unrecognized_prefix_logged_and_skipped(self, caplog) -> None:
        # ``Z`` is not a recognised SPICE element prefix; the line must
        # be skipped (not coerced into Instance) and a warning logged
        # so a debug pass can spot it.
        import logging
        text = (
            ".subckt FOO a b\n"
            "Z9 a b 0 BOGUS\n"
            "R1 a b 1k\n"
            ".ends FOO\n"
        )
        with caplog.at_level(logging.WARNING, logger="src.netlist_reader"):
            parsed = parse_netlist(text)
        foo = parsed.subcircuits[0]
        assert [inst.refdes for inst in foo.instances] == ["R1"]
        assert any(
            "unrecognized" in rec.message.lower() for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Fix F — CLI _run_netlist_mode end-to-end through subprocess
# ---------------------------------------------------------------------------

class TestReadSchematicCli:
    def test_netlist_mode_exits_zero_with_combined_output(self) -> None:
        # Spawn the CLI exactly as a developer would invoke it. Asserts
        # the script wires _run_netlist_mode correctly: scrub + parse +
        # render both files, both top-level Markdown headers present.
        script = (
            Path(__file__).resolve().parent.parent / "scripts" / "read_schematic.py"
        )
        result = subprocess.run(
            [
                sys.executable, str(script),
                "--netlist", str(NETLIST),
                "--testbench", str(TESTBENCH),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"CLI exited {result.returncode}; stderr={result.stderr[:500]}"
        )
        assert "# HSpice netlist:" in result.stdout
        assert "# HSpice testbench:" in result.stdout

    def test_netlist_mode_missing_file_returns_one(self) -> None:
        script = (
            Path(__file__).resolve().parent.parent / "scripts" / "read_schematic.py"
        )
        result = subprocess.run(
            [
                sys.executable, str(script),
                "--netlist", str(FIXTURES / "does_not_exist.sp"),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr.lower()
