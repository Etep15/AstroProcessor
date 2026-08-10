---
name: siril-ghs-stretch-pass2
description: "Run or resume the complete second GHS stretch stage. New stages run autonomously; incomplete runs resume automatically; a previously completed stage requires user confirmation before a fresh rerun."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Siril GHS Stretch — complete pass-2 stage

Helper version: **1.2.0**

## User-level invocation

A normal request is:

```text
Process M16 July 2026 with GHS pass 2.
```

The skill owns all low-level orchestration.

## Mandatory first command for an explicit Process request

For every explicit request to process or run GHS pass 2, the first helper
command is:

```text
ghs_pass2.py begin --project "<project>"
```

This is mandatory.

Do not call `status` first to decide whether to honor a process request.

Do not inspect the canonical manifest and independently conclude
"already complete."

`status` is for status questions and final verification. `begin` defines what
an explicit processing request means.

## `begin` outcomes

### No prior completed canonical result

Proceed immediately through the complete stage. Do not ask the user anything.

### Compatible incomplete run exists

Resume automatically.

Do not ask whether to run fresh.
Do not regenerate completed candidates.

### Completed canonical result exists

`begin` returns:

```text
status: confirmation_required
action: confirm_fresh_run
confirmation_required: true
question: "GHS pass 2 for <project> has already completed successfully. Do you want me to run it again as a fresh run?"
```

Stop processing and ask the user that question.

Do not create candidates yet.

Do not merely report that the stage is complete; the explicit Process request
requires completion status plus the fresh-run question.

## User confirms fresh rerun

When the next user reply clearly confirms the fresh rerun, run:

```text
ghs_pass2.py confirm-fresh --project "<project>"
```

Then continue the complete stage in the same turn.

Do not ask the user again.

The confirmation is durable and bound to the current pass-1 source and current
canonical pass-2 checksum.

If execution is interrupted after confirmation, the next `begin` recovers the
authorization and continues without another confirmation.

## User does not confirm

Do not run a fresh GHS pass 2.
Leave the existing canonical result untouched.

## Candidate generation

After `begin` permits a new run:

```text
ghs_pass2.py run \
  --project "<project>" \
  --max-candidates 3 \
  --timeout 7200
```

For a rerun of a completed result, `run` requires durable `confirm-fresh`
authorization.

Neither a bare `run` nor `run --fresh-run` can bypass the confirmation gate.

The existing canonical result remains untouched during candidate generation.

## Visual review and durable selection

CodeWarrior visually compares every publication-eligible candidate at the same
display scale. The user does not choose the candidate.

Persist CodeWarrior's decision before publication with `select`, listing every
eligible candidate as compared.

## Publication

Publish the durable selection. Failed `publish-staging` is preserved
automatically before retry.

## Interruption recovery

Always re-enter through `begin`.

```text
confirmed but generation not completed
→ do not ask again

candidates complete
→ resume review

selection complete
→ resume publication

completed canonical from a previous invocation
→ ask before another fresh rerun
```

## Processing policy

Unchanged:

```text
preferred median:       0.180
balanced median:        0.135–0.225
maximum p99:            0.80
maximum pixel value:    0.97
clipping permitted:     none
preferred luma corr:    >= 0.97

D:  0.70–3.50
B:  0.50–7.00
SP: 0.040–0.180
LP: exactly 0
HP: 0.860–0.990

maximum candidates: 3
```

## Final verification

After publication always run `status`.

Require actual ready status, completed visual review, black-point permission,
no errors, and canonical FITS SHA equal to the selected candidate SHA.

Then stop before black point.
