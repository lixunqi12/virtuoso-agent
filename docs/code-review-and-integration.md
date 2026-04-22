# Code Review & SKILL Integration Plan

## Part 1: Code Review

### 1.1 safe_bridge.py — Bugs & Security Issues

| # | Severity | Location | Issue | Fix |
|---|----------|----------|-------|-----|
| 1 | **Medium** | `_alias_cell()` L184-187 | Reverse-lookup bug: `self.cell_map.values()` check means if the input happens to equal a *generic* name (e.g., someone names a cell "NMOS"), it returns it as-is without mapping. An attacker could craft a cell named like a generic alias to bypass mapping. | Remove the `if original_cell in self.cell_map.values()` branch, or check the values set only after the `get()` miss. |
| 2 | **Low** | `_is_model_info()` L141-144 | Substring match is too broad: `any(param in key_lower ...)` will match `"vth"` inside `"vth"` (the safe OP key) — this means `_strip_model_info` could accidentally strip the safe `vth` key if it's at the top level. In practice this is masked because `_sanitize_op_point` uses a whitelist, but `_strip_model_info` used independently (e.g., in `simulate()`) has this false-positive. | Use exact match or word-boundary matching. The safe keys and model keys don't currently overlap at substring level (`vth` vs `vth0`), but `"k1"` would match any key containing `"k1"` like `"clock1"`. |
| 3 | **Medium** | `set_params()` L169-176 | The SKILL expression is built by string interpolation: `f'set_instance_param("{lib}" "{cell}" "{instance}" list({param_str}))'`. Although names are validated by `_NAME_RE`, the param values go through `_format_param_value()` which allows engineering strings like `"10u"`. If a future maintainer relaxes the regex, this becomes a SKILL injection vector. | Defense-in-depth: escape or quote param values explicitly for SKILL. |
| 4 | **Info** | `_sanitize_op_point()` L120 | The `vdd`/`vss` passthrough logic (`key.lower() in {"vdd", "vss"} and not self._is_model_info(key)`) is unusual — `_is_model_info("vdd")` is always False, so the `and` clause is redundant. Also, non-dict values at top level (like a float for `vdd`) pass through unsanitized. | Simplify: just check `key.lower() in {"vdd", "vss"}`. |
| 5 | **Info** | `_execute_skill_json()` L189-216 | Multiple type-checking branches (`isinstance(result, dict)`, `getattr(result, "output"...)`) suggest the VirtuosoClient return type is poorly defined. Not a bug, but fragile. | Document expected return type or add a protocol/type. |

### 1.2 agent.py — Bugs & Logic Issues

