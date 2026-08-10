---
name: siril-starnet-removal
description: "Run StarNet removal through the canonical v1.5.3 context-safe orchestrator, visually compare compact candidate panels, and publish native starless/starmask/unscreen products."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Canonical Siril StarNet Workflow

Installed orchestration version: **1.5.3**.

Processing engine version: **1.5.2** (unchanged).

Source-contract revision:

```text
native-starnet-channel-balance-v1
```

## Pipeline

```text
siril-background-neutralization
→ siril-starnet-removal
→ siril-sho-channel-balance
```

StarNet never hands the starless image directly to GHS.

## Normal entry point

For:

```text
Process <project> with StarNet removal
```

use exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-starnet-removal/bin/starnet-removal advance --project "<project>"
```

Do not inspect the skill directory and do not discover helper/Python paths.

Do not use:

```text
ls
find
cat
grep
jq
globbing
tail/head of helper source
direct reads of scripts/starnet_workflow.py
```

The v1.5.2 processing engine is an internal implementation detail and must not
be invoked directly during normal CodeWarrior operation.

## Completed-stage reruns

A pre-existing canonical StarNet result — including a legacy/obsolete-contract
canonical — is still a completed image-processing result.

`advance` must first return a confirmation question. It must not silently add
`--fresh-run`.

Only after the user explicitly confirms:

```text
.../bin/starnet-removal confirm-fresh --project "<project>"
.../bin/starnet-removal advance --project "<project>"
```

Fresh authorization is durable and tied to the current canonical/source hashes.
The existing canonical result remains untouched until successful publication.

## Candidate generation

The unchanged v1.5.2 engine generates the bounded StarNet set:

```text
candidate-00: target background 0.15, x1
candidate-01: target background 0.10, x1
candidate-02: target background 0.06, x1
candidate-03: target background 0.10, x2
```

All existing StarNet/Siril technical gates remain authoritative.

## Context-safe visual review

After candidate generation, `advance` returns exactly:

- the linked source preview;
- one compact review panel per generated candidate.

Each candidate panel is a fixed 2×2 contact sheet:

```text
top-left:     starless_linear_linked
top-right:    starmask_linked
bottom-left:  starmask_unlinked
bottom-right: unscreen_linked
```

The original individual previews remain preserved in the run as evidence.

Use OpenClaw **Read** on every returned `read_targets[].path` exactly as
returned. Never rediscover a path. If any Read fails, stop and report the exact
failed path.

Evaluate each candidate for:

- significant stars left in the starless image;
- recognizable broad M16 nebulosity in either starmask view;
- removed nebular knots/filaments, holes or other damage;
- halos, seams or other artifacts;
- plausibility/localization of the unscreen star layer.

The numerical recommendation is advisory.

## Review and publication

`advance` returns an exact `review_publish_command_template`.

Provide one `--note` for every generated candidate:

```text
candidate-NN=accepted:<true|false>; remaining_stars:<specific>; broad_nebula:<true|false>; nebula_damage:<specific>; halos:<specific>; observation:<specific visual comparison>
```

Exactly the selected candidate must have `accepted:true`.

Publication is performed only through:

```text
.../bin/starnet-removal review-publish
```

The orchestrator internally creates the legacy v1.5.2 structured review JSON,
asks the engine to validate it, publishes the validated candidate, then verifies
the native downstream contract.

Successful v1.5.3 publication also writes:

```text
processing/starnet/orchestration-review-v1.5.3.json
```

which records the OpenClaw-read contact-panel hashes and structured visual
notes.

## Canonical outputs

```text
processing/starnet/
├── SHO-starless-linear.fit
├── SHO-starmask.fit
├── SHO-stars-unscreen.fit
├── SHO-linear-neutralized-before-linked.png
├── SHO-starless-linear-linked.png
├── SHO-starmask-linked.png
├── SHO-starmask-unlinked.png
├── SHO-stars-unscreen-linked.png
├── visual-review-record.json
├── orchestration-review-v1.5.3.json
└── starnet-manifest.json
```

The starmask is StarNet's native `-m` product. Do not require:

```text
starless + starmask = original
```

Later recomposition uses the screen-aware/unscreen workflow.

## Native downstream contract

Every newly published ready StarNet manifest must report:

```text
source_contract_revision: native-starnet-channel-balance-v1
stage_order.upstream: siril-background-neutralization
stage_order.current: siril-starnet-removal
stage_order.downstream: siril-sho-channel-balance
next_stage: siril-sho-channel-balance
sho_channel_balance_permitted: true
ghs_pass1_permitted: false
starless_processing_permitted: true
starless_background_processing_permitted: false
```

Stop after StarNet publication. Do not automatically run SHO channel balance.
