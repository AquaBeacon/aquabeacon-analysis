// Real-browser inspection of local reports; no server or user browser profile.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';

const args = process.argv.slice(2);
const option = key => args[args.indexOf(key) + 1];
const sha256 = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
assert(args.includes('--experiment') && option('--experiment') === '20261103_field_review_000');
assert(args.includes('--playwright-root') && args.includes('--browser-executable'));
const require = createRequire(path.resolve(option('--playwright-root'), 'package.json'));
const {chromium} = require('playwright');
const root = process.cwd();
const out = path.join(root, 'results/20261103_field_review_000/research_review');
const evidence = path.join(root, '.local/field_review_20261103/browser');
fs.mkdirSync(evidence, {recursive: true});
const record = {started_utc: new Date().toISOString(), owner: 'field_review_20261103',
  purpose: 'headless local report and 3D viewer verification', reports: [], viewers: []};
let server, browser;
try {
  server = await chromium.launchServer({headless: true,
    executablePath: option('--browser-executable'), args: ['--disable-gpu']});
  const proc = server.process();
  record.browser_pid = proc.pid;
  record.browser_command = proc.spawnargs;
  fs.writeFileSync(path.join(evidence, 'process.json'), JSON.stringify(record, null, 2));
  browser = await chromium.connect(server.wsEndpoint());
  const page = await browser.newPage({viewport: {width: 1360, height: 980}});
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  for (const name of ['report', '04_pool', '05_nanyo', '06_simulation', '07_mapping', '../position_fallback/report']) {
    await page.goto(pathToFileURL(path.join(out, `${name}.html`)).href);
    await page.evaluate(() => document.fonts.ready);
    const result = await page.evaluate(() => ({title: document.title,
      images: [...document.images].map(x => ({source: x.getAttribute('src'), loaded: x.complete && x.naturalWidth > 0})),
      width: innerWidth, contentWidth: document.documentElement.scrollWidth,
      tables: document.querySelectorAll('table').length}));
    assert(result.images.every(x => x.loaded), `${name}: missing image`);
    assert(result.contentWidth <= result.width + 1, `${name}: horizontal overflow`);
    await page.screenshot({path: path.join(evidence, `${name.replaceAll('../', '').replaceAll('/', '_')}.png`)});
    record.reports.push({file: `${name}.html`, sha256: sha256(path.join(out, `${name}.html`)), ...result});
  }
  await page.setViewportSize({width: 480, height: 900});
  await page.goto(pathToFileURL(path.join(out, 'report.html')).href);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'mobile overflow');
  await page.screenshot({path: path.join(evidence, 'report_mobile.png')});
  await page.goto(pathToFileURL(path.join(out, '../position_fallback/report.html')).href);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'fallback mobile overflow');
  await page.screenshot({path: path.join(evidence, 'fallback_mobile.png')});
  await page.setViewportSize({width: 1360, height: 980});
  for (const [suffix, rate] of [['004', '2'], ['005', '5']]) {
    const relative = `results/20261101_nanyo_${suffix}/mapping_refinement/field_review_20261103_${rate}hz/viewer.html`;
    await page.goto(pathToFileURL(path.join(root, relative)).href);
    assert.equal(await page.locator('#map option').count(), 2);
    await page.selectOption('#map', '1');
    await page.locator('#sizeN').fill('3');
    await page.locator('#sizeN').dispatchEvent('input');
    assert.equal(await page.locator('#size').inputValue(), '3');
    const canvas = page.locator('canvas');
    const area = await canvas.boundingBox();
    await page.mouse.move(area.x + 200, area.y + 180);
    await page.mouse.down();
    await page.mouse.move(area.x + 300, area.y + 230, {steps: 5});
    await page.mouse.up();
    const saved = page.waitForEvent('download');
    await page.click('#save');
    const settings = await saved;
    const settingsPath = path.join(evidence, `${suffix}_view.json`);
    await settings.saveAs(settingsPath);
    await page.locator('#load').setInputFiles(settingsPath);
    const pngPromise = page.waitForEvent('download');
    await page.click('#png');
    const png = await pngPromise;
    await png.saveAs(path.join(evidence, `${suffix}_export.png`));
    await page.screenshot({path: path.join(evidence, `${suffix}_viewer.png`)});
    record.viewers.push({file: relative, sha256: sha256(path.join(root, relative)), models: 2, controls_and_settings_and_png: 'passed',
      displayed: await page.locator('#meta').innerText()});
  }
  assert.deepEqual(errors, []);
  record.passed = true;
} catch (error) {
  record.passed = false;
  record.error = String(error);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  if (server) {
    await server.close();
    record.browser_exited = server.process().exitCode !== null;
  }
  record.finished_utc = new Date().toISOString();
  fs.writeFileSync(path.join(out, 'browser_verification.json'), JSON.stringify(record, null, 2));
  fs.writeFileSync(path.join(evidence, 'process.json'), JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record, null, 2));
}
