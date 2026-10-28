"""P2b reply contract, old-format prompt stripping and history replay."""

from __future__ import annotations

import json
from typing import Any

from galgame2voice.services.chat_pipelines.line_builder import build_lines
from galgame2voice.services.chat_pipelines.line_scanner import EMOTIONS, FX, MOTIONS

# Generic old-format markers carried by the shared local character-prompt template.
OLD_FORMAT_HEADER = "【输出契约与情绪控制】"
OLD_FORMAT_START = "重要：你必须严格输出如下 JSON 格式"
OLD_FORMAT_END = '"japanese": "对应的口语化日文台词"}'


def render_lines_contract(menu: dict | None) -> str:
    """Renders compact Chinese instructions for the per-line JSON reply format."""
    emotions = "|".join(EMOTIONS)
    faces: list[str] = []
    if isinstance(menu, dict) and isinstance(menu.get("faces"), list):
        faces = [label for label in menu["faces"] if isinstance(label, str) and label]
    act_example = '"act": {"face": "表情标签"}' if faces else '"act": {}'
    poses = menu.get("poses", []) if isinstance(menu, dict) else []
    allow_pose = len(poses) >= 2
    if allow_pose:
        example = {"face": "表情标签"} if faces else {}
        example["pose"] = poses[0]
        act_example = '"act": ' + json.dumps(example, ensure_ascii=False)

    parts = [
        "【输出格式】请始终只输出如下 JSON，不要包含 Markdown 标记或多余文字：",
        '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": ['
        f'{{"zh": "给玩家看的中文台词", "ja": "意思相同的口语化日语台词", '
        f'"emotion": "gentle", {act_example}}}]}}',
        f"每行只写一句适合配音朗读的话，zh 与 ja 意思一致；emotion 必填，只能是 {emotions}。",
        ("act 可省略；不要输出 index、audio 或 outfit。" if allow_pose else
         "act 可省略；不要输出 index、audio、outfit 或 pose。"),
    ]
    if allow_pose:
        names = menu.get("pose_names", {})
        choices = "、".join(f"{pid}（{names.get(pid, pid)}）" for pid in poses)
        parts.append(f"pose 可省略，只能取：{choices}；省略时保持当前姿势。")
    if faces:
        parts.append("face 通常每行都给出，只能取：" + "、".join(faces) + "。")
    parts.append("motion 很少使用，大多数行不写；允许值：" + "、".join(MOTIONS) + "。")
    parts.append("move 可省略，只在人物真的移动时使用，不要每句都动：approach 走近一档、retreat 退后一档、closeup 特写、left 左移一档、right 右移一档、center 回到中间。")
    parts.append("fx 很少使用，大多数行不写；允许值：" + "、".join(FX) + "。")
    if isinstance(menu, dict) and menu.get("blush"):
        parts.append("blush 仅在需要时设为 true，省略或 false 表示不添加。")
    return "\n".join(parts)


def strip_old_format_block(system_prompt: str) -> str:
    """Removes the legacy bilingual contract and the shared old-format prompt section."""
    if not system_prompt:
        return system_prompt

    # Imported lazily: context_builder imports this module at import time.
    from galgame2voice.services.chat_pipelines.context_builder import BILINGUAL_OUTPUT_CONTRACT

    text = system_prompt
    changed = False
    if BILINGUAL_OUTPUT_CONTRACT in text:
        text = text.replace(BILINGUAL_OUTPUT_CONTRACT, "")
        changed = True

    while True:
        start = text.find(OLD_FORMAT_START)
        if start == -1:
            break
        end = text.find(OLD_FORMAT_END, start)
        if end == -1:
            break
        end += len(OLD_FORMAT_END)

        block_start = start
        header = text.rfind(OLD_FORMAT_HEADER, 0, start)
        if header != -1 and not text[header + len(OLD_FORMAT_HEADER):start].strip():
            block_start = header

        prefix = text[:block_start].rstrip()
        suffix = text[end:].lstrip() if not prefix else text[end:]
        text = prefix + suffix
        changed = True

    return text.strip() if changed else text


def format_lines_turn(
    lines: list[dict[str, Any]] | None,
    *,
    chinese: str,
    japanese: str,
    emotion: str | None,
    tts: dict[str, Any] | None = None,
) -> str:
    """Replays an assistant turn in the per-line format, rebuilding legacy text when needed."""
    if lines is None:
        lines = build_lines(chinese, japanese, emotion)

    fallback_emotion = emotion or "gentle"
    tts_data = tts if isinstance(tts, dict) else {}
    replayed: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        act = line.get("act")
        replayed.append({
            "zh": line.get("zh") or "",
            "ja": line.get("ja") or "",
            "emotion": line.get("emotion") or fallback_emotion,
            "act": act if isinstance(act, dict) else {},
        })

    payload = {
        "tts": {
            "speed": tts_data.get("speed", 1.0),
            "temp": tts_data.get("temp", 1.0),
            "emotion": tts_data.get("emotion") or fallback_emotion,
        },
        "lines": replayed,
    }
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["format_lines_turn", "render_lines_contract", "strip_old_format_block"]
