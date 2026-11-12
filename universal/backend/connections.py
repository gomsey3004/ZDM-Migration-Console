"""Credential storage and bounded, read-only connectivity probes from the ZDM host."""
import base64
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import ipaddress
import time
from urllib.parse import urlsplit
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa,ec,ed25519
from . import adapter

def vault():
    path=adapter.BASE/'gui-credentials'
    path.mkdir(mode=0o700,exist_ok=True)
    return path

def write_private(path,content):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'wb') as handle:
        handle.write(content)

def public_fingerprint(public):
    parts=public.split()
    if len(parts)<2:
        raise ValueError('Invalid SSH public key.')
    try:
        serialization.load_ssh_public_key((parts[0]+' '+parts[1]).encode())
        blob=base64.b64decode(parts[1],validate=True)
    except Exception:
        raise ValueError('Invalid SSH public key.') from None
    return 'SHA256:'+base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')

def inspect_private(content,kind):
    if len(content)>32768:
        raise ValueError('Private key file must be smaller than 32 KB.')
    try:
        if b'BEGIN OPENSSH PRIVATE KEY' in content:
            key=serialization.load_ssh_private_key(content,password=None)
        else:
            key=serialization.load_pem_private_key(content,password=None)
    except Exception:
        raise ValueError('Use a valid, unencrypted OpenSSH or PEM private key. Passphrase-protected and PuTTY PPK keys are not supported.') from None
    if kind=='oci':
        if not isinstance(key,rsa.RSAPrivateKey) or key.key_size<2048:
            raise ValueError('OCI API signing keys must be RSA keys of at least 2048 bits.')
        public=key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        digest=hashlib.md5(public,usedforsecurity=False).hexdigest()
        fingerprint=':'.join(digest[i:i+2] for i in range(0,len(digest),2))
        public_text=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        normalized=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    else:
        if not isinstance(key,(rsa.RSAPrivateKey,ec.EllipticCurvePrivateKey,ed25519.Ed25519PrivateKey)):
            raise ValueError('Use RSA, ECDSA, or Ed25519 SSH keys.')
        if isinstance(key,rsa.RSAPrivateKey) and key.key_size<1024:
            raise ValueError('RSA SSH keys must be at least 1024 bits.')
        public_text=key.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
        fingerprint=public_fingerprint(public_text)
        format=serialization.PrivateFormat.OpenSSH if isinstance(key,ed25519.Ed25519PrivateKey) else serialization.PrivateFormat.TraditionalOpenSSL
        normalized=key.private_bytes(serialization.Encoding.PEM,format,serialization.NoEncryption())
    return {'fingerprint':fingerprint,'public_key':public_text},normalized

def existing_key(path,kind):
    path=Path(adapter.safe_path(path,[adapter.BASE,adapter.OS_HOME/'.ssh',adapter.OS_HOME/'.oci']))
    if path.stat().st_uid!=os.geteuid():
        raise ValueError('The existing key must be owned by the ZDM service account.')
    info,_=inspect_private(path.read_bytes(),kind)
    path.chmod(0o600)
    return path,info

def command(args,timeout=15,env=None,json_output=False):
    proc=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE if json_output else subprocess.STDOUT,
                          start_new_session=True,env=env)
    try:
        output,errors=proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid,signal.SIGKILL)
        proc.communicate()
        raise ValueError('Connection check timed out. Check routing, firewall rules, and endpoint configuration.') from None
    if json_output:
        # OCI warnings belong to stderr and must never corrupt successful JSON.
        if proc.returncode:
            detail=(errors or b'').decode(errors='replace')+'\n'+output.decode(errors='replace')
            return proc.returncode,adapter.redact(detail.strip())[-4000:]
        if len(output)>65536:raise ValueError('OCI response exceeded the connection-check size limit.')
        return proc.returncode,adapter.redact(output.decode(errors='replace'))
    return proc.returncode,adapter.redact(output.decode(errors='replace'))[-4000:]

