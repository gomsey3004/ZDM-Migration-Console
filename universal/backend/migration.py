"""Typed migrate-database catalog filtered by the installed ZDM CLI."""
import re
from datetime import datetime
from pathlib import Path

DOCUMENTATION_PATH='zero-downtime-migration-zdmcli-command-reference.html'
IGNORE=['ALL','WARNING','PATCH_CHECK','NLS_CHECK','NLS_NCHAR_CHECK','ENDIAN_CHECK','VAULT_CHECK','DB_NK_CACHE_SIZE_CHECK','DB_NAME_CHECK']
AUTH_MODES=['plugin','root','credential','user','sudo','none']
MODES=['evaluate','migrate','advisor','summary','listphases']

def option(name,label,group,type='text',help='',**extra):
    return {'name':name,'label':label,'group':group,'type':type,'help':help,**extra}

OPTIONS=[
 option('targethome','Target Oracle home','Database',type='remote_path',help='Oracle home on the target server (-targethome).'),
 option('advisor_mode','CPAT policy','Advisor & checks',type='choice',choices=['normal','ignoreadvisor','skipadvisor'],help='Run CPAT normally, ignore CPAT errors, or skip CPAT. Advisor-only and Summary are separate actions.'),
 option('genfixup','CPAT fixup scripts','Advisor & checks',type='choice',choices=['','NO','YES'],help='YES generates remedial scripts and runs remedies that CPAT can apply automatically.'),
 option('ignore','Ignore checks','Advisor & checks',type='multi',choices=IGNORE,help='Bypass only the checks you explicitly select.'),
 option('ignoremissingpatches','Missing patches to ignore','Advisor & checks',type='csv',help='Comma-separated patch identifiers.'),
 option('schedule','When to run','Timing & phases',type='schedule',help='NOW, PAUSE, or an ISO-8601 date/time with time-zone offset. Applies to submitted migration/evaluation/advisor jobs.'),
 option('jobtag','Job tag','Timing & phases',help='Optional tag supported by the installed CLI.'),
 option('pauseafter','Pause after phase','Timing & phases',type='phase',help='Pause at this phase for Start Migration; the job can later be resumed. The GUI omits this option from Evaluate because ZDM does not accept -pauseafter with -eval.'),
 option('stopafter','Stop after phase','Timing & phases',type='phase',help='Finish the workflow at this phase. The job cannot resume beyond this stopping point.'),
 option('incrementalinterval','Incremental backup interval (minutes)','Timing & phases',type='integer',help='Interval between incremental RMAN backup/restore operations.'),
 option('backupuser','Backup user','Backup & encryption',type='word'),
 option('restoreuser','Restore user','Backup & encryption',type='word'),
 option('dvowner','Database Vault owner','Backup & encryption',type='word'),
 option('sourcesyswallet','Source SYS wallet','Backup & encryption',type='wallet'),
 option('tdekeystorewallet','Source TDE password wallet','Backup & encryption',type='wallet',help='Also covers the documented -tdewallet alias.'),
 option('tgttdekeystorewallet','Target TDE password wallet','Backup & encryption',type='wallet',help='Also covers the documented -tgttdewallet alias.'),
 option('osswallet','Object Storage backup wallet','Backup & encryption',type='wallet'),
 option('ossrestorewallet','Object Storage restore wallet','Backup & encryption',type='wallet'),
 option('dvwallet','Database Vault wallet','Backup & encryption',type='wallet'),
 option('backupwallet','RMAN backup password wallet','Backup & encryption',type='wallet'),
 option('tdekeystorepasswd','Prompt for source TDE password','Credential prompts',type='flag'),
 option('tgttdekeystorepasswd','Prompt for target TDE password','Credential prompts',type='flag'),
 option('tdemasterkey','Prompt for TDE master key','Credential prompts',type='flag'),
 option('backuppasswd','Prompt for RMAN backup password','Credential prompts',type='flag'),
 option('okvkeystorepasswd','Prompt for source OKV password','Credential prompts',type='flag'),
 option('tgtokvkeystorepasswd','Prompt for target OKV password','Credential prompts',type='flag'),
 option('imagetype','User-action image type','User actions',type='word',help='Use an image type already registered in ZDM.'),
 option('useractiondata','User-action data','User actions',help='Non-secret data passed to registered user-action scripts.'),
 option('srcred','Source OS credential wallet','Compatibility',type='wallet',help='Documented as -srcred in the 26.1 reference, but not advertised in this installation’s help. ZDM validates compatibility during Evaluate. This differs from -srccred, which is an authentication credential name.'),
]
BY_NAME={row['name']:row for row in OPTIONS}
ALIASES={'tdewallet':'tdekeystorewallet','tgttdewallet':'tgttdekeystorewallet'}
CPAT_OPTIONS={'advisor_mode','genfixup'}

