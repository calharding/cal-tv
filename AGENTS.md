# Contributor workflow

- At the start of each session, check the repository's open issues. While
  [the privacy audit](https://github.com/calharding/cal-tv/issues/1) is open,
  prioritize it over the separate source documentation work in issue #2.
- Before filing an issue, search existing issues and pull requests. Add useful
  evidence to the existing report when it covers the same problem. For a new,
  non-sensitive issue, include a minimal synthetic reproduction, expected and
  observed behavior, and the affected revision.
- Report a suspected disclosure privately to the maintainer. Do not paste real
  credentials, hostnames, IPs, usernames, private paths, logs, or raw scan
  matches in public issues, PRs, commits, or fixtures. Use fictional examples
  and ignored local settings or external secret storage for real values.
- Work in an isolated local Git checkout. Verify the actual entry point and
  run the relevant tests on a clean checkout with synthetic data. Review the
  outgoing diff and history for disclosures. Record the exact commands, results,
  and skips, then push the verified work on its own review branch when
  authorized. Keep unrelated issues out of the diff. A push or merge is not
  a deployment.
