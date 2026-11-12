#!/usr/bin/env bash
# Standalone GUI uninstaller. Oracle ZDM and migration files are preserved.
set -Eeuo pipefail
umask 077

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage(){
  cat <<'HELP'
ZDM Migration Console uninstaller
Usage: sudo ./zdm-gui-uninstall.run [--dry-run] [--yes]
       [--user] [--remove-zdm-autostart] [--app-dir /path] [--data-dir /path]
For a user installation, run with --user as its owner, without sudo.

Removes GUI services, application files, and GUI HTTPS configuration.
Preserves GUI data, Oracle ZDM, migration files, managed /etc/hosts entries,
host-file backups, and (by default) the optional ZDM auto-start service.
Shared RPM packages, NGINX, firewall and SELinux settings are preserved.
--dry-run prints the plan without changing the system.
--yes skips the interactive confirmation.
If ZDM auto-start is installed, interactive uninstall asks whether to remove it.
--remove-zdm-autostart explicitly removes the installed boot unit without
stopping the running Oracle ZDM service. With --yes alone, auto-start is kept.
HELP
}
remove_zdm_autostart(){
  # Do not use --now or stop: ExecStop would stop Oracle ZDM itself.
  systemctl disable zdmservice-autostart.service || fail 'Could not disable ZDM auto-start.'
  if systemctl is-enabled --quiet zdmservice-autostart.service; then
    fail 'ZDM auto-start is still enabled; its unit was not removed.'
  fi
  rm -f -- /etc/systemd/system/zdmservice-autostart.service
}
DRY_RUN=0; ASSUME_YES=0; REMOVE_AUTOSTART=0; USER_MODE=0; APP_DIR=''; DATA_DIR=''
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0;;
    --dry-run) DRY_RUN=1; shift;;
    --yes) ASSUME_YES=1; shift;;
    --remove-zdm-autostart) REMOVE_AUTOSTART=1; shift;;
    --user) USER_MODE=1; shift;;
    --app-dir|--data-dir)
      (($# >= 2)) || fail "$1 requires a directory"
      if [[ "$1" == --app-dir ]]; then APP_DIR=$2; else DATA_DIR=$2; fi
      shift 2;;
    *) fail "Unknown option: $1";;
  esac
done
if [[ "$USER_MODE" == 1 ]]; then
  [[ $(uname -s) == Linux ]] || fail 'Run user-mode uninstall on the Linux GUI host.'
  [[ $EUID -ne 0 ]] || fail 'Run --user uninstall as the GUI owner, without sudo.'
  USER_PYTHON=''
  for candidate in python3.11 python3.10 python3.9 python3.8 python38 python3 /opt/rh/rh-python38/root/usr/bin/python3.8; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,8))' >/dev/null 2>&1; then USER_PYTHON=$candidate; break; fi
  done
  [[ -n "$USER_PYTHON" ]] || fail 'Python 3.8+ is required for user-mode uninstall.'
  "$USER_PYTHON" - "$APP_DIR" "$DATA_DIR" "$DRY_RUN" "$ASSUME_YES" "$REMOVE_AUTOSTART" <<'USER_UNINSTALL'
import importlib.util,json,os,pwd,shutil,sqlite3,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path

os.umask(0o077)
app_arg,data_arg,dry,yes,remove=sys.argv[1:]
account_home=Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
manifest=account_home/'.config/zdm-gui/install.json'
saved=json.loads(manifest.read_text()) if manifest.exists() else {}
if not app_arg and not saved:
    raise SystemExit('No user installation manifest found. Specify --app-dir explicitly.')
app=Path(app_arg or saved['app']).resolve()
config_path=app/'deploy/user-config.json'
if not config_path.is_file() or config_path.stat().st_uid!=os.getuid():
    raise SystemExit('A user-owned GUI installation config was not found at '+str(config_path))
