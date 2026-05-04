# Spawn a personal-portfolio task op into a Discord thread.
#
# Usage:
#   .\spawn-portfolio-task.ps1 -SpaceId <discord-thread-id>
#   .\spawn-portfolio-task.ps1 -SpaceId 1234567890123456789 -OpenerHandle "@alice"
#
# Pre-requirement: personas must be running with cwd pointing at the
# portfolio scratch dir. Restart them first if they're still on a
# different project:
#
#   .\start-personas.ps1 -AgentCwd C:\Users\darkh\Projects\ops-cure-scratch\portfolio
#
# This script POSTs to /v2/operations and returns the new op id +
# event-stream URL so you can tail the conversation.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$SpaceId,

    [string]$BridgeUrl     = "http://172.30.1.12:18080",
    [string]$Token         = "kmagD8TckFIFoqr7gpgMjtIWKCOqat_GmvnyraA4IEUo3nhKDMbeKKtq9VaHNgJ9",
    [string]$OpenerHandle  = "@alice",
    [string]$ProjectDir    = "C:\Users\darkh\Projects\ops-cure-scratch\portfolio"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $ProjectDir)) {
    New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null
    "created project dir: $ProjectDir"
}

# --- Task body ---------------------------------------------------------
# Objective is intentionally rich: it states the user's positioning,
# the suggested division of labor across the four personas, the
# placeholder discipline, and the deliverable contract. Personas read
# this on their first turn.

$objective = @"
Build a polished single-page personal portfolio website for the user
(a systems / protocol engineer who designs ops-cure: multi-agent
governance protocols, the deep-fix root-cause methodology, and
phase-by-phase protocol hardenings).

Goals — primary (must hit) and secondary (nice to have):

PRIMARY:
1. Visual + UX completeness:
   - explicit type scale (≥ 4 sizes, modular ratio chosen, not
     arbitrary)
   - explicit color tokens (light + dark, sufficient WCAG AA contrast)
   - spacing rhythm based on an 8px or 4px grid
   - motion budget: at most one signature transition (no parallax-
     for-the-sake-of-parallax). Honors prefers-reduced-motion.
   - keyboard navigability + visible focus rings
   - responsive ≥ 360 px width with a test on 768 / 1280
2. Content fit for an engineering portfolio:
   - hero: one-line positioning + a small "what I'm doing now"
     block (datestamped)
   - 3–5 case-study cards drawn from REAL repo artifacts of
     ops-cure (e.g., protocol-rubric skill, deep-fix methodology,
     phase 11 axis-H adversarial-robustness boundary, the rubric
     audit that surfaced enforcement gaps). Each card: 1 image
     or diagram, 2-line problem, 1-line approach, 1-line outcome.
     Don't invent metrics — pull only what's verifiable in this
     repo's commits or docs.
   - methodology section: concise plain-language paragraph on
     deep-fix → deep-execute → protocol-rubric. Link to the
     skill files in the repo (relative paths OK; clicking opens
     locally).
   - contact: at least one of (email, GitHub, LinkedIn) — see
     PLACEHOLDER POLICY below.
3. Local-buildable:
   - either ``npm run dev`` (Vite + vanilla TS) OR a single
     ``index.html`` + linked CSS file. No remote build steps,
     no third-party API keys at runtime. ``npm run build`` must
     also work and emit ``dist/``.

SECONDARY (nice-to-have if time allows after primary lands):
   - Print stylesheet (so a print-to-PDF gives a clean resume-
     ish output)
   - Static OG/twitter card image generated from the hero
   - Reduced-motion + high-contrast preview button (toggle stays
     in localStorage)

PLACEHOLDER POLICY:
The user's display name, exact employer/title, contact email, and
social handles are NOT in the repo. Do NOT invent them. INVESTIGATOR
opens with a single tight [QUESTION] block listing the personal
facts that are needed (name, contact email/socials, optional
"what I'm doing now" line, color preference if any) and waits for
the user to answer. Until the answers arrive, write content with
``{{NAME}}``, ``{{EMAIL}}``, ``{{GITHUB}}`` placeholders so the
site is reviewable but not falsely-authoritative.

Once the user answers, search-and-replace the placeholders in a
single commit, post [EVIDENCE] with ARTIFACT lines for each touched
file, and proceed.

DIVISION OF LABOR:
- @designer — opens with a [PROPOSE→@reviewer,@alice kinds=ratify,object]
  containing the visual system: type scale (numeric values with
  rationale), color tokens, spacing scale, motion budget,
  accessibility checklist. Operator does NOT start coding until the
  design system is ratified.
- @operator — implements the ratified design system. Posts
  [EVIDENCE] with an ARTIFACT: line for every milestone:
  (a) skeleton + tokens.css, (b) hero, (c) case-study cards from
  repo artifacts, (d) methodology section, (e) contact, (f) a11y
  + reduced-motion polish. After (f), [MOVE_CLOSE→@operator
  kinds=ratify].
- @reviewer — reviews each milestone against the design system
  + the a11y checklist. Pushes back on magic numbers
  ([OBJECT→@operator]) and ratifies milestones that conform.
- @investigator — owns the [QUESTION] for personal facts. Also
  flags any case-study card whose claims aren't traceable to the
  repo (no inventing metrics).

DELIVERY:
- All work goes into the project cwd: ``$ProjectDir`` (already
  created; personas should be running there).
- Final closure requires ``policy.requires_artifact=true`` to
  resolve, so attach a real artifact (e.g., ``index.html`` or
  ``dist/index.html``) on the closing [EVIDENCE].
"@

# --- Build the JSON request --------------------------------------------

$payload = @{
    space_id              = $SpaceId
    kind                  = "task"
    title                 = "Personal portfolio site — design + UX-grade local prototype"
    intent                = "build"
    opener_actor_handle   = $OpenerHandle
    objective             = $objective
    addressed_to          = "@designer"
    success_criteria      = @{
        local_buildable        = "npm run dev OR opening index.html shows the site"
        accessibility          = "keyboard navigable, AA contrast, focus rings visible"
        motion_discipline      = "honors prefers-reduced-motion"
        responsive             = "no horizontal scroll at 360 / 768 / 1280"
        no_invented_facts      = "all personal info either filled by user or {{PLACEHOLDER}}"
    }
    policy                = @{
        close_policy        = "operator_ratifies"
        join_policy         = "self_or_invite"
        max_rounds          = 80
        requires_artifact   = $true
        bot_open            = $true
    }
    expected_response     = @{
        from_actor_handles  = @("@designer")
        kinds               = @("propose")
        # designer's first move should be the design-system proposal;
        # see DIVISION OF LABOR in the objective.
    }
} | ConvertTo-Json -Depth 10

# --- POST --------------------------------------------------------------

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
"  kind:      $($response.kind)"
"  policy:    close=$($response.policy.close_policy) requires_artifact=$($response.policy.requires_artifact) max_rounds=$($response.policy.max_rounds)"

"`nTail the conversation:"
"  curl -H 'Authorization: Bearer <token>' '$BridgeUrl/v2/operations/$($response.id)?include_events=true' | jq"
"`nOr stream the inbox:"
"  curl -N -H 'Authorization: Bearer <token>' '$BridgeUrl/v2/inbox/sse?actor_handle=@alice'"

"`n💡 Personal facts the user must answer when @investigator asks:"
"   - display name"
"   - contact email and/or GitHub / LinkedIn"
"   - one-line 'what I'm doing now' (optional)"
"   - color/aesthetic preference (optional; default = minimal mono+dark)"
