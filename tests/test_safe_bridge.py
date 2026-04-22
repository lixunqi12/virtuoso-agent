"""Unit tests for SafeBridge sanitization, whitelist, and input validation.

GREP-GATE EXCEPTION: this file intentionally contains synthetic
foundry-shaped tokens (nch_, pch_, cfmom, rppoly, rm1_, tsmc, tcbn)
used to exercise the _scrub() sanitizer and friends. These are
artificial stand-ins assembled from the banned-prefix alphabet; no
real foundry cell or library name appears in this file. Together with
src/safe_bridge.py (which defines _FOUNDRY_LEAK_RE), this file is one
of two allowed exceptions to the P0 grep gate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.safe_bridge import SafeBridge, _scrub, _validate_name


@pytest.fixture
def pdk_map_file(tmp_path):
    """Create a temporary pdk_map.yaml for testing."""
    content = """\
generic_cell_name: \"GENERIC_DEVICE\"

valid_aliases:
  - NMOS
  - NMOS_LVT
  - PMOS
  - PMOS_LVT
  - MIM_CAP

model_info_keys:
  - toxe
  - u0
  - vth0
  - k1
  - k2
  - pclm

allowed_params:
  - w
  - l
  - nf
  - m
  - multi
  - wf
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def mock_client():
    """Create a mock VirtuosoClient."""
    return MagicMock()


@pytest.fixture
def bridge(mock_client, pdk_map_file, tmp_path):
    """Create a SafeBridge with mocked dependencies.

    Uses a non-existent skill_dir so SKILL loading is skipped,
    testing the Python-side filtering path.

    Stage 1 rev 2 (2026-04-18): ``spectre=`` keyword removed; the
    legacy ``bridge.simulate()`` direct-Spectre path no longer exists.
    """
    return SafeBridge(
        mock_client, pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )


class TestInputValidation:
    def test_valid_names(self):
        _validate_name("mylib")
        _validate_name("my_cell_01")
        _validate_name("M1.2")
        _validate_name("inst-name")

    def test_rejects_skill_injection(self):
        with pytest.raises(ValueError):
            _validate_name('lib")')
        with pytest.raises(ValueError):
            _validate_name("lib; rm -rf /")
        with pytest.raises(ValueError):
            _validate_name("cell name")
        with pytest.raises(ValueError):
            _validate_name("lib()")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            _validate_name("")


class TestSanitization:
    def test_pre_aliased_cells_pass_through(self, bridge):
        """Normal path: remote host already aliased cells to generic names."""
        raw = {
            "instances": [
                {
                    "name": "M1",
                    "cell": "NMOS",
                    "lib": "GENERIC_PDK",
                    "params": {"w": "1u", "l": "100n"},
                },
                {
                    "name": "M2",
                    "cell": "PMOS_LVT",
                    "lib": "GENERIC_PDK",
                    "params": {"w": "2u", "l": "200n"},
                },
            ]
        }
        result = bridge._sanitize(raw)
        assert result["instances"][0]["cell"] == "NMOS"
        assert result["instances"][1]["cell"] == "PMOS_LVT"

    def test_remote_gap_foundry_name_becomes_generic(self, bridge):
        """Defense-in-depth: if a foundry name slips past remote host's filter,
        PC must replace it with the generic fallback (PC has no map).
        The placeholder below is an intentionally neutral stand-in; real
        foundry cell names must never appear in the PC-side repo."""
        raw = {
            "instances": [
                {
                    "name": "M1",
                    "cell": "raw_device_alpha",
                    "lib": "GENERIC_PDK",
                    "params": {"w": "1u"},
                },
            ]
        }
        result = bridge._sanitize(raw)
        assert result["instances"][0]["cell"] == "GENERIC_DEVICE"

    def test_lib_name_replaced(self, bridge):
        raw = {
            "instances": [
                {
                    "name": "M1",
                    "cell": "NMOS",
                    "lib": "remote_raw_lib",
                    "params": {},
                },
            ]
        }
        result = bridge._sanitize(raw)
        assert result["instances"][0]["lib"] == "GENERIC_PDK"

    def test_model_param_removed(self, bridge):
        raw = {
            "instances": [
                {
                    "name": "M1",
                    "cell": "NMOS",
                    "lib": "GENERIC_PDK",
                    "params": {
                        "w": "1u",
                        "l": "100n",
                        "model": "raw_model_str",
                        "u0": 400,
                    },
                },
            ]
        }
        result = bridge._sanitize(raw)
        assert "model" not in result["instances"][0]["params"]
        assert "u0" not in result["instances"][0]["params"]
        assert result["instances"][0]["params"]["w"] == "1u"

    def test_unknown_cell_redacted(self, bridge):
        raw = {
            "instances": [
                {
                    "name": "R1",
                    "cell": "unknown_res",
                    "lib": "GENERIC_PDK",
                    "params": {},
                },
            ]
        }
        result = bridge._sanitize(raw)
        assert result["instances"][0]["cell"] == "GENERIC_DEVICE"

    def test_empty_instances(self, bridge):
        raw = {"instances": []}
        result = bridge._sanitize(raw)
        assert result["instances"] == []


class TestModelInfoFiltering:
    def test_filters_bsim4_keys(self, bridge):
        assert bridge._is_model_info("toxe") is True
        assert bridge._is_model_info("M1.u0") is True
        assert bridge._is_model_info("vth0_data") is True
        assert bridge._is_model_info("pclm") is True

    def test_passes_safe_keys(self, bridge):
        assert bridge._is_model_info("vout") is False
        assert bridge._is_model_info("freq") is False
        assert bridge._is_model_info("gain") is False
        assert bridge._is_model_info("time") is False

    # Stage 1 rev 2 (2026-04-18): ``test_simulate_filters_nested_model_info``
    # removed along with ``bridge.simulate()``. ``_strip_model_info`` is
    # still exercised via ``read_circuit`` — see TestSanitization above.


class TestParamWhitelist:
    def test_allowed_params(self, bridge):
        bridge.set_params("mylib", "mycell", "M1", {"w": "1u", "l": "100n"})
        bridge.set_params("mylib", "mycell", "M1", {"nf": 4, "m": 2})
        bridge.set_params("mylib", "mycell", "M1", {"multi": 1, "wf": "500n"})

    def test_stage1_rev1_default_whitelist_includes_lc_vco_spec_params(
        self, mock_client, tmp_path
    ):
        """Stage 1 rev 1 (2026-04-18) — BLOCKER #3 regression guard.

        The Python-default whitelist in safe_bridge.py (used when a YAML
        config does not declare `allowed_params:`) must include the four
        names added per config/LC_VCO_spec.md §4: nfin, fingers, idc, vdc.
        A YAML without `allowed_params:` falls back to this default —
        that path is how run_agent.py boots before pdk_map.yaml overrides.
        """
        # Build a minimal YAML that EXERCISES THE DEFAULT — i.e. no
        # `allowed_params:` key present. SafeBridge should then use its
        # hard-coded default set.
        yaml_path = tmp_path / "defaultish_pdk_map.yaml"
        yaml_path.write_text(
            "generic_cell_name: GENERIC_DEVICE\n"
            "valid_aliases: [NMOS, PMOS]\n"
            "model_info_keys: [toxe]\n",
            encoding="utf-8",
        )
        b = SafeBridge(
            mock_client, str(yaml_path),
            skill_dir=tmp_path / "no_skill",
        )
        for name in ("nfin", "fingers", "idc", "vdc"):
            assert name in b.allowed_params, (
                f"{name!r} missing from default whitelist — "
                "LC_VCO_spec §4 compliance broken"
            )
        # Positive: Layer 1 accepts each of the four new names exactly.
        for name in ("nfin", "fingers", "idc", "vdc", "Nfin", "IDC"):
            assert b._is_allowed_param_name(name), (
                f"{name!r} rejected by _is_allowed_param_name despite "
                "being in core whitelist"
            )

    def test_stage1_rev1_new_whitelist_names_drive_set_params(self, bridge):
        """BLOCKER #3 end-to-end: set_params accepts nfin/fingers/idc/vdc
        when they appear in the whitelist YAML (the test fixture's
        allowed_params list does NOT include them, so this exercises
        Layer 2 regex fallback for those names against the bridge's
        own fixture). Smoke-test guards against regression where a
        future blocked-word addition accidentally swallows these names.
        """
        # Fixture whitelist excludes these, so validation goes via Layer 2.
        # All four are short ASCII identifiers with no blocked substring.
        bridge.set_params("mylib", "mycell", "M1", {"nfin": 8})
        bridge.set_params("mylib", "mycell", "M1", {"fingers": 4})
        bridge.set_params("mylib", "mycell", "M1", {"idc": "300u"})
        bridge.set_params("mylib", "mycell", "M1", {"vdc": "0.8"})

    def test_rejects_forbidden_params(self, bridge):
        with pytest.raises(ValueError, match="not allowed"):
            bridge.set_params("mylib", "mycell", "M1", {"vth0": 0.5})
        with pytest.raises(ValueError, match="not allowed"):
            bridge.set_params("mylib", "mycell", "M1", {"model": "anything"})
        with pytest.raises(ValueError, match="not allowed"):
            bridge.set_params("mylib", "mycell", "M1", {"toxe": 1e-9})

    def test_mixed_allowed_forbidden(self, bridge):
        with pytest.raises(ValueError, match="not allowed"):
            bridge.set_params(
                "mylib", "mycell", "M1", {"w": "1u", "model": "anything"}
            )

    def test_rejects_injected_param_value(self, bridge):
        with pytest.raises(ValueError, match="Unsafe parameter value"):
            bridge.set_params(
                "mylib",
                "mycell",
                "M1",
                {"w": '1u) "model" "secret"'},
            )


