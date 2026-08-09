# siril-sho-channel-balance v1.1.0 testing

Installation must prove, without processing M16:

1. installed v1.0.1 helper matches the validated SHA-256;
2. current M16 StarNet starless FITS and StarNet manifest checksums match the validated checkpoint;
3. existing pre-StarNet channel-balance canonical remains byte-for-byte unchanged;
4. helper syntax/API/policy/routing/post-StarNet contract tests pass;
5. real-Siril synthetic self-test runs on a synthetic STARLESS source and publishes a starless canonical;
6. M16 `advance --plan-only` points only to `processing/starnet/SHO-starless-linear.fit` and reports `stars_layer_modified: false`;
7. existing v1.0.1 canonical is obsolete/migratable rather than treated as completed v1.1.0;
8. no real M16 channel-balance candidate is generated during installation.

Production prompt:

```text
Process M16 July 2026 with SHO channel balance
```

The returned Read targets must be starless. After successful publication the output is `SHO-starless-linear-balanced.fit`, GHS pass 1 is permitted, and the old full-image v1.0.1 result is preserved under the new run.
