#
# P0 banned-pattern grep gate (PowerShell).
#
# Exits non-zero if any banned foundry token leaks into the PC-side
# source tree. Scope is the source workspace only — .venv, __pycache__,
# .pytest_cache, .quarantine, and logs/ are out of scope because they
# are either third-party or already-isolated artifacts.
#
# Phase-1 (repo scan) consults config/p0_gate_allowlist.yaml as the
# single source of truth for legitimate carriers. The yaml has two
# required keys:
#   paths         : exact relative paths (forward-slash) allowed to
#                   contain banned tokens
#   path_prefixes : relative directory prefixes (forward-slash, with
#                   trailing slash) — any file whose path begins with
#                   one of these is allowed
# The yaml itself includes scripts/check_p0_gate.ps1 (this script
# defines the banned-pattern regex literal, so it must contain the
# tokens by construction) and src/safe_bridge.py + tests/test_safe_bridge.py
# (authoritative banned-pattern list for _scrub and its synthetic test
# stand-ins). Any other hit fails the gate.
#
# T6 (2026-04-23): additive second phase scans the HSpice work-dir
# artifacts ({HSPICE_WORK_DIR}/**/*.{sp,mt[0-9]*,lis}) against the T1
# scrub YAML (config/hspice_scrub_patterns.private.yaml, gitignored;
# falls back to .template.yaml) as the single source of truth for
# banned_prefixes / banned_tokens / preserve_tokens.
# Matches .mt0 through .mt<N>, so multi-digit alters are covered.
# Hit logs follow T1's privacy posture: path + category + count only;
# the raw match text never surfaces. Phase skipped when
# HSPICE_WORK_DIR is unset or points at a non-directory — useful for
# CI nodes that don't stage artifacts locally.
#
# Usage:  pwsh -NoProfile -File scripts/check_p0_gate.ps1
#         (or powershell.exe -NoProfile -File ... on Windows PS 5.1)
#

$ErrorActionPreference = 'Stop'

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot

$Banned = 'nch_|pch_|cfmom|rppoly|rm1_|tsmc|tcbn'

# Excluded top-level directories (third-party, isolated artifacts, or
# runtime scratch). `tmp` is operator-local scratch — anything that
# matters out of `tmp` should land in a tracked location first.
$ExcludedTop = @(
    '.venv', 'venv',
    '.quarantine',
    'logs',
    '.git',
    '.idea', '.vscode',
    'tmp'
)
# Directory-name components that are excluded at any depth (generated
# build artifacts that may contain stale compiled strings).
$ExcludedAnywhere = @('__pycache__', '.pytest_cache')

# Top-level files that are excluded outright (transient logs / chat
# captures that naturally carry foundry token discussion).
$ExcludedFiles = @('.chat_recent.jsonl')


