---
name: siril-stretch
description: "Autonomous iterative Siril Stretch phase using repeated GHS + incremental Linear Black Point Shift rounds, numeric color-richness analysis, exact-path visual review, bounded repetition, final three-candidate selection, and green-reduction handoff."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril Stretch

Orchestration version: **1.2.2**  
Processing engine version: **1.2.1**

## Pipeline contract

```text
siril-sho-channel-balance
→ siril-stretch
→ siril-green-reduction
```

The historical fixed GHS1 / standalone black-point / GHS2 skills are not invoked by this stage and must remain untouched while `siril-stretch` is validated.

## Why v1.2.1 exists

The first v1.1.x production result was technically safe but too contrast-aggressive and visibly color-muted. Its final FITS had substantially lower absolute saturation/chroma than the user's successful manual iterative Siril stretch, while a few highlights were pushed farther right. v1.2.1 therefore changes **stretch policy**, not just orchestration:

- do not drive the first BP almost to zero;
- allow and intentionally explore a lifted grey background;
- use gentler repeated GHS+BP cycles instead of trying to finish in two aggressive rounds;
- explore Siril `independent`, `human`, and `even` color stretch models across three CLI-safe broad/low-locality candidates;
- use broad GHS locality (`B` near the successful manual range) rather than concentrating all contrast with large positive B;
- measure absolute saturation and chroma, not only retention relative to the immediately preceding image;
- ground the autonomous visual review in numeric color metrics.

The M16 manual reference is **advisory calibration evidence only**, never a hard runtime target for another object.

## Required autonomous workflow

For `Process <project> with stretch`:

1. Run exactly `bin/stretch advance --project "<project>"`.
2. If `confirmation_required`, report that a completed canonical stretch already exists and ask whether to run a fresh stretch. Do not process yet.
3. After explicit confirmation run exactly:
   - `bin/stretch confirm-fresh --project "<project>"`
   - `bin/stretch advance --project "<project>"`
4. For every `visual_review_required` round:
   - Read every returned `read_targets[].path` verbatim.
   - Do not discover or repair paths with `ls`, `find`, `grep`, `jq`, globbing, or directory browsing.
   - Review only `compared_candidate_order`; technically rejected candidates may intentionally have no preview.
   - Reconcile the image with `candidate_metrics`, especially absolute `saturation_median`, `chroma_median`, histogram placement, headroom, and clipping.
   - Never describe a candidate as richly saturated merely because its saturation retention ratio exceeds 1.0. Absolute saturation/chroma and the visible palette matter.
   - A dark/black background is **not** automatically better. A lifted grey background is explicitly acceptable when it preserves richer color, faint structure, and useful nebular contrast.
   - Repeat `--compared` once for each returned candidate in exact order and repeat `--note` once per candidate.
   - Round 1 must continue. From round 2 onward, continue whenever another safe GHS+BP cycle is likely to improve brightness, contrast, color richness, or useful histogram width. Do not stop merely because contrast is already strong. Round 5 is the hard ceiling.
5. For the final review, read all three exact targets and use `candidate_metrics` plus the images. The final panel deliberately includes checkpoints emphasizing overall technical balance, color richness, and useful brightness. A later round does not automatically win.
6. Publish only after `select-publish` returns `status: ready`.

## Hawthorne Siril 1.4.4 CLI locality compatibility

The successful manual M16 FITS records negative GUI `local` values, but the actual
Hawthorne Siril 1.4.4 CLI rejects negative `ght -B` arguments. Therefore this
headless skill must not pass negative `-B` values. It uses `B=0` as the broadest
CLI-safe GHS candidate and low positive B variants for additional locality. The
manual negative GUI values remain calibration evidence only, not literal CLI
parameters.

## Processing model

Each round is:

```text
Analyze current image
→ generate 3 deliberately different GHS candidates
→ Siril GHS with strict full-resolution clipping safety/backoff
→ propose an incremental BP that leaves useful low-end room
→ Siril Linear BP shift with strict no-new-clipping backoff
→ analyze histogram + absolute saturation/chroma
→ exact-path visual review
→ select winner
→ repeat if useful, maximum 5 rounds
```

The candidate family intentionally explores:

```text
candidate-00: broad locality, Independent color model, most lifted BP target
candidate-01: broad/balanced locality, Human-weighted color model, balanced BP target
candidate-02: moderate locality, Even-weighted color model, stronger BP target
```

Those are exploration profiles, not assumptions that one model is globally superior.

## Safety rules

- Siril performs every GHS and BP transformation.
- Zero newly clipped RGB pixels are permitted from GHS or BP.
- If GHS clips, back off GHS strength before attempting BP.
- If BP clips, back off BP.
- A zero BP is valid when the current GHS already leaves a suitable low-end placement.
- Preserve highlight headroom and luminance ordering.
- Preserve relative saturation and chroma; reject true washout.
- Preserve every successful round winner.
- Preserve the existing canonical result until a new fresh run has been fully reviewed and successfully published.

## M16 calibration context

For the exact M16 July 2026 upstream source checksum, the review payload includes metrics from the user's successful manual Siril stretch. These values are advisory only. They are present to prevent the agent from calling a visibly muted result "excellent" when the same source has already demonstrated much richer color through iterative GHS/BP processing.
## Final publication hard gate (v1.2.2)

The final visual selection is **not** completion. Publication is complete only when the runtime itself returns `status: ready`.

For final review:

- Read all returned final targets verbatim.
- Call `select-publish` with `--candidate` (or the accepted alias `--selected`).
- **Omit `--compared` for final publication.** The runtime derives and records the exact final candidate order automatically.
- Repeat `--note` once per final candidate. Each note must include `brightness:`, `contrast:`, `color:`, `structure:`, `background:`, `highlights:`, and `overall_balance:` with substantive observations.
- If `select-publish` returns `blocked` or exits non-zero, correct the call and retry. Do not tell the user the stage completed, do not announce green-reduction readiness, and do not abandon the active run.
- Only after `select-publish` returns `status: ready` may the agent report the canonical output and next-stage readiness.
- If a publication call has failed and state is uncertain, run `stage-status --project "<project>"`; an active run awaiting final review means publication has **not** happened.

This hard gate exists because v1.2.1 once finished all image processing and final visual review but publication failed on CLI/note validation while the agent incorrectly reported success. v1.2.2 must resume that preserved final-review state without rerunning the stretch rounds.