class TestReadCircuit:
    def test_read_circuit_sanitizes(self, bridge, mock_client):
        """Expected happy-path: remote host has already aliased cell and lib;
        PC passes the generic names through and strips model info."""
        mock_client.execute_skill.return_value = {
            "instances": [
                {
                    "name": "M1",
                    "cell": "NMOS",
                    "lib": "GENERIC_PDK",
                    "params": {"w": "1u", "l": "100n", "model": "raw_model_str"},
                },
                {
                    "name": "C1",
                    "cell": "MIM_CAP",
                    "lib": "GENERIC_PDK",
                    "params": {"w": "1u", "l": "10u"},
                },
            ]
        }
        result = bridge.read_circuit("mylib", "mycell")

        assert result["instances"][0]["cell"] == "NMOS"
        assert result["instances"][0]["lib"] == "GENERIC_PDK"
        assert "model" not in result["instances"][0]["params"]
        assert result["instances"][1]["cell"] == "MIM_CAP"

    def test_read_circuit_validates_names(self, bridge):
        with pytest.raises(ValueError):
            bridge.read_circuit('lib")', "cell")
        with pytest.raises(ValueError):
            bridge.read_circuit("lib", "cell; drop")

    def test_read_op_point_filters_sensitive_keys(self, bridge, mock_client):
        mock_client.execute_skill.return_value = {
            "vdd": 1.8,
            "M1": {"gm": 1e-3, "id": 1e-4, "toxe": 1.2e-9},
            "M2": {"u0": 350, "vth": 0.45},
        }

        result = bridge.read_op_point("mylib", "mycell")

        assert result["vdd"] == 1.8
        assert result["M1"] == {"gm": 1e-3, "id": 1e-4}
        assert result["M2"] == {"vth": 0.45}


