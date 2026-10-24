# NewsDesk RSS Dashboard: architecture and development

How the app is built, why, and how to work on it. For using the app, see
[USER-GUIDE.md](../USER-GUIDE.md).

NewsDesk is a local Python server with a browser UI, shown in an Edge `--app`
window. It ships as a portable zip that bundles python.org's embeddable Python, so
nothing is installed.

## Run from source

```powershell
py -3 app\launcher.py                    # opens an Edge app window
py -3 app\launcher.py --no-window --stay # server only, for development
py -3 app\launcher.py --diagnose         # test both download paths and print a report
py -3 -m unittest discover -s tests -t tests
```

| Option | Effect |
|---|---|
| `--port N` | Port to listen on (default 47821) |
| `--data DIR` | Data folder (default `%APPDATA%\NewsDesk`; the `NEWSDESK_DATA` variable also sets it) |

The live network tests are skipped unless `RSS_LIVE_TESTS=1` is set. Python 3.9 or later
works from source; the portable build bundles 3.14.

## Build the portable zip

```powershell
pwsh -File .\build_portable.ps1 -Test
```

The script does the following:

1. Downloads the pinned Python embeddable zip into `dist\cache`.
2. Checks it against python.org's published SHA-256.
3. Checks the Authenticode signature on `python.exe`.
4. Copies the app, with `USER-GUIDE.md` as the zip's `README.md`.
5. Runs an import smoke test, and with `-Test` the whole test suite, on that runtime.
6. Writes `dist\NewsDesk-<version>.zip`.

To move to a newer Python, pass `-PythonVersion` and `-PythonSha256` with the values from
the python.org release page, or update the defaults.

**Publishing a release.**
1. Set `VERSION` in `app/config.py` (for example `1.1.0`) and commit.
2. Build: `pwsh -File .\build_portable.ps1 -Test`.
3. Publish: `gh release create v1.1.0 dist\NewsDesk-1.1.0.zip --title "NewsDesk 1.1.0" --notes "..."`.

The tag must be `v` plus the same version: that is what the in-app update check compares.

**Publishing.** Attach the zip to a GitHub release.
- Downloaded copies carry Mark-of-the-Web, so Windows shows a security prompt on first run. Unblocking the zip before unzipping avoids it.
- The launcher also runs from a `\\server\share` folder. Each user's data still goes to their own `%APPDATA%`.

## Layout

```
Start NewsDesk.bat        runs python\pythonw.exe app\launcher.py (falls back to an installed pyw/pythonw)
Install NewsDesk.bat      runs app\installer.py with the bundled Python (per-user install, see below)
app\installer.py          copies the app, makes shortcuts (IShellLink via ctypes), writes install.json
app\uninstall.bat         copied into the install as "Uninstall NewsDesk.bat"
build_portable.ps1        builds dist\NewsDesk-<version>.zip
starter-feeds.opml        default feeds offered on first run
assets\logo.svg           the logo (master); PNGs, newsdesk.ico and social-preview.png are generated from it
tools\build_icons.py      renders the logo with headless Edge; rerun after editing logo.svg
app\launcher.py           single-instance lock, starts the server and refresher, opens the window, idle shutdown
app\server.py             HTTP server: JSON API and static UI, plus the security checks
app\store.py              SQLite schema and every query
app\fetcher.py            HTTP: urllib first, Windows PowerShell/.NET fallback
app\feeds.py              feedparser to normalized articles; JSON Feed; feed autodiscovery
app\refresher.py          background refresh loop; subscribe (add feed)
app\updates.py            daily check of GitHub releases for a newer version
app\opml.py, export_md.py OPML import/export; Markdown files with frontmatter
app\config.py             paths, settings schema and defaults
app\vendor\               feedparser 6.0.14 and feedparser-sgmllib 2.1.0 (pure Python)
app\static\               index.html, app.css, ES modules, icons.js (Lucide paths),
                          vendor\ DOMPurify, Readability, Turndown
tests\                    unittest suites and sample feeds
```

User data in `%APPDATA%\NewsDesk`:

| File | Contents |
|---|---|
| `dashboard.db` | SQLite database (WAL mode) |
| `settings.json` | User settings |
| `app.log` | Log, rotated at 1 MB |
| `instance.json` and `instance.lock` | Single-instance bookkeeping |

## Design decisions

**Python standard library plus a browser UI, not a desktop GUI toolkit.**
- Matching the look of RSS Dashboard for Obsidian needs real CSS, which Tkinter can't provide.
- Qt would add over 100 MB and a pip install.
- The only third-party Python code is feedparser, vendored as pure Python, so the runtime needs no pip at all.

**Embeddable Python, not PyInstaller.**
- The runtime is python.org's signed build.
- PyInstaller produces unsigned exes, which antivirus and SmartScreen tend to flag.

