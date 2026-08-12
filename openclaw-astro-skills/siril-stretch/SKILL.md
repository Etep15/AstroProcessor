---
name: siril-stretch
description: "Autonomous iterative Siril Stretch phase using repeated GHS + incremental Linear Black Point Shift rounds, absolute/normalized color analysis, exact-path visual review, bounded repetition, final three-candidate selection, and green-reduction handoff."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Stretch

Orchestration version: **1.2.5**  
Processing engine version: **1.2.5**

## Pipeline contract

```text
siril-sho-channel-balance
→ siril-stretch
→ siril-green-reduction
```

Historical fixed GHS1 / standalone black-point / GHS2 skills remain untouched.

## Why v1.2.4 exists

v1.2.2 recovered most of the M16 chroma but the selected result was still somewhat over-stretched and pastel: median luminance rose faster than normalized channel separation. v1.2.3 correctly fixed luminance placement in its calibration probe but lost too much saturation/chroma because it weakened the proven Round-2 GHS energy. v1.3.0 was separately rejected by its calibration gate and was never installed.

v1.2.4 therefore makes a narrower change:

- preserve the proven v1.2.2 Round-1 family exactly;
- keep Round-2 GHS energy near the successful broad Independent branch rather than reducing D sharply;
- search **pivot placement and low protection** around that branch;
- make Round-2 BP a fraction of the actual post-GHS minimum, so it approaches the strongest mathematically safe black-point shift while remaining below every RGB value;
- score `color_contrast_index = chroma_median / median_luminance` strongly and penalize luminance-only expansion;
- make Rounds 3-5 micro-refinements rather than new large stretches.

The installer performs a non-destructive real-Siril M16 grid search before installation and writes the three selected **relative** Round-2 profiles into the installed engine. The M16 reference is calibration evidence, not a universal histogram target.

## Required autonomous workflow

For `Process <project> with stretch`:

1. Run exactly `bin/stretch advance --project "<project>"`.
2. If `confirmation_required`, report the completed canonical and ask whether to run a fresh stretch. Do not process yet.
3. After explicit confirmation run exactly:
   - `bin/stretch confirm-fresh --project "<project>"`
   - `bin/stretch advance --project "<project>"`
4. For every `visual_review_required` round:
   - Read every returned `read_targets[].path` verbatim.
   - Never discover/recover paths with `ls`, `find`, `grep`, `jq`, globbing, or directory browsing.
   - Review only `compared_candidate_order`; technically rejected candidates may intentionally have no preview.
   - Reconcile the image with absolute `saturation_median`, `chroma_median`, `color_contrast_index`, median, p99.9 and clipping.
   - Do not call a result color-rich merely because a retention ratio is above 1.0.
   - A lifted background is acceptable only when it buys real structure/color separation; a brighter neutral baseline by itself is not improvement.
   - Repeat `--compared` and `--note` once per returned candidate in exact order.
   - Round 1 must continue. From Round 2 onward, stop when another cycle is likely to raise luminance/upper tail more than it improves channel separation or useful structure. Round 5 is the hard ceiling.
5. For final review, read all three exact targets and use the images plus saturation, chroma, `color_contrast_index`, median and p99.9. A later round does not automatically win.
6. Publish only after `select-publish` returns `status: ready`.

## Round-2 refinement model

The installed candidate family is selected during installation from a bounded real-Siril search around the proven v1.2.2 branch. Each installed profile is relative to the current image analysis, not an absolute M16 histogram target.

Conceptually the three candidates span:

```text
candidate-00: conservative broad Independent refinement
candidate-01: balanced broad Independent refinement
candidate-02: stronger color-contrast / shadow-protected Independent refinement
```

All three are complete GHS+BP attempts. The engine records the actual GHS parameters, BP strategy, absolute metrics and strict clipping evidence.

## Round-2 BP rule

Round 2 no longer asks a percentile-floor heuristic to decide the strongest BP. It proposes:

```text
BP = actual_post_GHS_minimum × calibrated_fraction
```

with the calibrated fraction constrained below 1.0 and the normal full-resolution safety/backoff still authoritative. This matters because v1.2.2 was already very close to the strongest BP allowed by the zero-new-clipping rule; the remaining improvement must come mainly from GHS geometry, not by blackening real data.

## Safety rules

- Siril performs every GHS and BP transformation.
- **Zero newly clipped RGB pixels** are permitted from GHS or BP.
- Existing zeros/ones in the source are not counted as newly clipped.
- If GHS clips, back off GHS before BP.
- If BP clips, back off BP.
- Preserve highlight headroom and luminance ordering.
- Preserve every successful round winner.
- Preserve the existing canonical until a fresh run has been fully reviewed and successfully published.

## M16 advisory calibration context

For the exact M16 July 2026 source checksum, review payloads may expose the successful manual reference. It is advisory only. Useful comparison values include approximately:

```text
median luminance       0.2096
p99.9                  0.5666
saturation median      0.3219
chroma median          0.0822
color contrast index   0.3920
```

These are not hard runtime targets for another object.

## Final publication hard gate

Final visual selection is **not** completion. Only `select-publish` returning `status: ready` means the stage is complete and green reduction is permitted. If publication is blocked, correct the call and retry; never summarize success first.
## v1.2.5 highlight-protection refinement

v1.2.4 recovered the desired SHO colour separation and selected a Round-2 result with a good global median, but full-resolution review showed the brightest pillar/nebular structures were still pushed slightly too hard. v1.2.5 does not broadly dim the image or weaken the successful colour-producing Round-2 energy. Instead it searches GHS `HP` (highlight protection) around the validated v1.2.4 geometry. Lower HP values protect/darken the bright end while leaving the lower/mid histogram substantially closer to the successful v1.2.4 result.

For M16 calibration, p95/p99.9 and visible bright-pillar structure now have explicit standing alongside median, saturation, chroma, and color_contrast_index. The manual reference remains advisory and zero newly clipped RGB pixels remains mandatory.

## v1.2.5 r4 selection refinement

r4 changes **selection/scoring only**. Candidate generation, the three calibrated Round-2 profiles, GHS/BP execution, clipping safety, and publication semantics are unchanged from functional v1.2.5 r3.

When two technically safe candidates have comparable color, prefer the one that protects the bright upper tail **without unnecessarily lowering the whole-image midtone level**. A lower median is not automatically better. In particular, do not select a globally dimmer HP=1.0 result merely because its numerical score benefits from lower p95; give equal standing to preserved midtone brightness, chroma, p99.9/headroom, and visible bright-pillar structure. For the validated M16 calibration family, the highlight-protected profile is the intended recommendation when its zero-clipping metrics remain in the validated neighborhood.