function Read-AllowlistYaml {
    # Strict parser for config/p0_gate_allowlist.yaml. Mirrors the
    # Read-HspiceScrubYaml posture (T6 R3 fail-closed): unknown top-level
    # keys, inline flow other than [], scalar RHS, orphaned list items,
    # missing required keys, and YAML list-item metasyntax on unquoted
    # values all throw rather than silently relaxing the allowlist.
    # Known top-level keys: paths, path_prefixes. Both required.
    # Privacy posture: errors carry only "line=<n> category=<token>" -
    # the offending key/value is never embedded.
    # Returns a PSCustomObject with Paths, PathPrefixes (each List[string]).
    param([Parameter(Mandatory)][string]$Path)

    $paths    = New-Object System.Collections.Generic.List[string]
    $prefixes = New-Object System.Collections.Generic.List[string]
    $seenKeys = New-Object System.Collections.Generic.HashSet[string]
    $knownKeys = @('paths','path_prefixes')
    $required  = @('paths','path_prefixes')
    $currentKey = $null
    $lineNo = 0

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $lineNo++
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }

        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$') {
            $key = $Matches[1]
            $rhs = $Matches[2]
            if ($knownKeys -notcontains $key) {
                throw "yaml_parse_fail: line=$lineNo category=unknown_top_level_key"
            }
            $null = $seenKeys.Add($key)
            if ($rhs -eq '') {
                $currentKey = $key
            } elseif ($rhs -eq '[]') {
                $currentKey = $null
            } else {
                throw "yaml_parse_fail: line=$lineNo category=inline_flow_or_scalar_not_supported"
            }
            continue
        }

        if ($line -match '^\s*-\s*(.+?)\s*$') {
            if (-not $currentKey) {
                throw "yaml_parse_fail: line=$lineNo category=list_item_without_key"
            }
            $val = $Matches[1]
            if ($val -match '^[*&!|>]') {
                throw "yaml_parse_fail: line=$lineNo category=list_item_metasyntax_not_supported"
            }
            if ($val -match '^"(.*)"$')     { $val = $Matches[1] }
            elseif ($val -match "^'(.*)'$") { $val = $Matches[1] }
            switch ($currentKey) {
                'paths'         { $null = $paths.Add($val) }
                'path_prefixes' { $null = $prefixes.Add($val) }
            }
            continue
        }

        throw "yaml_parse_fail: line=$lineNo category=malformed_line"
    }

    foreach ($req in $required) {
        if (-not $seenKeys.Contains($req)) {
            throw "yaml_parse_fail: line=0 category=missing_required_key"
        }
    }

    return [pscustomobject]@{
        Paths        = $paths
        PathPrefixes = $prefixes
    }
}


