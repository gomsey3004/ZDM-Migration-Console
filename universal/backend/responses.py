"""Form schema and deterministic response-file generation for supported ZDM releases."""
import json
import re
from pathlib import Path
from . import adapter,compatibility

METHODS={'logical':['OFFLINE_LOGICAL','ONLINE_LOGICAL'],'physical':['OFFLINE_PHYSICAL','ONLINE_PHYSICAL']}
MEDIA={'logical':['OSS','NFS','FSS','DBLINK','COPY','MANUAL_COPY','AMAZONS3'],
       'physical':['OSS','NFS','DIRECT','EXTBACKUP','ZDLRA','DBLINK']}
CORE={'logical':['MIGRATION_METHOD','DATA_TRANSFER_MEDIUM','SOURCEDATABASE_ADMINUSERNAME','TARGETDATABASE_ADMINUSERNAME'],
      'physical':['MIGRATION_METHOD','DATA_TRANSFER_MEDIUM','TGT_DB_UNIQUE_NAME']}
CHOICES={
 'TARGETDATABASE_DBTYPE':['ORACLE','ADBS','ADBD','ADBCC','EXADATA','ODA'],
 'SOURCEDATABASE_ENVIRONMENT_DBTYPE':['ORACLE','ADBS','ADBD','RDS_ORACLE'],
 'ZDM_RMAN_DIRECT_METHOD':['RESTORE_FROM_SERVICE','ACTIVE_DUPLICATE'],
 'ZDM_PDB_CLONE_METHOD':['COLD','HOT','REFRESHABLE'],
}
DEFAULTS={'SOURCEDATABASE_ADMINUSERNAME':'SYSTEM','TARGETDATABASE_ADMINUSERNAME':'ADMIN',
          'SOURCEDATABASE_CONNECTIONDETAILS_PORT':'1521','TARGETDATABASE_CONNECTIONDETAILS_PORT':'1521',
          'TARGETDATABASE_DBTYPE':'ORACLE','SOURCEDATABASE_ENVIRONMENT_DBTYPE':'ORACLE',
          'SOURCEDATABASE_GGADMINUSERNAME':'GGADMIN','TARGETDATABASE_GGADMINUSERNAME':'GGADMIN',
          'GOLDENGATEHUB_ADMINUSERNAME':'oggadmin','ZDM_RMAN_DIRECT_METHOD':'RESTORE_FROM_SERVICE'}

def template_path(kind):
    if kind not in METHODS:raise ValueError('Choose Logical or Physical migration.')
    home_template=adapter.HOME/'rhp/zdm/template'
    names=['zdm_logical_template.rsp'] if kind=='logical' else ['zdm_template.rsp','zdm_physical_template.rsp']
    candidates=[home_template/name for name in names]
    release=compatibility.require_supported()['release']
    for kit in sorted(adapter.STAGE.glob('zdm'+release+'*')):
        candidates.extend(sorted(kit.glob('template/*/zdm_'+kind+'_template.rsp')))
    for path in candidates:
        if path.is_file():return path
    searched=', '.join(str(path) for path in candidates)
    raise FileNotFoundError('The installed ZDM '+kind+' response template was not found. Checked: '+searched)

def read_template(kind):
    path=template_path(kind)
    try:return path.read_text()
    except OSError as exc:raise OSError('Cannot read the installed ZDM '+kind+' response template at '+str(path)+': '+str(exc)) from None

