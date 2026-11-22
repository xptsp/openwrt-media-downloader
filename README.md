# 🎬 Media Downloader

A lightweight, self-hosted media synchronization engine and web dashboard engineered specifically for resource-constrained systems like **OpenWrt routers** and **Network Attached Storage (NAS) units** (such as UGREEN NAS). It automatically monitors TV series and movie feeds from targeted drama and movie repositories (such as `thenkiri.com` and `dramakey.com`), solves intermediate file locker configurations natively behind the scenes, and streams media assets straight to attached storage arrays—all with zero operational bloat.

---

## ✨ Features

* **🎛️ Low Memory Footprint:** The downloader core uses Python's standard libraries (`http.server`, `urllib`, and `json`) without heavyweight frameworks like FastAPI, Flask, or BeautifulSoup. Native Web Push uses the `python3-cryptography` package for VAPID signing and payload encryption.
* **📱 Progressive Web App (PWA) & Android Share Target:** Features a responsive, mobile-first dashboard with folder-style tabs, light/dark themes, and bottom-anchored mobile controls. Installs directly to your home screen and integrates into the mobile Share sheet to clean titles, detect `Season 02+` subfolders, and pre-fill Jellyfin paths in one tap.
* **🖼️ Switchable Series Views:** Choose between a responsive poster gallery and the classic series table. The choice is saved per device/browser, with a configurable default for devices that have not chosen an override.
* **🗂️ Local Poster Cache:** Saves series posters in a configurable cache directory and serves the cached images from the dashboard. Existing poster files are reused on subsequent syncs; the image is fetched again if its cache file is missing.
* **👀 Automated Watchlist & Site Search:** Tracks upcoming shows and movies grouped by source site without needing a URL up front. Probes sites on save or during scheduled syncs, offers interactive multi-match selection when multiple titles match, auto-promotes unique matches, and notifies you when ambiguous matches need a choice before promotion.
* **🎬 Separate TV Series & Movie Routing:** Supports both multi-episode TV series (`Show Name - S01E01.mkv` in `base_dir`) and standalone movies (`Movie Title.mkv` in `movies_dir`), automatically marking movies `complete` once downloaded.
* **🔔 Instant Push Notifications:** Sends alerts through ntfy.sh and native browser Web Push whenever an episode finishes downloading, a movie is ready, or a Watchlist title promotes. Browser subscriptions are managed per device from Settings.
* **⏳ Full Queue & Crawler Control:** Features a dedicated Queue tab with real-time progress bars, deduplicates episodes that are already queued or downloading, individual removal for pending items, a **Clear Queue** button, and an emergency **Stop & Clear All** abort switch that terminates active `curl`/`ffmpeg` transfers, cleans up partial files, and halts in-flight scraping loops immediately.
* **🔓 File Locker & Stream Automation:** Follows episode links to supported file hosts, including `downloadwella.com` and `wetafiles.com`, submits free-download forms, and resolves the resulting video URL.
* **🔀 Smart Soft Dependencies (FFmpeg & Curl):** Uses an adaptable download pipeline. Direct media links (`.mp4` / `.mkv`) are handled via system `curl` character streams, while `.m3u8` HLS playlists trigger a soft-dependency handoff to local `ffmpeg` workers if installed.
* **🔄 State Persistence (UCI Format):** Integrates directly into native embedded system parameters. Storage paths, domain allowlists, notification settings, watchlist items, and downloaded sequences are managed via `/etc/config/media-downloader`.
* **📁 Recursive Storage Synchronizer & Safe Pruning:** Scans nested season folders on startup or on demand (`S01E02`, `Ep 4`) to sync existing files back to your config, while safely evicting unchecked episodes (including `.nfo` and `.trickplay` assets) when edited manually.
* **🛡️ Domain Allowlist & Session Security:** Restricts incoming shared links, series additions, and watchlist sources to a user-configurable list of allowed domains, backed by a cookie-based authentication gateway (`SameSite=Lax`, `HttpOnly`). Login cookies remain valid for 30 days unless you log out or restart the server.

---

## 🛠️ Package Architecture

The system maintains a clean, lightweight directory footprint when installed:

