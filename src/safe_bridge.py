"""SafeBridge: PDK-sanitizing wrapper around virtuoso-bridge-lite.

Core safety layer that ensures PDK-proprietary data never reaches the LLM:
- Replaces foundry cell names with generic names (NMOS/PMOS/etc.)
- Strips BSIM4 model info from simulation and operating-point results
- Whitelists only safe design parameters (W/L/nf/m/multi/wf)
- Validates input names and parameter values to prevent SKILL injection

Supports remote-side SKILL filtering (Method A) with Python-side as second defense.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import posixpath
import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import writer as _mae_writer
# Stage 1 rev 2 (2026-04-18): SpectreSimulator import removed along with
# the bridge.simulate() direct Spectre path. Per user directive
# ("OCEAN ???????????"), Direction C (OCEAN) is now the single
# simulation entrypoint; a Spectre fallback would defeat its scope-bound
# allow-list and is not needed.

logger = logging.getLogger(__name__)


# Strict pattern for lib/cell/instance names to prevent SKILL injection
_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_PARAM_ATOM_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:[a-zA-Z]+)?$"
)
# Scrub patterns for display_transient_waveform arguments.
_SAFE_PSF_DIR_RE = re.compile(r"^[A-Za-z0-9_./~:\-]+$")
_SAFE_RESULT_DIR_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_SAFE_NET_NAME_RE = re.compile(r"^/[A-Za-z0-9_]+$")
_SAFE_OP_HISTORY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_SAFE_OP_RESULT_PATH_RE = re.compile(r"^/[A-Za-z0-9_./!]{1,255}$")
_MAX_MAESTRO_OP_NETS = 64
_MAX_MAESTRO_OP_INSTANCES = 128
# Path-2 (2026-05-19): Maestro Interactive.<N> sweep root. Same alphabet
# as _SAFE_PSF_DIR_RE; require a `/Interactive.<digits>` tail so the
# manifest reader cannot be repurposed as a generic file slurp.
_SAFE_SWEEP_ROOT_RE = re.compile(r"^[A-Za-z0-9_./\-]{1,256}$")
_SAFE_INTERACTIVE_TAIL_RE = re.compile(r"/Interactive\.[0-9]+/?$")
_SAFE_OP_POINT_KEYS = {
    # Bias
    "vgs", "vds", "vbs",
    # Current
    "id", "ids",
    # Small-signal
    "gm", "gds", "gmb",
    # Threshold / overdrive
    "vth", "vdsat", "vov",
    # Region enum (integer, mapped to string label in feedback layer)
    "region",
    # Terminal capacitances
    "cgs", "cgd", "cdb", "cdg", "cgg",
    # Non-MOS attrs (resistor/inductor/cap from tranOp)
    "i", "v", "pwr",
}
_LLM_OP_POINT_COLUMNS = (
    "vgs", "vds", "vov", "id", "gm", "gds",
    "vth", "cgs", "cgd",
)
_SAVE_OP_POINT_EFFECT_KEYS = {
    "vgs", "vds", "ids", "id", "gm", "gds", "vth", "vdsat", "cgs", "cgd",
}
_TRUE_VDSAT_ALIAS_CANDIDATES = (
    "vdsat", "vdssat", "vdsat_eff", "vdsatEff", "Vdsat",
    "vdsatcv", "vdsatCV", "vdsat_cv", "vdsatc", "vdsatC",
    "vdsat_c", "VDSAT",
)
_NEARBY_VDSAT_DIAGNOSTIC_CANDIDATES = (
    "vdsat_vadj", "vdsat_vdl",
    "vdseff", "vdsEff", "vds_eff", "vgst", "vgsteff", "vgstEff",
)
_VDSAT_ALIAS_PROBE_CANDIDATES = (
    _TRUE_VDSAT_ALIAS_CANDIDATES + _NEARBY_VDSAT_DIAGNOSTIC_CANDIDATES
)

# Saturation-region enum (Spectre tranOp). Used to render `region` int into
# a human-readable label for LLM feedback. Values observed on IC23.1 /
# tmibsimcmg probe v4: region=1 for a deep-triode NMOS.
_REGION_LABELS = {
    0: "cutoff",
    1: "triode",
    2: "saturation",
    3: "subthreshold",
    4: "breakdown",
}

# Patterns for string-value sanitization in exception messages and logs.
# Used by _scrub() to strip foundry names, absolute paths, and other
# potentially sensitive substrings before they propagate to the LLM.
#
# GREP-GATE EXCEPTION: this file is the authoritative source of the
# banned-pattern list, so the sanitizer itself must literally contain
# those tokens. Any grep gate over src/ must treat the single line
# below (and this comment block) as the sole allowed occurrence; no
# other code or data in the PC-side repo may contain these tokens.
# The prefix seeds below correspond to foundry device families that
# must never appear outside this line.
_FOUNDRY_LEAK_RE = re.compile(
    r"\b(?i:nch_|pch_|cfmom|rppoly|rm1_|tsmc|tcbn|rxnp|vsubs)\w*"
    r"|\b(?i:nmos|pmos)(?!(?:_[LlSsHh][Vv][Tt])?\b)\w+",
)
_ABS_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|*?]*")
# UNC paths like \\server\share\... must be stripped before the drive-letter
# regex runs, otherwise the leading \\ is only partially handled.
_UNC_PATH_RE = re.compile(r"\\\\[^\s'\"<>|*?\\]+\\[^\s'\"<>|*?]*")
# Forward-slash UNC form //server/share/... â€“ also produced by
# `Path(...).as_posix()` on Windows when the input was a UNC path.
_FORWARD_UNC_PATH_RE = re.compile(r"//[^\s'\"<>|*?/]+/[^\s'\"<>|*?]*")
_ABS_UNIX_PATH_RE = re.compile(
    r"/(?:home|project|proj|tmp|var|Users|usr|opt|etc|nfs|mnt|srv|data"
    r"|tools|cadence|cad|pdk|eda|scratch|work|private)"
    r"/[^\s'\"<>|*?]*"
)

# remote host access paths — four controlled routes from Python to SKILL:
#
# 1. _execute_skill_json + this allow-list: only the entrypoints below are
#    forwarded. Anything else (raw hiOpenLib / dbOpenCellViewByType /
#    arbitrary load()) is rejected at the bridge.
#
# 2. _load_skill_helpers: sends skill/*.il wrappers to remote host at bridge
#    init time. Prefers _upload_skill_inline (path 4) so PC stays the
#    source of truth; falls back to load("<remote_path>") only if the PC
#    copy is missing. Each wrapper sanitizes its own arguments on the
#    SKILL side before touching Cadence state.
#
# 3. display_transient_waveform: controlled bypass of the allow-list for
#    pure-display waveform plotting. The SKILL expression is constructed
#    entirely inside SafeBridge (not agent/LLM side), all interpolated
#    arguments are scrubbed by _SAFE_PSF_DIR_RE / _SAFE_NET_NAME_RE, the
#    call returns a non-JSON Viva plot handle so _execute_skill_json cannot
#    be used, and the effect is pure display with no state mutation. See
#    the # SECURITY comment on the method for the full rationale.
#
# 4. _upload_skill_inline: controlled bypass that sends the full text of
#    a .il file under self._skill_dir to remote host, wrapped in progn(...). The
#    PC-side file is the source of truth so remote-side staleness cannot
#    mask an updated procedure binding (E2 in 2026-04-22 log: remote host still
#    had the 0-arg safeReadOpPointAfterTran after Task B1 made it 1-arg).
#    Defense-in-depth: path must resolve under self._skill_dir, size <=
#    _SKILL_INLINE_MAX_BYTES, content is linted for forbidden SKILL
#    primitives (system / popen / exec / shell / evalstring / ipcbegin).
_ALLOWED_SKILL_ENTRYPOINTS = frozenset({
    "safeReadSchematic",
    # Task H1 (2026-04-22): hierarchical BFS reader. Same-library only.
    # Emits root cellview + every unique same-lib subcell (up to
    # max_depth), deduplicated via SUBCELL_N handles. Cross-lib masters
    # stay leaves with cellAlias() naming — foundry cell names never
    # leave remote host, matching the flat reader's PDK posture.
    "safeReadSchematicDeep",
    "safeReadOpPoint",
    # Stage 1 rev 7 (2026-04-19): tranOp-based op-point read; no dedicated
    # DC analysis required. Dual-signed by probe 7 v3+v4 (dr: handle +
    # hard-coded handle->prop dispatcher in safe_read_op_point.il).
    "safeReadOpPointAfterTran",
    # 2026-06-04: read-only Maestro history DC operating-point summary.
    # Request-scoped: caller passes explicit node/instance paths. Remote
    # side selects only 'dc and 'instance; never model/primitives.
    "safeReadMaestroDcOpPoint",
    # 2026-06-04: current-run PSF DC operating-point summary. Used by
    # AC/DC optimizer loops after safeOceanRun; same explicit path list
    # and same dc/instance-only result selection as the Maestro reader.
    "safeReadDcOpPointFromResults",
    # 2026-06-05: bounded alias probe for Spectre OP vdsat naming. Reads
    # only caller-provided DUT paths and a hard-coded vdsat candidate set.
    "safeReadVdsatAliasProbeFromResults",
    "safeSetParam",
    "safeOceanRun",
    "safeOceanMeasure",
    # Stage 1 rev 4 (2026-04-18): generic dump + primitive-op pair.
    # Replaces the LC_VCO-specific 7-metric safeOceanMeasure.
    "safeOceanDumpAll",
    "safeOceanTCross",
    # Stage 1 rev 11 (2026-04-20, Bug 3): lightweight oscillation probe.
    # Reads ptp/mean of (VT(p1)-VT(p2)) over a single trailing window.
    # Called before safeOceanDumpAll to skip the 30 s dump hang on
    # non-oscillating waveforms (run_20260420_033152 iters 2/4/6/9/10).
    "safeOceanProbePtp",
    # Path-2 (2026-05-19): read .tuning_manifest.json from a Maestro
    # Interactive.<N> sweep root to recover per-point Vctrl mapping.
    "safeReadSweepManifest",
    # Path-2 (2026-05-19): author .tuning_manifest.json from a
    # PC-derived (point, vctrl) list. The agent derives entries from
    # spec.md §6.1 `sweep:` and writes them out before the read side
    # runs — Maestro itself never creates the file.
    "safeWriteSweepManifest",
    # 2026-06-13: clear stale per-point sweep results under a validated
    # Interactive.<N> root before a fresh benchmark run. This prevents
    # curve-search/readback from consuming a previous model's PSF payload.
    "safeClearSweepResults",
    # Stage 1 rev 6 (2026-04-18): generic design-variable auto-discovery.
    # Parses Maestro's input.scs to learn testbench desVars â€“ enables
    # the agent to pick up Maestro defaults instead of hardcoding names.
    "safeOceanListDesignVars",
    # Stage 1 rev 6 B2 (2026-04-18): generic analysis auto-discovery.
    # Parses Maestro's input.scs to learn analysis kwargs (e.g.
    # tran stop=200n) so OCEAN's analysis() call gets the same
    # stop-time Maestro intended; fixes OCN-6038 ghost psf.
    "safeOceanListAnalyses",
    # Stage 1 rev 10 (2026-04-19): Plan Auto ic-line patcher. Parses
    # spectre.fc and rewrites input.scs's ic line so bias nodes keep
    # their equilibrium values across skipdc=yes transients; perturb
    # nodes get a deliberate asymmetric kick. See src/plan_auto.py.
    "safePatchNetlistIC",
    "safeMaeWriteAndSave",
    "safeMaeSaveSetup",
    "safeMaeSetupSummary",
    # Diagnostic-only, read-only. See skill/safe_maestro_debug.il.
    "safeMae_debugInfo",
    # 2026-04-22: auto-discover Maestro input.scs under $HOME/simulation
    # so --scs-path becomes optional. Read-only filesystem inspection.
    "safeMaeFindInputScs",
    # Task F-B (2026-04-22): spec scaffolding. Read-only, returns only
    # top-level pin names + directions; no instance props / model cards.
    "safeGenerateSpecScaffold",
    "read_schematic",
    "read_op_point",
    "set_instance_param",
})

# Data constructors that are safe to nest inside the SKILL entrypoints.
# SKILL uses list(...) to build argument lists; it has no side effect, so
# we allow it. Any other nested call identifier is rejected.
_ALLOWED_SKILL_NESTED = frozenset({"list"})

# Layer-2 fallback for param-name validation: accept any name matching
# this pattern AND not containing a blocklist substring. Mirrors
# safeHelpers_validateParamName in skill/helpers.il. Keep both in sync.
_SAFE_PARAM_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,31}$")

# Task H1 (2026-04-22): handles issued by safeReadSchematicDeep for unique
# same-library subcells. Shape is ROOT | SUBCELL_<n> (integer). Python-side
# rejects any other string — prevents a compromised SKILL payload from
# smuggling a foundry cell name in the "subcell" slot of an instance row.
_SUBCELL_HANDLE_RE = re.compile(r"^(ROOT|SUBCELL_\d+)$")
_BLOCKED_PARAM_WORDS = frozenset({
    "load", "file", "path", "system", "eval", "exec", "shell",
    "include", "require", "getss", "errset", "evalstring",
    "infile", "outfile", "popen", "ipcbegin", "rexcompile",
    "rexexecute", "sprintf", "printf",
    "model", "subckt", "section",
    # Supply-rail and ground names must never be tunable design variables.
    "vdd", "vss", "gnd", "vcc", "vee",
})

# Mirrors safe_ocean.il's safeOcean_allowedAnalyses so PC rejects bogus
# analysis names without wasting a round-trip to remote host. Keep in sync with
# the SKILL-side constant; the SKILL side remains the semantic gate.
_OCEAN_ALLOWED_ANALYSES = frozenset({"tran", "ac", "dc", "noise", "xf", "stb"})

# Track C T1 (2026-05-17): Maestro can configure RF analyses that are not
# currently driven by safeOceanRun. Keep the OCEAN set above SKILL-synced, and
# use this wider set only for Maestro setup writeback.
_MAESTRO_ALLOWED_ANALYSES = _OCEAN_ALLOWED_ANALYSES | frozenset({
    "pss", "pnoise",
})

# RF analysis options intentionally use a finite per-analysis key set. The
# older tran/ac/dc/noise/xf/stb path remains permissive for backward
# compatibility, but pss/pnoise need string-valued knobs, so every accepted
# string value below is either a finite enum or a validated safe token/probe.
_RF_STRING_MAX_LEN = 128
_RF_FOUNDRY_REDACTED = "<redacted: matches foundry-leak pattern>"

# Cobi → PC return-value size cap. Defends against unbounded-string
# DoS: a malicious / buggy remote returning hundreds of MB of SKILL
# output would otherwise feed straight into _scrub, log formatters,
# and ultimately the LLM context — exhausting memory on PC and tokens
# on the LLM. Maestro / SKILL ops invoked by this module return at
# most a few tens of KB in normal operation; 1 MiB chars is ~100x
# typical with wide margin for legitimate large reads. Anything over
# this cap is presumed adversarial or buggy and rejected loudly (raise)
# rather than silently truncated, so the caller can investigate.
_REMOTE_OUTPUT_MAX_CHARS = 1 << 20
_PSS_ALLOWED_OPTIONS = frozenset({
    "fund", "freq", "harms", "tstab", "errpreset",
    "oscillator", "autonomous", "fundname", "skipdc", "maxstep",
})
_PNOISE_ALLOWED_OPTIONS = frozenset({
    "start", "stop", "dec", "lin", "maxsideband",
    "relativeharmonic", "refsideband", "sweeptype",
    "output", "input", "oprobe", "iprobe", "p", "n", "noisetype",
})
_MAESTRO_RF_ANALYSIS_OPTIONS: dict[str, frozenset[str]] = {
    "pss": _PSS_ALLOWED_OPTIONS,
    "pnoise": _PNOISE_ALLOWED_OPTIONS,
}
_MAESTRO_RF_REQUIRED_OPTIONS: dict[str, frozenset[str]] = {
    "pss": frozenset({"fund"}),
    "pnoise": frozenset({"noisetype"}),
}
_MAESTRO_RF_ENUM_OPTIONS: dict[tuple[str, str], frozenset[str]] = {
    ("pss", "errpreset"): frozenset({"conservative", "moderate", "liberal"}),
    ("pss", "oscillator"): frozenset({"yes", "no"}),
    ("pss", "autonomous"): frozenset({"yes", "no"}),
    ("pss", "skipdc"): frozenset({"yes", "no"}),
    ("pnoise", "sweeptype"): frozenset({"absolute", "relative"}),
    ("pnoise", "noisetype"): frozenset({"sources", "jitter", "timeaverage"}),
}
_MAESTRO_RF_TOKEN_OPTIONS = frozenset({
    ("pss", "fundname"),
    ("pnoise", "output"),
    ("pnoise", "input"),
    ("pnoise", "oprobe"),
    ("pnoise", "iprobe"),
    ("pnoise", "p"),
    ("pnoise", "n"),
})
_MAESTRO_RF_INT_RANGES: dict[tuple[str, str], tuple[int, int]] = {
    ("pss", "harms"): (1, 64),
    ("pnoise", "maxsideband"): (0, 64),
    ("pnoise", "refsideband"): (-64, 64),
    ("pnoise", "relativeharmonic"): (-64, 64),
    ("pnoise", "dec"): (1, 1000),
    ("pnoise", "lin"): (1, 100000),
}
# R3 (2026-05-17): physical sanity ranges for RF frequency/time atoms.
# These are intentionally broad enough for analog/RF design exploration while
# still rejecting zero/negative values and pathological magnitudes that can
# bloat SKILL payloads or represent non-physical setup knobs.
_MAESTRO_RF_POSITIVE_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("pss", "fund"): (1e-3, 1e15),
    ("pss", "freq"): (1e-3, 1e15),
    ("pss", "tstab"): (1e-18, 1e6),
    ("pss", "maxstep"): (1e-18, 1e6),
    ("pnoise", "start"): (1e-6, 1e15),
    ("pnoise", "stop"): (1e-6, 1e15),
}
_RF_NUMERIC_RE = re.compile(
    r"\A([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)\Z"
)
_RF_ENGINEERING_SUFFIX_SCALE = {
    "": 1.0,
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "meg": 1e6,
    "Meg": 1e6,
    "MEG": 1e6,
    "M": 1e6,
    "g": 1e9,
    "G": 1e9,
    "t": 1e12,
    "T": 1e12,
}


# Track C v2 (2026-05-15): simulator whitelist for ``create_maestro_test``.
# Bounded set chosen with the leader: the four production simulators we
# actually drive through Maestro today. Adding e.g. ``aimspice`` or
# ``HSIM`` is fine in principle but each addition needs a deliberate
# review — an LLM that proposes a simulator outside this set should fail
# closed rather than silently fall through to the remote SKILL default.
_MAESTRO_SIMULATOR_ALLOWED = frozenset(
    {"spectre", "spectreVerilog", "hspice", "auCdl"}
)
# Cellview names per CDB (``schematic`` / ``schematic_view`` / ``symbol``
# / ``config`` / ``calibre``). Use the existing ``_NAME_RE`` since CDB
# disallows anything outside ``[A-Za-z0-9_.-]`` in a cellview name.

# Maestro test-name pattern. Maestro emits tests as ``<lib>:<cell>:<n>`` or
# bare ``<cell>``; colon must be allowed alongside the strict-name charset.
# Keeps the surface tight enough that no whitespace / quote / SKILL
# metacharacter can slip into ``maeAddOutput("...", "...", ...)``.
_MAESTRO_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")

# Maestro output names — identifier-like, never reach SKILL primitives.
_MAESTRO_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# Allowed Maestro output_type values per virtuoso-bridge writer.add_output.
# Empty string means "let Maestro infer" (signal vs expr); the writer omits
# the ?outputType keyword in that case. Cadence's maeAddOutput uses
# ?outputType for EvalType ("point"), not for the row kind. The row kind
# is inferred from ?signalName vs ?expr. Keep "signal" / "expr" as
# backwards-compatible caller aliases, but normalize them away before
# dispatch so they cannot become live no-ops on ADE Assembler.
_MAESTRO_OUTPUT_TYPES = frozenset({"", "signal", "expr", "point"})

# Allow-list of OCEAN / Calculator function names permitted in a
# user-supplied Maestro output expression. R1 (2026-05-14) switched
# ``_validate_maestro_expr`` from deny-list to allow-list per dual-review:
# a deny-list cannot bound an unbounded set of SKILL primitives (codex R1
# called out 11 missing tokens — getq/process/lambda/apply/funcall/defun/
# procedure/prog/puts/fileSeek/dbWriteCellView — and the next audit would
# find more). Every identifier immediately followed by ``(`` in the expr
# MUST be in this set; anything else is rejected. The set is intentionally
# small and covers the OCEAN waveform-/measure-/math-function vocabulary
# documented in cdsdoc "OCEAN Reference > Waveform Calculator Functions";
# adding a new entry requires a deliberate edit here (and ideally a test
# pinning the new function).
_MAESTRO_EXPR_ALLOWED_FUNCS = frozenset({
    # Probes / waveform constructors
    "VT", "IT", "VS", "IS", "VF", "IF", "v", "i", "getData",
    # Sample / span readouts
    "value", "valueOf", "average", "mean", "stddev",
    "rms", "integ", "deriv",
    "ymax", "ymin", "ymaxlocal", "yminlocal",
    "xmax", "xmin", "xval", "yval",
    "peakToPeak", "swing",
    # Time-domain measures
    "frequency", "freq", "period", "dutyCycle", "delay", "cross",
    "riseTime", "fallTime", "settlingTime", "slewRate",
    "pulseWidth", "overshoot", "undershoot",
    # AC / frequency-domain
    "db20", "db10", "dB", "dB10", "dB20",
    "mag", "phase", "ph", "phaseDeg", "phaseRad",
    "real", "imag",
    "gainBwProd", "gainMargin", "phaseMargin", "unityGainFreq",
    # Noise-specific OCEAN measures (PSS pnoise outputs use these)
    "phaseNoise", "noiseSummary",
    # Math / reduction (SKILL function form of operators)
    "abs", "max", "min", "sqrt", "log", "log10", "exp",
    "sin", "cos", "tan", "asin", "acos", "atan2",
    "sinh", "cosh", "tanh",
    "floor", "ceil", "round",
    "plus", "minus", "times", "quotient",
    # SKILL data constructors that may legitimately appear inside an
    # OCEAN expression argument (e.g. ``value(list("/V") 1n)`` — rare,
    # but cheaper to allow than to special-case).
    "list",
    # Windowing — clip a waveform to a [tStart, tEnd] sub-range.
    # Track C Option I needs this so spec.metrics windows can be
    # mirrored into Maestro Outputs as
    # ``<stat>(clip(<wf> <tStart> <tEnd>))``. Pure waveform op
    # (no I/O, no eval), so allow-listing it is safe.
    "clip",
})

# Outer length cap on user-supplied OCEAN expressions. Real measure
# expressions like ``value(frequency(VT(/Vout)) 100n)`` are well under
# 256 chars; 1024 leaves room for legitimate compound expressions while
# bounding worst-case scanner cost.
_MAESTRO_EXPR_MAX_LEN = 1024

# R1 R2 (2026-05-14): absolute-path roots permitted as the prefix of a
# ``create_netlist_for_corner`` output_dir. The R1 default
# (``~/simulation/`` + ``~/.virtuoso-agent/``) was rejected on review:
#  * real cobi-style project paths live under ``/proj/...`` or
#    ``/project/<user>/...``, neither of which would have matched.
#  * tilde is a shell-expansion artifact and may or may not be expanded
#    by SKILL / SSH before the path reaches the FS layer, which would
#    leave SafeBridge unable to reason about the actual target.
# We therefore require an absolute POSIX path beginning with one of the
# roots below. Tilde is now forbidden anywhere in the string (position 0
# included). Env override ``VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS`` adds
# extra roots at validation time; each comma-separated entry must start
# and end with ``/`` so a malformed value cannot accidentally widen the
# allow-list to ``/`` itself.
_MAESTRO_REMOTE_OUTPUT_ROOTS: tuple[str, ...] = (
    "/tmp/",
    "/var/tmp/",
    "/scratch/",
    "/proj/",
    "/project/",
    "/home/",
)


def _resolve_remote_output_roots() -> tuple[str, ...]:
    """Return the active output-root allow-list (baseline + env override).

    Reads ``VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS`` lazily on each call so
    tests can patch the env without re-importing the module. Each
    comma-separated entry MUST start and end with ``/`` — a malformed
    entry raises ValueError rather than silently widening the allow-list.
    """
    extra: list[str] = []
    raw = os.environ.get("VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS", "")
    if raw:
        for entry in raw.split(","):
            piece = entry.strip()
            if not piece:
                continue
            if not piece.startswith("/") or not piece.endswith("/"):
                raise ValueError(
                    "VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS entry must be an "
                    f"absolute POSIX path bracketed by '/' (got len={len(piece)})."
                )
            extra.append(piece)
    return _MAESTRO_REMOTE_OUTPUT_ROOTS + tuple(extra)

# Function-call scanner for the expr allow-list. Matches an identifier
# immediately followed by ``(`` (optional whitespace). ASCII-only by
# construction because ``_validate_maestro_expr`` rejects non-ASCII expr
# strings up front.
_MAESTRO_EXPR_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

# Stage 1 rev 11 (2026-04-19): enum-valued kwargs permitted on analysis
# specs. _format_param_value / _PARAM_ATOM_RE accept only numeric literals
# (e.g. "200n", "5e-7"), so non-numeric strings like skipdc=yes were being
# rejected here and then silently dropped by safeOceanListAnalyses on the
# SKILL side. That stripped skipdc from the regenerated OCEAN netlist,
# which let Spectre perform DC init and trap LC-VCOs at the symmetric
# metastable equilibrium (V_diff flat at 0). Each entry below whitelists
# a specific (lower-cased) kwarg name against a finite set of literal
# string values â€“ mirrors safeOcean_isEnumKwarg in skill/safe_ocean.il.
_OCEAN_ENUM_KWARGS: dict[str, frozenset[str]] = {
    "skipdc": frozenset({"yes", "no"}),
    "oppoint": frozenset({"rawfile"}),
    "detail": frozenset({"node", "oppoint", "device", "all"}),
}


def assess_op_point_save_effectiveness(
    run_result: dict | None,
    op_point: dict | None,
) -> dict[str, Any]:
    """Summarize whether per-DUT saveOpPoint produced useful OP scalars.

    ``safeOceanRun`` can request saveOpPoint successfully while a simulator
    exposes only sparse instance metadata. Treat the check as effective only
    when at least one requested DUT instance has a non-region numeric scalar
    such as vgs/gm/cgs. The result is intentionally aggregate-only: no raw
    paths, model names, or unallowlisted OP keys leave this function.
    """
    requested_raw = (
        run_result.get("opPointsRequested")
        if isinstance(run_result, dict) else 0
    )
    try:
        requested = int(requested_raw or 0)
    except (TypeError, ValueError):
        requested = 0

    instances = (
        op_point.get("instances")
        if isinstance(op_point, dict) and isinstance(op_point.get("instances"), dict)
        else {}
    )
    saved_keys: set[str] = set()
    devices_with_scalars = 0
    vdsat_devices = 0
    region_only_devices = 0
    for params in instances.values():
        if not isinstance(params, dict):
            continue
        numeric_keys = {
            key for key, value in params.items()
            if key in _SAVE_OP_POINT_EFFECT_KEYS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        }
        scalar_keys = numeric_keys - {"region"}
        if scalar_keys:
            devices_with_scalars += 1
            saved_keys.update(scalar_keys)
        elif (
            isinstance(params.get("region"), (int, float))
            and not isinstance(params.get("region"), bool)
            and math.isfinite(float(params["region"]))
        ):
            region_only_devices += 1
        if "vdsat" in numeric_keys:
            vdsat_devices += 1

    issues: list[str] = []
    if requested <= 0:
        issues.append("saveOpPoint requested zero DUT instances")
    if instances and devices_with_scalars == 0:
        issues.append("instance OP readback returned only sparse metadata")
    if not instances:
        issues.append("no instance OP rows returned")
    ok = requested > 0 and devices_with_scalars > 0
    return {
        "ok": ok,
        "opPointsRequested": requested,
        "instancesReturned": len(instances),
        "devicesWithSavedScalars": devices_with_scalars,
        "regionOnlyDevices": region_only_devices,
        "vdsatDevices": vdsat_devices,
        "savedScalarKeys": sorted(saved_keys),
        "optionalScalarKeysMissing": (
            ["vdsat"] if ok and vdsat_devices == 0 else []
        ),
        "issues": issues,
    }

# Matches the leading identifier of a SKILL call: "funcName(".
_SKILL_ENTRYPOINT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Matches any identifier immediately followed by "(" anywhere in the expr.
# Used to reject nested calls like safeReadSchematic(load("/evil.il") ...).
_SKILL_ANY_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Forbidden primitives for _upload_skill_inline. Our safe_*.il / helpers.il
# files do not call any of these; blocking them keeps inline upload a
# scripted-procedure channel only, not an arbitrary-code channel. Note we
# do NOT block `eval`, `load`, `errset`, `infile`, `outfile`: those appear
# legitimately in safe_ocean.il / helpers.il / safe_patch_netlist.il.
_SKILL_INLINE_FORBIDDEN_RE = re.compile(
    r"\b(system|popen|exec|shell|evalstring|ipcbegin)\s*\(",
    re.IGNORECASE,
)
_SKILL_INLINE_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB — our largest .il is ~60 KB

# Stage 1 rev 4: validation patterns for the generic dump API. Mirror
# the SKILL-side safeOcean_validSigName / validProbePath / validKind.
_SIG_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,31}\Z")
_PROBE_PATH_RE = re.compile(r"\A(/[A-Za-z_][A-Za-z0-9_]*){1,8}\Z")

# T2 (2026-05-18): quoted net refs are the only string literals allowed
# inside Maestro calculator expressions. The path source is derived from
# _PROBE_PATH_RE so signal/probe validation has one regex owner.
#
# IMPORTANT for future contributors: every function added here MUST also
# be added to _MAESTRO_EXPR_ALLOWED_FUNCS below. The function-call scan
# in _validate_maestro_expr runs on the *original* expr (not the
# stripped one), so a name that only appears in this set will get its
# quoted-token form past the forbidden-char gate but then get rejected
# by the call-allow-list — confusing failure mode. Keep both in sync.
_MAESTRO_EXPR_NET_REF_FUNCS = frozenset({
    "VT", "IT", "VF", "IF", "v", "i", "VS", "IS", "getData",
})
_MAESTRO_EXPR_NET_REF_PATH_MAX_LEN = 128
_MAESTRO_EXPR_NET_REF_PLACEHOLDER = "__NETREF__"
_MAESTRO_EXPR_NET_REF_PATH_RE_SOURCE = (
    _PROBE_PATH_RE.pattern.removeprefix(r"\A").removesuffix(r"\Z")
)
_MAESTRO_EXPR_NET_REF_PATH_RE_SOURCE = (
    _MAESTRO_EXPR_NET_REF_PATH_RE_SOURCE.replace("(", "(?:", 1)
)
_MAESTRO_EXPR_NET_REF_FUNC_RE_SOURCE = "|".join(
    sorted(_MAESTRO_EXPR_NET_REF_FUNCS, key=len, reverse=True)
)
_MAESTRO_EXPR_NET_REF_RE = re.compile(
    rf'\b({_MAESTRO_EXPR_NET_REF_FUNC_RE_SOURCE})'
    rf'\("({_MAESTRO_EXPR_NET_REF_PATH_RE_SOURCE})"\)'
)
_MAESTRO_EXPR_ALLOWED_STRING_LITERALS = frozenset({
    "rising", "falling", "either",
})
_MAESTRO_EXPR_STRING_RE = re.compile(r'"([^"]*)"')
_MAESTRO_EXPR_STRING_PLACEHOLDER = "__STRING_LITERAL__"
_OCEAN_SIGNAL_KINDS = frozenset({"V", "I", "Vdiff", "Vsum_half"})
_OCEAN_CROSS_DIRS = frozenset({"rising", "falling", "either"})
# Kinds whose waveform is built from exactly N probe paths. Used by PC
# validation to reject a bad (kind, paths) shape before reaching SKILL.
_OCEAN_KIND_ARITY = {
    "V": 1,
    "I": 1,
    "Vdiff": 2,
    "Vsum_half": 2,
}


def _scrub(value: Any) -> Any:
    """Recursively redact foundry names and absolute paths in string values.

    Intended for use on exception messages and log payloads so that
    sensitive substrings (real foundry cell/lib names, filesystem paths
    that contain usernames or project roots) cannot leak to the LLM or
    external callers. Dicts and lists recurse; other types pass through.
    """
    if isinstance(value, str):
        out = _FOUNDRY_LEAK_RE.sub("<redacted>", value)
        # UNC (both backslash and forward-slash forms) must run before
        # the drive-letter / unix-root regexes, otherwise the leading
        # separators leave partial server residue.
        out = _UNC_PATH_RE.sub("<path>", out)
        out = _FORWARD_UNC_PATH_RE.sub("<path>", out)
        out = _ABS_WIN_PATH_RE.sub("<path>", out)
        out = _ABS_UNIX_PATH_RE.sub("<path>", out)
        return out
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def scrub(value: Any) -> Any:
    """Public wrapper around :func:`_scrub` for cross-module use.

    Same redaction semantics as ``_scrub`` (foundry tokens, abs/UNC
    paths). Callers outside this module — notably
    ``llm_client.py`` scrubbing ``reasoning_content`` before it is
    replayed into LLM conversation history — should use this name
    rather than reaching into the underscore-prefixed implementation.
    """
    return _scrub(value)


def assert_llm_feedback_safe(text: str, *, context: str = "feedback") -> str:
    """Fail closed if LLM-facing feedback still contains sensitive tokens.

    This is deliberately a *post*-sanitization gate. SafeBridge and SKILL
    helpers should already strip PDK data at source; this function catches
    any residual foundry/model-shaped token or absolute path immediately
    before text is replayed into an LLM prompt.
    """
    safe_context = (
        context if isinstance(context, str)
        and re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", context)
        else "feedback"
    )
    if not isinstance(text, str):
        raise TypeError(
            f"{safe_context} must be a string; "
            f"got type={type(text).__name__}"
        )
    hits: list[str] = []
    if _FOUNDRY_LEAK_RE.search(text):
        hits.append("foundry/model token")
    if (
        _UNC_PATH_RE.search(text)
        or _FORWARD_UNC_PATH_RE.search(text)
        or _ABS_WIN_PATH_RE.search(text)
        or _ABS_UNIX_PATH_RE.search(text)
    ):
        hits.append("absolute path")
    if hits:
        raise ValueError(
            f"{safe_context} failed sensitive-token scan "
            f"({', '.join(hits)}); withheld from LLM"
        )
    return text


def _cap_remote_output(value: Any, *, label: str) -> Any:
    """Reject cobi-returned string values larger than the size cap.

    Closes the out-bound DoS gap noted alongside the in-bound 128-char
    input cap (``_RF_STRING_MAX_LEN``): without this, a misbehaving
    remote could return arbitrarily large SKILL output which would
    flow unchecked into :func:`_scrub`, log formatters, and the LLM
    context. Non-string values pass through unchanged (cap only bounds
    string-shaped DoS; dict/list payloads are checked by their own
    structural validators downstream).
    """
    if isinstance(value, str) and len(value) > _REMOTE_OUTPUT_MAX_CHARS:
        raise ValueError(
            f"{label}: cobi return size {len(value)} chars exceeds cap "
            f"{_REMOTE_OUTPUT_MAX_CHARS}; refusing to ingest (possible "
            "DoS or PDK-leak surface — investigate remote behavior "
            "rather than raising the cap)."
        )
    return value


def _validate_name(name: str, label: str = "name") -> None:
    """Validate a lib/cell/instance name against injection attacks.

    Track C v2 R2 (2026-05-15): hard length cap of 128 chars (CDB
    identifier upper bound; Maestro/Cadence rejects longer names
    server-side anyway). The cap defends against unbounded-string DoS
    where the SKILL transport / log-formatter might choke on a
    multi-megabyte LLM-supplied identifier — bound the input here so
    every downstream consumer sees a sane size.
    """
    if not isinstance(name, str):
        raise ValueError(
            f"Invalid {label} (type={type(name).__name__}); "
            "must be a string."
        )
    if len(name) > 128:
        raise ValueError(
            f"Invalid {label} length ({len(name)} > 128 cap). "
            "Cadence CDB identifiers are bounded; reject upstream."
        )
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid {label} (len={len(name)}). "
            "Only alphanumeric, underscore, dot, and hyphen are allowed."
        )


def _is_safe_op_result_path(path: str) -> bool:
    """True for schematic result paths such as /Vout_p or /I0/M0 only."""
    if not isinstance(path, str) or not _SAFE_OP_RESULT_PATH_RE.fullmatch(path):
        return False
    if _FOUNDRY_LEAK_RE.search(path):
        return False
    if (
        _UNC_PATH_RE.search(path)
        or _FORWARD_UNC_PATH_RE.search(path)
        or _ABS_WIN_PATH_RE.search(path)
        or _ABS_UNIX_PATH_RE.search(path)
    ):
        return False
    return True


def _normalize_maestro_op_paths(
    paths: Iterable[str] | None,
    *,
    label: str,
    max_items: int,
) -> list[str]:
    """Validate, deduplicate, and bound requested Maestro OP result paths."""
    if paths is None:
        return []
    if isinstance(paths, str):
        raise ValueError(f"{label} must be an iterable of paths, not str")
    try:
        raw_paths = list(paths)
    except TypeError as exc:
        raise ValueError(f"{label} must be an iterable of paths") from exc
    if len(raw_paths) > max_items:
        raise ValueError(f"{label} exceeds max items ({max_items})")
    out: list[str] = []
    seen: set[str] = set()
    for idx, path in enumerate(raw_paths):
        if not _is_safe_op_result_path(path):
            raise ValueError(
                f"{label} item {idx} has invalid path shape "
                "(must be a schematic result path, len<=255)"
            )
        if path not in seen:
            out.append(path)
            seen.add(path)
    return out


def _format_param_atom_value(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("Boolean parameter values are not allowed")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite parameter values are not allowed")
        return format(value, ".12g")
    if isinstance(value, str):
        if value != value.strip() or not _PARAM_ATOM_RE.fullmatch(value):
            raise ValueError(
                "Unsafe parameter value "
                f"(type=str len={len(value)}). "
                "Only numeric literals or engineering-unit strings are allowed."
            )
        return value
    raise ValueError(
        f"Unsupported parameter value type: {type(value).__name__}"
    )


def _validate_rf_string_value(value: str, *, label: str) -> None:
    if len(value) > _RF_STRING_MAX_LEN:
        raise ValueError(
            f"{label} string length {len(value)} exceeds max "
            f"{_RF_STRING_MAX_LEN}."
        )
    if _FOUNDRY_LEAK_RE.search(value):
        raise ValueError(f"{label} contains {_RF_FOUNDRY_REDACTED}.")


def _format_maestro_analysis_token_value(value: Any, *, label: str) -> str:
    """Format a safe string token/probe for RF analysis options."""
    if not isinstance(value, str):
        raise ValueError(
            f"{label} must be a string token; got "
            f"type={type(value).__name__}"
        )
    _validate_rf_string_value(value, label=label)
    if not value:
        raise ValueError(f"{label} must be non-empty string (len<=128)")
    if _SAFE_NET_NAME_RE.fullmatch(value) or _PROBE_PATH_RE.fullmatch(value):
        return value
    _validate_name(value, label)
    return value


def _validate_maestro_analysis_option_key(analysis: str, key: Any) -> str:
    if not isinstance(key, str) or not _SAFE_PARAM_NAME_RE.fullmatch(key):
        raise ValueError(
            f"Invalid analysis option key (len={len(key) if isinstance(key, str) else -1}). "
            "Must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$."
        )
    key_lower = key.lower()
    allowed = _MAESTRO_RF_ANALYSIS_OPTIONS.get(analysis)
    if allowed is not None and key_lower not in allowed:
        raise ValueError(
            f"Analysis {_scrub(repr(analysis))} option "
            f"{_scrub(repr(key))} is not allowed. "
            f"Allowed options: {sorted(allowed)}"
        )
    return key_lower


def _format_rf_bounded_int(value: Any, *, label: str, lo: int, hi: int) -> str:
    if isinstance(value, bool):
        raise ValueError(f"Boolean parameter values are not allowed for {label}.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"{label} must be finite and in accepted range [{lo}, {hi}]; "
                f"got {value!r}."
            )
        if not value.is_integer():
            raise ValueError(
                f"{label} must be an integer in accepted range [{lo}, {hi}]; "
                f"got {value!r}."
            )
        parsed = int(value)
    elif isinstance(value, str):
        _validate_rf_string_value(value, label=label)
        if not re.fullmatch(r"[+-]?\d+", value):
            raise ValueError(
                f"{label} must be an integer in accepted range [{lo}, {hi}]; "
                f"got {_scrub(repr(value))}."
            )
        parsed = int(value, 10)
    else:
        raise ValueError(
            f"{label} must be an integer in accepted range [{lo}, {hi}]; "
            f"got type={type(value).__name__}."
        )
    if parsed < lo or parsed > hi:
        raise ValueError(
            f"{label} value {parsed!r} outside accepted range [{lo}, {hi}]."
        )
    return str(parsed)


def _parse_rf_bounded_positive_number(
    value: Any, *, label: str, lo: float, hi: float,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Boolean parameter values are not allowed for {label}.")
    if isinstance(value, (int, float)):
        try:
            parsed = float(value)
        except OverflowError:
            parsed = math.inf if value > 0 else -math.inf
    elif isinstance(value, str):
        _validate_rf_string_value(value, label=label)
        match = _RF_NUMERIC_RE.fullmatch(value)
        if not match:
            raise ValueError(
                f"{label} must be a finite number in accepted range "
                f"[{lo}, {hi}]."
            )
        base, suffix = match.groups()
        if suffix not in _RF_ENGINEERING_SUFFIX_SCALE:
            raise ValueError(
                f"{label} has unsupported unit suffix (len={len(suffix)})."
            )
        parsed = float(base) * _RF_ENGINEERING_SUFFIX_SCALE[suffix]
    else:
        raise ValueError(
            f"{label} must be a finite number in accepted range [{lo}, {hi}]; "
            f"got type={type(value).__name__}."
        )
    if not math.isfinite(parsed) or parsed < lo or parsed > hi:
        raise ValueError(
            f"{label} value {parsed!r} outside accepted range [{lo}, {hi}]."
        )
    return parsed


def _ensure_rf_atom_length(atom: str, *, label: str) -> str:
    if len(atom) > _RF_STRING_MAX_LEN:
        raise ValueError(
            f"{label} formatted length {len(atom)} exceeds "
            f"{_RF_STRING_MAX_LEN} character cap."
        )
    return atom


def _format_rf_bounded_positive_number(
    value: Any, *, label: str, lo: float, hi: float,
) -> str:
    _parse_rf_bounded_positive_number(value, label=label, lo=lo, hi=hi)
    return _ensure_rf_atom_length(_format_param_atom_value(value), label=label)


def _format_maestro_analysis_option_value(
    analysis: str, key: str, value: Any,
) -> str:
    """Format one Maestro analysis option with RF-specific string gates."""
    key_lower = _validate_maestro_analysis_option_key(analysis, key)
    label = f"{analysis}.{key_lower}"
    if analysis in _MAESTRO_RF_ANALYSIS_OPTIONS:
        if isinstance(value, str):
            _validate_rf_string_value(value, label=label)

        enum_vals = _MAESTRO_RF_ENUM_OPTIONS.get((analysis, key_lower))
        if enum_vals is not None:
            if not isinstance(value, str) or value not in enum_vals:
                raise ValueError(
                    f"Invalid {analysis}.{key_lower} value; allowed: "
                    f"{sorted(enum_vals)}"
                )
            return _ensure_rf_atom_length(value, label=label)

        if (analysis, key_lower) in _MAESTRO_RF_TOKEN_OPTIONS:
            return _ensure_rf_atom_length(
                _format_maestro_analysis_token_value(value, label=label),
                label=label,
            )

        int_range = _MAESTRO_RF_INT_RANGES.get((analysis, key_lower))
        if int_range is not None:
            lo, hi = int_range
            return _ensure_rf_atom_length(
                _format_rf_bounded_int(value, label=label, lo=lo, hi=hi),
                label=label,
            )

        positive_range = _MAESTRO_RF_POSITIVE_RANGES.get((analysis, key_lower))
        if positive_range is not None:
            lo, hi = positive_range
            return _format_rf_bounded_positive_number(
                value, label=label, lo=lo, hi=hi,
            )

    enum_vals = _OCEAN_ENUM_KWARGS.get(key_lower)
    if enum_vals is not None:
        if not isinstance(value, str) or value not in enum_vals:
            raise ValueError(
                f"Invalid {key!r} value; allowed: {sorted(enum_vals)}"
            )
        return _ensure_rf_atom_length(value, label=label)

    return _ensure_rf_atom_length(_format_param_atom_value(value), label=label)


def _sanitize_scaffold_cell(raw: dict) -> dict:
    """Defense-in-depth scrub of one scaffold cell entry.

    The SKILL side already filters, but we re-assert the shape on PC:
    lib pinned to "GENERIC_PDK"; cell and every pin name/direction
    coerced to strings. Pins with a name that does not pass the safe
    identifier regex are silently dropped — a malformed entry should
    not propagate into the rendered Markdown.
    """
    out: dict[str, Any] = {"lib": "GENERIC_PDK"}
    cell_val = raw.get("cell", "")
    out["cell"] = cell_val if isinstance(cell_val, str) else ""
    pins_raw = raw.get("pins") or []
    pins: list[dict[str, str]] = []
    if isinstance(pins_raw, list):
        for entry in pins_raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            direction = entry.get("direction")
            if not isinstance(name, str) or not name:
                continue
            if not _SAFE_PARAM_NAME_RE.fullmatch(name):
                continue
            if not isinstance(direction, str):
                direction = "unknown"
            pins.append({"name": name, "direction": direction})
    out["pins"] = pins
    return out


class SafeBridge:
    """PDK-safe wrapper around VirtuosoClient and OCEAN SKILL helpers.

    All data returned by read methods is sanitized: PDK cell names are
    replaced with generic names, model parameters are stripped, and library
    names are replaced with GENERIC_PDK.

    All write methods enforce a parameter whitelist. Simulation happens
    exclusively through ``run_ocean_sim`` (OCEAN over SKILL); the legacy
    direct-Spectre ``simulate()`` path was removed in Stage 1 rev 2
    (2026-04-18).
    """

    def __init__(
        self,
        client: VirtuosoClient,
        pdk_map_path: str,
        skill_dir: str | Path | None = None,
        remote_skill_dir: str | None = None,
    ):
        self.client = client
        with open(pdk_map_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        # PC never holds real foundry cell names. Only the set of generic
        # aliases that remote host is allowed to return. Legacy configs with a
        # "cell_map:" section are rejected â€“ they must be migrated so no
        # foundry names live in the PC-side repo.
        if "cell_map" in cfg:
            raise ValueError(
                "pdk_map.yaml contains a deprecated 'cell_map:' section. "
                "Remove it and declare 'valid_aliases:' instead; real "
                "foundry cell names must live only on the remote host."
            )
        aliases = cfg.get("valid_aliases") or []
        if not aliases:
            raise ValueError(
                "pdk_map.yaml must declare a non-empty 'valid_aliases:' list."
            )
        self._known_aliases: set[str] = {str(name) for name in aliases}
        self.generic_cell_name: str = cfg.get(
            "generic_cell_name", "GENERIC_DEVICE"
        )
        # Rev 5: stored as a lowercased frozenset so that
        # _is_allowed_param_name can perform a case-insensitive EXACT match
        # (substring would false-positive on e.g. "stk1" / "mu0level").
        self.model_info_keys: frozenset[str] = frozenset(
            str(param).lower() for param in cfg.get("model_info_keys", [
                "toxe", "u0", "vth0", "k1", "k2", "pclm",
                "dvt0", "dvt1", "nfactor", "cdsc", "eta0", "dsub",
            ])
        )
        self.allowed_params: set[str] = {
            str(param).lower() for param in cfg.get("allowed_params", [
                # Stage 1 rev 1 (2026-04-18): extended per LC_VCO_spec.md Â§4.
                # Added nfin/fingers (FinFET fin count + finger count
                # per gate), idc/vdc (bias-current and bias-voltage source
                # instance props). Layer-2 blocked-word regex still clamps
                # attack surface for names outside this core set.
                "w", "l", "nf", "m", "multi", "wf", "r", "c",
                "nfin", "fingers", "idc", "vdc",
            ])
        }
        self._skill_loaded = False
        self._skill_dir = Path(skill_dir) if skill_dir else (
            Path(__file__).resolve().parent.parent / "skill"
        )
        self._remote_skill_dir = remote_skill_dir
        # P1.3 scope binding: when set_scope() has been called, every
        # subsequent read/write must match the bound (lib, cell) pair.
        # This is defense-in-depth: agent.run() passes CLI-supplied lib/cell
        # to every method anyway, but an LLM-constructed or bugged caller
        # cannot slip in an unauthorized library after scope is bound.
        self._scope_lib: str | None = None
        self._scope_cell: str | None = None
        self._scope_tb_cell: str | None = None
        # Track C v2 (2026-05-15): set of Maestro test names this bridge
        # has already created via ``create_maestro_test``. PC-side cache
        # to short-circuit the same-name attempt within one bridge before
        # we even probe the remote. The authoritative dedup gate is the
        # ``_list_remote_maestro_tests`` SKILL probe that runs on every
        # create — this set just makes the no-op path cheap.
        self._created_maestro_tests: set[str] = set()
        # Track C v2 R2/R3 (2026-05-15): set of ``(name, resolved_test,
        # session)`` triples this bridge has issued ``maeAddOutput``
        # for, regardless of caller (Option I sync OR v2
        # ``apply_maestro_setup``). The R2 implementation keyed on bare
        # name and so collapsed legitimate same-name-different-test
        # outputs into a single bucket (codex R3 P2 — see
        # ``test_outputs_dedup_disambiguates_by_test``). The tuple key
        # treats two Maestro tests' "VOUT_rms" rows as independent.
        # The PC-side cache is short-circuit only; the authoritative
        # state lives in Maestro server-side. New SafeBridge instances
        # start with an empty set even if the remote already has
        # outputs — that's a known trade-off; the v2-wins remove-add
        # path handles the case where the v2 dispatcher proposes a
        # name the *bridge* hasn't seen but the *remote* has.
        self._added_maestro_outputs: set[tuple[str, str, str]] = set()
        # Track C T1 R2/R3 (2026-05-17): local per-bridge cache only,
        # not authoritative; it guards same-session LLM ordering mistakes,
        # while Maestro itself remains the final missing-pss arbiter.
        self._configured_maestro_analyses: dict[str, set[str]] = {}
        # Stage 1 rev 12 (2026-04-20): PSF directory from the most recent
        # ``run_ocean_sim``. Captured *before* _scrub() redacts absolute
        # paths so the OceanWorker subprocess can openResults(psfDir) on
        # remote host. Never exposed to the LLM â€“ agent reads directly.
        self._last_results_dir: str | None = None
        self._load_skill_helpers()

    @property
    def last_results_dir(self) -> str | None:
        """Un-scrubbed remote-side psf dir from the last run_ocean_sim().

        Returns None when no sim has run yet or the SKILL side did not
        report a valid resultsDir. Never leaked into LLM prompts â€“ only
        OceanWorker subprocess consumption.
        """
        return self._last_results_dir

    # ------------------------------------------------------------------ #
    #  Scope binding - restrict all operations to a single (lib, cell)
    # ------------------------------------------------------------------ #

    def set_scope(
        self, lib: str, cell: str, tb_cell: str | None = None,
    ) -> None:
        """Bind this bridge to a single (lib, cell) pair.

        After this is called, `read_circuit`, `read_op_point`, and
        `set_params` will reject any call whose lib/cell does not match.
        Can only be set once per bridge instance â€“ to operate on another
        cell, construct a new bridge.

        ``tb_cell`` (optional) is the testbench cell that owns the
        Maestro setup. Required for ``write_and_save_maestro`` -- if
        omitted, that method will fail-fast with a clear error.
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        if tb_cell is not None:
            _validate_name(tb_cell, "tb_cell")
        if self._scope_lib is not None or self._scope_cell is not None:
            raise RuntimeError(
                "SafeBridge scope already bound; construct a new bridge "
                "to operate on another cell."
            )
        self._scope_lib = lib
        self._scope_cell = cell
        self._scope_tb_cell = tb_cell
        logger.info(
            "SafeBridge scope bound (lib_len=%d, cell_len=%d%s)",
            len(lib), len(cell),
            f", tb_cell_len={len(tb_cell)}" if tb_cell else "",
        )

    def _check_scope(self, lib: str, cell: str) -> None:
        """Enforce the bound scope if one is set."""
        if self._scope_lib is None and self._scope_cell is None:
            return
        if lib != self._scope_lib or cell != self._scope_cell:
            raise ValueError(
                "lib/cell outside bound scope "
                f"(got lib_len={len(lib)} cell_len={len(cell)})."
            )

    # ------------------------------------------------------------------ #
    #  Read direction - auto-sanitize before returning to caller / LLM
    # ------------------------------------------------------------------ #

    def read_circuit(self, lib: str, cell: str) -> dict:
        """Read schematic topology from Virtuoso and return sanitized data.

        Uses remote-side safeReadSchematic() for source filtering, then
        applies Python-side _sanitize() as defense-in-depth.
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        self._check_scope(lib, cell)
        if self._skill_loaded:
            raw = self._execute_skill_json(
                f'safeReadSchematic("{lib}" "{cell}")'
            )
        else:
            raw = self._execute_skill_json(
                f'read_schematic("{lib}" "{cell}")'
            )
        return self._sanitize(raw)

    def read_circuit_hierarchical(
        self,
        lib: str,
        cell: str,
        max_depth: int = 10,
    ) -> dict:
        """Hierarchical, same-library BFS schematic read.

        Wraps the remote-side ``safeReadSchematicDeep`` SKILL helper. Returns
        a dict shaped like::

            {
              "lib":  "GENERIC_PDK",
              "cell": "<root cell>",
              "max_depth":         <effective cap applied by SKILL>,
              "max_depth_reached": <deepest depth actually visited>,
              "depth_limit_hit":   <bool — did BFS stop early on cap>,
              "root":     {handle:"ROOT", depth:0, lib:"GENERIC_PDK",
                           cell:"...", instances:[...], pins:[...]},
              "subcells": [ {handle:"SUBCELL_0", depth:1, ...}, ... ],
            }

        Each instance row inside ``root`` / ``subcells[i]`` carries the
        same PDK-safe payload as ``read_circuit`` (``cell`` aliased,
        ``lib`` pinned to ``GENERIC_PDK``, whitelisted params, net map).
        Rows whose master is another same-library schematic additionally
        carry a ``subcell: "SUBCELL_<n>"`` back-reference pointing at the
        dedup'd entry in the top-level ``subcells`` list.

        ``max_depth`` is clamped SKILL-side to ``[0, 50]`` — 0 reads the
        root only (same pin/instance visibility as ``read_circuit``), 50
        is a hard safety cap that bounds BFS even if the same-lib seen
        map somehow fails. Python-side rejects obviously bad values
        (negative / non-int) up front so we don't emit a SKILL call that
        will round-trip a bogus integer.

        Cross-library instance masters are NEVER opened — they remain
        leaves with ``cellAlias()`` naming, preserving the PDK posture
        of the flat reader (foundry cell names never leave remote host).
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        self._check_scope(lib, cell)
        # H1 round-2 (codex review): max_depth contract tightened to
        # [1, 50]. `0` is redundant with the flat read path (same
        # output as --depth 1), so the hierarchical API refuses it
        # outright. bool is rejected explicitly because it is an int
        # subclass in Python — `True`/`False` would otherwise slip
        # through the plain isinstance(int) check. float is rejected
        # so we never round-trip a non-integer literal to SKILL.
        if isinstance(max_depth, bool) or not isinstance(max_depth, int):
            raise ValueError(
                f"max_depth must be a plain int "
                f"(got {type(max_depth).__name__})"
            )
        if max_depth < 1 or max_depth > 50:
            raise ValueError(
                f"max_depth must be within [1, 50] (got {max_depth})"
            )
        if not self._skill_loaded:
            raise RuntimeError(
                "read_circuit_hierarchical requires the remote-side SKILL "
                "helpers. Pass --remote-skill-dir so "
                "safe_read_schematic.il can be loaded."
            )
        raw = self._execute_skill_json(
            f'safeReadSchematicDeep("{lib}" "{cell}" {max_depth})'
        )
        if not raw.get("ok", False):
            raise RuntimeError(
                "safeReadSchematicDeep failed: "
                f"{_scrub(str(raw.get('error', 'unknown')))}"
            )
        return self._sanitize_hierarchical(raw, cell)

    def _sanitize_hierarchical(self, raw: dict, root_cell: str) -> dict:
        """Defense-in-depth scrub for a hierarchical schematic payload.

        Runs the flat ``_sanitize`` on each cellview entry (root plus
        every subcell), validates the ``subcell`` back-reference on
        every instance row against ``_SUBCELL_HANDLE_RE`` (dropping
        bogus values), and coerces the top-level metadata (``lib`` ->
        ``GENERIC_PDK``, depth counters to int, ``depth_limit_hit`` to
        bool) so the caller gets a stable shape regardless of SKILL
        regressions.
        """
        root_raw = raw.get("root") or {}
        if not isinstance(root_raw, dict):
            raise RuntimeError(
                "safeReadSchematicDeep returned malformed root"
            )
        subcells_raw = raw.get("subcells") or []
        if not isinstance(subcells_raw, list):
            raise RuntimeError(
                "safeReadSchematicDeep returned malformed subcells"
            )

        root = self._sanitize_cellview_entry(root_raw, expected_handle="ROOT")
        subcells: list[dict[str, Any]] = []
        seen_handles: set[str] = {"ROOT"}
        for entry in subcells_raw:
            if not isinstance(entry, dict):
                continue
            handle = entry.get("handle")
            if not isinstance(handle, str) or not _SUBCELL_HANDLE_RE.fullmatch(
                    handle):
                logger.warning(
                    "hierarchical reader dropped subcell with bad handle "
                    "(len=%d)", len(handle) if isinstance(handle, str) else 0
                )
                continue
            if handle in seen_handles:
                logger.warning(
                    "hierarchical reader dropped duplicate subcell handle "
                    "(len=%d)", len(handle)
                )
                continue
            seen_handles.add(handle)
            subcells.append(self._sanitize_cellview_entry(entry))

        # H1 round-2 (codex review, Blocker #1 defense-in-depth):
        # Cross-check every instance's `subcell` back-reference against
        # the set of handles we actually emitted. If SKILL ever re-
        # introduces a dangling-ref regression (e.g. allocated a handle
        # but failed to produce the matching subcells[] entry), drop
        # the field here so callers never see a pointer into the void.
        actual_handles = {"ROOT"} | {s["handle"] for s in subcells}
        for cellview in (root, *subcells):
            for inst in cellview.get("instances") or []:
                if not isinstance(inst, dict):
                    continue
                ref = inst.get("subcell")
                if ref is None:
                    continue
                if ref not in actual_handles:
                    inst.pop("subcell", None)
                    logger.warning(
                        "hierarchical reader dropped dangling subcell "
                        "ref (len=%d) on cellview %s",
                        len(ref) if isinstance(ref, str) else 0,
                        cellview.get("handle", "?"),
                    )

        max_depth = raw.get("max_depth")
        max_depth_reached = raw.get("max_depth_reached")
        depth_limit_hit = raw.get("depth_limit_hit")
        return {
            "lib": "GENERIC_PDK",
            "cell": root_cell,
            "max_depth": int(max_depth) if isinstance(
                max_depth, (int, float)) else 0,
            "max_depth_reached": int(max_depth_reached) if isinstance(
                max_depth_reached, (int, float)) else 0,
            "depth_limit_hit": bool(depth_limit_hit),
            "root": root,
            "subcells": subcells,
        }

    def _sanitize_cellview_entry(
        self, entry: dict, expected_handle: str | None = None,
    ) -> dict:
        """Scrub a single cellview dict (root or subcell).

        Applies the existing ``_sanitize`` to ``instances`` (so each row
        gets its ``cell`` aliased and ``lib`` pinned to GENERIC_PDK),
        then re-validates the per-row ``subcell`` back-reference against
        ``_SUBCELL_HANDLE_RE``. Rows with a bogus reference have the
        field silently removed — a PDK-safe leaf representation remains.
        """
        # Copy shape that _sanitize expects: {"instances":[...], ...}.
        # _sanitize mutates instance dicts in place and returns a deep
        # copy of the whole payload, so we can use it as-is.
        sanitized = self._sanitize({
            "instances": entry.get("instances") or [],
        })
        instances = sanitized.get("instances") or []
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            ref = inst.get("subcell")
            if ref is None:
                continue
            if not isinstance(ref, str) or not _SUBCELL_HANDLE_RE.fullmatch(
                    ref):
                inst.pop("subcell", None)
                logger.warning(
                    "hierarchical reader dropped bad subcell ref on "
                    "instance (len=%d)",
                    len(ref) if isinstance(ref, str) else 0,
                )

        pins_raw = entry.get("pins") or []
        pins: list[dict[str, str]] = []
        if isinstance(pins_raw, list):
            for pin in pins_raw:
                if not isinstance(pin, dict):
                    continue
                name = pin.get("name")
                direction = pin.get("direction")
                if not isinstance(name, str) or not name:
                    continue
                if not _SAFE_PARAM_NAME_RE.fullmatch(name):
                    continue
                if not isinstance(direction, str):
                    direction = "unknown"
                pins.append({"name": name, "direction": direction})

        handle = entry.get("handle")
        if expected_handle is not None:
            handle = expected_handle
        elif not isinstance(handle, str) or not _SUBCELL_HANDLE_RE.fullmatch(
                handle):
            handle = ""

        cell_name = entry.get("cell", "")
        if not isinstance(cell_name, str):
            cell_name = ""

        depth = entry.get("depth")
        return {
            "handle": handle,
            "depth": int(depth) if isinstance(depth, (int, float)) else 0,
            "lib": "GENERIC_PDK",
            "cell": cell_name,
            "instances": instances,
            "pins": pins,
        }

    def read_op_point(self, lib: str, cell: str) -> dict:
        """Read and sanitize DC operating-point data.

        Uses remote-side safeReadOpPoint() for source filtering, then
        applies Python-side _sanitize_op_point() as defense-in-depth.
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        self._check_scope(lib, cell)
        if self._skill_loaded:
            raw = self._execute_skill_json(
                f'safeReadOpPoint("{lib}" "{cell}")'
            )
        else:
            raw = self._execute_skill_json(
                f'read_op_point("{lib}" "{cell}")'
            )
        return self._sanitize_op_point(raw)

    def read_op_point_after_tran(self) -> dict:
        """Read per-instance DC op-point from the tranOp sub-result.

        Works only after a tran run has been executed via run_ocean_sim()
        (which populates self._last_results_dir). Passes psfDir to the
        SKILL side so it can openResults() itself — rev 13b made the
        SKILL self-contained because A2's iter-start unselectResult()
        cleared the caller-side selectResult binding.

        Returned dict structure::

            {
              "instances": {
                "/I0/M1": {
                  "vgs": 1.97, "vds": 0.0093, "gm": 2.8e-5,
                  "region": 1, "region_label": "triode",
                  "vov": 1.572,   # derived = vgs - vth if both present
                  ...
                },
                ...
              }
            }

        Stage 1 rev 7 (2026-04-19); rev 13b (2026-04-21) added psf_dir.
        """
        psf_dir = self._last_results_dir
        if not psf_dir:
            raise RuntimeError(
                "read_op_point_after_tran: no results dir available "
                "(run_ocean_sim has not been called yet this session)"
            )
        if not _SAFE_PSF_DIR_RE.fullmatch(psf_dir):
            raise RuntimeError(
                f"read_op_point_after_tran: psf_dir contains unsafe "
                f"characters (len={len(psf_dir)})"
            )
        raw = self._execute_skill_json(
            f'safeReadOpPointAfterTran("{psf_dir}")'
        )
        sanitized = self._sanitize_op_point(raw)
        return self._decorate_op_point(sanitized)

    def read_dc_op_point_from_results(
        self,
        *,
        nets: Iterable[str] | None = None,
        instances: Iterable[str] | None = None,
    ) -> dict:
        """Read request-scoped DC OP data from the latest OCEAN PSF run.

        This is the current-run AC/DC companion to
        :meth:`read_maestro_dc_op_point`. It uses ``self._last_results_dir``
        populated by ``run_ocean_sim()``, opens that PSF directory on the
        SKILL side, and reads only the requested node/instance paths from
        ``dc`` / ``instance`` result containers.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "read_dc_op_point_from_results requires the remote-side "
                "SKILL helpers. Pass --remote-skill-dir so "
                "safe_read_op_point.il can be loaded."
            )
        psf_dir = self._last_results_dir
        if not psf_dir:
            raise RuntimeError(
                "read_dc_op_point_from_results: no results dir available "
                "(run_ocean_sim has not been called yet this session)"
            )
        if not _SAFE_PSF_DIR_RE.fullmatch(psf_dir):
            raise RuntimeError(
                f"read_dc_op_point_from_results: psf_dir contains unsafe "
                f"characters (len={len(psf_dir)})"
            )
        net_list = _normalize_maestro_op_paths(
            nets,
            label="nets",
            max_items=_MAX_MAESTRO_OP_NETS,
        )
        inst_list = _normalize_maestro_op_paths(
            instances,
            label="instances",
            max_items=_MAX_MAESTRO_OP_INSTANCES,
        )
        if not net_list and not inst_list:
            raise ValueError(
                "read_dc_op_point_from_results requires at least one net "
                "or instance path."
            )

        def _skill_list(values: list[str]) -> str:
            return "list(" + " ".join(f'"{value}"' for value in values) + ")"

        raw = self._execute_skill_json(
            f'safeReadDcOpPointFromResults("{psf_dir}" '
            f"{_skill_list(net_list)} {_skill_list(inst_list)})",
            timeout=120,
        )
        if not raw.get("ok", False):
            raise RuntimeError(
                "safeReadDcOpPointFromResults failed: "
                f"{_scrub(str(raw.get('error', 'unknown')))}"
            )
        sanitized = self._sanitize_maestro_dc_op_point(
            raw,
            requested_nets=net_list,
            requested_instances=inst_list,
        )
        return self._decorate_op_point(sanitized)

    def probe_vdsat_aliases_from_results(
        self,
        *,
        instances: Iterable[str],
    ) -> dict:
        """Probe the real Spectre OP scalar name used for MOS vdsat.

        The probe is current-run and request-scoped like
        :meth:`read_dc_op_point_from_results`: it opens
        ``self._last_results_dir`` and asks remote SKILL to test only the
        hard-coded vdsat candidate names against caller-provided instance
        paths. It never enumerates model/primitives result containers.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "probe_vdsat_aliases_from_results requires the remote-side "
                "SKILL helpers. Pass --remote-skill-dir so "
                "safe_read_op_point.il can be loaded."
            )
        psf_dir = self._last_results_dir
        if not psf_dir:
            raise RuntimeError(
                "probe_vdsat_aliases_from_results: no results dir available "
                "(run_ocean_sim has not been called yet this session)"
            )
        if not _SAFE_PSF_DIR_RE.fullmatch(psf_dir):
            raise RuntimeError(
                f"probe_vdsat_aliases_from_results: psf_dir contains unsafe "
                f"characters (len={len(psf_dir)})"
            )
        inst_list = _normalize_maestro_op_paths(
            instances,
            label="instances",
            max_items=_MAX_MAESTRO_OP_INSTANCES,
        )
        if not inst_list:
            raise ValueError(
                "probe_vdsat_aliases_from_results requires at least one "
                "instance path."
            )

        def _skill_list(values: list[str]) -> str:
            return "list(" + " ".join(f'"{value}"' for value in values) + ")"

        raw = self._execute_skill_json(
            f'safeReadVdsatAliasProbeFromResults("{psf_dir}" '
            f"{_skill_list(inst_list)})",
            timeout=240,
        )
        if not raw.get("ok", False):
            raise RuntimeError(
                "safeReadVdsatAliasProbeFromResults failed: "
                f"{_scrub(str(raw.get('error', 'unknown')))}"
            )
        return self._sanitize_vdsat_alias_probe(
            raw,
            requested_instances=inst_list,
        )

    def read_maestro_dc_op_point(
        self,
        *,
        history: str,
        nets: Iterable[str] | None = None,
        instances: Iterable[str] | None = None,
    ) -> dict:
        """Read request-scoped DC OP data from a Maestro run history.

        Remote SKILL selects only ``dc`` and ``instance`` results. It never
        selects ``model`` or ``primitives``. The caller passes explicit
        paths so the feedback surface stays bounded and audit-friendly.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "read_maestro_dc_op_point requires the remote-side SKILL "
                "helpers. Pass --remote-skill-dir so safe_read_op_point.il "
                "can be loaded."
            )
        self._require_scope_for_maestro("read_maestro_dc_op_point")
        if not isinstance(history, str) or not _SAFE_OP_HISTORY_RE.fullmatch(
            history
        ):
            raise ValueError(
                "Invalid Maestro history name; expected "
                "[A-Za-z0-9_.-]{1,128}."
            )
        net_list = _normalize_maestro_op_paths(
            nets,
            label="nets",
            max_items=_MAX_MAESTRO_OP_NETS,
        )
        inst_list = _normalize_maestro_op_paths(
            instances,
            label="instances",
            max_items=_MAX_MAESTRO_OP_INSTANCES,
        )
        if not net_list and not inst_list:
            raise ValueError(
                "read_maestro_dc_op_point requires at least one net or "
                "instance path."
            )

        def _skill_list(values: list[str]) -> str:
            return "list(" + " ".join(f'"{value}"' for value in values) + ")"

        expr = (
            f'safeReadMaestroDcOpPoint("{history}" '
            f"{_skill_list(net_list)} {_skill_list(inst_list)})"
        )
        raw = self._execute_skill_json(expr, timeout=120)
        if not raw.get("ok", False):
            raise RuntimeError(
                "safeReadMaestroDcOpPoint failed: "
                f"{_scrub(str(raw.get('error', 'unknown')))}"
            )
        sanitized = self._sanitize_maestro_dc_op_point(
            raw,
            requested_nets=net_list,
            requested_instances=inst_list,
        )
        return self._decorate_op_point(sanitized)

    def _sanitize_vdsat_alias_probe(
        self,
        data: dict,
        *,
        requested_instances: Iterable[str],
    ) -> dict[str, Any]:
        """Defense-in-depth filter for vdsat alias probe payloads."""
        requested = [
            path for path in requested_instances
            if isinstance(path, str) and _is_safe_op_result_path(path)
        ]
        out: dict[str, Any] = {
            "ok": bool(data.get("ok", True)),
            "source": "psf",
            "analysis": "dc",
            "resultKinds": [],
            "instancesTested": len(requested),
            "actualName": None,
            "candidates": {},
            "issues": [],
        }

        result_kinds = data.get("resultKinds")
        if isinstance(result_kinds, list):
            out["resultKinds"] = [
                kind for kind in result_kinds
                if kind in {"dc", "dcOp", "dcOpInfo", "finalTimeOP", "instance"}
            ]

        raw_candidates = data.get("candidates")
        if isinstance(raw_candidates, dict):
            for name in _VDSAT_ALIAS_PROBE_CANDIDATES:
                raw_stats = raw_candidates.get(name)
                if not isinstance(raw_stats, dict):
                    continue
                try:
                    hits = int(raw_stats.get("hits") or 0)
                except (TypeError, ValueError):
                    hits = 0
                example_raw = raw_stats.get("example")
                example: float | None
                if (
                    isinstance(example_raw, (int, float))
                    and not isinstance(example_raw, bool)
                    and math.isfinite(float(example_raw))
                ):
                    example = float(example_raw)
                else:
                    example = None
                values: dict[str, float] = {}
                raw_values = raw_stats.get("values")
                if isinstance(raw_values, dict):
                    for path, value in raw_values.items():
                        if path not in requested:
                            continue
                        if (
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(float(value))
                        ):
                            values[path] = float(value)
                out["candidates"][name] = {
                    "hits": max(hits, 0),
                    "example": example,
                    "trueVdsat": name in _TRUE_VDSAT_ALIAS_CANDIDATES,
                    "values": values,
                }

        actual_name = data.get("actualName")
        if (
            isinstance(actual_name, str)
            and actual_name in _TRUE_VDSAT_ALIAS_CANDIDATES
            and out["candidates"].get(actual_name, {}).get("hits", 0) > 0
        ):
            out["actualName"] = actual_name
        else:
            for name in _TRUE_VDSAT_ALIAS_CANDIDATES:
                if out["candidates"].get(name, {}).get("hits", 0) > 0:
                    out["actualName"] = name
                    break

        issues = data.get("issues")
        if isinstance(issues, list):
            for issue in issues[:20]:
                if not isinstance(issue, str):
                    continue
                safe_issue = str(_scrub(issue))[:200]
                try:
                    assert_llm_feedback_safe(
                        safe_issue,
                        context="vdsat alias probe issue",
                    )
                except ValueError:
                    safe_issue = "redacted issue"
                out["issues"].append(safe_issue)
        if out["actualName"] is None:
            out["issues"].append("no true vdsat OP alias returned numeric values")
        return out

    def _decorate_op_point(self, sanitized: dict) -> dict:
        """Post-process op-point data: compute vov, map region ? label.

        Pure Python derivation â€“ no PDK data touched. Runs only after
        _sanitize_op_point has already dropped anything outside the
        safe-key allowlist.
        """
        instances = sanitized.get("instances") if isinstance(
            sanitized.get("instances"), dict) else sanitized
        if not isinstance(instances, dict):
            return sanitized
        for inst_name, params in instances.items():
            if not isinstance(params, dict):
                continue
            # Spectre/PDK variants commonly expose the device drain-source
            # current as ids. saveOpPoint may also expose an id terminal
            # current with a simulator sign convention; the LLM-facing table
            # uses canonical ids under the generic column name id.
            if isinstance(params.get("ids"), (int, float)):
                params["id"] = params["ids"]
            # Derived vov from (vgs - vth) when both are numeric and vov
            # wasn't already returned by SKILL.
            if "vov" not in params:
                vgs = params.get("vgs")
                vth = params.get("vth")
                if isinstance(vgs, (int, float)) and isinstance(
                        vth, (int, float)):
                    params["vov"] = float(vgs) - float(vth)
            # region enum ? string label (keeps the int too for audit).
            region = params.get("region")
            if isinstance(region, (int, float)) and not isinstance(
                    region, bool):
                label = _REGION_LABELS.get(int(region), "unknown")
                params["region_label"] = label
        return sanitized

    def _sanitize_maestro_dc_op_point(
        self,
        data: dict,
        *,
        requested_nets: Iterable[str] | None = None,
        requested_instances: Iterable[str] | None = None,
    ) -> dict:
        """Defense-in-depth filter for safeReadMaestroDcOpPoint payloads."""
        out: dict[str, Any] = {
            "ok": bool(data.get("ok", True)),
            "analysis": "dc",
            "nodes": {},
            "instances": {},
            "resultKinds": [],
            "issues": [],
        }
        history = data.get("history")
        if isinstance(history, str) and _SAFE_OP_HISTORY_RE.fullmatch(history):
            out["history"] = history
        source = data.get("source")
        if source in {"maestro", "psf"}:
            out["source"] = source

        nodes = data.get("nodes")
        if isinstance(nodes, dict):
            for path, value in nodes.items():
                if not _is_safe_op_result_path(path):
                    continue
                if isinstance(value, (int, float)) and not isinstance(
                    value, bool
                ) and math.isfinite(float(value)):
                    out["nodes"][path] = value

        raw_instances = data.get("instances")
        if isinstance(raw_instances, dict):
            safe_instances = self._sanitize_op_point(
                {"instances": raw_instances}
            )
            for path, params in safe_instances.items():
                if not _is_safe_op_result_path(path):
                    continue
                if not isinstance(params, dict):
                    continue
                numeric_params = {
                    metric: metric_value
                    for metric, metric_value in params.items()
                    if isinstance(metric_value, (int, float))
                    and not isinstance(metric_value, bool)
                    and math.isfinite(float(metric_value))
                }
                if numeric_params:
                    out["instances"][path] = numeric_params

                    missing = [
                        key for key in _LLM_OP_POINT_COLUMNS
                        if key not in numeric_params
                        and not (
                            key == "vov"
                            and "vgs" in numeric_params
                            and "vth" in numeric_params
                        )
                        and not (key == "id" and "ids" in numeric_params)
                    ]
                    if missing:
                        out["issues"].append(
                            f"{path} missing OP fields: {','.join(missing)}"
                        )

        result_kinds = data.get("resultKinds")
        if isinstance(result_kinds, list):
            out["resultKinds"] = [
                kind for kind in result_kinds
                if kind in {"dc", "dcOp", "dcOpInfo", "finalTimeOP", "instance"}
            ]

        issues = data.get("issues")
        if isinstance(issues, list):
            for issue in issues[:20]:
                if not isinstance(issue, str):
                    continue
                safe_issue = str(_scrub(issue))[:200]
                try:
                    assert_llm_feedback_safe(
                        safe_issue,
                        context="Maestro OP issue",
                    )
                except ValueError:
                    safe_issue = "redacted issue"
                out["issues"].append(safe_issue)
        self._annotate_requested_op_readback(
            out,
            requested_nets=requested_nets,
            requested_instances=requested_instances,
        )
        return out

    def _append_safe_op_issue(self, payload: dict, issue: str) -> None:
        safe_issue = str(_scrub(issue))[:200]
        try:
            assert_llm_feedback_safe(
                safe_issue,
                context="Maestro OP issue",
            )
        except ValueError:
            safe_issue = "redacted issue"
        issues = payload.setdefault("issues", [])
        if isinstance(issues, list) and safe_issue not in issues:
            issues.append(safe_issue)

    def _annotate_requested_op_readback(
        self,
        payload: dict,
        *,
        requested_nets: Iterable[str] | None,
        requested_instances: Iterable[str] | None,
    ) -> None:
        """Add bounded, PDK-safe diagnostics for requested OP paths missed.

        The remote SKILL reader is request-scoped: it never enumerates model
        result containers. When a requested path is absent from the sanitized
        payload, make that absence explicit so the LLM does not mistake a
        sparse readback for a real circuit fact.
        """
        nodes = payload.get("nodes") if isinstance(
            payload.get("nodes"), dict
        ) else {}
        instances = payload.get("instances") if isinstance(
            payload.get("instances"), dict
        ) else {}

        returned_net_leafs = {
            str(path).rsplit("/", 1)[-1]
            for path in nodes
            if isinstance(path, str)
        }
        missing_net_leafs: list[str] = []
        seen_leafs: set[str] = set()
        for path in requested_nets or []:
            if not isinstance(path, str):
                continue
            leaf = path.rsplit("/", 1)[-1]
            if path in nodes or leaf in returned_net_leafs:
                continue
            if leaf in seen_leafs:
                continue
            if not _SAFE_OP_RESULT_PATH_RE.fullmatch(path):
                continue
            seen_leafs.add(leaf)
            missing_net_leafs.append(leaf)

        for leaf in missing_net_leafs[:12]:
            self._append_safe_op_issue(
                payload,
                f"requested node {leaf} returned no safe dc value",
            )
        if len(missing_net_leafs) > 12:
            self._append_safe_op_issue(
                payload,
                f"{len(missing_net_leafs) - 12} more requested nodes "
                "returned no safe dc value",
            )

        missing_instances: list[str] = []
        for path in requested_instances or []:
            if not isinstance(path, str):
                continue
            if path in instances:
                continue
            if not _SAFE_OP_RESULT_PATH_RE.fullmatch(path):
                continue
            missing_instances.append(path)

        for path in missing_instances[:24]:
            self._append_safe_op_issue(
                payload,
                f"{path} returned no safe OP data",
            )
        if len(missing_instances) > 24:
            self._append_safe_op_issue(
                payload,
                f"{len(missing_instances) - 24} more requested instances "
                "returned no safe OP data",
            )

    def _sanitize(self, data: dict) -> dict:
        """Replace PDK cell names with generic names and strip model params.

        Acts as defense-in-depth: if remote-side SKILL already filtered,
        this should be a no-op. Logs warnings if it catches something
        that SKILL should have already filtered.
        """
        sanitized = copy.deepcopy(data)
        for inst in sanitized.get("instances", []):
            if not isinstance(inst, dict):
                continue
            original_cell = inst.get("cell", "")
            new_cell = self._alias_cell(original_cell)
            if self._skill_loaded and new_cell != original_cell:
                logger.warning(
                    "Python filter caught unmapped cell (len=%d) â€“ "
                    "SKILL-side filter may have a gap", len(original_cell)
                )
            inst["cell"] = new_cell
            original_lib = inst.get("lib", "")
            if original_lib != "GENERIC_PDK":
                if self._skill_loaded:
                    logger.warning(
                        "Python filter caught non-generic lib (len=%d) â€“ "
                        "SKILL-side filter may have a gap", len(original_lib)
                    )
                inst["lib"] = "GENERIC_PDK"
            if isinstance(inst.get("params"), dict):
                inst["params"] = self._strip_model_info(inst["params"])
        return sanitized

    def _sanitize_op_point(self, data: dict) -> dict:
        """Return only safe operating-point keys for each instance."""
        if "instances" in data and isinstance(data["instances"], dict):
            working = data["instances"]
        else:
            working = data
        sanitized: dict[str, Any] = {}
        for key, value in working.items():
            if isinstance(value, dict):
                sanitized[key] = {
                    metric: metric_value
                    for metric, metric_value in value.items()
                    if metric in _SAFE_OP_POINT_KEYS
                }
                continue
            if key.lower() in {"vdd", "vss"} and not self._is_model_info(key):
                sanitized[key] = value
        return sanitized

    # Stage 1 rev 2 (2026-04-18): the Spectre ``simulate()`` method and its
    # ``_get_spectre()`` helper were removed here. Reason: per user
    # directive the agent runs OCEAN only ("OCEAN ???????????"),
    # and keeping a parallel Spectre code path (a) bypasses the
    # scope-binding + allow-list discipline enforced by ``run_ocean_sim``,
    # and (b) is dead weight no caller exercises post-cleanup. The
    # model-info filtering helper below is kept because ``read_circuit``
    # still uses it to scrub instance-params output (see L324).

    def _is_model_info(self, key: str) -> bool:
        """Check if a result key looks like BSIM4 model parameter data."""
        key_lower = key.lower()
        return any(param in key_lower for param in self.model_info_keys)

    # ------------------------------------------------------------------ #
    #  Write direction - whitelist-validated parameter modification
    # ------------------------------------------------------------------ #

    def set_params(
        self, lib: str, cell: str, instance: str, params: dict
    ) -> None:
        """Set design parameters on an instance.

        Allowed parameters: w, l, nf, m, multi, wf.
        Python-side validation runs first, then delegates to remote-side
        safeSetParam() which also validates independently.
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        _validate_name(instance, "instance")
        self._check_scope(lib, cell)

        # Python-side validation as first defense line.
        # Stage 0 Â§1.5: case-preservation contract does NOT apply to
        # set_params â€“ safe_set_param.il lowercases paramName on the remote host
        # side (L91/L108/L152), and the downstream builders at L377/L392
        # also call _normalize_param_name(). Only the predicate is swapped
        # here so Layer-2 names (e.g. Ibias, R0) can reach set_params at
        # all; they will still be silently lowercased in flight, matching
        # current behavior.
        for key in params:
            if not self._is_allowed_param_name(key):
                raise ValueError(
                    f"Parameter {_scrub(repr(key))} is not allowed. "
                    f"Must be in core set {sorted(self.allowed_params)} or match "
                    f"pattern ^[a-zA-Z][a-zA-Z0-9_]{{0,31}}$ without blocked words."
                )

        if self._skill_loaded:
            # Build SKILL param list for safeSetParam()
            param_pairs = " ".join(
                f'list("{self._normalize_param_name(key)}" '
                f'"{self._format_param_value(value)}")'
                for key, value in params.items()
            )
            result_json = self._execute_skill_json(
                f'safeSetParam("{lib}" "{cell}" "{instance}" '
                f"list({param_pairs}))"
            )
            if not result_json.get("ok", False):
                raise RuntimeError(
                    "safeSetParam failed: "
                    f"{_scrub(str(result_json.get('error', 'unknown')))}"
                )
        else:
            param_str = " ".join(
                f'"{self._normalize_param_name(key)}" '
                f"{self._format_param_value(value)}"
                for key, value in params.items()
            )
            result = self.client.execute_skill(
                f'set_instance_param("{lib}" "{cell}" "{instance}" '
                f"list({param_str}))"
            )
            self._raise_on_skill_failure(result, "set_instance_param")

    # ------------------------------------------------------------------ #
    #  Direction C: OCEAN automation + Maestro writeback
    # ------------------------------------------------------------------ #

    # Layer-2 guard on dut_path forwarded to safeOceanMeasure. Mirrors
    # the safeOcean_validInstPath regex on the SKILL side; failing fast
    # on PC saves a round-trip and keeps the rejection reason clean.
    # Uses \Z (not $) to match the SKILL-side \z â€“ $ would also match
    # just-before-a-trailing-\n, so "/I0\n" would pass and reach SKILL.
    _DUT_PATH_RE = re.compile(r"\A(/[A-Za-z_][A-Za-z0-9_]*){1,6}\Z")

    def run_ocean_sim(
        self,
        lib: str,
        cell: str,
        tb_cell: str,
        design_vars: dict[str, Any] | None = None,
        analyses: list[Any] | None = None,
        dut_path: str = "/I0",
    ) -> dict:
        """Run an OCEAN simulation with the given design variables.

        All work happens inside the remote-side ``safeOceanRun`` wrapper so
        that PC never sends raw OCEAN strings. ``design_vars`` values are
        validated against the same whitelist as ``set_params``; ``analyses``
        must be in the SKILL-side allow-list (tran/ac/dc/noise/xf/stb).

        After a successful ``safeOceanRun`` we immediately call
        ``safeOceanMeasure(dut_path)`` in the same SKILL session to
        extract the 7 LC_VCO spec Â§3 metrics from the transient history
        (``f_osc_GHz``, ``V_diff_pp_V``, ``V_cm_V``, ``duty_cycle_pct``,
        ``amp_hold_ratio``, ``t_startup_ns``, ``I_core_uA``). Stage 1
        rev 1/2 lacked this step: the agent asked the LLM to fabricate
        those numbers, which caused the all-zero SAFEGUARD aborts
        observed on 2026-04-18. Metrics are returned under
        ``result["measurements"]``.

        Requires SafeBridge to have loaded SKILL helpers successfully
        (i.e. ``--remote-skill-dir`` pointed to a directory containing
        ``safe_ocean.il``).
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        _validate_name(tb_cell, "tb_cell")
        self._check_scope(lib, cell)
        if not self._skill_loaded:
            raise RuntimeError(
                "run_ocean_sim requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_ocean.il can be loaded."
            )

        vars_dict = design_vars or {}
        for key in vars_dict:
            if not self._is_allowed_param_name(key):
                raise ValueError(
                    f"Design variable {_scrub(repr(key))} is not allowed. "
                    f"Must be in core set {sorted(self.allowed_params)} or match "
                    f"pattern ^[a-zA-Z][a-zA-Z0-9_]{{0,31}}$ without blocked words."
                )

        # Stage 1 rev 6 B2 (2026-04-18): each entry of ``analyses`` may
        # now be EITHER a plain string (legacy: bare `analysis('tran)`
        # with no kwargs â€“ OCN-6038 risk if Maestro never set a default
        # stop-time) OR a (name, kwargs_mapping) tuple produced by
        # ``list_analyses``. Each kwarg name/value is re-validated in
        # Python as layer 2; SKILL side re-validates again as layer 3.
        analyses_specs: list[tuple[str, list[tuple[str, str]]]] = []
        for raw in analyses or []:
            if isinstance(raw, str):
                name = raw.lower()
                kwargs: list[tuple[str, str]] = []
            elif isinstance(raw, (tuple, list)) and len(raw) == 2:
                raw_name, raw_kwargs = raw
                name = str(raw_name).lower()
                if isinstance(raw_kwargs, dict):
                    kw_items = list(raw_kwargs.items())
                elif isinstance(raw_kwargs, (list, tuple)):
                    kw_items = []
                    for item in raw_kwargs:
                        if not (isinstance(item, (list, tuple))
                                and len(item) == 2):
                            raise ValueError(
                                f"analysis kwarg entry malformed "
                                f"(expected 2-tuple), got "
                                f"{_scrub(repr(item))}"
                            )
                        kw_items.append((item[0], item[1]))
                else:
                    raise ValueError(
                        f"analysis kwargs must be dict or list of "
                        f"(key, value), got {_scrub(repr(type(raw_kwargs)))}"
                    )
                kwargs = []
                for k, v in kw_items:
                    if not (isinstance(k, str)
                            and self._is_allowed_param_name(k)):
                        raise ValueError(
                            f"analysis kwarg name rejected: "
                            f"{_scrub(repr(k))}"
                        )
                    # Stage 1 rev 11 (2026-04-19): allow enum-string
                    # kwargs (e.g. skipdc=yes) through without the
                    # numeric gate in _format_param_value. Must mirror
                    # safeOcean_isEnumKwarg on the SKILL side.
                    k_lower = k.lower()
                    enum_vals = _OCEAN_ENUM_KWARGS.get(k_lower)
                    if enum_vals is not None and isinstance(v, str) \
                            and v in enum_vals:
                        v_str = v
                    else:
                        v_str = self._format_param_value(v)
                    kwargs.append((k, v_str))
            else:
                raise ValueError(
                    f"analyses entry must be str or (name, kwargs); "
                    f"got {_scrub(repr(raw))}"
                )

            if not _NAME_RE.fullmatch(name):
                raise ValueError(
                    f"Analysis name failed validation (len={len(name)})."
                )
            if name not in _OCEAN_ALLOWED_ANALYSES:
                raise ValueError(
                    f"Analysis {_scrub(repr(name))} not allowed. "
                    f"Allowed: {sorted(_OCEAN_ALLOWED_ANALYSES)}"
                )
            if name == "dc":
                # Current-run OP readback depends on Spectre writing the
                # operating-point rawfile. This is output verbosity, not a
                # circuit stimulus change, so make it a SafeBridge default
                # even when Maestro's input.scs listed a sparse dc line.
                by_lower = {
                    str(k).lower(): idx for idx, (k, _v) in enumerate(kwargs)
                }
                for default_key, default_value in (
                    ("oppoint", "rawfile"),
                    ("detail", "all"),
                ):
                    idx = by_lower.get(default_key)
                    if idx is None:
                        kwargs.append((default_key, default_value))
                    else:
                        kwargs[idx] = (kwargs[idx][0], default_value)
            analyses_specs.append((name, kwargs))

        # Stage 1 rev 3 M2 (2026-04-18): validate dut_path BEFORE the
        # safeOceanRun round-trip. Rev 3.0 validated it after the sim
        # had already run, wasting a remote host round-trip when the path was
        # bad. Length cap (<=64 chars, M4) is enforced in concert with
        # safeOcean_validInstPath on the SKILL side to cap PCRE
        # backtracking against pathological input.
        if len(dut_path) > 64 or not self._DUT_PATH_RE.fullmatch(dut_path):
            raise ValueError(
                f"Invalid dut_path {_scrub(repr(dut_path))}. Must match "
                "^(/[A-Za-z_][A-Za-z0-9_]*){1,6}\\Z and len<=64"
            )

        # Stage 0 Â§1.5: preserve original case â€“ safe_ocean.il uses the
        # first element of each list(...) directly in desVar(varName ...),
        # which is case-sensitive. Must NOT call _normalize_param_name on
        # k here, or "Ibias" would be silently rewritten to "ibias".
        var_pairs = " ".join(
            f'list("{k}" '
            f'"{self._format_param_value(v)}")'
            for k, v in vars_dict.items()
        )
        # Build SKILL literal for analysisList. Each spec is either a
        # bare "name" string (no kwargs) or list("name" list(list("k" "v") ...))
        # â€“ matches safeOceanRun's cond() dispatch.
        analyses_literal_parts: list[str] = []
        for name, kwargs in analyses_specs:
            if not kwargs:
                analyses_literal_parts.append(f'"{name}"')
            else:
                kv_parts = " ".join(
                    f'list("{k}" "{v}")' for k, v in kwargs
                )
                analyses_literal_parts.append(
                    f'list("{name}" list({kv_parts}))'
                )
        analyses_literal = " ".join(analyses_literal_parts)

        # Stage 1 rev 11 (2026-04-19): OCEAN tran (esp. with skipdc=yes
        # and an LC-VCO that must run the full tran window) can take
        # well beyond the default 30 s. Bump to 10 min so Spectre has
        # headroom; the watchdog will still kill genuine hangs.
        result_json = self._execute_skill_json(
            f'safeOceanRun("{lib}" "{cell}" "{tb_cell}" '
            f'list({var_pairs}) list({analyses_literal}) "schematic" '
            f'"schematic" "{dut_path}")',
            timeout=600,
        )
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeOceanRun failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )

        # Stage 1 rev 3 (2026-04-18): extract the 7 LC_VCO spec Â§3
        # metrics in the SAME OCEAN session so measurements come from
        # the real transient, not an LLM guess. Failure here is
        # non-fatal for the sim run â€“ the agent records an empty
        # measurements dict and the SAFEGUARD path surfaces the issue
        # on the next iteration instead of crashing the whole loop.
        # (dut_path was validated above, before safeOceanRun â€“ M2.)
        measurements: dict[str, Any] = {}
        try:
            meas_json = self._execute_skill_json(
                f'safeOceanMeasure("{dut_path}")'
            )
            if meas_json.get("ok", False):
                metrics = meas_json.get("metrics") or {}
                if isinstance(metrics, dict):
                    measurements = metrics
            else:
                # Preserve error for diagnostics but do not raise.
                result_json["measure_error"] = _scrub(
                    str(meas_json.get("error", "unknown"))
                )
        except Exception as exc:  # noqa: BLE001 â€“ defensive, see docstring
            result_json["measure_error"] = _scrub(str(exc))
        result_json["measurements"] = measurements

        # Capture the raw resultsDir for OceanWorker BEFORE scrubbing â€“
        # it needs the real remote host path to run openResults() in a fresh
        # virtuoso subprocess. Internal-only (never reaches the LLM).
        raw_results_dir = result_json.get("resultsDir")
        if isinstance(raw_results_dir, str) and raw_results_dir \
                and raw_results_dir != "<results>":
            self._last_results_dir = raw_results_dir

        # Scrub happy-path fields too: resultsDir may carry the remote host
        # absolute path (/project/.../<user>/simulation/...). SKILL side
        # attempts to redact but PC is the final gate before the LLM.
        return _scrub(result_json)

    # ------------------------------------------------------------------ #
    #  Stage 1 rev 6: generic design-variable auto-discovery
    # ------------------------------------------------------------------ #

    def list_design_vars(self, scs_path: str) -> list[dict[str, str]]:
        """Return design variables declared in a Maestro ``input.scs`` file.

        Pure forwarder to the remote-side ``safeOceanListDesignVars`` SKILL
        helper (skill/safe_ocean.il), which regex-parses the
        ``parameters`` line of the supplied ``input.scs``; ``temperature``
        is filtered out and names are validated against the standard
        identifier regex. Returns ``[{"name": ..., "default": ...}, ...]``
        with no VCO- or cell-specific semantics â€“ the agent may use it
        for any Maestro-driven testbench to pick up baseline desVars
        without hardcoding variable names.

        Scope binding is NOT consulted: the SKILL helper reads a file on
        the remote host, does not touch any cellview. The caller is
        responsible for supplying a trustworthy path; a narrow Python
        check below rejects obviously malformed inputs before the
        round-trip, and the SKILL side independently validates.
        """
        if not isinstance(scs_path, str) or not scs_path:
            raise ValueError("scs_path must be a non-empty string")
        if len(scs_path) > 1024:
            raise ValueError(f"scs_path too long (len={len(scs_path)})")
        # Defensive: reject characters that would break the SKILL-side
        # string literal or smuggle a nested call. The SKILL helper does
        # its own validation; this is layer-2 before the round-trip.
        # Rev 6 A1 (claude_reviewer 2026-04-18): explicit NUL rejection
        # so the error message points at scs_path, not the generic
        # _check_skill_entrypoint control-char check downstream.
        forbidden = ("\0", '"', "\\", "\n", "\r", "\t", ";", "`", "$", "(", ")")
        if any(c in scs_path for c in forbidden):
            raise ValueError(
                f"scs_path contains forbidden characters: "
                f"{_scrub(repr(scs_path))}"
            )
        if not self._skill_loaded:
            raise RuntimeError(
                "list_design_vars requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_ocean.il can be loaded."
            )
        result_json = self._execute_skill_json(
            f'safeOceanListDesignVars("{scs_path}")'
        )
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeOceanListDesignVars failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        raw_vars = result_json.get("vars") or []
        if not isinstance(raw_vars, list):
            raise RuntimeError(
                "safeOceanListDesignVars returned non-list 'vars'"
            )
        out: list[dict[str, str]] = []
        for entry in raw_vars:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            default = entry.get("default")
            if isinstance(name, str) and isinstance(default, str):
                out.append({"name": name, "default": default})
        return out

    # ------------------------------------------------------------------ #
    #  Stage 1 rev 6 B2: generic analysis auto-discovery
    # ------------------------------------------------------------------ #

    def list_analyses(
        self, scs_path: str
    ) -> list[dict[str, Any]]:
        """Return analyses (tran/ac/dc/...) declared in a Maestro ``input.scs``.

        Pure forwarder to the remote-side ``safeOceanListAnalyses`` SKILL
        helper. The helper scans every logical line of ``input.scs`` for
        ``<instance> <analysisType> <key=value>...`` patterns; only
        allow-listed analysis types (``tran ac dc noise xf stb``) are
        returned, and only kwargs whose VALUE is a numeric literal
        (passes ``safeHelpers_validateParamValue``) are forwarded.
        String-valued kwargs Maestro emits for spectre bookkeeping
        (``write=...``, ``annotate=...``) are deliberately dropped
        SKILL-side so OCEAN's ``analysis()`` call stays narrow.

        Returns ``[{"name": "tran", "kwargs": [("stop", "200n"), ...]}, ...]``
        with NO circuit-specific semantics â€“ generic for any Maestro
        testbench.
        """
        if not isinstance(scs_path, str) or not scs_path:
            raise ValueError("scs_path must be a non-empty string")
        if len(scs_path) > 1024:
            raise ValueError(f"scs_path too long (len={len(scs_path)})")
        forbidden = ("\0", '"', "\\", "\n", "\r", "\t", ";", "`", "$", "(", ")")
        if any(c in scs_path for c in forbidden):
            raise ValueError(
                f"scs_path contains forbidden characters: "
                f"{_scrub(repr(scs_path))}"
            )
        if not self._skill_loaded:
            raise RuntimeError(
                "list_analyses requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_ocean.il can be loaded."
            )
        result_json = self._execute_skill_json(
            f'safeOceanListAnalyses("{scs_path}")'
        )
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeOceanListAnalyses failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        raw_analyses = result_json.get("analyses") or []
        if not isinstance(raw_analyses, list):
            raise RuntimeError(
                "safeOceanListAnalyses returned non-list 'analyses'"
            )
        out: list[dict[str, Any]] = []
        for entry in raw_analyses:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not (isinstance(name, str) and name in _OCEAN_ALLOWED_ANALYSES):
                continue
            raw_kwargs = entry.get("kwargs") or []
            if not isinstance(raw_kwargs, list):
                continue
            kwargs: list[tuple[str, str]] = []
            for kv in raw_kwargs:
                if not isinstance(kv, dict):
                    continue
                k = kv.get("key")
                v = kv.get("value")
                if isinstance(k, str) and isinstance(v, str):
                    kwargs.append((k, v))
            out.append({"name": name, "kwargs": kwargs})
        return out

    # ------------------------------------------------------------------ #
    #  2026-04-22: auto-discover Maestro input.scs
    # ------------------------------------------------------------------ #

    def find_input_scs(self, lib: str, tb_cell: str) -> dict[str, Any] | None:
        """Auto-discover the newest Maestro ``input.scs`` on the remote host.

        Pure forwarder to ``safeMaeFindInputScs`` (skill/safe_mae_find.il),
        which enumerates three standard IC23.1 output layouts under
        ``$HOME/simulation`` and returns the newest existing file with its
        tier label (``maestro`` / ``ade_flat`` / ``ade_explorer``).

        Returns ``None`` when nothing is found (caller can then fall back
        to ``--scs-path`` or run without scs auto-discovery). Raises
        ``RuntimeError`` for validation / transport errors.

        Callers typically use the return for three things:
          1. ``list_design_vars(path)``  - Maestro-declared defaults
          2. ``list_analyses(path)``     - stop-time / skipdc kwargs
          3. ``plan_auto`` ic patching   - requires spectre.fc alongside
             (Maestro tier is best for #3).
        """
        if not _NAME_RE.fullmatch(lib or ""):
            raise ValueError(f"lib must match {_NAME_RE.pattern}")
        if not _NAME_RE.fullmatch(tb_cell or ""):
            raise ValueError(f"tb_cell must match {_NAME_RE.pattern}")
        if not self._skill_loaded:
            raise RuntimeError(
                "find_input_scs requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_mae_find.il can be loaded."
            )
        result_json = self._execute_skill_json(
            f'safeMaeFindInputScs("{lib}" "{tb_cell}")'
        )
        if not result_json.get("ok", False):
            err = _scrub(str(result_json.get("error", "unknown")))
            # "no input.scs found" is expected (fresh tb, never simulated);
            # caller handles as None. Other errors bubble up.
            if "no input.scs found" in err:
                return None
            raise RuntimeError(f"safeMaeFindInputScs failed: {err}")
        path = result_json.get("path")
        tier = result_json.get("tier")
        if not isinstance(path, str) or not path:
            raise RuntimeError(
                "safeMaeFindInputScs returned ok=true but no 'path' string"
            )
        return {
            "path": path,
            "tier": tier if isinstance(tier, str) else "unknown",
            "mtime": result_json.get("mtime", 0),
            "num_candidates": result_json.get("numCandidates", 1),
        }

    # ------------------------------------------------------------------ #
    #  Task F-B (2026-04-22): spec scaffold generator
    # ------------------------------------------------------------------ #

    def generate_spec_scaffold(
        self,
        lib: str,
        cell: str,
        tb_cell: str,
        scs_path: str | None = None,
    ) -> dict[str, Any]:
        """Collect PDK-safe data to seed a new spec Markdown.

        Pure forwarder to the remote-side ``safeGenerateSpecScaffold`` SKILL
        plus (optionally) ``safeOceanListDesignVars`` /
        ``safeOceanListAnalyses``. Returns a dict shaped for
        ``src.spec_scaffold.render_spec_scaffold``::

            {
              "lib": "pll",
              "cell": "LC_VCO",
              "tb_cell": "LC_VCO_tb",
              "dut": {"lib": "GENERIC_PDK", "cell": "...",
                      "pins": [{"name": ..., "direction": ...}, ...]},
              "tb":  { same shape },
              "design_vars": [{"name": "Ibias", "default": "500u"}, ...],
              "analyses":    [{"name": "tran", "kwargs": [...]}, ...],
            }

        The ``design_vars`` / ``analyses`` keys are ``[]`` when
        ``scs_path`` is not supplied. All returned content is
        user-authored (top-level pin names, Maestro desVar names from
        input.scs, whitelisted analysis kwargs); no instance properties,
        model cards, or foundry-specific parameter values are exposed.
        """
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        _validate_name(tb_cell, "tb_cell")
        # Scope binding is not required for scaffold — the caller is
        # typically authoring a new spec and has not invoked set_scope
        # yet. The SKILL helper only reads cellview terminals; no mutable
        # state is touched.
        if not self._skill_loaded:
            raise RuntimeError(
                "generate_spec_scaffold requires the remote-side SKILL "
                "helpers. Pass --remote-skill-dir so "
                "safe_spec_scaffold.il can be loaded."
            )
        result_json = self._execute_skill_json(
            f'safeGenerateSpecScaffold("{lib}" "{cell}" "{tb_cell}")'
        )
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeGenerateSpecScaffold failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        dut = result_json.get("dut") or {}
        tb = result_json.get("tb") or {}
        if not isinstance(dut, dict) or not isinstance(tb, dict):
            raise RuntimeError(
                "safeGenerateSpecScaffold returned malformed dut/tb"
            )
        design_vars: list[dict[str, str]] = []
        analyses: list[dict[str, Any]] = []
        if scs_path:
            # Best-effort: a missing / unreadable scs is not fatal — the
            # scaffold is still useful with empty tables the user fills
            # in by hand. We log but do not raise.
            try:
                design_vars = self.list_design_vars(scs_path)
            except Exception as exc:
                logger.warning(
                    "generate_spec_scaffold: list_design_vars failed "
                    "(scaffold will have empty desVar table): %s",
                    _scrub(str(exc)),
                )
            try:
                analyses = self.list_analyses(scs_path)
            except Exception as exc:
                logger.warning(
                    "generate_spec_scaffold: list_analyses failed "
                    "(scaffold will have empty analyses list): %s",
                    _scrub(str(exc)),
                )
        return {
            "lib": lib,
            "cell": cell,
            "tb_cell": tb_cell,
            "dut": _sanitize_scaffold_cell(dut),
            "tb": _sanitize_scaffold_cell(tb),
            "design_vars": design_vars,
            "analyses": analyses,
        }

    # ------------------------------------------------------------------ #
    #  Stage 1 rev 10: Plan Auto ic-line patcher
    # ------------------------------------------------------------------ #

    def patch_netlist_ic(
        self,
        scs_path: str,
        fc_path: str,
        perturb_nodes: list[dict[str, Any]],
        v_cm_hint_V: float = 0.4,
        exclude_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Rewrite input.scs's ``ic`` line from a spectre.fc snapshot.

        Pure forwarder to the remote-side ``safePatchNetlistIC`` SKILL
        helper (skill/safe_patch_netlist.il). The helper:

          1. parses ``fc_path`` (spectre's equilibrium snapshot),
             dropping comments + branch currents + internal model
             nodes;
          2. computes V_cm from the mean of the first two perturb
             nodes' fc values (falls back to ``v_cm_hint_V``);
          3. builds a new ``ic`` line where every fc node keeps its
             equilibrium value, and each perturb node is set to
             ``V_cm + offset_mV/1000``. Nodes listed in
             ``exclude_nodes`` are omitted from the generated ``ic`` line;
          4. rewrites ``scs_path`` replacing the existing ``ic`` line
             (or inserting one before ``tran``) in place.

        Returns ``{"ok": True, "numBiasNodes": N, "numPerturb": M,
        "vcmMeasured": float}`` on success; raises RuntimeError on
        helper-reported failure. Caller (``plan_auto.PlanAuto``) is
        responsible for converting exceptions into best-effort logs.

        ``perturb_nodes`` shape: ``[{"name": str, "offset_mV": float},
        ...]``. Names are validated (identifier + optional dotted
        hierarchy); offsets are coerced to numeric literals.

        ``exclude_nodes`` shape: ``["I0.Ctrl", ...]`` with the same dotted
        identifier validation. Use it for source-driven control nodes that
        must follow a sweep variable rather than Plan Auto's fc snapshot.
        """
        if not isinstance(scs_path, str) or not scs_path:
            raise ValueError("scs_path must be a non-empty string")
        if not isinstance(fc_path, str) or not fc_path:
            raise ValueError("fc_path must be a non-empty string")
        if len(scs_path) > 1024 or len(fc_path) > 1024:
            raise ValueError("scs_path or fc_path too long (>1024)")
        forbidden = ("\0", '"', "\\", "\n", "\r", "\t", ";", "`", "$", "(", ")")
        for label, p in (("scs_path", scs_path), ("fc_path", fc_path)):
            if any(c in p for c in forbidden):
                raise ValueError(
                    f"{label} contains forbidden characters: {_scrub(repr(p))}"
                )
        if not isinstance(perturb_nodes, list) or not perturb_nodes:
            raise ValueError("perturb_nodes must be a non-empty list")

        # Validate each perturb entry + build the SKILL list() expression.
        # Name pattern: dotted identifier (up to 4 levels deep), mirrors
        # the SKILL-side allow-pattern in safe_patch_netlist.il.
        name_re = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,31}(\.[A-Za-z_][A-Za-z0-9_]{0,31}){0,3}\Z")
        skill_items: list[str] = []
        for entry in perturb_nodes:
            if not isinstance(entry, dict):
                raise ValueError("each perturb_node must be a dict")
            name = entry.get("name")
            off_raw = entry.get("offset_mV")
            if not isinstance(name, str) or not name_re.fullmatch(name):
                raise ValueError(
                    f"bad perturb name (len={len(str(name))}) â€“ must match "
                    "dotted-identifier pattern"
                )
            try:
                off_val = float(off_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"bad offset_mV for {name}: {off_raw!r}"
                ) from exc
            if not -5000.0 <= off_val <= 5000.0:
                raise ValueError(
                    f"offset_mV out of range for {name}: {off_val} (|.|<=5000)"
                )
            skill_items.append(f'list("{name}" {off_val:g})')

        exclude_items: list[str] = []
        for raw_name in exclude_nodes or []:
            if not isinstance(raw_name, str) or not name_re.fullmatch(raw_name):
                raise ValueError(
                    f"bad exclude node name (len={len(str(raw_name))}) "
                    "must match dotted-identifier pattern"
                )
            exclude_items.append(f'"{raw_name}"')

        try:
            vcm_hint = float(v_cm_hint_V)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"v_cm_hint_V must be numeric, got {v_cm_hint_V!r}"
            ) from exc
        if not -5.0 <= vcm_hint <= 5.0:
            raise ValueError(
                f"v_cm_hint_V out of range: {vcm_hint} (|.|<=5)"
            )

        if not self._skill_loaded:
            raise RuntimeError(
                "patch_netlist_ic requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_patch_netlist.il can be loaded."
            )

        perturb_list_expr = "list(" + " ".join(skill_items) + ")"
        exclude_list_expr = "list(" + " ".join(exclude_items) + ")"
        skill_expr = (
            f'safePatchNetlistIC("{scs_path}" "{fc_path}" '
            f'{perturb_list_expr} {vcm_hint:g} {exclude_list_expr})'
        )
        result_json = self._execute_skill_json(skill_expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safePatchNetlistIC failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        return _scrub(result_json)

    # ------------------------------------------------------------------ #
    #  Stage 1 rev 4: generic dump + primitive-op API
    # ------------------------------------------------------------------ #

    def _validate_signal_entry(self, entry: Any) -> tuple[str, str, list[str]]:
        """Validate a single (name, kind, paths) tuple from a dump spec.

        Returns the normalized (name, kind, paths) tuple; raises
        ValueError on any malformed input. Paths list is copied so
        callers can't mutate after validation.
        """
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise ValueError("signal entry must be a (name, kind, paths) triple")
        name, kind, paths = entry
        if not isinstance(name, str) or not _SIG_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Bad signal name (len={len(str(name))}) â€“ must match "
                "^[A-Za-z_][A-Za-z0-9_]{0,31}$"
            )
        if not isinstance(kind, str) or kind not in _OCEAN_SIGNAL_KINDS:
            raise ValueError(
                f"Signal kind not allowed. Must be one of {sorted(_OCEAN_SIGNAL_KINDS)}"
            )
        if not isinstance(paths, (tuple, list)):
            raise ValueError("signal paths must be a list")
        expected_arity = _OCEAN_KIND_ARITY[kind]
        if len(paths) != expected_arity:
            raise ValueError(
                f"Signal kind {kind} needs {expected_arity} path(s), got {len(paths)}"
            )
        safe_paths: list[str] = []
        for p in paths:
            if not isinstance(p, str) or not _PROBE_PATH_RE.fullmatch(p):
                raise ValueError(
                    f"Bad probe path (len={len(str(p))}) â€“ must match "
                    "^(/[A-Za-z_][A-Za-z0-9_]*){1,8}$"
                )
            if len(p) > 128:
                raise ValueError(f"Probe path too long (len={len(p)})")
            safe_paths.append(p)
        return name, kind, safe_paths

    def _validate_window_entry(self, entry: Any) -> tuple[str, float, float]:
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise ValueError("window entry must be a (name, tStart, tEnd) triple")
        name, t_start, t_end = entry
        if not isinstance(name, str) or not _SIG_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Bad window name (len={len(str(name))})"
            )
        try:
            ts = float(t_start)
            te = float(t_end)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"window {name!r}: tStart/tEnd must be numeric") from exc
        if not (math.isfinite(ts) and math.isfinite(te)):
            raise ValueError(f"window {name!r}: non-finite bounds")
        if te <= ts:
            raise ValueError(f"window {name!r}: tEnd must exceed tStart")
        if ts < 0.0:
            raise ValueError(f"window {name!r}: tStart must be >= 0")
        return name, ts, te

    @staticmethod
    def _format_time(value: float) -> str:
        # Use %.12g so PC-side floats round-trip into SKILL with full
        # precision but no scientific notation that SKILL's reader
        # couldn't parse (e.g. "1e-7" is fine, "1.5e-7" is fine).
        return format(float(value), ".12g")

    def run_ocean_dump_all(
        self,
        signals: list[tuple[str, str, list[str]]],
        windows: list[tuple[str, float, float]],
    ) -> dict[str, Any]:
        """Generic per-signal / per-window statistics dump.

        ``signals`` is a list of ``(name, kind, paths)`` tuples:
          * ``kind='V'``        â€“ 1 path, single-ended node voltage
          * ``kind='I'``        â€“ 1 path, branch current (e.g. ``/I0/M2/D``)
          * ``kind='Vdiff'``    â€“ 2 paths, ``VT(p1) - VT(p2)``
          * ``kind='Vsum_half'``â€“ 2 paths, ``(VT(p1) + VT(p2)) / 2``

        ``windows`` is a list of ``(name, t_start, t_end)`` tuples.

        Returns a dict whose ``dumps`` field is keyed by signal name,
        each value a dict keyed by window name with stats::

            {
              "ok": true,
              "dumps": {
                "Vdiff": {
                  "late": {"mean":..., "min":..., ..., "freq_Hz":..., "duty_pct":...},
                  ...
                },
                ...
              }
            }

        Must be called after ``run_ocean_sim`` has populated the OCEAN
        session's transient history. No knowledge of oscillator
        semantics lives here; compound metrics (rms_ratio, t_startup)
        are computed by ``src/spec_evaluator.py`` on the PC, possibly
        calling ``run_ocean_t_cross`` for timing lookups.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "run_ocean_dump_all requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_ocean.il can be loaded."
            )
        if not isinstance(signals, (list, tuple)) or not signals:
            raise ValueError("signals must be a non-empty list")
        if not isinstance(windows, (list, tuple)) or not windows:
            raise ValueError("windows must be a non-empty list")

        norm_signals: list[tuple[str, str, list[str]]] = []
        seen_names: set[str] = set()
        for entry in signals:
            name, kind, paths = self._validate_signal_entry(entry)
            if name in seen_names:
                raise ValueError(f"duplicate signal name {name!r}")
            seen_names.add(name)
            norm_signals.append((name, kind, paths))

        norm_windows: list[tuple[str, float, float]] = []
        seen_windows: set[str] = set()
        for entry in windows:
            name, ts, te = self._validate_window_entry(entry)
            if name in seen_windows:
                raise ValueError(f"duplicate window name {name!r}")
            seen_windows.add(name)
            norm_windows.append((name, ts, te))

        sig_list_parts = []
        for name, kind, paths in norm_signals:
            path_items = " ".join(f'"{p}"' for p in paths)
            sig_list_parts.append(
                f'list("{name}" "{kind}" list({path_items}))'
            )
        win_list_parts = []
        for name, ts, te in norm_windows:
            win_list_parts.append(
                f'list("{name}" {self._format_time(ts)} {self._format_time(te)})'
            )

        expr = (
            f"safeOceanDumpAll(list({' '.join(sig_list_parts)}) "
            f"list({' '.join(win_list_parts)}))"
        )
        result_json = self._execute_skill_json(expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeOceanDumpAll failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        # Scrub: dumps only hold numbers and the signal/window keys that
        # PC itself whitelisted; no foundry names or paths can sneak in.
        # Still run _scrub as defense-in-depth.
        return _scrub(result_json)

    def probe_oscillation(
        self,
        signal_p: str,
        signal_n: str,
        t_start: float,
        t_stop: float,
    ) -> dict[str, Any]:
        """Bug 3 (2026-04-20) â€“ lightweight oscillation gate probe.

        Reads ptp/mean of (VT(signal_p) - VT(signal_n)) over
        ``[t_start, t_stop]`` via ``safeOceanProbePtp``. Intended as
        a <1 s precheck the agent runs before the heavy
        ``safeOceanDumpAll`` so non-oscillating waveforms (low Ibias,
        symmetric equilibrium trap) are identified without hanging
        30 s inside the full dump path (see run_20260420_033152 iters
        2/4/6/9/10).

        Returns ``{"ok": bool, "ptp_V": float|None, "mean_V": float|None}``
        verbatim from SKILL. Raises ``RuntimeError`` on infrastructure
        failure (SKILL not loaded, socket timeout, malformed JSON).
        Semantic "no crossing / no waveform" outcomes travel via the
        ``ok=False`` branch so the caller can decide policy.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "probe_oscillation requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_ocean.il is loaded."
            )
        # Reuse the existing (kind=Vdiff) path validator for p/n.
        _, _, pair = self._validate_signal_entry(
            ("probe", "Vdiff", [signal_p, signal_n])
        )
        try:
            ts = float(t_start)
            te = float(t_stop)
        except (TypeError, ValueError) as exc:
            raise ValueError("t_start/t_stop must be numeric") from exc
        if not (math.isfinite(ts) and math.isfinite(te)):
            raise ValueError("t_start/t_stop must be finite")
        if te <= ts or ts < 0.0:
            raise ValueError("require 0 <= t_start < t_stop")

        expr = (
            f'safeOceanProbePtp("{pair[0]}" "{pair[1]}" '
            f"{self._format_time(ts)} {self._format_time(te)})"
        )
        result_json = self._execute_skill_json(expr)
        return _scrub(result_json)

    # ------------------------------------------------------------------ #
    #  Path-2 (2026-05-19) — sweep-aware read primitives for the
    #  tuning-curve evaluation pipeline. These never run a new sim;
    #  they read PSFs from an existing Maestro Interactive.<N> tree
    #  and surface them through the same DumpAll stat schema the
    #  single-point pipeline already uses.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_sweep_root(sweep_root: str) -> None:
        if not isinstance(sweep_root, str):
            raise ValueError("sweep_root must be a string")
        if not sweep_root:
            raise ValueError("sweep_root must be non-empty")
        if len(sweep_root) > 256:
            raise ValueError(f"sweep_root too long (len={len(sweep_root)})")
        # Path-2 R2 (2026-05-19, codex P1-1): boundary checks BEFORE the
        # charset regex. The charset gate alone allowed ``..``, ``//``,
        # ``.`` segments, and relative roots because every character is
        # individually inside [A-Za-z0-9_./-]. A caller could escape the
        # intended Interactive.N tree via /Interactive.0/../../secret/
        # Interactive.1 — fullmatch still passes, Interactive tail still
        # matches.
        if not sweep_root.startswith("/"):
            raise ValueError("sweep_root must be an absolute path")
        if "//" in sweep_root:
            raise ValueError("sweep_root must not contain '//' segments")
        # split("/") yields a leading "" (from the absolute "/") and an
        # optional trailing "" (when sweep_root ends with "/"). Anything
        # else empty is impossible because "//" was rejected above. Real
        # segments are everything except those bookend empties.
        for seg in sweep_root.strip("/").split("/"):
            if seg in (".", ".."):
                raise ValueError(
                    f"sweep_root must not contain '{seg}' segments"
                )
        if not _SAFE_SWEEP_ROOT_RE.fullmatch(sweep_root):
            raise ValueError(
                "sweep_root contains illegal characters; only "
                "[A-Za-z0-9_./-] allowed"
            )
        if not _SAFE_INTERACTIVE_TAIL_RE.search(sweep_root):
            raise ValueError(
                "sweep_root must end with /Interactive.<N> (optionally /)"
            )

    def read_sweep_manifest(self, sweep_root: str) -> dict[int, float]:
        """Path-2: load ``<sweep_root>/.tuning_manifest.json``.

        The manifest is a JSON list ``[{"point": <int>, "vctrl":
        <float>}, ...]`` produced out-of-band before the agent runs.
        SafeBridge only reads it (no remote-side writes from path-2).
        Returns a ``{point_idx: vctrl_value}`` map ordered by point.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "read_sweep_manifest requires the remote-side SKILL helpers."
            )
        self._validate_sweep_root(sweep_root)
        expr = f'safeReadSweepManifest("{sweep_root}")'
        result_json = self._execute_skill_json(expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeReadSweepManifest failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        raw = result_json.get("raw")
        if not isinstance(raw, str):
            raise RuntimeError("manifest payload missing 'raw' string")
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"manifest is not valid JSON: {exc}") from None
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("manifest must be a non-empty JSON list")
        mapping: dict[int, float] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"manifest entry must be object, got {type(entry).__name__}"
                )
            # R2 (2026-05-19, codex P3): bool is a subclass of int, so
            # `int(True) == 1` would silently accept ``{"point": true,
            # "vctrl": false}``. Reject booleans explicitly — they
            # almost certainly indicate a serializer bug in whatever
            # produced the manifest.
            try:
                raw_point = entry["point"]
                raw_vctrl = entry["vctrl"]
            except KeyError:
                raise RuntimeError(
                    "manifest entry malformed: missing point/vctrl"
                ) from None
            if isinstance(raw_point, bool) or isinstance(raw_vctrl, bool):
                raise RuntimeError(
                    "manifest entry malformed: point/vctrl must be "
                    "numeric, not boolean"
                )
            # R2 (2026-05-19, codex P3): strict isinstance, parity with the
            # write side. JSON does not natively produce strings here, but a
            # hand-edited manifest could carry ``"point": "2"`` or ``"point":
            # 1.9``; both would silently survive ``int()`` and corrupt the
            # point→vctrl mapping (rounding / lex-order traps).
            if not isinstance(raw_point, int):
                raise RuntimeError(
                    "manifest entry malformed: point must be int, got "
                    f"{type(raw_point).__name__}"
                )
            if not isinstance(raw_vctrl, (int, float)):
                raise RuntimeError(
                    "manifest entry malformed: vctrl must be int|float, got "
                    f"{type(raw_vctrl).__name__}"
                )
            point = raw_point
            vctrl = float(raw_vctrl)
            if not math.isfinite(vctrl):
                raise RuntimeError(f"manifest point {point}: non-finite vctrl")
            if point < 1 or point > 1024:
                raise RuntimeError(
                    f"manifest point {point} outside [1, 1024]"
                )
            if point in mapping:
                raise RuntimeError(f"duplicate point {point} in manifest")
            mapping[point] = vctrl
        return dict(sorted(mapping.items()))

    def write_sweep_manifest(
        self, sweep_root: str, entries: list[dict],
    ) -> int:
        """Path-2: author ``<sweep_root>/.tuning_manifest.json``.

        ``entries`` is a list of ``{"point": <int 1..1024>,
        "vctrl": <finite float>}`` records. The PC composes a typed
        SKILL list (no embedded JSON string) so the call site stays
        within the existing ``_check_skill_entrypoint`` allow-list
        (``list`` nesting is already permitted). SKILL formats the
        on-disk JSON itself. Returns the count of entries written.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "write_sweep_manifest requires the remote-side SKILL helpers."
            )
        self._validate_sweep_root(sweep_root)
        if not isinstance(entries, list) or not entries:
            raise ValueError("entries must be a non-empty list")
        seen: set[int] = set()
        skill_pairs: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"manifest entry must be dict, got {type(entry).__name__}"
                )
            try:
                raw_point = entry["point"]
                raw_vctrl = entry["vctrl"]
            except KeyError:
                raise ValueError(
                    "manifest entry missing point/vctrl"
                ) from None
            # R2 (2026-05-19, codex P3): strict isinstance, no silent
            # coercion. ``point=1.9`` would have rounded to 1; ``"2"`` would
            # have parsed via int(); ``vctrl="0.3"`` would have parsed via
            # float(). All three corrupt the manifest's schema contract.
            # Reject up front — the caller's job to pass the right type.
            # ``isinstance(True, int)`` is True, so the bool gate must fire
            # before the int/float type check.
            if isinstance(raw_point, bool) or isinstance(raw_vctrl, bool):
                raise ValueError(
                    "manifest entry malformed: point/vctrl must be "
                    "numeric, not boolean"
                )
            if not isinstance(raw_point, int):
                raise ValueError(
                    "manifest entry malformed: point must be int, got "
                    f"{type(raw_point).__name__}"
                )
            if not isinstance(raw_vctrl, (int, float)):
                raise ValueError(
                    "manifest entry malformed: vctrl must be int|float, got "
                    f"{type(raw_vctrl).__name__}"
                )
            point = raw_point
            vctrl = float(raw_vctrl)
            if point < 1 or point > 1024:
                raise ValueError(f"point {point} outside [1, 1024]")
            if not math.isfinite(vctrl):
                raise ValueError(f"point {point}: non-finite vctrl")
            if point in seen:
                raise ValueError(f"duplicate point {point} in entries")
            seen.add(point)
            # ``repr(float)`` round-trips in Python 3 and emits no SKILL
            # metacharacters for finite floats (no quotes, parens, semicolons).
            skill_pairs.append(f"list({point} {vctrl!r})")
        entries_skill = "list(" + " ".join(skill_pairs) + ")"
        expr = f'safeWriteSweepManifest("{sweep_root}" {entries_skill})'
        result_json = self._execute_skill_json(expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeWriteSweepManifest failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        count = result_json.get("count")
        if not isinstance(count, int) or count != len(entries):
            raise RuntimeError(
                f"safeWriteSweepManifest count mismatch: "
                f"got {count!r}, expected {len(entries)}"
            )
        return count

    def clear_sweep_results(self, sweep_root: str) -> dict[str, Any]:
        """Clear stale sweep artifacts below a validated ``Interactive.<N>``.

        The remote helper is intentionally narrow: the only accepted target is
        a Maestro sweep root that passes the same ``_validate_sweep_root`` gate
        used by manifest read/write, and the SKILL side moves the old root out
        of the way before recreating an empty root. This is used by benchmark
        harnesses immediately after resetting Maestro and before the first
        curve-searcher sweep, so a model cannot read another model's stale PSF
        directories from a fixed ``Interactive.0`` path.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "clear_sweep_results requires the remote-side SKILL helpers."
            )
        self._validate_sweep_root(sweep_root)
        expr = f'safeClearSweepResults("{sweep_root}")'
        result_json = self._execute_skill_json(expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeClearSweepResults failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        return _scrub(result_json)

    def run_ocean_dump_all_swept(
        self,
        signals: list[tuple[str, str, list[str]]],
        windows: list[tuple[str, float, float]],
        *,
        sweep_root: str,
        points: list[int],
        tb_cell: str | None = None,
        result_test: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Path-2: per-sweep-point variant of ``run_ocean_dump_all``.

        Iterates the supplied ``points`` against
        ``<sweep_root>/<P>/<result_dir>/psf`` and returns
        ``{point_idx: dump_json}``. A per-point SKILL failure is
        captured as ``{"ok": False, "error": ...}`` so the upstream
        ``evaluate_swept`` pipeline can still produce a partial result
        and the agent can decide whether to retry. ``tb_cell`` is the
        testbench cell used by OCEAN/final writeback; ``result_test`` is the
        exact ADE/result test directory name when it differs from the cell.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "run_ocean_dump_all_swept requires the remote-side SKILL helpers."
            )
        if not isinstance(signals, (list, tuple)) or not signals:
            raise ValueError("signals must be a non-empty list")
        if not isinstance(windows, (list, tuple)) or not windows:
            raise ValueError("windows must be a non-empty list")
        self._validate_sweep_root(sweep_root)
        if not isinstance(points, (list, tuple)) or not points:
            raise ValueError("points must be a non-empty list")
        norm_points: list[int] = []
        seen_points: set[int] = set()
        for p in points:
            try:
                pi = int(p)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"point {p!r} must be integer-coercible"
                ) from None
            if pi < 1 or pi > 1024:
                raise ValueError(f"point {pi} outside [1, 1024]")
            if pi in seen_points:
                raise ValueError(f"duplicate point {pi}")
            seen_points.add(pi)
            norm_points.append(pi)

        tb = tb_cell if tb_cell is not None else self._scope_tb_cell
        if not isinstance(tb, str) or not tb:
            raise RuntimeError(
                "tb_cell must be supplied (or set_scope(..., tb_cell=...) "
                "called) before run_ocean_dump_all_swept"
            )
        _validate_name(tb, "tb_cell")
        result_dir = (
            self._resolve_maestro_test(result_test)
            if result_test is not None
            else f"{tb}_1"
        )
        if not _SAFE_RESULT_DIR_RE.fullmatch(result_dir):
            raise ValueError(
                "result_test must be a safe PSF result directory leaf "
                "(letters, digits, underscore, dot, dash; no colon or slash)."
            )

        norm_signals: list[tuple[str, str, list[str]]] = []
        seen_names: set[str] = set()
        for entry in signals:
            name, kind, paths = self._validate_signal_entry(entry)
            if name in seen_names:
                raise ValueError(f"duplicate signal name {name!r}")
            seen_names.add(name)
            norm_signals.append((name, kind, paths))

        norm_windows: list[tuple[str, float, float]] = []
        seen_windows: set[str] = set()
        for entry in windows:
            name, ts, te = self._validate_window_entry(entry)
            if name in seen_windows:
                raise ValueError(f"duplicate window name {name!r}")
            seen_windows.add(name)
            norm_windows.append((name, ts, te))

        sig_parts = " ".join(
            'list("{n}" "{k}" list({paths}))'.format(
                n=n, k=k, paths=" ".join(f'"{p}"' for p in ps)
            )
            for n, k, ps in norm_signals
        )
        win_parts = " ".join(
            f'list("{n}" {self._format_time(ts)} {self._format_time(te)})'
            for n, ts, te in norm_windows
        )

        root = sweep_root.rstrip("/")
        out: dict[int, dict[str, Any]] = {}
        for point in norm_points:
            psf_dir = f"{root}/{point}/{result_dir}/psf"
            if not _SAFE_PSF_DIR_RE.fullmatch(psf_dir):
                raise RuntimeError(
                    f"assembled psfDir for point {point} fails safety regex"
                )
            expr = (
                f"safeOceanDumpAll(list({sig_parts}) "
                f'list({win_parts}) "{psf_dir}")'
            )
            try:
                result_json = self._execute_skill_json(expr)
            except Exception as exc:
                logger.warning(
                    "swept dump skill failure at point %d: %s",
                    point, _scrub(str(exc)),
                )
                out[point] = {"ok": False, "error": "skill_exception"}
                continue
            if not result_json.get("ok", False):
                logger.warning(
                    "swept dump returned not-ok at point %d: %s",
                    point,
                    _scrub(str(result_json.get("error", "unknown"))),
                )
                out[point] = _scrub(result_json)
                continue
            out[point] = _scrub(result_json)
        return out

    def run_ocean_t_cross(
        self,
        kind: str,
        paths: list[str],
        threshold: float,
        t_start: float,
        t_end: float,
        direction: str = "rising",
        use_abs: bool = False,
    ) -> dict[str, Any]:
        """First-threshold-crossing time (seconds) for a built waveform.

        PC caller computes ``threshold`` itself (typically as a fraction
        of a dumped ptp/rms value), so SKILL stays oblivious to compound
        metric semantics. Returns ``{"ok":true,"value":<seconds>}`` or
        ``{"ok":false,"error":"..."}`` â€“ caller decides how to interpret
        a missing crossing.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "run_ocean_t_cross requires the remote-side SKILL helpers."
            )
        # Reuse the shared validator for (kind, paths) shape.
        _, kind_n, paths_n = self._validate_signal_entry(("tcross", kind, paths))
        if not isinstance(threshold, (int, float)):
            raise ValueError("threshold must be numeric")
        threshold_f = float(threshold)
        if not math.isfinite(threshold_f):
            raise ValueError("threshold must be finite")
        try:
            ts = float(t_start)
            te = float(t_end)
        except (TypeError, ValueError) as exc:
            raise ValueError("t_start/t_end must be numeric") from exc
        if not (math.isfinite(ts) and math.isfinite(te)):
            raise ValueError("t_start/t_end must be finite")
        if te <= ts or ts < 0.0:
            raise ValueError("require 0 <= t_start < t_end")
        if direction not in _OCEAN_CROSS_DIRS:
            raise ValueError(
                f"direction must be one of {sorted(_OCEAN_CROSS_DIRS)}"
            )
        use_abs_lit = "t" if use_abs else "nil"

        path_items = " ".join(f'"{p}"' for p in paths_n)
        expr = (
            f'safeOceanTCross("{kind_n}" list({path_items}) '
            f"{self._format_time(threshold_f)} "
            f"{self._format_time(ts)} {self._format_time(te)} "
            f'"{direction}" {use_abs_lit})'
        )
        result_json = self._execute_skill_json(expr)
        # Unlike DumpAll, a "no crossing" outcome is a semantic answer,
        # not an infrastructure failure â€“ return the dict verbatim and
        # let the PC evaluator decide whether to record None.
        return _scrub(result_json)

    def write_and_save_maestro(self, design_vars: dict[str, Any]) -> dict:
        """Atomically write design variables AND save the Maestro setup
        for the scoped testbench cell (``tb_cell``).

        Calls remote-side ``safeMaeWriteAndSave(libName cellName varList)``
        in one SKILL round-trip, where ``cellName`` is the testbench cell
        (not the DUT). The SKILL helper resolves the Maestro session by
        matching the scope against open ADE window titles and pins that
        session across the entire write+save â€“ preventing the
        multi-session ambiguity where each independent
        ``car(maeGetSessions())`` could pick a different session.

        Closes the "OCEAN oscillates, Maestro shows flat waveform" class
        of bug: OCEAN ``desVar`` only mutates OCEAN session memory, while
        ``maeSetVar`` + ``maeSaveSetup`` updates the Maestro setup DB.
        Next time the user runs from Maestro for the scoped testbench
        cell, the netlist is regenerated from the new values.

        Requires ``set_scope(lib, cell, tb_cell=...)`` to have been
        called first so PC cannot send an arbitrary (lib, cell) pair
        past the allow-list. Raises ``RuntimeError`` if no open Maestro
        session matches the scope (user must open Maestro for the scoped
        testbench cell first â€“
        SafeBridge does not auto-pop GUI windows on the user's desktop).
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "write_and_save_maestro requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_maestro.il can be loaded."
            )
        if self._scope_lib is None or self._scope_cell is None:
            raise RuntimeError(
                "write_and_save_maestro requires set_scope() to have been "
                "called first so lib/cell cannot be forged from outside."
            )
        if self._scope_tb_cell is None:
            raise RuntimeError(
                "write_and_save_maestro requires set_scope(..., tb_cell=...) "
                "to have been called with the testbench cell. "
                "If using run_agent.py, pass --tb-cell <name> on the CLI."
            )
        if not design_vars:
            raise ValueError("design_vars must be a non-empty mapping")
        # Defensive re-validation: scope state is a long-lived Python
        # attribute; we don't rely on set_scope's invariant holding
        # forever.
        _validate_name(self._scope_lib, "lib")
        _validate_name(self._scope_tb_cell, "tb_cell")
        for key in design_vars:
            if not self._is_allowed_param_name(key):
                raise ValueError(
                    f"Design variable {_scrub(repr(key))} is not allowed. "
                    f"Must be in core set {sorted(self.allowed_params)} or match "
                    f"pattern ^[a-zA-Z][a-zA-Z0-9_]{{0,31}}$ without blocked words."
                )
        # Stage 0 Â§1.5: preserve original case â€“ Maestro desVar names are
        # case-sensitive (`Ibias` != `ibias` in a Maestro state file).
        # safe_maestro.il uses the first element of each list(...)
        # directly in maeSetVar(... varName ...).
        var_pairs = " ".join(
            f'list("{k}" '
            f'"{self._format_param_value(v)}")'
            for k, v in design_vars.items()
        )
        skill_expr = (
            f'safeMaeWriteAndSave("{self._scope_lib}" '
            f'"{self._scope_tb_cell}" list({var_pairs}))'
        )
        logger.info(
            "[DIAG] write_and_save_maestro SKILL expr: %s", skill_expr
        )
        result_json = self._execute_skill_json(skill_expr)
        logger.info(
            "[DIAG] write_and_save_maestro full response: %s", result_json
        )
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeMaeWriteAndSave failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        # Stage 1 rev 1 (2026-04-18): saved=False used to be gated by the
        # separate MaestroWriter wrapper; that wrapper was removed as
        # dead middle-layer and its one useful escalation (treat silent
        # saved=False as RuntimeError instead of returning ok:true)
        # was folded in here. Without this, a flat-waveform regression
        # would surface as success-shaped JSON with saved=False buried.
        if not result_json.get("saved", False):
            raise RuntimeError(
                "safeMaeWriteAndSave returned saved=False; opening Maestro "
                "will show the pre-optimization netlist."
            )
        # R13/R14: the SKILL helper mirrors values into test-local
        # Design Variables entries when Cadence exposes the open test
        # list. Keep the copy-pasteable table as a UI-cache fallback:
        # an already-open ADE Explorer pane can lag until redraw/reopen.
        self._log_manual_sync_table(
            design_vars,
            scope_lib=self._scope_lib,
            scope_tb_cell=self._scope_tb_cell,
            session=result_json.get("session", ""),
        )
        return _scrub(result_json)

    def save_maestro_setup(self) -> dict:
        """Save the scoped Maestro setup without changing design vars."""
        if not self._skill_loaded:
            raise RuntimeError(
                "save_maestro_setup requires the remote-side SKILL helpers. "
                "Pass --remote-skill-dir so safe_maestro.il can be loaded."
            )
        self._require_scope_for_maestro("save_maestro_setup")
        _validate_name(self._scope_lib, "lib")
        _validate_name(self._scope_tb_cell, "tb_cell")
        skill_expr = (
            f'safeMaeSaveSetup("{self._scope_lib}" '
            f'"{self._scope_tb_cell}")'
        )
        result_json = self._execute_skill_json(skill_expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeMaeSaveSetup failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        if not result_json.get("saved", False):
            raise RuntimeError("safeMaeSaveSetup returned saved=False.")
        return _scrub(result_json)

    def read_maestro_setup_summary(
        self, *, test: str | None = None,
    ) -> dict:
        """Read a bounded, PDK-scrubbed summary of the scoped Maestro setup.

        This is the readback half of Maestro writeback. It intentionally
        returns only setup metadata that the agent needs to verify writes:
        existing tests, enabled analyses, output row names/expressions,
        test-local design-variable values, and saved spec bounds. Full
        ``active.state`` / ``maestro.sdb`` contents are never returned.

        ``test=None`` means all tests in the scoped ADE session. Passing a
        test name filters the summary to that row.
        """
        if not self._skill_loaded:
            raise RuntimeError(
                "read_maestro_setup_summary requires the remote-side SKILL "
                "helpers. Pass --remote-skill-dir so safe_maestro.il can be "
                "loaded."
            )
        self._require_scope_for_maestro("read_maestro_setup_summary")
        if test is not None:
            self._resolve_maestro_test(test)
            resolved_test = test
        else:
            resolved_test = ""
        skill_expr = (
            f'safeMaeSetupSummary("{self._scope_lib}" '
            f'"{self._scope_tb_cell}" "{resolved_test}")'
        )
        result_json = self._execute_skill_json(skill_expr)
        if not result_json.get("ok", False):
            raise RuntimeError(
                "safeMaeSetupSummary failed: "
                f"{_scrub(str(result_json.get('error', 'unknown')))}"
            )
        if not result_json.get("specsRaw"):
            specs_raw = self._read_maestro_specs_raw_via_ssh()
            if specs_raw:
                result_json["specsRaw"] = specs_raw
        return _scrub(result_json)

    def _read_maestro_specs_raw_via_ssh(self) -> str:
        """Best-effort bounded read of the saved ``<specs>`` section.

        Some Cadence builds expose saved pass/fail specs in ``maestro.sdb``
        but not through ``maeGetSetup(?typeName "specs")``. This helper keeps
        the public summary API complete without returning the whole setup
        file: it resolves the scoped Maestro directory internally, validates
        the remote path, and returns at most 20 KiB between ``<specs>`` tags.
        """
        runner = getattr(self.client, "ssh_runner", None)
        if runner is None:
            return ""
        if self._scope_lib is None or self._scope_tb_cell is None:
            return ""
        expr = (
            f'ddGetObj("{self._scope_lib}" '
            f'"{self._scope_tb_cell}" "maestro")~>readPath'
        )
        try:
            result = self.client.execute_skill(expr)
        except Exception:  # noqa: BLE001 - summary fallback only
            return ""
        if getattr(result, "errors", None):
            return ""
        raw = (getattr(result, "output", "") or "").strip()
        if not raw or raw == "nil":
            return ""
        try:
            dir_path = json.loads(raw)
        except Exception:  # noqa: BLE001 - tolerate SKILL raw string shape
            dir_path = raw.strip('"')
        if not isinstance(dir_path, str) or not dir_path:
            return ""
        remote_path = posixpath.normpath(posixpath.join(dir_path, "maestro.sdb"))
        try:
            self._validate_remote_maestro_sdb_path(remote_path)
        except ValueError:
            return ""
        command = (
            "sed -n '/<specs>/,/<\\/specs>/p' "
            f"{shlex.quote(remote_path)} | head -c 20000"
        )
        try:
            ssh_result = runner.run_command(command, timeout=10)
        except Exception:  # noqa: BLE001 - summary fallback only
            return ""
        if getattr(ssh_result, "returncode", 1) != 0:
            return ""
        return getattr(ssh_result, "stdout", "") or ""

    @staticmethod
    def _validate_remote_maestro_sdb_path(path: str) -> None:
        """Validate an internally resolved read-only ``maestro.sdb`` path.

        This is intentionally narrower than ``_validate_remote_output_dir``:
        the input comes from ``ddGetObj(... "maestro")~>readPath`` for the
        already-open scoped ADE setup, not from the LLM or user prompt. Real
        project trees may include process-node-looking directory names, so this
        validator does not reject ``_FOUNDRY_LEAK_RE`` in the path itself.
        The path is never returned to callers; only the bounded ``<specs>``
        excerpt is returned and then scrubbed by ``read_maestro_setup_summary``.
        """
        if not isinstance(path, str) or not path:
            raise ValueError("maestro path must be a non-empty string")
        if len(path) > 1024:
            raise ValueError(f"maestro path too long (len={len(path)})")
        if "//" in path:
            raise ValueError(
                f"maestro path contains empty path component '//' "
                f"(len={len(path)})."
            )
        normalized = posixpath.normpath(path)
        if normalized != path:
            raise ValueError(
                f"maestro path is not normalized (len={len(path)})."
            )
        if not _SAFE_PSF_DIR_RE.fullmatch(path):
            raise ValueError(
                f"maestro path contains forbidden characters "
                f"(len={len(path)})."
            )
        segments = path.split("/")
        if any(seg == ".." for seg in segments):
            raise ValueError(
                f"maestro path contains '..' traversal (len={len(path)})."
            )
        if len(segments) < 3 or segments[-2:] != ["maestro", "maestro.sdb"]:
            raise ValueError(
                "maestro path must end with /maestro/maestro.sdb "
                f"(len={len(path)})."
            )
        forbidden_prefixes = (
            "/proc/", "/sys/", "/dev/", "/etc/", "/root/",
            "/bin/", "/sbin/", "/usr/", "/lib/", "/lib64/",
            "/boot/", "/opt/", "/cadence/", "/cad/", "/pdk/",
            "/eda/", "/tools/",
        )
        for fp in forbidden_prefixes:
            if path == fp.rstrip("/") or path.startswith(fp):
                raise ValueError(
                    f"maestro path starts with forbidden system prefix "
                    f"(len={len(path)})."
                )
        if "~" in path:
            raise ValueError(
                f"maestro path contains '~' (len={len(path)})."
            )
        active_roots = _resolve_remote_output_roots()
        if not any(path.startswith(p) for p in active_roots):
            raise ValueError(
                f"maestro path must start with one of {list(active_roots)} "
                f"(len={len(path)})."
            )

    # ------------------------------------------------------------------ #
    #  Maestro Outputs Setup writers (add_output / set_spec /
    #  set_analysis / create_netlist_for_corner)
    #
    #  These wrap virtuoso_bridge.virtuoso.maestro.writer.* — the SKILL
    #  expression is built inside virtuoso-bridge and dispatched through
    #  client.execute_skill(), bypassing SafeBridge._execute_skill_json's
    #  entrypoint allow-list. Each method therefore performs its own
    #  PDK-safe input validation BEFORE the writer is called, and scrubs
    #  the return value before it leaves the bridge.
    # ------------------------------------------------------------------ #

    def _resolve_maestro_test(self, test: str | None) -> str:
        """Return the Maestro test name to use, defaulting to scoped tb_cell."""
        if test is None:
            if self._scope_tb_cell is None:
                raise RuntimeError(
                    "test name not supplied and set_scope(..., tb_cell=...) "
                    "has not been called; cannot infer the Maestro test name."
                )
            test = self._scope_tb_cell
        if not isinstance(test, str) or not test:
            raise ValueError("Maestro test name must be a non-empty string")
        if len(test) > 128 or not _MAESTRO_TEST_NAME_RE.fullmatch(test):
            raise ValueError(
                f"Invalid Maestro test name (len={len(test)}). "
                "Only [A-Za-z0-9_.:-] is allowed."
            )
        return test

    @staticmethod
    def _validate_maestro_session(session: str) -> None:
        """Permit empty (let SKILL pick) or strict identifier-with-dot/dash."""
        if session == "":
            return
        if not isinstance(session, str) or len(session) > 128:
            raise ValueError(
                f"Maestro session must be a string (len<=128); "
                f"got type={type(session).__name__}"
            )
        if not _MAESTRO_TEST_NAME_RE.fullmatch(session):
            raise ValueError(
                "Maestro session contains forbidden characters "
                "(only [A-Za-z0-9_.:-] allowed)."
            )

    # ------------------------------------------------------------------
    # Track C v2 R2 (2026-05-15): remote dedup probes for Maestro setup
    # ------------------------------------------------------------------

    def _list_remote_maestro_tests(self, session: str = "") -> set[str]:
        """Return the set of test names that currently exist in the
        target Maestro session, as reported by ``maeGetSetup``.

        Parser layout (defense in depth, three filter stages):

          1. Extract every quoted token from the SKILL raw output.
          2. Keep only tokens matching the strict test-name whitelist
             ``_MAESTRO_TEST_NAME_RE``.
          3. Drop anything matching ``_FOUNDRY_LEAK_RE`` — foundry
             device-family prefixes (``nch_``, ``pch_``, ``tsmc``…)
             would mean either the Maestro raw output leaked PDK
             content into our parser, or a malicious caller named a
             test with a foundry prefix; either way it has no business
             driving the dedup gate.

        virtuoso-bridge 0.4.0 does NOT expose a structured
        ``maeGetTests`` primitive — only ``_get_test`` (first test) +
        the raw ``maeGetSetup`` blob. R3 P2 (2026-05-15) confirmed
        with the 0.4.0 source that no structured probe exists; the
        ``maeGetResultTests`` primitive in reader.py operates on
        post-sim results, not pre-sim setup. Until virtuoso-bridge
        ships a structured probe we keep the three-stage regex parser
        and document it honestly.

        Known limitation: the SKILL output blob may contain quoted
        tokens that are NOT test names (analysis primitives like
        ``"tran"``, session metadata, etc.). Stage 2 drops anything
        outside the test-name alphabet but cannot semantically
        distinguish "a test the user happens to have named
        ``tran``" from "the SKILL output leaked the analysis token
        ``tran``". This is acceptable because (a) naming a Maestro
        test ``tran`` is a degenerate user choice we don't need to
        support, and (b) a false-positive dedup just forces the LLM
        to pick a different name on the next iter, which is the same
        outcome it would have had if the name really did collide.

        Re-raises any SKILL transport error so the caller (``create_maestro_test``)
        propagates it to the LLM — a partially-known remote state is
        worse than a hard failure, since the LLM can recover by retrying
        next iteration. The session string is already pinned by
        ``_validate_maestro_session``; ``maeGetSetup`` itself is a
        read-only query, no writes.
        """
        self._validate_maestro_session(session)
        s_arg = f' ?session "{session}"' if session else ""
        expr = f"maeGetSetup({s_arg.lstrip()})" if session else "maeGetSetup()"
        result = self.client.execute_skill(expr)
        if getattr(result, "errors", None):
            err0 = result.errors[0]
            raise RuntimeError(
                f"_list_remote_maestro_tests SKILL probe failed: "
                f"{_scrub(str(err0))}"
            )
        raw = _cap_remote_output(
            getattr(result, "output", "") or "",
            label="_list_remote_maestro_tests",
        )
        if not raw or raw.strip() in ("nil", '""'):
            return set()
        return {
            m for m in re.findall(r'"([^"]+)"', raw)
            if _MAESTRO_TEST_NAME_RE.fullmatch(m)
            and not _FOUNDRY_LEAK_RE.search(m)
        }

    def _delete_maestro_output_remote(
        self, name: str, *, test: str | None = None, session: str = "",
    ) -> None:
        """Remove a Maestro Outputs Setup row by ``(name, test)``.

        Wraps the ``maeDeleteOutput`` SKILL primitive (not exposed by
        virtuoso-bridge 0.4.0). Used by ``apply_maestro_setup`` when the
        v2 dispatcher detects an outputs-block entry whose name was
        already added by Option I sync — the v2 expr wins, so we drop
        the prior row before re-adding.

        R3 (2026-05-15): ``test`` is now ``str | None``. ``None`` /
        omitted means "resolve via scoped tb_cell" (matches the
        ``add_maestro_output`` default-test contract). The R2
        implementation forced callers to pass an explicit non-empty
        string, which silently broke the LLM-common case where
        ``outputs`` entries don't carry a ``test`` field — the v2
        wins logic in ``apply_maestro_setup`` would resolve to ``""``,
        ``_resolve_maestro_test`` would raise, the fail-soft branch
        swallowed it, and ``add_maestro_output`` then issued a
        duplicate row.

        Inputs go through the same gates as ``add_maestro_output``:
        ``name`` validated by ``_MAESTRO_OUTPUT_NAME_RE``, ``test``
        (after default resolution) by ``_resolve_maestro_test``,
        ``session`` by ``_validate_maestro_session``. Failure raises;
        caller decides whether to fall back.
        """
        if not isinstance(name, str) or not _MAESTRO_OUTPUT_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Invalid Maestro output name for delete "
                f"(len={len(name) if isinstance(name, str) else -1})."
            )
        resolved_test = self._resolve_maestro_test(test)
        self._validate_maestro_session(session)
        s_arg = f' ?session "{session}"' if session else ""
        expr = (
            f'maeDeleteOutput("{name}" "{resolved_test}"{s_arg})'
        )
        logger.info(
            "[DIAG] _delete_maestro_output_remote (name_len=%d, test_len=%d)",
            len(name), len(resolved_test),
        )
        result = self.client.execute_skill(expr)
        if getattr(result, "errors", None):
            raise RuntimeError(
                f"maeDeleteOutput failed: {_scrub(str(result.errors[0]))}"
            )

    @staticmethod
    def _validate_maestro_expr(expr: str) -> None:
        """Reject anything in a user-supplied Maestro output expression
        that is not on the OCEAN/calculator function allow-list, plus the
        usual quote / control-char / foundry-leak defenses.

        R1 (2026-05-14): switched from deny-list to allow-list per dual
        review. ``re.findall(_MAESTRO_EXPR_CALL_RE, expr)`` enumerates
        every ``identifier(`` token; if any one is not in
        :data:`_MAESTRO_EXPR_ALLOWED_FUNCS`, the expr is rejected.
        Quote / backslash / backtick remain forbidden so the string
        cannot break the ``f'?expr "{expr}"'`` interpolation in
        virtuoso_bridge writer.add_output.
        """
        if not isinstance(expr, str) or not expr:
            raise ValueError("Maestro output expr must be a non-empty string")
        if len(expr) > _MAESTRO_EXPR_MAX_LEN:
            raise ValueError(
                f"Maestro output expr too long (len={len(expr)}, "
                f"max={_MAESTRO_EXPR_MAX_LEN})."
            )
        if not expr.isascii():
            raise ValueError(
                f"Maestro output expr must be pure ASCII (len={len(expr)})."
            )
        for ch in expr:
            code = ord(ch)
            # Reject all C0 controls (including \t/\n/\r — SKILL line-folds
            # don't belong in a single-line measure expression) and DEL.
            if code < 0x20 or code == 0x7F:
                raise ValueError(
                    "Maestro output expr contains disallowed control char "
                    f"(len={len(expr)}, code=0x{code:02x})."
                )
        net_refs = _MAESTRO_EXPR_NET_REF_RE.findall(expr)
        for fn, path in net_refs:
            if fn not in _MAESTRO_EXPR_NET_REF_FUNCS:
                raise ValueError(
                    f"Maestro output expr contains unsupported net-ref "
                    f"function {fn!r}."
                )
            if not _PROBE_PATH_RE.fullmatch(path):
                raise ValueError(
                    "Maestro output expr contains invalid net-ref path "
                    f"(len={len(path)})."
                )
            if len(path) > _MAESTRO_EXPR_NET_REF_PATH_MAX_LEN:
                raise ValueError(
                    "Maestro output expr net-ref path too long "
                    f"(len={len(path)}, "
                    f"max={_MAESTRO_EXPR_NET_REF_PATH_MAX_LEN})."
                )
        expr_stripped = _MAESTRO_EXPR_NET_REF_RE.sub(
            _MAESTRO_EXPR_NET_REF_PLACEHOLDER, expr,
        )
        for literal in _MAESTRO_EXPR_STRING_RE.findall(expr_stripped):
            if literal not in _MAESTRO_EXPR_ALLOWED_STRING_LITERALS:
                raise ValueError(
                    "Maestro output expr contains unsupported string "
                    f"literal (len={len(literal)})."
                )
        expr_stripped = _MAESTRO_EXPR_STRING_RE.sub(
            _MAESTRO_EXPR_STRING_PLACEHOLDER, expr_stripped,
        )
        # Quote / backslash / backtick / pipe / apostrophe would let the
        # user terminate the SKILL string literal or invoke a reader macro.
        # Valid quoted net-ref tokens have been replaced above; any quote
        # left in expr_stripped is therefore an illegal string literal.
        # R3 (2026-05-14, codex_reviewer_v4 P0): the pipe ``|`` and
        # apostrophe ``'`` are SKILL/Lisp reader-syntax markers that the
        # allow-list scan below does NOT see because they don't match
        # ``identifier(``. Specifically:
        #   * ``|foo bar|(...)`` - escaped-symbol reader; turns the
        #     enclosed text into a symbol name, bypassing the allow-list
        #     entirely (e.g. ``|system|("rm -rf /")``).
        #   * ``'(getq cv prop)`` - quote macro; produces a literal form
        #     that the SKILL evaluator may still introspect.
        # Backtick (already blocked) is the third reader macro of this
        # family. Reject all three at the char-blocklist gate so they
        # never reach the allow-list scan.
        for bad in ('"', "\\", "`", "|", "'"):
            if bad in expr_stripped:
                raise ValueError(
                    f"Maestro output expr contains forbidden character "
                    f"{bad!r} (len={len(expr)})."
                )
        # Semicolon would let the user splice a second SKILL form after
        # the writer's closing paren. Comma is harmless but @ ! # $ are
        # all reader macros / quoters in SKILL/Lisp - reject preemptively.
        # R3 P3 (2026-05-14): ``!`` is included to match the comment.
        # SKILL ``!=`` is a legitimate comparison operator in general
        # SKILL, but the OCEAN measure-expression allow-list contains no
        # conditional / comparison functions (no ``if``, ``cond``,
        # ``equal``, ``unequal``), so ``!=`` cannot appear in a legal
        # measure expression here - blocking ``!`` loses no expressive
        # power and forecloses one more reader-macro family.
        for bad in (";", "@", "!", "$", "#", "?"):
            if bad in expr_stripped:
                raise ValueError(
                    f"Maestro output expr contains forbidden character "
                    f"{bad!r} (len={len(expr)})."
                )
        if _FOUNDRY_LEAK_RE.search(expr):
            raise ValueError(
                f"Maestro output expr contains foundry-shaped token "
                f"(len={len(expr)}); rewrite without PDK-specific names."
            )
        # Allow-list scan: every ``ident(`` in the expr must be a known
        # OCEAN/calculator function. This replaces the prior deny-list,
        # which by construction missed dozens of SKILL primitives
        # (codex R1 enumerated 11 of them — getq / process / lambda /
        # apply / funcall / defun / procedure / prog / puts / fileSeek /
        # dbWriteCellView). Allow-list is the only defensible posture.
        seen = set()
        for fn in _MAESTRO_EXPR_CALL_RE.findall(expr):
            if fn in seen:
                continue
            seen.add(fn)
            if fn not in _MAESTRO_EXPR_ALLOWED_FUNCS:
                raise ValueError(
                    f"Maestro output expr calls disallowed function "
                    f"{fn!r}. Allowed functions: "
                    f"{sorted(_MAESTRO_EXPR_ALLOWED_FUNCS)}"
                )

    @staticmethod
    def _validate_remote_output_dir(output_dir: str) -> None:
        # TODO(post-MLCAD): docstring/name mismatch — this validator is
        # now also used by ``setup_maestro_corner.model_file``. Either
        # rename to ``_validate_remote_path`` or split into two
        # purpose-specific gates. Non-blocking: the validation rules
        # are identical for both call sites.
        """Validate remote-side dir for ``create_netlist_for_corner``.

        R1 R2 (2026-05-14): "absolute path + project-root prefix
        allow-list + traversal reject + char whitelist + forbidden
        system-root prefix list". Codex R1 flagged that the original
        validator would accept ``../../etc/cadence_secret`` (no banned
        chars, no foundry token, but a traversal); leader R1 review
        further rejected the tilde-prefixed allow-list because real
        cobi project paths live under ``/proj/...`` or ``/project/...``
        which would have bounced. The current rule: absolute POSIX
        path, tilde forbidden anywhere, must begin with one of
        :data:`_MAESTRO_REMOTE_OUTPUT_ROOTS` (or an env-provided
        addition), must NOT begin with any system / Cadence / PDK root,
        must not contain ``..``.
        """
        if not isinstance(output_dir, str) or not output_dir:
            raise ValueError("output_dir must be a non-empty string")
        if len(output_dir) > 1024:
            raise ValueError(f"output_dir too long (len={len(output_dir)})")
        # R3 (2026-05-14, codex_reviewer_v4 P1): reject empty path
        # components (``/tmp//foo``) BEFORE normalization, because
        # ``posixpath.normpath`` would collapse them silently and let
        # the suspicious input slip through with a clean-looking final
        # path. The redundant ``//`` form has no legitimate use and
        # often shows up in path-confusion attacks or downstream string
        # concatenation bugs — fail loud.
        if "//" in output_dir:
            raise ValueError(
                f"output_dir contains empty path component '//' "
                f"(len={len(output_dir)})."
            )
        # R3 P1: canonicalize with ``posixpath.normpath`` so that any
        # ``./`` segments or trailing slashes don't fool the later
        # prefix-allow-list check. NOTE: this is purely syntactic; we
        # do NOT call ``realpath`` because the path lives on the
        # *remote* host (no FS access here) — symlink resolution is a
        # known limitation; the prefix allow-list pins the visible
        # path to a safe root, but a symlink on the remote side from
        # ``/tmp/foo`` to ``/etc/secret`` cannot be detected here.
        # Downstream consumers SHOULD assume content under the
        # allow-listed roots is mounted via a controlled fstab and
        # not via arbitrary user-writable symlinks.
        normalized = posixpath.normpath(output_dir)
        if normalized != output_dir:
            raise ValueError(
                f"output_dir is not in normalized form (len={len(output_dir)}); "
                f"pass the canonical path (no redundant '.', trailing '/', etc.)."
            )
        # Character whitelist (first cheap gate) — rejects spaces, quotes,
        # control chars, glob metacharacters, etc.
        if not _SAFE_PSF_DIR_RE.fullmatch(output_dir):
            raise ValueError(
                f"output_dir contains forbidden characters (len={len(output_dir)}); "
                "only [A-Za-z0-9_./~:-] is allowed."
            )
        # Traversal reject: any literal ``..`` segment, or any reference
        # back into the absolute system paths Cadence install / process
        # FS live under. We do these BEFORE the prefix-whitelist check so
        # the error message is specific about *why* the path is rejected
        # rather than just "prefix not in [...]".
        # ``..`` as a path component (POSIX-style only — the character
        # whitelist already excludes backslash) catches ``../foo`` and
        # ``foo/../bar``. A leading ``..`` is also caught.
        segments = output_dir.split("/")
        if any(seg == ".." for seg in segments):
            raise ValueError(
                f"output_dir contains '..' traversal (len={len(output_dir)})."
            )
        # Absolute-path roots that would let the caller escape to system
        # / Cadence / PDK install dirs. Allow-list below pins the prefix
        # back to per-user simulation areas only.
        forbidden_prefixes = (
            "/proc/", "/sys/", "/dev/", "/etc/", "/root/",
            "/bin/", "/sbin/", "/usr/", "/lib/", "/lib64/",
            "/boot/", "/opt/", "/cadence/", "/cad/", "/pdk/",
            "/eda/", "/tools/",
        )
        # Match either the full equality (``/proc``) or with trailing
        # slash (``/proc/...``).
        for fp in forbidden_prefixes:
            if output_dir == fp.rstrip("/") or output_dir.startswith(fp):
                raise ValueError(
                    f"output_dir starts with forbidden system prefix "
                    f"(len={len(output_dir)})."
                )
        # Tilde is forbidden anywhere: ``~`` is a shell-expansion glyph
        # whose meaning depends on the consumer (the SKILL channel does
        # NOT expand it, the SSH side might, and the remote FS may treat
        # it as a literal). Requiring absolute paths removes that
        # ambiguity entirely. Allow-list below pins the prefix to a
        # bounded set of project-area roots.
        if "~" in output_dir:
            raise ValueError(
                f"output_dir contains '~' (forbidden — pass an absolute path "
                f"under one of {list(_MAESTRO_REMOTE_OUTPUT_ROOTS)} instead) "
                f"(len={len(output_dir)})."
            )
        # Prefix allow-list: absolute paths only, under a bounded set of
        # project / scratch / tmp roots. Includes the cobi-style real
        # project paths (``/proj/...``, ``/project/<user>/...``) plus
        # ``/home/`` / ``/tmp/`` / ``/var/tmp/`` / ``/scratch/`` for
        # scratch areas. Env override
        # ``VIRTUOSO_AGENT_REMOTE_OUTPUT_ROOTS`` adds extra roots — see
        # :func:`_resolve_remote_output_roots`.
        active_roots = _resolve_remote_output_roots()
        if not any(output_dir.startswith(p) for p in active_roots):
            raise ValueError(
                f"output_dir must start with one of {list(active_roots)} "
                f"(len={len(output_dir)})."
            )
        if _FOUNDRY_LEAK_RE.search(output_dir):
            raise ValueError(
                f"output_dir contains foundry-shaped token (len={len(output_dir)})."
            )

    def _require_scope_for_maestro(self, method: str) -> None:
        """Maestro writers must be scope-bound with a tb_cell."""
        if self._scope_lib is None or self._scope_cell is None:
            raise RuntimeError(
                f"{method} requires set_scope() to have been called first."
            )
        if self._scope_tb_cell is None:
            raise RuntimeError(
                f"{method} requires set_scope(..., tb_cell=...) to have "
                "been called with the testbench cell."
            )

    def create_maestro_test(
        self,
        test: str,
        *,
        lib: str,
        cell: str,
        view: str = "schematic",
        simulator: str = "spectre",
        session: str = "",
    ) -> str:
        """Create a new Maestro test row.

        Thin PDK-safe wrapper around
        ``virtuoso_bridge.virtuoso.maestro.writer.create_test``. The
        Maestro SKILL ``maeCreateTest`` silently overwrites an existing
        row of the same name, which would let an LLM-driven setup wipe a
        user's hand-authored test — this wrapper fails closed on a
        duplicate name so the LLM gets a ValueError it can react to
        (rename / skip / abort), per Track C v2 leader decision.

        Track C v2 R2 (2026-05-15): the duplicate check is now
        REMOTE-AUTHORITATIVE. The PC-side ``_created_maestro_tests``
        set only short-circuits the same-bridge case; the
        ``_list_remote_maestro_tests`` SKILL probe is consulted on every
        call so a test created by a different bridge (or interactively
        by the user before the agent attached) is also rejected. If the
        probe itself raises, the failure propagates: a partial-knowledge
        create is worse than a hard fail.

        ``simulator`` is locked to ``_MAESTRO_SIMULATOR_ALLOWED`` —
        anything outside the bounded set raises before the create_test
        SKILL call so a bogus simulator can't reach remote-side.
        """
        self._require_scope_for_maestro("create_maestro_test")
        # Reuse ``_resolve_maestro_test`` for the name validation gate
        # (length cap + char whitelist). An explicit test must be
        # supplied — defaulting to scoped tb_cell would defeat the
        # whole point of the call (which is to create a NEW row).
        if test is None:
            raise ValueError("create_maestro_test requires an explicit test name")
        resolved_test = self._resolve_maestro_test(test)
        _validate_name(lib, "lib")
        _validate_name(cell, "cell")
        _validate_name(view, "view")
        if not isinstance(simulator, str) or simulator not in _MAESTRO_SIMULATOR_ALLOWED:
            raise ValueError(
                f"Simulator must be one of {sorted(_MAESTRO_SIMULATOR_ALLOWED)}; "
                f"got {_scrub(repr(simulator))}."
            )
        # PC-side cache short-circuit: same-bridge duplicate.
        if resolved_test in self._created_maestro_tests:
            raise ValueError(
                f"Maestro test {_scrub(repr(resolved_test))} already "
                f"created by this bridge; remove the duplicate from the "
                f"LLM proposal or pick a different name."
            )
        self._validate_maestro_session(session)
        # R2 P1-1: remote-authoritative dedup. Catches the cross-bridge
        # / cross-session case (user authored a test interactively, or
        # the prior agent run created tests this bridge instance never
        # saw). If the SKILL probe itself raises, propagate — better to
        # fail loudly than overwrite a row the LLM doesn't know exists.
        remote_names = self._list_remote_maestro_tests(session)
        if resolved_test in remote_names:
            raise ValueError(
                f"Maestro test {_scrub(repr(resolved_test))} already "
                f"exists on remote Maestro session (created externally "
                f"or in a prior bridge instance); LLM must pick a new "
                f"name or explicitly delete the existing row first."
            )
        logger.info(
            "[DIAG] create_maestro_test (test_len=%d, lib_len=%d, "
            "cell_len=%d, view=%s, simulator=%s)",
            len(resolved_test), len(lib), len(cell), view, simulator,
        )
        raw = _cap_remote_output(
            _mae_writer.create_test(
                self.client,
                resolved_test,
                lib=lib,
                cell=cell,
                view=view,
                simulator=simulator,
                session=session,
            ),
            label="create_maestro_test",
        )
        # Record AFTER the writer returns — if create_test raised, the
        # remote-side row was not created and we shouldn't dedup against
        # a phantom entry.
        self._created_maestro_tests.add(resolved_test)
        return _scrub(raw) if isinstance(raw, str) else _scrub(str(raw))

    def setup_maestro_corner(
        self,
        name: str,
        *,
        model_file: str = "",
        model_section: str = "",
        variables: dict[str, Any] | None = None,
        session: str = "",
    ) -> str:
        """Create/configure a Maestro corner with optional model file + vars.

        Thin PDK-safe wrapper around
        ``virtuoso_bridge.virtuoso.maestro.writer.setup_corner``. The
        underlying SKILL chain (``maeSetCorner`` + ``maeSetVar`` +
        ``axlSetModelFile``) takes free-text strings — this wrapper
        validates everything PC-side first so no quote / backslash /
        SKILL primitive can reach remote-side interpolation.

        ``model_file`` (when supplied) is validated as a remote absolute
        path under the same allow-list ``create_netlist_for_corner``
        uses. ``variables`` keys go through ``_SAFE_PARAM_NAME_RE`` and
        values through ``_format_param_value``.
        """
        self._require_scope_for_maestro("setup_maestro_corner")
        _validate_name(name, "corner")
        if not isinstance(model_file, str):
            raise TypeError(
                f"model_file must be a string; got type={type(model_file).__name__}"
            )
        if model_file:
            self._validate_remote_output_dir(model_file)
        if not isinstance(model_section, str):
            raise TypeError(
                f"model_section must be a string; got "
                f"type={type(model_section).__name__}"
            )
        if model_section:
            _validate_name(model_section, "model_section")
        if variables is not None and not isinstance(variables, dict):
            raise TypeError(
                f"variables must be a dict or None; got "
                f"type={type(variables).__name__}"
            )
        var_pairs: dict[str, str] = {}
        for key, value in (variables or {}).items():
            if not isinstance(key, str) or not _SAFE_PARAM_NAME_RE.fullmatch(key):
                raise ValueError(
                    f"Invalid corner variable key (len={len(key) if isinstance(key, str) else -1}). "
                    "Must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$."
                )
            value_str = self._format_param_value(value)
            # Belt-and-suspenders tripwire (matches set_maestro_analysis):
            # ``key`` is pinned by _SAFE_PARAM_NAME_RE; ``value_str`` is
            # the output of _format_param_value (atom-level whitelist).
            # If either of those upstream gates regresses we still catch
            # the literal-quote chars here BEFORE the alist reaches SKILL.
            # NOTE: ValueError not assert (python -O strips asserts).
            # TODO(post-MLCAD): canary test that snapshots this tripwire's
            # AST so a future refactor that accidentally drops the loop
            # (e.g. extracting it into a helper that swallows the
            # ValueError) is caught by CI. Claude P3 NIT — non-blocking
            # because the tripwire is currently exercised by adversarial
            # unit tests in test_track_c_v2_safe_bridge.py.
            for forbidden in ('"', "\\", "`", "\n", "\r", "\t"):
                if forbidden in key or forbidden in value_str:
                    raise ValueError(
                        f"corner variable contains forbidden char "
                        f"(key_len={len(key)}, value_len={len(value_str)})"
                    )
            var_pairs[key] = value_str
        self._validate_maestro_session(session)
        logger.info(
            "[DIAG] setup_maestro_corner (name_len=%d, has_model_file=%s, "
            "model_section_len=%d, num_vars=%d)",
            len(name), bool(model_file), len(model_section), len(var_pairs),
        )
        raw = _cap_remote_output(
            _mae_writer.setup_corner(
                self.client,
                name,
                model_file=model_file,
                model_section=model_section,
                variables=var_pairs or None,
                session=session,
            ),
            label="setup_maestro_corner",
        )
        return _scrub(raw) if isinstance(raw, str) else _scrub(str(raw))

    def add_maestro_output(
        self,
        name: str,
        *,
        output_type: str = "",
        signal_name: str = "",
        expr: str = "",
        test: str | None = None,
        session: str = "",
    ) -> str:
        """Add an output row (waveform or expression) to the Maestro
        Outputs Setup of the scoped testbench.

        Thin PDK-safe wrapper around
        ``virtuoso_bridge.virtuoso.maestro.writer.add_output``. The SKILL
        ``maeAddOutput`` builder lives in virtuoso-bridge; this wrapper's
        job is strict input validation so no foundry name, SKILL
        primitive, or quote/backslash can reach the remote-side string
        interpolation.

        Requires ``set_scope(lib, cell, tb_cell=...)``. ``test`` defaults
        to the scoped tb_cell (Maestro convention) when omitted.

        Returns the raw SKILL output (scrubbed). Caller may use it for
        downstream Maestro IDs but the agent / LLM should not parse it.
        """
        self._require_scope_for_maestro("add_maestro_output")
        if not isinstance(name, str) or not _MAESTRO_OUTPUT_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Invalid Maestro output name (len={len(name) if isinstance(name, str) else -1}). "
                "Must match ^[A-Za-z][A-Za-z0-9_]{0,63}$."
            )
        if output_type not in _MAESTRO_OUTPUT_TYPES:
            raise ValueError(
                f"Invalid output_type {output_type!r}; allowed: "
                f"{sorted(_MAESTRO_OUTPUT_TYPES)}"
            )
        if not signal_name and not expr:
            raise ValueError(
                "add_maestro_output requires either signal_name or expr."
            )
        if signal_name and expr:
            raise ValueError(
                "add_maestro_output accepts signal_name OR expr, not both."
            )
        if signal_name:
            if not isinstance(signal_name, str) or len(signal_name) > 256:
                raise ValueError(
                    "signal_name must be a string (len<=256); got "
                    f"type={type(signal_name).__name__}"
                )
            # OCEAN signal references can be hierarchical (``/I0/D``) or
            # top-level (``/Vout``). Accept either shape.
            #
            # P2 (R1 2026-05-14): note the asymmetry vs ``expr`` — both
            # ``_SAFE_NET_NAME_RE`` and ``_PROBE_PATH_RE`` are strict
            # whitelist regexes that already bound the allowed alphabet
            # to [A-Za-z0-9_] (plus the leading slash for net names),
            # so a quote / backslash / backtick / ``;`` / etc. CANNOT
            # appear in a string that passes either regex. We therefore
            # do NOT need a second-pass character-blocklist for
            # ``signal_name``; the regex IS the blocklist.
            if not (
                _SAFE_NET_NAME_RE.fullmatch(signal_name)
                or _PROBE_PATH_RE.fullmatch(signal_name)
            ):
                raise ValueError(
                    f"Invalid signal_name (len={len(signal_name)}). "
                    "Must start with '/' and contain only safe identifier chars."
                )
        if expr:
            self._validate_maestro_expr(expr)
        resolved_test = self._resolve_maestro_test(test)
        self._validate_maestro_session(session)
        logger.info(
            "[DIAG] add_maestro_output (test_len=%d, name_len=%d, "
            "type=%r, has_signal=%s, has_expr=%s)",
            len(resolved_test), len(name), output_type,
            bool(signal_name), bool(expr),
        )
        # T2.1 (2026-05-18): SKILL string-literal escape for expr.
        # virtuoso_bridge.virtuoso.maestro.writer.add_output splices
        # expr into `?expr "{expr}"` without escaping; the inner quotes
        # in net-ref tokens like VT("/Vp") then prematurely terminate
        # the outer SKILL string literal (real-Maestro lineread/read
        # syntax error). _validate_maestro_expr already rejects '\', so
        # the only '"' chars in expr are inside whitelisted net-ref
        # tokens; replacing them with \" yields valid SKILL string
        # contents that unescape back to the original expr remote-side.
        skill_expr = expr.replace('"', r'\"') if expr else expr
        skill_output_type = output_type
        if output_type in {"signal", "expr"}:
            skill_output_type = ""
        raw = _cap_remote_output(
            _mae_writer.add_output(
                self.client,
                name,
                resolved_test,
                output_type=skill_output_type,
                signal_name=signal_name,
                expr=skill_expr,
                session=session,
            ),
            label="add_maestro_output",
        )
        # R2 P1-2 / R3 P2: record AFTER writer returns so a writer
        # exception does not poison the dedup set. Both Option I sync
        # and v2 dispatcher hit this code path, so a single canonical
        # record site covers both producers. Key is the resolved tuple
        # — two tests can each own an output called "VOUT_rms" and
        # remain independent.
        self._added_maestro_outputs.add(
            (name, resolved_test, session),
        )
        return _scrub(raw) if isinstance(raw, str) else _scrub(str(raw))

    def set_maestro_spec(
        self,
        name: str,
        *,
        lt: Any = None,
        gt: Any = None,
        test: str | None = None,
        session: str = "",
    ) -> str:
        """Attach pass/fail bounds to a Maestro output.

        Thin PDK-safe wrapper around
        ``virtuoso_bridge.virtuoso.maestro.writer.set_spec``. Both ``lt``
        and ``gt`` are optional but at least one must be supplied;
        each is validated through ``_format_param_value`` so only
        numeric / engineering-unit literals (``500u``, ``1.2``,
        ``2.5e-9``) can reach the remote-side SKILL string.
        """
        self._require_scope_for_maestro("set_maestro_spec")
        if not isinstance(name, str) or not _MAESTRO_OUTPUT_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Invalid Maestro output name (len={len(name) if isinstance(name, str) else -1}). "
                "Must match ^[A-Za-z][A-Za-z0-9_]{0,63}$."
            )
        if lt is None and gt is None:
            raise ValueError("set_maestro_spec requires at least one of lt/gt.")
        lt_str = "" if lt is None else self._format_param_value(lt)
        gt_str = "" if gt is None else self._format_param_value(gt)
        resolved_test = self._resolve_maestro_test(test)
        self._validate_maestro_session(session)
        logger.info(
            "[DIAG] set_maestro_spec (test_len=%d, name_len=%d, "
            "has_lt=%s, has_gt=%s)",
            len(resolved_test), len(name), bool(lt_str), bool(gt_str),
        )
        raw = _cap_remote_output(
            _mae_writer.set_spec(
                self.client,
                name,
                resolved_test,
                lt=lt_str,
                gt=gt_str,
                session=session,
            ),
            label="set_maestro_spec",
        )
        return _scrub(raw) if isinstance(raw, str) else _scrub(str(raw))

    @staticmethod
    def _format_maestro_analysis_token_value(value: Any, *, label: str) -> str:
        return _format_maestro_analysis_token_value(value, label=label)

    def _format_maestro_analysis_option_value(
        self, analysis: str, key: str, value: Any,
    ) -> str:
        return _format_maestro_analysis_option_value(analysis, key, value)

    def set_maestro_analysis(
        self,
        analysis: str,
        *,
        enable: bool = True,
        options: dict[str, Any] | None = None,
        test: str | None = None,
        session: str = "",
    ) -> str:
        """Enable / disable a Maestro analysis on the scoped test.

        Thin PDK-safe wrapper around
        ``virtuoso_bridge.virtuoso.maestro.writer.set_analysis``. The
        Python writer accepts ``options`` as a pre-built SKILL alist
        STRING (``'(("start" "0") ("stop" "200n"))'``) which is
        injection-prone if forwarded verbatim. This wrapper instead
        takes a dict, validates every key as ``_SAFE_PARAM_NAME_RE`` and
        every value through ``_format_param_value``, and assembles the
        alist string itself.
        """
        self._require_scope_for_maestro("set_maestro_analysis")
        if not isinstance(analysis, str) or analysis not in _MAESTRO_ALLOWED_ANALYSES:
            raise ValueError(
                f"Analysis must be one of {sorted(_MAESTRO_ALLOWED_ANALYSES)}; "
                f"got {_scrub(repr(analysis))}."
            )
        if not isinstance(enable, bool):
            raise ValueError(
                f"enable must be bool; got type={type(enable).__name__}"
            )
        # R3 (2026-05-14, codex_reviewer_v4 P2): type-check ``options``
        # BEFORE the ``or {}`` fallback. The prior ``opts = options or
        # {}`` silently coerced any falsy value — empty list, empty
        # string, ``0``, ``False`` — into an empty dict, which then
        # passed the dict isinstance check. Callers that pass the
        # wrong type by mistake (e.g. ``options=[]`` instead of
        # ``options={}``) should get a TypeError, not a silent no-op
        # that hides their bug.
        if options is not None and not isinstance(options, dict):
            raise TypeError(
                f"options must be a dict or None; got type="
                f"{type(options).__name__}"
            )
        opts = options if options is not None else {}
        resolved_test = self._resolve_maestro_test(test)
        pairs: list[str] = []
        for key, value in opts.items():
            if not isinstance(key, str) or not _SAFE_PARAM_NAME_RE.fullmatch(key):
                raise ValueError(
                    f"Invalid analysis option key (len={len(key) if isinstance(key, str) else -1}). "
                    "Must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$."
                )
            value_str = self._format_maestro_analysis_option_value(
                analysis, key, value,
            )
            # R3 (2026-05-14, codex_reviewer_v4 P2): belt-and-suspenders
            # tripwire. ``key`` is already pinned to
            # ``_SAFE_PARAM_NAME_RE`` and ``value_str`` is either an
            # enum literal from a finite set or the output of
            # ``_format_param_value`` (which itself rejects any char
            # outside ``_PARAM_ATOM_RE``). The two literal-quote chars
            # below therefore cannot appear unless one of those upstream
            # validators regresses; this check catches such a regression
            # BEFORE the malformed alist reaches remote-side SKILL.
            # NOTE: must be ``raise ValueError``, not ``assert``, because
            # ``python -O`` strips asserts at compile time and the
            # tripwire would silently disappear in optimized deploys.
            for forbidden in ('"', "\\", "`", "\n", "\r", "\t"):
                if forbidden in key:
                    raise ValueError(
                        f"alist key contains forbidden char (len={len(key)})"
                    )
                if forbidden in value_str:
                    raise ValueError(
                        f"alist value contains forbidden char "
                        f"(len={len(value_str)})"
                    )
            pairs.append(f'("{key}" "{value_str}")')
        options_str = "(" + " ".join(pairs) + ")" if pairs else ""
        if analysis == "pnoise" and enable:
            configured = self._configured_maestro_analyses.get(resolved_test, set())
            if "pss" not in configured:
                raise ValueError(
                    "pnoise requires pss on the same test; set pss before "
                    f"pnoise on test {resolved_test}"
                )
        self._validate_maestro_session(session)
        logger.info(
            "[DIAG] set_maestro_analysis (test_len=%d, analysis=%s, "
            "enable=%s, num_opts=%d)",
            len(resolved_test), analysis, enable, len(pairs),
        )
        raw = _cap_remote_output(
            _mae_writer.set_analysis(
                self.client,
                resolved_test,
                analysis,
                enable=enable,
                options=options_str,
                session=session,
            ),
            label="set_maestro_analysis",
        )
        configured = self._configured_maestro_analyses.setdefault(
            resolved_test, set(),
        )
        if enable:
            configured.add(analysis)
        else:
            configured.discard(analysis)
        return _scrub(raw) if isinstance(raw, str) else _scrub(str(raw))

    def create_netlist_for_corner(
        self,
        corner: str,
        output_dir: str,
        *,
        test: str | None = None,
    ) -> str:
        """Export a standalone netlist for the named corner.

        Thin PDK-safe wrapper around
        ``virtuoso_bridge.virtuoso.maestro.writer.create_netlist_for_corner``.
        ``output_dir`` is the remote-side directory where Maestro will
        write the netlist; both ``corner`` and ``output_dir`` are
        validated against the strict-char regexes before SKILL builds
        the ``maeCreateNetlistForCorner`` string.
        """
        self._require_scope_for_maestro("create_netlist_for_corner")
        _validate_name(corner, "corner")
        self._validate_remote_output_dir(output_dir)
        resolved_test = self._resolve_maestro_test(test)
        logger.info(
            "[DIAG] create_netlist_for_corner (test_len=%d, corner_len=%d, "
            "output_dir_len=%d)",
            len(resolved_test), len(corner), len(output_dir),
        )
        raw = _cap_remote_output(
            _mae_writer.create_netlist_for_corner(
                self.client,
                resolved_test,
                corner,
                output_dir,
            ),
            label="create_netlist_for_corner",
        )
        return _scrub(raw) if isinstance(raw, str) else _scrub(str(raw))

    @staticmethod
    def _log_manual_sync_table(
        design_vars: dict[str, Any],
        *,
        banner: str = "MANUAL SYNC REQUIRED",
        scope_lib: str | None = None,
        scope_tb_cell: str | None = None,
        session: str | None = None,
    ) -> None:
        """Log a copy-pasteable two-column table of design variables.

        Called after a successful ``write_and_save_maestro`` so the user
        can manually paste values into the Maestro Design Variables panel
        (which is NOT updated programmatically — see R13 notes).
        """
        if not design_vars:
            return
        name_w = max(len(str(k)) for k in design_vars)
        name_w = max(name_w, len("Variable"))
        val_w = max(len(str(v)) for v in design_vars.values())
        val_w = max(val_w, len("Value"))

        sep = "=" * 60
        lines = [
            sep,
            f"{banner} — paste into Maestro Design Variables:",
            f"  {'Variable':<{name_w}}  {'Value':>{val_w}}",
            f"  {'-' * name_w}  {'-' * val_w}",
        ]
        for k, v in design_vars.items():
            lines.append(f"  {str(k):<{name_w}}  {str(v):>{val_w}}")
        if scope_lib or scope_tb_cell or session:
            parts = []
            if scope_lib:
                parts.append(scope_lib)
            if scope_tb_cell:
                parts.append(scope_tb_cell)
            parts.append("maestro")
            scope_str = " / ".join(parts)
            if session:
                scope_str += f"  (session {session})"
            lines.append(f"Scope: {scope_str}")
        lines.append(sep)
        logger.info("\n".join(lines))

    def display_transient_waveform(
        self,
        psf_dir: str,
        net_pos: str,
        net_neg: str,
    ) -> None:
        """Open PSF results and plot a differential transient waveform.

        SECURITY: this is a controlled bypass of _check_skill_entrypoint
        because (a) the SKILL expression is constructed inside SafeBridge
        (not agent/LLM side), (b) all interpolated arguments are scrubbed
        by _SAFE_PSF_DIR_RE / _SAFE_NET_NAME_RE, (c) the call returns
        non-JSON (a Viva plot handle) so _execute_skill_json cannot be
        used, (d) the effect is pure display — no writeback, no OCEAN,
        no Maestro state mutation.
        """
        if not _SAFE_PSF_DIR_RE.fullmatch(psf_dir):
            raise RuntimeError(
                f"PSF dir contains unsafe characters (len={len(psf_dir)})"
            )
        if not _SAFE_NET_NAME_RE.fullmatch(net_pos):
            raise RuntimeError(
                f"net_pos contains unsafe characters: {_scrub(net_pos)}"
            )
        if not _SAFE_NET_NAME_RE.fullmatch(net_neg):
            raise RuntimeError(
                f"net_neg contains unsafe characters: {_scrub(net_neg)}"
            )
        # Wrap the 3 statements in progn so the RAMIC Bridge's
        # let((__vb_r <expr>)) binding accepts them as one expression;
        # otherwise SKILL raises "let: illegal binding form".
        result = self.client.execute_skill(
            f"progn("
            f'openResults("{psf_dir}") '
            f"selectResult('tran) "
            f'plot(VT("{net_pos}") - VT("{net_neg}"))'
            f")"
        )
        # Check for SKILL-side failure: the call returns a Viva plot
        # handle on success.  *Error*, nil, or empty output means the
        # plot failed (wrong PSF path, no tran data, etc.).
        output = _cap_remote_output(
            getattr(result, "output", "") or "",
            label="_plot_waveform",
        )
        ok = getattr(result, "ok", True)
        if not ok:
            errors = getattr(result, "errors", [])
            raise RuntimeError(
                f"Waveform SKILL call failed: {_scrub('; '.join(errors) if errors else 'unknown')}"
            )
        if not output.strip() or output.strip() == "nil" or "*Error*" in output:
            raise RuntimeError(
                f"Waveform SKILL returned failure indicator: {_scrub(output[:120])}"
            )

    def _upload_skill_inline(self, local_path: Path) -> None:
        """Send the text of a .il file to remote host via execute_skill.

        SECURITY: controlled bypass of _check_skill_entrypoint. The path
        must resolve under self._skill_dir (the PC-side repo), the file
        must be <= _SKILL_INLINE_MAX_BYTES, and the content is linted for
        forbidden SKILL primitives. Content is wrapped in progn(...) so
        the RAMIC Bridge's let((__vb_r <expr>)) accepts the multi-form
        file as one expression.

        Why: remote-side .il files can go stale (no file-sync). PC-side
        inline upload makes the PC tree the source of truth for
        procedure bindings. Added for Task E2 (2026-04-22): E2E log
        showed remote host still had the 0-arg safeReadOpPointAfterTran after
        Task B1 changed it to 1-arg.
        """
        skill_dir = self._skill_dir.resolve()
        try:
            path = local_path.resolve()
            path.relative_to(skill_dir)
        except (OSError, ValueError):
            raise RuntimeError(
                "Inline SKILL upload rejected: path not under skill_dir "
                f"(name={local_path.name})"
            ) from None
        if path.suffix != ".il":
            raise RuntimeError(
                f"Inline SKILL upload rejected: not a .il file ({path.name})"
            )
        size = path.stat().st_size
        if size > _SKILL_INLINE_MAX_BYTES:
            raise RuntimeError(
                "Inline SKILL upload rejected: file too large "
                f"(name={path.name}, size={size})"
            )
        content = path.read_text(encoding="utf-8")
        # SKILL single-line comments start with ";" and run to end-of-line.
        # Strip them BEFORE the forbidden-primitive lint so documentation
        # mentioning a blocked token (e.g. safe_ocean.il:87 has "evalstring("
        # in a comparative comment) doesn't bounce the real file back to the
        # legacy load() path — which is the exact remote-staleness the inline
        # upload was introduced to avoid. The uploaded text preserves the
        # original structure but normalizes non-ASCII comment glyphs to spaces;
        # the bridge transport has historically failed on non-ASCII payloads.
        upload_content = "".join(
            ch if ord(ch) < 128 else " " for ch in content
        )
        lint_view = re.sub(r";[^\n]*", "", content)
        match = _SKILL_INLINE_FORBIDDEN_RE.search(lint_view)
        if match:
            raise RuntimeError(
                "Inline SKILL upload rejected: forbidden primitive "
                f"{_scrub(repr(match.group(1)))} in {path.name}"
            )
        result = self.client.execute_skill(f"progn({upload_content})")
        ok = getattr(result, "ok", True)
        output = getattr(result, "output", "") or ""
        if not ok or "*Error*" in output:
            errors = getattr(result, "errors", None)
            detail = errors if errors else output[-500:]
            raise RuntimeError(
                "Inline SKILL upload failed for "
                f"{path.name}: {_scrub(str(detail))}"
            )

    def _load_skill_helpers(self) -> None:
        """Load SKILL safety scripts on the remote host server.

        Preferred path: _upload_skill_inline — sends the PC-side file
        contents directly to remote host, making PC the source of truth for
        procedure definitions (remote-side files can be stale).

        Fallback path: load("<remote_path>") — used only when the PC-side
        .il is missing (legacy deployments or unit-test environments).
        """
        scripts = [
            "helpers.il",
            "safe_read_schematic.il",
            "safe_read_op_point.il",
            "safe_set_param.il",
            "safe_ocean.il",
            "safe_maestro.il",
            "safe_patch_netlist.il",
            "safe_spec_scaffold.il",
            "safe_mae_find.il",
        ]
        for script in scripts:
            pc_path = self._skill_dir / script
            if pc_path.exists():
                try:
                    self._upload_skill_inline(pc_path)
                    continue
                except Exception as exc:
                    logger.warning(
                        "SKILL inline upload of %s failed (err=%s) — falling "
                        "back to remote-side load()",
                        script, _scrub(str(exc)),
                    )
            else:
                # Never log the absolute path — it exposes local filesystem
                # layout (e.g. C:\Users\<user>\...). Disclose script name
                # and path length only.
                logger.warning(
                    "SKILL script %s not found on PC (path_len=%d) — falling "
                    "back to remote-side load()",
                    script, len(str(pc_path)),
                )
            # Legacy load() fallback requires --remote-skill-dir. Without it
            # we cannot tell remote host where to load from, so SKILL is disabled
            # (matches legacy short-circuit behavior).
            if not self._remote_skill_dir:
                self._skill_loaded = False
                return
            load_path = f"{self._remote_skill_dir}/{script}"
            try:
                self.client.execute_skill(f'load("{load_path}")')
            except Exception as exc:
                # Do not pass exc_info=True: the formatted traceback would
                # include PC-side absolute paths and potentially remote host
                # error text. Scrub the message manually instead.
                logger.warning(
                    "Failed to load SKILL script %s on remote host (err=%s) — "
                    "falling back to Python-only filtering",
                    script, _scrub(str(exc)),
                )
                self._skill_loaded = False
                return
        self._skill_loaded = True
        logger.info("remote-side SKILL safety scripts loaded successfully")

    def _alias_cell(self, original_cell: str) -> str:
        # Pass-through only if remote host already returned a known generic alias
        # (or the generic fallback itself). Anything else is treated as a
        # remote-side filter gap: log its length only â€“ never the name â€“ and
        # return the generic fallback.
        if original_cell in self._known_aliases:
            return original_cell
        if original_cell == self.generic_cell_name:
            return original_cell
        logger.warning(
            "Non-generic cell name reached PC (len=%d) â€“ remote host aliasing gap; "
            "replacing with %s.",
            len(original_cell), self.generic_cell_name,
        )
        return self.generic_cell_name

    @staticmethod
    def _check_skill_entrypoint(expr: str) -> None:
        """Reject SKILL strings whose head call is not on the allow-list.

        The agent/LLM reaches remote host only through the sanitizing wrappers in
        skill/*.il (safeReadSchematic / safeReadOpPoint / safeSetParam) or
        the legacy fallbacks (read_schematic / read_op_point /
        set_instance_param). Raw SKILL like hiOpenLib / dbOpenCellViewByType
        / load() must never be forwarded here â€“ they bypass PDK sanitization.

        The SKILL language identifier grammar is strictly ASCII letters,
        digits and underscores. We therefore reject any expression that
        contains non-ASCII characters outright; otherwise an attacker
        could smuggle in a call like ``safeReadSchematic(?("x") "cell")``
        whose ? is a real Unicode letter in many regex engines but not in
        our ASCII-only ``_SKILL_ANY_CALL_RE``, causing the nested-call
        scanner to miss it entirely. Closing this gap is a P0 requirement.
        """
        if not expr.isascii():
            raise ValueError(
                "SKILL expression must be pure ASCII "
                f"(got len={len(expr)}, non-ASCII chars present)."
            )
        # Reject ASCII control characters (C0 0x00-0x1F and DEL 0x7F),
        # except for the three common whitespace forms that `\s` in
        # _SKILL_ENTRYPOINT_RE already tolerates (tab, LF, CR). NUL, BEL,
        # BS, VT, FF and friends have no place in a legitimate SKILL
        # expression, and permitting them enables embedded-NUL-style
        # bypasses (e.g. ``safeReadSchematic\x00(...)``) where the gate
        # sees a legal prefix but the client may execute past the NUL.
        for ch in expr:
            code = ord(ch)
            if code < 0x20 and ch not in ("\t", "\n", "\r"):
                raise ValueError(
                    "SKILL expression contains disallowed control char "
                    f"(got len={len(expr)}, code=0x{code:02x})."
                )
            if code == 0x7F:
                raise ValueError(
                    "SKILL expression contains disallowed DEL char "
                    f"(got len={len(expr)})."
                )
        match = _SKILL_ENTRYPOINT_RE.match(expr)
        if not match:
            raise ValueError(
                "SKILL expression must start with an allowed function call "
                f"(got len={len(expr)})."
            )
        entrypoint = match.group(1)
        if entrypoint not in _ALLOWED_SKILL_ENTRYPOINTS:
            raise ValueError(
                f"SKILL entrypoint {_scrub(repr(entrypoint))} is not allowed. "
                f"Allowed: {sorted(_ALLOWED_SKILL_ENTRYPOINTS)}"
            )
        # Scan the entire expression for nested `identifier(` patterns.
        # Any nested call that is not the outer entrypoint itself and not
        # in the data-constructor allow-list is a bypass attempt (e.g.
        # safeReadSchematic(load("/evil.il") "cell")) and must be rejected.
        allowed_any = _ALLOWED_SKILL_ENTRYPOINTS | _ALLOWED_SKILL_NESTED
        for nested in _SKILL_ANY_CALL_RE.findall(expr):
            if nested not in allowed_any:
                raise ValueError(
                    f"Nested SKILL call {_scrub(repr(nested))} is not allowed. "
                    f"Allowed nested: {sorted(_ALLOWED_SKILL_NESTED)}"
                )

    def _execute_skill_json(self, expr: str, timeout: int | None = None) -> dict:
        self._check_skill_entrypoint(expr)
        if timeout is None:
            result = self.client.execute_skill(expr)
        else:
            result = self.client.execute_skill(expr, timeout=timeout)
        self._raise_on_skill_failure(result, expr)

        if isinstance(result, dict):
            if "error" in result:
                raise RuntimeError(
                    f"SKILL helper returned error: {_scrub(str(result['error']))}"
                )
            return result

        payload = _cap_remote_output(
            getattr(result, "output", result),
            label="_skill_helper_dispatch",
        )
        if isinstance(payload, dict):
            if "error" in payload:
                raise RuntimeError(
                    f"SKILL helper returned error: {_scrub(str(payload['error']))}"
                )
            return payload
        if not isinstance(payload, str):
            raise TypeError(
                "Expected SKILL command to return JSON text, got "
                f"type={type(payload).__name__}"
            )

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            # Use `from None` so JSONDecodeError.doc (the raw payload,
            # which may contain foundry names or absolute paths) is not
            # chained into the RuntimeError via __cause__.
            raise RuntimeError(
                "SKILL helpers must return JSON text to Python. "
                f"Got type=str len={len(payload)}"
            ) from None

        # Some SKILL transports (e.g. virtuoso-bridge-lite) JSON-encode
        # string return values, producing a double-encoded payload
        # where the first json.loads() yields the inner JSON *text* as
        # a str rather than the decoded object. Unwrap exactly one
        # extra level (bounded â€“ no loop, no recursion) when that
        # happens. Still scrubs on failure: no payload content is
        # leaked into the exception.
        if isinstance(decoded, str):
            try:
                decoded = json.loads(decoded)
            except json.JSONDecodeError:
                raise RuntimeError(
                    "SKILL helpers must return JSON text to Python. "
                    f"Got type=str after single decode, len={len(decoded)}"
                ) from None

        if not isinstance(decoded, dict):
            raise TypeError(
                "Expected SKILL command to decode to dict, got "
                f"type={type(decoded).__name__}"
            )
        if "error" in decoded:
            raise RuntimeError(
                f"SKILL helper returned error: {_scrub(str(decoded['error']))}"
            )
        return decoded

    def _raise_on_skill_failure(self, result: Any, label: str) -> None:
        ok = getattr(result, "ok", None)
        if ok is False:
            errors = getattr(result, "errors", [])
            message = "; ".join(errors) if errors else "unknown error"
            # Both label (originates from an expr that may embed lib/cell
            # names) and the joined error text may carry foundry names or
            # absolute paths reported by remote/Spectre. Scrub both.
            raise RuntimeError(
                f"{_scrub(label)} failed: {_scrub(message)}"
            )

    def _strip_model_info(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._strip_model_info(child)
                for key, child in value.items()
                if key.lower() != "model" and not self._is_model_info(key)
            }
        if isinstance(value, list):
            return [self._strip_model_info(child) for child in value]
        return value

    @staticmethod
    def _normalize_param_name(key: str) -> str:
        normalized = key.strip().lower()
        if not normalized or not re.fullmatch(r"[a-z][a-z0-9_]*", normalized):
            raise ValueError(f"Invalid parameter name (len={len(key)})")
        return normalized

    def _is_allowed_param_name(self, name: Any) -> bool:
        """Two-layer param name check. Pure predicate â€“ never raises, never
        rewrites the key. Callers MUST pass the original string to SKILL-call
        builders; ``_normalize_param_name()`` is used here only to query the
        Layer 1 set (case-insensitive).

        Layer 1: core YAML whitelist, case-insensitive membership. Wrapped
                 in try/except because ``_normalize_param_name`` raises
                 ValueError on invalid strings (e.g. "1foo", "foo-bar", "").
                 Those fall through to Layer 2, which will also reject them.
        Layer 2: name matches ^[a-zA-Z][a-zA-Z0-9_]{0,31}$ AND no blocklist
                 substring (case-insensitive).
        """
        if not isinstance(name, str):
            return False
        # Reject whitespace-padded names upfront. ``_normalize_param_name``
        # internally strips, so Layer 1 alone would accept " w" / "W\n"
        # while SKILL's ``safeHelpers_validateParamName`` (no strip) would
        # reject them, and the original whitespace-padded key would leak
        # into the SKILL expression via Sites 2/3's builder.
        if name != name.strip():
            return False
        # Layer 1: case-insensitive membership against core set.
        try:
            if self._normalize_param_name(name) in self.allowed_params:
                return True
        except ValueError:
            pass  # Fell out of Layer 1's own validator; Layer 2 will also reject.
        # Layer 2: strict safe-char pattern on ORIGINAL case.
        if not _SAFE_PARAM_NAME_RE.fullmatch(name):
            return False
        lowered = name.lower()
        # Rev 4: generic SKILL-injection / reserved-word blocklist (substring).
        if any(word in lowered for word in _BLOCKED_PARAM_WORDS):
            return False
        # Rev 5: exact-match reject against BSIM model intrinsic names.
        # self.model_info_keys is a frozenset of lowercased tokens; exact
        # match avoids the substring false-positives that a "word in lowered"
        # check would produce on names like "stk1" / "link2" / "mu0level".
        if lowered in self.model_info_keys:
            return False
        # Rev 5: foundry-leak prefix check mirrors _scrub()'s output-side
        # defense on the input side, closing the asymmetry where names like
        # "tsmc_secret_key" or "Nch_Alpha" could enter desVar()/maeSetVar().
        if _FOUNDRY_LEAK_RE.search(name):
            return False
        return True

    @staticmethod
    def _format_param_value(value: Any) -> str:
        return _format_param_atom_value(value)

