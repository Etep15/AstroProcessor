---
name: siril-black-point
description: "Run/resume autonomous Siril black point with manifest-first completed/obsolete confirmation while preserving the exact v1.0.4 processing/review/publication wrapper and helper."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Black Point 1.0.5

Orchestration version: **1.0.5**

Processing helper: **1.0.4, byte-for-byte unchanged**

The installed v1.0.4 `bin/black-point` wrapper is also preserved byte-for-byte
as:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point-v1.0.4
```

v1.0.5 does not reimplement the mature v1.0.4 review/selection/publication
interface. It only intercepts named-stage entry and fresh-rerun authorization.

Use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point
```

Normal request:

```text
Process M16 July 2026 with black point.
```

## Deterministic named-stage entry

The first Exec must be:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point advance --project "<project>"
```

Do not first read `astro-processing`, inspect helper source, discover a run root,
or search for Python.

Do not use `find`, `ls`, `cat`, `grep`, `jq`, globbing, or manual run-manifest
inspection to route or recover this stage.

## Pipeline placement

```text
siril-ghs-stretch-pass2
→ siril-black-point
→ siril-green-reduction
```

Current GHS pass-2 must prove in its small canonical manifest:

```text
status: ready
helper_version: 1.2.0
visual_review_completed: true
quality_assessment.satisfactory: true
next_stage: siril-black-point
black_point_processing_permitted: true
```

## Completed-stage semantics

A mature v1.0.4 black-point canonical is still a completed result even if its
recorded GHS2 source has since changed.

```text
completed + current
→ confirmation_required

completed + upstream-obsolete
→ confirmation_required
```

The old canonical remains untouched until successful fresh publication.

A same-source older-policy canonical remains governed by the proven v1.0.4
reselection migration. A missing stage or compatible incomplete run also
remains governed by the preserved v1.0.4 wrapper.

## Manifest-first fast path

Before confirmation, v1.0.5 reads only the small GHS2/black-point manifests
and file-existence metadata. It does not hash the 100+ MB FITS files.

Large FITS hashing happens only after confirmation.

## Fresh confirmation

After the user clearly confirms once:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point confirm-fresh --project "<project>"
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point advance --project "<project>"
```

`confirm-fresh` binds durable authorization to:

1. current GHS2 FITS SHA;
2. preserved black-point manifest SHA;
3. preserved black-point FITS SHA.

State:

```text
<project>/.siril-black-point-v1.0.5/fresh-intent.json
```

If the binding remains valid, do not ask again after interruption.

For a completed/current canonical, v1.0.5 also bridges through the preserved
v1.0.4 wrapper's own `advance → confirm-fresh` durability. For an
upstream-obsolete canonical, the preserved v1.0.4 wrapper already treats the
old result as needing reprocessing, so no private helper bypass is added.

## Processing/review/publication contract remains v1.0.4

The preserved v1.0.4 wrapper owns:

- candidate generation;
- interrupted-run recovery;
- exact Read target production;
- compact visual-review handoff;
- selection policy enforcement;
- evidence reconstruction;
- durable selection;
- preservation-safe publication;
- publication recovery;
- final verification;
- green-reduction handoff.

The processing helper still uses:

```text
linstretch -BP=<candidate> -clipmode=rgbblend
maximum candidates: 3

candidate-00 p0.1% target: 0.0080
candidate-01 p0.1% target: 0.0045
candidate-02 p0.1% target: 0.0025

low-luma clipping hard max:      0.1%
channel-floor clipping hard max: 0.6%
channel-floor preferred:         0.2%
high-luma clipping hard max:     1e-7
luma correlation minimum:        0.995
output maximum:                  0.98
```

Preservation of faint emission and low-contrast dust outranks simply making the
background darker.

## Visual review

When `advance` returns `visual_review_required`, use OpenClaw Read on every
returned `read_targets[].path` exactly as returned.

Do not locate replacement paths yourself.

The user does not choose the candidate.

## Selection/publication

The public compact v1.0.4 interface is preserved exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point select-publish \
  --project "<project>" \
  --candidate "<selected>" \
  --visual-notes "<overall comparison>" \
  --note "candidate-00=<what was actually seen>" \
  --note "candidate-01=<what was actually seen>"
```

Repeat `--note` for every required candidate note. Use
`--policy-override-reason` only when the v1.0.4 selection policy requires it.

Do not supply run-root, compared candidates, review method, preview paths, or
SHA values; the preserved v1.0.4 wrapper reconstructs and revalidates that
evidence.

## Completion

Require actual final status:

```text
status: ready
helper_version: 1.0.4
canonical_manifest_compatible: true
selection_policy_status: current
visual_review_completed: true
next_stage: siril-green-reduction
green_reduction_processing_permitted: true
errors: []
```

Then stop before green reduction.
