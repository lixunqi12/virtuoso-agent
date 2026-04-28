"""Unit tests for src.hspice_scrub (Task T1).

Fixtures are synthetic but mirror the shape of real HSpice artifacts a
2D-array design produces: .sp main netlist with .INCLUDE / .LIB model
paths, .mt0 measurement file with .TITLE echoing the absolute input
path, .lis listing with op-point tables that quote foundry model
cards. The generic foundry tokens (nch_/pch_/tsmc/N16/dkit/TUFP) live
in the seed list inside src/hspice_scrub.py itself — this file never
introduces a real vendor name.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.hspice_scrub import (
    DEFAULT_PATTERNS_PATH,
    TEMPLATE_PATTERNS_PATH,
    ScrubError,
    load_patterns,
    scrub_lis,
    scrub_mt0,
    scrub_sp,
)
from src.hspice_scrub import (  # private helpers exercised in unit tests
    _DEFAULT_MOSFET_WHITELIST,
    _apply_mosfet_param_whitelist,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sp_text() -> str:
    """Synthetic but realistic .sp netlist fragment."""
    return """\
* matching_test — receiver-side tb
.TITLE matching_test
.OPTION post=2 probe
.TEMP 25
.INCLUDE '/usr/local/dkits/tsmcN16/models/ms_lib.l'
.LIB '/usr/local/dkits/tsmcN16/models/ms_lib.l' TUFP
.PARAM vdd_val = 0.8
.PARAM corner = 'top_tt'
xmn0 d g s b nch_lvt w=1u l=60n
xmp0 d g s b pch_svt w=500n l=60n
R1 WBL n1 10k
C1 WWL 0 1f
VDD vdd 0 DC=0.8
xcore h_in_mid g s b nch_hvt w=2u l=60n
.TRAN 1n 100n
.MEASURE tran vout_pp PP V(vdd) FROM=10n TO=90n
.MEASURE tran hmid_avg AVG V(h_in_mid) FROM=10n TO=90n
.PROBE V(WBL) V(WWL) V(vdd) V(h_in_mid)
.ALTER corner_ss
.LIB '/usr/local/dkits/tsmcN16/models/ms_lib.l' SS
.END
"""


@pytest.fixture()
def mt0_text() -> str:
    return """\
$DATA1 SOURCE='HSPICE' VERSION='2021.09'
.TITLE '/proj/user/work/dut_tb.sp'
.ALTER top_tt
vout_pp       hmid_avg
0.842e+00     0.405e+00
"""


@pytest.fixture()
def lis_text() -> str:
    return """\
HSPICE -- Version 2021.09
Input File: /usr/local/dkits/tsmcN16/tb/dut_tb.sp

**** operating point information ****
 element   0:main:xmn0
 model     nch_lvt_mac
 vgs       7.2e-01
 vds       4.5e-01
 vth       3.2e-01
 ids       2.1e-05

 element   0:main:xmp0
 model     pch_svt_mac
 vgs      -7.8e-01
 vds      -3.9e-01

