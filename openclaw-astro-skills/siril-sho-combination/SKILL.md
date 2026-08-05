---
name: siril-sho-combination
description: "Combine validated, aligned SII, Ha, and OIII masters into one linear SHO RGB FITS image using Siril."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril SHO Combination

Use this skill only when the user explicitly requests:

- `siril-sho-combination`
- SHO combination
- Hubble-palette composition
- combining aligned SII, Ha, and OIII masters into a color FITS image

This skill begins with the three validated outputs of
`siril-master-alignment` and ends with one validated, linear, three-channel
SHO FITS image.

The fixed channel mapping is:

- Red = SII
- Green = Ha
- Blue = OIII

This skill does not:

- run AstroProcessor
- rerun light-quality control
- rerun mono preprocessing
- rerun master alignment
- perform background extraction
- normalize or linearly match channel brightness
- remove stars
- denoise
- stretch the permanent FITS output
- reduce green
- adjust saturation
- color balance
- export a final PNG or TIFF
- overwrite or delete any file

# Fixed implementation

Use only:

    <this skill>/scripts/sho_combination.py

For CodeWarrior, run it with:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python

Do not write or substitute another composition script during an operational
run.

# Supporting execution rules

Read and follow the installed `siril-cli-runner` skill before execution.

The fixed helper enforces:

- Siril 1.4.4 through the approved AppRun
- a finite timeout
- an isolated attempt directory
- copied, checksum-verified inputs
- saved Siril scripts
- captured stdout and stderr
- immutable aligned masters
- no overwrite
- three-channel FITS validation
- numeric verification of the SHO channel mapping
- a structured stable manifest
- a separate AutoStretch evaluation preview

# Required upstream state

The exact project must contain a ready alignment manifest:

    <project>/processing/aligned/alignment-manifest.json

The helper verifies that:

- the manifest belongs to the exact project
- `sho_composition_permitted` is true
- aligned Ha, SII, and OIII paths are present
- their current SHA-256 checksums match the manifest
- all three files are finite monochrome FITS images
- all three dimensions match
- the alignment manifest itself has not changed during the run

For the current project the expected aligned inputs are:

    processing/aligned/aligned_Ha.fit
    processing/aligned/aligned_SII.fit
    processing/aligned/aligned_OIII.fit

Expected dimensions are currently 2908 × 2978, but the helper derives and
validates the dimensions from the files and manifest rather than hard-coding
them.

# Siril composition command

The helper creates verified private copies named:

    R_SII.fit
    G_Ha.fit
    B_OIII.fit

It runs this fixed Siril operation:

    rgbcomp "R_SII.fit" "G_Ha.fit" "B_OIII.fit" \
      -out=SHO_linear.fit -nosum

`-nosum` is intentional. The three filters can have different integration
times and stack counts, so the helper records per-channel exposure evidence in
its manifest instead of writing one potentially misleading summed exposure
value into the color image.

The permanent FITS output remains linear. The helper performs no histogram
matching, normalization, stretch, or color adjustment.

# Output locations

Every attempt is preserved under:

    <project>/.siril-sho-combination/<run-id>/

After all validation succeeds, the helper publishes:

    <project>/processing/sho/SHO-linear.fit
    <project>/processing/sho/sho-combination-manifest.json

The evaluation preview remains in the attempt directory:

    <attempt>/previews/SHO-linear-autostretch-preview.png

The preview uses linked AutoStretch only for visual review. It is not a final
image and does not alter `SHO-linear.fit`.

# Channel-mapping validation

A successful run requires more than a three-channel output.

The helper compares the output planes numerically:

- output red against aligned SII
- output green against aligned Ha
- output blue against aligned OIII

It records maximum absolute differences and requires each plane to match its
source within a strict floating-point tolerance.

This prevents an accidental channel-order swap from being published as a
valid SHO image.

# Existing-output boundary

If either stable output already exists:

    processing/sho/SHO-linear.fit
    processing/sho/sho-combination-manifest.json

do not overwrite it.

Run the helper `status` command and report the existing state. Do not rerun
automatically.

# Self-test

Before the first operational run in a session:

    ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
    HELPER="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-combination/scripts/sho_combination.py"

    "$ASTRO_PY" "$HELPER" self-test

The self-test creates and preserves a tiny synthetic Siril composition under:

    <workspace>/.skill-self-tests/siril-sho-combination/<run-id>/

It verifies:

- workspace derivation
- Siril 1.4.4
- exact fixed script construction
- actual Siril `rgbcomp` execution
- three-channel output validation
- SII→R, Ha→G, OIII→B mapping

Stop if the self-test does not report success.

# Run

For the current project:

    "$ASTRO_PY" "$HELPER" run \
      --project "M16 July 2026"

The helper:

1. derives the owning CodeWarrior workspace
2. resolves the exact project
3. validates the stable alignment manifest and all input checksums
4. creates a unique isolated attempt
5. copies the aligned masters into fixed RGB-role filenames
6. saves the exact Siril script
7. runs Siril with a finite timeout
8. validates the generated RGB FITS
9. numerically verifies all three channel mappings
10. atomically publishes the stable FITS without overwrite
11. creates a separate linked-AutoStretch preview
12. writes attempt and stable manifests
13. reports whether star-removal processing is permitted

# Status

Run:

    "$ASTRO_PY" "$HELPER" status \
      --project "M16 July 2026"

`ready` means:

- the stable manifest exists
- the recorded alignment manifest still matches
- all three aligned input checksums still match
- `SHO-linear.fit` exists and matches its recorded SHA-256
- the output is a finite three-channel FITS image
- output dimensions match the aligned inputs
- the current channel planes still validate as SII→R, Ha→G, OIII→B

# Failure behavior

On any failure:

1. stop immediately
2. preserve the attempt directory and all partial outputs
3. preserve scripts, logs, and result records
4. report the exact Siril command and exit status
5. report the first meaningful error
6. do not edit the helper
7. do not invent another Siril command
8. do not retry with different parameters
9. do not overwrite or delete anything
10. do not continue to star removal or later processing

# Result report

Report:

- exact project and project path
- helper and Siril versions
- alignment manifest path and SHA-256
- source path, dimensions, size, stack count, exposure evidence, and SHA-256
  for Ha, SII, and OIII
- fixed channel mapping
- attempt directory
- exact Siril command and script path
- stdout and stderr logs
- output dimensions, size, channel count, and SHA-256
- per-channel maximum absolute mapping differences
- preview path
- stable SHO manifest
- final `ready`, `failed`, `blocked`, or `needs_review` status
- whether the linear SHO image is ready for star removal

# Final rule

Create only the pure linear SHO composition.

Preserve every aligned source master.

Publish nothing unless the FITS structure, dimensions, checksums, and channel
mapping all validate together.
