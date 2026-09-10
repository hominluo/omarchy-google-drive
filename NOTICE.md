# Notices

## Derived from omarchy-google-drive

This plugin began as a rewrite of
[omarchy-google-drive](https://github.com/wesleycole/omarchy-google-drive)
by Wesley Cole, which takes the FUSE-mount approach to the same problem.

The rewrite replaced the sync engine entirely — this plugin uses `rclone bisync`
against a selected subset, where the original mounts the whole Drive — but a
substantial amount of the original's Quickshell widget scaffolding survives, in
particular the bar-widget and panel structure, the panel's keyboard-cursor
handling, and several formatting helpers.

Both projects are MIT licensed. Wesley Cole's copyright is retained in
[LICENSE](LICENSE) alongside the current author's, as MIT requires for
substantial portions.

## The Google Drive mark

`GoogleDriveIcon.qml` draws the Google Drive logo from Google's own 2020
artwork, sourced from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Google_Drive_icon_(2020).svg),
where it is recorded as **public domain** for copyright purposes — the mark is
below the threshold of originality — and **trademarked**.

The mark is reproduced here to identify the Google Drive service this plugin
connects to. It is recoloured to follow the user's Omarchy theme rather than
Google's brand palette, so that it sits with the other bar icons; the geometry
is unchanged.

Google Drive is a trademark of Google LLC. This plugin is not affiliated with,
endorsed by, or sponsored by Google.

## Runtime dependencies

The plugin executes but does not bundle:

| Tool | License | Used for |
|---|---|---|
| [rclone](https://rclone.org) | MIT | all syncing and mounting |
| `fusermount3` (fuse3) | LGPL-2.1 | unmounting the browse view |
| `findmnt` (util-linux) | GPL-2.0 | detecting mounts and dead endpoints |
| `nautilus` | GPL-3.0 | the Open buttons |

These are invoked as separate processes, not linked or redistributed, so their
licenses do not extend to this plugin.
