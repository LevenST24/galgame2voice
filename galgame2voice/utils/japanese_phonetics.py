"""
Japanese phonetics and character name normalizer for galgame2voice TTS synthesis.
Corrects MeCab / OpenJTalk mispronunciations of Galgame proper nouns (names, places, vocatives),
and parses inline Japanese furigana brackets before sending text to GPT-SoVITS.
"""

import re
from typing import Dict, List, Optional, Tuple

# ============================================================================
# Japanese Parentheses Cleaner (Stage Direction Stripper)
# ============================================================================

ENGLISH_STAGE_CUES = re.compile(
    r'^(?:'
    r'giggle|giggles|giggling|'
    r'sigh|sighs|sighing|'
    r'nod|nods|nodding|'
    r'smile|smiles|smiling|'
    r'whisper|whispers|whispering|'
    r'laugh|laughs|laughing|laughter|'
    r'blush|blushes|blushing|'
    r'chuckle|chuckles|chuckling|'
    r'pause|pauses|'
    r'wave|waves|waving|'
    r'wink|winks|winking|'
    r'pout|pouts|pouting|'
    r'shrug|shrugs|shrugging|'
    r'cough|coughs|coughing|'
    r'snicker|snickers|snickering|'
    r'sob|sobs|sobbing|crying|tears|'
    r'clears?\s+throat|'
    r'leaves|enters|'
    r'gasp|gasps|gasping|'
    r'happy|sad|angry|surprised|confused|gentle|tsundere|cool|neutral|excited|shy'
    r')(?::\s*.*)?$',
    re.IGNORECASE
)

STAGE_CUE_ACTION_PATTERN = re.compile(
    r'^(?:'
    r'微笑|笑|ため息|深呼吸|手|目|顔|首|静寂|咳|怒|泣|照れ|赤面|慌て|あわて|囁き|ささやき|小声|大声|早口|'
    r'ためいき|うなず|頷|振り|視線|息|声|口|表情|沈黙|足音|動作|仕草|仕組|ウインク|ウィンク|ジト目|'
    r'元気|落ち着'
    r')|'
    r'(?:心の声|心の叫び|心のつぶやき|独り言|ナレーション|効果音|雷鳴)$',
    re.IGNORECASE
)

STAGE_CUE_MODIFIERS_PATTERN = re.compile(r"(?:ながら|そうに|つつ|顔で|声で|气味に|気味に|風に|する|して|んで|った|よく|いて)$")
STAGE_CUE_EXACT_SET = {
    "汗", "苦笑", "照れ", "溜息", "ため息", "ためいき", "沈黙", "赤面", "困惑", "微笑", "微笑み", "笑顔", "爆笑", "失笑",
    "笑い", "クスクス", "クスクス笑い", "くすくす", "くすくす笑い", "クスッ", "クスッと笑う",
    "驚き", "安堵", "息", "咳", "ウインク", "ウィンク", "ウインクする", "ウィンクする", "手を振る", "手を振って",
    "首を横に振る", "首を振る", "首を傾げる", "首をかしげる", "うなずく", "頷く", "ジト目", "照れ笑い", "苦笑い",
    "吹き出す", "俯く", "うつむく", "微笑む", "微笑んで", "微笑んだ",
    "元気よく", "落ち着いて", "落ち着く", "深呼吸して", "深呼吸する",
    "ふふっ", "ふふ", "あはは", "えへへ", "ドキドキ", "キョロキョロ", "ぺこり", "にっこり", "きょとん", "むすっ"
}

