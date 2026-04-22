#
# P0 banned-pattern grep gate (PowerShell).
#
# Exits non-zero if any banned foundry token leaks into the PC-side
# source tree. Scope is the source workspace only — .venv, __pycache__,
# .pytest_cache, .quarantine, and logs/ are out of scope because they
# are either third-party or already-isolated artifacts.
#
# Three files are authorized GREP-GATE EXCEPTIONs:
#   - src/safe_bridge.py (authoritative banned-pattern list for _scrub)
#   - tests/test_safe_bridge.py (synthetic stand-ins for scrubber tests)
#   - scripts/check_p0_gate.ps1 (this script defines the banned-pattern
#     regex literal, so it must contain the tokens by construction)
# Any other hit fails the gate.
#
# Usage:  pwsh -NoProfile -File scripts/check_p0_gate.ps1
#

$ErrorActionPreference = 'Stop'

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot

$Banned = 'nch_|pch_|cfmom|rppoly|rm1_|tsmc|tcbn'

# Excluded top-level directories (third-party or isolated artifacts).
$ExcludedTop = @(
    '.venv', 'venv',
    '.quarantine',
    'logs',
    '.git',
    '.idea', '.vscode'
)
# Directory-name components that are excluded at any depth (generated
# build artifacts that may contain stale compiled strings).
$ExcludedAnywhere = @('__pycache__', '.pytest_cache')

# GREP-GATE EXCEPTION files (relative path from repo root, forward slash).
$Allowed = @(
    'src/safe_bridge.py',
    'tests/test_safe_bridge.py',
    'scripts/check_p0_gate.ps1'
)

Write-Host "P0 grep gate scanning $RepoRoot ..."

$files = Get-ChildItem -Path $RepoRoot -Recurse -File -Force |
    Where-Object {
        $rel = $_.FullName.Substring($RepoRoot.Path.Length + 1) -replace '\\', '/'
        $parts = $rel -split '/'
        $top = $parts[0]
        if ($ExcludedTop -contains $top) { return $false }
        foreach ($p in $parts) {
            if ($ExcludedAnywhere -contains $p) { return $false }
        }
        return $true
    }

$leaks = @()
foreach ($f in $files) {
    $rel = $f.FullName.Substring($RepoRoot.Path.Length + 1) -replace '\\', '/'
    if ($Allowed -contains $rel) { continue }
    try {
        $hit = Select-String -Path $f.FullName -Pattern $Banned -CaseSensitive:$false -SimpleMatch:$false -List -ErrorAction SilentlyContinue
    } catch {
        continue
    }
    if ($hit) {
        $leaks += $rel
    }
}

if ($leaks.Count -gt 0) {
    Write-Host "P0 GATE FAIL: banned tokens found in $($leaks.Count) file(s):" -ForegroundColor Red
    foreach ($l in $leaks) { Write-Host "  $l" -ForegroundColor Red }
    exit 1
}

Write-Host "P0 GATE PASS: no banned tokens in PC-side source workspace." -ForegroundColor Green
exit 0
