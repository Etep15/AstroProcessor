---
name: siril-black-point
description: "Run/resume Siril black point with context-safe orchestration, preferred-range channel clipping selection policy, mandatory image Read review, and preservation-safe publication/reselection."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Black Point 1.0.4

Use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point
```

Never search for the helper. Never choose Python.

Normal request:

```text
Process M16 July 2026 with black point.
```

## Routine workflow

Run:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point advance --project "<project>"
```

Do not use `find`, `ls`, `cat`, `grep`, `jq`, globbing, manual run-root
discovery, or `run-manifest.json`.

### v1.0.3 canonical migration

A valid v1.0.1-v1.0.3 canonical is not regenerated merely because the
selection policy changed.

v1.0.4 reports it as:

```text
status: needs_reselection
green_reduction_processing_permitted: false
```

`advance` reopens the canonical's existing compatible candidate run for visual
reselection. It first preserves the published run manifest. The current
canonical remains untouched until successful v1.0.4 publication.

## `visual_review_required`

Use OpenClaw **Read** on every path in `read_targets` exactly as returned:
the common before image and every eligible candidate image.

Do not locate files yourself.

### v1.0.4 selection priority

The hard technical gate remains unchanged, but the visual selection policy is
now stricter:

```text
preferred channel-floor clipping: <= 0.2%
hard channel-floor ceiling:       <= 0.6%
```

A technically eligible candidate is classified:

```text
preferred   <= 0.2%
aggressive  > 0.2% and <= 0.6%
```

**Do not prefer a candidate merely because its background is darker or its
contrast is higher.**

Preservation of faint outer emission and low-contrast dust outranks achieving a
deeper black background.

When two candidates are visually acceptable, prefer the candidate with
materially less channel-floor clipping unless the aggressive candidate visibly
improves structure **without losing faint emission**.

For the current M16 candidate set, the expected policy context is:

```text
candidate-00  ~0.158%  preferred
candidate-01  ~0.509%  aggressive

numerical recommendation: candidate-01
v1.0.4 selection-policy recommendation: candidate-00
```

The numerical recommendation remains visible as provenance; it is not the final
visual recommendation.

### Normal selection

After genuine rendered-image review:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point select-publish \
  --project "<project>" \
  --candidate "<selected>" \
  --visual-notes "<overall visual comparison>" \
  --note "candidate-00=<what was actually seen>" \
  --note "candidate-01=<what was actually seen>"
```

Repeat `--note` for every `required_candidate_notes` entry.

### Exceptional aggressive selection

If a preferred-range candidate exists and CodeWarrior deliberately selects an
aggressive candidate, it must also supply:

```text
--policy-override-reason "<specific visible structural improvement and how faint emission remains preserved>"
```

The override reason must be substantive. The helper blocks a missing or vague
override.

The user does not choose the candidate.

## Completion

Require:

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

Stop before green reduction.

## Processing policy unchanged

No Siril processing values changed from v1.0.3:

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

OpenClaw Read/image rendering remains mandatory.

## Verification-only requests

Verification is not processing. When a verification task says STOP, stop.
Never continue into production processing, selection, publication, or green
reduction.
