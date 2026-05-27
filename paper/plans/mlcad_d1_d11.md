# MLCAD 2026 — D1-D11 Execution Plan (post-pivot, FINAL)

> **Status (2026-05-12 evening):** locked by Claude Code (3 rulings) +
> paper_writer outline_v3. Single test-bed = LC_VCO_20G; lc_vco_40g not
> pursued; cobi_matching fully dropped (not even cameo). User may hand-
> build an OpAmp later — that does NOT block D1-D11. D1 starts immediately.

**Timeline anchors**
- 2026-05-12 (today) = D1
- 2026-05-16 = D5 → **abstract registration deadline (hard)**
- 2026-05-22 = D11
- 2026-05-23 = **full-paper submission deadline (hard)** — no slip room

**Token budget**: unlimited per leader.
**Auto-run policy**: NO. All run commands paste to main channel first.
**Python interpreter pinned**: `.venv/Scripts/python.exe` (3.12.x).
**Bridge-only constraint**: no SSH grep, no kinit-hacks, all PDK reads via
`safe_bridge.read_circuit`.

---

## Selling-point recap (from outline_v3)

| SP | Headline                                                       |
|----|----------------------------------------------------------------|
| 1  | Safe-Bridge (enabling substrate, demoted from headline)        |
| 2  | **HEADLINE — Multi-LLM benchmark on industrial-PDK** (8+1 ckpts)|
| 3  | Cost-quality Pareto (per-iter tokens incl. reasoning premium)  |
| 4  | Failure taxonomy + China-domestic LLM viability                |

---

## Checkpoint list (locked in outline_v3 §Checkpoint)

| # | Vendor    | Country | Checkpoint   | Client status (D1)              |
|---|-----------|---------|--------------|---------------------------------|
| 1 | Anthropic | US      | Opus 4.7     | ✅ existing (ClaudeClient + `--model claude-opus-4-7`) |
| 2 | Anthropic | US      | Sonnet 4.6   | ✅ existing (default of ClaudeClient) |
| 3 | Anthropic | US      | Haiku 4.5    | ✅ existing (`--model claude-haiku-4-5-20251001`) |
| 4 | OpenAI    | US      | GPT-5.5      | ❌ **need new client (D1-D2)**  |
| 5 | OpenAI    | US      | GPT-5.4-mini | ❌ same client as #4, `--model` differs |
| 6 | Moonshot  | China   | Kimi K2.5    | ✅ existing (default of KimiClient) |
| 7 | MiniMax   | China   | M2.7         | ✅ existing (default of MinimaxClient) |
| 8 | Xiaomi    | China   | MiMo 2.5     | ❌ **need new client (D2-D3)**  |
| 9 | Google    | US      | Gemini 2.x   | ✅ existing — *user optional 9th* |

---

## Workload (locked)

**Single test bed**: `lc_vco_base` (16nm LC VCO @ 20 GHz, Spectre/OCEAN).
- Spec: `projects/lc_vco_base/constraints/spec.md`
- DOF: 8 vars (Ibias, nfin_neg, nfin_cc, nfin_mirror, nfin_tail, R, C, L)
- ~30 s/iter local Spectre; 9 LLM × 10 iter × 3 seed = 270 runs ≈ 2.5 h
  pure Spectre CPU + API serial latency (bottleneck is LLM RTT, not Spectre).
- 3 historical baselines in `paper/data/extracted_logs.csv` already (cheap
  sanity check for new infra).

**Dropped (Claude Code ruling, 2026-05-12):**
- `lc_vco_40g`: not pursued. spec exists on disk but Cadence-schematic
  instantiation unverifiable from runner side; lift cost too high vs
  benchmark gain. §4 honestly states single-circuit scope.
- `cobi_matching`: **fully dropped** — not even cameo in §5. 22 min/iter
  HSpice on COBI is incompatible with the benchmark cadence, and the
  "industrial-PDK non-amp" framing would muddy the SP2 headline.
