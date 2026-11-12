"""Capability-driven, non-shell adapter for supported Oracle ZDM releases."""
import os
import re
import subprocess
import signal
import ipaddress
from pathlib import Path

HOME = Path(os.environ.get('ZDM_HOME', '/u01/app/zdmhome'))
BASE = Path(os.environ.get('ZDM_BASE', '/u01/app/zdmbase'))
STAGE = Path(os.environ.get('ZDM_STAGE', '/u01/app/zdmstage'))
OS_HOME = Path(os.environ.get('ZDM_OS_HOME', str(Path.home())))
STATES = {'SCHEDULED','EXECUTING','UNKNOWN','TERMINATED','FAILED','SUCCEEDED','PAUSED','ABORTED','SUSPENDING','SUSPENDED'}
WALLETS = {'sourcesyswallet','tdekeystorewallet','tgttdekeystorewallet','osswallet','ossrestorewallet','backupwallet','dvwallet','tdewallet','tgttdewallet','srcred'}

def redact(text):
    text = re.sub(r'(?im)((?:\w*(?:password|passwd|secret|token)\w*)\s*[:=]\s*)[^\r\n]+', r'\1[REDACTED]', text)
    return re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REDACTED]', text, flags=re.S)

def run(args, timeout=90, service=False, operation_id=None):
    if operation_id:
        from .interactive import run as interactive_run
        return interactive_run(operation_id,args,timeout)
    command = [str(HOME/'bin'/('zdmservice' if service else 'zdmcli')), *args]
    # No shell, no inherited stdin; a missing wallet must fail rather than hang for input.
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill only this client's process group, including Java children. The
        # separately running ZDM server owns accepted migration jobs.
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        raise TimeoutError('CLI response timed out; submission outcome is unknown. Refresh ZDM jobs before retrying. ' + redact(output.decode(errors='replace')[-12000:]))
    output = redact(output.decode(errors='replace'))
    if proc.returncode:
        raise RuntimeError(output[-16000:] or f'ZDM CLI exited with code {proc.returncode}')
    return output

def parse_jobs(output):
    jobs = []
    for chunk in re.split(r'(?m)^\s*Job ID\s*:\s*', output)[1:]:
        match = re.match(r'(\d+)', chunk)
        if not match:
            continue
        def field(*names):
            for name in names:
                m = re.search(r'^[ \t]*'+re.escape(name)+r'[ \t]*:[ \t]*(.*?)[ \t]*$',chunk,re.M|re.I)
                if m:
                    return m.group(1).strip().strip('"')
            return ''
        phases = []
        for line in chunk.splitlines():
            m = re.match(r'^\s*(ZDM_[A-Z0-9_]+)\s*(?:\.{2,}|\s{2,}|:)\s*([A-Z_]+)\s*$',line)
            if m:
                phases.append({'name':m[1], 'status':m[2]})
        jobs.append({'id':int(match[1]), 'status':field('Current status','Status') or 'UNKNOWN',
                     'type':field('Job Type'), 'database':field('Source database','Database name','Database'),
                     'source':field('Source node','Source node name','Source node name(s)'), 'target':field('Target node','Target node name','Target node name(s)'),
                     'started':field('Start time','Scheduled job execution start time','Scheduled job execution time'),
                     'result_path':field('Result file path'), 'phases':phases})
    return jobs

def safe_path(value, roots, exists=True):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('Use an absolute server path.')
    path = path.resolve()
    if not any(_is_relative_to(path, root.resolve()) for root in roots):
        raise ValueError('Path is outside the permitted ZDM directories.')
    if exists and not path.is_file():
        raise ValueError(f'Server file does not exist: {path}')
    if exists and not os.access(path, os.R_OK):
        raise ValueError(f'{OS_HOME.name} cannot read {path}')
    return str(path)

def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def word(value, label):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,253}', value):
        raise ValueError(f'Invalid {label}. Use a hostname or identifier, without spaces.')
    return value

def host(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',value):
            raise ValueError('Use a host IP address or DNS name without spaces or shell syntax.')
    return value

def migration_args(profile, evaluate=True, mode=None):
    from .migration import build
    return build(profile,evaluate,mode)


def tail_log(path):
    path = safe_path(path,[BASE])
    if Path(path).suffix.lower() not in {'.log','.out','.trc'}:
        raise ValueError('ZDM result path is not a supported log file.')
    with open(path,'rb') as handle:
        handle.seek(0,2)
        handle.seek(max(0,handle.tell()-128000))
        return redact(handle.read().decode(errors='replace'))
