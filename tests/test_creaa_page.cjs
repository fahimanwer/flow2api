const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const catalog = require('./fixtures/creaa/models.json');
const source = fs.readFileSync('worker-extension/creaa-page.js', 'utf8');
function fixture(options = {}) {
  const storage = new Map(); const calls = [];
  const params = {ratio: '9:16'};
  const app = {
    currentParams: params, currentMode: 'image',
    isUserAuthenticated: () => options.loggedIn !== false,
    getCurrentUnlimitedCacheUserId: () => options.account || '42',
    ensureUnifiedModelConfig: async () => true,
    refreshUnlimitedConfig: async () => options.fresh !== false,
    hasUnlimitedAccessForModel: () => options.unlimited !== false,
    getImageCostForImageParams: () => 12, getVideoCostForCurrentParams: () => 100,
    getSerializableModelInfo: m => ({model_id:m.model_id, provider:m.provider}),
    uploadTempImageAsset: async file => { calls.push(['upload',file.type]); return 'https://files.creaa.ai/ref.png'; },
    checkVideoGenerationPreflight: async () => true,
    generateMediaTask: async (media,payload) => { calls.push(['generate',media,payload]); if(options.submitError) throw Error('lost connection'); return options.submitResponse || {success:true,task_id:'task-1'}; },
    fetchMediaTaskStatus: async () => options.status || {data:{status:'PROCESSING',progress:0}},
  };
  const context = vm.createContext({module:{exports:{}}, window:{imageEditChat:app,IMAGE_MODELS_DATA:catalog.image_models,VIDEO_MODELS_DATA:catalog.video_models},
    location:{origin:'https://creaa.ai'}, localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    URL, File, Uint8Array, atob, console});
  vm.runInContext(source, context);
  const run = (op,arg={}) => context.module.exports.creaaPageOperation(op,{job_id:'j1',attempt_id:'a1',account_id:'42',media_type:'image',request:{model:'openai/gpt-image-2',prompt:'A cup',aspect_ratio:'1:1',image_size:'1k',quality:'medium',billing_policy:'unlimited_only'},...arg});
  return {run,calls,app,params};
}
test('preflight is read-only and restores page selection',async()=>{
 const f=fixture(); const r=await f.run('prepare'); assert.equal(r.ok,true); assert.equal(r.billing.estimated_credits,0);
 assert.equal(f.calls.length,0); assert.equal(f.app.currentParams,f.params);
});
test('rejects wrong account and missing entitlement before generation',async()=>{
 for(const [options,code] of [[{account:'43'},'account_changed'],[{unlimited:false},'not_unlimited'],[{fresh:false},'eligibility_unavailable'],[{loggedIn:false},'needs_login']]) {
  const f=fixture(options); const r=await f.run('submit'); assert.equal(r.code,code); assert.equal(f.calls.length,0);
 }
});
test('bounded paid request rejects before submission',async()=>{
 const f=fixture({unlimited:false}); const r=await f.run('submit',{request:{model:'openai/gpt-image-2',prompt:'cup',billing_policy:'allow_credits',max_credits:5}});
 assert.equal(r.code,'credit_limit'); assert.equal(f.calls.length,0);
});
test('accepted image preserves exact model and durable task; duplicate does not submit',async()=>{
 const f=fixture(); const r=await f.run('submit'); assert.equal(r.provider_task_id,'task-1');
 const again=await f.run('submit'); assert.equal(again.provider_task_id,'task-1'); assert.equal(f.calls.length,1);
 assert.equal(f.calls[0][2].model_id,'openai/gpt-image-2'); assert.equal(f.calls[0][2].image_size,'1K');
 assert.equal(f.calls[0][2].session_id,'flow2api-j1');
});
test('lost submission response is persisted as uncertain and never repeated',async()=>{
 const f=fixture({submitError:true}); assert.equal((await f.run('submit')).code,'submit_uncertain');
 const again=await f.run('submit'); assert.equal(again.already_attempted,true); assert.equal(f.calls.length,1);
});
test('video settings and image references map to the observed website contract',async()=>{
 const f=fixture(); const r=await f.run('submit',{media_type:'video',request:{model:'seedance-2.5',prompt:'Slow camera move',duration:6,aspect_ratio:'16:9',resolution:'1280x720',references:['https://files.creaa.ai/a.png','https://files.creaa.ai/b.png']}});
 assert.equal(r.ok,true); const body=f.calls[0][2]; assert.equal(body.duration,6); assert.equal(body.resolution,'1280x720');
 assert.equal(body.image_url,'https://files.creaa.ai/a.png'); assert.equal(body.reference_images_urls[0],'https://files.creaa.ai/b.png');
});
test('unsupported durations, qualities and mismatching resolution never submit',async()=>{
 const f=fixture(); let r=await f.run('submit',{media_type:'video',request:{model:'seedance-2.5',prompt:'cup',duration:50}});
 assert.equal(r.code,'unsupported_duration');
 r=await f.run('submit',{media_type:'video',request:{model:'seedance-2.5',prompt:'cup',duration:6,aspect_ratio:'9:16',resolution:'1280x720'}});
 assert.equal(r.code,'resolution_ratio_mismatch'); assert.equal(f.calls.length,0);
});
test('single image reference uses multi-image generation contract',async()=>{
 const f=fixture(); await f.run('submit',{request:{model:'openai/gpt-image-2',prompt:'Edit cup',references:['https://files.creaa.ai/ref.png']}});
 assert.equal(f.calls[0][2].input_images[0],'https://files.creaa.ai/ref.png');
});
test('task status extracts original artifacts and requires a URL before success',async()=>{
 const f=fixture({status:{data:{status:'SUCCEEDED'},raw:{artifacts:[{url:'https://files.creaa.ai/result.png'}]}}});
 const r=await f.run('poll',{provider_task_id:'task-1'});assert.equal(r.state,'succeeded');assert.equal(r.result.urls[0],'https://files.creaa.ai/result.png');
 const empty=fixture({status:{data:{status:'SUCCEEDED'}}}); assert.equal((await empty.run('poll',{provider_task_id:'task-1'})).state,'running');
});
test('poll failures are not reported as successful generation',async()=>{
 const f=fixture({status:{data:{status:'FAILED',error:'Provider refused'}}}); const r=await f.run('poll',{provider_task_id:'task-1'});assert.equal(r.state,'failed');
 const error=fixture({status:{success:false,error:'Unauthenticated'}}); assert.equal((await error.run('poll',{provider_task_id:'task-1'})).ok,false);
});
