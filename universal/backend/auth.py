"""Persist the console account separately from its one-time bootstrap password."""
import hashlib
import secrets
import sqlite3
from contextlib import closing

ITERATIONS = 600_000

def digest(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), ITERATIONS).hex()

def initialize(connection, username, bootstrap_password, timestamp):
    connection.execute('''CREATE TABLE IF NOT EXISTS console_account (
        id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
        salt TEXT NOT NULL, digest TEXT NOT NULL, changed_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS account_events (
        id INTEGER PRIMARY KEY, event TEXT NOT NULL, username TEXT NOT NULL, created TEXT NOT NULL)''')
    created_account = not connection.execute('SELECT 1 FROM console_account WHERE id=1').fetchone()
    if created_account:
        if not bootstrap_password:
            raise RuntimeError('Set the bootstrap GUI password before the first start.')
        salt = secrets.token_hex(32)
        connection.execute('INSERT INTO console_account VALUES (1,?,?,?,?)',
                           (username, salt, digest(bootstrap_password, salt), timestamp))
    migrate_users(connection,timestamp,created_account)
    initialize_sessions(connection)

def matches(row, username, password):
    return bool(row) and secrets.compare_digest(row['username'].encode(), username.encode()) and secrets.compare_digest(row['digest'], digest(password, row['salt']))

def authenticate(database,username,password):
    return bool(principal(database,username,password))


ROLES=('viewer','manager','admin')

def migrate_users(connection,timestamp,force_password_change=False):
    connection.execute('''CREATE TABLE IF NOT EXISTS console_users (
      username TEXT PRIMARY KEY COLLATE NOCASE,salt TEXT NOT NULL,digest TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('viewer','manager','admin')),
      enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),must_change_password INTEGER NOT NULL,
      created TEXT NOT NULL,changed_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS user_events (
      id INTEGER PRIMARY KEY,actor TEXT NOT NULL,event TEXT NOT NULL,
      subject TEXT NOT NULL,created TEXT NOT NULL)''')
    # Preserve the existing administrator's exact password hash and salt.
    connection.execute('''INSERT OR IGNORE INTO console_users
      SELECT username,salt,digest,'admin',1,?, ?,changed_at FROM console_account WHERE id=1''',(int(force_password_change),timestamp))

def principal(database,username,password):
    if len(password)>128 or len(username)>128:return None
    with closing(sqlite3.connect(database,timeout=20)) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute('SELECT * FROM console_users WHERE username=?',(username,)).fetchone()
    if not row:
        digest(password,'00'*32)
        return None
    if not matches(row,row['username'],password) or not row['enabled']:return None
    return {k:row[k] for k in ['username','role','must_change_password','changed_at']}

def permitted(role,method,path):
    if path.startswith('/api/users'):
        return role=='admin'
    if path in {'/api/ai/settings','/api/ai/test'} and method not in {'GET','HEAD'}:
        return role=='admin'
    if method in {'GET','HEAD'}:return role in ROLES
    if path=='/api/account/password':return role in ROLES
    return role in {'manager','admin'}

def public_user(row):
    return {k:row[k] for k in ['username','role','enabled','must_change_password','created','changed_at']}

def password_valid(value):
    if not 12<=len(value)<=128 or not value.strip():raise ValueError('Use a password of 12–128 characters, not only spaces.')

def set_password(conn,username,password,stamp,force=False):
    password_valid(password)
    conn.execute('DELETE FROM console_sessions WHERE username=?',(username,))
    salt=secrets.token_hex(32);hashed=digest(password,salt)
    conn.execute('UPDATE console_users SET salt=?,digest=?,changed_at=?,must_change_password=? WHERE username=?',(salt,hashed,stamp,int(force),username))
    # Keep the original administrator record synchronized for rollback compatibility.
    conn.execute('UPDATE console_account SET salt=?,digest=?,changed_at=? WHERE username=?',(salt,hashed,stamp,username))

COOKIE='zdm_session'
SESSION_SECONDS=8*60*60

def initialize_sessions(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS console_sessions (
      token_hash TEXT PRIMARY KEY,username TEXT NOT NULL,credential_version TEXT NOT NULL,
      expires REAL NOT NULL)''')

def new_session(conn,username):
    import time
    token=secrets.token_urlsafe(32)
    user=conn.execute('SELECT changed_at FROM console_users WHERE username=? AND enabled=1',(username,)).fetchone()
    if not user:raise ValueError('Account is unavailable.')
    conn.execute('DELETE FROM console_sessions WHERE expires<=?',(time.time(),))
    conn.execute('INSERT INTO console_sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),username,user['changed_at'],time.time()+SESSION_SECONDS))
    return token

def revoke_session(conn,token):
    if token:conn.execute('DELETE FROM console_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

def session_principal(database,token):
    import time
    if not token or len(token)!=43:return None
    with closing(sqlite3.connect(database,timeout=20)) as conn:
        conn.row_factory=sqlite3.Row
        row=conn.execute('''SELECT u.* FROM console_users u JOIN console_sessions s
          ON u.username=s.username WHERE s.token_hash=? AND s.expires>?
          AND s.credential_version=u.changed_at AND u.enabled=1''',
          (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    return {k:row[k] for k in ['username','role','must_change_password','changed_at']} if row else None

def set_cookie(response,token,secure):
    response.set_cookie(COOKIE,token,max_age=SESSION_SECONDS,httponly=True,secure=secure,samesite='strict',path='/')