def check_gg_rest(endpoint,username,password,allow_self_signed=True):
    """Run Oracle's ZDM-host to GoldenGate deployments REST validation."""
    started=time.monotonic()
    value=endpoint.strip().rstrip('/')
    if '://' not in value:value='https://'+value
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Enter a GoldenGate HTTPS host or base URL without credentials, a query, or a fragment.')
    if parsed.path not in {'','/'}:
        raise ValueError('Enter the GoldenGate base URL only; the GUI adds /services/v2/deployments.')
    adapter.host(parsed.hostname)
    try:port=parsed.port or 443
    except ValueError:raise ValueError('Enter a valid GoldenGate HTTPS port.') from None
    if not 1<=port<=65535:raise ValueError('Enter a GoldenGate HTTPS port from 1 to 65535.')
    if not username or len(username)>128 or any(ord(c)<33 or ord(c)==127 for c in username):
        raise ValueError('Enter a valid GoldenGate administrator username without spaces or control characters.')
    if not password or len(password)>512 or any(ord(c)<32 or ord(c)==127 for c in password):
        raise ValueError('Enter a non-empty GoldenGate administrator password without control characters.')
    context=ssl._create_unverified_context() if allow_self_signed else ssl.create_default_context()
    connection=http.client.HTTPSConnection(parsed.hostname,port,timeout=15,context=context)
    token=base64.b64encode((username+':'+password).encode()).decode()
    try:
        connection.request('GET','/services/v2/deployments',headers={
            'Authorization':'Basic '+token,'Accept':'application/json','User-Agent':'ZDM-GUI/1.0'})
        response=connection.getresponse()
        body=response.read(65537)
        if len(body)>65536:raise ValueError('GoldenGate returned a response larger than 64 KB.')
        if response.status==401:raise ValueError('GoldenGate rejected the administrator username or password (HTTP 401).')
        if response.status==403:raise ValueError('GoldenGate authenticated the request but denied access to deployments (HTTP 403).')
        if not 200<=response.status<300:raise ValueError('GoldenGate deployments API returned HTTP '+str(response.status)+'.')
        try:data=json.loads(body)
        except json.JSONDecodeError:raise ValueError('GoldenGate returned HTTP '+str(response.status)+' but the deployments response was not valid JSON.') from None
        count=None
        if isinstance(data,list):count=len(data)
        elif isinstance(data,dict):
            candidates=[data.get('items')]
            if isinstance(data.get('response'),dict):candidates.append(data['response'].get('items'))
            count=next((len(item) for item in candidates if isinstance(item,list)),None)
        detail='Authenticated HTTPS request succeeded (HTTP '+str(response.status)+').'
        if count is not None:detail+=' GoldenGate returned '+str(count)+' deployment'+('' if count==1 else 's')+'.'
        return {'ok':True,'name':'ZDM to GoldenGate REST','steps':[{
            'name':'GET /services/v2/deployments','status':'PASS','detail':detail,
            'duration_ms':round((time.monotonic()-started)*1000)}],
            'scope':'Executed from the ZDM server. TLS certificate verification was '+('disabled for this check.' if allow_self_signed else 'enabled using the server trust store.'),
            'endpoint':'https://'+parsed.hostname+(':'+str(port) if port!=443 else '')+'/services/v2/deployments'}
    except (socket.timeout,TimeoutError):
        raise ValueError('GoldenGate HTTPS connection timed out. Check DNS, routing, security rules, and port '+str(port)+'.') from None
    except ssl.SSLCertVerificationError:
        raise ValueError('GoldenGate TLS certificate verification failed. Trust the certificate or enable the self-signed certificate option.') from None
    except (ssl.SSLError,OSError,http.client.HTTPException) as exc:
        raise ValueError('GoldenGate HTTPS connection failed: '+adapter.redact(str(exc))[-500:]) from None
    finally:connection.close()

