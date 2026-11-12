import React,{useState} from 'react';
import {KeyRound,Plus,Check,ShieldCheck,Globe2,LoaderCircle,TriangleAlert} from 'lucide-react';
import InventoryDelete,{DeletedInventory} from './InventoryDelete';
import WalletCreateDialog from './WalletCreateDialog';

export default function Wallets({api,items,refresh,serviceUser='ZDM service account'}){
 const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const oggWallet=items.find(wallet=>['WALLET_OGGADMIN','WALLET_OGGSERVICEADMIN'].includes(wallet.purpose));
 const [gg,setGg]=useState({endpoint:'',username:oggWallet?.username||'oggadmin',password:'',allow_self_signed:true});
 const [ggBusy,setGgBusy]=useState(false),[ggError,setGgError]=useState(''),[ggReport,setGgReport]=useState(null);
 async function task(fn){setBusy(true);setError('');try{await fn()}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function checkGoldenGate(e){
  e.preventDefault();setGgBusy(true);setGgError('');setGgReport(null);
  try{const result=await api('/goldengate/rest/check',gg);setGgReport(result)}catch(e){setGgError(e.message)}
  finally{setGg(old=>({...old,password:''}));setGgBusy(false)}
 }
 return <>
  <div className="section-toolbar"><span>Oracle auto-login wallets · credentials read by ZDM during migration</span><button className="secondary" onClick={()=>{setNotice('');setError('');setOpen(true)}}><Plus size={16}/>Create credential wallet</button></div>
  {error&&<div className="error" role="alert">{error}</div>}
  {notice&&<div className="success-notice" role="status"><Check size={18}/>{notice}</div>}
  <div className="connection-grid">{items.map(wallet=><section key={wallet.id} className="panel connection-card"><div className="connection-card-top"><span className="tile-icon"><KeyRound size={20}/></span><span className="type-pill">Auto-login · cwallet.sso</span></div><h2>{wallet.name}</h2><p>{wallet.label}</p><div className="connection-facts"><span>Username</span><strong>{wallet.username}</strong><span>Parameter</span><strong>{wallet.parameter||wallet.purpose}</strong><span>Wallet directory</span><strong>{wallet.path}</strong><span>Credential alias</span><strong>store</strong></div><p className="last-check pass">Oracle wallet structure verified</p><footer><button className="secondary" disabled={busy} onClick={()=>task(async()=>{const result=await api('/wallets/'+wallet.id+'/verify',{});setNotice(result.message);await refresh()})}><ShieldCheck size={15}/>Verify wallet</button><InventoryDelete api={api} kind="wallets" item={wallet} refresh={refresh} disabled={busy}/></footer></section>)}</div>
  {!items.length&&<div className="panel empty"><div className="empty-icon"><KeyRound size={28}/></div><h3>Create database and GoldenGate credential wallets</h3><p>Enter each existing username and password once, then select the wallets when creating a response file.</p><button className="secondary" onClick={()=>setOpen(true)}><Plus size={16}/>Create wallet</button></div>}
  <section className="panel gg-rest-check">
   <div className="panel-heading"><div><h2><Globe2 size={20}/>GoldenGate REST connection</h2><p>Validate HTTPS access from this ZDM server to the GoldenGate deployments API.</p></div></div>
   <form onSubmit={checkGoldenGate}><div className="gg-check-fields">
    <label className="field"><span>GoldenGate HTTPS host or base URL</span><input required placeholder="https://ogg.example.com" value={gg.endpoint} onChange={e=>setGg({...gg,endpoint:e.target.value})}/><small>The GUI calls /services/v2/deployments. Do not include credentials or a path.</small></label>
    <div className="form-grid"><label className="field"><span>GoldenGate administrator username</span><input required autoComplete="username" value={gg.username} onChange={e=>setGg({...gg,username:e.target.value})}/></label><label className="field"><span>Password</span><input required type="password" autoComplete="current-password" value={gg.password} onChange={e=>setGg({...gg,password:e.target.value})}/></label></div>
    <label className="check-option"><input type="checkbox" checked={gg.allow_self_signed} onChange={e=>setGg({...gg,allow_self_signed:e.target.checked})}/>Allow the default GoldenGate self-signed HTTPS certificate for this check</label>
    {ggError&&<div className="error" role="alert"><TriangleAlert size={17}/>{ggError}</div>}
    {ggReport&&<div className="gg-check-result"><div className="check-overall pass"><Check size={18}/><strong>GoldenGate REST connection passed</strong></div>{ggReport.steps.map((step,index)=><div className="check-step pass" key={index}><span><Check size={15}/></span><div><strong>{step.name}</strong><p>{step.detail}</p></div><small>Passed</small></div>)}<p className="check-scope">{ggReport.scope}<br/>Endpoint: {ggReport.endpoint}<br/>Checked {new Date(ggReport.checked_at).toLocaleString()}.</p></div>}
   </div><footer className="modal-footer"><button className="primary" disabled={ggBusy}>{ggBusy?<LoaderCircle size={16} className="spin"/>:<ShieldCheck size={16}/>}Check GoldenGate REST</button></footer></form>
  </section>
  <div className="info"><ShieldCheck size={18}/><p>Choose credential wallets while creating a response file. The GUI inserts wallet paths and matching usernames into the response file. The GoldenGate check sends the entered password only for this HTTPS request and does not store it. This workflow checks only ZDM → GoldenGate REST; it does not test GoldenGate → source or target database connectivity.</p></div>
  <DeletedInventory api={api} kinds={['wallets']} refresh={refresh}/>
  {open&&<WalletCreateDialog api={api} serviceUser={serviceUser} onClose={()=>setOpen(false)} onCreated={async()=>{setOpen(false);setNotice('Wallet created. Select it in a response file.');await refresh()}}/>}
 </>
}
