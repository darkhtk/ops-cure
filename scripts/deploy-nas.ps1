[CmdletBinding()]
param(
    [string]$CredentialFile = "C:\Users\darkh\Projects\_runtime\ops-cure\config\synology_ssh_credentials.env",
    [string]$DeployPath = "/volume1/docker/discord-bridge",
    [string]$ComposeFile = "docker-compose.yml",
    [switch]$AllowDirty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-EnvFile {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path $Path)) {
        return $values
    }
    foreach ($line in Get-Content -Path $Path) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $values[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
    return $values
}

function Assert-CleanRepo {
    param([string]$RepoRoot)
    if ($AllowDirty) {
        return
    }
    $status = git -C $RepoRoot status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read git status for $RepoRoot"
    }
    if ($status) {
        throw "Working tree is dirty. Commit or stash changes first, or rerun with -AllowDirty."
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $repoRoot "nas_bridge"
$config = Read-EnvFile -Path $CredentialFile
$sshHost = $config["SSH_HOST"]
$sshPort = $config["SSH_PORT"]
$sshUser = $config["SSH_USER"]

if (-not $sshHost -or -not $sshPort -or -not $sshUser) {
    throw "Missing SSH_HOST / SSH_PORT / SSH_USER in $CredentialFile"
}

Assert-CleanRepo -RepoRoot $repoRoot

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) "ops-cure-nas-bridge-$timestamp.tar"
$remoteArchive = "/tmp/ops-cure-nas-bridge-$timestamp.tar"

try {
    $null = git -C $repoRoot archive --format=tar HEAD:nas_bridge --output $archivePath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create git archive from $sourceRoot"
    }

    & scp -O -P $sshPort $archivePath "${sshUser}@${sshHost}:${remoteArchive}"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload archive to NAS"
    }

    $remoteScript = @"
set -e
# Synology's non-interactive SSH starts with a minimal PATH; docker lives in
# /usr/local/bin which isn't on the inherited PATH. Add it explicitly.
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
mkdir -p '$DeployPath'
find '$DeployPath' -mindepth 1 -maxdepth 1 ! -name '.env' ! -name 'data' -exec rm -rf {} +
tar -xf '$remoteArchive' -C '$DeployPath'
cd '$DeployPath'
(docker compose -f '$ComposeFile' up -d --build || sudo docker compose -f '$ComposeFile' up -d --build)
rm -f '$remoteArchive'
"@

    & ssh -p $sshPort "${sshUser}@${sshHost}" $remoteScript
    if ($LASTEXITCODE -ne 0) {
        throw "Remote deploy command failed"
    }
}
finally {
    if (Test-Path $archivePath) {
        Remove-Item -Force $archivePath
    }
}

Write-Host "Deployed nas_bridge to $DeployPath on $sshHost"
