// Playwright CLI run-code --filename; use a loopback-only synthetic key server.
async (page) => {
  const check = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  let posts = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.url().endsWith('/api/connect')) posts++; });
  const ready = () => page.waitForFunction(() => document.getElementById('key').value.startsWith('wayon_') && !document.getElementById('copy').disabled);
  await page.goto('http://127.0.0.1:1108');
  await ready();
  const token = await page.locator('#key').inputValue();
  check(token.includes('demo_only'), 'Never exercise a real vehicle credential');
  check(posts === 1, 'Single automatic activation');
  check(await page.locator('button').count() === 2, 'Copy and reissue buttons only');
  check(await page.locator('input[type=checkbox],select,form').count() === 0, 'No setup controls');
  check(await page.locator('#status').innerText() === '', 'No extra ready-state prose');
  await page.reload();
  await ready();
  check(await page.locator('#key').inputValue() === token, 'Reload keeps existing key');
  await page.evaluate(() => {
    // Exercise copy feedback without replacing the user's system clipboard.
    Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:async value => {window.__copied = value;}}});
  });
  await page.locator('#copy').click();
  check(await page.evaluate(() => window.__copied) === token, 'Clipboard receives full key');
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {configurable:true,value:undefined});
    document.execCommand = command => {window.__fallback = command;return true;};
  });
  await page.locator('#copy').click();
  check(await page.evaluate(() => window.__fallback) === 'copy', 'Plain HTTP selection/copy fallback');
  let layouts = 0;
  for (const colorScheme of ['light','dark']) for (const width of [320,390,768,1100]) for (const scale of [1,2]) {
    await page.emulateMedia({colorScheme});
    await page.setViewportSize({width,height:844});
    await page.evaluate(scale => {document.documentElement.style.fontSize = (17*scale)+'px';window.dispatchEvent(new Event('resize'));},scale);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Page reflow');
    check(await page.locator('#key').evaluate(el => el.scrollHeight <= el.clientHeight+2), 'Full key visible at large text');
    check(await page.locator('#copy').evaluate(el => el.getBoundingClientRect().height >= 48), 'Touch target');
    layouts++;
  }
  await page.route('**/api/key', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ready:true,enabled:false,key:token})}));
  const beforeStop = posts;
  await page.waitForFunction(() => !document.getElementById('key').value && document.getElementById('copy').disabled, undefined, {timeout:8000});
  check(posts === beforeStop, 'Stopped connection is not automatically re-enabled by polling');
  await page.unroute('**/api/key');
  await page.route('**/api/connect', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'연결 실패 테스트. 새로고침해 주세요.'})}));
  await page.reload();
  await page.waitForFunction(() => document.getElementById('status').textContent.includes('연결 실패'));
  check(await page.locator('#copy').isDisabled(), 'Failed enrollment cannot copy stale key');
  check(await page.locator('#key').inputValue() === '', 'Failure does not invent a key');
  await page.unroute('**/api/connect');
  await page.setViewportSize({width:390,height:844});
  await page.emulateMedia({colorScheme:'light'});
  await page.reload();
  await ready();
  await page.screenshot({path:'output/playwright/wip-key-simple.png'});
  check(errors.length === 0, 'No runtime errors: '+errors.join(', '));
  return 'PASS automatic connection, stable key, both copy paths, stop/failure recovery and '+layouts+' layouts';
}
