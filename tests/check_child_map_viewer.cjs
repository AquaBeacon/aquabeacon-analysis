// Offline JavaScript logic test. This is not a browser or pixel-rendering test.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = process.argv[2];
const html = fs.readFileSync(path, 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const elements = {};
let arcCount = 0, downloadBlob = null, alerts = [];
const drawing = new Proxy({}, { get(_, key) {
  if (key === 'arc') return (...v) => { assert(v.every(Number.isFinite)); arcCount++; };
  return (...v) => { for (const value of v) if (typeof value === 'number') assert(Number.isFinite(value)); };
}, set() { return true; } });
for (const match of html.matchAll(/\bid="([^"]+)"[^>]*>/g)) {
  const id = match[1], value = match[0].match(/\bvalue="([^"]*)"/);
  elements[id] = { value: value ? value[1] : '', style: {}, textContent: '', options: [],
    append(x) { this.options.push(x); }, setPointerCapture() {},
    get clientWidth() { return parseFloat(this.style.width) || 900; },
    get clientHeight() { return parseFloat(this.style.height) || 650; },
    getContext() { return drawing; }, toBlob(fn) { fn(new Blob(['stub PNG'])); } };
}
const sandbox = { console, Blob, Math, JSON, setTimeout: fn => fn(),
  window: { devicePixelRatio: 1 }, alert: value => alerts.push(value),
  ResizeObserver: class { observe() {} },
  URL: { createObjectURL(blob) { downloadBlob = blob; return 'stub:download'; }, revokeObjectURL() {} },
  document: { getElementById: id => elements[id],
    createElement: () => ({ click() {} }) } };
vm.createContext(sandbox);
vm.runInContext(script, sandbox, { timeout: 10000 });
const original = vm.runInContext('JSON.stringify(DATA)', sandbox);
for (let i=0; i<elements.map.options.length; i++) {
  elements.map.value=String(i); elements.map.onchange();
}
assert(arcCount > 0);
elements.sizeN.value='2.5'; elements.sizeN.oninput();
assert.equal(elements.size.value, '2.5');
elements.canvas.onpointerdown({ clientX: 0, clientY: 0, pointerId: 1 });
elements.canvas.onpointermove({ clientX: 20, clientY: 10, shiftKey: false });
elements.canvas.onpointerup();
elements.frame.style.width='720px'; elements.frame.style.height='510px';
elements.save.onclick();
(async () => {
  const saved = await downloadBlob.text();
  const settings = JSON.parse(saved);
  assert.equal(settings.width, 720);
  assert.equal(settings.height, 510);
  assert.equal(settings.controls.size, '2.5');
  elements.reset.onclick();
  await elements.load.onchange({ target: { files: [{ text: async () => saved }] } });
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(view)', sandbox)), settings.view);
  assert.equal(elements.size.value, '2.5');
  assert.equal(elements.frame.style.height, '510px');
  elements.png.onclick();
  assert.equal(await downloadBlob.text(), 'stub PNG');
  assert.equal(vm.runInContext('JSON.stringify(DATA)', sandbox), original);
  assert.deepEqual(alerts, []);
  console.log(JSON.stringify({status:'passed', map_entries:elements.map.options.length,
    finite_point_draw_calls:arcCount, controls:true, rotation:true, dimension_restore:true,
    settings_roundtrip:true, original_data_unchanged:true,
    png_handler:true, actual_browser_render:false}));
})().catch(error => { console.error(error); process.exitCode=1; });
