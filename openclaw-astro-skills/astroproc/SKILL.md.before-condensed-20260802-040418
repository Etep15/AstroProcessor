---
name: astroproc
description: "Create AstroProcessor projects, copy exact ASIAIR source projects, prepare Siril folders, and verify every result."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# AstroProcessor

Use this skill when the user asks to create an AstroProcessor project, copy
files from an ASIAIR source, prepare the project for Siril, inspect an existing
AstroProcessor project, or perform those operations as one verified workflow.

This skill handles AstroProcessor only.

It must not:

- run Siril
- calibrate images
- register images
- stack images
- combine SHO channels
- process stars
- delete projects
- delete source files
- delete calibration files
- invent AstroProcessor options
- claim an operation succeeded without verifying its outputs

# Approved AstroProcessor installation

The canonical AstroProcessor repository is:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor

The approved executable is:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc

The projects root is:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/Projects

The shared calibration root is:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/calibration

Use these exact paths.

Do not:

- initialize or use a Git repository in the parent CodeWarrior workspace
- create a global `/usr/local/bin/astroproc` command
- move or copy `astroproc` elsewhere
- modify the AstroProcessor executable during an import or preparation run
- create projects outside the canonical `Projects` directory
- create a second calibration library outside the canonical `calibration`
  directory

# Required executable validation

Before the first AstroProcessor operation in a session:

1. Confirm that the approved executable exists.
2. Confirm that it is executable.
3. Confirm that its first line points to the project virtual environment:

       #!/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python

4. Run:

       /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc --help

5. Confirm that `--help` completes successfully.
6. Use only options shown by the installed command or explicitly documented in
   this skill.
7. Stop and report the blocker if the command fails.

Do not install Python packages automatically.

# Supported operations

This skill supports four modes:

1. `create`
2. `copy`
3. `prepare`
4. `full-setup`

`full-setup` performs `create`, then `copy`, then `prepare`, with verification
between every operation.

A failed operation stops the workflow. Do not continue to the next operation.

# Required values

Keep these values separate:

- `target`
- `source_project`
- `project_name`
- `source_root`
- `source_type`

## Meaning of each value

### target

A normalized target identifier used for reporting.

Example:

    M16

### source_project

The exact ASIAIR source-project folder name.

Example:

    M 16

Preserve `source_project` exactly as written by the user.

Do not:

- remove spaces
- alter capitalization
- replace spaces with underscores
- substitute the normalized target
- infer a different spelling when an exact source-project name was supplied

The exact value is passed to AstroProcessor with `-sp`.

### project_name

The destination AstroProcessor project name.

Example:

    M16 July 2026

The destination project path is:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/Projects/<project_name>

Preserve spaces in the user-facing project name unless AstroProcessor itself
applies documented sanitization.

### source_root

The mounted ASIAIR source root.

Example:

    /mnt/asiair/emmc

Do not mount or remount the ASIAIR. The source must already be available.

### source_type

The ASIAIR top-level source type.

Supported values are:

- `autorun`
- `live`
- `plan`
- `preview`
- `stacked`
- `video`

Normalize the source type to lowercase.

Do not invent another source type.

# Example prompt interpretation

For:

    Process my M 16 files found in Autorun in /mnt/asiair/emmc
    to a project named M16 July 2026.

derive:

- `target`: `M16`
- `source_project`: `M 16`
- `project_name`: `M16 July 2026`
- `source_root`: `/mnt/asiair/emmc`
- `source_type`: `autorun`

The values `M16`, `M 16`, and `M16 July 2026` are related but not
interchangeable.

# Missing or ambiguous values

Ask for clarification only when a required value cannot be determined safely.

For `copy` or `full-setup`, all of the following are required:

- exact ASIAIR source-project name
- destination project name
- source root
- source type

Do not guess `source_project`.

If the user supplies both a normalized target and an exact ASIAIR project
name, preserve both.

# Safety rules

- Never delete a project.
- Never delete a failed project.
- Never delete source images.
- Never delete calibration files.
- Never move files out of the ASIAIR source.
- Never alter files under the ASIAIR source.
- Never overwrite an existing project without explicit approval.
- Never replace existing imported files silently.
- Never recreate or replace the shared calibration root.
- Never modify an existing symbolic link without first reporting its current
  and proposed targets.
- Never treat command exit status alone as proof of success.
- Never mark an operation complete unless its required postconditions pass.
- Never continue after a zero-file copy.
- Never continue to preparation after a failed copy.
- Never use bare `rm`, `rmdir`, `unlink`, or destructive cleanup commands.
- Never use `eval`.
- Quote all paths and user-supplied names.
- Run shell procedures in a child shell.
- Do not set persistent shell options in the user’s interactive SSH shell.
- Do not use a bare `exit` that could terminate the user’s SSH session.

# Source immutability

Treat the ASIAIR source as read-only.

Before copying:

1. Confirm that the source root exists.
2. Confirm that the requested source-type directory exists.
3. Confirm that the exact source-project directory can be found.
4. Record the source paths.
5. Record source file counts.
6. Record source file sizes when practical.
7. Record checksums only when practical and not excessively expensive.

