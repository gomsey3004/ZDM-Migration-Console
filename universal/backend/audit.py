"""Secret-free audit trail for console access, API activity, and client metadata."""
import re
import sqlite3
from contextlib import closing


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS audit_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created TEXT NOT NULL,
      username TEXT NOT NULL,
      role TEXT NOT NULL,
      event TEXT NOT NULL,
      method TEXT NOT NULL,
      path TEXT NOT NULL,
      status INTEGER NOT NULL,
      client_ip TEXT NOT NULL)''')
    columns={row[1] for row in connection.execute('PRAGMA table_info(audit_events)')}
    for name in ('user_agent','client_platform','machine_details'):
        if name not in columns:
            connection.execute('ALTER TABLE audit_events ADD COLUMN '+name+" TEXT NOT NULL DEFAULT ''")
    connection.execute('CREATE INDEX IF NOT EXISTS audit_events_created_idx ON audit_events(id DESC)')


def _clean(value,limit):
    return re.sub(r'[\x00-\x1f\x7f]+',' ',str(value or '')).strip()[:limit]


def describe_client(user_agent,platform='',mobile=''):
    ua=_clean(user_agent,512);hint=_clean(platform,80).strip('"')
    if hint:operating_system=hint
    elif 'Windows NT' in ua:operating_system='Windows'
    elif 'Android' in ua:operating_system='Android'
    elif 'iPhone' in ua or 'iPad' in ua:operating_system='iOS/iPadOS'
    elif 'Mac OS X' in ua:operating_system='macOS'
    elif 'Linux' in ua:operating_system='Linux'
    else:operating_system='Unknown OS'
    if 'Edg/' in ua:browser='Microsoft Edge'
    elif 'OPR/' in ua or 'Opera/' in ua:browser='Opera'
    elif 'Chrome/' in ua or 'CriOS/' in ua:browser='Chrome'
    elif 'Firefox/' in ua or 'FxiOS/' in ua:browser='Firefox'
    elif 'Safari/' in ua:browser='Safari'
    elif ua:browser='Other browser/client'
    else:browser='Unknown browser'
    is_mobile=mobile.strip()=='?1' or bool(re.search(r'Android|iPhone|iPad|Mobile',ua,re.I))
    client_type='Mobile/tablet' if is_mobile else ('Desktop' if browser not in {'Other browser/client','Unknown browser'} else 'Server/API client')
    return ' · '.join((operating_system,browser,client_type))


def record(database, created, username, role, event, method, path, status, client_ip,
           user_agent='',client_platform='',client_mobile=''):
    """Store bounded metadata only; never bodies, query strings, auth headers, or credentials."""
    username = str(username or 'anonymous')[:128]
    role = str(role or '')[:32]
    event = str(event or 'api_request')[:40]
    method = str(method or '')[:12].upper()
    path = str(path or '')[:256].split('?', 1)[0]
    client_ip = _clean(client_ip,64)
    user_agent = _clean(user_agent,512)
    client_platform = _clean(client_platform,80).strip('"')
    machine_details=describe_client(user_agent,client_platform,client_mobile)
    with closing(sqlite3.connect(database, timeout=20)) as connection:
        connection.execute(
            '''INSERT INTO audit_events(created,username,role,event,method,path,status,client_ip,
               user_agent,client_platform,machine_details) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (created, username, role, event, method, path, int(status), client_ip,
             user_agent,client_platform,machine_details))
        connection.commit()