class TestSkillIntegration:
    """Tests for SKILL-enabled code paths."""

    def test_alias_cell_preserves_known_alias(self, bridge):
        """Bug 1: already-aliased names like NMOS must not become GENERIC_DEVICE."""
        assert bridge._alias_cell("NMOS") == "NMOS"
        assert bridge._alias_cell("PMOS_LVT") == "PMOS_LVT"
        assert bridge._alias_cell("MIM_CAP") == "MIM_CAP"

    def test_alias_cell_foundry_name_becomes_generic(self, bridge):
        """PC no longer holds real foundry names; any foundry-shaped name
        that reaches PC (meaning remote host's filter missed it) is replaced with
        the generic fallback rather than being mapped. The placeholders
        below stand in for real foundry names, which must never appear
        in the PC-side repo."""
        assert bridge._alias_cell("raw_device_alpha") == "GENERIC_DEVICE"
        assert bridge._alias_cell("raw_device_beta") == "GENERIC_DEVICE"

    def test_alias_cell_unknown_becomes_generic(self, bridge):
        assert bridge._alias_cell("unknown_res") == "GENERIC_DEVICE"

    def test_legacy_cell_map_config_rejected(self, tmp_path, mock_client):
        """Deprecated 'cell_map:' schema must be refused so foundry names
        cannot linger in the PC-side repo."""
        legacy_yaml = tmp_path / "legacy.yaml"
        legacy_yaml.write_text(
            "generic_cell_name: \"GENERIC_DEVICE\"\n"
            "cell_map:\n"
            "  foo: \"NMOS\"\n"
            "allowed_params: [w, l]\n"
            "model_info_keys: [toxe]\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="cell_map"):
            SafeBridge(
                mock_client, str(legacy_yaml),
                skill_dir=tmp_path / "no_skill",
            )

    def test_sanitize_op_point_skill_envelope(self, bridge):
        """Bug 2: SKILL returns {"instances": {"M1": {...}}} envelope."""
        skill_data = {
            "cell": "opamp",
            "lib": "GENERIC_PDK",
            "analysis": "dcOp",
            "instances": {
                "M1": {"gm": 1e-3, "id": 1e-4, "toxe": 1.2e-9},
                "M2": {"gds": 1e-6, "vth": 0.45},
            }
        }
        result = bridge._sanitize_op_point(skill_data)
        assert "M1" in result
        assert result["M1"] == {"gm": 1e-3, "id": 1e-4}
        assert result["M2"] == {"gds": 1e-6, "vth": 0.45}

    def test_sanitize_op_point_flat_still_works(self, bridge):
        """Flat format (Python-only path) must still work after Fix 2."""
        flat_data = {
            "vdd": 1.8,
            "M1": {"gm": 1e-3, "id": 1e-4},
        }
        result = bridge._sanitize_op_point(flat_data)
        assert result["vdd"] == 1.8
        assert result["M1"] == {"gm": 1e-3, "id": 1e-4}

    def test_sanitize_skill_prealiased(self, bridge):
        """Bug 1 end-to-end: SKILL-aliased cells pass through _sanitize()."""
        raw = {
            "instances": [
                {"instName": "M1", "cell": "NMOS", "lib": "GENERIC_PDK", "params": {"w": "1u"}},
            ]
        }
        result = bridge._sanitize(raw)
        assert result["instances"][0]["cell"] == "NMOS"
        assert result["instances"][0]["lib"] == "GENERIC_PDK"

    def test_execute_skill_json_raises_on_error(self, bridge, mock_client):
        """Bug 4: SKILL error JSON must raise RuntimeError."""
        mock_client.execute_skill.return_value = {"error": "cellView not found"}
        with pytest.raises(RuntimeError, match="SKILL helper returned error"):
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')


class TestScrub:
    """Tests for the _scrub() recursive sanitizer.

    Placeholders below intentionally use foundry-shaped prefixes only so
    that the scrubber is exercised; these stand in for real foundry
    names, which must never appear in the PC-side repo."""

    def test_scrub_redacts_foundry_prefixes(self):
        assert _scrub("cell nch_xyz not found") == "cell <redacted> not found"
        assert _scrub("pch_abc missing") == "<redacted> missing"
        assert _scrub("lib=tsmcN16") == "lib=<redacted>"
        assert _scrub("tcbn16 broken") == "<redacted> broken"
        assert _scrub("cfmom foo") == "<redacted> foo"
        assert _scrub("rppoly_x bad") == "<redacted> bad"
        assert _scrub("rm1_top missing") == "<redacted> missing"

    def test_scrub_is_case_insensitive(self):
        assert _scrub("NCH_ALPHA") == "<redacted>"
        assert _scrub("TSMC_STUFF") == "<redacted>"

    def test_scrub_redacts_windows_paths(self):
        scrubbed = _scrub("file at C:\\Users\\alice\\secret\\data.yaml not found")
        assert "C:\\" not in scrubbed
        assert "alice" not in scrubbed
        assert "<path>" in scrubbed

    def test_scrub_redacts_unix_paths(self):
        scrubbed = _scrub("see /home/bob/proj/file and /project/tape/out")
        assert "bob" not in scrubbed
        assert "/home" not in scrubbed
        assert "/project" not in scrubbed
        assert scrubbed.count("<path>") == 2

    def test_scrub_redacts_extended_unix_path_roots(self):
        """Batch A round-2: /nfs, /proj, /mnt, /srv, /data roots must scrub."""
        for raw, leaked in (
            ("err at /nfs/pdk/models.scs boom", "/nfs/pdk"),
            ("err at /proj/team/run boom", "/proj/team"),
            ("err at /mnt/cds/libs boom", "/mnt/cds"),
            ("err at /srv/flow/x boom", "/srv/flow"),
            ("err at /data/tape/y boom", "/data/tape"),
        ):
            scrubbed = _scrub(raw)
            assert leaked not in scrubbed, (raw, scrubbed)
            assert "<path>" in scrubbed

    def test_scrub_redacts_unc_paths(self):
        """Batch A round-2: UNC paths \\\\server\\share\\... must scrub."""
        scrubbed = _scrub("failure at \\\\fileserver\\pdk_share\\run.log end")
        assert "fileserver" not in scrubbed
        assert "pdk_share" not in scrubbed
        assert "<path>" in scrubbed

    def test_scrub_redacts_forward_slash_unc(self):
        """Batch A round-3: forward-slash UNC //server/share/... must scrub
        (produced by Path.as_posix() on Windows)."""
        scrubbed = _scrub("failure at //fileserver/pdk_share/run.log end")
        assert "fileserver" not in scrubbed
        assert "pdk_share" not in scrubbed
        assert "<path>" in scrubbed

    def test_scrub_redacts_eda_tool_paths(self):
        """Batch A round-4: EDA-common roots /tools, /cadence, /pdk, /eda,
        /scratch, /work must scrub (claude_reviewer Low finding)."""
        for raw, leaked in (
            ("fail at /tools/cadence/IC23.1/bin/virtuoso", "/tools/cadence"),
            ("fail at /cadence/pdk16/models", "/cadence/pdk16"),
            ("fail at /pdk/tsmc_stage/work boom", "/pdk/"),
            ("fail at /eda/runs/job1", "/eda/runs"),
            ("fail at /scratch/user/opamp", "/scratch/user"),
            ("fail at /work/user/proj", "/work/user"),
        ):
            scrubbed = _scrub(raw)
            assert leaked not in scrubbed, (raw, scrubbed)
            assert "<path>" in scrubbed

    def test_scrub_passes_through_safe_strings(self):
        assert _scrub("hello world") == "hello world"
        assert _scrub("NMOS ok") == "NMOS ok"
        assert _scrub("GENERIC_DEVICE fine") == "GENERIC_DEVICE fine"

    def test_scrub_passes_through_non_strings(self):
        assert _scrub(42) == 42
        assert _scrub(3.14) == 3.14
        assert _scrub(None) is None
        assert _scrub(True) is True

    def test_scrub_recurses_into_dict(self):
        data = {"msg": "found nch_xyz", "code": 7}
        result = _scrub(data)
        assert result == {"msg": "found <redacted>", "code": 7}

    def test_scrub_recurses_into_list(self):
        data = ["ok", "tsmc_blob", 3]
        result = _scrub(data)
        assert result == ["ok", "<redacted>", 3]

    def test_scrub_recurses_nested(self):
        data = {"a": [{"b": "pch_bad"}], "c": "/home/alice/x"}
        result = _scrub(data)
        assert result["a"][0]["b"] == "<redacted>"
        assert "/home" not in result["c"]
        assert "<path>" in result["c"]


class TestExceptionSanitization:
    """Tests that exception messages never leak foundry names or paths."""

    _BANNED_TOKENS = (
        "nch_", "pch_", "cfmom", "rppoly", "rm1_", "tsmc", "tcbn",
        "C:\\", "/home/", "/project/",
    )

    def _assert_clean(self, message: str):
        lower = message.lower()
        for token in self._BANNED_TOKENS:
            assert token.lower() not in lower, (
                f"exception message leaked token {token!r}: {message!r}"
            )

    def test_validate_name_hides_raw_name(self):
        """_validate_name must not echo the raw invalid name."""
        raw = "nch_xxx_synthetic"
        with pytest.raises(ValueError) as exc_info:
            _validate_name(raw + "; rm -rf /", "cell")
        self._assert_clean(str(exc_info.value))
        # Length-only disclosure must be present (regression guard).
        assert "len=" in str(exc_info.value)

    def test_normalize_param_name_hides_raw_key(self, bridge):
        # Use a format-invalid key that also contains a foundry-shaped
        # substring, so the regex rejects it and we can assert the
        # exception message does not echo the foundry substring.
        raw_bad_key = "nch_bad; inject"
        with pytest.raises(ValueError) as exc_info:
            bridge._normalize_param_name(raw_bad_key)
        self._assert_clean(str(exc_info.value))

    def test_format_param_value_hides_raw_value(self, bridge):
        unsafe = 'tsmc_blob) "model"'
        with pytest.raises(ValueError, match="Unsafe parameter value") as exc_info:
            bridge._format_param_value(unsafe)
        self._assert_clean(str(exc_info.value))

    def test_execute_skill_json_scrubs_error_field(self, bridge, mock_client):
        """SKILL-returned error strings may contain foundry names; must be scrubbed."""
        mock_client.execute_skill.return_value = {
            "error": "cellView nch_xxx not found in tsmcYY"
        }
        with pytest.raises(RuntimeError) as exc_info:
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')
        self._assert_clean(str(exc_info.value))
        assert "<redacted>" in str(exc_info.value)

    def test_execute_skill_json_invalid_payload_hides_content(
        self, bridge, mock_client
    ):
        """Invalid JSON payload must not be echoed verbatim into exceptions."""
        raw = "not-json-contains-nch_xxx-and-C:\\Users\\alice\\x"
        mock_client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output=raw,
        )
        with pytest.raises(RuntimeError) as exc_info:
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')
        self._assert_clean(str(exc_info.value))
        # type/length disclosure must be present (regression guard).
        assert "len=" in str(exc_info.value)

    def test_execute_skill_json_wrong_payload_type_hides_content(
        self, bridge, mock_client
    ):
        """Non-string payload must not be echoed into exception either."""
        mock_client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output=12345,
        )
        with pytest.raises(TypeError) as exc_info:
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')
        # Must only disclose type, not value.
        assert "type=" in str(exc_info.value)
        assert "12345" not in str(exc_info.value)

    def test_set_params_rejects_forbidden_with_clean_message(self, bridge):
        """set_params must refuse forbidden key without echoing foundry-shaped
        names verbatim (defense-in-depth; forbidden keys should not be
        foundry-shaped in practice, but scrub protects the path anyway)."""
        with pytest.raises(ValueError, match="not allowed") as exc_info:
            bridge.set_params("lib", "cell", "M1", {"tsmc_secret_key": 1})
        self._assert_clean(str(exc_info.value))

    # Stage 1 rev 2 (2026-04-18): ``test_simulate_scrubs_spectre_error_text``
    # removed along with ``bridge.simulate()``. OCEAN errors flow through
    # ``run_ocean_sim`` which already has equivalent scrubbing guarded by
    # TestRunOceanSim scrub coverage.

    def test_set_params_skill_fail_scrubs_error(
        self, bridge, monkeypatch
    ):
        """Batch A round-4 (claude LEAK-P1.2-C): safeSetParam failure
        path's result['error'] must be scrubbed.

        In normal flow `_execute_skill_json` would raise on any dict with
        an 'error' key, so this branch is only reachable via refactor or
        a bug. We monkeypatch `_execute_skill_json` to directly return a
        dict with ok=False and a leaking error, so the raw `safeSetParam
        failed: ...` branch is exercised for regression."""
        monkeypatch.setattr(bridge, "_skill_loaded", True)
        leaky = {
            "ok": False,
            "error": "cellView not found in tsmcYY at /pdk/tsmc_stage/models",
        }
        monkeypatch.setattr(bridge, "_execute_skill_json", lambda expr: leaky)
        with pytest.raises(RuntimeError, match="safeSetParam failed") as exc_info:
            bridge.set_params("mylib", "opamp", "M1", {"w": "1u"})
        msg = str(exc_info.value)
        self._assert_clean(msg)
        assert "/pdk" not in msg
        assert "<redacted>" in msg or "<path>" in msg

    def test_execute_skill_json_bad_payload_has_no_cause(
        self, bridge, mock_client
    ):
        """Batch A round-4 (claude Info): JSONDecodeError must not be
        chained as __cause__; its .doc attribute would leak raw payload."""
        mock_client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output="not-json-contains-nch_xxx",
        )
        with pytest.raises(RuntimeError) as exc_info:
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True

    def test_execute_skill_json_unwraps_double_encoded_payload(
        self, bridge, mock_client
    ):
        """Real-machine LC_VCO read (2026-04-17): virtuoso-bridge-lite
        JSON-encodes SKILL string return values, producing a payload that
        decodes to str on first pass. `_execute_skill_json` must unwrap
        exactly one extra level so sanitized dicts still come through."""
        import json as _json
        inner = {
            "cell": "LC_VCO",
            "lib": "GENERIC_PDK",
            "view": "schematic",
            "instances": [{"instName": "M9", "cell": "NMOS_SVT"}],
            "pins": [],
        }
        # Double-encode: first dumps inner object, then dumps the result
        # again as a JSON string. This mirrors what virtuoso-bridge-lite
        # puts on the wire.
        double = _json.dumps(_json.dumps(inner))
        mock_client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output=double,
        )
        decoded = bridge._execute_skill_json(
            'safeReadSchematic("lib" "cell")'
        )
        assert isinstance(decoded, dict)
        assert decoded["cell"] == "LC_VCO"
        assert decoded["lib"] == "GENERIC_PDK"
        assert decoded["instances"][0]["instName"] == "M9"

    def test_execute_skill_json_unwrap_bounded_one_level(
        self, bridge, mock_client
    ):
        """Unwrap is bounded to one extra level, no unbounded loop.
        A triple-encoded payload must still reach a clean TypeError
        (str after single extra decode → falls through to dict check)
        without echoing the raw payload content."""
        import json as _json
        inner = '{"cell":"X"}'
        triple = _json.dumps(_json.dumps(inner))  # dumps(dumps(str))
        mock_client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output=triple,
        )
        with pytest.raises(TypeError) as exc_info:
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')
        msg = str(exc_info.value)
        assert "type=str" in msg
        # Payload content must not leak into the error message.
        assert "cell" not in msg

    def test_raise_on_skill_failure_scrubs_label_and_errors(
        self, bridge, mock_client
    ):
        """Batch A round-2 blocker: _raise_on_skill_failure must scrub both
        the label (may embed lib/cell from expr) and errors list."""
        mock_client.execute_skill.return_value = SimpleNamespace(
            ok=False,
            errors=["skill load failed at /proj/team/run for tsmc_blob"],
            output=None,
        )
        with pytest.raises(RuntimeError) as exc_info:
            bridge._execute_skill_json('safeReadSchematic("lib" "cell")')
        msg = str(exc_info.value)
        self._assert_clean(msg)
        assert "/proj" not in msg
        assert "<path>" in msg