def capabilities():
    from . import compatibility
    detected=compatibility.require_supported()
    return detected,set(detected['capabilities']['migrate_switches'])

def command_name(name,switches):
    if name in switches:return name
    for alias,canonical in ALIASES.items():
        if canonical==name and alias in switches:return alias
    return ''

def supported_option(name,switches):
    if name=='advisor_mode':return bool({'ignoreadvisor','skipadvisor'} & switches)
    return bool(command_name(name,switches))

def available_options():
    detected,switches=capabilities();rows=[]
    for original in OPTIONS:
        if not supported_option(original['name'],switches):continue
        row=dict(original)
        if row['name']=='ignore' and detected['release']!='26.1':
            row['choices']=[choice for choice in row['choices'] if choice!='DB_NAME_CHECK']
        rows.append(row)
    return detected,switches,rows

def method_info(content):
    values=re.findall(r'^[ \t]*MIGRATION_METHOD[ \t]*=[ \t]*([^\r\n#]*)',content,re.M)
    values=list(dict.fromkeys(value.strip() for value in values))
    method=values[0] if len(values)==1 else ''
    kind='logical' if method in {'ONLINE_LOGICAL','OFFLINE_LOGICAL'} else 'physical' if method in {'ONLINE_PHYSICAL','OFFLINE_PHYSICAL'} else 'hybrid' if method in {'XTTS_OFFLINE','OFFLINE_XTTS'} else 'unknown'
    return {'migration_method':method,'migration_kind':kind,
            'available_actions':['evaluate','migrate','listphases']+(['advisor','summary'] if kind=='logical' else [])}

def target_info(content):
    from . import responses
    relevant='\n'.join(line for line in content.splitlines() if re.match(r'^\s*(?:TARGETDATABASE_DBTYPE|TARGETDATABASE_OCID)\s*=',line))
    try:values=responses.active_values(relevant)
    except ValueError:values={}
    kind=method_info(content)['migration_kind']
    dbtype=values.get('TARGETDATABASE_DBTYPE','')
    return {'autonomous_target':kind=='logical' and dbtype in {'ADBS','ADBD','ADBCC'},
            'target_dbtype':dbtype,'target_ocid':values.get('TARGETDATABASE_OCID',''),
            'response_oci_profile_id':responses.marked_oci_id(content)}

def describe(profile):
    from . import adapter
    try:
        path=adapter.safe_path(profile['rsp'],[adapter.STAGE,adapter.HOME])
        content=Path(path).read_text()
        return {**method_info(content),**target_info(content)}
    except (OSError,ValueError,KeyError):return {**method_info(''),**target_info('')}

def text(value,label):
    if not isinstance(value,str) or len(value)>2000 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError(label+' must be single-line text of at most 2000 characters.')
    if 'PRIVATE KEY-----' in value or re.search(r'(?i)(?:password|passwd|secret|token)\s*[:=]',value):
        raise ValueError('Enter secrets only in the secure credential prompt or an Oracle wallet.')
    return value.strip()

def word(value,label):
    value=text(value,label)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.$#@:/-]{0,253}',value):raise ValueError('Invalid '+label+'.')
    return value

def remote_path(value,label):
    value=text(value,label)
    if not value.startswith('/') or not re.fullmatch(r'[A-Za-z0-9_./:@+-]+',value) or '..' in Path(value).parts:
        raise ValueError(label+' must be an absolute server path without shell syntax.')
    return value