config=json.loads(config_path.read_text())
if config.get('mode')!='user' or config.get('uid')!=os.getuid() or Path(config['app']).resolve()!=app:
    raise SystemExit('Installation is not a user GUI owned by this account.')
data=Path(config['data']).resolve()
if data_arg and Path(data_arg).resolve()!=data:
    raise SystemExit('--data-dir does not match the installed data directory; nothing removed.')
if app in {Path('/'),Path('/home'),Path('/u01'),Path('/u01/app'),Path('/opt'),Path('/usr'),Path('/var'),account_home} or app.stat().st_uid!=os.getuid():
    raise SystemExit('Refusing broad or foreign-owned application directory.')
if not (app/'backend/app.py').is_file() or not (app/'frontend/dist').is_dir():
    raise SystemExit('Application markers are missing; nothing removed.')
if app==data or data in app.parents:
    raise SystemExit('Unsafe application/data layout; nothing removed.')
for value in config['protected']:
    protected=Path(value).resolve()
    if app==protected or app in protected.parents or protected in app.parents:
        raise SystemExit('Application overlaps protected ZDM storage; nothing removed.')
components=('backend','frontend','deploy','.venv','.oci-venv','tls','bin','run','logs')
for name in components:
    if app/name==data or app/name in data.parents:
        raise SystemExit('Data is inside a removable component; nothing removed.')
units=account_home/'.config/systemd/user'
auto=units/'zdmservice-autostart.service'
auto_installed=auto.exists() or auto.is_symlink()
print('Remove user GUI:',app,'\nPreserve data:',data)
print('Preserve Oracle ZDM, system services, shared packages, /etc/hosts and firewall settings.')
if auto_installed:
    print('User ZDM auto-start unit found.' + (' Removal selected.' if remove=='1' else ' Preserved unless selected.'))
else:
    print('No user ZDM auto-start unit installed; no auto-start question will be asked.')
if dry=='1':
    print('Dry run complete. No changes made.')
    raise SystemExit(0)
# stdin contains this program; use /dev/tty for interactive confirmation.
def question(text):
    try:
        with open('/dev/tty','r+') as tty:
            tty.write(text);tty.flush();return tty.readline().strip()
    except OSError:
        raise SystemExit('Use an interactive terminal or --yes after reviewing --dry-run.')
if auto_installed and remove=='0' and yes=='0':
    remove='1' if question('Remove installed user ZDM auto-start unit (running ZDM stays active)? [y/N]: ').lower() in {'y','yes'} else '0'
if yes=='0' and question('Type UNINSTALL to remove the user GUI: ')!='UNINSTALL':
    raise SystemExit('Cancelled.')
runtime_path=app/'deploy/user-runtime.py'
spec=importlib.util.spec_from_file_location('gui_runtime',runtime_path)
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
if runtime.pending(data):
    raise SystemExit('GUI operations are queued or running; wait before uninstalling.')
unit=units/'zdm-gui.service'
if unit.exists() and str(config_path) not in unit.read_text():
    raise SystemExit('The user GUI service belongs to another installation; nothing removed.')
subprocess.run([str(app/'.venv/bin/python'),str(runtime_path),str(config_path),'stop'],check=True)
backup=account_home/'.local/state/zdm-gui/uninstall-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup.mkdir(parents=True,mode=0o700)
for source in (config_path,app/'tls',unit):
    if source.is_dir():shutil.copytree(source,backup/source.name)
    elif source.exists():shutil.copy2(source,backup/source.name)
if unit.exists():
    subprocess.run(['systemctl','--user','disable','zdm-gui.service'],check=True)
    unit.unlink()
if auto_installed and remove=='1':
    shutil.copy2(auto,backup/auto.name)
    subprocess.run(['systemctl','--user','disable','zdmservice-autostart.service'],check=True)
    auto.unlink()
if config.get('service')=='user' or (auto_installed and remove=='1'):
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
if app in data.parents:
    for name in components:
        path=app/name
        if path.is_symlink():path.unlink()
        elif path.is_dir():shutil.rmtree(path)
        elif path.exists():path.unlink()
