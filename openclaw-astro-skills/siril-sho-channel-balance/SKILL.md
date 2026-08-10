---
name: siril-sho-channel-balance
description: "Autonomously balance the reviewed StarNet STARLESS SHO image with bounded Siril PixelMath refinement, exact-path visual review, and preservation-safe publication."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril SHO Channel Balance

Orchestration version: **1.2.0**

Processing engine version: **1.1.0** (unchanged)

Upstream StarNet contract:

```text
native-starnet-channel-balance-v1
```

Downstream GHS source contract:

```text
post-starnet-channel-balance-v1
```

## Pipeline

```text
siril-starnet-removal
→ siril-sho-channel-balance
→ siril-ghs-stretch
```

This stage processes only the **STARLESS** image. It never modifies or
regenerates the StarNet starmask or unscreen-stars layer.

## Normal one-line entry

For:

```text
Process <project> with SHO channel balance.
```

use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance advance --project "<project>"
```

Do not discover the project, helper, Python, run root or review files.

Never use `ls`, `find`, `tree`, `cat`, `grep`, `jq`, globbing, AstroProcessor,
ASIAIR inspection or source-code inspection to route this stage.

Canonical projects root:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/Projects
```

## Completed or obsolete canonical result

A canonical channel-balance result is still a completed result even when it
has become obsolete because the upstream StarNet result changed.

`advance` must return a user confirmation gate before any fresh replacement.

After the user explicitly answers yes:

```text
.../bin/sho-channel-balance confirm-fresh --project "<project>"
.../bin/sho-channel-balance advance --project "<project>"
```

The existing canonical output is preserved until successful publication.

The confirmation binds the fresh authorization to strong hashes of:

- the current native StarNet starless source;
- the current StarNet manifest/review;
- the existing channel-balance output;
- the existing channel-balance manifest.

Before confirmation, completed-stage detection is manifest-first and does not
hash the large FITS files.

Once fresh confirmation is given, **no further normal user interaction is
allowed**. CodeWarrior must finish candidate refinement, final selection and
publication autonomously unless there is a real processing/contract blocker or
an exact Read fails.

## PixelMath processing policy

Baseline:

```text
r = 1.00
g = 0.25
b = 1.00
```

Generalized STARLESS form:

```text
R = med(R) + r * (R - med(R))
G = med(R) + g * (G - med(G))
B = med(R) + b * (B - med(B))
```

Bounds:

```text
0.70 <= r <= 1.30
0.15 <= g <= 0.40
0.70 <= b <= 1.30
```

Maximum step per attempt:

```text
r: 0.15
g: 0.05
b: 0.15
```

Maximum attempts: **5**.

Only one coefficient changes between attempts. CodeWarrior never invents
numeric coefficients; the unchanged v1.1.0 engine chooses the next bounded
coefficient from the visual classification.

Allowed dominant problems:

```text
excessive_green
insufficient_green
magenta_cast
weak_red
excessive_red
weak_blue
excessive_blue
balanced
no_improvement
```

A reversal of the previous coefficient movement requires visible overshoot and
`--overshoot-observed`.

## Autonomous iterative visual review

When the wrapper returns:

```text
status: visual_review_required
action: continue_autonomously_review_refine
```

this is an internal continuation point, not a user handoff.

Use OpenClaw **Read** on every returned `read_targets[].path` exactly as
returned.

On attempt 1 the wrapper returns:

- the STARLESS source preview;
- candidate-01.

The source is read once. On later attempts, only the newly generated candidate
preview is returned because the source has not changed.

Do not:

```text
ls/find the review directory
emit MEDIA: paths
attach review images to the user
ask the user what dominant problem they see
ask the user whether to continue
choose numeric coefficients
```

For each candidate autonomously record specific observations for:

- green dominance/residual green;
- magenta/purple over-correction;
- SII-derived red/gold structure;
- OIII-derived blue/cyan structure;
- faint outer emission, Pillars and dark lanes;
- weak-channel noise amplification.

Each review note must contain at least 40 characters of specific visual
evidence.

Then invoke the exact `review-refine` command template returned by the wrapper.

Repeat until the wrapper returns `selection_review_required`.

## Autonomous final selection

Every generated candidate has already been visually reviewed by the time final
selection begins. v1.2.0 therefore does **not** require rereading the source
and all candidates again.

Use the accumulated visual reviews and technical summaries.

Choose the best acceptable candidate, not automatically the latest one.
When two candidates are materially equivalent, prefer the less aggressive
coefficient change.

Do not force SHO nebulosity to neutral grey. SHO colour is synthetic.

Do not evaluate star colour. The star layer is outside this stage.

Selection notes must cover every generated candidate:

```text
candidate-NN=balance:<40+ chars>; magenta:<40+ chars>; structure:<40+ chars>; noise:<40+ chars>
```

Overall visual comparison must contain at least 80 characters.

The orchestration layer safely normalizes embedded semicolons before calling
the unchanged v1.1.0 engine and rejects identical boilerplate candidate notes.

Then invoke `select-publish` autonomously.

If publication rejects only review formatting, repair the payload and retry
publication without rerunning Siril or generating new candidates.

## Canonical outputs

```text
processing/sho-channel-balance/
├── SHO-starless-linear-balanced.fit
├── SHO-starless-linear-before-channel-balance.png
├── SHO-starless-linear-balanced.png
├── sho-channel-balance-manifest.json
└── orchestration-review-v1.2.0.json
```

A successful publication must report:

```text
status: ready
source_is_starless: true
stars_layer_modified: false
next_stage: siril-ghs-stretch-pass1
ghs_pass1_permitted: true
background_neutralization_permitted: false
star_removal_permitted: false
source_contract_revision: post-starnet-channel-balance-v1
```

Stop before GHS pass 1.

## Important boundaries

Never alter:

```text
processing/starnet/SHO-starmask.fit
processing/starnet/SHO-stars-unscreen.fit
```

Do not run StarNet, background neutralization, GHS, green reduction,
saturation, star recomposition or later stages.
