---
name: siril-sho-combination
description: "Combine validated mono-linear-denoise outputs into a deterministic linear SHO RGB FITS image, then permit background neutralization as the next stage."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril SHO Combination

Installed helper version: **1.1.1**.

This stage consumes the ready canonical outputs of
`siril-mono-linear-denoise`.

The fixed mapping is:

```text
Red   = SII
Green = Ha
Blue  = OIII
```

## Correct pipeline boundary

The required order is:

```text
siril-mono-linear-denoise
→ siril-sho-combination
→ siril-background-neutralization
→ siril-starnet-removal
```

A ready SHO manifest must therefore report:

```text
stage_order.upstream: siril-mono-linear-denoise
stage_order.current: siril-sho-combination
stage_order.downstream: siril-background-neutralization
background_neutralization_permitted: true
star_removal_permitted: false
```

The SHO stage never directly permits StarNet.

## Upstream inputs

Only these canonical files are permitted:

```text
processing/mono-linear-denoise/mono-linear-denoise-manifest.json
processing/mono-linear-denoise/denoised_Ha.fit
processing/mono-linear-denoise/denoised_SII.fit
processing/mono-linear-denoise/denoised_OIII.fit
processing/mono-linear-denoise/visual-review-record.json
```

The helper validates the upstream manifest and evidence internally. CodeWarrior
must not read, print, or paste the full upstream manifest into chat. Use the
helper's compact `status` or `run` output.

## Composition

The permanent output remains linear:

```text
rgbcomp "R_SII.fit" "G_Ha.fit" "B_OIII.fit" \
  -out=SHO_linear.fit -nosum
```

No background neutralization, colour adjustment, stretch, StarNet, or export is
performed by this skill.

## Canonical outputs

```text
processing/sho/SHO-linear.fit
processing/sho/sho-combination-manifest.json
```

The helper verifies SII→R, Ha→G, and OIII→B numerically.

## Manifest-only migration from 1.1.0

The existing M16 helper-1.1.0 SHO image is valid. Only its downstream contract
is wrong.

Use:

```bash
sho_combination.py migrate-contract \
  --project "M16 July 2026"
```

Migration:

- validates the current mono-linear-denoise evidence;
- validates the current SHO checksum, format, and channel mapping;
- preserves the complete previous 1.1.0 manifest;
- atomically updates only the canonical manifest;
- does not rerun `rgbcomp`;
- does not alter `SHO-linear.fit`;
- verifies final status.

The previous manifest is preserved under:

```text
.siril-sho-combination/contract-migration-<id>/
```

## New combination runs

Future replacement runs publish the corrected contract directly:

```bash
sho_combination.py run \
  --project "<project>" \
  --fresh-run \
  --timeout 1800
```

Previous canonical SHO directories remain preservation-safe.

## Status

A ready 1.1.1 status requires:

```text
status: ready
background_neutralization_permitted: true
star_removal_permitted: false
errors: []
```

The status output includes only a compact upstream summary. Do not open the
full mono-linear-denoise manifest manually.

## Self-test

```bash
sho_combination.py self-test --timeout 1800
```

The synthetic self-test verifies:

- mono-linear-denoise 1.0.3 input validation;
- real Siril `rgbcomp`;
- fixed channel mapping;
- corrected background-neutralization contract;
- helper-1.1.0 manifest-only migration;
- unchanged SHO image checksum during migration;
- post-migration status verification.

It does not modify M16.