class TestSkillEntrypointAllowlist:
    """Tests for the SKILL entrypoint allow-list gate in _execute_skill_json.

    Only the sanitizing wrappers (safeReadSchematic / safeReadOpPoint /
    safeSetParam) and the legacy fallbacks (read_schematic / read_op_point /
    set_instance_param) may be forwarded to remote host. Anything else — raw
    hiOpenLib, dbOpenCellViewByType, arbitrary load() — must be rejected
    at the Python bridge before reaching execute_skill().
    """

    _BANNED_TOKENS = (
        "nch_", "pch_", "cfmom", "rppoly", "rm1_", "tsmc", "tcbn",
    )

    def _assert_clean(self, message: str):
        lower = message.lower()
        for token in self._BANNED_TOKENS:
            assert token.lower() not in lower, (
                f"exception message leaked token {token!r}: {message!r}"
            )

    def test_allowed_entrypoints_pass(self, bridge, mock_client):
        mock_client.execute_skill.return_value = {"ok": True, "instances": []}
        for entry in (
            'safeReadSchematic("lib" "cell")',
            'safeReadOpPoint("lib" "cell")',
            'safeSetParam("lib" "cell" "M1" list())',
            'read_schematic("lib" "cell")',
            'read_op_point("lib" "cell")',
            'set_instance_param("lib" "cell" "M1" list())',
        ):
            bridge._execute_skill_json(entry)

    def test_rejects_raw_hiopenlib(self, bridge, mock_client):
        with pytest.raises(ValueError, match="not allowed"):
            bridge._execute_skill_json('hiOpenLib("anylib")')
        mock_client.execute_skill.assert_not_called()

    def test_rejects_raw_dbopencellview(self, bridge, mock_client):
        with pytest.raises(ValueError, match="not allowed"):
            bridge._execute_skill_json(
                'dbOpenCellViewByType("lib" "cell" "schematic")'
            )
        mock_client.execute_skill.assert_not_called()

    def test_rejects_arbitrary_load(self, bridge, mock_client):
        with pytest.raises(ValueError, match="not allowed"):
            bridge._execute_skill_json('load("/tmp/evil.il")')
        mock_client.execute_skill.assert_not_called()

    def test_rejects_non_call_expression(self, bridge, mock_client):
        with pytest.raises(ValueError, match="allowed function call"):
            bridge._execute_skill_json('"bare string"')
        mock_client.execute_skill.assert_not_called()

    def test_rejects_empty_expression(self, bridge, mock_client):
        with pytest.raises(ValueError, match="allowed function call"):
            bridge._execute_skill_json('')
        mock_client.execute_skill.assert_not_called()

    def test_rejection_message_scrubs_foundry_entrypoint(
        self, bridge, mock_client
    ):
        """Even the rejection message itself must not leak a foundry-shaped
        entrypoint name (e.g. if someone tries hiOpenLib-like name that
        happens to start with a banned prefix)."""
        with pytest.raises(ValueError) as exc_info:
            bridge._execute_skill_json('nch_sneaky("lib")')
        self._assert_clean(str(exc_info.value))
        mock_client.execute_skill.assert_not_called()

    def test_rejects_before_client_call(self, bridge, mock_client):
        """Gate must short-circuit BEFORE reaching client.execute_skill."""
        with pytest.raises(ValueError):
            bridge._execute_skill_json('rmdir("/")')
        mock_client.execute_skill.assert_not_called()

    def test_rejects_nested_load_as_first_arg(self, bridge, mock_client):
        """Batch B round-2: even if the outer entrypoint is allowed,
        nested calls like safeReadSchematic(load("/evil.il") "cell") must
        be rejected — otherwise load() still executes on remote host."""
        with pytest.raises(ValueError, match="Nested SKILL call"):
            bridge._execute_skill_json(
                'safeReadSchematic(load("/tmp/evil.il") "cell")'
            )
        mock_client.execute_skill.assert_not_called()

    def test_rejects_nested_hiopenlib(self, bridge, mock_client):
        with pytest.raises(ValueError, match="Nested SKILL call"):
            bridge._execute_skill_json(
                'safeReadSchematic("lib" hiOpenLib("x"))'
            )
        mock_client.execute_skill.assert_not_called()

    def test_rejects_nested_in_safesetparam(self, bridge, mock_client):
        with pytest.raises(ValueError, match="Nested SKILL call"):
            bridge._execute_skill_json(
                'safeSetParam("lib" "cell" "M1" hiOpenLib("x"))'
            )
        mock_client.execute_skill.assert_not_called()

    def test_rejects_multi_call_sequence(self, bridge, mock_client):
        """Batch B round-2 (claude Gate-Weak): top-level sequence like
        safeReadSchematic("a" "b") hiOpenLib("c") must be rejected. The
        nested-call scanner catches the second identifier."""
        with pytest.raises(ValueError, match="Nested SKILL call"):
            bridge._execute_skill_json(
                'safeReadSchematic("a" "b") hiOpenLib("c")'
            )
        mock_client.execute_skill.assert_not_called()

    def test_load_skill_helpers_no_traceback_leak(
        self, tmp_path, mock_client, pdk_map_file, caplog
    ):
        """Batch A round-4 (claude LEAK-P1.2-D regression): _load_skill_helpers
        must NOT use exc_info=True — Python's logging formatter would dump
        absolute frame paths past the _scrub layer."""
        import logging
        # Arrange: a skill_dir with all 4 scripts present so we go past the
        # first loop, but make client.execute_skill raise so we hit the
        # except-Exception branch.
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        for name in ("helpers.il", "safe_read_schematic.il",
                     "safe_read_op_point.il", "safe_set_param.il"):
            (skill_dir / name).write_text("; dummy\n", encoding="utf-8")

        mock_client.execute_skill.side_effect = RuntimeError(
            "boom at /pdk/tsmc_stage/models.il"
        )

        with caplog.at_level(logging.WARNING, logger="src.safe_bridge"):
            SafeBridge(
                mock_client, pdk_map_file,
                skill_dir=skill_dir,
            )

        # No record should carry exc_info (traceback) — that would leak
        # frame file paths.
        for rec in caplog.records:
            assert rec.exc_info is None, (
                f"log record used exc_info=True — traceback would leak "
                f"frame paths: {rec.getMessage()!r}"
            )
        # Error message itself must be scrubbed.
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "/pdk" not in joined
        assert "tsmc" not in joined.lower()

    def test_allows_nested_list_constructor(self, bridge, mock_client):
        """SKILL list(...) is a pure data constructor and must be allowed
        so legitimate safeSetParam(... list(list("w" "1u"))) calls still work."""
        mock_client.execute_skill.return_value = {"ok": True, "instances": []}
        bridge._execute_skill_json(
            'safeSetParam("lib" "cell" "M1" list(list("w" "1u")))'
        )
        mock_client.execute_skill.assert_called_once()

    def test_rejects_unicode_identifier_bypass(self, bridge, mock_client):
        """Regression: codex round-4 blocker — an attacker could smuggle a
        non-ASCII identifier (e.g. the Greek lambda λ) past the ASCII-only
        _SKILL_ANY_CALL_RE scanner, because λ is not matched by
        [A-Za-z_]\\w* but is still a valid SKILL-like token on some parsers.

        The pure-ASCII pre-check in _check_skill_entrypoint() must reject
        this outright without ever calling through to execute_skill().
        """
        with pytest.raises(ValueError, match="pure ASCII"):
            bridge._execute_skill_json(
                'safeReadSchematic(\u03bb("x") "cell")'
            )
        mock_client.execute_skill.assert_not_called()

    def test_rejects_cyrillic_lookalike_entrypoint(self, bridge, mock_client):
        """Defense-in-depth: Cyrillic lookalike letters (e.g. the Cyrillic
        'a' U+0430 inside 'safeReadSchematic') must also be rejected by
        the ASCII gate — otherwise a homograph attack could forge an
        entrypoint that visually matches the allow-list."""
        # U+0430 is the Cyrillic 'а', visually identical to ASCII 'a'.
        expr = "s\u0430feReadSchematic(\"lib\" \"cell\")"
        with pytest.raises(ValueError, match="pure ASCII"):
            bridge._execute_skill_json(expr)
        mock_client.execute_skill.assert_not_called()

    def test_rejects_unicode_in_string_arg(self, bridge, mock_client):
        """Pure-ASCII gate also blocks non-ASCII data inside quoted string
        arguments. This is intentional: PDK cell names are ASCII and any
        non-ASCII content signals either a bug, data corruption, or an
        attempted bypass. Rejecting at the bridge is cheaper than trying
        to sanitize downstream."""
        expr = 'safeReadSchematic("lib" "ce\u00f1l")'  # 'ceñl'
        with pytest.raises(ValueError, match="pure ASCII"):
            bridge._execute_skill_json(expr)
        mock_client.execute_skill.assert_not_called()

    def test_rejects_embedded_nul(self, bridge, mock_client):
        """Regression: round-5 codex blocker — ``expr.isascii()`` alone
        still lets ASCII NUL (0x00) slip through because ``\\x00`` IS ASCII.
        An embedded NUL inside a string argument can confuse downstream
        SKILL/C parsers into truncating or re-splitting the payload.
        Control-char gate must reject it."""
        expr = 'safeReadSchematic("lib" "ce\x00ll")'
        with pytest.raises(ValueError, match="control char"):
            bridge._execute_skill_json(expr)
        mock_client.execute_skill.assert_not_called()

    def test_rejects_c0_control_chars(self, bridge, mock_client):
        """BEL / BS / VT / FF are ASCII but forbidden. They have no place
        in a SKILL expression and could be used for terminal-escape
        injection in error paths that print the expression verbatim."""
        for ch in ("\x07", "\x08", "\x0b", "\x0c"):
            expr = f'safeReadSchematic("lib" "ce{ch}ll")'
            with pytest.raises(ValueError, match="control char"):
                bridge._execute_skill_json(expr)
        mock_client.execute_skill.assert_not_called()

    def test_rejects_del_char(self, bridge, mock_client):
        """DEL (0x7F) is technically ASCII but historically a control
        character. Reject to stay consistent with the C0 policy."""
        expr = 'safeReadSchematic("lib" "ce\x7fll")'
        with pytest.raises(ValueError, match="DEL char"):
            bridge._execute_skill_json(expr)
        mock_client.execute_skill.assert_not_called()

    def test_allows_standard_whitespace(self, bridge, mock_client):
        """Tab / LF / CR must still be allowed since the entrypoint regex
        uses ``\\s*`` and legitimate multi-line SKILL payloads may use them."""
        mock_client.execute_skill.return_value = {"ok": True, "instances": []}
        bridge._execute_skill_json('safeReadSchematic("lib"\t"cell")')
        bridge._execute_skill_json('safeReadSchematic("lib"\n"cell")')
        bridge._execute_skill_json('safeReadSchematic("lib"\r"cell")')
        assert mock_client.execute_skill.call_count == 3


