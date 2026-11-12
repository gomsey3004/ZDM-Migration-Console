import React,{useState} from 'react';
import {Search,Plus,Trash2,ChevronDown} from 'lucide-react';

function localDate(value){if(!value||['NOW','PAUSE'].includes(value))return '';const date=new Date(value);if(!Number.isFinite(date.getTime()))return '';return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16)}

const WALLET_PURPOSES={
 sourcesyswallet:'SOURCE_SYS_PHYSICAL',
 tdekeystorewallet:'PHYSICAL_SOURCE_TDE',
 tgttdekeystorewallet:'PHYSICAL_TARGET_TDE',
 osswallet:'PHYSICAL_OSS_BACKUP',
 ossrestorewallet:'PHYSICAL_OSS_RESTORE',
 dvwallet:'PHYSICAL_DATABASE_VAULT',
 backupwallet:'PHYSICAL_RMAN_BACKUP'
};
const USER_PURPOSES={backupuser:'PHYSICAL_OSS_BACKUP',restoreuser:'PHYSICAL_OSS_RESTORE',dvowner:'PHYSICAL_DATABASE_VAULT'};
const WALLET_USERS={osswallet:'backupuser',ossrestorewallet:'restoreuser',dvwallet:'dvowner'};

export default function MigrationOptions({schema,form,setForm,busy,wallets=[]}){
 const [search,setSearch]=useState(''),[manual,setManual]=useState([]);
 const options={...(form.migration_options||{})};
 if(form.pause_after&&!options.pauseafter)options.pauseafter=form.pause_after;
 Object.entries(form.wallets||{}).forEach(([key,value])=>{if(value&&!options[key])options[key]=value});
 function change(name,value){const next={...(form.migration_options||{}),[name]:value};const wallets={...(form.wallets||{})};delete wallets[name];setForm({...form,pause_after:name==='pauseafter'?'':form.pause_after,wallets,migration_options:next})}
 function available(purpose){return wallets.filter(wallet=>wallet.purpose===purpose)}
 function withoutPurpose(purpose){return (form.wallet_ids||[]).filter(id=>wallets.find(wallet=>wallet.id===id)?.purpose!==purpose)}
 function chooseSaved(row,value,purpose,useUsername=false){
  if(value==='__manual__'){
   setManual(old=>[...new Set([...old,row.name])]);
   const next={...(form.migration_options||{}),[row.name]:''};
   const legacyWallets={...(form.wallets||{})};delete legacyWallets[row.name];
   setForm({...form,wallet_ids:row.type==='wallet'?withoutPurpose(purpose):form.wallet_ids,wallets:legacyWallets,migration_options:next});return
  }
  setManual(old=>old.filter(name=>name!==row.name));
  const next={...(form.migration_options||{})};
  const legacyWallets={...(form.wallets||{})};delete legacyWallets[row.name];
  const selected=wallets.find(wallet=>wallet.purpose===purpose&&(useUsername?wallet.username:wallet.path)===value);
  if(row.type==='wallet')delete next[row.name];else next[row.name]=value;
  const userOption=WALLET_USERS[row.name];
  if(selected&&userOption)next[userOption]=selected.username;
  const walletIds=row.type==='wallet'?[...withoutPurpose(purpose),...(selected?[selected.id]:[])]:form.wallet_ids;
  setForm({...form,wallet_ids:walletIds,wallets:legacyWallets,migration_options:next});
 }
 function savedControl(row,value,purpose,useUsername=false){
  const saved=available(purpose);const selected=saved.find(wallet=>(useUsername?wallet.username:wallet.path)===value);
  const manualMode=manual.includes(row.name)||(!selected&&!!value);
  return <><select aria-label={row.label} value={selected?(useUsername?selected.username:selected.path):manualMode?'__manual__':''} onChange={e=>chooseSaved(row,e.target.value,purpose,useUsername)}><option value="">Not configured</option>{saved.map(wallet=><option key={wallet.id} value={useUsername?wallet.username:wallet.path}>{wallet.name} · {wallet.username}</option>)}<option value="__manual__">Enter manually…</option></select>{manualMode&&<input aria-label={row.label+' manual value'} value={value} onChange={e=>change(row.name,e.target.value)} placeholder={useUsername?'Enter username':'Absolute Oracle wallet directory'}/>} {!saved.length&&<small>No matching saved wallet yet. Create one under DB & GG wallets, or enter a value manually.</small>}</>
 }
 const definitions=schema?.options||[];
 const groups=[...new Set(definitions.map(row=>row.group))];
 function field(row){const linked=row.type==='wallet'&&WALLET_PURPOSES[row.name]?wallets.find(wallet=>(form.wallet_ids||[]).includes(wallet.id)&&wallet.purpose===WALLET_PURPOSES[row.name]):null;const value=options[row.name]??linked?.path??'';const label=row.label;let control;
  if(row.type==='flag')control=<label className="check-option"><input type="checkbox" checked={value===true} onChange={e=>change(row.name,e.target.checked)}/>{label}<code>-{row.name}</code></label>;
  else if(row.type==='multi')control=<div className="migration-checks">{row.choices.map(choice=><label key={choice} className="check-option"><input type="checkbox" checked={Array.isArray(value)&&value.includes(choice)} onChange={e=>change(row.name,e.target.checked?(choice==='ALL'?['ALL']:[...(Array.isArray(value)?value.filter(v=>v!=='ALL'):[]),choice]):value.filter(v=>v!==choice))}/>{choice}</label>)}</div>;
  else if(row.type==='choice')control=<select aria-label={label} value={value} onChange={e=>change(row.name,e.target.value)}><option value="">Default</option>{row.choices.filter(Boolean).map(choice=><option key={choice} value={choice}>{({normal:'Run CPAT normally',ignoreadvisor:'Run CPAT; ignore its errors',skipadvisor:'Skip CPAT',NO:'No automatic fixups',YES:'Generate and apply available fixups'})[choice]||choice}</option>)}</select>;
  else if(row.type==='schedule')control=<><select value={!value||value==='NOW'?'NOW':value==='PAUSE'?'PAUSE':'AT'} onChange={e=>change(row.name,e.target.value==='AT'?new Date(Date.now()+3600000).toISOString():e.target.value)}><option value="NOW">Start when submitted</option><option value="AT">Schedule a date and time</option><option value="PAUSE">Create a paused job</option></select>{value&&!['NOW','PAUSE'].includes(value)&&<><input type="datetime-local" value={localDate(value)} onChange={e=>change(row.name,e.target.value?new Date(e.target.value).toISOString():'')}/><small>Your local time: {Intl.DateTimeFormat().resolvedOptions().timeZone}. Sent to ZDM with a UTC offset.</small></>}</>;
  else if(row.type==='wallet'&&WALLET_PURPOSES[row.name])control=savedControl(row,value,WALLET_PURPOSES[row.name]);
  else if(USER_PURPOSES[row.name])control=savedControl(row,value,USER_PURPOSES[row.name],true);
  else control=<input aria-label={label} type={row.type==='integer'?'number':'text'} min={row.type==='integer'?1:undefined} value={value} onChange={e=>change(row.name,e.target.value)} placeholder={row.type==='wallet'?'Absolute Oracle wallet directory':row.type==='phase'?'ZDM_PHASE_NAME':''}/>;
  return <div className="field migration-option" key={row.name}>{row.type!=='flag'&&<label><span>{label}</span><code>-{row.name==='advisor_mode'?'ignoreadvisor / -skipadvisor':row.name}</code></label>}{control}{row.help&&<small>{row.help}</small>}</div>
 }
 function auth(side){if(side==='source'&&schema?.migration_kind==='logical'&&form.source_access==='response')return <p className="check-scope" key={side}>Source SSH is omitted. Database connectivity and credentials come from the response file.</p>;const mode=form[side+'_auth_mode']||'plugin';const args=form[side+'_auth_args']||{};return <section className="migration-auth" key={side}><h4>{side==='source'?'Source':'Target'} authentication</h4><label className="field"><span>Authentication method</span><select value={mode} onChange={e=>setForm({...form,[side+'_auth_mode']:e.target.value})}><option value="plugin">SSH authentication plugin</option><option value="root">Root credential (-{side==='source'?'src':'tgt'}root)</option><option value="credential">Registered ZDM credential</option><option value="user">Privileged OS user</option><option value="sudo">Sudo OS user</option><option value="none">No OS authentication; response-file connection</option></select></label>
  {mode==='plugin'&&<><div className="form-grid"><label className="field"><span>Authentication plugin</span><input list={'migration-auth-plugins-'+side} value={form[side+'_auth_plugin']||'zdmauth'} onChange={e=>setForm({...form,[side+'_auth_plugin']:e.target.value})}/><datalist id={'migration-auth-plugins-'+side}><option value="zdmauth"/><option value="dbuser"/><option value="sudoasuser"/></datalist></label><label className="field"><span>SSH private-key path</span><input value={form[side+'_key']} disabled={!!form[side+'_server_id']} onChange={e=>setForm({...form,[side+'_key']:e.target.value})}/></label></div><p className="check-scope">Standard plugins get generated user, identity_file, sudo_location, and SSH-port arguments. Add numbered overrides only when required by the chosen plugin. Enter passwords in the masked runtime prompt.</p>
   {Object.entries(args).sort(([a],[b])=>Number(a)-Number(b)).map(([number,value])=><div className="plugin-arg" key={number}><select aria-label={side+' argument number '+number} value={number} onChange={e=>{const next={...args};delete next[number];next[e.target.value]=value;setForm({...form,[side+'_auth_args']:next})}}>{Array.from({length:15},(_,i)=>String(i+1)).map(n=><option key={n} disabled={n!==number&&n in args} value={n}>{side==='source'?'src':'tgt'}arg{n}</option>)}</select><input aria-label={side+' argument '+number} value={value} placeholder="property:value" onChange={e=>setForm({...form,[side+'_auth_args']:{...args,[number]:e.target.value}})}/><button type="button" className="icon-button" aria-label={'Remove '+side+' argument '+number} onClick={()=>{const next={...args};delete next[number];setForm({...form,[side+'_auth_args']:next})}}><Trash2 size={15}/></button></div>)}<button type="button" className="secondary" disabled={Object.keys(args).length>=15} onClick={()=>{const number=[4,5,6,7,8,9,10,11,12,13,14,15,1,2,3].find(n=>!(String(n) in args));setForm({...form,[side+'_auth_args']:{...args,[number]:''}})}}><Plus size={15}/>Plugin argument</button></>}
  {mode==='credential'&&<label className="field"><span>{side==='target'?'ZDM credential name / documented wallet path':'Existing ZDM credential name'}</span><input value={form[side+'_credential']||''} onChange={e=>setForm({...form,[side+'_credential']:e.target.value})}/><small>A credential reference registered in ZDM, not a password.</small></label>}
  {['plugin','sudo'].includes(mode)&&<label className="field"><span>Sudo binary on {side}</span><input value={form[side+'_sudo_path']||'/usr/bin/sudo'} onChange={e=>setForm({...form,[side+'_sudo_path']:e.target.value})}/></label>}
  <p className="check-scope">Saved connection checks verify the saved network/SSH identity. ZDM Evaluate verifies the authentication mode selected here. No OS authentication is intended for logical response-file workflows such as Autonomous or RDS.</p>
 </section>}
 return <fieldset disabled={busy} className="migration-options"><div className="search"><Search size={15}/><input aria-label="Search migrate options" placeholder="Search CLI options…" value={search} onChange={e=>setSearch(e.target.value)}/></div><details className="advanced"><summary><ChevronDown size={14} className="advanced-chevron"/>{schema?.autonomous_target?'Source authentication':'Source & target authentication'}</summary>{(schema?.autonomous_target?['source']:['source','target']).map(auth)}</details>{groups.map(group=>{const fields=definitions.filter(row=>row.group===group&&(!search||(row.name+' '+row.label+' '+row.help).toLowerCase().includes(search.toLowerCase())));return fields.length?<details className="advanced" key={group} open={search?true:undefined}><summary><ChevronDown size={14} className="advanced-chevron"/>{group}<small>{fields.filter(row=>options[row.name]).length} configured</small></summary><div className="form-grid">{fields.map(field)}</div></details>:null})}<div className="info"><Search size={17}/><p>Evaluate and List phases are available for saved profiles. Advisor-only and CPAT Summary appear only for logical migrations. Password flags open masked credential prompts when ZDM asks. <a href={schema?.documentation} target="_blank" rel="noreferrer">Oracle command reference</a></p></div></fieldset>
}