# Comprehensive Chinese Galgame stage cues, expressions, and body language
CHINESE_STAGE_CUE_EXACT_SET = {
    # 呼吸与声音
    "叹气", "叹息", "叹了口气", "长叹一声", "长舒一口气", "深吸一口气", "深呼吸", "深吸气",
    "轻叹", "轻咳", "咳嗽", "干咳", "倒吸一口凉气", "抽泣", "啜泣", "哽咽",
    # 笑容与喜悦
    "微笑", "微微一笑", "轻笑", "浅笑", "抿嘴一笑", "噗嗤一笑", "扑哧一笑", "会心一笑",
    "苦笑", "傻笑", "偷笑", "冷笑", "嗤笑", "大笑", "哈哈大笑", "笑逐颜开", "莞尔一笑",
    "眉开眼笑", "笑容满面", "忍俊不禁", "笑嘻嘻", "笑眯眯", "喜极而泣", "轻声笑",
    # 害羞与神态
    "脸红", "害羞", "羞涩", "羞怯", "红着脸", "满脸通红", "面红耳赤", "低下头", "害羞地低头",
    "别过头", "别过脸", "转过头", "撇开视线", "移开视线", "眼神飘忽", "眼神游移", "眼神闪烁",
    "低头不语", "有些不好意思", "不好意思", "红了脸", "低头",
    # 傲娇与小情绪
    "傲娇", "双手叉腰", "叉腰", "鼓起腮帮子", "鼓嘴", "嘟嘴", "撇嘴", "撇了撇嘴",
    "轻哼", "轻哼一声", "冷哼", "冷哼一声", "哼了一声", "哼", "跺脚", "气鼓鼓", "不服气",
    "别扭", "小声嘀咕", "嘀咕", "喃喃自语", "自言自语", "小声嘟囔", "嘟囔",
    # 肢体动作与体态
    "招手", "摆手", "摆了摆手", "挥手", "挥了挥手", "招了招手", "点头", "点了点头",
    "摇头", "摇了摇头", "耸肩", "耸了耸肩", "眨眼", "眨了眨眼", "眨眨眼", "眨巴眨巴眼睛",
    "握拳", "握紧拳头", "拍拍胸脯", "拍手", "鼓掌", "托腮", "托着下巴",
    "揉眼睛", "揉了揉眼睛", "挠头", "挠了挠头", "歪头", "歪了歪头", "侧过头",
    "凑近", "凑上前", "凑到耳边", "后退", "后退一步", "退后一步", "后退半步", "退后半步",
    # 思考与发呆
    "沉默", "沉思", "沉思片刻", "思考", "若有所思", "愣住", "愣了一下", "愣神", "发呆",
    "回过神来", "回过神", "顿了顿", "停顿", "稍作停顿", "困惑", "不解", "茫然",
}

CHINESE_STAGE_ACTION_PATTERN = re.compile(
    r'^(?:'
    r'叹|微?笑|轻笑|脸红|害羞|低头|抬头|转头|别过|移开|撇|嘟|鼓|叉腰|跺脚|招手|摆手|挥手|'
    r'点头|摇头|眨眼|耸肩|握拳|挠头|歪头|愣|发呆|沉默|思考|沉思|停顿|凑|退|揉|看|望|'
    r'小声|轻声|低语|嘀咕|喃喃|自言自语|轻哼|冷哼|气鼓鼓'
    r')',
    re.IGNORECASE
)

CHINESE_STAGE_MODIFIERS_PATTERN = re.compile(
    r'(?:地|着|起来|了一下|一口气|一声|一番|片刻|瞬间|半天|似的|似地|般地|的样子|的样子说|地说|着说)$'
)