class TestScopeBinding:
    """Tests for P1.3 set_scope() — bind bridge to a single (lib, cell).

    After scope is bound, every read/write with a mismatching lib/cell
    must be rejected at the bridge, even if the CLI-supplied pair is
    otherwise valid.
    """

    def test_default_bridge_has_no_scope(self, bridge, mock_client):
        """Backward-compat: if set_scope() is never called, any lib/cell
        (that passes _validate_name) is accepted."""
        mock_client.execute_skill.return_value = {"instances": []}
        bridge.read_circuit("lib1", "cellA")
        bridge.read_circuit("lib2", "cellB")

    def test_set_scope_allows_matching_calls(self, bridge, mock_client):
        mock_client.execute_skill.return_value = {"instances": []}
        bridge.set_scope("mylib", "opamp")
        bridge.read_circuit("mylib", "opamp")
        bridge.read_op_point("mylib", "opamp")
        bridge.set_params("mylib", "opamp", "M1", {"w": "1u"})

    def test_set_scope_rejects_mismatch_lib(self, bridge, mock_client):
        bridge.set_scope("mylib", "opamp")
        with pytest.raises(ValueError, match="outside bound scope"):
            bridge.read_circuit("otherlib", "opamp")
        mock_client.execute_skill.assert_not_called()

    def test_set_scope_rejects_mismatch_cell(self, bridge, mock_client):
        bridge.set_scope("mylib", "opamp")
        with pytest.raises(ValueError, match="outside bound scope"):
            bridge.read_circuit("mylib", "othercell")
        mock_client.execute_skill.assert_not_called()

    def test_set_scope_rejects_set_params_mismatch(self, bridge, mock_client):
        bridge.set_scope("mylib", "opamp")
        with pytest.raises(ValueError, match="outside bound scope"):
            bridge.set_params("otherlib", "opamp", "M1", {"w": "1u"})

    def test_set_scope_cannot_be_rebound(self, bridge):
        bridge.set_scope("mylib", "opamp")
        with pytest.raises(RuntimeError, match="already bound"):
            bridge.set_scope("otherlib", "opamp")
        with pytest.raises(RuntimeError, match="already bound"):
            bridge.set_scope("mylib", "othercell")

    def test_set_scope_validates_names(self, bridge):
        with pytest.raises(ValueError):
            bridge.set_scope('lib")', "cell")
        with pytest.raises(ValueError):
            bridge.set_scope("lib", "cell; drop")

    def test_set_scope_error_does_not_leak_names(self, bridge):
        """Rejection messages must disclose length only, never the raw
        unauthorized lib/cell string."""
        bridge.set_scope("mylib", "opamp")
        with pytest.raises(ValueError) as exc_info:
            bridge.read_circuit("foundry_leak_name", "opamp")
        msg = str(exc_info.value)
        assert "foundry_leak_name" not in msg
        assert "lib_len=" in msg


# ---------------------------------------------------------------- #
#  Stage 0: B-only dynamic param-name whitelist (Layer 2)          #
# ---------------------------------------------------------------- #


