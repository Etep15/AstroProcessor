# siril-sho-channel-balance v1.0.0 testing

## Installation safety test

The all-in-one installer:

1. verifies the exact current M16 pure SHO FITS and manifest hashes;
2. materializes the skill in a unique staging tree;
3. runs Python/API/policy tests;
4. runs a real-Siril synthetic PixelMath self-test;
5. runs only M16 `advance --plan-only`;
6. verifies the M16 pure SHO FITS and manifest stayed byte-for-byte unchanged;
7. builds an archival v1.0.0 ZIP;
8. installs the tested skill preservation-safely;
9. post-verifies version and M16 plan-only state.

The installer does not create real M16 channel-balance candidates.

## Simple CodeWarrior production test

Send exactly:

```text
Process M16 July 2026 with SHO channel balance
```

Expected behavior:

1. load the installed skill;
2. run/resume `advance`;
3. generate the recovered manual baseline:
   `r=1.00, g=0.25, b=1.00`;
4. Read the exact returned source and candidate preview paths;
5. classify one dominant problem and record all six visual observations;
6. call `review-refine`;
7. repeat as needed, stopping early when balanced or after at most five attempts;
8. Read source and all generated candidates again for final selection;
9. select the best acceptable attempt, not necessarily the last;
10. publish only after structured per-candidate selection notes pass validation;
11. finish `ready`;
12. stop before background neutralization.

CodeWarrior must never invent raw coefficient values.

CodeWarrior must never use `ls`, `find`, `cat`, `grep`, `jq`, globbing or
manual run-directory discovery to locate review files.

## Completed-stage test

After a successful publication, send the same prompt again:

```text
Process M16 July 2026 with SHO channel balance
```

Expected: report that the stage already completed and ask whether to run it
again as a fresh run. It must not silently rerun.

Only if intentionally testing the fresh-run path, answer:

```text
Yes, run it again as a fresh run.
```

The existing canonical result must remain preserved until successful
replacement publication.
