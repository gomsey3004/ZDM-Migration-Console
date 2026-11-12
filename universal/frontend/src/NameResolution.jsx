import React,{useEffect,useState} from 'react';
import {Server,Plus,Trash2,Check,RefreshCw,TriangleAlert,Search,Save} from 'lucide-react';

function namesFrom(text){
 const quoted=[...text.matchAll(/"([^"\n]+)"/g)].flatMap(match=>match[1].split(/[,\s]+/));
 return [...new Set((quoted.length?quoted:text.split(/[,;\s]+/)).map(name=>name.trim().replace(/\.$/,'').toLowerCase()).filter(Boolean))];
}

export default function NameResolution({api}){
 const [snapshot,setSnapshot]=useState(null),[rows,setRows]=useState([]),[query,setQuery]=useState(''),[report,setReport]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 function loaded(data){setSnapshot(data);setRows(data.entries.map(row=>({ip:row.ip,names:row.names.join(' ')})))}
 async function load(){loaded(await api('/name-resolution'))}
 async function run(fn){setBusy(true);setError('');setNotice('');try{await fn()}catch(e){setError(e.message)}finally{setBusy(false)}}
 useEffect(()=>{run(load)},[]);
 const payload=()=>rows.filter(row=>row.ip.trim()||row.names.trim()).map(row=>({ip:row.ip.trim(),names:row.names.trim().split(/[,\s]+/).filter(Boolean)}));
 const dirty=snapshot&&JSON.stringify(payload())!==JSON.stringify(snapshot.entries);
 function addMissing(){
  const names=report?report.results.filter(row=>!row.ok).map(row=>row.name):namesFrom(query);
  const existing=new Set([...rows.flatMap(row=>row.names.split(/[,\s]+/)),...(snapshot?.protected_entries||[]).flatMap(row=>row.names)].map(name=>name.toLowerCase()));
  const missing=names.filter(name=>!existing.has(name));
  setRows([...rows,...missing.map(name=>({ip:'',names:name}))]);
  setNotice(missing.length?'Enter the correct IP address for each added hostname, then save.':'These hostnames already have hosts-file entries. Review them or check DNS.');
 }
 return <>
  <div className="section-toolbar"><span>Resolve source and target database node names from the ZDM host</span><button className="secondary" disabled={busy} onClick={()=>run(async()=>{await load();setReport(null)})}><RefreshCw size={15}/>Reload saved entries</button></div>
  {error&&<div className="error" role="alert"><TriangleAlert size={17}/>{error}</div>}
  {notice&&<div className="success-notice" role="status"><Check size={17}/>{notice}</div>}
  <section className="panel resolution-panel"><h2><Search size={19}/>Check hostnames or a ZDM error</h2><p>Paste the PRGZ-3900 error, or enter hostnames separated by commas, spaces, or new lines. Checks use the ZDM server’s system resolver and DNS separately.</p>
   <label className="field"><span>Hostnames / PRGZ-3900 error</span><textarea rows={4} className="resolution-query" placeholder={'source-node1 source-node2\ntarget-node1 target-node2'} value={query} onChange={e=>{setQuery(e.target.value);setReport(null)}}/></label>
   <div className="resolution-actions"><button className="primary" disabled={busy||!query.trim()} onClick={()=>run(async()=>setReport(await api('/name-resolution/check',{names:namesFrom(query)})))}><Search size={15}/>Check resolution</button><button className="secondary" disabled={busy||!query.trim()||!snapshot||snapshot.read_only} onClick={addMissing}><Plus size={15}/>{report?'Add unresolved names to entries':'Add names to entries'}</button></div>
   {report&&<><div className={'check-overall '+(report.ok?'pass':'fail')}>{report.ok?<Check size={18}/>:<TriangleAlert size={18}/>}<strong>{report.ok?'All names resolve from the ZDM host':'Some names are not resolvable'}</strong></div>
    <div className="resolution-results">{report.results.map(row=><article key={row.name} className={'resolution-result '+(row.ok?'pass':'fail')}><strong>{row.name}</strong><span className={'badge '+(row.ok?'succeeded':'failed')}>{row.ok?'Resolvable':'Unresolved'}</span><small>System resolver: {row.addresses.join(', ')||'No address found'}</small><small>DNS: {row.dns_addresses.join(', ')||'No DNS answer'}</small><small>Hosts file: {row.hosts_addresses.join(', ')||'No local entry'}</small><small>Resolution source: {row.source}</small></article>)}</div><p className="check-scope">{report.scope}</p></>}
  </section>
  <section className="panel resolution-panel"><h2><Server size={19}/>Managed /etc/hosts entries</h2><p>Add the IP address, canonical hostname, and any aliases for each source/target node. If DNS already resolves the required name correctly, a local entry is unnecessary. Saving changes only the GUI-managed block and keeps a backup.</p>
   {snapshot?.read_only?<p className="check-scope">Hostname entries are read-only in user installation mode. Resolution checks remain available. Ask your system administrator to update DNS or /etc/hosts.</p>:snapshot&&!snapshot.helper_available&&<div className="error"><TriangleAlert size={16}/>The hostname-management helper is unavailable. Resolution checks still work; saving requires zdm-hosts-manager.service.</div>}
   {rows.map((row,index)=><div className="host-entry-row" key={index}><label className="field"><span>IPv4 / IPv6 address</span><input aria-label={'IP address row '+(index+1)} value={row.ip} placeholder="10.0.1.10" disabled={busy||snapshot?.read_only} onChange={e=>setRows(rows.map((r,i)=>i===index?{...r,ip:e.target.value}:r))}/></label><label className="field"><span>Hostname and aliases</span><input aria-label={'Hostnames row '+(index+1)} value={row.names} placeholder="dbnode1.example.com dbnode1" disabled={busy||snapshot?.read_only} onChange={e=>setRows(rows.map((r,i)=>i===index?{...r,names:e.target.value}:r))}/></label><button className="icon-button" aria-label={'Remove host entry '+(index+1)} disabled={busy||snapshot?.read_only} onClick={()=>setRows(rows.filter((_,i)=>i!==index))}><Trash2 size={16}/></button></div>)}
   {!rows.length&&<p className="check-scope">No managed entries yet. Add a row, or paste your ZDM error above to add its unresolved hostnames.</p>}
   <div className="resolution-actions"><button className="secondary" disabled={busy||!snapshot||snapshot.read_only} onClick={()=>setRows([...rows,{ip:'',names:''}])}><Plus size={15}/>Add host entry</button><button className="primary" disabled={busy||!snapshot?.helper_available||!dirty} onClick={()=>run(async()=>{const result=await api('/name-resolution',{revision:snapshot.revision,entries:payload()},'PUT');loaded(result);setReport(null);setNotice(result.message+(result.backup?' Backup: '+result.backup:''))})}><Save size={15}/>Save hosts entries</button></div>
   <p className="check-scope">Changes affect name resolution on the ZDM server only. Run the resolution check again, then Check connections and ZDM Evaluate. Source and target servers may also need their own DNS or hosts entries for server-to-server connectivity.</p>
  </section>
  <section className="panel resolution-panel"><h2>Protected system entries</h2><p>Existing entries outside the GUI-managed block are preserved and cannot be overwritten here.</p><div className="protected-hosts">{snapshot?.protected_entries.map((row,index)=><div key={index}><code>{row.ip}</code><span>{row.names.join(' ')}</span></div>)}</div></section>
 </>
}
