# siril-black-point v1.0.4 design

## Two recommendations are now distinct

The helper preserves both:

```text
numerical_recommended_candidate
recommended_candidate
```

The first is the original histogram/score recommendation.

The second is the v1.0.4 selection-policy recommendation, which first favors
technically valid candidates inside the preferred channel-floor clipping range.

For current M16:

```text
numerical: candidate-01
policy:    candidate-00
```

## Policy classification

```text
preferred:  technically eligible and channel floor <= 0.2%
aggressive: technically eligible and 0.2% < channel floor <= 0.6%
```

The hard publication gate remains unchanged.

## Reselection without reprocessing

A pre-v1.0.4 canonical with valid review evidence is marked
`needs_reselection`.

`workflow_state` finds the canonical's original published run. `advance`
preserves its run-manifest and reopens that same run for selection. Candidate
FITS and previews are not regenerated.

Publication remains preservation-safe: the current canonical stays in place
until a new selection is durably recorded and successfully published.

## Aggressive override

Selecting an aggressive candidate while a preferred candidate exists is still
possible, but requires an explicit reason. This preserves genuine visual
judgment while preventing "darker is better" from silently overriding the
preferred clipping policy.
