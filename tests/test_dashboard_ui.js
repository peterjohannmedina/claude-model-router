// Dependency-free DOM/fetch behavior checks. No sockets, browser or model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
class Element {
  constructor(tag='div',type='') {this.tagName=tag;this.type=type;this.children=[];this.value='';this.checked=false;this.textContent='';this.listeners={};this.attributes={};this.disabled=false;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  setAttribute(key,value){this.attributes[key]=value;}
  setCustomValidity(value){this.validationMessage=value;}
  addEventListener(kind,fn){this.listeners[kind]=fn;}
}
const html=fs.readFileSync(path.join(root,'assets/dashboard/index.html'),'utf8');
const nodes={};
for(const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  nodes[match[3]]=new Element(match[1],/\btype="([^"]+)"/.exec(match[2])?.[1]||'');
}
const session='11111111-1111-4111-8111-111111111111';
const config={schema_version:1,decisions:{enabled:false,api_key_env:'OPENAI_API_KEY',timeout_seconds:3,minimum_confidence:.6,max_input_tokens:null,max_cost_usd:null},local:{enabled:false,base_url:null,model:null,api_key_env:null,timeout_seconds:2,max_output_tokens:512,efforts:['default'],allow_remote:false,prefer_when_eligible:false},observability:{enabled:true,retention_days:7,max_events:500,local_input_usd_per_million:null,local_output_usd_per_million:null}};
let data={config,config_version:'v1',sessions:[session],selected_session:null,observed_at:'2026-10-15T22:00:00Z',daily:[{day:'2026-10-15',requests:3,completed_executions:1}],totals:{requests:3,completed_executions:1,evaluator:{input_tokens:{known:0,unknown_requests:0}},downstream:{input_tokens:{known:300,unknown_requests:1}},evaluator_base_cost_estimate_usd:0,local_cost_estimate_usd:null},events:[{source:'router',request_id:session,session_id:session,model:'gpt-6.1-sol',effort:'low',execution_status:'unobserved',mode:'recommend',observed_at:'2026-10-15T22:00:00Z'},{source:'claude_exec',request_id:session,session_id:session,model:null,execution_status:'completed',downstream_usage:{input_tokens:300,output_tokens:2},observed_at:'2026-10-15T22:00:01Z'}]};
const calls=[];let interval, failNext=false, confirmed=false, nextSaveIssue=null;
const fetch=async(url,options)=>{
  calls.push({url,options});
  assert.match(url,/^\/api\/(auth|session|state(?:\?session=[a-z0-9-]+)?|config|ledger\/clear)$/);
  if(url==='/api/auth') {assert.equal(options.headers['X-Routing-Bootstrap'],'synthetic-bootstrap');return {ok:true,json:async()=>({authorized:true,csrf:'synthetic-csrf'})};}
  assert.equal(options.headers['X-Routing-CSRF'],'synthetic-csrf');
  if(url==='/api/session') return {ok:true,json:async()=>({csrf:'synthetic-csrf'})};
  if(url.startsWith('/api/state')) {if(failNext){failNext=false;throw new Error('synthetic_disconnect');}return {ok:true,json:async()=>structuredClone(data)};}
  assert.equal(options.headers['X-Routing-CSRF'],'synthetic-csrf');
  if(url==='/api/config') {
    if(nextSaveIssue){const issue=nextSaveIssue;nextSaveIssue=null;return {ok:false,json:async()=>issue};}
    const body=JSON.parse(options.body);assert.equal(body.version,'v1');data.config=body.config;data.config_version='v3';
  }
  if(url==='/api/ledger/clear') {assert.deepEqual(JSON.parse(options.body),{confirm:true});data.events=[];data.totals.requests=0;}
  return {ok:true,json:async()=>({saved:true,config_version:data.config_version})};
};
let fragmentRemoved=false;
const storage=new Map();
const context=vm.createContext({sessionStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k)||null},location:{hash:'#authorize=synthetic-bootstrap',pathname:'/'},history:{replaceState:(_a,_b,path)=>{assert.equal(path,'/');fragmentRemoved=true;}},document:{hidden:false,getElementById:id=>{assert.ok(nodes[id],'unknown HTML id '+id);return nodes[id];},createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag)},fetch,setTimeout,clearTimeout,AbortController,structuredClone,setInterval:fn=>{interval=fn;},confirm:()=>confirmed});
vm.runInContext(fs.readFileSync(path.join(root,'assets/dashboard/app.js'),'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();await flush();
  assert.ok(fragmentRemoved,'bootstrap token remained in browser history');
  assert.equal(nodes.requests.textContent,'3');assert.equal(nodes.executions.textContent,'1');
  assert.equal(nodes['local-cost'].textContent,'Unknown');assert.match(nodes['downstream-unknown'].textContent,/1 requests with unknown/);
  assert.equal(nodes.events.children[0].children[0].children[3].textContent,'Recommendation');
  assert.equal(nodes.events.children[1].children[0].children[3].textContent,'Executed · complete');
  assert.equal(nodes.chart.children.length,2);
  assert.match(nodes['coverage-status'].textContent,/Historical records do not prove an active feed/);
  data.config.decisions.enabled=true;interval();await flush();
  assert.equal(nodes['decisions-configured'].textContent,'Enabled · settings only');
  assert.match(nodes['decisions-observed'].textContent,/unobserved/);
  let nativePrevented=false;
  nodes['decisions-key-env'].value='';
  nodes['decisions-key-env'].listeners.invalid({preventDefault(){nativePrevented=true;}});
  assert.ok(nativePrevented);
  assert.match(nodes['save-status'].textContent,/Enter OPENAI_API_KEY/);
  assert.equal(nodes['decisions-key-env'].attributes['aria-invalid'],'true');
  nodes['decisions-key-env'].value='OPENAI_API_KEY';nodes['decisions-key-env'].listeners.input();
  assert.equal(nodes['decisions-key-env'].validationMessage,'');
  assert.equal(nodes['decisions-key-env'].attributes['aria-invalid'],'false');
  const beforeInvalid=calls.filter(c=>c.url==='/api/config').length;
  for(const id of ['decisions-key-env','local-key-env']) {
    nodes[id].value='sk-synthetic-do-not-send-or-echo';nodes['config-form'].listeners.input();
    await nodes['config-form'].listeners.submit({preventDefault(){}});
    assert.equal(calls.filter(c=>c.url==='/api/config').length,beforeInvalid,'key-like value was sent to server');
    assert.match(nodes['save-status'].textContent,/environment-variable name/);
    assert.doesNotMatch(nodes['save-status'].textContent,/sk-synthetic|Reload/);
    assert.equal(nodes[id].attributes['aria-invalid'],'true');
    nodes[id].value=id==='decisions-key-env'?'OPENAI_API_KEY':'';
  }
  data.totals.otel={sessions:1,responses:2,input_tokens:{known:50,unknown_responses:0},output_tokens:{known:0,unknown_responses:1}};
  data.otel_receiver={enabled:true,last_received_at:'2026-10-15T22:00:02Z'};
  data.totals.hooks={sessions:1,observations:2,stops:1};
  data.events.unshift({source:'claude_otel',request_id:session,session_id:session,mode:'otel_session',model_source:'startup_configuration',model:'gpt-6.1-sol',effort:'low',execution_status:'session_observed'});
  interval();await flush();
  assert.match(nodes['otel-receiver'].textContent,/listening.*no metadata received/);
  assert.match(nodes['otel-summary'].textContent,/2 lifecycle observations/);
  assert.match(nodes['otel-summary'].textContent,/Hook token usage and billed cost are unavailable/);
  assert.equal(nodes.requests.textContent,'3','OTel response counts leaked into router/exec totals');
  assert.equal(nodes.events.children[0].children[0].children[3].textContent,'Session settings observed');
  nodes['local-model'].value='edited-model';nodes['config-form'].listeners.input();
  data.config.local.model='externally-saved';data.config_version='v2';interval();await flush();
  assert.equal(nodes['local-model'].value,'edited-model','poll overwrote unsaved form');
  // A dirty form retains its original CAS version even when live polls see v2.
  await nodes['config-form'].listeners.submit({preventDefault(){}});
  assert.equal(data.config.local.model,'edited-model');assert.equal(calls.find(c=>c.url==='/api/config').options.method,'POST');
  assert.match(nodes['save-status'].textContent,/No provider request/);
  nextSaveIssue={error:'invalid_confidence_threshold',field:'decisions.minimum_confidence'};
  await nodes['config-form'].listeners.submit({preventDefault(){}});
  assert.match(nodes['save-status'].textContent,/Minimum confidence: Use a minimum confidence between 0 and 1/);
  assert.doesNotMatch(nodes['save-status'].textContent,/Reload/);
  nextSaveIssue={error:'configuration_changed_reload_before_saving'};
  await nodes['config-form'].listeners.submit({preventDefault(){}});
  assert.match(nodes['save-status'].textContent,/Reload saved settings before retrying/);
  nextSaveIssue={error:'synthetic-private-unexpected-error',field:'synthetic-private-field'};
  await nodes['config-form'].listeners.submit({preventDefault(){}});
  assert.match(nodes['save-status'].textContent,/Could not confirm the save/);
  assert.doesNotMatch(nodes['save-status'].textContent,/synthetic-private|No settings were saved|Reload/);
  failNext=true;interval();await flush();
  assert.equal(nodes.connection.textContent,'Disconnected');assert.match(nodes.message.textContent,/may be stale/);
  interval();await flush();assert.match(nodes.connection.textContent,/Dashboard connected/);
  nodes.session.value=session;await nodes.session.listeners.change();assert.ok(calls.some(c=>c.url==='/api/state?session='+session));
  await nodes.clear.listeners.click();assert.equal(calls.filter(c=>c.url==='/api/ledger/clear').length,0);
  confirmed=true;await nodes.clear.listeners.click();assert.equal(nodes.requests.textContent,'0');
  assert.match(nodes.events.children[0].textContent,/No instrumented activity/);
  data.sessions=[];interval();await flush();
  assert.match(nodes['coverage-status'].textContent,/No recorded sessions yet/);
  assert.match(nodes['coverage-status'].textContent,/Dashboard connection does not connect sessions/);
  data.totals.otel={sessions:0,responses:0};
  data.otel_receiver={enabled:true,runtime_version:'2026.10.16.2',requests:0,accepted_records:0,ignored_records:0,rejected_records:0};interval();await flush();
  assert.match(nodes['otel-receiver'].textContent,/listening.*no metadata received/);
  assert.match(nodes['otel-diagnostics'].textContent,/Only explicitly configured hook producers/);
  assert.equal(nodes.session.children[0].textContent,'All recorded sessions');
  data.otel_receiver.unsupported_transport_records=3;data.otel_receiver.ignored_records=3;interval();await flush();
  assert.match(nodes['otel-diagnostics'].textContent,/OTel payloads are unsupported/);
  data.otel_receiver.recording_disabled_batches=1;interval();await flush();
  assert.match(nodes['otel-diagnostics'].textContent,/recording was disabled/);
  console.log('Dashboard UI behavior checks pass: render, scope, unknown usage, polling, dirty edits, CAS, consent-free save, disconnect recovery and explicit clear.');
})().catch(error=>{console.error(error);process.exitCode=1;});
