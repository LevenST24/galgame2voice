// A controllable SSE response using the real Fetch/ReadableStream interfaces.
export const tick = () => new Promise(resolve => setTimeout(resolve, 0));

export function fakeChatStream() {
  const requests = [];
  const encoder = new TextEncoder();
  const fetch = async (url, options) => {
    let output;
    let text = '';
    const body = new ReadableStream({ start(controller) { output = controller; } });
    const request = {
      url, options, ...JSON.parse(options.body),
      async event(type, payload) {
        await this.packet([[type, payload]]);
      },
      async packet(events) {
        output.enqueue(encoder.encode(events.map(([type, payload]) =>
          `event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`).join('')));
        await tick();
      },
      async onChunk(full) {
        const delta = full.slice(text.length);
        text = full;
        await this.event('text', { delta_chinese: delta });
      },
      async onEnd(full, meta = {}) {
        if (meta.error) {
          await this.event('error', { error: meta.error, error_code: meta.errorCode });
        } else {
          await this.event('done', { chinese: full, ...meta });
        }
      },
    };
    requests.push(request);
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
  };
  return { fetch, requests };
}
