---
name: siril-green-reduction
description: "Run/resume Siril Remove Green Noise after black point using Maximum Mask candidates, mandatory image review, compact state, and preservation-safe publication."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

<!-- OPENCLAW_GREEN_REDUCTION_V104_ORCHESTRATION_BEGIN -->
## v1.0.4 orchestration contract

Orchestration version: **1.0.4**. The image-processing helper remains **1.0.3**
byte-for-byte and continues to own Siril processing, candidate quality gates,
exact Read targets, visual selection, publication, and final verification.

For a named-stage request such as:

```text
Process M16 July 2026 with green reduction
```

the first Exec must be exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction advance --project "<project>"
```

Do not read the broad `astro-processing` skill first and do not inspect helper
source, manifests, run roots, or directories to decide what to do.

A mature completed canonical is still a completed result when its recorded
black-point source has changed:

```text
completed + current  -> confirmation_required
completed + obsolete -> confirmation_required
```

Before confirmation this classification is manifest-first; large FITS content
hashes are deferred.

After the user clearly confirms once, run this as ONE Exec:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction confirm-fresh --project "<project>" && /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction advance --project "<project>"
```

The durable v1.0.4 authorization is strongly bound to the current black-point
manifest/FITS and the preserved green-reduction manifest/FITS. Do not ask again
while that binding remains valid.

When `visual_review_required` is returned, use OpenClaw Read on every
`read_targets[].path` exactly as returned. Never construct, normalize, repair,
or rediscover a path. If any required Read fails, STOP and report the exact
failed path. Do not use `ls`, `find`, `cat`, `grep`, `jq`, globbing, directory
inspection, or manual run-manifest/source inspection as recovery.

The v1.0.3 note validator remains strict. For every eligible candidate use full
visual phrases in exactly these fields:

```text
green:<specifically state whether unwanted/residual green remains or was removed>;
magenta:<specifically state whether any magenta/purple shift is visible>;
structure:<specifically state whether faint emission, Pillars, and dark lanes remain preserved>
```

Do not use vague values such as `magenta:none` or `structure:preserved`.

CodeWarrior autonomously chooses the least aggressive technically eligible
candidate that removes unwanted green without introducing magenta/purple or
suppressing faint structure. Candidate-02 remains exceptional and requires the
existing substantive override rationale if selected.

Publication remains preservation-safe: the existing canonical stays intact
until successful publication. After final `ready`, stop and hand off to
`siril-saturation`; do not run saturation as part of this stage.
<!-- OPENCLAW_GREEN_REDUCTION_V104_ORCHESTRATION_END -->


# Siril Green Reduction 1.0.4

Use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction
```

Never search for the helper. Never choose Python.

Normal request:

```text
Process M16 July 2026 with green reduction.
```

## Placement

```text
siril-black-point
→ siril-green-reduction
→ siril-saturation
```

Required upstream:

```text
processing/black-point/SHO-starless-black-point.fit
processing/black-point/black-point-manifest.json
```

The upstream black-point manifest must be v1.0.4, `ready`, visually reviewed,
quality satisfactory, selection-policy v1.0.4, and explicitly permit green
reduction.

## Routine workflow

Run:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction advance --project "<project>"
```

`advance` owns run discovery, candidate generation, resume, and publication
recovery. Do not use `find`, `ls`, `cat`, `grep`, `jq`, globbing, manual run-root
discovery, or run-manifest reads.

### Candidate generation

Siril 1.4.4 command:

```text
rmgreen 2 <amount>
```

Type `2` is **Maximum Mask**. Preserve lightness is left ON by omitting
`-nopreserve`.

Exactly three bounded candidates are generated in **one Siril process**:

```text
candidate-00  amount 0.10  conservative
candidate-01  amount 0.15  manual M16 baseline / numerical recommendation
candidate-02  amount 0.20  assertive
```

The successful manual M16 baseline is:

```text
Subtractive Chromatic Green Noise Reduction
Maximum Mask
amount 0.15
Preserve lightness ON
```

## `visual_review_required`

