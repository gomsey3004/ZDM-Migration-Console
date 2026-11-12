#!/bin/bash
set -euo pipefail
setup=/var/lib/zdm-https-setup
test -s /etc/letsencrypt/live/zdm-ip/fullchain.pem
/usr/bin/python3.11 - <<'CHECK'
import sqlite3
p='/u01/app/zdm-gui/data/console.sqlite'
c=sqlite3.connect(p)
pending=c.execute("SELECT count(*) FROM operations WHERE state IN ('QUEUED','RUNNING')").fetchone()[0]
if pending:raise SystemExit('Pending CLI submissions; do not restart the GUI yet.')
b=sqlite3.connect('/var/lib/zdm-https-setup/console.sqlite.before-https');c.backup(b);b.close();c.close()
print('No pending submissions; GUI database backed up.')
CHECK
chmod 0600 "$setup/console.sqlite.before-https"
install -m 0644 "$setup/https/nginx.conf" /etc/nginx/nginx.conf
restorecon -RF /etc/nginx
nginx -t
setsebool -P httpd_can_network_relay on
firewall-cmd --permanent --add-service=https
firewall-cmd --add-service=https
install -m 0644 "$setup/zdm-gui.service" /etc/systemd/system/zdm-gui.service
systemctl daemon-reload
systemctl restart zdm-gui
systemctl reload nginx
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$setup/https/reload-nginx" /etc/letsencrypt/renewal-hooks/deploy/zdm-nginx
install -m 0644 "$setup/https/zdm-certbot-renew.service" /etc/systemd/system/zdm-certbot-renew.service
install -m 0644 "$setup/https/zdm-certbot-renew.timer" /etc/systemd/system/zdm-certbot-renew.timer
systemctl daemon-reload
systemctl enable --now zdm-certbot-renew.timer
systemctl is-active nginx zdm-gui
curl --silent --show-error --output /dev/null --write-out 'Loopback HTTPS response: %{http_code}\n' --resolve 144.24.37.222:443:127.0.0.1 https://144.24.37.222/
