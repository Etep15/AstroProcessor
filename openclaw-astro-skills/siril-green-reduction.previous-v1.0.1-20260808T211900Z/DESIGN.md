# siril-green-reduction v1.0.1 design

## Siril basis

Siril 1.4.4 documents `rmgreen [-nopreserve] [type] [amount]` for Remove Green
Noise. Type 2 is Maximum Mask. Preserve lightness is the default and is
recommended by Siril. The tool is intended for stretched/non-linear color
images, and the documentation warns that the amount must be chosen cautiously
to avoid a magenta cast.

Official references:
- https://siril.readthedocs.io/en/stable/Commands.html
- https://siril.readthedocs.io/en/stable/processing/colors.html

## Pipeline contract

Input: `processing/black-point/SHO-starless-black-point.fit`

Output: `processing/green-reduction/SHO-starless-green-reduced.fit`

The upstream contract requires black-point v1.0.4 and selection-policy v1.0.4.

## Candidate strategy

```text
0.10 conservative
0.15 successful manual baseline
0.20 assertive bounded comparison
```

All use Maximum Mask and preserve lightness. 0.15 is the numerical starting
recommendation; actual rendered-image review owns the final decision. Selecting
0.20 requires a specific override rationale.

## Efficiency

One Siril script generates the common before preview and all three candidate
FITS/after previews, so candidate generation uses one Siril process rather than
separate processing and preview processes for each candidate.

State discovery and review planning use durable manifests and preview hashes.
Full FITS/SHA validation occurs before generation and again before publication.

## Technical diagnostics

```text
positive green excess = max(G - max(R,B), 0)
magenta pressure       = max((R+B)/2 - G, 0)
```

These are diagnostics only. Actual Read review decides whether residual green
is artificial and whether magenta/purple over-correction has appeared.

v1.0.1 correction: arithmetic RGB-mean median change is also diagnostic only and
is not used as a CIE L* Preserve Lightness gate. Preserve Lightness is enforced
by omitting `-nopreserve` from every generated `rmgreen` command.

## Recovery

Selection is durable before publication. `advance` resumes an interrupted
publication without repeating image review. A completed canonical requires
explicit fresh-run confirmation.
