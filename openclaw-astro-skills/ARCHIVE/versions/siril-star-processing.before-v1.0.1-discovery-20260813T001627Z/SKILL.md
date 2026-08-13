# siril-star-processing v1.0.0

## Purpose

This skill processes the preserved Starnet star branch as its own branch stage.

It is intended for SHO / narrowband imaging where star colors in the preserved star layer often do not represent natural stellar colors. The skill therefore aims for:

1. mostly neutral / white stars,
2. substantial reduction of star dominance,
3. stronger suppression of the brightest stars than the dimmest stars,
4. preservation of dim stars and stellar structure where practical,
5. zero clipping and preservation-safe publication.

## Canonical contract

- Upstream expected FITS: `processing/starnet/SHO-stars-unscreen.fit`
- Canonical output FITS: `processing/star-processing/SHO-stars-processed.fit`
- Canonical output manifest: `processing/star-processing/star-processing-manifest.json`
- Downstream stage: `siril-star-recombination`

## Processing model

Three bounded candidates are generated using Siril only.

Each candidate performs two operations on the preserved star FITS:

1. Reduce star dominance using PixelMath soft-knee highlight compression. A target-adaptive luminance threshold preserves dim stars while increasingly compressing brighter star pixels.
2. Desaturate / neutralize star color using `satu amount background_factor 6` as the final transform, so brightness compression cannot re-introduce false channel imbalance.

## Candidate family

- candidate-00: mild neutralization + mild dimming
- candidate-01: balanced neutralization + substantial dimming
- candidate-02: strong neutralization + strong dimming

## Review criteria

Select the candidate with the best balance of:
- neutral / white-looking stars,
- reduced dominance of bright stars,
- retention of dim stars,
- preserved profiles / no ugly halos,
- acceptable overall brightness,
- no clipping.

## Stage behavior

- completed-current -> ask whether to rerun fresh
- completed-obsolete -> ask whether to rerun fresh
- confirmation authorizes exactly one fresh rerun for the current upstream source SHA
- exact-path read targets are provided for visual review
- publication preserves prior canonical output if present
