# siril-star-processing testing

Installer validation should prove:

1. static runtime self-test passes,
2. real-Siril smoke test on the current M16 preserved star FITS generates all 3 candidates,
3. at least one candidate materially reduces saturation and bright-star dominance without added clipping,
4. production evidence remains untouched until installation,
5. the installed runtime reaches `would_generate_candidates` and does not start production by itself.
