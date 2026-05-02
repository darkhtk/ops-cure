[CmdletBinding()]
param(
    [string]$CredentialFile = "C:\Users\darkh\Projects\_runtime\ops-cure\config\synology_ssh_credentials.env"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$config = @{}
foreach ($line in Get-Content -Path $CredentialFile) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) { continue }
    $parts = $line -split "=", 2
    if ($parts.Count -eq 2) { $config[$parts[0].Trim()] = $parts[1].Trim() }
}

$sudoPass = $config["SSH_PASSWORD"]
$sudoPassEsc = if ($sudoPass) { $sudoPass.Replace("'", "'\''") } else { "" }

$remote = @"
set +e
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
cd /volume1/docker/discord-bridge
echo '=== inject agent env vars (echo brain, no API key needed) ==='
if grep -q '^BRIDGE_AGENT_ENABLED' .env; then
  echo 'agent env already present'
else
  cat >> .env <<'EOF'

# Smoke -- echo agent for live behavior verification
BRIDGE_AGENT_ENABLED=true
BRIDGE_AGENT_HANDLE=@bridge-agent
BRIDGE_AGENT_BRAIN=echo
EOF
  echo 'agent env appended'
fi
echo '--- current .env (BRIDGE_AGENT_*) ---'
grep '^BRIDGE_AGENT_' .env || echo 'none'
echo
echo '=== compose up -d (reloads .env) ==='
echo '$sudoPassEsc' | sudo -S docker compose up -d 2>&1 | tail -10
sleep 4
echo
echo '=== status ==='
echo '$sudoPassEsc' | sudo -S docker ps --filter name=nas-bridge --format 'table {{.Names}}\t{{.Status}}'
echo
echo '=== logs (full last 40 lines) ==='
echo '$sudoPassEsc' | sudo -S docker logs nas-bridge --tail 40 2>&1
"@

& ssh -p $config["SSH_PORT"] "$($config["SSH_USER"])@$($config["SSH_HOST"])" $remote 2>&1
