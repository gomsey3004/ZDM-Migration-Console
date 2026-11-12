"""Guarded modify-job construction for supported ZDM releases."""
import hashlib,json
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field
from . import adapter,compatibility,migration,response_files,resume

DOCUMENTATION_PATH='zero-downtime-migration-zdmcli-command-reference.html'
STATES={'EXECUTING'}

class Options(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rsp:str=Field(min_length=1,max_length=4096)

def context(data):
    info=resume.context(data)
    job=data['jobs'][0]
    supported=compatibility.require_supported()['capabilities']['modify_job']
    online_logical=info['migration_method']=='ONLINE_LOGICAL'
    can_modify=(supported and job.get('type','').upper()=='MIGRATE' and
                job.get('status') in STATES and online_logical and
                not info['method_conflict'])
    if not supported:
        reason='The installed ZDM patch does not support modify job.'
    elif job.get('type','').upper()!='MIGRATE':
        reason='Modify is available only for migration jobs, not evaluation jobs.'
    elif job.get('status') not in STATES:
        reason='Oracle documents modify job for a running migration job. The job must be EXECUTING.'
    elif info['method_conflict']:
        reason='Job metadata conflicts about the migration method. Review the job before modifying it.'
    elif not online_logical:
        reason='Modify job applies to ONLINE_LOGICAL migrations because it changes Oracle GoldenGate Extract and Replicat parameters.'
    else:reason=''
    return {**info,'can_modify':can_modify,'reason':reason}

def response_info(value):
    path=Path(adapter.safe_path(value,[adapter.STAGE]))
    if path.suffix.lower()!='.rsp':raise ValueError('Select a .rsp response file.')
    with path.open('rb') as handle:content=handle.read(300001)
    if len(content)>300000:raise ValueError('Response file must be below 300 KB.')
    method=migration.method_info(content.decode('utf-8'))
    if method['migration_method']!='ONLINE_LOGICAL':
        raise ValueError('Choose a configured ONLINE_LOGICAL response file for GoldenGate modification.')
    if response_files.template(path):
        raise ValueError('Save a configured copy of the template before using it to modify a job.')
    return path,content

def responses():
    found=[]
    for path in sorted(adapter.STAGE.rglob('*.rsp')):
        if len(found)>=100:break
        try:
            actual,content=response_info(str(path))
            found.append({'name':actual.name,'path':str(actual),'revision':hashlib.sha256(content).hexdigest()})
        except (ValueError,OSError,UnicodeError):continue
    return found

def schema(data):
    info=context(data)
    result={k:v for k,v in info.items() if k!='identity'}
    result.update({
        'responses':responses() if info['can_modify'] else [],
        'documentation':compatibility.require_supported()['documentation']+DOCUMENTATION_PATH,
        'scope':'Oracle GoldenGate Extract and Replicat parameters only'
    })
    return result

def build(data,options):
    info=context(data)
    if not info['can_modify']:raise ValueError(info['reason'])
    path,content=response_info(options.rsp)
    args=['modify','job','-jobid',str(info['job_id']),'-rsp',str(path)]
    revision=hashlib.sha256(json.dumps({
        'job':info['identity'],'args':args,'response_sha256':hashlib.sha256(content).hexdigest()
    },sort_keys=True).encode()).hexdigest()
    return args,revision