class TestParamNameWhitelistLayer2:
    """Stage 0: safe-char pattern + blocklist for param names."""

    # --- Accepted names ---

    @pytest.mark.parametrize("name", [
        "Ibias",          # mixed case first letter
        "nfin_cc",        # underscore separator
        "R0",             # short, trailing digit
        "C1",
        "vctrl_coarse",   # longer name
        "a",              # minimum length 1
        "A" + "b" * 31,   # maximum length 32
    ])
    def test_accepts_safe_names(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is True

    @pytest.mark.parametrize("name", [
        "w", "l", "nf", "m", "multi", "wf",
    ])
    def test_accepts_core_params(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is True

    # --- Rejected by charset ---

    @pytest.mark.parametrize("name", [
        "",                # empty
        "1foo",            # starts with digit
        "_foo",            # starts with underscore
        "foo bar",         # contains space
        "foo-bar",         # contains hyphen
        "foo.bar",         # contains dot
        "foo(x)",          # parens (injection)
        "foo\"bar",        # quote
        "foo;rm",          # semicolon
        "a" * 33,          # length 33 > 32
    ])
    def test_rejects_unsafe_charset(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is False

    # --- Whitespace-padded names (Python/SKILL asymmetry guard) ---
    # `_normalize_param_name` internally strips, so without the upfront
    # whitespace check these would hit Layer 1 via strip() and the
    # ORIGINAL whitespace-padded key would leak into the SKILL call.

    @pytest.mark.parametrize("name", [
        " w",              # leading space on core name
        "w ",              # trailing space on core name
        "W\n",             # trailing newline on core name
        " Ibias",          # leading space on Layer-2 candidate
        "Ibias ",          # trailing space on Layer-2 candidate
        "nfin_cc\t",       # trailing tab
        " ",               # whitespace-only
    ])
    def test_rejects_whitespace_padded(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is False

    # --- Rejected by blocklist (even with safe charset) ---

    @pytest.mark.parametrize("name", [
        "loader",          # contains "load"
        "myPath",          # contains "path"
        "fileHandle",      # contains "file"
        "evalFunc",        # contains "eval"
        "sysCmd",          # does NOT contain "system" — should PASS
    ])
    def test_rejects_blocklist_substrings(self, bridge, name):
        expected = not any(
            word in name.lower()
            for word in (
                "load", "file", "path", "system", "eval", "exec", "shell",
                "include", "require", "getss", "errset", "evalstring",
                "infile", "outfile", "popen", "ipcbegin",
                "rexcompile", "rexexecute", "sprintf", "printf",
                "model", "subckt", "section",
            )
        )
        assert bridge._is_allowed_param_name(name) is expected

    # --- Rev 5: BSIM model intrinsic names (exact match, case-ins.) ---

    @pytest.mark.parametrize("name", [
        "vth0", "toxe", "u0", "k1", "k2", "pclm",
        "VTH0",   # case variant must also reject
        "Toxe",   # mixed case must also reject
    ])
    def test_rejects_bsim_model_params(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is False

    # --- Rev 5: BSIM tokens must NOT false-positive as substrings ---
    # These names legitimately contain "k1" / "k2" / "u0" as substrings
    # (stk1 = stack 1, link2 = link 2, mu0level) but are NOT BSIM
    # intrinsics. Exact match in self.model_info_keys avoids the false
    # positive that a generic substring blocklist would produce.

    @pytest.mark.parametrize("name", [
        "stk1",       # contains "k1" as suffix
        "link2",      # contains "k2" as substring
        "mu0level",   # contains "u0" as substring
    ])
    def test_model_param_substring_does_not_false_positive(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is True

    # --- Rev 5: foundry-prefix leak guard on input side ---
    # Mirrors _scrub()'s output-side _FOUNDRY_LEAK_RE so that names
    # like "tsmc_secret_key" or "Nch_Alpha" cannot enter desVar() /
    # maeSetVar() calls. Prefix-anchored and case-insensitive.

    @pytest.mark.parametrize("name", [
        "tsmc_secret_key",
        "TSMC_LEAK",          # uppercase variant
        "Nch_Alpha",          # nch_ family, mixed case
        "pch_beta",
        "cfmom_x",
        "rppoly_head",
        "rm1_tail",
        "tcbn_foo",
        "tsmcAlpha",          # no underscore after prefix
    ])
    def test_rejects_foundry_prefixes(self, bridge, name):
        assert bridge._is_allowed_param_name(name) is False

    # --- Non-string inputs ---

    @pytest.mark.parametrize("value", [None, 42, [], {}, ("foo",)])
    def test_rejects_non_string(self, bridge, value):
        assert bridge._is_allowed_param_name(value) is False


class TestParamNameCasePreservation:
    """Regression tests: Layer 2 names must retain original case in
    the SKILL call strings emitted to remote host. Maestro/OCEAN desVars are
    case-sensitive; lowercasing would silently write to the wrong var.
    Scope: OCEAN + Maestro paths only (see Stage 0 §1.5).
    """

    def test_run_ocean_sim_preserves_case(self, bridge, monkeypatch):
        bridge._skill_loaded = True  # force the SKILL path
        # Stage 1 rev 3 (2026-04-18): run_ocean_sim now makes TWO SKILL
        # calls (safeOceanRun then safeOceanMeasure). Capture every call
        # and assert against the one that carries design_vars.
        captured: list[str] = []

        def fake_exec_json(self_, expr, timeout=None):
            captured.append(expr)
            if expr.startswith("safeOceanRun"):
                return {
                    "ok": True,
                    "resultsDir": "/tmp/r",
                    "varsApplied": 2,
                    "analyses": ["tran"],
                }
            return {"ok": True, "metrics": {}}

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec_json)

        bridge.run_ocean_sim(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            design_vars={"Ibias": "500u", "R0": "10k"},
            analyses=["tran"],
        )

        run_exprs = [e for e in captured if e.startswith("safeOceanRun")]
        assert len(run_exprs) == 1
        expr = run_exprs[0]
        # Original case present in the emitted SKILL expression
        assert '"Ibias"' in expr
        assert '"R0"'    in expr
        # Lowercased form NOT present (regression guard)
        assert '"ibias"' not in expr
        assert '"r0"'    not in expr

    def test_write_and_save_maestro_preserves_case(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        bridge.set_scope(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb")
        captured = {}

        def fake_exec_json(self_, expr):
            captured["expr"] = expr
            # Stage 1 rev 1 (2026-04-18): write_and_save_maestro now
            # raises on saved=False (absorbed from deleted MaestroWriter
            # middle layer), so happy-path mocks must set saved=True.
            return {"ok": True, "wrote": 2, "saved": True}

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec_json)

        bridge.write_and_save_maestro(
            design_vars={"Ibias": "500u", "nfin_cc": 12},
        )

        expr = captured["expr"]
        assert '"Ibias"'    in expr
        assert '"nfin_cc"'  in expr
        assert '"ibias"'    not in expr
        # R12: SKILL call must target TB cell, not DUT cell
        assert '"LC_VCO_tb"' in expr
        assert 'safeMaeWriteAndSave("pll" "LC_VCO_tb"' in expr

    def test_write_and_save_maestro_requires_tb_cell(self, bridge):
        """set_scope without tb_cell must make write_and_save_maestro
        fail-fast with a clear error mentioning tb_cell."""
        bridge._skill_loaded = True
        bridge.set_scope(lib="pll", cell="LC_VCO")
        with pytest.raises(RuntimeError, match="tb_cell"):
            bridge.write_and_save_maestro(
                design_vars={"nfin_cc": 12},
            )


class TestWhitespacePaddedNamesRejected:
    """The upfront strip check in `_is_allowed_param_name` must stop
    whitespace-padded names from ever reaching the SKILL builder.
    Without it, Layer 1's `_normalize_param_name` internal strip() would
    accept " w" and the original whitespace-padded key would be emitted
    to SKILL, where safeHelpers_validateParamName (no strip) would
    reject it late.
    """

    def test_run_ocean_sim_rejects_padded(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        captured = {}

        def fake_exec_json(self_, expr):
            captured["expr"] = expr
            return {"ok": True, "resultsDir": "/tmp/r",
                    "varsApplied": 0, "analyses": []}

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec_json)

        with pytest.raises(ValueError):
            bridge.run_ocean_sim(
                lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
                design_vars={" Ibias": "500u"},  # leading space
                analyses=["tran"],
            )
        # No SKILL expression was built — the guard fired first.
        assert "expr" not in captured

    def test_write_and_save_maestro_rejects_padded(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        bridge.set_scope(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb")
        captured = {}

        def fake_exec_json(self_, expr):
            captured["expr"] = expr
            return {"ok": True, "wrote": 0}

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec_json)

        with pytest.raises(ValueError):
            bridge.write_and_save_maestro(
                design_vars={"W\n": "1u"},  # trailing newline
            )
        assert "expr" not in captured


# ---------------------------------------------------------------- #
#  display_transient_waveform — SKILL construction + scrub
# ---------------------------------------------------------------- #

class TestDisplayTransientWaveform:
    """SafeBridge.display_transient_waveform scrub + SKILL construction."""

    @pytest.fixture
    def wf_bridge(self, pdk_map_file, tmp_path):
        b = SafeBridge(
            MagicMock(),
            pdk_map_file,
            skill_dir=tmp_path / "no_skill",
        )
        b._skill_loaded = True
        return b

    def test_normal_skill_string(self, wf_bridge):
        """Valid psf_dir + successful SKILL result passes without error.

        E1: the 3 statements are wrapped in progn(...) because the RAMIC
        Bridge binds the expression in let((__vb_r <expr>)) which only
        accepts a single expression, not a sequence.
        """
        wf_bridge.client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output="plotHandle:12345"
        )
        wf_bridge.display_transient_waveform(
            "/home/user/sim/LC_VCO_tb/spectre/schematic",
            "/Vout_p", "/Vout_n",
        )
        expr = wf_bridge.client.execute_skill.call_args[0][0]
        assert expr.startswith("progn("), f"E1: expected progn wrap, got: {expr!r}"
        assert expr.endswith(")"), f"E1: expected closing paren, got: {expr!r}"
        assert 'openResults("/home/user/sim/LC_VCO_tb/spectre/schematic")' in expr
        assert "selectResult('tran)" in expr
        assert 'plot(VT("/Vout_p") - VT("/Vout_n"))' in expr

    def test_b3_nets_required_no_defaults(self, wf_bridge):
        """B3: net_pos / net_neg are required positional args; no defaults."""
        with pytest.raises(TypeError):
            wf_bridge.display_transient_waveform("/home/user/sim/out")

    def test_b3_alt_nets_in_skill_string(self, wf_bridge):
        """Non-default nets are correctly interpolated into SKILL output."""
        wf_bridge.client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output="plotHandle:1"
        )
        wf_bridge.display_transient_waveform(
            "/home/user/sim/out",
            "/outP", "/outN",
        )
        expr = wf_bridge.client.execute_skill.call_args[0][0]
        assert 'plot(VT("/outP") - VT("/outN"))' in expr

    def test_unsafe_psf_dir_raises(self, wf_bridge):
        """PSF dir with shell metacharacters raises RuntimeError."""
        with pytest.raises(RuntimeError, match="unsafe characters"):
            wf_bridge.display_transient_waveform(
                "/tmp/sim; rm -rf /", "/Vout_p", "/Vout_n",
            )
        wf_bridge.client.execute_skill.assert_not_called()

    def test_unsafe_net_pos_raises(self, wf_bridge):
        """net_pos with injection characters raises RuntimeError."""
        with pytest.raises(RuntimeError, match="unsafe characters"):
            wf_bridge.display_transient_waveform(
                "/home/user/sim/out",
                '/Vout"); load("evil', "/Vout_n",
            )
        wf_bridge.client.execute_skill.assert_not_called()

    def test_unsafe_net_neg_raises(self, wf_bridge):
        """net_neg with injection characters raises RuntimeError."""
        with pytest.raises(RuntimeError, match="unsafe characters"):
            wf_bridge.display_transient_waveform(
                "/home/user/sim/out", "/Vout_p", "bad net",
            )
        wf_bridge.client.execute_skill.assert_not_called()

    def test_skill_ok_false_raises(self, wf_bridge):
        """SKILL result with ok=False raises RuntimeError."""
        wf_bridge.client.execute_skill.return_value = SimpleNamespace(
            ok=False, errors=["PSF not found"], output=""
        )
        with pytest.raises(RuntimeError, match="SKILL call failed"):
            wf_bridge.display_transient_waveform(
                "/home/user/sim/out", "/Vout_p", "/Vout_n",
            )

    def test_skill_nil_output_raises(self, wf_bridge):
        """SKILL returning 'nil' means the plot failed."""
        wf_bridge.client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output="nil"
        )
        with pytest.raises(RuntimeError, match="failure indicator"):
            wf_bridge.display_transient_waveform(
                "/home/user/sim/out", "/Vout_p", "/Vout_n",
            )

    def test_skill_error_star_output_raises(self, wf_bridge):
        """SKILL returning '*Error* ...' means a SKILL error occurred."""
        wf_bridge.client.execute_skill.return_value = SimpleNamespace(
            ok=True, errors=[], output="*Error* selectResult: no results"
        )
        with pytest.raises(RuntimeError, match="failure indicator"):
            wf_bridge.display_transient_waveform(
                "/home/user/sim/out", "/Vout_p", "/Vout_n",
            )

    def test_execute_skill_exception_propagates(self, wf_bridge):
        """Unlike agent wrapper, bridge method does NOT swallow exceptions."""
        wf_bridge.client.execute_skill.side_effect = ConnectionError("lost")
        with pytest.raises(ConnectionError):
            wf_bridge.display_transient_waveform(
                "/home/user/sim/out", "/Vout_p", "/Vout_n",
            )


# ---------------------------------------------------------------- #
#  B1: read_op_point_after_tran passes psf_dir to SKILL
# ---------------------------------------------------------------- #

class TestReadOpPointAfterTran:
    """B1: SKILL call includes psf_dir so it can openResults itself."""

    @pytest.fixture
    def op_bridge(self, pdk_map_file, tmp_path):
        b = SafeBridge(
            MagicMock(),
            pdk_map_file,
            skill_dir=tmp_path / "no_skill",
        )
        b._skill_loaded = True
        return b

    def test_skill_call_passes_psf_dir(self, op_bridge, monkeypatch):
        """SKILL call is safeReadOpPointAfterTran("<psf_dir>") with scope lib/tb."""
        op_bridge._last_results_dir = "/home/user/sim/LC_VCO_tb/spectre/schematic"
        captured = {}

        def fake_exec(self_, expr):
            captured["expr"] = expr
            return {"analysis": "tranOp", "instances": {}}

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec)
        op_bridge.read_op_point_after_tran()
        assert captured["expr"] == (
            'safeReadOpPointAfterTran("/home/user/sim/LC_VCO_tb/spectre/schematic")'
        )

    def test_missing_psf_dir_raises(self, op_bridge):
        """No prior run_ocean_sim → RuntimeError, no SKILL call."""
        op_bridge._last_results_dir = None
        with pytest.raises(RuntimeError, match="no results dir"):
            op_bridge.read_op_point_after_tran()
        op_bridge.client.execute_skill.assert_not_called()

    def test_unsafe_psf_dir_raises(self, op_bridge):
        """psf_dir with injection chars → RuntimeError, no SKILL call."""
        op_bridge._last_results_dir = "/tmp; load(\"evil\")"
        with pytest.raises(RuntimeError, match="unsafe characters"):
            op_bridge.read_op_point_after_tran()
        op_bridge.client.execute_skill.assert_not_called()


# ---------------------------------------------------------------- #
#  E2: _upload_skill_inline + _load_skill_helpers inline preference
# ---------------------------------------------------------------- #

class TestInlineSkillUpload:
    """E2: PC-side inline SKILL upload so remote host staleness can't mask
    updated procedure bindings (observed in E2E log 2026-04-22:
    safeReadOpPointAfterTran called as 1-arg but remote host still had the
    old 0-arg definition)."""

    @pytest.fixture
    def inline_bridge(self, pdk_map_file, tmp_path):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        b = SafeBridge(
            MagicMock(), pdk_map_file,
            skill_dir=skill_dir,
        )
        return b

    def test_inline_upload_sends_file_contents(self, inline_bridge, tmp_path):
        """Content of the .il file is sent wrapped in progn(...)."""
        il_path = inline_bridge._skill_dir / "helpers.il"
        il_path.write_text(
            "procedure(safeFoo(x) x+1)\n"
            "procedure(safeBar(y) y*2)\n",
            encoding="utf-8",
        )
        inline_bridge.client.execute_skill.reset_mock()
        inline_bridge._upload_skill_inline(il_path)
        assert inline_bridge.client.execute_skill.call_count == 1
        expr = inline_bridge.client.execute_skill.call_args[0][0]
        assert expr.startswith("progn("), f"no progn wrap: {expr[:40]!r}"
        assert expr.endswith(")")
        assert "procedure(safeFoo(x) x+1)" in expr
        assert "procedure(safeBar(y) y*2)" in expr

    def test_inline_upload_rejects_bad_entrypoint(
        self, inline_bridge
    ):
        """E2: .il containing a forbidden primitive (system / popen /
        evalstring / ipcbegin / exec / shell) is refused, no upload."""
        il_path = inline_bridge._skill_dir / "safe_read_op_point.il"
        il_path.write_text(
            "procedure(safeReadOpPointAfterTran(psfDir)\n"
            "    system(\"rm -rf /\")\n"
            ")\n",
            encoding="utf-8",
        )
        inline_bridge.client.execute_skill.reset_mock()
        with pytest.raises(RuntimeError, match="forbidden primitive"):
            inline_bridge._upload_skill_inline(il_path)
        inline_bridge.client.execute_skill.assert_not_called()

    def test_inline_upload_rejects_path_outside_skill_dir(
        self, inline_bridge, tmp_path
    ):
        """Path that does not resolve under self._skill_dir is refused."""
        outside = tmp_path / "outside.il"
        outside.write_text("procedure(rogue(x) x)\n", encoding="utf-8")
        inline_bridge.client.execute_skill.reset_mock()
        with pytest.raises(RuntimeError, match="not under skill_dir"):
            inline_bridge._upload_skill_inline(outside)
        inline_bridge.client.execute_skill.assert_not_called()

    def test_inline_upload_rejects_non_il_extension(
        self, inline_bridge
    ):
        """Files without .il suffix are refused (defense-in-depth)."""
        bad = inline_bridge._skill_dir / "evil.txt"
        bad.write_text("procedure(foo(x) x)\n", encoding="utf-8")
        inline_bridge.client.execute_skill.reset_mock()
        with pytest.raises(RuntimeError, match="not a .il file"):
            inline_bridge._upload_skill_inline(bad)
        inline_bridge.client.execute_skill.assert_not_called()

    def test_load_skill_helpers_prefers_inline(
        self, pdk_map_file, tmp_path
    ):
        """E2: when the .il exists on PC, _load_skill_helpers must
        call _upload_skill_inline — not the legacy load("<path>")."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        for name in ("helpers.il", "safe_read_schematic.il",
                     "safe_read_op_point.il", "safe_set_param.il",
                     "safe_ocean.il", "safe_maestro.il",
                     "safe_patch_netlist.il"):
            (skill_dir / name).write_text(
                f"procedure({name.split('.')[0]}_init() t)\n",
                encoding="utf-8",
            )
        mock_client = MagicMock()
        SafeBridge(mock_client, pdk_map_file, skill_dir=skill_dir)
        # Every execute_skill call should be a progn(...) wrapper —
        # none should be raw load("<remote_path>") since PC has all files.
        calls = [c[0][0] for c in mock_client.execute_skill.call_args_list]
        assert calls, "no execute_skill calls recorded"
        for expr in calls:
            assert expr.startswith("progn("), (
                f"expected inline progn(...), got load()-style: {expr[:60]!r}"
            )

    def test_load_skill_helpers_falls_back_when_pc_missing(
        self, pdk_map_file, tmp_path
    ):
        """If PC-side .il is missing, _load_skill_helpers must fall back
        to the legacy load("<remote_path>") path (backwards compat)."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        # Intentionally do NOT create the files; every script is missing.
        mock_client = MagicMock()
        SafeBridge(
            mock_client, pdk_map_file,
            skill_dir=skill_dir, remote_skill_dir="/remote/skill",
        )
        calls = [c[0][0] for c in mock_client.execute_skill.call_args_list]
        assert calls, "no execute_skill calls recorded"
        # All calls must be load("/remote/skill/...") — nothing inline.
        for expr in calls:
            assert expr.startswith('load("/remote/skill/'), (
                f"expected load() fallback, got: {expr[:60]!r}"
            )

    # -------- Round-2 regressions: strip SKILL comments before lint -------- #

    def test_inline_upload_accepts_real_safe_ocean_il(
        self, pdk_map_file, tmp_path
    ):
        """Regression: safe_ocean.il has 'evalstring(' in a documentation
        comment (L87 historically). Forbidden-primitive lint must strip
        SKILL ';' comments before matching, otherwise the real file
        bounces to the legacy load() path — exactly the remote host staleness
        E2 is designed to avoid."""
        real_path = PROJECT_ROOT / "skill" / "safe_ocean.il"
        assert real_path.exists(), "safe_ocean.il missing in repo"
        # Build a bridge pointing at the real skill_dir so the path-under-
        # skill_dir containment check passes.
        mock_client = MagicMock()
        bridge = SafeBridge(
            mock_client, pdk_map_file,
            skill_dir=PROJECT_ROOT / "skill",
        )
        mock_client.execute_skill.reset_mock()
        bridge._upload_skill_inline(real_path)
        assert mock_client.execute_skill.call_count == 1
        expr = mock_client.execute_skill.call_args[0][0]
        assert expr.startswith("progn(")
        # Comments are preserved in the payload (we strip only for lint).
        assert "evalstring" in expr

    def test_forbidden_in_comment_not_blocked(
        self, inline_bridge
    ):
        """A forbidden primitive mentioned inside a SKILL comment does not
        trigger the lint — the strip-comments-first step ensures docs and
        example text cannot false-positive."""
        il_path = inline_bridge._skill_dir / "safe_doc.il"
        il_path.write_text(
            "; system(\"rm -rf /\") -- this is docs, not a call\n"
            "procedure(safeFoo() t)\n",
            encoding="utf-8",
        )
        inline_bridge.client.execute_skill.reset_mock()
        inline_bridge._upload_skill_inline(il_path)
        assert inline_bridge.client.execute_skill.call_count == 1
        expr = inline_bridge.client.execute_skill.call_args[0][0]
        # The comment is still present in what we actually send to SKILL.
        assert "system(" in expr

    def test_forbidden_outside_comment_still_blocked(
        self, inline_bridge
    ):
        """Strip-comments-first must not weaken the real block: a
        forbidden primitive in procedure-body code still raises."""
        il_path = inline_bridge._skill_dir / "safe_evil.il"
        il_path.write_text(
            "procedure(safeEvil() system(\"x\"))\n",
            encoding="utf-8",
        )
        inline_bridge.client.execute_skill.reset_mock()
        with pytest.raises(RuntimeError, match="forbidden primitive"):
            inline_bridge._upload_skill_inline(il_path)
        inline_bridge.client.execute_skill.assert_not_called()


# ---------------------------------------------------------------- #
#  Task F-B (2026-04-22): generate_spec_scaffold wrapper
# ---------------------------------------------------------------- #

class TestGenerateSpecScaffold:
    """SafeBridge.generate_spec_scaffold wrapper contract."""

    def _fake_scaffold_payload(self) -> dict:
        return {
            "ok": True,
            "dut": {
                "lib": "GENERIC_PDK",
                "cell": "LC_VCO",
                "pins": [
                    {"name": "Vout_p", "direction": "output"},
                    {"name": "vdd",    "direction": "inputOutput"},
                ],
            },
            "tb": {
                "lib": "GENERIC_PDK",
                "cell": "LC_VCO_tb",
                "pins": [
                    {"name": "vdd", "direction": "inputOutput"},
                ],
            },
        }

    def test_returns_expected_shape(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: self._fake_scaffold_payload(),
        )
        result = bridge.generate_spec_scaffold(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
        )
        assert result["lib"] == "pll"
        assert result["cell"] == "LC_VCO"
        assert result["tb_cell"] == "LC_VCO_tb"
        assert result["dut"]["lib"] == "GENERIC_PDK"
        assert len(result["dut"]["pins"]) == 2
        assert result["design_vars"] == []
        assert result["analyses"] == []

    def test_skill_call_shape(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        captured = {}

        def fake_exec(self_, expr):
            captured["expr"] = expr
            return self._fake_scaffold_payload()

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec)
        bridge.generate_spec_scaffold(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
        )
        assert captured["expr"] == (
            'safeGenerateSpecScaffold("pll" "LC_VCO" "LC_VCO_tb")'
        )

    def test_ok_false_raises(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {"ok": False, "error": "bad cell"},
        )
        with pytest.raises(RuntimeError, match="safeGenerateSpecScaffold"):
            bridge.generate_spec_scaffold(
                lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            )

    def test_requires_skill_loaded(self, bridge):
        bridge._skill_loaded = False
        with pytest.raises(RuntimeError, match="SKILL helpers"):
            bridge.generate_spec_scaffold(
                lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            )

    def test_invalid_lib_name_rejected(self, bridge):
        bridge._skill_loaded = True
        with pytest.raises(ValueError, match="lib"):
            bridge.generate_spec_scaffold(
                lib="bad lib!", cell="LC_VCO", tb_cell="LC_VCO_tb",
            )

    def test_invalid_tb_cell_name_rejected(self, bridge):
        bridge._skill_loaded = True
        with pytest.raises(ValueError, match="tb_cell"):
            bridge.generate_spec_scaffold(
                lib="pll", cell="LC_VCO", tb_cell='bad"tb',
            )

    def test_malformed_dut_raises(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {"ok": True, "dut": "not a dict", "tb": {}},
        )
        with pytest.raises(RuntimeError, match="malformed"):
            bridge.generate_spec_scaffold(
                lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            )

    def test_malformed_tb_raises(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {
                "ok": True,
                "dut": {"lib": "GENERIC_PDK", "cell": "X", "pins": []},
                "tb": "not a dict",
            },
        )
        with pytest.raises(RuntimeError, match="malformed"):
            bridge.generate_spec_scaffold(
                lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            )

    def test_sanitizer_drops_injection_pin_names(self, bridge, monkeypatch):
        """Pin names that fail the safe-identifier pattern are silently
        dropped by the PC-side sanitizer — defense in depth against a
        compromised SKILL payload."""
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {
                "ok": True,
                "dut": {
                    "lib": "GENERIC_PDK", "cell": "X",
                    "pins": [
                        {"name": "Vout_p", "direction": "output"},
                        {"name": 'bad" injection', "direction": "output"},
                        {"name": "", "direction": "output"},
                    ],
                },
                "tb": {"lib": "GENERIC_PDK", "cell": "Y", "pins": []},
            },
        )
        result = bridge.generate_spec_scaffold(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
        )
        pin_names = [p["name"] for p in result["dut"]["pins"]]
        assert pin_names == ["Vout_p"]

    def test_sanitizer_pins_lib_to_generic(self, bridge, monkeypatch):
        """Even if SKILL returned a non-generic lib name, PC pins it
        to GENERIC_PDK."""
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {
                "ok": True,
                "dut": {"lib": "some_real_lib", "cell": "X", "pins": []},
                "tb": {"lib": "other_real_lib", "cell": "Y", "pins": []},
            },
        )
        result = bridge.generate_spec_scaffold(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
        )
        assert result["dut"]["lib"] == "GENERIC_PDK"
        assert result["tb"]["lib"] == "GENERIC_PDK"

    def test_scs_path_triggers_list_design_vars(self, bridge, monkeypatch):
        """When scs_path is given, wrapper calls list_design_vars /
        list_analyses and threads their results through."""
        bridge._skill_loaded = True

        def fake_exec(self_, expr):
            if expr.startswith("safeGenerateSpecScaffold"):
                return self._fake_scaffold_payload()
            if expr.startswith("safeOceanListDesignVars"):
                return {
                    "ok": True,
                    "vars": [{"name": "Ibias", "default": "500u"}],
                }
            if expr.startswith("safeOceanListAnalyses"):
                return {
                    "ok": True,
                    "analyses": [{
                        "name": "tran",
                        "kwargs": [{"key": "stop", "value": "200n"}],
                    }],
                }
            raise AssertionError(f"unexpected SKILL call: {expr}")

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec)
        result = bridge.generate_spec_scaffold(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            scs_path="/tmp/input.scs",
        )
        assert result["design_vars"] == [
            {"name": "Ibias", "default": "500u"}
        ]
        assert result["analyses"][0]["name"] == "tran"
        assert result["analyses"][0]["kwargs"] == [("stop", "200n")]

    def test_scs_failure_does_not_abort(self, bridge, monkeypatch):
        """A list_design_vars / list_analyses error must NOT abort the
        scaffold — the dut/tb pin data is still useful on its own."""
        bridge._skill_loaded = True

        def fake_exec(self_, expr):
            if expr.startswith("safeGenerateSpecScaffold"):
                return self._fake_scaffold_payload()
            # Both list_* helpers return ok=False to trigger the except
            return {"ok": False, "error": "scs not found"}

        monkeypatch.setattr(SafeBridge, "_execute_skill_json", fake_exec)
        result = bridge.generate_spec_scaffold(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            scs_path="/tmp/missing.scs",
        )
        assert len(result["dut"]["pins"]) == 2
        assert result["design_vars"] == []
        assert result["analyses"] == []

    def test_entrypoint_in_allowlist(self):
        """Regression guard: the new SKILL entrypoint must appear in the
        frozen allow-list, else _execute_skill_json rejects every call."""
        from src.safe_bridge import _ALLOWED_SKILL_ENTRYPOINTS
        assert "safeGenerateSpecScaffold" in _ALLOWED_SKILL_ENTRYPOINTS

    def test_safe_spec_scaffold_il_in_helper_list(self):
        """Regression guard: safe_spec_scaffold.il is in the
        _load_skill_helpers script list so E2 inline upload ships it."""
        bridge_source = (
            PROJECT_ROOT / "src" / "safe_bridge.py"
        ).read_text(encoding="utf-8")
        assert '"safe_spec_scaffold.il"' in bridge_source


