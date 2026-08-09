---
name: astro-processing
description: "Orchestrate a complete bounded SHO astrophotography workflow using AstroProcessor and Siril CLI."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

## Named stage request router — highest priority

This routing rule takes precedence over the full-pipeline instructions below.

When the user requests a **named installed processing stage** for an existing
project, for example:

```text
Process M16 July 2026 with SHO channel balance
Process M16 July 2026 with green reduction
Process M16 July 2026 with black point
```

that request is **not** permission to start, recreate, import, prepare, or
resume the complete pipeline.

For named-stage requests:

1. Identify the matching installed stage skill.
2. Read that stage skill's `SKILL.md`.
3. Hand control to that stage immediately.
4. Follow the stage skill's canonical project root and helper.
5. Do not execute earlier or later pipeline stages.

Canonical existing CodeWarrior projects root:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/Projects
```

Do not substitute:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/Projects
```

For a named-stage request, **never** run any AstroProcessor operation,
including `astroproc --help`, `-np`, `-c`, or `-p`; never inspect ASIAIR source
directories; never create/copy/prepare a project; and never rediscover the
project in alternate roots.

Explicit routing:

```text
SHO channel balance
→ siril-sho-channel-balance
→ /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance
```

Therefore the request:

```text
Process M16 July 2026 with SHO channel balance
```

must be handled only by `siril-sho-channel-balance`. The first processing
command for that request is its `advance --project "M16 July 2026"` entry
point. Do not run AstroProcessor first.

The full `astro-processing` workflow below applies only when the user actually
asks to process an unprocessed/raw dataset or requests the complete pipeline
from source/import onward.


# Siril CLI command

The approved Siril CLI command prefix is:

    env APPDIR="/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root" "/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun" siril-cli

Use this exact command prefix whenever this skill or the supporting
`siril-cli-runner` skill invokes Siril.

Before processing:

1. Confirm that `/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun` exists.
2. Confirm that it is executable.
3. Confirm that the command reports Siril 1.4.4.
4. Do not invoke `/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/usr/bin/siril-cli` directly.
5. Do not create a global `/usr/local/bin/siril-cli` command.
6. Do not copy, move, replace, or modify the Siril runtime during processing.

# AstroProcessor executable

The approved AstroProcessor executable is:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc

Use this exact absolute path for every AstroProcessor command.

Before using it:

1. Confirm that the file exists.
2. Confirm that it is executable.
3. Run its `--help` option to verify the installed interface.
4. Do not search for or create a global `/usr/local/bin/astroproc` command.
5. Do not copy, move, replace, or modify the executable during processing.

# Astro Processing

Use this skill when the user asks to process monochrome astronomical images
from an ASIAIR source into a completed SHO image.

Example request:

    Process my M 16 files found in Autorun in /mnt/asiair/emmc
    to a project named M16 July 2026.

This skill is the workflow orchestrator.

It is responsible for:

- interpreting the request
- running AstroProcessor commands
- organizing the processing project
- validating the input data
- deciding the Siril stage order
- choosing bounded parameter candidates
- requesting Siril execution through `siril-cli-runner`
- evaluating each candidate
- accepting checkpoints
- exporting the finished image
- writing the processing report

It must not bypass the supporting Siril execution skill.

# Required supporting skill

This workflow requires `siril-cli-runner`.

Before the first Siril operation:

1. Locate `siril-cli-runner` in OpenClaw’s available-skills list.
2. Read its current `SKILL.md`.
3. Follow its path validation, source protection, attempt isolation, execution,
   logging, timeout, overwrite prevention, and output verification rules.
4. Re-read it if OpenClaw shows that its content version has changed.
5. Use it for every Siril operation.

If `siril-cli-runner` is unavailable:

- do not execute Siril directly
- stop before the first Siril stage
- report the missing supporting skill

# Workflow order

Use this processing order:

1. Preflight validation.
2. Create the AstroProcessor project.
3. Copy the ASIAIR files.
4. Prepare the Siril folder structure.
5. Validate filters and calibration frames.
6. Calibrate, register, and stack each filter.
7. Collect, identify, and evaluate the Ha, SII, and OIII masters.
8. Align the three master images.
9. Crop them to a common valid region.
10. Perform the SHO channel combination.
11. Remove stars from the linear SHO image.
12. Apply linear noise reduction to the starless image.
13. Apply GHS pass 1 to the starless image.
14. Apply GHS pass 2 to the starless image.
15. Adjust the black point on the starless image.
16. Apply green reduction to the starless image.
17. Adjust saturation and bounded colour balance on the starless image.
18. Process the star image separately.
19. Recombine the stars and starless image.
20. Export the final PNG, TIFF, and processing-master FITS.
21. Write a processing report.