**Two download paths.**
- urllib reads the proxy from the Windows registry. It cannot use a PAC script or log in to an NTLM/Kerberos proxy.
- When urllib fails at the network level, or the proxy returns HTTP 407, the request is retried through a long-lived `powershell.exe` worker that uses .NET `WebRequest` with the system proxy and default credentials. That is the same path Edge uses. After that success, the Windows path becomes the preferred one for every site.
- An HTTP 403 is also retried once through the Windows path. Some CDNs refuse Python's TLS handshake but accept Windows' (cisa.gov's Akamai does this). If the retry works, only that site switches to the Windows path.
- **Settings > Connection** can force either path.
- Default credentials go only to the proxy, never to the target site.

**Honest User-Agent.**
- The app sends `NewsDesk/1.0`, not a browser string.
- A Chrome User-Agent paired with a non-Chrome TLS handshake is exactly what bot filters look for. With a Chrome string, even the Windows path got a 403 from cisa.gov.

**TLS.**
- `ssl.create_default_context()` trusts the Windows certificate store, so a TLS-inspection CA installed on a managed network works.
- `VERIFY_X509_STRICT` is cleared, because Python 3.13 and later reject some inspection CAs with it set. Chain and hostname checks stay on.

**Full article extraction runs in the browser.**
- The server only downloads the page.
- Mozilla Readability (the engine behind Firefox Reader View) extracts the article inside an inert `DOMParser` document.
- Relative URLs are rewritten by hand first, because the CSP's `base-uri 'none'` also blocks `<base>` in parsed documents.
- The extracted HTML is cached in `items.content_full`.

**Layouts and grouping.**
- Cards, List and Feed layouts follow the plugin, and so does grouping by date, by feed, or by date then feed.
- Grouping works one page at a time, as in the plugin. Folded groups are remembered in the browser's local storage.

**Installer: a script, not a setup.exe.**
- `Install NewsDesk.bat` copies the app to `%LOCALAPPDATA%\Programs\NewsDesk`. No admin rights are needed and nothing is written to the registry; the only traces outside that folder are two `.lnk` files, on the desktop and in the Start menu.
- Updates copy into `NewsDesk.new`, then swap it with the old folder, so no stale files are left behind.
- Shortcuts are created through the shell's `IShellLink` COM object via ctypes, so this works where PowerShell scripts or VBScript are blocked. Desktop and Start menu paths come from `SHGetKnownFolderPath`, which follows OneDrive redirection.
- Before an update or uninstall, the process list is checked for any program running from the install folder.
  - A rename test isn't enough: Windows lets you rename the folder of a running program.
- The uninstaller is a fixed ASCII script that uses `%~dp0` and no literal paths.
  - `chcp 65001`, the usual way to put non-ASCII paths in a batch file, breaks `set /p` prompts.
  - The installer records its shortcuts in `install.json`. The uninstaller deletes only `NewsDesk.lnk` files listed there, and refuses to run where that file is missing, such as in the unzipped download.
- **Mark of the Web.**
  - Explorer marks every file unzipped from a downloaded zip with a `Zone.Identifier` stream, which is why `Install NewsDesk.bat` triggers "The publisher could not be verified".
  - Windows copies that stream along with each file. The installer therefore deletes it from the installed copy, as Unblock-File does, so the shortcuts don't prompt on every start.
  - The download itself is left untouched.
- **Why the publisher shows as Unknown.** Batch files can't carry an Authenticode signature, so the first prompt can only be avoided by unblocking the zip.
- **The fix for that is a later step:** a signed `setup.exe` built with Inno Setup, which would also add an uninstall entry under Settings > Apps.
  - Signing needs a code-signing certificate tied to a verified identity.
  - SmartScreen may still warn until the signed file builds up download reputation.

**Update check: tell the user, never act.**
- `app/updates.py` asks `api.github.com/repos/StevenMcGann/NewsDesk-RSS-Dashboard/releases/latest` at most once a day. The first check is 30 s after startup; then it re-checks hourly whether the day has passed. It goes through the normal fetcher, so it works behind proxies.
- The result is saved in `update.json` in the data folder, and the UI receives it with `/api/state` and every heartbeat.
- A tag newer than `config.VERSION` (for example `v1.1.0`) shows a sidebar note linking to the release page. Only links under the repo's `/releases/` are used; anything else falls back to the releases page.
- Nothing is downloaded or run. "Not now" stores the dismissed version in settings, and the check can be turned off.
- A 404 means no release exists yet, which is not treated as an error.

**Pruning.**
- Read articles older than the retention period are deleted only if all of these hold:
  - They aren't starred, saved or tagged.
  - They haven't been seen in their feed for 3 days.
- Without the last condition, a slow feed would bring a pruned article back as unread on the next refresh.

**Window lifecycle.**
- The page sends a heartbeat every 15 s, and a beacon on `pagehide`.
- The server exits 25 s after the last window says goodbye, or after 180 s with no heartbeat. Hidden windows are throttled to one timer per minute, so the timeout has to be long.
- A second launch finds the running server through `instance.lock` and `instance.json` and opens another window.

## Security model

Feed content and web pages are untrusted.

**Server:**
- Binds to `127.0.0.1` only. On Windows it uses `SO_EXCLUSIVEADDRUSE`, so no other process can take over the port.
- Every API call needs the per-launch token written into `index.html`, compared in constant time. The two exceptions:
  - `/api/ping` reveals only the app name and version.
  - `/api/bye` carries the token in its query string, because `navigator.sendBeacon` can't set headers.
- Requests with a foreign `Host` or `Origin` header are refused. This blocks CSRF and DNS rebinding from other sites open in the same browser.
- UI files are served from a list built at startup. A request path is never turned into a file-system path.
  - This matters because any web page or feed image can request UI files without the token.
  - The old approach of resolving the path and then checking it let `/%5C%5Chost%5Cshare` become `\\host\share`. Windows would then connect to that host with the user's credentials (NTLM).
- Connections that stall mid-request are dropped after 60 s.

**Downloads:**
- Only `http` and `https` are accepted, including for redirects, with at most 5 redirects. The urllib opener has no `ftp:`, `file:` or `data:` handlers.
- Each download has a 15 s timeout per connect or read, and 60 s in total. The total limit stops a server from trickling a response to tie up a refresh worker. Both the urllib and the PowerShell paths enforce it.
- Article pages come from feed links, so they refuse this computer's own addresses: loopback, `localhost`, link-local (such as `169.254.169.254`) and unspecified. Private LAN ranges stay allowed for intranet sites.
  - Every redirect hop is checked *before* it is requested. On the PowerShell path that means turning off .NET's automatic redirects and following them in Python.
- Feed addresses the user adds may be local, for example a self-hosted feed service.
- **Accepted risk: DNS rebinding.** The local-address check resolves the hostname, and the transport then resolves it again.
  - A hostile DNS server could answer differently the second time and steer an article download to a loopback service. Only a GET is sent, and the response only reaches this user's own reader.
  - Pinning the checked address would mean replacing both transports' connection logic, and the PowerShell path can't be pinned at all. Recorded as finding 6 of the 2026-10-24 review.

**Feed parsing:**
- Response bodies go to feedparser as `io.BytesIO`, never as raw bytes. Given bytes, feedparser first tries to *open them as a file name*, so a body consisting of `\\host\share\x` would make Windows connect to that host (finding 1).
- Two feedparser routines cost the square of their input size, and `feeds.py` wraps both at import (findings 2 and 3); the vendored files are unchanged.
  - Tag deduplication scans every earlier tag, so tags are capped at 100 per entry or feed.
  - The author email regex is unanchored and greedy, so author text is cut to 1,000 characters before it runs.

**Page:**
- The CSP allows only scripts and styles served by the app itself (`script-src 'self'`, `style-src 'self'`).
- Frames are not allowed.
- Feed and article HTML goes through DOMPurify, with scripts, frames, forms, `style`, `class`, `id` and `background` forbidden. It is then post-processed in an inert document, so blocked images and tracking pixels never load.
  - `background` is the legacy table-cell image attribute. DOMPurify allows it by default, and it would have bypassed **Hide images** (finding 4).

**Files and links:**
- Markdown files are written only inside the configured save folder, with sanitized names that avoid reserved Windows names.
- The database stores only `http(s)` addresses for feeds, website links and icons. OPML imports are checked the same way, so a `javascript:` link can't reach `href` or `window.open`. The UI checks again before opening a link.
- Responses are capped at 10 MB, and decompression at the same size.

## Updating vendored code

The pinned versions are in [THIRD-PARTY.md](../THIRD-PARTY.md).
- **Python packages:** download the wheel with `pip download <pkg>==<ver> --no-deps` and copy the package folder into `app\vendor\`.
- **JavaScript libraries:** use `npm pack <pkg>@<ver>` and copy the dist file into `app\static\vendor\`.
- **Icons:** `app\static\icons.js` holds path data from `lucide-static`. Add an icon by copying the inner SVG markup of `icons/<name>.svg`.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Nothing opens | `app.log` in the data folder. A message box shows startup errors. |
| Every feed fails | **Settings > Connection > Test connection**, or `python\python.exe app\launcher.py --diagnose` from the app folder. |
| One feed fails | Its sidebar row has a red dot. Click "N feeds failed" in the status bar to see each error. A 403 that survives the automatic Windows retry means the site refuses non-browser clients outright. |
| `pythonw.exe` blocked by AppLocker/WDAC | Run the app from a folder the policy allows, or ask the machine's administrator to allow the signed python.org runtime. |
