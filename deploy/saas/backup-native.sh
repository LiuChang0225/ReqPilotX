#!/usr/bin/env bash
# Run locally on ECS only after the operator has approved this exact maintenance scope.
# Cold DB+object backup: stop writers, checkpoint both stores, restart prior live services.
set -euo pipefail
[[ ${EUID:-$(id -u)} == 0 ]] || { echo 'Run as the authorized host operator' >&2; exit 2; }
[[ ${1:-} == --approved-maintenance ]] || { echo 'Requires --approved-maintenance' >&2; exit 2; }
root=/var/backups/reqpilotx
state=/var/lib/reqpilotx/application
mkdir -p "$root"
chmod 700 "$root"
[[ -d "$state" && ! -L "$state" ]] || { echo 'State directory is missing or unsafe' >&2; exit 2; }
# Reject symlinks/special files before copying; never traverse unexpected secrets/paths.
[[ -z $(find "$state" -mindepth 1 ! -type d ! -type f -print -quit) ]] || { echo 'Unsafe state file type' >&2; exit 2; }
exec 9>"$root/.backup.lock"
flock -n 9 || { echo 'A backup is already running' >&2; exit 2; }
# Require a conservative local free-space margin; retained data is never auto-deleted.
bytes=$(du -sb "$state" | cut -f1)
free=$(df -PB1 "$root" | awk 'NR==2 {print $4}')
(( free > bytes * 2 + 1073741824 )) || { echo 'Insufficient backup space' >&2; exit 2; }
active=()
for unit in reqpilotx-api.service reqpilotx-worker.service; do
  if systemctl is-active --quiet "$unit"; then active+=("$unit"); fi
done
restore_services() {
  if ((${#active[@]})); then systemctl start "${active[@]}"; fi
}
trap restore_services EXIT
systemctl stop reqpilotx-api.service reqpilotx-worker.service
systemctl is-active --quiet reqpilotx-api.service && exit 2
systemctl is-active --quiet reqpilotx-worker.service && exit 2
snapshot="$root/$(date -u +%Y%m%dT%H%M%SZ)-$$"
staging=$(mktemp -d "$root/.snapshot.XXXXXX")
chmod 700 "$staging"
# root writes the dump file; peer-authenticated postgres only reads the DB.
runuser -u postgres -- pg_dump --format=custom --dbname=reqpilotx > "$staging/database.dump"
pg_restore --list "$staging/database.dump" > "$staging/database.toc"
tar --one-file-system -C "$state" -czf "$staging/application.tar.gz" .
tar -tzf "$staging/application.tar.gz" >/dev/null
readlink -f /opt/reqpilotx/current > "$staging/release-path.txt"
cp /opt/reqpilotx/current/release.json "$staging/release.json"
(cd "$staging" && sha256sum database.dump application.tar.gz release.json > SHA256SUMS)
chmod -R go-rwx "$staging"
mv "$staging" "$snapshot"
echo "Consistent local backup created: $snapshot"
# No restore, retention deletion or remote backup upload is implicit in this command.