After copying:

1. Confirm that the original source paths still exist.
2. Confirm that the source file count did not decrease.
3. Confirm that AstroProcessor copied rather than moved the files.
4. Report any source change as a critical failure.

# Dry-run behavior

When the user requests a dry run:

- do not create a project
- do not copy files
- do not prepare folders
- do not write reports inside the project
- do not modify any files
- run only read-only validation such as `astroproc --help`, path checks,
  directory listings, and file counts
- show the exact commands that would be executed
- show all parsed values
- show all expected destination paths
- report blockers

# Operation 1 — Create a project

## Command

Use:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -np "<project_name>"

Example:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -np "M16 July 2026"

## Preflight

Before running the create command:

1. Confirm that `project_name` is present.
2. Resolve the expected project path under the canonical Projects root.
3. Confirm that the resolved path remains inside the canonical Projects root.
4. Check whether the project path already exists.
5. If it exists, stop and report it.
6. Do not delete, rename, merge, or overwrite it automatically.
7. Record the exact proposed command.

## Completion requirements

The create operation passes only when:

- the command exits successfully
- the project directory exists
- the project directory is inside the canonical Projects root
- the project directory is writable by the current user
- no unrelated project was modified
- the exact command and resulting path were recorded

Command success without a created project directory is a failure.

## Failure handling

On failure:

- preserve any partially created project
- do not delete it
- report the exit status
- report stdout and stderr
- report the expected project path
- stop before copying

# Operation 2 — Copy an ASIAIR source project

## Required command

Use all four required options:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -c "<project_name>" \
      -sp "<source_project>" \
      -sd "<source_root>" \
      -t "<source_type>"

Example:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -c "M16 July 2026" \
      -sp "M 16" \
      -sd "/mnt/asiair/emmc" \
      -t autorun

The `-sp` option is mandatory.

Never replace:

    -sp "M 16"

with:

    -sp "M16"

unless the exact ASIAIR source-project folder is actually named `M16`.

## Preflight

Before copying:

1. Confirm that the destination project exists.
2. Confirm that it is inside the canonical Projects root.
3. Confirm that `source_project` is present.
4. Preserve `source_project` exactly.
5. Confirm that `source_root` exists and is readable.
6. Confirm that `source_type` is supported.
7. Confirm that the requested source-type directory exists.
8. Locate the exact source-project directory without changing its spelling.
9. Count source FITS files.
10. Stop if no source FITS files are found.
11. Inspect the destination project for existing imported files.
12. Stop before overwriting or duplicating an existing import unless the user
    explicitly approves a documented resume behavior.
13. Record the exact proposed command.

## Required verification

After the copy command:

1. Capture the command exit status.
2. Preserve stdout and stderr.
3. Count all copied FITS files inside the destination project.
4. Count copied files by category when available:
   - lights
   - flats
   - darks
   - biases
5. Count copied files by filter when available:
   - Ha
   - SII
   - OIII
6. Confirm that the destination source directories contain FITS files.
7. Confirm that at least one FITS file was copied.
8. Confirm that the original ASIAIR files remain present.
9. Confirm that source counts did not decrease.
10. Preserve the AstroProcessor import report when one is produced.
11. Report rejected or unclassified files without deleting them.
12. Record source and destination paths.

## Completion requirements

The copy operation passes only when all of the following are true:

- the command included `-c`
- the command included the exact `-sp` value
- the command included `-sd`
- the command included `-t`
- the command exited successfully
- the destination project contains copied FITS files
- imported counts are nonzero
- the ASIAIR source remains intact
- the import result was reported

A zero-file import is a failure even when the command exits with status zero.

Do not mark the copy operation complete merely because the command was
attempted.

## Failure handling

Stop when:

- `-sp` was omitted
- the exact source project was not found
- the source type was not found
- no source FITS files were found
- no destination FITS files were created
- imported counts are zero
- source files disappeared or changed unexpectedly
- the destination project cannot be resolved
- AstroProcessor reports an unexplained partial import

Do not run `-p` after a failed copy.

# Operation 3 — Prepare the Siril folder structure

## Command

Use:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -p "<project_name>"

Example:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -p "M16 July 2026"

## Purpose

Preparation organizes the imported project into Siril-compatible per-filter
processing directories.

Expected filter roots include:

- `processing/Ha`
- `processing/SII`
- `processing/OIII`

Each available filter root should expose the required data through entries
such as:

- `lights`
- `flats`
- `darks`
- `biases`

Some entries may be symbolic links.

Project light and flat links should resolve to data inside the destination
project.

Dark and bias links should resolve to the canonical shared calibration root:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/calibration

Do not require a particular textual relative-link spelling when the resolved
target is correct.

Verify resolved targets, not merely the raw link text.

## Preflight

Before preparation:

1. Confirm that the destination project exists.
2. Confirm that the copy operation passed.
3. Confirm that the project contains nonzero imported FITS files.
4. Confirm that required light and flat groups exist for available filters.
5. Confirm that the shared calibration root exists.
6. Record the exact proposed command.
7. Stop if the project is empty.

## Required verification

After preparation:

