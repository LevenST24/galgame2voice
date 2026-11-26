"""Reach the LOG repair controls and play the repaired line in Chromium."""

import base64
from functools import partial
import io
import json
import threading
import time
import urllib.parse
import wave

import pytest

from test_audio_cache_recovery_browser import AudioRecoveryHandler
from test_home_layout_browser import browser_path, make_layout_server, run_layout_browser


def _page_setup_js():
    imports = """\
import {createStage} from '/frontend/src/adv/stage.js';
import {createBacklog} from '/frontend/src/adv/backlog.js';
import {createLineAudioRecovery} from '/frontend/src/controllers/line_audio_recovery.js';
import {StreamAudioController} from '/frontend/src/audio_player.js';"""
    wait_until = """\
const wait = () => new Promise(resolve => setTimeout(resolve, 20));
const until = async (check, ms = 0) => {
  const deadline = performance.now() + ms;
  for (let i = 0; i < 200 || performance.now() < deadline; i++) {
    if (check()) return;
    await wait();
  }
  throw new Error('Condition timed out');
};"""
    click = """\
  const click = selector => {
    const el = document.querySelector(selector), r = el?.getBoundingClientRect();
    const hit = r && document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (!el || el.disabled || el.hidden || el.closest('[inert]') || !r.width || !r.height || !el.contains(hit))
      throw new Error('Unreachable control ' + selector + ' hit=' + hit?.className);
    el.click();
  };"""
    recover = """\
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前测试音色' }],
    saveState: () => { saves++; return true; },
  });"""
    backlog_options = """\
    getSession: () => session,
    getSettings: () => session.settings,
    getCharacter: () => ({ id: 7, name: '当前测试音色' }),
    getRecoveryVoice: () => ({ id: 7, name: '当前测试音色' }),
    voiceEnabled: () => true,
    regenerateLineAudio: recover,
    audio: player,
    onClose: () => log.destroy(),"""
    return imports, wait_until, click, recover, backlog_options


_IMPORTS_JS, _WAIT_UNTIL_JS, _CLICK_JS, _RECOVER_JS, _BACKLOG_OPTIONS_JS = _page_setup_js()


def _fill_page_js(source):
    return (source
            .replace("__IMPORTS__", _IMPORTS_JS)
            .replace("__WAIT_UNTIL__", _WAIT_UNTIL_JS)
            .replace("__CLICK__", _CLICK_JS)
            .replace("__RECOVER__", _RECOVER_JS)
            .replace("__BACKLOG_OPTIONS__", _BACKLOG_OPTIONS_JS))


def _send_body(handler, status, content_type, body):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _ready_line_audio(sentence):
    return {
        "status": "ready",
        "generated_at": int(time.time() * 1000),
        "voice_profile_id": 7,
        "voice_name": "当前测试音色",
        "chunks": [{
            "index": 0,
            "line": 0,
            "audio_url": "/audio/repair_browser.wav",
            "sentence": sentence,
        }],
    }


def _regen_response(body, audio, session_default, message_id):
    return json.dumps({
        "session_id": body.get("session_id", session_default),
        "message_id": message_id,
        "line_index": 0,
        "voice_profile_id": 7,
        "audio": audio,
        "persisted": True,
    }, ensure_ascii=False).encode("utf-8")


def _silent_wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x20\x03" * 800)
    return output.getvalue()


def _module_case(script):
    return (
        '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/frontend/src/styles.css">'
        '<link rel="stylesheet" href="/frontend/src/adv/adv.css">'
        '<link rel="stylesheet" href="/frontend/src/adv/app.css">'
        '<body><div id="fixture-root"></div>'
        f'<script type="module">{script}</script></body>'
    ).encode()


def _stop_layout_server(server, ready, worker, *events):
    ready.set()
    for event in events:
        event.set()
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def _launch_browser(executable, port, user_data_dir, screenshot_path, width, height, *, extra_args=(), reset, done=None):
    return run_layout_browser([
        executable, "--headless", "--no-sandbox", "--disable-gpu", *extra_args, "--mute-audio",
        "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--disable-sync",
        "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
        f"--user-data-dir={user_data_dir}", f"--window-size={max(600, width + 100)},{height + 100}",
        "--dump-dom", f"--screenshot={screenshot_path}", f"http://127.0.0.1:{port}/",
    ], reset=reset, done=done)


