'use strict';
// Official Cindy JSON-RPC stdio worker. No Electron/private IPC or credentials.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {spawn,spawnSync} = require('node:child_process');
const readline = require('node:readline');
const config = JSON.parse(fs.readFileSync(path.join(__dirname, 'router.json'), 'utf8'));
const root = config.stateRoot || path.join(os.homedir(), '.agent-compass', 'cindy-plugin');
const children=new Set();
function stop(child) {
  if (!child.pid || child.exitCode!==null) return;
  if (process.platform==='win32') spawnSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});
  else {try {process.kill(-child.pid,'SIGKILL');} catch {child.kill('SIGKILL');}}
}
process.on('exit',()=>{for(const child of children) stop(child);});
process.on('SIGTERM',()=>{for(const child of children) stop(child);process.exit(143);});
process.on('SIGINT',()=>{for(const child of children) stop(child);process.exit(130);});
fs.mkdirSync(root, {recursive:true});
const digest = text => crypto.createHash('sha256').update(text).digest('hex');
const atomic = (file, value) => {
  const tmp = file + '.' + crypto.randomUUID() + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8');
  fs.renameSync(tmp, file);
};
function context(value) {
  if (!value || typeof value.session_id !== 'string' || !value.session_id.trim() ||
      value.workdir_is_local !== true || value.workdir_is_read_only !== false ||
      typeof value.workdir !== 'string' || !path.isAbsolute(value.workdir))
    throw Error('A host-verified writable local session_context is required. Remote/read-only contexts are unsupported.');
  return value;
}
function owned(operation, ctx) {
  if (typeof operation !== 'string' || !/^[a-f0-9]{40}$/.test(operation)) throw Error('Invalid operation');
  const dir = path.join(root, operation);
  const record = JSON.parse(fs.readFileSync(path.join(dir, 'receipt.json'), 'utf8'));
  if (record.parent !== ctx.session_id) throw Error('Operation belongs to a different parent session');
  return {dir, record};
}
async function route(task, ctx, dir) {
  const contextFile = path.join(dir, 'context.json');
  atomic(contextFile, {...ctx, version:1, source:'cindy-plugin-session-context'});
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(PASEO_|HC_|ORCA_|T3_|ROUTER_PARENT_|ROUTER_CINDY_CONTEXT)/.test(key)));
  env.ROUTER_CINDY_CONTEXT = contextFile;
  const command = config.command;
  if (!Array.isArray(command) || !command.length || command.some(v=>typeof v!=='string')) throw Error('Invalid public router command');
  return await new Promise((resolve,reject) => {
    const child=spawn(command[0], [...command.slice(1), '--task', '-', '--verbose'],
      {cwd:ctx.workdir, env, windowsHide:true, detached:process.platform!=='win32',stdio:['pipe','pipe','pipe']});
    children.add(child);
    const stdout=[], stderr=[];
    const timer=setTimeout(()=>stop(child), 110000);
    child.stdout.on('data',data=>stdout.push(data));
    child.stderr.on('data',data=>stderr.push(data));
    child.on('error',error=>{clearTimeout(timer);children.delete(child);reject(error);});
    child.on('close',code=>{
      clearTimeout(timer);
      children.delete(child);
      const text=Buffer.concat(stdout).toString('utf8');
      fs.writeFileSync(path.join(dir,'router.stdout.json'),text);
      fs.writeFileSync(path.join(dir,'router.stderr.txt'),Buffer.concat(stderr));
      try {
        const result=JSON.parse(text);
        if (code && !['self','split'].includes(result.status)) throw Error(result.error || result.next || 'Router blocked');
        resolve(result);
      } catch(error) { reject(Error('Router returned no usable route; inspect '+dir+': '+error.message)); }
    });
    child.stdin.end(task, 'utf8');
  });
}
async function handle(method, params) {
  const ctx=context(params.context);
  if (method==='prepare') {
    if (typeof params.task!=='string' || !params.task.trim()) throw Error('Task is empty');
    const operation=digest(ctx.session_id+'\0'+params.task).slice(0,40);
    const dir=path.join(root,operation);
    fs.mkdirSync(dir,{recursive:true});
    const file=path.join(dir,'receipt.json');
    if (fs.existsSync(file)) return {operation,...JSON.parse(fs.readFileSync(file,'utf8'))};
    const result=await route(params.task,ctx,dir);
    const bundle=result.route_bundle;
    const prompt=result.worker_prompt_file ? fs.readFileSync(result.worker_prompt_file,'utf8') : '';
    const record={parent:ctx.session_id, state:'prepared', router:result, bundle, prompt,
                  directory:ctx.workdir, mappings:config.accountMappings || []};
    atomic(file,record);
    return {operation,...record};
  }
  const {dir,record}=owned(params.operation,ctx);
  if (method==='load') return record;
  if (method==='save') {
    const update=params.record;
    if (!update || update.parent!==record.parent) throw Error('Parent cannot be changed');
    atomic(path.join(dir,'receipt.json'),update);
    return {saved:true};
  }
  if (method==='output') {
    const file=path.join(dir,'messages-'+digest(JSON.stringify(params.page)).slice(0,16)+'.json');
    atomic(file,params.page);
    return {artifact:file};
  }
  throw Error('Unsupported method');
}
// Cindy shares one worker across sessions; serialize preparation/persistence.
let queue=Promise.resolve();
readline.createInterface({input:process.stdin}).on('line', line=>{
  queue=queue.then(async()=>{
    let request;
    try {
      request=JSON.parse(line);
      const result=await handle(request.method,request.params || {});
      process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:request.id,result})+'\n');
    } catch(error) {
      process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:request?.id,error:{code:-32000,message:error.message}})+'\n');
    }
  });
});
