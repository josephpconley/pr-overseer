# Pull request review rules

## Severity
- **blocking**: wrong behavior, data loss/leak, unsafe migration, broken auth
- **important**: likely bug, missing critical test, fragile API contract
- **nit**: style or naming — at most five nits per review

## Do not report
- Issues already enforced by CI (lint, format, types) unless the PR disables those checks
- Generated files, lockfiles, vendored code

## Always check
- New public API / routes have tests or a clear reason they do not
- Error paths and auth boundaries for user-facing changes
- Migrations are forward-safe

## GitHub decision (required)
- Any **blocking** finding → `VERDICT: REQUEST_CHANGES` (ask the author to fix)
- No blocking findings → `VERDICT: APPROVE` (approve with feedback; important notes and nits stay in the body)
- End the review with that exact one-line verdict

## Output format
Rank findings by severity. Cite `path:line`. No essay. If nothing blocking or important, say so in one line, then still emit the VERDICT.
