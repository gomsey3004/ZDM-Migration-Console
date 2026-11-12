import React,{useEffect,useState} from 'react';
import {X,Check,LoaderCircle,TriangleAlert,FileText} from 'lucide-react';

import ResponseFields from './ResponseFields.jsx';
import OciResponseProfile from './OciResponseProfile';
import ResponseWalletPicker from './ResponseWalletPicker';

const USERNAME_PARAMETER={WALLET_SOURCEADMIN:'SOURCEDATABASE_ADMINUSERNAME',WALLET_TARGETADMIN:'TARGETDATABASE_ADMINUSERNAME',WALLET_SOURCEGGADMIN:'SOURCEDATABASE_GGADMINUSERNAME',WALLET_TARGETGGADMIN:'TARGETDATABASE_GGADMINUSERNAME',WALLET_SOURCECONTAINER:'SOURCECONTAINERDATABASE_ADMINUSERNAME',WALLET_SOURCECGGADMIN:'SOURCECONTAINERDATABASE_GGADMINUSERNAME',WALLET_OGGADMIN:'GOLDENGATEHUB_ADMINUSERNAME',WALLET_OGGSERVICEADMIN:'GOLDENGATEHUB_ADMINUSERNAME'};
function selectedWallets(content,wallets){
 const values=Object.fromEntries(content.split(/\r?\n/).flatMap(line=>{const match=line.match(/^\s*([A-Z][A-Z0-9_.-]*)\s*=\s*(.*?)\s*$/);return match?[[match[1],match[2].replace(/^['"]|['"]$/g,'')]]:[]}));
 return wallets.filter(wallet=>USERNAME_PARAMETER[wallet.purpose]&&values[wallet.purpose]===wallet.path&&values[USERNAME_PARAMETER[wallet.purpose]]===wallet.username).map(wallet=>wallet.id);
}

export default function ResponseEditor({api,file,onClose,onSaved,readOnly=false,wallets=[],refresh,serviceUser}){
 const [mode,setMode]=useState('fields');
 const [ociProfile,setOciProfile]=useState('');
 const [walletIds,setWalletIds]=useState([]);
 const [loadedWalletIds,setLoadedWalletIds]=useState([]);
 const [loaded,setLoaded]=useState(null),[content,setContent]=useState(''),[name,setName]=useState(file.name.replace(/\.rsp$/i,'').slice(0,75)+'-copy'),[copy,setCopy]=useState(false),[busy,setBusy]=useState(true),[error,setError]=useState('');
 useEffect(()=>{let active=true;api('/responses/content?path='+encodeURIComponent(file.path)).then(data=>{if(active){const selected=selectedWallets(data.content,wallets);setLoaded(data);setContent(data.content);setWalletIds(selected);setLoadedWalletIds(selected);setCopy(!data.editable)}}).catch(e=>active&&setError(e.message)).finally(()=>active&&setBusy(false));return()=>{active=false}},[file.path]);
 async function save(e){e.preventDefault();if(readOnly)return;setBusy(true);setError('');try{
  const wallet_ids=/^\s*MIGRATION_METHOD\s*=\s*(?:ONLINE|OFFLINE)_LOGICAL\b/m.test(content)?walletIds:[];
  const result=copy?await api('/responses',{name,content,oci_profile_id:ociProfile,wallet_ids}):await api('/responses/content',{path:loaded.path,content,revision:loaded.revision,oci_profile_id:ociProfile,wallet_ids},'PUT');
  await onSaved(result,copy);
 }catch(e){setError(e.message)}finally{setBusy(false)}}
 const logical=/^\s*MIGRATION_METHOD\s*=\s*(?:ONLINE|OFFLINE)_LOGICAL\b/m.test(content);
 return <div className="scrim" onClick={()=>!busy&&onClose()}><section className="modal builder-modal response-editor-modal" role="dialog" aria-modal="true" aria-label={readOnly?"View response file":"Edit response file"} onClick={e=>e.stopPropagation()}>
  <header><div><h2>{readOnly?"View response file":"Edit response file"}</h2><p>{file.name}</p></div><button className="icon-button" aria-label="Close response editor" disabled={busy} onClick={onClose}><X size={17}/></button></header>
  <form onSubmit={save}><div className="modal-body">
   {error&&<div className="error" role="alert"><TriangleAlert size={17}/>{error}</div>}
   <p className="response-edit-path">{file.path}</p>
   {loaded?<>{!readOnly&&<label className="check-option"><input type="checkbox" checked={copy} disabled={busy||!loaded.editable} onChange={e=>setCopy(e.target.checked)}/>Save as a new response file</label>}
   {!readOnly&&!loaded.editable&&<p className="check-scope">This template or server file is protected. Save an editable copy to keep the original intact.</p>}
   {!readOnly&&copy&&<label className="field"><span>New response file name</span><input required maxLength={80} pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" value={name} onChange={e=>setName(e.target.value)}/></label>}
   {!readOnly&&<OciResponseProfile api={api} selected={ociProfile} onSelect={setOciProfile} physical={/^\s*MIGRATION_METHOD\s*=\s*(?:ONLINE|OFFLINE)_PHYSICAL\b/m.test(content)} busy={busy} onApply={async()=>{setBusy(true);setError('');try{setContent((await api('/responses/oci-preview',{content,oci_profile_id:ociProfile})).content)}catch(e){setError(e.message)}finally{setBusy(false)}}}/>}
   {!readOnly&&logical&&<ResponseWalletPicker api={api} items={wallets} refresh={refresh} serviceUser={serviceUser} selected={walletIds} onChange={setWalletIds} physical={false}/>}
   <div className="connection-tabs"><button type="button" className={mode==='fields'?'active':''} onClick={()=>setMode('fields')}>Fields & dropdowns</button><button type="button" className={mode==='text'?'active':''} onClick={()=>setMode('text')}>Text editor</button></div>
   {mode==='fields'?<ResponseFields api={api} content={content} onChange={setContent} readOnly={readOnly} busy={busy} fileName={file.name} boundOci={!!ociProfile}/>:<label className="field"><span>Response file contents</span><textarea required spellCheck="false" className="rsp-editor response-edit-text" value={content} readOnly={readOnly} disabled={busy} onChange={e=>setContent(e.target.value)}/></label>}
   <p className="check-scope" hidden={readOnly}>Save changes updates this file and keeps a backup. Profiles referencing this path use the updated settings and require fresh connection checks. Use wallets for passwords, then run ZDM Evaluate before migration. A new copy must be selected separately in your migration profile.</p></>:busy&&<div className="empty"><LoaderCircle className="spin"/>Loading response file…</div>}
  </div><footer className="modal-footer"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button>{!readOnly&&<button className="primary" disabled={busy||!loaded||!content.trim()||(!copy&&content===loaded.content&&!ociProfile&&walletIds.join(',')===loadedWalletIds.join(','))}>{busy?<LoaderCircle size={16} className="spin"/>:copy?<FileText size={16}/>:<Check size={16}/>} {copy?'Save new response file':'Save changes'}</button>}</footer></form>
 </section></div>
}
