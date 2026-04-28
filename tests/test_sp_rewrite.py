"""Unit tests for src.sp_rewrite (T8.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.sp_rewrite import (
    ParamRewriteError,
    rewrite_param_file,
    rewrite_params,
)


# --------------------------------------------------------------------- #
#  Synthetic fixtures (intentionally minimal -- no foundry tokens, no
#  real schematic content, so the P0 gate allowlist does not need an
#  exemption for this file).
# --------------------------------------------------------------------- #

DELAY_TB = (
    ".TEMP 27\n"
    ".OPTION POST\n"
    "\n"
    ".PARAM delay = 50p\n"
    "+ SIGN = 0V\n"
    "+ LSB = 0V\n"
    "+ LSB2 = 0V\n"
    "+ MSB = 0V\n"
    "+ hinvoltage = 0\n"
    "\n"
    "V1 vdd 0 0.8V\n"
    "V21 H_IN 0 0.8V PWL (0 '0.8-hinvoltage' 7n '0.8-hinvoltage' 7.01n 'hinvoltage')\n"
    "V7 WBL 0 0V PWL (0 0 5n 0 5.01n SIGN 6n SIGN 6.01n 0)\n"
    "\n"
    ".TRAN 5p 10ns sweep delay -90p 90p 15p\n"
    ".measure tran h_tphl trig v(h_in_mid) val=0.4v rise=1 targ v(h_out_mid) val=0.4v fall=1\n"
    "\n"
    ".alter -1\n"
    ".PARAM delay = 50p\n"
    "+ SIGN = 0V\n"
    "+ LSB = 0V\n"
    "+ LSB2 = 0.8V\n"
    "+ MSB = 0V\n"
    "+ hinvoltage = 0\n"
    "\n"
    ".END\n"
)

DELAY_WHITELIST = ("delay", "hinvoltage", "sign", "lsb", "lsb2", "msb")

MATCHING_NETLIST = (
    "* matching_test netlist (synthetic fixture)\n"
    ".PARAM num_finger_n0=1 num_finger_n1=1 num_finger_p0=1 num_finger_p1=1\n"
    "\n"
    ".subckt TRI_SVT n0 n1 p0 p1 vdd vss zn\n"
    "xm0 zn n0 vss vss myn nf=num_finger_n0\n"
    "xm1 n2 n1 zn vss myn nf=num_finger_n1\n"
    "xm2 zn p0 vdd vdd myp nf=num_finger_p0\n"
    "xm3 n3 p1 zn vdd myp nf=num_finger_p1\n"
    ".ends\n"
)

MATCHING_WHITELIST = (
    "num_finger_n0", "num_finger_n1", "num_finger_p0", "num_finger_p1",
)


# --------------------------------------------------------------------- #
#  Multi-line .PARAM block (delay_test-style testbench)
# --------------------------------------------------------------------- #

def test_multiline_basic_rewrite() -> None:
    out = rewrite_params(
        DELAY_TB, {"delay": "75p", "hinvoltage": "0.8"}, DELAY_WHITELIST,
    )
    assert ".PARAM delay = 75p\n" in out
    assert "+ hinvoltage = 0.8\n" in out
    # Untouched keys keep their literal text.
    assert "+ SIGN = 0V\n" in out
    assert "+ LSB = 0V\n" in out


def test_multiline_unit_preservation_bare_number() -> None:
    """`50p` + bare `75` -> `75p` (engineering suffix borrowed)."""
    out = rewrite_params(DELAY_TB, {"delay": 75}, DELAY_WHITELIST)
    assert ".PARAM delay = 75p\n" in out


def test_multiline_unit_override_explicit() -> None:
    """`50p` + `75n` -> `75n` (explicit suffix wins over inferred)."""
    out = rewrite_params(DELAY_TB, {"delay": "75n"}, DELAY_WHITELIST)
    assert ".PARAM delay = 75n\n" in out


def test_multiline_voltage_suffix_preserved() -> None:
    """`0V` + bare `0.8` -> `0.8V` (V suffix borrowed)."""
    out = rewrite_params(DELAY_TB, {"sign": 0.8}, DELAY_WHITELIST)
    assert "+ SIGN = 0.8V\n" in out


# --------------------------------------------------------------------- #
#  Single-line .PARAM block (matching_test-style netlist)
# --------------------------------------------------------------------- #

def test_singleline_partial_rewrite() -> None:
    out = rewrite_params(
        MATCHING_NETLIST,
        {"num_finger_n0": 2, "num_finger_p1": 3},
        MATCHING_WHITELIST,
    )
    assert (
        ".PARAM num_finger_n0=2 num_finger_n1=1 num_finger_p0=1 num_finger_p1=3\n"
    ) in out


def test_singleline_int_renders_without_decimal() -> None:
    """Integer values must not gain a `.0` suffix in the rewritten file."""
    out = rewrite_params(
        MATCHING_NETLIST, {"num_finger_n0": 4}, MATCHING_WHITELIST,
    )
    assert "num_finger_n0=4 " in out
    assert "num_finger_n0=4.0" not in out


def test_instance_lines_with_param_references_untouched() -> None:
    """`xm0 ... nf=num_finger_n0` lines reference the param BY NAME and
    must not be touched -- they live outside the leading .PARAM block."""
    out = rewrite_params(
        MATCHING_NETLIST, {"num_finger_n0": 3}, MATCHING_WHITELIST,
    )
    assert "xm0 zn n0 vss vss myn nf=num_finger_n0\n" in out
    assert "xm1 n2 n1 zn vss myn nf=num_finger_n1\n" in out


# --------------------------------------------------------------------- #
#  Block-scope discipline: only the FIRST .PARAM block is rewritten.
# --------------------------------------------------------------------- #

def test_only_first_block_touched() -> None:
    """The .alter block carries its own .PARAM with `LSB2 = 0.8V`. After
    a rewrite the .alter block must be byte-identical."""
    out = rewrite_params(
        DELAY_TB, {"delay": "75p", "lsb2": "0.4V"}, DELAY_WHITELIST,
    )
    # First (leading) block was rewritten.
    assert ".PARAM delay = 75p\n" in out
    assert "+ LSB2 = 0.4V\n" in out
    # The .alter block's own LSB2=0.8V is preserved.
    alter_section = out.split(".alter -1\n", 1)[1]
    assert "+ LSB2 = 0.8V\n" in alter_section
    # And the .alter's `delay = 50p` was not changed to 75p.
    assert ".PARAM delay = 50p\n" in alter_section


def test_pwl_strings_with_param_references_untouched() -> None:
    """The H_IN PWL line references `'0.8-hinvoltage'` and `'hinvoltage'`
    inside quoted expressions. Rewriting `hinvoltage` must not touch
    those references -- they're outside the .PARAM block AND inside
    quoted expressions."""
    out = rewrite_params(DELAY_TB, {"hinvoltage": "0.8"}, DELAY_WHITELIST)
    assert "PWL (0 '0.8-hinvoltage' 7n '0.8-hinvoltage' 7.01n 'hinvoltage')" in out
    assert "+ hinvoltage = 0.8\n" in out


def test_measure_directives_untouched_even_with_kvpairs() -> None:
    """`.measure tran ... val=0.4v rise=1 targ ...` has KEY=VALUE pairs
    that the regex would otherwise match. They must survive a rewrite
    untouched because they live outside the leading .PARAM block."""
    out = rewrite_params(DELAY_TB, {"delay": "75p"}, DELAY_WHITELIST)
    measure_line = (
        ".measure tran h_tphl trig v(h_in_mid) val=0.4v rise=1 "
        "targ v(h_out_mid) val=0.4v fall=1\n"
    )
    assert measure_line in out


# --------------------------------------------------------------------- #
#  Whitelist / contract enforcement.
# --------------------------------------------------------------------- #

def test_whitelist_rejection_raises() -> None:
    with pytest.raises(ParamRewriteError) as exc:
        rewrite_params(DELAY_TB, {"intruder": "1"}, DELAY_WHITELIST)
    msg = str(exc.value)
    assert "intruder" in msg
    assert "whitelist" in msg.lower()


def test_case_insensitive_match() -> None:
    """LLM emits `DELAY` while the spec whitelist has `delay` and the
    .sp source has `.PARAM delay = 50p`. Should succeed and preserve
    the source's casing of the key."""
    out = rewrite_params(
        DELAY_TB, {"DELAY": "75p"}, [s.upper() for s in DELAY_WHITELIST],
    )
    assert ".PARAM delay = 75p\n" in out