This order separates the stars immediately after the linear SHO combination so
all subsequent stretching, black-point, green-reduction, and colour processing
can be applied to the starless image without creating magenta or green stars.

# Prompt interpretation

Extract:

- target name
- project name
- source root
- ASIAIR source type
- requested processing palette
- optional parameter profile
- optional final filename
- optional special instructions

For the example request, derive:

- target: `M16`
- project name: `M16 July 2026`
- source root: `/mnt/asiair/emmc`
- source type: `autorun`
- palette: `SHO`
- profile: `m16-sho-starting-profile`

Do not remove spaces from the user’s display name merely because the project
folder is sanitized.

Allow `astroproc` to apply its documented project-name sanitization.

# Missing information

Ask only when an essential value cannot be safely derived.

Do not ask for values already present in the user’s request.

A target written as `M 16` and project name written as `M16 July 2026` should
not be treated as contradictory.

Default the palette to SHO only when Ha, SII, and OIII data are present and the
request clearly concerns narrowband SHO processing.

# General safety rules

- Never delete source images.
- Never move source images out of the ASIAIR source.
- Never alter files under `/mnt/asiair/emmc`.
- Never delete calibration directories.
- Never overwrite accepted checkpoints.
- Never delete a failed candidate.
- Never reuse a failed attempt directory.
- Never run more than one Siril job at once for the project.
- Never make an unlimited parameter search.
- Never modify more than one parameter family in a refinement attempt.
- Never conceal rejected frames or failed registrations.
- Never exclude a large portion of the data without review.
- Never treat an aesthetic preference as an objective fact.
- Never claim visual improvement when no image-inspection capability was used.
- Never install dependencies during processing.
- Never invent an `astroproc` option.
- Never invent a Siril command.
- Never continue past a failed required checkpoint.
- Never overwrite the original imported FITS files.
- Never replace the best accepted candidate with a worse candidate.

# AstroProcessor command policy

Use only options shown by the installed version of:

    astroproc --help

The known project command is conceptually:

    astroproc -np "<project-name>"

The known copy command is conceptually:

    astroproc -c "<project-name>" \
      -sd "<source-root>" \
      -t "<source-type>"

Before using them, confirm their exact syntax with `astroproc --help`.

For the folder-preparation stage, use only the preparation or sorting command
actually documented by the installed `astroproc`.

If AstroProcessor does not yet expose that command:

1. Stop after the copy stage.
2. Preserve the imported project.
3. Report the missing AstroProcessor capability.
4. Do not manually invent a replacement folder layout.
5. Do not continue to Siril.

# Project state

Maintain a durable state file inside the project:

    processing-state.json

It should contain:

- project name
- target
- source root
- source type
- pipeline
- profile
- project path
- current stage
- accepted checkpoints
- attempt counts
- parameter values
- warnings
- blocked reason
- output paths
- timestamps
- software versions

Example conceptual state:

    {
      "project": "M16 July 2026",
      "target": "M16",
      "pipeline": "SHO",
      "current_stage": 13,
      "stages": {
        "01-preflight": "complete",
        "02-create-project": "complete",
        "03-copy-files": "complete",
        "04-prepare-folders": "complete",
        "05-validate-inputs": "complete",
        "06-stack-ha": "complete",
        "06-stack-sii": "complete",
        "06-stack-oiii": "complete",
        "11-star-removal": "complete",
        "13-ghs-pass-1": "in_progress"
      }
    }

Write state changes atomically when supported.

On resume:

1. Read the state.
2. Verify every accepted checkpoint still exists.
3. Verify checksums when available.
4. Skip valid accepted stages.
5. Resume from the first incomplete stage.
6. Never silently replace a missing checkpoint.

# Recommended project structure

Use AstroProcessor’s actual generated structure.

The processing portion should provide the equivalent of:

    <project>/
      source/
      processing/
        Ha/
          biases/
          darks/
          flats/
          lights/
        SII/
          biases/
          darks/
          flats/
          lights/
        OIII/
          biases/
          darks/
          flats/
          lights/
      masters/
      aligned/
      intermediate/
      attempts/
      accepted/
      previews/
      output/
      reports/
      logs/
      processing-state.json

Do not force this exact layout when AstroProcessor uses a documented equivalent.

# Global bounded-optimization policy

Stages 6 through 19 require evaluation after the baseline candidate.

Use the following rules for every tunable stage.

