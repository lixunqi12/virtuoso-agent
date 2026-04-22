# virtuoso-agent project instructions

This repo is a solo research project. Treat the PC-side working tree
(this directory) as the source of truth, GitHub as the canonical
remote, and remote host (university Linux server behind a firewall that
blocks outbound github.com) as a deploy target.

## Sync workflow — the ONE rule to remember

When the user says any of:
- "sync virtuoso-agent to GitHub"
- "同步 virtuoso-agent 到 GitHub"
- "把 virtuoso-agent 推到 GitHub"
- "sync to remote host" / "同步到 remote host"
- "deploy"
- generally: wants to ship current state outward

**run `bash scripts/sync_to_remote.sh --push`** from the repo root.

That script does, in one shot:
1. `git push` to GitHub (because `--push` was given)
2. `git bundle create` of the full history
3. `scp` the bundle to remote host
4. remote `git fetch` + `git reset --hard` on remote host

Without `--push`: only PC → remote host. Plain `git push` if they want only
GitHub. `sync_to_remote.sh --push` is the default "ship everything"
command.

The SSH target, remote repo path, and bundle location are read from
`config/.sync.local` (gitignored, template at `config/.sync.local.template`).
Never put real SSH host / username / remote path in any tracked file.

## Secrets posture

This is a PUBLIC repo. Before committing anything, keep out:
- real foundry / PDK cell names (`tsmc`, `nch_`, `pch_`, `cfmom`,
  `rppoly`, `rm1_`, `tcbn` and variants) — checked by
  `scripts/check_p0_gate.ps1`
- real SSH hosts / usernames (`.sync.local`, `.env`, `remote_deployment/`
  are gitignored for this reason)
- personal absolute paths (`C:\Users\<name>`, `/home/<realname>`, etc.)
- commit author email: repo is configured with a GitHub noreply
  address; don't reset `user.email` to the real gmail

If a sensitive value slips into a commit, `--amend + force-push` only
hides it from the branch; GitHub still retains the dangling object for
~90 days. For certain removal, delete and recreate the GitHub repo.

## Architecture pointers (for code questions)

- `src/safe_bridge.py` — PC-side PDK token scrubber, parameter
  whitelist. `_FOUNDRY_LEAK_RE` is the authoritative banned-pattern
  list.
- `src/agent.py` — CircuitAgent closed-loop main driver.
- `src/llm_client.py` — Claude / Gemini / Kimi / MiniMax / Ollama
  backends via a common `LLMClient` ABC.
- `src/ocean_worker.py` — hang-safe PSF dump via throwaway OCEAN
  subprocess (remote host's main Virtuoso session can't be preempted by SKILL
  callers, so we work around that with a fresh process per dump).
- `skill/safe_*.il` — remote-side SKILL entry points; safe_* names are
  the whitelist, others refuse to execute.
- `config/pdk_map.yaml` — PUBLIC generic aliases only (`NMOS`,
  `PMOS`, ...). The real cell → alias map lives ONLY on remote host at
  `~/.virtuoso/pdk_map_private.il`.

## Upstream

This project depends on
[Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite)
for the PC ↔ remote host SKILL IPC channel. It's installed from git in
`requirements.txt`. Don't assume we own that code — bugs there go to
their issue tracker.
