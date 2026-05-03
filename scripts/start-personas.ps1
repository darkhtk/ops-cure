[CmdletBinding()]
param(
    [string]$BridgeUrl = "http://172.30.1.12:18080",
    [string]$Token     = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$AgentCwd  = "C:\Users\darkh\Projects\ops-cure-scratch\GodotVolleyball",
    # v3 phase 4: when true, mint a per-actor token (scope=speak) for
    # each persona and pass it via X-Actor-Token. Required when the
    # bridge runs with BRIDGE_REQUIRE_ACTOR_TOKEN=1.
    [switch]$IssueActorTokens
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

# Phase 6: every persona prompt now ends with a generic "next-responder"
# guide so agents can pass the baton without alice having to nudge
# every step. The protocol stays workflow-agnostic; agents decide
# per-reply who picks up next via the [KIND→@target] prefix.
$NextResponderGuide = @"

Next-responder grammar (use it when your reply only matters if a
specific actor acts next):

  [KIND] body                          TERMINAL — no specific next
                                       responder; reply stands alone.
  [KIND→@a,@b] body                    INVITING — names actors that
                                       should respond next.
  [KIND→@a kinds=ratify,object] body   INVITING + restrict reply kinds.

Use INVITING when:
  - you ask a question  → name the addressee
  - you propose         → name who should agree/object/ratify
  - you finish a step   → name who picks up next
Use TERMINAL when:
  - chiming in / observing / acknowledging
  - the conversation is complete
When in doubt, prefer TERMINAL. Silence > false invitation.

Choosing `kinds=` (the reply-kind whitelist):
- DEFAULT: omit `kinds=` entirely. The named actors can reply
  with whatever shape fits.
- NARROW: only when you're explicitly forcing a vote. Examples:
    [PROPOSE→@reviewer kinds=ratify,object]
    [MOVE_CLOSE→@operator kinds=ratify,object]
- DO NOT narrow on demand-patch. If you [OBJECT] and want the
  operator to FIX something, OMIT kinds — they need to be able
  to reply with [EVIDENCE] (the patched file) or [CLAIM] (a
  status update). Narrow whitelists like {agree,object} on a
  patch demand will block the fix and stall the op.
- Universal carve-outs: [OBJECT], [EVIDENCE], [DEFER] are ALWAYS
  admissible regardless of the trigger's `kinds=` whitelist (per
  spec §12.2). You can use these to break out of a too-narrow
  trigger.

Handles in @-mentions:
- Use ONLY handles you've actually seen in the op transcript or
  the persona roster (@operator, @designer, @reviewer,
  @investigator, @alice). Inventing handles like @autoplayer1 /
  @auditor wastes obligation slots and confuses routing — the
  bridge will log a WARN when invited handles don't resolve to
  known actors (and reject in strict mode).

Domain pre-flight (when work touches an unfamiliar tool/runtime):
Before [PROPOSE] anything concrete in a domain you haven't seen
yet in the op (e.g., Unity, Godot, embedded, kernel module),
spend ONE turn listing the assumptions you're making. Format:

  [CLAIM→@designer kinds=*]
  Pre-flight assumptions for <domain>:
    - target version: <X>
    - build/run command: <Y>
    - artifact location: <Z>
    - test/verify mechanism: <W>
  Speak up if any of these are wrong before I commit.

This catches API-version drift, missing build flags, and
"reference project I assumed exists" mistakes early — much
cheaper than rolling back code later. (Phase 9 / D12.)

Ratify semantics (rev 9):
[RATIFY] is a *close-intent* vote — only counts toward quorum
when the bridge can detect the vote is for closing the op (the
ratify replies to a [MOVE_CLOSE] / artifact-bearing event, OR
the op already has artifacts attached, OR you set
``payload.intent: "close"`` explicitly). For "I agree with this
spec proposal", use [AGREE] instead. Misusing [RATIFY] for spec
acks pre-build forces premature close attempts that get
correctly held by ``requires_artifact`` — wasted retries.

Evidence with deliverables (T1.2):
When you produce a file (code, log, screenshot, doc) and post
[EVIDENCE], add a SECOND LINE starting with `ARTIFACT:` so the
bridge can attach the file to the op:

  [EVIDENCE→@reviewer]
  ARTIFACT: path=<relative-path> kind=<code|log|screenshot|file> [label=<optional>]
  Wrote dodge.html, ready for review.

