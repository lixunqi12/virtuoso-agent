# Tests for the P0 grep gate's phase-1 yaml-driven allowlist
# (scripts/check_p0_gate.ps1 + config/p0_gate_allowlist.yaml).
#
# No Pester dependency: each test is a try/catch block, with PASS/FAIL
# tracked in a counter. Style matches scripts/check_p0_gate.ps1's own
# Write-Host posture so the output is readable in PS 5.1 and pwsh 7.
#
# All scenarios run against a freshly-built temp repo so the live
# checkout is never touched. The temp repo has just enough scaffolding
# (scripts/check_p0_gate.ps1 copied verbatim + config/p0_gate_allowlist.yaml
# fixture + synthetic source files) for the gate to run end-to-end.

$ErrorActionPreference = 'Stop'

$RealRepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$RealScript   = Join-Path $RealRepoRoot 'scripts/check_p0_gate.ps1'

if (-not (Test-Path -LiteralPath $RealScript -PathType Leaf)) {
    throw "fixture_missing: $RealScript"
}

$pass = 0
$fail = 0
$failNames = New-Object System.Collections.Generic.List[string]


function New-TempRepo {
    <#
    .SYNOPSIS
    Build a fresh temp repo with a copy of check_p0_gate.ps1 and a
    caller-supplied allowlist yaml. Returns the temp repo root path.
    #>
    param(
        [string]$AllowlistContent,
        [bool]$WriteAllowlist = $true
    )
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) (
        'p0_gate_test_' + [System.Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $tmp
    $null = New-Item -ItemType Directory -Path (Join-Path $tmp 'scripts')
    $null = New-Item -ItemType Directory -Path (Join-Path $tmp 'config')
    Copy-Item -LiteralPath $RealScript -Destination (
        Join-Path $tmp 'scripts/check_p0_gate.ps1')
    if ($WriteAllowlist) {
        # WriteAllText (no trailing newline injection, no BOM coercion).
        [System.IO.File]::WriteAllText(
            (Join-Path $tmp 'config/p0_gate_allowlist.yaml'),
            $AllowlistContent,
            (New-Object System.Text.UTF8Encoding $false))
    }
    return $tmp
}

function Invoke-GateScript {
    <#
    .SYNOPSIS
    Run the temp-repo's check_p0_gate.ps1 in a clean child PowerShell
    process and capture stdout / stderr / exit code.
    #>
    param([Parameter(Mandatory)][string]$RepoRoot)
    $script = Join-Path $RepoRoot 'scripts/check_p0_gate.ps1'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'powershell.exe'
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    $stdout = $p.StandardOutput.ReadToEnd()
    $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return [pscustomobject]@{
        StdOut   = $stdout
        StdErr   = $stderr
        ExitCode = $p.ExitCode
    }
}

function Add-LeakFile {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$RelPath,
        [string]$Token = 'nch_lvt'
    )
    $abs = Join-Path $RepoRoot $RelPath
    $dir = Split-Path -Parent $abs
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $dir -Force
    }
    [System.IO.File]::WriteAllText(
        $abs, "synthetic content carrying $Token in body`n",
        (New-Object System.Text.UTF8Encoding $false))
}

function Test-Case {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body
    )
    try {
        & $Body
        Write-Host ("PASS  {0}" -f $Name) -ForegroundColor Green
        $script:pass++
    } catch {
        Write-Host ("FAIL  {0}" -f $Name) -ForegroundColor Red
        Write-Host ("        {0}" -f $_.Exception.Message) -ForegroundColor Red
        $script:fail++
        $script:failNames.Add($Name)
    }
}

function Assert-True {
    param([bool]$Cond, [string]$Msg)
    if (-not $Cond) { throw $Msg }
}

# ---------------------------------------------------------------------- #
# (a) paths exact match
# ---------------------------------------------------------------------- #
Test-Case 'paths-exact-match: leak in non-allowlisted file fails' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes: []
"@
    Add-LeakFile -RepoRoot $tmp -RelPath 'src/foo.py' -Token 'nch_lvt'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'src/foo.py') "expected leak listing src/foo.py; out=$($r.StdOut)"
}

Test-Case 'paths-exact-match: same leak with file in paths passes' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
  - src/foo.py
path_prefixes: []
"@
    Add-LeakFile -RepoRoot $tmp -RelPath 'src/foo.py' -Token 'nch_lvt'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 0) "expected exit 0, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'P0 GATE PASS') "expected PASS; out=$($r.StdOut)"
}

