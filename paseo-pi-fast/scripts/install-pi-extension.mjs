import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';

const source=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../server/pi-extension.ts');
if(!fs.existsSync(source)) throw new Error('Missing canonical Pi request extension');
const target=path.join(os.homedir(),'.pi/agent/extensions/fast-mode.ts');
const shim=`// Managed by paseo-pi-fast; shared with the atomic Paseo RPC state.\nexport { default } from ${JSON.stringify(source.replaceAll('\\','/'))};\n`;
let backup=null;
if(!fs.existsSync(target)||fs.readFileSync(target,'utf8')!==shim){
  if(fs.existsSync(target)){
    backup=path.join(os.homedir(),'.route-manager/backups',`pi-fast-extension-${randomUUID()}`,'fast-mode.ts');
    fs.mkdirSync(path.dirname(backup),{recursive:true});fs.copyFileSync(target,backup,fs.constants.COPYFILE_EXCL);
  }
  fs.mkdirSync(path.dirname(target),{recursive:true});
  const temporary=`${target}.${randomUUID()}.tmp`;
  try{fs.writeFileSync(temporary,shim,{flag:'wx'});fs.renameSync(temporary,target);}
  finally{fs.rmSync(temporary,{force:true});}
}
console.log(JSON.stringify({target,source,backup,activation:'New Pi processes or Pi /reload; existing process hook stays until it reloads'}));
