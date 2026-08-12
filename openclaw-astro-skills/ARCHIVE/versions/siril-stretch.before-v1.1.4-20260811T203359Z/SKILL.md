---
name: siril-stretch
description: "Autonomous iterative Siril Stretch phase using repeated GHS + Linear Black Point Shift rounds, technical analysis, exact-path visual review, bounded repetition, final three-candidate selection, and green-reduction handoff."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Stretch

Orchestration / engine version: **1.1.3**

## Pipeline contract

```text
siril-sho-channel-balance
→ siril-stretch
→ siril-green-reduction
```

The old fixed concepts `siril-ghs-stretch`, `siril-black-point`, and `siril-ghs-stretch-pass2` are **not** invoked by this stage. They remain installed and untouched while this replacement is production-validated.

## Required autonomous workflow

For `Process <project> with stretch`:

1. Run exactly `bin/stretch advance --project "<project>"`.
2. If `confirmation_required`, tell the user the stage already completed and ask whether to run it again fresh. Do not process yet.
3. After explicit confirmation run exactly:
   - `bin/stretch confirm-fresh --project "<project>"`
   - `bin/stretch advance --project "<project>"`
4. When `visual_review_required` with `review_scope: round`:
   - Read **every** returned `read_targets[].path` verbatim using OpenClaw Read.
   - Do not discover or repair paths with `ls`, `find`, `grep`, `jq`, globbing, or directory browsing.
   - Compare all three candidates autonomously.
   - Evaluate brightness, nebular contrast, color richness/separation, faint structure, dark lanes/target-specific structure, background quality, and highlight safety.
   - Histogram locations such as a peak around the lower quarter or useful data extending toward the lower half are **rough guides only**, never hard targets.
   - A lifted grey background is acceptable when it yields a stronger nebula, better contrast, and richer retained color without unsafe clipping.
   - Call `select-round` using the exact run root, exact candidate order from the response, structured notes for every candidate using `brightness:...; contrast:...; color:...; structure:...; background:...; highlights:...`, and `--continue yes|no`.
   - Round 1 must continue. From round 2 through round 4, continue only when another GHS+BP cycle is likely to materially improve the image safely. Round 5 is the hard ceiling.
5. Repeat review/selection autonomously until the runtime returns `review_scope: final`.
6. Read all three final targets verbatim and compare them. A later round does not automatically win.
7. Call `select-publish` with the selected final candidate, all three exact `--compared` values, and substantive notes for every candidate.
8. Stop only after publication reports `status: ready`, or on a real contract/read/processing blocker.

## Processing model

Every round is:

```text
Analyze current image
→ generate 3 GHS candidates
→ run Siril GHS (-clipmode=rgbblend -even)
→ analyze each GHS output
→ calculate safe BP left of meaningful data
→ run Siril Linear Stretch BP shift (-clipmode=rgbblend)
→ analyze final paired candidates
→ exact-path visual review
→ select round winner
→ optionally repeat
```

Minimum rounds: **2**. Maximum rounds: **5**.

Siril performs every GHS and BP transformation. Python performs orchestration, metrics, provenance, safety gating and publication only.

## Safety rules

- Never deliberately clip meaningful shadow data to hit a histogram target.
- BP is proposed from the actual post-GHS histogram, then automatically backed off and re-tested in Siril until clipping/headroom gates are safe; a failed BP proposal never forces publication.
- Never permit highlight clipping or loss of required headroom.
- Preserve color richness and hue separation; reject washed-out candidates.
- Preserve faint structure and local contrast.
- Preserve every successful round winner.
- If a later round is worse, retain and allow an earlier round to win.
- Final publication is chosen from a three-candidate panel assembled from preserved successful processing checkpoints.
- Preserve an existing canonical stretch until the new publication is fully staged and validated.

## Current migration note

During validation, the current reviewed `siril-sho-channel-balance` canonical is accepted directly by exact canonical path and checksum even though the older channel-balance manifest may still name the historical GHS1 downstream. Do not modify channel balance or green reduction until this stretch stage is validated on M16.
