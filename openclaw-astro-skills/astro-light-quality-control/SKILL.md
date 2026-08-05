---
name: astro-light-quality-control
description: "Analyze prepared light frames, review deterministic evidence groups, and move only checksum-verified reviewed rejects."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Astro Light Quality Control

Use this skill only when the user explicitly requests light-frame quality
control or invokes `astro-light-quality-control` by name.

This skill is independent. It does not run `astroproc`, prepare projects, copy
files, or run Mono Preprocessing.

It analyzes the direct FITS files in:

    <agent workspace>/Projects/<project>/processing/<filter>/lights

and may move definite rejects to:

    <agent workspace>/Projects/<project>/processing/<filter>/lights/rejects

# Fixed helper

The only approved implementation is:

    <this skill directory>/scripts/quality_control.py

Run it with the AstroProcessor virtual-environment Python:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python

When this skill is installed for another agent on the same host, continue to
use that verified Python environment unless an approved replacement containing
Astropy and NumPy is documented.

The helper derives the owning agent workspace from its installed path:

    <workspace>/skills/astro-light-quality-control/scripts/quality_control.py

# Non-negotiable execution boundary

During a quality-control run, the agent must not:

- write another analysis program
- edit or repair `quality_control.py`
- create substitute shell or Python scripts
- rewrite Siril command construction
- switch to a representative sample
- inspect only a subset of the lights
- invent alternate analysis methods
- automatically retry using different code
- move files by hand
- delete files by hand
- invoke `mv`, `rm`, `unlink`, or `shutil.move` outside the helper
- promise background work after a failed command

If the helper fails, stop and report:

- the exact helper command
- exit status
- stdout
- stderr
- result paths created before failure

Do not fix anything during the operational run.

Code changes belong in a separate development request.

# Exact project name

Preserve the exact project name supplied by the user.

Use:

    M16 July 2026

not:

    M 16 July 2026

Do not infer a project name from the ASIAIR source-project name or a similarly
named folder.

# Filters

Supported selections are:

- Ha
- SII
- OIII
- all

Harmless aliases may be normalized by the helper.

# Prepared-path requirement

The project must already be prepared.

The skill does not run:

    astroproc -p

The prepared light path must exist:

    <workspace>/Projects/<exact project>/processing/<filter>/lights

The `lights` entry may be a real directory or a directory symbolic link. The
helper operates through this prepared path. A resolved target may appear in
reports, but it is not substituted as the operating path.

If the prepared path is missing, stop and tell the user to run project
preparation separately.

Do not inspect darks, flats, or biases.

# Deterministic two-phase design

Quality control has two separate phases:

1. Analysis and review
2. Applying reviewed decisions

The analysis phase never moves or deletes a light.

The apply phase accepts only a completed decisions file tied to an exact
analysis manifest and exact SHA-256 checksums.

# Phase 0 — Self-test

Before the first run in a session, run:

    ASTRO_PY="/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python"
    HELPER="<installed skill directory>/scripts/quality_control.py"

    "$ASTRO_PY" "$HELPER" self-test

The helper uses the approved Siril AppRun directly with:

- `APPDIR` supplied as a process environment variable
- `AppRun` and `siril-cli` supplied as separate process arguments
- no shell command string
- no `eval`
- no `bash -c`

Stop if self-test does not report success.

# Phase 1 — Reapply previous rejects

AstroProcessor may recopy frames that were rejected during an earlier run.

Before analyzing new candidates, run a dry run:

    "$ASTRO_PY" "$HELPER" reapply \
      --project "<exact project>" \
      --filter "<filter>" \
      --dry-run

Review the action report.

When the dry run contains only valid checksum-matched actions, run:

    "$ASTRO_PY" "$HELPER" reapply \
      --project "<exact project>" \
      --filter "<filter>"

The helper may:

- move a previously rejected checksum back into `rejects`
- delete a newly recopied direct-child duplicate only when an identical reject
  is already verified and preserved
- create a unique checksum-based reject filename when the same filename has
  different content

The agent must not perform these operations manually.

If there is no rejection index, reapply succeeds with zero actions.

# Phase 2 — Analyze every light

