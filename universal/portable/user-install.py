"""Install a private ZDM GUI as the existing ZDM OS owner; no elevation."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys


def die(message):
    raise SystemExit(message)


def check_layout(app, data, protected):
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    for path in (app, data):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)):
            die('Use absolute directory paths without whitespace or shell characters.')
        if path in {Path('/'), Path('/u01'), Path('/u01/app'), Path('/opt'), Path('/var'),
                    Path('/var/lib'), Path('/usr'), Path('/usr/local'), Path('/etc'), Path('/home'), account_home}:
            die('Refusing broad installation/data directory: ' + str(path))
        for item in protected:
            if path == item or path in item.parents or item in path.parents:
                die('Installation/data directory overlaps ZDM storage: ' + str(item))
        if path.exists() and path.stat().st_uid != os.getuid():
            die('Directory is not owned by the current account: ' + str(path))
        parent = path
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            die('Directory is not writable by this account: ' + str(parent))
    if app == data or data in app.parents:
        die('Keep application and data directories separate (app/data is allowed).')
    if data in [app / name for name in ('backend', 'frontend', 'deploy', '.venv', '.oci-venv', 'tls', 'bin', 'run', 'logs')]:
        die('Data cannot be stored inside a removable application component.')
    for name in ('backend', 'frontend', 'deploy', '.venv', '.oci-venv', 'tls', 'bin', 'run', 'logs'):
        if app / name in data.parents:
            die('Data cannot be stored inside a removable application component.')
    if app.exists() and any(app.iterdir()) and not (app / 'deploy/user-config.json').exists():
        die('Application directory is not an existing user installation; choose an empty directory.')


def ask(value, label, default, yes):
    return value if value is not None else default if yes else (input(label + ' [' + str(default) + ']: ').strip() or default)


def load_runtime():
    path = Path(__file__).parent / 'deploy/user-runtime.py'
    spec = importlib.util.spec_from_file_location('user_runtime', path)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('app-dir', 'data-dir', 'zdm-home', 'zdm-base', 'zdm-stage', 'tls-name', 'bind', 'allowed-clients'):
        parser.add_argument('--' + name)
    parser.add_argument('--port', type=int)
    parser.add_argument('--service', choices=('auto', 'manual', 'user'), default='auto')
    parser.add_argument('--yes', action='store_true', help='Use defaults for unanswered prompts.')
    parser.add_argument('--check', action='store_true', help='Check prerequisites and paths without installing.')
    parser.add_argument('--no-start', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if sys.version_info < (3, 9):
        die('Python 3.9+ is required by the bundled GUI packages.')
    if os.getuid() == 0:
        die('Run --user mode as the ZDM owner, without sudo.')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        die('User installation requires Linux x86-64.')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    distro = release.get('ID', '').strip('"')
    major = release.get('VERSION_ID', '').strip('"').split('.')[0]
    if (distro, major) not in {('ol', '7'), ('ol', '8'), ('ol', '9'), ('rhel', '8'), ('rhel', '9')}:
        die('Use a supported Oracle Linux 7/8/9 or RHEL 8/9 host.')
    if not args.yes and not sys.stdin.isatty():
        die('Use an interactive terminal or --yes with explicit path options.')
    account = pwd.getpwuid(os.getuid())
    account_home = Path(account.pw_dir).resolve()
    app = Path(ask(args.app_dir, 'GUI application directory', str(account_home / 'zdm-gui'), args.yes)).resolve()
    data = Path(ask(args.data_dir, 'GUI data directory', str(account_home / '.local/share/zdm-gui'), args.yes)).resolve()
    zdm = {}
    for key in ('home', 'base', 'stage'):
        value = getattr(args, 'zdm_' + key)
        zdm[key] = str(Path(ask(value, 'ZDM ' + key + ' directory', '/u01/app/zdm' + key, args.yes)).resolve())
    home = Path(zdm['home'])
    check_layout(app, data, [Path(path) for path in zdm.values()])
    if not all(os.access(home / 'bin' / name, os.X_OK) for name in ('zdmcli', 'zdmservice')):
        die('ZDM home must contain executable bin/zdmcli and bin/zdmservice.')
    if home.stat().st_uid != os.getuid():
        die('Run the installer as the operating-system account that owns ZDM home.')
    try:
        build = subprocess.run([str(home / 'bin/zdmcli'), '-build'], check=True, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        die('Could not query the installed ZDM build.')
    match = re.search(r'(?im)^\s*full version\s*:\s*["\']?(21\.4|21\.5|26\.1)(?:[."\s]|$)', build)
    if not match:
        die('This installer supports Oracle ZDM 21.4, 21.5, and 26.1 only.')
    zdm_release = match.group(1)
    if zdm_release != '26.1' and major == '9':
        die('ZDM ' + zdm_release + ' requires Oracle Linux 7/8 or RHEL 8.')
    for key in ('base', 'stage'):
        if not Path(zdm[key]).is_dir() or not os.access(zdm[key], os.W_OK | os.X_OK):
            die('ZDM ' + key + ' must be an existing writable directory.')
    for module in ('venv', 'ssl', 'sqlite3'):
        if importlib.util.find_spec(module) is None:
            die('Ask the administrator to install Python 3.9+ with venv, SSL and SQLite.')
    missing = [name for name in ('openssl', 'ssh', 'ssh-keygen', 'expect', 'unzip') if not shutil.which(name)]
    if missing:
        die('Missing prerequisites: ' + ', '.join(missing) + '. Ask an administrator to install them; user mode never runs dnf/yum.')
    tls_name = ask(args.tls_name, 'HTTPS certificate hostname/IP', socket.getfqdn(), args.yes)
    if not re.fullmatch(r'[A-Za-z0-9.:-]+', tls_name):
        die('Enter a valid certificate hostname or IP address.')
    try:
        san = 'IP:' + str(ipaddress.ip_address(tls_name))
    except ValueError:
        if ':' in tls_name or len(tls_name) > 253 or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-') for part in tls_name.rstrip('.').split('.')):
            die('Enter a valid certificate hostname.')
        san = 'DNS:' + tls_name
    bind = ask(args.bind, 'Listen address (127.0.0.1 for SSH tunnel; 0.0.0.0 for direct access)', '127.0.0.1', args.yes)
    if bind not in ('127.0.0.1', '0.0.0.0'):
        die('Use 127.0.0.1 or 0.0.0.0 as the listen address.')
    try:
        port = int(ask(args.port, 'HTTPS port', 8443, args.yes))
        if not 1024 <= port <= 65535:
            raise ValueError()
    except ValueError:
        die('Use an unprivileged port between 1024 and 65535.')
    allowed = ask(args.allowed_clients, 'Allowed client IPs/CIDRs, comma-separated', '127.0.0.1/32,::1/128', args.yes)
    try:
        networks = [str(ipaddress.ip_network(v.strip(), strict=False)) for v in allowed.split(',') if v.strip()]
        if not networks:
            raise ValueError()
    except ValueError:
        die('Enter a nonempty list of client IPs/CIDRs.')
    service_available = bool(shutil.which('systemctl') and subprocess.run(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
    if args.service == 'user' and not service_available:
        die('User systemd manager is unavailable. Log in directly as the user or use --service manual.')
    service = 'user' if args.service != 'manual' and service_available else 'manual'
    runtime = load_runtime()
    if runtime.pending(data):
        die('GUI operations are queued or running; wait before upgrading.')
    print('User installation:', app, '\nPreserved GUI data:', data, '\nZDM release:', zdm_release,
          '\nHTTPS:', bind + ':' + str(port), '\nService mode:', service)
    if args.check:
        print('Checks passed. No changes made.')
        return
    source = Path(__file__).parent
    for item in ('backend/app.py', 'frontend/dist/index.html', 'deploy/oci-requirements.txt', 'deploy/user-runtime.py'):
        if not (source / item).is_file():
            die('Incomplete release: ' + item)
    config_home = account_home / '.config/zdm-gui'
    user_units = account_home / '.config/systemd/user'
    unit = user_units / 'zdm-gui.service'
    previous = app / 'deploy/user-config.json'
    if unit.exists() and (not previous.exists() or str(previous) not in unit.read_text()):
        die('A different user GUI service already exists. Review it before installing.')
    if previous.exists():
        subprocess.run([sys.executable, str(app / 'deploy/user-runtime.py'), str(previous), 'stop'], check=True)
        backup = account_home / '.local/state/zdm-gui/backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup.mkdir(parents=True, mode=0o700)
        if (data / 'console.sqlite').exists():
            with closing(sqlite3.connect(data / 'console.sqlite')) as db, closing(sqlite3.connect(backup / 'console.sqlite')) as dest:
                db.backup(dest)
        shutil.copy2(previous, backup / 'user-config.json')
        print('Upgrade backup:', backup)
    for path in (app, data, config_home, app / 'logs', app / 'tls', app / 'bin', Path(zdm['stage']) / 'gui-responses'):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    app.chmod(0o700)
    data.chmod(0o700)
    for item in ('backend', 'frontend/dist'):
        shutil.copytree(source / item, app / item, dirs_exist_ok=True)
    (app / 'deploy').mkdir(mode=0o700, exist_ok=True)
    for item in ('oci-requirements.txt', 'user-runtime.py'):
        shutil.copy2(source / 'deploy' / item, app / 'deploy' / item)
    for name, requirements in (('.venv', app / 'backend/requirements.txt'), ('.oci-venv', app / 'deploy/oci-requirements.txt')):
        python = app / name / 'bin/python'
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(app / name)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', 'pip<25.1'], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
    cert = app / 'tls/gui.crt'
    key = app / 'tls/gui.key'
    openssl_config = app / 'tls/openssl.cnf'
    openssl_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n[dn]\nCN=' + tls_name + '\n[ext]\nsubjectAltName=' + ','.join(dict.fromkeys([san, 'IP:127.0.0.1', 'IP:::1'])) + '\n')
    subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:3072', '-sha256', '-days', '365', '-keyout', str(key), '-out', str(cert), '-config', str(openssl_config)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    oci_site = subprocess.run([str(app / '.oci-venv/bin/python'), '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'],
                              text=True, capture_output=True, check=True).stdout.strip()
    environment = {'ZDM_HOME': zdm['home'], 'ZDM_BASE': zdm['base'], 'ZDM_STAGE': zdm['stage'],
                   'ZDM_OS_HOME': str(account_home), 'ZDM_GUI_DATA': str(data), 'ZDM_GUI_USERNAME': 'zdmadmin',
                   'ZDM_GUI_SERVICE_USER': account.pw_name, 'ZDM_GUI_ALLOWED_IPS': ','.join(networks),
                   'ZDM_GUI_HOSTS_READ_ONLY': '1', 'ZDM_OCI_PYTHON': str(app / '.oci-venv/bin/python'),
                   'ZDM_OCI_SITE': oci_site,
                   'HOME': str(account_home), 'PATH': zdm['home'] + '/bin:' + os.environ.get('PATH', '/usr/bin:/bin')}
    config = {'mode': 'user', 'uid': os.getuid(), 'app': str(app), 'data': str(data), 'bind': bind,
              'port': port, 'service': service, 'protected': list(zdm.values()), 'environment': environment}
    previous.write_text(json.dumps(config, indent=2))
    previous.chmod(0o600)
    (config_home / 'install.json').write_text(json.dumps({'app': str(app), 'data': str(data), 'uid': os.getuid()}))
    (config_home / 'install.json').chmod(0o600)
    temporary_password = secrets.token_urlsafe(24)
    initialize = '''import json,sys,sqlite3
from datetime import datetime,timezone
from backend import auth
data=json.load(sys.stdin)
with sqlite3.connect(data['database']) as db:
    exists=db.execute("select 1 from sqlite_master where name='console_account'").fetchone()
    existing=bool(exists and db.execute('select 1 from console_account where id=1').fetchone())
    auth.initialize(db,'zdmadmin',data['password'],datetime.now(timezone.utc).isoformat())
print('existing' if existing else 'created')
'''
    result = subprocess.run([str(app / '.venv/bin/python'), '-c', initialize], cwd=app,
                            input=json.dumps({'database': str(data / 'console.sqlite'), 'password': temporary_password}),
                            text=True, capture_output=True, check=True)
    if result.stdout.strip() == 'created':
        print('Temporary login: zdmadmin\nTemporary password (shown once): ' + temporary_password + '\nChange it at first sign-in.', flush=True)
    else:
        print('Existing GUI accounts and password hashes were preserved.')
    for action in ('start', 'stop', 'status'):
        wrapper = app / 'bin' / ('zdm-gui-' + action)
        wrapper.write_text('#!/bin/sh\nexec ' + ' '.join(shlex.quote(v) for v in [str(app / '.venv/bin/python'), str(app / 'deploy/user-runtime.py'), str(previous), action]) + '\n')
        wrapper.chmod(0o700)
    if service == 'user':
        user_units.mkdir(parents=True, exist_ok=True)
        unit.write_text('[Unit]\nDescription=ZDM Migration Console (user HTTPS)\nAfter=network.target\n[Service]\nType=simple\nWorkingDirectory=' + str(app) + '\nExecStart=' + str(app / '.venv/bin/python') + ' ' + str(app / 'deploy/user-runtime.py') + ' ' + str(previous) + ' run\nRestart=on-failure\nRestartSec=5\nUMask=0077\n[Install]\nWantedBy=default.target\n')
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', 'zdm-gui.service'], check=True)
        print('User service enabled. Starting at boot and surviving logout requires user lingering; ask an administrator if it is not enabled.')
    elif unit.exists():
        subprocess.run(['systemctl', '--user', 'disable', 'zdm-gui.service'], check=True)
        unit.unlink()
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    if not args.no_start:
        subprocess.run([str(app / 'bin/zdm-gui-start')], check=True)
    print('\nUser GUI installation complete. HTTPS URL: https://' + tls_name + ':' + str(port))
    print('Controls:', app / 'bin', '\nData:', data, '\nHostname entries are read-only. System firewall and Oracle ZDM boot services are administrator-managed.')


if __name__ == '__main__':
    main()