def _run_iframe_repair(tmp_path, width, height, mode, script_source, screenshot, *, track_requests=False):
    executable = browser_path()
    if not executable:
        pytest.skip("LOG repair needs an installed Chromium browser")
    wav_bytes = _silent_wav_bytes()
    script = script_source.replace("__MODE__", json.dumps(mode)).replace(
        "__WAV__", json.dumps(base64.b64encode(wav_bytes).decode()),
    )
    case = _module_case(script)
    page = (
        f'<body style="margin:0"><iframe src="/case" style="border:0;width:{width}px;height:{height}px"></iframe>'
        '<img src="/hold-image" hidden></body>'
    ).encode()
    ready, result = threading.Event(), {}
    shared_requests = None
    kwargs = {"page": page, "case": case, "ready": ready, "result": result}
    if track_requests:
        shared_requests = {
            "missing_audio_gets": [],
            "repair_audio_gets": [],
            "regen_posts": [],
        }
        kwargs["wav_bytes"] = wav_bytes
        kwargs["shared_requests"] = shared_requests
    server = make_layout_server(partial(LineRecoveryHandler, **kwargs))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        rendered = _launch_browser(
            executable, server.server_port, tmp_path / "browser", tmp_path / screenshot,
            width, height, done=lambda: bool(result),
            reset=lambda: (ready.clear(), result.clear(), *(log.clear() for log in (shared_requests or {}).values())),
        )
    finally:
        _stop_layout_server(server, ready, worker)
    (tmp_path / "rendered.html").write_bytes(rendered.stdout)
    (tmp_path / "browser.log").write_bytes(rendered.stderr)
    if track_requests:
        return rendered, result, shared_requests
    return rendered, result


SCRIPT = _fill_page_js("""
__IMPORTS__
const nativeFetch=globalThis.fetch.bind(globalThis),mode=__MODE__;
const result={},oldTime=Date.now()-9*86400000;
const session={id:'repair-browser',settings:{voiceProfileId:7},messages:[{id:'m_42',serverMessageId:42,role:'assistant',
  content:'原始中文',ts:oldTime,lines:[{index:0,zh:'原始中文',ja:'こんにちは。',emotion:'neutral',act:{},audio:{status:'failed',chunks:[]}}]}]};
const posts=[],gets=[];let saves=0,requestSignal,release;
const line=session.messages[0].lines[0],response={session_id:session.id,message_id:42,line_index:0,voice_profile_id:7,persisted:true,
  audio:{status:'ready',generated_at:Date.now(),voice_profile_id:7,voice_name:'当前测试音色',
    chunks:[{index:0,line:0,audio_url:'/audio/repair_browser.wav',sentence:line.ja}]}};
const wav=new Blob([Uint8Array.from(atob(__WAV__),c=>c.charCodeAt(0))],{type:'audio/wav'});
globalThis.fetch=async (url,options={})=>{
  if(url==='/api/voice/regenerate-line'){
    posts.push(JSON.parse(options.body));requestSignal=options.signal;
    if(mode==='cancel')await new Promise(resolve=>{release=resolve;});
    return Response.json(response);
  }
  if(url==='/audio/repair_browser.wav'){gets.push(url);return new Response(wav);}
  throw new Error('Unexpected request '+url);
};
__WAIT_UNTIL__
const player=new StreamAudioController({fetchTimeoutMs:3000});
let log;
try {
  const stage=createStage(document.getElementById('fixture-root'));
__RECOVER__
  log=createBacklog(stage.overlay,{
__BACKLOG_OPTIONS__
  });
  await document.fonts.ready;await wait();
__CLICK__
  result.namedVoice=document.querySelector('.adv-log-regenerate-btn').title;
  click('.adv-log-regenerate-btn');await until(()=>posts.length===1);
  if(mode==='cancel'){
    click('.adv-log-close-btn');await until(()=>requestSignal.aborted);
    release();await wait();await wait();
    result.cancelled=requestSignal.aborted;result.oldAudioKept=line.audio.status==='failed';result.noWrites=saves===0;result.noDownloads=gets.length===0;
  }else{
    await until(()=>line.audio.status==='ready');await wait();
    const replay=document.querySelector('.adv-log-replay-btn');
    result.replayEnabled=!replay.disabled;result.noAutomaticDownloads=gets.length===0;
    const row=document.querySelector('.adv-log-entry-controls'),rect=row.getBoundingClientRect();
    result.overflow=row.scrollWidth-row.clientWidth;result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
    click('.adv-log-replay-btn');await until(()=>!replay.classList.contains('is-playing'));
    result.played=gets.length===1&&player._linePlayState===null;
    result.generatedAt=line.audio.generated_at;result.historyTimestamp=session.messages[0].ts;result.originalTimestamp=oldTime;
    result.saves=saves;
  }
  result.posts=posts;
}catch(error){result.error=String(error.stack||error);}
finally{if(mode==='cancel')log?.destroy();await player.close();}
const output=document.createElement('pre');output.id='line-recovery-result';output.hidden=true;output.textContent=JSON.stringify(result);parent.document.body.append(output);
await nativeFetch('/result',{method:'POST',body:JSON.stringify(result)});
""")


