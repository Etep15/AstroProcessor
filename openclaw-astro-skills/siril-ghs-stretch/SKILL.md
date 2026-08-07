---
name: siril-ghs-stretch
description: "Generate at most three source-aware first-pass GHS candidates from the reviewed StarNet starless image, block unsuitable candidate sets, visually compare balanced candidates, and publish one reviewed result."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Adaptive Siril GHS Stretch — pass 1

Installed helper version: **1.3.1**.

## Required pipeline

```text
siril-starnet-removal 1.5.2
→ siril-ghs-stretch-pass1 1.3.1
→ siril-ghs-stretch-pass2
```

Only:

```text
processing/starnet/SHO-starless-linear.fit
```

is processed.

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