# Mapping stage cue actions to standard emotion archetypes
STAGE_CUE_EMOTION_MAP: Dict[str, str] = {
    # shy
    "脸红": "shy", "害羞": "shy", "羞涩": "shy", "羞怯": "shy", "照れ": "shy", "赤面": "shy",
    "blush": "shy", "blushes": "shy", "blushing": "shy", "面红耳赤": "shy", "满脸通红": "shy",
    "红着脸": "shy", "红了脸": "shy", "移开视线": "shy", "别过脸": "shy", "别过头": "shy",
    # tsundere
    "傲娇": "tsundere", "双手叉腰": "tsundere", "叉腰": "tsundere", "撇嘴": "tsundere",
    "撇了撇嘴": "tsundere", "鼓腮": "tsundere", "鼓起腮帮子": "tsundere", "嘟嘴": "tsundere",
    "轻哼": "tsundere", "冷哼": "tsundere", "哼": "tsundere", "哼了一声": "tsundere", "气鼓鼓": "tsundere",
    "tsundere": "tsundere", "pout": "tsundere", "pouts": "tsundere", "pouting": "tsundere",
    # happy
    "微笑": "happy", "微微一笑": "happy", "轻笑": "happy", "浅笑": "happy", "笑眯眯": "happy",
    "笑嘻嘻": "happy", "大笑": "happy", "哈哈大笑": "happy", "高兴": "happy", "开心": "happy",
    "兴奋": "happy", "眉开眼笑": "happy", "laugh": "happy", "laughs": "happy", "laughing": "happy",
    "chuckle": "happy", "chuckles": "happy", "smile": "happy", "smiles": "happy",
    "giggle": "happy", "giggles": "happy", "giggling": "happy",
    "クスクス": "happy", "くすくす": "happy", "笑顔": "happy", "笑逐颜开": "happy",
    # sad
    "叹气": "sad", "叹息": "sad", "叹了口气": "sad", "抽泣": "sad", "啜泣": "sad", "哽咽": "sad",
    "悲伤": "sad", "难过": "sad", "伤心": "sad", "哭泣": "sad", "ため息": "sad", "溜息": "sad",
    "sigh": "sad", "sighs": "sad", "crying": "sad", "sob": "sad", "sobs": "sad", "tears": "sad",
    # angry
    "生气": "angry", "愤怒": "angry", "跺脚": "angry", "瞪": "angry", "怒": "angry",
    "angry": "angry", "furious": "angry",
    # cool
    "冷淡": "cool", "冷静": "cool", "冷漠": "cool", "平静": "cool", "高冷": "cool",
    "无所谓": "cool", "耸肩": "cool", "shrug": "cool", "shrugs": "cool",
    # gentle
    "温柔": "gentle", "温和": "gentle", "安抚": "gentle", "摸摸头": "gentle", "摸头": "gentle",
    "深呼吸": "gentle", "呼吸": "gentle", "吸了一口气": "gentle", "呼了一口气": "gentle",
    "gentle": "gentle", "soothing": "gentle",
}


def is_spoken_dialogue_inside_brackets(content: str) -> bool:
    """
    Determines if bracketed content is actually spoken dialogue or character thought (which should be voiced)
    rather than a silent stage direction or action cue (which should be stripped).
    Supports English, Japanese, and Chinese stage cues, action verbs, and emotion tags.
    """
    c = re.sub(r"^[（(【\[〖〔*]+|[)）】\]〖〔*]+$", "", content.strip()).strip()
    if not c:
        return False

    # Check English stage direction cues first (e.g. (giggles), (waves), (sigh), (whispers: 'hello'))
    if ENGLISH_STAGE_CUES.match(c):
        return False

    # Bare string without surrounding punctuation / ellipses / whitespace
    c_bare = re.sub(r'[。！？!?….~〜 　\-\*]+', '', c).strip()

    # Strip exact action cue nouns/phrases across Japanese and Chinese
    if c in STAGE_CUE_EXACT_SET or c_bare in STAGE_CUE_EXACT_SET:
        return False
    if c in CHINESE_STAGE_CUE_EXACT_SET or c_bare in CHINESE_STAGE_CUE_EXACT_SET:
        return False

    # Check bracketed emotion tags: e.g. 【傲娇】, (tsundere), 【害羞】, (shy)
    cand_emo = re.sub(r'^(?:情绪|心情|状态|emotion|emo)[:：\s]*', '', c_bare, flags=re.IGNORECASE).strip().lower()
    if cand_emo in STAGE_CUE_EMOTION_MAP or cand_emo in ("gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"):
        return False

    # If it matches common Japanese stage direction action keywords or prefixes
    if (STAGE_CUE_ACTION_PATTERN.search(c) or STAGE_CUE_ACTION_PATTERN.search(c_bare)) and (
        len(c_bare) < 20 and (STAGE_CUE_MODIFIERS_PATTERN.search(c_bare) or "心の声" in c_bare or len(c_bare) <= 8)
    ):
        return False

    # If it matches common Chinese stage direction action verbs and modifier suffixes
    if (CHINESE_STAGE_ACTION_PATTERN.search(c) or CHINESE_STAGE_ACTION_PATTERN.search(c_bare)) and (
        len(c_bare) <= 15 or (len(c_bare) < 25 and CHINESE_STAGE_MODIFIERS_PATTERN.search(c_bare))
    ):
        return False

    if len(c_bare) <= 12 and (STAGE_CUE_MODIFIERS_PATTERN.search(c_bare) or CHINESE_STAGE_MODIFIERS_PATTERN.search(c_bare)):
        return False

    # If it contains dialogue terminal punctuation marks and is not an action cue, it is spoken text
    if re.search(r'[。！？!?…]', c):
        return True

    # Default to True so pure spoken dialogue/thoughts inside parentheses are preserved and voiced
    return True


