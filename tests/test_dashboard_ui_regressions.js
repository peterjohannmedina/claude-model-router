// Focused asynchronous save and authorized-navigation regressions. No network.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const script=fs.readFileSync(path.join(root,'assets/dashboard/app.js'),'utf8');
const html=fs.readFileSync(path.join(root,'assets/dashboard/index.html'),'utf8');
class Element{
  constructor(type='',owner=null){this.type=type;this.ownerDocument=owner;this.value='';this.checked=false;this.children=[];this.listeners={};this.textContent='';}
  append(...items){this.children.push(...items);}
  contains(node){return this===node||this.children.some(child=>child?.contains?.(node));}
  replaceChildren(...items){if(this.children.some(child=>child?.contains?.(this.ownerDocument?.activeElement)))this.ownerDocument.activeElement=null;this.children=items;}
  focus(options){this.ownerDocument.activeElement=this;this.focusOptions=options;}
  setAttribute(){}addEventListener(kind,fn){this.listeners[kind]=fn;}
  setCustomValidity(value){this.validationMessage=value;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function boot(hash,delaySave=false,denied=false,initialStorage=[]){
  const nodes={},calls=[],historyChanges=[];
  for(const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g))nodes[match[3]]=new Element(/\btype="([^"]+)"/.exec(match[2])?.[1]||'');
  let snapshot={config:JSON.parse(fs.readFileSync(path.join(root,'assets/routing-config.example.json'),'utf8')),config_version:'base',events:[],sessions:[],daily:[],observed_at:'2026-10-15T22:00:00Z',totals:{requests:0,completed_executions:0,evaluator:{input_tokens:{known:0,unknown_requests:0}},downstream:{input_tokens:{known:0,unknown_requests:0}},evaluator_base_cost_estimate_usd:null,local_cost_estimate_usd:null}};
  let interval, release, saves=0, nextStateGate=null;
  const gate=new Promise(resolve=>{release=resolve;});
  const storage=new Map([['routing-csrf','fixture-capability'],...initialStorage]);
  const fetch=async(url,options)=>{
    calls.push({url,options});
    if(url==='/api/auth')throw new Error('navigation_anchor_sent_as_bootstrap');
    assert.equal(options.headers['X-Routing-CSRF'],'fixture-capability');
    if(denied)return{ok:false,status:403,json:async()=>({error:'forbidden_origin_or_session'})};
    if(url==='/api/session')return{ok:true,json:async()=>({csrf:'fixture-capability'})};
    if(url.startsWith('/api/state')){
      const captured=structuredClone(snapshot), delayed=nextStateGate;captured.selected_session=new URL('http://fixture'+url).searchParams.get('session');nextStateGate=null;
      if(delayed)await delayed;
      return{ok:true,json:async()=>captured};
    }
    if(url==='/api/models/refresh')return{ok:true,json:async()=>({model_catalog:snapshot.model_catalog})};
    assert.equal(url,'/api/config');const body=JSON.parse(options.body);
    if(delaySave&&saves===0)await gate;
    saves++;snapshot.config=body.config;snapshot.config_version='saved-'+saves;
    return{ok:true,json:async()=>({saved:true,config_version:snapshot.config_version})};
  };
  const document={hidden:false,activeElement:null,getElementById:id=>nodes[id],createElement:type=>new Element(type,document),createElementNS:()=>new Element('',document)};
  Object.values(nodes).forEach(node=>{node.ownerDocument=document;});
  const context=vm.createContext({document,location:{hash,pathname:'/'},history:{replaceState:(_a,_b,value)=>historyChanges.push(value)},sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},fetch,URL,AbortController,setTimeout,clearTimeout,setInterval:fn=>{interval=fn;},confirm:()=>false});
  vm.runInContext(script,context);
  return{nodes,document,calls,historyChanges,release,storage,poll:()=>interval(),snapshot:()=>snapshot,forbid:()=>{denied=true;},
    holdNextPoll:()=>{let resume;nextStateGate=new Promise(resolve=>{resume=resolve;});return resume;}};
}
async function anchors(){
  for(const hash of ['#providers','#activity','#overview']){
    const ui=boot(hash);await flush();await flush();
    assert.equal(ui.calls.filter(c=>c.url==='/api/auth').length,0,'navigation was treated as bootstrap');
    assert.equal(ui.nodes.requests.textContent,'0','stored authorized session was not recovered');
    assert.equal(ui.historyChanges.length,0,'navigation anchor was discarded');
    ui.poll();await flush();assert.match(ui.nodes.connection.textContent,/Dashboard connected/);
  }
  const expired=boot('#authorize=expired-nonce');await flush();await flush();
  assert.equal(expired.nodes.requests.textContent,'0','failed bootstrap discarded a valid stored capability');
  assert.equal(expired.historyChanges.length,1);
}
async function delayedSave(){
  const ui=boot('',true);await flush();await flush();
  ui.nodes['local-model'].value='first-snapshot';ui.nodes['config-form'].listeners.input();
  const pending=ui.nodes['config-form'].listeners.submit({preventDefault(){}});
  await flush();
  ui.nodes['local-model'].value='newer-unsaved-edit';ui.nodes['config-form'].listeners.input();
  ui.poll();await flush();
  ui.release();await pending;await flush();
  assert.equal(ui.nodes['local-model'].value,'newer-unsaved-edit','save response clobbered a newer edit');
  assert.match(ui.nodes['save-status'].textContent,/newer.*unsaved/i);
  assert.equal(ui.snapshot().config.local.model,'first-snapshot','first POST was not a snapshot');
  await ui.nodes['config-form'].listeners.submit({preventDefault(){}});
  const posts=ui.calls.filter(c=>c.url==='/api/config');
  assert.equal(JSON.parse(posts[1].options.body).version,'saved-1','next edit used a stale saved version');
  assert.equal(ui.snapshot().config.local.model,'newer-unsaved-edit');
}
async function stalePoll(){
  const ui=boot('',true);await flush();await flush();
  ui.nodes['local-model'].value='saved-before-old-get';ui.nodes['config-form'].listeners.input();
  const pending=ui.nodes['config-form'].listeners.submit({preventDefault(){}});await flush();
  const resumePoll=ui.holdNextPoll();ui.poll();await flush();
  ui.release();await pending;
  assert.equal(ui.nodes['local-model'].value,'saved-before-old-get');
  resumePoll();await flush();await flush();
  assert.equal(ui.nodes['local-model'].value,'saved-before-old-get','old GET restored the pre-save form after POST completion');
  ui.nodes['local-model'].value='next-edit';ui.nodes['config-form'].listeners.input();
  await ui.nodes['config-form'].listeners.submit({preventDefault(){}});
  const posts=ui.calls.filter(c=>c.url==='/api/config');
  assert.equal(JSON.parse(posts[1].options.body).version,'saved-1','old GET restored the pre-save version');
}
async function expiredAuthorization(){
  const initial=boot('',false,true);await flush();await flush();
  assert.equal(initial.nodes.connection.textContent,'Browser authorization required');
  assert.match(initial.nodes.message.textContent,/installed shortcut or bootstrap open/);
  assert.match(initial.nodes.message.textContent,/no server restart/);
  const calls=initial.calls.length;initial.poll();initial.poll();await flush();
  assert.equal(initial.calls.length,calls,'unauthorized tab kept polling and overwrote recovery guidance');
  const existing=boot('');await flush();await flush();
  existing.forbid();existing.poll();await flush();await flush();
  assert.equal(existing.nodes.connection.textContent,'Browser authorization required');
  assert.equal(existing.nodes.save.disabled,true);
  assert.match(existing.nodes.message.textContent,/displayed values may be stale/);
  const after=existing.calls.length;existing.poll();await flush();assert.equal(existing.calls.length,after);
  const racing=boot('');await flush();await flush();
  const resume=racing.holdNextPoll();racing.poll();await flush();racing.forbid();
  await racing.nodes['config-form'].listeners.submit({preventDefault(){}});
  resume();await flush();await flush();
  assert.equal(racing.nodes.connection.textContent,'Browser authorization required','late successful GET cleared a newer authorization failure');
  assert.match(racing.nodes.message.textContent,/installed shortcut or bootstrap open/);
}
async function observationExpansion(){
  const ui=boot('');await flush();await flush();
  const a={source:'claude_otel',session_id:'11111111-1111-4111-8111-111111111111',request_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',mode:'otel_response',model:'gpt-6-sol',effort:'max'};
  const b={...a,request_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'},c={...a,request_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'};
  ui.snapshot().events=[a,b];ui.poll();await flush();
  const opened=ui.nodes.events.children[0];opened.open=true;opened.children[0].focus();
  for(let i=0;i<3;i++){ui.poll();await flush();assert.equal(ui.nodes.events.children[0],opened);assert.equal(opened.open,true);assert.equal(ui.document.activeElement,opened.children[0]);}
  ui.snapshot().events=[c,b,a];ui.poll();await flush();
  assert.equal(ui.nodes.events.children[2],opened,'reordering changed record identity');
  assert.equal(opened.open,true,'new event collapsed an existing row');
  assert.equal(ui.document.activeElement,opened.children[0],'reordering lost summary focus');
  assert.equal(opened.children[0].focusOptions.preventScroll,true);
  ui.snapshot().events=[c,b,{...a,effort:'high'}];ui.poll();await flush();
  const enriched=ui.nodes.events.children[2];assert.notEqual(enriched,opened);
  assert.equal(enriched.open,true,'enrichment collapsed the same observation');
  assert.equal(ui.document.activeElement,enriched.children[0]);
  enriched.open=false;ui.poll();await flush();assert.equal(ui.nodes.events.children[2].open,false,'poll reopened a manually closed row');
  ui.snapshot().events=[a,c,b];ui.poll();await flush();assert.equal(ui.nodes.events.children[0].open,false,'reordering reopened a manually closed row');
  ui.nodes.events.children[0].open=true;
  ui.snapshot().events=[{...a,session_id:'22222222-2222-4222-8222-222222222222'}];ui.poll();await flush();
  assert.equal(ui.nodes.events.children[0].open,false,'a different session borrowed expansion state');
  ui.nodes.events.children[0].open=true;
  ui.snapshot().events=[{...a,source:'router',session_id:'22222222-2222-4222-8222-222222222222'}];ui.poll();await flush();
  assert.equal(ui.nodes.events.children[0].open,false,'a different source borrowed expansion state');
}
async function catalogControls(){
  const ui=boot('');await flush();await flush();
  assert.equal(ui.calls.filter(c=>c.url==='/api/models/refresh').length,0,'polling performed account discovery');
  const reported=id=>({id,efforts:['low','high'],policy_supported:true,input_modalities:['text']});
  ui.snapshot().model_catalog={status:'queried',source:'claude_model_picker_confirmation',models:[reported('model-one'),reported('model-two')]};
  ui.snapshot().config.native={allowed_models:['model-one']};ui.poll();await flush();
  let choices=ui.nodes['catalog-models'].children.map(label=>label.children[0]);
  assert.equal(choices.length,2);assert.equal(choices[0].checked,true);assert.equal(choices[1].checked,false);
  assert.equal(ui.nodes['native-managed'].checked,true);assert.equal(choices[1].disabled,false);
  choices[1].checked=true;choices[1].listeners.change();ui.nodes['config-form'].listeners.input();
  ui.nodes.reload.listeners.click();await flush();await flush();
  assert.equal(ui.nodes['catalog-models'].children[1].children[0].checked,false,'reload retained an unsaved choice with an unchanged catalog signature');
  choices[1].checked=true;choices[1].listeners.change();ui.nodes['config-form'].listeners.input();
  ui.poll();await flush();assert.equal(ui.nodes['catalog-models'].children[1].children[0].checked,true,'poll discarded model edit');
  ui.snapshot().model_catalog.models.push(reported('new-model'));ui.poll();await flush();
  choices=ui.nodes['catalog-models'].children.map(label=>label.children[0]);
  assert.equal(choices[2].checked,false,'newly discovered model became eligible');
  await ui.nodes['config-form'].listeners.submit({preventDefault(){}});
  const save=ui.calls.filter(c=>c.url==='/api/config').at(-1);assert.deepEqual(JSON.parse(save.options.body).config.native.allowed_models,['model-one','model-two']);
  for(const label of ui.nodes['catalog-models'].children)label.children[0].checked=false;
  ui.nodes['config-form'].listeners.input();await ui.nodes['config-form'].listeners.submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(ui.calls.filter(c=>c.url==='/api/config').at(-1).options.body).config.native.allowed_models,[]);
  assert.match(ui.nodes['native-policy-status'].textContent,/blocks native routing/);
  ui.snapshot().totals.hooks={sessions:4,observations:42,stops:2};ui.poll();await flush();
  assert.equal(ui.nodes['observed-sessions'].textContent,'4');assert.equal(ui.nodes['observed-responses'].textContent,'42');
  assert.equal(ui.nodes.requests.textContent,'0');assert.equal(ui.nodes.executions.textContent,'0');
  await ui.nodes['catalog-refresh'].listeners.click();assert.equal(ui.calls.filter(c=>c.url==='/api/models/refresh').length,1);
  ui.forbid();await ui.nodes['catalog-refresh'].listeners.click();
  assert.equal(ui.nodes['catalog-refresh'].disabled,true);assert.equal(ui.nodes['native-managed'].disabled,true);
}
async function decisionProvenance(){
  const ui=boot('');await flush();await flush();
  ui.snapshot().events=[{source:'router',session_id:'unassigned',request_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    requested_provider:'decisions',used_provider:'rules',evaluator_status:'blocked',evaluator_reason:'local_credential_error',
    credential_source:'none',evaluator_attempts:0,observed_at:'2026-10-17T00:00:00Z'}];
  ui.poll();await flush();
  assert.match(ui.nodes['decisions-observed'].textContent,/blocked \/ local_credential_error/);
  const labels=ui.nodes.events.children[0].children[1].children.map(n=>n.textContent);
  assert.ok(labels.includes('decisions / rules'));assert.ok(labels.includes('blocked / local_credential_error'));
  assert.ok(labels.includes('Credential delivery'));
  ui.snapshot().events[0]={...ui.snapshot().events[0],selected_candidate_id:'sol_low',selected_action:'stay',
    evaluator_model:'gpt-6-luna',policy_version:'v1',confidence:.78,selection_elapsed_ms:110,elapsed_ms:125,
    evaluator_attempted:true,evaluator_succeeded:true,evaluator_fallback:false,
    evaluator_usage:{input_tokens:384,output_tokens:0},admitted_choices:[
      {id:'sol_low',model:'gpt-6.1-sol',effort:'low',action:'stay',adapter:'native'},
      {id:'luna_high',model:'gpt-6-luna',effort:'high',action:'delegate',adapter:'native'}],
    choice_probabilities:{sol_low:.78,luna_high:.22}};
  ui.poll();await flush();
  const detail=ui.nodes.events.children[0].children[1].children.map(n=>n.textContent);
  assert.ok(detail.includes('Evaluator confidence (not task quality)'));assert.ok(detail.includes('0.780'));
  assert.ok(detail.includes('sol_low / stay'));assert.ok(detail.includes('110 / 125'));
  assert.ok(detail.includes('true / true / false'));
  assert.ok(detail.some(t=>t.includes('luna_high: gpt-6-luna / high / delegate / native (probability 0.220)')));
  assert.ok(detail.includes('0.00003840; billed cost unknown'));
  assert.ok(detail.some(t=>t.includes('session UUID alone does not link')));
  delete ui.snapshot().events[0].confidence;delete ui.snapshot().events[0].admitted_choices;
  ui.poll();await flush();
  const old=ui.nodes.events.children[0].children[1].children.map(n=>n.textContent);
  assert.ok(old.includes('Not recorded'));
}
async function restoredTabState(){
  const first=boot('');await flush();await flush();
  first.nodes['local-model'].value='safe-unsaved-model';first.nodes['config-form'].listeners.input();
  const saved=first.storage.get('routing-draft');assert.ok(saved,'safe unsaved form was not retained');
  const second=boot('#providers',false,false,Array.from(first.storage));await flush();await flush();
  assert.equal(second.nodes['local-model'].value,'safe-unsaved-model','reload lost safe unsaved settings');
  assert.match(second.nodes['save-status'].textContent,/Unsaved settings restored/);
  second.poll();await flush();assert.equal(second.nodes['local-model'].value,'safe-unsaved-model');
  second.nodes['local-key-env'].value='sk-synthetic-must-not-persist';second.nodes['config-form'].listeners.input();
  assert.equal(second.storage.get('routing-draft'),saved,'credential-like value entered browser storage');
  second.nodes.reload.listeners.click();await flush();assert.equal(second.storage.get('routing-draft'),'');
  const scope='11111111-1111-4111-8111-111111111111';
  const scoped=boot('',false,false,[['routing-session',scope]]);await flush();await flush();
  assert.ok(scoped.calls.some(c=>c.url==='/api/state?session='+scope),'page reload lost session scope');
  const key=JSON.stringify(['router',scope,scope]);
  const expanded=boot('',false,false,[['routing-expanded',JSON.stringify([key])]]);await flush();await flush();
  expanded.snapshot().events=[{source:'router',session_id:scope,request_id:scope}];expanded.poll();await flush();
  assert.equal(expanded.nodes.events.children[0].open,true,'reload lost expanded observation');
}
(async()=>{const selected=process.argv[2];if(!selected||selected==='restore')await restoredTabState();if(!selected||selected==='anchors')await anchors();if(!selected||selected==='save')await delayedSave();if(!selected||selected==='poll')await stalePoll();if(!selected||selected==='auth')await expiredAuthorization();if(!selected||selected==='expansion')await observationExpansion();if(!selected||selected==='catalog')await catalogControls();if(!selected||selected==='decisions')await decisionProvenance();console.log('Dashboard navigation, save, poll, authorization, catalog controls and expansion regressions pass.');})().catch(error=>{console.error(error);process.exitCode=1;});
