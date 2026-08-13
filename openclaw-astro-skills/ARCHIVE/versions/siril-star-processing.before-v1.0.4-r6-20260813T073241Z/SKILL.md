---
name: siril-star-processing
description: "Process a preserved StarNet star layer: neutralize false SHO star colors, substantially dim bright stars while retaining faint stars, target residual red/purple halo artifacts around bright stars, review candidates, and publish for star recombination."
---

# siril-star-processing

Orchestration version: **1.0.4 r5**  
Processing engine version: **1.0.4**

## Trigger and ownership

Use this skill when the user asks to process stars after StarNet, including requests such as:

- `Process <project> with star processing.`
- `Run star processing on <project>.`
- `Neutralize and reduce the stars for <project>.`

This skill owns **post-StarNet processing of the preserved star branch**.

**Do not invoke `siril-starnet-removal` for these requests.** StarNet removal is an already-completed upstream stage. This skill consumes its preserved star output; it does not repeat StarNet.

Do not use memory search to decide whether this request means StarNet removal. Route directly to this skill when the request says `star processing`, `process the stars`, `neutralize stars`, or `dim/reduce stars` in the post-StarNet workflow.

## Canonical contract

Upstream stage: `siril-starnet-removal`

Required preserved star input:

- `processing/starnet/SHO-stars-unscreen.fit`

Canonical outputs:

- `processing/star-processing/SHO-stars-processed.fit`
- `processing/star-processing/SHO-stars-processed-before-recombination.png`
- `processing/star-processing/star-processing-manifest.json`
- `processing/star-processing/visual-selection-record.json`

Downstream stage: `siril-star-recombination`

The preserved StarNet star layer is a **branch input**. The completed starless branch is not modified by this skill.

## Processing policy

The v1.0.4 engine keeps the validated bright-star dimming policy unchanged.

All candidates use the same target-adaptive PixelMath soft-knee compression:

- threshold quantile 0.99
- knee strength 0.80
- base scale 0.92

The candidate family isolates only the residual color-artifact cleanup:

- candidate-00 — exact v1.0.3 control
- candidate-01 — direct cleanup of the exact red/purple pixels identified by the validator inside the same bright-star edge ring, with a conservative neutral blend and tiny feather
- candidate-02 — stronger/broader validator-derived artifact cleanup upper bound

The goal is mostly neutral/white-looking stars with substantially reduced bright-star dominance, while preserving faint blue/green stars that are acceptable in a narrowband/SHO context.

In r5, tiny **red edge specks** and **purple fringe pixels** around bright stars are treated as specific defects. The processing mask deliberately reuses the validator's exact spatial ring and hue thresholds so those defect pixels receive a strong direct correction. Faint isolated blue/green stars elsewhere in the field are not defects.

## Autonomous short-prompt workflow

For `Process <project> with star processing`, perform the following workflow autonomously.

### 1. Enter the stage

Run exactly:

```bash
{baseDir}/bin/star-processing begin --project "<project>"
```

Interpret the returned status literally.

- `would_generate_candidates`: immediately continue to step 2. Do not ask the user for permission.
- `confirmation_required`: report that the stage already completed (or is obsolete) and ask the exact fresh-rerun question returned by the skill. Do not rerun until the user confirms.
- `blocked`: report the exact blocker and stop.

### 2. Generate candidates

Run exactly:

```bash
{baseDir}/bin/star-processing advance --project "<project>"
```

When it returns `visual_review_required`, use only the exact `read_targets[].path` values returned by the command.

### 3. Visual review

Read every returned candidate preview using the Read tool.

Hard path rules:

- use each returned path verbatim;
- do not discover candidate files with `ls`, `find`, `grep`, `jq`, globbing, directory scans, or guessed paths;
- if any exact Read fails, stop and report the exact failed path;
- review every eligible candidate before publication.

Compare:

- star color neutrality / whiteness;
- whether tiny **red edge specks** remain around bright stars;
- whether tiny **purple fringe** remains around bright stars;
- bright-star dominance;
- retention of dim blue/green stars;
- star profiles, halos, ringing, or other artifacts;
- overall balance for a nebula-focused image.

### 4. Select and publish

Call the same installed entrypoint with the exact `run_root` returned by `advance`:

```bash
{baseDir}/bin/star-processing select-publish \
  --project "<project>" \
  --run-root "<exact run_root>" \
  --candidate "<selected candidate>" \
  --compared "candidate-00" --note "<specific visual observations>" \
  --compared "candidate-01" --note "<specific visual observations>" \
  --compared "candidate-02" --note "<specific visual observations>"
```

Notes must be specific enough to show that red specks, purple fringe, bright-star dominance, dim-star retention, profiles/artifacts, and overall balance were actually assessed.

After successful publication, report:

- selected candidate;
- canonical output path and SHA-256;
- important visual rationale;
- important preservation/dimming metrics;
- next stage: `siril-star-recombination`.

## Fresh reruns

If `begin` says confirmation is required and the user explicitly confirms, run:

```bash
{baseDir}/bin/star-processing confirm-fresh --project "<project>"
{baseDir}/bin/star-processing advance --project "<project>"
```

Then continue autonomously through exact-path visual review and publication.

Never delete or overwrite the existing canonical result before successful publication of the fresh run.