else:
    shutil.rmtree(app)
if manifest.exists() and Path(saved.get('app','/')).resolve()==app:
    manifest.unlink()
print('User GUI uninstalled. Data preserved:',data,'\nConfiguration/TLS backup:',backup)
USER_UNINSTALL
  exit $?
fi
[[ $(uname -s) == Linux ]] || fail 'Run this uninstaller on the Linux GUI host.'
[[ $EUID -eq 0 || $DRY_RUN == 1 ]] || fail 'Run as root, or use --dry-run.'
command -v systemctl >/dev/null || fail 'systemd is required.'
command -v realpath >/dev/null || fail 'realpath is required.'

# Read simple installer-written values as data; never execute the env file.
env_value(){
  local key=$1
  [[ -r /etc/zdm-gui.env ]] || return 0
  sed -n "s/^${key}=//p" /etc/zdm-gui.env | head -1
}
if [[ -z "$APP_DIR" ]]; then
  APP_DIR=$(systemctl show zdm-gui.service --property=WorkingDirectory --value 2>/dev/null || true)
  if [[ -z "$APP_DIR" ]]; then
    if [[ -f /u01/app/zdm-gui/backend/app.py ]]; then APP_DIR=/u01/app/zdm-gui
    else APP_DIR=/opt/zdm-gui; fi
  fi
fi
if [[ -z "$DATA_DIR" ]]; then
  DATA_DIR=$(env_value ZDM_GUI_DATA)
  if [[ -z "$DATA_DIR" ]]; then
    if [[ -f "$APP_DIR/data/console.sqlite" ]]; then DATA_DIR=$APP_DIR/data
    else DATA_DIR=/var/lib/zdm-gui; fi
  fi
fi
normalize(){
  [[ "$1" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail "Invalid directory: $1"
  realpath -m -- "$1"
}
APP_DIR=$(normalize "$APP_DIR"); DATA_DIR=$(normalize "$DATA_DIR")
case "$APP_DIR" in /|/opt|/u01|/u01/app|/usr|/usr/local|/var|/var/lib|/home|/root|/etc|/tmp) fail "Refusing broad application path: $APP_DIR";; esac
[[ "$APP_DIR" != "$DATA_DIR" ]] || fail 'Application and data directories are identical; review manually.'
# An application removal must never encompass the ZDM home/base/stage/account.
for key in ZDM_HOME ZDM_BASE ZDM_STAGE ZDM_OS_HOME; do
  value=$(env_value "$key")
  case "$key" in
    ZDM_HOME) value=${value:-/u01/app/zdmhome};;
    ZDM_BASE) value=${value:-/u01/app/zdmbase};;
    ZDM_STAGE) value=${value:-/u01/app/zdmstage};;
  esac
  [[ -n "$value" ]] || continue
  value=$(normalize "$value")
  [[ "$value" != "$APP_DIR" && "$value" != "$APP_DIR/"* && "$APP_DIR" != "$value/"* ]] || fail "Application overlaps protected $key: $value"
done
APP_PRESENT=0
if [[ -e "$APP_DIR" ]]; then
  [[ -f "$APP_DIR/backend/app.py" && -d "$APP_DIR/frontend/dist" ]] || fail "GUI application markers missing at $APP_DIR; review --app-dir."
  APP_PRESENT=1
fi
NESTED_DATA=0
[[ "$DATA_DIR" != "$APP_DIR/"* ]] || NESTED_DATA=1
if [[ "$NESTED_DATA" == 1 ]]; then
  for item in backend frontend deploy .venv .oci-venv; do
    [[ "$DATA_DIR" != "$APP_DIR/$item" && "$DATA_DIR" != "$APP_DIR/$item/"* ]] || fail 'Data is inside an application component; review manually.'
    # Avoid following a component symlink into preserved data or ZDM storage.
    [[ ! -L "$APP_DIR/$item" ]] || fail "Symlinked application component: $item; review manually."
  done