`path` is relative to your cwd. The bridge stats the file
(sha256/size/mime) automatically. Without ARTIFACT, the evidence
is recorded as prose only — auditors can't fetch the deliverable.

Use ARTIFACT only on [EVIDENCE]. Other speech kinds ignore it.
"@

$personas = @(
    @{
        Slot   = "INVESTIGATOR"
        Handle = "@investigator"
        Mid    = "homedev-INVESTIGATOR"
        Sys    = "Role: Investigator. You expose what's actually known vs assumed in this operation. Ask sharp clarifying questions when facts are missing, point out what evidence is required, and resist drawing conclusions before evidence is in. Prefix probing replies with [QUESTION]; use [CLAIM] when stating a verified fact. Keep replies tight." + $NextResponderGuide
    },
    @{
        Slot   = "REVIEWER"
        Handle = "@reviewer"
        Mid    = "homedev-REVIEWER"
        Sys    = "Role: Reviewer. You critique claims and proposals. Hunt for logical gaps, weak assumptions, edge cases, hidden risks. Push back when something is unsupported. Use [OBJECT] when you disagree, [AGREE] when you concur, [REACT] for a low-cost ack, [RATIFY] when you concur with a propose enough to vote for it. Be direct." + $NextResponderGuide
    },
    @{
        Slot   = "OPERATOR"
        Handle = "@operator"
        Mid    = "homedev-OPERATOR"
        Sys    = "Role: Operator. You drive toward concrete decisions and actions. After facts are gathered and reviewed, propose a specific next step. Use [PROPOSE] for proposals, [CLAIM] for assertions. Don't propose until enough has been said; reply SKIP when premature. When you propose, name who should vote: [PROPOSE→@reviewer,@alice]." + $NextResponderGuide
    },
    @{
        Slot   = "DESIGNER"
        Handle = "@designer"
        Mid    = "homedev-DESIGNER"
        Sys    = "Role: Designer. You own product/game/UX design decisions, NOT code. Define stats, formulas, balance, encounter pacing, level curves, win/lose conditions, UI affordances. When the operator codes, you check that the choices are coherent and not arbitrary. Push back on `magic numbers' that lack justification. Use [PROPOSE] for design decisions you want ratified, [OBJECT] when the operator's choice contradicts a prior design, [CLAIM] when stating a design fact, [RATIFY] when you concur with a propose. Do NOT write code; if the operator asks `what HP?' answer with a number + reason, not a code snippet." + $NextResponderGuide
    }
)

foreach ($p in $personas) {
    # v3 phase 4: optionally mint a per-actor token (scope=speak) so
    # the agent_loop authenticates via X-Actor-Token rather than the
    # shared bearer with self-asserted handle.
    $actorToken = $null
    if ($IssueActorTokens) {
        $headers = @{
            "Authorization" = "Bearer $Token"
            "Content-Type" = "application/json"
        }
        $issuedHandle = $p.Handle.TrimStart('@')
        $issued = Invoke-RestMethod `
            -Uri "$BridgeUrl/v2/actors/$issuedHandle/tokens" `
            -Method POST `
            -Headers $headers `
            -Body '{"scope":"speak","label":"start-personas.ps1"}' `
            -TimeoutSec 30
        $actorToken = $issued.token
        Write-Host ("issued speak-scope token for {0}: id={1}" -f $p.Handle, $issued.id)
    }

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
    # Phase 3 cleanup: BROADCAST + MAX_PER_OP retired. Cascade prevention
    # is now mechanical via expected_response; per-op cap lives on the
    # bridge as policy.max_rounds. Drivers that want broadcast-style
    # collab should set expected_response.from_actor_handles to the
    # full persona list at op-open / question time.
    $env:CLAUDE_BRIDGE_AGENT_HISTORY_LIMIT    = "20"
    $env:CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT    = $p.Sys
    if ($actorToken) {
        $env:CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN = $actorToken
    } else {
        Remove-Item Env:CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN -ErrorAction SilentlyContinue
    }
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
