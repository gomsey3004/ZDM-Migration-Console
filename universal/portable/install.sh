#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
prompt(){ local label=$1 default=$2 value; read -r -p "$label [$default]: " value; REPLY=${value:-$default}; }
confirm(){ local label=$1 answer; read -r -p "$label [Y/n]: " answer; [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; }
confirm_default_no(){ local label=$1 answer; read -r -p "$label [y/N]: " answer; [[ "$answer" =~ ^[Yy] ]]; }
valid_path(){ [[ "$1" =~ ^/[A-Za-z0-9_./-]+$ ]]; }
python_version_ok(){ "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)' >/dev/null 2>&1; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  cat <<'HELP'
ZDM Migration Console installer
Run as root on an existing Oracle ZDM 21.4, 21.5, or 26.1 Linux x86-64 service host.
Or run as the ZDM owner without sudo: ./zdm-gui-linux-x86_64.run --user
User mode uses private directories and HTTPS on port 8443, without system changes.
The installer does not install or upgrade Oracle ZDM. It can optionally configure a separate systemd unit to start the installed ZDM service at boot.
HELP
  exit 0
fi
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ${1:-} == --user || $EUID -ne 0 ]]; then
  [[ ${1:-} != --user ]] || shift
  for candidate in python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then
      exec "$candidate" "$SCRIPT_DIR/user-install.py" "$@"
    fi
  done
  fail 'User installation requires Python 3.9+ with venv/SSL/SQLite for the bundled packages. Ask an administrator to install the runtime (OL8: python39).'
fi
[[ $# -eq 0 ]] || fail "Unknown installer option: $1"
[[ $(uname -s) == Linux ]] || fail 'This installer supports Linux only.'
[[ $(uname -m) == x86_64 ]] || fail 'Supported ZDM service hosts and this release require x86_64.'
[[ $EUID -eq 0 ]] || fail 'Run this installer as root, for example: sudo ./install.sh'
[[ -t 0 ]] || fail 'Run this installer from an interactive terminal so it can prompt for installation paths and HTTPS settings.'
command -v systemctl >/dev/null || fail 'systemd is required.'

[[ -r /etc/os-release ]] || fail 'Cannot identify this Linux distribution.'
. /etc/os-release
OS_MAJOR=${VERSION_ID%%.*}
case "${ID:-}:$OS_MAJOR" in
  ol:7|ol:8|ol:9|rhel:8|rhel:9) ;;
  *) fail "Unsupported distribution: ${PRETTY_NAME:-unknown}. Use Oracle Linux 7/8/9 or RHEL 8/9 on x86_64." ;;
esac

APP_DEFAULT=/opt/zdm-gui
APP_DIR=${ZDM_GUI_INSTALL_DIR:-$APP_DEFAULT}
if [[ -f "$APP_DIR/data/console.sqlite" ]]; then DATA_DEFAULT="$APP_DIR/data"; else DATA_DEFAULT=/var/lib/zdm-gui; fi
prompt 'GUI application directory' "$APP_DIR"; APP_DIR=$REPLY
valid_path "$APP_DIR" || fail 'Use an absolute application directory without quotes or percent signs.'
prompt 'GUI data directory (profiles, users, and audit history)' "$DATA_DEFAULT"; DATA_DIR=$REPLY
valid_path "$DATA_DIR" || fail 'Use an absolute data directory without quotes or percent signs.'

ZDM_HOME_DEFAULT=/u01/app/zdmhome; [[ -x "$ZDM_HOME_DEFAULT/bin/zdmcli" ]] || ZDM_HOME_DEFAULT=''
prompt 'ZDM home directory' "${ZDM_HOME_DEFAULT:-/u01/app/zdmhome}"; ZDM_HOME=$REPLY
valid_path "$ZDM_HOME" && [[ -x "$ZDM_HOME/bin/zdmcli" && -x "$ZDM_HOME/bin/zdmservice" ]] || fail 'ZDM_HOME must contain executable bin/zdmcli and bin/zdmservice.'
ZDM_BASE_DEFAULT=/u01/app/zdmbase; [[ -d "$ZDM_BASE_DEFAULT" ]] || ZDM_BASE_DEFAULT=$(dirname "$ZDM_HOME")/zdmbase
prompt 'ZDM base directory' "$ZDM_BASE_DEFAULT"; ZDM_BASE=$REPLY
valid_path "$ZDM_BASE" && [[ -d "$ZDM_BASE" ]] || fail 'ZDM_BASE must be an existing absolute directory.'
ZDM_STAGE_DEFAULT=/u01/app/zdmstage; [[ -d "$ZDM_STAGE_DEFAULT" ]] || ZDM_STAGE_DEFAULT=$(dirname "$ZDM_HOME")/zdmstage
prompt 'ZDM stage directory' "$ZDM_STAGE_DEFAULT"; ZDM_STAGE=$REPLY
valid_path "$ZDM_STAGE" && [[ -d "$ZDM_STAGE" ]] || fail 'ZDM_STAGE must be an existing absolute directory.'

