---
name: astroproc
description: "Create, copy, or prepare AstroProcessor projects only. A prepare-only request runs astroproc -p and stops."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

<!-- ASTROPROC-CALIBRATION-SELECTION-V2-START -->

# Calibration selection v2

`astroproc -p` keeps Siril-compatible copy mode: `processing/<filter>/darks`
and `processing/<filter>/biases` are real directories containing direct FITS
copies. Directory symlinks are not used for prepared Siril inputs.

The active shared calibration library is:

    /home/peter/.openclaw/workspace/agents/codewarrior/calibration

Dated subdirectories are organizational only. Capture date MUST NOT be used to
rank or select calibration frames.

For darks, AstroProcessor recursively evaluates FITS headers and requires a
compatible image type, camera, binning, gain, offset, dimensions, exposure, and
sensor temperature. `FILTER` is ignored for dark compatibility. For this cooled
camera workflow, dark temperature must be within ±1.0 C of the median light
sensor temperature.

For biases, AstroProcessor requires compatible image type, camera, binning,
gain, offset, dimensions, and sensor temperature, then selects the dominant
compatible bias-exposure group. `FILTER` and capture date are ignored.

All matching frames across all dated subdirectories are selected. Prepare must
fail closed when no compatible calibration population exists.

If a real prepared dark/bias directory already contains FITS files outside the
current compatibility selection, prepare must stop before copying and preserve
the existing files. It must never silently mix stale incompatible calibration
frames with a newly selected population.

<!-- ASTROPROC-CALIBRATION-SELECTION-V2-END -->


<!-- ASTROPROC-PREPARE-COPY-OVERRIDE-START -->

# Prepare copy-mode override

These rules override later references in this skill to prepared directory
symbolic links.

`astroproc -p` and `astroproc --prepare` now create real directories:

    processing/<filter>/lights
    processing/<filter>/flats
    processing/<filter>/darks
    processing/<filter>/biases

AstroProcessor copies direct FITS files into those directories. It does not
create directory symbolic links.

On a legacy project, prepare may remove only the four AstroProcessor-created
directory symlinks themselves. It never removes or modifies their targets.
Existing `lights/rejects` evidence is migrated into the new real lights
directory.

Prepare is safe to rerun:

- identical files are retained
- different-content filename collisions block the run
- indexed rejected checksums are not recopied into direct lights
- source, calibration, and rejected FITS files are never deleted
- extra existing destination files are preserved rather than deleted

Verification must confirm that all four category paths are real directories,
not symbolic links, and contain direct FITS files.

<!-- ASTROPROC-PREPARE-COPY-OVERRIDE-END -->

# AstroProcessor command skill


<!-- INVOCATION-BOUNDARY-START -->

# Strict invocation boundary

These rules override any later general workflow language in this skill.

Use this skill for AstroProcessor operations only:

- create a project
- copy ASIAIR files
- prepare a project with `astroproc -p`

When the original user request asks only to prepare a project:

1. run `astroproc -p "<exact project name>"`
2. report the exact command, exit status, stdout, and stderr
3. report the filters and calibration selections printed by AstroProcessor
4. stop

A successful `astroproc -p` command completes a prepare-only request.

Do not automatically invoke:

- `siril-mono-preprocessing`
- `astro-light-quality-control`
- `siril-cli-runner`
- Siril
- any calibration, registration, or stacking command

Only continue to another skill when the original user message explicitly
requests that additional stage.

Permission to prepare a project is not permission to preprocess it.

<!-- INVOCATION-BOUNDARY-END -->
Use this skill when the user asks to create an AstroProcessor project, copy an
ASIAIR project into it, prepare it for Siril, or perform those operations
together.

This skill explains how to invoke `astroproc`. AstroProcessor itself owns the
project, source-layout, copying, calibration-selection, and preparation logic.

Do not duplicate or second-guess that logic in the skill.

# Approved installation

Repository:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor

Executable:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc

Projects:

    /home/peter/.openclaw/workspace/agents/codewarrior/Projects

Shared calibration:

    /home/peter/.openclaw/workspace/agents/codewarrior/calibration

Always invoke the executable by its absolute path.

# Important operating rules

- Let `astroproc` handle an existing project directory.
- Do not stop merely because the project directory already exists.
- Do not delete, rename, move, or replace an existing project manually.
- Do not inspect the ASIAIR tree and reject a source-project location before
  running `astroproc`.
