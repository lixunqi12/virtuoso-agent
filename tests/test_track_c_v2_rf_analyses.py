"""Track C T1 tests for Maestro RF analyses (pss / pnoise)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.maestro_setup import apply_maestro_setup, validate_maestro_setup_block  # noqa: E402
from src.safe_bridge import SafeBridge  # noqa: E402


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


@pytest.fixture
def bridge(pdk_map_file, tmp_path):
    b = SafeBridge(
        MagicMock(), pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
    return b


@pytest.fixture
def writer_mocks():
    with patch("src.safe_bridge._mae_writer.set_analysis") as m_an:
        m_an.return_value = "ok-analysis"
        yield {"set_analysis": m_an}


def _assert_exception_scrubbed(exc_info, token: str) -> None:
    assert token not in str(exc_info.value)
    for arg in exc_info.value.args:
        assert token not in str(arg)


class TestSafeBridgeRFAnalysisNames:
    @pytest.mark.parametrize("analysis", ["pss", "pnoise"])
    def test_pss_pnoise_are_allowed(self, bridge, writer_mocks, analysis):
        if analysis == "pnoise":
            bridge.set_maestro_analysis(
                analysis="pss", options={"fund": "20G"},
            )
        bridge.set_maestro_analysis(analysis=analysis)
        args = writer_mocks["set_analysis"].call_args.args
        assert args[1] == "MYTB"
        assert args[2] == analysis

    @pytest.mark.parametrize("analysis", [
        "pac", "pxf", "psp", "hb", "qpss", "qpnoise",
    ])
    def test_other_rf_families_remain_rejected(
        self, bridge, writer_mocks, analysis,
    ):
        with pytest.raises(ValueError, match="Analysis must be one of"):
            bridge.set_maestro_analysis(analysis=analysis)
        assert not writer_mocks["set_analysis"].called


class TestSafeBridgeRFOptions:
    def test_pss_options_are_formatted(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            analysis="pss",
            options={
                "fund": "20G",
                "harms": 9,
                "tstab": "20n",
                "errpreset": "moderate",
                "oscillator": "yes",
                "fundname": "osc1",
            },
        )
        opts = writer_mocks["set_analysis"].call_args.kwargs["options"]
        assert '("fund" "20G")' in opts
        assert '("harms" "9")' in opts
        assert '("tstab" "20n")' in opts
        assert '("errpreset" "moderate")' in opts
        assert '("oscillator" "yes")' in opts
        assert '("fundname" "osc1")' in opts

    def test_pnoise_options_are_formatted(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            analysis="pss", options={"fund": "20G"},
        )
        bridge.set_maestro_analysis(
            analysis="pnoise",
            options={
                "start": "100k",
                "stop": "100M",
                "dec": 20,
                "maxsideband": 7,
                "oprobe": "/Vout",
                "iprobe": "/I0/PLUS",
                "sweeptype": "absolute",
                "noisetype": "sources",
            },
        )
        opts = writer_mocks["set_analysis"].call_args.kwargs["options"]
        assert '("start" "100k")' in opts
        assert '("stop" "100M")' in opts
        assert '("dec" "20")' in opts
        assert '("maxsideband" "7")' in opts
        assert '("oprobe" "/Vout")' in opts
        assert '("iprobe" "/I0/PLUS")' in opts
        assert '("sweeptype" "absolute")' in opts
        assert '("noisetype" "sources")' in opts

    @pytest.mark.parametrize("analysis,key", [
        ("pss", "port"),
        ("pnoise", "probe"),
        ("pnoise", "write"),
    ])
    def test_rf_unknown_option_key_rejected(
        self, bridge, writer_mocks, analysis, key,
    ):
        with pytest.raises(ValueError, match="option .* is not allowed"):
            bridge.set_maestro_analysis(
                analysis=analysis, options={key: "1"},
            )
        assert not writer_mocks["set_analysis"].called

    @pytest.mark.parametrize("analysis,key,value", [
        ("pss", "errpreset", "fast"),
        ("pnoise", "sweeptype", "lin"),
        ("pnoise", "noisetype", "thermal"),
    ])
    def test_rf_enum_values_rejected(
        self, bridge, writer_mocks, analysis, key, value,
    ):
        with pytest.raises(ValueError, match="allowed:"):
            bridge.set_maestro_analysis(
                analysis=analysis, options={key: value},
            )
        assert not writer_mocks["set_analysis"].called

    def test_pnoise_foundry_shaped_token_value_rejected_and_scrubbed(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                analysis="pnoise", options={"oprobe": "nch_alpha"},
            )
        assert "nch_alpha" not in str(exc.value)
        assert not writer_mocks["set_analysis"].called

    def test_pnoise_probe_path_injection_rejected(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError):
            bridge.set_maestro_analysis(
                analysis="pnoise", options={"oprobe": "/Vout;system"},
            )
        assert not writer_mocks["set_analysis"].called

    def test_rf_numeric_option_rejects_bool_before_int(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError, match="Boolean parameter values"):
            bridge.set_maestro_analysis(
                analysis="pss", options={"harms": True},
            )
        assert not writer_mocks["set_analysis"].called


class TestMaestroSetupRFDispatch:
    def test_schema_accepts_pss_pnoise_minimal(self):
        err = validate_maestro_setup_block({
            "analyses": [
                {"test": "T1", "analysis": "pss", "options": {"fund": "20G"}},
                {
                    "test": "T1", "analysis": "pnoise",
                    "options": {"noisetype": "sources"},
                },
            ],
        })
        assert err is None

    @pytest.mark.parametrize("analysis", ["pss", "pnoise"])
    def test_apply_dispatches_pss_pnoise(
        self, bridge, writer_mocks, analysis,
    ):
        entries = [{
            "test": "MYTB",
            "analysis": "pss",
            "options": {"fund": "20G"},
        }]
        if analysis == "pnoise":
            entries.append({
                "test": "MYTB",
                "analysis": "pnoise",
                "options": {
                    "start": "100k", "stop": "10M",
                    "noisetype": "sources",
                },
            })
        out = apply_maestro_setup(bridge, {"analyses": entries})
        assert writer_mocks["set_analysis"].called
        if analysis == "pss":
            assert out["applied"]["analyses"] == ["MYTB:pss"]
        else:
            assert out["applied"]["analyses"] == ["MYTB:pss", "MYTB:pnoise"]

    def test_apply_skips_non_t1_rf_family(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, {
            "analyses": [{"test": "MYTB", "analysis": "pac"}],
        })
        assert out["applied"]["analyses"] == []
        assert out["skipped"]["analyses"][0][0] == "MYTB:pac"
        assert not writer_mocks["set_analysis"].called

class TestSafeBridgeRFR2Validation:
    @pytest.mark.parametrize("analysis,options", [
        ("pss", {"fund": "20G", "h\u0430rms": 1}),
        ("pnoise", {"noisetype": "sources", "iprobe": "/I\u0440robe"}),
    ])
    def test_unicode_confusables_rejected(
        self, bridge, writer_mocks, analysis, options,
    ):
        with pytest.raises(ValueError):
            bridge.set_maestro_analysis(analysis=analysis, options=options)
        assert not writer_mocks["set_analysis"].called

    @pytest.mark.parametrize("analysis,options", [
        ("pss", {"fund": "20G", "harms": sys.maxsize}),
        ("pss", {"fund": "20G", "harms": 1e308}),
        ("pnoise", {"noisetype": "sources", "maxsideband": -1e308}),
        ("pss", {"fund": "20G", "maxstep": float("inf")}),
        ("pss", {"fund": "20G", "maxstep": float("nan")}),
        ("pnoise", {"noisetype": "sources", "lin": "9" * 200}),
    ])
    def test_oversized_numeric_values_rejected(
        self, bridge, writer_mocks, analysis, options,
    ):
        with pytest.raises(ValueError):
            bridge.set_maestro_analysis(analysis=analysis, options=options)
        assert not writer_mocks["set_analysis"].called

    @pytest.mark.parametrize("token", [
        "nmos_secret", "pmos_secret", "rxnp_res", "vsubs_node",
    ])
    @pytest.mark.parametrize("analysis,options_for", [
        ("pss", lambda token: {"fund": token}),
        ("pss", lambda token: {"fund": "20G", "harms": token}),
        ("pss", lambda token: {"fund": "20G", "fundname": token}),
        ("pnoise", lambda token: {"noisetype": "sources", "start": token}),
        ("pnoise", lambda token: {"noisetype": "sources", "iprobe": token}),
        ("pnoise", lambda token: {"noisetype": "sources", "p": token}),
        ("pnoise", lambda token: {"noisetype": "sources", "n": token}),
        ("pnoise", lambda token: {"noisetype": token}),
    ])
    def test_foundry_prefix_tokens_rejected_and_scrubbed_everywhere(
        self, bridge, writer_mocks, token, analysis, options_for,
    ):
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                analysis=analysis, options=options_for(token),
            )
        _assert_exception_scrubbed(exc, token)
        assert not writer_mocks["set_analysis"].called

    @pytest.mark.parametrize("analysis,options,field", [
        ("pss", {"fund": "20G", "harms": 0}, "pss.harms"),
        ("pss", {"fund": "20G", "harms": 65}, "pss.harms"),
        ("pss", {"fund": "20G", "maxstep": 0}, "pss.maxstep"),
        ("pss", {"fund": "20G", "maxstep": float("inf")}, "pss.maxstep"),
        ("pss", {"fund": "20G", "maxstep": float("nan")}, "pss.maxstep"),
        ("pnoise", {"noisetype": "sources", "maxsideband": -1}, "pnoise.maxsideband"),
        ("pnoise", {"noisetype": "sources", "maxsideband": 65}, "pnoise.maxsideband"),
        ("pnoise", {"noisetype": "sources", "refsideband": -65}, "pnoise.refsideband"),
        ("pnoise", {"noisetype": "sources", "refsideband": 65}, "pnoise.refsideband"),
        ("pnoise", {"noisetype": "sources", "dec": 0}, "pnoise.dec"),
        ("pnoise", {"noisetype": "sources", "dec": 1001}, "pnoise.dec"),
        ("pnoise", {"noisetype": "sources", "lin": 0}, "pnoise.lin"),
        ("pnoise", {"noisetype": "sources", "lin": 100001}, "pnoise.lin"),
    ])
    def test_numeric_range_bounds_rejected(
        self, bridge, writer_mocks, analysis, options, field,
    ):
        with pytest.raises(ValueError, match=field):
            bridge.set_maestro_analysis(analysis=analysis, options=options)
        assert not writer_mocks["set_analysis"].called

    def test_string_length_cap_applies_to_rf_string_values(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError, match="pss.fund.*129"):
            bridge.set_maestro_analysis(
                analysis="pss", options={"fund": "9" * 129},
            )
        assert not writer_mocks["set_analysis"].called


class TestMaestroSetupRFR2SchemaGate:
    @pytest.mark.parametrize("entry,needle", [
        ({
            "test": "T1", "analysis": "pss",
            "options": {"fund": "20G", "port": "vdd"},
        }, "port"),
        ({
            "test": "T1", "analysis": "pss",
            "options": {"fund": "20G", "harms": 65},
        }, "pss.harms"),
        ({
            "test": "T1", "analysis": "pnoise",
            "options": {"noisetype": "sources", "iprobe": "nmos_secret"},
        }, "<redacted"),
        ({"test": "T1", "analysis": "pss", "options": {}}, "fund"),
        ({"test": "T1", "analysis": "pnoise", "options": {}}, "noisetype"),
    ])
    def test_schema_rejects_rf_invalid_before_apply(self, entry, needle):
        err = validate_maestro_setup_block({"analyses": [entry]})
        assert err is not None
        assert needle in err
        assert "nmos_secret" not in err

    @pytest.mark.parametrize("entry", [
        {
            "test": "T1", "analysis": "pss",
            "options": {"fund": "20G", "h\u0430rms": 1},
        },
        {
            "test": "T1", "analysis": "pnoise",
            "options": {"noisetype": "sources", "iprobe": "/I\u0440robe"},
        },
    ])
    def test_schema_rejects_unicode_confusables(self, entry):
        err = validate_maestro_setup_block({"analyses": [entry]})
        assert err is not None


class TestSafeBridgePNoiseRequiresPss:
    def test_pnoise_before_pss_same_test_raises_exact_message(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                "pnoise",
                test="MYTB",
                options={"noisetype": "sources"},
            )
        assert str(exc.value) == (
            "pnoise requires pss on the same test; set pss before "
            "pnoise on test MYTB"
        )
        assert not writer_mocks["set_analysis"].called

    def test_pnoise_after_pss_same_test_succeeds(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            "pss", test="MYTB", options={"fund": "20G"},
        )
        bridge.set_maestro_analysis(
            "pnoise", test="MYTB", options={"noisetype": "sources"},
        )
        assert writer_mocks["set_analysis"].call_count == 2
        assert writer_mocks["set_analysis"].call_args.args[2] == "pnoise"

    def test_pnoise_requires_pss_on_same_test(self, bridge, writer_mocks):
        bridge.set_maestro_analysis(
            "pss", test="TEST_A", options={"fund": "20G"},
        )
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                "pnoise", test="TEST_B", options={"noisetype": "sources"},
            )
        assert str(exc.value) == (
            "pnoise requires pss on the same test; set pss before "
            "pnoise on test TEST_B"
        )
        assert writer_mocks["set_analysis"].call_count == 1



def _r3_positive_range_options(analysis, field, value):
    if analysis == 'pss':
        options = {'fund': '5.92G'}
    else:
        options = {'noisetype': 'sources'}
    options[field] = value
    return options


_R3_POSITIVE_RANGE_FIELDS = [
    ('pss', 'fund'),
    ('pss', 'tstab'),
    ('pss', 'maxstep'),
    ('pnoise', 'start'),
    ('pnoise', 'stop'),
]

_R3_INVALID_POSITIVE_VALUES = [
    10**200,
    1e308,
    0,
    -1,
    '-1n',
    float('inf'),
    float('nan'),
]

_R3_SCHEMA_RANGE_REJECTION_CASES = [
    ('pss', 'fund', 10**200),
    ('pss', 'tstab', 0),
    ('pss', 'maxstep', float('nan')),
    ('pnoise', 'start', '-1n'),
    ('pnoise', 'stop', 1e308),
]


class TestMaestroSetupRFR3MalformedAnalysis:
    @pytest.mark.parametrize('analysis,type_name', [
        ({}, 'dict'),
        ([], 'list'),
        (None, 'NoneType'),
        (123, 'int'),
        (True, 'bool'),
    ])
    def test_analysis_non_string_returns_repairable_error(
        self, analysis, type_name,
    ):
        err = validate_maestro_setup_block({
            'analyses': [
                {'test': 'T1', 'analysis': analysis, 'options': {}},
            ],
        })
        assert isinstance(err, str)
        assert 'analysis' in err
        assert 'must be a string' in err
        assert type_name in err

    @pytest.mark.parametrize('options,type_name', [
        ('not a dict', 'str'),
        ([], 'list'),
    ])
    def test_options_non_dict_returns_repairable_error(
        self, options, type_name,
    ):
        err = validate_maestro_setup_block({
            'analyses': [
                {'test': 'T1', 'analysis': 'pss', 'options': options},
            ],
        })
        assert isinstance(err, str)
        assert 'options' in err
        assert 'must be a dict' in err
        assert type_name in err

    def test_options_none_returns_rf_missing_required_error(self):
        err = validate_maestro_setup_block({
            'analyses': [
                {'test': 'T1', 'analysis': 'pss', 'options': None},
            ],
        })
        assert isinstance(err, str)
        assert 'pss options missing required' in err
        assert 'fund' in err

    def test_test_non_string_returns_repairable_error(self):
        err = validate_maestro_setup_block({
            'analyses': [
                {'test': 123, 'analysis': 'pss', 'options': {'fund': '5.92G'}},
            ],
        })
        assert isinstance(err, str)
        assert 'test' in err
        assert 'must be a string' in err
        assert 'int' in err


class TestSafeBridgeRFR3PositiveRangeCaps:
    @pytest.mark.parametrize('analysis,field', _R3_POSITIVE_RANGE_FIELDS)
    @pytest.mark.parametrize('value', _R3_INVALID_POSITIVE_VALUES)
    def test_bounded_positive_rf_values_reject_bad_magnitudes(
        self, bridge, writer_mocks, analysis, field, value,
    ):
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                analysis=analysis,
                options=_r3_positive_range_options(analysis, field, value),
            )
        msg = str(exc.value)
        assert f'{analysis}.{field}' in msg
        assert 'outside accepted range' in msg
        assert not writer_mocks['set_analysis'].called

    def test_pss_accepts_numeric_values_inside_ranges(
        self, bridge, writer_mocks,
    ):
        bridge.set_maestro_analysis(
            analysis='pss',
            options={'fund': 5.92e9, 'tstab': 10e-6, 'maxstep': 1e-12},
        )
        assert writer_mocks['set_analysis'].call_args.args[2] == 'pss'

    def test_pss_accepts_engineering_suffix_values_inside_ranges(
        self, bridge, writer_mocks,
    ):
        bridge.set_maestro_analysis(
            analysis='pss',
            options={'fund': '5.92G', 'tstab': '10u', 'maxstep': '100p'},
        )
        opts = writer_mocks['set_analysis'].call_args.kwargs['options']
        assert '("fund" "5.92G")' in opts
        assert '("tstab" "10u")' in opts
        assert '("maxstep" "100p")' in opts

    def test_pnoise_accepts_values_inside_ranges_after_pss(
        self, bridge, writer_mocks,
    ):
        bridge.set_maestro_analysis('pss', options={'fund': '5.92G'})
        bridge.set_maestro_analysis(
            'pnoise',
            options={'noisetype': 'sources', 'start': 1e3, 'stop': '10M'},
        )
        opts = writer_mocks['set_analysis'].call_args.kwargs['options']
        assert writer_mocks['set_analysis'].call_args.args[2] == 'pnoise'
        assert '("noisetype" "sources")' in opts
        assert '("stop" "10M")' in opts

    def test_generic_numeric_atom_length_cap_applies_to_ints(
        self, bridge, writer_mocks,
    ):
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                analysis='tran',
                options={'freq': 10**200},
            )
        msg = str(exc.value)
        assert 'tran.freq formatted length' in msg
        assert 'exceeds 128 character cap' in msg
        assert not writer_mocks['set_analysis'].called


class TestMaestroSetupRFR3RangeCaps:
    @pytest.mark.parametrize(
        'analysis,field,value', _R3_SCHEMA_RANGE_REJECTION_CASES,
    )
    def test_schema_rejects_out_of_range_positive_rf_options(
        self, analysis, field, value,
    ):
        err = validate_maestro_setup_block({
            'analyses': [
                {
                    'test': 'T1',
                    'analysis': analysis,
                    'options': _r3_positive_range_options(
                        analysis, field, value,
                    ),
                },
            ],
        })
        assert isinstance(err, str)
        assert f'{analysis}.{field}' in err
        assert 'outside accepted range' in err

    def test_schema_accepts_in_range_positive_rf_options(self):
        err = validate_maestro_setup_block({
            'analyses': [
                {
                    'test': 'T1',
                    'analysis': 'pss',
                    'options': {
                        'fund': '5.92G',
                        'tstab': '10u',
                        'maxstep': '100p',
                    },
                },
                {
                    'test': 'T1',
                    'analysis': 'pnoise',
                    'options': {
                        'noisetype': 'sources',
                        'start': 1e3,
                        'stop': '10M',
                    },
                },
            ],
        })
        assert err is None

    def test_schema_rejects_pss_freq_above_range(self):
        err = validate_maestro_setup_block({
            'analyses': [
                {
                    'test': 'T1',
                    'analysis': 'pss',
                    'options': {'fund': '5.92G', 'freq': 10**200},
                },
            ],
        })
        assert isinstance(err, str)
        assert 'pss.freq' in err
        assert 'outside accepted range' in err


class TestSafeBridgeR4FreqAndRelativeharmonicRanges:
    @pytest.mark.parametrize('value', [-1, 0, '-1G', 10**200])
    def test_pss_freq_rejects_out_of_range_values_in_bridge_and_schema(
        self, bridge, writer_mocks, value,
    ):
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis(
                analysis='pss',
                options={'fund': '5.92G', 'freq': value},
            )
        msg = str(exc.value)
        assert 'pss.freq' in msg
        assert 'value' in msg
        assert 'outside accepted range' in msg
        assert not writer_mocks['set_analysis'].called

        err = validate_maestro_setup_block({
            'analyses': [
                {
                    'test': 'T1',
                    'analysis': 'pss',
                    'options': {'fund': '5.92G', 'freq': value},
                },
            ],
        })
        assert isinstance(err, str)
        assert 'pss.freq' in err
        assert 'outside accepted range' in err

    @pytest.mark.parametrize('value', [5.92e9, '5.92G'])
    def test_pss_freq_accepts_in_range_values_in_bridge_and_schema(
        self, bridge, writer_mocks, value,
    ):
        bridge.set_maestro_analysis(
            analysis='pss',
            options={'fund': '5.92G', 'freq': value},
        )
        assert writer_mocks['set_analysis'].call_args.args[2] == 'pss'

        err = validate_maestro_setup_block({
            'analyses': [
                {
                    'test': 'T1',
                    'analysis': 'pss',
                    'options': {'fund': '5.92G', 'freq': value},
                },
            ],
        })
        assert err is None

    @pytest.mark.parametrize('value', [-65, 65, '1.5', '-1G', True, False])
    def test_pnoise_relativeharmonic_rejects_bad_values_in_bridge_and_schema(
        self, bridge, writer_mocks, value,
    ):
        options = {'noisetype': 'sources', 'relativeharmonic': value}
        with pytest.raises(ValueError) as exc:
            bridge.set_maestro_analysis('pnoise', options=options)
        msg = str(exc.value)
        assert 'pnoise.relativeharmonic' in msg
        assert 'accepted range' in msg or 'Boolean parameter values' in msg
        assert not writer_mocks['set_analysis'].called

        err = validate_maestro_setup_block({
            'analyses': [
                {'test': 'T1', 'analysis': 'pnoise', 'options': options},
            ],
        })
        assert isinstance(err, str)
        assert 'relativeharmonic' in err
        if isinstance(value, bool):
            assert 'got bool' in err
        else:
            assert 'pnoise.relativeharmonic' in err
            assert 'accepted range' in err

    @pytest.mark.parametrize('value', [0, -64, 64, 1])
    def test_pnoise_relativeharmonic_accepts_boundaries_and_typical_value(
        self, bridge, writer_mocks, value,
    ):
        bridge.set_maestro_analysis('pss', options={'fund': '5.92G'})
        writer_mocks['set_analysis'].reset_mock()
        options = {'noisetype': 'sources', 'relativeharmonic': value}
        bridge.set_maestro_analysis('pnoise', options=options)
        assert writer_mocks['set_analysis'].call_args.args[2] == 'pnoise'

        err = validate_maestro_setup_block({
            'analyses': [
                {'test': 'T1', 'analysis': 'pnoise', 'options': options},
            ],
        })
        assert err is None