## Maximum attempts

- maximum of 3 total candidates per stage
- attempt 1 is the baseline
- attempts 2 and 3 are refinements
- no fourth candidate without explicit user authorization
- no nested retry loop
- no background parameter search

Stage 6 applies the limit separately to Ha, SII, and OIII.

## Candidate ancestry

Every candidate for a stage must start from the same accepted parent
checkpoint.

Do not create attempt 3 from attempt 2 unless the stage explicitly requires a
sequential operation. Parameter comparisons must otherwise share the same
input.

## One-change rule

In each refinement attempt:

- change one parameter, or
- change one tightly related parameter family

Do not simultaneously change stretch strength, symmetry point, black point,
saturation, and colour balance.

A result produced by many simultaneous changes cannot be evaluated reliably.

## Best-candidate rule

After each candidate:

1. Validate technical success.
2. Generate a standardized preview.
3. Compare it with the parent and current best candidate.
4. Record metrics and visual observations.
5. Accept it only when it improves the intended stage objective without causing
   a hard regression.
6. Retain the previous best when the result is ambiguous.
7. Stop early when a refinement does not improve the current best.

## Hard regressions

Reject a candidate when it causes any of the following:

- new clipping beyond the stage’s limit
- missing image area
- invalid dimensions
- severe registration residuals
- obvious double stars
- dark halos or hard rims around stars
- crushed faint nebulosity
- blown star cores
- posterization
- colour-channel clipping
- waxy or painted fine structure
- increased background gradient
- invalid FITS output
- unmatched star and starless layers
- NaN or non-finite pixels
- failed output validation

## Evaluation evidence

For each attempt, record:

- input checkpoint
- parameters
- output path
- preview path
- clipping statistics when available
- background statistics when available
- noise estimate when available
- star statistics when relevant
- registration evidence when relevant
- visual observations
- acceptance or rejection
- reason
- exact Siril result record

When image-inspection capability is available, inspect:

- the full frame
- the target centre
- faint outer nebulosity
- background areas
- bright stars
- dense small-star fields
- high-contrast boundaries
- image corners
- a 100% crop

When image-inspection capability is unavailable:

- use deterministic metrics
- do not claim that a candidate “looks better”
- request human review when the choice is subjective

## Parameter movement

Do not jump directly from one extreme to another.

Move in the direction indicated by the observed problem and use the smallest
permitted step.

If a change worsens the result, return to the current best. Do not compensate
by making several unrelated changes.

# M16 SHO starting profile

Use this profile as a proven starting point for M16-like data, not as a
universal answer.

## Channel mapping

- red: SII
- green: Ha
- blue: OIII
- alignment reference: Ha
- output: 32-bit RGB FITS

## Balanced M16 Pixel Math starting expression

Use aligned variables:

- `S` for SII
- `H` for Ha
- `O` for OIII

Starting expressions:

- red: `S`
- green: `med(S) + 0.25 * (H - med(H))`
- blue: `med(S) + (O - med(O))`

Starting Ha scale:

- `g = 0.25`

Disable automatic result rescaling unless an approved workflow explicitly
requires it.

## StarNet

- approved script: `StarNet.py`
- source image is the accepted linear SHO combination
- linear-image option: enabled
- 2x upsampling: enabled
- custom stride: disabled
- preserve matched starless and star outputs from the same execution
- use the descreen star layer when produced

## Linear denoise

- remove salt-and-pepper noise: enabled
- independent channels: disabled
- secondary denoising: none
- modulation: `0.75`

## GHS pass 1

- colour model: even weighted luminance
- display stretch factor: `4.400`
- local stretch intensity B: `15.000`
- symmetry point SP: `0.00400`
- shadow protection LP: `0.00000`
- highlight protection HP: `0.86000`
- clip mode: RGB Blend

## GHS pass 2

- colour model: even weighted luminance
- display stretch factor: `0.831`
- local stretch intensity B: `4.000`
- symmetry point SP: `0.25636`
- shadow protection LP: `0.00000`
- highlight protection HP: `0.80000`
- clip mode: RGB Blend

## Black point

- mode: linear stretch or black-point shift
- black point: `0.24105`
- colour model: even weighted luminance
- clip mode: RGB Blend
- target clipping: `0.000%`

## Green reduction

- protection: Maximum Mask
- amount: `0.15`
- preserve lightness: enabled

## Saturation

- mode: global
- amount: `0.13`
- background factor: `2.00`

## Optional targeted red enhancement

Apply only when red structure remains materially weak after global saturation.

Starting values:

