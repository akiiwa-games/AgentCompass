const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const ts=require('typescript');
function fixture(home,fsImpl=fs){
  const modules={};
  function load(name){
    if(modules[name])return modules[name];
    const exports={};modules[name]=exports;
    const source=fs.readFileSync(path.join(__dirname,`../server/${name}.ts`),'utf8');
    const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
    vm.runInNewContext(code,{exports,console,require(id){
      if(id==='node:fs')return fsImpl;
      if(id==='node:os')return {homedir:()=>home};
      if(id==='./fast')return load('fast');
      return require(id);
    }});return exports;
  }
  const hooks={},commands={},notices=[];
  load('pi-extension').default({on(name,fn){hooks[name]=fn;},registerCommand(name,definition){commands[name]=definition;}});
  return {hooks,commands,api:load('fast'),ctx:{model:{provider:'openai-codex',api:'openai-codex-responses'},hasUI:true,
    ui:{setStatus(){},notify(text){notices.push(text);},async select(){return undefined;}}},notices};
}

test('real Pi payload hook sees each RPC state update and preserves other payload fields',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'fast-hook-'));
  try{const f=fixture(dir);const payload={model:'test',input:['research'],service_tier:'priority'};
    assert.equal(f.hooks.before_provider_request({payload},f.ctx).service_tier,'priority');
    await f.api.handleSetFastMode({enabled:false});
    const result=f.hooks.before_provider_request({payload},f.ctx);
    assert.equal(result.service_tier,'default');assert.equal(result.input,payload.input);assert.equal(payload.service_tier,'priority');
    await f.api.handleSetFastMode({enabled:true});assert.equal(f.hooks.before_provider_request({payload},f.ctx).service_tier,'priority');
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test('hook leaves other providers and non-object payloads alone',()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'fast-hook-other-'));
  try{const f=fixture(dir);assert.equal(f.hooks.before_provider_request({payload:{}},{...f.ctx,model:{provider:'anthropic',api:'anthropic-messages'}}),undefined);
    assert.equal(f.hooks.before_provider_request({payload:[]},f.ctx),undefined);
    assert.equal(fs.existsSync(path.join(dir,'.pi/agent/fast-mode.json')),false,'Factory must not change settings');
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test('Pi command write failure rejects without success notice or state change',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'fast-hook-fault-'));
  try{fixture(dir).api.writeFastModeState(false);const f=fixture(dir,{...fs,renameSync(){throw new Error('fixture permission denied');}});
    await assert.rejects(f.commands.fast.handler('on',f.ctx),/permission denied/);
    assert.equal(f.api.readFastModeState().enabled,false);assert.deepEqual(f.notices,[]);
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test('non-UI Pi commands work; corrupt settings do not silently request priority',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'fast-hook-headless-'));
  try{const f=fixture(dir);await f.commands.fast.handler('off',{...f.ctx,hasUI:false});assert.equal(f.api.readFastModeState().enabled,false);assert.deepEqual(f.notices,[]);
    fs.writeFileSync(path.join(dir,'.pi/agent/fast-mode.json'),'{broken');
    assert.throws(()=>f.hooks.before_provider_request({payload:{}},f.ctx));
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});
