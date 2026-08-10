---
name: siril-sho-channel-balance
description: "Stage-only bounded PixelMath colour balancing of the reviewed StarNet STARLESS SHO image before GHS. Never touch the star layer, rerun StarNet, or invoke AstroProcessor."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril SHO Channel Balance 1.1.0 — native post-StarNet starless contract

Use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance
```

Normal request:

```text
Process M16 July 2026 with SHO channel balance.
```

## Stage-only routing contract — highest priority

This is a single existing-project stage request. Canonical projects root:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/Projects
```

Never invoke AstroProcessor or invoke `astroproc` for any reason. Never inspect `/mnt/asiair`, create/import/prepare a project, or rediscover the project in another root. Never use `ls`, `find`, `cat`, `grep`, `jq`, globbing, or manual run-root discovery as review-file recovery.

Do **not** use or inspect:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/Projects
```

## Correct pipeline placement

```text
siril-sho-combination
→ siril-background-neutralization
→ siril-starnet-removal
→ siril-sho-channel-balance
→ siril-ghs-stretch
```

**This stage processes only the StarNet STARLESS image. It never modifies the starmask or stars/unscreen layer.** This placement exists specifically so nebular colour balancing cannot turn the preserved stars magenta/purple.

StarNet 1.5.2 must use source-contract revision `native-starnet-channel-balance-v1` and point directly to `siril-sho-channel-balance`. The former direct-StarNet-to-GHS compatibility bridge is no longer accepted.

## Required source

```text
processing/starnet/SHO-starless-linear.fit
processing/starnet/starnet-manifest.json
processing/starnet/visual-review-record.json
```

Require StarNet helper 1.5.2, source-contract revision `native-starnet-channel-balance-v1`, ready status, completed visual review, finite BITPIX -32 RGB starless FITS, exact source/review checksums, `next_stage: siril-sho-channel-balance`, `sho_channel_balance_permitted: true`, and `ghs_pass1_permitted: false`.

Never process:

```text
processing/starnet/SHO-starmask.fit
processing/starnet/SHO-stars-unscreen.fit
```

## Starless PixelMath

The post-StarNet RGB channels retain SHO semantics: R is SII-derived, G is Ha-derived, B is OIII-derived. Apply:

```text
R' = med(R) + r * (R - med(R))
G' = med(R) + g * (G - med(G))
B' = med(R) + b * (B - med(B))
```

Baseline:

```text
r = 1.00
g = 0.25
b = 1.00
```

This is the starless equivalent of the successful manual M16 formula. Siril PixelMath runs with result rescaling OFF and RGB recomposition with `-nosum`.

## Bounded adaptive policy

Maximum attempts: 5, stop early when balanced.

```text
r: 0.70–1.30, max step 0.15
g: 0.15–0.40, max step 0.05
b: 0.70–1.30, max step 0.15
```

Only one coefficient changes per attempt. CodeWarrior classifies one dominant problem; the helper calculates the bounded numeric move. Immediate reversal requires explicit overshoot evidence.

Allowed problems:

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

`magenta_cast` here means **magenta/purple in the starless nebula/background**, not star colour.

## Routine workflow

Start/resume only with:

```text
sho-channel-balance advance --project "<project>"
```

For every `visual_review_required`, pass each `read_targets[].path` verbatim to OpenClaw Read. Both source and candidate are starless. Review green, magenta/purple nebular over-correction, SII-derived red/gold, OIII-derived blue/cyan, faint outer emission/Pillars/dark lanes, and weak-channel noise.

Then call `review-refine` with all six specific review notes. On `selection_review_required`, Read every returned target again and choose the best reviewed starless candidate, not necessarily the last. Structured per-candidate `balance`, `magenta`, `structure`, and `noise` notes remain mandatory.

## Existing v1.0.1 result migration

The existing pre-StarNet v1.0.1 canonical result is **obsolete for the new pipeline but is a recognized migration predecessor**. Do not delete it. v1.1.0 may start a new starless run while leaving that canonical directory untouched. On successful publication the old directory is preserved beneath the new run.

Once a valid v1.1.0 result exists, repeating the stage requires normal fresh-run confirmation.

## Canonical outputs

```text
processing/sho-channel-balance/
├── SHO-starless-linear-balanced.fit
├── SHO-starless-linear-before-channel-balance.png
├── SHO-starless-linear-balanced.png
└── sho-channel-balance-manifest.json
```

Successful completion reports:

```text
status: ready
source_is_starless: true
stars_layer_modified: false
next_stage: siril-ghs-stretch-pass1
ghs_pass1_permitted: true
background_neutralization_permitted: false
star_removal_permitted: false
```

Stop before GHS pass 1.

## Installed GHS skill-name routing

The actual installed GHS skill names are:

```text
pass 1: siril-ghs-stretch
pass 2: siril-ghs-stretch-pass2
```

For compatibility, existing helper/manifests may still expose the logical stage
label `siril-ghs-stretch-pass1`. Treat that label as an alias for the installed
`siril-ghs-stretch` skill. Do not search for or create a
`siril-ghs-stretch-pass1` skill directory.
