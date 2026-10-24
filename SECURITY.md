# Security policy

## Supported versions

Only the [latest release](https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard/releases/latest)
gets security fixes. NewsDesk tells you when a newer version is out (see **Settings > Help**).

## Reporting a vulnerability

Please report security problems **privately**: open the repository's **Security** tab
and click **Report a vulnerability**. Don't open a public issue for them.

Helpful to include:
- what an attacker could do, and what they need to control (a feed, a website, a file
  the user imports, another program on the same PC)
- steps or a sample feed that reproduces it
- the NewsDesk version (**Settings > Help**) and your Windows version

This is a personal project, so responses are best-effort; I'll acknowledge reports as
soon as I can. Fixes ship as a new release, and the release notes credit the reporter
unless you'd rather not be named.

## What's in scope

NewsDesk runs a small web server on your own computer and reads content from feeds and
websites you choose. Feed and article content is treated as untrusted. Examples of what
matters:
- a feed or web page that can run script in the NewsDesk window, read local files, make
  Windows connect somewhere, or make NewsDesk write outside its folders
- another website open in your browser that can control NewsDesk's local server
- anything in the install or update path that could run code you didn't approve

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) describes the security model, including
the risks that are accepted on purpose.

## How the code is checked

- **Tests** run on every change and daily.
- **CodeQL** scans the Python and JavaScript daily.
- **Bundled libraries** are checked daily against the [OSV](https://osv.dev)
  vulnerability database.
- **Secret scanning** and push protection are on.
