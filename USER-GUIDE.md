# NewsDesk RSS Dashboard: user guide

NewsDesk collects new articles from websites you follow and shows them in one
window. It runs only on your computer.

## Install it

1. Unzip the download. Don't run it from inside the zip preview.
2. Open the **NewsDesk** folder and double-click **Install NewsDesk**. A small window
   shows the progress.
   - Windows will probably ask "The publisher could not be verified. Are you sure you want
     to run this software?" It asks this about any script downloaded from the internet.
     Click **Run**. You're only asked once: the installer clears the "downloaded" mark
     from the installed copy, so the shortcuts start without prompts.
   - To skip the prompt entirely, right-click the zip before unzipping it, choose
     **Properties**, tick **Unblock**, and click **OK**.
3. Answer **y** to start NewsDesk. The first time, click **Add the starter feeds**,
   **Add a feed** or **Import a file**.

The installer copies NewsDesk to `%LOCALAPPDATA%\Programs\NewsDesk` and adds a
**NewsDesk** shortcut to your desktop and Start menu. It needs no admin rights and
registers nothing with Windows. Afterwards you can delete the folder you unzipped.

Closing the last NewsDesk window normally stops the app after about 25 seconds.
It keeps running while a window is minimized, in the background, or paused by
Windows locking/sleeping. **Settings > Help > Quit NewsDesk** stops it explicitly
for every window. Start it again from the shortcut.

**Without installing:** double-click **Start NewsDesk** in the unzipped folder instead.
It runs the same app from wherever you unzipped it.

**Already used NewsDesk on this PC?** The installed copy picks up your existing feeds and
settings from `%APPDATA%\NewsDesk`. To start completely fresh, uninstall and answer **y**
when asked whether to delete your feeds and settings, then install again.

**To uninstall,** close NewsDesk and run **Uninstall NewsDesk** in
`%LOCALAPPDATA%\Programs\NewsDesk`. It removes the app and its shortcuts, then asks
whether to delete your feeds and settings too.

## Update NewsDesk

Install version 1.1.0 manually once. Later versions can update inside the app:

1. Choose **Update** at the bottom of the sidebar or in **Settings > Help**.
2. Read the release notes and check the version and download size. **View on GitHub**
   opens the release page. Notes inside NewsDesk are plain text and load no images.
3. Choose **Update and restart**. You can keep reading during the download and
   verification. **Cancel update** is available until installation begins.
4. NewsDesk briefly pauses and restarts. Open windows reconnect, including windows
   that were minimized or paused while Windows was locked. Their feed, filters and
   open article are restored where still available. If the original local port is
   busy, NewsDesk opens a fresh window instead.

Feeds, reading history, stars, settings, connection choices, saved Markdown files,
shortcuts, and the application location are preserved. Installed copies stay
installed; portable copies stay portable and gain no shortcuts. No administrator
permission is required.

Checks happen at most once a day while NewsDesk runs. They only notify you.
**Settings > Help** has **Check now**, dismissal, and a switch to turn daily checks
off. Nothing downloads or installs until you choose **Update and restart**.

**Manual updates remain available.** Use **Download manually**, unzip the release,
close NewsDesk with **Settings > Help > Quit NewsDesk**, and run **Install NewsDesk**
again. For a portable copy, replace the app with the new unzipped folder. Shared or
network folders, read-only copies, source checkouts, and copies saving user data
inside the app folder use manual updates. Missing verification information also
leaves manual downloading available.

NewsDesk verifies the ZIP against GitHub's SHA-256 digest and the release's checksum,
checks its contents, and backs up the database and settings before stopping.
If the replacement cannot start, it restores the previous version and data.
The previous application and a local snapshot are retained until a later successful
update cleans them up. Downloads and recovery logs are in
`%LOCALAPPDATA%\NewsDesk\Updates`; the previous application is kept beside the app
folder. These files stay on your computer.

If a power loss or locked file prevents recovery, keep those folders and the error
log when requesting help. Do not delete or move them while an update is active.
The [recovery guide](docs/UPDATING.md#interrupted-updates) describes the recovery tool.

## Everyday use

| To do this | Do this |
|---|---|
| Read an article | Click it. It opens on the right. |
| Keep an article | Click the star. Starred articles are in **Starred** on the left. |
| Follow a new site | Click **+** at the top left and paste the site's address. The app finds its feed. |
| Organize feeds | Make a folder with the folder button, then drag feeds onto it. |
| Read the whole article when a feed only gives a preview | Click **Load full article** in the reader. |
| Save an article as a file | Click the download icon in the reader. Files go to **Documents\NewsDesk**; you can change this in Settings. |
| Find something | Use the search box. The filter button narrows to unread or read, dates, starred or tags. |
| Switch between cards, a list and the one-column feed | Click the ☰ button at the top right. It also groups articles by date and/or feed and changes the sort order. Click a group's header to fold it away. |
| Bring feeds from another reader | Export an OPML file from it, then click the upload button at the top left and choose **Import from a file**. |

Keyboard: **j** and **k** for next and previous, **m** for read or unread, **s** to star,
**/** to search, **?** for every shortcut.

## If something goes wrong

- **Nothing opens.** Wait about 10 seconds, then double-click again.
- **Feeds won't update.** Open **Settings** and click **Test connection**. Then open an
  issue on this project's GitHub page, with a screenshot of the result.
- **CISA is refused.** Version 1.0.6 uses the CISA request headers verified on
  the affected work PC. Use
  **Settings > Connection > Windows (WinHTTP)**, or leave **Automatic** selected
  to try WinHTTP first, with .NET and Python fallbacks. The app remembers a
  successful method for each site until it restarts.
- **"NewsDesk has stopped."** Version 1.0.7 fixes automatic shutdown after a
  background window stops sending heartbeats. Update, then start it from its
  shortcut. If it happens again, include the nearby `app.log` shutdown/error
  messages when reporting it.
- **"NewsDesk is still running"** when installing or uninstalling. Use
  **Settings > Help > Quit NewsDesk**, or close every NewsDesk window. If the
  browser crashed or a close notification was lost, reopen NewsDesk and use
  **Quit NewsDesk**. Wait about 30 seconds for it to stop, then try again.

Your feeds and settings are stored in `%APPDATA%\NewsDesk`, separate from the app, so
installing a newer version doesn't touch them.

## Credit

NewsDesk's layout and workflow are modeled on
[RSS Dashboard for Obsidian](https://github.com/amatya-aditya/obsidian-rss-dashboard)
by [amatya-aditya](https://github.com/amatya-aditya). This is a separate project that
shares no code with the plugin.