ZDM_USER_DEFAULT=$(stat -c '%U' "$ZDM_HOME")
[[ "$ZDM_USER_DEFAULT" != root ]] || ZDM_USER_DEFAULT=zdmuser
getent passwd "$ZDM_USER_DEFAULT" >/dev/null || ZDM_USER_DEFAULT=''
prompt 'Operating-system account that owns/runs ZDM' "${ZDM_USER_DEFAULT:-zdmuser}"; ZDM_USER=$REPLY
id "$ZDM_USER" >/dev/null 2>&1 || fail "Linux account '$ZDM_USER' does not exist. Create/use the existing ZDM service account first."
ZDM_HOME_OS=$(getent passwd "$ZDM_USER" | cut -d: -f6)
[[ -n "$ZDM_HOME_OS" && -d "$ZDM_HOME_OS" ]] || fail "Cannot locate the home directory for '$ZDM_USER'."
ZDM_GROUP=$(id -gn "$ZDM_USER")
[[ $(stat -c '%U' "$ZDM_HOME") == "$ZDM_USER" ]] || printf 'Note: %s does not own %s; ensure it can run the installed ZDM tools and access the configured directories.\n' "$ZDM_USER" "$ZDM_HOME"
if command -v runuser >/dev/null 2>&1; then
  ZDM_BUILD=$(runuser -u "$ZDM_USER" -- "$ZDM_HOME/bin/zdmcli" -build 2>&1) || fail 'Could not query the installed ZDM build.'
else
  ZDM_BUILD=$(sudo -u "$ZDM_USER" -- "$ZDM_HOME/bin/zdmcli" -build 2>&1) || fail 'Could not query the installed ZDM build.'
fi
if grep -Eq '^[[:space:]]*full version[[:space:]]*:[[:space:]]*"?21\.4([."[:space:]]|$)' <<<"$ZDM_BUILD"; then ZDM_RELEASE=21.4
elif grep -Eq '^[[:space:]]*full version[[:space:]]*:[[:space:]]*"?21\.5([."[:space:]]|$)' <<<"$ZDM_BUILD"; then ZDM_RELEASE=21.5
elif grep -Eq '^[[:space:]]*full version[[:space:]]*:[[:space:]]*"?26\.1([."[:space:]]|$)' <<<"$ZDM_BUILD"; then ZDM_RELEASE=26.1
else fail 'This installer supports Oracle ZDM 21.4, 21.5, and 26.1 only.'; fi
if [[ "$ZDM_RELEASE" != 26.1 && "$OS_MAJOR" == 9 ]]; then
  fail "Oracle ZDM $ZDM_RELEASE service hosts do not support ${PRETTY_NAME:-this OS}; use Oracle Linux 7/8 or RHEL 8."
fi
printf 'Detected Oracle ZDM %s. Runtime command capabilities will be probed after startup.\n' "$ZDM_RELEASE"
AUTO_START_ZDM=0
if confirm_default_no 'Automatically start zdmservice after each server reboot'; then AUTO_START_ZDM=1; fi

TLS_NAME_DEFAULT=$(hostname -f 2>/dev/null || hostname)
prompt 'HTTPS certificate name (the hostname or IP users will enter in their browser)' "$TLS_NAME_DEFAULT"; TLS_NAME=$REPLY
[[ "$TLS_NAME" =~ ^[A-Za-z0-9.:-]+$ ]] || fail 'Use a hostname or IP address containing only letters, digits, dots, colons, and hyphens.'
prompt 'Allowed browser client IPs/CIDRs, comma-separated (0.0.0.0/0,::/0 allows all clients)' '127.0.0.1/32,::1/128'; ALLOWED_CLIENTS=$REPLY