fi

AUTOSTART_INSTALLED=0
autostart_load=$(systemctl show zdmservice-autostart.service --property=LoadState --value 2>/dev/null || true)
if [[ -e /etc/systemd/system/zdmservice-autostart.service || -L /etc/systemd/system/zdmservice-autostart.service || ( -n "$autostart_load" && "$autostart_load" != not-found ) ]]; then
  AUTOSTART_INSTALLED=1
fi
if [[ "$AUTOSTART_INSTALLED" == 1 && "$REMOVE_AUTOSTART" == 0 && "$DRY_RUN" == 0 && "$ASSUME_YES" == 0 ]]; then
  [[ -t 0 ]] || fail 'Use an interactive terminal, or --yes after reviewing --dry-run.'
  read -r -p 'ZDM auto-start is installed. Remove its boot unit (leave running ZDM active)? [y/N]: ' answer
  if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then REMOVE_AUTOSTART=1; fi
fi

printf 'Remove GUI application: %s\nPreserve GUI data: %s\n' "$APP_DIR" "$DATA_DIR"
printf '%s\n' 'Remove: zdm-gui and zdm-hosts-manager services, GUI HTTPS config/certificates.'
printf '%s\n' 'Preserve: Oracle ZDM, migration files, /etc/hosts and host backups.'
if [[ "$AUTOSTART_INSTALLED" == 0 ]]; then
  printf 'ZDM auto-start: not installed; nothing to remove.\n'
elif [[ "$REMOVE_AUTOSTART" == 1 ]]; then
  printf 'Remove: ZDM auto-start boot unit (running Oracle ZDM stays active).\n'
else
  printf 'Preserve: installed ZDM auto-start boot unit.\n'
  [[ "$DRY_RUN" == 0 || "$ASSUME_YES" == 1 ]] || printf 'Interactive uninstall will ask whether to remove this installed boot unit.\n'
fi
printf '%s\n' 'Preserve: shared packages, NGINX, firewall and SELinux settings.'
[[ "$APP_PRESENT" == 1 ]] || printf 'Application directory is absent; only GUI configuration will be removed.\n'
[[ "$NESTED_DATA" == 0 ]] || printf 'Data is inside the application directory; only code and virtual environments will be removed.\n'
if [[ "$DRY_RUN" == 1 ]]; then printf 'Dry run complete. No changes made.\n'; exit 0; fi
if [[ "$ASSUME_YES" == 0 ]]; then
  [[ -t 0 ]] || fail 'Use an interactive terminal, or --yes after reviewing --dry-run.'
  read -r -p 'Type UNINSTALL to remove the GUI: ' answer
  [[ "$answer" == UNINSTALL ]] || { printf 'Cancelled.\n'; exit 0; }
fi

# Do not interrupt work launched through the GUI.
if [[ -f "$DATA_DIR/console.sqlite" ]]; then
  PYTHON=''
  for candidate in "$APP_DIR/.venv/bin/python" python3 /opt/rh/rh-python38/root/usr/bin/python3.8; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON=$candidate; break; fi
  done
  [[ -n "$PYTHON" ]] || fail 'Cannot check queued/running GUI operations: Python is missing.'
  "$PYTHON" - "$DATA_DIR/console.sqlite" <<'PY'
import sqlite3, sys
from urllib.parse import quote
with sqlite3.connect('file:'+quote(sys.argv[1],safe='/')+'?mode=ro',uri=True) as db:
    exists=db.execute("select 1 from sqlite_master where type='table' and name='operations'").fetchone()
    pending=db.execute("select count(*) from operations where state in ('QUEUED','RUNNING')").fetchone()[0] if exists else 0
    if pending:
        raise SystemExit('GUI operations are queued or running. Wait for or resolve them before uninstalling.')
PY
fi

