"""Protected templates and recoverable response-file deletion."""
import hashlib
import os
import re
import shlex
import uuid
from pathlib import Path
from fastapi import HTTPException
from . import adapter

TERMINAL={'SUCCEEDED','ABORTED','TERMINATED','REVERTED'}

def checksum(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(131072),b''):digest.update(chunk)
    return digest.hexdigest()

def _is_relative_to(path,root):
    try:path.relative_to(root);return True
    except ValueError:return False

def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS deleted_responses (
      id TEXT PRIMARY KEY,path TEXT NOT NULL,name TEXT NOT NULL,
      revision TEXT NOT NULL,deleted_at TEXT NOT NULL)''')

def template(path):
    path=Path(path).resolve();stage=adapter.STAGE.resolve()
    if _is_relative_to(path,stage) and any(part.lower().startswith(('zdm21.4','zdm21.5','zdm26.1')) for part in path.relative_to(stage).parts):return True
    if _is_relative_to(path,stage) and any(p.lower() in {'template','templates'} for p in path.relative_to(stage).parts[:-1]):return True
    return bool(re.fullmatch(r'zdm_(?:logical_|physical_)?template(?:_.*)?\.rsp',path.name,re.I))

def editable(path):
    path=Path(path)
    return not template(path) and path.stat().st_uid==os.geteuid() and os.access(path.parent,os.W_OK)

def validated_path(value,exists=True):
    path=Path(adapter.safe_path(value,[adapter.STAGE],exists=exists))
    if path.suffix.lower()!='.rsp':raise ValueError('Select a .rsp response file.')
    return path

def backup_root():
    root=adapter.STAGE/'gui-response-backups'
    root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or not _is_relative_to(root.resolve(),adapter.STAGE.resolve()):
        raise ValueError('Response backup directory is not safe.')
    return root

def backup_file(name):
    if not isinstance(name,str) or not re.fullmatch(r'[0-9a-f]{32}-[A-Za-z0-9][A-Za-z0-9_.-]{0,250}\.rsp\.bak',name):
        raise ValueError('Invalid response backup identifier.')
    path=backup_root()/name
    if not path.is_file() or path.is_symlink():raise HTTPException(404,'Response backup not found.')
    return path

def backups():
    result=[]
    for path in backup_root().iterdir():
        if not path.is_file() or path.is_symlink():continue
        try:path=backup_file(path.name)
        except (ValueError,HTTPException):continue
        info=path.stat();original=path.name[33:-4]
        result.append({'id':path.name,'original_name':original,'created':info.st_mtime,'size':info.st_size})
    return sorted(result,key=lambda item:item['created'],reverse=True)[:200]

def references(conn,path):
    import json
    used=[]
    for row in conn.execute('SELECT body FROM profiles'):
        profile=json.loads(row['body'])
        if profile.get('rsp') and Path(profile['rsp']).resolve()==path:
            used.append(profile.get('name','Unnamed profile'))
    return used

def check_request(conn,value,revision,confirmation):
    if Path(value).is_symlink():raise HTTPException(403,'Symlink response files cannot be deleted through the GUI.')
    path=validated_path(value)
    if not editable(path):raise HTTPException(403,'Template and protected response files cannot be deleted.')
    if confirmation!='DELETE '+path.name:raise HTTPException(422,'Type DELETE followed by the exact response filename.')
    if conn.execute("SELECT 1 FROM operations WHERE state IN ('QUEUED','RUNNING')").fetchone():raise HTTPException(409,'Wait for the pending GUI command to finish before deleting a response file.')
    if checksum(path)!=revision:raise HTTPException(409,'This response file changed. Reopen the deletion dialog before continuing.')
    used=references(conn,path)
    if used:raise HTTPException(409,'This response file is used by saved profiles: '+', '.join(used[:10])+'. Select a different response file in those profiles first.')
    return path

def check_jobs(raw,path):
    for chunk in re.split(r'(?m)^\s*Job ID\s*:\s*',raw)[1:]:
        jobs=adapter.parse_jobs('Job ID: '+chunk)
        if not jobs or jobs[0]['status'] in TERMINAL:continue
        job=jobs[0];match=re.search(r'^\s*Scheduled job command\s*:\s*(.+)$',chunk,re.M)
        if not match:
            if job['type'] in {'MIGRATE','EVAL'}:raise HTTPException(409,'Cannot verify the response file used by resumable job #'+str(job['id'])+'. Review that job before deleting files.')
            continue
        try:args=shlex.split(match[1].strip().strip('"'))
        except ValueError:raise HTTPException(409,'Cannot safely parse the response reference for job #'+str(job['id'])+'.') from None
        for i,arg in enumerate(args[:-1]):
            if arg!='-rsp':continue
            candidate=Path(args[i+1])
            if not candidate.is_absolute() or candidate.resolve()==path:
                raise HTTPException(409,'This file may be needed by active or resumable job #'+str(job['id'])+' ('+job['status']+'). Finish, abort or delete that job first.')

def trash_path(iid):
    root=adapter.STAGE/'gui-response-trash'
    root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or not _is_relative_to(root.resolve(),adapter.STAGE.resolve()):raise ValueError('Response trash directory is not safe.')
    return root/(iid+'.deleted')

def delete(conn,value,revision,confirmation,raw_jobs,stamp):
    conn.execute('BEGIN IMMEDIATE')
    path=check_request(conn,value,revision,confirmation);check_jobs(raw_jobs,path)
    iid=uuid.uuid4().hex;archived=trash_path(iid)
    os.replace(path,archived)
    try:
        archived.chmod(0o600)
        conn.execute('INSERT INTO deleted_responses VALUES (?,?,?,?,?)',(iid,str(path),path.name,revision,stamp))
        conn.commit()
    except Exception:
        conn.rollback()
        if not path.exists():os.replace(archived,path)
        raise
    return {'message':'Response file moved to Deleted response files. It can be restored.','id':iid}

def deleted(conn):
    return [dict(row) for row in conn.execute('SELECT id,path,name,deleted_at FROM deleted_responses ORDER BY deleted_at DESC')]

def restore(conn,iid,confirmation):
    conn.execute('BEGIN IMMEDIATE')
    row=conn.execute('SELECT * FROM deleted_responses WHERE id=?',(iid,)).fetchone()
    if not row:raise HTTPException(404,'Deleted response file not found.')
    if confirmation!='RESTORE '+row['name']:raise HTTPException(422,'Type RESTORE followed by the exact response filename.')
    if conn.execute("SELECT 1 FROM operations WHERE state IN ('QUEUED','RUNNING')").fetchone():raise HTTPException(409,'Wait for the pending GUI command to finish before restoring a response file.')
    path=validated_path(row['path'],exists=False)
    if path.exists() or path.is_symlink():raise HTTPException(409,'A file already exists at the original path. Restore would overwrite it.')
    if template(path) or not path.parent.is_dir():raise HTTPException(403,'The original location is protected or unavailable.')
    archived=trash_path(iid)
    if not archived.is_file() or archived.is_symlink():raise HTTPException(409,'The archived response file is unavailable.')
    if checksum(archived)!=row['revision']:raise HTTPException(409,'The archived file changed. Review it on the server before restoring.')
    os.replace(archived,path)
    try:
        path.chmod(0o600)
        conn.execute('DELETE FROM deleted_responses WHERE id=?',(iid,));conn.commit()
    except Exception:
        conn.rollback();os.replace(path,archived);raise
    return {'message':'Response file restored. Run connection checks and Evaluate before using it.','path':str(path)}