def catalog(kind):
    text=read_template(kind)
    fields={};comments=[]
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            comments.append(line.lstrip()[1:].strip());continue
        match=re.match(r'^\s*([A-Z][A-Z0-9_.-]*)\s*=(.*)$',line)
        if not match:continue
        key,value=match.groups()
        description=' '.join(c for c in comments[-18:] if c and not re.fullmatch(r'[-=#\s]+',c)
                             and not re.match(r'^[A-Z](?:-\d+)?\s*[.:]',c)
                             and not c.startswith(('Default value:','Allowed values','Modifiable on resume','[',key+'=')))
        choices=CHOICES.get(key,[])
        for c in comments[-18:]:
            if c.startswith('['):
                try:
                    parsed=json.loads(c)
                    if isinstance(parsed,list) and all(isinstance(x,str) for x in parsed):choices=parsed
                except (ValueError,TypeError):pass
        fields[key]={'name':key,'description':description[-1000:],'choices':choices,'default':DEFAULTS.get(key,value.strip())}
        comments=[]
    for key in CORE[kind]+required_fields(kind,{**DEFAULTS,'DATA_TRANSFER_MEDIUM':'OSS','MIGRATION_METHOD':METHODS[kind][0]}):
        fields.setdefault(key,{'name':key,'description':'','choices':CHOICES.get(key,[]),'default':DEFAULTS.get(key,'')})
    fields['MIGRATION_METHOD']['choices']=METHODS[kind]
    fields['MIGRATION_METHOD']['default']=METHODS[kind][0]
    fields['DATA_TRANSFER_MEDIUM']['choices']=MEDIA[kind]
    fields['DATA_TRANSFER_MEDIUM']['default']='OSS'
    return {'kind':kind,'core':CORE[kind],'rules':rules(kind),'fields':list(fields.values()),
            'note':'Required fields adapt to common method and transfer-medium choices. Platform-specific requirements and parameter combinations still require ZDM Evaluate.'}

def rules(kind):
    if kind=='physical':
        return [{'when':{'DATA_TRANSFER_MEDIUM':['NFS','EXTBACKUP']},'required':['BACKUP_PATH']},
                {'when':{'DATA_TRANSFER_MEDIUM':'OSS'},'required':['HOST','OPC_CONTAINER']},
                {'when':{'DATA_TRANSFER_MEDIUM':'DIRECT'},'required':['ZDM_RMAN_DIRECT_METHOD','ZDM_SRC_DB_RESTORE_SERVICE_NAME']},
                {'when':{'DATA_TRANSFER_MEDIUM':'DBLINK'},'required':['ZDM_PDB_CLONE_METHOD']}]
    return [{'when':{},'required':['TARGETDATABASE_DBTYPE','SOURCEDATABASE_ENVIRONMENT_DBTYPE']},
            {'when':{'SOURCEDATABASE_ENVIRONMENT_DBTYPE':['ADBS','ADBD']},'required':['SOURCEDATABASE_OCID']},
            {'when':{'SOURCEDATABASE_ENVIRONMENT_DBTYPE':{'not_in':['ADBS','ADBD']}},'required':['SOURCEDATABASE_CONNECTIONDETAILS_HOST','SOURCEDATABASE_CONNECTIONDETAILS_PORT','SOURCEDATABASE_CONNECTIONDETAILS_SERVICENAME']},
            {'when':{'TARGETDATABASE_DBTYPE':['ADBS','ADBD','ADBCC']},'required':['TARGETDATABASE_OCID']},
            {'when':{'TARGETDATABASE_DBTYPE':{'not_in':['ADBS','ADBD','ADBCC']}},'required':['TARGETDATABASE_CONNECTIONDETAILS_HOST','TARGETDATABASE_CONNECTIONDETAILS_PORT','TARGETDATABASE_CONNECTIONDETAILS_SERVICENAME']},
            {'when':{'MIGRATION_METHOD':'ONLINE_LOGICAL'},'required':['SOURCEDATABASE_GGADMINUSERNAME','TARGETDATABASE_GGADMINUSERNAME','GOLDENGATEHUB_ADMINUSERNAME','GOLDENGATEHUB_URL','GOLDENGATEHUB_SOURCEDEPLOYMENTNAME','GOLDENGATEHUB_TARGETDEPLOYMENTNAME']}]

def required_fields(kind,values):
    required=list(CORE[kind])
    values={**DEFAULTS,**values}
    for rule in rules(kind):
        matches=all((values.get(key) not in value['not_in'] if isinstance(value,dict) else values.get(key) in value if isinstance(value,list) else values.get(key)==value) for key,value in rule['when'].items())
        if matches:required+=rule['required']
    return list(dict.fromkeys(required))

def no_plaintext_secrets(values):
    for key,value in values.items():
        if key in {'DATAPUMPSETTINGS_CREATEAUTHTOKEN','DATAPUMPSETTINGS_USEAUTHTOKEN'} and value:
            if value.lower() not in {'true','false'}:raise ValueError(key+' must be true or false.')
        elif (key.startswith('WALLET_') or key in {'ZDM_WALLET_ODA_PASSWORD','ZDM_TGT_DBLINK_PASSWORD_WALLET','SRC_DB_PASSWORDFILE_LOC'}) and value:
            adapter.safe_path(value,[adapter.BASE,adapter.HOME,adapter.STAGE,adapter.OS_HOME],exists=False)
        elif re.search('PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE_KEY',key,re.I) and value:
            raise ValueError('Use a credential wallet instead of plaintext secret parameters.')

