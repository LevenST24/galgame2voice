"""
Japanese dialogue sentence boundary splitter for galgame2voice.
Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
Preserves punctuation with the sentence and removes empty segments.
"""

import re
from typing import List

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


def is_natural_clause_boundary(clause: str) -> bool:
    """
    Determines if a clause ending with a comma is a natural pause point suitable for agile first-chunk TTS:
    - Formulaic greetings (e.g. こんにちは, 初めまして, お久しぶりですね) are natural standalone salutations.
    - Modal particles and soft connectors (e.g. ね, よ, わ, な, けど, から, ので, etc.) indicate continuation
      of a thought and should NOT be split to avoid awkward disjoint pauses.
    """
    if not clause:
        return False
    if GREETING_PREFIX_PATTERN.match(clause):
        return True
    return not bool(MODAL_PARTICLES_PATTERN.search(clause))


def normalize_dialogue_prosody(text: str) -> str:
    """
    Normalizes punctuation and prosodic markers in spoken dialogue for natural TTS synthesis:
    - Strips whitespace before punctuation marks
    - Collapses consecutive commas (、、 -> 、)
    - Normalizes awkward ellipsis + comma sequences (……、 -> ……)
    - Normalizes commas immediately preceding terminal punctuation (、。 -> 。)
    - Removes leading commas
    - Normalizes trailing hanging commas at end of utterance to terminal period so TTS pitch finishes naturally
    """
    if not text:
        return ""
    s = text.strip()
    # Strip whitespace around fullwidth CJK punctuation
    s = re.sub(r'\s*([、，。！？…])\s*', r'\1', s)
    # Strip whitespace preceding halfwidth punctuation
    s = re.sub(r'\s+([,.!?])', r'\1', s)
    # Collapse multiple commas
    s = re.sub(r'[、，,]{2,}', '、', s)
    # Collapse ellipsis followed by comma (……、 -> ……)
    s = re.sub(r'(…+|\.{3,})[、，,]+', r'\1', s)
    # Collapse comma before terminal punctuation
    s = re.sub(r'[、，,]+([。！？!?])', r'\1', s)
    # Remove leading comma
    s = re.sub(r'^[、，,]+', '', s)
    # Normalize trailing comma at end of utterance to period so TTS intonation finishes naturally
    s = re.sub(r'[、，,]+$', '。', s)
    return s.strip()


def split_japanese_sentences(
    text: str,
    is_first_chunk: bool = False,
    min_chars: int = 6,
) -> List[str]:
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
        pattern = r'([^。！？!?\n]+(?:[。！？!?\n]+[」』"\'”’\)）\]】]*|\s*$))'
        matches = re.findall(pattern, text)
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
                rem_text = text[i + 1:]
                subsequent = split_japanese_sentences(rem_text, is_first_chunk=False) if rem_text.strip() else []
                return [first_sent] + subsequent
        elif c in clause_punct:
            while i + 1 < n and (text[i + 1] in clause_punct or text[i + 1] in CLOSING_BRACKETS):
                i += 1
                curr.append(text[i])
            cand = "".join(curr).strip()
            clause = re.sub(r'[、，,\s…\.〜~ー\-」』"\'”’\)）\]】]+$', '', cand)
            if len(cand) >= min_chars and is_natural_clause_boundary(clause):
                rem_text = text[i + 1:]
                subsequent = split_japanese_sentences(rem_text, is_first_chunk=False) if rem_text.strip() else []
                return [cand] + subsequent
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
    "is_natural_clause_boundary",
]
