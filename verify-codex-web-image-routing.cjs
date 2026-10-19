/** 核对下载的桥接源码：图片请求走 Codex 后端，测试不联网、不读取凭据。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');

async function main() {
  const sourceRoot = path.join(__dirname, 'downloads/imagegen-research/codex-chatgpt-web/src');
  const nativeSource = fs.readFileSync(path.join(sourceRoot, 'native-passthrough.ts'), 'utf8');
  // 只执行已审阅的转发模块；依赖导入移除，上游请求由本地模拟函数提供。
  const compiled = stripTypeScriptTypes(nativeSource.replace(/^import[\s\S]*?from\s+['"][^'"]+['"];\s*/gm, ''), { mode: 'strip' })
    .replace(/^export\s+/gm, '') + '\nthis.forwardNativeCodexRequest = forwardNativeCodexRequest;';
  const sandbox = vm.createContext({ URL, Request, Response, Headers, ReadableStream, TextDecoder, console });
  vm.runInContext(compiled, sandbox, { timeout: 1000 });
  const forward = sandbox.forwardNativeCodexRequest;

  for (const endpoint of ['images/generations', 'images/edits']) {
    const payload = JSON.stringify({ prompt: 'offline route check' });
    const req = new Request(`http://127.0.0.1:1/v1/${endpoint}`, {
      method: 'POST', headers: { authorization: 'Bearer offline-test-placeholder', 'content-type': 'application/json' }, body: payload,
    });
    let calls = 0;
    const response = await forward(req, endpoint, async upstream => {
      calls++;
      assert.equal(upstream.url, `https://chatgpt.com/backend-api/codex/${endpoint}`);
      assert.equal(upstream.headers.get('authorization'), 'Bearer offline-test-placeholder');
      assert.equal(upstream.redirect, 'manual');
      assert.equal(await upstream.text(), payload);
      return new Response(JSON.stringify({ error: 'mock upstream denied' }), { status: 403 });
    });
    assert.equal(calls, 1);
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), { error: 'mock upstream denied' });
    console.log(`${endpoint}：Codex 上游路由、授权转发、禁止重定向重发及上游错误保留通过`);
  }
  // 缺少 Codex 授权时直接拒绝，不能从网页登录状态自动补充凭据。
  let unauthorizedCalls = 0;
  await assert.rejects(forward(new Request('http://127.0.0.1:1/v1/images/generations', { method: 'POST', body: '{}' }),
    'images/generations', async () => { unauthorizedCalls++; throw new Error('不应到达上游'); }), /incoming Bearer authorization/);
  assert.equal(unauthorizedCalls, 0);
  const serverSource = fs.readFileSync(path.join(sourceRoot, 'server.ts'), 'utf8');
  assert.match(serverSource, /Native image requests require incoming Codex Bearer authorization/);
  assert.match(serverSource, /nativeImagesRequest\(new Request\(req, \{ signal \}\), endpoint, dependencies.fetchUpstream\)/);
  console.log('无 Codex 授权拒绝及服务器图片路由接线通过；未发出网络请求、未调用生图。');
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