# Phase-1 allowlist: fail-closed if missing or malformed. Silent fallback
# would break the gate either direction (every file fails, OR every file
# passes), neither of which is acceptable.
$AllowlistYaml = Join-Path $RepoRoot 'config/p0_gate_allowlist.yaml'
if (-not (Test-Path -LiteralPath $AllowlistYaml -PathType Leaf)) {
    Write-Host "P0 GATE FAIL (repo): config/p0_gate_allowlist.yaml missing - cannot run phase-1 scan." -ForegroundColor Red
    exit 1
}
try {
    $Allowlist = Read-AllowlistYaml -Path $AllowlistYaml
} catch {
    Write-Host ("P0 GATE FAIL (repo): {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}

# T8.9 R2: hard-fail if any path under config/logs/ is tracked by git.
# config/logs/ is gitignored runtime scratch (HSpice transcripts, agent
# logs). The phase-1 allowlist's path_prefixes entry for "config/logs/"
# short-circuits the banned-token scan via a pure prefix match - which
# is the correct posture for ignored local artifacts, but silently
# admits any file that was force-added with `git add -f` past .gitignore.
# Close the loop by forbidding *tracked* paths under config/logs/
# outright; ignored / untracked artifacts are unaffected.
$gitMarker = Join-Path $RepoRoot '.git'
if (Test-Path -LiteralPath $gitMarker) {
    $tracked = & git -C $RepoRoot ls-files -- 'config/logs' 2>$null
    if ($LASTEXITCODE -eq 0 -and $tracked) {
        $trackedList = @($tracked | Where-Object { $_ -and $_.Trim() })
        if ($trackedList.Count -gt 0) {
            Write-Host "P0 GATE FAIL (repo): config/logs/ has $($trackedList.Count) tracked file(s) - gitignored runtime artifacts must NEVER be committed (force-add bypass detected):" -ForegroundColor Red
            foreach ($t in $trackedList) { Write-Host "  $t" -ForegroundColor Red }
            exit 1
        }
    }
}

Write-Host "P0 grep gate scanning $RepoRoot ..."

$files = Get-ChildItem -Path $RepoRoot -Recurse -File -Force |
    Where-Object {
        $rel = $_.FullName.Substring($RepoRoot.Path.Length + 1) -replace '\\', '/'
        $parts = $rel -split '/'
        $top = $parts[0]
        if ($ExcludedTop -contains $top) { return $false }
        if ($parts.Count -eq 1 -and $ExcludedFiles -contains $top) { return $false }
        foreach ($p in $parts) {
            if ($ExcludedAnywhere -contains $p) { return $false }
        }
        return $true
    }

$leaks = @()
foreach ($f in $files) {
    $rel = $f.FullName.Substring($RepoRoot.Path.Length + 1) -replace '\\', '/'
    if ($Allowlist.Paths -contains $rel) { continue }
    $prefixHit = $false
    foreach ($pfx in $Allowlist.PathPrefixes) {
        if ($rel.StartsWith($pfx)) { $prefixHit = $true; break }
    }
    if ($prefixHit) { continue }
    try {
        $hit = Select-String -Path $f.FullName -Pattern $Banned -CaseSensitive:$false -SimpleMatch:$false -List -ErrorAction SilentlyContinue
    } catch {
        continue
    }
    if ($hit) {
        $leaks += $rel
    }
}

$exitCode = 0
if ($leaks.Count -gt 0) {
    Write-Host "P0 GATE FAIL (repo): banned tokens found in $($leaks.Count) file(s):" -ForegroundColor Red
    foreach ($l in $leaks) { Write-Host "  $l" -ForegroundColor Red }
    $exitCode = 1
} else {
    Write-Host "P0 GATE PASS (repo): no banned tokens in PC-side source workspace." -ForegroundColor Green
}


# ------------------------------------------------------------------- #
# T6 — HSpice work-dir artifact scan.
#
# Reads {HSPICE_WORK_DIR}/**/*.{sp,mt[0-9]*,lis} and rejects any file
# carrying a banned prefix / banned token / foundry seed that was not
# granted an exception via preserve_tokens. The YAML at
# config/hspice_scrub_patterns.private.yaml (gitignored; derived from
# the committed .template.yaml) is the single source of truth for
# the configurable patterns; the foundry-seed regex literal reuses
# $Banned above (the one authoritative copy in this script).
#
# Privacy: the log line for a hit is only the relative path + category
# + count. The raw matched substring is never emitted — T1's scrubber
# made the same call in ScrubError.__str__ for exactly this reason
# (matched text often IS the sensitive payload).
# ------------------------------------------------------------------- #


function Read-HspiceScrubYaml {
    <#
    .SYNOPSIS
    Strict parser for config/hspice_scrub_patterns.{private,template}.yaml.

    .DESCRIPTION
    Fail-closed (R2 B1): the pre-rework parser tolerated a number of
    silent-corruption cases — unknown top-level keys, inline flow
    (``key: [a, b]``), scalar RHS, orphaned list items, missing
    required keys. The new posture throws on any deviation from the
    hand-authored schema so a broken config cannot silently disable
    gate coverage.

    Known top-level keys: banned_prefixes, banned_tokens, model_regex,
    preserve_tokens. The first two and last are consumed; model_regex
    is accepted-but-unused (T1's scrubber uses it; the gate does not).

    Privacy: errors are thrown with the shape
    ``yaml_parse_fail: line=<n> category=<token>`` — only the line
    number and a fixed category label. The offending key or value is
    NEVER embedded in the exception, matching T1's ScrubError posture.

    .OUTPUTS
    PSCustomObject with BannedPrefixes, BannedTokens, PreserveTokens.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $bannedPrefixes = New-Object System.Collections.Generic.List[string]
    $bannedTokens   = New-Object System.Collections.Generic.List[string]
    $preserveTokens = New-Object System.Collections.Generic.List[string]
    $seenKeys = New-Object System.Collections.Generic.HashSet[string]
    $knownKeys = @('banned_prefixes','banned_tokens','model_regex','preserve_tokens')
    $required  = @('banned_prefixes','banned_tokens','preserve_tokens')
    $currentKey = $null
    $lineNo = 0

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $lineNo++
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }

        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$') {
            $key = $Matches[1]
            $rhs = $Matches[2]
            if ($knownKeys -notcontains $key) {
                throw "yaml_parse_fail: line=$lineNo category=unknown_top_level_key"
            }
            $null = $seenKeys.Add($key)
            if ($rhs -eq '') {
                $currentKey = $key
            } elseif ($rhs -eq '[]') {
                $currentKey = $null
            } else {
                # Inline flow ``[a, b]``, scalar ``foo``, quoted scalar,
                # folded/literal markers, etc. Our schema only accepts
                # empty inline ``[]`` or block-list form — everything
                # else is fail-closed.
                throw "yaml_parse_fail: line=$lineNo category=inline_flow_or_scalar_not_supported"
            }
            continue
        }

        if ($line -match '^\s*-\s*(.+?)\s*$') {
            if (-not $currentKey) {
                throw "yaml_parse_fail: line=$lineNo category=list_item_without_key"
            }
            $val = $Matches[1]
            # R3 B1 residual: reject YAML list-item metasyntax starting
            # an unquoted value. ``*`` = anchor ref, ``&`` = anchor
            # definition, ``!`` = tag, ``|`` = literal block, ``>`` =
            # folded block. A real YAML parser would process these
            # specially; our mini-parser has no such semantics, so
            # silently treating them as plain strings could let a
            # malicious config smuggle non-literal content into a
            # banned/preserve list. Fail-closed — the author can still
            # escape with surrounding quotes (``- "*lit"``) if they
            # genuinely need a string that starts with one of these
            # characters.
            if ($val -match '^[*&!|>]') {
                throw "yaml_parse_fail: line=$lineNo category=list_item_metasyntax_not_supported"
            }
            if ($val -match '^"(.*)"$')     { $val = $Matches[1] }
            elseif ($val -match "^'(.*)'$") { $val = $Matches[1] }
            switch ($currentKey) {
                'banned_prefixes' { $null = $bannedPrefixes.Add($val) }
                'banned_tokens'   { $null = $bannedTokens.Add($val) }
                'preserve_tokens' { $null = $preserveTokens.Add($val) }
                'model_regex'     { }  # accepted but not consumed
            }
            continue
        }

        throw "yaml_parse_fail: line=$lineNo category=malformed_line"
    }

    foreach ($req in $required) {
        if (-not $seenKeys.Contains($req)) {
            throw "yaml_parse_fail: line=0 category=missing_required_key"
        }
    }

    return [pscustomobject]@{
        BannedPrefixes = $bannedPrefixes
        BannedTokens   = $bannedTokens
        PreserveTokens = $preserveTokens
    }
}


