---
name: siril-linear-denoise
description: "Apply deterministic Siril NL-Bayes noise reduction to the canonical linear StarNet starless image."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Linear Denoise

Use this skill after the canonical `siril-starnet-removal` stage reports:

```text
status: ready
starless_background_processing_permitted: true
```

Run only:

```text
<this skill>/scripts/linear_denoise.py
```

with:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
```

Installed helper version: **1.0.1**.

## Canonical input

```text
<project>/processing/starnet/SHO-starless-linear.fit
```

The helper verifies this file against:

```text
<project>/processing/starnet/starnet-manifest.json
```

It refuses to proceed unless the StarNet status is `ready`, starless background
processing is permitted, and the source SHA-256 matches the manifest.

## Denoise method

The fixed Siril command is:

```text
denoise -mod=0.75
```

This means:

- NL-Bayes
- modulation 0.75
- Cosmetic Correction enabled
- no Anscombe VST
- no DA3D
- no SOS
- joint RGB processing unless Siril itself reports a failure

Do not change these settings automatically. They reproduce Peter's previously
successful M16 linear-denoise configuration.

## Canonical outputs

```text
<project>/processing/linear-denoise/SHO-starless-linear-denoised.fit
<project>/processing/linear-denoise/SHO-starless-linear-before-linked.png
<project>/processing/linear-denoise/SHO-starless-linear-denoised-linked.png
<project>/processing/linear-denoise/linear-denoise-manifest.json
```

## Fresh-run behavior

If `processing/linear-denoise` exists, `run` refuses to reuse it as though a new
run occurred.

Use `--fresh-run` to execute NL-Bayes again. The previous canonical directory
is retained in the new evidence run as:

```text
previous-processing-linear-denoise/
```

The previous directory remains in place until the new result passes every
quality safeguard. Publication then uses an atomic directory rename. Nothing
is deleted.

## Quality safeguards

The helper verifies:

- same 32-bit RGB dimensions as the source
- all output values are finite
- source and output are not identical
- global correlation remains at least 0.995
- relative RMS change is neither zero nor excessive
- median and bright-detail levels remain stable
- the robust background-noise proxy does not increase materially
- before and after linked previews exist

Failed candidates remain under:

```text
<project>/.siril-linear-denoise/
```

and do not replace the canonical result.

## Commands

```bash
ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
DENOISE="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-linear-denoise/scripts/linear_denoise.py"

"$ASTRO_PY" "$DENOISE" --version
"$ASTRO_PY" "$DENOISE" self-test --timeout 1800

"$ASTRO_PY" "$DENOISE" run   --project "M16 July 2026"   --fresh-run   --timeout 7200

"$ASTRO_PY" "$DENOISE" status   --project "M16 July 2026"
```

## Prohibited actions

Do not:

- process the stellar mask or unscreen stars in this stage
- denoise the original SHO image instead of the canonical starless file
- enable VST, DA3D, SOS or `-indep` automatically
- change modulation from 0.75
- stretch the FITS output
- overwrite an existing canonical result without `--fresh-run`
- delete previous results or evidence
- proceed when the final status is not `ready`


## Installer self-test versus project acceptance

The real synthetic installer test verifies that Siril NL-Bayes executes and
returns a structurally valid changed FITS file with previews. Synthetic data
does not have to pass every production astrophotography threshold.

Real project publication remains blocked unless the complete production
quality assessment is satisfactory.