def scan_host(server):
    code,output=command(['/usr/bin/ssh-keyscan','-T','5','-p',str(server['ssh_port']),'-t','ed25519,ecdsa,rsa',server['host']],timeout=12)
    found=[]
    for line in output.splitlines():
        if line.startswith('#'):
            continue
        fields=line.split()
        if len(fields)!=3:
            continue
        public=fields[1]+' '+fields[2]
        try:
            fingerprint=public_fingerprint(public)
        except ValueError:
            continue
        if not any(item['fingerprint']==fingerprint for item in found):
            found.append({'public_key':public,'algorithm':fields[1],'fingerprint':fingerprint})
    if not found:
        raise ValueError('No SSH host key was received. '+output[-1000:])
    return found

def known_hosts_path(server_id):
    return vault()/'known-hosts'/server_id

def host_line(server,public):
    address=server['host'] if server['ssh_port']==22 else '['+server['host']+']:'+str(server['ssh_port'])
    return address+' '+public+'\n'

def ssh_arguments(server,key,known,remote_command):
    options=['-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','IdentityAgent=none',
             '-o','ProxyCommand=none','-o','PermitLocalCommand=no','-o','ClearAllForwardings=yes',
             '-o','StrictHostKeyChecking=yes','-o','GlobalKnownHostsFile=/dev/null','-o','UserKnownHostsFile='+str(known),
             '-o','CheckHostIP=no',
             '-o','ConnectTimeout=8','-o','ConnectionAttempts=1','-o','RequestTTY=no']
    algorithm=server['trusted_key'].split()[0]
    if algorithm=='ssh-rsa':
        algorithm='rsa-sha2-512,rsa-sha2-256'
    options += ['-o','HostKeyAlgorithms='+algorithm]
    return ['/usr/bin/ssh',*options,'-i',key,'-p',str(server['ssh_port']),server['os_user']+'@'+server['host'],remote_command]

