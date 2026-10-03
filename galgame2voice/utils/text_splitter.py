"""
Japanese dialogue sentence boundary splitter for galgame2voice.
Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
Preserves punctuation with the sentence and removes empty segments.
"""

import re

# Modal particles (语气词) and soft connective particles in Japanese and Chinese dialogue.
# When a clause ends with one of these particles before a soft comma (、, ，, ,),
# splitting at that comma artificially truncates an incomplete grammatical/intonational unit,
# leaving the particle hanging with an unnatural pause ("句末夹着语气词").
MODAL_PARTICLES_PATTERN = re.compile(
    r'(?:'
    # Japanese sentence-ending and interjectional particles (終助詞・間投助詞)
    r'[ねよわなさぞぜのかや]|ねえ|ねぇ|よね|わよ|わね|なあ|なぁ|かしら|かな|もん|っけ|'
    r'でしょ|でしょう|じゃん|ってば|てば|かい|だい|もんね|もんよ|んだ|んだから|んです|んだもん|'
    # Galgame character-specific speech particles (Murasame: じゃ/のじゃ/じゃろ, Mako: っす, Kanna: のよ/んだよ)
    r'じゃ|のじゃ|じゃな|じゃの|じゃよ|じゃろ|じゃろう|おる|おるぞ|おるの|'
    r'っす|っすよ|っすね|っすから|っすけど|っすもん|'
    r'のよ|のね|のさ|んだよ|んだね|んだよね|わけ|わけよ|わけね|わけだ|'
    r'ぜよ|わい|わさ|やん|やろ|やんな|やんか|やんけ|やねん|やんね|'
    # Japanese soft clause connectors/hedges
    r'けど|けれど|けれども|から|ので|のに|ですが|ですから|ても|でも|たり|'
    # Chinese modal particles & compound particles (语气助词与复合适词)
    r'[呢吧啊呀啦哇嘛哦哈耶呐么吗呗嘞哒滴喵嗷咯]|'
    r'呢吧|啊呀|啦呀|嘛呢|哦哈|哎呀|好嘛|对吧|行吧|对呀|行啦|好嘞|是嘛|好哒|好咯|对咯'
    r')$'
)

CLOSING_BRACKETS = set("」』\"'”’）)】]")


# Formulaic opening greetings in Japanese dialogue.
# When dialogue begins with a standard opening greeting (e.g., お久しぶりですね, こんにちは, 初めまして),
# it is a natural standalone salutation that can be emitted early for low latency.
GREETING_PREFIX_PATTERN = re.compile(
    r'^(?:'
    r'お?久しぶり(?:です(?:ね)?)?|'
    r'こんにちは|こんばんは|おはよう(?:ございます)?|'
    r'初めまして|はじめまして|'
    r'いらっしゃい(?:ませ)?|'
    r'失礼(?:します|いたします)?'
    r')$'
)

# Short conversational interjections and acknowledgments in Japanese dialogue.
# Natural standalone pause units that can be dispatched immediately to TTS (TTFA < 1s).
SHORT_INTERJECTIONS_PATTERN = re.compile(
    r'^(?:'
    r'はい|ええ|うん|あの|さあ|いや|まあ|えっと|ええと|ねえ|わあ|あら|ふむ|ほう|よし|'
    r'大丈夫|分かった|了解|承知(?:しました)?|そうです(?:ね|か)?'
    r')$'
)


def is_short_salutation(clause: str) -> bool:
    """Returns True if the clause matches a formulaic greeting or short interjection."""
    if not clause:
        return False
    cleaned = clause.strip().rstrip("、，,！？!?。")
    return bool(GREETING_PREFIX_PATTERN.match(cleaned) or SHORT_INTERJECTIONS_PATTERN.match(cleaned))


def is_natural_clause_boundary(clause: str) -> bool:
    """
    Determines if a clause is a natural pause point suitable for agile first-chunk TTS:
    - Formulaic greetings (e.g. お久しぶりですね) are natural standalone salutations.
    - Modal particles and soft connectors (e.g. ね, よ, わ, な, けど, から, ので, etc.) indicate continuation
      of a thought and should NOT be split to avoid awkward disjoint pauses.

    The greeting test strips trailing punctuation before matching, but the modal-particle test matches the
    string as passed and is anchored at its end, so it only fires once the trailing comma has been removed
    (as split_japanese_sentences does before calling here); a clause still ending in a comma is always
    reported as a natural boundary.
    """
    if not clause:
        return False
    cleaned = clause.strip().rstrip("、，,！？!?。")
    if GREETING_PREFIX_PATTERN.match(cleaned):
        return True
    if MODAL_PARTICLES_PATTERN.search(clause):
        return False
    return True


_RE_CJK_PUNCT_WHITESPACE = re.compile(r'\s*([、，。！？…])\s*')
_RE_HALFWIDTH_PUNCT_WHITESPACE = re.compile(r'\s+([,.!?])')
_RE_MULTIPLE_COMMAS = re.compile(r'[、，,]{2,}')
_RE_ELLIPSIS_COMMA = re.compile(r'(…+|\.{3,})[、，,]+')
_RE_COMMA_BEFORE_TERMINAL = re.compile(r'[、，,]+([。！？!?])')
_RE_LEADING_COMMAS = re.compile(r'^[、，,]+')
_RE_TRAILING_COMMAS = re.compile(r'[、，,]+$')

