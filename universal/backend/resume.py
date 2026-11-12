"""Method-specific resume controls filtered by the installed ZDM CLI."""
import hashlib,json,re,shlex
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field,StrictBool
from typing import Literal
from . import adapter,compatibility,migration,response_files

DOCUMENTATION_PATH='zero-downtime-migration-zdmcli-command-reference.html'
STATES={'PAUSED','FAILED','SUSPENDED'}
class Options(BaseModel):
    model_config=ConfigDict(extra='forbid')
    pauseafter:str=Field(default='',max_length=128,pattern=r'^(?:ZDM_[A-Z0-9_]+)?$')
    rsp:str=Field(default='',max_length=4096)
    ignore:Literal['','IMPORT_ERRORS','EXPORT_ERRORS']=''
    rerun:Literal['','BACKUP','RESTORE']=''
    skip:Literal['','SWITCHOVER','UPGRADE','DG_CONFIG','DG_CONFIG_STBY']=''
    restore_ack:StrictBool=False

def scheduled_rsp(raw):
    paths=set()
    for text in re.findall(r'^\s*Scheduled job command\s*:\s*(.+)$',raw,re.M):
        text=text.strip()
        if len(text)>1 and text[0]==text[-1]=='"':text=text[1:-1]
        try:args=shlex.split(text)
        except ValueError:continue
        for i,arg in enumerate(args[:-1]):
            if arg=='-rsp':paths.add(args[i+1])
    return paths.pop() if len(paths)==1 else ''

def response_info(value):
    path=Path(adapter.safe_path(value,[adapter.STAGE,adapter.HOME]))
    if path.suffix.lower()!='.rsp':raise ValueError('Select a .rsp response file.')
    with path.open('rb') as handle:content=handle.read(300001)
    if len(content)>300000:raise ValueError('Response file must be below 300 KB.')
    return path,content,migration.method_info(content.decode('utf-8'))

def context(data):
    if not data.get('jobs'):raise ValueError('ZDM job not found.')
    job=data['jobs'][0];raw=data.get('raw','');evidence=[];source=[]
    explicit=re.findall(r'^\s*Migration [Mm]ethod\s*:\s*"?([A-Z_]+)',raw,re.M)
    for method in explicit:
        kind=migration.method_info('MIGRATION_METHOD='+method)['migration_kind']
        if kind!='unknown':evidence.append(kind);source.append('ZDM migration method')
    names=list(dict.fromkeys(p['name'] for p in job.get('phases',[])))
    if any(re.search(r'DATAPUMP|_GG_|_CPAT|_LOGICAL',name) for name in names):evidence.append('logical');source.append('ZDM workflow phases')
    if any(re.search(r'_DG_|_RMAN_|_BACKUP_(?:FULL|INCREMENTAL)_SRC|_RESTORE_(?:FULL|INCREMENTAL)_TGT|_DUPLICATE_TGT',name) for name in names):evidence.append('physical');source.append('ZDM workflow phases')
    rsp=scheduled_rsp(raw);digest='';method=explicit[0] if len(set(explicit))==1 else ''
    if rsp:
        try:
            path,content,info=response_info(rsp);digest=hashlib.sha256(content).hexdigest();method=info['migration_method']
            if info['migration_kind']!='unknown':evidence.append(info['migration_kind']);source.append('Scheduled response file')
        except (ValueError,OSError,UnicodeError):pass
    kinds=set(evidence);kind=next(iter(kinds)) if len(kinds)==1 else 'unknown'
    conflict=len(kinds)>1
    can_resume=job.get('status') in STATES and job.get('type','').upper()=='MIGRATE'
    reason='' if can_resume else 'Only paused, failed or suspended migration jobs can resume. Evaluation and terminated jobs cannot resume.'
    identity={'id':job['id'],'type':job.get('type'),'status':job.get('status'),'kind':kind,'method':method,'rsp':rsp,'digest':digest,'phases':names}
    return {'job_id':job['id'],'migration_kind':kind,'migration_method':method,'method_source':', '.join(dict.fromkeys(source)) or 'Method unavailable','method_conflict':conflict,'status':job.get('status'),'can_resume':can_resume,'reason':reason,'phases':names,'identity':identity}

def schema(data):
    detected=compatibility.require_supported();switches=set(detected['capabilities']['resume_switches'])
    info=context(data);kind=info['migration_kind'];responses=[]
    if kind=='logical':
        for path in sorted(adapter.STAGE.rglob('*.rsp')):
            if len(responses)>=100:break
            try:
                if response_files.template(path):continue
                actual,content,method=response_info(str(path))
                if method['migration_kind']=='logical':responses.append({'name':path.name,'path':str(actual)})
            except (ValueError,OSError,UnicodeError):continue
    result={k:v for k,v in info.items() if k!='identity'}
    candidates=['pauseafter']+(['rsp','ignore'] if kind=='logical' else ['rerun','skip'] if kind=='physical' else [])
    result.update({'fields':[name for name in candidates if name in switches],'responses':responses,
                   'documentation':detected['documentation']+DOCUMENTATION_PATH})
    return result

def build(data,options):
    switches=set(compatibility.require_supported()['capabilities']['resume_switches'])
    info=context(data)
    if not info['can_resume']:raise ValueError(info['reason'])
    kind=info['migration_kind'];values=options.model_dump();args=['resume','job','-jobid',str(info['job_id'])];rsp_digest=''
    if options.rsp and options.pauseafter:raise ValueError('Choose either an updated response file or pause after a phase, as documented for resume.')
    if (options.rsp or options.ignore) and kind!='logical':raise ValueError('Updated response files and Data Pump error options require a verified logical job.')
    if (options.rerun or options.skip or options.restore_ack) and kind!='physical':raise ValueError('Backup/restore reruns and Data Guard/upgrade skip options require a verified physical job.')
    if options.ignore and info['status']!='FAILED':raise ValueError('Ignore Data Pump errors is available only for a failed logical job.')
    if options.restore_ack and options.rerun!='RESTORE':raise ValueError('Restore acknowledgement applies only to rerun RESTORE.')
    if options.rerun=='RESTORE' and not options.restore_ack:raise ValueError('Confirm that rerun RESTORE drops and recreates the target database.')
    if options.rsp:
        if 'rsp' not in switches:raise ValueError('The installed ZDM release does not support an updated response file during resume.')
        path,content,method=response_info(options.rsp)
        if method['migration_kind']!='logical':raise ValueError('Choose a logical migration response file for resume.')
        if response_files.template(path):raise ValueError('Save a configured copy of the template before using it to resume.')
        rsp_digest=hashlib.sha256(content).hexdigest();args+=['-rsp',str(path)]
    for key in ['pauseafter','rerun','ignore','skip']:
        if values[key]:
            if key not in switches:raise ValueError('The installed ZDM release does not support resume -'+key+'.')
            args+=['-'+key,values[key]]
    revision=hashlib.sha256(json.dumps({'job':info['identity'],'args':args,'rsp_digest':rsp_digest,'restore_ack':options.restore_ack},sort_keys=True).encode()).hexdigest()
    return args,revision
