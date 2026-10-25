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

**Updating:** when a new version comes out, a note appears at the bottom of the sidebar.
Click **Get it** to open the download page, unzip the new version, and run
**Install NewsDesk** again. Your feeds and settings are kept. NewsDesk checks once a day;
**Settings > Help** has **Check now** and a switch to turn the check off.

**Without installing:** double-click **Start NewsDesk** in the unzipped folder instead.
It runs the same app from wherever you unzipped it.

**Already used NewsDesk on this PC?** The installed copy picks up your existing feeds and
settings from `%APPDATA%\NewsDesk`. To start completely fresh, uninstall and answer **y**
when asked whether to delete your feeds and settings, then install again.

**To uninstall,** close NewsDesk and run **Uninstall NewsDesk** in
`%LOCALAPPDATA%\Programs\NewsDesk`. It removes the app and its shortcuts, then asks
whether to delete your feeds and settings too.

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
