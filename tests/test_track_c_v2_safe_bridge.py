"""Unit tests for Track C v2 SafeBridge wrappers.

Covers:
  * :meth:`SafeBridge.create_maestro_test` — simulator allow-list,
    same-name ValueError, injection-vector rejection
  * :meth:`SafeBridge.setup_maestro_corner` — model_file path
    validation, variables key/value validation
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
model_info_keys:
  - toxe
allowed_params:
  - w
  - l
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _make_client_mock(remote_test_names: tuple[str, ...] = ()) -> MagicMock:
    """Build a ``VirtuosoClient`` MagicMock whose ``execute_skill``
    returns a ``maeGetSetup``-shaped output the bridge can parse.

    R2 P1-1: ``create_maestro_test`` now consults
    ``_list_remote_maestro_tests`` before each create, so every test
    that exercises the happy path needs the SKILL probe to return an
    empty (or specified) test-name set.
    """
    client = MagicMock()
    raw = " ".join(f'"{n}"' for n in remote_test_names)
    client.execute_skill.return_value = MagicMock(
        output=raw, errors=None,
    )
    return client


@pytest.fixture
def bridge(pdk_map_file, tmp_path):
    b = SafeBridge(
        _make_client_mock(), pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
    return b


@pytest.fixture
def writer_mocks():
    with (
        patch("src.safe_bridge._mae_writer.create_test") as m_create,
        patch("src.safe_bridge._mae_writer.setup_corner") as m_corner,
    ):
        m_create.return_value = "ok-test"
        m_corner.return_value = "ok-corner"
        yield {"create_test": m_create, "setup_corner": m_corner}


# --------------------------------------------------------------------- #
#  create_maestro_test
# --------------------------------------------------------------------- #


class TestCreateMaestroTest:
    """Happy path + simulator allow-list + same-name dedup."""

    def test_happy_path_dispatches_to_writer(self, bridge, writer_mocks):
        bridge.create_maestro_test(
            "AC_OPENLOOP", lib="mylib", cell="MYCELL",
            view="schematic", simulator="spectre",
        )
        m = writer_mocks["create_test"]
        assert m.called
        kwargs = m.call_args.kwargs
        assert kwargs["lib"] == "mylib"
        assert kwargs["cell"] == "MYCELL"
        assert kwargs["view"] == "schematic"
        assert kwargs["simulator"] == "spectre"

    @pytest.mark.parametrize("sim", [
        "spectre", "spectreVerilog", "hspice", "auCdl",
    ])
    def test_each_allowed_simulator_passes(self, bridge, writer_mocks, sim):
        bridge.create_maestro_test(
            f"T_{sim}", lib="mylib", cell="MYCELL", simulator=sim,
        )
        assert writer_mocks["create_test"].called

    @pytest.mark.parametrize("bad_sim", [
        "ngspice",      # legitimate simulator but not whitelisted
        "eldo",         # legitimate but not whitelisted
        "spectre rf",   # space injection
        "spectre;rm",   # shell injection
        '"spectre"',    # quote injection
        "",             # empty string — clearly invalid
    ])
    def test_simulator_outside_whitelist_raises(self, bridge, writer_mocks, bad_sim):
        with pytest.raises(ValueError, match=r"Simulator must be one of"):
            bridge.create_maestro_test(
                "T", lib="mylib", cell="MYCELL", simulator=bad_sim,
            )
        assert not writer_mocks["create_test"].called

    def test_same_name_second_call_raises(self, bridge, writer_mocks):
        bridge.create_maestro_test(
            "TB_A", lib="mylib", cell="MYCELL", simulator="spectre",
        )
        with pytest.raises(ValueError, match=r"already created"):
            bridge.create_maestro_test(
                "TB_A", lib="mylib", cell="MYCELL", simulator="spectre",
            )
        # Only the first call reached the writer.
        assert writer_mocks["create_test"].call_count == 1

    def test_different_name_second_call_succeeds(self, bridge, writer_mocks):
        bridge.create_maestro_test(
            "TB_A", lib="mylib", cell="MYCELL", simulator="spectre",
        )
        bridge.create_maestro_test(
            "TB_B", lib="mylib", cell="MYCELL", simulator="spectre",
        )
        assert writer_mocks["create_test"].call_count == 2

    def test_writer_exception_does_not_record_name(self, bridge, writer_mocks):
        # If create_test raises, the bridge should NOT record the name
        # in the dedup set — otherwise a transient failure would
        # permanently lock that name out.
        writer_mocks["create_test"].side_effect = RuntimeError("simulated")
        with pytest.raises(RuntimeError):
            bridge.create_maestro_test(
                "TB_X", lib="mylib", cell="MYCELL", simulator="spectre",
            )
        # Now a retry with the same name should work — failure was transient.
        writer_mocks["create_test"].side_effect = None
        writer_mocks["create_test"].return_value = "ok"
        bridge.create_maestro_test(
            "TB_X", lib="mylib", cell="MYCELL", simulator="spectre",
        )

    @pytest.mark.parametrize("bad_test_name", [
        'TB"name',        # quote
        "TB name",        # whitespace
        "TB;evil",        # semicolon
        "TB(){injected}", # parens/braces
    ])
    def test_malformed_test_name_raises(self, bridge, writer_mocks, bad_test_name):
        with pytest.raises(ValueError):
            bridge.create_maestro_test(
                bad_test_name, lib="mylib", cell="MYCELL",
                simulator="spectre",
            )
        assert not writer_mocks["create_test"].called

    def test_unscoped_bridge_raises(self, pdk_map_file, tmp_path, writer_mocks):
        b = SafeBridge(
            MagicMock(), pdk_map_file,
            skill_dir=tmp_path / "no_skill",
        )
        with pytest.raises(RuntimeError):
            b.create_maestro_test(
                "T", lib="mylib", cell="MYCELL", simulator="spectre",
            )

    def test_missing_test_name_raises(self, bridge, writer_mocks):
        with pytest.raises(ValueError):
            bridge.create_maestro_test(
                None, lib="mylib", cell="MYCELL",  # type: ignore[arg-type]
                simulator="spectre",
            )


# --------------------------------------------------------------------- #
#  setup_maestro_corner
# --------------------------------------------------------------------- #


class TestSetupMaestroCorner:

    def test_happy_path_just_name(self, bridge, writer_mocks):
        bridge.setup_maestro_corner("tt_25")
        assert writer_mocks["setup_corner"].called
        kwargs = writer_mocks["setup_corner"].call_args.kwargs
        assert kwargs["model_file"] == ""
        assert kwargs["model_section"] == ""
        assert kwargs["variables"] is None

    def test_with_variables(self, bridge, writer_mocks):
        bridge.setup_maestro_corner(
            "ff_85",
            variables={"temperature": 85, "vdd": "1.32"},
        )
        kwargs = writer_mocks["setup_corner"].call_args.kwargs
        assert "temperature" in kwargs["variables"]
        assert "vdd" in kwargs["variables"]

    @pytest.mark.parametrize("bad_path", [
        "/etc/passwd",                 # forbidden system root
        "/proj/x/../../etc/secret",    # traversal
        "../relative",                 # not absolute
        "/tmp/" + "a" * 1100,          # length cap
        '/tmp/foo"bar',                # forbidden char
    ])
    def test_bad_model_file_raises(self, bridge, writer_mocks, bad_path):
        with pytest.raises(ValueError):
            bridge.setup_maestro_corner("tt", model_file=bad_path)
        assert not writer_mocks["setup_corner"].called

    @pytest.mark.parametrize("bad_var", [
        ("temp ",        25),     # space in key
        ('te"mp',        25),     # quote in key
        ("temperature",  "85;rm"),# value with forbidden after format
        ("temperature",  '85"'),  # value with quote
    ])
    def test_bad_variable_raises(self, bridge, writer_mocks, bad_var):
        key, value = bad_var
        with pytest.raises(ValueError):
            bridge.setup_maestro_corner("tt", variables={key: value})
        assert not writer_mocks["setup_corner"].called

    def test_variables_wrong_type_raises(self, bridge, writer_mocks):
        with pytest.raises(TypeError):
            bridge.setup_maestro_corner(
                "tt", variables=[("temperature", 25)],  # type: ignore[arg-type]
            )
        assert not writer_mocks["setup_corner"].called

    def test_bad_corner_name_raises(self, bridge, writer_mocks):
        with pytest.raises(ValueError):
            bridge.setup_maestro_corner('tt"weird')
        assert not writer_mocks["setup_corner"].called

    def test_bad_model_section_raises(self, bridge, writer_mocks):
        with pytest.raises(ValueError):
            bridge.setup_maestro_corner(
                "tt", model_file="/tmp/m.scs", model_section='tt"x',
            )
        assert not writer_mocks["setup_corner"].called


# --------------------------------------------------------------------- #
#  R2 P1-1: remote-authoritative dedup for create_maestro_test
# --------------------------------------------------------------------- #


class TestRemoteDedupCreateMaestroTest:
    """The PC-side ``_created_maestro_tests`` set only catches the
    same-bridge case. R2 adds a ``_list_remote_maestro_tests`` SKILL
    probe that runs on every create — this class pins that behavior."""

    def test_remote_probe_returns_existing_name_raises(
        self, pdk_map_file, tmp_path, writer_mocks,
    ):
        # The remote already has "TB_A" (e.g. user authored it
        # interactively before the bridge attached). create_maestro_test
        # must refuse to overwrite.
        client = _make_client_mock(remote_test_names=("TB_A",))
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        with pytest.raises(ValueError, match=r"already exists on remote"):
            b.create_maestro_test(
                "TB_A", lib="mylib", cell="MYCELL", simulator="spectre",
            )
        assert not writer_mocks["create_test"].called

    def test_remote_probe_empty_allows_create(
        self, pdk_map_file, tmp_path, writer_mocks,
    ):
        # Clean session — no remote tests, no PC-side ones.
        client = _make_client_mock(remote_test_names=())
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        b.create_maestro_test(
            "TB_FRESH", lib="mylib", cell="MYCELL", simulator="spectre",
        )
        assert writer_mocks["create_test"].called

    def test_remote_probe_skill_error_propagates(
        self, pdk_map_file, tmp_path, writer_mocks,
    ):
        # The SKILL probe itself errored — propagate so the LLM /
        # caller sees the failure rather than silently overwriting.
        client = MagicMock()
        client.execute_skill.return_value = MagicMock(
            output="", errors=["SKILL_TRANSPORT_DOWN"],
        )
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        with pytest.raises(RuntimeError,
                           match=r"_list_remote_maestro_tests"):
            b.create_maestro_test(
                "TB_X", lib="mylib", cell="MYCELL", simulator="spectre",
            )
        assert not writer_mocks["create_test"].called

    def test_cross_bridge_dedup(self, pdk_map_file, tmp_path, writer_mocks):
        # Bridge #1 creates TB_A locally. Bridge #2 (separate instance,
        # so PC-side set is empty) is told via the remote probe that
        # TB_A exists — must refuse.
        client1 = _make_client_mock(remote_test_names=())
        b1 = SafeBridge(client1, pdk_map_file, skill_dir=tmp_path / "ns1")
        b1.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        b1.create_maestro_test(
            "TB_A", lib="mylib", cell="MYCELL", simulator="spectre",
        )
        assert writer_mocks["create_test"].call_count == 1

        # Now b2's view of the remote includes TB_A.
        client2 = _make_client_mock(remote_test_names=("TB_A",))
        b2 = SafeBridge(client2, pdk_map_file, skill_dir=tmp_path / "ns2")
        b2.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        with pytest.raises(ValueError, match=r"already exists on remote"):
            b2.create_maestro_test(
                "TB_A", lib="mylib", cell="MYCELL", simulator="spectre",
            )
        # Still only the first call reached the writer.
        assert writer_mocks["create_test"].call_count == 1

    def test_remote_probe_ignores_foundry_leak(
        self, pdk_map_file, tmp_path, writer_mocks,
    ):
        # Defense-in-depth: if maeGetSetup somehow returns a token
        # that doesn't match the strict ``_MAESTRO_TEST_NAME_RE``
        # whitelist (e.g. a foundry name accidentally leaked into the
        # output blob), the parser drops it silently rather than using
        # it as a dedup gate against a legit LLM-proposed name.
        client = MagicMock()
        client.execute_skill.return_value = MagicMock(
            output='"my$bad name" "TB_LEGIT"', errors=None,
        )
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        # "TB_LEGIT" is matched and should block.
        with pytest.raises(ValueError, match=r"already exists on remote"):
            b.create_maestro_test(
                "TB_LEGIT", lib="mylib", cell="MYCELL", simulator="spectre",
            )
        # "TB_NEW" is safe — the malformed leak does NOT block it.
        b.create_maestro_test(
            "TB_NEW", lib="mylib", cell="MYCELL", simulator="spectre",
        )
        assert writer_mocks["create_test"].called

    @pytest.mark.parametrize("leak_token", [
        _p0_token("n", "ch_alpha"),
        _p0_token("p", "ch_secret"),
        _p0_token("ts", "mc_18nm"),
        _p0_token("cf", "mom_cap"),
        _p0_token("rp", "poly_high_ohm"),
        _p0_token("rm", "1_2um"),
        _p0_token("tc", "bn_lib_cell"),
    ])
    def test_remote_probe_drops_foundry_prefixed_tokens(
        self, pdk_map_file, tmp_path, leak_token,
    ):
        """R3 P2 — even if a foundry-prefixed token clears the
        ``_MAESTRO_TEST_NAME_RE`` whitelist (it's char-legal), the
        second-stage ``_FOUNDRY_LEAK_RE`` filter drops it. Directly
        probes ``_list_remote_maestro_tests`` so the assertion is on
        the parser output, not on a downstream raise.
        """
        client = MagicMock()
        client.execute_skill.return_value = MagicMock(
            output=f'"TB_REAL" "{leak_token}"',
            errors=None,
        )
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        names = b._list_remote_maestro_tests()
        assert "TB_REAL" in names
        assert leak_token not in names

    def test_remote_probe_parser_documented_behavior(
        self, pdk_map_file, tmp_path,
    ):
        """R3 P2 — pin the exact parser output for a representative
        raw blob so future refactors can't silently change behavior."""
        client = MagicMock()
        leak_token = _p0_token("n", "ch_leak")
        client.execute_skill.return_value = MagicMock(
            output=(
                # Quoted tokens: legit + bad chars + foundry-shaped
                # token + ASCII-only OCEAN word (a degenerate
                # same-as-analysis test name that remains accepted).
                f'"TB_legit_1" "bad token" "$weird" "{leak_token}" "tran"'
            ),
            errors=None,
        )
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        names = b._list_remote_maestro_tests()
        # Whitelist drops "bad token" + "$weird"; foundry filter drops
        # the synthetic leak. "tran" survives per the documented
        # limitation: it could be a legitimate user test name.
        assert names == {"TB_legit_1", "tran"}


# --------------------------------------------------------------------- #
#  R2 P2-1: _validate_name length cap (128 chars)
# --------------------------------------------------------------------- #


class TestNameLengthCap:
    """The CDB identifier cap of 128 chars is enforced inside
    ``_validate_name``, so every Maestro wrapper that funnels lib /
    cell / view / corner name through it inherits the bound."""

    def test_lib_at_cap_passes(self, bridge, writer_mocks):
        # 128 chars — boundary case, must NOT raise.
        long_lib = "L" * 128
        # Long lib still has to be alphanumeric — use 128 ``L`` chars.
        bridge.create_maestro_test(
            "TB", lib=long_lib, cell="MYCELL", simulator="spectre",
        )
        assert writer_mocks["create_test"].called

    @pytest.mark.parametrize("field,value", [
        ("lib",   "L" * 129),
        ("cell",  "C" * 200),
        ("view",  "V" * 129),
    ])
    def test_overlong_field_raises(self, bridge, writer_mocks, field, value):
        kwargs = {
            "lib": "mylib", "cell": "MYCELL",
            "simulator": "spectre",
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match=r"length"):
            bridge.create_maestro_test("TB", **kwargs)
        assert not writer_mocks["create_test"].called

    def test_corner_name_at_cap_passes(self, bridge, writer_mocks):
        bridge.setup_maestro_corner("c" * 128)
        assert writer_mocks["setup_corner"].called

    def test_corner_name_over_cap_raises(self, bridge, writer_mocks):
        with pytest.raises(ValueError, match=r"length"):
            bridge.setup_maestro_corner("c" * 129)
        assert not writer_mocks["setup_corner"].called


# --------------------------------------------------------------------- #
#  R3 P1: _delete_maestro_output_remote accepts test=None (default-test)
# --------------------------------------------------------------------- #


class TestDeleteMaestroOutputDefaultTest:
    """The R2 implementation required ``test=str``; passing ``""``
    raised inside ``_resolve_maestro_test``. R3 P1 widens the
    signature to ``str | None`` so the v2 dispatcher can forward the
    LLM's "missing test field" intent and the bridge resolves via
    the scoped tb_cell."""

    def test_none_resolves_via_scoped_tb_cell(
        self, pdk_map_file, tmp_path,
    ):
        client = MagicMock()
        client.execute_skill.return_value = MagicMock(
            output="t", errors=None,
        )
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        # No raise — and the constructed SKILL expr references MYTB.
        b._delete_maestro_output_remote("vrms", test=None)
        assert client.execute_skill.called
        expr = client.execute_skill.call_args.args[0]
        assert "MYTB" in expr
        assert "vrms" in expr

    def test_explicit_test_used_verbatim(self, pdk_map_file, tmp_path):
        client = MagicMock()
        client.execute_skill.return_value = MagicMock(
            output="t", errors=None,
        )
        b = SafeBridge(client, pdk_map_file, skill_dir=tmp_path / "ns")
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        b._delete_maestro_output_remote("vrms", test="ANOTHER_TB")
        expr = client.execute_skill.call_args.args[0]
        assert "ANOTHER_TB" in expr
        assert "MYTB" not in expr

    def test_empty_string_still_rejected(self, pdk_map_file, tmp_path):
        """Empty-string is still bad — only ``None`` is the
        default-test sentinel (matches ``add_maestro_output``)."""
        b = SafeBridge(
            _make_client_mock(), pdk_map_file, skill_dir=tmp_path / "ns",
        )
        b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
        with pytest.raises(ValueError, match=r"non-empty string"):
            b._delete_maestro_output_remote("vrms", test="")
