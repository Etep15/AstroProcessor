---
name: siril-sho-channel-balance
description: "Stage-only SHO/PixelMath channel balancing for an existing CodeWarrior project. Use for prompts such as Process <project> with SHO channel balance. Never initialize/import/prepare a project and never invoke AstroProcessor or the broad astro-processing workflow."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril SHO Channel Balance 1.0.1

Use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance
```

Never search for the helper. Never choose Python manually.

Normal request:

```text
Process M16 July 2026 with SHO channel balance.
```

## Stage-only routing contract — highest priority

The request above is a **single existing-project stage request**, not a request
to start or resume the complete astrophotography pipeline.

Canonical CodeWarrior projects root:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/Projects
```

For M16 July 2026 the only project path for this stage is:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/Projects/M16 July 2026
```

Do **not** use or inspect:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/Projects
```

For this stage, never:

- load or execute the broad `astro-processing` workflow as the processing plan;
- invoke `astroproc` for any reason, including `--help`;
- create a project;
- copy/import ASIAIR data;
- prepare/reprepare project folders;
- inspect `/mnt/asiair`;
- rediscover the project in alternate roots;
- restart calibration, stacking, alignment, crop, or SHO combination.

The only normal processing entry point is:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance advance --project "<project>"
```

If this skill was reached by the broad `astro-processing` router, take over
immediately and follow only this skill. Do not return to earlier pipeline
stages.


## Pipeline placement

```text
siril-sho-combination
→ siril-sho-channel-balance
→ siril-background-neutralization
```

This skill preserves the pure `siril-sho-combination` checkpoint and creates a
separate balanced checkpoint.

Current v1.0.1 compatibility temporarily accepts the validated
`siril-sho-combination` 1.1.1 M16 contract as a legacy bridge. After this skill
is validated, the adjacent pipeline contracts will be updated so the bridge is
no longer necessary.

## Baseline PixelMath

The first attempt reproduces the successful manual M16 expression:

```text
R = S
G = med(S) + 0.25 * (H - med(H))
B = med(S) + (O - med(O))
```

The generalized bounded form is:

```text
R = med(S) + r * (S - med(S))
G = med(S) + g * (H - med(H))
B = med(S) + b * (O - med(O))
```

Baseline:

```text
r = 1.00
g = 0.25
b = 1.00
```

Siril PixelMath is run without `-rescale`. RGB recomposition uses `-nosum`.

## Bounded five-attempt policy

Maximum attempts: **5**. Stop early when balanced.

Hard bounds:

```text
0.70 <= r <= 1.30
0.15 <= g <= 0.40
0.70 <= b <= 1.30
```

Maximum movement per attempt:

```text
r: 0.15
g: 0.05
b: 0.15
```

Only one coefficient changes between attempts.

CodeWarrior never invents numeric coefficients. It classifies the single
dominant visual problem; the helper computes the next bounded coefficient.

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

A reversal of the immediately previous coefficient movement is blocked unless
CodeWarrior explicitly records `--overshoot-observed`.

## Routine workflow

Start/resume with:

```text
.../sho-channel-balance advance --project "<project>"
```

`advance` owns stage discovery, completed-stage confirmation, run discovery,
baseline generation and resume.

Do not use `find`, `ls`, `cat`, `grep`, `jq`, globbing, manual run-root
discovery, or run-manifest reads.

### `visual_review_required`

Use OpenClaw **Read** on every `read_targets[].path` exactly as returned.
Pass the paths verbatim.

If any required Read fails, STOP and report the exact failed path. Do not
inspect the run directory to recover it.

Evaluate the current candidate against the source for:

- green dominance/residual green;
- magenta or purple over-correction;
- SII red/yellow/orange structure;
- OIII blue/cyan structure;
- faint outer emission, Pillars and dark lanes;
- weak-channel noise amplification.

Then call:

```text
.../sho-channel-balance review-refine \
  --project "<project>" \
  --candidate "<current candidate>" \
  --dominant-problem "<one allowed value>" \
  --green-note "<specific observation>" \
  --magenta-note "<specific observation>" \
  --red-note "<specific observation>" \
  --blue-note "<specific observation>" \
  --structure-note "<specific observation>" \
  --noise-note "<specific observation>"
```

Add `--overshoot-observed` only when reversing the immediately previous
coefficient movement because the previous result visibly overshot.

The helper either generates one next bounded candidate or enters final
selection review.

### `selection_review_required`

Read every returned source/candidate path again exactly as returned.

Choose the **best acceptable reviewed attempt**, not necessarily the latest.

Prefer the least aggressive coefficients that give convincing SHO separation
without:

- magenta/purple bias;
- weak OIII noise amplification;
- loss of faint structure;
- unnatural domination by one synthetic channel.

Do not force the nebula to neutral grey. SHO colour is synthetic.

Publish with:

```text
.../sho-channel-balance select-publish \
  --project "<project>" \
  --candidate "<selected>" \
  --visual-notes "<overall comparison>" \
  --note "candidate-01=balance:<...>; magenta:<...>; structure:<...>; noise:<...>" \
  ...
```

A structured `--note` is required for every generated candidate. Vague fields
are mechanically rejected.

## Fresh rerun

A completed valid canonical result requires confirmation.

After the user answers yes:

```text
.../sho-channel-balance confirm-fresh --project "<project>"
.../sho-channel-balance advance --project "<project>"
```

The existing canonical result remains untouched until a successful replacement
is published.

## Canonical outputs

```text
processing/sho-channel-balance/
├── SHO-linear-balanced.fit
├── SHO-linear-before-channel-balance.png
├── SHO-linear-balanced.png
└── sho-channel-balance-manifest.json
```

Successful completion reports:

```text
status: ready
visual_review_completed: true
next_stage: siril-background-neutralization
background_neutralization_permitted: true
star_removal_permitted: false
```

Stop before background neutralization.

## Important boundaries

This stage does not alter:

```text
processing/sho/SHO-linear.fit
processing/sho/sho-combination-manifest.json
```

It does not run background neutralization, StarNet, stretch, green reduction,
saturation or any later stage.
