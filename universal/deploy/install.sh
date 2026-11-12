#!/bin/bash
set -euo pipefail
# Run as root on the ZDM host after copying the release and environment file.
release=${1:?release tar.gz path required}
environment=${2:?environment file path required}
app=/u01/app/zdm-gui
install -d -o zdmuser -g zdm -m 0750 "$app"
tar -xzf "$release" -C "$app"
install -d -o zdmuser -g zdm -m 0700 "$app/data" /u01/app/zdmstage/gui-responses
if [ ! -x "$app/.venv/bin/python" ]; then
  sudo -u zdmuser python3.11 -m venv "$app/.venv"
fi
sudo -u zdmuser "$app/.venv/bin/pip" install -r "$app/backend/requirements.txt"
if [ ! -x "$app/.oci-venv/bin/python" ]; then
  sudo -u zdmuser python3.11 -m venv "$app/.oci-venv"
fi
sudo -u zdmuser "$app/.oci-venv/bin/pip" install -r "$app/deploy/oci-requirements.txt"
if [ ! -e /usr/local/bin/oci ]; then
  install -o root -g root -m 0755 "$app/deploy/oci-wrapper" /usr/local/bin/oci
fi
install -o root -g root -m 0600 "$environment" /etc/zdm-gui.env
install -o root -g root -m 0644 "$app/deploy/zdm-gui.service" /etc/systemd/system/zdm-gui.service
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$app/backend/host_records.py" /usr/local/libexec/zdm-hosts-manager.py
install -o root -g root -m 0644 "$app/deploy/zdm-hosts-manager.service" /etc/systemd/system/zdm-hosts-manager.service
install -o root -g root -m 0755 "$app/deploy/zdm-server-boot" /usr/local/libexec/zdm-server-boot
install -o root -g root -m 0644 "$app/deploy/zdm-server.service" /etc/systemd/system/zdm-server.service
systemctl daemon-reload
systemctl enable --now zdm-server
systemctl enable --now zdm-hosts-manager
systemctl enable zdm-gui
systemctl restart zdm-gui
systemctl --no-pager --full status zdm-gui