PYTHON=''
if [[ "$ID" == ol && "$OS_MAJOR" == 7 ]]; then
  PYTHON=/opt/rh/rh-python38/root/usr/bin/python3.8
  command -v nginx >/dev/null 2>&1 || :
  PKGS=(gcc openssl-devel libffi-devel glibc-devel expect unzip libaio sqlite nginx oraclelinux-developer-release-el7 oracle-softwarecollection-release-el7 scl-utils rh-python38 rh-python38-python-devel)
  if ! rpm -q oracle-epel-release-el7 >/dev/null 2>&1 && ! rpm -q nginx >/dev/null 2>&1; then PKGS+=(oracle-epel-release-el7); fi
elif [[ "$OS_MAJOR" == 8 ]]; then
  for candidate in python3.11 python3.10 python3.9 python3.8 python38; do
    if command -v "$candidate" >/dev/null 2>&1 && python_version_ok "$candidate"; then PYTHON=$(command -v "$candidate"); break; fi
  done
  PKGS=(gcc openssl-devel libffi-devel glibc-devel expect unzip libaio libnsl sqlite nginx)
  [[ "$ID" != ol ]] || PKGS+=(oraclelinux-developer-release-el8 ncurses-compat-libs)
  if [[ -z "$PYTHON" ]]; then PKGS+=(python39 python39-devel); else
    PYTHON_MINOR_CHECK=$($PYTHON -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
    case "$PYTHON_MINOR_CHECK" in 3.11) PKGS+=(python3.11-devel);; 3.10) PKGS+=(python3.10-devel);; 3.9) PKGS+=(python39-devel);; 3.8) PKGS+=(python38-devel);; esac
  fi
else
  for candidate in python3.11 python3.10 python3.9 python3.8 python38 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_version_ok "$candidate"; then PYTHON=$(command -v "$candidate"); break; fi
  done
  PKGS=(gcc openssl-devel libffi-devel glibc-devel expect unzip libaio libnsl sqlite nginx)
  if [[ -z "$PYTHON" ]]; then PKGS+=(python3 python3-devel); else
    PYTHON_MINOR_CHECK=$($PYTHON -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
    case "$PYTHON_MINOR_CHECK" in 3.11) PKGS+=(python3.11-devel);; 3.10) PKGS+=(python3.10-devel);; 3.8) PKGS+=(python38-devel);; *) PKGS+=(python3-devel);; esac
  fi
fi
printf '\nLinux packages required by the GUI/ZDM host:\n  %s\n' "${PKGS[*]}"
printf 'Python application packages (installed in isolated environments):\n  FastAPI, Uvicorn, HTTPX, Cryptography, Pexpect, OCI CLI\n'
printf 'The bundled web interface is prebuilt; Node.js and pnpm are not required on this server.\n\n'

