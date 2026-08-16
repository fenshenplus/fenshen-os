// 分身 v6.3 移动端通讯器校验（评测 P2-4 沉淀）：H5 原型要素 + 零报错
// 用法: NODE_PATH=<playwright> node tests/verify_mobile.cjs [原型html路径]
// 默认: ~/WorkBuddy/2026-06-06-18-41-29/分身-v6-移动端原型.html
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const proto = process.argv[2] ||
    '/Users/a13401098230/WorkBuddy/2026-06-06-18-41-29/' + encodeURIComponent('分身-v6-移动端原型.html');
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const perr = [];
  page.on('pageerror', e => perr.push(e.message));
  await page.goto(proto, { waitUntil: 'load' });
  await page.waitForTimeout(1500);

  const out = await page.evaluate(() => {
    const r = {};
    r.tabbar = !!document.querySelector('.tabbar');
    r.tabCount = document.querySelectorAll('.tabbar .tab').length;
    r.tabs = [...document.querySelectorAll('.tabbar .tab')].map(t => (t.textContent || '').trim()).filter(Boolean).slice(0, 3);
    r.drwTabs = [...document.querySelectorAll('.drw-tabs .dtab')].map(t => (t.textContent || '').trim()).filter(Boolean);
    r.hasVoice = /语音/.test(document.body.innerText);
    r.hasLongPress = /长按/.test(document.body.innerText);
    r.hasRadar = /雷达/.test(document.body.innerText);
    r.hasBoard = /看板/.test(document.body.innerText);
    return r;
  });
  const ok = out.tabbar && out.tabCount >= 3 && out.hasVoice && out.hasLongPress && out.hasRadar && out.hasBoard && perr.length === 0;
  console.log(JSON.stringify({ ...out, pageErrors: perr, PASS: ok }, null, 2));
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
