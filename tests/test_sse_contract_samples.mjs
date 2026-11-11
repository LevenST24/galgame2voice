import assert from 'node:assert/strict';
import fs from 'node:fs';
import { test } from 'node:test';
import { streamChat } from '../frontend/src/ai.js';
import { createConversation } from '../frontend/src/conversation.js';
import { createLineAudioRecovery } from '../frontend/src/controllers/line_audio_recovery.js';
import { resolveTurnReplyIdentity } from '../frontend/src/controllers/turn_reply_identity.js';
import { mapBackendMessage } from '../frontend/src/main.js';
import { normalizeLines } from '../frontend/src/session_data.js';

const contractPath = new URL('./contracts/sse_turn_reply_v1.json', import.meta.url);
const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));

function formatSseFrame(sample) {
  return `event: ${sample.event}\ndata: ${JSON.stringify(sample.data)}\n\n`;
}

function createSseFetch(sseSamples) {
  const encoder = new TextEncoder();
  return async () => {
    const stream = new ReadableStream({
      start(controller) {
        for (const sample of sseSamples) {
          controller.enqueue(encoder.encode(formatSseFrame(sample)));
        }
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };
}

function setupConversationHarness(sseSamples, sessionId = 'sess_contract_1') {
  const session = {
    id: sessionId,
    title: '契约测试会话',
    settings: { temperature: 0.7, voiceProfileId: 7 },
    messages: [],
  };
  const state = { activeId: sessionId, global: { voiceMode: false, ttsPreset: 'balanced' } };
  const fetch = createSseFetch(sseSamples);
  let seq = 0;
  const cachedAudio = [];
  const events = [];

  let donePromiseResolve;
  const settledPromise = new Promise((resolve) => {
    donePromiseResolve = resolve;
  });

  const conversation = createConversation({
    state,
    getActive: () => session,
    getSession: (id) => (id === session.id ? session : null),
    uid: (prefix) => `${prefix}_${++seq}`,
    saveState() {},
    addMessage(role, content) {
      const msg = { id: `u_${++seq}`, role, content, ts: Date.now() };
      session.messages.push(msg);
      return msg;
    },
    persistSession: async () => {},
    isSessionDeleting: () => false,
    audioStore: new Map(),
    putCachedAudioBlob() {},
    fetchAndCacheAudio: async (url) => {
      cachedAudio.push(url);
    },
    audioPlayer: {
      isPlaying: false,
      interrupt() {},
    },
    getCurrentVoice: () => null,
    stopCurrentVoice() {},
    onBlocked() {},
    fetch,
  });

  for (const type of ['turnstart', 'turn_started', 'text', 'line', 'line_audio', 'audio_chunk', 'done', 'error', 'cancelled']) {
    conversation.on(type, (payload) => {
      events.push({ type, payload });
      if (type === 'done' || type === 'error' || type === 'cancelled') {
        donePromiseResolve({ type, payload });
      }
    });
  }

  return {
    session,
    conversation,
    cachedAudio,
    events,
    settledPromise,
  };
}

test('contract schema_version is pinned to sse_turn_reply_v1', () => {
  assert.equal(contract.schema_version, 'sse_turn_reply_v1');
  assert.ok(contract.sse_events && typeof contract.sse_events === 'object');
  assert.ok(contract.turn_reply_responses && typeof contract.turn_reply_responses === 'object');
  assert.ok(contract.edge_cases && typeof contract.edge_cases === 'object');
});

test('SSE events contract: streamChat and createConversation consume complete 3-line turn', async () => {
  const ev = contract.sse_events;
  const sequence = [
    ev.turn_started,
    ev.line_0,
    ev.audio_chunk_0,
    ev.line_audio_ready,
    ev.line_1,
    ev.line_audio_skipped,
    ev.line_2,
    ev.line_audio_failed,
    ev.done_complete,
  ];

  const harness = setupConversationHarness(sequence, 'sess_contract_1');
  assert.equal(harness.conversation.send('早安'), true);

  const terminal = await harness.settledPromise;
  assert.equal(terminal.type, 'done');
  assert.equal(harness.conversation.busy, false);

  const turnStartedEvents = harness.events.filter((e) => e.type === 'turn_started');
  assert.equal(turnStartedEvents.length, 1);
  assert.deepEqual(turnStartedEvents[0].payload, ev.turn_started.data);

  const lineEvents = harness.events.filter((e) => e.type === 'line');
  assert.deepEqual(lineEvents.map((e) => e.payload.line.index), [0, 1, 2]);

  const lineAudioEvents = harness.events.filter((e) => e.type === 'line_audio');
  assert.deepEqual(
    lineAudioEvents.map((e) => ({ line: e.payload.line, status: e.payload.status })),
    [
      { line: 0, status: 'ready' },
      { line: 1, status: 'skipped' },
      { line: 2, status: 'failed' },
    ]
  );

  const assistantMsg = harness.session.messages.find((m) => m.role === 'assistant');
  assert.ok(assistantMsg, 'assistant message must be persisted in session');
  assert.equal(assistantMsg.serverUserMessageId, 101);
  assert.equal(assistantMsg.serverMessageId, 102);
  assert.equal(assistantMsg.turnConflict, undefined);
  assert.equal(assistantMsg.content, ev.done_complete.data.chinese);
  assert.equal(assistantMsg.japanese, ev.done_complete.data.japanese);
  assert.equal(assistantMsg.lines.length, 3);
  assert.deepEqual(
    assistantMsg.lines.map((l) => l.audio.status),
    ['ready', 'skipped', 'failed']
  );
  assert.deepEqual(assistantMsg.lines, normalizeLines(ev.done_complete.data.lines));
  assert.deepEqual(harness.cachedAudio, ['/audio/cache/contract_0.wav']);
});

test('SSE events contract: streamChat and createConversation consume truncated turn', async () => {
  const ev = contract.sse_events;
  const sequence = [
    ev.turn_started,
    ev.line_0,
    ev.audio_chunk_0,
    ev.line_audio_ready,
    ev.line_2,
    ev.done_truncated,
  ];

  const harness = setupConversationHarness(sequence, 'sess_contract_1');
  assert.equal(harness.conversation.send('早安'), true);

  const terminal = await harness.settledPromise;
  assert.equal(terminal.type, 'done');
  assert.equal(terminal.payload.meta.truncated, true);
  assert.equal(terminal.payload.meta.cancelled, false);

  const assistantMsg = harness.session.messages.find((m) => m.role === 'assistant');
  assert.ok(assistantMsg);
  assert.equal(assistantMsg.serverUserMessageId, 101);
  assert.equal(assistantMsg.serverMessageId, 103);
  assert.equal(assistantMsg.lines.length, 2);
  assert.deepEqual(
    assistantMsg.lines.map((l) => ({ index: l.index, status: l.audio.status })),
    [
      { index: 0, status: 'ready' },
      { index: 2, status: 'failed' },
    ]
  );
});

test('turn-reply contract: resolveTurnReplyIdentity and createLineAudioRecovery handle all 5 statuses', async () => {
  const responses = contract.turn_reply_responses;

  // 1. ready: resolves immediately and pairs with createLineAudioRecovery
  let readyAttempts = 0;
  const readyResult = await resolveTurnReplyIdentity({
    sessionId: 'sess_contract_1',
    userMessageId: 101,
    request: async () => {
      readyAttempts++;
      return structuredClone(responses.ready);
    },
  });
  assert.equal(readyAttempts, 1);
  assert.equal(readyResult.status, 'ready');
  assert.equal(readyResult.messageId, 102);

  // Full interrupted turn recovery via createLineAudioRecovery consuming responses.ready
  const session = {
    id: 'sess_contract_1',
    settings: { voiceProfileId: 7 },
    messages: [
      {
        id: 'm_interrupted_1',
        role: 'assistant',
        content: '早安。（轻轻挥手）听得到吗？',
        japanese: 'おはよう。聞こえる？',
        serverUserMessageId: 101,
        lines: structuredClone(contract.sse_events.done_complete.data.lines),
      },
    ],
  };
  const calls = [];
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '测试角色' }],
    saveState: () => true,
    request: async (url, options = {}) => {
      calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
      if ((options.method || 'GET') === 'GET') {
        return structuredClone(responses.ready);
      }
      return {
        session_id: 'sess_contract_1',
        message_id: 102,
        line_index: 2,
        voice_profile_id: 7,
        persisted: true,
        audio: {
          status: 'ready',
          generated_at: 1711972800000,
          voice_profile_id: 7,
          voice_name: '测试角色',
          chunks: [{ index: 0, line: 2, sentence: '聞こえる？', audio_url: '/audio/repair_contract_2.wav' }],
        },
      };
    },
  });
  const recovered = await recover({ messageId: 'm_interrupted_1', lineIndex: 2 });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].method, 'GET');
  assert.equal(calls[1].method, 'POST');
  assert.equal(calls[1].body.message_id, 102);
  assert.equal(session.messages[0].serverMessageId, 102);
  assert.equal(recovered.persisted, true);
  assert.equal(session.messages[0].lines[2].audio.status, 'ready');

  // 2. pending: retries up to maxAttempts, or succeeds when transitioning to ready
  let pendingAttempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'sess_contract_1',
      userMessageId: 101,
      maxAttempts: 3,
      delayMs: 5,
      request: async () => {
        pendingAttempts++;
        return structuredClone(responses.pending);
      },
    }),
    /中断回复尚未就绪/
  );
  assert.equal(pendingAttempts, 3);

  // 3. unavailable: retries up to maxAttempts, or succeeds when transitioning to ready
  let unavailableAttempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'sess_contract_1',
      userMessageId: 101,
      maxAttempts: 3,
      delayMs: 5,
      request: async () => {
        unavailableAttempts++;
        return structuredClone(responses.unavailable);
      },
    }),
    /服务暂不可读/
  );
  assert.equal(unavailableAttempts, 3);

  // 4. gone: gives up immediately on first attempt without retry
  let goneAttempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'sess_contract_1',
      userMessageId: 999,
      maxAttempts: 3,
      delayMs: 5,
      request: async () => {
        goneAttempts++;
        return structuredClone(responses.gone);
      },
    }),
    /已被删除或清空/
  );
  assert.equal(goneAttempts, 1);

  // 5. ambiguous: gives up immediately on first attempt without retry
  let ambiguousAttempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'sess_contract_1',
      userMessageId: 101,
      maxAttempts: 3,
      delayMs: 5,
      request: async () => {
        ambiguousAttempts++;
        return structuredClone(responses.ambiguous);
      },
    }),
    /多条匹配/
  );
  assert.equal(ambiguousAttempts, 1);

  // 6. persisted_assistant_message maps cleanly via mapBackendMessage
  const mapped = mapBackendMessage(contract.persisted_assistant_message);
  assert.equal(mapped.serverMessageId, 102);
  assert.equal(mapped.role, 'assistant');
  assert.equal(mapped.content, contract.persisted_assistant_message.content_chinese);
  assert.equal(mapped.japanese, contract.persisted_assistant_message.content_japanese);
  assert.deepEqual(mapped.audioUrls, [contract.persisted_assistant_message.audio_url]);
  assert.deepEqual(mapped.lines, normalizeLines(contract.persisted_assistant_message.lines));
});

