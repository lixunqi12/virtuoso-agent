"""Unit tests for SafeBridge Maestro Outputs Setup writers.

Covers the four PDK-safe wrappers added in Task 7fd8d467:

* :meth:`SafeBridge.add_maestro_output`
* :meth:`SafeBridge.set_maestro_spec`
* :meth:`SafeBridge.set_maestro_analysis`
* :meth:`SafeBridge.create_netlist_for_corner`

All tests mock ``virtuoso_bridge.virtuoso.maestro.writer.*`` so no real
Cadence/Maestro round-trip happens. Synthetic foundry-shaped payloads are
built at runtime so the source file itself stays P0-clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.safe_bridge import SafeBridge  # noqa: E402


def _p0_token(*parts: str) -> str:
    return "".join(parts)


@pytest.fixture
def pdk_map_file(tmp_path):
    content = """\
generic_cell_name: "GENERIC_DEVICE"

valid_aliases:
  - NMOS
  - PMOS

model_info_keys:
  - toxe
  - u0

allowed_params:
  - w
  - l
  - nf
  - m
  - multi
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def bridge(mock_client, pdk_map_file, tmp_path):
    b = SafeBridge(
        mock_client, pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
    return b


@pytest.fixture
def unscoped_bridge(mock_client, pdk_map_file, tmp_path):
    return SafeBridge(
        mock_client, pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )


@pytest.fixture
def writer_mocks():
    """Patch the four writer functions; yields the four MagicMocks."""
    with (
        patch("src.safe_bridge._mae_writer.add_output") as m_add,
        patch("src.safe_bridge._mae_writer.set_spec") as m_spec,
        patch("src.safe_bridge._mae_writer.set_analysis") as m_an,
        patch("src.safe_bridge._mae_writer.create_netlist_for_corner") as m_corner,
    ):
        m_add.return_value = "ok-output"
        m_spec.return_value = "ok-spec"
        m_an.return_value = "ok-analysis"
        m_corner.return_value = "ok-corner"
        yield {
            "add_output": m_add,
            "set_spec": m_spec,
            "set_analysis": m_an,
            "create_netlist_for_corner": m_corner,
        }


# ---------------------------------------------------------------- #
#  add_maestro_output
# ---------------------------------------------------------------- #


class TestAddMaestroOutput:
    def test_happy_path_signal(self, bridge, writer_mocks):
        out = bridge.add_maestro_output(name="vout", signal_name="/Vout")
        assert out == "ok-output"
        writer_mocks["add_output"].assert_called_once()
        kwargs = writer_mocks["add_output"].call_args.kwargs
        # test defaults to scoped tb_cell
        assert writer_mocks["add_output"].call_args.args[2] == "MYTB"
        assert kwargs["signal_name"] == "/Vout"
        assert kwargs["expr"] == ""

    def test_happy_path_expr(self, bridge, writer_mocks):
        bridge.add_maestro_output(
            name="f_osc",
            expr="value(frequency(VT(/Vout)) 100n)",
        )
        kwargs = writer_mocks["add_output"].call_args.kwargs
        assert kwargs["expr"] == "value(frequency(VT(/Vout)) 100n)"
        assert kwargs["signal_name"] == ""

    def test_expr_output_type_alias_not_forwarded(self, bridge, writer_mocks):
        """Caller alias 'expr' must not become maeAddOutput ?outputType.

        In ADE Assembler, row kind is inferred from ?expr. ?outputType
        is EvalType ("point"), and passing "expr" can become a no-op.
        """
        bridge.add_maestro_output(
            name="f_osc",
            output_type="expr",
            expr="frequency(VT(/Vout))",
        )
        kwargs = writer_mocks["add_output"].call_args.kwargs
        assert kwargs["output_type"] == ""

    def test_signal_output_type_alias_not_forwarded(self, bridge, writer_mocks):
        bridge.add_maestro_output(
            name="vout",
            output_type="signal",
            signal_name="/Vout",
        )
        kwargs = writer_mocks["add_output"].call_args.kwargs
        assert kwargs["output_type"] == ""

    def test_hierarchical_signal_name_allowed(self, bridge, writer_mocks):
        bridge.add_maestro_output(name="id_m1", signal_name="/I0/M1/D")
        assert writer_mocks["add_output"].called

    def test_explicit_test_overrides_scope(self, bridge, writer_mocks):
        bridge.add_maestro_output(
            name="vout", signal_name="/Vout", test="otherlib:OTHERTB:1"
        )
        assert writer_mocks["add_output"].call_args.args[2] == "otherlib:OTHERTB:1"

    def test_requires_scope(self, unscoped_bridge, writer_mocks):
        with pytest.raises(RuntimeError, match="set_scope"):
            unscoped_bridge.add_maestro_output(name="x", signal_name="/V")
        assert not writer_mocks["add_output"].called

    def test_requires_tb_cell(self, mock_client, pdk_map_file, tmp_path,
                              writer_mocks):
        b = SafeBridge(mock_client, pdk_map_file, skill_dir=tmp_path / "no_skill")
        b.set_scope("mylib", "MYCELL")  # no tb_cell
        with pytest.raises(RuntimeError, match="tb_cell"):
            b.add_maestro_output(name="x", signal_name="/V")
        assert not writer_mocks["add_output"].called

    def test_rejects_bad_name(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="output name"):
            bridge.add_maestro_output(name="1bad", signal_name="/V")
        with pytest.raises(ValueError, match="output name"):
            bridge.add_maestro_output(name="bad name", signal_name="/V")
        with pytest.raises(ValueError, match="output name"):
            bridge.add_maestro_output(name="bad-name", signal_name="/V")
        assert not writer_mocks["add_output"].called

    def test_rejects_neither_signal_nor_expr(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="signal_name or expr"):
            bridge.add_maestro_output(name="vout")
        assert not writer_mocks["add_output"].called

    def test_rejects_both_signal_and_expr(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="not both"):
            bridge.add_maestro_output(
                name="vout", signal_name="/V", expr="value(VT(/V) 1n)"
            )

    def test_rejects_bad_signal_name(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="signal_name"):
            bridge.add_maestro_output(name="vout", signal_name="Vout")  # no leading /
        with pytest.raises(ValueError, match="signal_name"):
            bridge.add_maestro_output(name="vout", signal_name='/V"a')
        assert not writer_mocks["add_output"].called

    def test_rejects_bad_output_type(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="output_type"):
            bridge.add_maestro_output(
                name="vout", signal_name="/V", output_type="bogus",
            )

    def test_rejects_expr_with_quote(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="unsupported string literal"):
            bridge.add_maestro_output(name="x", expr='value("VT(/V)" 1n)')

    def test_rejects_expr_with_backslash(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(name="x", expr="value(\\VT(/V) 1n)")

    def test_rejects_expr_with_backtick(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(name="x", expr="value(`VT(/V) 1n)")

    def test_rejects_expr_with_skill_primitive(self, bridge, writer_mocks):
        # R1: allow-list now rejects any non-OCEAN function call, so
        # ``system(...)`` / ``load(...)`` bounce with the allow-list
        # error rather than the prior deny-list error.
        with pytest.raises(ValueError, match="disallowed function"):
            bridge.add_maestro_output(name="x", expr="system(rm -rf /)")
        with pytest.raises(ValueError, match="disallowed function"):
            bridge.add_maestro_output(
                name="x", expr="load(/proj/evil.il)"
            )

    def test_rejects_expr_with_foundry_token(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="foundry-shaped token"):
            token = _p0_token("n", "ch_x")
            bridge.add_maestro_output(
                name="x", expr=f"value(VT(/{token}/d) 1n)"
            )
        with pytest.raises(ValueError, match="foundry-shaped token"):
            token = _p0_token("ts", "mc_a")
            bridge.add_maestro_output(
                name="x", expr=f"value(VT(/{token}) 1n)"
            )

    def test_rejects_expr_with_control_char(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="control char"):
            bridge.add_maestro_output(name="x", expr="value(VT(/V)\n 1n)")

    def test_rejects_expr_with_nonascii(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="ASCII"):
            bridge.add_maestro_output(name="x", expr="value(VT(/Vé) 1n)")

    def test_rejects_long_expr(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="too long"):
            bridge.add_maestro_output(name="x", expr="a" * 2000)

    def test_rejects_bad_session(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="session"):
            bridge.add_maestro_output(
                name="x", signal_name="/V", session="bad session",
            )

    def test_rejects_bad_test_name(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="test name"):
            bridge.add_maestro_output(
                name="x", signal_name="/V", test="bad test",
            )

    def test_return_value_scrubbed(self, bridge, writer_mocks):
        token = _p0_token("n", "ch_xyz")
        writer_mocks["add_output"].return_value = (
            f"result for {token} at /home/u"
        )
        scrubbed = bridge.add_maestro_output(name="vout", signal_name="/V")
        assert _p0_token("n", "ch_") not in scrubbed
        assert "/home/" not in scrubbed


# ---------------------------------------------------------------- #
#  set_maestro_spec
# ---------------------------------------------------------------- #


class TestSetMaestroSpec:
    def test_happy_path_both_bounds(self, bridge, writer_mocks):
        bridge.set_maestro_spec(name="f_osc", lt="21G", gt="19G")
        kwargs = writer_mocks["set_spec"].call_args.kwargs
        assert kwargs["lt"] == "21G"
        assert kwargs["gt"] == "19G"

    def test_happy_path_lt_only(self, bridge, writer_mocks):
        bridge.set_maestro_spec(name="f_osc", lt="21G")
        kwargs = writer_mocks["set_spec"].call_args.kwargs
        assert kwargs["lt"] == "21G"
        assert kwargs["gt"] == ""

    def test_happy_path_numeric_value(self, bridge, writer_mocks):
        bridge.set_maestro_spec(name="vout", lt=1.2, gt=0.8)
        kwargs = writer_mocks["set_spec"].call_args.kwargs
        # numeric values are formatted by _format_param_value
        assert kwargs["lt"] == "1.2"
        assert kwargs["gt"] == "0.8"

    def test_rejects_neither_bound(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="at least one"):
            bridge.set_maestro_spec(name="f_osc")
        assert not writer_mocks["set_spec"].called

    def test_rejects_bad_name(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="output name"):
            bridge.set_maestro_spec(name="1bad", lt="1")

    def test_rejects_non_numeric_lt(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="Unsafe parameter value"):
            bridge.set_maestro_spec(name="vout", lt="abc")

    def test_rejects_injection_in_lt(self, bridge, writer_mocks):
        with pytest.raises(ValueError):
            bridge.set_maestro_spec(name="vout", lt='1") system("rm')

    def test_requires_scope(self, unscoped_bridge, writer_mocks):
        with pytest.raises(RuntimeError, match="set_scope"):
            unscoped_bridge.set_maestro_spec(name="vout", lt="1")


# ---------------------------------------------------------------- #
#  set_maestro_analysis
# ---------------------------------------------------------------- #


class TestSetMaestroAnalysis:
    def test_happy_path_tran(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            analysis="tran",
            options={"start": "0", "stop": "200n"},
        )
        args = writer_mocks["set_analysis"].call_args.args
        kwargs = writer_mocks["set_analysis"].call_args.kwargs
        # args: (client, test, analysis)
        assert args[1] == "MYTB"
        assert args[2] == "tran"
        assert kwargs["enable"] is True
        # The wrapper builds the SKILL alist string itself
        assert kwargs["options"] == '(("start" "0") ("stop" "200n"))'

    def test_disable(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(analysis="ac", enable=False)
        kwargs = writer_mocks["set_analysis"].call_args.kwargs
        assert kwargs["enable"] is False
        assert kwargs["options"] == ""

    def test_enum_kwarg_skipdc(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            analysis="tran", options={"skipdc": "yes", "stop": "1u"},
        )
        kwargs = writer_mocks["set_analysis"].call_args.kwargs
        assert '("skipdc" "yes")' in kwargs["options"]
        assert '("stop" "1u")' in kwargs["options"]

    def test_dc_op_numeric_readback_options(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            analysis="dc", options={"oppoint": "rawfile", "detail": "all"},
        )
        kwargs = writer_mocks["set_analysis"].call_args.kwargs
        assert '("oppoint" "rawfile")' in kwargs["options"]
        assert '("detail" "all")' in kwargs["options"]

    def test_rejects_bad_analysis(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="Analysis must be"):
            bridge.set_maestro_analysis(analysis="bogus")
        with pytest.raises(ValueError, match="Analysis must be"):
            bridge.set_maestro_analysis(analysis="TRAN")  # case-sensitive
        assert not writer_mocks["set_analysis"].called

    def test_rejects_bad_option_key(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="option key"):
            bridge.set_maestro_analysis(
                analysis="tran", options={"bad key": "1"}
            )
        with pytest.raises(ValueError, match="option key"):
            bridge.set_maestro_analysis(
                analysis="tran", options={"1invalid": "1"}
            )

    def test_rejects_bad_option_value(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="Unsafe parameter value"):
            bridge.set_maestro_analysis(
                analysis="tran", options={"stop": "200n; rm -rf"}
            )

    def test_rejects_enum_with_bad_value(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="allowed:"):
            bridge.set_maestro_analysis(
                analysis="tran", options={"skipdc": "maybe"}
            )
        with pytest.raises(ValueError, match="allowed:"):
            bridge.set_maestro_analysis(
                analysis="dc", options={"detail": "model"}
            )

    def test_rejects_non_bool_enable(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="enable must be bool"):
            bridge.set_maestro_analysis(analysis="tran", enable="yes")

    def test_options_none_yields_empty_string(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(analysis="tran")
        kwargs = writer_mocks["set_analysis"].call_args.kwargs
        assert kwargs["options"] == ""

    def test_requires_scope(self, unscoped_bridge, writer_mocks):
        with pytest.raises(RuntimeError, match="set_scope"):
            unscoped_bridge.set_maestro_analysis(analysis="tran")


# ---------------------------------------------------------------- #
#  create_netlist_for_corner
# ---------------------------------------------------------------- #


class TestCreateNetlistForCorner:
    def test_happy_path(self, bridge, writer_mocks):
        bridge.create_netlist_for_corner(
            corner="typ_25",
            output_dir="/proj/myteam/sim/corner_typ",
        )
        args = writer_mocks["create_netlist_for_corner"].call_args.args
        assert args[1] == "MYTB"
        assert args[2] == "typ_25"
        assert args[3] == "/proj/myteam/sim/corner_typ"

    def test_rejects_bad_corner_name(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="corner"):
            bridge.create_netlist_for_corner(
                corner="bad corner", output_dir="~/x"
            )
        with pytest.raises(ValueError, match="corner"):
            bridge.create_netlist_for_corner(
                corner='evil")', output_dir="~/x"
            )

    def test_rejects_bad_output_dir(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="output_dir"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir="~/path with space"
            )
        with pytest.raises(ValueError, match="output_dir"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir='~/"; rm -rf /'
            )

    def test_rejects_foundry_token_in_output_dir(self, bridge, writer_mocks):
        # R1 R2: must first pass tilde + char + traversal + forbidden-
        # prefix + allow-list-prefix gates before the foundry-leak gate
        # fires. /tmp/<foundry-token>/x clears all the earlier gates and
        # exercises the foundry check specifically.
        with pytest.raises(ValueError, match="foundry"):
            token = _p0_token("n", "ch_secret")
            bridge.create_netlist_for_corner(
                corner="typ", output_dir=f"/tmp/{token}/x"
            )

    def test_requires_scope(self, unscoped_bridge, writer_mocks):
        with pytest.raises(RuntimeError, match="set_scope"):
            unscoped_bridge.create_netlist_for_corner(
                corner="typ", output_dir="~/x"
            )


# ---------------------------------------------------------------- #
#  CLI smoke
# ---------------------------------------------------------------- #


class TestConfigureMaestroOutputsCLI:
    def test_dry_run_parses_yaml(self, tmp_path, capsys):
        recipe_path = tmp_path / "recipe.yaml"
        recipe_path.write_text(
            "analyses:\n"
            "  - name: tran\n"
            "    enable: true\n"
            "    options:\n"
            "      stop: '200n'\n"
            "outputs:\n"
            "  - name: vout\n"
            "    signal_name: '/Vout'\n"
            "    spec:\n"
            "      gt: '0.5'\n"
            "      lt: '1.5'\n"
            "corner_netlists:\n"
            "  - corner: typ_25\n"
            "    output_dir: '/proj/myteam/sim/corner_typ'\n",
            encoding="utf-8",
        )
        pdk_map = tmp_path / "pdk_map.yaml"
        pdk_map.write_text(
            'valid_aliases:\n  - NMOS\n  - PMOS\nallowed_params: [w, l]\n',
            encoding="utf-8",
        )

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "configure_maestro_outputs",
            PROJECT_ROOT / "scripts" / "configure_maestro_outputs.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib",
            "--cell", "MYCELL",
            "--tb-cell", "MYTB",
            "--yaml", str(recipe_path),
            "--pdk-map", str(pdk_map),
            "--dry-run",
        ]):
            rc = module.main()
        assert rc == 0


# ---------------------------------------------------------------- #
#  R1: expr allow-list (P0-1)
# ---------------------------------------------------------------- #


# Codex R1 explicitly enumerated these 11 SKILL primitives that the
# original deny-list missed. Each one MUST be rejected by the allow-list.
_CODEX_R1_PRIMITIVES = [
    "getq", "process", "lambda", "apply", "funcall", "defun",
    "procedure", "prog", "puts", "fileSeek", "dbWriteCellView",
]


class TestExprAllowList:
    """R1: ``_validate_maestro_expr`` is a function-call allow-list.

    Any identifier-immediately-followed-by-``(`` whose name is not in
    :data:`_MAESTRO_EXPR_ALLOWED_FUNCS` must be rejected, regardless of
    whether the original deny-list happened to list it.
    """

    @pytest.mark.parametrize("primitive", _CODEX_R1_PRIMITIVES)
    def test_rejects_codex_r1_primitive(
        self, bridge, writer_mocks, primitive
    ):
        with pytest.raises(ValueError, match="disallowed function"):
            bridge.add_maestro_output(
                name="x", expr=f"{primitive}(VT(/V) 1n)",
            )
        assert not writer_mocks["add_output"].called

    @pytest.mark.parametrize("expr", [
        "VT(/Vout)",
        "value(VT(/Vout) 100n)",
        "frequency(VT(/Vout))",
        "value(frequency(VT(/Vout)) 100n)",
        "average(VT(/V))",
        "rms(VT(/V))",
        "peakToPeak(VT(/V))",
        "riseTime(VT(/V) 0.1 0.9)",
        "phaseMargin(VF(/V))",
        "db20(mag(VF(/V)))",
        "max(abs(VT(/V)))",
        "plus(VT(/Va) VT(/Vb))",
        "list(VT(/V) 1n)",
    ])
    def test_accepts_ocean_expression(self, bridge, writer_mocks, expr):
        bridge.add_maestro_output(name="x", expr=expr)
        assert writer_mocks["add_output"].called

    def test_accepts_ac_vf_quoted_netrefs(self, bridge, writer_mocks):
        bridge.add_maestro_output(
            name="ugb",
            expr='gainBwProd(VF("/Vout_p") - VF("/Vout_n"))',
        )
        kwargs = writer_mocks["add_output"].call_args.kwargs
        assert kwargs["expr"] == (
            r'gainBwProd(VF(\"/Vout_p\") - VF(\"/Vout_n\"))'
        )
        assert kwargs["signal_name"] == ""

    def test_rejects_mixed_allowed_and_forbidden_calls(
        self, bridge, writer_mocks
    ):
        # First identifier is allowed, second is not — must still reject.
        with pytest.raises(ValueError, match="disallowed function"):
            bridge.add_maestro_output(
                name="x", expr="value(system(/V) 1n)"
            )

    def test_rejects_arbitrary_identifier_call(
        self, bridge, writer_mocks
    ):
        # An identifier that's plausibly typo'd OCEAN ("Value" with cap V)
        # must still bounce because the allow-list is case-sensitive.
        with pytest.raises(ValueError, match="disallowed function"):
            bridge.add_maestro_output(name="x", expr="Value(VT(/V) 1n)")

    def test_rejects_semicolon(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(
                name="x", expr="value(VT(/V) 1n); system(rm)"
            )

    def test_rejects_dollar_macro(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(name="x", expr="value(VT(/V) $1n)")

    def test_rejects_at_macro(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(name="x", expr="value(VT(/V) @1n)")


# ---------------------------------------------------------------- #
#  R3: SKILL reader-syntax bypass attack vectors
# ---------------------------------------------------------------- #


class TestSkillReaderSyntaxBypass:
    """R3 (codex_reviewer_v4 P0): the allow-list scan in
    ``_validate_maestro_expr`` only sees ``identifier(`` tokens, so a
    SKILL/Lisp reader-syntax form like ``|system|(...)`` or
    ``'(getq cv prop)`` would bypass it entirely. R3 plugs this by
    extending the forbidden-char blocklist with ``|`` and ``'`` (backtick
    was already blocked); these characters have no legitimate use in an
    OCEAN measure expression and removing them is a strictly safer
    posture.

    The five vectors below correspond to the codex_reviewer_v4 finding's
    enumerated attack surface (escaped-symbol reader, quote macro,
    multi-line pipe, embedded apostrophe inside an allow-listed call,
    and pipe-after-allow-listed-prefix).
    """

    def test_rejects_escaped_symbol_reader(self, bridge, writer_mocks):
        # ``|system|(rm -rf /)`` — the pipe-enclosed text becomes a
        # symbol that the SKILL evaluator dispatches to, completely
        # sidestepping the ``identifier(`` allow-list pattern.
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(
                name="x", expr="|system|(VT(/V) 1n)"
            )
        assert not writer_mocks["add_output"].called

    def test_rejects_quote_macro(self, bridge, writer_mocks):
        # ``'(getq cv prop)`` — the leading apostrophe is the SKILL/Lisp
        # quote reader, producing a literal form. The function-call
        # allow-list never even sees ``getq``.
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(
                name="x", expr="'(getq cv prop)"
            )
        assert not writer_mocks["add_output"].called

    def test_rejects_pipe_inside_otherwise_allowlisted_call(
        self, bridge, writer_mocks
    ):
        # An allow-listed prefix can't whitewash a pipe payload — the
        # blocklist runs before the allow-list scan.
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(
                name="x", expr="value(|system|(rm) 1n)"
            )
        assert not writer_mocks["add_output"].called

    def test_rejects_apostrophe_inside_allowlisted_call(
        self, bridge, writer_mocks
    ):
        # Even nested deep inside what looks like a valid ``value(...)``
        # form, an apostrophe must be rejected because it changes
        # SKILL reader semantics for the form that follows.
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(
                name="x", expr="value(VT(/V) '(load /tmp/x.il))"
            )
        assert not writer_mocks["add_output"].called

    def test_rejects_pipe_after_allowlisted_prefix(
        self, bridge, writer_mocks
    ):
        # Sequence pattern: a legitimate-looking allow-listed call
        # followed by a piped form. The blocklist must fail this
        # before the allow-list scan reports the call as valid.
        with pytest.raises(ValueError, match="forbidden character"):
            bridge.add_maestro_output(
                name="x", expr="VT(/V)|exec|(killall)"
            )
        assert not writer_mocks["add_output"].called


# ---------------------------------------------------------------- #
#  R1: output_dir traversal / forbidden-prefix / allow-list (P0-2)
# ---------------------------------------------------------------- #


class TestOutputDirHardening:
    """R1 R2: ``_validate_remote_output_dir`` requires an absolute POSIX
    path under one of :data:`_MAESTRO_REMOTE_OUTPUT_ROOTS`, rejects all
    tilde glyphs, rejects ``..`` traversal, and rejects system /
    Cadence / PDK install roots even before the allow-list runs."""

    @pytest.mark.parametrize("output_dir", [
        "../../etc/cadence_secret",
        "/proj/foo/../../etc",
        "foo/../bar",
        "/tmp/sub/../../root",
    ])
    def test_rejects_dot_dot_traversal(
        self, bridge, writer_mocks, output_dir
    ):
        # R3 P1: ``..`` makes the path non-canonical, so it may bounce
        # either at the new normalize-form check or at the explicit
        # ``..`` segment check (depending on whether the input is also
        # otherwise non-canonical). Either rejection reason is fine.
        with pytest.raises(
            ValueError,
            match=r"\.\.|forbidden|must start|not in normalized form",
        ):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir=output_dir
            )
        assert not writer_mocks["create_netlist_for_corner"].called

    @pytest.mark.parametrize("output_dir", [
        "/proc/self/environ",
        "/etc/passwd",
        "/root/.ssh",
        "/usr/local/cadence",
        "/cadence/IC23/install",
        "/pdk/foundry",
        "/dev/null",
        "/sys/class",
    ])
    def test_rejects_forbidden_absolute_prefix(
        self, bridge, writer_mocks, output_dir
    ):
        with pytest.raises(ValueError, match="forbidden|prefix"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir=output_dir
            )

    @pytest.mark.parametrize("output_dir", [
        "~",                         # bare tilde
        "~root/secret",              # other-user tilde
        "~/simulation/x",            # tilde at pos 0 — R2 forbids tilde entirely
        "/proj/foo/~root/x",         # tilde anywhere
        "./relative/path",           # relative
        "relative/path",             # bare relative
        "/data/scratch",             # absolute but not in allow-list
        "/var/log",                  # absolute but not in allow-list
    ])
    def test_rejects_outside_allow_list(
        self, bridge, writer_mocks, output_dir
    ):
        with pytest.raises(ValueError):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir=output_dir
            )

    @pytest.mark.parametrize("output_dir", [
        "/tmp/scratch",
        "/var/tmp/sim_run01",
        "/scratch/user/sim",
        "/proj/myteam/sim",
        "/project/userA/labroot/sim",        # cobi-style real-project path (anonymized)
        "/home/user/sim",
    ])
    def test_accepts_allow_list_prefixes(
        self, bridge, writer_mocks, output_dir
    ):
        bridge.create_netlist_for_corner(corner="typ", output_dir=output_dir)
        assert writer_mocks["create_netlist_for_corner"].called

    def test_env_override_extends_allow_list(
        self, bridge, writer_mocks, monkeypatch
    ):
        # Without the env, ``/lab/...`` is rejected.
        monkeypatch.delenv("VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS", raising=False)
        with pytest.raises(ValueError, match="must start"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir="/lab/scratch/run"
            )
        # With the env naming ``/lab/``, the same path is accepted.
        monkeypatch.setenv(
            "VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS", "/lab/,/work/"
        )
        bridge.create_netlist_for_corner(
            corner="typ", output_dir="/lab/scratch/run"
        )
        assert writer_mocks["create_netlist_for_corner"].called

    def test_env_override_rejects_malformed_entry(
        self, bridge, writer_mocks, monkeypatch
    ):
        # Entry missing trailing slash — must error, not silently widen.
        monkeypatch.setenv("VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS", "/lab")
        with pytest.raises(ValueError, match="bracketed by"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir="/tmp/x"
            )
        # Entry not starting with '/' — same rejection.
        monkeypatch.setenv("VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS", "lab/")
        with pytest.raises(ValueError, match="bracketed by"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir="/tmp/x"
            )

    @pytest.mark.parametrize("output_dir", [
        "/tmp//foo",                  # codex_reviewer_v4 P1 vector
        "/proj/myteam//sim",          # mid-path double-slash
        "//tmp/sim",                  # leading double-slash (UNC-like)
        "/tmp/sim//",                 # trailing double-slash
    ])
    def test_rejects_double_slash(
        self, bridge, writer_mocks, output_dir
    ):
        # R3 P1: ``//`` has no legitimate use in a remote POSIX dir and
        # is a known path-confusion vector; must fail loud even though
        # ``posixpath.normpath`` would have collapsed it.
        with pytest.raises(ValueError, match="empty path component|forbidden|must start"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir=output_dir
            )
        assert not writer_mocks["create_netlist_for_corner"].called

    @pytest.mark.parametrize("output_dir", [
        "/tmp/./sim",                 # redundant '.' segment
        "/proj/myteam/sim/.",         # trailing '.'
        "/tmp/sim/",                  # trailing slash
        "/tmp/sim/./run",             # interior './'
    ])
    def test_rejects_non_canonical(
        self, bridge, writer_mocks, output_dir
    ):
        # R3 P1: paths must be in canonical normalized form so that no
        # later string-prefix check is fooled by a syntactic variant.
        with pytest.raises(ValueError, match="not in normalized form"):
            bridge.create_netlist_for_corner(
                corner="typ", output_dir=output_dir
            )
        assert not writer_mocks["create_netlist_for_corner"].called


# ---------------------------------------------------------------- #
#  R1: set_maestro_analysis P2-2 defensive assert smoke
# ---------------------------------------------------------------- #


class TestAnalysisAlistDefensiveAssert:
    """R1 P2-2: the alist key/value f-string interpolation has a
    belt-and-suspenders ``assert`` for quote / backslash / control chars.

    Upstream regex validators already prevent these characters from
    reaching the assert; this test just pins the happy path so a
    regression that disabled the assert wouldn't go unnoticed.
    """

    def test_alist_assembly_does_not_inject_quotes(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            analysis="tran",
            options={"start": "0", "stop": "200n"},
        )
        opts = writer_mocks["set_analysis"].call_args.kwargs["options"]
        # The wrapper-built alist must be exactly this shape — any embedded
        # quote / backslash would corrupt the SKILL string literal.
        assert opts == '(("start" "0") ("stop" "200n"))'
        for ch in ('\n', '\r', '\t'):
            assert ch not in opts

    def test_alist_tripwire_survives_python_dash_O(self, bridge, writer_mocks):
        # R3 (codex_reviewer_v4 P2) + P3 nit: AST-based check that
        # ``set_maestro_analysis`` contains NO ``assert`` statements
        # anywhere in its body. ``python -O`` strips ``assert`` at
        # compile time, so any defensive tripwire MUST use
        # ``raise ValueError`` instead. The earlier substring-based
        # version was brittle (renaming the loop var ``forbidden``
        # would have silently passed the gate); walking the AST is
        # robust against rename / refactor / whitespace changes.
        import ast
        import inspect
        import textwrap
        import src.safe_bridge as sb_mod
        src = inspect.getsource(sb_mod.SafeBridge.set_maestro_analysis)
        # ``getsource`` keeps method indentation — dedent so ``ast``
        # parses it as a top-level function.
        tree = ast.parse(textwrap.dedent(src))
        asserts = [node for node in ast.walk(tree)
                   if isinstance(node, ast.Assert)]
        assert not asserts, (
            f"set_maestro_analysis contains {len(asserts)} assert "
            f"statement(s) at line offset(s) "
            f"{[n.lineno for n in asserts]}; convert to "
            "raise ValueError so python -O cannot strip them."
        )
        # And confirm the tripwire still exists at all — at least one
        # ``raise ValueError`` somewhere in the method body.
        raises = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Raise)
                  and isinstance(node.exc, ast.Call)
                  and isinstance(node.exc.func, ast.Name)
                  and node.exc.func.id == "ValueError"]
        assert raises, (
            "set_maestro_analysis no longer contains any "
            "raise ValueError — the forbidden-char tripwire is gone."
        )

    @pytest.mark.parametrize("bad_options", [
        [],                  # codex_reviewer_v4 P2 vector
        [("start", "0")],    # list of tuples (looks alist-shaped)
        "start=0",           # string
        42,                  # int
        ("start", "0"),      # tuple
    ])
    def test_options_must_be_dict_or_none(
        self, bridge, writer_mocks, bad_options
    ):
        # R3 P2: pre-fallback type-check. ``options=[]`` would previously
        # become ``opts = {}`` via the ``or {}`` short-circuit and
        # silently succeed; now it must TypeError.
        with pytest.raises(TypeError, match="options must be a dict or None"):
            bridge.set_maestro_analysis(
                analysis="tran", options=bad_options
            )
        assert not writer_mocks["set_analysis"].called

    def test_options_none_is_ok(self, bridge, writer_mocks):
        # Sanity check: ``None`` still means "no options" and the
        # writer is invoked with an empty alist string.
        bridge.set_maestro_analysis(analysis="tran", options=None)
        opts = writer_mocks["set_analysis"].call_args.kwargs["options"]
        assert opts == ""

    def test_options_empty_dict_is_ok(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(analysis="tran", options={})
        opts = writer_mocks["set_analysis"].call_args.kwargs["options"]
        assert opts == ""


# ---------------------------------------------------------------- #
#  R1: CLI strict type coercion (P0-5)
# ---------------------------------------------------------------- #


class TestCLITypeCoercion:
    """R1: CLI rejects ``"true"``/``"false"`` strings for ``enable``
    and reject non-string scalars for signal_name / output_type / expr.
    """

    @pytest.fixture
    def cli_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "configure_maestro_outputs",
            PROJECT_ROOT / "scripts" / "configure_maestro_outputs.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None, "yes"])
    def test_coerce_bool_rejects_non_bool(self, cli_module, value):
        with pytest.raises(ValueError, match="must be a YAML boolean"):
            cli_module._coerce_bool(value, field="x")

    @pytest.mark.parametrize("value", [True, False])
    def test_coerce_bool_accepts_real_bool(self, cli_module, value):
        assert cli_module._coerce_bool(value, field="x") is value

    @pytest.mark.parametrize("value", [42, 1.5, True, [1, 2], {"k": "v"}])
    def test_coerce_optional_str_rejects_non_string(self, cli_module, value):
        with pytest.raises(ValueError, match="must be a string or omitted"):
            cli_module._coerce_optional_str(value, field="x")

    def test_coerce_optional_str_accepts_none(self, cli_module):
        assert cli_module._coerce_optional_str(None, field="x") == ""

    def test_coerce_optional_str_accepts_string(self, cli_module):
        assert cli_module._coerce_optional_str("hello", field="x") == "hello"

    def test_coerce_optional_str_accepts_empty_string(self, cli_module):
        assert cli_module._coerce_optional_str("", field="x") == ""


# ---------------------------------------------------------------- #
#  R1: YAML hardening caps (P0-4)
# ---------------------------------------------------------------- #


class TestYamlCaps:
    """R1: ``_load_recipe`` enforces size, depth, list-length, alias,
    and symlink caps before any of the recipe content reaches
    SafeBridge.
    """

    @pytest.fixture
    def cli_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "configure_maestro_outputs",
            PROJECT_ROOT / "scripts" / "configure_maestro_outputs.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_oversized_file_rejected(self, cli_module, tmp_path):
        path = tmp_path / "huge.yaml"
        # File whose serialized size exceeds the file-byte cap.
        payload = (
            "test: \""
            + ("a" * (cli_module._YAML_MAX_FILE_BYTES + 1024))
            + "\"\n"
        )
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match="too large"):
            cli_module._load_recipe(path)

    def test_symlink_rejected(self, cli_module, tmp_path):
        target = tmp_path / "real.yaml"
        target.write_text("analyses: []\n", encoding="utf-8")
        link = tmp_path / "link.yaml"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks unavailable on this filesystem")
        with pytest.raises(ValueError, match="symlink"):
            cli_module._load_recipe(link)

    def test_yaml_anchor_rejected(self, cli_module, tmp_path):
        path = tmp_path / "alias.yaml"
        path.write_text(
            "defs: &big\n"
            "  - 1\n"
            "  - 2\n"
            "analyses: *big\n",
            encoding="utf-8",
        )
        with pytest.raises(yaml.YAMLError):
            cli_module._load_recipe(path)

    def test_excessive_list_length_rejected(self, cli_module, tmp_path):
        path = tmp_path / "biglist.yaml"
        n = cli_module._YAML_MAX_ITEMS_PER_LIST + 5
        # Build a list of n harmless name entries; the depth / length
        # walker must flag the overflow regardless of file size.
        items = "\n".join(f"  - name: a{i:04d}" for i in range(n))
        path.write_text(f"outputs:\n{items}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="max is"):
            cli_module._load_recipe(path)

    def test_excessive_nesting_rejected(self, cli_module, tmp_path):
        path = tmp_path / "deep.yaml"
        # Build a deeply nested mapping that bypasses the schema's list
        # rules but trips the depth walker. Indent-style YAML is easier
        # to keep syntactically valid than nested flow ``{...}`` mappings.
        depth = cli_module._YAML_MAX_DEPTH + 3
        lines: list[str] = []
        for i in range(depth):
            lines.append("  " * i + f"k{i}:")
        lines.append("  " * depth + "v: 1")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="depth"):
            cli_module._load_recipe(path)

    def test_oversized_scalar_rejected(self, cli_module, tmp_path):
        path = tmp_path / "longstr.yaml"
        n = cli_module._YAML_MAX_STR_LEN + 10
        path.write_text(f"test: \"{'a' * n}\"\n", encoding="utf-8")
        with pytest.raises(ValueError, match="too long"):
            cli_module._load_recipe(path)

    def test_happy_path_recipe_loads(self, cli_module, tmp_path):
        path = tmp_path / "ok.yaml"
        path.write_text(
            "analyses:\n"
            "  - name: tran\n"
            "    enable: true\n"
            "outputs:\n"
            "  - name: vout\n"
            "    signal_name: '/Vout'\n",
            encoding="utf-8",
        )
        recipe = cli_module._load_recipe(path)
        assert recipe["analyses"][0]["name"] == "tran"
        assert recipe["outputs"][0]["signal_name"] == "/Vout"


# ---------------------------------------------------------------- #
#  R1: --dry-run actually runs validators (P0-3)
# ---------------------------------------------------------------- #


class TestDryRunRunsValidators:
    """R1: ``--dry-run`` must construct a SafeBridge and run every
    validator that a real run would. The previous implementation
    short-circuited before SafeBridge was even built, so bad YAML
    silently dry-ran clean.
    """

    @pytest.fixture
    def cli_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "configure_maestro_outputs",
            PROJECT_ROOT / "scripts" / "configure_maestro_outputs.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _write_pdk_map(self, tmp_path):
        path = tmp_path / "pdk_map.yaml"
        path.write_text(
            "valid_aliases:\n  - NMOS\n  - PMOS\nallowed_params: [w, l]\n",
            encoding="utf-8",
        )
        return path

    def test_dry_run_rejects_bad_signal_name(self, cli_module, tmp_path):
        recipe = tmp_path / "bad.yaml"
        recipe.write_text(
            "outputs:\n"
            "  - name: vout\n"
            "    signal_name: 'Vout'\n",  # missing leading '/'
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run",
        ]):
            with pytest.raises(ValueError, match="signal_name"):
                cli_module.main()

    def test_dry_run_rejects_bad_output_dir(self, cli_module, tmp_path):
        recipe = tmp_path / "bad_corner.yaml"
        recipe.write_text(
            "corner_netlists:\n"
            "  - corner: typ\n"
            "    output_dir: '/etc/passwd'\n",
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run",
        ]):
            with pytest.raises(ValueError, match="forbidden|prefix"):
                cli_module.main()

    def test_dry_run_rejects_bad_expr(self, cli_module, tmp_path):
        recipe = tmp_path / "bad_expr.yaml"
        recipe.write_text(
            "outputs:\n"
            "  - name: hack\n"
            "    expr: 'system(rm -rf /)'\n",
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run",
        ]):
            with pytest.raises(ValueError, match="disallowed function"):
                cli_module.main()

    def test_dry_run_rejects_ac_output_using_tran_probe(
        self, cli_module, tmp_path
    ):
        recipe = tmp_path / "bad_ac_dc.yaml"
        recipe.write_text(
            "outputs:\n"
            "  - name: gain\n"
            "    analysis: ac\n"
            "    expr: 'rms(VT(/Vout))'\n",
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run",
        ]):
            with pytest.raises(ValueError, match="analysis='ac'.*VT"):
                cli_module.main()

    def test_dry_run_rejects_non_bool_enable(self, cli_module, tmp_path):
        recipe = tmp_path / "bad_enable.yaml"
        recipe.write_text(
            "analyses:\n"
            "  - name: tran\n"
            "    enable: 'true'\n",  # string, not bool
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run",
        ]):
            with pytest.raises(ValueError, match="YAML boolean"):
                cli_module.main()

    def test_dry_run_accepts_design_vars_presets_and_verify(
        self, cli_module, tmp_path
    ):
        recipe = tmp_path / "setup.yaml"
        recipe.write_text(
            "test: MYTB\n"
            "verify: true\n"
            "design_vars:\n"
            "  w: '1u'\n"
            "analyses:\n"
            "  - preset: dc_op\n"
            "  - preset: ac_log\n"
            "    start: '1'\n"
            "    stop: '100G'\n"
            "    points_per_dec: '100'\n"
            "outputs:\n"
            "  - name: vout\n"
            "    analysis: dc\n"
            "    signal_name: '/Vout'\n"
            "    spec:\n"
            "      gt: '0.5'\n"
            "delete_outputs:\n"
            "  - old_vout\n",
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run", "--verify",
        ]):
            assert cli_module.main() == 0

    def test_skip_design_vars_leaves_recipe_vars_unapplied(
        self, cli_module, tmp_path
    ):
        recipe = tmp_path / "setup.yaml"
        report = tmp_path / "report.json"
        recipe.write_text(
            "design_vars:\n"
            "  w: '1u'\n"
            "outputs:\n"
            "  - name: vout\n"
            "    signal_name: '/Vout'\n",
            encoding="utf-8",
        )
        pdk = self._write_pdk_map(tmp_path)
        with patch.object(sys, "argv", [
            "configure_maestro_outputs",
            "--lib", "mylib", "--cell", "MYCELL", "--tb-cell", "MYTB",
            "--yaml", str(recipe), "--pdk-map", str(pdk),
            "--dry-run", "--skip-design-vars",
            "--report-json", str(report),
        ]):
            assert cli_module.main() == 0

        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["applied"]["design_vars"] == 0
        assert data["writeback"] is None

    def test_analysis_preset_expands_to_ac_dec(self, cli_module):
        name, enable, options = cli_module._analysis_from_entry({
            "preset": "ac_log",
            "start": "1",
            "stop": "100G",
            "points_per_dec": "100",
        })
        assert name == "ac"
        assert enable is True
        assert options == {"start": "1", "stop": "100G", "dec": "100"}

    def test_dc_op_preset_requests_device_op_scalars(self, cli_module):
        name, enable, options = cli_module._analysis_from_entry({
            "preset": "dc_op",
        })
        assert name == "dc"
        assert enable is True
        assert options == {
            "oppoint": "rawfile",
            "detail": "all",
            "maxiters": "150",
            "maxsteps": "10000",
        }