def check_server(server,key_path=None):
    steps=[]
    resolved_addresses={}
    def step(name,fn):
        started=time.monotonic()
        try:
            detail=fn()
            steps.append({'name':name,'status':'PASS','detail':detail,'duration_ms':round((time.monotonic()-started)*1000)})
            return True
        except Exception as exc:
            steps.append({'name':name,'status':'FAIL','detail':adapter.redact(str(exc))[-2000:],'duration_ms':round((time.monotonic()-started)*1000)})
            return False
    def resolve(host,port):
        code,output=command(['/usr/bin/getent','ahosts',host],timeout=6)
        addresses=[]
        for line in output.splitlines():
            try:
                address=str(ipaddress.ip_address(line.split()[0]))
            except (ValueError,IndexError):
                continue
            if address not in addresses:
                addresses.append(address)
        if code or not addresses:
            raise ValueError('DNS resolution failed for '+host)
        resolved_addresses[host]=addresses[:4]
        return 'Resolved to '+', '.join(addresses)
    def tcp(host,port):
        deadline=time.monotonic()+5
        errors=[]
        for address in resolved_addresses.get(host,[]):
            remaining=deadline-time.monotonic()
            if remaining<=0:
                break
            try:
                with socket.create_connection((address,port),timeout=remaining):
                    return f'TCP {host}:{port} is reachable from the ZDM host.'
            except OSError as exc:
                errors.append(str(exc))
        raise ValueError('TCP '+host+':'+str(port)+' is unreachable. '+'; '.join(errors))
    def pinned():
        if not server.get('trusted_key'):
            raise ValueError('Verify and trust this server’s SSH host fingerprint before checking authentication.')
        known=known_hosts_path(server['id'])
        expected=host_line(server,server['trusted_key'])
        if not known.is_file():
            raise ValueError('The pinned SSH host-key file is missing or changed. Verify the host key again.')
        content=known.read_text()
        if content!=expected:
            expected_fields=expected.split()
            lines=[line.split() for line in content.splitlines() if line.strip()]
            same_key_aliases=(lines and any(line[0]==expected_fields[0] for line in lines)
                              and all(len(line)==3 and line[1:]==expected_fields[1:] for line in lines))
            if not same_key_aliases:
                raise ValueError('The pinned SSH host-key file is missing or changed. Verify the host key again.')
            temporary=known.with_name(known.name+'.repair-'+str(os.getpid())+'-'+str(time.time_ns()))
            try:
                write_private(temporary,expected.encode())
                os.replace(temporary,known)
            finally:
                temporary.unlink(missing_ok=True)
        return public_fingerprint(server['trusted_key'])
    def ssh(remote):
        if not key_path or not Path(key_path).is_file():
            raise ValueError('The selected SSH private key is unavailable.')
        mode=stat.S_IMODE(Path(key_path).stat().st_mode)
        if mode&0o077:
            raise ValueError('SSH private key permissions must be 0600 or stricter.')
        code,output=command(ssh_arguments(server,key_path,known_hosts_path(server['id']),remote),timeout=13)
        if code:
            raise ValueError(output or 'SSH command failed.')
        return output.strip() or 'Command succeeded.'
    if server['kind']=='ssh':
        dns=step('DNS resolution',lambda:resolve(server['host'],server['ssh_port']))
        reachable=dns and step('SSH TCP port',lambda:tcp(server['host'],server['ssh_port']))
        trusted=step('SSH host identity',pinned)
        deferred=server.get('authentication_check','key')=='zdm'
        authenticated=not deferred and reachable and trusted and step('SSH key authentication',lambda:ssh("printf 'ZDM_CONNECTION_OK\\n'; id -un; hostname"))
        if deferred and reachable and trusted:
            steps.append({'name':'OS authentication','status':'DEFERRED','detail':'Network and pinned host identity checked. The chosen migration authentication and OS privileges must be validated by ZDM Evaluate.','duration_ms':0})
        elif authenticated:
            if server['os_user']=='root':
                steps.append({'name':'OS privileges','status':'PASS','detail':'Authenticated as root.','duration_ms':0})
            else:
                step('Passwordless sudo',lambda:ssh('/usr/bin/sudo -n true'))
        else:
            steps.append({'name':'Authentication / sudo','status':'SKIPPED','detail':'Resolve the failed SSH checks first.','duration_ms':0})
    if server.get('database_port'):
        dbhost=server.get('database_host') or server['host']
        resolved=step('Database DNS',lambda:resolve(dbhost,server['database_port']))
        if resolved:
            step('Database listener TCP',lambda:tcp(dbhost,server['database_port']))
    else:
        steps.append({'name':'Database listener TCP','status':'NOT_CONFIGURED','detail':'No database listener port was configured.','duration_ms':0})
    return {'ok':not any(s['status'] in {'FAIL','SKIPPED'} for s in steps),'steps':steps,
            'scope':'ZDM-host connectivity only. Listener TCP reachability is not a database login or a full ZDM evaluation. DEFERRED authentication has not been tested by this check.'}

def validate_oci(body,key):
    for field,resource in [('user','user'),('tenancy','tenancy')]:
        if not re.fullmatch(r'ocid1\.'+resource+r'\.[a-z0-9]+\.\.[a-z0-9]+',body[field]):
            raise ValueError('Enter a valid '+resource+' OCID.')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',body['profile_name']):
        raise ValueError('OCI profile name must start with a letter and use letters, numbers, underscores, or hyphens.')
    if not re.fullmatch(r'[a-z]{2}-[a-z0-9-]+-\d+',body['region']):
        raise ValueError('Enter an OCI region identifier such as us-phoenix-1.')
    if body.get('fingerprint') and body['fingerprint'].lower()!=key['fingerprint']:
        raise ValueError('The OCI fingerprint does not match the selected RSA private key.')

def oci_config(body,key):
    return ('['+body['profile_name']+']\nuser='+body['user']+'\ntenancy='+body['tenancy']+
            '\nregion='+body['region']+'\nfingerprint='+key['fingerprint']+'\nkey_file='+key['path']+'\n')

