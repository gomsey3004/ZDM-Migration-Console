#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
out=${1:-"$root/portable/dist/zdm-gui-linux-x86_64.run"}
mkdir -p "$(dirname "$out")"
command -v pnpm >/dev/null || { echo 'pnpm and Node.js are needed only on the build computer.' >&2; exit 1; }
(cd "$root/frontend" && pnpm build)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/release/backend" "$tmp/release/frontend" "$tmp/release/deploy"
cp -a "$root/backend/." "$tmp/release/backend/"
cp -a "$root/frontend/dist" "$tmp/release/frontend/dist"
cp -a "$root/deploy/oci-requirements.txt" "$tmp/release/deploy/oci-requirements.txt"
cp -a "$root/portable/install.sh" "$tmp/release/install.sh"
cp -a "$root/portable/user-install.py" "$tmp/release/user-install.py"
cp -a "$root/portable/user-runtime.py" "$tmp/release/deploy/user-runtime.py"
cp -a "$root/portable/README.md" "$tmp/release/README.md"
chmod 0755 "$tmp/release/install.sh"
tar -czf "$tmp/payload.tar.gz" -C "$tmp/release" .
cat > "$tmp/launcher" <<'LAUNCHER'
#!/usr/bin/env bash
set -Eeuo pipefail
self=$(readlink -f -- "$0")
tmp=$(mktemp -d "${TMPDIR:-/tmp}/zdm-gui-install.XXXXXXXX")
trap 'rm -rf "$tmp"' EXIT
line=$(awk '/^__ZDM_GUI_PAYLOAD__$/ { print NR + 1; exit }' "$self")
[[ -n "$line" ]] || { echo 'Installer archive is damaged.' >&2; exit 1; }
tail -n +"$line" "$self" | tar -xz -C "$tmp"
"$tmp/install.sh" "$@"
exit 0
LAUNCHER
chmod 0755 "$tmp/launcher"
cat "$tmp/launcher" > "$out"
printf '\n__ZDM_GUI_PAYLOAD__\n' >> "$out"
cat "$tmp/payload.tar.gz" >> "$out"
chmod 0755 "$out"
printf 'Created executable installer: %s\n' "$out"
uninstaller="$(dirname "$out")/zdm-gui-uninstall.run"
cp "$root/portable/uninstall.sh" "$uninstaller"
chmod 0755 "$uninstaller"
printf 'Created standalone uninstaller: %s\n' "$uninstaller"