Use OpenClaw **Read** on every `read_targets[].path` exactly as returned: the
common before image and every technically eligible candidate image.

Pass each returned path to Read **verbatim**. Do not construct, shorten,
normalize, infer, repair, or rediscover a path.

Do not locate files yourself. Do not use `ls`, `find`, `cat`, `grep`, `jq`,
globbing, directory inspection, manual run-root discovery, or manifest reads as
a fallback-even after a failed Read.

If any required Read fails, **STOP and report the exact failed path**. Do not
attempt to recover by inspecting the run directory.

Visually compare unwanted green cast, residual artificial green, new magenta or
purple cast, preservation of SHO color separation, faint outer Eagle Nebula
emission, Pillars/dark lanes, luminance continuity, clipping, posterization,
blocks, seams, and ringing.

For every eligible candidate, `select-publish` now mechanically requires this
exact three-field note structure:

```text
candidate-NN=green:<specific observation>; magenta:<specific observation>; structure:<specific observation>
```

The fields mean:
- `green:` what residual/unwanted green remains or was removed;
- `magenta:` whether magenta/purple over-correction is visible;
- `structure:` whether faint outer emission, Pillars, and dark-lane structure are preserved.

Vague notes or missing fields are rejected by the helper. Do not justify a
candidate only because it matches the baseline or numerical recommendation.

The `visual_review_required` response is intentionally compact so the exact
before/candidate Read paths remain together in routine tool output. Consume the
returned `read_targets` directly; never reconstruct a path.

**Choose the least aggressive amount that removes the unwanted green cast
without creating magenta/purple or suppressing faint structure.**

Candidate-01 (`0.15`) is the starting recommendation because it matches the
successful manual workflow. Candidate-00 is preferred when 0.15 looks visibly
too strong.

Candidate-02 is exceptional. Select it only when 0.10/0.15 leave clearly
unwanted residual green and 0.20 does not introduce magenta/purple or erase
faint structure. The user does not choose the candidate.

## Selection/publication

```text
.../green-reduction select-publish   --project "<project>"   --candidate "<selected>"   --visual-notes "<overall visual comparison>"   --note "candidate-00=<what was actually seen>"   --note "candidate-01=<what was actually seen>"   --note "candidate-02=<what was actually seen>"
```

Repeat `--note` for exactly the eligible candidates returned by `advance`.

If selecting candidate-02, also supply:

```text
--policy-override-reason "<residual green in lower candidates; no magenta/purple in 0.20; faint structure preserved>"
```

The helper infers the run root, eligible list, preview hashes, and review
method. The OpenClaw transcript is the audit proof that Read occurred.

## Fresh rerun

A completed valid canonical requires confirmation. After the exact question is
answered yes:

```text
.../green-reduction confirm-fresh --project "<project>"
.../green-reduction advance --project "<project>"
```

## Canonical outputs

```text
processing/green-reduction/
├── SHO-starless-green-reduced.fit
├── SHO-starless-black-point-before-green-reduction.png
├── SHO-starless-green-reduced.png
└── green-reduction-manifest.json
```

Successful completion must report:

```text
status: ready
helper_version: 1.0.1
canonical_manifest_compatible: true
visual_review_completed: true
next_stage: siril-saturation
saturation_processing_permitted: true
errors: []
```

Stop before saturation.

## Safeguards, preservation, efficiency

Candidates must preserve RGB dimensions and 32-bit float format, finite pixels,
clipping and structural intensity correlation while reducing—not increasing—the
positive green-excess proxy. Arithmetic RGB-mean median change is diagnostic only;
it is not a CIE L* Preserve Lightness gate. Preserve Lightness is enforced by
generating `rmgreen` without `-nopreserve`. Actual Read review decides residual
green versus magenta/purple over-correction.

All three candidates are produced in one Siril process. Existing canonical
results and failed publication staging are preserved; nothing is deleted.
Routine helper responses are capped at 12 KB.

## Verification-only requests

Verification is not processing. When a verification prompt says STOP, stop.
Never continue into candidate generation, image review, publication, saturation,
or later stages.