test('edge_cases contract: missing_fields, id_conflict, out_of_order, duplicate, unknown_events', async () => {
  const edges = contract.edge_cases;

  // 1. missing_fields
  {
    const { turn_started_missing_id, line_missing_index, done_empty } = edges.missing_fields;
    // 1a & 1b: turn_started missing user_message_id + line missing index, followed by valid line & done
    const harness = setupConversationHarness(
      [
        { event: turn_started_missing_id.event, data: turn_started_missing_id.data },
        { event: line_missing_index.event, data: line_missing_index.data },
        contract.sse_events.line_0,
        contract.sse_events.line_audio_ready,
        edges.out_of_order.line_audio_before_line.authoritative_done,
      ],
      'sess_contract_1'
    );
    harness.conversation.send('测试缺字段');
    await harness.settledPromise;
    const lineEvents = harness.events.filter((e) => e.type === 'line');
    assert.equal(lineEvents.length, 1, 'line without integer index must be ignored');
    assert.equal(lineEvents.some((e) => e.payload.line.zh === line_missing_index.data.zh), false);
    assert.equal(lineEvents[0].payload.line.index, 0);
    const assistantMsg = harness.session.messages.find((m) => m.role === 'assistant');
    assert.equal(assistantMsg.serverUserMessageId, undefined);
    assert.equal(assistantMsg.turnConflict, undefined);

    // 1c: done_empty produces empty_reply error and does not store an assistant message
    const emptyHarness = setupConversationHarness(
      [{ event: done_empty.event, data: done_empty.data }],
      'sess_contract_1'
    );
    emptyHarness.conversation.send('测试空回复');
    const terminal = await emptyHarness.settledPromise;
    assert.equal(terminal.type, done_empty.expected.terminal_event);
    assert.equal(terminal.payload.kind, done_empty.expected.error_code);
    assert.equal(terminal.payload.failedInline, done_empty.expected.failed_inline);
    assert.equal(terminal.payload.message, null);
    assert.equal(
      emptyHarness.session.messages.some((m) => m.role === 'assistant'),
      done_empty.expected.stored_assistant_message
    );
  }

  // 2. id_conflict
  {
    const { first_turn_started, conflicting_turn_started, expected } = edges.id_conflict;
    const harness = setupConversationHarness(
      [
        first_turn_started,
        conflicting_turn_started,
        contract.sse_events.line_0,
        contract.sse_events.done_truncated,
      ],
      'sess_contract_1'
    );
    harness.conversation.send('测试ID冲突');
    await harness.settledPromise;
    const assistantMsg = harness.session.messages.find((m) => m.role === 'assistant');
    assert.equal(assistantMsg.turnConflict, expected.turn_conflict);
    delete assistantMsg.serverMessageId;

    let networkCalls = 0;
    const recover = createLineAudioRecovery({
      getSession: () => harness.session,
      getVoiceProfiles: () => [{ id: 7, name: '测试角色' }],
      saveState: () => true,
      request: async () => {
        networkCalls++;
        throw new Error('should not be called');
      },
    });
    await assert.rejects(
      recover({ messageId: assistantMsg.id, lineIndex: 2 }),
      new RegExp(expected.recovery_error_substring)
    );
    assert.equal(networkCalls, 0);
  }

  // 3. out_of_order
  {
    // 3a: audio_chunk arrives before line
    const { early_audio_chunk, late_line, expected: expChunkFirst } = edges.out_of_order.audio_chunk_before_line;
    const harnessChunk = setupConversationHarness(
      [
        early_audio_chunk,
        late_line,
        contract.sse_events.line_audio_ready,
        contract.sse_events.done_truncated,
      ],
      'sess_contract_1'
    );
    const observedOnLineArrival = [];
    harnessChunk.conversation.on('line', (e) => {
      observedOnLineArrival.push(structuredClone(e.line));
    });
    harnessChunk.conversation.send('测试乱序chunk');
    await harnessChunk.settledPromise;
    assert.equal(observedOnLineArrival[0].audio.status, expChunkFirst.initial_line_audio_status);
    assert.equal(observedOnLineArrival[0].audio.chunks.length, 1);
    assert.equal(observedOnLineArrival[0].audio.chunks[0].audio_url, expChunkFirst.attached_audio_url);

    // 3b: line_audio arrives before line, then reconciled by done.lines
    const { early_line_audio, late_line: lateLine2, authoritative_done, expected: expAudioFirst } =
      edges.out_of_order.line_audio_before_line;
    const harnessAudio = setupConversationHarness(
      [early_line_audio, lateLine2, authoritative_done],
      'sess_contract_1'
    );
    const lineArrivalSnapshots = [];
    harnessAudio.conversation.on('line', (e) => {
      lineArrivalSnapshots.push(structuredClone(e.line));
    });
    harnessAudio.conversation.send('测试乱序line_audio');
    await harnessAudio.settledPromise;
    assert.equal(lineArrivalSnapshots[0].audio.status, expAudioFirst.line_arrival_status);
    const assistantMsg = harnessAudio.session.messages.find((m) => m.role === 'assistant');
    assert.equal(assistantMsg.lines[0].audio.status, expAudioFirst.final_persisted_status);
    const lineAudioEmissions = harnessAudio.events.filter((e) => e.type === 'line_audio');
    assert.equal(lineAudioEmissions.length, expAudioFirst.total_line_audio_emissions);
  }

  // 4. duplicate
  {
    const { duplicate_turn_started, duplicate_line_same_index, duplicate_line_audio_in_done } = edges.duplicate;
    const harness = setupConversationHarness(
      [
        duplicate_turn_started.first,
        duplicate_turn_started.second,
        duplicate_line_same_index.first_line,
        duplicate_line_same_index.updated_line,
        duplicate_line_audio_in_done.line_audio_event,
        duplicate_line_audio_in_done.done_event,
      ],
      'sess_contract_1'
    );
    harness.conversation.send('测试重复事件');
    await harness.settledPromise;
    const assistantMsg = harness.session.messages.find((m) => m.role === 'assistant');
    assert.equal(assistantMsg.serverUserMessageId, duplicate_turn_started.expected.server_user_message_id);
    assert.equal(Boolean(assistantMsg.turnConflict), duplicate_turn_started.expected.turn_conflict);
    assert.equal(assistantMsg.lines.length, duplicate_line_same_index.expected.persisted_line_count);
    assert.equal(assistantMsg.lines[0].zh, duplicate_line_same_index.expected.persisted_zh);
    assert.equal(assistantMsg.lines[0].emotion, duplicate_line_same_index.expected.persisted_emotion);
    const lineAudioEmissions = harness.events.filter((e) => e.type === 'line_audio');
    assert.equal(lineAudioEmissions.length, duplicate_line_audio_in_done.expected.emitted_line_audio_count);
  }

  // 5. unknown_events
  {
    const { unknown_sse_event, unknown_turn_reply_status } = edges.unknown_events;
    const customEvents = [];
    const harness = setupConversationHarness(
      [
        { event: unknown_sse_event.event, data: unknown_sse_event.data },
        contract.sse_events.line_0,
        contract.sse_events.line_audio_ready,
        contract.sse_events.done_complete,
      ],
      'sess_contract_1'
    );
    harness.conversation.on(unknown_sse_event.event, (payload) => {
      customEvents.push(payload);
    });
    harness.conversation.send('测试未知SSE事件');
    await harness.settledPromise;
    assert.equal(customEvents.length, 1);
    assert.deepEqual(customEvents[0], unknown_sse_event.data);
    const assistantMsg = harness.session.messages.find((m) => m.role === 'assistant');
    assert.equal(assistantMsg.content, contract.sse_events.done_complete.data.chinese);

    // Direct streamChat verification for unknown event
    const directEvents = [];
    const fetch = createSseFetch([
      { event: unknown_sse_event.event, data: unknown_sse_event.data },
      contract.sse_events.done_complete,
    ]);
    await new Promise((resolve) => {
      streamChat({
        prompt: '直接流测试',
        sessionId: 'sess_contract_1',
        fetch,
        onEvent: (type, payload) => directEvents.push({ type, payload }),
        onEnd: () => resolve(),
      });
    });
    assert.equal(directEvents.some((e) => e.type === unknown_sse_event.event), true);

    // Unknown turn-reply status gives up immediately on attempt 1
    let mysteryAttempts = 0;
    await assert.rejects(
      resolveTurnReplyIdentity({
        sessionId: 'sess_contract_1',
        userMessageId: 101,
        maxAttempts: 3,
        delayMs: 5,
        request: async () => {
          mysteryAttempts++;
          return structuredClone(unknown_turn_reply_status.response);
        },
      }),
      new RegExp(unknown_turn_reply_status.expected.error_substring)
    );
    assert.equal(mysteryAttempts, unknown_turn_reply_status.expected.attempts);
  }
});

