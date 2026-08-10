---
name: siril-starnet-removal
description: "Generate, visually review, and publish canonical StarNet products from the accepted background-neutralized SHO image."
user-invocable: true
metadata: {"openclaw":{"os":["linux"]}}
---

# Canonical Siril StarNet Workflow

Installed workflow version: **1.5.2**.

Source-contract revision: **native-starnet-channel-balance-v1**.

This update retains the 1.5.1 source contract, native-mask workflow, candidate
set, review requirements, preservation policy, and SHO channel-balance downstream gate.

## Corrected path handling

Siril reported:

```text
Filename too long (max 255 bytes)
```

when the 1.5.1 synthetic self-test attempted to save its native starmask and
unscreen product beneath a path approximately 298 bytes long.

Version 1.5.2:

1. Uses a compact synthetic workspace:
   ```text
   .skill-self-tests/sn/<id>/w/Projects/T/
   ```
2. Calculates the byte length of every expected generated path before StarNet.
3. Blocks before execution when any expected path exceeds 255 bytes.
4. Records the evidence under:
   ```text
   path_budget
   ```

The conservative path check covers:

```text
SHO_starless_stretched.fit
starnetmask_SHO_input_stretched.fit
starnetdescreen_SHO_input_stretched.fit
```

The M16 operational path is comfortably inside the limit.

## Pipeline

```text
siril-background-neutralization
→ siril-starnet-removal
→ siril-sho-channel-balance
```

## Input

```text
processing/background-neutralization/SHO-linear-neutralized.fit
processing/background-neutralization/background-neutralization-manifest.json
```

Required upstream helper: `1.1.0`.

## Candidate set

```text
candidate-00: target 0.15, x1
candidate-01: target 0.10, x1
candidate-02: target 0.06, x1
candidate-03: target 0.10, x2
```

## Completion and log evidence

The broad `starnet: could not` text remains fatal unless Siril exits zero, all
required completion messages exist, and all expected products exist.

Version 1.5.2 therefore does not suppress filename-too-long or other genuine
save failures.

## Visual review

CodeWarrior must inspect the source and every candidate's starless, linked
starmask, unlinked starmask, and unscreen previews. Publication requires the
structured review record.

## Preservation

Successful `--fresh-run` publication preserves the old canonical StarNet
directory beneath the new run. `processing/starnet-native` remains untouched.
Nothing is deleted.

## Downstream

A ready result reports:

```text
next_stage: siril-ghs-stretch-pass1
sho_channel_balance_permitted: true
ghs_pass1_permitted: false
starless_background_processing_permitted: false
```


## Native downstream source contract

Every newly published ready StarNet manifest must report:

```text
source_contract_revision: native-starnet-channel-balance-v1
stage_order.downstream: siril-sho-channel-balance
next_stage: siril-sho-channel-balance
sho_channel_balance_permitted: true
ghs_pass1_permitted: false
starless_processing_permitted: true
starless_background_processing_permitted: false
```

StarNet never hands the starless image directly to GHS. The dedicated SHO channel-balance stage is mandatory and is the only next image-processing stage.
