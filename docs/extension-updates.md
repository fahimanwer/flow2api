# How the worker extension gets updated (facts 2026-11-05)

## Today

| Where | How it updates | Proof |
|---|---|---|
| Server browsers flow-ultra-01 / flow-ultra-02 (cf-worker-01) | Automatic. A systemd timer (`flow-ultra-ext-sync.timer`, `flow-ultra-02-ext-sync.timer`, every 10 min, enabled at boot) downloads whatever version flow2api publishes (`/api/plugin/ext-version`, `/download/worker-latest.zip`), installs it in `/srv/flow-ultra*/releases/<v>`, restarts the container and checks it runs `<v>` (`docker/flow-ultra/ext-sync.sh`). Keeps one previous version for rollback. | 2026-11-04 21:19 UTC: flow-ultra-02 moved 3.7.5 → 3.8.0 by itself 2 s after publish and pushed 2 s later. |
| Staff laptops (25 Windows, 6 macOS Chromes, from the reported user agents 2026-11-05) | Manual. The extension is loaded unpacked; it shows an update banner when flow2api publishes a newer version, and a person must download and reload it. | Chrome never updates an unpacked extension by itself. |

## Why laptops cannot simply auto-update (Google's rule)

Google's policy page for `ExtensionInstallForcelist` (chromeenterprise.google, read 2026-11-05): "On Microsoft Windows
instances, apps and extensions from outside the Chrome Web Store can only be forced installed if the instance is joined
to a Microsoft Active Directory domain, joined to Microsoft Azure Active Directory or enrolled in Chrome Enterprise
Core. On macOS instances, … managed via MDM, joined to a domain via MCX or enrolled in Chrome Enterprise Core."
pinterest-factory hit the same wall (its `host/README.md`, 2026-09-15: Chrome ignores a local force-install policy on an
unmanaged browser) and its options page tells laptop users "Installed from a folder (Load unpacked): Chrome cannot update
it by itself; when a newer version is served, download it here." Its server browsers update by an image rebuild + host
agent restart run by the operator (its `AGENTS.md`, release steps).

## Laptops stay manual (owner decision 2026-11-05)

Owner: "keep it manual no chrome enterprise". Staff update the unpacked extension by hand when the banner shows; no Chrome
Enterprise Core enrollment and no laptop helper. Options that were weighed (for the record; reopen only on a new owner order):
Chrome Enterprise Core enrollment + a signed CRX force-installed from our `updates.xml` (no clicks, but Chrome shows
"managed by your organization" and the extension id changes once), or a per-laptop helper that refreshes the unpacked folder
(never tested). Note found 2026-11-05 by the pinterest-factory research: both extensions set Chrome's proxy, so one profile
cannot run the flow2api worker and the Pinterest extension together.
