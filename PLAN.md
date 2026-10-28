# relocarr — project plan

> Status: **planning, no code yet.**
> Tagline: *hands finished Radarr and Sonarr imports over to your NAS or custom layout, then
> cleans up the download client.*
> A small self-hosted service that takes over the *last step* of a Radarr / Sonarr import:
> it moves finished, already-renamed files from a local staging folder to a remote or
> differently-laid-out media location, then cleans up after itself.

---

## 1. Problem

Radarr and Sonarr are opinionated about where files end up:

* Radarr **always** creates one folder per movie (`<root>/<Movie Folder>/<file>`). The
  *Movie Folder Format* setting cannot be empty, and there is no "flat" mode. This has been
  requested for years ([Radarr #153](https://github.com/Radarr/Radarr/issues/153),
  [#436](https://github.com/Radarr/Radarr/issues/436),
  [#826](https://github.com/Radarr/Radarr/issues/826),
  [#6191](https://github.com/Radarr/Radarr/issues/6191)) and the Servarr FAQ says a flat
  layout is unlikely ever to be supported.
* Sonarr always writes `<Series>/<Season NN>/<file>`; users with an existing library that is
  laid out differently (no season folders, one folder per show, …) have the same problem.
* Both apps import **synchronously into the final location**. When that location is a network
  share (SMB/NFS) that is sometimes offline, imports fail with "root folder not found" or hang
  on free-space checks, and torrents sit around until someone notices.
* Nothing coordinates the download client, the *arr app and the destination: a torrent keeps
  seeding while its file is copied, and cleanup of the torrent and the leftover files is a
  separate, manual (or racy) affair.

No existing project solves this. Related tools
([radarr-folder-organizer](https://github.com/rubasace/radarr-folder-organizer), archived, and
similar scripts) sort movies *into* folders; none take them *out*, and none handle the download
client or an offline destination.

## 2. Goals and non-goals

**Goals**

1. Radarr and Sonarr never touch the final destination. They import into a **local staging
   root folder** and keep doing what they are good at: search, grab, rename, quality decisions.
2. relocarr copies each imported file to the destination using a **configurable layout**
   (flat for movies; the *arr's own `<Series>/<Season N>/` layout for TV), safely and
   verifiably.
3. The download client is **stopped as soon as the download completes**, before anything is
   renamed, copied or deleted, and the torrent and its data are removed once the copy is
   verified.
4. If the destination is offline, nothing is lost or failed: jobs wait and retry.
5. A **web UI** to configure everything, with the download client list fetched from the
   *arr app, and a **preflight page** that checks the *arr configuration so that only one
   automation is moving files.
6. Works with **Radarr and Sonarr** (any number of instances of each) and is designed so other
   *arr apps and download clients can be added by adapter.

**Non-goals (v0.x)**

* Not a downloader, indexer manager or renamer. Radarr/Sonarr rename; we never rename.
* Not a media-server replacement. We only notify (Plex first) after publishing.
* No transcoding, no metadata scraping.
* No support for keeping the movie/episode tracked as "has file" in Radarr/Sonarr (see §11).

## 3. Architecture overview

```
 Download client                *arr app                       relocarr                      Destination
 (Transmission, …)            (Radarr / Sonarr)                                                (SMB / NFS / disk)
        │                            │                              │                               │
  1.    │ torrent completes          │                              │                               │
        │◄────── poll (every ~15 s) ─┼──────────────────────────────┤ 1. detect complete + STOP     │
        │                            │                              │    (only torrents in the      │
        │                            │                              │     *arr queue)               │
  2.    │                            │ import into local STAGING    │                               │
        │                            │ (hardlink + rename)          │                               │
  3.    │                            │ ── webhook "Download" ─────► │ 2. job created / matched      │
  4.    │                            │                              │ 3. copy → .part → verify ────►│ final file
        │                            │                              │    → atomic rename            │
  5.    │                            │ ◄── DELETE file, unmonitor ──┤ 4. reconcile *arr             │
  6.    │ ◄── remove torrent + data ─┼──────────────────────────────┤ 5. cleanup client + staging   │
        │                            │                              │ 6. notify Plex (optional)     │
```

Components:

| Component | Responsibility |
|---|---|
| **Webhook receiver** | Accepts the *arr "Download" (import/upgrade) events, authenticated by a shared secret header. |
| **Client watcher** | Polls each configured download client, finds finished torrents that belong to the *arr queue, stops them. |
| **Job worker** | Persistent state machine (SQLite) that copies, verifies, publishes, reconciles and cleans. |
| **Arr adapters** | `radarr` / `sonarr`: payload parsing, API calls (root folders, download clients, queue, delete file, unmonitor), preflight checks. |
| **Client adapters** | `transmission` first; `is_complete`, `stop`, `remove(delete_data)`, `list`. |
| **Web UI + API** | Setup, preflight, jobs. Server-rendered, no JS build step. |

## 4. Key design decisions

1. **Local staging root folder.** The only way to stop Radarr creating folders on the
   destination is for it to never write there. Staging lives on the same mount as the
   download folder, so the import is an instant hardlink.
2. **Webhook, not a custom script.** Radarr and Sonarr both expose a built-in *Webhook*
   connection with the same event set as custom scripts. No script mounted into the *arr
   container, no long-running script blocking the *arr, and the payload carries the file path,
   client name and torrent hash.
3. **Two-phase per download: seal first, act later.** The client watcher stops the torrent the
   moment it completes (phase A). The copy only starts once the *arr has imported and renamed
   the file (phase B, the webhook). Waiting for the rename is therefore automatic.
4. **Copy, verify, publish, then delete.** Staging → destination is always a copy plus delete
   (different filesystems), so the only question is ordering: copy to a hidden temp name,
   verify, atomically rename to the final name, and only then delete anything.
5. **Delete through the *arr API**, not `unlink`, so the *arr's database stays consistent,
   then unmonitor so it does not re-download. Torrent removal uses the client's own API
   (`remove` with delete-data), not file deletion.
6. **Only touch what the *arr tracks.** The client watcher acts only on torrents whose hash
   appears in the *arr queue. Other torrents on the same client are never stopped or removed.
7. **Adapters from day one.** Download clients and *arr apps sit behind small interfaces so
   qBittorrent, Deluge, SABnzbd, Lidarr, … can be contributed without redesign.

## 5. Job lifecycle

One job per imported media file. A download (`downloadId`) can produce several jobs (season
pack, multi-movie torrent).

| State | Entered when | Action |
|---|---|---|
| `SEALED` | Torrent seen complete | Stop torrent; re-read and confirm it is stopped. Recorded per `downloadId`, before any job exists. |
| `IMPORTED` | Webhook received | Resolve final target path via the layout (§7); create the job. |
| `COPYING` | Target healthy | Copy to `.<name>.part` next to the final name, plus sidecar files (e.g. `.srt`). |
| `VERIFIED` | Copy finished | Compare size (default) or checksum (optional). |
| `PUBLISHED` | Verified | Atomic rename `.part` → final name. Notify media server. |
| `RECONCILED` | Published | `DELETE` file via *arr API (removes the staging file), unmonitor movie / episodes. |
| `CLEANED` | Reconciled **and** removal gate open | Remove torrent + data from the client; prune empty staging folders. |
| `DONE` | Cleaned | Terminal. |
| `NEEDS_ATTENTION` | Unrecoverable / ambiguous | Nothing further is deleted. Shown in the UI. |

**Torrent removal gate** (season packs and partial imports): remove the torrent only when
(a) every job for that `downloadId` is at least `RECONCILED`, **and** (b) the *arr queue entry
for that `downloadId` is `imported` or gone (not `importPending`, `importBlocked`,
`importFailed`), **and** (c) a short settle window has passed with no new webhook for it. If
the gate does not open within a timeout, the torrent stays stopped (never deleted) and the job
is flagged.

**Restart safety:** every transition is persisted before it is acted on and every step is
idempotent (`.part` files are recreated, deletes tolerate "already gone"). A crash mid-copy
resumes from `COPYING`.

## 6. Radarr vs Sonarr differences

| | Radarr | Sonarr |
|---|---|---|
| Webhook event used | `Download` (import; upgrades are `isUpgrade`) | `Download` |
| Identifies media | `movie{id,title,year,tmdbId,folderPath}` | `series{id,title,path,tvdbId}`, `episodes[]` |
| File info | `movieFile{id,relativePath,path}` | `episodeFile` / `episodeFiles[]` (shape to be confirmed by capture) |
| Files per download | Normally 1 | Many (season packs, multi-episode files) → refcount in the removal gate |
| API: delete file | `DELETE /api/v3/moviefile/{id}` | `DELETE /api/v3/episodefile/{id}` |
| API: unmonitor | `PUT /api/v3/movie/editor` `{movieIds, monitored:false}` | `PUT /api/v3/episode/monitor` `{episodeIds, monitored:false}` — **episodes only; the series stays monitored so future episodes still download** |
| Native layout | `<Movie Folder>/<file>` | `<Series>/<Season NN>/<file>` |
| Layout | `flat` (default); `preserve` also allowed | **`preserve`, fixed** — series folder **and** season subfolders are required (see §7) |
| Root folder in the *arr | `<staging>/movies` | `<staging>/tv` |

Everything else (staging, verify, publish, cleanup) is shared code behind an `ArrApp` interface.

## 7. Layouts

The layout maps an imported file to its path under the destination root.

| Mode | Result | Used by |
|---|---|---|
| `flat` | `<target>/<file>` | Radarr (default) |
| `preserve` | `<target>/<path relative to the *arr root folder>`, e.g. `<Series>/<Season N>/<file>` | Sonarr (**fixed, not user-selectable**); optional for Radarr |

A `series-folder` mode (`<Series>/<file>`, no season folder) was considered and **dropped**:
TV on the destination always needs the series folder and the season subfolders.

Rules: the filename is always exactly what the *arr produced (its naming format is the single
source of truth). **relocarr never builds folder names itself**: for `preserve` it copies the
path the *arr produced, relative to the *arr's root folder, so series and season folder names
come from Sonarr's *Series Folder Format* and *Season Folder Format*. Folders are created only
as the layout requires. Every computed path is checked to stay inside the target root.

**Folder-name mismatches with an existing library.** Because folder names come from Sonarr,
they must line up with what already exists on the destination, or relocarr will create a
second folder next to the old one:

* *Season folder format.* Sonarr's default is `Season {season}` (`Season 1`). If the
  destination uses `Season 01` (or anything else), align Sonarr's format **before the first
  import**.
* *Series folder name.* Sonarr's default is `{Series Title}`. An existing folder named
  differently (e.g. `Example.Show.2019`) would gain a new sibling `Example Show/`. Add such shows to
  Sonarr with a **series path under the staging root whose folder name is identical to the
  existing destination folder** (`<staging>/tv/Example.Show.2019`). Do **not** point the series at
  the destination itself, or Sonarr would import there directly and bypass relocarr. New
  episodes then copy into the existing folder.
* *Existing shows that have no season folders* keep their old episodes in the show root; new
  episodes land in `Season N/` beside them. The result is a mixed folder (media-server
  behaviour not yet verified).

From v0.2 the preflight page compares Sonarr's series folder names with the folders already on
the destination and warns about collisions and near-matches before anything is copied.

**Conflicts** (target already exists), per app: `replace` (publish the new file, then delete
the old one, after verification), `skip`, or `keep_both` (suffix). Default: `skip` with the job
flagged, so nothing is overwritten silently.

**Sidecars:** files in the same staging folder that share the media file's stem and have a
configured extension (default `srt`) are copied with it, and get the same layout treatment.

## 8. Download client adapters

Interface (Python `Protocol`): `list_torrents()`, `is_complete(id)`, `stop(id)`,
`get_status(id)`, `remove(id, delete_data)`.

* **v0.1: Transmission** (RPC, session-id handshake, optional basic auth).
* **Later:** qBittorrent, Deluge, then usenet clients (SABnzbd/NZBGet have nothing to stop;
  they only need history cleanup).
* The client *list* is fetched from the *arr (`GET /api/v3/downloadclient`), including host,
  port and URL base. The *arr masks passwords in its API, so credentials are entered once in
  the UI. Clients without an adapter appear greyed out ("not supported yet").
* Per client: **Stop on completion** (default on; warn about private-tracker ratios) and
  **Remove after copy** (default on). A future seeding policy (seed to ratio/time, then
  remove) fits because staging is a hardlink and independent of the seeding copy.

## 9. Web UI

Server-rendered (FastAPI + Jinja + htmx), single container, mobile-friendly. **Visual design
intentionally mirrors Radarr/Sonarr's own look and feel** (decided 2026-10-21) — same dark
theme, sidebar layout, and component conventions, so it feels like a natural extension of the
*arr stack rather than a bolted-on separate tool. Reuse Radarr's actual CSS variables/palette as
a starting point rather than designing a new visual language.

| Page | Contents |
|---|---|
| **Setup wizard** | Add an app: type (Radarr/Sonarr), URL, API key → *Test connection*. |
| **App detail** | **Staging folder**: dropdown of the *arr's root folders, or a button to create one. **Target folder**: text field with a live check (exists, writable, free space, mounted). **Layout** mode (locked to `preserve` for Sonarr). **Download clients**: one row per client fetched from the *arr, with adapter status, credentials, stop/remove toggles, *Test*. |
| **Preflight** | Live checklist of the *arr's settings (§10). Green/red per item, a *Fix* button where it is safe. |
| **Jobs** | Table of jobs with state, size, target path, error, **Retry**, **Skip**, per-job log. Banner when the target is offline. |
| **Settings** | UI login, dry-run toggle, poll interval, sidecar extensions, conflict policy, verify mode, notifications, **per-destination max file size** (optional, blank/0 = unlimited; decided 2026-10-21 — a configurable field, never hardcoded, after the 13.6 GB Shield-wedge incident in §16's spike 9 addendum prompted the user to adopt a personal 4.5 GB ceiling on that destination). A job whose file exceeds the configured max is flagged `NEEDS_ATTENTION` rather than attempted. |

Security: first-run password setup (hashed), session cookie, an explicit
`AUTH_DISABLED` env var for reverse-proxy setups. The webhook endpoint requires a shared
secret in a header (the *arr Webhook connection supports custom headers). Config and secrets
live in the `/config` volume with `0600` permissions and are never logged. Bind to the LAN
only by default.

## 10. Prerequisites and preflight (avoiding two automations)

The core risk is *two* things moving files. The preflight page checks each of these through
the *arr API. The same list is the README's setup guide.

### Radarr

| Setting | Required | Why |
|---|---|---|
| Root folders | Only the staging folder (e.g. `<staging>/movies`). Remove any root folder on the destination. | Otherwise Radarr still writes to the destination itself. |
| Existing movies | Repoint to the staging root **without moving files** (Movie Editor → change root folder → "no, I'll move them"). | Their stored paths would otherwise still point at the destination. |
| Collections and Import Lists | Repoint each one's own `Root Folder` field to the staging root (found independently under Movies → Collections, and Settings → Import Lists). | They store their own root folder separately from individual movies; removing the destination root folder leaves these pointing at a now-missing path (confirmed via Radarr's health check, spike 3, 2026-10-21). |
| Download Client → Completed Download Handling (global, above the client list) | **On** | Unrelated to relocarr itself, but required for Radarr to ever notice a finished download. Found off during spike 3 and had evidently been silently blocking all auto-import. |
| Connect → Webhook | Add one: URL of relocarr, method `POST`, header `X-Relocarr-Token: …`; enable **On Import** and **On Upgrade** (later: **On Rename**). | The trigger. |
| Connect → Plex / Jellyfin / Emby | Disabled | They would scan the staging path, which the media server cannot see. relocarr notifies after publishing. |
| Download client → Remove Completed Downloads | **Off** | relocarr removes the torrent itself. With it on, a stopped torrent that has hit a seed limit is *moved* out of the download folder and removed by the *arr (verified in source, spike 1), which collides with relocarr's cleanup. |
| Media Management → Recycle Bin | Empty | Otherwise API deletes fill the recycle bin instead of freeing staging. |
| Media Management → Use Hardlinks instead of Copy | On | Instant, no extra space, requires staging and downloads on the same mount. |
| Media Management → Rename Movies | On | The naming format now defines the filename on the destination. |
| Import Extra Files | Extensions match relocarr's sidecar list | Sidecars are only copied if the *arr imported them. |

### Sonarr

Same table, with `<staging>/tv` as the root folder and *Rename Episodes* on, plus:

| Setting | Required | Why |
|---|---|---|
| Media Management → Season Folder Format | Must produce the same folder names as the destination already uses | relocarr copies Sonarr's path as-is (§7). Mismatch = duplicate season folders. |
| Media Management → Series Folder Format / per-show path | Folder name under `<staging>/tv` identical to the existing destination folder | Mismatch = duplicate series folders. Existing shows need this mapping (§7). |
| Series → Season Folders | On for every series | Season subfolders are mandatory on the destination. |

### Shared

* relocarr's container must mount the download folder, staging folder and destination at the
  **same paths as the *arr containers** (v0.1 has no path mapping).
* Staging must not be inside the destination or vice versa; relocarr refuses to start otherwise.
* Nothing else may write to the staging root. relocarr only deletes inside it, and never
  above it.

## 11. Known limitations (by design)

* **No automatic quality upgrades.** After the copy, the movie/episode is unmonitored and has
  no file in the *arr, so it will never be upgraded. To replace a file, re-monitor the item and
  let it download again; the conflict policy handles the existing destination file.
* **The *arr no longer shows the file** (it will look "missing"/unmonitored). Availability in
  request apps (Seerr/Overseerr) must come from the media server sync. To be verified.
* **Stop-on-completion ends seeding immediately**, which is harmful on private trackers. It is
  a per-client toggle with a warning.

A possible v2 that keeps the *arr in sync (a symlink "shadow library" in staging) is
deliberately out of scope: more moving parts, and upgrade edge cases.

## 12. Robustness

* **Offline destination:** health probe immediately before *each* copy attempt — never a fixed-
  interval poll (e.g. a 60-min cron ping) decoupled from actual attempts, considered and
  rejected 2026-10-21. A ping/port-open check is not sufficient on its own: the 2026-09-19
  incident showed the Shield answering ping and SMB's port being open while the CIFS mount was
  still wedged with no protocol reply, so pure reachability would have reported healthy while
  writes failed. The probe must do real I/O: mount not stale, a small write+delete (not just
  stat/list), and enough free space. On failure, back off exponentially per job (e.g. 1 → 2 → 4
  → 8 min, capped at 30 min) and re-probe right before the next attempt, so a destination that
  recovers is noticed within one backoff interval, not up to an hour late. Imports into staging
  are unaffected while a destination is down. A staging free-space guard raises a UI banner.
* **Half-copied files:** copy goes to a hidden `.part` name so media servers ignore it; renamed
  atomically only after verification. Stale `.part` files are cleaned on startup.
* **Verification:** size by default; optional checksum (reads the file back over the network,
  so it doubles traffic).
* **Copy mechanism: `rsync` (invoked locally as a subprocess), not a hand-rolled read/write
  loop — decided 2026-10-21.** For a plain local-path-to-mounted-share copy, rsync does its own
  internal buffered read/write (it's delta-transfer based, not a `sendfile`/`copy_file_range`
  zero-copy path), so it stays compatible with spike 9's finding without reintroducing the
  risky I/O path. Chosen over hand-rolling this because it gets real transfer features for
  free that the original 13.6 GB Shield-wedge incident (§16, spike 9 addendum) showed the value
  of: `--partial` (and `--partial-dir` for the hidden in-progress name, replacing a bespoke
  `.part` implementation) means an interrupted transfer resumes instead of restarting from
  zero, `--checksum`/`-c` is a mature, already-correct implementation of the optional-checksum
  verification mode above, and progress/rate can be parsed from its own output for the Jobs UI.
  relocarr's job worker shells out to it and parses exit code + stderr for transient-error
  classification, rather than catching I/O exceptions directly in Python.
* **Unreliable destinations:** treat mid-copy I/O errors (`EFAULT`, `EIO`, `ESTALE`, timeouts,
  and rsync's corresponding non-zero exit codes) as transient: retry with backoff (rsync's own
  `--partial` means a retry resumes rather than restarting), never leave a partial file under
  the final name, and include a small write test in the health probe.
* **Concurrency:** one copy at a time per destination by default (configurable).
* **Path safety:** `realpath` checks on every delete; refuse anything outside the staging root;
  never follow symlinks out of it.
* **Dry-run:** logs every action it would take and performs none. Strongly recommended for a
  first run.
* **Idempotency:** duplicate webhooks (retries) are de-duplicated by `(app, fileId, downloadId)`.

## 13. Tech stack

* Python 3.12, FastAPI, Jinja2 + htmx, `httpx`, SQLite (`sqlite3`/SQLAlchemy Core), `pydantic`
  settings, `pytest`, `rsync` (system package in the Docker image — the actual copy mechanism,
  see §12).
* One Docker image (non-root, PUID/PGID, healthcheck), a `/config` volume, one port.
* MIT license.

Proposed layout:

```
relocarr/
├── README.md  PLAN.md  LICENSE  Dockerfile  docker-compose.example.yml  pyproject.toml
├── docs/            radarr-setup.md  sonarr-setup.md  clients.md  troubleshooting.md
├── src/relocarr/
│   ├── main.py  config.py  db.py
│   ├── arr/         base.py  radarr.py  sonarr.py        # API client, payloads, preflight
│   ├── clients/     base.py  transmission.py             # download-client adapters
│   ├── jobs/        machine.py  worker.py  gate.py       # state machine, removal gate
│   ├── transfer/    layout.py  copier.py  safety.py
│   ├── notify/      plex.py
│   └── web/         routes.py  templates/  static/
└── tests/
```

## 14. Testing

* **Unit:** layout mapping, path-safety, state machine with fake client/app/target, removal
  gate (season-pack cases), conflict policies.
* **Contract:** recorded real Radarr/Sonarr webhook payloads and API responses replayed
  against the adapters; a mock Transmission RPC server.
* **Integration:** docker-compose with real Radarr, Sonarr and Transmission plus a local
  target directory; simulate an offline destination by unmounting/permissions.
* **Manual:** a documented checklist for real trackers/shares before each release.

## 15. Roadmap

| Version | Scope |
|---|---|
| **v0.1 (MVP)** — **implemented 2026-10-21/22**, see below | Radarr + Sonarr, Transmission, webhook receiver, staging → destination with the `flat` and `preserve` layouts, size verification, retry when offline, reconcile + cleanup, removal gate, UI (setup, app detail, preflight, jobs), dry-run, Docker image. |
| **v0.2** | Preflight check for Sonarr series/season folder collisions against the destination, Plex library scan, conflict/replace handling, *On Rename* handling, `adopt` command to flatten/import existing per-movie folders on the destination, path mapping, checksum verification, notifications (webhook/ntfy). **Deregister Plex** -- a way to clear the stored connection from Settings → Plex (currently add/edit only, §19; no delete). **Jellyfin quality lookup** -- mirror the Plex integration (§19: background-cached index of each file's real resolution/codec/bitrate, matched by filename) for users running Jellyfin instead of/alongside Plex -- distinct from the existing Jellyfin/Emby *notifications* item under v0.3 below, which is about telling the media server to rescan, not reading quality info from it. Both added to the roadmap 2026-10-25, explicitly not started yet. |
| **v0.3** | qBittorrent (and Deluge) adapter, seeding policy (seed to ratio/time before removal), Jellyfin/Emby notifications, usenet clients. |
| **Later** | Lidarr/Readarr, multiple destinations, optional shadow-library mode. |
| **Distribution (post-MVP, not before)** | AUR package (added to roadmap 2026-10-21) — once v0.1 has an actual tagged release to package. Precedent: Radarr/Sonarr themselves have native AUR packages as an alternative to their Docker images, running as a systemd service rather than wrapping the container. GitHub release + the `docker-compose.yml`/`install.sh` build-from-source flow (§17) remain the primary distribution path; AUR is an additional one for Arch users, not a replacement. |

### v0.1 implementation status (2026-10-21/22)

Built in one continuous session after the spikes above, culminating in an autonomous overnight
continuation. Repo: https://github.com/a000a999a/relocarr, 6 commits (`8ecd2a2` through
`7283cef`), 93 passing tests. Every v0.1 roadmap item above is implemented and exercised by
tests; most were additionally smoke-tested live against the real local Radarr instance (adding
the app, configuring target/layout/download-client through the actual forms, running the real
Preflight page — which caught and fixed a genuine leftover misconfiguration from the manual
migration earlier the same night) and the real Docker image (built, run, non-root/PUID-PGID
confirmed via `/proc/1/status`, healthcheck confirmed transitioning to healthy).

**Not yet done, deliberately left for the user:** an actual end-to-end run with `dry_run` turned
off against the real Shield. Every individual piece has been verified against real systems, but
no one has yet pointed a running relocarr instance at production with dry-run off and watched a
real file move through the whole pipeline unattended. Recommended first step once reviewed:
leave `dry_run` on, add the real webhook connection in Radarr pointing at a running relocarr
instance, and watch the dry-run log output for a real import before flipping it off.

Two real bugs were found and fixed *by* the process of building this, not just by writing
tests: an rsync exit-code-23 ambiguity (shared between genuine transient failures and a simple
missing source file — distinguished by rsync's `[sender]` error tag, see §12), and a real-world
Radarr Plex-notification misconfiguration from earlier tonight's manual walkthrough (`onUpgrade`
and `onMovieFileDeleteForUpgrade` were still enabled, caught by the new Preflight page and fixed
live via the API).

## 16. Spikes to run before writing real code

Each of these is an assumption that has **not** been verified yet.

1. Does Radarr/Sonarr still import a **stopped**, fully-downloaded torrent, and does the queue
   report it as importable? (Determines whether stop-on-completion can precede the import.)
2. Capture **real webhook payloads**: Radarr import and upgrade; Sonarr single episode,
   multi-episode file, season pack. Confirm field names, especially `episodeFile(s)`,
   `downloadClient`, `downloadId`, `deletedFiles`.
3. Does the *arr accept a root folder that is a sibling of the download client's folder, with
   no health warnings? Do hardlinks work from the download folder to staging (same bind mount)?
4. Transmission RPC: session-id handshake, auth, stop status codes, and what `remove` with
   delete-data does when the data is hardlinked into staging.
5. Queue `trackedDownloadState` values during and after import (input for the removal gate).
6. Media-server behaviour: Plex matching for the chosen filenames, and request-app (Seerr)
   availability once the *arr holds no file.
7. Sonarr `preserve` layout with season packs and multi-episode files, including season/series
   folder-name mismatches against an existing library (§7) and a show that mixes old flat
   episodes with new `Season N/` folders.
8. Behaviour of empty movie/series folders left in staging after `DELETE …/file`.
9. Characterise the destination's copy failures (see "New finding" below): does `cp`
   (`copy_file_range`) fail where a plain read/write loop succeeds, and what do the share
   server's logs say?

### Spike results

**Spike 1: stopped torrents are imported. PASS** (2026-09-19, Radarr 6.3, Sonarr 4.0)

* *Source.* Radarr's `TransmissionBase` maps `LeftUntilDone == 0` with status `Stopped`,
  `Seeding` or `SeedingWait` to `Completed`. Sonarr's block is identical (diffed; only the
  error-message formatting differs).
* *Live system.* Four torrents were stopped at 100 %. Radarr reported them as `completed`
  (`trackedDownloadState`: `importPending` / `importing`) and ran its import pipeline through
  the decision step to the file copy, so the stopped state does not block import. A failed
  import stays `importPending` with a "Failed to import movie" message, so the removal gate
  (§5) stays closed, as designed.
* *Import mode.* `CanMoveFiles = RemoveCompleted && HasReachedSeedLimit && Stopped`. With
  Remove Completed off the *arr never moves the file; it hardlinks or copies. This is why that
  setting is required off (§10).
* *Not yet proven end-to-end.* No import of a stopped torrent has *completed* yet, because the
  copies failed at the destination (below). Re-verify once the staging root exists (spike 3).
  The torrents were stopped by Transmission's own limits, not by relocarr; the client state
  (`status 0`) is the same either way.

**New finding: the destination is unreliable.** In the retained Radarr logs there are 1213
`System.IO.IOException: Bad address` errors during the copy to the CIFS mount, the kernel logs
`CIFS: VFS: reconnect tcon failed rc = -2`, and one destination file was left partial (about
1.2 GB of 1.79 GB) before being rolled back and retried. The root cause is **not established**;
a share/connection problem is the leading suspect. Design impact: §12 now requires
retry-on-transient-error, `.part` files and no reliance on `sendfile`/`copy_file_range`, and
spike 9 was added. **Update (2026-09-19 21:40):** the failure is on the destination *server*: while testing, a fresh SMB connection to it got a TCP connect but no protocol reply, its Plex port refused connections, and the local CIFS mount wedged (unkillable writers). Radarr had not imported anything to it since the Aug 24 migration. A test write of mine (a 1.7 GB `dd`) coincided with the wedge, so large test writes to that share should be avoided until it is healthy. This is also a strong argument for the staging design: imports would
succeed locally and only the ferry step would retry.

**Spike 3: staging root as a sibling of the download client's folder. PASS** (2026-10-21,
Radarr 6.4.4, real production library)

* The Shield outage (started 2026-10-04) resolved on its own; while bringing Radarr/Sonarr back
  up, the real staging migration from §10 was done by hand against the live instances instead
  of a throwaway test, so this spike got verified as a side effect: `/mnt/nvidia` was removed as
  Radarr's and Sonarr's only root folder and replaced with
  `/home/user/Downloads/transmission/relocarr/{movies,tv}` — a subfolder of the same
  `~/Downloads/transmission` tree Transmission downloads into, same filesystem.
* Radarr's own health check (`RootFolderCheck`/download-client-path check) stayed clean for this
  layout — no warning about the root folder being inside/beside the download client's working
  directory.
* `Use Hardlinks instead of Copy` was already on. Three real completed downloads (all
  movies, one with an `.srt` sidecar) were auto-imported by Radarr
  after `enableCompletedDownloadHandling` was turned on (it had been off, unrelated to this
  migration but was silently blocking all auto-import) — each file landed in staging with link
  count 2, confirming an actual hardlink, not a copy, and zero extra disk space used.
* Existing movies/series were repointed to the new root via `Movie Editor`-equivalent (API
  `PUT /movie` and `PUT /series`) with move disabled; files already on the Shield were
  confirmed untouched afterward. Radarr additionally required repointing two *Collections* and
  one *Import List* (Plex Watchlist) that had their own independent `rootFolderPath` pointing at
  `/mnt/nvidia` — not mentioned in §10, add to the prerequisites checklist.
* Sonarr currently tracks **zero series** (library is empty) — spike 3 only covers the Radarr
  side end-to-end; the equivalent Sonarr import-into-staging path is unverified.

**Spike 7 (partial): real folder-naming mismatch confirmed.** Auditing the Shield's existing
`/mnt/nvidia/series/` during the Sonarr migration (2026-10-21) found inconsistent layout across
shows, not just a theoretical risk: *Show A* uses `Season 1`, `Season 2`,
`Season 12` — matching Sonarr's current default `seasonFolderFormat` (`Season {season}`)
exactly — but *Show B* has **no season folders at all**, episodes sit flat in the
series root (`Show B 1x01.mkv`, …). Re-adding Show B to Sonarr today would create
a `Season 3/` folder next to the old flat files per §7's predicted "mixed folder" case. No
series are currently being re-added to Sonarr (deliberately held back) until relocarr or a
preflight check can catch this per-show before import.

**Spike 2 (partial): real Radarr webhook payload captured.** (2026-10-21, Radarr 6.4.4)

* A throwaway webhook listener was pointed at via a temporary Connect → Webhook entry
  (`On Import`/`On Upgrade`/`On Rename`), reached from inside the Radarr container at its
  Docker bridge gateway IP (`172.18.0.1`), not `localhost` — the container has its own network
  namespace (`arr-stack_lan-net`). Both the webhook connection and listener were removed after;
  samples saved to `docs/webhook-samples/`.
* Captured a real `eventType: "Download"` payload by adding *Example Film* to Radarr and
  running a Manual Import against its already-downloaded file
  (`radarr-manual-import-no-downloadid-2026-10-21.txt`). Confirms top-level shape: `movie`,
  `remoteMovie`, `movieFile` (with nested `mediaInfo` and `sourcePath` — the original download
  path before the hardlink), `isUpgrade`, `customFormatInfo`, `release`, `eventType`,
  `instanceName`.
* **Important gap found:** this manually-imported payload has **no `downloadId` or
  `downloadClient` field at all.** Cross-checked against Radarr's History API
  (`/api/v3/history/movie`) for the three *real* download-triggered imports done earlier the
  same day (spike 3) — those DO carry `downloadId` (the Transmission torrent hash, e.g.
  `0123456789ABCDEF0123456789ABCDEF01234567`) and `downloadClient`/`downloadClientName`
  (sample saved as `radarr-history-real-download-with-downloadid-2026-10-21.json`; history's
  `eventType` for this is `downloadFolderImported`, not `Download` — the webhook and history
  feeds use different event-name vocabularies for the same event, parse accordingly).
  **Design impact:** `downloadId` must be treated as optional in the job model. A webhook
  without one (manual import, or any import not driven by the tracked download queue) cannot
  participate in the removal gate (§5), since there is no torrent to correlate against or
  remove — these need a simpler, gate-skipping path straight to cleanup.
* **Still not captured:** a genuine webhook fired by an actual queue-driven download (only
  verified indirectly via History, since the three real imports happened before the webhook
  was wired up) and any Sonarr payload (single episode, multi-episode file, season pack) —
  Sonarr currently has zero tracked series, so there's nothing to generate one from yet.

**Spike 4: Transmission RPC. PASS** (2026-10-21, Transmission 6.0.1 via rpc-version 6.0.1)

* *Session handshake.* Confirmed exactly per spec: an initial request gets `409 Conflict` with
  an `X-Transmission-Session-Id` header; resending with that header as a request header
  succeeds. No separate auth challenge observed (Radarr/Sonarr's client config has blank
  username/password, so this instance has auth disabled — session-id CSRF protection is
  independent of that and always required).
* *Status semantics.* `torrent-get` status `0` = stopped. Found that `isFinished` stayed
  `false` on all 5 torrents even at `percentDone: 1.0` and status stopped — `isFinished` tracks
  "seeding finished by hitting a ratio/idle limit", not "download complete". **Design impact:**
  relocarr's completion check must be `percentDone == 1.0`, not `isFinished`; stopping must be
  done explicitly via `torrent-stop`, never inferred from this flag.
* *Hash casing mismatch.* Transmission's `hashString` is lowercase
  (`0123456789abcdef0123456789abcdef01234567`); Radarr's `downloadId` for the same torrent
  (confirmed via History API, spike 2) is uppercase
  (`0123456789ABCDEF0123456789ABCDEF01234567`). **Design impact:** the client/arr correlation
  by hash must be case-insensitive.
* *`torrent-stop` with no `ids` is not reliable for "all torrents".* Tested live (2026-10-21):
  calling `torrent-stop` with an empty `arguments: {}` (no `ids` key) returned `"success"` but
  left an actively-seeding torrent (status 6) untouched; only passing explicit `ids: [...]`
  stopped it. **Design impact:** the client adapter's `stop(id)` must always pass the specific
  torrent id — never rely on an "ids omitted = all" shortcut, despite some RPC docs implying
  that behavior.
* *`remove` with `delete-local-data` on a hardlinked file — the actual question this spike was
  added for.* Tested for real on a live, already-imported download (Sidecar Movie, torrent
  id 5): confirmed link count 2 beforehand (one in `~/Downloads/transmission/radarr/…`, one in
  staging), called `torrent-remove` with `delete-local-data: true`. Result: the Downloads-side
  original was deleted, the torrent was delisted from Transmission, and **the staging copy
  survived completely intact**, link count now 1. Confirms the core safety assumption behind
  the whole design — removing the torrent's data can never touch a file relocarr has already
  published, because hardlinks are independent references to the same inode.

**Spike 5 (partial): queue trackedDownloadState. PASS for the success path, failure states not
observed** (2026-10-21, Radarr 6.4.4, real grab of a movie)

* While actively downloading, `/api/v3/queue` reports `trackedDownloadState: "downloading"`,
  `trackedDownloadStatus: "ok"`, plus `downloadId` (uppercase, matches Transmission's hash
  case-insensitively per spike 4), `downloadClient: "Transmission"`, `outputPath`, `sizeleft`.
* Polled every 20s (both a background logger and a `Monitor` watch, independently) through the
  entire download. The import step — `downloading` → queue entry gone — happened **inside a
  single 20s poll window**: neither poll caught an intermediate `importPending`/`importing`
  state. **Design impact:** relocarr's own queue-state polling cannot assume it will observe
  transient import states for a fast/local import; it must treat "entry disappeared from the
  queue" and "History shows `downloadFolderImported` for this `downloadId`" as the
  authoritative success signal, not a specific transient state value. The removal gate's
  "`imported` or gone" condition (§5) holds: the entry was simply gone once Radarr's own history
  confirmed `downloadFolderImported` at the same timestamp (20:29:00Z).
* **Not observed:** `importPending` (sustained), `importBlocked`, `importFailed` — this spike
  only exercised a clean successful download. Would need a deliberately broken scenario (e.g. a
  file Radarr can't match, or a destination write failure) to see those; not attempted here.
* Confirmed (again) hardlink import worked correctly: link count 2 on the new file in staging.

**Spike 8: empty folders after `DELETE …/file`. PASS** (2026-10-21, Radarr 6.4.4, real movie —
Movie B)

* Ran the real §5 `RECONCILED` step against a live staged movie: `DELETE /api/v3/moviefile/18`
  (HTTP 200) followed by unmonitoring via `PUT /api/v3/movie/editor`
  (`{"movieIds":[15],"monitored":false}`, confirmed `monitored: false` in the response).
* The per-movie folder (`Movie B2007/`) **survived, empty** — matches the
  "Delete empty movie folders" setting being off (confirmed unchecked earlier in the Radarr UI
  walkthrough).
* Also triggered an explicit `RescanMovie` command afterward: the empty folder still survived.
  **Confirms Radarr never prunes empty folders on its own, under any setting combination tested
  here, even on a manual rescan** — pruning empty staging folders (§5 `CLEANED` step, §12) is
  entirely relocarr's own responsibility, not something that can be delegated to a Radarr
  setting. The original download in `~/Downloads/transmission/radarr/` was left untouched by
  this step, as expected — torrent cleanup is a separate action (spike 4).

**Spike 9 (partial): destination copy behavior on a healthy Shield. PASS for the success path**
(2026-10-21)

* Before testing, confirmed the Shield is actually healthy now (it was not, back on
  2026-09-19): ping to the Shield <1.5ms, SMB port 445 open, Plex port 32400 open, CIFS mount
  reads fine, 678G free of 5.5T.
* Tested both candidate copy methods with a known checksum, to the real §9 question (does `cp`
  fail where a plain read/write loop succeeds): `cp` of a 500MB file — succeeded, 12s
  (~43 MB/s), checksum matched. A plain Python read/write loop (4MB chunks) of a 2GB file —
  succeeded, 84.5s (~25.4 MB/s), checksum matched exactly.
* **Conclusion: the original 1213 `IOException: Bad address` failures (spike 1's "New finding")
  were caused by the destination server itself being unhealthy at the time, not by `cp`'s
  `copy_file_range`/`sendfile` I/O path specifically** — on a healthy share, `cp` works fine.
  §12's design decision (retry-on-transient-error, `.part` files, plain read/write loop) stays
  correct regardless — a healthy share today doesn't guarantee one tomorrow, and the whole point
  of the staging design is tolerating exactly this kind of destination flakiness — but this
  spike did not reproduce an actual transient-error scenario to test the retry path itself.
  That would require deliberately degrading the share, not attempted here.

**Spike 9 addendum: reproduced the wedge for real, on a large file.** (2026-10-21, ~21:17–21:47)

* While ferrying the real staging backlog to the Shield as a stopgap (4 clean titles succeeded,
  one with 52 `.srt` sidecars — all
  verified by size, one additionally by checksum after an unrelated operator race condition),
  a `cp` of a 13.6 GB file (a 2026 movie) wedged the Shield mid-transfer.
* Symptoms matched the 2026-09-19 incident exactly: `ls /mnt/nvidia/` started returning
  `Host is down` for the *entire* mount, while `ping` to the Shield kept succeeding
  (<1.5 ms) throughout and the relocarr host itself stayed fully responsive. The `cp` process landed in
  **D state** (uninterruptible kernel sleep) — confirmed via `ps`, un-killable even with
  `SIGKILL` — and stayed wedged for 30+ minutes (a background watch hit its cap still blocked,
  never recovered on its own within that window).
* **Pattern: both wedge incidents (2026-09-19's 1.7 GB `dd`, this 13.6 GB `cp`) involved a
  single large sustained write.** The three smaller real transfers earlier the same evening
  (2.35–2.8 GB each) all completed cleanly. This is circumstantial but consistent across both
  incidents on two different dates — worth treating as a real lead, not coincidence. Root cause
  still not established (could be an SMB timeout/buffer setting, a resource limit on the Shield
  itself, thermal throttling, anything) — characterizing it further would need deliberately
  reproducing it with controlled file sizes, not attempted here.
* **Resolved as a relocarr feature, not left to the operator:** rather than chase the root
  cause, the user set a personal policy of never sending files >4.5 GB to this Shield, and
  abandoned that movie entirely (deleted from Radarr with `deleteFiles=true`, staging content
  and orphaned Downloads folder removed). **Decided 2026-10-21:** relocarr enforces this as a
  configurable **per-destination max file size** field on the Settings page (§9) — never
  hardcoded. A job whose file exceeds the configured limit is flagged `NEEDS_ATTENTION` instead
  of attempted, so a doomed large copy never gets a chance to wedge the destination again.
* **Process hygiene lesson (unrelated to the Shield):** while copying one of the titles, a background `cp` was
  accidentally started twice (once via a bare shell `&`, once via the properly-tracked backgrounding
  mechanism) — two writers to the same destination file concurrently. Caught before completion,
  the duplicate was killed, and the result was checksum-verified clean. **Never background a copy
  manually outside the tracked mechanism** — it breaks the "one copy at a time per destination"
  invariant from this section without any visibility that it happened.

## 17. Reference deployment (shipped, 2026-10-23)

No prebuilt image is published (no `ghcr.io`/Docker Hub tag) — every user builds locally from
source, via `docker-compose.yml` (builds the image with `build: .`) and `./install.sh`, both in
the repo root. `install.sh` is interactive: it prompts for the staging/target roots, optionally
an existing Docker network to join by name (so relocarr can reach Radarr/Sonarr/Transmission by
container hostname — left blank, it falls back to the default bridge network and the *arr apps
are reached by host IP instead), PUID/PGID, and the port, then writes `.env` and runs
`docker compose up -d --build`. The two root paths are bind-mounted source==target
(`${DOWNLOADS_PATH}:${DOWNLOADS_PATH}`, `${MEDIA_PATH}:${MEDIA_PATH}`) since relocarr has to see
each file at the exact path the *arr reports it at in the webhook payload — see `.env.example`.

Re-running `install.sh` with no flags reuses the existing `.env`, rebuilds, and restarts without
touching configured apps/jobs (the SQLite DB lives in the bind-mounted `./config`, untouched by
a container recreate). `--reconfigure` re-asks every question (backs up the old `.env` first);
`--reset-db` wipes just the database for a true from-scratch reconfigure (backs it up first too).
Verified against the real production deployment: stopped the old hand-run `docker run`
container, ran `install.sh --reconfigure` with the real values, confirmed both configured apps
and all job history survived untouched, container healthy, scheduler polling normally.

Suggested layout on disk: downloads in `/downloads/<client>/<category>`, staging in
`/downloads/<client>/staging/movies` and `/downloads/<client>/staging/tv` (same mount as the
downloads so hardlinks work), destination on the network share.

## 18. Open questions

* Ship a `docker-compose` "full demo" stack (Radarr + Sonarr + Transmission + relocarr, all
  preconfigured) for contributors, on top of the basic single-service compose file that now
  ships in §17?
* Should the UI be able to create the *arr Webhook connection automatically, or only verify it?
  (Leaning: automatic, with a confirmation.)
* Publish a prebuilt image (GHCR) once v0.1 has settled, so `install.sh` doesn't need to build
  from source on every machine?
* Check why the System page's "Apps configured" count only shows 2 when Transmission and Plex
  are also configured (added 2026-10-25, not investigated yet).
* Check where the "smaller than current file" redownload threshold is configured/stored --
  user recalls it may be <10% (added 2026-10-25, not investigated yet).
* Revisit "Find completed torrents still in Transmission" -- user's original intent: analyze
  whether the movie file was actually transferred to target, then clean up orphan torrent
  records in Transmission. Needs a fresh look at what the current logic actually checks vs.
  that intent (added 2026-10-25, not investigated yet).
* **Item 4 -- Plex posters + deep link.** On the Library page's redownload table and the
  duplicate-review page, switch the poster source to Plex's own artwork (and make it a
  clickable link to that movie's page in Plex) whenever Plex is configured; when it's not,
  show a placeholder reading exactly "Plex integration needed" instead of falling back to
  Radarr's poster. Ship alongside a version bump (1.0.0 -> 1.1.0) and fuller Plex
  documentation. **Explicit instruction: do not start before 2026-10-29, and even then only
  when the user asks -- do not auto-start.** (added 2026-10-25, not investigated yet)
* **Item 5 -- file transfers destabilizing the Shield.** User reports bugs in the file
  transfer causing the Nvidia Shield's own service(s) to go down/become unstable. Not yet
  investigated -- no confirmed root cause. (added 2026-10-28)
* **Item 6 -- half-transferred files left behind after Shield reboots caused by item 5.**
  Several resumed transfers during the item 5 instability (which required manually rebooting
  the Shield) left cryptic leftover files in the target folder, shaped like
  `.Example Movie.2025.mp4.jfZJw6` -- a leading `.` and a random 6-character suffix after the
  real extension. User supplied a screenshot with several more examples of the same shape
  (different titles/years, same dot-prefix + 6-char-suffix pattern -- real filenames
  deliberately not repeated here, see the screenshot in this conversation for the exact
  originals if needed). Unconfirmed hypothesis, not yet
  checked against the real rsync invocation in `transfer/copier.py`: this naming shape
  (hidden dot-prefix + random suffix) looks like rsync's own default temp-file convention
  for an in-progress transfer, which would normally get renamed to the real filename on
  success and deleted on a clean failure -- worth checking whether relocarr's rsync call uses
  a specific `--temp-dir`/suffix option or relies on this default, and whether cleanup after
  an abnormal interruption (destination crashing mid-transfer) is handled at all. Also worth
  a cleanup pass on the leftover files themselves once the cause is understood -- don't just
  delete them blind before confirming they're not something still in use. (added 2026-10-28)
* **Item 7 -- check whether the new Plex integration itself is contributing to the item 5
  instability.** User suspects the instability may come from either the file transfer process
  or the new Plex quality-lookup background check (added 2026-10-25, §19) -- not yet
  determined which, or whether it's unrelated to both. User has switched Plex off
  (Settings -> Plex) to isolate the variable and analyze step by step. (added 2026-10-28)
* **Item 8 -- would enabling ADB over Wi-Fi on the Shield's developer settings help diagnose
  items 5/6/7?** Raised as a possible additional diagnostic tool, not yet evaluated or acted
  on. (added 2026-10-28)

## 19. Post-v0.1 feature additions (2026-10-24/25)

Built in production against the real deployment (§17), not from the roadmap above — each one
driven by a real operational problem hit while actually running the v0.1 pipeline day to day.

**Redownload (Log page).** The Log page's size-filtered file list can trigger Radarr to replace
an oversized file with a smaller one at the same quality — per file or in bulk. Deliberately
**not** a blind `MoviesSearch`: Radarr's own auto-pick only ever replaces a file it judges a
quality *upgrade*, so left to itself it finds nothing for "smaller, same quality." Instead uses
Radarr's interactive search (`GET /api/v3/release`, the same list its manual-search UI shows),
overriding only the "existing file meets cutoff" rejection, and ranks candidates to match how they
were actually being picked by hand: zero-seeder releases excluded outright, then ranked by
seeder health (≥10 seeders beats anything below, but the best-seeded option still wins if
nothing clears that bar), then resolution, then seeder count, then smallest size
(`arr/radarr.py`'s `find_smaller_release`). Nothing is downloaded if nothing smaller turns up.
Once a smaller file lands, any older/larger file sharing the same (title, year) is removed
automatically (`library.py`'s `remove_larger_duplicates`), closing the gap left by Radarr's own
file tracking not knowing about a pre-relocarr duplicate.

**Log page: file age and poster (2026-10-25).** The same filtered file list now shows each
file's modification time (Linux has no true file-birth-time field via plain `os.stat()`; for a
file written once and never touched again, mtime is close enough) and a poster thumbnail pulled
from Radarr's own movie index (the TMDB-hosted `remoteUrl`, so the browser loads it directly, no
Radarr auth needed) — both to make it obvious at a glance whether a file was recently
redownloaded, without having to click into Radarr to check.

**Jobs page maintenance tools (2026-10-25).** Three manual fallbacks, each covering a gap the
automatic pipeline (§5's removal gate, the webhook-driven job setup) can leave behind in
practice:

- *Find completed torrents still in Transmission* — the removal gate only removes a torrent once
  every job sharing that download reaches `RECONCILED`; a stuck sibling, a torrent removed from
  Transmission out-of-band, or no download client configured each leave it seeding indefinitely
  with nothing left to retrigger the gate.
- *Find transferred movies on source* — same gap, from the file's side: a `DONE` job whose
  staging file is still on disk (Radarr's own `delete_file` API normally removes it during
  `RECONCILED`, but not reliably in every case). Still-seeding files are removed through
  Transmission (stopped first); everything else is deleted directly.
- *Find & trigger missed transfers to target folder (i.e. your Plex library)* — confirmed live,
  2026-10-25: a missed webhook delivery (no
  retry on Radarr's side) left movies Radarr had fully imported with **no relocarr job at all**,
  never copied to the target — 31 found on the first run. Scans Radarr's live movie list
  directly (not relocarr's own job history, which is exactly what's incomplete here) for a file
  with no matching job and backfills one, computed the same way the webhook handler would have.
  `download_id` is left `NULL` for these (no reliable way to recover which torrent produced a
  file from Radarr's movie API alone), so they rely on the two tools above as their own fallback
  if a torrent is still sitting around for one of them.