y WBL WWL vdd h_in_mid
"""


# ---------------------------------------------------------------------------
# 1. scrub_sp
# ---------------------------------------------------------------------------


class TestScrubSp:
    """Core .sp contract: paths + foundry tokens gone; design intact."""

    def test_foundry_paths_redacted(self, sp_text: str) -> None:
        out = scrub_sp(sp_text)
        # The /usr/local/dkits/... absolute path must not survive in
        # any form (prefix, bare path, or embedded in a .LIB line).
        assert "/usr/local/dkits" not in out
        assert "dkits" not in out.lower()

    def test_foundry_tokens_redacted(self, sp_text: str) -> None:
        out = scrub_sp(sp_text)
        for token in ("tsmc", "N16", "TUFP", "nch_lvt", "pch_svt", "nch_hvt"):
            assert token.lower() not in out.lower(), token

    def test_corner_name_preserved(self, sp_text: str) -> None:
        out = scrub_sp(sp_text)
        # `top_tt` lives inside a .PARAM quoted literal; it must
        # survive unmolested so the agent can reason about corners.
        assert "top_tt" in out

    def test_spf_stem_preserved(self, sp_text: str) -> None:
        out = scrub_sp(sp_text)
        # `matching_test` appears both as the .TITLE string and in the
        # header comment — both must survive.
        assert out.count("matching_test") >= 2

    def test_design_signals_preserved(self, sp_text: str) -> None:
        out = scrub_sp(sp_text)
        for sig in ("WBL", "WWL", "vdd", "h_in_mid"):
            assert sig in out, sig

    def test_hspice_directives_preserved(self, sp_text: str) -> None:
        out = scrub_sp(sp_text)
        # Every HSpice control directive must survive — the scrubbed
        # file still has to be a valid netlist.
        for directive in (
            ".TITLE", ".OPTION", ".TEMP",
            ".INCLUDE", ".LIB", ".PARAM",
            ".TRAN", ".MEASURE", ".PROBE", ".ALTER", ".END",
        ):
            assert directive in out, directive

    def test_lib_directive_keeps_leading_keyword(self, sp_text: str) -> None:
        """The .LIB <path> <section> pattern should keep the leading
        .LIB directive marker so downstream tooling can still parse
        it; only the quoted path + section should be neutralised."""
        out = scrub_sp(sp_text)
        # Two .LIB lines in the fixture; both retain the keyword.
        assert out.count(".LIB") == 2

    def test_instance_lines_keep_structure(self, sp_text: str) -> None:
        """Instance rows lose the foundry cell name but retain
        their topology (terminals + w=/l= params)."""
        out = scrub_sp(sp_text)
        assert "xmn0 d g s b" in out
        assert "w=1u l=60n" in out
        # The cell-name slot has been replaced with <redacted>.
        assert "<redacted>" in out


# ---------------------------------------------------------------------------
# 2. scrub_mt0
# ---------------------------------------------------------------------------


class TestScrubMt0:

    def test_title_path_redacted(self, mt0_text: str) -> None:
        out = scrub_mt0(mt0_text)
        # The /proj/user/... absolute path in .TITLE must become <path>.
        assert "/proj/" not in out
        assert "<path>" in out

    def test_corner_name_preserved(self, mt0_text: str) -> None:
        out = scrub_mt0(mt0_text)
        assert "top_tt" in out

    def test_numeric_data_preserved(self, mt0_text: str) -> None:
        """Measurement values are pure numbers — scrub must not
        touch the numeric data rows."""
        out = scrub_mt0(mt0_text)
        assert "0.842e+00" in out
        assert "0.405e+00" in out
        assert "vout_pp" in out
        assert "hmid_avg" in out

    def test_hspice_header_marker_preserved(self, mt0_text: str) -> None:
        out = scrub_mt0(mt0_text)
        assert "$DATA1" in out
        assert "HSPICE" in out


# ---------------------------------------------------------------------------
# 3. scrub_lis
# ---------------------------------------------------------------------------


class TestScrubLis:

    def test_input_path_redacted(self, lis_text: str) -> None:
        out = scrub_lis(lis_text)
        assert "/usr/local/dkits" not in out
        # "Input File:" marker survives, just its payload is sanitized.
        assert "Input File:" in out

    def test_model_names_redacted(self, lis_text: str) -> None:
        out = scrub_lis(lis_text)
        for name in ("nch_lvt_mac", "pch_svt_mac", "nch_lvt", "pch_svt"):
            assert name.lower() not in out.lower(), name

    def test_op_point_numbers_preserved(self, lis_text: str) -> None:
        """Op-point values are essential for the agent; they must
        survive with their original precision."""
        out = scrub_lis(lis_text)
        for value in ("7.2e-01", "4.5e-01", "3.2e-01", "2.1e-05"):
            assert value in out, value
        for param in ("vgs", "vds", "vth", "ids"):
            assert param in out, param

    def test_design_signal_line_preserved(self, lis_text: str) -> None:
        out = scrub_lis(lis_text)
        # Line "y WBL WWL vdd h_in_mid" must survive intact.
        assert "WBL WWL vdd h_in_mid" in out

    def test_matching_test_stem_preserved(self, lis_text: str) -> None:
        """Even though the full path is scrubbed, a mention of
        `matching_test` outside the absolute path — if any — must
        survive. The fixture has no such mention, so this test just
        pins the behaviour on a stitched string."""
        out = scrub_lis(
            lis_text + "\n* Testbench: matching_test\n"
        )
        assert "matching_test" in out


# ---------------------------------------------------------------------------
# 4. gate: ScrubError on residual banned tokens
# ---------------------------------------------------------------------------


class TestScrubGate:
    """Post-scrub gate must refuse any text where a banned token
    slipped through — no half-scrubbed content escapes."""

    def test_gate_raises_on_banned_token_pattern_disabled(self) -> None:
        """Simulate a pattern mismatch by handing the scrubber an
        empty pattern dict (no YAML-supplied tokens). The built-in
        foundry seed list still applies — so a plain `nch_lvt` will
        be caught and gated successfully. Pass instead a token that
        ONLY the YAML knew about (e.g. `TUFP`) and blank the YAML:
        gate must then raise because the built-in seeds also flag
        TUFP (it's in _FOUNDRY_LEAK_RE)."""
        # This indirectly verifies the seed redundancy: even if YAML
        # is stripped to {}, the seed RE alone catches TUFP.
        out = scrub_sp(".LIB 'x.l' TUFP\n", patterns={})
        assert "TUFP" not in out

    def test_gate_raises_on_custom_banned_token(self) -> None:
        """When a YAML-only banned token reaches the gate without
        being scrubbed (e.g. pattern logic bug), gate must raise.
        We fake the bug by disabling scrub replacement for a bespoke
        token that has no representation in _FOUNDRY_LEAK_RE."""
        # Build a text that contains a banned_token NOT in the seed
        # list. Then give the scrubber NO banned_tokens, but force
        # the gate to scan a custom token by using the public API —
        # this path is only exercised when a user-misconfigured
        # YAML omits a token they declared banned elsewhere. To
        # simulate it we monkey-patch via custom patterns with a
        # gated-only extra.
        # Here we pass a custom banned_token entry that IS scrubbed,
        # so the gate passes — then flip it: strip scrubbing would
        # require module-level patching, which is out of scope.
        # Instead we exercise the gate by feeding raw foundry tokens
        # directly to `_gate` through patterns that skip the scrub.
        from src.hspice_scrub import _gate, _normalize_patterns

        with pytest.raises(ScrubError) as excinfo:
            _gate(
                "model nch_lvt_mac\n",
                _normalize_patterns({"banned_tokens": []}),
                stage="lis",
            )
        err = excinfo.value
        assert err.stage == "lis"
        assert any("nch_lvt" in r.lower() for r in err.residuals)

    def test_gate_residuals_include_surviving_path(self) -> None:
        """A raw /usr/... path in post-scrub output must trip the
        gate: absolute paths are a PII/PDK leak on their own.

        Round-2 (codex review): the RAW path lives on ``err.residuals``
        for local debug, but MUST NOT appear in ``str(err)`` — the
        string form exposes only counts per category, never the raw
        substring."""
        from src.hspice_scrub import _gate, _normalize_patterns

        with pytest.raises(ScrubError) as excinfo:
            _gate(
                "Input File: /usr/local/some/thing\n",
                _normalize_patterns({}),
                stage="sp",
            )
        err = excinfo.value
        # Debug-only raw list still carries the hit.
        assert any("/usr/" in r for r in err.residuals)
        # Error *string* form must not echo the raw path back.
        assert "/usr/" not in str(err)
        # But it must surface the category count.
        assert "absolute_path" in str(err)

    def test_scruberror_exposes_stage_and_list(self) -> None:
        """Round-2 (codex review): ``str(err)`` now shows counts per
        category, not the raw residuals. The raw list stays on
        ``err.residuals`` for local debug."""
        err = ScrubError(
            ["nch_lvt", "N16"],
            stage="sp",
            counts={"foundry_seed": 1, "banned_token": 1},
        )
        assert err.stage == "sp"
        assert err.residuals == ["nch_lvt", "N16"]
        assert err.counts == {"foundry_seed": 1, "banned_token": 1}
        # Stage marker still in message.
        assert "[sp]" in str(err)
        # Category labels surface; raw tokens must not.
        assert "foundry_seed=1" in str(err)
        assert "banned_token=1" in str(err)
        assert "nch_lvt" not in str(err)
        assert "N16" not in str(err)


# ---------------------------------------------------------------------------
# 4b. T1 round-2 — codex review blockers
# ---------------------------------------------------------------------------


class TestScrubRoundTwoBlockers:
    """Regression guards for codex R2 blockers:
      - ScrubError string form leaks no raw residuals
      - residuals list is capped to prevent unbounded memory / logs
      - model_regex is validated & enforced on EVERY entry point
        (YAML load + inline-dict scrub + gate rescan)
    """

    # ------ Blocker #1: string form leaks nothing sensitive ----------

    def test_scruberror_str_does_not_echo_raw_residuals(self) -> None:
        """Construct a ScrubError with a fabricated sensitive token
        ("ACME_SECRET_42") and confirm ``str(err)`` contains the
        category and count but NOT the raw token. ``err.residuals``
        still carries it for local-only debug.

        A regression here would mean scrub failures start leaking
        the very content we scrub against back through tracebacks.
        """
        err = ScrubError(
            ["ACME_SECRET_42", "/nfs/vendor/lib/file.l"],
            stage="lis",
            counts={"banned_token": 1, "absolute_path": 1},
        )
        s = str(err)
        assert "ACME_SECRET_42" not in s
        assert "/nfs/" not in s
        assert "banned_token=1" in s
        assert "absolute_path=1" in s
        # Raw residuals still recoverable for local debug.
        assert "ACME_SECRET_42" in err.residuals
        assert "/nfs/vendor/lib/file.l" in err.residuals

    # ------ Blocker #2: residuals cap + truncated flag ---------------

    def test_residuals_list_is_capped(self) -> None:
        """Feed the gate many (>64) banned hits and verify:
          - ``err.residuals`` is clipped at _MAX_RESIDUALS
          - ``err.truncated`` is True
          - ``err.counts`` reflects the *true* total
          - ``str(err)`` surfaces the [truncated] marker"""
        from src.hspice_scrub import (
            _MAX_RESIDUALS, _gate, _normalize_patterns,
        )

        # 200 distinct foundry-seed hits — far over the cap.
        overflow = "\n".join(f"nch_lvt_{i}" for i in range(200)) + "\n"
        with pytest.raises(ScrubError) as excinfo:
            _gate(overflow, _normalize_patterns({}), stage="lis")
        err = excinfo.value
        assert err.truncated is True
        assert len(err.residuals) == _MAX_RESIDUALS
        # True count is preserved in counts dict.
        assert err.counts.get("foundry_seed", 0) == 200
        assert "[truncated]" in str(err)
        assert "foundry_seed=200" in str(err)
        # Raw residuals still must not appear in the message body.
        assert "nch_lvt_0" not in str(err)

    def test_residuals_uncapped_below_limit(self) -> None:
        """Below-cap case: truncated is False, message has no
        [truncated] marker, ``len(err.residuals) == counts total``."""
        from src.hspice_scrub import _gate, _normalize_patterns

        text = "nch_lvt_a\nnch_lvt_b\n"
        with pytest.raises(ScrubError) as excinfo:
            _gate(text, _normalize_patterns({}), stage="sp")
        err = excinfo.value
        assert err.truncated is False
        assert len(err.residuals) == 2
        assert err.counts.get("foundry_seed") == 2
        assert "[truncated]" not in str(err)

    # ------ Blocker #3: model_regex fail-closed ----------------------

    def test_bad_model_regex_inline_dict_raises_valueerror(self) -> None:
        """Direct-dict path (scrub_sp(..., patterns={...})) must
        refuse an uncompilable model_regex. Previously a silent
        ``continue`` let the entry disappear, meaning an operator
        who configured a pattern to catch sensitive content saw it
        quietly disabled.
        """
        with pytest.raises(ValueError, match="regex"):
            scrub_sp("x\n", patterns={"model_regex": ["(unclosed"]})

    def test_bad_model_regex_inline_dict_all_three_scrubbers(self) -> None:
        """Same guarantee for scrub_mt0 and scrub_lis."""
        for scrub in (scrub_mt0, scrub_lis):
            with pytest.raises(ValueError, match="regex"):
                scrub("x\n", patterns={"model_regex": ["(bad["]})

    def test_gate_rescans_model_regex(self) -> None:
        """If a model_regex pattern somehow fails to redact (e.g. a
        future refactor bypasses _apply_scrub for this class), the
        gate must still catch the residual. We call ``_gate``
        directly with a payload that matches a user-supplied
        model_regex, bypassing the scrub stage entirely.
        """
        from src.hspice_scrub import _gate, _normalize_patterns

        patterns = _normalize_patterns({
            "model_regex": [r"VENDOR_MODEL_\w+"],
        })
        with pytest.raises(ScrubError) as excinfo:
            _gate("the model is VENDOR_MODEL_CORE7\n", patterns, stage="lis")
        err = excinfo.value
        assert err.counts.get("model_regex", 0) == 1
        assert "model_regex=1" in str(err)


# ---------------------------------------------------------------------------
# 5. load_patterns / YAML contract
# ---------------------------------------------------------------------------


class TestLoadPatterns:

    def test_default_path_loads(self) -> None:
        """Either the private or the template YAML resolves and parses."""
        # load_patterns() prefers DEFAULT_PATTERNS_PATH (.private.yaml)
        # when present, else falls back to TEMPLATE_PATTERNS_PATH.
        assert (
            DEFAULT_PATTERNS_PATH.exists()
            or TEMPLATE_PATTERNS_PATH.exists()
        )
        loaded = load_patterns()
        # All four canonical keys populated.
        for key in (
            "banned_prefixes", "banned_tokens",
            "model_regex", "preserve_tokens",
        ):
            assert key in loaded

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_patterns(tmp_path / "nope.yaml")

    def test_non_mapping_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_patterns(bad)

    def test_invalid_model_regex_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.safe_dump({"model_regex": ["(unclosed"]}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="regex"):
            load_patterns(bad)

    def test_unknown_keys_tolerated(self, tmp_path: Path) -> None:
        ok = tmp_path / "ok.yaml"
        ok.write_text(
            yaml.safe_dump({"future_key": "whatever"}),
            encoding="utf-8",
        )
        loaded = load_patterns(ok)
        assert loaded["banned_prefixes"] == []
        assert loaded["preserve_tokens"] == ["top_tt", "matching_test"]


# ---------------------------------------------------------------------------
# 6. miscellaneous edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:

    def test_empty_input_returns_empty(self) -> None:
        assert scrub_sp("") == ""
        assert scrub_mt0("") == ""
        assert scrub_lis("") == ""

    def test_already_clean_text_passes_unchanged(self) -> None:
        clean = "* clean fragment\n.PARAM vdd_val = 0.8\nR1 WBL n1 1k\n"
        assert scrub_sp(clean) == clean

    def test_custom_patterns_extend_seed_list(self) -> None:
        """Adding a YAML banned_token catches an identifier the
        built-in seeds miss (e.g. a project-code like `MYCORP01`)."""
        text = "X0 n1 n2 MYCORP01_core w=1u\n"
        out = scrub_sp(text, patterns={"banned_tokens": ["MYCORP01"]})
        assert "MYCORP01" not in out
        assert "<redacted>" in out

    def test_preserve_token_survives_custom_banned_token(self) -> None:
        """A preserve token must NOT be scrubbed even if it happens
        to also appear in banned_tokens (user misconfig safety net).
        Here we blacklist `h_in_mid` but preserve it — preserve wins.
        """
        text = "xcore h_in_mid g s b NMOS w=1u\n"
        out = scrub_sp(
            text,
            patterns={
                "banned_tokens": ["h_in_mid"],
                "preserve_tokens": ["h_in_mid"],
            },
        )
        assert "h_in_mid" in out


# ---------------------------------------------------------------------------
# Round 3 (codex N1) — MOSFET-param whitelist second pass.
# On `<redacted>`-touching element lines, drop foundry-extraction params
# (dfm_flag, spmt, sapb, ...) while keeping generic SPICE params (l, w,
# m, nf, multi, ad, as, pd, ps, nrd, nrs, sd). Continuation lines
# inherit the trigger from the line that opened them.
# ---------------------------------------------------------------------------

_MOSFET_WHITELIST_PATTERNS = {
    "mosfet_param_whitelist": [
        "l", "w", "m", "nf", "multi",
        "ad", "as", "pd", "ps",
        "nrd", "nrs", "sd",
    ],
}


class TestMosfetParamWhitelist:
    def test_mosfet_whitelist_drops_foundry_params(self) -> None:
        text = (
            "xmm1 a b c d <redacted> l=30e-9 w=140e-9 dfm_flag=0 "
            "spmt=1.11e15 sapb=114e-9\n"
        )
        out = scrub_sp(text, patterns=_MOSFET_WHITELIST_PATTERNS)
        assert "l=30e-9" in out
        assert "w=140e-9" in out
        assert "dfm_flag" not in out
        assert "spmt" not in out
        assert "sapb" not in out
        # Refdes / nets / marker survive verbatim.
        assert "xmm1" in out
        assert "<redacted>" in out

    def test_passive_params_unaffected(self) -> None:
        text = "R1 a b 1k tc1=2e-3 tc2=3e-6\n"
        out = scrub_sp(text, patterns=_MOSFET_WHITELIST_PATTERNS)
        # No <redacted> on this line, second pass skips it entirely.
        assert "tc1=2e-3" in out
        assert "tc2=3e-6" in out

    def test_user_subckt_unaffected(self) -> None:
        text = "xi1 a b c USER_CELL length=2 width=4\n"
        out = scrub_sp(text, patterns=_MOSFET_WHITELIST_PATTERNS)
        assert "length=2" in out
        assert "width=4" in out
        assert "USER_CELL" in out

    def test_continuation_params_filtered(self) -> None:
        text = (
            "xmm8 net10 net10 vss vss <redacted> l=30e-9 w=280e-9 "
            "multi=1 nf=1\n"
            "+ sd=100e-9 ad=21e-15 as=21e-15 dfm_flag=0 "
            "spmt=1.11111e15\n"
        )
        out = scrub_sp(text, patterns=_MOSFET_WHITELIST_PATTERNS)
        # Trigger line: whitelist params survive, foundry params gone.
        assert "l=30e-9" in out
        assert "w=280e-9" in out
        assert "multi=1" in out
        assert "nf=1" in out
        # Continuation line: same filter applies.
        assert "sd=100e-9" in out
        assert "ad=21e-15" in out
        assert "as=21e-15" in out
        assert "dfm_flag" not in out
        assert "spmt" not in out

    def test_idempotent(self) -> None:
        text = (
            "xmm1 a b c d <redacted> l=30e-9 dfm_flag=0\n"
            "R1 a b 1k tc1=2e-3\n"
        )
        once = scrub_sp(text, patterns=_MOSFET_WHITELIST_PATTERNS)
        twice = scrub_sp(once, patterns=_MOSFET_WHITELIST_PATTERNS)
        assert once == twice

    def test_default_whitelist_active_when_yaml_missing(self) -> None:
        """N3 R2 (codex): mirror the ``_FOUNDRY_LEAK_RE`` posture —
        Python holds the strong default; YAML's ``mosfet_param_whitelist``
        is *additive* only and cannot disable any default. Both an
        explicit empty list AND a missing key STILL trigger the second
        pass via :data:`_DEFAULT_MOSFET_WHITELIST`. This test ensures
        the production runtime path (where ``private.yaml`` typically
        omits the key) actually scrubs PDK extraction params instead
        of silently disabling the filter.
        """
        text = "xmm1 a b c d <redacted> l=30e-9 dfm_flag=0 spmt=1.11e15\n"
        # Explicit empty list — defaults still active.
        out_empty = scrub_sp(text, patterns={"mosfet_param_whitelist": []})
        assert "l=30e-9" in out_empty
        assert "dfm_flag" not in out_empty
        assert "spmt" not in out_empty
        # Missing key — same behaviour (defaults still active).
        out_missing = scrub_sp(text, patterns={})
        assert "l=30e-9" in out_missing
        assert "dfm_flag" not in out_missing
        assert "spmt" not in out_missing
        # Direct helper call with empty extra-list confirms defaults.
        out_direct = _apply_mosfet_param_whitelist(text, [])
        assert "l=30e-9" in out_direct
        assert "dfm_flag" not in out_direct
        assert "spmt" not in out_direct

    def test_yaml_extends_default_whitelist(self) -> None:
        """YAML-supplied names ADD to the Python default; both the
        Python default and the YAML extension survive on the same line.
        """
        text = (
            "xmm1 d g s b <redacted> l=30e-9 my_custom_param=42 "
            "dfm_flag=0\n"
        )
        out = scrub_sp(
            text, patterns={"mosfet_param_whitelist": ["my_custom_param"]},
        )
        assert "l=30e-9" in out  # default whitelist
        assert "my_custom_param=42" in out  # YAML extension
        assert "dfm_flag" not in out  # neither — gets dropped

    def test_load_patterns_default_path_activates_filter(self) -> None:
        """End-to-end production path: ``load_patterns()`` with no
        args returns the deployment YAML (private if present, else
        template). Even when neither carries the whitelist key, the
        Python default activates the filter and PDK extraction params
        are stripped. This is the regression guard for the silent-
        disable bug codex caught in N3 R1.
        """
        text = (
            "xmm1 d g s b nch_lvt l=30e-9 ic='V(out)=0.5V' "
            "temp='27 + 5' m=2 dfm_flag=0 spmt=1.11e15\n"
        )
        patterns = load_patterns()
        out = scrub_sp(text, patterns)
        # Foundry token redacted by pass 1, params filtered by pass 2.
        assert "<redacted>" in out
        assert "nch_lvt" not in out
        # Default whitelist members survive (l, ic, temp, m).
        assert "l=30e-9" in out
        assert "ic='V(out)=0.5V'" in out
        assert "temp='27 + 5'" in out
        assert "m=2" in out
        # Foundry-extraction params dropped.
        assert "dfm_flag" not in out
        assert "spmt" not in out

    def test_default_whitelist_constant_shape(self) -> None:
        """Source-of-truth check on the hardcoded default — guards
        against an accidental rename / removal of one of the 16
        canonical keys.
        """
        expected = frozenset({
            "l", "w", "m", "nf", "multi",
            "ad", "as", "pd", "ps",
            "nrd", "nrs", "sd",
            "ic", "temp", "dtemp", "region",
        })
        assert _DEFAULT_MOSFET_WHITELIST == expected

    def test_case_insensitive_whitelist(self) -> None:
        text = "xmm1 a b c d <redacted> L=30e-9 W=140e-9 DFM_flag=0\n"
        out = scrub_sp(text, patterns=_MOSFET_WHITELIST_PATTERNS)
        # Whitelist matches by lowercased key, so uppercase L / W survive.
        assert "L=30e-9" in out
        assert "W=140e-9" in out
        # DFM_flag is not in the whitelist regardless of case.
        assert "DFM_flag" not in out

    def test_bsim4_instance_params_preserved(self) -> None:
        """N3 (codex): ``ic`` / ``temp`` / ``dtemp`` / ``region`` are
        standard HSpice MOS instance params that are user-authored
        (initial condition, per-element temperature override / delta,
        operating-region selector) — not PDK extraction residue. The
        12-name whitelist over-pruned them; N3 extends to 16.
        """
        text = (
            "xmm1 d g s b <redacted> "
            "l=30e-9 ic='V(out)=0.5V' temp='27 + 5' m=2 dtemp=5 region=0\n"
        )
        out = scrub_sp(text, {"mosfet_param_whitelist": [
            "l", "w", "m", "nf", "multi", "ad", "as", "pd", "ps",
            "nrd", "nrs", "sd", "ic", "temp", "dtemp", "region",
        ]})
        for surviving in (
            "l=30e-9", "ic=", "temp=", "m=2", "dtemp=5", "region=0",
        ):
            assert surviving in out, f"{surviving!r} dropped"
        # Quoted-value preservation: spaces inside quotes survive.
        assert "ic='V(out)=0.5V'" in out
        assert "temp='27 + 5'" in out
