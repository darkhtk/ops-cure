[CmdletBinding()]
param([string]$CredentialFile = "C:\Users\darkh\Projects\_runtime\ops-cure\config\synology_ssh_credentials.env")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$cfg = @{}
foreach ($l in Get-Content $CredentialFile) {
    if ($l -match '^([A-Z_]+)=(.*)$') { $cfg[$matches[1]] = $matches[2].Trim() }
}
$sp = $cfg["SSH_PASSWORD"].Replace("'", "'\''")
$h = "${($cfg["SSH_USER"])}@${($cfg["SSH_HOST"])}"
$port = $cfg["SSH_PORT"]

$repoRoot = Split-Path -Parent $PSScriptRoot
$files = @(
    "nas_bridge/app/behaviors/agent/reply_watcher.py",
    "nas_bridge/app/behaviors/agent/service.py"
)

$tmpTar = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "ops-hotpatch.tar")
if (Test-Path $tmpTar) { Remove-Item -Force $tmpTar }
& tar -cf $tmpTar -C $repoRoot $files
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

& scp -O -P $port $tmpTar "$($cfg["SSH_USER"])@$($cfg["SSH_HOST"]):/tmp/ops-hotpatch.tar"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

$remote = @"
set -e
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
TMP=`$(mktemp -d)
tar -xf /tmp/ops-hotpatch.tar -C `$TMP
echo '$sp' | sudo -S docker cp `$TMP/nas_bridge/app/behaviors/agent/reply_watcher.py nas-bridge:/app/app/behaviors/agent/reply_watcher.py
echo '$sp' | sudo -S docker cp `$TMP/nas_bridge/app/behaviors/agent/service.py nas-bridge:/app/app/behaviors/agent/service.py
echo '$sp' | sudo -S docker exec nas-bridge sh -c 'find /app/app -name __pycache__ -prune -exec rm -rf {} +'
echo '$sp' | sudo -S docker restart nas-bridge >/dev/null
rm -rf `$TMP /tmp/ops-hotpatch.tar
echo OK
"@
$out = & ssh -p $port "$($cfg["SSH_USER"])@$($cfg["SSH_HOST"])" $remote 2>&1
$out | ForEach-Object { "$_" }
Remove-Item -Force $tmpTar