# Stop only GUI services. Never stop Oracle ZDM or its optional boot unit.
for unit in zdm-gui.service zdm-hosts-manager.service; do
  load=$(systemctl show "$unit" --property=LoadState --value 2>/dev/null || true)
  if [[ -n "$load" && "$load" != not-found ]]; then
    systemctl stop "$unit"
    systemctl disable "$unit"
    if systemctl is-active --quiet "$unit"; then fail "Service is still active: $unit"; fi
  fi
done

# Save configuration and TLS material in a root-only directory for recovery.
mkdir -p /var/backups
BACKUP_DIR=$(mktemp -d /var/backups/zdm-gui-uninstall.XXXXXXXX)
chmod 0700 "$BACKUP_DIR"
for path in /etc/zdm-gui.env /etc/zdm-gui /etc/nginx/conf.d/zdm-gui.conf \
  /etc/pki/tls/certs/zdm-gui.crt /etc/pki/tls/private/zdm-gui.key \
  /etc/systemd/system/zdm-gui.service /etc/systemd/system/zdm-hosts-manager.service \
  /etc/systemd/system/zdmservice-autostart.service \
  /etc/tmpfiles.d/zdm-hosts-manager.conf /usr/local/libexec/zdm-hosts-manager.py; do
  [[ ! -e "$path" && ! -L "$path" ]] || cp -a --parents -- "$path" "$BACKUP_DIR/"
done
NGINX_CHANGED=0
if [[ -e /etc/nginx/conf.d/zdm-gui.conf ]]; then
  rm -f -- /etc/nginx/conf.d/zdm-gui.conf
  NGINX_CHANGED=1
  if command -v nginx >/dev/null && ! nginx -t; then
    cp -a -- "$BACKUP_DIR/etc/nginx/conf.d/zdm-gui.conf" /etc/nginx/conf.d/zdm-gui.conf
    fail "NGINX check failed; GUI proxy config restored. Backup: $BACKUP_DIR"
  fi
  if systemctl is-active --quiet nginx.service; then
    if ! systemctl reload nginx.service; then
      cp -a -- "$BACKUP_DIR/etc/nginx/conf.d/zdm-gui.conf" /etc/nginx/conf.d/zdm-gui.conf
      fail "NGINX reload failed; GUI proxy config restored. Backup: $BACKUP_DIR"
    fi
  fi
fi
rm -f -- /etc/systemd/system/zdm-gui.service /etc/systemd/system/zdm-hosts-manager.service \
  /etc/tmpfiles.d/zdm-hosts-manager.conf /usr/local/libexec/zdm-hosts-manager.py \
  /etc/zdm-gui.env /run/zdm-gui-bootstrap.env
if [[ "$NGINX_CHANGED" == 1 ]]; then
  rm -f -- /etc/pki/tls/certs/zdm-gui.crt /etc/pki/tls/private/zdm-gui.key
fi
rm -rf -- /etc/zdm-gui /run/zdm-hosts-manager
if [[ "$AUTOSTART_INSTALLED" == 1 && "$REMOVE_AUTOSTART" == 1 ]]; then
  remove_zdm_autostart
fi
systemctl daemon-reload
if [[ "$APP_PRESENT" == 1 ]]; then
  if [[ "$NESTED_DATA" == 1 ]]; then
    for item in backend frontend deploy .venv .oci-venv; do rm -rf -- "$APP_DIR/$item"; done
  else
    rm -rf -- "$APP_DIR"
  fi
fi
printf '\nGUI uninstalled.\nPreserved GUI data: %s\nConfiguration backup: %s\n' "$DATA_DIR" "$BACKUP_DIR"
if [[ "$AUTOSTART_INSTALLED" == 1 && "$REMOVE_AUTOSTART" == 1 ]]; then
  printf '%s\n' 'ZDM auto-start removed; running Oracle ZDM was not stopped.'
else
  printf '%s\n' 'Oracle ZDM and its auto-start configuration were left unchanged.'
fi