def test_phantom_key_in_whitelist_but_absent_from_block_raises() -> None:
    """`hinvoltage` IS in the matching_test-style scenario's whitelist
    in some specs, but the netlist.sp's .PARAM block declares only the
    four num_finger keys. Proposing it must raise."""
    extended_wl = list(MATCHING_WHITELIST) + ["hinvoltage"]
    with pytest.raises(ParamRewriteError) as exc:
        rewrite_params(
            MATCHING_NETLIST, {"hinvoltage": "0.8"}, extended_wl,
        )
    assert "not declared" in str(exc.value)


def test_no_param_directive_raises() -> None:
    text_without_param = (
        "* netlist with no .PARAM\n"
        "V1 vdd 0 0.8V\n"
        ".END\n"
    )
    with pytest.raises(ParamRewriteError) as exc:
        rewrite_params(
            text_without_param, {"delay": "75p"}, DELAY_WHITELIST,
        )
    assert "no .PARAM" in str(exc.value)


def test_empty_new_params_is_noop() -> None:
    out = rewrite_params(DELAY_TB, {}, DELAY_WHITELIST)
    assert out == DELAY_TB


def test_whitespace_preservation_around_equals() -> None:
    """`delay = 50p` (with spaces) stays `delay = 75p`, not `delay=75p`."""
    out = rewrite_params(DELAY_TB, {"delay": "75p"}, DELAY_WHITELIST)
    assert ".PARAM delay = 75p\n" in out
    assert ".PARAM delay=75p" not in out