class LineRecoveryHandler(AudioRecoveryHandler):
    def __init__(self, *args, case, wav_bytes=None, shared_requests=None, **kwargs):
        self.case = case
        self.wav_bytes = wav_bytes or b""
        self.shared_requests = shared_requests if shared_requests is not None else {}
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/case":
            _send_body(self, 200, "text/html; charset=utf-8", self.case)
        elif parsed.path == "/audio/missing_legacy.wav":
            if "missing_audio_gets" in self.shared_requests:
                self.shared_requests["missing_audio_gets"].append(self.path)
            self.send_error(404, "Audio file not found on disk")
        elif parsed.path == "/audio/repair_browser.wav":
            if "repair_audio_gets" in self.shared_requests:
                self.shared_requests["repair_audio_gets"].append(self.path)
            _send_body(self, 200, "audio/wav", self.wav_bytes)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/voice/regenerate-line":
            body = _read_json_body(self)
            if "regen_posts" in self.shared_requests:
                self.shared_requests["regen_posts"].append(body)
            audio = _ready_line_audio(body.get("text", "こんにちは。"))
            resp_bytes = _regen_response(body, audio, "repair-browser-legacy", body.get("message_id", 42))
            _send_body(self, 200, "application/json; charset=utf-8", resp_bytes)
        else:
            super().do_POST()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
@pytest.mark.parametrize("mode", ["success", "cancel"])
def test_log_line_repair_is_reachable_and_owned(tmp_path, width, height, mode):
    rendered, result = _run_iframe_repair(tmp_path, width, height, mode, SCRIPT, "line-repair.png")
    assert rendered.returncode == 0 and result and "error" not in result, result or rendered.stderr[-1000:]
    assert "当前测试音色" in result["namedVoice"]
    assert len(result["posts"]) == 1 and result["posts"][0]["voice_profile_id"] == 7
    assert result["posts"][0]["message_id"] == 42 and result["posts"][0]["text"] == "こんにちは。"
    if mode == "cancel":
        assert result["cancelled"] and result["oldAudioKept"] and result["noWrites"] and result["noDownloads"]
    else:
        assert result["replayEnabled"] and result["noAutomaticDownloads"] and result["played"]
        assert result["historyTimestamp"] == result["originalTimestamp"] and result["saves"] == 1
        assert result["overflow"] <= 1 and result["left"] >= 0 and result["right"] <= width, result