- red channel only
- GHS display stretch factor: `0.607`
- B: `6.000`
- SP: `0.32570`
- LP: `0.00000`
- HP: `0.80000`

Do not apply this automatically when the image already has balanced red detail.

## Star layer

- starting star saturation: `0.90` to `1.00`
- starting star scale: `0.55`
- recomposition: `starless + stars * scale`

# Stage 1 — Preflight validation

## Actions

1. Parse the user request.
2. Normalize the ASIAIR source type to lowercase.
3. Confirm that the source root exists.
4. Confirm that the requested ASIAIR subfolder exists.
5. Confirm that it is readable.
6. Confirm that `astroproc` exists.
7. Confirm that `siril-cli` exists.
8. Record both versions.
9. Confirm that `siril-cli-runner` is available.
10. Confirm adequate free storage when storage information is available.
11. Confirm that no active run is already writing to the same project.
12. Record the initial processing plan.

## Stop conditions

Stop when:

- the source directory is missing
- the requested ASIAIR folder is missing
- the source is not readable
- either required command is unavailable
- the supporting skill is unavailable
- the requested project conflicts with an active run

Do not create the project until preflight passes.

# Stage 2 — Create the AstroProcessor project

## Actions

1. Run `astroproc --help`.
2. Confirm the new-project syntax.
3. Run the supported new-project command.
4. Capture the resulting project path.
5. Confirm that the project exists.
6. Confirm that it is inside AstroProcessor’s approved Projects root.
7. Initialize the processing state.
8. Record the exact command and output.

## Existing project

When the project already exists:

- do not overwrite it
- inspect its state
- resume only when it is the intended project
- stop for review if its source or target conflicts with the request

# Stage 3 — Copy the ASIAIR files

## Actions

1. Confirm the copy syntax with `astroproc --help`.
2. Supply the project name, source root, and ASIAIR source type.
3. Run the supported copy command.
4. Capture the import report.
5. Count imported files.
6. Record source and destination paths.
7. Verify that source files still exist.
8. Confirm that the operation copied rather than moved the files.
9. Preserve rejected or unclassified-file reports.

## Failure conditions

Stop when:

- no files are imported
- the source type was not found
- source files were moved or altered
- AstroProcessor reports a partial import without an explainable reason
- the project cannot be resolved after import

# Stage 4 — Prepare the Siril folder structure

## Actions

1. Inspect `astroproc --help`.
2. Locate the documented prepare, classify, organize, or equivalent command.
3. Run it against the project.
4. Require separate Ha, SII, and OIII processing roots.
5. Require the exact Siril folder names:
   - `lights`
   - `darks`
   - `flats`
   - `biases`
6. Keep each filter’s flats with that filter.
7. Record every classification.
8. Preserve unclassified files without deleting them.
9. Generate a preparation report.

## Stop condition

When AstroProcessor lacks this capability, stop and report the missing feature.

Do not manually invent a replacement organization.

# Stage 5 — Validate filters and calibration frames

This stage is a data-quality gate. It does not process the images.

## FITS readability

For every imported FITS file:

- confirm that it opens
- confirm that the header is readable
- record image dimensions
- record bit depth when available
- record camera
- record frame type
- record filter
- record exposure
- record gain
- record offset
- record binning
- record temperature
- record observation time
- record readout mode when available

Flag files with missing or contradictory metadata.

## Filter validation

Require:

- an Ha light group
- an SII light group
- an OIII light group
- filter-specific flats for each group

Do not infer a filter solely from a folder name when the FITS header contradicts
it.

Normalize harmless naming variants such as:

- `Ha`
- `H-alpha`
- `Halpha`

and:

- `OIII`
- `O3`

and:

- `SII`
- `S2`

Record the original header value.

## Light-frame consistency

Within each filter group, verify compatibility of:

- camera
- image dimensions
- binning
- gain
- offset
- exposure class
- readout mode

Separate materially different exposure groups rather than silently combining
them.

Exclude previews, focus frames, test exposures, old stacks, and already
processed images from the light sequence.

Do not delete excluded files.

## Dark matching

Darks should match their lights in:

- camera
- dimensions
- binning
- gain
- offset
- exposure
- readout mode

Temperature should match closely enough for the camera and workflow.

Use the exact configured tolerance when AstroProcessor provides one.

Do not automatically approve a materially mismatched dark merely because no
better dark exists.

## Flat matching

Flats should match:

- camera
- dimensions
- binning
- gain when required
- offset when required
- filter
- optical configuration represented by the dataset

Never mix Ha, SII, and OIII flats.

