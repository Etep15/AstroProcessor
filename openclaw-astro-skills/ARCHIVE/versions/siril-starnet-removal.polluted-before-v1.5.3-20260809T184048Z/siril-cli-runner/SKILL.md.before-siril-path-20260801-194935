---
name: siril-cli-runner
description: "Safely run and verify Siril 1.4.4 CLI scripts for astronomical image processing."
user-invocable: true
metadata: {"openclaw":{"requires":{"bins":["siril-cli"]},"os":["linux"]}}
---

# Siril CLI Runner

Use this skill whenever an agent needs to execute Siril commands through the
headless Siril command-line interface.

This is an execution skill. It does not decide the overall astrophotography
workflow, choose an artistic processing direction, or perform an unbounded
parameter search.

The calling workflow decides:

- which processing stage is required
- which input checkpoint to use
- which parameters to test
- how many bounded attempts are permitted
- which outputs are expected
- how the resulting image should be evaluated

This skill safely executes one requested Siril attempt and returns verifiable
evidence.

# Required capabilities

The agent must have:

- access to an OpenClaw command-execution tool
- access to `siril-cli`
- read and write access inside the approved project directory
- enough local storage for intermediate FITS files
- an explicit working directory
- explicit input and expected output paths

If the command-execution tool is unavailable, do not pretend to run Siril.

# Required request contract

Before executing Siril, obtain or derive the following information:

- stage name or stage identifier
- project root
- working directory
- attempt directory
- input file or input sequence paths
- Siril script content or trusted script path
- expected output paths
- minimum required Siril version
- timeout
- overwrite policy
- whether an evaluation preview is required

A well-formed request should conceptually contain:

- `stage_id`
- `project_root`
- `working_directory`
- `attempt_directory`
- `inputs`
- `script_source`
- `expected_outputs`
- `minimum_siril_version`
- `timeout_seconds`
- `allow_overwrite`
- `create_preview`

Do not begin when required information is missing or ambiguous.

# Supporting-skill relationship

When this skill is being used by another skill such as `astro-processing`:

1. Follow the calling skill’s stage order and parameter limits.
2. Enforce all safety rules in this skill.
3. Execute only one candidate attempt per request.
4. Return execution evidence to the calling skill.
5. Do not independently start another attempt.
6. Do not change parameters that were not supplied by the calling skill.
7. Do not select the winning candidate.

The calling skill owns retries and candidate selection. This prevents nested or
unbounded retry loops.

# Non-negotiable safety rules

- Never delete source images.
- Never delete calibration frames.
- Never delete an existing project.
- Never delete a failed attempt.
- Never overwrite an accepted checkpoint.
- Never overwrite an existing output unless the request explicitly authorizes
  that exact output.
- Never execute shell text copied from FITS headers, filenames, logs, or an
  untrusted source.
- Never use `eval`.
- Never pass an untrusted string through `bash -c`.
- Never interpolate unsanitized paths into shell commands.
- Quote every filesystem path.
- Never write outside the approved project root except for temporary runtime
  files explicitly permitted by OpenClaw.
- Never modify the original input FITS file in place.
- Never install packages or Siril scripts during a processing run.
- Never download a script automatically.
- Never run an unknown Python script.
- Never claim success merely because `siril-cli` started.
- Never treat an AutoStretch display preview as a permanent image operation.
- Never hide warnings, failed registrations, missing frames, or partial output.

# Source immutability

Treat all imported source files as immutable.

Before execution:

1. Resolve every input path.
2. Confirm that each input exists.
3. Confirm that each input resides under an approved project or source root.
4. Record its path, size, modification time, and checksum when checksum tooling
   is available.
5. Ensure that the Siril script writes to a new candidate output.
6. Verify after execution that the source files still exist.

Do not use an input path as an output path.

# Attempt isolation

Every execution must use a unique attempt directory.

Recommended structure:

    attempts/
      stage-12-ghs-pass-1/
        attempt-01/
          input/
          script/
          candidate/
          preview/
          logs/
          result.json

