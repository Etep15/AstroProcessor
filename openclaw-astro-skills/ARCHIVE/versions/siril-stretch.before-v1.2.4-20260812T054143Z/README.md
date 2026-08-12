# siril-stretch v1.2.1

Iterative Siril GHS + incremental BP Stretch phase.

v1.2.1 changes the processing policy after the first v1.1.x M16 production run proved technically safe but too color-muted. It preserves strict no-new-clipping safety while using broader GHS candidates, multiple Siril color models, lifted/incremental BP targets, absolute saturation/chroma metrics, and color-grounded review payloads.

Pipeline: `siril-sho-channel-balance → siril-stretch → siril-green-reduction`.