Check that flat median values are plausible and that the files are not
completely dark, saturated, or corrupt when statistics are available.

## Bias matching

Biases should match:

- camera
- dimensions
- binning
- gain
- offset
- readout mode when relevant

If the installed approved workflow uses synthetic bias, record the exact
formula and do not silently substitute it.

## Counts

Report counts for every group:

- Ha lights
- Ha flats
- SII lights
- SII flats
- OIII lights
- OIII flats
- darks
- biases
- rejected
- unclassified

## Approval decision

Pass Stage 5 only when:

- all three light groups are present
- all required calibration groups are present
- frames are readable
- mappings are unambiguous
- no critical calibration mismatch remains
- the folder structure is valid

When mismatches remain:

1. Stop before Siril calibration.
2. Preserve the validation report.
3. Explain the exact mismatch.
4. Request review.
5. Do not compensate by changing stacking parameters.

# Stage 6 — Calibrate, register, and stack each filter

Process Ha, SII, and OIII independently.

Siril’s built-in monochrome preprocessing workflow expects each filter root to
contain `lights`, `darks`, `flats`, and `biases`.

## Baseline attempt

For each filter:

1. Set the working directory to the filter root.
2. Use the approved Mono Preprocessing script.
3. Execute it through `siril-cli-runner`.
4. Preserve all logs.
5. Verify the calibrated sequence.
6. Verify the registered sequence.
7. Verify the final stack.
8. Record accepted and skipped frame counts.
9. Generate a standardized AutoStretch preview for evaluation.
10. Keep the master linear.

## Evaluation

Evaluate:

- calibration artifacts
- dark over-subtraction
- flat-field correction
- vignetting
- amp glow
- hot and cold pixels
- registered-frame completeness
- star alignment
- doubled stars
- stack clipping
- background noise
- gradients
- retained integration time
- target framing
- faint signal visibility under the standardized preview

## Guardrails

Maximum 3 candidates per filter.

Automatic exclusion is limited to the smaller of:

- 10% of the filter’s usable lights
- 5 light frames

Never reduce a stack below 20 lights automatically.

If more frames appear defective, stop for review.

Do not change calibration-frame mapping during a stacking refinement. Return to
Stage 5 when calibration mapping is wrong.

Allow only one bounded refinement family per attempt, such as:

- documented registration fallback
- documented frame-quality threshold
- documented stack-rejection parameter

Do not change registration method, rejection thresholds, normalization, and
calibration mapping simultaneously.

Do not keep increasing rejection merely to make a smoother-looking stack.

Retained integration and real faint signal take priority over cosmetic
smoothness.

# Stage 7 — Collect, identify, and evaluate the masters

## Actions

1. Locate the successful Ha result.
2. Locate the successful SII result.
3. Locate the successful OIII result.
4. Copy or publish them into the project’s masters area without deleting the
   originals.
5. Give them unambiguous names.
6. Record their dimensions, bit depth, channels, exposure, and checksum.
7. Verify that all three are linear.
8. Generate identical evaluation previews.

Recommended names:

- `master-Ha.fit`
- `master-SII.fit`
- `master-OIII.fit`

## Evaluation

Confirm:

- the target appears in all three
- the orientation is plausible
- stars are present
- there is no severe calibration defect
- no channel is accidentally a duplicate of another
- no channel was swapped
- the dimensions are compatible
- relative noise differences are documented

This stage does not tune the masters directly.

When a master is unacceptable, return only that filter to Stage 6. Do not
modify it in Stage 7.

# Stage 8 — Align the three masters

Use Ha as the default reference because it is usually the strongest channel in
this workflow.

## Baseline

1. Preserve the three original masters.
2. Align SII to Ha.
3. Align OIII to Ha.
4. Use a documented deep-sky registration method.
5. Save new aligned monochrome outputs.
6. Do not combine colour yet.

## Evaluation

Inspect:

- target overlap
- star-centre residuals
- doubled or elongated stars
- rotation
- scale
- distortion
- registration completeness
- border loss

## Guardrails

Maximum 3 candidates.

Try the preferred global or deep-sky method first.

Permit only one documented fallback registration model.

Do not cycle repeatedly among registration methods.

Do not use a large crop to hide failed registration.

Reject any candidate with obvious double stars or materially worse star
residuals than the current best.

If no method aligns all channels reliably, stop for review.

# Stage 9 — Crop to a common valid region

This is a logical checkpoint even when the alignment tool calculates the common
area during Stage 8.

## Actions

