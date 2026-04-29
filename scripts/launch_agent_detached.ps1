$ErrorActionPreference = 'Stop'
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'

# Absolute paths — fill in for your machine before running.
$RepoRoot = $PSScriptRoot | Split-Path -Parent
$logDir = Join-Path $RepoRoot 'logs'
$py     = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$cwd    = $RepoRoot

$logOut = Join-Path $logDir ("run_detached_{0}.stdout.log" -f $ts)
$logErr = Join-Path $logDir ("run_detached_{0}.stderr.log" -f $ts)

# remote host SKILL dir — set env var VB_REMOTE_SKILL_DIR, or hard-code below.
$RemoteSkillDir = $env:VB_REMOTE_SKILL_DIR
if (-not $RemoteSkillDir) {
    $RemoteSkillDir = '/project/<user>/tool/virtuoso_bridge_lite/skill'
}

# Project name under projects/<name>/ — set env var VIRTUOSO_PROJECT, or hard-code below.
$ProjectName = $env:VIRTUOSO_PROJECT
if (-not $ProjectName) {
    $ProjectName = '<your-project>'
}

$runArgs = @(
    'scripts/run_agent.py',
    '--lib', '<your-lib>',
    '--cell', '<your-cell>',
    '--tb-cell', '<your-tb-cell>',
    '--spec', "projects/$ProjectName/constraints/spec.md",
    '--max-iter', '5',
    '--remote-skill-dir', $RemoteSkillDir,
    '--auto-bias-ic'
    # --scs-path omitted: auto-discovery picks newest Maestro input.scs.
)

$p = Start-Process -FilePath $py -ArgumentList $runArgs -WorkingDirectory $cwd `
    -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru

Write-Host ("PID: {0}" -f $p.Id)
Write-Host ("STDOUT: {0}" -f $logOut)
Write-Host ("STDERR: {0}" -f $logErr)
$p.Id | Out-File (Join-Path $logDir 'last_agent.pid') -Encoding ascii