Run:

    "$ASTRO_PY" "$HELPER" analyze \
      --project "<exact project>" \
      --filter "<filter>"

Do not add a frame limit.

Do not sample.

Do not stop after finding the first bad session.

The helper analyzes every direct-child FITS file and writes a timestamped run
under:

    <project>/.astro-light-quality-control/<filter>/<run-id>/

The helper produces:

- `analysis-manifest.json`
- `metrics.csv`
- `review.html`
- `decision-template.json`
- `summary.json`
- one isolated Siril attempt directory per frame
- one standardized autostretched preview per successful frame
- Siril star CSV output and logs

The helper uses Siril commands equivalent to:

    requires 1.4.4
    setfindstar reset
    load "<exact FITS>"
    findstar "-out=<attempt>/stars.csv"
    autostretch -linked
    savepng "<attempt>/preview"
    close

Siril is launched using a fixed subprocess argument array. The agent must not
construct the Siril command itself.

# Analysis evidence

For every frame, the helper records:

- exact filename and source path
- SHA-256
- file size
- DATE-OBS
- filter
- exposure
- gain
- offset
- temperature
- binning
- dimensions
- robust pixel statistics
- Siril star count
- median FWHM when available
- median roundness when available
- autostretched preview
- stdout and stderr logs
- deterministic outlier flags
- observing-session group

Fixed flags are review prompts, not automatic rejection decisions.

The helper never decides that an image is aesthetically bad.

# Phase 2A — Compact review summary

After analysis succeeds, do not load the entire `metrics.csv` or full
`analysis-manifest.json` into one model turn.

Reuse the successful analysis:

    "$ASTRO_PY" "$HELPER" review-summary       --analysis "<run>/analysis-manifest.json"

This command does not rerun Siril and does not modify light frames.

It writes:

- `compact-review.json`
- `review-plan-template.json`
- `representative-review.html`
- `flagged-files.csv`

The command prints compact session and evidence-group information,
including:

- frames per session
- deterministic evidence groups within each session
- successful and failed analysis counts
- fixed review flags for each group
- star-count minimum, median, and maximum
- DATE-OBS range
- rotator-angle hints
- bounded representative preview paths for every evidence group
- whether group-level accept or reject is supported

Read `compact-review.json`.

Review the representative previews for every evidence group. Review additional
individual previews only when a group is ambiguous.

Do not read the complete metrics CSV or manually reproduce dozens of
filename-and-checksum overrides.

# Evidence-group review rules

Every analyzed frame still receives an explicit final decision. The helper
deterministically groups frames within a session by:

- analysis success or failure
- exact fixed-review-flag signature

A mixed session may therefore contain groups such as:

    session-01/evidence-01
    unflagged
    30 frames

    session-01/evidence-02
    very_low_star_count
    30 frames

A group-level reject is permitted when:

- every member was analyzed successfully
- the group has a nonempty fixed-review-flag signature
- representative previews support the rejection
- the reviewer records a reason
- confidence is `high` or `confirmed`

A user-confirmed group may also be rejected by setting `user_confirmed` to
true, but never set it unless the user actually confirmed that group.

A group-level accept is permitted only when:

- every member was analyzed successfully
- the group has no review flags

Use exact filename-and-checksum `file_overrides` only for genuine exceptions
inside an otherwise coherent evidence group.

The agent may reject only when evidence is high confidence, such as:

- unreadable FITS
- blank or nearly blank exposure
- complete tracking loss
- severe star trails
- gross defocus
- major obstruction or cloud failure
- incompatible dimensions
- checksum already present in the rejection index
- a user-confirmed rejected filename, checksum, or session

Do not reject solely because:

- a narrowband image has fewer stars
- one metric is mildly worse
- the background differs modestly
- there is a gradient or vignetting
- a filename resembles another rejected file
- the observing date differs

# Phase 2B — Small review plan

Copy the small generated template:

    cp "<run>/review-plan-template.json" "<run>/review-plan.json"

The agent may edit only `review-plan.json`.

Complete:

- `review_completed`: true
- `reviewer`
- `review_notes`
- one decision for every generated evidence group
- a reason for every reject or needs-review group
- confidence `high` or `confirmed` for rejection
- `user_confirmed` only when the user actually confirmed the group
- optional exact filename-and-checksum `file_overrides` for exceptions

