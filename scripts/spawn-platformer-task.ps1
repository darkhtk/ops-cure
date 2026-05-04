[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string]$SpaceId,
    [string]$BridgeUrl    = "http://172.30.1.12:18080",
    [string]$Token        = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$OpenerHandle = "@alice",
    [string]$ProjectDir   = "C:\Users\darkh\Projects\ops-cure-scratch\platformer-mini"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not (Test-Path $ProjectDir)) { New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null }

$objective = @"
Build a Celeste-inspired precision platformer mini in Godot 4
under cwd ``$ProjectDir``. ONE novel mechanic: dash. Everything
else is precision-jumping fundamentals.

REFERENCE BAR:
- Celeste (gravity, jump, coyote-time, jump-buffer, dash). Cite
  Celeste's published values where adapted; explain divergences.
- Super Meat Boy for instant-respawn UX.

SCOPE:
- 5-7 levels of escalating mechanic depth. Each level introduces
  or pushes ONE facet of the dash (jump-only → dash intro → mid-
  air dash → dash-then-jump → final).
- Recorded-input replay per level. Replays MUST be deterministic
  — running the same input 3× yields identical final-frame state.
- Affordance vocabulary: death/safe/goal/dash-restore tiles
  visually distinguishable.
- Local Godot 4 project, opens in editor, runs replays via
  ``godot --path . --headless --script tests/replay.gd``.

QUALITY GATES:
- physics.md (@curator) lists every constant + reference.
- Every recorded replay clears its level at the same final frame
  every time. Non-determinism → [OBJECT→@operator].
- Every level visibly demonstrates the mechanic facet it
  introduces.
- ``requires_artifact=true`` — closing demands the built project
  attached.

CREW:
- @curator (Physics): physics.md + post-replay determinism audit.
- @designer (Levels + UX): tile grids + affordance vocab + camera/restart UX.
- @copywriter: level names + UI text.
- @operator: Godot 4 impl + replay harness.
- @reviewer: per-milestone audit + 3× replay determinism + close.

OPENING SEQUENCE:
- @curator first: [PROPOSE→@reviewer,@alice kinds=ratify,object]
  full physics table.
- @designer in parallel (or after): [PROPOSE→@reviewer,@alice]
  design system + level grids + affordance vocab.
- @copywriter waits for @designer's ratify, THEN [PROPOSE]
  level names + UI strings.
- @operator gated until all three ratified.
- @reviewer audits each milestone, runs 3× replay after (e),
  [MOVE_CLOSE→@alice] when deterministic.

DELIVERABLE on close: a Godot 4 project that boots, has 5-7
levels each clearable via the recorded-input replay reproducibly,
matching every ratified spec.
"@

$payload = @{
    space_id            = $SpaceId
    kind                = "task"
    title               = "Precision platformer mini (Godot 4, Celeste-inspired, dash mechanic)"
    intent              = "build"
    opener_actor_handle = $OpenerHandle
    objective           = $objective
    addressed_to        = "@curator"
    success_criteria    = @{
        physics_table         = "physics.md exists; every constant cited + justified"
        replay_determinism    = "3× replay of every level yields identical final frame"
        mechanic_facets       = "each level demonstrates a distinct dash facet"
        godot_runnable        = "boots in editor; headless replay runs"
        ratification_chain    = "physics + design + copy ratified BEFORE @operator codes"
    }
    policy              = @{
        close_policy        = "opener_unilateral"
        join_policy         = "invite_only"
        max_rounds          = 200
        requires_artifact   = $true
        bot_open            = $true
    }
    expected_response   = @{
        from_actor_handles  = @("@curator", "@designer")
        kinds               = @("propose")
    }
} | ConvertTo-Json -Depth 10

$headers = @{ "Authorization" = "Bearer $Token"; "Content-Type" = "application/json" }
$response = Invoke-RestMethod -Uri "$BridgeUrl/v2/operations" -Method POST -Headers $headers -Body $payload -TimeoutSec 30

"`n✓ op opened"
"  id:        $($response.id)"
"  state:     $($response.state)"
"  policy:    close=$($response.policy.close_policy) requires_artifact=$($response.policy.requires_artifact) max_rounds=$($response.policy.max_rounds)"
