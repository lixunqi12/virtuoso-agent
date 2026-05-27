# virtuoso-agent Project Guide

This is the canonical guide for all maintainers and AI agents working in this
repository. Tool-specific entry points such as `CLAUDE.md` and
`.claude/skills/*` are compatibility mirrors only. Put durable project rules
here or in `docs/`.

## Repository Role

This repo is a research prototype for closed-loop analog and mixed-signal
optimization. Treat the PC-side working tree as the source of truth, GitHub as
the canonical remote, and the university Linux host as a deploy target for
Virtuoso, Spectre, and HSpice runs.

The remote host is not the authoritative source repo. Do not recover or rewrite
local project state from remote deployment copies unless the user explicitly
asks for that.

## Local Runtime

- Use the repository virtual environment for local Python work:
  `.\.venv\Scripts\python.exe` on Windows.
- Do not use the system Python 3.14 runtime for agent runs or tests unless a
  task explicitly asks for runtime investigation.
- Prefer focused tests for touched behavior before broad suites.
- Assume the worktree may contain user changes. Read before editing, and do not
  revert unrelated changes.

## Sync And Deploy

When the user says any of:

- `sync virtuoso-agent to GitHub`
- `同步 virtuoso-agent 到 GitHub`
- `把 virtuoso-agent 推到 GitHub`
- `sync to remote host` / `同步到 remote host`
- `deploy`
- generally: ship the current state outward

run this from the repo root:

```bash
bash scripts/sync_to_remote.sh --push
```

That script performs the normal full outward sync:

1. `git push` to GitHub because `--push` was given.
2. `git bundle create` of the full history.
3. `scp` of the bundle to the remote host.
4. Remote `git fetch` plus `git reset --hard` on the deploy checkout.

Without `--push`, the script only syncs PC to remote host. Use plain `git push`
only when the user wants GitHub and not the remote host.

The SSH target, remote repo path, and bundle location are read from
`config/.sync.local`, which is gitignored. The public template is
`config/.sync.local.template`. Never put the real SSH host, username, or remote
path in tracked files.

## Public Repo Safety

This is a public repository. Before committing or publishing, keep out:

- Real foundry or PDK cell names. The exact deny list belongs in
  `scripts/check_p0_gate.ps1`; do not duplicate raw blocked tokens in docs.
- Real SSH hosts, usernames, and remote paths.
- Local secret files such as `.sync.local`, `.env`, and deployment scratch
  directories.
- Personal absolute paths such as `C:\Users\<name>` or `/home/<realname>`.
- Real personal email addresses in Git author config or docs.

Run the P0 gate before committing:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_p0_gate.ps1
```

`src/safe_bridge.py` owns the PC-side PDK scrubber and its
`_FOUNDRY_LEAK_RE` deny list. `config/pdk_map.yaml` must contain only public
generic aliases such as `NMOS` and `PMOS`; the real cell-to-alias map belongs
only on the remote host at `~/.virtuoso/pdk_map_private.il`.

If a sensitive value gets committed, amend plus force-push only removes it from
the branch tip. GitHub may retain dangling objects for roughly 90 days. Certain
cleanup requires deleting and recreating the public repo.

## Architecture Pointers

- `src/agent.py` - `CircuitAgent` closed-loop driver shared by backends.
- `src/safe_bridge.py` - PC-side PDK token scrubber, whitelist, and safe
  Virtuoso bridge calls.
- `src/ocean_worker.py` - hang-safe PSF dump through a throwaway OCEAN process.
- `src/hspice_worker.py` - remote SSH-driven HSpice execution.
- `src/hspice_scrub.py` - HSpice `.sp`, `.mt0`, and `.lis` scrubbing.
- `src/spec_evaluator.py` and `src/spec_validator.py` - metric evaluation and
  JSON/spec contract validation.
- `src/llm_client.py` - Claude, Gemini, Kimi, MiniMax, OpenAI-compatible, and
  Ollama model adapters behind one interface.
- `skill/safe_*.il` - remote-side safe SKILL entry points. The `safe_*` names
  form the whitelist.
- `docs/spec_authoring_rules.md` - stable spec-writing rules.
- `docs/hspice_backend.md` - HSpice backend workflow.
- `docs/llm_protocol.md` - LLM output contract.

The PC to remote SKILL IPC channel comes from
`Arcadia-1/virtuoso-bridge-lite`, installed from Git in `requirements.txt`.
Do not assume this repository owns that upstream implementation.

## Verification

Use the narrowest test command that covers the change. Common local checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_run_agent_argparse.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_spec_evaluator_swept.py -q
```

For PDK or publish-related changes, run the P0 gate shown above.

Remote Virtuoso, Spectre, and HSpice smoke tests depend on the user's current
remote session and licenses. Do not restart, kill, or recover CIW, daemons, or
remote jobs unless the user explicitly asks for that operation.

## Documentation Rules

Durable workflow knowledge belongs in `docs/` or this file. `HANDOFF.md` is
temporary handoff context only and should not become the canonical workflow
manual.

When adding tool-specific instructions, first put the general rule in
`AGENTS.md` or `docs/`, then add only the minimal compatibility pointer needed
by that tool.
