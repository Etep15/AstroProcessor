# siril-sho-channel-balance v1.1.0 design

## Purpose

Move bounded PixelMath SHO channel balancing from the full star+nebula image to the reviewed StarNet starless checkpoint. The v1.0.1 production result demonstrated that the nebula colour could be improved substantially, but changing Ha-derived green on the complete RGB image also changed stellar RGB ratios and produced magenta stars.

## Pipeline

```text
siril-sho-combination
→ siril-background-neutralization
→ siril-starnet-removal
→ siril-sho-channel-balance
→ siril-ghs-stretch-pass1
```

The star layer is outside this stage and is never altered.

## PixelMath

```text
R' = med(R) + r*(R-med(R))
G' = med(R) + g*(G-med(G))
B' = med(R) + b*(B-med(B))
```

R/G/B are SII/Ha/OIII-derived StarNet starless channels. Baseline r=1.00, g=0.25, b=1.00. Bounds, one-coefficient movement, five-attempt limit, overshoot reversal gate, visual review, and preservation-safe publication are unchanged from v1.0.1.

## Migration

The pre-StarNet v1.0.1 canonical is intentionally classified obsolete but recognized as a migratable predecessor. It stays in place during candidate generation and is preserved under the successful v1.1.0 run only when a new starless canonical is published.

## Downstream

The published v1.1.0 manifest is the sole source contract for GHS pass 1 after this migration.

## Native StarNet source contract

Only StarNet source-contract revision `native-starnet-channel-balance-v1` is accepted. Direct StarNet -> GHS compatibility is intentionally removed.