TWO_TAB_SCRIPT = _fill_page_js("""
__IMPORTS__
import {putCachedAudioBlob} from '/frontend/src/cache.js';

const nativeFetch = globalThis.fetch.bind(globalThis);
const tab = new URLSearchParams(location.search).get('tab') || '1';
const warmCache = __WARM_CACHE__;
const base64Wav = '__WAV_B64__';
const result = { tab: Number(tab) };
const session = {
  id: 'tab-sync-session',
  settings: { voiceProfileId: 7 },
  messages: [{
    id: 'm_42',
    serverMessageId: 42,
    role: 'assistant',
    content: '原始中文',
    ts: Date.now() - 3600000,
    lines: [{
      index: 0, zh: '原始中文', ja: 'こんにちは。', emotion: 'neutral', act: {},
      audio: { status: 'failed', chunks: [] }
    }]
  }]
};

let saves = 0;
__WAIT_UNTIL__

const player = new StreamAudioController({ fetchTimeoutMs: 5000 });
const playOutcomes = [];
const originalPlayLine = player.playLine.bind(player);
player.playLine = async (...args) => {
  const outcome = await originalPlayLine(...args);
  playOutcomes.push(outcome);
  return outcome;
};
let log;
try {
  const stage = createStage(document.getElementById('fixture-root'));
__RECOVER__
  log = createBacklog(stage.overlay, {
__BACKLOG_OPTIONS__
  });
  await document.fonts.ready;
  await wait();

__CLICK__

  const line = session.messages[0].lines[0];
  const regenBtn = document.querySelector('.adv-log-regenerate-btn');
  const replayBtn = document.querySelector('.adv-log-replay-btn');
  const statusSpan = document.querySelector('.adv-log-audio-status');

  click('.adv-log-regenerate-btn');

  // The server holds each repair until the other page has posted its own (up to 15 s), and on a busy
  // runner that page can start seconds later, so this wait must outlast the hold, not 200 short polls.
  await until(() => line.audio.status === 'ready', 20000);
  await wait();

  result.audioStatus = line.audio.status;
  result.audioUrl = line.audio.chunks[0]?.audio_url;
  result.replayEnabled = !replayBtn.disabled;
  result.regenHidden = regenBtn.hidden;
  result.statusText = statusSpan.textContent;
  result.saves = saves;

  if (warmCache && result.audioUrl) {
    const raw = atob(base64Wav);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const wavBlob = new Blob([bytes], { type: 'audio/wav' });
    await putCachedAudioBlob(result.audioUrl, wavBlob);
  }

  click('.adv-log-replay-btn');
  await until(() => playOutcomes.length === 1 && !replayBtn.classList.contains('is-playing'));
  result.playOutcome = playOutcomes[0];

  log.destroy();
  log = createBacklog(stage.overlay, {
__BACKLOG_OPTIONS__
  });
  await wait();
  const replayBtn2 = document.querySelector('.adv-log-replay-btn');
  const regenBtn2 = document.querySelector('.adv-log-regenerate-btn');
  result.reopenReplayEnabled = !replayBtn2.disabled;
  result.reopenRegenHidden = regenBtn2.hidden;
} catch (error) {
  result.error = String(error.stack || error);
} finally {
  log?.destroy();
  await player.close();
}
const output = document.createElement('pre');
output.id = 'two-tab-result';
output.hidden = true;
output.textContent = JSON.stringify(result);
(parent?.document?.body || document.body).append(output);
await nativeFetch('/result', { method: 'POST', body: JSON.stringify(result) });
if (tab === '2') {
  window.close();
}
""")