- `cobi_delay`: 28nm digital delay-line, irrelevant to amp-sizing story.

**User's OpAmp (parallel track, NOT blocking)**: user is hand-building
an OpAmp in Cadence. If schematic + testbench land before D9 freeze, we
add it as a second circuit and re-fire reduced sweep (1-2 seeds, all 9
LLMs). If not, single-circuit scope stays — D1-D11 does NOT wait.

**Failure-mode probe (Case A, preserved)**: `amp_hold_ratio` spec-parse-
error variant. **Reframed for §6**: same Case A spec run across all 8+1
LLMs (×1 seed × 5 iter ≈ 30 min API-bound) to populate the §6 bucket-×-
LLM matrix (Table 4) with measured data, not hand-classification. Case
B (cobi_matching tightened pass-band) **dropped permanently**.

---

## D1 → D11 deliverables

### D1 — 2026-05-12 (today, partial-day) — **STARTED**

Locks received from Claude Code (2026-05-12 evening):
- 3 rulings applied above (single test bed; lc_vco_40g out; cobi_matching out)
- `config/.env` already has OPENAI_* + MIMO_* slots (user fills keys)

**Runner actions (running now):**
1. Read OpenAI Python SDK docs in `.venv` — verify GPT-5.5 / GPT-5.4-mini
   model IDs match SDK version (check `pip show openai` against the
   advertised models).
2. Implement `OpenAIClient` in `src/llm_client.py`:
   - Mirror `KimiClient` structure (OpenAI-compat).
   - `base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")`
   - `api_key = os.environ["OPENAI_API_KEY"]`
   - default `model="gpt-5.5"` (verifiable at D1; if SDK uses different
     canonical ID we pin to that).
   - **MUST inherit reasoning_content scrub plumbing** (line-by-line copy
     of L320-340 from KimiClient — GPT-5 family is reasoning-class, must
     not bypass scrub per e750189c P0 lesson).
   - Extend `_normalize_usage()` L42: merge `provider == "openai"` into
     the existing `("kimi", "minimax")` branch (same usage shape).
   - Add `"openai": OpenAIClient` to factory dict L515.
3. Unit test: assert `create_llm_client("openai", model="gpt-5.5")` returns
   an `OpenAIClient` with `last_usage` populated after a no-op `ask()`.
   (Cannot run live without API key; mock via `responses` library.)
4. **DO NOT** push code or commit — leave staged. codex_reviewer_v2 gets
   the diff at D3.

**Stop sign**: if user D2 ruling lands and rules outline_v2 (NDA headline)
instead of v3 (multi-LLM headline), this whole D1-D11 plan needs rework —
SP2 stops being the headline, and we'd revert to the failure-probe path.
runner halts and waits for new directive in that case.

### D2 — 2026-05-13

