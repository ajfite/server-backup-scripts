# Server Backup Scripts

A python script set for managing backups on my server.

The repo is ~80% vibe coded, I wouldn't recommend running it directly on your server without an audit, its intended for me.

## Deploying

Install or upgrade the `backup` and `update` commands as root, install the units,
and enable only the timers:

```sh
sudo apt install age
sudo pipx install --force git+https://git.nclf.net/ajfite/server-backup-scripts.git
sudo cp systemd/server-*.service systemd/server-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-backup-vaultwarden.timer server-backup-databases.timer \
    server-backup-weekly.timer server-update.timer
```

Put the age public keys that backups are encrypted to in
`/etc/server-backup/age-recipients.txt`, one per line. Only the public keys go on the
server. Without this file every encrypted backup fails.

```sh
sudo install -d -m 0755 /etc/server-backup
sudo install -m 0644 age-recipients.txt /etc/server-backup/age-recipients.txt
```

Make unattended-upgrades wait for any cleanup, backup or update that is running:

```sh
sudo mkdir -p /etc/systemd/system/apt-daily-upgrade.service.d
sudo cp systemd/apt-daily-upgrade.service.d/wait-for-maintenance.conf \
    /etc/systemd/system/apt-daily-upgrade.service.d/
sudo systemctl daemon-reload
```

## Behaviour

- `backup` exits non-zero if any collector, rotation or export step fails, so the
  failure shows on its service. Missing tools or directories are logged as skips and
  are not failures. Retention per type is in `KEEP_BY_TYPE`.

  | Service | Types | Schedule (Pacific) | Keep |
  | --- | --- | --- | --- |
  | `server-backup-vaultwarden` | Vaultwarden | Daily, 3:30 AM | 30 |
  | `server-backup-databases` | Postgres, MariaDB | Monday and Thursday, 4:00 AM | 5 |
  | `server-backup-weekly` | `/etc`, `/www` | Sunday, 4:00 AM | 4 |
  | `server-backup-weekly` | GitLab | Sunday, 4:00 AM | 5 |
  | `server-backup-vaultwarden` | AMP (export only) | Daily, 3:30 AM | AMP decides |
- A backup type that failed is not rotated that run.
- Postgres, MariaDB, Vaultwarden, `/etc` and `/www` are compressed with zstd, then
  encrypted with age to every key in `/etc/server-backup/age-recipients.txt`, and
  saved as `.sql.zst.age` or `.tar.zst.age`. The server holds no private key, so it
  cannot decrypt them. The compressed stream is checked with `zstd -t` as it is
  written. GitLab and AMP backups are not encrypted. Older unencrypted backups rotate
  out normally, counting toward the same limit.
- `/opt/backup-management` is private to root. `backup-user` reads backups through
  the root-owned export directory.
- GitLab backups are moved out of `/var/opt/gitlab/backups` rather than copied.
- AMP backups are exported but never rotated. Set retention in AMP.
- `update` runs Tuesdays at 3:31 AM Pacific, right after the Vaultwarden backup. It
  runs `apt-get update` and `apt-get upgrade`, then pulls and restarts each
  running compose project under `/opt/docker`. Stopped projects stay stopped. It
  keeps existing config files, never removes packages, and never reboots: check the
  log for a warning that a reboot is required. It exits non-zero if anything failed.

- If a `server-backup-*.service` or `server-update.service` fails, including by timing
  out or being killed, `server-alert@.service` emails the result and that run's log
  to `aj@nclf.net` from `server-alerts@nclf.net`. Successful runs send nothing.
  Alerts go through the local Postfix, so a server that is down, or a timer that
  stops firing, sends nothing.

- `mastodon/clean-mastodon.sh` runs Sunday and Wednesday nights from `mastodon-clean.timer`, as the
  `mastodon` user, and alerts by email if a step fails or it runs out of time. It is
  not installed by the steps above: copy it to `/home/mastodon/`, copy the
  `systemd/mastodon-clean.*` units, and enable the timer.

Do not run the tests as root on a production host.

## Restoring

Decrypt with either private key, then decompress:

```sh
age -d -i ovhalpha-backup-age-key-primary.txt 20261109-120158+0000-postgres.sql.zst.age \
    | zstd -d > postgres.sql
age -d -i ovhalpha-backup-age-key-primary.txt 20261109-113001+0000-vaultwarden.tar.zst.age \
    | zstd -d | tar -x -C restore/
```