Do not add, remove, rename, or merge evidence groups.

Do not edit the analysis manifest, metrics, checksums, or helper.

# Phase 2C — Expand reviewed decisions

Run:

    "$ASTRO_PY" "$HELPER" build-decisions       --plan "<run>/review-plan.json"

The helper generates a complete timestamped `decisions-*.json` containing one
filename-and-SHA-256 decision for every analyzed frame.

It validates:

- every evidence group is covered exactly once
- group-level reject and accept eligibility
- every file override matches an analyzed filename and checksum
- every frame receives exactly one valid decision
- the resulting decisions file passes the existing full-file validator

Use the `decisions_file` path returned by this command for the apply dry run.

# Phase 3 — Dry-run the reviewed moves

Run:

    "$ASTRO_PY" "$HELPER" apply \
      --decisions "<returned decisions_file path>" \
      --dry-run

Review every planned action.

Stop when:

- a checksum changed
- a file disappeared
- a decision is missing
- an unreviewed frame exists
- an unexpected collision is reported
- the analysis manifest no longer matches the prepared path

Do not repair the problem during this run.

# Phase 4 — Apply decisions

After the dry run is clean, run:

    "$ASTRO_PY" "$HELPER" apply \
      --decisions "<returned decisions_file path>"

Only entries marked `reject` are moved.

Accepted and `needs_review` files remain direct children of `lights`.

The helper:

- verifies each source checksum
- never overwrites an existing reject
- verifies every destination checksum
- records every action
- updates `rejects/rejection-index.json` atomically
- preserves timestamped manifests outside `lights`

# Duplicate deletion boundary

The helper may delete only a direct-child light when:

- its SHA-256 matches a previous rejection
- an identical reject is already verified and readable
- the reject copy remains preserved
- the action is recorded

It never deletes:

- a unique light
- an ASIAIR file
- a file already in rejects
- calibration data
- a directory
- a project
- a processing result
- a manifest or log

# Phase 5 — Verify final status

Run:

    "$ASTRO_PY" "$HELPER" status \
      --project "<exact project>" \
      --filter "<filter>"

The status command now distinguishes:

- `ready`: a successful applied decisions file is checksum-verified against
  the current direct lights and rejects, with no needs-review frames
- `needs_review`: an applied decision set is verified but contains unresolved
  needs-review frames
- `unreviewed`: the filesystem is safe, but no successful applied decision set
  can be verified for the current direct lights
- `blocked`: files are missing, rejected checksums remain directly in lights,
  or another filesystem safety check fails

Do not treat `unreviewed` as ready for Mono Preprocessing.

# Failure behavior

On any helper error:

1. stop the current stage
2. preserve all generated evidence
3. report the exact command
4. report exit status, stdout, and stderr
5. report existing result paths
6. do not write replacement code
7. do not rerun with another method
8. do not move files manually
9. do not continue to Mono Preprocessing
10. return `blocked` or `failed`

# Result report

Return:

- exact project and filter
- self-test result
- reapply dry-run and apply result paths
- analysis run directory
- compact review path
- representative review path
- review plan path
- generated decisions path
- total frames analyzed
- successful and failed analysis counts
- session groups
- accepted count
- rejected count
- needs-review count
- every rejected filename, checksum, and reason
- duplicate direct-child files deleted
- collision-safe reject names
- decisions file path
- apply result path
- final status result
- whether Mono Preprocessing is permitted

# Test prompt

    Use the astro-light-quality-control skill on Ha in "M16 July 2026".

    Analyze every Ha light independently. Use DATE-OBS to group observing
    sessions, but reject frames only from the image evidence and measurements.
    Move definite rejects into lights/rejects, leave borderline frames as
    needs_review, and report whether Ha is ready for mono preprocessing.

# Final rule

Use only the installed deterministic helper.

Analyze every direct-child FITS light.

Reuse successful analysis results through `review-summary`; do not rerun Siril
because a model turn stalled during review.

Keep model review compact by deterministic evidence group and let
`build-decisions` expand the reviewed plan to every checksum-bound frame.

Never develop or repair the implementation during a quality-control run.
