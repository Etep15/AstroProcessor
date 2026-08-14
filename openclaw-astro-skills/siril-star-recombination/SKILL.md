---
name: siril-star-recombination
description: "For any compatible project, recombine the finished saturated starless branch with the finished processed-star branch using Siril PixelMath screen blending, review bounded star-contribution candidates, and publish the final SHO image."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# siril-star-recombination

Version: **1.0.0**

## Purpose

This is the target-agnostic final autonomous stage for compatible projects in the SHO pipeline. It combines:

- the canonical saturated starless image from `siril-saturation`; and
- the canonical processed-star layer from `siril-star-processing`.

The processed stars originate from StarNet's native unscreen-aware stars product. **Do not linearly add the stars to the starless image.** Recombination uses Siril PixelMath with a screen-aware RGB expression:

```text
1 - (1 - starless) * (1 - k * stars)
```

where `k` controls the contribution of the already-processed star layer.

This is a scriptable PixelMath recombination workflow. It is not an attempt to reproduce Siril's GUI Star Recomposition tool byte-for-byte.


## Target-agnostic contract

The installed skill is **not specific to M16 or any astronomical target**. The project is supplied through `--project`, and all canonical paths are resolved beneath that project's own `processing/` tree. Any compatible target may use this stage when its upstream manifests satisfy the contracts below.

The installer uses `M16 July 2026` only as an exact, non-destructive validation fixture because it is the currently completed project with accepted canonical starless and processed-star branches. Those M16 hashes are installation gates only; they are not processing policy and are not embedded as runtime target choices.

## Canonical inputs

Starless branch:

```text
processing/saturation/SHO-starless-saturated.fit
processing/saturation/saturation-manifest.json
```

Stars branch:

```text
processing/star-processing/SHO-stars-processed.fit
processing/star-processing/star-processing-manifest.json
```

The stage validates both manifests and both FITS checksums before processing. Star processing must explicitly permit `siril-star-recombination`.

The existing `siril-saturation` v1.0.0 manifest predates assignment of a downstream recombination stage and records `next_stage: null` plus `downstream_processing_permitted: false`. Version 1.0.0 accepts that legacy saturation handoff only when the canonical saturation output, project, status, visual review, path and SHA all validate exactly. A future saturation manifest may instead explicitly hand off to this skill.

## Candidate family

All candidates use the same screen-aware PixelMath formula and differ only in processed-star contribution:

- `candidate-00`: `k=0.70` — conservative star contribution
- `candidate-01`: `k=0.85` — balanced/default recommendation
- `candidate-02`: `k=1.00` — full contribution of the already-processed star layer

No candidate uses linear addition or PixelMath rescaling.

## Autonomous short-prompt workflow

For requests such as:

```text
Process <project> with star recombination.
```

or:

```text
Recombine the stars for <project>.
```

use this skill directly. Do not route back to StarNet removal or star processing.

### 1. Begin

Run exactly:

```bash
{baseDir}/bin/star-recombination begin --project "<project>"
```

Interpret status literally:

- `would_generate_candidates`: immediately continue to `advance`.
- `confirmation_required`: tell the user the stage already completed or is obsolete and ask the returned fresh-rerun question. Do not rerun without confirmation.
- `blocked`: report the exact blocker and stop.

### 2. Generate candidates

Run exactly:

```bash
{baseDir}/bin/star-recombination advance --project "<project>"
```

A successful result returns `visual_review_required` with exact `read_targets`.

### 3. Exact-path visual review

Read **every** returned target verbatim.

The skill returns:

- one full-frame preview for each candidate; and
- one 2×3 star diagnostic panel for each candidate.

Hard rules:

- use only the exact `read_targets[].path` values returned by the stage;
- do not use `ls`, `find`, `grep`, `jq`, globbing, guessed paths or directory discovery to locate review evidence;
- if an exact Read fails, stop and report that exact failed path;
- inspect all six images before selecting.

Assess:

- star-to-nebula balance;
- whether stars overpower the nebula;
- star profiles and apparent size;
- halos, ringing, dark seams or holes around stars;
- neutral/natural star appearance;
- preservation of nebular color, structure and contrast;
- overall final-image balance.

The nebula should remain the primary subject. Metrics are technical gates, not a substitute for visual selection.

### 4. Select and publish

Call:

```bash
{baseDir}/bin/star-recombination select-publish \
  --project "<project>" \
  --run-root "<exact run_root>" \
  --candidate "<selected candidate>" \
  --compared "candidate-00" --note "<specific observations>" \
  --compared "candidate-01" --note "<specific observations>" \
  --compared "candidate-02" --note "<specific observations>"
```

Notes must demonstrate that all required visual fields were evaluated.

## Canonical outputs

Successful publication creates:

```text
processing/star-recombination/SHO-recombined.fit
processing/star-recombination/SHO-recombined.png
processing/star-recombination/star-recombination-manifest.json
processing/star-recombination/visual-selection-record.json
```

The stage records:

- both upstream canonical paths and SHA-256 values;
- both upstream manifest paths and SHA-256 values;
- screen blend model and selected star contribution;
- selected candidate metrics;
- exact final FITS and PNG SHA-256 values;
- visual review notes;
- run root and publication time.

`next_stage` is `null` and `final_processing_complete` is `true`.

## Fresh reruns and retention

A completed stage never silently reruns. After explicit confirmation:

```bash
{baseDir}/bin/star-recombination confirm-fresh --project "<project>"
{baseDir}/bin/star-recombination advance --project "<project>"
```

The existing canonical output remains untouched during candidate generation and review. On successful republishing, prior publication metadata and a recoverable prior candidate path are recorded when available.

Do **not** create accumulating `SHO-recombined.before-<timestamp>.fit` copies.

## Processing safeguards

Publication is prohibited when:

- either input or manifest is missing;
- input FITS dimensions/channels differ;
- an upstream path or SHA differs from its manifest;
- star processing does not explicitly permit recombination;
- input pixels are non-finite or outside the normalized domain;
- Siril PixelMath fails;
- screen formula verification fails;
- a candidate adds clipping;
- non-star background changes exceed the technical gate;
- visual review is incomplete.

Never use simple `starless + stars` recombination for this workflow.