def clean_japanese_parentheses(text: str, max_passes: int = 5) -> str:
    """
    Strips stage cues and action directions enclosed in fullwidth （...）, ASCII (...),
    lenticular 【...】, square [...], white lenticular 〖...〗, tortoise shell 〔...〕 brackets,
    or Markdown asterisks *...*.
    Preserves actual spoken dialogue text that may be wrapped in parenthetical quotes.
    Applies multi-pass regex sanitization (up to max_passes) to handle nested brackets like （（ため息））.
    """
    if not text:
        return ""

    cleaned = text

    # 1. Clean Markdown action asterisks e.g. *sighs*, *blushes*, *叹气*, *脸红*
    def _replace_asterisk(m: re.Match) -> str:
        inside = m.group(1).strip()
        if is_spoken_dialogue_inside_brackets(inside):
            return m.group(0)
        return ""

    cleaned = re.sub(r'(?<!\*)\*([^*]{1,35})\*(?!\*)', _replace_asterisk, cleaned)

    # 2. Clean parenthetical brackets
    bracket_pairs = [('（', '）'), ('(', ')'), ('【', '】'), ('[', ']'), ('〖', '〗'), ('〔', '〕')]
    for _ in range(max_passes):
        prev = cleaned
        for o, cl in bracket_pairs:
            pattern = re.escape(o) + r'([^' + re.escape(o) + re.escape(cl) + r']*)' + re.escape(cl)

            def _replace_bracket(m: re.Match) -> str:
                inside = m.group(1)
                if is_spoken_dialogue_inside_brackets(inside):
                    return inside
                return ""

            cleaned = re.sub(pattern, _replace_bracket, cleaned)
        if cleaned == prev:
            break

    cleaned = (
        cleaned.replace('（', '').replace('）', '')
        .replace('(', '').replace(')', '')
        .replace('【', '').replace('】', '')
        .replace('[', '').replace(']', '')
        .replace('〖', '').replace('〗', '')
        .replace('〔', '').replace('〕', '')
    )
    return cleaned.strip()


# Alias for cleaning stage directions and action parentheticals
strip_stage_directions = clean_japanese_parentheses


def extract_stage_directions_and_emotion(text: str) -> Tuple[str, Optional[str]]:
    """
    Cleans stage cues and action prompts from text for pure dialogue speech synthesis,
    while simultaneously inferring character emotion ('gentle', 'shy', 'happy', 'tsundere', 'cool', 'sad', 'angry')
    from the stripped action cues.
    Returns (cleaned_text, inferred_emotion_or_none).
    """
    if not text:
        return "", None

    inferred_emotion: Optional[str] = None

    # Scan bracketed contents and asterisks to infer emotion
    bracket_cues = re.findall(r'[（\(\[【〖〔\*]([^）\)\]】〗〕\*]{1,30})[）\)\]】〗〕\*]', text)
    for raw_cue in bracket_cues:
        cue = re.sub(r'^[（(【\[〖〔*]+|[)）】\]〖〔*]+$', '', raw_cue.strip()).strip()
        cue_bare = re.sub(r'[。！？!?….~〜 　\-\*]+', '', cue).strip()
        # Direct match in map
        if cue_bare in STAGE_CUE_EMOTION_MAP:
            inferred_emotion = STAGE_CUE_EMOTION_MAP[cue_bare]
            break
        # Substring match in map
        for k, v in STAGE_CUE_EMOTION_MAP.items():
            if k in cue_bare:
                inferred_emotion = v
                break
        if inferred_emotion:
            break

    cleaned_text = clean_japanese_parentheses(text)
    return cleaned_text, inferred_emotion


# ============================================================================
# Furigana & Proper Name Normalization
# ============================================================================

