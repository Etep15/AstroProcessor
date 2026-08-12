# siril-stretch v1.1.3 testing

Installer tests must pass before publication:

1. exact v1.0.0 scaffold starting hashes
2. canonical AstroProcessor Python has numpy + astropy
3. Siril 1.4.4 AppRun exists
4. reviewed channel-balance input and manifest are internally consistent
5. Python compile checks
6. real-Siril mechanical GHS→BP command self-test (not production aesthetic gating)
7. real-Siril two-round stride-4 M16 policy probe using the actual reviewed channel-balance source
8. non-destructive M16 `advance --plan-only`
9. existing production processing evidence remains byte-for-byte unchanged

After installation, production validation prompt:

```text
Process M16 July 2026 with stretch
```

Expected behavior: autonomous 2–5 round GHS+BP iteration, exact-path review after each round, preserved round winners, final three-candidate review, publication only after final visual selection.
