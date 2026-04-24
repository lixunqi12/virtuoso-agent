# virtuoso-agent

[English](#english) | [中文](#中文)

---

## English

LLM-driven closed-loop optimization agent for analog circuits. The agent reads a
Spec (Markdown specification), modifies design variables inside Cadence Virtuoso
Maestro via SKILL / OCEAN, runs transient simulation, reads waveforms from PSF,
evaluates pass/fail, and feeds the result back to the LLM for iteration until
the Spec is met or the max-iteration budget is exhausted.

This project depends on
[Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite)
for the PC ↔ remote-host SKILL IPC channel. virtuoso-agent layers a PDK scrub
layer, an OCEAN subprocess sandbox, a Spec evaluator, and LLM closed-loop
control on top of it.

### Architecture

```
  PC (Windows / Linux)                         remote host (Linux + Virtuoso IC23.1)
  ┌──────────────────────────┐   SSH tunnel   ┌───────────────────────────────┐
  │ virtuoso-agent           │ ◀───────────▶ │ virtuoso-bridge-lite          │
  │   CircuitAgent           │                │   ramic_bridge.il (IPC)       │
  │   SafeBridge  (scrub)    │                │   safe_*.il   (PDK scrub)     │
  │   OceanWorker (sandbox)  │                │   Maestro / Spectre / OCEAN   │
  │   LLM client             │                │                               │
  └──────────────────────────┘                └───────────────────────────────┘
```

Main modules:

| Path | Purpose |
|------|---------|
| `src/agent.py` | CircuitAgent main loop |
| `src/safe_bridge.py` | PC-side PDK scrub + parameter whitelist + hierarchical schematic read |
| `src/ocean_worker.py` | One-shot OCEAN subprocess for PSF dump, kills on timeout |
| `src/spec_evaluator.py` / `src/spec_validator.py` | Computes pass/fail from `metrics:` in the Spec |
| `src/llm_client.py` | Unified interface for Claude / Gemini / Kimi / MiniMax / Ollama |
| `src/plan_auto.py` | Auto bias-IC writeback for oscillator-class circuits |
| `skill/helpers.il`, `skill/safe_*.il` | Safe SKILL entry layer on the remote side |

### Install

#### Remote side (Linux host running Virtuoso)

Follow the virtuoso-bridge-lite
[AGENTS.md](https://github.com/Arcadia-1/virtuoso-bridge-lite/blob/main/AGENTS.md)
to start the bridge daemon:

```bash
module load cadence/ic_23.1
pip3 install --user virtuoso-bridge-lite
virtuoso-bridge start
```

Then use `scripts/sync_to_remote.sh` to push the repo's `skill/` directory to
the remote host, and point to it at runtime via `--remote-skill-dir`.

#### PC side

```bash
git clone https://github.com/lixunqi12/virtuoso-agent.git
cd virtuoso-agent
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt # pulls virtuoso-bridge-lite from GitHub
cp config/.env.template config/.env
```

Edit `config/.env`:

```
VB_REMOTE_HOST=your-host.example.edu
VB_REMOTE_USER=your_username
VB_REMOTE_PORT=65081
VB_LOCAL_PORT=65082

# Pick one
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
KIMI_API_KEY=
MINIMAX_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434

DEFAULT_LLM=claude
```

### Usage

#### Run one closed-loop optimization round

```bash
python scripts/run_agent.py \
    --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \
    --spec config/LC_VCO_spec.md \
    --llm claude \
    --remote-skill-dir /project/<user>/tool/virtuoso-agent/skill \
    --max-iter 20 --auto-bias-ic
```

Prerequisites: an `LC_VCO_tb` session already exists in Maestro, and every
design variable listed in Spec §3 has a numeric default value (otherwise
startup fails with `SFE-1997`).

#### Read schematic (no simulation)

```bash
# Flat read — output is byte-identical to the agent's first-round prompt
python scripts/read_schematic.py --lib pll --cell LC_VCO

# Hierarchical read (expand same-library sub-cells); auto = walk to leaf
python scripts/read_schematic.py --lib pll --cell LC_VCO \
    --depth auto --format both --output ./out/lc_vco
```

`--depth N` controls the recursion level (`1` is equivalent to flat;
`auto` = hard cap 50, BFS stops once the same library is exhausted).
Cross-library masters are always treated as leaves and are not expanded.
PDK names are replaced by generic aliases (`NMOS_SVT` / `PMOS` / `MIM_CAP` …)
by the remote-side `pdk_map_private.il` before returning.

### Writing a Spec

`config/LC_VCO_spec.md` (20 GHz) and `config/LC_VCO_40G_spec.md` (40 GHz)
are complete templates. Every Spec must contain:

- **§1 Design under test**: lib / cell / tb-cell / VDD / target frequency
- **§2 Machine-readable eval block**: three YAML sections — `signals:` /
  `windows:` / `metrics:` — consumed directly by `spec_evaluator.py`.
  Supports statistics like `freq_Hz` / `ptp` / `rms` / `duty_pct`, plus
  compound metrics like `ratio` / `t_cross_frac`
- **§3 Design variables**: parameters the LLM can tune, plus range and priority
- **§4 Startup convergence aids** (optional): used with `--auto-bias-ic`,
  reads bias from the previous round's `spectre.fc` and writes back `ic`
  statements — useful for VCOs, latches, and other unstable-equilibrium
  circuits

Detailed grammar is in `docs/spec_authoring_rules.md` and
`docs/llm_protocol.md`.

### PDK Data Isolation

This repo is public. Real PDK names (`tsmc*`, `nch_*`, `pch_*`, `cfmom`,
`rppoly`, `tcbn`, etc.) must never appear in PC-side source or logs. The
implementation uses two layers of defense:

1. **Remote side**: `skill/helpers.il` uses `~/.virtuoso/pdk_map_private.il`
   (a private mapping table that is never checked in) to rename real cell
   names to generic aliases before returning.
2. **PC side**: `_scrub()` / `_sanitize*()` regexes in `src/safe_bridge.py`
   serve as a safety net in case the remote host leaks anything.

`scripts/check_p0_gate.ps1` is the pre-commit self-check — it scans `src/`
and `skill/` for banned tokens and is recommended as a pre-commit hook.

Config isolation:

| File | Status |
|------|--------|
| `config/pdk_map.yaml` | Public, generic alias whitelist |
| `config/.env.template` | Public, placeholders only |
| `config/.env` | **gitignored**, contains SSH / API keys |
| `~/.virtuoso/pdk_map_private.il` (remote side) | **never checked in**, real cell mapping |

### Development

```bash
pip install -r requirements.txt
pytest tests/ -v                    # full unit tests, no remote host needed
pwsh scripts/check_p0_gate.ps1      # PDK token leakage self-check
```

Unit tests cover every path locally by mocking `VirtuosoClient`. Integration
tests require an SSH-reachable Virtuoso host; run `scripts/test_connection.py`
for a smoke test.

### Credits

- [Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite) —
  the PC ↔ remote-host Virtuoso SKILL communication layer. This project's
  `requirements.txt` installs it directly from GitHub; without it there is
  no virtuoso-agent.
- Cadence Virtuoso / OCEAN / Spectre — vendor tools, not distributed with
  this repo.

### License

MIT.

---

## 中文

LLM 驱动的模拟电路闭环优化 Agent。Agent 读取 Spec（Markdown 规格书），
通过 SKILL / OCEAN 修改 Cadence Virtuoso Maestro 中的 design variables，
执行瞬态仿真，从 PSF 读取波形并评估 pass/fail，再反馈给 LLM 迭代，
直到满足 Spec 或达到最大迭代数。

本项目依赖 [Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite)
提供的 PC ↔ remote host SKILL IPC 通道。virtuoso-agent 在其之上叠加
PDK 脱敏层、OCEAN 子进程隔离、Spec 评估器和 LLM 闭环控制。

### 架构

```
  PC (Windows / Linux)                         remote host (Linux + Virtuoso IC23.1)
  ┌──────────────────────────┐   SSH tunnel   ┌───────────────────────────────┐
  │ virtuoso-agent           │ ◀───────────▶ │ virtuoso-bridge-lite          │
  │   CircuitAgent           │                │   ramic_bridge.il (IPC)       │
  │   SafeBridge  (scrub)    │                │   safe_*.il   (PDK scrub)     │
  │   OceanWorker (sandbox)  │                │   Maestro / Spectre / OCEAN   │
  │   LLM client             │                │                               │
  └──────────────────────────┘                └───────────────────────────────┘
```

主要模块：

| 路径 | 作用 |
|------|------|
| `src/agent.py` | CircuitAgent 主循环 |
| `src/safe_bridge.py` | PC 端 PDK 脱敏 + 参数白名单 + 分层 schematic 读取 |
| `src/ocean_worker.py` | 一次性 OCEAN 子进程跑 PSF dump，超时直接 kill |
| `src/spec_evaluator.py` / `src/spec_validator.py` | 根据 Spec 中的 `metrics:` 计算 pass/fail |
| `src/llm_client.py` | Claude / Gemini / Kimi / MiniMax / Ollama 统一接口 |
| `src/plan_auto.py` | 振荡器类电路的 bias IC 自动回写 |
| `skill/helpers.il`, `skill/safe_*.il` | remote 端安全 SKILL 入口层 |

### 安装

#### remote 端（跑 Virtuoso 的 Linux 主机）

参考 virtuoso-bridge-lite 的
[AGENTS.md](https://github.com/Arcadia-1/virtuoso-bridge-lite/blob/main/AGENTS.md)
启动 bridge daemon：

```bash
module load cadence/ic_23.1
pip3 install --user virtuoso-bridge-lite
virtuoso-bridge start
```

然后用 `scripts/sync_to_remote.sh` 把本仓库的 `skill/` 推到 remote host，
运行时通过 `--remote-skill-dir` 指过去。

#### PC 端

```bash
git clone https://github.com/lixunqi12/virtuoso-agent.git
cd virtuoso-agent
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt # 会从 GitHub 拉 virtuoso-bridge-lite
cp config/.env.template config/.env
```

编辑 `config/.env`：

```
VB_REMOTE_HOST=your-host.example.edu
VB_REMOTE_USER=your_username
VB_REMOTE_PORT=65081
VB_LOCAL_PORT=65082

# 任选其一
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
KIMI_API_KEY=
MINIMAX_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434

DEFAULT_LLM=claude
```

### 使用

#### 跑一轮闭环优化

```bash
python scripts/run_agent.py \
    --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \
    --spec config/LC_VCO_spec.md \
    --llm claude \
    --remote-skill-dir /project/<user>/tool/virtuoso-agent/skill \
    --max-iter 20 --auto-bias-ic
```

前置条件：Maestro 中已有 `LC_VCO_tb` session，且 Spec §3 列出的
design variables 均有数值默认值（否则启动时 `SFE-1997`）。

#### 读取 schematic（无仿真）

```bash
# 单层读取，输出与 agent 首轮 prompt 字节一致
python scripts/read_schematic.py --lib pll --cell LC_VCO

# 分层读取（展开同库子单元），auto 表示走到最底层
python scripts/read_schematic.py --lib pll --cell LC_VCO \
    --depth auto --format both --output ./out/lc_vco
```

`--depth N` 控制递归层数（`1` 等同扁平读，`auto` = 硬上限 50，BFS
在同库内走完即停）。跨库 master 始终作为 leaf 不展开，PDK 名称由
remote 侧 `pdk_map_private.il` 替换成 generic 别名（`NMOS_SVT` / `PMOS` /
`MIM_CAP` 等）后才返回。

### 编写 Spec

`config/LC_VCO_spec.md`（20 GHz）和 `config/LC_VCO_40G_spec.md`（40 GHz）
是可参考的完整模板。每份 Spec 必须包含：

- **§1 Design under test**：lib / cell / tb-cell / VDD / 目标频率
- **§2 Machine-readable eval block**：`signals:` / `windows:` /
  `metrics:` 三段 YAML，被 `spec_evaluator.py` 直接消费。支持
  `freq_Hz` / `ptp` / `rms` / `duty_pct` 等统计量，以及 `ratio` /
  `t_cross_frac` 等 compound metric
- **§3 Design variables**：LLM 可调的参数、范围、优先级
- **§4 Startup convergence aids**（可选）：配 `--auto-bias-ic` 使用，
  从上一轮 `spectre.fc` 读 bias 回写 `ic` 语句，适用于 VCO / latch
  等不稳定平衡点电路

详细语法见 `docs/spec_authoring_rules.md` 和 `docs/llm_protocol.md`。

### PDK 数据隔离

本仓库公开，真实 PDK 名称（`tsmc*`, `nch_*`, `pch_*`, `cfmom`,
`rppoly`, `tcbn` 等）绝不能出现在 PC 端源码或日志。实现为双层防线：

1. **remote 端**：`skill/helpers.il` 通过 `~/.virtuoso/pdk_map_private.il`
   （不入库的私有映射表）把真实 cell 名转成 generic 别名后才返回。
2. **PC 端**：`src/safe_bridge.py` 的 `_scrub()` / `_sanitize*()`
   正则兜底，防止 remote host 漏网。

`scripts/check_p0_gate.ps1` 是 commit 前的自检工具，扫 `src/` 和
`skill/` 是否有 banned token 泄漏，建议接入 pre-commit hook。

配置隔离：

| 文件 | 状态 |
|------|------|
| `config/pdk_map.yaml` | 公开，generic 别名白名单 |
| `config/.env.template` | 公开，仅占位符 |
| `config/.env` | **gitignore**，含 SSH / API key |
| `~/.virtuoso/pdk_map_private.il`（remote 端） | **永不入库**，真实 cell 映射表 |

### 开发

```bash
pip install -r requirements.txt
pytest tests/ -v                    # 全部单测，不打 remote host
pwsh scripts/check_p0_gate.ps1      # PDK token 泄漏自检
```

单测通过 mock `VirtuosoClient` 本地跑完全部路径。集成测需要一台能
SSH 到的 Virtuoso 主机，跑 `scripts/test_connection.py` 做 smoke。

### Credits

- [Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite) ——
  PC ↔ remote host Virtuoso SKILL 通信层，本项目 `requirements.txt` 直接
  从其 GitHub 安装，没有这个项目就没有 virtuoso-agent。
- Cadence Virtuoso / OCEAN / Spectre —— 厂商工具，不随仓库分发。

### License

MIT。
