# Spawn the deck-builder mini task op.
#
# Usage:
#   .\spawn-deckbuilder-task.ps1 -SpaceId <discord-thread-id>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string]$SpaceId,
    [string]$BridgeUrl    = "http://172.30.1.12:18080",
    [string]$Token        = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$OpenerHandle = "@alice",
    [string]$ProjectDir   = "C:\Users\darkh\Projects\ops-cure-scratch\deckbuilder-mini"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $ProjectDir)) { New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null }

$objective = @"
Build a Slay-the-Spire-inspired single-class mini deck-builder
roguelike in Godot 4 under cwd ``$ProjectDir``. The game is a
CHALLENGE: passing requires not just running code but a
balance-verified, autoplay-stable deliverable.

REFERENCE BAR:
- Slay the Spire (StS): card economy, intent display, status
  stacking, encounter shape. ``balance.md`` (@curator) cites
  StS values where adapted, and explains every divergence.
- Visual / UX bar — clean, dense, dark-default, StS-like
  card-detail-on-hover. Don't ape the art; pin down the
  affordances.

SCOPE OF THE BUILD:
- 1 player class (e.g., "Defender" — block-leaning), 15-20
  cards.
- 6 encounters: 4 normal, 1 elite, 1 boss. Encounter difficulty
  curve must be playtested via autoplay.
- 4-6 status effects (e.g., Block, Vulnerable, Weak, Strength,
  Poison). Each MUST be deterministic and machine-resolvable.
- Random AI autoplay harness that runs N≥100 games and prints
  win-rate.
- Local Godot 4 project — opens with the editor, runs with
  ``godot --path . --headless --script tests/autoplay.gd`` (or
  equivalent).

QUALITY GATES:
- Autoplay win-rate must land in [0.30, 0.70] before close.
  Outside band → @reviewer requests rebalance from @curator.
- Every status effect's behavior must match its on-card text
  EXACTLY. @reviewer audits this from card-list ratify against
  status-script behavior.
- Intent display required: enemies show their next-turn action
  (icon + number) BEFORE the player acts.
- ``requires_artifact=true`` — closing demands the built project
  attached on the closing [EVIDENCE].

CREW (5 personas, scope-strict):
- @curator (Balance Custodian): owns numbers + ``balance.md`` +
  StS reference citations + post-autoplay audit.
- @designer (Game/UX): pacing + intent UI + layout + motion +
  color/type tokens.
- @copywriter: card names, effect text, status descriptions, UI
  labels. NO filler ("powerful" / "epic" banned).
- @operator: Godot 4 impl, ARTIFACT lines on every milestone.
  No own decisions; [QUESTION→...] when ambiguous.
- @reviewer: audit + run autoplay + [MOVE_CLOSE→@alice] when in
  band.

OPENING SEQUENCE:
- @curator first: [PROPOSE→@reviewer,@alice kinds=ratify,object]
  baseline economy (HP, energy/turn, hand, draw, deck, status
  duration unit) WITH reference citations.
- @designer in parallel: [PROPOSE→@reviewer,@alice kinds=ratify,object]
  design system + encounter pacing + intent UI spec.
- @copywriter waits for @curator's ratify, THEN
  [PROPOSE→@reviewer,@curator kinds=ratify,object] full card list
  (15-20) + status descriptions.
- @operator stays silent until balance + design + copy are all
  ratified, then begins the milestone chain.
- @reviewer audits each milestone, runs autoplay after (f), then
  [MOVE_CLOSE→@alice] when win-rate is in band.

DELIVERABLE on close: a Godot 4 project that boots, plays through
6 encounters via either UI or autoplay harness, with every card
matching its ratified text and status effect, and with a
documented autoplay win-rate.
"@

$payload = @{
    space_id            = $SpaceId
    kind                = "task"
    title               = "Mini deck-builder roguelike (Godot 4, autoplay-balanced)"
    intent              = "build"
    opener_actor_handle = $OpenerHandle
    objective           = $objective
    addressed_to        = "@curator"
    success_criteria    = @{
        balance_table        = "balance.md exists; every card has cost/effect/source/rationale"
        autoplay_band        = "100+ random-AI runs land win-rate in [0.30, 0.70]"
        intent_display       = "enemies show next action BEFORE player acts"
        status_determinism   = "every status text matches script behavior exactly"
        godot_runnable       = "project boots in editor; headless autoplay command runs"
        ratification_chain   = "balance + design + copy ratified BEFORE @operator codes"
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

$headers = @{
    "Authorization" = "Bearer $Token"
    "Content-Type"  = "application/json"
}

"`n→ POST $BridgeUrl/v2/operations  (space_id=$SpaceId)"
$response = Invoke-RestMethod `
    -Uri "$BridgeUrl/v2/operations" `
    -Method POST `
    -Headers $headers `
    -Body $payload `
    -TimeoutSec 30

"`n✓ op opened"
"  id:        $($response.id)"
"  state:     $($response.state)"
"  policy:    close=$($response.policy.close_policy) requires_artifact=$($response.policy.requires_artifact) max_rounds=$($response.policy.max_rounds)"
"  addressed: @curator + @designer (parallel propose; @copywriter waits; @operator gated)"
"`nDiscord forwarding: lifecycle markers will appear in thread $SpaceId."
