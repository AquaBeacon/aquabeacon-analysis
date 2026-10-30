// Execute the self-contained viewer with a mock DOM/canvas, without launching a browser.
// This checks data/control logic only; it is not a browser-layout or screenshot test.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const root=process.cwd();
const args=process.argv.slice(2);
const files=args.includes('--html')?[args[args.indexOf('--html')+1]]:[...['002','003','004'].map(s=>`results/20261028_ngt_${s}/position_visualization/viewer.html`),
  'results/20261028_ngt_004/map_visualization/wall_viewer.html'];
const reports=[];
for(const file of files){
  const html=fs.readFileSync(path.join(root,file),'utf8');
  assert(!html.includes('__DATA__'));
  const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
  let draws=0,blob;
  const canvasContext=new Proxy({}, {get(target,key){return target[key]??((...args)=>{
    if(['moveTo','lineTo','arc','fillRect'].includes(key)){
      assert(args.filter(x=>typeof x==='number').every(Number.isFinite),`Non-finite draw in ${key}`);draws++;
    }
  });},set(target,key,value){target[key]=value;return true;}});
  const elements=new Map();
  function element(id){if(!elements.has(id))elements.set(id,{value:id==='pointSize'||id==='pointNumber'?'2':'0',checked:true,
    hidden:false,children:[],textContent:'',clientWidth:980,clientHeight:560,width:0,height:0,
    appendChild(child){this.children.push(child);},addEventListener(){},setPointerCapture(){},
    getContext(){return canvasContext;},toDataURL(){return 'data:image/png;base64,';},click(){}});return elements.get(id);}
  const window={addEventListener(){}};
  const context=vm.createContext({document:{getElementById:element,createElement:tag=>element(`new-${tag}-${Math.random()}`)},
    window,devicePixelRatio:1,performance:{now:()=>0},requestAnimationFrame(){},setTimeout(){},Blob,
    URL:{createObjectURL(b){blob=b;return 'blob:unit-test';},revokeObjectURL(){}},console});
  new vm.Script(script,{filename:file}).runInContext(context,{timeout:10000});
  const viewer=window.NGT_VIEWER;
  assert(viewer&&draws>0);
  const kind=viewer.data.kind;
  if(kind==='trajectory'){
    const missing=viewer.data.child.findIndex(p=>p.some(x=>x===null));
    assert(missing>=0);
    element('time').value=missing;element('time').oninput();
    assert.equal(viewer.getState().index,missing);
    assert(element('status').textContent.includes('子機：欠測'));
    element('timeNumber').value='8.0';element('timeNumber').onchange();
    assert.equal(viewer.getState().index,40);
    assert(element('mapControls').hidden);
    element('follow').onclick();assert(viewer.getState().follow);
  }else{
    assert(element('timeControls').hidden);
    for(let i=0;i<viewer.data.maps.length;i++){
      element('mapSelect').value=i;element('mapSelect').onchange();
      assert.equal(viewer.getState().selected,i);
      assert(element('status').textContent.length>20);
    }
    assert.equal(element('mapSelect').children.length,viewer.data.maps.length);
    element('pointNumber').value='4';element('pointNumber').oninput();
    assert.equal(+element('pointSize').value,4);
  }
  element('top').onclick();element('side').onclick();element('reset').onclick();
  element('saveSettings').onclick();
  const settings=await blob.text();
  await element('loadSettings').onchange({target:{files:[{text:async()=>settings}]}});
  element('saveImage').onclick();
  reports.push({file,kind,draw_calls:draws,controls_and_settings:'passed',browser_rendering:'not exercised'});
}
const out=args.includes('--report')?args[args.indexOf('--report')+1]:'results/20261028_ngt_000/position_evaluation/viewer_logic_verification.json';
fs.writeFileSync(out,JSON.stringify({pid:process.pid,checked_utc:new Date().toISOString(),
  method:'Node vm with mock DOM/canvas; no browser and no network',reports},null,2));
console.log(JSON.stringify({status:'passed',viewers:reports.length,scope:'data/control logic only'}));
