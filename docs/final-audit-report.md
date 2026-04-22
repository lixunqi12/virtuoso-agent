# Virtuoso-Agent Final Audit Report

> **SUPERSEDED (2026-04-17).** The bugs described below (stale `cell_map` feedback
> loop, op-point envelope mismatch, etc.) have been fixed in the current code.
> This document is kept for historical reference only. For the current PDK
> isolation architecture, see:
> - `src/safe_bridge.py` (remote-side SKILL filtering + PC-side `_scrub()` +
>   entrypoint allow-list + `set_scope()` binding)
> - `.quarantine/batch_a_review_codex.md` and sibling reports for the P0/P1
>   dual-review trail.
> Do not rely on the architecture claims below; they describe pre-fix state.

## Scope

Reviewed:

- `src/safe_bridge.py`
- `src/agent.py`
- `src/analyzer.py`
- `src/llm_client.py`
- `scripts/run_agent.py`
- `skill/helpers.il`
- `skill/safe_read_schematic.il`
- `skill/safe_read_op_point.il`
- `skill/safe_set_param.il`
- `tests/test_safe_bridge.py`
- `tests/test_analyzer.py`
- `config/pdk_map.yaml`
- `config/.env` structure only, without inspecting secret values

## Test Execution

Executed:

```powershell
cd <repo-root>
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

Result: `31 passed in 1.41s`

Additional local probes against the live code confirmed two SKILL-path regressions that are not covered by the current tests:

- `_sanitize({"instances": [{"cell": "NMOS", ...}]})` returns `GENERIC_DEVICE`
- `_sanitize_op_point({"instances": {"M1": {"gm": 1e-3, "id": 1e-4}}})` returns `{"instances": {}}`

## Fix Verification

The following previously reported fixes are present and look correct in isolation:

- `src/agent.py:147-166` now iterates fenced JSON blocks and selects one containing `"changes"`.
- `src/agent.py:221-249` now accepts both `instName` and `name`, and formats per-instance `nets`.
- `src/safe_bridge.py:285-286` removed the old reverse-lookup `values()` bypass in `_alias_cell()`.
- `src/analyzer.py:28-59` added `gain_format` handling to AC extraction.
- `src/analyzer.py:134-157` removed the hardcoded `1.8V` default and now requires `vdd` from input or argument.
- `src/llm_client.py:183-189` added `timeout=60` to the Ollama request.
- `src/safe_bridge.py:98-124`, `219-233`, `246-278` added SKILL integration hooks for read/write operations.

However, the SKILL integration is not functionally complete. The most serious remaining issues are below.

## Findings

### 1. High — Enabling SKILL-side schematic filtering destroys device-type information

Files:

- `src/safe_bridge.py:126-145`
- `src/safe_bridge.py:285-286`
- `skill/helpers.il:117-126`
- `skill/safe_read_schematic.il:39-44`

`safeReadSchematic()` returns already-aliased cell names such as `NMOS` / `PMOS`. The Python defense-in-depth pass then feeds those aliases back into `_alias_cell()`, which only knows foundry names from `cell_map`. The result is that every already-sanitized alias falls through to `generic_cell_name`, so known devices become `GENERIC_DEVICE`.

Impact:

- With `_skill_loaded=True`, the LLM loses the distinction between NMOS, PMOS, capacitors, resistors, etc.
- The closed-loop optimizer can no longer reason about topology correctly even though tests still pass.

This is a real integration regression, not a theoretical risk. The local probe reproduced it directly.

### 2. High — `read_op_point()` is incompatible with the SKILL JSON schema and drops all OP data

Files:

- `src/safe_bridge.py:116-124`
- `src/safe_bridge.py:156-169`
- `skill/safe_read_op_point.il:81-89`
- `skill/safe_read_op_point.il:144-171`

`safeReadOpPoint()` returns JSON shaped like:

```json
{
  "cell": "...",
  "lib": "GENERIC_PDK",
  "analysis": "dcOp",
  "instances": {
    "M1": {"gm": ..., "id": ...}
  }
}
```

But `_sanitize_op_point()` expects the Python-only shape:

```json
{
  "M1": {"gm": ..., "id": ...},
  "vdd": 1.8
}
```

When the SKILL path is enabled, `_sanitize_op_point()` treats `"instances"` as if it were a single device record, filters by safe metric names, and returns `{}`. That makes SKILL-side OP reads effectively unusable.

Impact:

- The advertised remote-side OP filtering path does not work end-to-end.
- Current tests miss this because `tests/test_safe_bridge.py:270-281` only mocks the Python-only flattened structure.

### 3. High — The SKILL loader only works for locally visible paths, so remote remote host deployment silently falls back to Python-only filtering

Files:

- `src/safe_bridge.py:61-84`
- `src/safe_bridge.py:246-278`
- `scripts/run_agent.py:115`

`SafeBridge` validates `skill_dir` with local `Path.exists()` and then sends that exact local path string into SKILL `load(...)`. On a normal SSH/remote remote host setup, the Python host filesystem path and the remote host filesystem path are different. That means:

- remote-only remote host paths cannot be configured, because local existence is required first
- local Windows paths are sent directly to remote host, which usually cannot read them
- `_skill_loaded` quietly flips back to `False`, reverting to Python-only filtering

Impact:

- The project currently does not achieve its stated security goal of preventing raw PDK data from crossing the bridge before filtering.
- The code gives the appearance of remote-side filtering support, but typical deployment still transmits raw data over SSH.

### 4. Medium — SKILL helper JSON errors are not raised on the Python side, leading to silent bad states

Files:

- `src/safe_bridge.py:98-124`
- `src/safe_bridge.py:288-315`
- `skill/safe_read_schematic.il:21-31`
- `skill/safe_read_op_point.il:20-49`

The SKILL helper read functions return JSON like `{"error":"..."}` on failure. `_execute_skill_json()` successfully parses that JSON and returns it as a normal dict, and `read_circuit()` / `read_op_point()` do not check for an `"error"` field.

Observed behavior:

- `read_circuit()` can continue with an empty/invalid topology instead of failing fast.
- `read_op_point()` can collapse to `{}` and hide the root cause.

This weakens both debuggability and runtime safety because the optimizer can proceed on invalid inputs without a hard failure.

### 5. Medium — Test coverage does not exercise the SKILL-enabled integration path, so the main regressions ship undetected

Files:

- `tests/test_safe_bridge.py:65-74`
- `tests/test_safe_bridge.py:239-281`

All current `SafeBridge` tests intentionally construct the bridge with a missing `skill_dir`, which forces Python-only behavior. That means the suite never validates:

- `_skill_loaded=True` behavior
- SKILL JSON schema compatibility
- remote-side alias preservation
- end-to-end interaction between SKILL outputs and Python sanitizers

This is why `31/31` tests pass while the two highest-severity integration bugs remain present.

## Security Notes

What looks good:

- Python-side lib/cell/instance validation in `src/safe_bridge.py:37-43`
- parameter whitelist enforcement in `src/safe_bridge.py:206-244`
- parameter atom validation in `src/safe_bridge.py:343-361`
- mirrored name/value validation in `skill/safe_set_param.il:20-165`
- removal of hardcoded VDD and Ollama timeout issue

Remaining security concern:

- Because the remote SKILL loading path is not actually deployable in the common case, the system often falls back to Python-only sanitization. That leaves the original PDK data crossing the SSH bridge before filtering, which is exactly the architecture the SKILL integration was meant to eliminate.

## Overall Assessment

The isolated bug fixes requested in the Python code are mostly present, and the current unit tests are clean. However, the most important claim in this revision — that remote-side SKILL filtering is now integrated and working — is not yet true end-to-end.

Current state:

- Python-only mode: mostly sound for the reviewed behaviors
- SKILL-enabled mode: not production-ready

## Recommended Next Actions

1. Define a single canonical JSON schema for SKILL outputs and make Python consume that exact shape.
2. Fix the alias-preservation path so already-generic cell names remain stable under the second Python filter.
3. Separate local source script discovery from remote remote host load location; do not assume the same filesystem path is valid on both hosts.
4. Make `_execute_skill_json()` or its callers raise on helper payloads containing `"error"`.
5. Add dedicated tests for `_skill_loaded=True` using real SKILL-shaped payloads for both `safeReadSchematic()` and `safeReadOpPoint()`.