class TestMaestroExprQuotedNetRefs:
    @pytest.mark.parametrize('expr', [
        'VT("/Vout_p")',
        '(VT("/Vp") - VT("/Vn"))',
        'clip(VT("/Vp") 1e-7 2e-7)',
        'peakToPeak(clip((VT("/Vout_p") - VT("/Vout_n")) 1.5e-07 2e-07))',
        'IT("/I0/M2/D")',
        'average(abs(clip(IT("/I0/M2/D") 1.5e-07 2e-07)))',
        'getData("/Vout_p")',
    ])
    def test_quoted_net_ref_tokens_are_allowed(
        self, bridge, writer_mocks, expr,
    ):
        bridge.add_maestro_output(name='x', expr=expr)
        kwargs = writer_mocks['add_output'].call_args.kwargs
        # T2.1: SafeBridge SKILL-escapes `"` → `\"` before handing expr
        # to virtuoso_bridge writer, which splices it into the outer
        # `?expr "..."` SKILL string literal.
        assert kwargs['expr'] == expr.replace('"', r'\"')
        assert kwargs['signal_name'] == ''

    @pytest.mark.parametrize('expr', [
        'VT("/Vp") + "evil"',
        'VT("hello world")',
        'VT("/Vp\\";rm -rf /")',
        'VT("../etc/passwd")',
        'system("/cmd")',
        'getq("/foo")',
        'VT("/Vp" "/Vn")',
        '"VT"("/Vp")',
        '|VT|("/Vp")',
        "VT('/Vp')",
        '"x"',
        'VT("/' + ('a' * 257) + '")',
    ])
    def test_non_net_ref_string_literals_still_rejected(
        self, bridge, writer_mocks, expr,
    ):
        with pytest.raises(ValueError):
            bridge.add_maestro_output(name='x', expr=expr)
        assert not writer_mocks['add_output'].called

    def test_quote_chars_skill_escaped_before_writer(
        self, bridge, writer_mocks,
    ):
        """Every `"` in expr must reach writer as `\\"` so the outer
        SKILL `?expr "..."` literal does not terminate early."""
        bridge.add_maestro_output(
            name='vdiff_pp',
            expr='peakToPeak(clip((VT("/Vp") - VT("/Vn")) 1e-7 2e-7))',
        )
        forwarded = writer_mocks['add_output'].call_args.kwargs['expr']
        assert '"' not in forwarded.replace(r'\"', '')
        assert forwarded.count(r'\"') == 4

    def test_quote_free_expr_passes_through_unchanged(
        self, bridge, writer_mocks,
    ):
        """Legacy quote-free exprs (no net-ref tokens) must not be
        touched by the escape — backwards-compat for any caller still
        using bareword signal refs the validator happens to accept."""
        expr = 'average(clip(IT(/I0/D) 1e-7 2e-7))'
        bridge.add_maestro_output(name='id_avg', expr=expr)
        forwarded = writer_mocks['add_output'].call_args.kwargs['expr']
        assert forwarded == expr

    def test_freq_cross_type_string_literal_allowed(
        self, bridge, writer_mocks,
    ):
        expr = '(freq((VT("/Vp") - VT("/Vn")) "rising") * 1e-9)'
        bridge.add_maestro_output(name='f_inst_GHz', expr=expr)
        forwarded = writer_mocks['add_output'].call_args.kwargs['expr']
        assert forwarded == expr.replace('"', r'\"')

    def test_arbitrary_string_literal_still_rejected(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError, match="unsupported string literal"):
            bridge.add_maestro_output(name='x', expr='freq(VT("/Vp") "evil")')
        assert not writer_mocks['add_output'].called