1. Determine the intersection valid in Ha, SII, and OIII.
2. Crop all three to exactly the same rectangle.
3. Confirm identical dimensions.
4. Save separate common-crop outputs.
5. Preserve the uncropped aligned images.

## Evaluation

Confirm:

- no invalid registration borders remain
- all three images have identical dimensions
- the target is not unnecessarily truncated
- important nebular structure remains
- excessive field area was not discarded

## Guardrails

Use the smallest crop that removes invalid borders.

If the common crop removes more than 20% of the original image area, stop for
review rather than accepting it automatically.

Do not make an aesthetic crop at this stage.

Do not use cropping to conceal channel misalignment.

# Stage 10 — SHO channel combination

## Baseline

Map:

- red = SII
- green = Ha
- blue = OIII

For the M16 profile, start with:

- red: `S`
- green: `med(S) + 0.25 * (H - med(H))`
- blue: `med(S) + (O - med(O))`

Save a new 32-bit linear RGB FITS checkpoint.

Do not permanently stretch the individual channel masters.

## Evaluation

Under a standardized preview, inspect:

- background-channel balance
- excessive green dominance
- magenta background
- clipped channels
- nebular structure in all channels
- amplified OIII noise
- colour discontinuities
- correct filter mapping

## Guardrails

Maximum 3 candidates.

For the Ha coefficient:

- starting value: `0.25`
- maximum per-attempt change: `0.05`
- automatic bounds: `0.15` to `0.40`

For channel normalization factors:

- maximum per-attempt change: 15%
- maximum cumulative change from baseline: 30%

Do not change multiple channel factors and the combination expression in one
attempt.

Do not force a neutral-grey nebula. SHO colour is synthetic.

Do not brighten a weak channel so aggressively that its noise dominates.

Ideal clipping is zero. Reject a candidate with meaningful new channel
clipping.

Save the accepted linear SHO image as a major restart checkpoint.

# Stage 11 — Star removal

Use the approved installed `StarNet.py` Siril Python script immediately after
the accepted linear SHO combination and before any denoising, stretching,
black-point, green-reduction, or saturation processing.

## Baseline

- source image is the accepted linear SHO image from Stage 10
- linear-image option: enabled
- 2x upsampling: enabled
- custom stride: disabled
- preserve the descreen star layer when available

Produce:

- linear starless image
- isolated star image
- StarNet log
- matched-pair record

## Matched-pair rule

The starless image and star image must come from:

- the same input checkpoint
- the same StarNet run
- the same parameters

Never combine a starless image from one run with stars from another.

## Evaluation

Inspect:

- residual stars in the starless image
- missing nebular knots
- dark holes
- bright-star halos
- hard rims
- star-layer background leakage
- completeness of small stars
- dense-field quality
- recomposition plausibility

## Guardrails

Maximum 3 candidates.

Try the approved M16 settings first.

Permit only one bounded comparison such as:

- 2x upsampling enabled versus disabled
- one documented stride adjustment when the default produces tiling artifacts

Do not test many stride values.

Do not accept a cleaner starless image when it removes real compact nebular
structure.

Do not continue unless both matched outputs are validated.

# Stage 12 — Linear noise reduction

Apply noise reduction to the accepted linear starless image before permanent
stretching.

## Baseline

Use:

- salt-and-pepper correction: enabled
- independent channels: disabled
- secondary algorithm: none
- modulation: `0.75`

## Evaluation

Inspect at 100%:

- background smoothness
- fine nebular filaments
- dark structures
- residual star artifacts
- edges around the Pillars or equivalent structures
- blockiness
- waxy texture
- painted appearance
- colour blotching

## Guardrails

Maximum 3 candidates.

Modulation:

- starting value: `0.75`
- maximum step: `0.10`
- automatic range: `0.50` to `0.85`

Keep independent-channel processing disabled unless there is clear,
documented channel-specific colour noise.

Do not introduce a secondary denoising algorithm automatically.

When the result looks waxy or damages fine structure, reduce modulation.

When the result remains noisy but preserves structure, increase modulation by
no more than `0.10`.

Prefer slight remaining noise over destroyed detail.

# Stage 13 — GHS pass 1

The first pass should reveal most of the starless nebula while leaving it
subdued.

## Baseline

Use:

- colour model: even weighted luminance
- display stretch factor: `4.400`
- B: `15.000`
- SP: `0.00400`
- LP: `0.00000`
- HP: `0.86000`
- clip mode: RGB Blend

## Evaluation

Judge the permanent result in Linear display mode.

Inspect:

