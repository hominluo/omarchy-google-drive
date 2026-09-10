# Google Drive for Omarchy

Keep a **real on-disk folder** two-way synced with the Google Drive folders you
choose — and never download the ones you don't.

<p align="center">
  <img src="preview.png" alt="The Google Drive panel: storage summary, sync controls, and a per-folder picker" width="440">
</p>

<p align="center">
  <img src="docs/bar.png" alt="The widget in the Omarchy bar" width="620">
</p>

Google ships no Drive client for Linux. The usual workaround is an rclone FUSE
mount, but a mount means your files aren't really on disk: they vanish when
you're offline, and every app pays a network round trip. A full sync fixes that
but drags your entire Drive down.

This plugin does neither by default. You tick the folders worth having locally;
they become ordinary files that any app can open offline. Everything else stays
untouched in the cloud, still reachable through an optional read-only browse
mount.

| | rclone mount | Full sync | This plugin |
|---|---|---|---|
| Files really on disk | No | Yes | Yes, for what you pick |
| Works offline | Only what's cached | Yes | Yes |
| Downloads everything | No | Yes | No |
| Survives a shell restart | Sync dies with the bar | — | systemd timer keeps going |

## How it works

- **Selection → filters.** The folders you tick become an rclone
  `--filters-file`. Everything else is excluded, so bisync never even lists it.