class TwoTabRecoveryHandler(AudioRecoveryHandler):
    def __init__(self, *args, case, shared_state, state_lock, barrier_event, wav_bytes, width=1280, height=900, page=b"", **kwargs):
        self.case = case
        self.shared_state = shared_state
        self.state_lock = state_lock
        self.barrier_event = barrier_event
        self.wav_bytes = wav_bytes
        self.width = width
        self.height = height
        super().__init__(*args, page=page, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            page = (f'<body style="margin:0">'
                    f'<iframe src="/case?tab=1" style="border:0;width:{self.width}px;height:{self.height}px"></iframe>'
                    '<img src="/hold-image" hidden>'
                    '<script>'
                    'window.open("/case?tab=2", "_blank");'
                    '</script>'
                    '</body>').encode("utf-8")
            _send_body(self, 200, "text/html; charset=utf-8", page)
        elif parsed.path == "/case":
            _send_body(self, 200, "text/html; charset=utf-8", self.case)
        elif parsed.path == "/api/chat/history":
            query = urllib.parse.parse_qs(parsed.query)
            session_id = query.get("session_id", [""])[0]
            with self.state_lock:
                self.shared_state["history_gets"].append(self.path)
                line_audio = self.shared_state.get("line_audio")
                payload = {
                    "session_id": session_id,
                    "count": 1,
                    "has_more": False,
                    "oldest_id": 42,
                    "messages": [{
                        "id": 42,
                        "role": "assistant",
                        "content_chinese": "原始中文",
                        "content_japanese": "こんにちは。",
                        "lines": [{
                            "index": 0,
                            "zh": "原始中文",
                            "ja": "こんにちは。",
                            "emotion": "neutral",
                            "act": {},
                            "audio": line_audio or {"status": "failed", "chunks": []},
                        }],
                    }],
                }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            _send_body(self, 200, "application/json; charset=utf-8", body)
        elif parsed.path == "/audio/repair_browser.wav":
            with self.state_lock:
                self.shared_state["audio_gets"].append(self.path)
            _send_body(self, 200, "audio/wav", self.wav_bytes)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/voice/regenerate-line":
            body = _read_json_body(self)
            with self.state_lock:
                self.shared_state["regen_posts"].append(body)
                if len(self.shared_state["regen_posts"]) >= 2:
                    self.barrier_event.set()

            # Bounded barrier: wait until both live pages have their POSTs in flight!
            if not self.barrier_event.wait(timeout=15):
                self.send_error(500, "Timed out waiting for both POSTs to be in flight")
                return

            with self.state_lock:
                if self.shared_state.get("line_audio") is None:
                    # First concurrent request to acquire lock commits first (HTTP 200)
                    audio = _ready_line_audio("こんにちは。")
                    self.shared_state["line_audio"] = audio
                    self.shared_state["post_responses"].append(200)
                    success_resp = _regen_response(body, audio, "tab-sync-session", 42)
                    _send_body(self, 200, "application/json; charset=utf-8", success_resp)
                else:
                    # Second concurrent request encounters HTTP 409 Conflict
                    self.shared_state["post_responses"].append(409)
                    conflict_resp = json.dumps({
                        "detail": "这句已有其他音频修复结果，未覆盖现有音频，请重新打开「对话记录」检查。"
                    }, ensure_ascii=False).encode("utf-8")
                    _send_body(self, 409, "application/json; charset=utf-8", conflict_resp)
        elif parsed.path == "/result":
            payload = _read_json_body(self)
            tab = payload.get("tab")
            with self.state_lock:
                if tab == 1:
                    self.shared_state["tab1_result"] = payload
                else:
                    self.shared_state["tab2_result"] = payload
                # Release hold-image only when both live pages have completed
                if self.shared_state.get("tab1_result") and self.shared_state.get("tab2_result"):
                    self.ready.set()
            self.send_response(204)
            self.end_headers()
        else:
            super().do_POST()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold_cache", "warm_cache"])
def test_two_tab_repair_sync_via_409(tmp_path, warm_cache):
    width, height = 1280, 900
    executable = browser_path()
    if not executable:
        pytest.skip("Two-tab repair sync needs an installed Chromium browser")
    wav_bytes = _silent_wav_bytes()
    b64_wav = base64.b64encode(wav_bytes).decode("ascii")

    script = (TWO_TAB_SCRIPT
              .replace("__WARM_CACHE__", "true" if warm_cache else "false")
              .replace("__WAV_B64__", b64_wav))

    case = _module_case(script)

    ready = threading.Event()
    barrier_event = threading.Event()
    state_lock = threading.Lock()
    shared_state = {
        "line_audio": None,
        "regen_posts": [],
        "post_responses": [],
        "history_gets": [],
        "audio_gets": [],
        "tab1_result": {},
        "tab2_result": {},
    }

    server = make_layout_server(partial(
        TwoTabRecoveryHandler,
        case=case,
        shared_state=shared_state,
        state_lock=state_lock,
        barrier_event=barrier_event,
        wav_bytes=wav_bytes,
        ready=ready,
        result={},
        width=width,
        height=height,
    ))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    try:
        browser_dir = tmp_path / f"browser_twotab_{'warm' if warm_cache else 'cold'}"
        ready.clear()
        browser_proc = _launch_browser(
            executable, server.server_port, browser_dir, tmp_path / "tabs.png", width, height,
            extra_args=("--disable-popup-blocking",), reset=lambda: ready.clear(),
        )
        assert browser_proc.returncode == 0, browser_proc.stderr[-1000:]

        with state_lock:
            tab1_result = dict(shared_state["tab1_result"])
            tab2_result = dict(shared_state["tab2_result"])
            regen_posts = list(shared_state["regen_posts"])
            post_responses = list(shared_state["post_responses"])
            history_gets = list(shared_state["history_gets"])
            audio_gets = list(shared_state["audio_gets"])

        # Both live pages must finish cleanly without unhandled error
        assert tab1_result and "error" not in tab1_result, tab1_result
        assert tab2_result and "error" not in tab2_result, tab2_result

        results = [tab1_result, tab2_result]
        # Invariants shared across both live pages regardless of which page commits first
        for res in results:
            assert res["audioStatus"] == "ready", res
            assert res["audioUrl"] == "/audio/repair_browser.wav", res
            assert res["replayEnabled"] is True, res
            assert res["regenHidden"] is True, res
            assert res["playOutcome"] == "ended", res
            assert res["saves"] == 1, res
            assert res["reopenReplayEnabled"] is True, res
            assert res["reopenRegenHidden"] is True, res

        # Scheduling-independent classification: exactly one page wins direct repair (200)
        # and exactly one page loses (409) and synchronizes the remote repair
        synced_results = [r for r in results if r["statusText"] == "这一句已在另一网页修好，已更新"]
        direct_results = [r for r in results if r["statusText"] != "这一句已在另一网页修好，已更新"]
        assert len(synced_results) == 1, f"Expected exactly 1 remote-synced page, got {len(synced_results)}: {results}"
        assert len(direct_results) == 1, f"Expected exactly 1 direct-repair page, got {len(direct_results)}: {results}"

        # Responses: exactly one 200 and one 409 recorded at the server
        assert sorted(post_responses) == [200, 409], post_responses

        # Exactly 2 synthesis POSTs (1 from each live page; neither replayed)
        assert len(regen_posts) == 2, f"Expected 2 posts total, got {len(regen_posts)}"
        assert len(history_gets) == 1, f"Expected 1 history GET from losing tab, got {len(history_gets)}"
        assert "before_id=43" in history_gets[0] and "session_id=tab-sync-session" in history_gets[0]

        # Audio fetch network requests:
        # In warm cache mode, pre-cached blobs serve playback so zero network audio GETs are made.
        # In cold cache mode, 1 or 2 network audio GETs are legitimate due to shared browser Cache Storage.
        if warm_cache:
            assert len(audio_gets) == 0, f"Expected 0 audio GETs for warm cache, got {len(audio_gets)}: {audio_gets}"
        else:
            assert 1 <= len(audio_gets) <= 2, f"Expected 1 or 2 audio GETs for cold cache, got {len(audio_gets)}: {audio_gets}"
            for path in audio_gets:
                assert path.split("?")[0] == "/audio/repair_browser.wav", path
    finally:
        _stop_layout_server(server, ready, worker, barrier_event)


LEGACY_SCRIPT = _fill_page_js("""
__IMPORTS__

const mode = __MODE__;
const nativeFetch = globalThis.fetch.bind(globalThis);
const isMissingFile = mode === 'missing_file';
const result = {};
const oldTime = isMissingFile ? Date.now() - 1000 : Date.now() - 9 * 86400000;
const audioUrls = isMissingFile ? ['/audio/missing_legacy.wav'] : ['/audio/cache/old.wav'];
const session = {
  id: 'repair-browser-legacy',
  settings: { voiceProfileId: 7 },
  messages: [{
    id: 'm_42',
    serverMessageId: 42,
    role: 'assistant',
    content: '原始旧版中文',
    japanese: 'こんにちは。',
    ts: oldTime,
    audioUrls,
  }],
};

const posts = [];
let saves = 0, requestSignal, release;
const stubResponse = {
  session_id: session.id,
  message_id: 42,
  line_index: 0,
  voice_profile_id: 7,
  persisted: true,
  audio: {
    status: 'ready',
    generated_at: Date.now(),
    voice_profile_id: 7,
    voice_name: '当前测试音色',
    chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_browser.wav', sentence: 'こんにちは。' }],
  },
};
const wav = new Blob([Uint8Array.from(atob(__WAV__), c => c.charCodeAt(0))], { type: 'audio/wav' });

if (!isMissingFile) {
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/voice/regenerate-line') {
      posts.push(JSON.parse(options.body));
      requestSignal = options.signal;
      if (mode === 'cancel' || mode === 'late_change') await new Promise(resolve => { release = resolve; });
      return Response.json(stubResponse);
    }
    if (url === '/audio/repair_browser.wav') {
      return new Response(wav);
    }
    throw new Error('Unexpected request ' + url);
  };
}

__WAIT_UNTIL__

const player = new StreamAudioController({ fetchTimeoutMs: 3000 });

const outcomes = [];
const origPlayLine = player.playLine.bind(player);
player.playLine = async function(...args) {
  const outcome = await origPlayLine(...args);
  outcomes.push(outcome);
  return outcome;
};

let log;
try {
  const stage = createStage(document.getElementById('fixture-root'));
__RECOVER__

  const makeBacklog = () => createBacklog(stage.overlay, {
__BACKLOG_OPTIONS__
  });

  log = makeBacklog();
  await document.fonts.ready; await wait();

__CLICK__

  result.isLegacyEntry = Boolean(document.querySelector('.adv-log-entry.is-legacy'));

  if (isMissingFile) {
    const replay = document.querySelector('.adv-log-replay-btn');
    const regen = document.querySelector('.adv-log-regenerate-btn');
    result.initialReplayEnabled = !replay.disabled;
    result.initialRegenHidden = regen.hidden;

    click('.adv-log-replay-btn');
    await until(() => outcomes.length === 1 && replay.disabled && !regen.hidden);
    result.post404ReplayDisabled = replay.disabled;
    result.post404RegenReachable = !regen.hidden && !regen.disabled;
    result.namedVoice = regen.title;

    click('.adv-log-regenerate-btn');
    await until(() => Array.isArray(session.messages[0].lines) && session.messages[0].lines[0].audio.status === 'ready');
    await wait();
    result.repairedReplayEnabled = !replay.disabled;
    result.repairedRegenHidden = regen.hidden;

    click('.adv-log-replay-btn');
    await until(() => outcomes.length === 2 && !replay.classList.contains('is-playing'));

    log.destroy();
    log = makeBacklog();
    await wait();
    const reopenedReplay = document.querySelector('.adv-log-replay-btn');
    const reopenedRegen = document.querySelector('.adv-log-regenerate-btn');
    result.reopenedPlayable = !reopenedReplay.disabled;
    result.reopenedRegenHidden = reopenedRegen.hidden;

    click('.adv-log-replay-btn');
    await until(() => outcomes.length === 3 && !reopenedReplay.classList.contains('is-playing'));

    result.outcomes = outcomes;
    result.historyTimestamp = session.messages[0].ts;
    result.originalTimestamp = oldTime;
    result.saves = saves;
    result.canonicalLine = session.messages[0].lines[0];
  } else {
    result.namedVoice = document.querySelector('.adv-log-regenerate-btn').title;
    click('.adv-log-regenerate-btn');
    await until(() => posts.length === 1);
    if (mode === 'cancel') {
      click('.adv-log-close-btn');
      await until(() => requestSignal.aborted);
      release(); await wait(); await wait();
      result.cancelled = requestSignal.aborted;
      result.linesUndefined = session.messages[0].lines === undefined;
      result.noWrites = saves === 0;
    } else if (mode === 'late_change') {
      session.settings.voiceProfileId = 99;
      release(); await wait(); await wait();
      result.linesUndefined = session.messages[0].lines === undefined;
      result.noWrites = saves === 0;
    } else {
      await until(() => Array.isArray(session.messages[0].lines) && session.messages[0].lines[0].audio.status === 'ready');
      await wait();
      const replay = document.querySelector('.adv-log-replay-btn');
      result.replayEnabled = !replay.disabled;
      click('.adv-log-replay-btn');
      await until(() => outcomes.length === 1 && !replay.classList.contains('is-playing'));
      result.outcomes = outcomes;
      result.played = outcomes[0] === 'ended';
      result.historyTimestamp = session.messages[0].ts;
      result.originalTimestamp = oldTime;
      result.saves = saves;
      result.canonicalLine = session.messages[0].lines[0];
      log.destroy();
      log = makeBacklog();
      await wait();
      const reopenedReplay = document.querySelector('.adv-log-replay-btn');
      result.reopenedPlayable = !reopenedReplay.disabled;
    }
    result.posts = posts;
  }
} catch (error) {
  result.error = String(error.stack || error);
} finally {
  if (mode === 'cancel' || mode === 'late_change') log?.destroy();
  await player.close();
}
const output = document.createElement('pre');
output.id = 'line-recovery-result';
output.hidden = true;
output.textContent = JSON.stringify(result);
parent.document.body.append(output);
await nativeFetch('/result', { method: 'POST', body: JSON.stringify(result) });
""")


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
@pytest.mark.parametrize("mode", ["success", "cancel", "late_change", "missing_file"])
def test_legacy_log_reply_repair_in_chromium(tmp_path, width, height, mode):
    rendered, result, shared_requests = _run_iframe_repair(
        tmp_path, width, height, mode, LEGACY_SCRIPT, "legacy-line-repair.png", track_requests=True,
    )
    assert rendered.returncode == 0 and result and "error" not in result, result or rendered.stderr[-1000:]
    assert result["isLegacyEntry"] is True
    assert "当前测试音色" in result["namedVoice"]
    if mode == "cancel":
        assert result["cancelled"] and result["linesUndefined"] and result["noWrites"]
        assert len(result["posts"]) == 1 and result["posts"][0]["voice_profile_id"] == 7
    elif mode == "late_change":
        assert result["linesUndefined"] and result["noWrites"]
        assert len(result["posts"]) == 1 and result["posts"][0]["voice_profile_id"] == 7
    elif mode == "missing_file":
        assert result["initialReplayEnabled"] is True
        assert result["initialRegenHidden"] is True
        assert result["post404ReplayDisabled"] is True
        assert result["post404RegenReachable"] is True
        assert result["repairedReplayEnabled"] is True
        assert result["repairedRegenHidden"] is True
        assert result["reopenedPlayable"] is True
        assert result["reopenedRegenHidden"] is True
        assert result["outcomes"] == ["failed", "ended", "ended"]
        assert shared_requests["missing_audio_gets"] == ["/audio/missing_legacy.wav"]
        assert len(shared_requests["regen_posts"]) == 1
        assert shared_requests["regen_posts"][0]["line_index"] == 0
        assert shared_requests["regen_posts"][0]["message_id"] == 42
        assert shared_requests["regen_posts"][0]["text"] == "こんにちは。"
        assert len(shared_requests["repair_audio_gets"]) >= 1
        assert result["historyTimestamp"] == result["originalTimestamp"] and result["saves"] == 1
        canonical = result["canonicalLine"]
        assert canonical["index"] == 0 and canonical["ja"] == "こんにちは。" and canonical["zh"] == "原始旧版中文"
        assert canonical["audio"]["status"] == "ready"
    else:
        assert result["replayEnabled"] and result["played"]
        assert result["outcomes"] == ["ended"]
        assert result["historyTimestamp"] == result["originalTimestamp"] and result["saves"] == 1
        assert result["reopenedPlayable"] is True
        canonical = result["canonicalLine"]
        assert canonical["index"] == 0 and canonical["ja"] == "こんにちは。" and canonical["zh"] == "原始旧版中文"
        assert canonical["audio"]["status"] == "ready"