1. Capture the command exit status.
2. Preserve stdout and stderr.
3. Confirm that the processing directory exists.
4. Identify all prepared filter directories.
5. For Ha, SII, and OIII when data exists, verify:
   - the filter directory exists
   - `lights` exists
   - `flats` exists
   - `darks` exists
   - `biases` exists
6. Determine whether each entry is a directory or symbolic link.
7. For every symbolic link:
   - record the raw link target
   - resolve the link
   - confirm the resolved target exists
   - confirm the resolved target is inside the intended project or canonical
     shared calibration root
8. Confirm that prepared `lights` exposes nonzero FITS files.
9. Confirm that prepared `flats` exposes nonzero FITS files.
10. Confirm that calibration links expose the selected shared calibration
    files.
11. Report the selected calibration date directories when visible.
12. Report missing filters or calibration groups.
13. Do not conceal broken links.

## Completion requirements

The prepare operation passes only when:

- the command exits successfully
- the processing directory exists
- expected filter directories exist for imported filters
- required entries exist
- every symbolic link resolves
- lights and flats expose nonzero FITS files
- calibration links resolve inside the canonical calibration root
- the verification results were reported

A command exit status of zero with empty directories or broken links is a
failure.

## Failure handling

On failure:

- preserve the project
- preserve all created directories and links
- do not delete or rewrite them automatically
- report every broken or incorrect link
- report missing data groups
- stop before Siril processing
- request review before replacing existing links

# Full setup workflow

When the user requests creation, copying, and preparation together:

1. Parse and report all values.
2. Validate the AstroProcessor executable.
3. Run the create preflight.
4. Create the project.
5. Verify the created project.
6. Run the copy preflight.
7. Copy using `-c`, `-sp`, `-sd`, and `-t`.
8. Verify nonzero copied FITS files.
9. Confirm the ASIAIR source remains intact.
10. Run the prepare preflight.
11. Prepare using `-p`.
12. Verify filter folders and every symbolic link.
13. Return a structured completion report.
14. Stop before any Siril command.

Never skip verification between operations.

# Existing projects

If the destination project already exists:

1. Do not delete it.
2. Do not recreate it.
3. Do not overwrite it.
4. Inspect it read-only.
5. Report:
   - whether imported FITS files exist
   - whether processing folders exist
   - whether links resolve
   - whether it appears complete, partial, or empty
6. Ask the user whether to:
   - use the existing project
   - choose a different project name
   - manually move the old project aside

Do not choose for the user.

# Calibration handling

The shared calibration root is persistent and must remain in place between
projects.

Do not delete or recreate it when starting a new project.

AstroProcessor may select dated calibration directories under the shared
calibration root.

Report:

- the selected dark directory
- the selected bias directory
- the dates represented by those directories
- the resolved link targets used by each filter

Do not copy the shared calibration library into each project unless
AstroProcessor explicitly documents that behavior.

# Verification commands

Use read-only tools such as:

- `find`
- `stat`
- `readlink`
- `readlink -f`
- `test`
- `wc`
- Python path inspection
- FITS-header inspection through AstroProcessor’s existing code when available

Do not use deletion commands for verification.

When counting FITS files, account for common extensions case-insensitively:

- `.fit`
- `.fits`
- `.fts`

# Structured result

Return a result containing:

- operation
- status
- target
- exact source project
- destination project
- source root
- source type
- AstroProcessor executable
- exact command or commands executed
- command exit statuses
- project path
- shared calibration path
- source FITS count
- copied FITS count
- counts by frame type
- counts by filter
- prepared filter directories
- symbolic-link targets
- resolved symbolic-link targets
- selected calibration directories
- warnings
- blockers
- next permitted operation

Use one of these statuses:

- `success`
- `failed`
- `blocked`
- `needs_review`
- `dry_run`

# Response examples

## Successful create

Report:

- project created
- exact project path
- exact command
- verification evidence
- next permitted operation: `copy`

## Successful copy

Report:

- exact ASIAIR source project
- exact destination project
- source and copied FITS counts
- counts by frame type and filter
- source preservation result
- import report path when available
- next permitted operation: `prepare`

## Successful prepare

Report:

- prepared filter roots
- each link’s raw and resolved target
- FITS counts visible through each link
- selected calibration directories
- broken-link count
- next permitted operation: external Siril-processing skill

# User-invocation examples

## Create only

    Use the astroproc skill to create a project named M16 July 2026.

## Copy only

    Use the astroproc skill to copy the exact ASIAIR source project "M 16"
    from Autorun under /mnt/asiair/emmc into the AstroProcessor project
    "M16 July 2026".

## Prepare only

    Use the astroproc skill to prepare the project "M16 July 2026" for Siril.

## Full setup

    Use the astroproc skill to create the project "M16 July 2026", copy the
    exact ASIAIR source project "M 16" from Autorun under /mnt/asiair/emmc,
    and prepare the project for Siril.

# Final rule

This skill ends after AstroProcessor project creation, verified ASIAIR copying,
and verified folder preparation.

It must not run Siril or claim that the project is ready for Siril unless the
copy counts are nonzero and every required prepared link resolves correctly.
