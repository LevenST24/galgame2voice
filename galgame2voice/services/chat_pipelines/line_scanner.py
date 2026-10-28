"""
Incremental quote-, escape-, and nesting-aware line scanner for the new LLM reply format.
Extracts complete line objects from streaming tokens without rescanning past buffers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

EMOTIONS = ("gentle", "happy", "shy", "tsundere", "cool", "sad", "angry")
MOTIONS = ("nod", "bounce", "startle", "sigh", "fidget", "head-shake", "lean-in", "step-back")
MOVES = ("approach", "retreat", "closeup", "left", "right", "center")
FX = ("sweat", "anger", "heart", "sparkle", "question", "exclamation")


@dataclass
class ScannedLine:
    ordinal: int
    zh: str
    ja: str
    emotion: str | None
    act: dict[str, Any]
    issues: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    format: str  # 'unknown' | 'lines' | 'legacy'
    tts: dict[str, Any] | None
    lines: list[ScannedLine]
    truncated: bool
    issues: list[str]
    trailing_text: str


def validate_line(obj: Any, ordinal: int = 0) -> tuple[ScannedLine | None, list[str]]:
    """
    Validates a single line object.
    Returns (ScannedLine, issues) if valid, or (None, issues) if dropped.
    """
    issues: list[str] = []
    if not isinstance(obj, dict):
        issues.append(f"non-object element at ordinal {ordinal} skipped: {type(obj).__name__}")
        return None, issues

    raw_zh = obj.get("zh")
    raw_ja = obj.get("ja")

    # Validate zh
    if raw_zh is None:
        zh = ""
    elif isinstance(raw_zh, str):
        zh = raw_zh.strip()
    else:
        issues.append(f"zh must be a string, got {type(raw_zh).__name__}")
        zh = ""

    if len(zh) > 2000:
        zh = zh[:2000]
        issues.append("zh exceeds 2000 characters and was capped")

    # Validate ja
    if raw_ja is None:
        ja = ""
    elif isinstance(raw_ja, str):
        ja = raw_ja.strip()
    else:
        issues.append(f"ja must be a string, got {type(raw_ja).__name__}")
        ja = ""

    if len(ja) > 2000:
        ja = ja[:2000]
        issues.append("ja exceeds 2000 characters and was capped")

    # At least one must be non-empty
    if not zh and not ja:
        issues.append("both zh and ja are empty")
        return None, issues

    # Validate emotion
    raw_emotion = obj.get("emotion")
    normalized_emotion = raw_emotion.strip().lower() if isinstance(raw_emotion, str) else None
    if normalized_emotion in EMOTIONS:
        emotion = normalized_emotion
    else:
        emotion = None
        issues.append(f"invalid or missing emotion: {raw_emotion!r}")

    # Validate act
    act: dict[str, Any] = {}
    raw_act = obj.get("act")
    if raw_act is not None:
        if not isinstance(raw_act, dict):
            issues.append(f"act must be a dict, got {type(raw_act).__name__}")
        else:
            for k, v in raw_act.items():
                if k == "face":
                    if isinstance(v, str) and 1 <= len(v) <= 40 and "/" not in v and "\\" not in v:
                        act["face"] = v
                    else:
                        issues.append(f"invalid act face: {v!r}")
                elif k == "pose":
                    if isinstance(v, str) and 1 <= len(v) <= 40 and "/" not in v and "\\" not in v:
                        act["pose"] = v
                    else:
                        issues.append(f"invalid act pose: {v!r}")
                elif k == "motion":
                    if v is None or v in MOTIONS:
                        act["motion"] = v
                    else:
                        issues.append(f"invalid act motion: {v!r}")
                elif k == "move":
                    if isinstance(v, str) and v in MOVES:
                        act["move"] = v
                    else:
                        issues.append(f"invalid act move: {v!r}")
                elif k == "blush":
                    if isinstance(v, bool):
                        act["blush"] = v
                    else:
                        issues.append(f"invalid act blush: {v!r}")
                elif k == "fx":
                    if v is None or v in FX:
                        act["fx"] = v
                    else:
                        issues.append(f"invalid act fx: {v!r}")
                elif k == "outfit":
                    issues.append("act outfit is rejected")
                else:
                    issues.append(f"rejected act key: {k!r}")

    line = ScannedLine(
        ordinal=ordinal,
        zh=zh,
        ja=ja,
        emotion=emotion,
        act=act,
        issues=list(issues),
    )
    return line, issues


def aggregate(lines: list[ScannedLine]) -> tuple[str, str]:
    """Joins zh and ja from lines with empty string."""
    return "".join(line.zh for line in lines), "".join(line.ja for line in lines)


class LineScanner:
    """
    Incremental quote-, escape-, and nesting-aware scanner for extracting
    lines from streaming LLM responses in O(total length) without regex.
    """

    def __init__(self) -> None:
        self.format: str = "unknown"
        self.tts: dict[str, Any] | None = None
        self.lines: list[ScannedLine] = []
        self.issues: list[str] = []
        self.trailing_text: str = ""

        # Ordinal counter for lines array items
        self._line_ordinal: int = 0

        # High-level phase:
        # 0: BEFORE_ROOT (looking for '{')
        # 1: ROOT_OPENING (saw '{', verifying next non-ws is '"' or '}')
        # 2: IN_ROOT (inside top-level object)
        # 3: AFTER_ROOT (top-level object cleanly closed)
        self._phase: int = 0

        # String / escape tracking
        self._in_string: bool = False
        self._escape: bool = False

        # Root object key/value state
        self._reading_key: bool = False
        self._key_chars: list[str] = []
        self._waiting_colon: bool = False
        self._waiting_value: bool = False
        self._current_key: str | None = None

        # Mode inside IN_ROOT
        self._in_lines: bool = False
        self._in_tts: bool = False
        self._in_other_val: bool = False

        # tts buffering
        self._tts_chars: list[str] = []
        self._tts_obj_depth: int = 0

        # lines element buffering
        self._in_line_elem: bool = False
        self._elem_chars: list[str] = []
        self._elem_obj_depth: int = 0
        self._elem_arr_depth: int = 0

        # non-object element in lines buffering
        self._in_non_obj_elem: bool = False
        self._non_obj_chars: list[str] = []
        self._non_obj_obj_depth: int = 0
        self._non_obj_arr_depth: int = 0

        # other value depth tracking
        self._other_obj_depth: int = 0
        self._other_arr_depth: int = 0

    def feed(self, chunk: str) -> list[ScannedLine]:
        """
        Feeds an incoming stream token chunk.
        Returns elements of lines array that became complete in this chunk, in order.
        """
        if not chunk:
            return []

        emitted_lines: list[ScannedLine] = []

        for c in chunk:
            if self._phase == 3:
                # AFTER_ROOT: All subsequent text is trailing chatter
                self.trailing_text += c
                continue

            if self._phase == 0:
                # BEFORE_ROOT: Look for opening '{'
                if c == "{":
                    self._phase = 1
                continue

            if self._phase == 1:
                # ROOT_OPENING: Expecting '"' or '}' or whitespace
                if c.isspace():
                    continue
                if c == '"':
                    self._phase = 2
                    self._reading_key = True
                    self._key_chars = []
                    self._in_string = True
                    self._escape = False
                    continue
                if c == "}":
                    # Empty root object {}
                    self._phase = 3
                    continue
                # Not a valid JSON object start; reset to BEFORE_ROOT
                self._phase = 0
                if c == "{":
                    self._phase = 1
                continue

            # self._phase == 2: IN_ROOT
            if self._in_lines:
                self._process_in_lines(c, emitted_lines)
            elif self._in_tts:
                self._process_in_tts(c)
            elif self._in_other_val:
                self._process_in_other_val(c)
            else:
                self._process_root_level(c)

        return emitted_lines

    def _process_in_lines(self, c: str, emitted_lines: list[ScannedLine]) -> None:
        if self._in_line_elem:
            self._elem_chars.append(c)
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif c == "\\":
                    self._escape = True
                elif c == '"':
                    self._in_string = False
            else:
                if c == '"':
                    self._in_string = True
                elif c == "{":
                    self._elem_obj_depth += 1
                elif c == "}":
                    self._elem_obj_depth -= 1
                    if self._elem_obj_depth == 0:
                        raw_json = "".join(self._elem_chars)
                        self._in_line_elem = False
                        self._elem_chars = []
                        try:
                            obj = json.loads(raw_json)
                            line, line_issues = validate_line(obj, ordinal=self._line_ordinal)
                        except Exception as e:
                            line = None
                            line_issues = [f"JSON decode error in line {self._line_ordinal}: {e}"]

                        self._line_ordinal += 1
                        if line is not None:
                            self.lines.append(line)
                            emitted_lines.append(line)
                        if line_issues:
                            self.issues.extend(line_issues)
                elif c == "[":
                    self._elem_arr_depth += 1
                elif c == "]":
                    self._elem_arr_depth -= 1
        elif self._in_non_obj_elem:
            if self._in_string:
                self._non_obj_chars.append(c)
                if self._escape:
                    self._escape = False
                elif c == "\\":
                    self._escape = True
                elif c == '"':
                    self._in_string = False
            else:
                if c == '"':
                    self._in_string = True
                    self._non_obj_chars.append(c)
                elif c == "[":
                    self._non_obj_arr_depth += 1
                    self._non_obj_chars.append(c)
                elif c == "]":
                    if self._non_obj_arr_depth > 0:
                        self._non_obj_arr_depth -= 1
                        self._non_obj_chars.append(c)
                    else:
                        # Non-object element ended and lines array closed
                        raw = "".join(self._non_obj_chars).strip()
                        self.issues.append(f"skipped non-object element at ordinal {self._line_ordinal}: {raw}")
                        self._line_ordinal += 1
                        self._in_non_obj_elem = False
                        self._non_obj_chars = []
                        self._in_lines = False
                elif c == "{":
                    self._non_obj_obj_depth += 1
                    self._non_obj_chars.append(c)
                elif c == "}":
                    if self._non_obj_obj_depth > 0:
                        self._non_obj_obj_depth -= 1
                    self._non_obj_chars.append(c)
                elif c == "," and self._non_obj_obj_depth == 0 and self._non_obj_arr_depth == 0:
                    raw = "".join(self._non_obj_chars).strip()
                    self.issues.append(f"skipped non-object element at ordinal {self._line_ordinal}: {raw}")
                    self._line_ordinal += 1
                    self._in_non_obj_elem = False
                    self._non_obj_chars = []
                else:
                    self._non_obj_chars.append(c)
        else:
            # Inside lines array, waiting for next element or ']'
            if c.isspace() or c == ",":
                return
            if c == "{":
                self._in_line_elem = True
                self._elem_obj_depth = 1
                self._elem_arr_depth = 0
                self._elem_chars = ["{"]
            elif c == "]":
                self._in_lines = False
            else:
                # Non-object element
                self._in_non_obj_elem = True
                self._non_obj_chars = [c]
                self._non_obj_obj_depth = 0
                self._non_obj_arr_depth = 1 if c == "[" else 0
                if c == '"':
                    self._in_string = True

    def _process_in_tts(self, c: str) -> None:
        self._tts_chars.append(c)
        if self._in_string:
            if self._escape:
                self._escape = False
            elif c == "\\":
                self._escape = True
            elif c == '"':
                self._in_string = False
        else:
            if c == '"':
                self._in_string = True
            elif c == "{":
                self._tts_obj_depth += 1
            elif c == "}":
                self._tts_obj_depth -= 1
                if self._tts_obj_depth == 0:
                    raw_json = "".join(self._tts_chars)
                    self._in_tts = False
                    self._tts_chars = []
                    try:
                        obj = json.loads(raw_json)
                        if isinstance(obj, dict):
                            self.tts = obj
                        else:
                            self.issues.append(f"tts must be a dict, got {type(obj).__name__}")
                    except Exception as e:
                        self.issues.append(f"failed to parse tts JSON: {e}")

    def _process_in_other_val(self, c: str) -> None:
        if self._in_string:
            if self._escape:
                self._escape = False
            elif c == "\\":
                self._escape = True
            elif c == '"':
                self._in_string = False
                if self._other_obj_depth == 0 and self._other_arr_depth == 0:
                    self._in_other_val = False
        else:
            if c == '"':
                self._in_string = True
            elif c == "{":
                self._other_obj_depth += 1
            elif c == "}":
                if self._other_obj_depth > 0:
                    self._other_obj_depth -= 1
                else:
                    self._in_other_val = False
                    self._phase = 3
            elif c == "[":
                self._other_arr_depth += 1
            elif c == "]":
                if self._other_arr_depth > 0:
                    self._other_arr_depth -= 1
            elif c == "," and self._other_obj_depth == 0 and self._other_arr_depth == 0:
                self._in_other_val = False

    def _process_root_level(self, c: str) -> None:
        if self._reading_key:
            if self._escape:
                self._key_chars.append(c)
                self._escape = False
            else:
                if c == "\\":
                    self._escape = True
                    self._key_chars.append(c)
                elif c == '"':
                    self._reading_key = False
                    self._in_string = False
                    raw_key = "".join(self._key_chars)
                    try:
                        key = json.loads(f'"{raw_key}"')
                    except Exception:
                        key = raw_key
                    self._current_key = key
                    if key == "lines":
                        self.format = "lines"
                    elif key in ("chinese", "japanese"):
                        if self.format == "unknown":
                            self.format = "legacy"
                    self._waiting_colon = True
                else:
                    self._key_chars.append(c)
        elif self._waiting_colon:
            if c == ":":
                self._waiting_colon = False
                self._waiting_value = True
        elif self._waiting_value:
            if c.isspace():
                return
            self._waiting_value = False
            key = self._current_key
            self._current_key = None
            if key == "lines":
                if c == "[":
                    self._in_lines = True
                else:
                    self.issues.append(f"lines value must be an array, got {c!r}")
                    self._in_other_val = True
                    self._other_obj_depth = 1 if c == "{" else 0
                    self._other_arr_depth = 0
                    if c == '"':
                        self._in_string = True
                    elif c == "}":
                        self._in_other_val = False
                        self._phase = 3
            elif key == "tts":
                if c == "{":
                    self._in_tts = True
                    self._tts_obj_depth = 1
                    self._tts_chars = ["{"]
                else:
                    self.issues.append(f"tts value must be an object, got {c!r}")
                    self._in_other_val = True
                    self._other_obj_depth = 0
                    self._other_arr_depth = 1 if c == "[" else 0
                    if c == '"':
                        self._in_string = True
                    elif c == "}":
                        self._in_other_val = False
                        self._phase = 3
            else:
                self._in_other_val = True
                self._other_obj_depth = 1 if c == "{" else 0
                self._other_arr_depth = 1 if c == "[" else 0
                if c == '"':
                    self._in_string = True
                elif c == "}":
                    self._in_other_val = False
                    self._phase = 3
        else:
            if c == '"':
                self._reading_key = True
                self._key_chars = []
                self._in_string = True
                self._escape = False
            elif c == "}":
                self._phase = 3

    def finish(self) -> ScanResult:
        """
        Completes the scan and returns a ScanResult.
        """
        if self._phase == 3:
            truncated = False
        elif self._phase == 0:
            truncated = False
        else:
            truncated = True
            self.issues.append("stream truncated before root object closed")

        # In legacy format or unknown, return no lines
        lines_to_return = list(self.lines) if self.format == "lines" else []

        return ScanResult(
            format=self.format,
            tts=self.tts,
            lines=lines_to_return,
            truncated=truncated,
            issues=list(self.issues),
            trailing_text=self.trailing_text,
        )
