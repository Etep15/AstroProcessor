---
name: siril-background-neutralization
description: "Generate and review robust linear background-neutralization candidates from the validated SHO-combination output, then publish the accepted input for StarNet."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Background Neutralization

Installed helper version: **1.1.0**.

## Pipeline placement

```text
siril-sho-combination
→ siril-background-neutralization
→ siril-starnet-removal
```

This replaces the former StarNet → linear denoise → background-neutralization
placement.

## Required upstream input

```text
processing/sho/SHO-linear.fit
processing/sho/sho-combination-manifest.json
```

The SHO manifest must report:

```text
helper_version: 1.1.1
status: ready
stage_order.current: siril-sho-combination
stage_order.downstream: siril-background-neutralization
background_neutralization_permitted: true
star_removal_permitted: false
```

The helper validates the complete upstream manifest internally. CodeWarrior
must not read or print that complete manifest.

## Candidate set

```text
candidate-00: exact pass-through
candidate-01: first automatically discovered outer-field region
candidate-02: second automatically discovered outer-field region
candidate-03: third automatically discovered outer-field region
```

Pass-through is valid when all corrections are visually worse or ambiguous.

Region discovery is star-robust: it favours flat, low-gradient outer-field
boxes and rejects bright stars and nebulosity using robust luminance limits.

## Neutralization method

For each region:

1. Compute even-weighted luminance.
2. Reject pixels outside `median - 2.8 × MAD` and
   `median + 2.0 × MAD`.
3. Measure clipped R, G, and B medians.
4. Use their arithmetic mean as the target.
5. Apply one constant additive offset per channel.

The offsets sum to zero, preserving even-weighted luminance.

Negative values and values above one are valid in an unclipped linear
floating-point FITS. They are recorded as diagnostics and are not falsely
treated as clipping.

## Review evidence

The helper creates three common-stretch contact previews:

```text
full-contact.png
background_regions-contact.png
detail-contact.png
```

CodeWarrior must open all three with an image-capable tool.

The structured review records:

- preview paths and SHA-256 values;
- candidate-specific observations;
- background naturalness;
- faint-nebula preservation;
- star and halo impact;
- artifact flags;
- selected candidate and rationale.

Generic `--visual-notes` publication is removed.

## Commands

```bash
ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
BN="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-background-neutralization/scripts/background_neutralization.py"

"$ASTRO_PY" "$BN" --version
"$ASTRO_PY" "$BN" self-test --timeout 1800

"$ASTRO_PY" "$BN" run \
  --project "M16 July 2026" \
  --fresh-run \
  --timeout 7200

"$ASTRO_PY" "$BN" record-review \
  --project "M16 July 2026" \
  --run-root "<run-root>" \
  --review-json "<completed-review-json>"

"$ASTRO_PY" "$BN" publish \
  --project "M16 July 2026" \
  --run-root "<run-root>" \
  --review-record "<run-root>/visual-review-record.json" \
  --fresh-run

"$ASTRO_PY" "$BN" status \
  --project "M16 July 2026"
```

## Canonical outputs

```text
processing/background-neutralization/
├── SHO-linear-neutralized.fit
├── SHO-linear-before-linked.png
├── SHO-linear-neutralized-linked.png
├── full-contact.png
├── background-regions-contact.png
├── detail-contact.png
├── visual-review-record.json
└── background-neutralization-manifest.json
```

A pass-through publication keeps the `SHO-linear-neutralized.fit` name but
records:

```text
neutralization_applied: false
```

## Downstream contract

A ready result reports:

```text
stage_order.downstream: siril-starnet-removal
visual_review_completed: true
star_removal_permitted: true
```

This skill does not execute StarNet.

## Existing canonical output

The current helper-1.0.0 background-neutralization result belongs to the former
starless/linear-denoise pipeline. Version 1.1.0 classifies it as obsolete.

Candidate generation leaves it untouched. Successful publication with
`--fresh-run` preserves it under the new run as:

```text
previous-processing-background-neutralization/
```

Nothing is deleted.