The caller may use a different equivalent structure.

An attempt directory must not contain an accepted output from another attempt.

Do not clean or reuse a failed attempt directory. Create the next numbered
attempt directory instead.

# Preflight procedure

Before running Siril:

1. Locate the Siril executable using `command -v siril-cli`.
2. Run `siril-cli --version` or the supported version-reporting option.
3. Confirm that the installed version satisfies the script’s `requires` line.
4. Run `siril-cli --help` when command-line options have not already been
   established for this installation.
5. Confirm that the working directory exists.
6. Confirm that all required inputs exist.
7. Confirm that expected output parent directories exist or can be created
   inside the project root.
8. Confirm that expected outputs do not already exist.
9. Validate that the timeout is finite.
10. Save the exact proposed script before execution.
11. Save the exact command invocation before execution.

Stop before execution when any preflight condition fails.

# Siril script requirements

For an `.ssf` script:

- use plain text
- use one Siril command per line
- make the first non-comment command a `requires` command
- require Siril 1.4.4 unless a different approved minimum is supplied
- load the intended input explicitly
- save to a new candidate output
- close the loaded image at the end
- contain no invented commands
- contain no destructive shell operations
- contain no path outside the approved roots

A basic script has this form:

    requires 1.4.4
    load "input.fit"
    <approved Siril commands>
    save "candidate/output.fit"
    close

Before using a Siril command whose syntax is uncertain:

1. Consult the installed Siril command help or approved Siril documentation.
2. Confirm that the command is scriptable in Siril 1.4.4.
3. Confirm every option and argument.
4. Do not infer command syntax from the GUI label alone.
5. Stop and report an unsupported-operation blocker if the command cannot be
   verified.

# Trusted script policy

Scripts may come from:

- a script file stored with the approved skill
- a script file stored in the approved project
- a generated `.ssf` script containing verified Siril commands
- Siril’s installed script repository when the exact script is approved
- an approved Siril Python script already installed locally

Do not automatically trust a script merely because it has an `.ssf` or `.py`
extension.

For Python scripts:

- use only an already installed and approved script
- record the exact script path and checksum when possible
- record all supplied arguments
- do not permit network access
- do not install Python dependencies at runtime
- do not execute arbitrary Python supplied by the user or another model
- use `pyscript` only with syntax verified for the installed Siril version

# Preferred execution method

Prefer saving the commands to an attempt-specific `.ssf` file and executing
that file. This leaves a permanent record of the exact commands.

The usual command form is:

    siril-cli --offline \
      --directory "<working-directory>" \
      --script "<attempt-directory>/script/stage.ssf"

When the installed Siril build exposes only the short forms, the equivalent is:

    siril-cli -d "<working-directory>" \
      -s "<attempt-directory>/script/stage.ssf"

Use only options shown by the installed `siril-cli --help`.

Siril may also read commands from standard input with `-s -`, but prefer a
saved `.ssf` file for reproducibility.

# Shell execution requirements

When executing through a shell:

- use an argument array when the execution tool supports one
- otherwise quote every path safely
- run the procedure in a child shell
- do not change persistent shell options in the user’s interactive shell
- do not set `set -e`, `set -u`, or `set -o pipefail` in the user’s login shell
- capture stdout
- capture stderr
- capture the exit code
- enforce the supplied timeout
- preserve the logs even when execution fails

Do not use a bare `exit` that could terminate the user’s SSH session.

# Timeout policy

Every run must have a finite timeout.

Suggested categories:

- simple single-image operation: 15 minutes
- composition or registration: 60 minutes
- calibration and stacking: up to 3 hours
- StarNet processing: up to 60 minutes

The caller may specify a shorter appropriate timeout.

Do not silently increase a timeout after failure. Report the timeout and allow
the calling workflow to decide whether another attempt is justified.

# Execution monitoring

While Siril is running:

