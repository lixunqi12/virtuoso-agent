# virtuoso-agent

LLM-driven closed-loop optimization agent for analog / mixed-signal
circuits. Two simulation backends:

- **Maestro / Spectre** (via OCEAN) — Cadence Virtuoso schematic flow.
- **HSpice (remote)** — direct SSH-driven HSpice on a Linux farm,
  parameterised via `.alter` blocks and `.measure` directives.

[简体中文](#zh) · [English](#en)

This project depends on
[Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite)
for the PC ↔ remote-host SKILL IPC channel. virtuoso-agent layers a
PDK scrub barrier, OCEAN sub-process isolation, spec evaluator, and
LLM closed-loop control on top of it.

---

<a id="zh"></a>

## 简体中文

### 概览

Agent 读取一份 Markdown spec（设计目标 + 可调变量 + 评估方法），驱动
LLM 提建议，跑仿真，从结果里算 pass/fail，回填给 LLM 继续迭代，直到
满足规格或达到迭代上限。

```
  PC (Windows / Linux)                        远端主机 (Linux + Cadence / HSpice)
  ┌──────────────────────────┐  SSH tunnel  ┌────────────────────────────────┐
  │ virtuoso-agent           │ ◀──────────▶│ Maestro / Spectre / OCEAN       │
  │   CircuitAgent loop      │              │ 或 HSpice (.sp + .alter)        │
  │   SafeBridge (脱敏)       │              │ virtuoso-bridge-lite (SKILL IPC)│
  │   OceanWorker / HspiceWorker             │                                │
  │   LLM client             │              │                                │
  └──────────────────────────┘              └────────────────────────────────┘
```

### 核心模块

| 路径 | 作用 |
|------|------|
| `src/agent.py` | CircuitAgent 主循环（两种后端共用） |
| `src/safe_bridge.py` | OCEAN 路径 PDK 脱敏 + 参数白名单 + 分层 schematic 读取 |
| `src/ocean_worker.py` | 一次性 OCEAN 子进程跑 PSF dump，超时直接 kill |
| `src/hspice_worker.py` | HSpice 后端：远程 SSH 跑 hspice，回收 `.mt0` |
| `src/hspice_scrub.py` | HSpice 路径下的 `.sp` / `.mt0` / `.lis` PDK 脱敏 |
| `src/hspice_resolver.py` | 把 spec 的 `metrics:` 和 `.mt` 列对齐，含 `linregress` reducer |
| `src/netlist_reader.py` | 把 Virtuoso 导出的 `.sp` 解析成 LLM 友好的 Markdown |
| `src/sp_rewrite.py` / `src/remote_patch.py` | 远端 in-place 改写 `.sp` 设计变量 |
| `src/spec_evaluator.py` / `src/spec_validator.py` | 通用 metric 评估 + JSON 契约校验 |
| `src/llm_client.py` | Claude / Gemini / Kimi / MiniMax / Ollama 统一接口 |
| `src/plan_auto.py` | 振荡器类电路的 bias IC 自动回写 |
| `skill/helpers.il`, `skill/safe_*.il` | remote 端安全 SKILL 入口层 |

### 安装

#### 远端（跑 Virtuoso 或 HSpice 的 Linux 主机）

参考 [virtuoso-bridge-lite/AGENTS.md](https://github.com/Arcadia-1/virtuoso-bridge-lite/blob/main/AGENTS.md)
启动 bridge daemon：

```bash
module load cadence/ic_23.1
pip3 install --user virtuoso-bridge-lite
virtuoso-bridge start
```

把本仓的 `skill/` 推到 remote host，运行时通过 `--remote-skill-dir`
指过去。HSpice 后端额外需要远端 `module load hspice/...`，agent
跑 ssh 调度时会自己包 `module load`。

#### PC 端

```bash
git clone https://github.com/lixunqi12/virtuoso-agent.git
cd virtuoso-agent
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt   # 会从 GitHub 拉 virtuoso-bridge-lite
cp config/.env.template config/.env
cp config/hspice_scrub_patterns.template.yaml config/hspice_scrub_patterns.private.yaml
```

编辑 `config/.env`：

```
VB_REMOTE_HOST=your-host.example.edu
VB_REMOTE_USER=your_username
VB_REMOTE_PORT=65081
VB_LOCAL_PORT=65082

# 任选其一（缺省读 DEFAULT_LLM）
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
KIMI_API_KEY=
MINIMAX_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434

DEFAULT_LLM=claude
```

编辑 `config/hspice_scrub_patterns.private.yaml`（**永不入库**）：填入
真实 PDK / 厂商 token，作为公共 template + Python 内置正则的额外
脱敏黑名单。

### 使用

#### Maestro / Spectre 后端

```bash
python scripts/run_agent.py \
    --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \
    --spec config/LC_VCO_spec.md \
    --llm claude \
    --remote-skill-dir /project/<user>/tool/virtuoso-agent/skill \
    --max-iter 20 --auto-bias-ic
```

前置条件：Maestro 已开 `LC_VCO_tb` session，且 spec §3 列出的 design
variables 都有数值默认值（否则启动时 `SFE-1997`）。

#### HSpice 后端（远端闭环）

```bash
python scripts/run_agent.py \
    --sim-backend hspice \
    --hspice-loop \
    --spec config/<your_dut>_spec.md \
    --spec-root specs_work \
    --remote-spec-root /project/<user>/work/<your_run> \
    --testbench <your_tb>.sp \
    --llm claude --max-iter 20
```

`--testbench` 指 HSpice 真正执行的入口 `.sp`（即 `hspice ./<basename>.sp`
的那一份）。每轮迭代被 LLM 改写的目标文件由 spec 的
`hspice.param_rewrite_target` 字段决定，不是 `--testbench`。

仓库里只附了 LC_VCO 的 spec 模板（`config/LC_VCO_spec.md` /
`config/LC_VCO_40G_spec.md`）做参考。HSpice 后端 spec 写法见
`docs/hspice_backend.md`，里面有 `metrics:` / `reduce:` / `linregress`
等 reducer 的契约说明。

#### 读 schematic（不仿真）

```bash
# 单层：和 agent 首轮 prompt 字节一致
python scripts/read_schematic.py --lib pll --cell LC_VCO

# 分层（同库子单元展开），auto = 走到底
python scripts/read_schematic.py --lib pll --cell LC_VCO \
    --depth auto --format both --output ./out/lc_vco

# HSpice 模式：从 .sp + 测试台 渲染 LLM 友好 Markdown
python scripts/read_schematic.py \
    --netlist specs_work/netlist.scrubbed.sp \
    --testbench specs_work/dut_tb.scrubbed.sp
```

### Spec 写法

`config/LC_VCO_spec.md`（20 GHz）和 `config/LC_VCO_40G_spec.md`（40 GHz）
是参考模板。Maestro 后端要求每份 spec：

- **§1 Design under test**: lib / cell / tb-cell / VDD / 目标频率
- **§2 Machine-readable eval block**: `signals:` / `windows:` /
  `metrics:` 三段 YAML，`spec_evaluator.py` 直接消费。支持
  `freq_Hz` / `ptp` / `rms` / `duty_pct` 等统计量，以及 `ratio` /
  `t_cross_frac` 等 compound metric
- **§3 Design variables**: LLM 可调的参数、范围、优先级
- **§4 Startup convergence aids**（可选）: 配 `--auto-bias-ic` 用，
  从上一轮 `spectre.fc` 读 bias 回写 `ic` 语句，VCO / latch 类
  亚稳点电路有用

HSpice 后端要求每份 spec 多带：
- 一个 ` ```yaml metrics: ... ``` ` 块（用 `.mt` 列名 + 可选
  `reduce:`，详见 `docs/hspice_backend.md`）；
- 一个 ` ```yaml hspice: ... ``` ` 块，含 `param_rewrite_target` 等。

详细语法见 `docs/spec_authoring_rules.md` / `docs/llm_protocol.md` /
`docs/hspice_backend.md`。

### PDK 数据隔离

公开仓库，真实 PDK token（`tsmc*`, `nch_*`, `pch_*`, `cfmom`,
`rppoly`, `tcbn` 等）绝不能出现在 PC 端源码或日志。双层防线：

1. **远端**：`skill/helpers.il` 读 `~/.virtuoso/pdk_map_private.il`
   （永不入库）把真实 cell 名替换为 generic 别名后才返回。HSpice
   路径下，远端 `scripts/scrub_remote_sp.py` 把 `.sp` 脱敏后再回
   PC 端。
2. **PC 端**：`src/safe_bridge.py` 的 `_scrub()` 和
   `src/hspice_scrub.py` 的 `scrub_sp/scrub_mt0/scrub_lis` 对所有
   入站文本做正则兜底。

`scripts/check_p0_gate.ps1` 是 commit 前的 PDK token 自检，扫
`src/` `skill/` `tests/`，已接 pre-commit hook。

| 文件 | 状态 |
|------|------|
| `config/pdk_map.yaml` | 公开，generic 别名白名单 |
| `config/.env.template` | 公开，仅占位符 |
| `config/.env` | **gitignore**，含 SSH / API key |
| `config/hspice_scrub_patterns.template.yaml` | 公开模板，仅 generic preserve token |
| `config/hspice_scrub_patterns.private.yaml` | **gitignore**，真实厂商 token |
| `~/.virtuoso/pdk_map_private.il`（远端） | **永不入库**，真实 cell 映射表 |

### 开发

```bash
pip install -r requirements.txt
.venv\Scripts\python -m pytest tests/ -v   # 全部单测，不打 remote host
pwsh scripts/check_p0_gate.ps1             # PDK token 泄漏自检
```

测试用 mock `VirtuosoClient` 跑全部路径。集成测要一台能 SSH 的
Virtuoso 主机，跑 `scripts/test_connection.py` smoke。

`tests/fixtures/netlist_reader/sample_chain.sp` 是合成的 9-subckt
inverter chain fixture，用 `DEMO_LIB` 当 library 名，单元测试 + 公开
仓库专用，不映射到任何真实电路。

---

<a id="en"></a>

## English

### Overview

The agent reads a Markdown spec (design goals + tunable variables +
evaluation rules), prompts an LLM for proposed values, runs the
simulator, computes pass/fail from the result, feeds it back, and
iterates until the spec passes or the iteration cap is hit.

```
  PC (Windows / Linux)                        Remote host (Linux + Cadence / HSpice)
  ┌──────────────────────────┐  SSH tunnel  ┌────────────────────────────────┐
  │ virtuoso-agent           │ ◀──────────▶│ Maestro / Spectre / OCEAN        │
  │   CircuitAgent loop      │              │ or HSpice (.sp + .alter)         │
  │   SafeBridge (PDK scrub) │              │ virtuoso-bridge-lite (SKILL IPC) │
  │   OceanWorker / HspiceWorker            │                                  │
  │   LLM client             │              │                                  │
  └──────────────────────────┘              └────────────────────────────────┘
```

### Core modules

| Path | Role |
|------|------|
| `src/agent.py` | CircuitAgent main loop (shared by both backends) |
| `src/safe_bridge.py` | OCEAN-path PDK scrubber + param whitelist + hierarchical schematic reader |
| `src/ocean_worker.py` | One-shot OCEAN subprocess for PSF dump; killed on timeout |
| `src/hspice_worker.py` | HSpice backend: SSH-launches hspice, fetches `.mt0` |
| `src/hspice_scrub.py` | PDK scrub for `.sp` / `.mt0` / `.lis` artefacts |
| `src/hspice_resolver.py` | Maps spec `metrics:` to `.mt` columns; includes `linregress` reducer |
| `src/netlist_reader.py` | Parses Virtuoso-exported `.sp` into LLM-friendly Markdown |
| `src/sp_rewrite.py` / `src/remote_patch.py` | Remote in-place rewrite of `.sp` design variables |
| `src/spec_evaluator.py` / `src/spec_validator.py` | Generic metric evaluation + JSON contract validation |
| `src/llm_client.py` | Unified Claude / Gemini / Kimi / MiniMax / Ollama client |
| `src/plan_auto.py` | Auto bias-IC writeback for oscillator-class circuits |
| `skill/helpers.il`, `skill/safe_*.il` | Remote-side safe SKILL entry layer |

### Install

#### Remote host (the Linux box that runs Virtuoso or HSpice)

Follow [virtuoso-bridge-lite/AGENTS.md](https://github.com/Arcadia-1/virtuoso-bridge-lite/blob/main/AGENTS.md)
to start the bridge daemon:

```bash
module load cadence/ic_23.1
pip3 install --user virtuoso-bridge-lite
virtuoso-bridge start
```

Push the `skill/` directory of this repo to the remote and pass it
via `--remote-skill-dir` at run-time. The HSpice backend additionally
needs `module load hspice/...` available remotely; the agent wraps
its ssh invocations with that automatically.

#### PC side

```bash
git clone https://github.com/lixunqi12/virtuoso-agent.git
cd virtuoso-agent
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.template config/.env
cp config/hspice_scrub_patterns.template.yaml config/hspice_scrub_patterns.private.yaml
```

Edit `config/.env`:

```
VB_REMOTE_HOST=your-host.example.edu
VB_REMOTE_USER=your_username
VB_REMOTE_PORT=65081
VB_LOCAL_PORT=65082

# Pick one (DEFAULT_LLM if not specified on the CLI)
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
KIMI_API_KEY=
MINIMAX_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434

DEFAULT_LLM=claude
```

Edit `config/hspice_scrub_patterns.private.yaml` (**never committed**)
with your real PDK / foundry tokens — these augment the public
template and the built-in regex seeds.

### Usage

#### Maestro / Spectre backend

```bash
python scripts/run_agent.py \
    --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \
    --spec config/LC_VCO_spec.md \
    --llm claude \
    --remote-skill-dir /project/<user>/tool/virtuoso-agent/skill \
    --max-iter 20 --auto-bias-ic
```

Pre-conditions: Maestro has the `LC_VCO_tb` session open, and every
design variable listed in spec §3 has a numeric default (otherwise
the run aborts with `SFE-1997` at startup).

#### HSpice backend (remote closed loop)

```bash
python scripts/run_agent.py \
    --sim-backend hspice \
    --hspice-loop \
    --spec config/<your_dut>_spec.md \
    --spec-root specs_work \
    --remote-spec-root /project/<user>/work/<your_run> \
    --testbench <your_tb>.sp \
    --llm claude --max-iter 20
```

`--testbench` is the entry `.sp` HSpice will execute (i.e. the file
name passed to `hspice ./<basename>.sp`). The file *rewritten* each
iteration with new design vars is determined by the spec's
`hspice.param_rewrite_target` field, not `--testbench`.

Only the LC_VCO spec templates (`config/LC_VCO_spec.md` /
`config/LC_VCO_40G_spec.md`) ship in the public repo. HSpice spec
authoring is documented in `docs/hspice_backend.md` (covers
`metrics:` / `reduce:` / `linregress` reducer contracts).

#### Read schematic (no simulation)

```bash
# Flat read — byte-identical to the agent's first-iteration prompt
python scripts/read_schematic.py --lib pll --cell LC_VCO

# Hierarchical (expand same-library children); auto = full depth
python scripts/read_schematic.py --lib pll --cell LC_VCO \
    --depth auto --format both --output ./out/lc_vco

# HSpice mode — render LLM-friendly Markdown from .sp + testbench
python scripts/read_schematic.py \
    --netlist specs_work/netlist.scrubbed.sp \
    --testbench specs_work/dut_tb.scrubbed.sp
```

### Writing a spec

`config/LC_VCO_spec.md` (20 GHz) and `config/LC_VCO_40G_spec.md`
(40 GHz) are the reference templates. The Maestro backend requires:

- **§1 Design under test**: lib / cell / tb-cell / VDD / target freq
- **§2 Machine-readable eval block**: three YAML fences `signals:` /
  `windows:` / `metrics:` consumed directly by `spec_evaluator.py`.
  Built-in stats include `freq_Hz` / `ptp` / `rms` / `duty_pct`, plus
  compound metrics like `ratio` / `t_cross_frac`.
- **§3 Design variables**: parameters the LLM can tune, with ranges
  and priority hints
- **§4 Startup convergence aids** (optional): used with
  `--auto-bias-ic`. Reads bias from the previous run's `spectre.fc`
  and writes it back as `ic` statements — useful for VCO / latch
  unstable-equilibrium circuits.

The HSpice backend additionally requires:
- A ` ```yaml metrics: ... ``` ` fence — `.mt` column names plus
  optional `reduce:` reducers (see `docs/hspice_backend.md`);
- A ` ```yaml hspice: ... ``` ` fence with `param_rewrite_target`
  and friends.

Full grammar: `docs/spec_authoring_rules.md`,
`docs/llm_protocol.md`, `docs/hspice_backend.md`.

### PDK data isolation

The repo is public; real PDK tokens (`tsmc*`, `nch_*`, `pch_*`,
`cfmom`, `rppoly`, `tcbn`, …) must never appear in PC-side source
or logs. Two layers of defense:

1. **Remote**: `skill/helpers.il` consults
   `~/.virtuoso/pdk_map_private.il` (never committed) to translate
   real cell names into generic aliases before returning. On the
   HSpice path, the remote `scripts/scrub_remote_sp.py` scrubs `.sp`
   files before they cross the SSH boundary.
2. **PC side**: `src/safe_bridge.py::_scrub()` plus
   `src/hspice_scrub.py::scrub_sp/scrub_mt0/scrub_lis` regex-scrub
   every inbound text artefact as a backstop.

`scripts/check_p0_gate.ps1` is a pre-commit self-check that scans
`src/` `skill/` `tests/` for banned tokens; it ships with a
pre-commit hook.

| File | Status |
|------|--------|
| `config/pdk_map.yaml` | public, generic-alias whitelist |
| `config/.env.template` | public, placeholders only |
| `config/.env` | **gitignored**, holds SSH / API keys |
| `config/hspice_scrub_patterns.template.yaml` | public template, generic preserve tokens only |
| `config/hspice_scrub_patterns.private.yaml` | **gitignored**, real foundry tokens |
| `~/.virtuoso/pdk_map_private.il` (remote) | **never committed**, real cell-name map |

### Development

```bash
pip install -r requirements.txt
.venv\Scripts\python -m pytest tests/ -v   # full unit suite, no remote host
pwsh scripts/check_p0_gate.ps1             # PDK token leak check
```

Unit tests run end-to-end with `VirtuosoClient` mocked. Integration
testing needs an SSH-reachable Virtuoso host — run
`scripts/test_connection.py` for a smoke.

`tests/fixtures/netlist_reader/sample_chain.sp` is a synthetic
9-subckt inverter-chain fixture using `DEMO_LIB` as the library
name; it exists purely for unit tests and does not map to any real
circuit.

---

## Credits

- [Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite) —
  the PC ↔ remote-host Virtuoso SKILL IPC layer; this repo's
  `requirements.txt` installs it directly from GitHub. Without it
  there is no virtuoso-agent.
- Cadence Virtuoso / OCEAN / Spectre, Synopsys HSpice — vendor
  tools, not redistributed.

## License

MIT.
