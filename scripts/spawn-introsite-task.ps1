# Spawn the ops-cure intro-site task op.
#
# Usage:
#   .\spawn-introsite-task.ps1 -SpaceId <discord-thread-id>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string]$SpaceId,
    [string]$BridgeUrl    = "http://172.30.1.12:18080",
    [string]$Token        = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$OpenerHandle = "@alice",
    [string]$ProjectDir   = "C:\Users\darkh\Projects\ops-cure-scratch\opscure-introsite"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $ProjectDir)) { New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null }

$objective = @"
Build a single-page intro/landing site for the ops-cure project
itself (NOT the user's personal portfolio). The site must
truthfully describe what ops-cure is: a multi-agent governance
protocol kernel with audit-grade enforcement, plus the
deep-fix / deep-execute / protocol-rubric methodology that
surrounds it.

The crew is FIVE roles with non-overlapping scope. Each prompt
encodes its own boundaries; this objective only states what
crosses boundaries.

DESIGN REFERENCE — the bar:
- Linear.app   — density, precision, restraint. Note the type
                 hierarchy and the way the hero leads with one
                 sharp claim, not a tagline soup.
- Stripe Docs  — technical-readable, code-adjacent voice, the
                 way it earns trust with specifics rather than
                 adjectives.
@designer must cite which choice is influenced by which (no
imitation without rationale).

INFORMATION ARCHITECTURE (crew refines):
1. Hero — one-sentence positioning + one specific anchor (e.g.,
   "rev 11 spec, 7 phases shipped, 38 conformance vectors").
2. The problem — why ops-cure exists (silent failures, drift,
   multi-actor coordination without an audit trail).
3. How the protocol works — speech kinds, state machine,
   capability model, governance acts (move_close / ratify),
   bounds & quotas.
4. The methodology — deep-fix → deep-execute → protocol-rubric
   chain. Cite the .claude/skills/ files.
5. Phases shipped — terse timeline of what each phase fixed
   (phase 1–11). @curator vouches every line with a commit/doc
   reference.
6. GitHub / repo link. (No invented metrics.)

FACT POLICY:
- @curator vouches every claim by file:line, commit SHA, or doc
  section. The repo is the ground truth.
- No marketing filler. @copywriter rejects ``cutting-edge``,
  ``industry-leading``, ``robust``, ``seamless``, etc. in own
  drafts and others'. @reviewer enforces.
- @designer must justify every numeric value (type ratio, color
  contrast, spacing scale).
- @operator implements only what is ratified. Ambiguity →
  [QUESTION] to the owning persona.

DELIVERABLE:
- All files in cwd ``$ProjectDir``.
- Either single ``index.html`` + linked CSS, or Vite + vanilla TS
  with ``npm run build`` succeeding.
- Responsive (no horizontal scroll at 360 / 768 / 1280),
  WCAG AA contrast, keyboard navigable, prefers-reduced-motion
  respected.
- Final [EVIDENCE] from @operator MUST attach the built artifact
  (index.html or dist/index.html) — ``requires_artifact=true``
  enforces this.
- @reviewer posts [MOVE_CLOSE→@alice] when audit passes;
  @alice (close_policy=opener_unilateral) holds final close.

OPENING SEQUENCE:
- @curator first: [CLAIM→@designer,@copywriter,@operator,@reviewer]
  fact survey of 3–5 verifiable hooks (file:line each).
- @designer in parallel: [PROPOSE→@reviewer,@alice kinds=ratify,object]
  with full design system.
- @copywriter waits for @curator's survey, then
  [PROPOSE→@reviewer,@curator kinds=ratify,object] with copy slabs.
- @operator stays silent until both proposals are ratified, then
  begins the milestone chain with [EVIDENCE]+ARTIFACT lines.
- @reviewer audits every milestone; pushes back with [OBJECT];
  closes via [MOVE_CLOSE] when done.
"@

$payload = @{
    space_id            = $SpaceId
    kind                = "task"
    title               = "ops-cure intro site — 5-persona build (Linear/Stripe-grade)"
    intent              = "build"
    opener_actor_handle = $OpenerHandle
    objective           = $objective
    addressed_to        = "@curator"
    success_criteria    = @{
        local_buildable        = "open index.html OR npm run build emits dist/"
        truthful_facts         = "every claim has a curator-vouched citation"
        design_grade           = "matches the Linear/Stripe reference bar (no filler, no magic numbers)"
        accessibility          = "AA contrast, keyboard navigable, focus rings, prefers-reduced-motion"
        responsive             = "no horizontal scroll at 360 / 768 / 1280"
        ratification_chain     = "@designer + @copywriter ratified BEFORE @operator codes"
    }
    policy              = @{
        close_policy        = "opener_unilateral"
        join_policy         = "invite_only"
        max_rounds          = 120
        requires_artifact   = $true
        bot_open            = $true
    }
    expected_response   = @{
        from_actor_handles  = @("@curator")
        kinds               = @("claim")
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
"  addressed: @curator (first move: [CLAIM] fact survey)"
"`nDiscord forwarding: open + lifecycle markers will appear in thread $SpaceId."
