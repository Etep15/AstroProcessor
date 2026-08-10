---
name: siril-mono-linear-denoise
description: "Default mono denoise to pass-through and publish NL-Bayes only when conservative metrics and explicit three-view visual comparison prove a material improvement."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Mono Linear Denoise

Installed helper version: **1.0.3**.

This stage remains between `siril-mono-background-cleanup` and
`siril-sho-combination`, but denoising is optional.

## Core decision rule

```text
When denoise is not clearly and materially better, select pass-through.
```

Candidate set remains:

```text
candidate-00: exact pass-through source
candidate-01: denoise -mod=0.20
candidate-02: denoise -mod=0.40
```

Candidate-00 is the default recommendation. A lower numerical noise proxy does
not justify selecting denoise.

## Material-improvement gate

A non-pass-through candidate may be selected only when all conditions hold:

- it is technically satisfactory;
- it clears the conservative metric materiality gate;
- CodeWarrior finds visible improvement in at least two of `full`,
  `background`, and `detail` contacts;
- no contact view is worse;
- background naturalness is better;
- detail preservation is the same or better;
- the benefit is specific, visible, and documented.

Close, mixed, ambiguous, indistinguishable, or merely different results must
select candidate-00.

## Metric gate

A denoise candidate must meet every conservative threshold, including:

```text
noise_ratio <= 0.70
global_correlation >= 0.9999
detail_correlation >= 0.9995
relative_rms_change <= 0.008
0.995 <= p99_retention <= 1.005
texture_correlation_increase <= 0
residual_texture_correlation <= 0.12
```

This gate is intentionally strict. Later denoising can be performed on the
combined starless SHO image, where colour relationships and faint structures
can be judged together.

## Structured review

The version-1.0.3 review template requires, for every candidate:

- `accepted`;
- `material_improvement`;
- `improved_views`;
- `worse_views`;
- `indistinguishable_views`;
- `background_naturalness`;
- `detail_preservation`;
- artifact flags;
- a specific benefit description;
- detailed observations.

The helper rejects unsupported confidence terms including `optimal`,
`maximum`, `necessary`, `best`, `perfect`, and `superior`.

## Autonomous flow

```text
run
→ CodeWarrior opens all nine contact previews
→ complete review schema 2
→ record-review
→ publish
→ automatic status verification
```

Do not ask Peter or ChatGPT to select candidates.

## Existing output

Any canonical result created by helper 1.0.2 or older becomes `obsolete` after
installation and cannot permit SHO combination. A successful replacement
preserves the old canonical directory under the new run.
