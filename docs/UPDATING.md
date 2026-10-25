# In-app updates and release checks

The updater first ships in 1.1.0. Existing users install that version manually;
subsequent stable releases can be installed from inside NewsDesk. Automatic checks
only request release metadata. There is no service, scheduled installer or new
dependency. The bundled Python runtime runs the temporary update worker.

## Trust and transaction boundaries

- Start accepts only a release identity, never a URL or installation path. Every
  update API except the public reconnect proof uses the existing launch token and
  Host/Origin validation. Release bodies are returned to the UI when the dialog opens, not
  included in recurring heartbeat responses, and rendered with `textContent`.
- The latest stable release is rechecked after confirmation. Release ID, tag, asset
  IDs, sizes, URLs and SHA-256 digests bind the selection. Both matching assets
  (`NewsDesk-<version>.zip` and `.zip.sha256`) are mandatory. The ZIP must agree with
  both GitHub's digest and the published checksum; the checksum asset's digest is
  checked too. This authenticates the repository's release over HTTPS; it is not a
  separate publisher signature.
- Downloads honor the configured Python/.NET/WinHTTP connection choice and proxy
  behavior. Each redirect is checked against an exact GitHub asset-host allowlist.
  Archive downloads have a 128 MiB cap and ten-minute deadline; feed limits remain
  unchanged. Extraction caps are 512 MiB and 4,096 entries.
- Paths, Windows device names, duplicate/case-colliding destinations, traversal,
  links, encryption, package structure and version are checked before execution.
  Destination eligibility and free space are checked before stopping NewsDesk.
- One shared job serves all windows. An update lease keeps the server alive during
  preparation. Heartbeat silence never requests update shutdown. Normal APIs pause,
  queued refreshes stop, and active refresh/API writes drain before the consistent
  SQLite/settings snapshot is taken.
- The worker runs from the verified package in the private update cache, outside
  the replaced folder. It holds a per-installation lock, waits up to 120 seconds
  for the original process handle to exit, then swaps sibling folders. It never
  terminates the original app or an unrelated process.
- The replacement runs with user writes and background work deferred. Its instance
  PID, version and per-update HMAC proof must pass within 60 seconds. Only then is
  the transaction committed and normal work enabled. Startup/migration failure
  restores the application and SQLite/settings snapshot. A failed health-check
  child may be terminated by its owning worker before rollback.
- Existing windows keep a separate handoff nonce, verify the replacement proof,
  reload to get a new launch token and restore their view. Open document registrations
  are inherited, and each verified update reload resumes its document ID once, so
  even every window remaining suspended cannot trigger the first-window timeout.
  Ordinary reloads keep the usual fresh-document/close-tombstone behavior. The nonce is never sent
  in a public ping request. A changed port gets a fresh-window fallback.

## Interrupted updates

Every transaction keeps `job.json`, `worker.json`, `worker.log`, the verified
package and `data-backup` under `%LOCALAPPDATA%\NewsDesk\Updates\<job-id>`.
Application siblings are `.NewsDesk.update-<job-id>.new`, `.old` and, on rollback,
`.failed` (the prefix follows the chosen application folder name).

Automatic recovery is attempted when the worker observes failure. An interrupted
worker or unavailable/locked filesystem may require explicit recovery. Preserve
these files; do not start competing copies against the same data or delete recovery
folders. After closing NewsDesk and confirming the worker is no longer running,
use the **same staged runtime and job** in PowerShell:

```powershell
$job = "$env:LOCALAPPDATA\NewsDesk\Updates\<job-id>"
& "$job\extracted\NewsDesk\python\python.exe" -B `
  "$job\extracted\NewsDesk\app\update_worker.py" "$job\job.json" --recover
```

Recovery refuses a running application. It restores only the known database and
settings files and the previous application, leaving exported Markdown untouched.
Do not recover an already committed update; the tool treats it as complete. If
recovery fails, retain the log and folders for support. Backups can contain private
feed URLs and reading history; do not attach them to a public issue.

## Release checklist

- Run Windows unit tests on supported Python versions and CodeQL. Complete the
  portable build and the offline packaged acceptance test on Windows.
- Build with `pwsh -File .\build_portable.ps1 -Test`. Verify both the ZIP and its
  `.zip.sha256` sidecar. Upload **both** to the same stable GitHub release; confirm
  the asset API supplies both SHA-256 digests before declaring in-app updates ready.
- Review the release notes and current/new versions in the dialog, plain-text HTML
  escaping, progress, cancellation, and simultaneous starts from multiple windows.
- Attend tests on **both the home PC and affected work PC before publishing**:
  installed and portable copies, custom paths with spaces, connection choices,
  retained data/shortcuts, foreground and locked/background windows, reconnection,
  and a forced failed-startup/database-migration rollback.
- Keep 1.1.0 unpublished until those checks pass. CI candidate artifacts are for
  testing and are not releases.

## Disposable acceptance test

The CI candidate artifact includes the ZIP, checksum and
`test_update_acceptance.py`. Extract the ZIP alongside the script, then run:

```powershell
& '.\NewsDesk\python\python.exe' -B '.\test_update_acceptance.py' --candidate '.\NewsDesk' --keep
& '.\NewsDesk\python\python.exe' -B '.\test_update_acceptance.py' --candidate '.\NewsDesk' --mode portable --interactive --keep
```

The first command exercises portable, installed-layout and intentional startup
failure cases using real packaged processes. It seeds disposable data and checks
reading history, stars, settings, exported files and shortcut records. The second
prints a localhost URL for attended browser checks and waits for Enter after
verification. Open the URL in two windows, choose different views, cancel once,
then update. Keep one window minimized/locked during the restart, then unlock and
verify that both reconnect. Repeat with `--mode installed` or `--mode rollback`.

This test creates fresh copies under a guarded temporary directory and substitutes
offline release/download fixtures only in those copies. It never updates your
normal installation and makes no external network requests. Its fake next version
is not a published release. Success reports are retained as `acceptance-result.json`
with `--keep`; failed fixtures are always retained. Real GitHub/proxy download
behavior still requires an attended candidate check against published assets.
