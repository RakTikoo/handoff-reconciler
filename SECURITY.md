# Security policy

Handoff Reconciler reads handoff files written by other sessions and,
only when explicitly asked with `--run-verify`, runs the verify
command recorded in a handoff. A handoff file that can deceive the
checker, crash it, or get a command executed without `--run-verify`
defeats the tool's purpose.

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue:

- Use GitHub's private vulnerability reporting for this repository:
  **Security → Report a vulnerability** on
  https://github.com/RakTikoo/handoff-reconciler
  (GitHub Security Advisories). This keeps the report visible only to
  the reporter and the maintainer until a fix is available.

If private vulnerability reporting is not available to you, open a
minimal public issue saying you have a security report and asking for
a private contact route — do not post the details in the issue.

Please include, as far as you can:

- the Handoff Reconciler version and your Python version,
- a minimal handoff file and git state (or a description of them) that
  triggers the issue,
- what you expected to happen and what actually happened.

Do **not** include real credentials or anyone else's private data in a
report.

## Supported versions

Handoff Reconciler is pre-1.0. Only the **latest release on the `main`
branch** is supported with security fixes; older checkouts should be
updated rather than patched.
