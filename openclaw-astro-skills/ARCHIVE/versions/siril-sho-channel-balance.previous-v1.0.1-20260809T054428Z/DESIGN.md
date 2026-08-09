# siril-sho-channel-balance v1.0.1 design

## Purpose

Preserve the existing pure SII→R / Ha→G / OIII→B SHO composition as an
immutable scientific checkpoint and perform subjective-but-bounded PixelMath
channel balancing as its own restartable stage.

## Recovered manual M16 baseline

The successful manual M16 PixelMath operation was:

```text
R = S
G = med(S) + 0.25 * (H - med(H))
B = med(S) + (O - med(O))
```

with result rescaling OFF and summed exposure time OFF.

The generalized form is:

```text
R = med(S) + r * (S - med(S))
G = med(S) + g * (H - med(H))
B = med(S) + b * (O - med(O))
```

At r=1.00 the red formula is exactly equivalent to R=S.

## Why this runs before background neutralization

Channel balance changes the relative SHO signal strengths while anchoring all
three channels to the SII median. Background neutralization should therefore
operate on the selected balanced image and remove only residual local
sky/background offsets.

## Five-attempt adaptive policy

Maximum five candidates; early stopping is preferred when balanced.

- r: 0.70–1.30, max step 0.15
- g: 0.15–0.40, max step 0.05
- b: 0.70–1.30, max step 0.15

Only one coefficient changes between candidates.

The agent classifies the dominant problem; the helper calculates the numeric
move. Immediate reversal is blocked unless the previous move visibly overshot.

## Visual authority

Numeric diagnostics verify that Siril produced the requested bounded PixelMath
formula and a finite 32-bit RGB result. They do not decide aesthetic balance.

CodeWarrior must visually assess green, magenta/purple, SII red/yellow,
OIII blue/cyan, faint structure and weak-channel noise.

The final selected candidate can be any reviewed technically valid candidate;
the latest candidate is never automatically preferred.

## Preservation

Candidate generation never changes `processing/sho/`.

A previous canonical `processing/sho-channel-balance/` directory remains
untouched until successful publication of a replacement. It is then preserved
beneath the successful new run. Nothing is deleted.

## Temporary pipeline bridge

The currently installed `siril-sho-combination` 1.1.1 contract points directly
to background neutralization. v1.0.0 accepts that validated contract as a
temporary legacy bridge so M16 can test this skill without modifying the pure
SHO checkpoint.

After M16 validation, update the adjacent stage contracts:

```text
siril-sho-combination
→ siril-sho-channel-balance
→ siril-background-neutralization
```

## v1.0.1 routing enforcement

The v1.0.0 production test showed that OpenClaw could route the short,
stage-specific prompt to the older broad `astro-processing` skill first. That
skill then attempted project discovery and AstroProcessor operations.

v1.0.1 does not change the PixelMath algorithm, coefficient policy, candidate
policy, Siril commands, quality gates, or publication behavior.

It strengthens the stage skill's routing contract and is installed together
with a high-priority guard in `astro-processing/SKILL.md`. A request naming an
installed stage is never permission to recreate, re-import, reprepare, or
resume earlier stages of the full pipeline.

The canonical existing-project root for CodeWarrior stage skills is:

`/home/peter/.openclaw/workspace/agents/codewarrior/Projects`
