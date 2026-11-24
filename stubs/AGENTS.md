# Project agent instructions

- Prefer small, reviewable changes.
- Do not commit secrets, `.env`, or credentials.
- Run the project's usual lint/typecheck/tests before considering work done when those commands exist.
- Match existing patterns in nearby files.

## Review focus
- Correctness and regressions
- Security (authz, injection, secret leakage)
- Missing tests for behavior changes
