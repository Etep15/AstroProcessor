---
name: siril-ghs-stretch-pass2
description: "Standalone autonomous GHS pass-2 stage with manifest-first fast entry, durable completed/obsolete rerun authorization, bounded candidates, exact-path visual review, durable selection, publication recovery, and black-point handoff."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril GHS Stretch — pass 2

Standalone orchestration version: **1.3.1**

Processing helper version: **1.2.0**.

The GHS equations, candidate tiers, technical thresholds, three-candidate
limit, Siril execution, and publication products remain the helper's 1.2.0
processing policy.

## Standalone architecture

This skill remains independently runnable. It owns its own prerequisite and
provenance validation, completed/obsolete detection, fresh-rerun confirmation,
durable authorization, candidate generation, exact-path visual review,
autonomous selection, publication, verification, and black-point handoff.

A future pipeline controller decides only that this stage runs next.

## Named-stage fast entry

For:

```text
Process <project> with GHS Stretch pass 2
```

the first Exec is exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 advance --project "<project>"
```

Do not read `astro-processing` first.

Do not inspect helper/orchestrator source, discover Python, inspect the project
tree, or use `ls`, `find`, `tree`, `grep`, `jq`, globbing, system `python3`,
AstroProcessor, or ASIAIR discovery for this named-stage request.

The wrapper owns the canonical Python:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
```

## Fast completed/obsolete status

Before a fresh-rerun confirmation, `advance` is manifest-first. It reads only
the small GHS pass-1 manifest, pass-1 visual-selection record, and GHS pass-2
manifest, plus fixed-path existence metadata.

It does **not** hash the large GHS pass-1 or GHS pass-2 FITS before asking the
confirmation question.

A previously completed result remains completed when a changed pass-1 source
makes it obsolete. Both `ready` and `completed-but-obsolete` require one
explicit fresh-rerun confirmation.

## Fresh authorization

`confirm-fresh` strongly verifies SHA-256 for:

```text
current GHS pass-1 FITS
existing GHS pass-2 manifest
existing GHS pass-2 FITS
```

and writes durable standalone authorization.

A still-valid v1.3.0 authorization is migrated rather than asking the user to
confirm again.

The helper 1.2.0 also has its own durable `stage-intents` confirmation system.
Version 1.3.1 deliberately uses the same native sequence that previously worked:

```text
helper begin
→ pending fresh-run intent
→ helper confirm-fresh
→ durable helper authorization
```

For a completed-but-obsolete canonical, the two native authorization calls and
the immediately authorized candidate-generation call receive the private environment
marker:

```text
GHS_PASS2_OBSOLETE_AUTHORIZED=1
```

The marker stays internal to the v1.3.1 orchestrator. Keeping it on the authorized
`run` call preserves the same helper-native completed-canonical view under which
`begin` created and `confirm-fresh` authorized the durable intent. It does not bypass
that intent: candidate generation still requires the helper's native authorization.

Under that marker, helper status may treat the existing canonical as ready only
when its own manifest is structurally `ready`, its output still matches its own
recorded SHA-256, the current-source change is present, and every status error
is one of the expected upstream source/manifest staleness errors. No damaged or
arbitrarily invalid canonical is made ready. Normal direct helper behavior is
unchanged. CodeWarrior must never set that environment variable itself.

The existing canonical pass-2 result remains untouched until successful
publication.

## Candidate generation

The helper retains:

```text
preferred median:       0.180
balanced median:        0.135–0.225
maximum p99:            0.80
maximum pixel value:    0.97
clipping permitted:     none
preferred luma corr:    >= 0.97

D:  0.70–3.50
B:  0.50–7.00
SP: 0.040–0.180
LP: exactly 0
HP: 0.860–0.990

maximum candidates: 3
```

Do not invent GHS parameters.

## Autonomous visual review

When `advance` returns `visual_review_required`:

1. Use OpenClaw Read on every exact `read_targets[].path` verbatim.
2. Compare every publication-eligible candidate.
3. Treat the numerical recommendation as advisory.
4. Do not ask the user to select a candidate.
5. Do not reread images after selection.
6. Use the exact returned `select_publish_command_template`.
7. Supply each eligible candidate with its own repeated `--compared`.
8. Never discover alternative paths if a Read fails.

Review second-pass stretch strength, faint Eagle Nebula emission, Pillars and
dark lanes, SHO colour integrity, background/noise, clipping/compression,
artifacts, and highlight headroom.

## Publication

Publication-format or staging recovery is bounded to two same-run attempts.
It never regenerates candidates.

Successful completion requires:

```text
status: ready
visual_review_completed: true
next_stage: siril-black-point
black_point_processing_permitted: true
```

Then stop. Do not execute black point from this skill.
