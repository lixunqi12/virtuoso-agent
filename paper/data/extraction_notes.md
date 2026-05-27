# extracted_logs.csv — extraction notes

**Task**: T3 [ee4396a6] — mine historical project transcripts for per-iteration
cost/sim-count dataset feeding MLCAD 2026 paper §cost-analysis.

**Producer**: `paper/scripts/extract_transcript_logs.py`
**Output**: `paper/data/extracted_logs.csv` (47 rows, header included)
**Generated**: 2026-05-12

## Row counts

| project       | transcripts | rows | rows w/ sim follow-up | PASS | FAIL |
|---------------|-------------|------|-----------------------|------|------|
| lc_vco_base   | 1           | 3    | 2                     | 0    | 2    |
| cobi_delay    | 8           | 16   | 8                     | 0    | 8    |
| cobi_matching | 7           | 28   | 12                    | 0    | 12   |
| **total**     | **16**      | **47** | **22**              | **0**| **22** |

The 25 rows with no sim follow-up are the final accepted assistant turn of each
transcript — by construction the loop ended (timeout / iter cap / human stop)
before another HSpice/OCEAN result message was logged, so there is no metrics
block to bind to that turn. We keep these rows because the assistant *did*
produce a design proposal at that iteration; only `sim_count_this_iter` and
`spec_pass_flag` are blank for them.

**Zero PASS rows across all 16 historical runs.** Every run ended in FAIL or
was truncated before any PASS was reached. This is the failure-mode baseline
for the paper. Re-runs with Kimi vs Claude (T-3..4) are the comparator.

## Project scope

In scope (per leader directive):
- `lc_vco_base/` — 1 transcript, OCEAN/Maestro/Spectre backend
- `cobi_delay/` — 8 transcripts, HSpice backend
- `cobi_matching/` — 7 transcripts, HSpice backend

Found but excluded:
- `lc_vco_40g/` — directory exists (`HOW_TO_RUN.md` + `constraints/` only); no
  transcripts have been produced yet.
- `_scratch/` — out of scope by directive; appears to be ad-hoc/throwaway work.

No other `lc_vco_*` projects exist on disk.

## Schema differences between projects

Two transcript dialects coexist:

1. **OCEAN/Maestro/Spectre backend** (lc_vco_base):
   - Results block header: `### Metrics` (three hashes).
   - Lives under `logs/agent/transcript_*.jsonl` next to `run_*.log`.

2. **HSpice backend** (cobi_delay, cobi_matching):
   - Results block has *two* headers in the same user message:
     - `## Iteration N HSpice results` (a per-iter banner), and
     - `## Metrics` (two hashes) listing the parsed metric lines.
   - Lives under `logs/hspice/hspice_transcript_*.jsonl`.

The extractor matches either via the regex `^##+\s*Metrics\s*$` and falls back
to `^## Iteration \d+ HSpice results` when no `Metrics` block is present.

Metric lines themselves share one grammar across backends:
`- <metric_name>: <value?> <PASS|FAIL|UNMEASURABLE>(<detail>)`

The three-valued verdict is collapsed to a boolean for the CSV:
- any FAIL → `spec_pass_flag=FALSE`
- all PASS (and at least one) → `spec_pass_flag=TRUE`
- only UNMEASURABLE, or no metric lines at all → blank with reason recorded

## Repair-retry deduplication

When the assistant emits a structurally invalid response (bad JSON, missing
keys, malformed `design_vars`), virtuoso-agent injects a user message starting
with `Your previous response violated HARD CONSTRAINTS` and the same `iter`
counter is re-used for the retry. A single iteration can therefore have
multiple assistant entries.

Rule used here: **last assistant entry at iter K wins** — that is the response
that was actually accepted and forwarded to the simulator. Earlier rejected
attempts at the same K are discarded from the row set. The number of rejected
retries per iter is not currently emitted as a column (TODO if useful for the
paper).

## Missing fields and why

| field | populated? | reason |
|---|---|---|
| `prompt_tokens` | **all blank** | Transcripts contain `{iteration, role, timestamp, content}` only — no usage stats. The `openai._base_client` logs in `run_*.log` capture the request body via httpx but not the response body's `usage` block. We have no historical token counts. Token data will only become available from this point forward if the agent loop is patched to dump `response.usage` per call. Out of scope for this task (no new experiments). |
| `completion_tokens` | all blank | same |
| `reasoning_tokens` | all blank | same; also Kimi-only field even when available |
| `total_tokens` | all blank | same |
| `llm_model` | 17 / 47 populated | Only 4 `run_*.log` files exist alongside the 16 transcripts. Of those, 2 logs contain the literal `'model': 'MiniMax-M2.7'` substring (the openai client parameter dict at boot). The other 2 only have the API hostname `api.minimaxi.com` in httpx events → recorded as `MiniMax (host=api.minimaxi.com, exact ver unknown)`. The 30 unmatched rows are presumed to be MiniMax based on workspace conventions but are left blank rather than guessed. cobi_delay has zero agent logs at all. |
| `timestamp` | all populated | ISO8601 UTC from the assistant entry. |
| `spec_pass_flag` | 22 / 47 populated | The 25 blanks are final-iteration turns with no subsequent sim message. |
| `fail_reason_if_any` | 22 / 47 populated | `'; '`-joined `<metric>:FAIL(<detail>)` snippets. UNMEASURABLE metrics are reported even when there is no FAIL. |

## Gotchas the regression of this CSV must guard against

- **Encoding**: every transcript file is UTF-8; the default Windows `cp936/gbk`
  decoder fails on the en-dash and emoji characters that appear in OCEAN
  Maestro printouts. The extractor opens with explicit `encoding="utf-8"`.
- **`### Metrics` vs `## Metrics`** is real, not a typo. Earlier draft of the
  extractor only matched the 2-hash variant and silently zeroed lc_vco_base.
  The regex is now `^##+\s*Metrics\s*$`.
- **Final-iter blanks are not a bug.** They count toward `iter_index` (the LLM
  did produce a design) but contribute 0 to `sim_count_cumulative`.
- **PASS counter at zero is expected.** None of the 16 historical runs reached
  spec. The Kimi vs Claude comparator (D-3..4) will be the first PASS data.

## Reproducibility

```
python paper/scripts/extract_transcript_logs.py
```

Walks `projects/{lc_vco_base,cobi_delay,cobi_matching}/logs/**/(transcript|hspice_transcript)_*.jsonl`,
re-derives `run_id` from filename timestamps, re-matches `llm_model` via
`run_*.log` content + ±5s timestamp window. Idempotent overwrite of
`paper/data/extracted_logs.csv`.
