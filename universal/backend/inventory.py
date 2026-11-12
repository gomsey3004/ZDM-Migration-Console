"""Recoverable inventory deletion. Credential files are never removed."""
import json
from pathlib import Path
from fastapi import HTTPException

TABLES={'wallets':'credential_wallets','connections':'servers','keys':'managed_keys','oci-profiles':'oci_profiles'}

def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS deleted_inventory (
      kind TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL,record TEXT NOT NULL,
      deleted_at TEXT NOT NULL,PRIMARY KEY(kind,id))''')

def table(kind):
    if kind not in TABLES:raise HTTPException(404,'Unknown inventory type.')
    return TABLES[kind]

def name(record):
    return json.loads(record['body'])['name'] if 'body' in record else record['name']

def references(conn,kind,iid,row):
    hits=[]
    body=json.loads(row['body']) if 'body' in row else row
    path=body.get('path') or body.get('config_file')
    for record in conn.execute('SELECT body FROM profiles'):
        p=json.loads(record['body'])
        used=(kind=='wallets' and iid in p.get('wallet_ids',[])) or (kind=='connections' and iid in [p.get('source_server_id'),p.get('target_server_id')]) or (kind=='oci-profiles' and iid==p.get('oci_profile_id'))
        # Also detect manually entered paths in profile fields/authentication options.
        if path and path in json.dumps(p):used=True
        if used:hits.append('migration profile '+p.get('name','Unnamed'))
    if kind=='keys':
        for record in conn.execute('SELECT body FROM servers'):
            p=json.loads(record['body'])
            if p.get('ssh_key_id')==iid:hits.append('connection '+p['name'])
        for record in conn.execute('SELECT body FROM oci_profiles'):
            p=json.loads(record['body'])
            if p.get('key_id')==iid or (path and p.get('key_file')==path):hits.append('OCI profile '+p['name'])
    return hits

def delete(conn,kind,iid,confirmation,stamp):
    target=table(kind)
    conn.execute('BEGIN IMMEDIATE')
    if conn.execute("SELECT 1 FROM operations WHERE state IN ('QUEUED','RUNNING')").fetchone():
        raise HTTPException(409,'Wait for the pending GUI command to finish before deleting inventory.')
    record=conn.execute('SELECT * FROM '+target+' WHERE id=?',(iid,)).fetchone()
    if not record:raise HTTPException(404,'Item not found. Refresh the page.')
    record=dict(record);label=name(record)
    if confirmation!='DELETE '+label:raise HTTPException(422,'Type DELETE followed by the exact item name.')
    used=references(conn,kind,iid,record)
    if used:raise HTTPException(409,'This item is still used by: '+', '.join(used[:10])+'. Edit those references before deleting it.')
    conn.execute('INSERT INTO deleted_inventory VALUES (?,?,?,?,?)',(kind,iid,label,json.dumps(record),stamp))
    conn.execute('DELETE FROM '+target+' WHERE id=?',(iid,))
    return {'message':'Deleted from the active list. Restore it from Deleted items. Credential files are retained on the server.'}

def deleted(conn):
    return [dict(r) for r in conn.execute('SELECT kind,id,name,deleted_at FROM deleted_inventory ORDER BY deleted_at DESC')]

def restore(conn,kind,iid,confirmation):
    target=table(kind)
    conn.execute('BEGIN IMMEDIATE')
    if conn.execute("SELECT 1 FROM operations WHERE state IN ('QUEUED','RUNNING')").fetchone():
        raise HTTPException(409,'Wait for the pending GUI command to finish before restoring inventory.')
    archived=conn.execute('SELECT * FROM deleted_inventory WHERE kind=? AND id=?',(kind,iid)).fetchone()
    if not archived:raise HTTPException(404,'Deleted item not found. Refresh the list.')
    if confirmation!='RESTORE '+archived['name']:raise HTTPException(422,'Type RESTORE followed by the exact item name.')
    row=json.loads(archived['record'])
    if conn.execute('SELECT 1 FROM '+target+' WHERE id=?',(iid,)).fetchone():raise HTTPException(409,'An active item already has this ID.')
    if kind in {'connections','oci-profiles'}:
        body=json.loads(row['body']);kid=body.get('ssh_key_id') if kind=='connections' else body.get('key_id')
        if kid and not conn.execute('SELECT 1 FROM managed_keys WHERE id=?',(kid,)).fetchone():raise HTTPException(409,'Restore the referenced private key first.')
    if kind=='keys' and not Path(row['path']).is_file():raise HTTPException(409,'The original private-key file is missing. Import the key again.')
    if kind=='wallets' and not (Path(row['path'])/'cwallet.sso').is_file():raise HTTPException(409,'The original wallet file is missing. Recreate the wallet.')
    if kind=='oci-profiles' and not Path(json.loads(row['body'])['config_file']).is_file():raise HTTPException(409,'The original OCI configuration file is missing. Recreate the profile.')
    # Restored connections must be checked again before migration start.
    if kind in {'connections','oci-profiles'}:row['last_check']=None
    columns=list(row)
    conn.execute('INSERT INTO '+target+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row[c] for c in columns))
    conn.execute('DELETE FROM deleted_inventory WHERE kind=? AND id=?',(kind,iid))
    conn.execute('DELETE FROM preflights')
    return {'message':'Item restored. Run connection checks and ZDM Evaluate before using it.'}

def require_link(conn,target,iid):
    if iid and not conn.execute('SELECT 1 FROM '+table(target)+' WHERE id=?',(iid,)).fetchone():
        raise HTTPException(409,'A selected inventory item was removed. Refresh or restore it before saving.')

def profile_links(conn,profile):
    if not Path(profile.get('rsp','')).is_file():raise HTTPException(409,'The response file was removed. Refresh or restore it before saving this profile.')
    # Recheck links inside the write transaction to avoid saving a reference
    # after another request has archived its inventory item.
    for field in ['source_server_id','target_server_id']:require_link(conn,'connections',profile.get(field))
    require_link(conn,'oci-profiles',profile.get('oci_profile_id'))
    for iid in profile.get('wallet_ids',[]):require_link(conn,'wallets',iid)
