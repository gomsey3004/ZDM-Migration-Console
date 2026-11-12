"""Ephemeral masked credential prompts for the ZDM CLI; no secrets are persisted."""
import os
import re
import signal
import threading
import time
import uuid

import pexpect
from . import adapter

LOCK=threading.Lock()
SESSIONS={}
PROMPT=r"(?im)^(?:[^\r\n]{0,140}(?:enter|provide|re-enter|confirm)[^\r\n]{0,180}(?:password|passphrase|secret|master(?: encryption)? key)[^\r\n]{0,140}[:?][ \t\r]*|[^\r\n]{0,160}(?:password|passphrase)(?: for [^:\r\n]{1,120})?:[ \t\r]*)$"

def pending(operation_id):
    with LOCK:
        session=SESSIONS.get(operation_id)
        if not session or not session.get('prompt'):return None
        return {'id':session['id'],'text':session['prompt'],'expires_at':session['expires_at']}

def answer(operation_id,prompt_id,value):
    if not isinstance(value,str) or not 1<=len(value)<=512 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Enter a non-empty credential without control characters (maximum 512 characters).')
    with LOCK:
        session=SESSIONS.get(operation_id)
        if not session or session.get('id')!=prompt_id or not session.get('prompt') or session.get('value') is not None:
            raise ValueError('This credential prompt has expired or was already answered. Refresh the operation status.')
        if time.time()>session['expires_at']:raise ValueError('This credential prompt has expired.')
        session['value']=value;session['event'].set()

def run(operation_id,args,timeout=900):
    child=None;output='';secrets=[];start=time.monotonic()
    def scrub(value):
        for secret in sorted(secrets,key=len,reverse=True):value=value.replace(secret,'[REDACTED]')
        return adapter.redact(value)
    try:
        child=pexpect.spawn(str(adapter.HOME/'bin/zdmcli'),args,encoding='utf-8',echo=False,timeout=1,maxread=4096)
        child.logfile=None
        while True:
            if time.monotonic()-start>timeout:raise TimeoutError()
            match=child.expect([PROMPT,pexpect.EOF,pexpect.TIMEOUT],timeout=1)
            if match==2:continue
            output+=child.before or ''
            if match==1:break
            prompt=scrub((child.after or '').strip())[-500:]
            event=threading.Event()
            session={'id':uuid.uuid4().hex,'prompt':prompt,'event':event,'value':None,'expires_at':time.time()+180}
            with LOCK:SESSIONS[operation_id]=session
            if not event.wait(min(180,max(0,timeout-(time.monotonic()-start)))):raise TimeoutError()
            with LOCK:
                secret=session.pop('value',None)
                session['prompt']=''
                SESSIONS.pop(operation_id,None)
            if secret is None:raise TimeoutError()
            secrets.append(secret)
            output+=prompt+' [credential supplied]\n'
            child.setecho(False);child.sendline(secret)
            secret=None
        child.close()
        result=scrub(output)[-20000:]
        if child.exitstatus!=0:raise RuntimeError(result or 'ZDM CLI rejected the command. Check the migration settings.')
        return result
    except TimeoutError:
        raise TimeoutError('The ZDM CLI or credential prompt timed out. Submission outcome may be unknown; check ZDM jobs before retrying.') from None
    except RuntimeError:raise
    except Exception:
        raise RuntimeError('The ZDM CLI interactive session failed. Check ZDM jobs before retrying. Credential values are not included in this error.') from None
    finally:
        with LOCK:
            session=SESSIONS.pop(operation_id,None)
            if session:session['value']=None;session['prompt']=''
        if child is not None and child.isalive():
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            child.close(force=True)
        secrets.clear()