def clean_options(raw):
    if not isinstance(raw,dict):raise ValueError('Migration options must be named fields.')
    detected,switches,_=available_options();clean={}
    for original,value in raw.items():
        name=ALIASES.get(original,original)
        if name not in BY_NAME or not supported_option(name,switches):raise ValueError('The installed ZDM release does not support this migration option.')
        if value is None or value=='' or value==[]:continue
        if name in clean:raise ValueError('Configure only one value for '+name+'.')
        row=BY_NAME[name];kind=row['type']
        if kind=='flag':
            if value is False:continue
            if value is not True:raise ValueError(row['label']+' must be a checkbox value.')
            clean[name]=True;continue
        if kind=='multi':
            choices=row['choices'] if detected['release']=='26.1' else [item for item in row['choices'] if item!='DB_NAME_CHECK']
            if not isinstance(value,list) or any(item not in choices for item in value):raise ValueError('Select supported ignore checks.')
            if 'ALL' in value and len(value)>1:raise ValueError('Choose ALL or individual ignore checks, not both.')
            clean[name]=list(dict.fromkeys(value));continue
        if kind=='integer':
            if isinstance(value,bool) or not str(value).isdigit() or not 1<=int(value)<=525600:raise ValueError('Incremental interval must be 1–525600 minutes.')
            clean[name]=str(int(value));continue
        value=text(value,row['label'])
        if kind=='choice' and value not in row['choices']:raise ValueError('Unsupported '+row['label']+'.')
        if kind=='word':value=word(value,row['label'])
        if kind=='remote_path':value=remote_path(value,row['label'])
        if kind=='phase' and not re.fullmatch('ZDM_[A-Z0-9_]+',value):raise ValueError('Select a ZDM_ phase for '+row['label']+'.')
        if kind=='csv':value=','.join(word(v.strip(),row['label']) for v in value.split(','))
        if kind=='schedule' and value not in {'NOW','PAUSE'}:
            try:
                instant=datetime.fromisoformat(value.replace('Z','+00:00'))
                if instant.tzinfo is None:raise ValueError()
                value=instant.isoformat(timespec='seconds')
            except ValueError:raise ValueError('Use NOW, PAUSE, or an ISO-8601 schedule with a time-zone offset.') from None
        clean[name]=value
    if clean.get('advisor_mode')=='skipadvisor' and clean.get('genfixup')=='YES':
        raise ValueError('CPAT fixups cannot run when CPAT is skipped.')
    for flag,wallet in [('tdekeystorepasswd','tdekeystorewallet'),('tgttdekeystorepasswd','tgttdekeystorewallet'),('backuppasswd','backupwallet')]:
        if clean.get(flag) and clean.get(wallet):raise ValueError('Choose a wallet or an interactive prompt for '+flag+', not both.')
    return clean

def schema():
    detected,switches,options=available_options()
    actions=['migrate']+[name for name in ['eval','advisor','summary','listphases'] if name in switches]
    return {'options':options,'auth_modes':AUTH_MODES,'execution_modes':MODES,
            'documentation':detected['documentation']+DOCUMENTATION_PATH,
            'zdm_release':detected['release'],'detected_switches':sorted(switches),
            'coverage':{'core':['sourcedb','sourcesid','rsp','sourcenode','targetnode'],
                        'actions':actions,
                        'authentication':['srcroot','srccred','srcuser','srcsudouser','srcsudopath','srcauth','srcarg1..15','tgtroot','tgtcred','tgtuser','tgtsudouser','tgtsudopath','tgtauth','tgtarg1..15'],
                        'aliases':ALIASES},
            'notes':['Password options are flags only. ZDM prompts are answered through masked HTTPS forms; secret values never enter profiles, CLI arguments, or history.',
                     'The reference repeats -tgtcred with different descriptions; the installed CLI advertises it as a named OS authentication credential.']}

def authentication(profile,side):
    from . import adapter
    prefix='src' if side=='source' else 'tgt'
    mode=profile.get(side+'_auth_mode','plugin')
    if mode not in AUTH_MODES:raise ValueError('Choose a supported '+side+' authentication mode.')
    if mode=='none':return []
    if mode=='root':return ['-'+prefix+'root']
    if mode=='credential':
        value=profile.get(side+'_credential','')
        if side=='target' and value.startswith('/'):
            from .wallets import directory
            value=str(directory(value))
        else:value=word(value,side+' credential name')
        return ['-'+prefix+'cred',value]
    user=word(profile.get(side+'_user','opc'),side+' OS user')
    if mode=='user':return ['-'+prefix+'user',user]
    sudo=remote_path(profile.get(side+'_sudo_path','/usr/bin/sudo'),side+' sudo path')
    if mode=='sudo':return ['-'+prefix+'sudouser',user,'-'+prefix+'sudopath',sudo]
    plugin=word(profile.get(side+'_auth_plugin','zdmauth'),side+' authentication plugin')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',plugin):raise ValueError('Use an authentication plugin name, without paths or shell syntax.')
    args=['-'+prefix+'auth',plugin]
    if plugin in {'zdmauth','dbuser','sudoasuser'}:
        key=adapter.safe_path(profile.get(side+'_key',''),[adapter.BASE,adapter.OS_HOME/'.ssh'])
        params={'1':'user:'+user,'2':'identity_file:'+key,'3':'sudo_location:'+sudo}
        port=profile.get(side+'_ssh_port',22)
        if not isinstance(port,int) or not 1<=port<=65535:raise ValueError('Invalid SSH port.')
        if port!=22:params['15' if plugin=='sudoasuser' else '14']='ssh_port='+str(port)
    else:params={}
    overrides=profile.get(side+'_auth_args',{})
    if not isinstance(overrides,dict):raise ValueError('Authentication arguments must be named by their argument number.')
    for index,value in overrides.items():
        if not str(index).isdigit() or not 1<=int(index)<=15:raise ValueError('Authentication argument numbers must be between 1 and 15.')
        value=text(value,'Authentication argument')
        if not value:continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*[:=].+',value):raise ValueError('Use property:value or property=value for plugin arguments.')
        if re.search(r'(?i)password|passwd|secret|token|private_key',value.split(':',1)[0].split('=',1)[0]):
            raise ValueError('Use a secure credential prompt rather than secret authentication arguments.')
        property_name,property_value=re.split('[:=]',value,maxsplit=1)
        if property_name=='identity_file':adapter.safe_path(property_value,[adapter.BASE,adapter.OS_HOME/'.ssh'])
        if property_name=='user':word(property_value,'Plugin OS user')
        if property_name=='sudo_location':remote_path(property_value,'Plugin sudo path')
        if property_name=='ssh_port' and (not property_value.isdigit() or not 1<=int(property_value)<=65535):raise ValueError('Invalid plugin SSH port.')
        params[str(int(index))]=value
    for index in sorted(params,key=int):args+=['-'+prefix+'arg'+index,params[index]]
    return args

