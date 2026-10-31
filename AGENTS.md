# Contribution and release workflow

These instructions apply throughout this repository, including application code,
dependencies, documentation, workflow files and release version changes.

## Pull requests

- Work on a feature branch, using the `codex/` prefix for agent-created branches.
- Submit changes through a pull request. Do not commit or push changes directly to
  `main`.
- A request to commit, push or publish a release does not by itself waive the pull
  request and security review requirements.

## Security review before merge and release

- Complete an explicit security review of the final pull request diff and record
  its outcome on the PR before merging. Use the applicable security review tools
  or workflow; passing unit tests or CodeQL alone does not replace this review.
- Resolve security findings and complete the required checks before merging.
  If the diff changes after review, review the changed portions again before merge.
- Publish releases only from the merged, reviewed changes after validation passes.
  Follow the build, package, checksum and update/rollback checks in
  [docs/UPDATING.md](docs/UPDATING.md).
- Keep unreviewed or unmerged changes out of release packages. If a required review
  or check is unavailable, report the blocker rather than bypassing it.
