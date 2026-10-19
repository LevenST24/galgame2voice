"""Local OpenAI/GPT-SoVITS development servers; no models or credentials needed."""

import argparse
import asyncio
from array import array
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
import random
import signal
import socket
import sys
from typing import Awaitable, Callable
import wave

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import uvicorn

REPLIES_PATH = Path(__file__).with_name("dev_standins_replies.json")
SAMPLE_RATE = 32_000
BROKEN_VARIANTS = ("fence", "preamble", "truncated_string", "truncated_lines", "unknown_act",
                   "missing_emotion", "trailing_text")
Sleep = Callable[[float], Awaitable[None]]


@dataclass
class Options:
    llm_port: int = 18080
    tts_port: int = 19880
    llm_format: str = "old"
    first_token_ms: float = 800
    tokens_per_sec: float = 35
    seed: int | None = None
    tts_latency_factor: float = 0.3
    tts_base_ms: float = 150
    tts_fail_rate: float = 0
    tts_offline: bool = False


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Replies:
    def __init__(self, options: Options):
        self.options = options
        self.data = json.loads(REPLIES_PATH.read_text(encoding="utf-8"))
        self.turn = 0
        self.indices = {"old": 0, "lines": 0}

    def next(self) -> str:
        mode = self.options.llm_format
        fmt = ("old" if self.turn % 2 == 0 else "lines") if mode == "mixed" else mode
        if mode == "broken":
            fmt = "lines"
        reply = deepcopy(self.data[fmt][self.indices[fmt] % len(self.data[fmt])])
        self.indices[fmt] += 1
        variant = BROKEN_VARIANTS[self.turn % len(BROKEN_VARIANTS)]
        self.turn += 1
        if mode != "broken":
            return encode(reply)
        faults = self.data["broken"]
        if variant == "unknown_act":
            reply["lines"][0]["act"] = faults["unknown_act"]
        elif variant == "missing_emotion":
            reply["tts"].pop("emotion", None)
            for line in reply["lines"]:
                line.pop("emotion", None)
        text = encode(reply)
        if variant == "fence":
            return "```json\n" + text + "\n```"
        if variant == "preamble":
            return faults["preamble"] + text
        if variant == "truncated_string":
            # Cut inside the first Japanese string, never inside a JSON escape.
            marker = '"ja":"'
            start = text.index(marker) + len(marker)
            return text[:start + max(1, len(reply["lines"][0]["ja"]) // 2)]
        if variant == "truncated_lines":
            return text[:text.index('"lines":[') + len('"lines":[')] + encode(reply["lines"][0])
        if variant == "trailing_text":
            return text + faults["trailing_text"]
        return text


def split_pieces(text: str, rng: random.Random) -> list[str]:
    pieces = []
    offset = 0
    while offset < len(text):
        size = rng.randint(1, 4)
        pieces.append(text[offset:offset + size])
        offset += size
    return pieces


def event(value: object) -> str:
    return f"data: {encode(value)}\n\n"


def create_llm_app(options: Options, *, sleep: Sleep = asyncio.sleep) -> FastAPI:
    app = FastAPI(title="Stand-in LLM")
    replies = Replies(options)
    rng = random.Random(options.seed)

    @app.get("/v1/models")
    async def models(stream: bool = False):
        model = {"id": "standin", "object": "model", "created": 0, "owned_by": "standin"}
        if stream:
            async def events():
                yield event(model)
                yield "data: [DONE]\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")
        return {"object": "list", "data": [model]}

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a JSON object")
        text = replies.next()
        pieces = split_pieces(text, rng)
        base = {"id": f"chatcmpl-standin-{replies.turn}", "created": 0, "model": "standin"}

        def chunk(delta: dict, finish: str | None = None):
            return {**base, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        async def events():
            await sleep(options.first_token_ms / 1000)
            yield event(chunk({"role": "assistant", "content": pieces[0]}))
            for previous, piece in zip(pieces, pieces[1:], strict=False):
                await sleep(len(previous) / options.tokens_per_sec)
                yield event(chunk({"content": piece}))
            yield event(chunk({}, "stop"))
            yield "data: [DONE]\n\n"

        if payload.get("stream", False):
            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await sleep(options.first_token_ms / 1000 + len(text) / options.tokens_per_sec)
        return {**base, "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

    return app


def audio_duration(text: str) -> float:
    return max(0.4, 0.13 * len(text))


def synthesize_wav(text: str, seed: int | None = None) -> bytes:
    """Quiet voiced/noisy bursts with punctuation pauses, suitable for RMS lip sync."""
    rng = random.Random(seed)
    duration = audio_duration(text)
    count = round(duration * SAMPLE_RATE)
    syllables_hz = rng.uniform(4, 7)
    pcm = array("h")
    for index in range(count):
        t = index / SAMPLE_RATE
        char = text[min(len(text) - 1, int(t / duration * len(text)))] if text else ""
        burst = max(0, math.sin(2 * math.pi * syllables_hz * t)) ** 1.5
        envelope = (0.015 + 0.985 * burst) * min(1, t / 0.015, (duration - t) / 0.025)
        if char in "、。！？!?，,.…":
            envelope *= 0.015
        voice = 0.75 * math.sin(2 * math.pi * 185 * t) + 0.25 * rng.uniform(-1, 1)
        pcm.append(round(4000 * envelope * voice))
    if sys.byteorder != "little":
        pcm.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def create_tts_app(options: Options, *, sleep: Sleep = asyncio.sleep) -> FastAPI:
    app = FastAPI(title="Stand-in GPT-SoVITS")
    rng = random.Random(options.seed)
    app.state.engine = {}

    @app.middleware("http")
    async def offline(request: Request, call_next):
        if options.tts_offline:
            return JSONResponse({"error": "GPT-SoVITS stand-in is offline"}, status_code=503)
        return await call_next(request)

    @app.api_route("/", methods=["GET", "POST"])
    @app.api_route("/health", methods=["GET", "POST"])
    @app.api_route("/control", methods=["GET", "POST"])
    async def health():
        # Control is deliberately a no-op; never restart or terminate another process.
        return {"status": "running", "version": "v2", "service": "GPT-SoVITS"}

    async def parameters(request: Request) -> dict:
        data = await request.json() if request.method == "POST" else dict(request.query_params)
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected a JSON object")
        return data

    @app.api_route("/set_gpt_weights", methods=["GET", "POST"])
    @app.api_route("/set_sovits_weights", methods=["GET", "POST"])
    @app.api_route("/set_refer_audio", methods=["GET", "POST"])
    async def configure(request: Request):
        data = await parameters(request)
        key = "refer_audio_path" if request.url.path == "/set_refer_audio" else "weights_path"
        path = data.get(key)
        if not path or "invalid" in str(path):
            return JSONResponse({"code": 1, "message": f"Invalid {key}"}, status_code=400)
        app.state.engine[request.url.path] = data
        return {"code": 0, "message": "Stand-in configuration updated"}

    @app.api_route("/tts", methods=["GET", "POST"])
    async def tts(request: Request):
        data = await parameters(request)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"code": 1, "message": "Empty text for TTS"}, status_code=400)
        if rng.random() < options.tts_fail_rate:
            return JSONResponse({"code": 1, "message": "Simulated synthesis failure"}, status_code=500)
        audio_seed = rng.getrandbits(64)
        await sleep(options.tts_base_ms / 1000 + options.tts_latency_factor * audio_duration(text))
        audio = await asyncio.to_thread(synthesize_wav, text, audio_seed)
        # A complete WAV also works with the app's streaming HTTP reader.
        return Response(audio, media_type="audio/wav")

    return app


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm-port", type=int, default=18080)
    parser.add_argument("--tts-port", type=int, default=19880)
    parser.add_argument("--llm-format", choices=("old", "lines", "mixed", "broken"), default="old")
    parser.add_argument("--first-token-ms", type=float, default=800)
    parser.add_argument("--tokens-per-sec", type=float, default=35, help="Approximate characters per second")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--tts-latency-factor", type=float, default=0.3)
    parser.add_argument("--tts-base-ms", type=float, default=150)
    parser.add_argument("--tts-fail-rate", type=float, default=0)
    parser.add_argument("--tts-offline", action="store_true")
    args = parser.parse_args(argv)
    for key in ("llm_port", "tts_port"):
        if not 1 <= getattr(args, key) <= 65535:
            parser.error(f"--{key.replace('_', '-')} must be between 1 and 65535")
    if args.llm_port == args.tts_port:
        parser.error("LLM and TTS ports must differ")
    for key in ("first_token_ms", "tts_latency_factor", "tts_base_ms", "tts_fail_rate", "tokens_per_sec"):
        value = getattr(args, key)
        if not math.isfinite(value) or value < 0:
            parser.error(f"--{key.replace('_', '-')} must be finite and nonnegative")
    if args.tokens_per_sec == 0 or args.tts_fail_rate > 1:
        parser.error("--tokens-per-sec must be positive; --tts-fail-rate must be in [0, 1]")
    return Options(**vars(args))


def print_instructions(options: Options) -> None:
    print(f"LLM: http://127.0.0.1:{options.llm_port}/v1 (model: standin)", flush=True)
    print(f"TTS: http://127.0.0.1:{options.tts_port}")
    print("In app settings: enable allow_private_llm_endpoints; configure/select provider openai:")
    print(f"  api_base_url=http://127.0.0.1:{options.llm_port}/v1, chat_model=standin, api_key=standin")
    print("  Set active_provider_id=openai. LLM configuration is stored in SQLite, not environment variables.")
    print("Ready-to-run app command (PowerShell, from the repository root; configure LLM as above):")
    print(f"$env:GALGAME2VOICE_GPT_SOVITS_BASE_URL='http://127.0.0.1:{options.tts_port}'; "
          ".\\.venv\\Scripts\\python -m galgame2voice.main")
    print("Keep/configure an active character voice profile with reference audio: app-side validation still applies.")
    print("Audio is a synthetic tone/noise envelope, not intelligible speech. Ctrl+C stops both servers.", flush=True)


class StandinServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # One owner for both listeners, including Windows Ctrl+C.
        yield


async def serve(options: Options, sockets: list[socket.socket]) -> None:
    apps = (create_llm_app(options), create_tts_app(options))
    servers = [StandinServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info",
                                          timeout_graceful_shutdown=2))
               for app, port in zip(apps, (options.llm_port, options.tts_port), strict=True)]

    def shutdown(*_args):
        for server in servers:
            server.should_exit = True

    old_handlers = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
    tasks = [asyncio.create_task(server.serve(sockets=[sock]))
             for server, sock in zip(servers, sockets, strict=True)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        shutdown()
        await asyncio.gather(*tasks)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> None:
    options = parse_args(argv)
    # Bind both before starting either so a busy port cannot leave half a pair running.
    with ExitStack() as stack:
        sockets = []
        for port in (options.llm_port, options.tts_port):
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            sock.setblocking(False)
            sockets.append(sock)
        print_instructions(options)
        asyncio.run(serve(options, sockets))


if __name__ == "__main__":
    main()
