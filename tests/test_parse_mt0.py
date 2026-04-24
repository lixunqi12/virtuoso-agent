"""Tests for src/parse_mt0.py."""

from __future__ import annotations

import pytest

from src.parse_mt0 import Mt0ParseError, Mt0Result, parse_mt0


# --- Fixtures --------------------------------------------------------------


# 13 rows, 7 columns (PARAM_COUNT=1; 4 measures + temper + alter# = 6; plus
# 1 param = 7). Columns and data rows wrap 4+3, matching HSpice's ~80-char
# physical-line width. The `alter#` value is 1 on every row (single-alter
# .mt0). Deliberately includes `0.0` without exponent to exercise the
# non-scientific-notation code path.
REAL_COBI_MT0_SAMPLE = """\
$DATA1 SOURCE='HSPICE' VERSION='Q-2020.03' PARAM_COUNT=1
.TITLE 'cobi_read_margin'
 vbl_sweep           read_margin         write_margin        hold_current
 leak_current        temper              alter#
 0.600000000e+00     1.234500000e-01     5.678900000e-02     1.100000000e-06
 0.0                 2.500000000e+01     1.000000000e+00
 0.620000000e+00     1.254500000e-01     5.778900000e-02     1.120000000e-06
 1.100000000e-09     2.500000000e+01     1.000000000e+00
 0.640000000e+00     1.274500000e-01     5.878900000e-02     1.140000000e-06
 2.100000000e-09     2.500000000e+01     1.000000000e+00
 0.660000000e+00     1.294500000e-01     5.978900000e-02     1.160000000e-06
 3.100000000e-09     2.500000000e+01     1.000000000e+00
 0.680000000e+00     1.314500000e-01     6.078900000e-02     1.180000000e-06
 4.100000000e-09     2.500000000e+01     1.000000000e+00
 0.700000000e+00     1.334500000e-01     6.178900000e-02     1.200000000e-06
 5.100000000e-09     2.500000000e+01     1.000000000e+00
 0.720000000e+00     1.354500000e-01     6.278900000e-02     1.220000000e-06
 6.100000000e-09     2.500000000e+01     1.000000000e+00
 0.740000000e+00     1.374500000e-01     6.378900000e-02     1.240000000e-06
 7.100000000e-09     2.500000000e+01     1.000000000e+00
 0.760000000e+00     1.394500000e-01     6.478900000e-02     1.260000000e-06
 8.100000000e-09     2.500000000e+01     1.000000000e+00
 0.780000000e+00     1.414500000e-01     6.578900000e-02     1.280000000e-06
 9.100000000e-09     2.500000000e+01     1.000000000e+00
 0.800000000e+00     1.434500000e-01     6.678900000e-02     1.300000000e-06
 1.010000000e-08     2.500000000e+01     1.000000000e+00
 0.820000000e+00     1.454500000e-01     6.778900000e-02     1.320000000e-06
 1.110000000e-08     2.500000000e+01     1.000000000e+00
 0.840000000e+00     1.474500000e-01     6.878900000e-02     1.340000000e-06
 1.210000000e-08     2.500000000e+01     1.000000000e+00
"""


# PARAM_COUNT=2 synthetic fixture: 2 params + 2 measures + temper + alter#
# = 6 columns, 3 rows, alter# = 3 (simulating a .mt2 file).
SYNTH_PARAM2_MT0 = """\
$DATA1 SOURCE='HSPICE' VERSION='Z-2025.01' PARAM_COUNT=2
.TITLE 'two_param_sweep'
 vdd_sweep temp_sweep  delay_rise  delay_fall  temper  alter#
 0.9       25.0        1.5e-10     1.7e-10     25.0    3.0
 1.0       25.0        1.3e-10     1.5e-10     25.0    3.0
 1.1       25.0        1.1e-10     1.3e-10     25.0    3.0
"""


# Minimal happy-path fixture for edge-case tests.
MINIMAL_MT0 = """\
$DATA1 SOURCE='HSPICE' VERSION='V-0.0' PARAM_COUNT=0
.TITLE 'minimal'
 delay  temper  alter#
 1.0e-9 25.0    1.0
"""


# --- Happy-path tests ------------------------------------------------------


