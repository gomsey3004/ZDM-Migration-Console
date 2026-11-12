"""Validated hosts-file records and a fixed-purpose privileged Unix-socket helper.

The deployed helper is a standalone, root-owned copy of this stdlib-only file.
It accepts no file paths or commands from clients and can edit only its own block.
"""
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import socket
import stat
import struct
import tempfile
import uuid
from datetime import datetime,timezone
from pathlib import Path

BEGIN='# BEGIN ZDM GUI MANAGED HOSTS'
END='# END ZDM GUI MANAGED HOSTS'
HOSTS=Path('/etc/hosts')
STATE=Path('/var/lib/zdm-hosts-manager')
SOCKET=Path('/run/zdm-hosts-manager/control.sock')
LIMIT=65536
HOST_RE=re.compile(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z')

def hostname(value):
    if not isinstance(value,str):raise ValueError('Enter a valid hostname.')
    value=value.lower().rstrip('.')
    if not HOST_RE.fullmatch(value):raise ValueError('Use hostnames without spaces, shell syntax, underscores, or empty labels.')
    try:ipaddress.ip_address(value)
    except ValueError:return value
    raise ValueError('Enter a hostname rather than an IP address in the hostname field.')

def records(text):
    result=[]
    for line in text.splitlines():
        parts=line.split('#',1)[0].split()
        if len(parts)<2:continue
        try:address=str(ipaddress.ip_address(parts[0]))
        except ValueError:continue
        result.append({'ip':address,'names':parts[1:]})
    return result

def sections(content):
    lines=content.splitlines(keepends=True)
    starts=[i for i,line in enumerate(lines) if line.strip()==BEGIN]
    ends=[i for i,line in enumerate(lines) if line.strip()==END]
    if not starts and not ends:return content,'',''
    if len(starts)!=1 or len(ends)!=1 or starts[0]>=ends[0]:
        raise ValueError('The ZDM-managed hosts block is malformed. An administrator must repair its markers before saving.')
    start,end=starts[0],ends[0]
    return ''.join(lines[:start]),''.join(lines[start+1:end]),''.join(lines[end+1:])

def snapshot(content):
    before,managed,after=sections(content)
    return {'revision':hashlib.sha256(content.encode()).hexdigest(),
            'entries':records(managed),'protected_entries':records(before+after)}

def validate(entries,protected=()):
    if not isinstance(entries,list) or len(entries)>200:raise ValueError('Use at most 200 hosts-file rows.')
    protected_names={name.lower().rstrip('.') for row in protected for name in row['names']}
    protected_names.add(socket.gethostname().lower())
    clean=[];seen=set()
    for row in entries:
        if not isinstance(row,dict) or set(row)!={'ip','names'}:raise ValueError('Each row needs an IP address and hostnames.')
        try:
            if not isinstance(row['ip'],str) or '%' in row['ip']:raise ValueError()
            address=ipaddress.ip_address(row['ip'])
        except ValueError:raise ValueError('Enter a valid IPv4 or IPv6 address for every row.') from None
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError('Migration hosts must use a unicast address other than loopback or an unspecified address.')
        if not isinstance(row['names'],list) or not 1<=len(row['names'])<=20:
            raise ValueError('Each IP address needs between 1 and 20 hostnames or aliases.')
        names=[hostname(name) for name in row['names']]
        for name in names:
            if name in protected_names or name=='localhost' or name.startswith('localhost.'):
                raise ValueError('A hostname already exists in protected system entries. Manage only migration hostnames in the GUI.')
            if name in seen:raise ValueError('A hostname or alias can appear only once in the managed entries.')
            seen.add(name)
        clean.append({'ip':str(address),'names':names})
    return clean

def render(content,entries):
    before,managed,after=sections(content)
    clean=validate(entries,records(before+after))
    if not clean:return before+after
    block=BEGIN+'\n'+''.join(row['ip']+'\t'+' '.join(row['names'])+'\n' for row in clean)+END+'\n'
    return before+('' if not before or before.endswith('\n') else '\n')+block+after

def apply(entries,revision):
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (STATE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if HOSTS.is_symlink() or not stat.S_ISREG(HOSTS.stat().st_mode):
            raise ValueError('The hosts file must be a regular file managed by the operating system.')
        original=HOSTS.read_bytes()
        if len(original)>LIMIT:raise ValueError('The hosts file is too large for GUI management.')
        if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{64}',revision):raise ValueError('A valid hosts-file revision is required.')
        if hashlib.sha256(original).hexdigest()!=revision:
            return {'ok':False,'conflict':True,'message':'The hosts file changed since it was loaded. Reload it before saving.'}
        updated=render(original.decode('utf-8'),entries).encode('utf-8')
        if len(updated)>LIMIT:raise ValueError('The resulting hosts file is too large.')
        if updated==original:return {'ok':True,'changed':False,'revision':revision}
        backup=STATE/('hosts-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex+'.bak')
        with backup.open('xb') as handle:
            os.chmod(backup,0o600);handle.write(original);handle.flush();os.fsync(handle.fileno())
        descriptor,temporary=tempfile.mkstemp(prefix='.zdm-hosts-',dir=HOSTS.parent)
        temporary=Path(temporary)
        try:
            with os.fdopen(descriptor,'wb') as handle:
                handle.write(updated);handle.flush();os.fsync(handle.fileno())
            previous=HOSTS.stat()
            os.chown(temporary,previous.st_uid,previous.st_gid)
            shutil.copystat(HOSTS,temporary)
            if hasattr(os,'getxattr'):
                try:label=os.getxattr(HOSTS,'security.selinux')
                except OSError:label=None
                if label is not None and os.getxattr(temporary,'security.selinux')!=label:
                    raise ValueError('Could not preserve the hosts-file SELinux label.')
            # Recheck external administrator changes immediately before replacement.
            if HOSTS.read_bytes()!=original:
                return {'ok':False,'conflict':True,'message':'The hosts file changed during the save. Reload it before saving.'}
            os.replace(temporary,HOSTS)
            directory=os.open(HOSTS.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:temporary.unlink(missing_ok=True)
        return {'ok':True,'changed':True,'backup':str(backup),'revision':hashlib.sha256(updated).hexdigest()}

def dispatch(body):
    if not isinstance(body,dict) or set(body)!={'op','revision','entries'} or body['op']!='apply':
        raise ValueError('Only managed hosts-file entries can be updated.')
    return apply(body['entries'],body['revision'])

def serve():
    os.umask(0o077)
    allowed=pwd.getpwnam(os.environ.get('ZDM_GUI_SERVICE_USER','zdmuser')).pw_uid
    SOCKET.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET));os.chmod(SOCKET,0o660);listener.listen(8)
        while True:
            connection,_=listener.accept()
            with connection:
                connection.settimeout(10)
                result={'ok':False,'message':'Invalid hosts-file management request.'}
                try:
                    _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
                    if uid not in {0,allowed}:raise ValueError('This user cannot manage ZDM hostnames.')
                    payload=bytearray()
                    while not payload.endswith(b'\n'):
                        chunk=connection.recv(min(4096,LIMIT+1-len(payload)))
                        if not chunk or len(payload)+len(chunk)>LIMIT:raise ValueError('Invalid hosts-file management request size.')
                        payload.extend(chunk)
                    body=json.loads(payload)
                    result=dispatch(body)
                except ValueError as exc:result={'ok':False,'message':str(exc)}
                except Exception as exc:
                    print('Hosts helper failure: '+type(exc).__name__+' errno='+str(getattr(exc,'errno',None)),flush=True)
                    result={'ok':False,'message':'The hosts-file helper could not apply this update. Check its service log.'}
                try:connection.sendall(json.dumps(result).encode()+b'\n')
                except OSError:pass

if __name__=='__main__':serve()
