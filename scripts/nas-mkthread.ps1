[CmdletBinding()]
param([string]$CredentialFile = "C:\Users\darkh\Projects\_runtime\ops-cure\config\synology_ssh_credentials.env")
$cfg = @{}
foreach ($l in Get-Content $CredentialFile) {
    if ($l -match '^([A-Z_]+)=(.*)$') { $cfg[$matches[1]] = $matches[2].Trim() }
}
$sp = $cfg["SSH_PASSWORD"].Replace("'", "'\''")
$py = @'
import uuid, sys
sys.path.insert(0, "/app")
from app.behaviors.chat.models import ChatThreadModel
from app.db import session_scope, init_db
init_db()
discord_id = "smoke-" + uuid.uuid4().hex[:8]
with session_scope() as s:
    s.add(ChatThreadModel(
        id=str(uuid.uuid4()), guild_id="smoke", parent_channel_id="smoke",
        discord_thread_id=discord_id, title="behavior smoke",
        created_by="smoke",
    ))
    s.flush()
print(discord_id)
'@
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($py))
$r = "export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin; echo '$sp' | sudo -S docker exec nas-bridge sh -c 'echo $b64 | base64 -d | python' 2>&1"
$out = & ssh -p $cfg["SSH_PORT"] "$($cfg["SSH_USER"])@$($cfg["SSH_HOST"])" $r 2>&1
"--- raw output ---"
$out | ForEach-Object { "$_" }
"--- end raw ---"
