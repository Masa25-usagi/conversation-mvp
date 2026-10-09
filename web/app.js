"use strict";
const $ = id => document.getElementById(id);
const ACTIONS = {IGNORE:"黙って待つ",SUGGEST_PRIVATE:"自分への提案",START_PROTOTYPE:"試作を始める",TRIGGER_PROTOTYPE:"試作の候補",CALL_VOICE_AI:"音声AIの候補",REQUIRE_PERMISSION:"許可が必要",STUB:"スタブ"};
let revision=-1, capture=null, latestState=null, audioPosting=Promise.resolve(), queuedChunks=0;
let voiceCapture=null, voicePending=false, voiceStarting=false, replyAudio=null, replyURL=null;
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function error(message){$("error").textContent=message;$("error").hidden=false;}
let sessionRefresh=null;
async function refreshSession(){
 if(!sessionRefresh)sessionRefresh=(async()=>{
  const response=await fetch("/api/session",{cache:"no-store"});
  const value=await response.json();
  if(!response.ok||typeof value.token!=="string"||!value.token)throw new Error("再接続できませんでした。画面を更新してください。");
  window.MVP.token=value.token;
 })().finally(()=>{sessionRefresh=null;});
 return sessionRefresh;
}
async function api(path,body,retry=true){
 const response=await fetch(path,{method:body===undefined?"GET":"POST",headers:{"X-MVP-Token":window.MVP.token,...(body===undefined?{}:{"Content-Type":"application/json"})},body:body===undefined?undefined:JSON.stringify(body)});
 const value=await response.json();
 if(response.status===403&&value.error==="session token required"){
  if(!retry)throw new Error("接続が更新されました。もう一度操作してください。");
  await refreshSession();return api(path,body,false);
 }
 if(!response.ok)throw new Error(value.error||`HTTP ${response.status}`);return value;
}
function stamp(value){return new Date(value).toLocaleTimeString("ja-JP",{hour:"2-digit",minute:"2-digit",second:"2-digit"});}
function render(state){
 latestState=state;$("connection").textContent="個人用画面に接続中";
 const modelName=role=>state.models?.[role]||state.adapters[role];
 $("mode").textContent=`入力：${state.input_source}　判断役：${modelName("trigger")}　文脈判断：${modelName("main")}　試作：${modelName("coder")}`;
 if(state.adapters.coder==="ollama")$("mode").textContent+="。ローカルAIで生成します。CPUでの生成は数分かかる場合があります。";
 $("voice-provider").textContent=state.adapters.voice==="hermes_nous"?`文字起こし：Groq Whisper　返答：Nous Portal / ${state.models.voice}　読み上げ：gpt-4o-mini-tts。各サービスの利用枠・クレジットを使用します。音声ファイルは保存せず、会話の文字と処理記録をこのPCに残します。`:"音声APIは未設定です。";
 $("voice-stop").hidden=!state.voice_busy;
 if(state.revision===revision)return;revision=state.revision;
 const timeline=$("timeline");timeline.replaceChildren();
 if(!state.timeline.length)timeline.append(node("li","デモを始めるか、発言を追加してください。","empty"));
 for(const item of [...state.timeline].reverse()){
  const action=item.gate?.action;const li=node("li",undefined,"event"+(action&&action!=="IGNORE"?" active":""));const meta=node("div",undefined,"meta");
  meta.append(node("time",`${stamp(item.utterance.start_at)} – ${stamp(item.utterance.end_at)}`),node("span",ACTIONS[action]||item.status,"badge"+(action==="REQUIRE_PERMISSION"?" blocked":"")));
  if(item.utterance.provenance==="synthetic")meta.append(node("span","合成データ"));li.append(meta,node("p",item.utterance.text));
  if(item.gate)li.append(node("p",item.gate.reason,"reason"));
  if(item.detection){const details=node("details");details.append(node("summary","なぜ、そう判断したか"),node("p",`判断役：${ACTIONS[item.detection.trigger]||item.detection.trigger} / ${item.detection.reason}`));const interpretation=item.decision?.context_interpretation||item.context_interpretation;if(interpretation)details.append(node("p",`文脈：${interpretation}`));if(item.trigger_metrics)details.append(node("p",`判断役の応答：${item.trigger_metrics.latency_ms} ms`));li.append(details);}timeline.append(li);
 }
 const artifacts=$("artifacts");artifacts.replaceChildren();
 if(!state.prototypes.length)artifacts.append(node("p","試作ができると、元の発言と一緒にここに届きます。","empty"));
 for(const item of [...state.prototypes].reverse()){
  const card=node("article",undefined,"card"+(item.status==="取消済み"?" revoked":""));card.append(node("span",item.status,"badge"),node("h3","この発言から作りました"),node("blockquote",`${stamp(item.utterance.start_at)}　${item.utterance.text}`));
  card.append(node("p",item.summary||item.reason||"隔離した場所で小さく試しています。"));
  if(item.status==="完成"){
   const toolbar=node("div",undefined,"toolbar"),open=node("button","試作を開く"),revoke=node("button","取り消す");
   open.onclick=()=>{$("preview-frame").src=`/artifact/${item.prototype_id}/index.html`;$("preview").showModal();};
   revoke.onclick=async()=>{try{await api("/api/prototype/revoke",{prototype_id:item.prototype_id});$("preview").close();$("preview-frame").src="about:blank";await refresh();}catch(e){error(e.message);}};
   const duration=item.total_latency_ms>=1000?`${(item.total_latency_ms/1000).toFixed(1)} 秒`:`${item.total_latency_ms} ms`;
   toolbar.append(open,revoke);card.append(toolbar,node("p",`${duration}で生成しました。公開されていません。`,"caption"));
  } else if(["制作待ち","制作中"].includes(item.status)) {const cancel=node("button","取り消す");cancel.onclick=()=>api("/api/prototype/revoke",{prototype_id:item.prototype_id}).then(refresh).catch(e=>error(e.message));card.append(cancel);}
  if(item.status==="取消済み")card.append(node("p","試作の表示を取り消しました。ファイルと監査記録は残っています。","caption"));artifacts.append(card);
 }
 const suggestions=$("suggestions");suggestions.replaceChildren();if(!state.suggestions.length)suggestions.append(node("p","提案はあなたの画面だけに表示します。","empty"));
 for(const note of [...state.suggestions].reverse()){const card=node("article",undefined,"note");card.append(node("h3",`${stamp(note.timestamp)}　${note.title}`),node("p",note.text));suggestions.append(card);}
 const voice=state.voice.at(-1);$("voice-result").replaceChildren();if(voice){const metrics=voice.metrics;$("voice-result").append(node("p",voice.kind==="voice_turn"?`音声AI：${voice.reply}`:voice.result?.summary||voice.output?.summary||voice.reason||"音声セッションを記録しました。","voice-info"));if(metrics)$("voice-result").append(node("p",metrics.measurement_kind==="stub"?"実接続・料金は未計測です。":`起動：${metrics.ready_latency_ms??"未準備"} ms / API費用：${metrics.api_cost??"不明"} ${metrics.currency}`,"voice-info"));}
 if(state.errors.length)error(state.errors.at(-1).reason||state.errors.at(-1).detail||"処理に失敗しました。");
}
async function refresh(){render(await api("/api/state"));}
$("demo").onclick=async()=>{try{$("error").hidden=true;await api("/api/demo",{});}catch(e){error(e.message);}};
$("text-input").onsubmit=async e=>{e.preventDefault();try{$("error").hidden=true;await api("/api/utterance",{text:$("utterance").value});$("utterance").value="";await refresh();}catch(e){error(e.message);}};
$("voice").onclick=()=>{if(latestState?.adapters.voice==="hermes_nous"){$("voice-panel").hidden=false;$("voice-panel").scrollIntoView({behavior:"smooth",block:"center"});}else api("/api/voice/call",{}).catch(e=>error(e.message));};
$("voice-stop").onclick=()=>api("/api/voice/stop",{}).catch(e=>error(e.message));
$("finish").onclick=async()=>{try{await finishVoiceCapture(false);if(replyAudio)replyAudio.pause();await stopCapture();await api("/api/flush",{});await refresh();}catch(e){error(e.message);}};
$("close-preview").onclick=()=>{$("preview").close();$("preview-frame").src="about:blank";};
$("preview").addEventListener("close",()=>{$("preview-frame").src="about:blank";});
async function listDevices(){try{const devices=await navigator.mediaDevices.enumerateDevices();const select=$("microphone");select.replaceChildren(new Option("既定のデバイス",""));for(const d of devices.filter(x=>x.kind==="audioinput"))select.add(new Option(d.label||`入力 ${select.options.length}`,d.deviceId));}catch(e){error(e.message);}}
$("devices").onclick=listDevices;
function base64(buffer){const bytes=new Uint8Array(buffer);let text="";for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(text);}
function postChunk(data,source){
 if(queuedChunks>=8){error("文字起こしが追い付いていません。音声入力を止めます。");stopCapture();return;}
 queuedChunks++;const packet={source,sample_rate:16000,start_at:data.start_at,pcm_base64:base64(data.pcm)};
 audioPosting=audioPosting.then(()=>api("/api/audio",packet)).catch(e=>{error(e.message);stopCapture();}).finally(()=>queuedChunks--);
}
async function startCapture(){
 if(voiceCapture||voicePending||voiceStarting)throw new Error("音声AIとの会話を終えてから文字起こしを始めてください。");
 if(capture)return;if(!$("reviewed").checked)throw new Error("接続前の確認と参加者への周知を記録してください。");
 const source=$("audio-source").value;
 if(!latestState||(!latestState.audio_ready))throw new Error("config.tomlのinput.sourceをbrowserまたはffmpegへ変更し、再起動してください。");
 await api("/api/input/acknowledge",{source,reviewed:true});
 if(source==="ffmpeg"){await api("/api/capture/start",{});capture={ffmpeg:true};$("record").disabled=true;$("stop").disabled=false;$("audio-status").textContent="設定したOSデバイスを文字起こししています。";return;}
 if(latestState.input_source!=="browser")throw new Error("ブラウザ入力を使うにはinput.sourceをbrowserへ変更してください。");
 const streams=[];let context=null;
 try{
  if(source==="browser_system"||source==="browser_mixed"){
   const stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true,systemAudio:"include"});streams.push(stream);
   if(!stream.getAudioTracks().length)throw new Error("選んだ共有元から音声を取得できませんでした。音声付きのタブかループバック入力を選んでください。");
  }
  if(source==="browser_mic"||source==="browser_mixed"){
   const selected=$("microphone").value;streams.push(await navigator.mediaDevices.getUserMedia({audio:{...(selected?{deviceId:{exact:selected}}:{}),echoCancellation:true,noiseSuppression:true},video:false}));
  }
  context=new AudioContext();await context.resume();await context.audioWorklet.addModule("/pcm-worklet.js");
  const worklet=new AudioWorkletNode(context,"pcm-capture",{numberOfOutputs:1,outputChannelCount:[1],processorOptions:{chunkSeconds:window.MVP.chunkSeconds,startAt:Date.now()}});
  const gain=context.createGain();gain.gain.value=0;worklet.connect(gain).connect(context.destination);
  for(const stream of streams){context.createMediaStreamSource(new MediaStream(stream.getAudioTracks())).connect(worklet);for(const track of stream.getTracks())track.addEventListener("ended",()=>stopCapture(),{once:true});}
  worklet.port.onmessage=e=>{if(e.data.pcm)postChunk(e.data,source);if(e.data.flushed&&capture?.flushResolve)capture.flushResolve();};
  capture={streams,context,worklet,source};$("record").disabled=true;$("stop").disabled=false;$("audio-status").textContent=latestState.transcription_provider==="hermes_groq"?"Groq APIで文字起こししています。音声ファイルは保存しません。":"ローカルで文字起こししています。音声ファイルは保存しません。";
 }catch(e){streams.forEach(s=>s.getTracks().forEach(t=>t.stop()));if(context)await context.close();throw e;}
}
async function stopCapture(){
 if(!capture)return;const active=capture;if(active.stopping)return;active.stopping=true;
 try{
  if(active.ffmpeg){await api("/api/capture/stop",{});}
  else{await new Promise(resolve=>{active.flushResolve=resolve;active.worklet.port.postMessage({flush:true});setTimeout(resolve,1000);});active.streams.forEach(s=>s.getTracks().forEach(t=>t.stop()));active.worklet.disconnect();await active.context.close();}
 }finally{capture=null;$("record").disabled=false;$("stop").disabled=true;$("audio-status").textContent="音声の取得を止めました。";}
 await audioPosting;
}
$("record").onclick=()=>startCapture().catch(e=>error(e.message));$("stop").onclick=()=>stopCapture().catch(e=>error(e.message));
function voiceControls(){
 $("voice-record").disabled=!!voiceCapture||voicePending||voiceStarting;
 $("voice-send").disabled=!voiceCapture||voiceCapture.stopping;
 $("voice-cancel").disabled=!voiceCapture||voiceCapture.stopping;
 $("voice-text-send").disabled=!!voiceCapture||voicePending||voiceStarting;
 $("voice-text").disabled=!!voiceCapture||voicePending||voiceStarting;
}
async function voiceTurn(packet){
 if(voicePending)throw new Error("音声AIが返答中です。");
 if(voiceCapture||voiceStarting)throw new Error("録音を終えてから送信してください。");
 voicePending=true;voiceControls();$("error").hidden=true;$("voice-status").textContent="文字起こしと音声の返答を処理しています…";
 if(replyAudio)replyAudio.pause();
 try{
  const result=await api("/api/voice/turn",packet);
  const raw=atob(result.audio_base64),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
  replyURL=URL.createObjectURL(new Blob([bytes],{type:result.audio_type}));
  replyAudio=node("audio");replyAudio.controls=true;replyAudio.src=replyURL;
  const card=node("article",undefined,"card");card.dataset.audioUrl=replyURL;card.append(node("p",`あなた：${result.text}`),node("p",`AI：${result.reply}`),replyAudio);
  $("voice-conversation").prepend(card);
  while($("voice-conversation").children.length>6){const old=$("voice-conversation").lastChild;URL.revokeObjectURL(old.dataset.audioUrl);old.remove();}
  $("voice-status").textContent=`返答しました（${(result.latency_ms/1000).toFixed(1)}秒）。続けて話せます。`;
  replyAudio.play().catch(()=>{$("voice-status").textContent+=" 再生ボタンを押すと音声が流れます。";});
 }catch(e){$("voice-status").textContent="送信に失敗しました。もう一度試してください。";throw e;}
 finally{voicePending=false;voiceControls();}
}
async function startVoiceCapture(){
 if(capture)throw new Error("文字起こしを止めてから音声AIと話してください。");
 if(voiceCapture||voicePending||voiceStarting)return;
 if(!$("voice-reviewed").checked)throw new Error("音声の外部送信と参加者への周知を確認してください。");
 voiceStarting=true;voiceControls();
 $("error").hidden=true;
 if(replyAudio)replyAudio.pause();
 let stream=null,context=null;
 try{
  await api("/api/input/acknowledge",{source:"browser_mic",reviewed:true});
  const selected=$("microphone").value;
  stream=await navigator.mediaDevices.getUserMedia({audio:{...(selected?{deviceId:{exact:selected}}:{}),echoCancellation:true,noiseSuppression:true},video:false});
  context=new AudioContext();await context.resume();await context.audioWorklet.addModule("/pcm-worklet.js");
  const worklet=new AudioWorkletNode(context,"pcm-capture",{numberOfOutputs:1,outputChannelCount:[1],processorOptions:{chunkSeconds:15,startAt:Date.now()}});
  const gain=context.createGain();gain.gain.value=0;worklet.connect(gain).connect(context.destination);context.createMediaStreamSource(stream).connect(worklet);
  const active={stream,context,worklet,chunks:[]};voiceCapture=active;
  worklet.port.onmessage=e=>{if(e.data.pcm)active.chunks.push(new Uint8Array(e.data.pcm));if(e.data.flushed)active.flushResolve?.();};
  active.timer=setTimeout(()=>finishVoiceCapture(true).catch(e=>error(e.message)),14500);
  stream.getAudioTracks()[0].addEventListener("ended",()=>finishVoiceCapture(false).catch(e=>error(e.message)),{once:true});
  $("voice-status").textContent="録音中です。話し終わったら「録音を終えて送信」を押してください。";voiceControls();
 }catch(e){stream?.getTracks().forEach(t=>t.stop());if(context)await context.close();if(e.name==="NotAllowedError")throw new Error("マイクの利用が許可されていません。ブラウザのサイト設定でマイクを許可してください。");throw e;}
 finally{voiceStarting=false;voiceControls();}
}
async function finishVoiceCapture(send){
 const active=voiceCapture;if(!active||active.stopping)return;active.stopping=true;clearTimeout(active.timer);voiceControls();
 try{
  await new Promise(resolve=>{active.flushResolve=resolve;active.worklet.port.postMessage({flush:true});setTimeout(resolve,1000);});
 }finally{active.stream.getTracks().forEach(t=>t.stop());active.worklet.disconnect();await active.context.close();voiceCapture=null;voiceControls();}
 if(!send){$("voice-status").textContent="録音を取り消しました。音声は送信していません。";return;}
 const size=Math.min(480000,active.chunks.reduce((sum,x)=>sum+x.length,0)),bytes=new Uint8Array(size);let offset=0;
 for(const chunk of active.chunks){const slice=chunk.subarray(0,size-offset);bytes.set(slice,offset);offset+=slice.length;if(offset===size)break;}
 if(size<6400)throw new Error("短すぎて認識できませんでした。少し長く話してください。");
 await voiceTurn({source:"browser_mic",sample_rate:16000,pcm_base64:base64(bytes.buffer)});
}
$("voice-record").onclick=()=>startVoiceCapture().catch(e=>error(e.message));
$("voice-send").onclick=()=>finishVoiceCapture(true).catch(e=>error(e.message));
$("voice-cancel").onclick=()=>finishVoiceCapture(false).catch(e=>error(e.message));
$("voice-text-form").onsubmit=async e=>{e.preventDefault();try{await voiceTurn({text:$("voice-text").value});$("voice-text").value="";}catch(e){error(e.message);}};
async function requirements(){const data=await api("/api/requirements");const current=$("audio-source").value;const source=data.sources.find(x=>x.source===current);const div=$("requirements");div.replaceChildren(node("p",`${source.name}：${source.status}`));const list=node("ul");for(const check of source.checks)list.append(node("li",check));div.append(list);}
$("audio-source").onchange=()=>{$("reviewed").checked=false;requirements().catch(e=>error(e.message));};
async function poll(){try{await refresh();}catch(e){$("connection").textContent="接続を確認してください";}setTimeout(poll,700);}
requirements().catch(e=>error(e.message));poll();
