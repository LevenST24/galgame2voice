"""Fast protocol/audio checks; simulated delays are recorded, never slept."""

from array import array
import io
import json
import math
import sys
import wave

import httpx
import pytest

from scripts.dev_standins import (
    BROKEN_VARIANTS, Options, REPLIES_PATH, SAMPLE_RATE, create_llm_app,
    create_tts_app, parse_args, print_instructions, synthesize_wav,
)

pytestmark = pytest.mark.asyncio
CANNED = json.loads(REPLIES_PATH.read_text(encoding="utf-8"))


async def no_sleep(_seconds):
    pass


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://standin", trust_env=False)


def read_chunks(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    records = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    assert records[-1] == "[DONE]"
    chunks = [json.loads(item) for item in records[:-1]]
    assert all(item["model"] == "standin" and item["object"] == "chat.completion.chunk" for item in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    pieces = [item["choices"][0]["delta"].get("content", "") for item in chunks[:-1]]
    assert all(1 <= len(piece) <= 4 for piece in pieces)
    return "".join(pieces), pieces


@pytest.mark.parametrize("fmt", ["old", "lines"])
async def test_stream_reassembles_every_canned_reply(fmt):
    app = create_llm_app(Options(llm_format=fmt, seed=42), sleep=no_sleep)
    assert len(CANNED[fmt]) >= 6
    async with client_for(app) as client:
        # Include wraparound, so the data file is the authority for every turn.
        for expected in CANNED[fmt] + CANNED[fmt][:1]:
            response = await client.post("/v1/chat/completions", json={"model": "standin", "stream": True})
            text, _ = read_chunks(response)
            assert json.loads(text) == expected
    if fmt == "lines":
        assert {len(reply["lines"]) for reply in CANNED[fmt]} == {1, 2, 3, 4, 5}
        assert any(line["ja"] == "……" for reply in CANNED[fmt] for line in reply["lines"])
        assert any("act" not in line for reply in CANNED[fmt] for line in reply["lines"])


@pytest.mark.parametrize("variant", BROKEN_VARIANTS)
async def test_broken_cycle(variant):
    async with client_for(create_llm_app(Options(llm_format="broken"), sleep=no_sleep)) as client:
        outputs = []
        for index in range(len(BROKEN_VARIANTS)):
            response = await client.post("/v1/chat/completions", json={"stream": True})
            outputs.append(read_chunks(response)[0])
        text = outputs[BROKEN_VARIANTS.index(variant)]
        if variant == "fence":
            assert text.startswith("```json\n") and text.endswith("\n```")
            assert json.loads(text[8:-4]) == CANNED["lines"][0]
        elif variant == "preamble":
            prefix = CANNED["broken"]["preamble"]
            assert text.startswith(prefix)
            assert json.loads(text[len(prefix):]) == CANNED["lines"][1]
        elif variant == "truncated_string":
            with pytest.raises(json.JSONDecodeError, match="Unterminated string"):
                json.loads(text)
            assert '"ja":"' in text and not text.endswith('"')
        elif variant == "truncated_lines":
            prefix, first = text.split('"lines":[', 1)
            assert json.loads(first) == CANNED["lines"][3]["lines"][0]
            assert prefix.startswith('{"tts":') and text.endswith("}")
            with pytest.raises(json.JSONDecodeError):
                json.loads(text)
        elif variant == "unknown_act":
            assert json.loads(text)["lines"][0]["act"] == CANNED["broken"]["unknown_act"]
        elif variant == "missing_emotion":
            reply = json.loads(text)
            assert "emotion" not in reply["tts"]
            assert all("emotion" not in line for line in reply["lines"])
        else:
            reply, end = json.JSONDecoder().raw_decode(text)
            assert reply == CANNED["lines"][0]
            assert text[end:] == CANNED["broken"]["trailing_text"]
        # The seventh fault is followed by a fenced reply again.
        response = await client.post("/v1/chat/completions", json={})
        assert response.json()["choices"][0]["message"]["content"].startswith("```json\n")


async def test_nonstreaming_mixed_and_models():
    async with client_for(create_llm_app(Options(llm_format="mixed"), sleep=no_sleep)) as client:
        for fmt in ("old", "lines", "old", "lines"):
            response = await client.post("/v1/chat/completions", json={"stream": False})
            result = response.json()
            assert result["object"] == "chat.completion" and result["model"] == "standin"
            choice = result["choices"][0]
            assert choice["finish_reason"] == "stop" and choice["message"]["role"] == "assistant"
            reply = json.loads(choice["message"]["content"])
            assert reply in CANNED[fmt]
        models = (await client.get("/v1/models")).json()
        assert models["object"] == "list" and [model["id"] for model in models["data"]] == ["standin"]
        streamed = await client.get("/v1/models?stream=true")
        assert streamed.text.endswith("data: [DONE]\n\n")
        assert json.loads(streamed.text.splitlines()[0][6:]) == models["data"][0]


async def test_seed_and_injected_timing():
    waits = []

    async def record_delay(seconds):
        waits.append(seconds)

    options = Options(seed=3)
    async with client_for(create_llm_app(options, sleep=record_delay)) as client:
        first = await client.post("/v1/chat/completions", json={"stream": True})
        text, pieces = read_chunks(first)
    assert waits[0] == 0.8
    assert waits[1:] == [len(piece) / 35 for piece in pieces[:-1]]
    async with client_for(create_llm_app(options, sleep=no_sleep)) as client:
        second = await client.post("/v1/chat/completions", json={"stream": True})
        assert first.content == second.content
    waits.clear()
    async with client_for(create_llm_app(options, sleep=record_delay)) as client:
        await client.post("/v1/chat/completions", json={})
    assert waits == [pytest.approx(0.8 + len(text) / 35)]


def wav_samples(content):
    with wave.open(io.BytesIO(content), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) == (1, 2, 32000, "NONE")
        duration = wav.getnframes() / wav.getframerate()
        samples = array("h", wav.readframes(wav.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    return duration, samples


async def test_wav_duration_envelope_and_punctuation():
    durations = []
    for text in ("あ", "こんにちは", "今日はいい天気ですね"):
        duration, samples = wav_samples(synthesize_wav(text, 7))
        assert duration == pytest.approx(max(0.4, 0.13 * len(text)), abs=1 / SAMPLE_RATE)
        durations.append(duration)
        windows = [samples[i:i + 640] for i in range(0, len(samples) - 639, 640)]
        rms = sorted(math.sqrt(sum(value * value for value in win) / len(win)) for win in windows)
        assert rms[int(len(rms) * 0.9)] > max(100, 5 * rms[int(len(rms) * 0.1)])
        assert max(abs(value) for value in samples) < 5000
    assert durations[0] < durations[1] < durations[2]
    _, plain = wav_samples(synthesize_wav("ああああああああああ", 7))
    _, paused = wav_samples(synthesize_wav("あああ、。！？あああ", 7))
    start, end = round(0.39 * SAMPLE_RATE), round(0.91 * SAMPLE_RATE)
    assert sum(value * value for value in paused[start:end]) < sum(value * value for value in plain[start:end]) / 100


async def test_tts_endpoints_and_latency():
    waits = []

    async def record_delay(seconds):
        waits.append(seconds)

    app = create_tts_app(Options(seed=4), sleep=record_delay)
    async with client_for(app) as client:
        for path in ("/", "/health", "/control"):
            assert (await client.get(path)).json()["status"] == "running"
        assert (await client.post("/control", json={"command": "restart"})).status_code == 200
        for path, key in (("/set_gpt_weights", "weights_path"), ("/set_sovits_weights", "weights_path"),
                          ("/set_refer_audio", "refer_audio_path")):
            assert (await client.get(path, params={key: "fake.wav"})).json()["code"] == 0
            assert (await client.post(path, json={key: "fake.wav"})).json()["code"] == 0
            assert app.state.engine[path][key] == "fake.wav"
            assert (await client.get(path, params={key: "invalid"})).status_code == 400
        for stream in (False, True):
            response = await client.post("/tts", json={"text": "こんにちは", "text_lang": "ja", "streaming_mode": stream})
            assert response.headers["content-type"] == "audio/wav"
            assert wav_samples(response.content)[0] == pytest.approx(0.65)
        assert (await client.get("/tts", params={"text": "あ"})).status_code == 200
        assert (await client.post("/tts", json={"text": ""})).status_code == 400
    assert waits == [pytest.approx(0.15 + 0.3 * duration) for duration in (0.65, 0.65, 0.4)]


async def test_tts_offline_and_failure_knobs():
    options = parse_args(["--tts-offline"])
    async with client_for(create_tts_app(options, sleep=no_sleep)) as client:
        for path in ("/health", "/control", "/set_gpt_weights", "/tts"):
            assert (await client.get(path)).status_code == 503
    for rate, status in ((0, 200), (1, 500)):
        async with client_for(create_tts_app(Options(tts_fail_rate=rate), sleep=no_sleep)) as client:
            assert (await client.post("/tts", json={"text": "あ"})).status_code == status
    outcomes = []
    for _ in range(2):
        async with client_for(create_tts_app(Options(tts_fail_rate=0.5, seed=9), sleep=no_sleep)) as client:
            outcomes.append([(await client.post("/tts", json={"text": "あ"})).content for _ in range(4)])
    assert outcomes[0] == outcomes[1]


async def test_cli_and_startup_instructions(capsys):
    options = parse_args(["--llm-port", "18081", "--tts-port", "19881", "--llm-format", "lines", "--seed", "7"])
    print_instructions(options)
    output = capsys.readouterr().out
    assert "http://127.0.0.1:18081/v1" in output
    assert "GALGAME2VOICE_GPT_SOVITS_BASE_URL='http://127.0.0.1:19881'" in output
    assert "allow_private_llm_endpoints" in output and "chat_model=standin" in output
    for args in (["--tokens-per-sec", "0"], ["--tts-fail-rate", "1.1"], ["--first-token-ms", "-1"],
                 ["--tts-base-ms", "nan"], ["--llm-port", "0"], ["--tts-port", "18080"]):
        with pytest.raises(SystemExit):
            parse_args(args)
