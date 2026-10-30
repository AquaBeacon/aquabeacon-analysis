// Offline control/data verification. This does not launch or simulate a real browser.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
const directory='results/20261028_ngt_004/presentation_mapping';
const html=fs.readFileSync(`${directory}/textured_map.html`,'utf8');
assert(!html.includes('__DATA__'));
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),pending=[];let draws=0,blob;
const gl=new Proxy({getShaderParameter:()=>true,getProgramParameter:()=>true,
 getAttribLocation:()=>0,getUniformLocation:()=>({}),createBuffer:()=>({}),
 createTexture:()=>({}),createShader:()=>({}),createProgram:()=>({}),
 drawArrays(mode,start,count){assert(Number.isFinite(count)&&count>=0);draws++;}},
 {get(target,key){return target[key]??(()=>{});}});
function element(id){if(!elements.has(id))elements.set(id,{value:id==='size'||id==='sizeNumber'?'2':'0',
 checked:true,disabled:false,clientWidth:1100,clientHeight:720,width:1100,height:720,children:[],
 textContent:'',style:{},append(v){this.children.push(v);},appendChild(v){this.children.push(v);},
 addEventListener(){},setPointerCapture(){},getContext(){return gl;},click(){},toDataURL(){return 'data:image/png;base64,';}});return elements.get(id);}
const window={addEventListener(){}};
class MockImage{set src(value){assert(value.startsWith('data:image/jpeg;base64,'));pending.push(()=>this.onload());}}
const context=vm.createContext({document:{getElementById:element,createElement:tag=>element(`${tag}-${Math.random()}`)},
 window,Image:MockImage,devicePixelRatio:1,console,Blob,
 URL:{createObjectURL(value){blob=value;return 'blob:verification';},revokeObjectURL(){}},
 requestAnimationFrame(){},setTimeout(){}});
new vm.Script(script).runInContext(context,{timeout:15000});
for(const run of pending)run();
const viewer=window.NGT_TEXTURE_VIEWER;
const before=crypto.createHash('sha256').update(JSON.stringify(viewer.data)).digest('hex');
assert.equal(window.NGT_TEXTURE_VIEWER_STATE.texturesReady,64);
assert.equal(window.NGT_TEXTURE_VIEWER_STATE.selectedMap,'2');
assert.equal(element('map').children.length,5);
for(let i=0;i<5;i++){element('map').value=i;element('map').onchange();assert.equal(window.NGT_TEXTURE_VIEWER_STATE.selectedMap,String(i));assert.equal(element('source').disabled,i!==2);}
element('map').value=2;element('map').onchange();
element('timeNumber').value=1182;element('timeNumber').oninput();assert.equal(+element('time').value,1182);
assert(window.NGT_TEXTURE_VIEWER_STATE.currentSurface>40);
element('all').checked=false;element('all').onchange();
element('source').onclick();assert.equal(window.NGT_TEXTURE_VIEWER_STATE.yaw,0);
element('sizeNumber').value=4;element('sizeNumber').oninput();assert.equal(+element('size').value,4);
element('view').onkeydown({key:'ArrowLeft',shiftKey:true,preventDefault(){}});
assert.notEqual(window.NGT_TEXTURE_VIEWER_STATE.pan[0],0);
element('saveSettings').onclick();const settings=await blob.text();
assert.equal(JSON.parse(settings).size,4);
element('reset').onclick();
await element('loadSettings').onchange({target:{files:[{text:async()=>settings}]}});
assert.equal(viewer.getSettings().pan[0],JSON.parse(settings).pan[0]);
await element('loadSettings').onchange({target:{files:[{text:async()=>'{}'}]}});
assert(element('status').textContent.includes('設定を読み込めません'));
element('play').onclick();assert.equal(element('play').textContent,'停止');element('play').onclick();
element('save').onclick();
assert.equal(crypto.createHash('sha256').update(JSON.stringify(viewer.data)).digest('hex'),before);
assert(draws>100);
const report={checked_utc:new Date().toISOString(),pid:process.pid,status:'passed',
 method:'Node VM with mock DOM/WebGL; no browser rendering or pixel verification',
 checks:['all five independent map selections','64 embedded photo textures','time range and direct numeric input',
 'source-camera view','point size pair','keyboard pan','settings JSON round-trip and invalid-input rejection',
 'play/stop','PNG export invocation','input scene data unchanged'],draw_calls:draws,
 browser_rendering:'unverified: local file URL browser policy blocked preview; no alternate browser/server used'};
fs.writeFileSync(`${directory}/viewer_logic_verification.json`,JSON.stringify(report,null,2));
console.log(JSON.stringify({status:'passed',draw_calls:draws,scope:report.method}));
