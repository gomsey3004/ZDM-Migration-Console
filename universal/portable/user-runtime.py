"""User-owned HTTPS process control. Never controls system-wide services."""
import fcntl
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time


def process_matches(record):
    try:
        pid = int(record['pid'])
        proc = Path('/proc') / str(pid)
        return (pid > 1 and proc.stat().st_uid == os.getuid() and
                ('ZDM_GUI_PROCESS_TOKEN=' + record['token']).encode() in
                (proc / 'environ').read_bytes().split(b'\0'))
    except (OSError, ValueError, KeyError):
        return False


def command(config):
    app = Path(config['app'])
    return [str(app / '.venv/bin/python'), '-m', 'uvicorn', 'backend.app:app',
            '--host', config['bind'], '--port', str(config['port']), '--workers', '1',
            '--no-proxy-headers', '--ssl-certfile', str(app / 'tls/gui.crt'),
            '--ssl-keyfile', str(app / 'tls/gui.key')]


def pending(data):
    import sqlite3
    from urllib.parse import quote
    database = Path(data) / 'console.sqlite'
    if not database.exists():
        return False
    with closing(sqlite3.connect('file:' + quote(str(database), safe='/') + '?mode=ro', uri=True)) as db:
        exists = db.execute("select 1 from sqlite_master where name='operations' and type='table'").fetchone()
        return bool(exists and db.execute("select 1 from operations where state in ('QUEUED','RUNNING') limit 1").fetchone())


def main():
    os.umask(0o077)
    config_path = Path(sys.argv[1]).resolve()
    config = json.loads(config_path.read_text())
    if os.getuid() == 0 or config['uid'] != os.getuid():
        raise SystemExit('Run GUI controls as the installation owner, without sudo.')
    action = sys.argv[2] if len(sys.argv) > 2 else 'status'
    if action not in {'start', 'stop', 'status', 'run'}:
        raise SystemExit('Use start, stop, or status.')
    app = Path(config['app'])
    os.environ.update(config['environment'])
    os.chdir(app)
    if action == 'run':
        os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', *command(config)[3:]])
    if config.get('service') == 'user':
        if action == 'stop' and pending(config['data']):
            raise SystemExit('GUI operations are queued or running; wait before stopping.')
        result = subprocess.call(['systemctl', '--user', action, 'zdm-gui.service'])
        if result or action != 'start':
            raise SystemExit(result)
        host = '127.0.0.1' if config['bind'] == '0.0.0.0' else config['bind']
        for _ in range(60):
            connection = None
            try:
                connection = http.client.HTTPSConnection(host, config['port'], timeout=1,
                                                        context=ssl._create_unverified_context())
                connection.request('GET', '/')
                if connection.getresponse().status == 200:
                    print('GUI HTTPS started on port ' + str(config['port']))
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                if connection:
                    connection.close()
            time.sleep(1)
        raise SystemExit('User service did not become HTTPS-ready. Check journalctl --user -u zdm-gui.')
    run = app / 'run'
    run.mkdir(mode=0o700, exist_ok=True)
    record_path = run / 'process.json'
    with (run / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(record_path.read_text()) if record_path.exists() else {}
        running = process_matches(record)
        if action == 'status':
            print('GUI running: ' + str(running).lower())
            raise SystemExit(0 if running else 3)
        if action == 'stop':
            if running:
                if pending(config['data']):
                    raise SystemExit('GUI operations are queued or running; wait before stopping.')
                os.kill(record['pid'], signal.SIGTERM)
                for _ in range(100):
                    if not process_matches(record):
                        break
                    time.sleep(0.1)
                else:
                    raise SystemExit('GUI did not stop; files have been preserved.')
            record_path.unlink(missing_ok=True)
            print('GUI stopped.')
            return
        if running:
            print('GUI is already running.')
            return
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            try:
                check.bind((config['bind'], config['port']))
            except OSError:
                raise SystemExit('HTTPS port is already in use; GUI was not started.')
        token = secrets.token_hex(32)
        environment = dict(os.environ, ZDM_GUI_PROCESS_TOKEN=token)
        (app / 'logs').mkdir(mode=0o700, exist_ok=True)
        with (app / 'logs/gui.log').open('ab') as log:
            process = subprocess.Popen(command(config), cwd=app, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        record_path.write_text(json.dumps({'pid': process.pid, 'token': token}))
        record_path.chmod(0o600)
        host = '127.0.0.1' if config['bind'] == '0.0.0.0' else config['bind']
        for _ in range(60):
            if process.poll() is not None:
                raise SystemExit('GUI exited. Review ' + str(app / 'logs/gui.log'))
            connection = None
            try:
                connection = http.client.HTTPSConnection(host, config['port'], timeout=1,
                                                        context=ssl._create_unverified_context())
                connection.request('GET', '/')
                if connection.getresponse().status == 200:
                    print('GUI HTTPS started on port ' + str(config['port']))
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                if connection:
                    connection.close()
            time.sleep(1)
        # Failure must not leave an unreported background server.
        if process_matches({'pid': process.pid, 'token': token}):
            process.terminate()
        raise SystemExit('HTTPS startup timed out. Review ' + str(app / 'logs/gui.log'))


if __name__ == '__main__':
    main()
