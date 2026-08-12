# siril-stretch v1.2.1 testing

## Installation expectations

The installer must:

- verify exact installed v1.1.5 code;
- verify the current v1.1.5 canonical M16 stretch and exact upstream source;
- confirm no active stretch run exists;
- pass a real-Siril synthetic self-test using Independent, Human, and Even GHS color models;
- pass a non-destructive three-round stride-4 M16 calibration probe;
- show materially better color-richness potential than the current v1.1.5 canonical without newly clipped RGB pixels;
- leave all M16 production processing files byte-for-byte unchanged during installation.

## Production rerun

After installation:

```text
Process M16 July 2026 with stretch
```

The existing v1.1.5 canonical should be reported as completed/obsolete and the agent should ask whether to run a fresh stretch. After explicit confirmation, it should autonomously start a fresh v1.2.1 run while preserving the current canonical until successful publication.

## Review focus

Pay particular attention to:

- absolute saturation/chroma, not only relative retention;
- color separation in the nebula;
- whether a grey/lifted background gives richer color and faint structure;
- avoiding an overly black background merely for stronger contrast;
- zero newly clipped RGB pixels;
- whether 3–4 gentler rounds outperform 2 aggressive rounds.
