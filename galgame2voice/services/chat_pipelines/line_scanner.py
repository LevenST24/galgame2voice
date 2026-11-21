"""
Incremental quote-, escape-, and nesting-aware line scanner for the new LLM reply format.
Extracts complete line objects from streaming tokens without rescanning past buffers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

EMOTIONS = ("gentle", "happy", "shy", "tsundere", "cool", "sad", "angry")
MOTIONS = ("nod", "bounce", "startle", "sigh", "fidget", "head-shake", "lean-in", "step-back")
MOVES = ("approach", "retreat", "closeup", "left", "right", "center")
FX = ("sweat", "anger", "heart", "sparkle", "question", "exclamation")

_RE_THINK_OPEN_MATCH = re.compile(r"^<\s*(think(?:ing)?|thought|reasoning)\b", re.IGNORECASE)
_RE_THINK_CLOSE_PREFIX = re.compile(r"^</\s*(think(?:ing)?|thought|reasoning)\b", re.IGNORECASE)
_RE_THINK_CLOSE_MATCH = re.compile(r"^</\s*(think(?:ing)?|thought|reasoning)\b[^>]*>", re.IGNORECASE)
_RE_THINK_SELF_CLOSING = re.compile(r"^<\s*(think(?:ing)?|thought|reasoning)\b[^>]*?/\s*>", re.IGNORECASE)


def _is_think_prefix(s: str) -> bool:
    s_clean = s.lstrip().lower()
    if not s_clean.startswith("<"):
        return False
    tag_part = s_clean[1:].lstrip()
    if not tag_part:
        return True
    return any(t.startswith(tag_part) for t in ("think", "thinking", "thought", "reasoning")) or bool(
        _RE_THINK_OPEN_MATCH.match(s)
    )


def _is_think_close_prefix(s: str) -> bool:
    s_clean = s.lstrip().lower()
    if not s_clean.startswith("</"):
        return False
    tag_part = s_clean[2:].lstrip()
    if not tag_part:
        return True
    return any(t.startswith(tag_part) for t in ("think", "thinking", "thought", "reasoning")) or bool(
        _RE_THINK_CLOSE_PREFIX.match(s)
    )


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
    format: str  # 'unknown' | 'lines'
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


_RE_LINE_STR_OPEN = re.compile(r'"(?:zh|ja|emotion)"\s*:\s*"')
_RE_NEXT_KNOWN_FIELD = re.compile(r'\s*(?:\}\s*$|,\s*"(?:zh|ja|emotion|act)"\s*:)')
_RE_NEXT_OTHER_FIELD = re.compile(r'\s*,\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:\s*["{\[\dtfn-]')
_RE_BROKEN_NEXT_FIELD = re.compile(
    r'\s*(?:,\s*"(?:zh|ja|emotion|act)"\s*(?!:)|,\s*(?:zh|ja|emotion|act)\s*:|"(?:zh|ja|emotion|act)"\s*:|,\s*\}\s*$)'
)


def _escape_inner_quotes(inner: str) -> str:
    out: list[str] = []
    escaped = False
    for ch in inner:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            out.append(ch)
            escaped = True
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return "".join(out)


def repair_line_quotes(raw_json: str) -> str:
    """
    Escapes unescaped ASCII double quotes inside top-level zh/ja/emotion string
    fields of a single line JSON object while leaving act untouched.
    """
    pieces: list[str] = []
    last_copied = 0
    changed = False
    depth = 0
    expect_key = False
    in_str = False
    escaped = False
    i = 0
    n = len(raw_json)

    while i < n:
        ch = raw_json[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == "{":
            if depth == 0:
                expect_key = True
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if depth == 1 and ch == ",":
            expect_key = True
            i += 1
            continue
        if depth == 1 and ch == ":":
            expect_key = False
            i += 1
            continue
        if ch == '"':
            if depth == 1 and expect_key:
                m = _RE_LINE_STR_OPEN.match(raw_json, i)
                if m is not None:
                    val_start = m.end()
                    val_esc = False
                    inner_quotes = 0
                    close_q: int | None = None
                    for q in range(val_start, n):
                        vc = raw_json[q]
                        if val_esc:
                            val_esc = False
                        elif vc == "\\":
                            val_esc = True
                        elif vc == '"':
                            if _RE_NEXT_KNOWN_FIELD.match(raw_json, q + 1) or (
                                inner_quotes % 2 == 0 and _RE_NEXT_OTHER_FIELD.match(raw_json, q + 1)
                            ):
                                close_q = q
                                break
                            if inner_quotes % 2 == 0 and _RE_BROKEN_NEXT_FIELD.match(raw_json, q + 1):
                                return raw_json
                            inner_quotes += 1
                    if close_q is None:
                        return raw_json
                    inner = raw_json[val_start:close_q]
                    escaped_inner = _escape_inner_quotes(inner)
                    if escaped_inner != inner:
                        pieces.append(raw_json[last_copied:val_start])
                        pieces.append(escaped_inner)
                        last_copied = close_q
                        changed = True
                    expect_key = False
                    i = close_q + 1
                    continue
            in_str = True
        i += 1

    if not changed:
        return raw_json
    pieces.append(raw_json[last_copied:])
    return "".join(pieces)


class LineScanner:
    """
    Incremental quote-, escape-, and nesting-aware scanner for extracting
    lines from streaming LLM responses in O(total length) without regex.
    """

    def __init__(self) -> None:
        self._reset_state()

    def _reset_state(self) -> None:
        self.format: str = "unknown"
        self.tts: dict[str, Any] | None = None
        self.lines: list[ScannedLine] = []
        self.issues: list[str] = []
        self.trailing_text: str = ""

        # Ordinal counter for lines array items
        self._line_ordinal: int = 0

        # High-level phase:
        # 0: BEFORE_ROOT (looking for '{' or leading '[')
        # 1: ROOT_OPENING (saw '{', verifying next non-ws is '"' or '}')
        # 2: IN_ROOT (inside top-level object or top-level lines array)
        # 3: AFTER_ROOT (top-level value cleanly closed)
        # 4: ARRAY_OPENING (saw leading '[', verifying next non-ws is '{')
        self._phase: int = 0

        # Root shape tracking (top-level array vs root object / flat line object)
        self._can_open_array: bool = True
        self._fence_buf: str = ""
        self._fence_consumed: bool = False
        self._root_is_array: bool = False
        self._saw_lines_key: bool = False
        self._saw_flat_line_key: bool = False
        self._root_chars: list[str] = []

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

        # non-object element in lines buffering
        self._in_non_obj_elem: bool = False
        self._non_obj_chars: list[str] = []
        self._non_obj_obj_depth: int = 0
        self._non_obj_arr_depth: int = 0

        # other value depth tracking
        self._other_obj_depth: int = 0
        self._other_arr_depth: int = 0

        # Thinking tag tracking
        self._think_depth: int = 0
        self._tag_pending: str = ""
        self._orphan_close_pending: str = ""

    def _feed_think_tag_outside_string(self, c: str) -> bool:
        """Tracks reasoning tags outside JSON strings; returns True only on an unmatched closing tag."""
        if c == "<":
            self._orphan_close_pending = "<"
            return False
        if self._orphan_close_pending:
            self._orphan_close_pending += c
            if c == ">":
                tag = self._orphan_close_pending
                self._orphan_close_pending = ""
                if _RE_THINK_SELF_CLOSING.match(tag):
                    return False
                if _RE_THINK_OPEN_MATCH.match(tag):
                    self._think_depth += 1
                    return False
                if _RE_THINK_CLOSE_MATCH.match(tag):
                    if self._think_depth > 0:
                        self._think_depth -= 1
                        return False
                    return True
                return False
            if not _is_think_prefix(self._orphan_close_pending) and not _is_think_close_prefix(
                self._orphan_close_pending
            ):
                self._orphan_close_pending = ""
        return False

    def _enter_root_object(self) -> None:
        self._can_open_array = False
        self._fence_buf = ""
        self._root_is_array = False
        self._saw_lines_key = False
        self._saw_flat_line_key = False
        self._root_chars = ["{"]
        self._phase = 1

    def _finish_flat_root_if_needed(self, emitted_lines: list[ScannedLine]) -> None:
        raw_chars = self._root_chars
        self._root_chars = []
        if self._saw_lines_key or not self._saw_flat_line_key:
            return
        raw_json = "".join(raw_chars)
        try:
            obj = json.loads(raw_json)
        except Exception as e:
            self.issues.append(f"JSON decode error in line {self._line_ordinal}: {e}")
            self._line_ordinal += 1
            return
        if not isinstance(obj, dict) or "lines" in obj:
            return
        line, line_issues = validate_line(obj, ordinal=self._line_ordinal)
        self._line_ordinal += 1
        if line is not None:
            self.lines.append(line)
            emitted_lines.append(line)
        if line_issues:
            self.issues.extend(line_issues)

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
                # AFTER_ROOT: Paired <think>...</think> blocks are ignored; an unmatched closing
                # reasoning tag means everything before it was thinking.
                if self._feed_think_tag_outside_string(c):
                    self._reset_state()
                    emitted_lines.clear()
                    continue
                self.trailing_text += c
                continue

            if self._phase == 0:
                # BEFORE_ROOT: Handle thinking tags and look for opening '{' or leading '['
                if self._think_depth > 0:
                    # Inside a thinking block: look for tags e.g. </think> or nested <thought>
                    if c == "<":
                        self._tag_pending = "<"
                        continue
                    if self._tag_pending:
                        self._tag_pending += c
                        if c == ">":
                            if _RE_THINK_CLOSE_MATCH.match(self._tag_pending):
                                self._think_depth = max(0, self._think_depth - 1)
                            elif _RE_THINK_SELF_CLOSING.match(self._tag_pending):
                                pass
                            elif _RE_THINK_OPEN_MATCH.match(self._tag_pending):
                                self._think_depth += 1
                            self._tag_pending = ""
                            continue
                        if not _is_think_prefix(self._tag_pending) and not _is_think_close_prefix(self._tag_pending):
                            self._tag_pending = ""
                        continue
                    # Ignore all characters (including '{' and '[') while inside thinking block
                    continue

                # Outside thinking block in phase 0:
                if c == "<":
                    if self._fence_buf:
                        self._can_open_array = False
                        self._fence_buf = ""
                    self._tag_pending = "<"
                    continue
                if self._tag_pending:
                    self._tag_pending += c
                    if c == ">":
                        if _RE_THINK_SELF_CLOSING.match(self._tag_pending):
                            self._tag_pending = ""
                            continue
                        if _RE_THINK_OPEN_MATCH.match(self._tag_pending):
                            self._think_depth += 1
                            self._tag_pending = ""
                            continue
                        if _RE_THINK_CLOSE_MATCH.match(self._tag_pending):
                            self._reset_state()
                            emitted_lines.clear()
                            continue
                        # Not a think tag (e.g. <p>)
                        pending = self._tag_pending
                        self._tag_pending = ""
                        self._can_open_array = False
                        self._fence_buf = ""
                        if "{" in pending:
                            self._enter_root_object()
                            continue
                    elif c == "{":
                        if _is_think_prefix(self._tag_pending):
                            # '{' inside tag attribute (e.g. <think metadata="{...}">)
                            continue
                        self._tag_pending = ""
                        self._enter_root_object()
                        continue
                    elif not _is_think_prefix(self._tag_pending) and not _is_think_close_prefix(self._tag_pending):
                        pending = self._tag_pending
                        self._tag_pending = ""
                        self._can_open_array = False
                        self._fence_buf = ""
                        if "{" in pending:
                            self._enter_root_object()
                            continue
                    continue

                # Leading '[' (with optional leading whitespace or single markdown code fence)
                if self._can_open_array:
                    if not self._fence_buf:
                        if c.isspace():
                            continue
                        if c == "[":
                            self._can_open_array = False
                            self._phase = 4
                            continue
                        if c == "`" and not self._fence_consumed:
                            self._fence_buf = "`"
                            continue
                    else:
                        cand = (self._fence_buf + c).lower()
                        if cand in ("`", "``", "```", "```j", "```js", "```jso", "```json"):
                            self._fence_buf = cand
                            continue
                        if self._fence_buf in ("```", "```json"):
                            self._fence_buf = ""
                            self._fence_consumed = True
                            if c.isspace():
                                continue
                            if c == "[":
                                self._can_open_array = False
                                self._phase = 4
                                continue
                            if c == "{":
                                self._enter_root_object()
                                continue
                        self._fence_buf = ""
                    self._can_open_array = False

                # Normal phase 0 character check
                if c == "{":
                    self._enter_root_object()
                continue

            if self._phase == 4:
                # ARRAY_OPENING: Only counts as top-level lines array if '[' is followed by '{'
                if c.isspace():
                    continue
                if c == "{":
                    self.format = "lines"
                    self._root_is_array = True
                    self._phase = 2
                    self._in_lines = True
                    self._in_line_elem = True
                    self._elem_obj_depth = 1
                    self._elem_chars = ["{"]
                    continue
                # Not '[{' (e.g. '[happy] 今天真开心！'); reset to BEFORE_ROOT
                self._phase = 0
                if c == "<":
                    self._tag_pending = "<"
                continue

            if self._phase == 1:
                # ROOT_OPENING: Expecting '"' or '}' or whitespace
                if c.isspace():
                    self._root_chars.append(c)
                    continue
                if c == '"':
                    self._phase = 2
                    self._root_chars.append(c)
                    self._reading_key = True
                    self._key_chars = []
                    self._in_string = True
                    self._escape = False
                    continue
                if c == "}":
                    # Empty root object {}
                    self._root_chars = []
                    self._phase = 3
                    continue
                # Not a valid JSON object start; reset to BEFORE_ROOT
                self._root_chars = []
                self._phase = 0
                if c == "{":
                    self._enter_root_object()
                elif c == "<":
                    self._tag_pending = "<"
                continue

            # self._phase == 2: IN_ROOT
            if self._in_string:
                self._orphan_close_pending = ""
                if not self._root_is_array and not self._saw_lines_key:
                    self._root_chars.append(c)
            else:
                had_tag_pending = bool(self._orphan_close_pending)
                if self._feed_think_tag_outside_string(c):
                    self._reset_state()
                    emitted_lines.clear()
                    continue
                if self._think_depth > 0:
                    continue
                if (
                    not self._root_is_array
                    and not self._saw_lines_key
                    and not self._orphan_close_pending
                    and not (had_tag_pending and c == ">")
                ):
                    self._root_chars.append(c)
            if self._in_lines:
                self._process_in_lines(c, emitted_lines)
            elif self._in_tts:
                self._process_in_tts(c)
            elif self._in_other_val:
                self._process_in_other_val(c)
            else:
                self._process_root_level(c)
            if not self._root_is_array and self._phase == 3:
                self._finish_flat_root_if_needed(emitted_lines)

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
                        parsed_ok = False
                        try:
                            obj = json.loads(raw_json)
                            parsed_ok = True
                        except Exception as e:
                            repaired = repair_line_quotes(raw_json)
                            if repaired != raw_json:
                                try:
                                    obj = json.loads(repaired)
                                    parsed_ok = True
                                except Exception:
                                    pass
                            if not parsed_ok:
                                line = None
                                line_issues = [f"JSON decode error in line {self._line_ordinal}: {e}"]

                        if parsed_ok:
                            line, line_issues = validate_line(obj, ordinal=self._line_ordinal)

                        self._line_ordinal += 1
                        if line is not None:
                            self.lines.append(line)
                            emitted_lines.append(line)
                        if line_issues:
                            self.issues.extend(line_issues)
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
                        if self._root_is_array:
                            self._phase = 3
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
                self._elem_chars = ["{"]
            elif c == "]":
                self._in_lines = False
                if self._root_is_array:
                    self._phase = 3
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
                        self._saw_lines_key = True
                        self._root_chars = []
                        self.format = "lines"
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
                if key in ("zh", "ja") and c == '"':
                    self._saw_flat_line_key = True
                    self.format = "lines"
                self._in_other_val = True
                self._other_obj_depth = 1 if c == "{" else 0
                self._other_arr_depth = 1 if c == "[" else 0
                if c == '"':
                    self._in_string = True
                    self._escape = False
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
            tag_is_think = False
            if self._tag_pending:
                tag_part = self._tag_pending.lstrip().lower()[1:].lstrip()
                tag_is_think = bool(tag_part) and (
                    any(t.startswith(tag_part) for t in ("think", "thinking", "thought", "reasoning"))
                    or bool(_RE_THINK_OPEN_MATCH.match(self._tag_pending))
                )
            if self._think_depth > 0 or tag_is_think:
                truncated = True
                self.issues.append("stream truncated inside thinking block")
            else:
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
