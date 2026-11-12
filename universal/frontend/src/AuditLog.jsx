import React,{useCallback,useEffect,useState} from 'react';
import {Activity,LoaderCircle,RefreshCw,ShieldCheck,TriangleAlert} from 'lucide-react';

const actionLabel=row=>{
 if(row.event==='sign_in_succeeded')return 'Signed in';
 if(row.event==='sign_in_failed')return 'Sign-in failed';
 if(row.event==='sign_out')return 'Signed out';
 const path=row.path.replace(/^\/api\/?/,'').replaceAll('/',' · ').replaceAll('-',' ');
 const verb=({GET:'Viewed',HEAD:'Viewed',POST:'Submitted',PUT:'Updated',PATCH:'Updated',DELETE:'Deleted'})[row.method]||row.method;
 return `${verb} ${path||'console'}`;
};

export default function AuditLog({api}){
 const [events,setEvents]=useState([]),[loading,setLoading]=useState(true),[error,setError]=useState('');
 const load=useCallback(async()=>{setLoading(true);setError('');try{const data=await api('/audit/events?limit=500');setEvents(data.events)}catch(e){setError(e.message)}finally{setLoading(false)}},[api]);
 useEffect(()=>{load()},[load]);
 return <section className="panel activity">
  <div className="panel-heading"><div><h2><ShieldCheck size={18}/> GUI access and activity</h2><p>Sign-ins and API actions · latest 500 · newest first</p></div><button className="secondary" onClick={load} disabled={loading}><RefreshCw size={15} className={loading?'spin':''}/>Refresh</button></div>
  {error&&<div className="error" role="alert"><TriangleAlert size={17}/>{error}</div>}
  {loading&&!events.length?<div className="empty"><LoaderCircle className="spin"/><p>Loading audit events…</p></div>:events.length?<div className="table-wrap"><table className="audit-table"><thead><tr><th>TIME</th><th>USER</th><th>ACTIVITY</th><th>RESULT</th><th>CLIENT IP</th><th>MACHINE / CLIENT</th></tr></thead><tbody>{events.map(row=><tr key={row.id}><td>{new Date(row.created).toLocaleString()}</td><td><strong>{row.username}</strong><small>{row.role||'Unauthenticated'}</small></td><td>{actionLabel(row)}</td><td><span className={'badge '+(row.status>=200&&row.status<400?'succeeded':row.status===401||row.status===403?'failed':'unknown')}><i/>{row.status}</span></td><td>{row.client_ip||'—'}</td><td><details className="audit-client"><summary>{row.machine_details||'Legacy event · details unavailable'}</summary><small>{row.user_agent||'User-Agent was not recorded for this event.'}</small>{row.client_platform&&<small>Reported platform: {row.client_platform}</small>}</details></td></tr>)}</tbody></table></div>:<div className="empty"><Activity size={28}/><h3>No audit activity yet</h3><p>Sign-ins and console requests will appear here.</p></div>}
  <footer className="panel-footer"><span>{events.length} events shown</span><span>Passwords, keys, request content, authorization headers, and query parameters are not recorded.</span></footer>
 </section>;
}