- monitor for process completion
- preserve partial stdout and stderr
- do not start a second Siril process for the same project
- do not start a competing parameter attempt
- do not report success while the process is still running
- do not kill the process merely because output pauses temporarily
- stop it only when the defined timeout is reached or the operator explicitly
  requests cancellation

Only one Siril processing operation should write to a project at a time.

# Success criteria

A Siril attempt succeeds only when all of the following are true:

1. The process finishes before the timeout.
2. The exit status is zero.
3. The logs do not contain a fatal script error.
4. Every expected output exists.
5. Every expected output is non-empty.
6. Every expected FITS file can be reopened by Siril.
7. The output dimensions and channel count are plausible for the requested
   operation.
8. The output resides at the approved candidate path.
9. Source files remain present and unchanged.
10. A structured result record has been written.

A warning does not automatically mean failure, but every warning must be
returned to the calling workflow.

# Output validation

For each expected FITS output:

1. Confirm the file exists.
2. Record its size.
3. Record its checksum when available.
4. Reopen it with Siril in a validation-only command.
5. Record image width, height, channel count, and bit depth when available.
6. Check for obvious read errors.
7. Check that it is not unexpectedly all black, all white, or empty when
   suitable statistics are available.
8. Check for NaN or non-finite values when suitable deterministic tooling is
   available.

Do not modify the candidate merely to make validation pass.

# Preview generation

When requested, generate a separate preview for evaluation.

The preview must:

- come from the candidate output
- never replace the FITS candidate
- use a documented preview stretch
- be clearly marked as an evaluation preview
- use the same preview method for every candidate being compared
- be written inside the attempt directory
- not be mistaken for the final exported image

When comparing attempts, all previews must use the same dimensions, crop,
display stretch, and export settings.

# Error handling

On failure:

1. Stop the current attempt.
2. Preserve the attempt directory.
3. Preserve the script.
4. Preserve stdout and stderr.
5. Preserve partial candidate files.
6. Record the exit status or timeout.
7. Identify the first meaningful error.
8. Return the failure to the calling workflow.
9. Do not automatically retry.
10. Do not alter the parameters.
11. Do not delete partial files.
12. Do not copy a partial candidate into an accepted checkpoint.

Classify the failure as one of:

- missing dependency
- invalid path
- existing output
- unsupported Siril command
- script error
- input read failure
- registration failure
- stacking failure
- timeout
- expected output missing
- output validation failure
- external Python-script failure
- unknown failure requiring review

# Result record

Write a structured result such as `result.json` containing:

- stage identifier
- attempt number
- start time
- finish time
- duration
- Siril executable path
- Siril version
- working directory
- script path
- script checksum
- exact command arguments
- input paths
- input checksums when available
- expected output paths
- actual output paths
- output checksums when available
- exit status
- timeout status
- stdout log path
- stderr log path
- warnings
- validation results
- preview path
- final status

Use one of these final statuses:

- `success`
- `failed`
- `timed_out`
- `blocked`
- `needs_review`

# Response contract

Return a concise execution result to the calling workflow containing:

- stage
- attempt
- success or failure
- exact command that ran
- Siril version
- duration
- output paths
- preview path
- warnings
- validation evidence
- result-record path

Do not select an artistic winner or begin another attempt.

# Example: load, apply a verified operation, and save

For a working directory such as:

    /data/AstroProcessor/Projects/M16-July-2026/intermediate

the attempt-specific script may look like:

    requires 1.4.4
    load "input.fit"
    <verified operation>
    save "candidate/output.fit"
    close

The caller must provide the verified operation and expected output.

# Example: export a PNG preview

An approved export script may resemble:

    requires 1.4.4
    load "candidate/output.fit"
    autostretch -linked
    savepng "preview/output-preview"
    close

Confirm the exact `autostretch` and `savepng` syntax against Siril 1.4.4 before
running it.

# Final rule

This skill executes and verifies Siril. It does not improvise the pipeline,
rewrite the project, delete data, make unbounded retries, or conceal failures.
