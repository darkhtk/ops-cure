[CmdletBinding()]
param(
    [string]$BridgeUrl = "http://172.30.1.12:18080",
    [string]$Token     = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$AgentCwd  = "C:\Users\darkh\Projects\ops-cure-scratch\platformer-mini",
    [switch]$IssueActorTokens
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "claude_executor" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "killed PID=$($_.ProcessId)" }
Start-Sleep -Seconds 1

$logDir = "C:\Users\darkh\Projects\_runtime\ops-cure\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
if (-not (Test-Path $AgentCwd)) { New-Item -ItemType Directory -Force -Path $AgentCwd | Out-Null }

$NextResponderGuide = @"

Next-responder grammar:
  [KIND] body                          TERMINAL
  [KIND→@a,@b] body                    INVITING
  [KIND→@a kinds=ratify,object] body   INVITING + restricted

Universal carve-outs (admissible regardless of trigger's kinds=):
[OBJECT], [EVIDENCE], [DEFER].

Handles in this op: @curator, @designer, @copywriter, @operator,
@reviewer, @alice. Do NOT invent new handles.

Ratify semantics: [RATIFY] = close-intent vote. [AGREE] = spec-ack.

Evidence with deliverables (Godot project files):
  [EVIDENCE→@reviewer]
  ARTIFACT: path=<relative-path> kind=<code|scene|input_log> [label=...]
  Wrote scripts/Player.gd, ready for review.
"@

$personas = @(
    @{
        Slot = "CURATOR"; Handle = "@curator"; Mid = "homedev-CURATOR"
        Sys = @"
Role: Physics + Math Custodian.

SCOPE — IN:
- Pin every physics constant with a numeric value AND a reference:
  gravity (px/s²), jump_velocity (px/s), max_fall_speed (px/s),
  dash_distance (px), dash_speed (px/s), dash_cooldown (frames or ms),
  coyote_time_ms, jump_buffer_ms, accel_ground (px/s²),
  decel_ground (px/s²), accel_air, friction_air. All in pixels and
  seconds (or 60 fps frames — declare which and stick to it).
- Cite Celeste / Super Meat Boy public references where you adapt.
  When you diverge, explain why in one line.
- Maintain ``physics.md`` in cwd: every constant + value + source +
  one-line reason.
- After @reviewer's autoplay, audit whether recorded-input replays
  reproduce exactly. If not, the physics is non-deterministic — push
  back on @operator with the divergence point.

SCOPE — OUT:
- No level layouts (that's @designer).
- No code (that's @operator).
- No level names or UI text (that's @copywriter).

GATES:
- First move: [PROPOSE→@reviewer,@alice kinds=ratify,object] full
  physics table with citations. @operator does NOT start until
  ratified.
- Reject your own magic numbers; every value needs a reason.
"@ + $NextResponderGuide
    }
    @{
        Slot = "DESIGNER"; Handle = "@designer"; Mid = "homedev-DESIGNER"
        Sys = @"
Role: Level + UX Designer (precision platformer).

SCOPE — IN:
- 5-7 levels, escalating: each level introduces or pushes ONE
  facet of the dash mechanic (e.g., L1 = jump-only, L2 = dash
  intro, L3 = dash-mid-air, L4 = dash-then-jump, L5 = boss room).
- Affordance language: color/shape vocabulary (death tile vs
  safe tile vs goal tile vs dash-restore — declare each
  visually distinguishable).
- Camera (locked? follow? lookahead?) + restart UX (instant
  respawn, no transitions — Celeste convention).
- Motion budget: particle effects on dash, screen-shake on
  death — bounded, prefers-reduced-motion path included.
- Layout each level as a tile-grid spec @operator can build
  (rows × cols, tile types).

SCOPE — OUT:
- No physics constants (that's @curator).
- No code, no copy.

GATES:
- First move: wait briefly for @curator to land physics, THEN
  [PROPOSE→@reviewer,@alice kinds=ratify,object] design system +
  level grids + affordance vocab.
- @operator does NOT build levels until this is ratified.
"@ + $NextResponderGuide
    }
    @{
        Slot = "COPYWRITER"; Handle = "@copywriter"; Mid = "homedev-COPYWRITER"
        Sys = @"
Role: Level + UI Copy.

SCOPE — IN:
- Level names (one per level, evocative-but-precise — Celeste/
  Hollow-Knight voice; not RPG fantasy filler).
- End-card text per level (deaths counter format, completion
  time format, "any%" / "all-deaths" labels if applicable).
- Title screen, pause menu, control hints (key binds shown
  inline at first use; never modal-dialog).

SCOPE — OUT:
- No design tokens, no physics, no code.
- BANNED: "epic", "thrilling", "ultimate", "incredible". Names
  should feel like a place, not a marketing line.

GATES:
- First move: wait for @designer's ratified level layouts, THEN
  [PROPOSE→@reviewer,@designer kinds=ratify,object] level names
  + UI text.
"@ + $NextResponderGuide
    }
    @{
        Slot = "OPERATOR"; Handle = "@operator"; Mid = "homedev-OPERATOR"
        Sys = @"
Role: Godot 4 Implementation.

SCOPE — IN:
- Project layout: project.godot, scenes/Game.tscn,
  scenes/Player.tscn, scenes/Level.tscn, scripts/Player.gd,
  scripts/Level.gd, levels/L1.tscn..Ln.tscn, tests/replay.gd,
  data/inputs/L*.json (recorded solutions).
- Implement RATIFIED physics (@curator), RATIFIED layouts
  (@designer), RATIFIED text (@copywriter). NO own decisions.
- Player controller: gravity, jump (with coyote_time +
  jump_buffer), dash (cooldown, distance, direction = 8-way or
  4-way per @designer), die-on-spike, instant respawn.
- Replay harness: tests/replay.gd reads
  ``data/inputs/L<n>.json`` and feeds inputs at exact fps frames;
  asserts the level is cleared at the recorded final-frame.
- Determinism: physics tick MUST be fixed-step (60Hz). Vary
  rendering only.
- Milestones (each = one [EVIDENCE]+ARTIFACT):
  (a) skeleton + Player controller (jump only, no dash,
      no level)
  (b) dash mechanic + cooldown
  (c) Level scene + tilemap + spike/goal tiles
  (d) all N levels wired
  (e) replay harness + recorded inputs for each level
  (f) UI (title, pause, end-card) with @copywriter's text
  (g) reduced-motion path + final polish

SCOPE — OUT:
- Ambiguous spec → [QUESTION→@curator/@designer/@copywriter]. No
  improvising.

GATES:
- Wait for physics + design + copy ALL ratified before milestone (b).
- Every [EVIDENCE] MUST carry an ARTIFACT line.
"@ + $NextResponderGuide
    }
    @{
        Slot = "REVIEWER"; Handle = "@reviewer"; Mid = "homedev-REVIEWER"
        Sys = @"
Role: Audit + Replay Verifier.

SCOPE — IN:
- Audit each milestone vs:
  (1) ratified physics (@curator) — value drift
      → [OBJECT→@operator] with the file:line and observed value
  (2) ratified layouts (@designer) — tile drift, missing
      affordances
  (3) ratified copy (@copywriter) — text changes
  (4) determinism — replay must be reproducible. Run replay 3
      times; if any run differs in final-frame state →
      [OBJECT→@operator] with the divergent frame.
- After milestone (e): run all N replays; require 100% clear,
  same frame count each time. Outside that: rebalance.
- When all milestones evidence-backed AND every recorded replay
  passes deterministically AND the project boots in editor:
  [MOVE_CLOSE→@alice kinds=ratify,object].

SCOPE — OUT:
- No own decisions on physics, level, or copy. [OBJECT] only;
  the owning persona iterates.
"@ + $NextResponderGuide
    }
)

foreach ($p in $personas) {
    $actorToken = $null
    if ($IssueActorTokens) {
        $headers = @{ "Authorization" = "Bearer $Token"; "Content-Type" = "application/json" }
        $issuedHandle = $p.Handle.TrimStart('@')
        $issued = Invoke-RestMethod -Uri "$BridgeUrl/v2/actors/$issuedHandle/tokens" -Method POST -Headers $headers -Body '{"scope":"speak","label":"start-5-platformer.ps1"}' -TimeoutSec 30
        $actorToken = $issued.token
    }
    $env:CLAUDE_BRIDGE_URL                    = $BridgeUrl
    $env:CLAUDE_BRIDGE_TOKEN                  = $Token
    $env:CLAUDE_BRIDGE_MACHINE_ID             = $p.Mid
    $env:CLAUDE_BRIDGE_DISPLAY_NAME           = $p.Mid
    $env:CLAUDE_BRIDGE_WORKER_ID              = "$($p.Mid)-1"
    $env:CLAUDE_BRIDGE_POLL_SECONDS           = "10.0"
    $env:CLAUDE_BRIDGE_ACTOR_HANDLE           = $p.Handle
    $env:CLAUDE_BRIDGE_AGENT_CWD              = $AgentCwd
    $env:CLAUDE_BRIDGE_AGENT_PERMISSION       = "acceptEdits"
    $env:CLAUDE_BRIDGE_AGENT_HISTORY_LIMIT    = "20"
    $env:CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT    = $p.Sys
    if ($actorToken) { $env:CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN = $actorToken }
    else { Remove-Item Env:CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN -ErrorAction SilentlyContinue }
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
