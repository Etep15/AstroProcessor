---
name: siril-ghs-stretch
description: "Generate bounded first-pass GHS candidates from the reviewed post-StarNet channel-balanced STARLESS image, visually compare balanced candidates, and publish one reviewed result."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Adaptive Siril GHS Stretch — pass 1

Installed helper version: **1.3.1**.

Source-contract revision: **post-starnet-channel-balance-v1**.

## Required pipeline

```text
siril-background-neutralization
→ siril-starnet-removal 1.5.2
→ siril-sho-channel-balance 1.1.0
→ siril-ghs-stretch 1.3.1
→ siril-ghs-stretch-pass2
```

Only:

```text
processing/sho-channel-balance/SHO-starless-linear-balanced.fit
```

is processed. The channel-balance manifest must be helper 1.1.0, ready, visually reviewed, explicitly STARLESS, confirm that the stars layer was not modified, and permit GHS pass 1.

Do not process the StarNet starmask or unscreen-stars layer. They remain unchanged for later independent star processing and recomposition.

The previous direct StarNet → GHS pass-1 canonical checkpoint is intentionally classified **obsolete** under this source-contract revision. It is preserved and is replaced only after a successful new GHS publication from the balanced starless source.

The historical M16 1.3.1 candidate values remain bounded starting/reference values only. Because the source checkpoint has changed, use the helper's measured source-aware metrics and the mandatory visual review on the newly balanced starless image rather than assuming the old M16 candidate ranking will remain identical.

## M16 calibration incorporated into 1.3.1

The preserved 1.3.0 real-M16 probe measured:

```text
candidate-00
D=4.40 B=15.0 SP=0.00400 HP=0.860
median=0.374810
classification=too_strong

candidate-01
D=2.80 B=5.5 SP=0.00543 HP=0.930
median=0.114271
p90=0.118760
p99=0.143899

candidate-02
D=3.05 B=6.5 SP=0.00523 HP=0.925
median=0.142980
classification=too_strong
```

The pass-1 median target is:

```text
0.085
```

Candidate-01 was much closer to that target than candidate-00, but 1.3.0
misclassified it `too_gentle` because it imposed absolute lower p90/p99 floors
that do not fit a background-dominated starless field.

## Source-aware brightness classification

Version 1.3.1 makes the median the primary pass-1 brightness gate.

```text
balanced median: 0.055–0.125
target median:   0.085
too strong:      median > 0.125
too gentle:      median < 0.055
```

Any clipping is `too_strong`.

An output p99 above `0.75` is also `too_strong`.

Absolute lower p90/p99 floors are removed. p90/p99 remain evidence and are used
in the recommendation through their ratio to the median compared with the
source's own percentile ratios.

## Source-relative recommendation

The numerical score now prioritizes:

1. distance of output median from 0.085;
2. preservation of source p90/median shape;
3. preservation of source p99/median shape;
4. luminance correlation;
5. inverse-GHT roundtrip;
6. clipping.

It no longer tries to force every image toward absolute p90=0.35 and p99=0.60.

## Bounded candidates

Maximum total candidates remains **3**.

Candidate-00 remains the historical reference baseline.

When candidate-00 is too strong, candidate-01 remains the measured gentle tier:

```text
D=2.800
B=5.500
SP=max(0.00500, 0.95 × source median)
LP=0
HP=0.930
```

For current M16:

```text
SP≈0.00543
```

When candidate-01 is balanced but more than 15% above the target median,
candidate-02 applies the inverse of the measured M16 stronger-response step:

```text
D = candidate-01 D  - 0.25
B = candidate-01 B  - 1.00
SP= candidate-01 SP + 0.00020
LP=0
HP= candidate-01 HP + 0.005
```

For M16 this resolves to:

```text
D=2.550
B=4.500
SP=0.00563
LP=0
HP=0.935
```

The real 1.3.0 probe showed that the opposite step increased median by about
0.0287. This inverse step is therefore expected to move candidate-01 from
~0.114 toward ~0.085.

## Hard parameter bounds

```text
D:  1.500–5.000
B:  2.000–15.000
SP: 0.00200–0.01200
LP: exactly 0
HP: 0.82000–0.97000
```

## Publication policy

A publication-eligible candidate must be:

```text
technical status: satisfactory
histogram classification: balanced
```

`too_strong` and `too_gentle` candidates are not publishable.

If the final generated candidate is still `too_strong`, the whole run is a
hard stop:

```text
status: needs_adjustment
publication_permitted: false
ghs_pass2_processing_permitted: false
```

If no balanced candidate exists, publication is also blocked.

The `publish` command recalculates the gate.

## Visual selection

When the gate opens, CodeWarrior must inspect every balanced permanent after
preview and select the best actual image. Do not ask Peter or ChatGPT to choose.

The numerical recommendation is advisory.

## Preservation

Candidate generation never changes the current canonical GHS directory.

Successful fresh publication preserves the entire old:

```text
processing/ghs-pass1
```

beneath the new run as:

```text
previous-processing-ghs-pass1/
```

Nothing is deleted.

An existing 1.2.2 result is obsolete under 1.3.1 and cannot permit pass 2.

## Stop point

A valid 1.3.1 publication may set:

```text
next_stage: siril-ghs-stretch-pass2
ghs_pass2_processing_permitted: true
```

but this skill does not execute pass 2.

## Installed GHS skill names

This skill is **GHS pass 1**, and its installed skill name/path is:

```text
siril-ghs-stretch
```

The next installed skill is:

```text
siril-ghs-stretch-pass2
```

If an older manifest or orchestration note uses `siril-ghs-stretch-pass1`,
treat that as a compatibility stage label for this `siril-ghs-stretch` skill.

## v1.3.2 orchestration contract — highest priority

Normal request:

```text
Process M16 July 2026 with GHS stretch pass 1
```

Use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch/bin/ghs-stretch
```

Do not discover Python or helper paths. Do not use `python3`, alternate virtual
environments, `ls`, `find`, `cat`, `grep`, `jq`, globbing, or manual run-root
discovery.

Routine entry point:

```text
.../bin/ghs-stretch advance --project "<project>"
```

`advance` owns completed-stage detection, fresh-run authorization, candidate
generation/resume, run-root tracking and exact visual-review targets.

A completed canonical result never silently reruns. Only after the user
explicitly confirms the returned question:

```text
.../bin/ghs-stretch confirm-fresh --project "<project>"
.../bin/ghs-stretch advance --project "<project>"
```

When `visual_review_required` is returned, OpenClaw Read must be used on every
returned `read_targets[].path` exactly as returned. If any Read fails, STOP and
report that exact path; never inspect directories to recover it.

The numerical recommendation is advisory. Every publication-eligible balanced
candidate must be visually compared.

Selection requires one structured note per eligible candidate:

```text
candidate-NN=stretch:<...>; structure:<...>; color:<...>; noise:<...>; highlights:<...>
```

The helper mechanically rejects missing/vague fields. `structure:` must address
faint nebula/Pillars/dark lanes; `color:` must address SHO colour; `noise:`
must address noise/grain; `highlights:` must address highlight/clipping
appearance.

Publish only through `bin/ghs-stretch select-publish`. Successful v1.3.2
publication writes:

```text
processing/ghs-pass1/visual-selection-record-v1.3.2.json
```

Pass 2 is permitted only when that record validates against the current
canonical output.

The underlying `scripts/ghs_stretch.py` v1.3.1 processing engine remains
byte-for-byte unchanged and is not invoked directly during normal CodeWarrior
operation.
