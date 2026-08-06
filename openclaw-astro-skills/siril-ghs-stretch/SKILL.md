---
name: siril-ghs-stretch
description: "Generate up to three bounded first-pass GHS candidates, compare their permanent previews, and publish the best satisfactory starless result."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Adaptive Siril GHS Stretch

Installed helper version: **1.1.0**.

This skill implements **GHS pass 1 only**.

## Required input

```text
<project>/processing/linear-denoise/SHO-starless-linear-denoised.fit
```

The helper verifies it against:

```text
<project>/processing/linear-denoise/linear-denoise-manifest.json
```

The upstream manifest must be `ready`, permit downstream linear processing,
and report helper version `1.0.1`.

## Two-phase workflow

### Phase 1: Generate and analyze candidates

```bash
ghs_stretch.py run   --project "M16 July 2026"   --fresh-run   --max-candidates 3   --timeout 7200
```

This creates **at most three total candidates** and does not modify the
canonical `processing/ghs-pass1` directory.

The result status is:

```text
awaiting_visual_selection
```

when at least one candidate passes all production safeguards.

### Phase 2: Visually select and publish

CodeWarrior must inspect every satisfactory permanent after-preview, compare
them, and publish exactly one:

```bash
ghs_stretch.py publish   --project "M16 July 2026"   --run-root "<adaptive-run-directory>"   --candidate "candidate-XX"   --visual-notes "<specific comparison and selection rationale>"   --fresh-run
```

The selected candidate must be technically satisfactory. Visual notes are
mandatory and are stored in the canonical manifest.

## Candidate policy

### Candidate 00 — proven baseline

```text
D=4.400
B=15.000
SP=0.00400
LP=0.00000
HP=0.86000
Colour model: Even weighted luminance
Clip mode: RGB Blend
```

### Candidate 01 — controlled comparison

The baseline histogram and clipping metrics determine one predefined change:

- **Too gentle:** controlled stronger step
- **Too strong or clipped:** controlled gentler step
- **Balanced:** predefined gentler comparison candidate

### Candidate 02 — final bounded refinement

The second candidate's metrics determine one final predefined adjustment:

- small stronger refinement
- small gentler refinement
- or a bounded midpoint when already near target

There is no fourth candidate.

## Hard parameter bounds

```text
D: 3.800–5.000
B: 10.000–20.000
SP: 0.00250–0.00600
LP: fixed at 0.00000
HP: 0.82000–0.92000
```

The helper cannot invent arbitrary values or exceed these ranges.

## Preserved evidence

Every candidate keeps:

```text
candidate-XX/work/SHO-starless-ghs-pass1.fit
candidate-XX/work/SHO-starless-ghs-pass1-roundtrip.fit
candidate-XX/previews/SHO-starless-ghs-pass1-linear.png
candidate-XX/previews/SHO-starless-linear-denoised-before-linked.png
candidate-XX/logs/
candidate-XX/ghs-pass1.ssf
candidate-XX/previews.ssf
```

Each run manifest records:

- parameters and adaptation reason
- GHT and inverse-GHT commands
- FITS evidence and checksums
- clipping, histogram, correlation, and roundtrip metrics
- numerical selection score
- script and log paths
- preview paths
- the numerically recommended candidate

## Selection rules

Only candidates whose production quality assessment is satisfactory can be
published.

The script gives a numerical recommendation. CodeWarrior must still compare
all satisfactory permanent previews and may choose a different satisfactory
candidate when the visual evidence is better.

Visual comparison must consider:

- visibility of the Eagle Nebula without making pass 1 final-bright
- preservation of the dark Pillars
- faint outer emission
- highlight protection
- background naturalness
- absence of clipping, blocks, posterization, rings, hard edges, or missing
  areas

If CodeWarrior cannot inspect the previews, it must not claim visual review or
publish a candidate.

## Canonical outputs

After publication:

```text
<project>/processing/ghs-pass1/SHO-starless-ghs-pass1.fit
<project>/processing/ghs-pass1/SHO-starless-linear-denoised-before-linked.png
<project>/processing/ghs-pass1/SHO-starless-ghs-pass1-linear.png
<project>/processing/ghs-pass1/ghs-pass1-manifest.json
```

The after-preview is saved without AutoStretch and represents the actual
permanent GHS result.

## Preservation behavior

An existing canonical `processing/ghs-pass1` directory remains untouched while
all candidates are generated and reviewed.

On successful publication, it is preserved intact under:

```text
<adaptive-run>/previous-processing-ghs-pass1/
```

The new canonical directory is published atomically. Nothing is deleted.

## Commands

```bash
ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
GHS="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch/scripts/ghs_stretch.py"

"$ASTRO_PY" "$GHS" --version
"$ASTRO_PY" "$GHS" self-test --timeout 1800

"$ASTRO_PY" "$GHS" run   --project "M16 July 2026"   --fresh-run   --max-candidates 3   --timeout 7200

"$ASTRO_PY" "$GHS" publish   --project "M16 July 2026"   --run-root "<run-root>"   --candidate "candidate-XX"   --visual-notes "<comparison rationale>"   --fresh-run

"$ASTRO_PY" "$GHS" status   --project "M16 July 2026"
```

## Prohibited actions

Do not:

- generate more than three total candidates
- use parameters outside the hard bounds
- invent unplanned values
- publish before reviewing every satisfactory preview
- publish an unsatisfactory candidate
- process the starmask or unscreen stars
- apply GHS pass 2
- adjust black point, green, or saturation
- apply AutoStretch to the canonical FITS
- delete candidates, previous results, logs, or evidence
