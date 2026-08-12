# siril-stretch v1.1.5

Autonomous iterative GHS + Linear Black Point Shift stretch phase.

Public entrypoint: `bin/stretch`

Commands:
- `advance --project NAME [--plan-only]`
- `confirm-fresh --project NAME`
- `select-round --project NAME --run-root PATH --candidate ID --compared ID ... --continue yes|no --note ID=... ...`
- `select-publish --project NAME --run-root PATH --candidate ID --compared ID ... --note ID=... ...`
- `stage-status --project NAME`
- `self-test`

Recovery note: v1.1.5 resumes compatible active v1.1.4 runs and only asks the agent to visually compare technically eligible candidates that have exact preview targets. `--compared` and `--note` are repeatable flags and must be supplied once per reviewed candidate.
