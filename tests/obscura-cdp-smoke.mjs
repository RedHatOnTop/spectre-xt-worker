// End-to-end check of the obscura CDP endpoint (RUNBOOK 7.12): connect over
// the browser WebSocket, open a tab, navigate, read document.title back.
// Node >= 22 (global WebSocket), no dependencies. Copy to the box and run:
//
//   node obscura-cdp-smoke.mjs [ws://127.0.0.1:9222/devtools/browser]
//
// Exit 0 only when the title comes back as expected.

const endpoint = process.argv[2] ?? 'ws://127.0.0.1:9222/devtools/browser';
const ws = new WebSocket(endpoint);
let id = 0;
const pending = new Map();

function send(method, params = {}, sessionId) {
  return new Promise((res, rej) => {
    const i = ++id;
    pending.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}

ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (!m.id || !pending.has(m.id)) return;
  const p = pending.get(m.id);
  pending.delete(m.id);
  if (m.error) p.rej(new Error(JSON.stringify(m.error)));
  else p.res(m.result);
};

ws.onerror = () => {
  console.error(`FAIL: cannot reach ${endpoint} — is obscura-cdp.service active?`);
  process.exit(1);
};

ws.onopen = async () => {
  try {
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Page.navigate', { url: 'https://example.com' }, sessionId);
    await new Promise((r) => setTimeout(r, 1500));
    const out = await send(
      'Runtime.evaluate',
      { expression: 'document.title', returnByValue: true },
      sessionId
    );
    await send('Target.closeTarget', { targetId });
    const title = out.result?.value;
    if (title !== 'Example Domain') throw new Error(`unexpected title: ${JSON.stringify(title)}`);
    console.log(`PASS  CDP navigate + evaluate -> "${title}"`);
    process.exit(0);
  } catch (e) {
    console.error(`FAIL: ${e.message}`);
    process.exit(1);
  }
};
