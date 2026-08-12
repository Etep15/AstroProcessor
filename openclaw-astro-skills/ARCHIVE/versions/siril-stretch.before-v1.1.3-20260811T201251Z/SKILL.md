# siril-stretch skill

Version: 1.0.0

## Purpose

`siril-stretch` is the **single Stretch phase** for the Siril SHO pipeline. It replaces the conceptual separation of `siril-ghs-stretch`, `siril-black-point`, and `siril-ghs-stretch-pass2` with one iterative stretch phase built around repeated **GHS + Linear Black Point Shift** cycles.

Pipeline position:

```text
siril-sho-channel-balance
→ siril-stretch
→ siril-green-reduction
```

## Core model

Each round consists of:

1. Analyze the current image and histogram.
2. Generate 3 paired candidates for the round:
   - candidate-00: conservative
   - candidate-01: balanced
   - candidate-02: stronger safe stretch
3. For each candidate, run:
   - `ghs-pass`
   - then `black-point-pass`
4. Analyze technical metrics and prepare exact-path visual review.
5. Select the round winner.
6. Decide whether to stop or run another round.

Repeat for **minimum 2 rounds**, **maximum 5 rounds**.

## Guiding principles

- GHS and BP shift are treated as one combined iterative operation.
- The goal is **progressive histogram widening**, not brightness alone.
- Rough target guidance:
  - meaningful data should progressively occupy more of the histogram,
  - histogram peak often trends toward the lower quarter,
  - main data body often trends toward the lower half,
  - but these are guides, not hard rules.
- The skill must allow a somewhat lifted grey background if that produces a stronger nebula with better color richness and contrast.
- The skill must fail closed if clipping or highlight damage becomes unsafe.

## Hard safety rules

- Do not clip meaningful shadow data with the BP shift.
- Do not clip highlights or compress bright structures excessively.
- Preserve color richness, hue separation, and local contrast.
- Preserve faint nebular structure and dark-lane definition.
- Use Siril for the actual transformations.
- Normal GHS mode should use:
  - `-clipmode=rgbblend`
  - `-even`
  unless future evidence shows a different mode is superior.

## Round candidate design

Each round starts from the current round input image and creates **three complete GHS→BP candidates**.

Example round structure:

```text
Round N input
  ├── candidate-00: GHS conservative → BP conservative
  ├── candidate-01: GHS balanced     → BP balanced
  └── candidate-02: GHS stronger     → BP stronger-safe
```

The winner of each round becomes the input to the next round.

## Technical analysis requirements

After each candidate, measure or estimate:

- histogram position and spread
- p01 / p05 / p10 / p50 / p90 / p95 / p99
- spread metrics such as `p95-p05` and `p99-p01`
- clipping counts / percentages
- highlight headroom
- luminance correlation vs. round input
- color preservation / saturation / chroma / hue stability as available

## Visual review criteria

The exact-path review must explicitly evaluate:

- overall brightness
- nebular contrast
- color richness / color separation
- faint structure visibility
- dark lane preservation
- highlight preservation
- background quality
- whether another round would likely improve the image safely

## Stop rules

Stop if any of the following become true:

- the image is technically eligible and visually strong,
- another round does not materially improve the image,
- another round makes the image flatter, washed out, clipped, or less natural,
- the maximum of 5 rounds has been reached.

## Final candidate review

Preserve the winners of all successful rounds. At the end, prepare **3 final candidates** selected from the round winners:

- best earlier checkpoint
- best balanced checkpoint
- strongest safe checkpoint

Run a final exact-path review and select the winner for publication and handoff.

## Inputs and outputs

### Expected input
Reviewed starless image from `siril-sho-channel-balance`.

### Expected output
Published stretch result suitable for handoff to `siril-green-reduction`.

## Manual operator prompts

For autonomous use, the short entry prompt should be:

```text
Process <project name> with stretch
```

For fresh reruns of an already-completed stretch stage, the skill must report completion and request confirmation before replacing the canonical result.
