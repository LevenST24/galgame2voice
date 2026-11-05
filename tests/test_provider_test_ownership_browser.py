"""Real Chromium browser verification of settings provider test lifecycle and ownership."""

import pytest

from test_engine_status_browser import render_engine_case

SCRIPT = """
const testHoldResolvers = [];
const testCalls = [];
const holdTestPromise = (tag) => new Promise(resolve => {
  testHoldResolvers.push({ tag, resolve });
});

globalThis.fetch = async (url, options = {}) => {
  const path = new URL(typeof url === 'string' ? url : url.url, location.href).pathname;
  let result = { status: 'ok' };
  if (path === '/api/chat/sessions') {
    result = { sessions: [] };
  } else if (path === '/api/voice/profiles') {
    result = { profiles: [{ id: 1, name: '测试角色' }], active_profile_id: 1 };
  } else if (path === '/api/providers') {
    result = {
      providers: [
        { id: 'openai', name: 'OpenAI', chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1', is_active: true },
        { id: 'custom', name: '自定义模型', chat_model: 'deepseek-chat', api_base_url: 'https://api.deepseek.com/v1', is_active: false },
      ],
      presets: [],
    };
  } else if (path === '/api/providers/test') {
    testCalls.push({ url, options: { ...options } });
    const signal = options.signal;
    const responsePromise = holdTestPromise('test-' + testCalls.length);
    if (signal) {
      if (signal.aborted) {
        throw new DOMException('操作已取消', 'AbortError');
      }
      signal.addEventListener('abort', () => {}, { once: true });
    }
    return await responsePromise;
  } else if (path === '/api/config') {
    result = { status: 'ok', settings: { gpt_sovits_url: 'http://127.0.0.1:19872', inference_precision: 'cpu' } };
  } else if (path === '/api/system/engine-status') {
    result = { state: 'ready', ready: true, message: '语音引擎已就绪。' };
  } else if (path === '/api/system/status') {
    result = { gpt_sovits: { status: 'reachable', base_url: 'http://127.0.0.1:19872' } };
  } else if (path.includes('portrait')) {
    result = { enabled: false };
  } else if (path.includes('background')) {
    result = { backgrounds: [] };
  } else if (path === '/api/auth/status') {
    result = { auth_required: false };
  }
  return Response.json(result);
};

await import('/frontend/src/main.js');

const wait = (ms = 120) => new Promise(resolve => setTimeout(resolve, ms));
const settle = () => document.getAnimations().forEach(a => { if (a instanceof CSSTransition) a.finish(); });

const click = selector => {
  settle();
  const el = document.querySelector(selector);
  if (!el) throw new Error('Missing ' + selector);
  el.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (el.disabled || el.closest('[inert]') || r.width <= 0 || r.height <= 0 || !el.contains(hit)) {
    throw new Error('Control is not reachable: ' + selector);
  }
  el.click();
};

const state = {
  left: 0,
  right: innerWidth,
  viewport: innerWidth,
  overflow: 0,
};

try {
  await wait(200);

  // 1. Open settings dialog via real DOM gear button on Home screen
  click('.home-gear');
  await wait();

  // Navigate to models tab
  const modelTab = document.querySelector('[data-gtab="models"]');
  if (!modelTab) throw new Error('Missing models tab button');
  modelTab.click();
  await wait();

  const providerSelect = document.getElementById('gProvider');
  const testBtn = document.getElementById('gProviderTest');
  const testResultBox = document.getElementById('gProviderTestResult');
  const testResultBody = document.getElementById('gTestResultBody');
  const chatModelInput = document.getElementById('gChatModel');
  const toastRoot = document.getElementById('toastRoot');

  if (!providerSelect || !testBtn || !testResultBox || !testResultBody || !chatModelInput) {
    throw new Error('Missing required settings DOM elements');
  }

  // --- SCENARIO 1: Real UI switch provider while test is in flight ---
  // Initiate test on initial provider (openai)
  testBtn.click();
  await wait(60);

  state.test1StartedBusy = (testBtn.disabled === true);
  state.test1Indicator = testBtn.textContent.includes('测试中');

  // Switch dropdown to 'custom' provider while OpenAI test is in flight
  providerSelect.value = 'custom';
  providerSelect.dispatchEvent(new Event('change', { bubbles: true }));
  await wait(60);

  state.test1SwitchRestoredBtn = (testBtn.disabled === false);
  state.test1SwitchBoxHidden = testResultBox.classList.contains('hidden');

  // Release stale OpenAI test with an error response
  const holder1 = testHoldResolvers.shift();
  if (!holder1) throw new Error('Missing test 1 promise resolver');
  holder1.resolve(Response.json({ success: false, error: 'OpenAI 401: Invalid API Key' }));
  await wait();
  await wait();

  // Assert: Custom provider view is NOT polluted by stale OpenAI failure
  state.staleResultBoxHidden = testResultBox.classList.contains('hidden');
  state.staleResultBodyEmpty = (testResultBody.textContent.trim() === '');
  const toasts1 = [...toastRoot.querySelectorAll('.toast')].map(t => t.textContent);
  state.noToastForStaleTest = (toasts1.length === 0);
  state.testBtnUsable = (testBtn.disabled === false);

  // --- SCENARIO 2: Edit parameter draft & stale finally cannot unlock newer test ---
  // Start Test 2 on custom provider
  testBtn.click();
  await wait(60);
  state.test2StartedBusy = (testBtn.disabled === true);

  // User edits chat model draft
  chatModelInput.value = 'deepseek-coder';
  chatModelInput.dispatchEvent(new Event('input', { bubbles: true }));
  await wait(60);

  state.draftEditRestoredBtn = (testBtn.disabled === false);

  // User starts Test 3 immediately on edited model
  testBtn.click();
  await wait(60);
  state.test3StartedBusy = (testBtn.disabled === true);
  state.test3Indicator = testBtn.textContent.includes('测试中');

  // Now stale Test 2 finishes first with an error
  const holder2 = testHoldResolvers.shift();
  if (!holder2) throw new Error('Missing test 2 promise resolver');
  holder2.resolve(Response.json({ success: false, error: 'deepseek-chat connection failed' }));
  await wait();
  await wait();

  // Stale Test 2's finally MUST NOT clear Test 3's busy state!
  state.test2FinallyDidNotUnlockTest3 = (testBtn.disabled === true);
  state.test3StillShowsTesting = testBtn.textContent.includes('测试中');
  state.noToastFromStaleTest2 = (toastRoot.querySelectorAll('.toast').length === 0);

  // Test 3 finishes with success
  const holder3 = testHoldResolvers.shift();
  if (!holder3) throw new Error('Missing test 3 promise resolver');
  holder3.resolve(Response.json({ success: true, latency_ms: 45, models: ['deepseek-coder'] }));
  await wait();
  await wait();

  // Now Test 3 finishes and properly displays result and toast
  state.test3CompletedBtnEnabled = (testBtn.disabled === false);
  state.test3ResultBoxVisible = !testResultBox.classList.contains('hidden');
  state.test3ResultSuccess = testResultBox.classList.contains('test-result-success');
  state.test3BodyContainsModel = testResultBody.textContent.includes('deepseek-coder');
  const toastsAfterTest3 = [...toastRoot.querySelectorAll('.toast')].map(t => t.textContent);
  state.toastAfterTest3Count = toastsAfterTest3.length;
  state.toastAfterTest3Success = toastsAfterTest3.some(t => t.includes('连通性测试成功'));

  // --- SCENARIO 3: Close modal while test is in flight ---
  toastRoot.innerHTML = '';
  testBtn.click();
  await wait(60);

  // Close modal via close button
  const closeBtn = document.querySelector('#globalModal [data-close]');
  if (!closeBtn) throw new Error('Missing modal close button');
  closeBtn.click();
  await wait();

  // Resolve Test 4 while modal is closed
  const holder4 = testHoldResolvers.shift();
  if (!holder4) throw new Error('Missing test 4 promise resolver');
  holder4.resolve(Response.json({ success: true, latency_ms: 20, models: ['deepseek-coder'] }));
  await wait();
  await wait();

  // No toast leaked to stage outside modal
  state.modalClosedNoToast = (toastRoot.querySelectorAll('.toast').length === 0);

  // Reopen modal: verify clean state
  document.querySelector('.home-gear').click();
  await wait();
  document.querySelector('[data-gtab="models"]').click();
  await wait();

  state.reopenResultBoxClean = testResultBox.classList.contains('hidden');

  // --- SCENARIO 4: Repeated initialization disposal & single user click via reachable control ---
  const { initGlobalSettings } = await import('/frontend/src/controllers/global_settings.js');
  const currentDom = {
    globalModal: document.getElementById('globalModal'),
    gTabs: document.getElementById('gTabs'),
    gProvider: providerSelect,
    gProviderTest: testBtn,
    gProviderTestResult: testResultBox,
    gTestResultTitle: document.getElementById('gTestResultTitle'),
    gTestResultLatency: document.getElementById('gTestResultLatency'),
    gTestResultBody: testResultBody,
    gChatModel: chatModelInput,
    gChatModelSelect: document.getElementById('gChatModelSelect'),
    gApiKey: document.getElementById('gApiKey'),
    gBaseUrl: document.getElementById('gBaseUrl'),
    gReasoningEffort: document.getElementById('gReasoningEffort'),
    gCustomName: document.getElementById('gCustomName'),
    gProviderActivate: document.getElementById('gProviderActivate'),
  };
  initGlobalSettings(currentDom);
  initGlobalSettings(currentDom);

  const callsBeforeReinitClick = testCalls.length;
  click('#gProviderTest');
  await wait(60);
  const callsAfterReinitClick = testCalls.length;
  state.singleRequestAfterReinitClick = (callsAfterReinitClick - callsBeforeReinitClick === 1);
  state.reinitClickCallsCount = (callsAfterReinitClick - callsBeforeReinitClick);
  state.test5StartedBusy = (testBtn.disabled === true);

  // Release and drain the held Test 5 response
  const holder5 = testHoldResolvers.shift();
  if (!holder5) throw new Error('Missing test 5 promise resolver');
  holder5.resolve(Response.json({ success: true, latency_ms: 25, models: ['deepseek-coder'] }));
  await wait();
  await wait();

  state.test5CompletedBtnEnabled = (testBtn.disabled === false);
  state.test5ResultBoxVisible = !testResultBox.classList.contains('hidden');
  state.test5ResultSuccess = testResultBox.classList.contains('test-result-success');

  state.ok = Boolean(
    state.test1StartedBusy &&
    state.test1Indicator &&
    state.test1SwitchRestoredBtn &&
    state.test1SwitchBoxHidden &&
    state.staleResultBoxHidden &&
    state.staleResultBodyEmpty &&
    state.noToastForStaleTest &&
    state.testBtnUsable &&
    state.test2StartedBusy &&
    state.draftEditRestoredBtn &&
    state.test3StartedBusy &&
    state.test3Indicator &&
    state.test2FinallyDidNotUnlockTest3 &&
    state.test3StillShowsTesting &&
    state.noToastFromStaleTest2 &&
    state.test3CompletedBtnEnabled &&
    state.test3ResultBoxVisible &&
    state.test3ResultSuccess &&
    state.test3BodyContainsModel &&
    state.toastAfterTest3Count === 1 &&
    state.toastAfterTest3Success &&
    state.modalClosedNoToast &&
    state.reopenResultBoxClean &&
    state.singleRequestAfterReinitClick &&
    state.test5StartedBusy &&
    state.test5CompletedBtnEnabled &&
    state.test5ResultSuccess
  );
} catch (error) {
  state.error = String(error.stack || error);
}

const output = document.createElement('pre');
output.id = 'engine-result';
output.hidden = true;
output.textContent = JSON.stringify(state);
parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_provider_test_ownership_and_concurrency_in_browser(tmp_path, width, height):
    data = render_engine_case(tmp_path, width, height, SCRIPT, 12000)
    assert "error" not in data, data.get("error")
    failed_fields = {k: v for k, v in data.items() if v is False}
    assert not failed_fields, f"Browser test subfields failed: {failed_fields}"
    assert data.get("ok") is True, data
    assert data.get("test1SwitchRestoredBtn") is True
    assert data.get("staleResultBoxHidden") is True
    assert data.get("noToastForStaleTest") is True
    assert data.get("test2FinallyDidNotUnlockTest3") is True
    assert data.get("test3ResultSuccess") is True
    assert data.get("modalClosedNoToast") is True
    assert data.get("reopenResultBoxClean") is True
    assert data.get("singleRequestAfterReinitClick") is True, (
        f"Expected 1 call after click, got {data.get('reinitClickCallsCount')}"
    )
    assert data.get("test5CompletedBtnEnabled") is True
    assert data.get("test5ResultSuccess") is True
