import base64
import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shlex
import sqlite3
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Literal, Optional, Union

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, SecretStr
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from starlette.concurrency import run_in_threadpool

from . import adapter, auth, audit, compatibility, connections, wallets as wallet_tools, responses as response_tools, host_records, migration, interactive, assistant, inventory, response_files, resume as resume_tools, modify as modify_tools

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('ZDM_GUI_DATA', str(ROOT/'data')))
WORK = queue.Queue()
CACHE = {}
CACHE_LOCK = threading.Lock()
SUBMIT_LOCK = threading.Lock()
RESPONSE_LOCK = threading.Lock()

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def db():
    connection = sqlite3.connect(DATA/'console.sqlite', timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()

def cached(key, seconds, fn):
    with CACHE_LOCK:
        stamp, value = CACHE.get(key, (0,None))
        if time.monotonic()-stamp < seconds:
            return value
    # Never hold the global cache lock while a ZDM CLI call is running. Active
    # logical jobs can make query output wait on GoldenGate metrics.
    value = fn()
    with CACHE_LOCK:
        CACHE[key] = (time.monotonic(),value)
    return value

def cached_value(key):
    with CACHE_LOCK:return CACHE.get(key,(0,None))

def clear_cache():
    with CACHE_LOCK:
        CACHE.clear()

def worker():
    while True:
        oid, args, kind, job_id = WORK.get()
        with db() as conn:
            conn.execute('UPDATE operations SET state=?,updated=? WHERE id=?',('RUNNING',now(),oid))
        state, result, actual_job = 'FAILED','',job_id
        try:
            migration_command=kind in {'evaluate','migrate','advisor','summary','listphases'}
            result = adapter.run(args,timeout=900 if migration_command else 180,operation_id=oid if migration_command else None)
            if kind in {'evaluate','migrate','advisor','summary'}:
                match = re.search(r'scheduled with (?:the )?job ID\s*["\x27:]?\s*(\d+)',result,re.I)
                if match:
                    actual_job = int(match[1])
                    state = 'SUBMITTED'
                elif kind=='summary':
                    state='COMPLETED'
                else:
                    state = 'UNKNOWN'
                    result += '\nNo submitted job ID was returned. Inspect ZDM jobs before retrying.'
            else:
                state = 'COMPLETED'
        except TimeoutError as exc:
            state,result = 'UNKNOWN',str(exc)
        except Exception as exc:
            result = str(exc)
        with db() as conn:
            conn.execute('UPDATE operations SET state=?,result=?,job_id=?,updated=? WHERE id=?',
                         (state,adapter.redact(result)[-20000:],actual_job,now(),oid))
        clear_cache()
        WORK.task_done()

@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    with db() as conn:
        conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations (
          id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
          profile_id TEXT, profile_name TEXT, job_id INTEGER, state TEXT NOT NULL,
          command TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS managed_keys (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
          fingerprint TEXT NOT NULL, public_key TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS servers (
          id TEXT PRIMARY KEY, body TEXT NOT NULL, trusted_key TEXT, scan TEXT, last_check TEXT, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS oci_profiles (
          id TEXT PRIMARY KEY, body TEXT NOT NULL, last_check TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS preflights (
          id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, signature TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS credential_wallets (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL, username TEXT NOT NULL,
          path TEXT NOT NULL, created TEXT NOT NULL, last_check TEXT NOT NULL);
        ''')
        conn.execute("UPDATE operations SET state='UNKNOWN',result='GUI restarted before the CLI result was recorded. Refresh ZDM jobs before retrying.',updated=? WHERE state IN ('QUEUED','RUNNING')",(now(),))
        inventory.initialize(conn)
        response_files.initialize(conn)
        auth.initialize(conn, os.environ.get('ZDM_GUI_USERNAME','zdmadmin'), os.environ.get('ZDM_GUI_PASSWORD',''), now())
        audit.initialize(conn)
    threading.Thread(target=worker,daemon=True).start()
    yield

app = FastAPI(title='ZDM Migration Console',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

def security_headers(response):
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Accept-CH']='Sec-CH-UA-Platform, Sec-CH-UA-Mobile'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
    return response

def client_ip_allowed(client, configured):
    try:
        address=ipaddress.ip_address(client)
    except ValueError:
        return False
    for item in configured:
        item=item.strip()
        if not item:
            continue
        try:
            if address in ipaddress.ip_network(item,strict=False):
                return True
        except ValueError:
            continue
    return False

@app.middleware('http')
async def access(request: Request, call_next):
    path=request.url.path
    def client_metadata():
        return (request.client.host if request.client else '',request.headers.get('user-agent',''),
                request.headers.get('sec-ch-ua-platform',''),request.headers.get('sec-ch-ua-mobile',''))
    async def record_request(status, event='api_request', principal=None):
        if not path.startswith('/api/') or path in {'/api/auth/login','/api/auth/logout'}:
            return
        user=principal or getattr(request.state,'user',None) or {}
        try:
            await run_in_threadpool(audit.record,DATA/'console.sqlite',now(),user.get('username','anonymous'),user.get('role',''),event,request.method,path,status,*client_metadata())
        except (OSError,sqlite3.Error):
            pass
    allowed=os.environ.get('ZDM_GUI_ALLOWED_IPS','127.0.0.1/32,::1/128').split(',')
    if not client_ip_allowed(request.client.host if request.client else '',allowed):
        await record_request(403,'access_denied')
        return security_headers(JSONResponse({'detail':'This client IP is not allowed.'},status_code=403))
    if request.headers.get('content-length','0').isdigit() and int(request.headers.get('content-length','0'))>400000:
        await record_request(413,'request_rejected')
        return security_headers(JSONResponse({'detail':'Request is too large.'},status_code=413))
    # Only the sign-in shell and its static assets are public; data APIs stay protected.
    if request.method in {'GET','HEAD'} and (path=='/' or path.startswith('/assets/') or path=='/favicon.ico'):
        return security_headers(await call_next(request))
    public_auth=path in {'/api/auth/login','/api/auth/logout'}
    browser=request.headers.get('x-zdm-session')=='browser' or 'sec-fetch-mode' in request.headers or 'Mozilla/' in request.headers.get('user-agent','')
    principal=None
    if not public_auth:
        token=request.cookies.get(auth.COOKIE)
        if token:principal=await run_in_threadpool(auth.session_principal,DATA/'console.sqlite',token)
        # Browser credentials cached by HTTP Basic must never silently sign a user
        # back in after logout. Explicit non-browser API clients retain Basic auth.
        if not principal and not browser:
            try:
                scheme,encoded=request.headers.get('authorization','').split(' ',1)
                username,password=base64.b64decode(encoded,validate=True).decode().split(':',1)
                if scheme.lower()=='basic':principal=await run_in_threadpool(auth.principal,DATA/'console.sqlite',username,password)
            except (ValueError,UnicodeError):pass
        if not principal:
            headers={} if browser else {'WWW-Authenticate':'Basic realm="ZDM Console", charset="UTF-8"'}
            await record_request(401,'authentication_required')
            return security_headers(JSONResponse({'detail':'Sign in to the ZDM console.'},status_code=401,headers=headers))
        request.state.user=principal
        if principal['must_change_password'] and path.startswith('/api/') and path not in {'/api/account','/api/account/password'}:
            await record_request(403,'password_change_required',principal)
            return security_headers(JSONResponse({'detail':'Change your temporary password before using the console.'},status_code=403))
        if path.startswith('/api/') and not auth.permitted(principal['role'],request.method,path):
            await record_request(403,'permission_denied',principal)
            return security_headers(JSONResponse({'detail':'Your role does not permit this action.'},status_code=403))
    if request.method not in {'GET','HEAD','OPTIONS'}:
        origin=request.headers.get('origin')
        if request.headers.get('x-zdm-request')!='console' or (origin and origin!=str(request.base_url).rstrip('/')):
            await record_request(403,'request_rejected')
            return security_headers(JSONResponse({'detail':'Invalid request origin.'},status_code=403))
    response=await call_next(request)
    await record_request(response.status_code)
    return security_headers(response)

class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=1,max_length=128)
    password:SecretStr

@app.post('/api/auth/login')
def login(body:Login,request:Request,response:Response):
    user_secure(request)
    user=auth.principal(DATA/'console.sqlite',body.username,body.password.get_secret_value())
    if not user:
        audit.record(DATA/'console.sqlite',now(),body.username.strip()[:128],'','sign_in_failed','POST','/api/auth/login',401,request.client.host if request.client else '',request.headers.get('user-agent',''),request.headers.get('sec-ch-ua-platform',''),request.headers.get('sec-ch-ua-mobile',''))
        raise HTTPException(401,'Username or password is incorrect, or the account is disabled.')
    with db() as conn:
        auth.revoke_session(conn,request.cookies.get(auth.COOKIE))
        token=auth.new_session(conn,user['username'])
    auth.set_cookie(response,token,request.url.scheme=='https')
    audit.record(DATA/'console.sqlite',now(),user['username'],user['role'],'sign_in_succeeded','POST','/api/auth/login',200,request.client.host if request.client else '',request.headers.get('user-agent',''),request.headers.get('sec-ch-ua-platform',''),request.headers.get('sec-ch-ua-mobile',''))
    return user

@app.post('/api/auth/logout')
def logout(request:Request,response:Response):
    token=request.cookies.get(auth.COOKIE)
    actor=auth.session_principal(DATA/'console.sqlite',token) if token else None
    with db() as conn:auth.revoke_session(conn,request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE,path='/',secure=request.url.scheme=='https',httponly=True,samesite='strict')
    audit.record(DATA/'console.sqlite',now(),actor['username'] if actor else 'anonymous',actor['role'] if actor else '', 'sign_out','POST','/api/auth/logout',200,request.client.host if request.client else '',request.headers.get('user-agent',''),request.headers.get('sec-ch-ua-platform',''),request.headers.get('sec-ch-ua-mobile',''))
    return {'message':'Signed out.'}

@app.exception_handler(RequestValidationError)
async def validation_error(request,exc):
    if request.url.path.startswith('/api/auth/') or request.url.path.startswith('/api/users') or request.url.path.startswith('/api/ai/') or request.url.path.startswith('/api/profiles') or request.url.path.endswith('/credential') or request.url.path in {'/api/account/password','/api/keys','/api/oci-profiles','/api/wallets','/api/responses','/api/responses/build','/api/responses/content'}:
        # Pydantic normally includes rejected inputs in its errors. Never echo secrets.
        message='Enter the current password, a new password, and its confirmation as text.' if request.url.path=='/api/account/password' else 'Invalid credential form. Check the required fields and their types. Secret values are never included in validation errors.'
        return JSONResponse({'detail':message},status_code=422)
    return await request_validation_exception_handler(request,exc)

def ai_secure(request):
    if request.url.scheme!='https' and request.client.host not in {'127.0.0.1','::1'}:
        raise HTTPException(403,'Use HTTPS or a loopback SSH tunnel for the AI assistant.')

@app.get('/api/ai/settings')
def ai_settings():
    return assistant.load(DATA)

@app.put('/api/ai/settings')
def ai_save(body:assistant.Settings,request:Request):
    ai_secure(request)
    return assistant.save(DATA,body)

@app.post('/api/ai/test')
def ai_test(request:Request):
    ai_secure(request)
    return assistant.ask(DATA,None,test=True)

@app.post('/api/ai/chat')
def ai_chat(body:assistant.Question,request:Request):
    ai_secure(request)
    if (body.job_id is not None or body.include_log) and not body.context_consent:
        raise HTTPException(422,'Confirm sharing the selected redacted job context with your configured AI provider.')
    if body.include_log and body.job_id is None:
        raise HTTPException(422,'Select a job before including its result-log errors.')
    context=None
    if body.job_id is not None:
        record=job(body.job_id)['job']
        text=adapter.tail_log(record['result_path']) if body.include_log and record.get('result_path') else None
        context=assistant.job_context(record,text)
    return assistant.ask(DATA,body,context)

@app.get('/api/account')
def account(request:Request):
    user=request.state.user
    return {**user,'can_manage':user['role'] in {'manager','admin'},'can_admin':user['role']=='admin','minimum_password_length':12,'maximum_password_length':128}

@app.get('/api/audit/events')
def audit_events(request:Request,limit:int=200):
    if request.state.user['username'].lower()!='zdmadmin':
        raise HTTPException(403,'The audit log is available only to zdmadmin.')
    limit=max(1,min(limit,500))
    with db() as conn:
        rows=conn.execute('''SELECT id,created,username,role,event,method,path,status,client_ip,
          user_agent,client_platform,machine_details FROM audit_events ORDER BY id DESC LIMIT ?''',(limit,)).fetchall()
    return {'events':[dict(row) for row in rows]}

class UserCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=3,max_length=64,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    role:Literal['viewer','manager','admin']='viewer'
    password:SecretStr
    confirm_password:SecretStr

class UserUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role:Literal['viewer','manager','admin']
    enabled:bool
    confirmation:str

class UserReset(BaseModel):
    model_config=ConfigDict(extra='forbid')
    password:SecretStr
    confirm_password:SecretStr
    confirmation:str

@app.get('/api/users')
def users():
    with db() as conn:
        return {'users':[auth.public_user(r) for r in conn.execute('SELECT * FROM console_users ORDER BY username')],
                'events':[dict(r) for r in conn.execute('SELECT actor,event,subject,created FROM user_events ORDER BY id DESC LIMIT 100')]}

def user_password(body):
    password=body.password.get_secret_value()
    auth.password_valid(password)
    if not secrets.compare_digest(password.encode(),body.confirm_password.get_secret_value().encode()):raise HTTPException(422,'Password confirmation does not match.')
    return password

def user_secure(request):
    if request.url.scheme!='https' and request.client.host not in {'127.0.0.1','::1'}:raise HTTPException(403,'Use HTTPS or an SSH tunnel for user management.')

def user_event(conn,request,event,subject):
    conn.execute('INSERT INTO user_events(actor,event,subject,created) VALUES (?,?,?,?)',(request.state.user['username'],event,subject,now()))

@app.post('/api/users')
def create_user(body:UserCreate,request:Request):
    user_secure(request)
    password=user_password(body);username=body.username.lower();salt=secrets.token_hex(32);stamp=now()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM console_users WHERE username=?',(username,)).fetchone():raise HTTPException(409,'Username already exists.')
        conn.execute('INSERT INTO console_users VALUES (?,?,?,?,?,?,?,?)',(username,salt,auth.digest(password,salt),body.role,1,1,stamp,stamp))
        user_event(conn,request,'user_created:'+body.role,username)
    return {'username':username,'message':'User created. They must change their temporary password at first sign-in.'}

@app.put('/api/users/{username}')
def update_user(username:str,body:UserUpdate,request:Request):
    user_secure(request)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM console_users WHERE username=?',(username,)).fetchone()
        if not row:raise HTTPException(404,'User not found.')
        if body.confirmation!='UPDATE '+row['username']:raise HTTPException(422,'User update confirmation does not match.')
        if row['role']=='admin' and row['enabled'] and (body.role!='admin' or not body.enabled):
            if conn.execute("SELECT count(*) FROM console_users WHERE role='admin' AND enabled=1").fetchone()[0]<=1:raise HTTPException(409,'Keep at least one enabled Administrator account.')
        conn.execute('UPDATE console_users SET role=?,enabled=? WHERE username=?',(body.role,int(body.enabled),username))
        if not body.enabled:conn.execute('DELETE FROM console_sessions WHERE username=?',(row['username'],))
        user_event(conn,request,'user_updated:'+body.role+(' enabled' if body.enabled else ' disabled'),row['username'])
    return {'message':'User permissions updated. Changes apply to subsequent requests.'}

@app.post('/api/users/{username}/password')
def reset_user_password(username:str,body:UserReset,request:Request):
    user_secure(request);password=user_password(body)
    if username.lower()==request.state.user['username'].lower():raise HTTPException(422,'Use Change password to change your own password.')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM console_users WHERE username=?',(username,)).fetchone()
        if not row:raise HTTPException(404,'User not found.')
        if body.confirmation!='RESET '+row['username']:raise HTTPException(422,'Password reset confirmation does not match.')
        auth.set_password(conn,row['username'],password,now(),True)
        user_event(conn,request,'password_reset',row['username'])
    return {'message':'Temporary password set. The user must change it at next sign-in.'}

class PasswordChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    current_password:SecretStr
    new_password:SecretStr
    confirm_password:SecretStr

@app.post('/api/account/password')
def change_password(body:PasswordChange,request:Request,response:Response):
    current=body.current_password.get_secret_value()
    new=body.new_password.get_secret_value()
    confirmation=body.confirm_password.get_secret_value()
    if not 12<=len(new)<=128 or not new.strip():
        raise HTTPException(422,'The new password must contain 12–128 characters and cannot be only spaces.')
    if not secrets.compare_digest(new.encode(),confirmation.encode()):
        raise HTTPException(422,'The new passwords do not match.')
    if secrets.compare_digest(current.encode(),new.encode()):
        raise HTTPException(422,'Choose a password different from your current password.')
    with db() as conn:
        # Serialize concurrent changes and recheck the latest credential under the lock.
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM console_users WHERE username=?',(request.state.user['username'],)).fetchone()
        if not auth.matches(row,row['username'],current):
            raise HTTPException(403,'The current password is incorrect.')
        stamp=now()
        auth.set_password(conn,row['username'],new,stamp)
        user_event(conn,request,'password_changed',row['username'])
        conn.execute('INSERT INTO account_events(event,username,created) VALUES (?,?,?)',
                     ('password_changed',row['username'],stamp))
        if request.cookies.get(auth.COOKIE):auth.set_cookie(response,auth.new_session(conn,row['username']),request.url.scheme=='https')
    return {'username':row['username'],'message':'Password changed. Use your new password the next time you sign in.'}

@app.exception_handler(ValueError)
async def invalid(request,exc):
    return JSONResponse({'detail':str(exc)},status_code=422)

@app.exception_handler(RuntimeError)
async def cli_failure(request,exc):
    return JSONResponse({'detail':str(exc)},status_code=502)

@app.exception_handler(TimeoutError)
async def cli_timeout(request,exc):
    return JSONResponse({'detail':str(exc)},status_code=504)

def query_jobs(job_id=None,timeout=90,brief=False):
    args=['query','job'] + (['-jobid',str(job_id)] if job_id is not None else [])
    if brief:args.append('-brief')
    raw=adapter.run(args,timeout=timeout)
    jobs=adapter.parse_jobs(raw)
    if not jobs and 'No scheduled jobs' not in raw:
        raise RuntimeError('Unrecognized ZDM job output. '+raw[-4000:])
    return {'jobs':jobs,'raw':raw,'checked_at':now()}

def resume_job_snapshot(job_id):
    """Use ZDM's brief query, which omits long-running GoldenGate metrics."""
    key='resume-job:'+str(job_id)
    data=cached(key,15,lambda:query_jobs(job_id,timeout=20,brief=True))
    stamp,_=cached_value(key)
    return {**data,'snapshot_age_seconds':round(time.monotonic()-stamp)}

def modify_job_snapshot(job_id):
    """Use a brief query so GoldenGate metrics cannot delay modify controls."""
    key='modify-job:'+str(job_id)
    data=cached(key,15,lambda:query_jobs(job_id,timeout=20,brief=True))
    stamp,_=cached_value(key)
    return {**data,'snapshot_age_seconds':round(time.monotonic()-stamp)}

@app.get('/api/health')
def health():
    def load():
        status=adapter.run(['status'],service=True)
        detected=compatibility.current()
        version=detected['build_output']
        return {'running':bool(re.search(r'Running:\s*true',status,re.I)),'status':status,'version':version,
                'host':socket.gethostname(),'zdm_home':str(adapter.HOME),'zdm_base':str(adapter.BASE),
                'zdm_stage':str(adapter.STAGE),'service_user':adapter.OS_HOME.name,
                'default_key':str(adapter.BASE/'zdm/admin/id_rsa'),'checked_at':now(),
                'zdm_release':detected['release'],'zdm_full_version':detected['full_version'],
                'zdm_patch_version':detected['patch_version'],'zdm_supported':detected['supported'],
                'capabilities':detected['capabilities'],'compatibility_warnings':detected['warnings']}
    return cached('health',30,load)

@app.get('/api/jobs')
def jobs():
    return cached('jobs',10,query_jobs)

@app.get('/api/jobs/{job_id}')
def job(job_id: int):
    if job_id<1:
        raise HTTPException(422,'Invalid job ID')
    data=cached('job:'+str(job_id),10,lambda:query_jobs(job_id))
    if not data['jobs']:
        raise HTTPException(404,'Job not found')
    return {'job':data['jobs'][0],'raw':data['raw'],'checked_at':data['checked_at']}

@app.get('/api/jobs/{job_id}/log')
def log(job_id: int):
    data=job(job_id)
    path=data['job']['result_path']
    if not path:
        return {'text':'ZDM has not reported a result log path for this job yet.'}
    return {'text':adapter.tail_log(path)}

@app.get('/api/jobs/{job_id}/download/{artifact}')
def download_job_artifact(job_id:int,artifact:Literal['cli','result-log']):
    data=job(job_id)
    if artifact=='cli':
        return Response(content=data['raw'],media_type='text/plain; charset=utf-8',
                        headers={'Content-Disposition':f'attachment; filename="zdm-job-{job_id}-cli-output.txt"'})
    reported=data['job']['result_path']
    if not reported:
        raise HTTPException(404,'ZDM has not reported a result log path for this job yet.')
    path=Path(adapter.safe_path(reported,[adapter.BASE]))
    if path.suffix.lower() not in {'.log','.out','.trc'}:
        raise ValueError('ZDM result path is not a supported log file.')
    return FileResponse(path,media_type='text/plain',filename=f'zdm-job-{job_id}-result{path.suffix.lower()}')

class Profile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str=Field(min_length=1,max_length=80)
    source_host: str=''
    source_access:Literal['auto','response']='auto'
    source_identifier: str=''
    identifier_type: str='sourcedb'
    target_host: str=''
    source_user: str='opc'
    target_user: str='opc'
    source_key: str=Field(default_factory=lambda:str(adapter.BASE/'zdm/admin/id_rsa'))
    target_key: str=Field(default_factory=lambda:str(adapter.BASE/'zdm/admin/id_rsa'))
    rsp: str
    pause_after: str=''
    wallets: Dict[str,str]=Field(default_factory=dict)
    source_server_id:str=''
    target_server_id:str=''
    oci_profile_id:str=''
    source_ssh_port:int=Field(default=22,ge=1,le=65535)
    target_ssh_port:int=Field(default=22,ge=1,le=65535)
    wallet_ids:List[str]=Field(default_factory=list,max_length=32)
    migration_options:Dict[str,Union[str,bool,int,List[str]]]=Field(default_factory=dict)
    source_auth_mode:str='plugin'
    target_auth_mode:str='plugin'
    source_auth_plugin:str='zdmauth'
    target_auth_plugin:str='zdmauth'
    source_credential:str=''
    target_credential:str=''
    source_sudo_path:str='/usr/bin/sudo'
    target_sudo_path:str='/usr/bin/sudo'
    source_auth_args:Dict[str,str]=Field(default_factory=dict)
    target_auth_args:Dict[str,str]=Field(default_factory=dict)

def get_profile(pid):
    with db() as conn:
        row=conn.execute('SELECT * FROM profiles WHERE id=?',(pid,)).fetchone()
    if not row:
        raise HTTPException(404,'Profile not found')
    return json.loads(row['body'])

@app.get('/api/profiles')
def profiles():
    with db() as conn:
        rows=list(conn.execute('SELECT * FROM profiles ORDER BY updated DESC'))
    return [{'id':r['id'],**json.loads(r['body']),**migration.describe(json.loads(r['body'])),'updated':r['updated'],'connection_check':latest_preflight(r['id'],json.loads(r['body']))} for r in rows]

@app.post('/api/profiles')
def save_profile(body:Profile):
    profile=normalize_profile(body.model_dump())
    adapter.migration_args(resolve_profile(profile),True)
    profile=bind_profile_wallets(profile)
    pid=uuid.uuid4().hex
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        inventory.profile_links(conn,profile)
        conn.execute('INSERT INTO profiles VALUES (?,?,?)',(pid,json.dumps(profile),now()))
    return {'id':pid}

@app.put('/api/profiles/{pid}')
def update_profile(pid:str,body:Profile):
    previous=get_profile(pid)
    profile=normalize_profile(body.model_dump())
    adapter.migration_args(resolve_profile(profile),True)
    profile=bind_profile_wallets(profile,previous)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        inventory.profile_links(conn,profile)
        conn.execute('UPDATE profiles SET body=?,updated=? WHERE id=?',(json.dumps(profile),now(),pid))
    return {'id':pid}

@app.get('/api/profiles/{pid}/preview')
def preview(pid:str, evaluate:bool=True,mode:Optional[Literal['migrate','evaluate','advisor','summary','listphases']]=None):
    mode=mode or ('evaluate' if evaluate else 'migrate')
    return {'command':shlex.join([str(adapter.HOME/'bin/zdmcli'),*adapter.migration_args(resolve_profile(get_profile(pid)),evaluate,mode)]),'mode':mode}

@app.get('/api/migration-options')
def migration_options(rsp:Optional[str]=None):
    schema=migration.schema()
    if rsp is not None:
        info=migration.describe({'rsp':rsp})
        logical=info['migration_kind']=='logical'
        options=[{**row,'group':'Checks' if not logical and row['group']=='Advisor & checks' else row['group']} for row in schema['options'] if (row['name'] not in migration.CPAT_OPTIONS or logical) and not (info['autonomous_target'] and row['name']=='targethome')]
        schema={**schema,**info,'options':options}
    return schema

class ResponseFile(BaseModel):
    name:str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    content:str=Field(min_length=1,max_length=300000)
    oci_profile_id:str=''
    wallet_ids:List[str]=Field(default_factory=list,max_length=32)

@app.get('/api/responses')
def responses():
    return [{'name':p.name,'path':str(p),'template':response_files.template(p),'deletable':not p.is_symlink() and response_files.editable(p)} for p in sorted(adapter.STAGE.rglob('*.rsp')) if p.is_file() and adapter._is_relative_to(p.resolve(),adapter.STAGE.resolve())][:100]

def response_path(value):
    path=Path(adapter.safe_path(value,[adapter.STAGE]))
    if path.suffix.lower()!='.rsp':raise ValueError('Select a .rsp response file.')
    return path

def response_editable(path):
    return response_files.editable(path)

def response_bytes(path):
    with path.open('rb') as handle:content=handle.read(300001)
    if len(content)>300000:raise ValueError('Response file must be below 300 KB to edit in the GUI.')
    return content

@app.get('/api/responses/content')
def read_response(path:str):
    file=response_path(path)
    content=response_bytes(file)
    return {'path':str(file),'name':file.name,'content':content.decode('utf-8'),
            'revision':hashlib.sha256(content).hexdigest(),'editable':response_editable(file),'template':response_files.template(file)}

class ResponseDeletion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path:str=Field(max_length=4096)
    revision:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:str=Field(max_length=300)

class ResponseRestore(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmation:str=Field(max_length=300)

@app.post('/api/responses/delete')
def delete_response_file(body:ResponseDeletion):
    with SUBMIT_LOCK,RESPONSE_LOCK:
        with db() as conn:response_files.check_request(conn,body.path,body.revision,body.confirmation)
        current=query_jobs()  # Fresh repository state; never trust the browser cache.
        with db() as conn:result=response_files.delete(conn,body.path,body.revision,body.confirmation,current['raw'],now())
    clear_cache()
    return result

@app.get('/api/responses/deleted')
def deleted_response_files():
    with db() as conn:return response_files.deleted(conn)

@app.get('/api/responses/backups')
def response_backups():
    return response_files.backups()

@app.get('/api/responses/backups/{name}/download')
def download_response_backup(name:str):
    path=response_files.backup_file(name)
    original=path.name[33:-4]
    return FileResponse(path,media_type='text/plain',filename=original[:-4]+'.backup.rsp')

@app.post('/api/responses/deleted/{iid}/restore')
def restore_response_file(iid:str,body:ResponseRestore):
    with SUBMIT_LOCK,RESPONSE_LOCK:
        with db() as conn:result=response_files.restore(conn,iid,body.confirmation)
    return result

class ResponseUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path:str=Field(max_length=4096)
    content:str=Field(min_length=1,max_length=300000)
    oci_profile_id:str=''
    wallet_ids:List[str]=Field(default_factory=list,max_length=32)
    revision:str=Field(pattern=r'^[0-9a-f]{64}$')

def validate_response_content(content):
    if len(content.encode('utf-8'))>300000:raise ValueError('Response file must be below 300 KB.')
    if '\x00' in content:raise ValueError('Response file cannot contain NUL characters.')
    for line in content.splitlines():
        if line.lstrip().startswith('#') or '=' not in line:continue
        key,value=line.split('=',1)
        response_tools.no_plaintext_secrets({key.strip().upper():value.strip()})

@app.put('/api/responses/content')
def update_response(body:ResponseUpdate):
    content=bind_response_oci(body.content,body.oci_profile_id)
    content=bind_response_wallets(content,body.wallet_ids)
    validate_response_content(content)
    with RESPONSE_LOCK:
        path=response_path(body.path)
        if not response_editable(path):
            raise HTTPException(403,'Installed templates and files not owned by the ZDM service account must be saved as a new response file.')
        original=response_bytes(path)
        if not secrets.compare_digest(hashlib.sha256(original).hexdigest(),body.revision):
            raise HTTPException(409,'This response file changed since you opened it. Reopen it before saving, or save your edits as a new file.')
        backup=response_files.backup_root()/(uuid.uuid4().hex+'-'+path.name+'.bak')
        connections.write_private(backup,original)
        temporary=path.parent/('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            connections.write_private(temporary,content.encode('utf-8'))
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)
    return {'path':str(path),'name':path.name,'backup':str(backup),
            'revision':hashlib.sha256(content.encode('utf-8')).hexdigest()}

@app.get('/api/templates/{kind}')
def template(kind:str):
    if kind not in {'physical','logical'}:
        raise HTTPException(404,'Template not found')
    try:return {'content':response_tools.read_template(kind)}
    except OSError as exc:raise HTTPException(503,str(exc)) from None

@app.get('/api/response-schema/{kind}')
def response_schema(kind:str):
    try:return response_tools.catalog(kind)
    except OSError as exc:raise HTTPException(503,str(exc)) from None

class BuildResponse(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    kind:Literal['logical','physical']
    values:Dict[str,str]
    preview:bool=False
    oci_profile_id:str=''
    wallet_ids:List[str]=Field(default_factory=list,max_length=32)

@app.post('/api/responses/build')
def build_response(body:BuildResponse):
    if body.oci_profile_id and body.kind!='logical':raise ValueError('OCI API profile response parameters are supported only for logical migrations.')
    values={**body.values,**(response_oci_values(body.oci_profile_id)['values'] if body.oci_profile_id else {})}
    content=response_tools.render(body.kind,values)
    content=bind_response_oci(content,body.oci_profile_id)
    content=bind_response_wallets(content,body.wallet_ids)
    if body.preview:return {'content':content}
    result=response_file(ResponseFile(name=body.name,content=content))
    return {**result,'content':content}

@app.post('/api/responses')
def response_file(body:ResponseFile):
    content=bind_response_oci(body.content,body.oci_profile_id)
    content=bind_response_wallets(content,body.wallet_ids)
    validate_response_content(content)
    folder=adapter.STAGE/'gui-responses'
    folder.mkdir(mode=0o700,exist_ok=True)
    base_name=body.name[:-4] if body.name.lower().endswith('.rsp') else body.name
    path=folder/(base_name+'.rsp')
    try:
        with path.open('x') as handle:
            os.chmod(path,0o600)
            handle.write(content)
    except FileExistsError:
        raise ValueError('A response file named '+path.name+' already exists. Edit or delete the existing file, or choose another name.') from None
    return {'path':str(path),'name':path.name}

class Submission(BaseModel):
    profile_id:str
    evaluate:bool=True
    request_key:str=Field(min_length=16,max_length=100)
    confirmation:str=''
    preflight_id:str=''
    mode:Optional[Literal['migrate','evaluate','advisor','summary','listphases']]=None

def enqueue(key,kind,args,profile_id=None,profile_name=None,job_id=None):
    with SUBMIT_LOCK:
        with db() as conn:
            existing=conn.execute('SELECT id FROM operations WHERE request_key=?',(key,)).fetchone()
            if existing:
                return {'id':existing['id']}
            if conn.execute("SELECT id FROM operations WHERE state IN ('QUEUED','RUNNING')").fetchone():
                raise HTTPException(409,'Another ZDM command is still being submitted. Wait for its result.')
            oid=uuid.uuid4().hex
            stamp=now()
            conn.execute('INSERT INTO operations(id,request_key,kind,profile_id,profile_name,job_id,state,command,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (oid,key,kind,profile_id,profile_name,job_id,'QUEUED',shlex.join([str(adapter.HOME/'bin/zdmcli'),*args]),stamp,stamp))
        WORK.put((oid,args,kind,job_id))
    return {'id':oid}

@app.post('/api/submissions',status_code=202)
def submit(body:Submission):
    profile=get_profile(body.profile_id)
    mode=body.mode or ('evaluate' if body.evaluate else 'migrate')
    if mode=='migrate' and body.confirmation != 'START '+profile['name']:
        raise HTTPException(422,'Migration start confirmation does not match the profile name.')
    if mode=='migrate':
        with db() as conn:
            existing=conn.execute('SELECT id FROM operations WHERE request_key=?',(body.request_key,)).fetchone()
        if existing:
            return {'id':existing['id']}
        checked_signature=require_preflight(body.profile_id,profile,body.preflight_id)
    args=adapter.migration_args(resolve_profile(profile),body.evaluate,mode)
    if mode=='migrate' and checked_signature!=profile_signature(get_profile(body.profile_id)):
        raise HTTPException(409,'The migration configuration changed during submission. Refresh the profile and review its settings before retrying.')
    return enqueue(body.request_key,mode,args,body.profile_id,profile['name'])

class ResumePreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    resume_options:resume_tools.Options=Field(default_factory=resume_tools.Options)

@app.get('/api/jobs/{job_id}/resume-options')
def resume_options(job_id:int):
    if job_id<1:raise HTTPException(422,'Invalid job ID')
    data=resume_job_snapshot(job_id)
    return {**resume_tools.schema(data),'snapshot_checked_at':data['checked_at'],'snapshot_age_seconds':data['snapshot_age_seconds']}

@app.post('/api/jobs/{job_id}/resume/preview')
def resume_preview(job_id:int,body:ResumePreview):
    if job_id<1:raise HTTPException(422,'Invalid job ID')
    args,revision=resume_tools.build(resume_job_snapshot(job_id),body.resume_options)
    return {'command':shlex.join([str(adapter.HOME/'bin/zdmcli'),*args]),'revision':revision}

class ModifyPreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    modify_options:modify_tools.Options

class ModifySubmission(ModifyPreview):
    revision:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:str=Field(max_length=100)
    request_key:str=Field(min_length=16,max_length=100)

@app.get('/api/jobs/{job_id}/modify-options')
def modify_options(job_id:int):
    if job_id<1:raise HTTPException(422,'Invalid job ID')
    data=modify_job_snapshot(job_id)
    return {**modify_tools.schema(data),'snapshot_checked_at':data['checked_at'],'snapshot_age_seconds':data['snapshot_age_seconds']}

@app.post('/api/jobs/{job_id}/modify/preview')
def modify_preview(job_id:int,body:ModifyPreview):
    if job_id<1:raise HTTPException(422,'Invalid job ID')
    args,revision=modify_tools.build(modify_job_snapshot(job_id),body.modify_options)
    return {'command':shlex.join([str(adapter.HOME/'bin/zdmcli'),*args]),'revision':revision}

@app.post('/api/jobs/{job_id}/modify',status_code=202)
def modify_job(job_id:int,body:ModifySubmission):
    if job_id<1:raise HTTPException(422,'Invalid job ID')
    if body.confirmation!='MODIFY '+str(job_id):raise ValueError('Job modification confirmation does not match.')
    with db() as conn:
        existing=conn.execute('SELECT id FROM operations WHERE request_key=?',(body.request_key,)).fetchone()
    if existing:return {'id':existing['id']}
    data=query_jobs(job_id,timeout=20,brief=True)
    args,revision=modify_tools.build(data,body.modify_options)
    if body.revision!=revision:
        raise HTTPException(409,'The job or response file changed. Review the modify command again before submitting.')
    return enqueue(body.request_key,'modify',args,job_id=job_id)

class Action(BaseModel):
    model_config=ConfigDict(extra='forbid')
    resume_options:resume_tools.Options=Field(default_factory=resume_tools.Options)
    resume_revision:str=Field(default='',pattern=r'^(?:[0-9a-f]{64})?$')
    action:str
    confirmation:str
    request_key:str=Field(min_length=16,max_length=100)

@app.post('/api/jobs/{job_id}/actions',status_code=202)
def action(job_id:int,body:Action):
    allowed={'resume':{'PAUSED','FAILED','SUSPENDED'},'delete':{'FAILED','SUCCEEDED','ABORTED','TERMINATED','REVERTED'},'suspend':{'EXECUTING'},'abort':{'SCHEDULED','EXECUTING','PAUSED','SUSPENDED','SUSPENDING','FAILED'}}
    if body.action not in allowed:
        raise HTTPException(422,'Unsupported job action')
    if body.confirmation != body.action.upper()+' '+str(job_id):
        raise HTTPException(422,'Job action confirmation does not match.')
    with db() as conn:
        existing=conn.execute('SELECT id FROM operations WHERE request_key=?',(body.request_key,)).fetchone()
    if existing:
        return {'id':existing['id']}
    # A normal query of an online logical job can wait while ZDM gathers
    # GoldenGate metrics. The documented brief query still supplies fresh job
    # state and the scheduled response file without that metrics collection.
    data=query_jobs(job_id,timeout=20,brief=True) if body.action=='resume' else query_jobs(job_id)
    if not data['jobs'] or data['jobs'][0]['status'] not in allowed[body.action]:
        raise HTTPException(409,'This action is not available for the current ZDM job state.')
    if body.action=='resume':
        args,revision=resume_tools.build(data,body.resume_options)
        customized=any(body.resume_options.model_dump().values())
        if (customized or body.resume_revision) and body.resume_revision!=revision:
            raise HTTPException(409,'Resume settings or job metadata changed. Review the command again before submitting.')
        return enqueue(body.request_key,body.action,args,job_id=job_id)
    if any(body.resume_options.model_dump().values()) or body.resume_revision:raise ValueError('Resume options can only be used with the resume action.')
    # Delete uses plural -jobids even for a single job. Never select by status or force.
    option='-jobids' if body.action=='delete' else '-jobid'
    return enqueue(body.request_key,body.action,[body.action,'job',option,str(job_id)],job_id=job_id)

@app.get('/api/operations')
def operations():
    with db() as conn:
        return [{**dict(row),'credential_prompt':interactive.pending(row['id'])} for row in conn.execute('SELECT id,kind,profile_name,job_id,state,command,result,created,updated FROM operations ORDER BY created DESC LIMIT 100')]

class CredentialAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prompt_id:str
    value:SecretStr

@app.post('/api/operations/{oid}/credential')
def answer_credential(oid:str,body:CredentialAnswer,request:Request):
    if request.url.scheme!='https' and request.client.host not in {'127.0.0.1','::1'}:
        raise HTTPException(403,'Credential entry requires HTTPS or a loopback SSH tunnel.')
    interactive.answer(oid,body.prompt_id,body.value.get_secret_value())
    return {'ok':True,'message':'Credential delivered to this ZDM CLI prompt; it was not saved.'}

class InventoryConfirmation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmation:str=Field(max_length=200)

@app.get('/api/inventory/deleted')
def deleted_inventory():
    with db() as conn:return inventory.deleted(conn)

@app.post('/api/inventory/{kind}/{iid}/delete')
def delete_inventory(kind:str,iid:str,body:InventoryConfirmation):
    with SUBMIT_LOCK:
        with db() as conn:result=inventory.delete(conn,kind,iid,body.confirmation,now())
    clear_cache()
    return result

@app.post('/api/inventory/{kind}/{iid}/restore')
def restore_inventory(kind:str,iid:str,body:InventoryConfirmation):
    with SUBMIT_LOCK:
        with db() as conn:result=inventory.restore(conn,kind,iid,body.confirmation)
    clear_cache()
    return result

def key_record(kid,kind=None):
    with db() as conn:
        row=conn.execute('SELECT * FROM managed_keys WHERE id=?',(kid,)).fetchone()
    if not row or (kind and row['kind']!=kind):
        raise ValueError('Select a registered '+(kind or '')+' private key.')
    return dict(row)

class KeyImport(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    kind:Literal['ssh','oci']='ssh'
    private_key:Optional[SecretStr]=None
    existing_path:str=''

@app.get('/api/keys')
def keys():
    with db() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM managed_keys ORDER BY created DESC')]

@app.post('/api/keys')
def import_key(body:KeyImport,request:Request):
    if body.private_key and request.url.scheme!='https' and request.client.host not in {'127.0.0.1','::1'}:
        raise HTTPException(403,'Private-key file uploads require HTTPS or a loopback SSH tunnel. Existing server files can be registered over this connection.')
    if bool(body.private_key)==bool(body.existing_path):
        raise ValueError('Upload a private key or register an existing server path, not both.')
    kid=uuid.uuid4().hex
    if body.existing_path:
        path,info=connections.existing_key(body.existing_path,body.kind)
    else:
        info,normalized=connections.inspect_private(body.private_key.get_secret_value().encode(),body.kind)
        path=connections.vault()/body.kind/(kid+'.key')
        connections.write_private(path,normalized)
    with db() as conn:
        conn.execute('INSERT INTO managed_keys VALUES (?,?,?,?,?,?,?)',
                     (kid,body.name,body.kind,str(path),info['fingerprint'],info['public_key'],now()))
    return key_record(kid)

class ServerConnection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    role:Literal['source','target','both']='source'
    kind:Literal['ssh','database']='ssh'
    host:str
    os_user:str='opc'
    ssh_port:int=Field(default=22,ge=1,le=65535)
    ssh_key_id:str=''
    authentication_check:Literal['key','zdm']='key'
    database_host:str=''
    database_port:Optional[int]=Field(default=1521,ge=1,le=65535)

def validate_server(body):
    adapter.host(body['host'])
    if body['database_host']:
        adapter.host(body['database_host'])
    if body['kind']=='ssh':
        adapter.word(body['os_user'],'OS user')
        if body.get('authentication_check','key')=='key' or body['ssh_key_id']:key_record(body['ssh_key_id'],'ssh')
    elif not body['database_port']:
        raise ValueError('A database endpoint requires a listener port.')

def server_record(sid):
    with db() as conn:
        row=conn.execute('SELECT * FROM servers WHERE id=?',(sid,)).fetchone()
    if not row:
        raise ValueError('Saved server connection not found.')
    return {'id':sid,**json.loads(row['body']),'trusted_key':row['trusted_key'],
            'scan':json.loads(row['scan']) if row['scan'] else None,
            'last_check':json.loads(row['last_check']) if row['last_check'] else None,'updated':row['updated']}

@app.get('/api/connections')
def saved_connections():
    with db() as conn:
        ids=[r['id'] for r in conn.execute('SELECT id FROM servers ORDER BY updated DESC')]
    return [server_record(sid) for sid in ids]

@app.post('/api/connections')
def save_connection(body:ServerConnection):
    data=body.model_dump()
    validate_server(data)
    sid=uuid.uuid4().hex
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        inventory.require_link(conn,'keys',data.get('ssh_key_id'))
        conn.execute('INSERT INTO servers(id,body,updated) VALUES (?,?,?)',(sid,json.dumps(data),now()))
    return server_record(sid)

@app.put('/api/connections/{sid}')
def edit_connection(sid:str,body:ServerConnection):
    old=server_record(sid)
    data=body.model_dump()
    validate_server(data)
    trusted=old['trusted_key'] if data['host']==old['host'] and data['ssh_port']==old['ssh_port'] and data['kind']==old['kind'] else None
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        inventory.require_link(conn,'keys',data.get('ssh_key_id'))
        conn.execute('UPDATE servers SET body=?,trusted_key=?,scan=NULL,last_check=NULL,updated=? WHERE id=?',
                     (json.dumps(data),trusted,now(),sid))
    return server_record(sid)

@app.post('/api/connections/{sid}/host-key/scan')
def scan_connection_host(sid:str):
    server=server_record(sid)
    if server['kind']!='ssh':
        raise ValueError('Database-only endpoints do not use SSH host keys.')
    result={'keys':connections.scan_host(server),'created':time.time()}
    with db() as conn:
        conn.execute('UPDATE servers SET scan=? WHERE id=?',(json.dumps(result),sid))
    return result

class HostTrust(BaseModel):
    fingerprint:str
    confirmation:str

@app.post('/api/connections/{sid}/host-key/trust')
def trust_connection_host(sid:str,body:HostTrust):
    server=server_record(sid)
    scan=server['scan']
    if not scan or time.time()-scan['created']>300:
        raise ValueError('Scan the host key again; the previous scan has expired.')
    selected=next((key for key in scan['keys'] if key['fingerprint']==body.fingerprint),None)
    if not selected or body.confirmation!=body.fingerprint:
        raise ValueError('Confirm the exact host fingerprint after verifying it independently with the server administrator.')
    path=connections.known_hosts_path(sid)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex)
    connections.write_private(temporary,connections.host_line(server,selected['public_key']).encode())
    os.replace(temporary,path)
    with db() as conn:
        conn.execute('UPDATE servers SET trusted_key=?,last_check=NULL,updated=? WHERE id=?',(selected['public_key'],now(),sid))
    return {'fingerprint':selected['fingerprint']}

def perform_server_check(server):
    key=key_record(server['ssh_key_id'],'ssh')['path'] if server['kind']=='ssh' and server.get('ssh_key_id') else None
    result={**connections.check_server(server,key),'name':server['name'],'checked_at':now()}
    with db() as conn:
        conn.execute('UPDATE servers SET last_check=? WHERE id=?',(json.dumps(result),server['id']))
    return result

@app.post('/api/connections/{sid}/check')
def check_connection(sid:str):
    return perform_server_check(server_record(sid))

class OciProfile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    profile_name:str='ZDM'
    key_id:str
    user:str
    tenancy:str
    region:str='us-phoenix-1'
    fingerprint:str=''

def oci_record(oid):
    with db() as conn:
        row=conn.execute('SELECT * FROM oci_profiles WHERE id=?',(oid,)).fetchone()
    if not row:
        raise ValueError('OCI CLI profile not found.')
    return {'id':oid,**json.loads(row['body']),'last_check':json.loads(row['last_check']) if row['last_check'] else None,'created':row['created']}

@app.get('/api/oci-profiles')
def oci_profiles():
    with db() as conn:
        ids=[r['id'] for r in conn.execute('SELECT id FROM oci_profiles ORDER BY created DESC')]
    return [oci_record(oid) for oid in ids]

@app.post('/api/oci-profiles')
def save_oci_profile(body:OciProfile):
    data=body.model_dump()
    key=key_record(body.key_id,'oci')
    connections.validate_oci(data,key)
    oid=uuid.uuid4().hex
    config=connections.vault()/'oci-profiles'/oid/'config'
    connections.write_private(config,connections.oci_config(data,key).encode())
    data.update({'config_file':str(config),'key_file':key['path'],'fingerprint':key['fingerprint']})
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        inventory.require_link(conn,'keys',data.get('key_id'))
        conn.execute('INSERT INTO oci_profiles(id,body,created) VALUES (?,?,?)',(oid,json.dumps(data),now()))
    return oci_record(oid)

def perform_oci_check(profile):
    result={**connections.check_oci(profile),'name':profile['name'],'checked_at':now()}
    with db() as conn:
        conn.execute('UPDATE oci_profiles SET last_check=? WHERE id=?',(json.dumps(result),profile['id']))
    return result

@app.post('/api/oci-profiles/{oid}/check')
def check_oci_profile(oid:str):
    return perform_oci_check(oci_record(oid))

@app.get('/api/oci-profiles/{oid}/response-values')
def response_oci_values(oid:str):
    profile=oci_record(oid)
    key=key_record(profile['key_id'],'oci')
    adapter.safe_path(key['path'],[adapter.BASE,adapter.OS_HOME])
    return {'id':oid,'name':profile['name'],'values':response_tools.oci_values({**profile,'key_file':key['path'],'fingerprint':key['fingerprint']})}

def bind_response_oci(content,oid):
    if not oid:return content
    data=response_oci_values(oid)
    profile={'id':oid,**{field:data['values'][parameter] for field,parameter in response_tools.OCI_FIELDS.items()}}
    return response_tools.apply_oci(content,profile)

def bind_response_wallets(content,wallet_ids):
    if not wallet_ids:return content
    selected=[wallet_record(wid) for wid in wallet_ids]
    if migration.method_info(content)['migration_kind']!='logical' or any(not wallet['purpose'].startswith('WALLET_') for wallet in selected):
        raise ValueError('Database and GoldenGate wallet selections can be added to logical response files only.')
    return wallet_tools.render_response(content,wallet_tools.response_settings(selected))

class OciResponsePreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:str=Field(min_length=1,max_length=300000)
    oci_profile_id:str

@app.post('/api/responses/oci-preview')
def preview_response_oci(body:OciResponsePreview):
    content=bind_response_oci(body.content,body.oci_profile_id)
    validate_response_content(content)
    return {'content':content}

def profile_oci_id(profile):
    if profile.get('oci_profile_id'):return profile['oci_profile_id']
    path=adapter.safe_path(profile['rsp'],[adapter.STAGE,adapter.HOME])
    return response_tools.marked_oci_id(Path(path).read_text())

def normalize_profile(profile):
    info=migration.describe(profile)
    if info['migration_kind']=='logical' and profile.get('source_access')=='response':
        profile.update(source_server_id='',source_host='',source_key='',source_identifier='',identifier_type='none',source_auth_mode='none',source_credential='',source_auth_args={})
    if info['autonomous_target']:
        profile.update(target_server_id='',target_host='',target_auth_mode='none',target_credential='',target_auth_args={})
    if not profile.get('oci_profile_id'):profile['oci_profile_id']=profile_oci_id(profile)
    return profile

def resolve_profile(profile):
    resolved=normalize_profile(dict(profile))
    resolved['wallets']=dict(profile.get('wallets',{}))
    resolved['migration_options']=dict(profile.get('migration_options',{}))
    for wallet in selected_wallets(profile):
        option=wallet_tools.CATALOG[wallet['purpose']].get('cli_option')
        if option:resolved['wallets'][option]=str(wallet_tools.directory(wallet['path']))
        user_option=wallet_tools.CATALOG[wallet['purpose']].get('cli_username_option')
        if user_option:
            supplied=resolved['migration_options'].get(user_option)
            if supplied and supplied!=wallet['username']:raise ValueError('The selected wallet username conflicts with '+user_option+'.')
            resolved['migration_options'][user_option]=wallet['username']
    for side in ['source','target']:
        sid=resolved.get(side+'_server_id')
        if not sid:
            continue
        server=server_record(sid)
        if server['role'] not in {side,'both'}:
            raise ValueError('The saved connection is not enabled for the '+side+' role.')
        if server['kind']!='ssh':resolved[side+'_auth_mode']='none'
        resolved[side+'_host']=server['host'] if server['kind']=='ssh' else ''
        resolved[side+'_user']=server['os_user']
        resolved[side+'_ssh_port']=server['ssh_port']
        if server['kind']=='ssh':
            if server.get('ssh_key_id'):resolved[side+'_key']=key_record(server['ssh_key_id'],'ssh')['path']
            elif resolved.get(side+'_auth_mode','plugin')=='plugin' and resolved.get(side+'_auth_plugin','zdmauth') in {'zdmauth','dbuser','sudoasuser'}:
                raise ValueError('Select an SSH key for the '+side+' connection, or choose root, credential, user, sudo, or a custom authentication plugin.')
    if resolved.get('oci_profile_id'):
        oci_record(resolved['oci_profile_id'])
    return resolved

def profile_signature(profile):
    resolved=resolve_profile(profile)
    material={'profile':resolved,'servers':[],'files':{}}
    files=[resolved['rsp'],str(host_records.HOSTS),'/etc/resolv.conf']
    for key,value in resolved.get('migration_options',{}).items():
        name=migration.ALIASES.get(key,key)
        if value and migration.BY_NAME.get(name,{}).get('type')=='wallet':files.append(str(wallet_tools.directory(value)/'cwallet.sso'))
    for key,value in resolved.get('wallets',{}).items():
        if value:files.append(str(wallet_tools.directory(value)/'cwallet.sso'))
    for side in ['source','target']:
        credential=resolved.get(side+'_credential','')
        if side=='target' and credential.startswith('/'):
            files.append(str(wallet_tools.directory(credential)/'cwallet.sso'))
        if resolved.get(side+'_auth_mode','plugin')=='plugin' and resolved.get(side+'_host'):
            if resolved.get(side+'_key'):files.append(resolved[side+'_key'])
            for value in resolved.get(side+'_auth_args',{}).values():
                match=re.match(r'^identity_file[:=](.+)$',value)
                if match:files.append(match[1])
    files += [str(wallet_tools.directory(w['path'])/'cwallet.sso') for w in selected_wallets(profile)]
    for side in ['source','target']:
        if resolved.get(side+'_server_id'):
            server=server_record(resolved[side+'_server_id'])
            material['servers'].append({k:server.get(k) for k in ['id','host','ssh_port','os_user','ssh_key_id','authentication_check','database_host','database_port','trusted_key','updated','kind']})
            if server['kind']=='ssh':
                if server.get('ssh_key_id'):files.append(key_record(server['ssh_key_id'],'ssh')['path'])
                files.append(str(connections.known_hosts_path(server['id'])))
    if resolved.get('oci_profile_id'):
        oci=oci_record(resolved['oci_profile_id'])
        files += [oci['config_file'],oci['key_file']]
    for filename in files:
        file=Path(filename)
        material['files'][filename]={'digest':hashlib.sha256(file.read_bytes()).hexdigest(),'mode':file.stat().st_mode&0o777,'owner':file.stat().st_uid} if file.is_file() else 'MISSING'
    return hashlib.sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()

def latest_preflight(pid,profile):
    with db() as conn:
        row=conn.execute('SELECT * FROM preflights WHERE profile_id=? ORDER BY created DESC LIMIT 1',(pid,)).fetchone()
    if not row:
        return None
    result=json.loads(row['result'])
    try:
        valid=result['ok'] and time.time()-row['created']<=900 and row['signature']==profile_signature(profile)
    except (ValueError,OSError):
        valid=False
    return {'id':row['id'],**result,'valid':valid,'signature':row['signature']}

@app.post('/api/profiles/{pid}/connections/check')
def check_profile_connections(pid:str):
    profile=get_profile(pid)
    resolved=resolve_profile(profile)
    info=migration.describe(profile);autonomous=info['autonomous_target'];logical=info['migration_kind']=='logical'
    if not logical and (not resolved.get('source_server_id') or (not autonomous and not resolved.get('target_server_id'))):
        raise ValueError('Select a saved source connection and, for non-Autonomous targets, a saved target connection.')
    signature=profile_signature(profile)
    def skipped(name):return {'ok':True,'name':name,'steps':[{'name':'Response-file connectivity','status':'SKIP','detail':'No saved OS connection selected. ZDM Evaluate validates the database connectivity and credentials in the response file.','duration_ms':0}],'checked_at':now()}
    tasks={'source':lambda:perform_server_check(server_record(resolved['source_server_id'])) if resolved.get('source_server_id') else skipped('Source database (response file)')}
    if autonomous:
        oid=resolved.get('oci_profile_id')
        if not oid:
            tasks['target']=lambda:skipped('Autonomous target (response file)')
        else:
            content=Path(resolved['rsp']).read_text();values=response_tools.active_values(content)
            if any(values.get(k)!=v for k,v in response_oci_values(oid)['values'].items()):raise ValueError('Apply the selected OCI profile to this response file before checking the Autonomous target.')
            target=migration.target_info(content)
            tasks['target']=lambda:{**connections.check_autonomous(oci_record(oid),target['target_ocid']),'name':'Autonomous Database target','checked_at':now()}
    else:
        tasks['target']=lambda:perform_server_check(server_record(resolved['target_server_id'])) if resolved.get('target_server_id') else skipped('Target database (response file)')
        if resolved.get('oci_profile_id'):tasks['oci']=lambda:perform_oci_check(oci_record(resolved['oci_profile_id']))
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={name:pool.submit(fn) for name,fn in tasks.items()}
        results={name:future.result() for name,future in futures.items()}
    result={'ok':all(item['ok'] for item in results.values()),'results':results,'advisory':logical,'checked_at':now()}
    # A configuration change during checks invalidates their result.
    result['ok']=result['ok'] and signature==profile_signature(get_profile(pid))
    check_id=uuid.uuid4().hex
    with db() as conn:
        conn.execute('INSERT INTO preflights VALUES (?,?,?,?,?)',(check_id,pid,signature,json.dumps(result),time.time()))
    return {'id':check_id,**result,'valid':result['ok']}

def require_preflight(pid,profile,check_id):
    if migration.describe(profile)['migration_kind']=='logical':return profile_signature(profile)
    if not profile.get('source_server_id') or (not migration.describe(profile)['autonomous_target'] and not profile.get('target_server_id')):
        raise HTTPException(409,'Select the required saved connections and run connection checks. Autonomous targets do not require a target host.')
    check=latest_preflight(pid,profile)
    if not check or not check['valid'] or check['id']!=check_id:
        raise HTTPException(409,'Run successful connection checks for this profile before starting. Checks expire after 15 minutes or any configuration/key change.')
    return check['signature']

def wallet_record(wid):
    with db() as conn:
        row=conn.execute('SELECT * FROM credential_wallets WHERE id=?',(wid,)).fetchone()
    if not row:raise ValueError('Saved credential wallet not found.')
    info=wallet_tools.CATALOG[row['purpose']]
    return {**dict(row),'label':info['label'],'parameter':'-'+info['cli_option'] if info.get('cli_option') else info.get('response_parameter',row['purpose']),'last_check':json.loads(row['last_check'])}

@app.get('/api/wallets')
def list_wallets():
    with db() as conn:
        ids=[row['id'] for row in conn.execute('SELECT id FROM credential_wallets ORDER BY created DESC')]
    return [wallet_record(wid) for wid in ids]

@app.get('/api/wallets/catalog')
def wallet_catalog():
    return {'purposes':[{'id':key,**value} for key,value in wallet_tools.available_catalog().items()],
            'documentation':wallet_tools.documentation()}

class WalletCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    purpose:str
    username:str
    password:SecretStr
    confirm_password:SecretStr

@app.post('/api/wallets')
def create_wallet(body:WalletCreate,request:Request):
    if request.url.scheme!='https' and request.client.host not in {'127.0.0.1','::1'}:
        raise HTTPException(403,'Wallet credential entry requires HTTPS or a loopback SSH tunnel.')
    password=body.password.get_secret_value()
    if not secrets.compare_digest(password.encode(),body.confirm_password.get_secret_value().encode()):
        raise ValueError('The credential password confirmation does not match.')
    wallet_tools.validate_input(body.purpose,body.username,password)
    wid=uuid.uuid4().hex
    path=connections.vault()/'wallets'/wid
    wallet_tools.create(path,body.purpose,body.username,password)
    check={'ok':True,'alias':'store','checked_at':now(),'message':'Oracle wallet created and its store credential verified. ZDM evaluation validates the actual service login.'}
    with db() as conn:
        conn.execute('INSERT INTO credential_wallets VALUES (?,?,?,?,?,?,?)',
                     (wid,body.name,body.purpose,body.username,str(path),now(),json.dumps(check)))
    return wallet_record(wid)

@app.post('/api/wallets/{wid}/verify')
def verify_wallet(wid:str):
    wallet=wallet_record(wid)
    check={**wallet_tools.verify(wallet['path'],wallet['username']),'checked_at':now()}
    with db() as conn:
        conn.execute('UPDATE credential_wallets SET last_check=? WHERE id=?',(json.dumps(check),wid))
    return check

class GoldenGateRestCheck(BaseModel):
    model_config=ConfigDict(extra='forbid')
    endpoint:str=Field(min_length=1,max_length=512)
    username:str=Field(min_length=1,max_length=128)
    password:SecretStr
    allow_self_signed:bool=True

@app.post('/api/goldengate/rest/check')
def check_goldengate_rest(body:GoldenGateRestCheck,request:Request):
    if request.url.scheme!='https' and request.client.host not in {'127.0.0.1','::1'}:
        raise HTTPException(403,'GoldenGate credential entry requires HTTPS or a loopback SSH tunnel.')
    result=connections.check_gg_rest(body.endpoint,body.username,body.password.get_secret_value(),body.allow_self_signed)
    return {**result,'checked_at':now()}

def selected_wallets(profile):
    selected=[wallet_record(wid) for wid in profile.get('wallet_ids',[])]
    wallet_tools.response_settings(selected)  # rejects duplicate purposes/conflicting usernames
    return selected

def bind_profile_wallets(profile,previous=None):
    selected=selected_wallets(profile)
    settings=wallet_tools.response_settings(selected)
    old=selected_wallets(previous or {})
    remove={key for key in wallet_tools.response_settings(old) if key.startswith('WALLET_')}-set(settings)
    if not selected and not remove:return profile
    source=Path(adapter.safe_path(profile['rsp'],[adapter.STAGE,adapter.HOME]))
    content=source.read_text()
    method=re.search(r'^\s*MIGRATION_METHOD\s*=\s*(\w+)',content,re.M)
    if selected and not method:
        raise ValueError('Set MIGRATION_METHOD in the response file before assigning credential wallets.')
    logical=method and method[1] in {'ONLINE_LOGICAL','OFFLINE_LOGICAL'}
    if any(w['purpose'].startswith('WALLET_') for w in selected) and not logical:
        raise ValueError('These database and GoldenGate wallet selections are for logical migrations. Choose the physical source SYS wallet for a physical migration.')
    if logical and any(w['purpose']=='SOURCE_SYS_PHYSICAL' or w['purpose'].startswith('PHYSICAL_') for w in selected):
        raise ValueError('Physical migration wallets require a physical migration response file.')
    if not settings and not remove:return profile
    rendered=wallet_tools.render_response(content,settings,remove)
    if rendered==content:return profile
    folder=adapter.STAGE/'gui-responses'
    folder.mkdir(mode=0o700,exist_ok=True)
    target=folder/(uuid.uuid4().hex+'-wallets.rsp')
    connections.write_private(target,rendered.encode())
    return {**profile,'rsp':str(target)}

@app.get('/api/name-resolution')
def name_resolution():
    content=host_records.HOSTS.read_bytes()
    if len(content)>host_records.LIMIT:raise ValueError('The hosts file is too large for GUI management.')
    read_only=os.environ.get('ZDM_GUI_HOSTS_READ_ONLY')=='1'
    return {**host_records.snapshot(content.decode('utf-8')),'helper_available':not read_only and host_records.SOCKET.exists(),'read_only':read_only,
            'message':'Checks run from the ZDM host. DNS configuration stays with the network administrator; managed entries provide a local hosts-file mapping.'}

class HostEntry(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ip:str=Field(max_length=64)
    names:List[str]=Field(min_length=1,max_length=20)

class HostSave(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:str=Field(pattern=r'^[0-9a-f]{64}$')
    entries:List[HostEntry]=Field(max_length=200)

@app.put('/api/name-resolution')
def save_name_resolution(body:HostSave):
    if os.environ.get('ZDM_GUI_HOSTS_READ_ONLY')=='1':
        raise HTTPException(403,'Hostname entries are read-only in user installation mode. Ask the system administrator to update DNS or /etc/hosts.')
    current=name_resolution()
    entries=host_records.validate([row.model_dump() for row in body.entries],current['protected_entries'])
    payload=json.dumps({'op':'apply','revision':body.revision,'entries':entries}).encode()+b'\n'
    if len(payload)>host_records.LIMIT:raise ValueError('The hosts-file update is too large.')
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(12)
            connection.connect(str(host_records.SOCKET));connection.sendall(payload)
            response=bytearray()
            while not response.endswith(b'\n'):
                chunk=connection.recv(4096)
                if not chunk or len(response)+len(chunk)>host_records.LIMIT:raise OSError('Invalid helper response')
                response.extend(chunk)
        result=json.loads(response)
    except (OSError,ValueError):
        raise HTTPException(503,'The hostname-management helper is unavailable. Check zdm-hosts-manager.service on the ZDM host.') from None
    if not result.get('ok'):
        raise HTTPException(409 if result.get('conflict') else 422,result.get('message','The hosts-file update failed.'))
    clear_cache()
    return {**result,**name_resolution(),'message':'Managed hostname entries saved. Run the resolution check and ZDM Evaluate again.'}

class HostCheck(BaseModel):
    model_config=ConfigDict(extra='forbid')
    names:List[str]=Field(min_length=1,max_length=50)

@app.post('/api/name-resolution/check')
def check_name_resolution(body:HostCheck):
    names=list(dict.fromkeys(host_records.hostname(name) for name in body.names))
    current=name_resolution()
    rows=current['protected_entries']+current['entries']
    def lookup(name,dns=False):
        try:
            code,output=connections.command(['/usr/bin/getent',*(['-s','dns'] if dns else []),'ahosts',name],timeout=5)
            addresses=[]
            for line in output.splitlines():
                try:address=str(ipaddress.ip_address(line.split()[0]))
                except (ValueError,IndexError):continue
                if address not in addresses:addresses.append(address)
            return addresses if code==0 else []
        except ValueError:return []
    def check(name):
        system=lookup(name);dns=lookup(name,True)
        hosts=list(dict.fromkeys(row['ip'] for row in rows if name in [n.lower().rstrip('.') for n in row['names']]))
        source='Unresolved' if not system else '/etc/hosts' if set(system)&set(hosts) else 'DNS' if set(system)&set(dns) else 'System resolver'
        return {'name':name,'ok':bool(system),'addresses':system,'dns_addresses':dns,'hosts_addresses':hosts,'source':source,
                'message':'Resolvable from the ZDM host.' if system else 'Not resolvable from the ZDM host. Add a correct IP mapping or have DNS updated.'}
    with ThreadPoolExecutor(max_workers=min(8,len(names))) as pool:
        results=list(pool.map(check,names))
    return {'ok':all(row['ok'] for row in results),'results':results,'checked_at':now(),
            'scope':'Hostname resolution only. This does not verify SSH authentication, database access, cluster interconnects, or server-to-server routing.'}

app.mount('/',StaticFiles(directory=ROOT/'frontend/dist',html=True),name='frontend')
