# siril-saturation v1.0.0

Standalone autonomous saturation stage for the AstroProcessor SHO starless pipeline.

## Stage contract

Upstream: `siril-green-reduction`

Required canonical input:

- `processing/green-reduction/SHO-starless-green-reduced.fit`
- `processing/green-reduction/green-reduction-manifest.json`

The upstream manifest must be `ready`, must explicitly hand off to `siril-saturation`, and must permit saturation processing.

## Processing policy

Siril 1.4.4 performs all image transforms. Saturation is adjusted on the image saturation channel with inverse GHT.

Candidates:

- candidate-00: no change
- candidate-01: mild recovery, `invght -D=0.350 -B=0 -SP=0.500 -HP=0.750 -clipmode=rgbblend -sat`
- candidate-02: moderate recovery, `invght -D=0.700 -B=0 -SP=0.500 -HP=0.700 -clipmode=rgbblend -sat`

The no-change candidate is always available so the agent may decide saturation is already sufficient. Visual review is authoritative.

## Review

The agent must Read every exact returned preview path. It must compare color richness, oversaturation/color artifacts, and preservation of nebular structure/background. Publication is only allowed after explicit review of every eligible candidate.

## Stable output

- `processing/saturation/SHO-starless-saturated.fit`
- `processing/saturation/SHO-starless-green-reduced-before-saturation.png`
- `processing/saturation/SHO-starless-saturated.png`
- `processing/saturation/saturation-manifest.json`
- `processing/saturation/visual-selection-record.json`

The downstream stage is intentionally left unassigned in v1.0.0; defining the next stage is a separate pipeline decision.
