import React,{useEffect,useState} from 'react';
import {ExternalLink,FileText,LoaderCircle,Settings2,X} from 'lucide-react';

export default function ModifyJob({api,jobId,requestKey,onClose,onQueued,onOpenResponses}){
 const [schema,setSchema]=useState(null),[rsp,setRsp]=useState(''),[preview,setPreview]=useState(null),[confirmation,setConfirmation]=useState(''),[busy,setBusy]=useState(true),[error,setError]=useState('');
 useEffect(()=>{let active=true;api('/jobs/'+jobId+'/modify-options').then(data=>active&&setSchema(data)).catch(e=>active&&setError(e.message)).finally(()=>active&&setBusy(false));return()=>{active=false}},[jobId]);
 function change(value){setRsp(value);setPreview(null);setConfirmation('');setError('')}
 async function review(){setBusy(true);setError('');setPreview(null);try{setPreview(await api('/jobs/'+jobId+'/modify/preview',{modify_options:{rsp}}))}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function submit(e){e.preventDefault();if(!preview)return;setBusy(true);setError('');try{await api('/jobs/'+jobId+'/modify',{modify_options:{rsp},revision:preview.revision,confirmation,request_key:requestKey});await onQueued()}catch(e){setError(e.message);setPreview(null);setConfirmation('')}finally{setBusy(false)}}
 return <div className="scrim" onClick={()=>!busy&&onClose()}><section className="modal wide resume-job-modal" role="dialog" aria-modal="true" aria-label={'Modify job #'+jobId} onClick={e=>e.stopPropagation()}>
  <header><div><h2>Modify job #{jobId}</h2><p>Running online logical migration · GoldenGate settings</p></div><button className="icon-button" aria-label="Close modify job" disabled={busy} onClick={onClose}><X size={17}/></button></header>
  <form onSubmit={submit}><div className="modal-body">
   {error&&<div className="error" role="alert">{error}</div>}
   {!schema?busy&&<p><LoaderCircle size={16} className="spin"/>Loading modify options…</p>:<>
    <div className="info"><Settings2 size={18}/><p><strong>What this changes</strong><br/>Oracle ZDM uses this command only to modify Oracle GoldenGate Extract and Replicat parameters in a running migration. It does not replace general source, target, wallet, or physical-migration settings.</p></div>
    <p className="check-scope">Verified method: {schema.migration_method||'not available'} · state: {schema.status}. Job details checked {new Date(schema.snapshot_checked_at).toLocaleString()}.</p>
    {!schema.can_modify&&<div className="error">{schema.reason}</div>}
    {schema.can_modify&&!schema.responses.length&&<div className="error">No configured ONLINE_LOGICAL response file is available. Create or save one on the Response files page first.</div>}
    <fieldset disabled={busy||!schema.can_modify} className="role-fieldset">
     <label className="field"><span>Updated online logical response file</span><select required aria-label="Updated online logical response file" value={rsp} onChange={e=>change(e.target.value)}><option value="">Select configured response file…</option>{schema.responses.map(file=><option key={file.path} value={file.path}>{file.name}</option>)}</select><small>Only configured ONLINE_LOGICAL files are shown. ZDM validates which GoldenGate parameters are modifiable at the current job phase.</small></label>
    </fieldset>
    <button type="button" className="text-button" onClick={onOpenResponses}><FileText size={15}/>Create or edit response files</button>
    <p className="modify-doc-link"><a href={schema.documentation} target="_blank" rel="noreferrer">Oracle ZDM modify job reference <ExternalLink size={13}/></a></p>
    {preview&&<><h4>Command preview</h4><pre className="command-preview">{preview.command}</pre><label className="field"><span>Type MODIFY {jobId} to confirm</span><input autoComplete="off" value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></label></>}
   </>}
  </div><footer className="modal-footer"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button>{!preview?<button type="button" className="primary" disabled={busy||!schema?.can_modify||!rsp} onClick={review}>{busy?<LoaderCircle size={16} className="spin"/>:<Settings2 size={16}/>}Review command</button>:<button className="primary" disabled={busy||confirmation!=='MODIFY '+jobId}>{busy?<LoaderCircle size={16} className="spin"/>:<Settings2 size={16}/>}Modify job</button>}</footer></form>
 </section></div>
}