def render(kind,values):
    if kind not in METHODS:raise ValueError('Choose Logical or Physical migration.')
    clean={}
    if len(values)>400:raise ValueError('Too many response-file parameters.')
    for key,value in values.items():
        if not re.fullmatch('[A-Z][A-Z0-9_.-]{0,127}',key):raise ValueError('Invalid response-file parameter name.')
        if any(c in value for c in ['\n','\r','\x00']):raise ValueError('Parameter values must occupy a single line.')
        if len(value)>10000:raise ValueError('Parameter value is too long.')
        clean[key]=value.strip()
    if clean.get('MIGRATION_METHOD') not in METHODS[kind]:raise ValueError('Migration method does not match the Logical/Physical builder.')
    if clean.get('DATA_TRANSFER_MEDIUM') not in MEDIA[kind]:raise ValueError('Select a supported data transfer medium.')
    missing=[key for key in required_fields(kind,clean) if not clean.get(key)]
    if missing:raise ValueError('Required fields: '+', '.join(missing))
    for key,choices in CHOICES.items():
        if clean.get(key) and clean[key] not in choices:raise ValueError('Unsupported value for '+key)
    no_plaintext_secrets(clean)
    ordered=required_fields(kind,clean)
    optional=[key for key in clean if key not in ordered and clean[key]]
    release=compatibility.require_supported()['release']
    lines=['# Oracle Zero Downtime Migration '+release+' · '+kind.title()+' migration','# Required settings']
    lines += [key+'='+clean[key] for key in ordered]
    if optional:lines+=['','# Custom parameters']+[key+'='+clean[key] for key in optional]
    return '\n'.join(lines)+'\n'

OCI_FIELDS={
 'region':'OCIAUTHENTICATIONDETAILS_REGIONID',
 'tenancy':'OCIAUTHENTICATIONDETAILS_USERPRINCIPAL_TENANTID',
 'user':'OCIAUTHENTICATIONDETAILS_USERPRINCIPAL_USERID',
 'fingerprint':'OCIAUTHENTICATIONDETAILS_USERPRINCIPAL_FINGERPRINT',
 'key_file':'OCIAUTHENTICATIONDETAILS_USERPRINCIPAL_PRIVATEKEYFILE'}

def active_values(content):
    values={}
    for line in content.splitlines():
        match=re.match(r'^\s*([A-Z][A-Z0-9_.-]*)\s*=(.*)$',line)
        if not match:continue
        key,value=match.groups();value=value.strip().strip('\"\'')
        if key in values and values[key]!=value:raise ValueError('Conflicting duplicate response parameter: '+key)
        values[key]=value
    return values

def oci_values(profile):
    values={parameter:profile[field] for field,parameter in OCI_FIELDS.items()}
    if any(not isinstance(v,str) or not v or any(ord(c)<32 for c in v) for v in values.values()):raise ValueError('Saved OCI authentication values must be non-empty single-line text.')
    return values

def apply_oci(content,profile):
    from . import migration
    if migration.method_info(content)['migration_kind']!='logical':
        raise ValueError('Saved OCI API profile fields apply to logical response files. Physical migrations use their documented Object Storage/wallet authentication settings.')
    fields=oci_values(profile)
    lines=[];seen=set()
    for line in content.splitlines():
        if line.startswith('# GUI_OCI_PROFILE_ID='):continue
        match=re.match(r'^\s*([A-Z][A-Z0-9_.-]*)\s*=',line)
        key=match[1] if match else None
        if key in fields:
            if key not in seen:lines.append(key+'='+fields[key]);seen.add(key)
        else:lines.append(line)
    lines+=['','# OCI authentication copied from saved profile','# GUI_OCI_PROFILE_ID='+profile['id']]
    lines += [key+'='+value for key,value in fields.items() if key not in seen]
    return '\n'.join(lines)+'\n'

def marked_oci_id(content):
    ids=set(re.findall(r'^# GUI_OCI_PROFILE_ID=([a-f0-9]{32})\s*$',content,re.M))
    return ids.pop() if len(ids)==1 else ''
