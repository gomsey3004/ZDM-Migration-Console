"""Oracle ZDM credential wallets; secrets are supplied only through a non-echoing PTY."""
import os
import re
import shutil
import signal
from pathlib import Path

import pexpect
from . import adapter,connections

DOCUMENTATION_PATH='provide-passwords-non-interactively-using-wallet.html'
CATALOG={
 'WALLET_SOURCEADMIN':{'label':'Source database administrator','default_user':'SYSTEM','username_parameter':'SOURCEDATABASE_ADMINUSERNAME'},
 'WALLET_TARGETADMIN':{'label':'Target database administrator','default_user':'ADMIN','username_parameter':'TARGETDATABASE_ADMINUSERNAME'},
 'WALLET_SOURCEGGADMIN':{'label':'Source database GoldenGate user','default_user':'GGADMIN','username_parameter':'SOURCEDATABASE_GGADMINUSERNAME'},
 'WALLET_TARGETGGADMIN':{'label':'Target database GoldenGate user','default_user':'GGADMIN','username_parameter':'TARGETDATABASE_GGADMINUSERNAME'},
 'WALLET_SOURCECONTAINER':{'label':'Source container database administrator','default_user':'SYSTEM','username_parameter':'SOURCECONTAINERDATABASE_ADMINUSERNAME'},
 'WALLET_SOURCECGGADMIN':{'label':'Source container GoldenGate user','default_user':'C##GGADMIN','username_parameter':'SOURCECONTAINERDATABASE_GGADMINUSERNAME'},
 'WALLET_OGGADMIN':{'label':'GoldenGate hub administrator','default_user':'oggadmin','username_parameter':'GOLDENGATEHUB_ADMINUSERNAME'},
 'WALLET_OGGSERVICEADMIN':{'label':'OCI GoldenGate service administrator','default_user':'oggadmin','username_parameter':'GOLDENGATEHUB_ADMINUSERNAME'},
 'SOURCE_SYS_PHYSICAL':{'label':'Physical migration source SYS','default_user':'SYS','cli_option':'sourcesyswallet'},
 'PHYSICAL_SOURCE_TDE':{'label':'Physical source TDE keystore password','default_user':'tdeuser','cli_option':'tdekeystorewallet'},
 'PHYSICAL_TARGET_TDE':{'label':'Physical target TDE keystore password','default_user':'tdeuser','cli_option':'tgttdekeystorewallet'},
 'PHYSICAL_OSS_BACKUP':{'label':'Object Storage backup credential','default_user':'ossuser','cli_option':'osswallet','cli_username_option':'backupuser'},
 'PHYSICAL_OSS_RESTORE':{'label':'Object Storage restore credential','default_user':'ossuser','cli_option':'ossrestorewallet','cli_username_option':'restoreuser'},
 'PHYSICAL_DATABASE_VAULT':{'label':'Database Vault owner credential','default_user':'dvuser','cli_option':'dvwallet','cli_username_option':'dvowner'},
 'PHYSICAL_RMAN_BACKUP':{'label':'RMAN backup password','default_user':'backupuser','cli_option':'backupwallet'},
 'PHYSICAL_SOURCE_OKV':{'label':'Physical source OKV keystore password','default_user':'okvuser','response_parameter':'WALLET_OKVKEYSTORE'},
 'PHYSICAL_TARGET_OKV':{'label':'Physical target OKV keystore password','default_user':'okvuser','response_parameter':'WALLET_TGTOKVKEYSTORE'},
}

def available_catalog():
    from . import compatibility
    detected=compatibility.require_supported();switches=set(detected['capabilities']['migrate_switches'])
    return {key:value for key,value in CATALOG.items()
            if not value.get('cli_option') or value['cli_option'] in switches}

def documentation():
    from . import compatibility
    return compatibility.require_supported()['documentation']+DOCUMENTATION_PATH

def validate_input(purpose,username,password):
    if purpose not in available_catalog():
        raise ValueError('Choose a supported database or GoldenGate wallet purpose.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.$#@:/-]{0,127}',username):
        raise ValueError('Enter a valid credential username without spaces or control characters.')
    if not 1<=len(password)<=512 or any(ord(c)<32 or ord(c)==127 for c in password):
        raise ValueError('Enter a non-empty password of at most 512 characters without control characters.')

