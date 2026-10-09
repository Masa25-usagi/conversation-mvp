const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const client=source.slice(source.indexOf('let sessionRefresh='),source.indexOf('function stamp('));
const response=(status,value)=>({status,ok:status>=200&&status<300,json:async()=>value});
function setup(fetch){const context=vm.createContext({fetch,window:{MVP:{token:'old-session'}}});vm.runInContext(client,context);return context;}

test('concurrent stale requests share renewal and recover without navigating',async()=>{
 const calls=[];
 const ctx=setup(async(path,options)=>{
  calls.push({path,options});
  if(path==='/api/session'){await new Promise(resolve=>setImmediate(resolve));return response(200,{token:'new-session'});}
  return options.headers['X-MVP-Token']==='new-session'?response(200,{connected:true}):response(403,{error:'session token required'});
 });
 const results=await Promise.all([ctx.api('/api/state'),ctx.api('/api/requirements')]);
 assert.ok(results.every(x=>x.connected));
 assert.equal(calls.filter(x=>x.path==='/api/session').length,1);
 assert.equal(ctx.window.MVP.token,'new-session');
});

test('POST rejected before execution is retried once with the original body',async()=>{
 let executed=0;const sent=[];
 const ctx=setup(async(path,options)=>{
  if(path==='/api/session')return response(200,{token:'new-session'});
  sent.push(options.body);
  if(options.headers['X-MVP-Token']==='old-session')return response(403,{error:'session token required'});
  executed++;return response(200,{accepted:true});
 });
 await ctx.api('/api/voice/turn',{text:'接続テスト'});
 assert.equal(executed,1);assert.equal(sent.length,2);assert.equal(sent[0],sent[1]);
});

test('different permission failures do not renew or repeat requests',async()=>{
 const calls=[];const ctx=setup(async(path)=>{calls.push(path);return response(403,{error:'cross-origin request refused'});});
 await assert.rejects(ctx.api('/api/state'),/cross-origin/);assert.equal(calls.length,1);
});

test('repeated stale-session failure ends after one retry',async()=>{
 const calls=[];const ctx=setup(async(path)=>{calls.push(path);return path==='/api/session'?response(200,{token:'new-session'}):response(403,{error:'session token required'});});
 await assert.rejects(ctx.api('/api/state'),/もう一度操作/);assert.equal(calls.length,3);
});