# Pattern to extract explicit inline furigana: 漢字（ふりがな） or 漢字(ふりがな) -> ふりがな
# e.g., 乃愛（のあ） -> のあ, 李空（りく） -> りく
INLINE_FURIGANA_PATTERN = re.compile(
    r'([一-龥々]+)[（\(]([ぁ-んァ-ヶー]+)[）\)]'
)

# Canonical Galgame character name & vocative phonetic mappings for Yuzusoft titles
# (Tenshi Souzou RE-BOOT!, RIDDLE JOKER, Senren * Banka, Cafe Stella, Sanoba Witch, etc.)
# Note: Longer multi-kanji names must precede shorter substrings to prevent partial collisions.
GALGAME_PHONETIC_REPLACEMENTS: List[Tuple[re.Pattern, str]] = [
    # --- Tenshi Souzou RE-BOOT! (天使☆騒々 RE-BOOT!) ---
    # 白雪乃愛 (Shirayuki Noa) - MeCab erroneously maps 乃愛 to リノ (rino)!
    (re.compile(r'白雪乃[愛爱]'), 'しらゆきのあ'),
    (re.compile(r'乃[愛爱]'), 'のあ'),
    # 谷風天音 (Tanikaze Amane) - MeCab reads 天音 alone as テンオン (ten-on)!
    (re.compile(r'谷[風风]天音'), 'たにかぜあまね'),
    (re.compile(r'天音'), 'あまね'),
    # 谷風李空 (Tanikaze Riku) - MeCab reads 李空 as リイソラ (riisora)!
    (re.compile(r'谷[風风]李空'), 'たにかぜりく'),
    (re.compile(r'李空'), 'りく'),
    # 小雲雀来海 (Kohibari Kurumi) - MeCab reads 来海 as ライウミ (raiumi)!
    (re.compile(r'小[雲云]雀来海'), 'こひばりくるみ'),
    (re.compile(r'小[雲云]雀'), 'こひばり'),
    (re.compile(r'来海'), 'くるみ'),
    # 星河輝耶 (Hoshikawa Kaguya) - MeCab reads 輝耶 as テル (teru)!
    (re.compile(r'星河[輝辉]耶'), 'ほしかわかぐや'),
    (re.compile(r'星河'), 'ほしかわ'),
    (re.compile(r'[輝辉]耶'), 'かぐや'),
    # 高楯オリエ (Takadate Orie) - MeCab reads 高楯 as コーダテ
    (re.compile(r'高楯オリエ'), 'たかだておりえ'),
    (re.compile(r'高楯[欧歐]麗[葉叶]'), 'たかだておりえ'),
    (re.compile(r'高楯'), 'たかだて'),
    # 西園寺風莉 (Saionji Fuuri) - MeCab reads 風莉 as カゼリ (kazeri)
    (re.compile(r'西[園园]寺[風风]莉'), 'さいおんじふうり'),
    (re.compile(r'[風风]莉'), 'ふうり'),
    # 仮屋和奏 (Kariya Wakana) - MeCab reads 和奏 as ワソー (waso)
    (re.compile(r'[仮假]屋和奏'), 'かりやわかな'),
    (re.compile(r'[仮假]屋'), 'かりや'),
    (re.compile(r'和奏'), 'わかな'),

    # --- RIDDLE JOKER ---
    # 在原七海 (Arihara Nanami) - MeCab reads 七海 alone as ナナウミ (nanaumi), 在原 as アリワラ
    (re.compile(r'在原七海'), 'ありはらななみ'),
    (re.compile(r'在原[暁晓]'), 'ありはらさとる'),
    (re.compile(r'在原'), 'ありはら'),
    (re.compile(r'七海'), 'ななみ'),
    # 三司あやせ (Mitsukasa Ayase) - MeCab reads 三司 as サンツカサ (santsukasa)
    (re.compile(r'三司あやせ'), 'みつかさあやせ'),
    (re.compile(r'三司[綾绫][瀬濑]'), 'みつかさあやせ'),
    (re.compile(r'三司'), 'みつかさ'),
    # 式部茉優 (Shikibe Mayu)
    (re.compile(r'式部茉[優优]'), 'しきぶまゆ'),
    (re.compile(r'茉[優优]'), 'まゆ'),
    # 二条院羽月 (Nijoin Hazuki) - MeCab reads 羽月 as ハツキ (hatsuki unvoiced)
    (re.compile(r'二条院羽月'), 'にじょういんはづき'),
    (re.compile(r'二条院'), 'にじょういん'),
    (re.compile(r'羽月'), 'はづき'),
    # 戸隠憧子 (Togakushi Toko) - MeCab reads 憧子 as ショーコ (shoko)
    (re.compile(r'[戸户][隠隐]憧子'), 'とがくしとうこ'),
    (re.compile(r'[戸户][隠隐]'), 'とがくし'),
    (re.compile(r'憧子'), 'とうこ'),

    # --- Senren * Banka (千恋＊万花) ---
    # 朝武芳乃 (Tomotake Yoshino) - MeCab reads 朝武 as アサタケ
    (re.compile(r'朝武芳乃'), 'ともたけよしの'),
    (re.compile(r'朝武'), 'ともたけ'),
    # 常陸茉子 (Hitachi Mako)
    (re.compile(r'常[陸陆]茉子'), 'ひたちまこ'),
    (re.compile(r'常[陸陆]'), 'ひたち'),
    (re.compile(r'茉子'), 'まこ'),
    # 幼刀ムラサメ / 叢雨 (Murasame) - MeCab reads 叢雨 as クサムラウ (kusamurau)
    (re.compile(r'[叢丛]雨'), 'むらさめ'),
    # 鞍馬小春 (Kurama Koharu) - MeCab reads 鞍馬 as アンバ
    (re.compile(r'鞍[馬马]小春'), 'くらまこはる'),
    (re.compile(r'鞍[馬马]'), 'くらま'),

    # --- Cafe Stella and the Reaper's Butterflies (喫茶ステラと死神の蝶) ---
    # 明月栞那 (Mayuzuki Kanna) - MeCab reads 栞那 as シオリアノ (shoriano)
    (re.compile(r'明月栞那'), 'めいげつかんな'),
    (re.compile(r'栞那'), 'かんな'),
    # 四季夏目 (Shiki Natsume)
    (re.compile(r'四季夏目'), 'しきなつめ'),

    # --- Sanoba Witch (サノバウィッチ) ---
    # 綾地寧々 (Ayachi Nene) - MeCab reads 綾地 as アヤジ
    (re.compile(r'[綾绫]地[寧宁][々宁]'), 'あやちねね'),
    (re.compile(r'[綾绫]地'), 'あやち'),
    # 椎葉紬 (Shiiba Tsumugi)
    (re.compile(r'椎[葉叶]紬'), 'しいばつむぎ'),

    # --- DRACU-RIOT! ---
    # 矢来美羽 (Yarai Miu) - MeCab reads 美羽 as ミワ (miwa)!
    (re.compile(r'矢来美羽'), 'やらいみう'),
    (re.compile(r'矢来'), 'やらい'),
    (re.compile(r'(?<![ぁ-んァ-ヶー\w])美羽(?![ぁ-んァ-ヶー\w])'), 'みう'),
    # 布良梓 (Mera Azusa) - MeCab reads 布良 as フリョウ
    (re.compile(r'布良梓'), 'めらあずさ'),
    (re.compile(r'布良'), 'めら'),
    # 稲叢莉音 (Inamura Rion)
    (re.compile(r'稲[叢丛]莉音'), 'いなむらりおん'),
    (re.compile(r'稲[叢丛]'), 'いなむら'),
    (re.compile(r'莉音'), 'りおん'),
    # エリナ・オレゴヴナ・アヴェーン (Erina)
    (re.compile(r'エリナ・オレゴヴナ・アヴェーン'), 'えりな'),
    (re.compile(r'(?i)DRACU-RIOT!?'), 'どらくりおっと'),

    # --- Tenjin Ranman (天神乱漫) ---
    # 千歳佐奈 (Chitose Sana)
    (re.compile(r'千[歳岁]佐奈'), 'ちとせさな'),
    # 竜胆ルリ (Rindou Ruri)
    (re.compile(r'[竜龍]胆ルリ'), 'りんどうるり'),
    (re.compile(r'[竜龍]胆[瑠璃]'), 'りんどうるり'),
    (re.compile(r'[竜龍]胆'), 'りんどう'),

    # --- Series & World Names ---
    (re.compile(r'千恋[＊\*]?万花'), 'せんれんばんか'),
    (re.compile(r'天使[☆\*]?騒々'), 'てんしさわぎ'),
    (re.compile(r'天色アイルノーツ'), 'あまいろあいるのーつ'),
    (re.compile(r'サノバウィッチ'), 'さのばうぃっち'),
    (re.compile(r'喫茶ステラと死神の蝶'), 'きっさてらとしにがみのちょう'),

    # --- Visual Novel Kinship & Honorific Vocatives ---
    # お兄様 (Oniisama) - MeCab reads as オアニサマ (oanisama)!
    (re.compile(r'お兄[様さま]'), 'おにいさま'),
    # お兄 (Onii standalone) - MeCab reads as オアニ (oani)!
    # Matches お兄 followed by punctuation, particles, or line boundary, not by さん/ちゃん/様
    (
        re.compile(
            r'(?<![ぁ-んァ-ヶー\w])お兄(?=[？!！、,\s\.\?…～~っってはがをにともでよし]|って|にして|とか|なら|じゃ|相手|のか|から|まで|ぜ|$)(?!さん|ちゃん|様|さま)'
        ),
        'おにい'
    ),
    # お姉様 (Oneesama) - MeCab reads as オアネサマ (oanesama)!
    (re.compile(r'お姉[様さま]'), 'おねえさま'),
    # お姉 (Onee standalone) - MeCab reads as オアネ (oane)!
    (
        re.compile(
            r'(?<![ぁ-んァ-ヶー\w])お姉(?=[？!！、,\s\.\?…～~っってはがをにともでよし]|って|にして|とか|なら|じゃ|相手|のか|から|まで|ぜ|$)(?!さん|ちゃん|様|さま)'
        ),
        'おねえ'
    ),
    # 義妹 (Gimai) / 義兄 (Gikei)
    (re.compile(r'[義义]妹'), 'ぎまい'),
    (re.compile(r'[義义]兄'), 'ぎけい'),
]