_RE_NON_FIRST_SENTENCES = re.compile(r'([^。！？!?\n]+(?:[。！？!?\n]+[」』"\'”’\)）\]】]*|\s*$))')
_RE_CLAUSE_TRAILING_PUNCT = re.compile(r'[、，,\s…\.〜~ー\-」』"\'”’\)）\]】]+$')


def normalize_dialogue_prosody(text: str) -> str:
    """
    Normalizes punctuation and prosodic markers in spoken dialogue for natural TTS synthesis:
    - Strips whitespace on both sides of CJK punctuation (、，。！？…) and whitespace before
      halfwidth punctuation (,.!?); fullwidth spaces count as whitespace
    - Collapses consecutive commas (、、 -> 、)
    - Normalizes awkward ellipsis + comma sequences (……、 -> ……)
    - Normalizes commas immediately preceding terminal punctuation (、。 -> 。)
    - Removes leading commas
    - Normalizes trailing hanging commas at end of utterance to terminal period so TTS pitch finishes naturally
    """
    if not text:
        return ""
    s = text.strip()
    s = _RE_CJK_PUNCT_WHITESPACE.sub(r'\1', s)
    s = _RE_HALFWIDTH_PUNCT_WHITESPACE.sub(r'\1', s)
    s = _RE_MULTIPLE_COMMAS.sub('、', s)
    s = _RE_ELLIPSIS_COMMA.sub(r'\1', s)
    s = _RE_COMMA_BEFORE_TERMINAL.sub(r'\1', s)
    s = _RE_LEADING_COMMAS.sub('', s)
    s = _RE_TRAILING_COMMAS.sub('。', s)
    return s.strip()


def split_japanese_sentences(
    text: str,
    is_first_chunk: bool = False,
    min_chars: int = 6,
) -> list[str]:
    """
    Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
    Preserves punctuation with the sentence and removes empty segments.

    When is_first_chunk=True, allows the first sentence chunk to split on
    clause pauses (、, ，, ,) provided the segment length >= min_chars AND
    the clause does not end with a modal particle or soft conjunction
    (e.g., ね, よ, わ, な, けど, etc.), ensuring natural intonational units.
    Subsequent sentence chunks strictly require terminal punctuation.

    Examples:
        "こんにちは！先生、今日はいい天気ですね。一緒に出かけましょう？"
        -> ["こんにちは！", "先生、今日はいい天気ですね。", "一緒に出かけましょう？"]
        "第一行。\n\n第二行！\n第三行？"
        -> ["第一行。", "第二行！", "第三行？"]
        "こんにちは、先生、今日はいい天気ですね。" (is_first_chunk=True, min_chars=6)
        -> ["こんにちは、", "先生、今日はいい天気ですね。"]
        "そうですね、私もそう思いますよ。" (is_first_chunk=True, min_chars=6)
        -> ["そうですね、私もそう思いますよ。"] (preserved together; particle 'ね' not cut)
    """
    if not text:
        return []

    if not is_first_chunk:
        # Match contiguous segments of non-punctuation followed by punctuation markers and optional closing brackets/quotes
        matches = _RE_NON_FIRST_SENTENCES.findall(text)
        sentences = [m.strip() for m in matches if m.strip()]
        if not sentences and text.strip():
            return [text.strip()]
        return sentences

    # Agile first-chunk mode:
    # Scan for the first split boundary:
    # Either terminal punctuation [。！？!?\n] OR clause pause [、，,] if accumulated chars >= min_chars
    # and the preceding clause does not end with a modal particle (语气词).
    terminal_punct = set("。！？!?\n")
    clause_punct = set("、，,")

    def _emit_first_and_remainder(first_chunk: str, split_idx: int) -> list[str]:
        rem_text = text[split_idx:]
        subsequent = split_japanese_sentences(rem_text, is_first_chunk=False) if rem_text.strip() else []
        return [first_chunk] + subsequent

    curr = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        curr.append(c)
        if c in terminal_punct:
            while i + 1 < n and (text[i + 1] in terminal_punct or text[i + 1] in CLOSING_BRACKETS):
                i += 1
                curr.append(text[i])
            first_sent = "".join(curr).strip()
            if first_sent:
                return _emit_first_and_remainder(first_sent, i + 1)
        elif c in clause_punct:
            while i + 1 < n and (text[i + 1] in clause_punct or text[i + 1] in CLOSING_BRACKETS):
                i += 1
                curr.append(text[i])
            cand = "".join(curr).strip()
            clause = _RE_CLAUSE_TRAILING_PUNCT.sub('', cand)
            if len(cand) >= min_chars and is_natural_clause_boundary(clause):
                return _emit_first_and_remainder(cand, i + 1)
        i += 1

    remaining = "".join(curr).strip()
    if remaining:
        return [remaining]
    return []


__all__ = [
    "split_japanese_sentences",
    "normalize_dialogue_prosody",
    "MODAL_PARTICLES_PATTERN",
    "GREETING_PREFIX_PATTERN",
    "SHORT_INTERJECTIONS_PATTERN",
    "is_short_salutation",
    "is_natural_clause_boundary",
]
