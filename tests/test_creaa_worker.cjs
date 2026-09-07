const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('worker-extension/creaa-worker.js','utf8');
const spin = async () => { for(let i=0;i<35;i++) await new Promise(setImmediate); };
async function harness({stored={},responses={},ack={},tabUrl='https://creaa.ai/image?flow2api_worker=1'}={}) {
 const storage={creaaEnabled:true,creaaDeviceId:'dev1',...stored};
 const sent=[], pageCalls=[], sockets=[]; let messageHandler, createdTabs=0;
 class Socket {
  static OPEN=1;
  constructor(){this.readyState=1;sockets.push(this);setImmediate(()=>this.onopen?.());}
  send(text){const m=JSON.parse(text);sent.push(m);if(m.type==='register')setImmediate(()=>this.onmessage?.({data:JSON.stringify({type:'register_ack'})}));
   if(m.type==='job_event') setImmediate(()=>this.onmessage?.({data:JSON.stringify({type:'event_ack',job_id:m.job_id,attempt_id:m.attempt_id,state:m.state,accepted:true,...(ack[m.state]||{})})}));}
  close(){this.readyState=3;this.onclose?.();}
 }
 const chrome={
  storage:{local:{get:async keys=>{if(keys===null)return {...storage}; if(typeof keys==='string')keys=[keys];return Object.fromEntries(keys.filter(k=>k in storage).map(k=>[k,structuredClone(storage[k])]));},set:async x=>Object.assign(storage,structuredClone(x))}},
  tabs:{query:async()=>[],create:async()=>{createdTabs++;return{id:1,url:tabUrl};},get:async()=>({id:1,url:tabUrl,status:'complete'}),reload:async()=>{},update:async()=>{}},
  scripting:{executeScript:async({args})=>{const [op,arg]=args;pageCalls.push([op,arg]);const defaults={inspect:{ok:true,account_id:'42',models:[],capabilities:{}},recover:{ok:true,entry:null},prepare:{ok:true},submit:{ok:true,provider_task_id:'task1'},poll:{ok:true,state:'succeeded',result:{urls:['https://files.creaa.ai/result.png'],media_type:'image'}}};const response=responses[op]; return[{result:typeof response==='function' ? await response(arg) : response||defaults[op]}];}},
  alarms:{create(){},onAlarm:{addListener(){}}},runtime:{id:'extension',onMessage:{addListener(fn){messageHandler=fn;}}}
 };
 const context=vm.createContext({chrome,importScripts(){},creaaPageOperation(){},getSettings:async()=>({serverBase:'https://flow.ashuthefire.com',apiKey:'test'}),
  crypto:require('node:crypto').webcrypto,WebSocket:Socket,URL,console,structuredClone,
  setInterval:()=>0,clearInterval(){},setTimeout:(fn,ms)=>{const t=setTimeout(fn,ms);t.unref();return t;},clearTimeout});
 vm.runInContext(source,context);await spin();
 return {sent,pageCalls,storage,get createdTabs(){return createdTabs;},send:async(type,job)=>{sockets.at(-1).onmessage({data:JSON.stringify({type,job})});await spin();},messageHandler};
}
const job={id:'job1',attempt_id:'a1',account_id:'42',media_type:'image',request:{model:'openai/gpt-image-2',prompt:'Cup',references:['data:image/png;base64,AAAA']},provider_task_id:null};
test('worker submits only after durable submitting ack and reports result',async()=>{
 const h=await harness();await h.send('execute',job);
 assert.deepEqual(h.pageCalls.map(c=>c[0]),['inspect','recover','prepare','submit','poll']);
 assert.deepEqual(h.sent.filter(m=>m.type==='job_event').map(m=>m.state),['submitting','submitted','succeeded']);
 assert.equal(h.storage['creaaJob:job1'].pending,null);
 assert.equal(h.storage['creaaJob:job1'].job.request.references,undefined);
});
test('backend-known resume with empty local storage does not regress running to submitted',async()=>{
 const h=await harness();await h.send('resume',{...job,provider_task_id:'task1'});
 assert.deepEqual(h.pageCalls.map(c=>c[0]),['inspect','poll']);
 assert.deepEqual(h.sent.filter(m=>m.type==='job_event').map(m=>m.state),['succeeded']);
});
test('uncertain previous attempt never resubmits',async()=>{
 const h=await harness({stored:{'creaaJob:job1':{job,attempted:true,state:'submitting'}}});await h.send('resume',job);
 assert.equal(h.pageCalls.some(c=>c[0]==='submit'),false);
 assert.equal(h.sent.find(m=>m.type==='job_event').state,'needs_review');
});
test('recovered website task is reported before polling without regeneration',async()=>{
 const h=await harness({responses:{recover:{ok:true,entry:{provider_task_id:'recovered-task'}}}});await h.send('resume',job);
 assert.equal(h.pageCalls.some(c=>c[0]==='submit'),false);
 assert.equal(h.sent.find(m=>m.state==='submitted').provider_task_id,'recovered-task');
});
test('terminal acknowledgement replay is recognized without changing outcome',async()=>{
 const h=await harness({ack:{succeeded:{accepted:false,error:'terminal_state',job_state:'succeeded'}}});await h.send('execute',job);
 assert.equal(h.storage['creaaJob:job1'].pending,null);
 assert.equal(h.sent.some(m=>m.state==='needs_review'),false);
});
test('wrong-account dispatch never touches the page',async()=>{
 const h=await harness();await h.send('execute',{...job,account_id:'other'});
 assert.deepEqual(h.pageCalls.map(c=>c[0]),['inspect']);
});