- visibility of the faint signal
- highlight control
- background elevation
- colour preservation
- clipped pixels
- strong structures
- faint outer emission
- residual StarNet artifacts

## Guardrails

Maximum 3 candidates.

Per-attempt limits:

- stretch factor: maximum change `0.40`
- B: maximum change `2.0`
- SP: maximum change `0.002`
- HP: maximum change `0.04`

Automatic ranges:

- stretch factor: `3.60` to `5.20`
- B: `12.0` to `15.0`
- SP: `0.002` to `0.008`
- HP: `0.80` to `0.92`
- LP remains `0.0` unless a documented problem justifies changing it

Change only one parameter family per refinement.

Keep clipping at `0.000%` whenever possible.

Do not attempt to reach final brightness in pass 1.

# Stage 14 — GHS pass 2

Pass 2 shapes the starless nebular midtones and reveals faint outer structure.

## Baseline

Use:

- colour model: even weighted luminance
- display stretch factor: `0.831`
- B: `4.000`
- SP: `0.25636`
- LP: `0.00000`
- HP: `0.80000`
- clip mode: RGB Blend

## Evaluation

Inspect:

- midtone separation
- faint outer nebulosity
- contrast in dark structures
- background brightness
- flattened regions
- highlight compression
- retained colour
- clipping

For M16, the Pillars should remain darker than the surrounding emission.

## Guardrails

Maximum 3 candidates.

Per-attempt limits:

- stretch factor: maximum change `0.20`
- B: maximum change `1.0`
- SP: maximum change `0.03`
- HP: maximum change `0.05`

Automatic ranges:

- stretch factor: `0.40` to `1.50`
- B: `2.0` to `6.0`
- SP: `0.18` to `0.35`
- HP: `0.70` to `0.90`
- LP remains `0.0` by default

When the sky turns medium grey or faint structures flatten into the
background, reduce the stretch rather than compensating with a severe black
point later.

# Stage 15 — Black-point adjustment

Darken the raised background of the starless image without crushing faint
emission.

## Baseline

Use:

- linear stretch or black-point shift
- black point: `0.24105`
- even weighted luminance
- RGB Blend
- clipping target: `0.000%`

## Evaluation

Inspect:

- faint dust
- outer nebulosity
- dark structures
- background neutrality
- black clipping
- residual artifacts
- image corners

Aim for dark charcoal rather than absolute black.

## Guardrails

Maximum 3 candidates.

Black point:

- starting value: `0.24105`
- maximum per-attempt change: `0.015`
- automatic range: `0.18` to `0.28`

Change only the black point during this stage.

Reject a candidate that erases faint surrounding signal.

Do not accept clipping merely to produce a visually black background.

# Stage 16 — Green reduction

Apply green reduction only to the accepted starless image.

## Baseline

Use:

- method: subtractive chromatic green-noise reduction
- protection: Maximum Mask
- amount: `0.15`
- preserve lightness: enabled

## Evaluation

Inspect:

- reduction of unwanted green cast
- retained green/cyan nebular detail
- transition smoothness
- neutral background
- colour noise
- new magenta areas
- luminance preservation

## Guardrails

Maximum 3 candidates.

Amount:

- starting value: `0.15`
- maximum step: `0.05`
- automatic range: `0.05` to `0.25`

Keep Maximum Mask and preserve-lightness enabled during automatic refinements.

Do not exceed `0.25` automatically.

Do not attempt to remove all green. Real SHO structure may legitimately remain
green or cyan.

Reject a candidate that creates obvious magenta contamination.

# Stage 17 — Saturation and bounded colour balance

Apply saturation to the accepted green-reduced starless image.

## Baseline

Use:

- global saturation amount: `0.13`
- background factor: `2.00`

## Evaluation

Inspect:

- colour separation in the nebula
- channel clipping
- colour noise
- oversaturated highlights
- unnatural hard colour boundaries
- background colour
- preserved luminance detail

## Guardrails

Maximum 3 candidates for global saturation.

Saturation amount:

- starting value: `0.13`
- maximum step: `0.05`
- automatic range: `0.00` to `0.30`

Background factor:

- starting value: `2.00`
- automatic range: `1.50` to `2.00`
- do not change it in the same refinement that changes saturation amount

Reject candidates with clipped colour channels or amplified chromatic noise.

## Optional targeted red enhancement

Apply at most one targeted red-enhancement pass when:

- red structure remains materially weak
- the global saturation result is otherwise acceptable
- the operation does not distort stars because the image is starless

Starting values:

- red channel only
- display stretch factor: `0.607`
- B: `6.000`
- SP: `0.32570`
- LP: `0.00000`
- HP: `0.80000`

