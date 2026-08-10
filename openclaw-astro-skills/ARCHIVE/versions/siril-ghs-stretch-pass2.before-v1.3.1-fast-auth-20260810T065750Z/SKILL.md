---
name: siril-ghs-stretch-pass2
description: "Standalone autonomous GHS pass-2 stage with deterministic entry, bounded candidates, exact-path visual review, durable selection, publication recovery, completed-stage rerun confirmation, and black-point handoff."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril GHS Stretch — pass 2

Standalone orchestration version: **1.3.0**

Processing helper version: **1.2.0**.

The processing helper retains the validated GHS math/candidate policy. This
patch changes only its pass-1 pipeline provenance gate so it accepts the current
native pipeline:

```text
siril-sho-channel-balance
→ siril-ghs-stretch-pass1
→ siril-ghs-stretch-pass2
→ siril-black-point
```

The current pass-1 installed skill is named `siril-ghs-stretch`; the pass-1
manifest may use compatibility stage label `siril-ghs-stretch-pass1`.

## Standalone rule

This skill owns its own complete stage. It does not require the
`astro-processing` skill to execute GHS pass 2.

A future pipeline controller may call this skill, but the controller only
decides that GHS pass 2 is next. This skill owns:

- prerequisite/provenance validation;
- completed-stage/fresh-rerun handling;
- bounded candidate generation;
- exact-path review targets;
- autonomous visual selection;
- durable selection;
- publication and bounded publication recovery;
- final verification;
- black-point handoff.

## Normal direct entry

For:

```text
Process <project> with GHS Stretch pass 2
```

the first Exec must be exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 advance --project "<project>"
```

Do not first read `astro-processing`, inspect helper source, inspect project
directories, search for Python, or discover an entrypoint.

Never use system `python3`.

Never run:

```text
ls
find
tree
grep
jq
globbing
```

to route or recover this stage.

The wrapper permanently owns the required Python:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
```

## Completed or obsolete canonical result

A pre-existing canonical GHS pass-2 result is still a completed
image-processing result even when a newer GHS pass-1 source makes it obsolete.

Both a compatible completed result and a completed-but-obsolete result require
explicit confirmation before a fresh replacement run. The old canonical result
is preserved until successful publication.

For an obsolete canonical, the orchestration layer creates durable fresh
authorization bound to all three current hashes:

```text
current GHS pass-1 source
existing GHS pass-2 manifest
existing GHS pass-2 output
```

When `advance` returns `confirmation_required`, ask the returned question once.

After the user explicitly confirms:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 confirm-fresh --project "<project>"
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 advance --project "<project>"
```

Continue in the same turn.

The existing canonical pass-2 result remains preserved until successful
publication.

## Candidate generation

The unchanged processing helper owns the bounded policy:

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

## Visual review

When `advance` returns:

```text
status: visual_review_required
action: continue_autonomously_select_publish
```

this is an internal continuation point, not a user handoff.

Use OpenClaw Read on every exact `read_targets[].path` verbatim.

Stop on an exact Read failure. Never discover an alternative path.

Review:

- overall second-pass stretch strength;
- faint Eagle Nebula emission;
- Pillars and dark-lane structure;
- SHO colour integrity;
- background flatness/noise;
- clipping/compression;
- halos/ringing/seams/blocks/missing areas;
- remaining highlight headroom for black point and later colour work.

The numerical recommendation is advisory only.

The user does not select a candidate.

## Selection and publication

Use the returned `select_publish_command_template`.

Every eligible candidate must be supplied using a **repeated** `--compared`:

```text
--compared "candidate-01" --compared "candidate-02"
```

Do not write:

```text
--compared "candidate-01,candidate-02"
```

and do not place two candidate names after one `--compared`.

Example shape:

```text
.../bin/ghs-pass2 select-publish \
  --project "<project>" \
  --run-root "<exact returned run_root>" \
  --candidate "<selected>" \
  --compared "<eligible-1>" \
  --compared "<eligible-2>" \
  --visual-notes "<80+ chars comparing every eligible candidate>"
```

`select-publish` durably records the visual selection, publishes it, performs
bounded same-run publication recovery if necessary, runs final status
verification, and stops.

Do not regenerate candidates to recover publication.

Do not reread images after the selection has been made.

## Final contract

Successful completion requires:

```text
status: ready
visual_review_completed: true
next_stage: siril-black-point
black_point_processing_permitted: true
```

Then stop. Do not execute black point from this skill.