# ---------------------------------------------------------------------- #
# (b) path_prefixes directory match (positive + sibling negative)
# ---------------------------------------------------------------------- #
Test-Case 'path_prefixes: file under listed prefix passes' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes:
  - allowed/
"@
    Add-LeakFile -RepoRoot $tmp -RelPath 'allowed/sub/bar.md' -Token 'tsmc'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 0) "expected exit 0, got $($r.ExitCode); out=$($r.StdOut)"
}

Test-Case 'path_prefixes: sibling outside the prefix still fails' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes:
  - allowed/
"@
    Add-LeakFile -RepoRoot $tmp -RelPath 'allowed/in/x.md'  -Token 'tsmc'
    Add-LeakFile -RepoRoot $tmp -RelPath 'other/out/y.md'   -Token 'tsmc'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'other/out/y.md') "expected leak listing other/out/y.md; out=$($r.StdOut)"
    Assert-True (-not ($r.StdOut -match 'allowed/in/x.md')) "did not expect allowed/in/x.md; out=$($r.StdOut)"
}

# ---------------------------------------------------------------------- #
# (c) Strict yaml parse: malformed allowlist fails closed with line-number
# ---------------------------------------------------------------------- #
Test-Case 'strict-parse: unknown top-level key fails closed' {
    $tmp = New-TempRepo -AllowlistContent @"
paths: []
path_prefixes: []
unexpected_key: []
"@
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'yaml_parse_fail.*unknown_top_level_key') `
        "expected unknown_top_level_key error; out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'line=3') "expected line=3 in error; out=$($r.StdOut)"
}

Test-Case 'strict-parse: inline flow scalar RHS fails closed' {
    $tmp = New-TempRepo -AllowlistContent @"
paths: not-a-list
path_prefixes: []
"@
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'inline_flow_or_scalar_not_supported') `
        "expected inline_flow_or_scalar error; out=$($r.StdOut)"
}

Test-Case 'strict-parse: list-item metasyntax fails closed' {
    # `- *literal` - star at start of unquoted list item is YAML anchor ref.
    # Mini-parser has no anchor semantics, so this MUST fail-closed.
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - *anchor_ref
path_prefixes: []
"@
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'list_item_metasyntax_not_supported') `
        "expected metasyntax error; out=$($r.StdOut)"
}

Test-Case 'strict-parse: missing required key (path_prefixes) fails closed' {
    $tmp = New-TempRepo -AllowlistContent @"
paths: []
"@
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'missing_required_key') `
        "expected missing_required_key error; out=$($r.StdOut)"
}

