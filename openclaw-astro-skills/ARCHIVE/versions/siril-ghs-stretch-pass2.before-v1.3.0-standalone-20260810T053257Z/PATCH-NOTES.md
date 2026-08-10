# Patch notes — v1.2.0

A completed canonical GHS pass-2 result now causes an explicit Process request
to ask whether the user wants a fresh rerun.

New commands:

```text
begin
confirm-fresh
```

Incomplete compatible runs still resume automatically.

A fresh rerun of a completed canonical result cannot start until the user's
confirmation is durably recorded. Even `--fresh-run` cannot bypass the gate.

Confirmation state is stored under:

```text
<project>/.siril-ghs-stretch-pass2/stage-intents/
```

Authorization survives an interrupted turn and is consumed only after a new
run manifest is durably created. A later rerun requires a new confirmation.

GHS math, candidate limits, visual review, durable selection, publication,
failed-staging preservation, and final verification are unchanged.
