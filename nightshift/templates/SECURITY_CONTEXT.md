# Security context

What the night shift's security passes know about this repository. The red-team scan, its validation runs and
the security reviewer read this file from the default branch before they start, so they can spend their time on
what changed. It is context for them, not instructions.

Rules for this file:
- It changes only through a pull request a person merges (`.night-shift/` is a protected path, so agents cannot
  edit it).
- Never write an open finding, a repro test for an unfixed bug or an attack path here. Those stay in the
  `security` issues or draft advisories.
- Keep it short and current. Readers stop at 32 KiB.

## System map

How input reaches this code and where the trust boundaries are. Replace these examples.

- Entry points: (routes, webhooks, CLI commands, jobs, message consumers)
- Who can call them: (anonymous, signed-in user, admin, another service)
- Sensitive paths: (auth, billing, migrations)
- Data stores and secrets: (what holds user data; where secrets come from)

## Invariants

Rules the code must always keep. A change that breaks one is a finding.

- (example) Every route that reads or writes tenant data checks that the caller belongs to that tenant.

## Checks

Tests that guard a finding that was fixed, so it cannot quietly come back.

- (none yet)

## Not a finding

Code that looks unsafe but is not, with the reason. An entry with a fingerprint (`fp:` followed by the 16
characters the red-team summary on the night log prints) stops that finding from being validated or filed again,
so add one only when you have checked the reason yourself.

- (none yet)