# ---------------------------------------------------------------------- #
# (d) Missing yaml file - script exits 1 with clear error (no silent fallback)
# ---------------------------------------------------------------------- #
Test-Case 'missing-yaml: gate fails closed when allowlist absent' {
    $tmp = New-TempRepo -AllowlistContent '' -WriteAllowlist $false
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'p0_gate_allowlist.yaml missing') `
        "expected missing-yaml error; out=$($r.StdOut)"
}

# ---------------------------------------------------------------------- #
# (e) Empty allowlist - only $ExcludedTop directories spared
# ---------------------------------------------------------------------- #
Test-Case 'empty-allowlist: leak in scanned tree fails' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes: []
"@
    Add-LeakFile -RepoRoot $tmp -RelPath 'anywhere/baz.py' -Token 'pch_lvt'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'anywhere/baz.py') "expected leak listing; out=$($r.StdOut)"
}

Test-Case 'empty-allowlist: $ExcludedTop dirs still spared (tmp not scanned)' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes: []
"@
    # tmp/ is in $ExcludedTop, so a leak there should NOT trip the gate.
    Add-LeakFile -RepoRoot $tmp -RelPath 'tmp/leak.sp' -Token 'tsmc'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 0) "expected exit 0 (tmp excluded), got $($r.ExitCode); out=$($r.StdOut)"
}

Test-Case 'empty-allowlist: .chat_recent.jsonl still spared' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes: []
"@
    Add-LeakFile -RepoRoot $tmp -RelPath '.chat_recent.jsonl' -Token 'nch_lvt'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 0) "expected exit 0 (.chat_recent.jsonl excluded); out=$($r.StdOut)"
}

# ---------------------------------------------------------------------- #
# (f) T8.9 R2 — tracked-config/logs hard-fail (force-add bypass).
#
# Background: config/logs/ is gitignored runtime scratch but is also
# listed under path_prefixes in the live allowlist (transcripts contain
# "tsmc" only as an LLM-prose echo of the spec brand). Pure prefix match
# means a force-added file under config/logs/ — banned tokens and all —
# would slip past phase-1. The new check forbids ANY tracked path under
# config/logs/, closing the force-add bypass without disturbing the
# legitimate ignored-artifact path.
# ---------------------------------------------------------------------- #
Test-Case 'tracked-config-logs: force-added transcript fails closed' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes:
  - config/logs/
"@
    & git -C $tmp init --quiet 2>$null | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) "git init failed in temp repo $tmp"
    # Suppress git's CRLF warning so stderr lines aren't promoted to a
    # terminating error by $ErrorActionPreference='Stop' under PS 5.1.
    & git -C $tmp config core.autocrlf false 2>$null | Out-Null
    & git -C $tmp config core.safecrlf false 2>$null | Out-Null
    Add-LeakFile -RepoRoot $tmp -RelPath 'config/logs/transcript_attack.jsonl' -Token 'nch_lvt'
    & git -C $tmp add -f -- 'config/logs/transcript_attack.jsonl' 2>$null | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) "git add -f failed for force-add fixture"
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'config/logs/') "expected message to name config/logs/; out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'tracked file') "expected 'tracked file' wording; out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'transcript_attack.jsonl') "expected offending path listed; out=$($r.StdOut)"
}

Test-Case 'tracked-config-logs: untracked artifact under config/logs/ still passes' {
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes:
  - config/logs/
"@
    & git -C $tmp init --quiet 2>$null | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) "git init failed in temp repo $tmp"
    # Suppress git's CRLF warning so stderr lines aren't promoted to a
    # terminating error by $ErrorActionPreference='Stop' under PS 5.1.
    & git -C $tmp config core.autocrlf false 2>$null | Out-Null
    & git -C $tmp config core.safecrlf false 2>$null | Out-Null
    # File is created but never `git add`-ed — represents the legitimate
    # local-only HSpice transcript flow.
    Add-LeakFile -RepoRoot $tmp -RelPath 'config/logs/transcript_legit.jsonl' -Token 'tsmc'
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 0) "expected exit 0 (untracked artifact, prefix-allowed); got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -notmatch 'tracked file') "did not expect tracked-file wording; out=$($r.StdOut)"
}

Test-Case 'tracked-config-logs: leak outside config/logs/ goes through normal scan' {
    # Sanity: the new check is scoped to config/logs/. A force-added file
    # OUTSIDE config/logs/ should NOT trigger the new check's wording —
    # it should fail via the existing phase-1 banned-token scan instead.
    $tmp = New-TempRepo -AllowlistContent @"
paths:
  - scripts/check_p0_gate.ps1
path_prefixes: []
"@
    & git -C $tmp init --quiet 2>$null | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) "git init failed in temp repo $tmp"
    # Suppress git's CRLF warning so stderr lines aren't promoted to a
    # terminating error by $ErrorActionPreference='Stop' under PS 5.1.
    & git -C $tmp config core.autocrlf false 2>$null | Out-Null
    & git -C $tmp config core.safecrlf false 2>$null | Out-Null
    Add-LeakFile -RepoRoot $tmp -RelPath 'src/leak_elsewhere.py' -Token 'pch_lvt'
    & git -C $tmp add -f -- 'src/leak_elsewhere.py' 2>$null | Out-Null
    $r = Invoke-GateScript -RepoRoot $tmp
    Assert-True ($r.ExitCode -eq 1) "expected exit 1, got $($r.ExitCode); out=$($r.StdOut)"
    Assert-True ($r.StdOut -notmatch 'force-add bypass detected') "did not expect new-check wording for non-config/logs path; out=$($r.StdOut)"
    Assert-True ($r.StdOut -match 'src/leak_elsewhere.py') "expected normal phase-1 leak listing; out=$($r.StdOut)"
}


Write-Host ''
Write-Host ('---- {0} pass, {1} fail ----' -f $pass, $fail)
if ($fail -gt 0) {
    foreach ($n in $failNames) { Write-Host ('  FAIL: {0}' -f $n) -ForegroundColor Red }
    exit 1
}
exit 0
