[CmdletBinding()]
param([string]$CredentialFile = "C:\Users\darkh\Projects\_runtime\ops-cure\config\synology_ssh_credentials.env")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$cfg = @{}
foreach ($l in Get-Content $CredentialFile) {
    if ($l -match '^([A-Z_]+)=(.*)$') { $cfg[$matches[1]] = $matches[2].Trim() }
}
$sp = $cfg["SSH_PASSWORD"].Replace("'", "'\''")

# Append slot-2 vars (idempotent — wipe any prior slot-2 first)
$py = @'
import os, re, pathlib
p = pathlib.Path("/volume1/docker/discord-bridge/.env")
text = p.read_text(encoding="utf-8") if p.exists() else ""
new = []
for line in text.splitlines():
    if re.match(r"^BRIDGE_AGENT_2_", line.strip()):
        continue
    new.append(line)
new += [
    "BRIDGE_AGENT_2_HANDLE=@bridge-reviewer",
    "BRIDGE_AGENT_2_BRAIN=pc-claude",
    "BRIDGE_AGENT_2_PC_MACHINE_ID=homedev-smoke-2",
    "BRIDGE_AGENT_2_PC_CWD=C:/Users/darkh/Projects/ops-cure-scratch",
    "BRIDGE_AGENT_2_PC_PERMISSION_MODE=acceptEdits",
]
p.write_text("\n".join(new) + "\n", encoding="utf-8")
print("\n".join(l for l in new if l.startswith("BRIDGE_AGENT_")))
'@
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($py))
$r = "export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin; echo $b64 | base64 -d | python3 2>&1"
$out = & ssh -p $cfg["SSH_PORT"] "$($cfg["SSH_USER"])@$($cfg["SSH_HOST"])" $r 2>&1
"--- env after edit ---"
$out
