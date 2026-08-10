# GHS pass-2 v1.2.0 — fresh rerun confirmation

`workflow-state` remains the mechanical resumability detector.

`begin` adds user-intent semantics:

```text
incomplete compatible run → resume
no ready canonical        → start immediately
ready canonical           → confirmation_required
```

`confirm-fresh` records an authorization bound to project, pass-1 source SHA,
and current canonical pass-2 SHA.

`run` requires that authorization whenever a completed canonical result exists;
`--fresh-run` cannot bypass it.

Authorization remains active across interruption until the new run manifest is
durably written, then becomes consumed.