function createRawByteStreamFetch(byteChunks) {
  return async () => {
    const stream = new ReadableStream({
      start(controller) {
        for (const chunk of byteChunks) {
          controller.enqueue(chunk);
        }
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };
}

test('SSE parser: omits event header after named event resets to default text frame', async () => {
  const encoder = new TextEncoder();

  const rawStandardFrames = [
    'event: audio_chunk\ndata: {"index":0,"sentence":"おはよう。","audio_url":"/audio/0.wav"}\n\n',
    'data: {"delta_chinese":"早安。"}\n\n',
    'event: done\ndata: {"chinese":"早安。","japanese":"おはよう。","session_id":"sess_m1"}\n\n',
  ].join('');

  const eventsA = [];
  const audioCallsA = [];
  const textChunksA = [];
  const endA = await new Promise((resolve) => {
    streamChat({
      prompt: '早安',
      sessionId: 'sess_m1',
      fetch: createRawByteStreamFetch([encoder.encode(rawStandardFrames)]),
      onEvent: (type, payload) => eventsA.push({ type, payload }),
      onAudio: (url, idx, sentence) => audioCallsA.push({ url, idx, sentence }),
      onChunk: (acc, delta) => {
        if (delta) textChunksA.push({ acc, delta });
      },
      onEnd: (acc, meta) => resolve({ acc, meta }),
    });
  });

  assert.equal(eventsA.filter((e) => e.type === 'audio_chunk').length, 1);
  assert.equal(audioCallsA.length, 1);
  assert.equal(audioCallsA[0].sentence, 'おはよう。');
  assert.deepEqual(textChunksA, [{ acc: '早安。', delta: '早安。' }]);
  assert.equal(endA.acc, '早安。');
  assert.equal(endA.meta.error, null);

  // Also verify immediate post-dispatch reset before any blank line and empty-boundary reset
  const rawImmediateAndEmptyReset = [
    'event: audio_chunk\ndata: {"index":0,"sentence":"おはよう。","audio_url":"/audio/0.wav"}\n',
    'data: {"delta_chinese":"早安。"}\n\n',
    'event: unhandled_ping\n\n',
    'data: {"delta_chinese":"再会。"}\n\n',
    'event: done\ndata: {"chinese":"早安。再会。","japanese":"おはよう。","session_id":"sess_m1_b"}\n\n',
  ].join('');

  const eventsB = [];
  const textDeltasB = [];
  const endB = await new Promise((resolve) => {
    streamChat({
      prompt: '早安',
      sessionId: 'sess_m1_b',
      fetch: createRawByteStreamFetch([encoder.encode(rawImmediateAndEmptyReset)]),
      onEvent: (type, payload) => eventsB.push({ type, payload }),
      onChunk: (_acc, delta) => {
        if (delta) textDeltasB.push(delta);
      },
      onEnd: (acc, meta) => resolve({ acc, meta }),
    });
  });

  assert.equal(eventsB.filter((e) => e.type === 'audio_chunk').length, 1, 'audio_chunk must fire only once');
  assert.deepEqual(textDeltasB, ['早安。', '再会。']);
  assert.equal(endB.acc, '早安。再会。');
  assert.equal(endB.meta.error, null);
});

test('SSE parser: tolerates variable colon whitespace, CRLF line endings, and split byte chunks', async () => {
  const rawCrlfStream = [
    'event:audio_chunk\r\n',
    'data:{"index":0,"line":0,"sentence":"甲。","audio_url":"/audio/0.wav"}\r\n\r\n',
    'event:   line\r\n',
    'data:   {"index":0,"zh":"甲乙","ja":"甲。乙。","emotion":"happy"}\r\n\r\n',
    'data:{"delta_chinese":"甲"}\r\n\r\n',
    'data:   {"delta_chinese":"乙"}\r\n\r\n',
    'event: done\r\n',
    'data: {"chinese":"甲乙","japanese":"甲。乙。","session_id":"sess_m1_crlf"}\r\n\r\n',
  ].join('');

  const fullBytes = new TextEncoder().encode(rawCrlfStream);
  const splitChunks = [];
  for (let offset = 0; offset < fullBytes.length; offset += 7) {
    splitChunks.push(fullBytes.subarray(offset, offset + 7));
  }

  const events = [];
  const audioCalls = [];
  const textDeltas = [];
  const end = await new Promise((resolve) => {
    streamChat({
      prompt: '测试CRLF与分片',
      sessionId: 'sess_m1_crlf',
      fetch: createRawByteStreamFetch(splitChunks),
      onEvent: (type, payload) => events.push({ type, payload }),
      onAudio: (url, idx, sentence) => audioCalls.push({ url, idx, sentence }),
      onChunk: (_acc, delta) => {
        if (delta) textDeltas.push(delta);
      },
      onEnd: (acc, meta) => resolve({ acc, meta }),
    });
  });

  assert.equal(events.length, 2);
  assert.equal(events[0].type, 'audio_chunk');
  assert.equal(events[0].payload.sentence, '甲。');
  assert.equal(events[1].type, 'line');
  assert.equal(events[1].payload.zh, '甲乙');
  assert.equal(events[1].payload.emotion, 'happy');
  assert.deepEqual(audioCalls, [{ url: '/audio/0.wav', idx: 0, sentence: '甲。' }]);
  assert.deepEqual(textDeltas, ['甲', '乙']);
  assert.equal(end.acc, '甲乙');
  assert.equal(end.meta.japanese, '甲。乙。');
  assert.equal(end.meta.error, null);
  assert.equal(end.meta.cancelled, false);
});