def directory(value):
    path=Path(value)
    if path.name=='cwallet.sso':path=path.parent
    resolved=adapter.safe_path(str(path/'cwallet.sso'),[adapter.BASE,adapter.HOME,adapter.STAGE,adapter.OS_HOME])
    return Path(resolved).parent

def verify(path,username):
    path=directory(path)
    file=path/'cwallet.sso'
    if file.stat().st_uid!=os.geteuid() or file.stat().st_mode&0o077:
        raise ValueError('Wallet must be owned by the ZDM service account with private file permissions (0600).')
    code,output=connections.command([str(adapter.HOME/'bin/mkstore'),'-wrl',str(path),'-listCredential'],timeout=15)
    # Listing prints only connection-string/username metadata, never the password.
    rows=re.findall(r'^\s*(\d+)\s*:\s*(\S+)\s+(\S+)\s*$',output,re.M)
    if code or rows!=[('1','store',username)]:
        raise ValueError('Wallet verification failed: expected exactly one credential with alias store and the supplied username.')
    return {'ok':True,'alias':'store','username':username,'file':str(file),'message':'Oracle mkstore verified the auto-login wallet structure. Database/GoldenGate authentication is checked by ZDM evaluation.'}

def create(path,purpose,username,password):
    validate_input(purpose,username,password)
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.mkdir(mode=0o700,exist_ok=False)
    child=None
    try:
        code,_=connections.command([str(adapter.HOME/'bin/orapki'),'wallet','create','-wallet',str(path),'-auto_login_only'],timeout=30)
        if code:raise ValueError('Oracle orapki could not create the auto-login wallet.')
        child=pexpect.spawn(str(adapter.HOME/'bin/mkstore'),['-wrl',str(path),'-createCredential','store',username],
                            encoding='utf-8',echo=False,timeout=20)
        child.logfile=None
        child.expect(r'Enter your secret/Password:\s*')
        child.setecho(False)
        child.sendline(password)
        child.expect(r'Re-enter your secret/Password:\s*')
        child.setecho(False)
        child.sendline(password)
        child.expect(pexpect.EOF)
        child.close()
        if child.exitstatus!=0:raise ValueError('Oracle mkstore could not store the credential.')
        for file in path.iterdir():
            if file.is_file():file.chmod(0o600)
        verify(path,username)
        return str(path)
    except Exception:
        if child is not None and child.isalive():
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            child.close(force=True)
        shutil.rmtree(path,ignore_errors=True)
        # Never include PTY buffers or exceptions that may contain entered secrets.
        raise ValueError('Wallet creation failed. No credential was saved. Check the installed Oracle tools and retry; no password is included in this error.') from None

def response_settings(selected):
    settings={}
    seen=set()
    for wallet in selected:
        purpose=wallet['purpose']
        if purpose in seen:raise ValueError('Select only one wallet for each credential purpose.')
        seen.add(purpose)
        path=str(directory(wallet['path']))
        info=CATALOG[purpose]
        if info.get('response_parameter'):settings[info['response_parameter']]=path
        if purpose.startswith('WALLET_'):
            settings[purpose]=path
        parameter=info.get('username_parameter')
        if parameter:
            if parameter in settings and settings[parameter]!=wallet['username']:
                raise ValueError('Selected GoldenGate wallets specify conflicting administrator usernames.')
            settings[parameter]=wallet['username']
    return settings

def render_response(content,settings,remove_keys=()):
    """Replace active entries, preserve comments, and avoid ambiguous duplicate assignments."""
    lines=[];written=set()
    for line in content.splitlines():
        key=line.split('=',1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in remove_keys:continue
        if key in settings:
            if key not in written:lines.append(key+'='+settings[key]);written.add(key)
        else:lines.append(line)
    missing=[key for key in settings if key not in written]
    if missing:
        lines += ['','# Managed database / GoldenGate credential wallets']
        lines += [key+'='+settings[key] for key in missing]
    return '\n'.join(lines)+'\n'
