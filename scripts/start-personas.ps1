[CmdletBinding()]
param(
    [string]$BridgeUrl = "http://172.30.1.12:18080",
    [string]$Token     = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$AgentCwd  = "C:\Users\darkh\Projects\ops-cure-scratch"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Kill any existing executors first.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "claude_executor" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "killed PID=$($_.ProcessId)" }
Start-Sleep -Seconds 1

$logDir = "C:\Users\darkh\Projects\_runtime\ops-cure\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

$personas = @(
    @{
        Slot   = "INVESTIGATOR"
        Handle = "@investigator"
        Mid    = "homedev-INVESTIGATOR"
        Sys    = "Role: Investigator. You expose what's actually known vs assumed in this operation. Ask sharp clarifying questions when facts are missing, point out what evidence is required, and resist drawing conclusions before evidence is in. Prefix probing replies with [QUESTION]; use [CLAIM] when stating a verified fact. Keep replies tight."
    },
    @{
        Slot   = "REVIEWER"
        Handle = "@reviewer"
        Mid    = "homedev-REVIEWER"
        Sys    = "Role: Reviewer. You critique claims and proposals. Hunt for logical gaps, weak assumptions, edge cases, hidden risks. Push back when something is unsupported. Use [OBJECT] when you disagree, [AGREE] when you concur, [REACT] for a low-cost ack. Be direct."
    },
    @{
        Slot   = "OPERATOR"
        Handle = "@operator"
        Mid    = "homedev-OPERATOR"
        Sys    = "Role: Operator. You drive toward concrete decisions and actions. After facts are gathered and reviewed, propose a specific next step. Use [PROPOSE] for proposals, [CLAIM] for assertions. Don't propose until enough has been said; reply SKIP when premature."
    }
)

foreach ($p in $personas) {
    # Per-process env overrides (PowerShell carries Env:* into Start-Process).
    $env:CLAUDE_BRIDGE_URL                    = $BridgeUrl
    $env:CLAUDE_BRIDGE_TOKEN                  = $Token
    $env:CLAUDE_BRIDGE_MACHINE_ID             = $p.Mid
    $env:CLAUDE_BRIDGE_DISPLAY_NAME           = $p.Mid
    $env:CLAUDE_BRIDGE_WORKER_ID              = "$($p.Mid)-1"
    # Legacy /agent/commands/claim polling is unused for persona work
    # but the runner still drives it. 10s keeps SQLite quiet without
    # affecting the SSE-based agent_loop path.
    $env:CLAUDE_BRIDGE_POLL_SECONDS           = "10.0"
    $env:CLAUDE_BRIDGE_ACTOR_HANDLE           = $p.Handle
    $env:CLAUDE_BRIDGE_AGENT_CWD              = $AgentCwd
    $env:CLAUDE_BRIDGE_AGENT_PERMISSION       = "acceptEdits"
    $env:CLAUDE_BRIDGE_AGENT_BROADCAST        = "true"
    $env:CLAUDE_BRIDGE_AGENT_HISTORY_LIMIT    = "20"
    $env:CLAUDE_BRIDGE_AGENT_MAX_PER_OP       = "3"
    $env:CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT    = $p.Sys
    $proc = Start-Process -FilePath python `
        -ArgumentList "-u","-m","pc_launcher.connectors.claude_executor.runner" `
        -WorkingDirectory "C:\Users\darkh\Projects\ops-cure" `
        -RedirectStandardOutput "$logDir\persona-$($p.Slot).out.log" `
        -RedirectStandardError  "$logDir\persona-$($p.Slot).err.log" `
        -PassThru -WindowStyle Hidden
    "started $($p.Handle) PID=$($proc.Id)"
    Start-Sleep -Milliseconds 500
}

Start-Sleep -Seconds 4
"--- subscribe state ---"
foreach ($p in $personas) {
    $log = Get-Content "$logDir\persona-$($p.Slot).err.log" -ErrorAction SilentlyContinue
    $sub = ($log | Where-Object { $_ -match "subscribed:" }) | Select-Object -Last 1
    "$($p.Handle): $sub"
}