- Do not assume the source project must be a direct child of `Autorun`.
- Let `astroproc` understand the ASIAIR layout, including folders such as
  `Autorun/Light/<source project>`.
- Do not manually copy files as a substitute for `astroproc -c`.
- Do not manually build processing folders or symbolic links as a substitute
  for `astroproc -p`.
- Preserve the exact ASIAIR source-project name supplied by the user.
- Quote project names, source-project names, and paths.
- Never delete source files or shared calibration files.
- Do not run Siril from this skill.

Only stop before running a requested command when the executable or a required
user-supplied argument is missing.

After a command runs, report its actual output and exit status. Do not invent a
failure based on assumptions about the filesystem layout.

# Required values

Keep these values distinct:

- destination project name
- exact ASIAIR source-project name
- ASIAIR source root
- ASIAIR source type

Example:

- destination project: `M16 July 2026`
- exact ASIAIR source project: `M 16`
- source root: `/mnt/asiair/emmc`
- source type: `autorun`

Do not change `M 16` to `M16`.

# Check the command

Before the first operation in a session, confirm that this succeeds:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc --help

Do not install dependencies or modify the launcher during an AstroProcessor
operation.

# Create a project

Command:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -np "<destination project>"

Example:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -np "M16 July 2026"

Run the command even when the destination directory already exists. Let
AstroProcessor decide whether to create, reuse, or reject it.

Do not make that decision in the skill.

# Copy an ASIAIR project

Command:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -c "<destination project>" \
      -sp "<exact ASIAIR source project>" \
      -sd "<ASIAIR source root>" \
      -t "<source type>"

Example:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -c "M16 July 2026" \
      -sp "M 16" \
      -sd "/mnt/asiair/emmc" \
      -t autorun

All four options are required:

- `-c` destination AstroProcessor project
- `-sp` exact ASIAIR source-project name
- `-sd` mounted ASIAIR source root
- `-t` ASIAIR source type

Do not search for the source project and substitute a discovered path for
`-sp`. Pass the exact source-project name and source root to AstroProcessor.

Do not reject a source because it appears under `Autorun/Light`. Run the
documented command and let AstroProcessor locate the relevant ASIAIR files.

After the command finishes, report:

- exit status
- AstroProcessor's copy summary
- destination paths reported by AstroProcessor
- copied-file counts when AstroProcessor reports them
- warnings or rejected files reported by AstroProcessor

If the command reports success but zero copied files, report that result as a
copy failure and stop before preparation.

# Prepare a project

Command:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -p "<destination project>"

Example:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -p "M16 July 2026"

Let AstroProcessor select calibration folders and create the processing
structure.

Do not manually create or replace links.

After the command finishes, report:

- exit status
- filters prepared
- processing paths reported by AstroProcessor
- calibration directories selected
- warnings or errors reported by AstroProcessor

A read-only post-check may confirm that created links resolve, but it must not
replace AstroProcessor's behavior or modify the project.

# Full setup

When the user asks to create, copy, and prepare a project, run these commands in
order:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -np "<destination project>"

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -c "<destination project>" \
      -sp "<exact ASIAIR source project>" \
      -sd "<ASIAIR source root>" \
      -t "<source type>"

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -p "<destination project>"

Proceed according to the actual result of each AstroProcessor command.

- If `-np` reports that the project already exists but does not report a fatal
  failure, continue to `-c`.
- If `-c` fails or copies zero files, stop before `-p`.
- If `-c` succeeds with copied files, run `-p`.
- Do not add additional filesystem assumptions between these commands.

# Short invocation example

For:

    Create "M16 July 2026", copy the exact ASIAIR project "M 16" from
    Autorun at /mnt/asiair/emmc, and prepare it for Siril.

run:

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -np "M16 July 2026"

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -c "M16 July 2026" \
      -sp "M 16" \
      -sd "/mnt/asiair/emmc" \
      -t autorun

    /home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc \
      -p "M16 July 2026"

# Final response

Report:

- commands executed
- exit status of each command
- AstroProcessor's own summaries
- project path
- shared calibration path
- copied-file counts
- prepared filters
- any actual blocker reported by AstroProcessor

Do not report speculative blockers based only on directory names or an
existing destination folder.
