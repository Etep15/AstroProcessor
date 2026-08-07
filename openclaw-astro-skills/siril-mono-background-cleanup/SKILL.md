---
name: siril-mono-background-cleanup
description: "Generate bounded mono background-cleanup candidates, create a compact fresh-session review bundle, and publish one selected result per filter."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Mono Background Cleanup

Installed helper version: **1.0.1**.

This stage follows `siril-master-alignment` and precedes
`siril-mono-linear-denoise`.

## Fixed candidates

Each Ha, SII, and OIII master receives exactly:

```text
candidate-00: subsky 1 -samples=20 -tolerance=1.0
candidate-01: subsky 2 -samples=20 -tolerance=1.0
candidate-02: subsky -rbf -samples=12 -tolerance=1.0 -smooth=0.75
```

## Context-safe phase boundary

`run` preserves all nine full candidates and writes the complete evidence to
`run-manifest.json`. It does **not** print that manifest to chat.

It also creates a compact review bundle containing:

```text
decision-brief.md
decision-summary.json
CONTINUE-IN-FRESH-SESSION.txt
mono-background-cleanup-review.zip
Ha/before.png
Ha/candidate-00-after.png
Ha/candidate-00-model.png
...
```

Only one `before` preview is retained per filter because it is identical for
all three candidates. There are 21 compact previews total, each no larger than
1200 pixels on its longest side.

After `run` returns `awaiting_visual_selection`, the agent must stop the
session. Start a fresh CodeWarrior session with the generated continuation
prompt. Do not print or read the full `run-manifest.json` into chat.

## Existing 1.0.0 run

Version 1.0.1 can prepare a compact review for the existing run without
rerunning candidate generation:

```bash
mono_background_cleanup.py prepare-review   --project "M16 July 2026"   --run-root "<existing-run-root>"   --timeout 1800
```

## Visual review

Use `decision-brief.md` and the compact previews only. Inspect each candidate's
cleaned result and removed model. Reject models containing recognizable Eagle
Nebula, Pillars, stars, or filaments. Prefer the lowest-complexity candidate
that adequately removes the broad gradient.

## Publication

Publication requires a compact review bundle, one satisfactory candidate per
filter, and concise visual notes.

```bash
mono_background_cleanup.py publish   --project "M16 July 2026"   --run-root "<run-root>"   --ha-candidate "candidate-XX"   --sii-candidate "candidate-XX"   --oiii-candidate "candidate-XX"   --visual-notes "<concise per-filter rationale>"   --fresh-run
```

## Canonical outputs

```text
processing/mono-background-cleanup/background-clean_Ha.fit
processing/mono-background-cleanup/background-clean_SII.fit
processing/mono-background-cleanup/background-clean_OIII.fit
processing/mono-background-cleanup/background-model_Ha.fit
processing/mono-background-cleanup/background-model_SII.fit
processing/mono-background-cleanup/background-model_OIII.fit
processing/mono-background-cleanup/mono-background-cleanup-manifest.json
```

## Response limits

The agent must not:

- print the full run manifest;
- report every nested candidate record;
- run `find`, `ls -R`, or manual SHA loops over candidate directories;
- recalculate metrics already recorded in the decision brief;
- combine generation, full review, and publication in one model session.

Final publication reporting is limited to selected candidates, concise visual
notes, canonical paths and checksums, manifest path, final status,
`visual_review_completed`, and `mono_linear_denoise_permitted`.

Nothing is deleted.