missing=()
for package in "${PKGS[@]}"; do rpm -q "$package" >/dev/null 2>&1 || missing+=("$package"); done
if ((${#missing[@]})); then
  manager=$(command -v dnf || command -v yum || true)
  [[ -n "$manager" ]] || fail 'Install the missing RPM packages above using dnf/yum, then rerun the installer.'
  printf 'Missing packages: %s\n' "${missing[*]}"
  confirm "Install these packages with $(basename "$manager")" || fail 'Install the listed system packages and rerun this installer.'
  if [[ "$ID" == ol && "$OS_MAJOR" == 7 ]]; then
    repo_pkgs=()
    for package in oraclelinux-developer-release-el7 oracle-softwarecollection-release-el7 oracle-epel-release-el7; do
      for item in "${missing[@]}"; do [[ "$item" != "$package" ]] || repo_pkgs+=("$package"); done
    done
    if ((${#repo_pkgs[@]})); then "$manager" install -y "${repo_pkgs[@]}"; "$manager" clean all; fi
  elif [[ "$ID" == ol && "$OS_MAJOR" == 8 ]] && [[ " ${missing[*]} " == *" oraclelinux-developer-release-el8 "* ]]; then
    "$manager" install -y oraclelinux-developer-release-el8
    "$manager" clean all
  fi
  "$manager" install -y "${missing[@]}"
fi

PYTHON=''
if [[ "$ID" == ol && "$OS_MAJOR" == 7 ]]; then
  PYTHON=/opt/rh/rh-python38/root/usr/bin/python3.8
else
  PYTHON=''
  for candidate in python3.11 python3.10 python3.9 python3.8 python38 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_version_ok "$candidate"; then PYTHON=$(command -v "$candidate"); break; fi
  done
fi
[[ -x "$PYTHON" ]] && python_version_ok "$PYTHON" || fail 'Python 3.8 or newer is required. Install the runtime and matching development headers for this supported OS, then rerun.'
"$PYTHON" -c 'import venv,ssl,sqlite3; print("Python",__import__("sys").version.split()[0],"is ready")' || fail 'Python venv, SSL, and SQLite modules are required.'
command -v rpm >/dev/null || fail 'The RPM package database tool is required for supported Oracle Linux/RHEL systems.'
TLS_SAN=$($PYTHON - "$TLS_NAME" <<'PY'
import ipaddress,sys
name=sys.argv[1]
try: print('IP:'+str(ipaddress.ip_address(name)))
except ValueError:
    if len(name)>253 or any(not part or len(part)>63 or part.startswith('-') or part.endswith('-') for part in name.rstrip('.').split('.')): raise SystemExit(1)
    print('DNS:'+name)
PY
) || fail 'Enter a valid DNS hostname or IP address for the HTTPS certificate.'
ALLOWED_CLIENTS=$($PYTHON - "$ALLOWED_CLIENTS" <<'PY'
import ipaddress,sys
try:
    values=[ipaddress.ip_network(item.strip(),strict=False) for item in sys.argv[1].split(',') if item.strip()]
    if not values: raise ValueError('empty')
    print(','.join(map(str,values)))
except ValueError: raise SystemExit('Enter valid IP addresses or CIDRs separated by commas.')
PY
) || fail "$ALLOWED_CLIENTS"

if [[ -f "$DATA_DIR/console.sqlite" ]] && command -v sqlite3 >/dev/null; then
  pending=$(sqlite3 "$DATA_DIR/console.sqlite" "select count(*) from operations where state in ('QUEUED','RUNNING');" 2>/dev/null || echo 0)
  [[ "$pending" == 0 ]] || fail 'A GUI operation is queued or running. Wait for it to finish before upgrading.'
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ -d "$SCRIPT_DIR/backend" && -d "$SCRIPT_DIR/frontend/dist" && -f "$SCRIPT_DIR/deploy/oci-requirements.txt" ]] || fail 'Release files are incomplete. Use the packaged executable or build the release with portable/build-linux.sh.'
mkdir -p "$APP_DIR" "$DATA_DIR" "$ZDM_STAGE/gui-responses"
chmod 700 "$DATA_DIR"
chown "$ZDM_USER:$ZDM_GROUP" "$DATA_DIR" "$ZDM_STAGE/gui-responses"

DB_EXISTS=0
if [[ -f "$DATA_DIR/console.sqlite" ]] && command -v sqlite3 >/dev/null; then
  DB_EXISTS=$(sqlite3 "$DATA_DIR/console.sqlite" "select count(*) from sqlite_master where type='table' and name='console_account';" 2>/dev/null || echo 0)
fi
BOOTSTRAP_PASSWORD=''
GENERATED_PASSWORD=''
if [[ "$DB_EXISTS" == 0 ]]; then
  GENERATED_PASSWORD=$($PYTHON -c 'import secrets;print(secrets.token_urlsafe(24))')
  BOOTSTRAP_PASSWORD=$GENERATED_PASSWORD
  printf 'A temporary zdmadmin password will be generated and shown once after installation. The first sign-in will require a password change.\n'
else
  printf 'An existing GUI database was found. Its accounts and password hashes will be preserved; use the GUI password-change option if needed.\n'
fi

systemctl stop zdm-gui 2>/dev/null || true
if [[ -d "$APP_DIR/backend" ]]; then
  backup="/var/backups/zdm-gui/$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 "$backup"
  cp -a "$APP_DIR/backend" "$backup/backend"
  [[ ! -d "$APP_DIR/frontend/dist" ]] || cp -a "$APP_DIR/frontend/dist" "$backup/dist"
  [[ ! -f "$DATA_DIR/console.sqlite" ]] || cp -a "$DATA_DIR/console.sqlite" "$backup/console.sqlite"
  printf 'Existing GUI files and data snapshot saved under %s\n' "$backup"
fi
install -d -m 0755 "$APP_DIR/backend" "$APP_DIR/frontend/dist" "$APP_DIR/deploy"
cp -a "$SCRIPT_DIR/backend/." "$APP_DIR/backend/"
cp -a "$SCRIPT_DIR/frontend/dist/." "$APP_DIR/frontend/dist/"
install -m 0644 "$SCRIPT_DIR/deploy/oci-requirements.txt" "$APP_DIR/deploy/oci-requirements.txt"
chown -R root:root "$APP_DIR"
chmod -R a+rX,go-w "$APP_DIR"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then "$PYTHON" -m venv "$APP_DIR/.venv"; fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade 'pip<25.1'
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/backend/requirements.txt"
if [[ ! -x "$APP_DIR/.oci-venv/bin/python" ]]; then "$PYTHON" -m venv "$APP_DIR/.oci-venv"; fi
"$APP_DIR/.oci-venv/bin/python" -m pip install --upgrade 'pip<25.1'
"$APP_DIR/.oci-venv/bin/python" -m pip install -r "$APP_DIR/deploy/oci-requirements.txt"
# The installer runs as root with umask 077, while both services run as the
# ZDM OS account. Keep package files root-owned and non-writable, but allow the
# service account to traverse the venvs and execute their interpreters/tools.
chmod -R a+rX,go-w "$APP_DIR/.venv" "$APP_DIR/.oci-venv"
PYTHON_MINOR=$($PYTHON -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
OCI_SITE="$APP_DIR/.oci-venv/lib/python$PYTHON_MINOR/site-packages"

cat > /etc/zdm-gui.env <<EOF
ZDM_HOME=$ZDM_HOME
ZDM_BASE=$ZDM_BASE
ZDM_STAGE=$ZDM_STAGE
ZDM_OS_HOME=$ZDM_HOME_OS
ZDM_GUI_DATA=$DATA_DIR
ZDM_GUI_USERNAME=zdmadmin
ZDM_GUI_SERVICE_USER=$ZDM_USER
ZDM_GUI_ALLOWED_IPS=$ALLOWED_CLIENTS
ZDM_OCI_PYTHON=$APP_DIR/.oci-venv/bin/python
ZDM_OCI_SITE=$OCI_SITE
EOF
chown root:root /etc/zdm-gui.env; chmod 0600 /etc/zdm-gui.env

install -d -m 0755 /usr/local/libexec
install -m 0755 "$APP_DIR/backend/host_records.py" /usr/local/libexec/zdm-hosts-manager.py
cat > /etc/systemd/system/zdm-hosts-manager.service <<EOF
[Unit]
Description=ZDM GUI managed hostname entries
After=local-fs.target
[Service]
Type=simple
User=root
Group=$ZDM_GROUP
EnvironmentFile=/etc/zdm-gui.env
ExecStart=$APP_DIR/.venv/bin/python -I /usr/local/libexec/zdm-hosts-manager.py
UMask=0077
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=$([[ "$OS_MAJOR" == 7 ]] && printf full || printf strict)
ReadWritePaths=/etc /var/lib/zdm-hosts-manager /run/zdm-hosts-manager
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF

if [[ "$OS_MAJOR" != 7 ]]; then
  sed -i '/^ReadWritePaths=/a ProtectHome=true\nProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\nRestrictAddressFamilies=AF_UNIX\nRestrictSUIDSGID=true' /etc/systemd/system/zdm-hosts-manager.service
fi
install -d -m 0750 -o root -g "$ZDM_GROUP" /run/zdm-hosts-manager /var/lib/zdm-hosts-manager
if command -v systemd-tmpfiles >/dev/null 2>&1; then
  cat > /etc/tmpfiles.d/zdm-hosts-manager.conf <<EOF
d /run/zdm-hosts-manager 0750 root $ZDM_GROUP -
EOF
fi

cat > /etc/systemd/system/zdm-gui.service <<EOF
[Unit]
Description=ZDM Migration Console
After=network-online.target zdm-hosts-manager.service
Wants=network-online.target zdm-hosts-manager.service
[Service]
Type=simple
User=$ZDM_USER
Group=$ZDM_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/zdm-gui.env
EnvironmentFile=-/run/zdm-gui-bootstrap.env
Environment=HOME=$ZDM_HOME_OS
ExecStart=$APP_DIR/.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8080 --proxy-headers --forwarded-allow-ips=127.0.0.1 --workers 1
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=$([[ "$OS_MAJOR" == 7 ]] && printf full || printf strict)
ReadWritePaths=$DATA_DIR $ZDM_HOME $ZDM_BASE $ZDM_STAGE $ZDM_HOME_OS
[Install]
WantedBy=multi-user.target
EOF

if [[ "$OS_MAJOR" != 7 ]]; then
  sed -i '/^ReadWritePaths=/a ProtectHome=read-only\nProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\nRestrictSUIDSGID=true' /etc/systemd/system/zdm-gui.service
fi

ZDM_AUTOSTART_UNIT=/etc/systemd/system/zdmservice-autostart.service
if [[ "$AUTO_START_ZDM" == 1 ]]; then
  cat > "$ZDM_AUTOSTART_UNIT" <<EOF
[Unit]
Description=Oracle Zero Downtime Migration service
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=$ZDM_USER
Group=$ZDM_GROUP
WorkingDirectory=$ZDM_HOME
Environment=HOME=$ZDM_HOME_OS
ExecStart=/bin/sh $ZDM_HOME/bin/zdmservice start
ExecStop=/bin/sh $ZDM_HOME/bin/zdmservice stop
RemainAfterExit=yes
TimeoutStartSec=300
TimeoutStopSec=300
UMask=0027
[Install]
WantedBy=multi-user.target
EOF
else
  systemctl disable zdmservice-autostart.service >/dev/null 2>&1 || true
  rm -f "$ZDM_AUTOSTART_UNIT"
fi

if [[ -n "$BOOTSTRAP_PASSWORD" ]]; then
  escaped=${BOOTSTRAP_PASSWORD//\\/\\\\}; escaped=${escaped//\"/\\\"}
  printf 'ZDM_GUI_PASSWORD="%s"\n' "$escaped" > /run/zdm-gui-bootstrap.env
  chmod 0600 /run/zdm-gui-bootstrap.env
  unset BOOTSTRAP_PASSWORD escaped
fi
if command -v restorecon >/dev/null; then restorecon -F /etc/zdm-gui.env /etc/systemd/system/zdm-gui.service /etc/systemd/system/zdm-hosts-manager.service "$ZDM_AUTOSTART_UNIT" "$APP_DIR" "$DATA_DIR" || true; fi
systemctl daemon-reload
systemctl enable --now zdm-hosts-manager.service
systemctl enable --now zdm-gui.service
if [[ "$AUTO_START_ZDM" == 1 ]]; then systemctl enable zdmservice-autostart.service; fi

# HTTPS is mandatory for browser access. The application remains loopback-only;
# nginx presents a locally generated certificate and forwards trusted headers.
TLS_DIR=/etc/zdm-gui/tls
CERT_FILE=/etc/pki/tls/certs/zdm-gui.crt
KEY_FILE=/etc/pki/tls/private/zdm-gui.key
install -d -m 0755 /etc/zdm-gui "$TLS_DIR" /etc/nginx/conf.d /etc/pki/tls/certs
install -d -m 0700 /etc/pki/tls/private
if [[ "$TLS_SAN" == IP:* ]]; then ALT_TYPE=IP; ALT_VALUE=${TLS_SAN#IP:}; else ALT_TYPE=DNS; ALT_VALUE=${TLS_SAN#DNS:}; fi
cat > "$TLS_DIR/openssl.cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no
[req_distinguished_name]
CN = $TLS_NAME
[v3_req]
subjectAltName = @alt_names
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[alt_names]
EOF
if [[ "$ALT_TYPE" == DNS ]]; then
  printf 'DNS.1 = %s\nIP.1 = 127.0.0.1\nIP.2 = ::1\n' "$ALT_VALUE" >> "$TLS_DIR/openssl.cnf"
elif [[ "$ALT_VALUE" == 127.0.0.1 ]]; then
  printf 'IP.1 = 127.0.0.1\nIP.2 = ::1\n' >> "$TLS_DIR/openssl.cnf"
elif [[ "$ALT_VALUE" == ::1 ]]; then
  printf 'IP.1 = ::1\nIP.2 = 127.0.0.1\n' >> "$TLS_DIR/openssl.cnf"
else
  printf 'IP.1 = %s\nIP.2 = 127.0.0.1\nIP.3 = ::1\n' "$ALT_VALUE" >> "$TLS_DIR/openssl.cnf"
fi
openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 3650 -keyout "$KEY_FILE" -out "$CERT_FILE" -config "$TLS_DIR/openssl.cnf" -extensions v3_req >/dev/null 2>&1 || fail 'Could not create the self-signed HTTPS certificate.'
chmod 0600 "$KEY_FILE" "$TLS_DIR/openssl.cnf"
chmod 0644 "$CERT_FILE"
ALLOW_RULES=''
IFS=, read -r -a client_networks <<< "$ALLOWED_CLIENTS"
for network in "${client_networks[@]}"; do ALLOW_RULES+="        allow $network;"$'\n'; done
cat > /etc/nginx/conf.d/zdm-gui.conf <<EOF
server {
    listen 443 ssl;
    server_name $TLS_NAME;
    ssl_certificate $CERT_FILE;
    ssl_certificate_key $KEY_FILE;
    ssl_protocols TLSv1.2;
    ssl_session_cache shared:ZDM_GUI_SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    server_tokens off;
    client_max_body_size 400k;
$ALLOW_RULES    deny all;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header Connection "";
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
EOF
if command -v getenforce >/dev/null && [[ $(getenforce) == Enforcing ]] && command -v setsebool >/dev/null; then setsebool -P httpd_can_network_connect 1; fi
if command -v restorecon >/dev/null; then restorecon -RF /etc/nginx /etc/zdm-gui; restorecon -F "$CERT_FILE" "$KEY_FILE" || true; fi
nginx -t || fail 'NGINX configuration check failed; review /etc/nginx/conf.d/zdm-gui.conf.'
systemctl enable --now nginx
systemctl reload nginx
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  if confirm 'Allow HTTPS (TCP 443) through the active firewalld zone'; then
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
  else
    printf 'firewalld was left unchanged; allow TCP 443 in the host firewall before accessing the GUI remotely.\n'
  fi
fi

if [[ -f /run/zdm-gui-bootstrap.env ]]; then
  initialized=0
  for _ in $(seq 1 30); do
    if [[ -f "$DATA_DIR/console.sqlite" ]] && command -v sqlite3 >/dev/null && [[ $(sqlite3 "$DATA_DIR/console.sqlite" "select count(*) from console_users where username='zdmadmin';" 2>/dev/null || echo 0) == 1 ]]; then initialized=1; break; fi
    sleep 2
  done
  rm -f /run/zdm-gui-bootstrap.env
  [[ $initialized == 1 ]] || { systemctl status zdm-gui --no-pager || true; fail 'The initial zdmadmin account was not created. Review journalctl -u zdm-gui and rerun after correcting the issue.'; }
  systemctl restart zdm-gui
fi
systemctl is-active --quiet zdm-gui || { systemctl status zdm-gui --no-pager || true; fail 'The GUI service did not start. Review journalctl -u zdm-gui.'; }

printf '\nInstallation complete.\nGUI service: zdm-gui (enabled at boot)\nHTTPS proxy: nginx (enabled at boot)\nZDM CLI: %s/bin/zdmcli\nOS account: %s\nData: %s\n' "$ZDM_HOME" "$ZDM_USER" "$DATA_DIR"
printf 'Browse to https://%s (the certificate is self-signed and must be trusted on each client).\n' "$TLS_NAME"
if [[ "$AUTO_START_ZDM" == 1 ]]; then
  printf 'ZDM service auto-start: enabled for each server boot (zdmservice-autostart.service).\n'
else
  printf 'ZDM service auto-start: not configured.\n'
fi
if [[ -n "$GENERATED_PASSWORD" ]]; then
  printf '\nTemporary first-sign-in credentials (shown once):\n  Username: zdmadmin\n  Password: %s\nChange this password when prompted on first sign-in. Store it securely; it is not saved in the installer.\n' "$GENERATED_PASSWORD"
  unset GENERATED_PASSWORD
fi
printf 'The backend remains bound to 127.0.0.1:8080. Ensure TCP 443 is permitted by the cloud/network firewall and matches the configured client allowlist.\n'
