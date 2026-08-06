---
name: siril-background-neutralization
description: "Generate robust linear background-neutralization candidates from flat outer-field regions, compare linked Siril previews, and publish the best starless result."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Background Neutralization

Installed helper version: **1.0.0**.

This skill runs after linear denoising and before GHS pass 1.

## Required input

```text
<project>/processing/linear-denoise/SHO-starless-linear-denoised.fit
```

The helper verifies it against:

```text
<project>/processing/linear-denoise/linear-denoise-manifest.json
```

The upstream manifest must report:

```text
status: ready
downstream_linear_processing_permitted: true
helper_version: 1.0.1
```

## Why the helper applies the correction itself

Siril 1.4.4 documents manual background neutralization as selecting a
background rectangle, measuring each RGB median, and equalizing the channels.
The 1.4.4 scriptable command index does not expose a dedicated background-
neutralization command.

This helper implements those documented median-equalization semantics
deterministically with Astropy/NumPy, then uses Siril 1.4.4 to produce linked
before/after previews and verify that the resulting FITS loads correctly.

## Neutralization method

For each selected region:

1. Compute even-weighted luminance.
2. Reject very dark and bright pixels using robust MAD limits:
   `-2.8` and `+2.0`.
3. Measure the clipped R, G, and B medians.
4. Set the target background median to their arithmetic mean.
5. Apply one constant additive offset to each channel.

Because the three offsets sum to zero, even-weighted luminance is preserved.
The operation does not stretch the image and does not multiply the narrowband
channels.

## Candidate regions

Without explicit regions, the helper searches the outer field for three large,
flat, low-gradient, mutually distinct background boxes.

Optional manually chosen regions may be supplied as:

```text
--region x,y,width,height
```

up to three times.

## Two-phase workflow

### Generate candidates

```bash
background_neutralization.py run   --project "M16 July 2026"   --fresh-run   --timeout 7200
```

This preserves all candidate FITS files, region measurements, corrections,
metrics, logs, and linked previews. It does not publish.

Expected status:

```text
awaiting_visual_selection
```

### Compare and publish

CodeWarrior must inspect every satisfactory linked after-preview and choose the
region that best neutralizes the empty sky without suppressing real faint
nebula.

```bash
background_neutralization.py publish   --project "M16 July 2026"   --run-root "<run-root>"   --candidate "candidate-XX"   --visual-notes "<specific comparison rationale>"   --fresh-run
```

## Visual review

Compare every satisfactory candidate at the same linked display stretch.

The selected candidate should have:

- a substantially calmer red background;
- no new blue or green cast;
- neutral-looking empty sky;
- preserved faint outer Eagle Nebula emission;
- unchanged Pillars and internal structure;
- no clipping, blocks, rings, posterization, or hard region boundary;
- no evidence that real nebulosity was used as the background reference.

Do not publish when image inspection is unavailable.

## Canonical outputs

```text
<project>/processing/background-neutralization/
├── SHO-starless-linear-denoised-neutralized.fit
├── SHO-starless-linear-denoised-before-linked.png
├── SHO-starless-linear-denoised-neutralized-linked.png
└── background-neutralization-manifest.json
```

The neutralized FITS remains linear and starless. It becomes the canonical
input for GHS pass 1.

## Technical safeguards

A candidate cannot publish unless:

- RGB dimensions and 32-bit floating-point format are preserved;
- all values are finite;
- no low or high clipping is introduced;
- selected-region RGB medians become equal;
- even-weighted luminance is preserved;
- source/output structure correlation is effectively one;
- the source/output difference is a constant offset within each channel;
- channel corrections remain bounded;
- both linked Siril previews exist.

## Preservation

An existing canonical background-neutralization directory remains untouched
while candidates are generated and reviewed.

On successful replacement it is preserved beneath the new run as:

```text
previous-processing-background-neutralization/
```

Nothing is deleted.

## Commands

```bash
ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
BN="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-background-neutralization/scripts/background_neutralization.py"

"$ASTRO_PY" "$BN" --version
"$ASTRO_PY" "$BN" self-test --timeout 1800

"$ASTRO_PY" "$BN" run   --project "M16 July 2026"   --fresh-run   --timeout 7200

"$ASTRO_PY" "$BN" publish   --project "M16 July 2026"   --run-root "<run-root>"   --candidate "candidate-XX"   --visual-notes "<comparison rationale>"   --fresh-run

"$ASTRO_PY" "$BN" status   --project "M16 July 2026"
```

## Prohibited actions

Do not:

- run this on a stretched image;
- process the starmask or unscreen stars;
- use central Eagle Nebula emission as a background reference;
- multiply or independently stretch the RGB channels;
- publish without comparing all satisfactory previews;
- delete previous results, candidates, logs, or evidence;
- perform GHS or later processing in this skill.