| # | Severity | Location | Issue | Fix |
|---|----------|----------|-------|-----|
| 1 | **High** | `_parse_changes()` L147-149 | The first regex `r"```(?:json)?\s*(\{.*?\})\s*```"` uses `.*?` (non-greedy) inside backtick fences. If the LLM returns multiple JSON blocks, only the **first** `{...}` is captured, which might be a reasoning block, not the changes block. | Use a more targeted regex that looks for `"changes"` inside the block, or try all matches and pick the one containing `"changes"`. |
| 2 | **Medium** | `_parse_changes()` L153-160 | The fallback regex `r"\{[^{}]*\"changes\"[^{}]*\[.*?\]\s*\}"` uses `[^{}]*` which prevents nested objects inside `params`. A change like `{"instance":"M1","params":{"w":"10u"}}` contains nested `{}`, so this regex can fail to match valid responses. | Use the first regex as primary (it's more reliable), or parse all `{...}` blocks and try `json.loads` on each. |
| 3 | **Medium** | `_format_topology()` L211-236 | Uses `inst.get("name", "?")` but `safeReadSchematic` returns `"instName"` as the key. The field names don't match: SKILL returns `instName`, `cell`, `lib`, `params`, `nets`; the formatter expects `name`, `cell`, `lib`, `params`. Also `connections` is looked for as a separate top-level key, but SKILL embeds nets per-instance. | Align field names between SKILL output and Python consumer. See Integration section below. |
| 4 | **Low** | `_meets_spec()` L181-207 | No handling of `"=="` with tolerance — the epsilon `1e-9` is hardcoded, which doesn't work for values in the nano/pico range typical in IC design. | Use relative tolerance or make it configurable. |
| 5 | **Low** | `run()` L93-142 | The LLM message history grows unboundedly. With 20 iterations, this could exceed token limits for smaller models. | Add a sliding window or summarize old iterations. |

### 1.3 analyzer.py — Bugs & Issues

| # | Severity | Location | Issue | Fix |
|---|----------|----------|-------|-----|
| 1 | **High** | `extract_tran_metrics()` L208 | Slew rate calculation is inverted: `max_slew * 1e-6` divides by 1M, but slew rate in V/µs should be `max_slew / 1e6` only if `max_slew` is in V/s. The math is correct (`dvdt` is V/s, dividing by 1e6 gives V/µs), but the variable name `1e-6` is confusing. **However**, the actual bug is: `max_slew * 1e-6` = V/s × 10⁻⁶ = V/µs is actually wrong — it should be `max_slew / 1e6` which is the same numerically. Wait — `max_slew * 1e-6 == max_slew / 1e6`. OK, numerically correct but semantically misleading. | Use `max_slew / 1e6` for clarity, or add a comment. |
| 2 | **Medium** | `extract_ac_metrics()` L50-52 | Heuristic `if gain_dB.max() > 100` to detect linear-vs-dB is fragile. A high-gain amplifier with >100 dB gain would be misclassified. | Accept an explicit `gain_format` parameter ("dB" or "linear"). |
| 3 | **Medium** | `extract_dc_metrics()` L142 | `vdd = result.get("vdd", 1.8)` — hardcoded default VDD of 1.8V will give wrong power calculation for different process nodes (0.9V, 1.0V, 3.3V, etc.). | Make VDD a required parameter or read from config. |
| 4 | **Low** | `extract_tran_metrics()` L183 | `final_value = vout[-1]` assumes the signal has settled by end of simulation. If the sim time is too short, this gives a wrong reference. | Accept `expected_final` as optional parameter or use median of last N samples. |

### 1.4 llm_client.py — Issues

| # | Severity | Location | Issue | Fix |
|---|----------|----------|-------|-----|
| 1 | **Medium** | `OllamaClient.chat()` L183-189 | Uses `urllib.request.urlopen()` with no timeout. If Ollama server hangs, the agent hangs forever. | Add `timeout=60` to `urlopen()`. |
| 2 | **Low** | `ClaudeClient.__init__()` L59-61 | `os.environ["ANTHROPIC_API_KEY"]` raises `KeyError` with no helpful message. Other clients use `.get()` with fallback. | Use `os.environ.get()` with a descriptive error. |
| 3 | **Info** | `KimiClient` L131 | `os.environ.get("KIMI_API_KEY", "")` — empty string as default means an empty API key is silently sent, causing opaque auth failures. | Raise early if no key is provided. |

### 1.5 run_agent.py — Issues

| # | Severity | Location | Issue | Fix |
|---|----------|----------|-------|-----|
| 1 | **Medium** | L17-18 | `sys.path.insert(0, str(PROJECT_ROOT))` — path manipulation is fragile. Works for CLI but breaks if imported as a module. | Use proper package install (`pip install -e .`) or `__main__.py`. |
| 2 | **Low** | L88 | `load_dotenv(env_path)` — loads `.env` which may contain API keys. If a malicious `.env` is placed in the project, keys could be overridden. | Document that `.env` should not be committed; add to `.gitignore`. |

---

## Part 2: SKILL Integration Plan

### 2.1 Current Architecture (Python-side filtering)

```
Virtuoso (remote host)                    PC
┌──────────────┐    raw data    ┌──────────────────┐
│ read_schematic│ ─────────────>│ SafeBridge        │
│ read_op_point │               │  ._sanitize()     │──> LLM
│               │               │  ._sanitize_op()  │
└──────────────┘               └──────────────────┘
```

**Problem**: Raw PDK data traverses the network before filtering.

### 2.2 Target Architecture (remote-side filtering, Method A)

```
Virtuoso (remote host)                         PC
┌─────────────────────────┐          ┌──────────────────┐
│ safeReadSchematic()     │  safe    │ SafeBridge        │
│ safeReadOpPoint()       │──JSON──>│  (pass-through +  │──> LLM
│ safeSetParam()          │         │   second filter)   │
│   [helpers.il loaded]   │         └──────────────────┘
└─────────────────────────┘
```

### 2.3 Integration Changes to safe_bridge.py

#### Step 1: Add SKILL loader on connection

```python
class SafeBridge:
    def __init__(self, client, pdk_map_path, spectre=None):
        # ... existing init ...
        self._load_skill_helpers()

    def _load_skill_helpers(self):
        """Load SKILL safety scripts on the remote host server."""
        skill_dir = Path(__file__).parent.parent / "skill"
        for script in ["helpers.il", "safe_read_schematic.il",
                        "safe_read_op_point.il", "safe_set_param.il"]:
            path = skill_dir / script
            if path.exists():
                # Use SKILL load() to load the script on remote host
                self.client.execute_skill(f'load("{path.as_posix()}")')
```

> **Note**: The exact `load()` mechanism depends on how virtuoso-bridge-lite transfers files to remote host. If remote host can't access PC paths, the scripts need to be deployed to a known remote host directory first (e.g., `/tools/skill/safe/`), and the path should be configurable.

#### Step 2: Modify read_circuit()

```python
def read_circuit(self, lib: str, cell: str) -> dict:
    _validate_name(lib, "lib")
    _validate_name(cell, "cell")
    # Call remote-side safe SKILL function instead of raw read
    raw = self._execute_skill_json(
        f'safeReadSchematic("{lib}" "{cell}")'
    )
    # Keep Python-side sanitize as defense-in-depth
    return self._sanitize(raw)
```

#### Step 3: Modify read_op_point()

```python
def read_op_point(self, lib: str, cell: str) -> dict:
    _validate_name(lib, "lib")
    _validate_name(cell, "cell")
    raw = self._execute_skill_json(
        f'safeReadOpPoint("{lib}" "{cell}")'
    )
    # Second filter still applies
    return self._sanitize_op_point(raw)
```

#### Step 4: Modify set_params()

```python
def set_params(self, lib, cell, instance, params):
    _validate_name(lib, "lib")
    _validate_name(cell, "cell")
    _validate_name(instance, "instance")

    # Python-side validation (keep as first line of defense)
    for key in params:
        normalized = self._normalize_param_name(key)
        if normalized not in self.allowed_params:
            raise ValueError(f"Parameter '{key}' not allowed.")

    # Build SKILL param list
    param_list_str = "list("
    for key, value in params.items():
        safe_val = self._format_param_value(value)
        param_list_str += f'list("{self._normalize_param_name(key)}" "{safe_val}") '
    param_list_str += ")"

    result_json = self._execute_skill_json(
        f'safeSetParam("{lib}" "{cell}" "{instance}" {param_list_str})'
    )
    if not result_json.get("ok", False):
        raise RuntimeError(f"safeSetParam failed: {result_json.get('error', 'unknown')}")
```

### 2.4 Should Python-side filtering be kept?

**Yes — keep it as a second defense layer.** Reasons:

1. **Defense-in-depth**: If a SKILL script has a bug or is tampered with on remote host, the Python filter catches leaks before data reaches the LLM.
2. **Version mismatch**: remote host SKILL scripts might be outdated while Python is updated. The Python filter covers the gap.
3. **Minimal overhead**: The Python filtering is lightweight (dict iteration) — no performance penalty.
4. **Different threat model**: SKILL filtering prevents data from leaving remote host. Python filtering prevents data from reaching the LLM. Both are needed.

**Recommendation**: Keep `_sanitize()` and `_sanitize_op_point()` exactly as they are. Add a log warning if the Python filter catches something that SKILL should have already filtered — this signals a SKILL-side bug:

```python
def _sanitize(self, data):
    sanitized = copy.deepcopy(data)
    for inst in sanitized.get("instances", []):
        original_cell = inst.get("cell", "")
        new_cell = self._alias_cell(original_cell)
        if new_cell != original_cell:
            logger.warning(
                "Python filter caught unmapped cell '%s' — "
                "SKILL-side filter may have a gap", original_cell
            )
        inst["cell"] = new_cell
        # ... rest unchanged
```

### 2.5 Field Name Alignment

The SKILL scripts output these JSON keys:

| SKILL output key | Current Python consumer expects | Action needed |
|---|---|---|
| `instName` | `name` (in `_format_topology`) | Fix `_format_topology` to use `instName` |
| `nets` (object per instance) | `connections` (top-level list) | Fix `_format_topology` to read per-instance `nets` |
| `params` (object) | `params` (object) | OK, no change |
| `cell` | `cell` | OK |
| `lib` | `lib` | OK |
| `pins` (top-level array) | Not consumed | OK, ignored |

Fix in `agent.py` `_format_topology()`:

```python
@staticmethod
def _format_topology(circuit: dict) -> str:
    lines = ["### Instances"]
    for inst in circuit.get("instances", []):
        name = inst.get("instName", inst.get("name", "?"))
        cell = inst.get("cell", "?")
        params = inst.get("params", {})
        param_str = ", ".join(f"{k}={v}" for k, v in params.items())
        lines.append(f"- **{name}** (GENERIC_PDK/{cell}): {param_str}")

        nets = inst.get("nets", {})
        if nets:
            net_str = ", ".join(f"{pin}->{net}" for pin, net in nets.items())
            lines.append(f"  Connections: {net_str}")

    pins = circuit.get("pins", [])
    if pins:
        lines.append("\n### Pins")
        for pin in pins:
            pname = pin.get("name", "?") if isinstance(pin, dict) else str(pin)
            lines.append(f"- {pname}")

    return "\n".join(lines)
```

### 2.6 SKILL Deployment Checklist

1. [ ] Deploy `skill/*.il` files to a known path on remote host server (e.g., `/opt/virtuoso-agent/skill/`)
2. [ ] Update `SafeBridge.__init__` to load SKILL scripts via bridge on startup
3. [ ] Add config option for remote-side skill path (`skill_dir` in YAML or env var)
4. [ ] Fix `_format_topology()` field name alignment
5. [ ] Add integration tests that verify SKILL JSON output matches Python `_sanitize()` output
6. [ ] Add the warning logger for Python-catches-what-SKILL-missed
7. [ ] Test with IC23.1 — verify no SKILL++ syntax errors in the `.il` files

---

## Part 3: Priority Summary

### Must fix (High severity)
- `agent.py` `_parse_changes()` — regex may capture wrong JSON block
- `agent.py` `_format_topology()` — field name mismatch with SKILL output

### Should fix (Medium severity)
- `safe_bridge.py` `_alias_cell()` — reverse-lookup bypass
- `analyzer.py` gain format heuristic — fragile 100 dB threshold
- `analyzer.py` hardcoded VDD 1.8V
- `llm_client.py` OllamaClient — no timeout on HTTP request
- `safe_bridge.py` `set_params()` — SKILL injection defense-in-depth

### Nice to have (Low / Info)
- Token window management in agent loop
- Better error messages for missing API keys
- `_meets_spec()` tolerance handling for nano-scale values
