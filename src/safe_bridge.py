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
import re
from pathlib import Path
from typing import Any

import yaml
from virtuoso_bridge import VirtuosoClient
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
_SAFE_NET_NAME_RE = re.compile(r"^/[A-Za-z0-9_]+$")
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
    r"\b(?:nch_|pch_|cfmom|rppoly|rm1_|tsmc|tcbn)\w*",
    re.IGNORECASE,
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
    "safeReadOpPoint",
    # Stage 1 rev 7 (2026-04-19): tranOp-based op-point read; no dedicated
    # DC analysis required. Dual-signed by probe 7 v3+v4 (dr: handle +
    # hard-coded handle->prop dispatcher in safe_read_op_point.il).
    "safeReadOpPointAfterTran",
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


def _validate_name(name: str, label: str = "name") -> None:
    """Validate a lib/cell/instance name against injection attacks."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid {label} (len={len(name)}). "
            "Only alphanumeric, underscore, dot, and hyphen are allowed."
        )


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
            f"list({var_pairs}) list({analyses_literal}))",
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
             ``V_cm + offset_mV/1000``;
          4. rewrites ``scs_path`` replacing the existing ``ic`` line
             (or inserting one before ``tran``) in place.

        Returns ``{"ok": True, "numBiasNodes": N, "numPerturb": M,
        "vcmMeasured": float}`` on success; raises RuntimeError on
        helper-reported failure. Caller (``plan_auto.PlanAuto``) is
        responsible for converting exceptions into best-effort logs.

        ``perturb_nodes`` shape: ``[{"name": str, "offset_mV": float},
        ...]``. Names are validated (identifier + optional dotted
        hierarchy); offsets are coerced to numeric literals.
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
        skill_expr = (
            f'safePatchNetlistIC("{scs_path}" "{fc_path}" '
            f'{perturb_list_expr} {vcm_hint:g})'
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
        # R13: Maestro GUI Design Variables panel is NOT updated by
        # maeSetVar/axlSetVarValue (EXPLORER-8127 edit lock). Log a
        # copy-pasteable table so the user can manually sync if needed.
        self._log_manual_sync_table(
            design_vars,
            scope_lib=self._scope_lib,
            scope_tb_cell=self._scope_tb_cell,
            session=result_json.get("session", ""),
        )
        return _scrub(result_json)

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
        output = getattr(result, "output", "") or ""
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
        # upload was introduced to avoid. The original content (comments
        # intact) is what we actually send to SKILL.
        lint_view = re.sub(r";[^\n]*", "", content)
        match = _SKILL_INLINE_FORBIDDEN_RE.search(lint_view)
        if match:
            raise RuntimeError(
                "Inline SKILL upload rejected: forbidden primitive "
                f"{_scrub(repr(match.group(1)))} in {path.name}"
            )
        self.client.execute_skill(f"progn({content})")

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

        payload = getattr(result, "output", result)
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