- **Sync.** A systemd user timer runs [`rclone bisync`](https://rclone.org/bisync/)
  on an interval. bisync holds its own lock, and `Type=oneshot` stops runs from
  overlapping.
- **Baseline.** bisync needs a baseline (`--resync`) before its first run, and
  again whenever the filter set changes. The backend records a hash of the
  filters file after each good run and re-baselines automatically when it
  differs — so ticking a new folder just works instead of erroring out.
- **Google-native files** (Docs, Sheets, Slides) are skipped with
  `--drive-skip-gdocs`. They have no real file to store; open them in a browser.

Credentials belong entirely to rclone. This plugin never reads or writes your
client ID, client secret, or OAuth token.

## Requirements

| | Needed for | Package |
|---|---|---|
| Omarchy with the Quickshell plugin runtime | the widget itself | — |
| `rclone` (1.66+) | all syncing and mounting; `bisync --resync-mode` and `--conflict-resolve` | `rclone` |
| `python3` | the backend | `python` |
| A systemd **user** session | the sync timer and browse mount units | — |
| `findmnt` | detecting mounts and stale endpoints | `util-linux` |
| `fusermount3` | unmounting the browse view | `fuse3` |
| `nautilus` | the **Open** buttons, via `uwsm-app` | `nautilus` |
| `omarchy-launch-browser` | opening Drive in a browser | Omarchy |

Only `rclone` is strictly required. Without `fuse3` the browse mount is
unavailable; without `nautilus` the Open buttons do nothing. Syncing itself
needs neither.

An authenticated rclone Google Drive remote is also required — see below.

## Setting up rclone

The plugin drives rclone but never configures it, so do this first. It is a
one-time setup.

### 1. Install rclone

```sh
omarchy pkg add rclone fuse3
```

`fuse3` is only needed for the optional browse mount.

### 2. Create your own Google OAuth client

rclone's shared Google Drive OAuth client is being retired during 2026, and it
is heavily rate-limited in the meantime. Make your own — it is free and takes a
few minutes.

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create
   a project (or pick an existing one).
2. **APIs & Services → Library →** enable the **Google Drive API**.
3. **APIs & Services → OAuth consent screen →** choose **External**. Fill in the
   app name and your email.
   **Publish the app to Production.** Left in *Testing*, Google expires your
   refresh token after **7 days** and the sync silently stops.
   Personal use does not require Google's verification review.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →**
   application type **Desktop app**. Copy the **client ID** and **client secret**.

Full detail lives in rclone's own
[client ID guide](https://rclone.org/drive/#making-your-own-client-id).

### 3. Create the remote

Non-interactive, in one line — this opens a browser for consent:

```sh
rclone config create gdrive drive \
  client_id=YOUR_CLIENT_ID.apps.googleusercontent.com \
  client_secret=YOUR_CLIENT_SECRET \
  scope=drive
```

Answer `y` to the web-browser question. Google will warn that the app is
unverified — that is expected for a personal client; choose **Advanced → Go to
… (unsafe)**.

`scope=drive` gives read/write to your whole Drive, which is what a two-way sync
needs. Use `drive.readonly` if you only ever want to pull down.

Prefer a guided walkthrough? Plain `rclone config` asks the same questions.

### 4. Verify

```sh
rclone listremotes      # should list gdrive:
rclone about gdrive:    # should print your storage usage
```

If `rclone about` prints numbers, you are done.

> **Migrating an existing `gdrive:` off rclone's shared client:** run
> `rclone config`, choose **Edit existing remote → gdrive**, enter your own
> client ID and secret, keep the scope, and replace the token when prompted.

## Install

```sh
omarchy plugin add https://github.com/hominluo/omarchy-google-drive.git --enable
```

The widget appears in the right section of the bar. Open the panel and tick the
folders you want on disk — nothing is downloaded until you do.

If rclone or the remote is missing, the panel says so and changes nothing.

## Panel

| Key | Action |
|---|---|
| `j` / `k`, arrows | Move through folders |
| Enter / Space | Toggle the selected folder, or the auto-sync switch |
| `s` | Sync now |
| `a` | Toggle automatic sync |
| `b` | Mount / unmount the browse view |
| `o` | Open the synced folder |
| `r` | Refresh |
| `c` | Clean up folders you stopped syncing |
| Escape | Close |

Left click opens the panel, right click refreshes. Right-clicking **Browse all**
opens the mounted browse folder instead of toggling it.

## Settings

```sh
omarchy bar set io.github.hominluo.google-drive remoteName gdrive
omarchy bar set io.github.hominluo.google-drive folderPath "$HOME/Google Drive"
omarchy bar set io.github.hominluo.google-drive browseMountPath "$HOME/GDrive-Browse"
omarchy bar set io.github.hominluo.google-drive syncIntervalMin 10 --json
```

| Setting | Default | Description |
|---|---:|---|
| `remoteName` | `gdrive` | rclone remote name, no trailing colon |
| `folderPath` | `~/Google Drive` | The real on-disk synced folder |
| `browseMountPath` | `~/GDrive-Browse` | Read-only on-demand view of the whole Drive |
| `syncIntervalMin` | `10` | Written to a systemd timer drop-in |
| `refreshIntervalSec` | `30` | How often the panel re-reads status |

## Deleting local copies

Unticking a folder only *stops syncing* it — the files stay on disk, and the
panel reports how much space they use. **Clean up** deletes them, but only after
`rclone check --one-way` proves every local file still exists in Drive. If that
check fails for a folder it is left alone and reported. Nothing is ever deleted
on an unverified path.

## Surviving a reboot

Everything that should come back does, and none of it depends on the bar
running:

- **Sync** — `omarchy-gdrive-sync.timer` is `WantedBy=timers.target`, rearmed at
  login, firing 2 minutes after boot and every 10 minutes after that.
- **Browse mount** — `omarchy-gdrive-browse.service` is
  `WantedBy=default.target`. The panel's Browse toggle enables/disables the unit
  rather than just mounting, so whichever state you left it in is what you get
  back.
- **Stale mounts** — a FUSE mount whose daemon died (power loss, crash) stays in
  the mount table and answers every call with `ENOTCONN`. The backend probes
  liveness instead of trusting the table, reports it, and lazily detaches the
  corpse before remounting.
- **Orphaned bisync lock** — a run killed mid-flight leaves a `.lck` that blocks
  every later run. It is cleared automatically, but only after confirming no
  `rclone bisync` process is actually alive.
- **Network not up yet** — a sync that fails purely because the network isn't
  ready is recorded as `offline`, not `error`. The bar shows "Waiting for
  network" and the next tick picks it up.

These are per-user units, so they run once you log in. To keep syncing while
logged out:

```sh
sudo loginctl enable-linger "$USER"
```

## Diagnostics

```sh
omarchy-shell io.github.hominluo.google-drive status
python3 ~/.config/omarchy/plugins/io.github.hominluo.google-drive/gdrive-sync.py status | jq

systemctl --user status omarchy-gdrive-sync.service
systemctl --user status omarchy-gdrive-browse.service
systemctl --user list-timers omarchy-gdrive-sync.timer
journalctl --user -u omarchy-gdrive-sync.service -n 50

tail -40 ~/.local/state/omarchy-gdrive/sync.log     # rclone's own log
cat ~/.local/state/omarchy-gdrive/filters.txt       # generated selection
jq . ~/.local/state/omarchy-gdrive/state.json       # last run result

# Prove the boot path without rebooting
systemctl --user stop omarchy-gdrive-browse.service omarchy-gdrive-sync.timer
systemctl --user start default.target timers.target
```

Force a fresh baseline (safe — bisync builds a superset, it does not blindly
delete):

```sh
python3 ~/.config/omarchy/plugins/io.github.hominluo.google-drive/gdrive-sync.py run --resync
```

## State

```
~/.local/state/omarchy-gdrive/
  selection.json   folders you picked
  filters.txt      generated rclone filter rules
  state.json       last run result + filters hash
  sync.log         rclone bisync log
  workdir/         bisync's own listings
~/.config/systemd/user/omarchy-gdrive-sync.{service,timer}
~/.config/systemd/user/omarchy-gdrive-sync.timer.d/interval.conf
~/.config/systemd/user/omarchy-gdrive-browse.service
```

## Security

Omarchy plugins run unsandboxed as your user. This one installs no packages,
asks for no elevated privileges, and never touches rclone's config. Every
command is executed as an argument array, never an interpolated shell string.

## Removing

```sh
python3 ~/.config/omarchy/plugins/io.github.hominluo.google-drive/gdrive-sync.py timer --disable
python3 ~/.config/omarchy/plugins/io.github.hominluo.google-drive/gdrive-sync.py browse --disable
systemctl --user disable --now omarchy-gdrive-sync.timer omarchy-gdrive-browse.service
rm ~/.config/systemd/user/omarchy-gdrive-sync.{service,timer}
rm ~/.config/systemd/user/omarchy-gdrive-browse.service
rm -rf ~/.config/systemd/user/omarchy-gdrive-sync.timer.d
systemctl --user daemon-reload
omarchy plugin remove io.github.hominluo.google-drive --yes
```

Your synced folder, the rclone remote, and anything in Drive are left alone.

## Credits

Began as a rewrite of [omarchy-google-drive](https://github.com/wesleycole/omarchy-google-drive)
by Wesley Cole, which takes the FUSE-mount approach, and retains parts of its
QML widget scaffolding. Both are MIT licensed.

## License

MIT — see [LICENSE](LICENSE).