# ---------------------------------------------------------------- #
#  T3: out-bound DoS cap on cobi-returned strings
# ---------------------------------------------------------------- #


class TestRemoteOutputSizeCap:
    """The PC-side cap (`_cap_remote_output`) closes the out-bound DoS
    gap that the in-bound 128-char cap (`_RF_STRING_MAX_LEN`) does not
    cover: a misbehaving cobi returning hundreds of MB of SKILL output
    would otherwise flow unchecked into _scrub / log / LLM context.
    Cap rejects loudly so the caller can investigate the upstream
    cause rather than silently truncating away evidence of a bug."""

    def test_helper_passes_through_short_string(self):
        from src.safe_bridge import _cap_remote_output
        assert _cap_remote_output("ok", label="t") == "ok"

    def test_helper_passes_through_non_string(self):
        from src.safe_bridge import _cap_remote_output
        # dict / list / int / None — only string-shaped DoS is in scope
        assert _cap_remote_output({"a": 1}, label="t") == {"a": 1}
        assert _cap_remote_output([1, 2, 3], label="t") == [1, 2, 3]
        assert _cap_remote_output(None, label="t") is None
        assert _cap_remote_output(42, label="t") == 42

    def test_helper_passes_through_at_cap_boundary(self):
        from src.safe_bridge import (
            _REMOTE_OUTPUT_MAX_CHARS, _cap_remote_output,
        )
        # Exactly at cap → allowed.
        s = "x" * _REMOTE_OUTPUT_MAX_CHARS
        assert _cap_remote_output(s, label="t") is s

    def test_helper_rejects_over_cap_string(self):
        from src.safe_bridge import (
            _REMOTE_OUTPUT_MAX_CHARS, _cap_remote_output,
        )
        s = "x" * (_REMOTE_OUTPUT_MAX_CHARS + 1)
        with pytest.raises(ValueError, match="exceeds cap"):
            _cap_remote_output(s, label="probe_xyz")

    def test_helper_error_message_includes_label(self):
        from src.safe_bridge import (
            _REMOTE_OUTPUT_MAX_CHARS, _cap_remote_output,
        )
        s = "x" * (_REMOTE_OUTPUT_MAX_CHARS + 1)
        with pytest.raises(ValueError, match="my_call_site_label"):
            _cap_remote_output(s, label="my_call_site_label")

    def test_add_output_rejects_over_cap_writer_return(
        self, bridge, writer_mocks,
    ):
        """Sample hookup: confirm add_maestro_output wires through the
        cap (one of 6 _mae_writer.* sites + 3 direct getattr sites)."""
        from src.safe_bridge import _REMOTE_OUTPUT_MAX_CHARS
        writer_mocks['add_output'].return_value = (
            "x" * (_REMOTE_OUTPUT_MAX_CHARS + 1)
        )
        with pytest.raises(ValueError, match="add_maestro_output"):
            bridge.add_maestro_output(
                name='vp', expr='peakToPeak(VT("/Vp"))',
            )

    def test_set_analysis_rejects_over_cap_writer_return(
        self, bridge, writer_mocks,
    ):
        """Second sample hookup, covers set_analysis path."""
        from src.safe_bridge import _REMOTE_OUTPUT_MAX_CHARS
        writer_mocks['set_analysis'].return_value = (
            "x" * (_REMOTE_OUTPUT_MAX_CHARS + 1)
        )
        with pytest.raises(ValueError, match="set_maestro_analysis"):
            bridge.set_maestro_analysis(
                analysis="tran",
                options={"start": "0", "stop": "200n"},
            )

    def test_set_spec_rejects_over_cap_writer_return(
        self, bridge, writer_mocks,
    ):
        from src.safe_bridge import _REMOTE_OUTPUT_MAX_CHARS
        writer_mocks['set_spec'].return_value = (
            "x" * (_REMOTE_OUTPUT_MAX_CHARS + 1)
        )
        with pytest.raises(ValueError, match="set_maestro_spec"):
            bridge.set_maestro_spec(name='m', gt=0.5)

    def test_normal_writer_return_still_works(
        self, bridge, writer_mocks,
    ):
        """Sanity: under-cap writer returns must NOT be affected."""
        bridge.add_maestro_output(
            name='vp', expr='peakToPeak(VT("/Vp"))',
        )
        # No raise — writer return was the fixture default ('ok-output',
        # 9 chars), well under cap.