function Get-SafeArtifactFiles {
    <#
    .SYNOPSIS
    Reparse-point-safe recursion over a work-dir for HSpice artifacts.

    .DESCRIPTION
    Rolled by hand (R2 B2) because ``Get-ChildItem -Recurse`` on PS 5.1
    has ambiguous behaviour around junctions/symlinks (varies by host).
    This BFS skips any entry whose Attributes carry ``ReparsePoint`` —
    both directories (preventing traversal escape) and files (so a
    symlinked .sp pointing at a foundry tree doesn't get read either).

    Files are filtered to the same extension regex used by the caller
    (``.sp`` / ``.mt<digits>`` / ``.lis``) so .mt10+ is covered.
    #>
    param([Parameter(Mandatory)][string]$Root)

    $results = New-Object System.Collections.Generic.List[System.IO.FileInfo]
    $queue = New-Object System.Collections.Generic.Queue[string]
    $queue.Enqueue($Root)
    $reparse = [IO.FileAttributes]::ReparsePoint

    while ($queue.Count -gt 0) {
        $dir = $queue.Dequeue()
        try {
            $items = Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop
        } catch {
            continue
        }
        foreach ($it in $items) {
            if ($it.Attributes -band $reparse) { continue }
            if ($it.PSIsContainer) {
                $queue.Enqueue($it.FullName)
            } elseif ($it.Extension -match '^\.(sp|mt\d+|lis)$') {
                $null = $results.Add($it)
            }
        }
    }
    return $results
}


function Invoke-HspiceWorkDirScan {
    <#
    .SYNOPSIS
    Scan {WorkDir}/**/*.{sp,mt<N>,lis} for YAML-banned patterns.

    .DESCRIPTION
    Returns 0 when the work-dir is clean, 1 for:
      - YAML parse failure (R2 B1 fail-closed)
      - WorkDir being a reparse point (R2 B2 symlink escape guard)
      - Any artifact above the ``$MaxBytes`` cap (R2 B4 .lis fail-closed)
      - Any banned-pattern hit that survives preserve_tokens exemption

    The hit log emits one line per (file, category) pair. Raw matched
    text is never materialised: the inner loop uses
    ``Regex.Matches($text).Count`` and never reads match payloads (R2 B3).
    Preserve-token exemption is regex-level (R3 B2): the preserve
    alternation is baked into each banned pattern as a negative
    lookahead at the start, so only the preserve token itself is
    exempted — a foundry leak sharing a line with ``top_tt`` still
    reports. This replaces R2's whole-line exemption, which
    codex flagged as a false-negative hole.

    .PARAMETER FoundrySeedRegex
    The top-of-file $Banned literal, passed in as-is so we don't fork
    its seed list into a second source of truth.
    #>
    param(
        [Parameter(Mandatory)][string]$WorkDir,
        [Parameter(Mandatory)][string]$YamlPath,
        [Parameter(Mandatory)][string]$FoundrySeedRegex
    )

    # R2 B4: cap to keep a 100MB+ .lis from ballooning PS memory or
    # DoS-ing CI. 50MB is comfortably above any realistic transient
    # netlist / mt0 / moderate .lis; anything above is almost certainly
    # a raw foundry dump that the scrubber was supposed to process.
    $MaxBytes = 50MB

    # R2 B2: reject root itself being a reparse point — a junction
    # pointed at the foundry tree would let a symlink at HSPICE_WORK_DIR
    # silently reroute the scan outside the operator's stated work area.
    try {
        $rootItem = Get-Item -LiteralPath $WorkDir -Force -ErrorAction Stop
    } catch {
        Write-Host "P0 GATE FAIL (workdir): cannot stat HSPICE_WORK_DIR." -ForegroundColor Red
        return 1
    }
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Write-Host "P0 GATE FAIL (workdir): HSPICE_WORK_DIR is a reparse point (symlink/junction); refusing to traverse." -ForegroundColor Red
        return 1
    }

    # R2 B1: strict parse — throws on unknown key / inline flow / missing key / malformed line.
    try {
        $cfg = Read-HspiceScrubYaml -Path $YamlPath
    } catch {
        Write-Host ("P0 GATE FAIL (workdir): {0}" -f $_.Exception.Message) -ForegroundColor Red
        return 1
    }

    # R3 B2: preserve exemption moves from whole-line IsMatch to a
    # regex-embedded negative lookahead. ``$preserveLookahead`` is
    # prepended to every banned regex so only the preserve token
    # itself is exempted — a banned hit sharing a line with (e.g.)
    # ``top_tt`` still reports. No preserve tokens → empty prefix.
    $preserveLookahead = ''
    if ($cfg.PreserveTokens.Count -gt 0) {
        $alts = foreach ($t in $cfg.PreserveTokens) { [regex]::Escape($t) }
        $preserveLookahead = '(?!\b(?:' + ($alts -join '|') + ')\b)'
    }

    $patterns = New-Object System.Collections.Generic.List[object]
    $patterns.Add([pscustomobject]@{
        Category = 'foundry_seed'
        Regex    = [regex]::new(
            $preserveLookahead + '\b(?:' + $FoundrySeedRegex + ')\w*',
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }) | Out-Null
    foreach ($p in $cfg.BannedPrefixes) {
        $rx = $preserveLookahead + [regex]::Escape($p) + '[^\s''"<>|*?]*'
        $patterns.Add([pscustomobject]@{
            Category = 'banned_prefix'
            Regex    = [regex]::new(
                $rx,
                [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
            )
        }) | Out-Null
    }
    foreach ($t in $cfg.BannedTokens) {
        $rx = $preserveLookahead + '\b' + [regex]::Escape($t) + '\w*'
        $patterns.Add([pscustomobject]@{
            Category = 'banned_token'
            Regex    = [regex]::new(
                $rx,
                [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
            )
        }) | Out-Null
    }

    # R2 B2: reparse-point-safe enumeration. Get-SafeArtifactFiles
    # filters ReparsePoint on BOTH directories (blocks traversal) and
    # files (blocks symlinked .sp pointing at foundry content).
    $files = Get-SafeArtifactFiles -Root $WorkDir

    $workRoot = (Resolve-Path -LiteralPath $WorkDir).Path
    $violations = 0
    Write-Host "P0 workdir scan: $workRoot ..."
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($workRoot.Length).TrimStart(
            [char]'\', [char]'/'
        ) -replace '\\', '/'

        if ($f.Length -gt $MaxBytes) {
            Write-Host ("  {0} | category=size_cap_exceeded | bytes={1} | cap={2}" -f `
                $rel, $f.Length, $MaxBytes) -ForegroundColor Red
            Write-Host "P0 GATE FAIL (workdir): artifact exceeds $MaxBytes byte cap; refusing to scan (fail-closed)." -ForegroundColor Red
            return 1
        }

        try {
            $text = Get-Content -LiteralPath $f.FullName -Raw `
                        -Encoding UTF8 -ErrorAction Stop
        } catch {
            continue
        }
        if ([string]::IsNullOrEmpty($text)) { continue }

        # R3 B2: preserve is embedded as a negative lookahead inside
        # each banned regex (see pattern construction above), so a
        # single .Matches($text).Count per pattern is enough — no
        # per-line pre-filter, no preserve lookup, no match payload
        # read. Match payloads are never materialised.
        $byCat = @{}
        foreach ($p in $patterns) {
            $c = $p.Regex.Matches($text).Count
            if ($c -gt 0) {
                if (-not $byCat.ContainsKey($p.Category)) {
                    $byCat[$p.Category] = 0
                }
                $byCat[$p.Category] += $c
            }
        }

        if ($byCat.Count -gt 0) {
            foreach ($cat in ($byCat.Keys | Sort-Object)) {
                Write-Host ("  {0} | category={1} | count={2}" -f `
                    $rel, $cat, $byCat[$cat]) -ForegroundColor Red
                $violations++
            }
        }
    }

    if ($violations -gt 0) {
        Write-Host "P0 GATE FAIL (workdir): $violations violation(s) across HSpice artifacts." -ForegroundColor Red
        return 1
    }
    Write-Host "P0 GATE PASS (workdir): no banned patterns in HSpice artifacts." -ForegroundColor Green
    return 0
}


$workDir = $env:HSPICE_WORK_DIR
if ([string]::IsNullOrWhiteSpace($workDir)) {
    Write-Host "HSPICE_WORK_DIR not set - skipping work-dir artifact scan." -ForegroundColor DarkGray
} elseif (-not (Test-Path -LiteralPath $workDir -PathType Container)) {
    Write-Host "HSPICE_WORK_DIR=$workDir not a directory - skipping work-dir artifact scan." -ForegroundColor DarkGray
} else {
    # Prefer the private (gitignored) YAML with real foundry tokens.
    # Fall back to the public template when the private copy is absent
    # (new clone, CI without secrets): banned_{prefixes,tokens} are empty
    # there, so the work-dir scan still runs but only the hardcoded
    # foundry seed regex fires — matching the posture in src/hspice_scrub.py.
    $privateYaml  = Join-Path $RepoRoot 'config/hspice_scrub_patterns.private.yaml'
    $templateYaml = Join-Path $RepoRoot 'config/hspice_scrub_patterns.template.yaml'
    if (Test-Path -LiteralPath $privateYaml -PathType Leaf) {
        $yamlPath = $privateYaml
    } elseif (Test-Path -LiteralPath $templateYaml -PathType Leaf) {
        Write-Host "hspice_scrub_patterns.private.yaml missing - falling back to .template.yaml (seed-only scan)." -ForegroundColor DarkYellow
        $yamlPath = $templateYaml
    } else {
        Write-Host "No hspice_scrub_patterns YAML found (private or template) - cannot run work-dir scan." -ForegroundColor Red
        $exitCode = 1
        $yamlPath = $null
    }
    if ($yamlPath) {
        $workRc = Invoke-HspiceWorkDirScan `
                    -WorkDir $workDir `
                    -YamlPath $yamlPath `
                    -FoundrySeedRegex $Banned
        if ($workRc -ne 0) { $exitCode = 1 }
    }
}

exit $exitCode
