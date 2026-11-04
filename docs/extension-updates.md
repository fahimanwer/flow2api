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

## Plan to make laptops automatic (not started; needs the owner)

1. **Chrome Enterprise Core** (Google: "cloud-based management and reporting for $0"). The owner signs up once with an
   admin Google account (needs a domain we own) and creates an enrollment token.
2. Each staff Chrome gets that token once (a `.reg` file on Windows, a profile on macOS) — after that Chrome counts as
   managed.
3. flow2api packs the extension as a signed CRX with a fixed key and serves `updates.xml` + the CRX (as pinterest-factory
   does); the admin console force-installs that extension id from our update URL.
4. From then on, publishing a version updates every staff Chrome within hours, with no clicks. Staff Chrome shows
   "managed by your organization"; staff cannot remove the extension.
5. One-time cost: the CRX has a new extension id, so each laptop's extension settings (route key) start fresh once; the
   server re-binds accounts by email on the first push.

Fallback if enrollment is unwanted: a small helper on each laptop (Windows scheduled task) that downloads the published
zip into the unpacked folder, plus an extension check that reloads itself when its files change. Not tested yet.
