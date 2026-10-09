/* Mono Float32 input -> PCM16LE at 16 kHz, with continuous resampling phase. */
class PCMCapture extends AudioWorkletProcessor {
 constructor(options){super();this.chunk=Math.round((options.processorOptions.chunkSeconds||4)*16000);this.buffer=new Int16Array(this.chunk);this.length=0;this.phase=0;this.count=0;this.start=options.processorOptions.startAt;this.active=true;this.port.onmessage=e=>{if(e.data.flush){this.active=false;this.emit();this.port.postMessage({flushed:true});}};}
 emit(){if(this.length>=3200){const pcm=this.buffer.slice(0,this.length);const start_at=new Date(this.start+(this.count-this.length)/16).toISOString();this.port.postMessage({pcm:pcm.buffer,start_at},[pcm.buffer]);}this.length=0;}
 process(inputs,outputs){for(const channels of outputs)for(const channel of channels)channel.fill(0);if(!this.active)return true;const channels=inputs[0];if(!channels?.length)return true;for(let i=0;i<channels[0].length;i++){this.phase+=16000;if(this.phase>=sampleRate){this.phase-=sampleRate;let sample=0;for(const channel of channels)sample+=channel[i]||0;sample/=channels.length;sample=Math.max(-1,Math.min(1,sample));this.buffer[this.length++]=Math.round(sample<0?sample*32768:sample*32767);this.count++;if(this.length===this.chunk)this.emit();}}return true;}
}
registerProcessor("pcm-capture",PCMCapture);
