// Local Windows bridge UI restoration before an actual new request, not a readiness-only check.
import fs from 'node:fs';
import path from 'node:path';
const args=process.argv.slice(2);
if(args.length!==6||args[0]!=='--home'||args[2]!=='--out'||args[4]!=='--base')throw Error('Expected --home, --out and --base');
const home=path.resolve(args[1]),output=path.resolve(args[3]);
if(path.basename(output)!=='browser_surface_preflight.json')throw Error('Unexpected output name');
const descriptor=JSON.parse(fs.readFileSync(path.join(home,'runtime/launcher-browser.json'),'utf8'));
const endpoint=new URL(descriptor.endpoint);
if(endpoint.hostname!=='127.0.0.1'||endpoint.protocol!=='http:')throw Error('Nonlocal controller');
const base=new URL(args[5]);if(base.hostname!=='127.0.0.1'||base.protocol!=='http:')throw Error('Nonlocal bridge');
const configEndpoint=new URL('/healthz',base);
const health=await(await fetch(configEndpoint,{signal:AbortSignal.timeout(5000)})).json();
if(health.mode!=='browser-only'||health.active_http_turns!==0||health.active_browser_turns!==0)throw Error('An active request must finish before UI restoration');
const pages=await(await fetch(new URL('/json/list',endpoint),{signal:AbortSignal.timeout(5000)})).json();
const main=pages.filter(p=>p.type==='page'&&p.url.startsWith('file:'));
if(main.length!==1)throw Error('No unique bridge app UI');
async function restore(){
 if(document.title!=='Codex Web GPT'||typeof window.codexWebLauncher?.snapshot!=='function')throw Error('Unexpected app UI');
 const text=n=>(n.getAttribute('aria-label')||n.innerText||'').trim();
 const matches=[...document.querySelectorAll('button')].filter(n=>['浏览器','Browser'].includes(text(n))&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0&&!n.disabled);
 if(matches.length!==1)throw Error('No unique observed Browser navigation control');
 matches[0].click();await new Promise(r=>setTimeout(r,600));
 const s=await window.codexWebLauncher.snapshot();
 return {observed_control_clicked:text(matches[0]),browser_status:s.browser?.status,browser_tabs:(s.browser?.tabs||[]).map(t=>({id:t.id,status:t.status,active:t.active})),use_saved_chats:s.state?.useSavedChats,account_settings_changed:false};
}
let ws;
try{
 const u=new URL(main[0].webSocketDebuggerUrl);if(u.hostname!=='127.0.0.1')throw Error('Nonlocal socket');
 ws=new WebSocket(u);
 const result=await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{ws.close();reject(Error('Bounded preflight timeout'));},7000);
  ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:'('+restore.toString()+')()',awaitPromise:true,returnByValue:true}}));
  ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id!==1)return;clearTimeout(timer);if(m.error||m.result?.exceptionDetails){ws.close();reject(Error('Observed Browser navigation could not be restored'));return;}resolve(m.result.result.value);ws.close();};
  ws.onerror=()=>{clearTimeout(timer);reject(Error('Local UI controller unavailable'));};
 });
 await new Promise(r=>{if(ws.readyState===3)return r();ws.onclose=r;setTimeout(r,1000);});
 if(result.browser_status!=='ready')throw Error('Browser surface not ready');
 const out={observed_at_utc:new Date().toISOString(),status:'restored',result,debug_connection_closed:ws.readyState===3,message_submission:0,scope:'Only the observed app Browser navigation, before a new request. No auth/profile/private API, model change, request retry or controller restart.'};
 fs.writeFileSync(output,JSON.stringify(out,null,2)+'\n',{flag:'wx'});console.log(JSON.stringify(out));
}finally{
 if(ws&&ws.readyState!==3){ws.close();await new Promise(r=>{if(ws.readyState===3)return r();ws.onclose=r;setTimeout(r,1000);});}
}