def build(profile,evaluate=True,mode=None):
    from . import adapter,wallets
    detected,switches,_=available_options()
    mode=mode or ('evaluate' if evaluate else 'migrate')
    if mode not in MODES:raise ValueError('Unsupported migration execution mode.')
    rsp=adapter.safe_path(profile['rsp'],[adapter.STAGE,adapter.HOME])
    content=Path(rsp).read_text()
    info=method_info(content)
    logical=info['migration_kind']=='logical'
    physical=info['migration_kind']=='physical'
    action_switch={'evaluate':'eval','advisor':'advisor','summary':'summary','listphases':'listphases'}.get(mode)
    if action_switch and action_switch not in switches:
        raise ValueError(f'ZDM {detected["release"]} does not support the {mode} action advertised by this profile.')
    if mode in {'advisor','summary'} and not logical:
        raise ValueError('Advisor and CPAT summary are available only for logical migrations. Set MIGRATION_METHOD to ONLINE_LOGICAL or OFFLINE_LOGICAL in the response file.')
    args=['migrate','database']
    identifier=profile.get('source_identifier','') if not logical or profile.get('source_host') else ''
    kind=profile.get('identifier_type','sourcedb')
    if kind not in {'sourcedb','sourcesid','none'}:raise ValueError('Choose a valid database identifier type.')
    if physical and (not identifier or kind=='none'):
        raise ValueError('A physical migration requires a database identifier using -sourcedb or -sourcesid.')
    if identifier and kind!='none':args+=['-'+kind,adapter.word(identifier,'database identifier')]
    args+=['-rsp',rsp]
    for side in ['source','target']:
        if side=='target' and target_info(content)['autonomous_target']:continue
        node=profile.get(side+'_host','')
        if not node:
            if side=='source' and not logical:raise ValueError('A source host is required for physical migration.')
            continue
        args+=['-sourcenode' if side=='source' else '-targetnode',adapter.host(node)]
        if not logical and profile.get(side+'_auth_mode','plugin')=='none':raise ValueError('Physical migration requires OS authentication for '+side+'.')
        args+=authentication(profile,side)
    raw=dict(profile.get('migration_options',{}))
    if target_info(content)['autonomous_target']:raw.pop('targethome',None)
    if not logical:
        for name in CPAT_OPTIONS:raw.pop(name,None)
    if profile.get('pause_after') and not raw.get('pauseafter'):raw['pauseafter']=profile['pause_after']
    for name,value in profile.get('wallets',{}).items():
        if name not in adapter.WALLETS:raise ValueError('Unsupported wallet option: '+name)
        if value:
            if name in raw and raw[name]!=value:raise ValueError('Conflicting wallet values for '+name+'.')
            raw[name]=value
    clean=clean_options(raw)
    for name,value in clean.items():
        # The installed ZDM CLI rejects -pauseafter together with -eval.
        # Retain the profile setting for migration, but omit it from evaluation.
        if name=='pauseafter' and mode=='evaluate':continue
        if name=='advisor_mode':
            if mode in {'evaluate','migrate'} and value!='normal':args+=['-'+value]
            continue
        if name in {'schedule','jobtag'} and mode in {'listphases','summary'}:continue
        if name=='genfixup' and mode in {'summary','listphases'}:continue
        row=BY_NAME[name];cli_name=command_name(name,switches)
        if row['type']=='flag':args+=['-'+cli_name];continue
        if row['type']=='wallet':value=str(wallets.directory(value))
        if isinstance(value,list):value=','.join(value)
        args+=['-'+cli_name,str(value)]
    if mode=='evaluate':args+=['-eval']
    elif mode!='migrate':args+=['-'+('listphases' if mode=='listphases' else mode)]
    return args