class TestSafeBridgeFindInputScs:
    """find_input_scs: auto-discover Maestro input.scs on remote host."""

    def test_requires_skill_loaded(self, bridge):
        bridge._skill_loaded = False
        with pytest.raises(RuntimeError, match="find_input_scs"):
            bridge.find_input_scs(lib="pll", tb_cell="LC_VCO_tb")

    def test_invalid_lib_rejected(self, bridge):
        bridge._skill_loaded = True
        with pytest.raises(ValueError):
            bridge.find_input_scs(lib="bad lib!", tb_cell="LC_VCO_tb")

    def test_invalid_tb_cell_rejected(self, bridge):
        bridge._skill_loaded = True
        with pytest.raises(ValueError):
            bridge.find_input_scs(lib="pll", tb_cell='bad"tb')

    def test_empty_lib_rejected(self, bridge):
        bridge._skill_loaded = True
        with pytest.raises(ValueError):
            bridge.find_input_scs(lib="", tb_cell="LC_VCO_tb")

    def test_returns_none_when_no_candidates(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {
                "ok": False,
                "error": "no input.scs found for pll/LC_VCO_tb under /home/x/simulation",
            },
        )
        result = bridge.find_input_scs(lib="pll", tb_cell="LC_VCO_tb")
        assert result is None

    def test_other_skill_errors_raise(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {"ok": False, "error": "HOME env var not set"},
        )
        with pytest.raises(RuntimeError, match="safeMaeFindInputScs failed"):
            bridge.find_input_scs(lib="pll", tb_cell="LC_VCO_tb")

    def test_happy_path_maestro_tier(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        expected_path = (
            "/home/u/simulation/pll/LC_VCO_tb/maestro/results/maestro/"
            "ExplorerRun.0/1/pll_LC_VCO_tb_1/netlist/input.scs"
        )
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {
                "ok": True,
                "path": expected_path,
                "tier": "maestro",
                "mtime": 1714000000,
                "numCandidates": 2,
            },
        )
        result = bridge.find_input_scs(lib="pll", tb_cell="LC_VCO_tb")
        assert result == {
            "path": expected_path,
            "tier": "maestro",
            "mtime": 1714000000,
            "num_candidates": 2,
        }

    def test_happy_path_ade_flat_tier(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {
                "ok": True,
                "path": "/home/u/simulation/LC_VCO_tb/spectre/schematic/"
                        "netlist/input.scs",
                "tier": "ade_flat",
                "mtime": 1714001234,
                "numCandidates": 1,
            },
        )
        result = bridge.find_input_scs(lib="pll", tb_cell="LC_VCO_tb")
        assert result["tier"] == "ade_flat"
        assert result["path"].endswith("/spectre/schematic/netlist/input.scs")

    def test_ok_without_path_raises(self, bridge, monkeypatch):
        bridge._skill_loaded = True
        monkeypatch.setattr(
            SafeBridge, "_execute_skill_json",
            lambda self_, expr: {"ok": True, "tier": "maestro"},
        )
        with pytest.raises(RuntimeError, match="no 'path'"):
            bridge.find_input_scs(lib="pll", tb_cell="LC_VCO_tb")

    def test_entrypoint_in_allowlist(self):
        from src.safe_bridge import _ALLOWED_SKILL_ENTRYPOINTS
        assert "safeMaeFindInputScs" in _ALLOWED_SKILL_ENTRYPOINTS

    def test_safe_mae_find_il_in_helper_list(self):
        bridge_source = (
            PROJECT_ROOT / "src" / "safe_bridge.py"
        ).read_text(encoding="utf-8")
        assert '"safe_mae_find.il"' in bridge_source
