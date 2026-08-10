---
name: siril-master-alignment
description: "Align verified Ha, SII, and OIII master stacks in Siril and crop them to their minimum common area."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Master Alignment

Use this skill only when the user explicitly requests alignment of completed
monochrome master stacks, invokes `siril-master-alignment`, or asks to align
Ha, SII, and OIII masters before SHO composition.

This skill begins with three completed monochrome master stacks and ends with
three validated, mutually aligned, common-cropped master FITS files.

It does not:

- run AstroProcessor project creation, copy, or prepare
- run light-frame quality control
- calibrate, register, or stack individual light frames
- combine SHO channels
- remove stars
- denoise
- stretch the permanent FITS outputs
- perform colour processing
- delete or overwrite any source or prior output

# Fixed implementation

Use only:

    <this skill>/scripts/master_alignment.py

For CodeWarrior, run it with:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python

Do not write a substitute alignment script during an operational run.

# Supporting execution rules

Read and follow the installed `siril-cli-runner` skill before execution.

The fixed helper implements the same runner safety contract:

- exact Siril 1.4.4 AppRun command
- finite timeout
- isolated attempt directory
- saved Siril scripts
- captured stdout and stderr
- immutable source masters
- no overwrite
- validated expected outputs
- structured result records
- evaluation previews separate from permanent FITS outputs

# Exact project name

Preserve the user-supplied AstroProcessor project name exactly.

For the current project use:

    M16 July 2026

Do not change it to `M 16 July 2026`.

# Expected inputs

Unless the user supplies explicit master paths, the helper discovers one
distinct master for each filter from the exact project:

- `processing/<filter>/result_<filter>_*.fit` — the normal output from
  Siril's Mono Preprocessing script
- `processing/result_<filter>_*.fit`
- or `processing/<filter>/result.fit`

When multiple paths contain identical content, the helper deduplicates them by
SHA-256. When distinct candidate contents exist for the same filter, stop and
ask the user which master is authoritative.

For the current project, expected discovery includes:

    processing/Ha/result_Ha_1800s.fit
    processing/SII/result_SII_720s.fit
    processing/OIII/result_OIII_1800s.fit

All three inputs must be:

- readable FITS files
- monochrome
- 32-bit or 16-bit numeric image data
- the same dimensions
- finite and non-empty
- inside the exact project root unless explicit external inputs were approved

# Alignment method

The helper follows Siril's documented workflow for aligning monochrome layers:

    register masters -2pass -transf=homography
    seqapplyreg masters -framing=min -interp=lanczos4 -prefix=aligned_

Siril writes the three transformed sequence members using names such as:

    aligned_masters_00001.fit
    aligned_masters_00002.fit
    aligned_masters_00003.fit

The helper must recognize that exact underscore-delimited naming convention
and map the files back to the original sequence order: Ha, SII, OIII.

The three copied inputs are ordered:

1. Ha
2. SII
3. OIII

Siril performs two-pass global star alignment and chooses the registration
reference from the sequence quality evidence. `-framing=min` crops all three
outputs to the area common to every channel, which is required before channel
composition.

The helper may use file-level sequence links inside its private attempt
directory because those links never change Siril's working-directory boundary.
It never creates directory symlinks and never points Siril at a shared
calibration directory.

# Output locations

Each run is preserved under:

    <project>/.siril-master-alignment/<run-id>/

Stable validated outputs are published to:

    <project>/processing/aligned/aligned_Ha.fit
    <project>/processing/aligned/aligned_SII.fit
    <project>/processing/aligned/aligned_OIII.fit
    <project>/processing/aligned/alignment-manifest.json

Evaluation previews are stored under the attempt directory. AutoStretch is
used only for those previews and never modifies the linear aligned FITS files.

# Existing-output boundary

If any stable aligned output or stable alignment manifest already exists:

- do not overwrite it
- run the helper `status` command
- report the existing state
- do not rerun automatically

A new alignment version requires an explicit development or archival decision.
Never delete an existing alignment result to make a rerun possible.

# Self-test

Before the first run in a session:

    ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
    HELPER="/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-master-alignment/scripts/master_alignment.py"

    "$ASTRO_PY" "$HELPER" self-test

Stop if it does not report success.

# Run

For the current project:

    "$ASTRO_PY" "$HELPER" run \
      --project "M16 July 2026"

The helper:

1. derives the owning CodeWarrior workspace
2. resolves the exact project
3. discovers and checksum-validates Ha, SII, and OIII masters
4. verifies matching mono dimensions
5. creates a unique isolated attempt
6. copies the three source masters without modifying them
7. saves the exact Siril alignment script
8. invokes Siril 1.4.4 with a finite timeout
9. requires exactly three aligned sequence outputs
10. validates identical aligned dimensions and finite data
11. publishes stable outputs atomically without overwrite
12. writes the stable and attempt manifests
13. creates separate autostretched previews
14. reports all evidence paths

# Status

Run:

    "$ASTRO_PY" "$HELPER" status \
      --project "M16 July 2026"

`ready` means:

- the stable alignment manifest exists
- all three source checksums still match the recorded inputs
- all three aligned outputs exist and match recorded checksums
- all aligned outputs have identical dimensions
- the outputs remain readable, finite monochrome FITS images

# Failure behavior

On any failure:

1. stop immediately
2. preserve the attempt directory
3. preserve scripts, stdout, stderr, and partial outputs
4. report the exact command and exit status
5. report the first meaningful error
6. do not edit the helper
7. do not invent another Siril command
8. do not retry with different parameters
9. do not overwrite or delete any file
10. do not proceed to SHO composition

# Result report

Report:

- exact project and project path
- helper and Siril versions
- source master path, filter, dimensions, size, and SHA-256
- attempt directory
- exact Siril command
- registration and common-crop script path
- stdout and stderr logs
- aligned output paths, dimensions, sizes, and SHA-256
- preview paths
- stable alignment manifest
- final `ready`, `failed`, `blocked`, or `needs_review` status
- whether SHO composition is permitted

# Final rule

Use only the fixed helper.

Preserve all original masters.

Publish nothing unless all three aligned outputs validate together.
