---
name: siril-starnet-removal
description: "Create the canonical StarNet starless image, starmask and unscreen stars product for a validated linear SHO project."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Canonical Siril StarNet Workflow

Use only:

    <this skill>/scripts/starnet_workflow.py

with:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python

Installed workflow version: 1.4.1.

## Canonical project directory

Successful project outputs belong only in:

    <project>/processing/starnet/

with these names:

    SHO-starless-linear.fit
    SHO-starmask.fit
    SHO-stars-unscreen.fit
    starnet-manifest.json

Do not create new project outputs under `processing/starnet-native`.
That older directory is historical evidence and must remain untouched.

## Products

- `SHO-starless-linear.fit`
  - linear image used by subsequent starless processing
- `SHO-starmask.fit`
  - StarNet `-m` starmask
  - sparse, nonnegative and 16-bit-derived
  - not `original - starless`
- `SHO-stars-unscreen.fit`
  - StarNet `-n` unscreen product
  - used later by a screen-aware recomposition workflow

Never require:

    starless + starmask = original

## Fresh-run requirement

The command:

    run --project "<project>"

must refuse to reuse or silently accept an existing `processing/starnet`
directory.

To execute StarNet again, use:

    run --project "<project>" --fresh-run

A fresh run:

1. leaves the current canonical directory in place while candidates run
2. validates the selected new candidate completely
3. moves the old canonical directory intact to:
   `<new run>/previous-processing-starnet`
4. atomically publishes the new directory as `processing/starnet`
5. restores the old directory if publication fails

No directory is deleted.

## Candidate policy

- baseline: temporary target background 0.15, x1
- retry 1: target 0.10, x1
- retry 2: target 0.06, x1
- retry 3: target 0.10, x2

There are no more than three retries.

The StarNet command always requests:

    --masks starnet-mask,starnet-unscreen

Do not use a subtraction-derived stars layer or compact-cleanup workflow.

## Quality gates

A publishable starmask must be:

- finite RGB
- nonnegative
- at least 99.5% aligned to 16-bit quantization
- at least 40% exactly zero
- nonempty
- below diffuse-structure correlation limits
- below relative-nebula-leakage limits

The starless image must also pass remaining-star detection.

## Correct path reporting

The stable manifest and status output must report actual paths under:

    <project>/processing/starnet/

They must never report `publish-staging` as the final file location.

## Commands

    ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
    STARNET="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-starnet-removal/scripts/starnet_workflow.py"

    "$ASTRO_PY" "$STARNET" --version

    "$ASTRO_PY" "$STARNET" run       --project "M16 July 2026"       --fresh-run       --max-retries 3       --timeout 7200

    "$ASTRO_PY" "$STARNET" status       --project "M16 July 2026"

## Prohibited actions

Do not:

- reuse an old result as though a new run occurred
- manually delete or replace `processing/starnet`
- modify `processing/starnet-native`
- alter the source `processing/sho/SHO-linear.fit`
- exceed three retries
- use `--masks subtract`
- linearly add the starmask to the starless image
- continue to later processing when status is not `ready`
