param(
    [string]$Server = 'tx-cloud'
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if ($Server -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$') { throw 'Invalid SSH host alias' }
$changes = & git status --porcelain
if ($LASTEXITCODE -ne 0 -or $changes) { throw 'Commit local changes before publishing' }
& git push origin main
if ($LASTEXITCODE -ne 0) { throw 'GitHub push failed' }
& ssh -o BatchMode=yes -o ConnectTimeout=15 $Server 'bash /opt/codex-reset-radar/scripts/deploy.sh'
if ($LASTEXITCODE -ne 0) { throw 'Remote deployment failed; inspect server logs' }
