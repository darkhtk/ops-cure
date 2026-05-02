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
export PATH=/usr/local/bin:`$PATH
echo '=== bridge logs (anything mentioning enqueue / run_start / brain / watcher) ==='
echo '$sudoPassEsc' | sudo -S docker logs nas-bridge --tail 2000 2>&1 | grep -iE 'enqueue|run_start|brain|watcher|pc-claude|pc_claude|PCClaudeBrain|RemoteClaudeReplyWatcher|forward_event|publish_event' | tail -80
echo ''
echo '=== bridge logs (errors / exceptions, last 60) ==='
echo '$sudoPassEsc' | sudo -S docker logs nas-bridge --tail 2000 2>&1 | grep -iE 'error|exception|traceback' | tail -60
echo ''
echo '=== bridge logs (run_start dispatch endpoints) ==='
echo '$sudoPassEsc' | sudo -S docker logs nas-bridge --tail 2000 2>&1 | grep -iE 'POST /api/remote-claude/(commands|sessions)|POST /api/remote-claude/agent/' | grep -v '/agent/commands/claim ' | grep -v '/agent/sync ' | tail -40
"@

& ssh -p $config["SSH_PORT"] "$($config["SSH_USER"])@$($config["SSH_HOST"])" $remote 2>&1