**SP3 smoke acceptance criteria (locked at D1 close, per claude_reviewer_v2 refinement)**:
- **gpt-5.5** (checkpoint #4, full reasoning): first response's
  `last_usage["reasoning_tokens"]` MUST be a positive integer. If `None`,
  SP3 §5 reasoning-premium for OpenAI is unreliable → escalate.
- **gpt-5.4-mini** (checkpoint #5, may be non-reasoning variant):
  `reasoning_tokens` either positive int OR `None` is acceptable. Log
  which it is; do NOT fail the smoke on `None` (would be false positive).
- **OpenAI `max_completion_tokens=16384`** vs Kimi/MiniMax `max_tokens=16384`:
  intentional asymmetry per codex_reviewer_v2 D1 P1; OpenAI API rejects
  `max_tokens` for reasoning models. If smoke shows API still complains
  (e.g. unknown field), flip back + escalate; if it works clean, lock.

1. Read Xiaomi MiMo 2.5 official endpoint docs (via WebFetch — needs
   internet; if Xiaomi docs are gated, escalate to user for direct
   info-share). Verify:
   - OpenAI-compatible chat-completions endpoint? (expected yes)
   - `reasoning_content` field name / `thinking` field name?
   - Rate-limit behavior + 429 semantics.
   - Public pricing as of 2026-05-13 (for §5 USD conversion).
2. Implement `XiaomiClient`:
   - If OpenAI-compat: clone KimiClient (90% reuse).
   - If native HTTP: clone OllamaClient template (urllib + json).
   - Default `model = os.environ.get("XIAOMI_MODEL", "mimo-2.5")` (pin to
     official ID after D2 doc read).
   - Reasoning scrub: **mandatory** (same e750189c rationale).
   - `_normalize_usage()` branch for `xiaomi`.
   - Factory dict entry `"xiaomi": XiaomiClient`.
3. Smoke-test OpenAIClient end-to-end: 1 LLM × LC_VCO_20G × 2 iter (paste
   command to main channel; user runs). Goal: confirm token counts land
   in transcript `usage` block and `extract_transcript_logs.py` picks them
   up (the historical 47-row CSV has all blank token fields because pre-
   pivot transcripts pre-dated `last_usage` writes; new runs must not have
   that gap).

### D3 — 2026-05-14

1. Both new clients (OpenAI + Xiaomi) → codex_reviewer_v2 for review.
   Specifically ask reviewer to verify:
   - reasoning_content scrub identical to Kimi/MiniMax pattern
     (no path that bypasses `scrub()`)
   - `_normalize_usage()` does not silently drop reasoning_tokens
   - rate-limit outer retry loop replicates `_LLM_MAX_RATE_LIMIT_RETRIES`
     semantics
2. Address review comments same-day; second pass to claude_reviewer_v2 for
   final OK (dual-review per project convention).
3. Claude Code / user verifies lc_vco_40g schematic instantiation in
   Cadence (runner cannot do this). Output: green/red.
4. Pilot run #2: 2 LLM (1 existing Kimi + 1 new OpenAI) × LC_VCO_20G ×
   3 iter; confirm Pareto data shape end-to-end.

### D4 — 2026-05-15

1. Draft §5 figure spec (Pareto scatter axes, log-USD vs success-rate)
   and Table 3 column list — submit to paper_writer for sign-off so
   D6-D8 runs collect the right data the first time.
2. If lc_vco_40g greenlit at D3 → pilot run on 40g (1 LLM × 3 iter); else
   skip and double-down on 20G repetitions.
3. Lock abstract text with paper_writer (D5 is registration day).

### D5 — 2026-05-16 — **ABSTRACT REGISTRATION (hard deadline EOD)**

1. User submits abstract reg. Runner stands by for any title/abstract
   late-changes.
2. Runner action: **launch Anthropic-tier benchmark runs** (Opus 4.7 /
   Sonnet 4.6 / Haiku 4.5 × LC_VCO_20G × 10 iter × 3 seed = 90 runs ≈
   45 min API-bound). Paste commands to main channel for user/Claude Code
   to fire.
3. Stream transcripts into `paper/data/extracted_logs.csv` via T3 script.

### D6 — 2026-05-17

1. **OpenAI-tier runs**: GPT-5.5 / GPT-5.4-mini × LC_VCO_20G × 10 iter ×
   3 seed = 60 runs.
2. If Gemini greenlit → Gemini 2.x × LC_VCO_20G × 10 iter × 3 seed = 30
   runs.
3. Refresh `extracted_logs.csv`; paper_writer drafts §5 figure 1st pass
   from running data.

### D7 — 2026-05-18

1. **China-tier runs**: Kimi K2.5 / MiniMax M2.7 / Xiaomi MiMo 2.5 ×
   LC_VCO_20G × 10 iter × 3 seed = 90 runs.
2. Watch for reasoning_content-replay regressions on the new MiMo client
   (D3 review caught any path, but live data is the only true witness);
   abort and patch if scrub() bypass is observed.
3. EOD: full Pareto data set in CSV.

### D8 — 2026-05-19 — slack reframed

1. **Primary D8 work**: Case A failure-mode probe across all 8+1 LLMs ×
   LC_VCO Case A spec × 1 seed × 5 iter = 45 runs ≈ 30 min API-bound.
   Output: §6 Table 4 (bucket × LLM) from measured data, not heuristics.
2. **Secondary D8 work**: re-run any LLM that hit rate-limit cascade /
   timeout in D5-D7; back-fill missing seeds for any LLM whose median
   IQR is suspect.
3. **User OpAmp opportunistic slot**: if user's hand-built OpAmp lands
   today, fire a reduced sweep (9 LLM × 1 seed × 10 iter ≈ 1 h API-bound)
   on it; data then feeds §5 as a "2nd circuit" robustness panel. If no
   OpAmp by D8 EOD → stick with single-circuit story; do not block.
4. **No cobi_matching work, period.** Dropped per Claude Code ruling.

### D9 — 2026-05-20

1. Final `extracted_logs.csv` freeze; paper_writer locks Pareto figure
   and per-LLM table.
2. §6 failure-bucket × LLM matrix filled from Case A cross-LLM run +
   historical bucket inventory.
3. Runner writes §5 prose ~½ page (operating-point recommendations).

### D10 — 2026-05-21

1. **Full-paper dual review**: claude_reviewer_v2 + codex_reviewer_v2.
   Targets: §3 prose (Safe-Bridge demoted-but-still-needed framing), §4
   threat-to-validity paragraph, §5 Pareto + per-LLM table, §6 taxonomy.
2. Address P0/P1 review comments same-day.

### D11 — 2026-05-22

1. **User审** (final). Runner stands by for last-minute tweaks.
2. PDF build via acmart sigconf template; page-count sanity check
   (target 5.5p body + buffer per outline_v3 §Page budget).
3. Pre-submit checklist: anonymization for double-blind; references
   complete; appendix length OK; reproducibility statement.

### 2026-05-23 (post-D11) — **SUBMISSION**

User submits.

---

## Cost / risk register

| Risk                                                | Probability | Mitigation                                                            |
|-----------------------------------------------------|-------------|-----------------------------------------------------------------------|
| MiMo 2.5 endpoint is gated / no public docs         | medium      | Escalate to user D2 morning; fallback = drop to 8 ckpts + Gemini-9th  |
| Rate-limit cascade during D5-D7 runs                | medium      | Outer retry policy already in place; D8 absorbs re-runs                |
| GPT-5.5 reasoning-token bill higher than budgeted   | low (token unlimited per leader) | Tracked; reported in §5 reasoning-premium subsection      |
| User's OpAmp doesn't land by D8                     | medium      | Single-circuit scope retained; §4 honestly states it                  |
| Pareto figure thin (few non-pass LLMs)              | low         | Switch to "iter-to-converge under abort criterion" as fallback y-axis |
| codex_reviewer_v2 finds scrub bypass in new clients | low         | Patch + revert; D2-D3 has 1 D rework budget                            |

---

## Cross-links

- `paper/outline_v3.md` — paper structure (this plan implements it)
- `paper/failures/case_A.md` — Case A failure probe (now §6 input)
- `paper/data/extracted_logs.csv` — CSV growth target through D9
- `src/llm_client.py` — D1-D2 edit target
- `projects/lc_vco_base/HOW_TO_RUN.md` — D5-D8 run reference

---

## What's NOT in this plan

- Re-running historical transcripts to back-fill missing token columns
  (impossible: original SDK calls didn't store `usage`; data is
  unrecoverable per T3 extraction notes).
- `lc_vco_40g` benchmark (Claude Code ruling 2026-05-12: not pursued).
- `cobi_matching` benchmark or cameo (Claude Code ruling 2026-05-12: out).
- Auto-launching runs end-to-end. Every run command is pasted to the
  main channel and the user / Claude Code fires it.
- `git commit` of any kind during D1-D11. Work-tree stays staged.