def normalize_japanese_furigana(text: str) -> str:
    """
    Substitutes inline furigana bracket patterns with the target kana pronunciation.
    e.g., "乃愛（のあ）" -> "のあ"
          "李空(りく)" -> "りく"
    """
    if not text:
        return ""
    return INLINE_FURIGANA_PATTERN.sub(r'\2', text)


def normalize_galgame_names_and_readings(text: str) -> str:
    """
    Normalizes Galgame proper names and vocatives to their canonical phonetic kana readings.
    Prevents pyopenjtalk / MeCab IPADic from misreading names (e.g. 乃愛 -> rino, 天音 -> ten-on).
    """
    if not text:
        return ""
    result = text
    for pattern, replacement in GALGAME_PHONETIC_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    return result


def normalize_japanese_for_tts(text: str) -> str:
    """
    Complete pre-synthesis Japanese text normalization pipeline:
    1. Replaces inline furigana brackets 漢字（ふりがな） with phonetic kana.
    2. Strips stage cues and bracketed action directions via clean_japanese_parentheses.
    3. Substitutes known Galgame proper names and vocatives with canonical kana.
    """
    if not text:
        return ""
    # 1. Furigana first before brackets might get touched
    s = normalize_japanese_furigana(text)
    # 2. Stage cues & bracket cleaner
    s = clean_japanese_parentheses(s)
    # 3. Canonical name phonetics
    s = normalize_galgame_names_and_readings(s)
    return s.strip()


__all__ = [
    "ENGLISH_STAGE_CUES",
    "STAGE_CUE_ACTION_PATTERN",
    "STAGE_CUE_MODIFIERS_PATTERN",
    "STAGE_CUE_EXACT_SET",
    "CHINESE_STAGE_CUE_EXACT_SET",
    "CHINESE_STAGE_ACTION_PATTERN",
    "CHINESE_STAGE_MODIFIERS_PATTERN",
    "STAGE_CUE_EMOTION_MAP",
    "is_spoken_dialogue_inside_brackets",
    "clean_japanese_parentheses",
    "extract_stage_directions_and_emotion",
    "normalize_japanese_furigana",
    "normalize_galgame_names_and_readings",
    "normalize_japanese_for_tts",
]