def oci_response(output):
    if not output.strip():raise ValueError('OCI CLI returned no JSON data. Check the CLI runtime and output configuration.')
    try:data=json.loads(output)
    except json.JSONDecodeError:raise ValueError('OCI CLI returned an invalid JSON response. Check CLI output settings and the server-side CLI runtime.') from None
    if not isinstance(data,dict):raise ValueError('OCI CLI returned an unexpected response format; expected a JSON object.')
    return data

def check_oci(profile):
    env={k:v for k,v in os.environ.items() if not k.startswith('OCI_')}
    runtime=Path(os.environ.get('ZDM_OCI_SITE',''))
    env['PYTHONPATH']=str(runtime)
    args=[os.environ.get('ZDM_OCI_PYTHON',os.sys.executable),'-c','from oci_cli.cli import cli; cli()',
          '--config-file',profile['config_file'],'--profile',profile['profile_name'],'--auth','api_key',
          '--region',profile['region'],'--output','json','--connection-timeout','5','--read-timeout','10','--max-retries','0',
          'iam','availability-domain','list','--compartment-id',profile['tenancy']]
    started=time.monotonic()
    try:
        code,output=command(args,timeout=30,env=env,json_output=True)
        if code:
            raise ValueError(output)
        data=oci_response(output)
        names=[item['name'] for item in data.get('data',[])]
        return {'ok':True,'steps':[{'name':'OCI signed API request','status':'PASS','detail':'Availability domains: '+', '.join(names),'duration_ms':round((time.monotonic()-started)*1000)}],
                'scope':'Verifies this API key and permission to list availability domains, not every migration permission.'}
    except Exception as exc:
        return {'ok':False,'steps':[{'name':'OCI signed API request','status':'FAIL','detail':adapter.redact(str(exc))[-2000:],'duration_ms':round((time.monotonic()-started)*1000)}],
                'scope':'A failure can indicate credentials, network access, or IAM permissions. It does not automatically change the migration response file.'}

def check_autonomous(profile,ocid):
    if not re.fullmatch(r'ocid1\.autonomousdatabase\.[a-z0-9_.-]{8,}',ocid):
        raise ValueError('Set a valid TARGETDATABASE_OCID for the Autonomous Database target.')
    env={k:v for k,v in os.environ.items() if not k.startswith('OCI_')}
    env['PYTHONPATH']=os.environ.get('ZDM_OCI_SITE','')
    args=[os.environ.get('ZDM_OCI_PYTHON',os.sys.executable),'-c','from oci_cli.cli import cli; cli()',
          '--config-file',profile['config_file'],'--profile',profile['profile_name'],'--auth','api_key',
          '--region',profile['region'],'--output','json','--connection-timeout','5','--read-timeout','10','--max-retries','0',
          'db','autonomous-database','get','--autonomous-database-id',ocid,
          '--query','data.{id:id,state:"lifecycle-state"}']
    started=time.monotonic()
    try:
        code,output=command(args,timeout=30,env=env,json_output=True)
        if code:raise ValueError(output)
        resource=oci_response(output)
        if resource.get('id')!=ocid:raise ValueError('OCI returned an unexpected Autonomous Database identifier.')
        if resource.get('state')!='AVAILABLE':raise ValueError('Autonomous Database is not AVAILABLE (state: '+str(resource.get('state','unknown'))+').')
        return {'ok':True,'steps':[{'name':'Autonomous target OCI API','status':'PASS','detail':'The configured OCI identity can read the target database and its lifecycle state is AVAILABLE. No target SSH host is needed.','duration_ms':round((time.monotonic()-started)*1000)}],
                'scope':'Verifies OCI resource access and availability only. ZDM Evaluate verifies database connectivity, wallet credentials and full migration permissions.'}
    except Exception as exc:
        return {'ok':False,'steps':[{'name':'Autonomous target OCI API','status':'FAIL','detail':adapter.redact(str(exc))[-2000:],'duration_ms':round((time.monotonic()-started)*1000)}],
                'scope':'Check target OCID, region, OCI API credentials and Autonomous Database IAM permissions. No SSH attempt was made.'}
