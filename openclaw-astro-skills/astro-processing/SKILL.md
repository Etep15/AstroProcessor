---
name: astro-processing
description: "Fully process a target-agnostic narrowband SHO project from AstroProcessor import through the autonomous Siril stage chain, then write a complete processing report."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Astro Processing Orchestrator

Version: **2.0.0**

This is the thin, stateful controller for the complete AstroProcessor + Siril SHO workflow. It does not reimplement image-processing algorithms. Each child skill remains authoritative for its own validation, candidates, exact-path visual review, autonomous selection, publication, and provenance.

## Full-pipeline routing

Use this skill for requests such as:

```text
Fully process project "Target" from /mnt/asiair/emmc in autorun.
Process my Target project from /mnt/asiair/emmc in autorun.
Fully process project "Destination" from /mnt/asiair/emmc in autorun using source project "Exact ASIAIR Name".
```

A request for one named installed stage is **not** a full-pipeline request. Route it directly to that child skill and do not run AstroProcessor or other stages.

This orchestrator is target-agnostic but currently pipeline-specific to narrowband SHO projects containing Ha, SII, and OIII.

## Parse the request

Keep these values distinct:

- destination AstroProcessor project;
- exact ASIAIR source-project name;
- source root;
- source type;
- optional target/display name.

If no separate source-project name is supplied, use the destination project name for `-sp`, matching AstroProcessor's documented default. Never inspect the ASIAIR tree to guess a different source-project name.

## AstroProcessor setup

Use only:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc
```

First verify `astroproc --help`. Then run, in order:

```text
astroproc -np "<destination project>"

astroproc -c "<destination project>" \
  -sp "<exact source project>" \
  -sd "<source root>" \
  -t "<source type>"

astroproc -p "<destination project>"
```

Let AstroProcessor own source-layout interpretation, copying, calibration selection, and preparation.

If `-np` reports that the project already exists, continue when that is the only reason for nonzero exit. If copy fails or reports zero matching/copied lights, stop before preparation. If preparation fails, stop before Siril.

Never alter source files, manually substitute copying/preparation, or guess another source-project path.

After successful setup, initialize the durable controller:

```text
{baseDir}/bin/astro-processing begin \
  --project "<destination project>" \
  --source-project "<exact source project>" \
  --source-root "<source root>" \
  --source-type "<source type>" \
  --new-project-exit <exit> \
  --copy-exit <exit> \
  --prepare-exit <exit> \
  --setup-note "<copy counts, prepared filters, calibration choices, warnings>"
```

Omit `--source-project` only when it intentionally equals the destination project.

## Current stage order

```text
siril-mono-preprocessing
→ siril-master-alignment
→ siril-mono-background-cleanup
→ siril-mono-linear-denoise
→ siril-sho-combination
→ siril-background-neutralization
→ siril-starnet-removal
→ siril-sho-channel-balance
→ siril-stretch
→ siril-green-reduction
→ siril-saturation
→ siril-star-processing
→ siril-star-recombination
```

Master alignment owns the common valid-area crop. `siril-stretch` replaces the historical fixed GHS-pass/standalone-black-point chain. Star processing uses the preserved StarNet stars branch. Recombination consumes the completed saturation and star-processing branches and is the final automated image.

## One-command authorization

A complete-pipeline user request authorizes normal forward progress through all required missing/obsolete stages without asking the user between stages.

It does not authorize blindly replacing current valid work:

- current valid child result → record `skipped`;
- child obsolete only because this same full run replaced its upstream → use that child's documented `confirm-fresh` path without asking again;
- completed entire project before starting another full run → ask before full reprocessing.

Do not ask the user to choose processing candidates. The child skill owns autonomous review/selection.

## Child-stage loop

Call:

```text
{baseDir}/bin/astro-processing next --project "<project>"
```

For `status: delegate`:

1. Read the exact returned `skill_md`.
2. Follow that installed child SKILL as the sole authority for the stage.
3. Complete its processing, exact-path visual review, autonomous selection, publication, and final status.
4. Do not use directory discovery to locate review evidence.
5. Do not execute earlier or later stages from inside the child.
6. If it blocks, stop the full pipeline.

After a child finishes, record only exact paths returned by the child:

```text
{baseDir}/bin/astro-processing record-stage \
  --project "<project>" \
  --stage "<child skill>" \
  --status ready \
  --manifest "<exact manifest path>" \
  --output "<exact canonical output>" \
  --selected "<compact selection>" \
  --note "<concise result>"
```

Repeat `--output` for multiple canonical outputs.

Use `--status skipped` for an already-current stage and `--status blocked --note "<exact blocker>"` for a blocker.

Then call `next` again and continue autonomously.

## Session-boundary exception

The goal is one user command for the full workflow. If an installed child skill explicitly mandates a fresh CodeWarrior model session for context safety, that child constraint remains authoritative. Durable `processing-state.json` preserves the orchestration run. If automatic fresh-session continuation is unavailable, return only the exact continuation instruction for this same run; do not ask the user to repeat processing choices.

## Final report

When `next` returns `ready_to_finish`, run:

```text
{baseDir}/bin/astro-processing finish --project "<project>"
```

It writes:

```text
<project>/processing/full-processing-report.json
<project>/processing/full-processing-report.md
<project>/processing/reports/full-processing-<run-id>.json
<project>/processing/reports/full-processing-<run-id>.md
```

The report records the source binding, AstroProcessor setup, every stage and whether it ran or was skipped, stage versions when available, selections, manifest/output paths and SHA-256 values, warnings/blockers, and final star-recombination output.

The project-root `processing-state.json` is the durable resume record.

## Safety

- Never delete source data, calibration data, attempts, candidates, or accepted checkpoints.
- Never overwrite a child checkpoint except through that child's preservation-safe fresh-run publication contract.
- Run only one Siril processing stage at a time.
- Never invent AstroProcessor options or Siril commands.
- Never bypass child provenance or review gates.
- Never continue past a required blocker.
- Never run commands remotely on the Mac Studio.