test('hard stale-attempt rejection clears its outbox and never submits',async()=>{
 const h=await harness({ack:{submitting:{accepted:false,error:'attempt_mismatch',job_state:'claimed'}}});
 await h.send('execute',job);
 assert.equal(h.storage['creaaJob:job1'].pending,null);
 assert.equal(h.pageCalls.some(c=>c[0]==='submit'),false);
 assert.equal(h.sent.filter(m=>m.type==='job_event').length,1);
});
test('new tracking attempt replaces an older active attempt without losing the new command',async()=>{
 let release;let count=0;
 const wait=new Promise(resolve=>{release=resolve;});
 const h=await harness({responses:{poll:async()=>{if(++count===1)await wait;return{ok:true,state:'succeeded',result:{urls:['https://files.creaa.ai/video.mp4'],media_type:'video'}};}}});
 await h.send('resume',{...job,provider_task_id:'task1'});
 await h.send('resume',{...job,attempt_id:'a2',provider_task_id:'task1'});
 release();await spin();
 const events=h.sent.filter(m=>m.type==='job_event');
 assert.equal(events.some(m=>m.attempt_id==='a1'),false);
 assert.equal(events.some(m=>m.attempt_id==='a2' && m.state==='succeeded'),true);
});
test('worker-owned tab may lose its query marker without creating a replacement',async()=>{
 const h=await harness({stored:{creaaTabId:1},tabUrl:'https://creaa.ai/image?session=abc'});await h.send('resume',{...job,provider_task_id:'task1'});
 assert.equal(h.sent.some(m=>m.state==='succeeded'),true);
 assert.equal(h.createdTabs,0);
});
test('distinct jobs share one page executor but both reach their own task and result',async()=>{
 let inPage=0, peak=0;
 const h=await harness({responses:{submit:async args=>{inPage++;peak=Math.max(peak,inPage);await new Promise(setImmediate);inPage--;return {ok:true,provider_task_id:'task-'+args.job_id};}}});
 await Promise.all([h.send('execute',job),h.send('execute',{...job,id:'job2',attempt_id:'b1'})]);
 await spin();
 assert.equal(peak,1);
 const completed=h.sent.filter(m=>m.state==='succeeded');
 assert.deepEqual(completed.map(m=>m.job_id).sort(),['job1','job2']);
 assert.deepEqual(completed.map(m=>m.provider_task_id).sort(),['task-job1','task-job2']);
});
