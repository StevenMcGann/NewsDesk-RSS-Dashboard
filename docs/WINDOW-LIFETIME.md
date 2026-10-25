# Unexpected shutdown after locking or minimizing: issue #4

Issue [#4](https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard/issues/4) reports
the "NewsDesk has stopped" overlay after the app has been open for a while.
The reporter clarified that the PC had been locked, and the app was minimized
and in the background; it also occurs on the development PC.

## Defect

Through 1.0.6, the watchdog stopped the server 180 seconds after its last
heartbeat. That assumed the browser's JavaScript timers would keep running.
Browsers can suspend freezable task queues for background pages, so a timer gap
does not establish that the app window closed. See
[Chromium's page lifecycle documentation](https://developer.chrome.com/docs/web-platform/page-lifecycle-api)
and [Microsoft's Edge performance documentation](https://support.microsoft.com/en-us/edge/learn-about-performance-features-in-microsoft-edge).

The old close beacon also tracked one global goodbye timestamp. Closing one
window could start a shutdown countdown while another window was paused.

## Version 1.0.7

- Register an independent document ID for each page and close that ID only.
- Keep registered windows alive through missing heartbeats, including browser
  freezing and PC locking/sleeping.
- Retain the 25-second grace after the last registered document closes, and the
  120-second startup grace when no page has connected.
- Ignore late heartbeats from closed documents and repeated close notifications.
  A new document can cancel a pending normal close.
- Preserve back/forward-cache pages and refresh them when restored.
- Add **Settings > Help > Quit NewsDesk**, with confirmation that it stops the
  app for every window. This also works with `--stay` and with stale registrations
  left by a browser crash or lost close beacon.
- Keep authentication and same-origin checks on lifecycle requests; the beacon
  carries its launch token and document ID in the URL, and Quit requires the
  ordinary `X-Token` header.

## Verification

Regression tests exercise long timer gaps, multiple windows, reload during the
close grace, late/duplicate notifications, startup cleanup, `--stay`, explicit
Quit, invalid IDs and endpoint authentication. Release notes record the
portable-build and browser reproduction results.

On an affected PC, install 1.0.7, leave NewsDesk minimized/in the background,
lock Windows for longer than three minutes, then unlock and verify that the
existing window still works. Also check that closing one of two NewsDesk
windows leaves the other usable, and that **Quit NewsDesk** stops the server.
Keep issue #4 open until the affected-machine result is confirmed.

If the app still stops, the relevant evidence is the matching shutdown/error
entry in `%APPDATA%\NewsDesk\app.log`. The new log messages distinguish explicit
Quit, all registered windows closing, and a first window that never connected.
An operating-system process termination is a separate possibility and would
need its own evidence.