class TestRealCobiSample:
    def test_parses_without_error(self):
        assert isinstance(parse_mt0(REAL_COBI_MT0_SAMPLE), Mt0Result)

    def test_header_fields(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert r.header["source"] == "HSPICE"
        assert r.header["version"] == "Q-2020.03"
        assert r.header["param_count"] == "1"

    def test_title(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert r.title == "cobi_read_margin"

    def test_column_names_and_count(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert list(r.columns) == [
            "vbl_sweep",
            "read_margin",
            "write_margin",
            "hold_current",
            "leak_current",
            "temper",
            "alter#",
        ]
        assert len(r.columns) == 7

    def test_row_count_and_shape(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert len(r.rows) == 13
        for row in r.rows:
            assert len(row) == 7

    def test_first_row_values(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        row0 = r.rows[0]
        assert row0[0] == pytest.approx(0.6)
        assert row0[1] == pytest.approx(0.12345)
        assert row0[2] == pytest.approx(0.056789)
        assert row0[3] == pytest.approx(1.1e-6)
        assert row0[4] == 0.0
        assert row0[5] == pytest.approx(25.0)
        assert row0[6] == pytest.approx(1.0)

    def test_alter_number_extracted(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert r.alter_number == 1

    def test_param_count_property(self):
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert r.param_count == 1

    def test_measure_count_derivation(self):
        # 7 cols - 1 param - 2 (temper + alter#) = 4
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert r.measure_count == 4

    def test_zero_without_exponent_parses(self):
        # row 0 has `0.0` in the leak_current column — must parse as float
        r = parse_mt0(REAL_COBI_MT0_SAMPLE)
        assert r.rows[0][4] == 0.0


# --- PARAM_COUNT=2 synthetic ----------------------------------------------


class TestParamCount2:
    def test_param_count_two(self):
        r = parse_mt0(SYNTH_PARAM2_MT0)
        assert r.param_count == 2

    def test_measure_count_two(self):
        # 6 cols - 2 params - 2 (temper + alter#) = 2 measures
        r = parse_mt0(SYNTH_PARAM2_MT0)
        assert r.measure_count == 2

    def test_alter_number_three(self):
        r = parse_mt0(SYNTH_PARAM2_MT0)
        assert r.alter_number == 3

    def test_rows_parse(self):
        r = parse_mt0(SYNTH_PARAM2_MT0)
        assert len(r.rows) == 3
        assert r.rows[0][0] == pytest.approx(0.9)
        assert r.rows[-1][0] == pytest.approx(1.1)


# --- Minimal / edge-case shape --------------------------------------------


class TestMinimal:
    def test_minimal_happy_path(self):
        r = parse_mt0(MINIMAL_MT0)
        assert r.title == "minimal"
        assert r.header.get("param_count") == "0"
        assert r.columns == ("delay", "temper", "alter#")
        assert len(r.rows) == 1
        assert r.alter_number == 1

    def test_measure_count_minimal(self):
        # 3 cols - 0 params - 2 = 1 measure
        r = parse_mt0(MINIMAL_MT0)
        assert r.measure_count == 1

    def test_result_is_frozen(self):
        r = parse_mt0(MINIMAL_MT0)
        with pytest.raises((AttributeError, Exception)):
            r.title = "mutated"  # type: ignore[misc]


# --- Whitespace / wrap variants -------------------------------------------


class TestWhitespaceAndWrap:
    def test_handles_tabs_and_extra_spaces(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'ws_test'\n"
            "\t delay\t\ttemper\talter#\n"
            "  1.0e-9    \t25.0   1.0\n"
        )
        r = parse_mt0(payload)
        assert r.columns == ("delay", "temper", "alter#")
        assert r.rows[0][0] == pytest.approx(1.0e-9)

    def test_blank_lines_in_body_are_skipped(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'blanks'\n"
            "\n"
            " delay temper alter#\n"
            "\n"
            " 1.0e-9 25.0 1.0\n"
            "\n"
            " 2.0e-9 25.0 1.0\n"
        )
        r = parse_mt0(payload)
        assert len(r.rows) == 2
        assert r.rows[1][0] == pytest.approx(2.0e-9)

    def test_extreme_wrap_every_token_own_line(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'wrap'\n"
            "delay\n"
            "temper\n"
            "alter#\n"
            "1.0e-9\n"
            "25.0\n"
            "1.0\n"
        )
        r = parse_mt0(payload)
        assert r.columns == ("delay", "temper", "alter#")
        assert r.rows == ((1.0e-9, 25.0, 1.0),)


# --- Error paths ----------------------------------------------------------


class TestErrors:
    def test_empty_string(self):
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0("")
        assert "empty" in str(exc.value).lower()

    def test_none_input(self):
        with pytest.raises(Mt0ParseError):
            parse_mt0(None)  # type: ignore[arg-type]

    def test_missing_data1_header(self):
        payload = (
            "# not a data1 header\n"
            ".TITLE 'x'\n"
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert "$DATA1" in str(exc.value) or "malformed" in str(exc.value)
        assert exc.value.line_no == 1

    def test_missing_title(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert ".TITLE" in str(exc.value) or "title" in str(exc.value).lower()

    def test_uneven_data_token_count(self):
        # 3 columns but 5 data tokens (5 % 3 != 0)
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'bad'\n"
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0 2.0e-9 25.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert "multiple" in str(exc.value).lower() or "column" in str(exc.value).lower()

    def test_non_float_in_data_region(self):
        # Boundary identifies first float token as data start. If a
        # non-float is interleaved after a float (e.g. typo'd garbage
        # mid-row), the reshape catches the shape mismatch or the
        # float() raises during row construction.
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'bad'\n"
            "delay temper alter#\n"
            "1.0e-9 NOT_A_NUMBER 1.0\n"
        )
        with pytest.raises(Mt0ParseError):
            parse_mt0(payload)

    def test_no_data_rows(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'no_rows'\n"
            "delay temper alter#\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert "numeric" in str(exc.value).lower() or "row" in str(exc.value).lower()

    def test_last_column_not_alter(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'wrong_last_col'\n"
            "delay temper somethingelse\n"
            "1.0e-9 25.0 1.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert "alter" in str(exc.value).lower()

    def test_inconsistent_alter_across_rows(self):
        # One .mt0 file = one alter; any divergence is an error.
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'alter_mismatch'\n"
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0\n"
            "2.0e-9 25.0 2.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert "alter" in str(exc.value).lower()

    def test_mt0_parse_error_carries_line_no_and_snippet(self):
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0("garbage first line\n.TITLE 'x'\na temper alter#\n1.0 25.0 1.0\n")
        assert exc.value.line_no == 1
        assert exc.value.snippet is not None
        assert "garbage" in exc.value.snippet
        # Snippet must not leak into the str-formatted message — upstream
        # may not have scrubbed the payload, and .mt0 bodies can carry
        # absolute paths in .TITLE lines.
        assert "garbage" not in str(exc.value)

    def test_mt0_parse_error_is_exception_subclass(self):
        assert issubclass(Mt0ParseError, Exception)
        err = Mt0ParseError("bad", line_no=3, snippet="foo bar")
        assert err.line_no == 3
        assert err.snippet == "foo bar"
        assert err.category == "bad"
        assert "bad" in str(err)
        # Privacy posture: snippet is retained on the attr but never
        # echoed in __str__ (mirrors hspice_scrub.ScrubError).
        assert "foo bar" not in str(err)


class TestR2Blockers:
    """Round-2 codex-reviewer blockers for parse_mt0."""

    def test_r1_title_snippet_does_not_leak_absolute_path(self):
        # Realistic .mt0 accident: a malformed .TITLE whose value
        # contains an absolute path. The parser must NOT echo that
        # path into `str(err)`; it belongs on `err.snippet` only.
        leaky_path = "/usr/local/dkits/foundry_x/cobi/top_tt.lib"
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            f".TITLE '{leaky_path}\n"  # missing closing quote
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert exc.value.snippet is not None
        assert leaky_path in exc.value.snippet
        # But nothing from that path should appear in the message.
        assert leaky_path not in str(exc.value)
        assert "/usr/local" not in str(exc.value)
        assert "foundry_x" not in str(exc.value)

    def test_r2_non_integer_alter_in_later_row_raises(self):
        # Row 1 alter=1.0 (valid); row 2 alter=1.5 (invalid). Before
        # the fix, `int(row[-1]) != alter_number` would compare
        # int(1.5)=1 == 1 and silently accept the malformed file.
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'bad_alter'\n"
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0\n"
            "2.0e-9 25.0 1.5\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert exc.value.category.startswith("alter#") or "alter" in exc.value.category.lower()
        assert "integer" in exc.value.category.lower()

    def test_r3_param_count_exceeds_column_count_raises(self):
        # 3 columns (delay, temper, alter#); PARAM_COUNT=10 would
        # imply measure_count = 3 - 10 - 2 = -9, nonsense. Must raise
        # rather than silently return a negative measure_count.
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=10\n"
            ".TITLE 'bad_pc'\n"
            "delay temper alter#\n"
            "1.0e-9 25.0 1.0\n"
        )
        with pytest.raises(Mt0ParseError) as exc:
            parse_mt0(payload)
        assert "PARAM_COUNT" in exc.value.category

    def test_r3_param_count_at_upper_bound_accepted(self):
        # Boundary condition: PARAM_COUNT = n_cols - 2 (every
        # non-reserved column is a param, measure_count = 0) must
        # succeed.
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=1\n"
            ".TITLE 'edge_pc'\n"
            "param_a temper alter#\n"
            "0.5 25.0 1.0\n"
        )
        r = parse_mt0(payload)
        assert r.param_count == 1
        assert r.measure_count == 0

    def test_r4_column_named_inf_does_not_flip_boundary(self):
        # A column literally named `inf` would be accepted by plain
        # `float()` as infinity and misclassified as the start of the
        # data region. The strict HSpice-numeric regex rejects it so
        # column/data split remains correct.
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'inf_col'\n"
            "inf other temper alter#\n"
            "1.0e-9 2.0e-9 25.0 1.0\n"
        )
        r = parse_mt0(payload)
        assert list(r.columns) == ["inf", "other", "temper", "alter#"]
        assert len(r.rows) == 1
        assert r.rows[0][0] == pytest.approx(1.0e-9)

    def test_r4_column_named_nan_does_not_flip_boundary(self):
        payload = (
            "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
            ".TITLE 'nan_col'\n"
            "nan temper alter#\n"
            "3.3 25.0 1.0\n"
        )
        r = parse_mt0(payload)
        assert list(r.columns) == ["nan", "temper", "alter#"]
        assert r.rows[0][0] == pytest.approx(3.3)
