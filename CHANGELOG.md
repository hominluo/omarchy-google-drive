# Changelog

## 1.0.2 — 2026-09-18

Security hardening after the second marketplace review.

- rclone is run through one bounded runner: its own session (process
  group), a deadline, and a byte cap on each pipe. A child that overruns any
  of them is killed with its group and reported with no output at all; a
  watchdog SIGTERM from the widget kills the running rclone too, and every
  child carries `PR_SET_PDEATHSIG` as a backstop.
- The Drive folder listing (`rclone lsjson`) is streamed and parsed one
  entry at a time under an 8 MiB transfer cap, a 5000-entry cap and a
  255-byte name cap. Past any cap there is no listing, never a partial one.
  The loose-file count uses `rclone size` with the sync's own filter rules,
  which answers with one small object however many files there are.
- Folder names are validated once (`valid_drive_name`) everywhere they
  enter: the listing, `select --add`, and `selection.json` itself. A name
  with a newline could previously add a rule to the rclone filters file.
  The widget passes `--add=NAME`, so a folder called `-Something` is a
  folder, not an option.
- `rclone check` in cleanup runs under a stderr cap; cleanup refusals now
  reach the panel.
- `sync.log` and `mount.log` roll over at 2 MiB; tails are read by seeking,
  never by reading the whole file. Whether a run needs `--resync` is
  decided by bisync's exit code plus rclone's own timestamped ERROR line,
  not by a substring a remote file name could contain.
- The panel renders every string as plain text (a folder called
  `<img src=…>` no longer fetches anything); the on-disk size walk has an
  entry and time budget and is shown as `≈` when cut short; `rclone about`
  is asked at most once a minute.
- Names may start or end with whitespace (Drive allows it and the filter
  rule carries it); only control characters, `/`, `.` and `..` are refused.
  Each top-level folder gets its own walk budget under one clock, and the
  panel keys the "still on disk" notice on a folder count as well as bytes,
  so one huge folder cannot hide another. A SIGTERM to the helper forwards
  SIGTERM to rclone and waits before killing, so a `systemctl stop` lets
  bisync drop its lock. Resync detection recognises rclone's critical-error
  line with and without `--resilient`.
- Unit files escape `%` and `$`; paths with control characters are
  refused. The mount point and the sync root must be real directories of
  yours (a symlinked sync root is refused); the browse mount caps its VFS
  cache at 2 GB and logs at NOTICE level. The state directory and every
  file in it are tightened to 0700/0600 on each run.

## 1.0.1 — 2026-09-17

State writes and cleanup made race-resistant: unpredictable
`O_CREAT|O_EXCL|O_NOFOLLOW` temps, descriptor-relative renames, and cleanup
that verifies and removes one directory identity.

## 1.0.0 — 2026-09-09

First release.