Maximum one refinement candidate.

Per-attempt limits:

- stretch factor change: `0.15`
- B change: `1.0`
- SP change: `0.03`
- HP change: `0.05`

Skip targeted red enhancement when the baseline already has adequate red
structure.

# Stage 18 — Process the star image separately

Use the matched star layer from Stage 11.

## Objectives

- preserve natural-looking star colour
- control star dominance
- avoid hard black rims
- avoid blown cores
- avoid enlarged stars
- remove inappropriate background leakage

## Baseline

Start with:

- saturation between `0.90` and `1.00`
- conservative star stretch
- neutral or minimal black-point change
- no star scaling yet

Save a separately processed star checkpoint.

## Evaluation

Inspect:

- bright-star cores
- small-star completeness
- colour neutrality
- orange or purple casts
- halos
- hard edges
- star-layer background
- relative brightness distribution

## Guardrails

Maximum 3 candidates.

Saturation:

- starting value: `0.90`
- maximum step: `0.05`
- automatic range: `0.80` to `1.00`

Star black-point adjustment:

- maximum step: `0.01`
- maximum cumulative automatic change: `0.02`

Do not apply strong denoising to the star layer.

Do not aggressively sharpen stars.

Do not clip star cores.

Do not force all stars to pure white.

# Stage 19 — Recombine stars and starless image

Use only the matched and accepted starless and star checkpoints.

## Baseline

Start with:

    starless + stars * 0.55

Disable automatic rescaling unless the verified recomposition method requires
it.

## Evaluation

Inspect:

- star dominance
- bright-star halos
- hard rims
- black outlines
- star colours
- small-star visibility
- nebular contrast
- transitions around bright stars
- overall balance at full-frame and 100% scale

## Guardrails

Maximum 3 candidates.

Star scale:

- starting value: `0.55`
- maximum step: `0.05`
- automatic range: `0.40` to `0.70`

Do not change star scale and star black point in the same refinement attempt.

Reject a candidate when stars dominate the nebula or appear unnaturally
suppressed.

Reject a candidate with dark rings, hard cores, or layer mismatch.

Do not combine unrelated StarNet outputs.

When no candidate is clearly superior, retain `0.55`.

# Stage 20 — Export final products

From the accepted recomposed image, produce:

- 32-bit FITS processing master
- 16-bit TIFF display file
- PNG display preview
- final standardized QA preview

Use clear names derived from the project name.

Example:

- `M16-July-2026-SHO-final.fit`
- `M16-July-2026-SHO-final.tif`
- `M16-July-2026-SHO-final.png`

## Validation

Confirm:

- all files exist
- all files are non-empty
- the FITS file reopens
- the TIFF and PNG dimensions are correct
- the orientation is correct
- the colour channels are present
- the exported image is not unexpectedly dark
- output checksums are recorded

Do not overwrite an earlier final export. Use a version suffix when necessary.

# Stage 21 — Write the processing report

Create a human-readable Markdown report and a structured JSON report.

Include:

- project name
- target
- source location
- source type
- processing date
- AstroProcessor version
- Siril version
- supporting skill version when available
- imported-file counts
- validation results
- calibration matching summary
- rejected and skipped frames
- retained frames
- retained integration per filter
- master paths
- alignment method
- crop dimensions
- SHO expressions
- every parameter candidate
- accepted candidate for every stage
- rejection reasons
- warnings
- StarNet parameters
- matched star-layer identities
- final recomposition scale
- final output paths
- checksums
- incomplete or uncertain findings

Include a compact stage table containing:

- stage
- status
- attempts
- accepted output
- accepted parameters
- evaluation summary

Do not describe a subjective choice as objectively optimal.

# Completion response

Report completion only when all required stages are accepted and all final
outputs validate.

Provide:

- project path
- final FITS path
- final TIFF path
- final PNG path
- report path
- retained integration per filter
- number of attempts per tuned stage
- important warnings

When processing stops:

- identify the blocked stage
- identify the exact reason
- provide the relevant report or log path
- preserve all completed checkpoints
- do not restart automatically

# Final operating principle

Use AstroProcessor for project creation, import, classification, and folder
preparation.

Use `astro-processing` for orchestration, bounded parameter selection,
evaluation, checkpointing, and reporting.

Use `siril-cli-runner` for safe Siril execution and verification.

Use `.ssf` or approved Siril Python scripts for deterministic image operations.

Never sacrifice source preservation, reproducibility, or bounded execution in
pursuit of a marginally better-looking image.