# --------------------------------------------------------------------- #
#  rewrite_param_file: atomic write semantics.
# --------------------------------------------------------------------- #

def test_rewrite_param_file_writes_and_cleans_tmp(tmp_path: Path) -> None:
    sp_file = tmp_path / "dut_tb.sp"
    sp_file.write_text(MATCHING_NETLIST, encoding="utf-8")
    changed = rewrite_param_file(
        sp_file, {"num_finger_n0": 2}, MATCHING_WHITELIST,
    )
    assert changed is True
    new_text = sp_file.read_text(encoding="utf-8")
    assert "num_finger_n0=2 " in new_text
    # No stray .tmp left behind on the happy path.
    assert not (tmp_path / "dut_tb.sp.tmp").exists()


def test_rewrite_param_file_noop_returns_false(tmp_path: Path) -> None:
    sp_file = tmp_path / "dut_tb.sp"
    sp_file.write_text(MATCHING_NETLIST, encoding="utf-8")
    mtime_before = sp_file.stat().st_mtime_ns
    changed = rewrite_param_file(sp_file, {}, MATCHING_WHITELIST)
    assert changed is False
    # File untouched -- mtime preserved.
    assert sp_file.stat().st_mtime_ns == mtime_before


def test_rewrite_param_file_whitelist_violation_does_not_corrupt(
    tmp_path: Path,
) -> None:
    sp_file = tmp_path / "dut_tb.sp"
    sp_file.write_text(MATCHING_NETLIST, encoding="utf-8")
    with pytest.raises(ParamRewriteError):
        rewrite_param_file(
            sp_file, {"intruder": "1"}, MATCHING_WHITELIST,
        )
    # Original file content preserved -- the validation runs before any
    # write, so nothing should hit disk.
    assert sp_file.read_text(encoding="utf-8") == MATCHING_NETLIST
    assert not (tmp_path / "dut_tb.sp.tmp").exists()
