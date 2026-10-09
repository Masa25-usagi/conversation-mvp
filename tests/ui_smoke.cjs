/* Optional development check; Playwright is not a runtime dependency. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const {chromium}=require(require.resolve('playwright',{paths:[process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES||process.cwd()]}));
const root=path.resolve(__dirname,'..');
const output=process.env.MVP_UI_OUTPUT||path.join(os.tmpdir(),'conversation-mvp-ui');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(condition,timeout=15000){const start=Date.now();while(!(await condition())){if(Date.now()-start>timeout)throw new Error('UI condition timed out');await sleep(80);}}
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'conversation-ui-'));
 fs.mkdirSync(output,{recursive:true});
 let serverProcess,browser,whisper;
 const whisperRequests=[],requests=[],errors=[];
 try{
  whisper=http.createServer((req,res)=>{const parts=[];req.on('data',chunk=>parts.push(chunk));req.on('end',()=>{const body=Buffer.concat(parts);whisperRequests.push({path:req.url,hasWav:body.includes(Buffer.from('RIFF')),hasFormat:body.includes(Buffer.from('response_format')),bytes:body.length});res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({text:'集中タイマーを小さく試作したい。'}));});});
  await new Promise(resolve=>whisper.listen(0,'127.0.0.1',resolve));
  const whisperPort=whisper.address().port;
  const reserve=http.createServer();await new Promise(resolve=>reserve.listen(0,'127.0.0.1',resolve));const port=reserve.address().port;await new Promise(resolve=>reserve.close(resolve));
  const config=path.join(temp,'config.toml');fs.writeFileSync(config,`[app]\nport=${port}\nlookahead_seconds=0.2\nprototype_cooldown_seconds=0\n[input]\nsource="browser"\nchunk_seconds=1\nmin_rms=0.0\nwhisper_url="http://127.0.0.1:${whisperPort}/inference"\n`);
  serverProcess=spawn('python3',['-m','conversation_mvp','--config',config,'--runtime',path.join(temp,'runtime'),'serve'],{cwd:root,stdio:['ignore','pipe','pipe']});
  let serverOutput='';serverProcess.stdout.on('data',chunk=>serverOutput+=chunk);serverProcess.stderr.on('data',chunk=>serverOutput+=chunk);
  await until(()=>serverOutput.includes('個人用画面'),10000);
  browser=await chromium.launch({headless:true,...(process.env.MVP_CHROMIUM_PATH?{executablePath:process.env.MVP_CHROMIUM_PATH}:{}),args:['--no-sandbox','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
  const context=await browser.newContext({viewport:{width:1280,height:900},permissions:['microphone'],locale:'ja-JP',timezoneId:'Asia/Tokyo'});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.on('request',req=>requests.push(req.url()));
  await page.goto(`http://127.0.0.1:${port}`,{waitUntil:'networkidle'});
  await page.getByRole('button',{name:'会話のデモを試す'}).click();
  await page.getByRole('button',{name:'試作を開く'}).waitFor({timeout:15000});
  await until(async()=>await page.locator('.event').count()===7);
  assert.equal(await page.locator('.event').count(),7);
  assert.equal(await page.locator('.note').count(),1);
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
  await page.getByRole('button',{name:'試作を開く'}).click();
  const preview=page.frameLocator('#preview-frame');
  await preview.getByRole('button',{name:'散歩のテーマを選ぶ'}).click();
  assert.ok((await preview.locator('#result').textContent()).length>0);
  await page.getByRole('button',{name:'閉じる',exact:true}).click();
  await page.getByRole('button',{name:'取り消す',exact:true}).click();
  await until(async()=>await page.getByText('試作の表示を取り消しました。ファイルと監査記録は残っています。').isVisible());
  await page.locator('#audio-settings').evaluate(e=>e.open=true);
  await page.locator('#reviewed').check();
  await page.getByRole('button',{name:'文字起こしを始める'}).click();
  await until(()=>whisperRequests.length>=1,15000);
  await page.getByRole('button',{name:'止める',exact:true}).click();
  await page.getByRole('button',{name:'会話を終えて確認する'}).click();
  await until(async()=>(await page.getByRole('button',{name:'試作を開く'}).count())===1);
  await page.getByRole('button',{name:'試作を開く'}).click();
  assert.equal(await preview.getByRole('heading',{name:'一つのことに集中する'}).count(),1);
  await page.getByRole('button',{name:'閉じる',exact:true}).click();
  await page.locator('#audio-settings').evaluate(e=>e.open=false);
  await page.setViewportSize({width:390,height:844});
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
  assert.equal(overflow,false,'mobile page overflows horizontally');
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  assert.ok(whisperRequests.every(req=>req.path==='/inference'&&req.hasWav&&req.hasFormat));
  assert.ok(requests.every(url=>url.startsWith(`http://127.0.0.1:${port}/`)),'unexpected external browser request');
  const report={passed:true,checks:['demo produces one prototype and one private suggestion','prototype button works in sandbox iframe','revoke persists and hides preview','fake microphone through AudioWorklet, PCM, multipart whisper adapter and core','generated timer follows transcribed idea','390px mobile viewport has no horizontal overflow','no JS errors or external browser requests'],whisperRequests,screenshots:['desktop.png','mobile.png'],note:'Fake microphone and mock whisper endpoint; no real recording, model or voice provider was tested.'};
  fs.writeFileSync(path.join(output,'ui-report.json'),JSON.stringify(report,null,2));
  process.stdout.write(JSON.stringify(report,null,2)+'\n');
 }finally{
  if(browser)await browser.close();
  if(serverProcess){serverProcess.kill('SIGINT');await new Promise(resolve=>{serverProcess.once('exit',resolve);setTimeout(()=>{serverProcess.kill('SIGTERM');resolve();},4000).unref();});}
  if(whisper)await new Promise(resolve=>whisper.close(resolve));
  fs.rmSync(temp,{recursive:true,force:true});
 }
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