```text
/
├── etc/
│   ├── config/media-downloader         # Unified UCI configuration storage file
│   ├── crontabs/root                   # System crontab automation timer (using the --cron flag)
│   └── init.d/media-downloader         # procd/systemd service control daemon script
└── usr/
    ├── libexec/media-downloader.py     # Stream-aware core scraping, search & download server engine
    └── share/media-downloader/         # Web dashboard UI container folder
        ├── index.html                  # Responsive front-end dashboard & PWA Share Target handler
        ├── manifest.json               # Progressive Web App & Share Target manifest
        ├── sw.js                       # Offline PWA service routing worker
        ├── app-icon.png                # Cohesive branding favicon and app launcher asset
        └── style.css                   # Responsive light/dark theme layout stylesheet
```

---

## 🚀 Quick Setup & Installation

### 1. Install System Dependencies
Connect to your host machine via SSH and pull down the minimum Python core standard library expansions along with standard system utilities:

```bash
# On OpenWrt:
apk update
apk add python3-light python3-urllib python3-codecs python3-openssl python3-cryptography curl

# (Optional for HLS/.m3u8 stream support)
apk add ffmpeg
```

### 2. Standard Configuration Setup (`/etc/config/media-downloader`)
Configure your network bind interface targets, web security settings, allowed source domains, push notification topic, and default storage folders. The configuration format follows standard OpenWrt UCI layout blocks:

```text
package media-downloader

config global 'main'
    option bind_address '0.0.0.0'
    option port '8080'
    option base_dir '/mnt/hdd/KDramas'
    option cache_dir '/mnt/hdd/KDramas/.media-downloader-cache'
    option movies_dir '/mnt/hdd/Movies'
    option auth_enabled '1'
    option auth_username 'admin'
    option auth_password 'admin'
    option allowed_domains 'thenkiri.com,dramakey.com'
    option ntfy_enabled '1'
    option ntfy_topic 'your-private-ntfy-topic'
    option vapid_subject 'mailto:you@example.com'
    option theme_mode 'auto'
    option default_series_view 'gallery'

config series 'the-divorce-insurance'
    option name 'The Divorce Insurance'
    option url 'https://thenkiri.com'
    option path '/mnt/hdd/KDramas/The Divorce Insurance'
    option type 'series'
    option status 'complete'
    option downloaded_episodes '1 2 3 4 5 6 7 8 9 10 11 12'
    option last_update '1794508804'

config watchlist 'merry-berry-love'
    option name 'Merry Berry Love'
    option source 'thenkiri.com'
    option type 'series'
```

### 3. Enable System Automation Services
To guarantee the application daemon launches automatically on system boots, run:

```bash
# Enable and launch the web engine daemon service
/etc/init.d/media-downloader enable
/etc/init.d/media-downloader start
```

To run headless scraping cycles seamlessly in the background without launching the persistent HTTP server socket, configure a cron entry to pass the explicit execution flag:

```text
0 */2 * * * /usr/libexec/media-downloader.py --cron
```

---

## 📱 Mobile PWA & Share Sheet Installation Guide

Because mobile browsers restrict Progressive Web App and Share Target features to secure contexts (`HTTPS` or `localhost`), choose one of the following setups:

### Option A: HTTPS Reverse Proxy (Recommended for Everyday Mobile Use)
1. Expose your dashboard over HTTPS using a reverse proxy (such as Caddy, Nginx, or Tailscale Serve) pointing to `127.0.0.1:8080`.
2. Open the HTTPS URL in Chrome on Android or Safari on iOS.
3. Tap **Install App** or **Add to Home Screen**. Once installed, sharing any link from your mobile browser to **Media Downloader** automatically opens the pre-filled Add Series modal.

### Option B: Local SSH Tunnel
1. Connect to your router/NAS via SSH using local port forwarding (tunneling local port `8080` to `127.0.0.1:8080`).
2. Open Chrome or Safari on your phone and head directly to **`http://localhost:8080`**.
3. Tap **Add to Home Screen** or **Install App** to deploy the standalone app wrapper to your device launcher.

### Browser Push Notifications
After opening the dashboard over HTTPS (or localhost), visit **Settings** and choose **Enable Browser Push** on each device. Grant the browser notification permission; episode, movie, and Watchlist promotion alerts can then arrive even when the PWA is closed. Use **Test** beside the browser push control to verify delivery. The server persists its VAPID private key and device subscriptions under `/etc/config/` with owner-only permissions; keep those files when backing up the app configuration. Set `vapid_subject` to a contact email or HTTPS URL identifying the service.

The existing ntfy.sh integration remains independently configurable. Browser Web Push requires the `python3-cryptography` runtime package and supported browser push services; it does not require an ntfy account.

---

## 📄 License
This application is distributed as free, open-source software under the Apache License 2.0. Feel free to modify and adapt it for your personal home labs!
