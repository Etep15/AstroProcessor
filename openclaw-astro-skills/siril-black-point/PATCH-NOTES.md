# siril-black-point v1.0.4 patch notes

## Scope

v1.0.4 changes **selection policy only**. It does not change black-point
candidate generation, Siril parameters, clipping hard gates, or the context-safe
orchestration introduced in v1.0.3.

## Why

The v1.0.3 M16 run selected candidate-01 because its deeper blacks and stronger
contrast were treated as visually desirable.

However:

```text
candidate-00 channel floor clipping ~0.158%  (inside preferred <=0.2%)
candidate-01 channel floor clipping ~0.509%  (eligible, but aggressive)
```

The selected candidate-01 remained below the 0.6% hard ceiling, but it pushed a
substantially larger fraction of channel samples to the floor and visually
suppressed more faint outer M16 emission.

## New policy

- preferred-range candidates receive a meaningful selection preference;
- above-preferred-but-under-hard-limit candidates are labeled `aggressive`;
- darker background / higher contrast are not positive criteria by themselves;
- faint emission and low-contrast dust preservation outrank deeper black;
- when both look acceptable, prefer materially lower channel-floor clipping;
- an aggressive selection while a preferred candidate exists requires a
  specific policy-override reason describing both the visible structural gain
  and preservation of faint emission.

## Efficient migration

A valid v1.0.1-v1.0.3 canonical becomes `needs_reselection`, not
`needs_reprocessing`.

v1.0.4 reuses the already-generated compatible candidate run:

```text
current canonical preserved
old run-manifest copied to immutable evidence backup
same before/candidate PNGs re-read
new selection recorded
successful publication replaces canonical
old canonical preserved under run root
```

No Siril candidate generation is needed for the current M16 correction.

## Additional fix

v1.0.4 also fixes the latent fresh-rerun confirmation variable error in the
v1.0.3 `begin_stage` / `confirm_fresh_run` path. The normal M16 path had not
triggered that defect.
