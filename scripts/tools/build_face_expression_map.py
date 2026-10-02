"""Parse galgame stand CG face-differential metadata into an emotion map.

Reads the .sinfo dumps of ナツメa/ナツメb to recover what each numbered face
differential (01~60) actually depicts, then decomposes every composite into its
brow/eye/mouth parts so the portrait layer can drift inside one emotion family.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Canonical part names as authored in the sinfo face entries.
# zh = badge label, romaji = human readable stem, emotions = app emotion keys.
LEXICON: dict[str, dict] = {
    "基本表情": {"romaji": "base", "zh": "平常", "emotions": {"gentle": 2}},
    "基本": {"romaji": "base", "zh": "平常", "emotions": {"gentle": 2}},
    "笑顔1": {"romaji": "smile1", "zh": "微笑", "emotions": {"happy": 2, "gentle": 2}},
    "笑顔3": {"romaji": "smile3", "zh": "大笑", "emotions": {"happy": 3}},
    "きょとん": {"romaji": "blank", "zh": "发呆", "emotions": {"normal": 3, "surprised": 1}},
    "驚き": {"romaji": "surprise", "zh": "惊讶", "emotions": {"surprised": 3}},
    "焦る": {"romaji": "fluster", "zh": "慌乱", "emotions": {"shy": 2, "tsundere": 2}},
    "恥ずかしい": {"romaji": "ashamed", "zh": "难为情", "emotions": {"shy": 3}},
    "照れる": {"romaji": "bashful", "zh": "害羞", "emotions": {"shy": 2, "blush": 1}},
    "寂しい": {"romaji": "lonely", "zh": "寂寞", "emotions": {"sad": 3}},
    "考える": {"romaji": "think", "zh": "思考", "emotions": {"thinking": 3}},
    "困った": {"romaji": "trouble", "zh": "为难", "emotions": {"tsundere": 3, "sad": 1}},
    "苦笑い": {"romaji": "wry", "zh": "苦笑", "emotions": {"tsundere": 3}},
    "困り笑い": {"romaji": "wry", "zh": "苦笑", "emotions": {"tsundere": 3}},
    "訝しむ": {"romaji": "doubt", "zh": "怀疑", "emotions": {"cool": 3}},
    "納得する": {"romaji": "concede", "zh": "信服", "emotions": {"thinking": 3}},
    "真剣": {"romaji": "serious", "zh": "认真", "emotions": {"cool": 3}},
    "は？": {"romaji": "dumbfound", "zh": "错愕", "emotions": {"surprised": 3}},
    "悪戯笑顔": {"romaji": "mischief", "zh": "坏笑", "emotions": {"tsundere": 3, "happy": 2}},
    "半眼": {"romaji": "halfopen", "zh": "半睁眼", "emotions": {"cool": 3, "sleepy": 2}},
    "適当に流す": {"romaji": "deflect", "zh": "敷衍", "emotions": {"cool": 3, "tsundere": 2}},
    "頬": {"romaji": "blush", "zh": "脸红", "emotions": {"blush": 3}},
}

# Longest-first so 笑顔1 never loses to 笑顔, and 基本表情 before 基本.
_PART_RE = re.compile(
    "|".join(sorted((re.escape(k) for k in LEXICON), key=len, reverse=True))
)

FACE_LINE = re.compile(r"^face\s+(\d+h?)\s+base\s+(.+?)\s*$")

# The chat pipeline only emits these seven emotions, but the CG set has richer
# faces. Each chat emotion owns the faces whose primary reads as that beat, so a
# reply tagged `happy` can only ever drift across faces that still read as happy.
# 3 = the emotion's own faces, 2 = adjacent, 1 = rare neighbour.
DRIFT_ALIASES: dict[str, dict[str, int]] = {
    "gentle": {"gentle": 3, "normal": 2, "thinking": 1},
    "happy": {"happy": 3},
    "shy": {"shy": 3, "blush": 2, "surprised": 1},
    "tsundere": {"tsundere": 3},
    "cool": {"cool": 3, "sleepy": 2, "thinking": 1},
    "sad": {"sad": 3},
    # This game has no angry differential at all: 焦る/困った/半眼 carry the heat.
    "angry": {"tsundere": 3, "cool": 2},
}

POOL_MAX_SIZE = 18


def build_expression_sets(faces: dict) -> dict:
    """chat emotion -> face ids ranked by how strongly they read as that emotion."""
    sets = {}
    for emotion, aliases in DRIFT_ALIASES.items():
        scored = [
            (face_id, aliases[meta["primary"]])
            for face_id, meta in faces.items()
            if meta["primary"] in aliases
        ]
        scored.sort(key=lambda kv: (-kv[1], int(kv[0])))
        sets[emotion] = [
            {"id": fid, "strength": strength} for fid, strength in scored[:POOL_MAX_SIZE]
        ]
    return sets


def read_sinfo(path: Path) -> list[tuple[str, str]]:
    """Yield (face_id, description) pairs; the dumps carry stray NUL padding."""
    raw = path.read_bytes().replace(b"\x00", b"")
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        m = FACE_LINE.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def decompose(desc: str) -> list[dict]:
    """Split a face description into its named parts with emotion weights."""
    # Drop the authoring prefix ("表情/", "追加/7　") and duplicate variant tags.
    body = desc.split("/", 1)[1] if "/" in desc else desc
    body = re.sub(r"^\d+\s*", "", body)
    parts = []
    for match in _PART_RE.finditer(body):
        name = match.group(0)
        # The authored label names which regions the part drives, e.g.
        # 寂しい眉口 moves brow+mouth while 訝しむ目 only moves the eyes.
        tail = body[match.end(): match.end() + 3]
        region = re.match(r"[眉眼口頬]+", tail)
        region = region.group(0) if region else ""
        regions = len(region) or 1
        entry = LEXICON[name]
        parts.append(
            {
                "jp": name,
                "romaji": entry["romaji"],
                "label": entry["zh"] + region.replace("頬", "脸颊"),
                "regions": regions,
                "emotions": entry["emotions"],
            }
        )
    return parts


def merge(parts: list[dict]) -> tuple[dict, dict]:
    """Collapse part weights into per-emotion strength and per-emotion part count."""
    emotions: dict[str, int] = {}
    for part in parts:
        for key, weight in part["emotions"].items():
            emotions[key] = emotions.get(key, 0) + weight * part["regions"]
    # Neutral substrates must not win a tie: 基本眉目 under a 寂しい口 reads as sad.
    generic = {"gentle": 1, "normal": 1, "blush": 1}
    ranked = sorted(emotions.items(), key=lambda kv: (-kv[1], generic.get(kv[0], 0), kv[0]))
    primary = ranked[0][0] if ranked else "gentle"
    return {"primary": primary, "weights": dict(ranked)}, {
        p["romaji"]: 1 for p in parts
    }


def build(sinfo_paths: list[Path]) -> dict:
    faces: dict[str, dict] = {}
    for path in sinfo_paths:
        for face_id, desc in read_sinfo(path):
            is_blush = face_id.endswith("h")
            stem = face_id[:-1] if is_blush else face_id
            parts = decompose(desc)
            emotion_info, _ = merge(parts)
            record = faces.setdefault(
                stem,
                {
                    "id": stem,
                    "jp": desc,
                    "kind": "extra" if desc.startswith("追加") else "core",
                    "label": "＋".join(p["label"] for p in parts),
                    "parts": parts,
                    **emotion_info,
                    "blush_jp": "",
                },
            )
            if is_blush:
                record["blush_jp"] = desc
                record["has_blush"] = True
            # A face can have two authored variants (32 -> は？/16a, /16b).
            elif len(record["jp"]) < len(desc):
                record["jp"] = desc
    for record in faces.values():
        record.setdefault("has_blush", False)
    return dict(sorted(faces.items(), key=lambda kv: (kv[0], kv[1])))


def contact_sheet(faces: dict, gallery_dir: Path, out_path: Path, cols: int = 6) -> None:
    """Tile cropped head shots so the emotion grouping can be eyeballed."""
    from PIL import Image, ImageDraw

    cell_w, head_h = 200, 300
    rows = -(-len(faces) // cols)
    sheet = Image.new("RGB", (cols * cell_w, rows * (head_h + 22)), "white")
    draw = ImageDraw.Draw(sheet)

    for i, (face_id, meta) in enumerate(faces.items()):
        img_path = gallery_dir / f"{face_id}.png"
        if not img_path.exists():
            continue
        with Image.open(img_path) as im:
            # Head sits in the upper band of the 1120x3459 full-body canvas.
            w, h = im.size
            crop = im.crop((int(w * 0.22), int(h * 0.015), int(w * 0.86), int(h * 0.015) + head_h * 2))
            crop.thumbnail((cell_w, head_h))
            x, y = (i % cols) * cell_w, (i // cols) * (head_h + 22)
            sheet.paste(crop, (x, y), crop)
            draw.text((x + 2, y + head_h + 2), f"{face_id} {meta['primary']}", fill="black")
            draw.text(
                (x + 2, y + head_h + 14),
                " ".join(f"{k}{v}" for k, v in list(meta["weights"].items())[:4]),
                fill="#444",
            )
    sheet.save(out_path)
    print(f"contact sheet -> {out_path} ({sheet.size})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"E:\extracted_fgimage")
    ap.add_argument("--out", default=r"E:\extracted_fgimage\faces_map.json")
    ap.add_argument("--sheet", default=r"E:\extracted_fgimage\faces_sheet.png")
    ap.add_argument("--pose", default="ナツメa")
    ap.add_argument("--skip-sheet", action="store_true")
    args = ap.parse_args()

    src = Path(args.src)
    faces = build([src / f"{args.pose}.sinfo.txt"])
    expression_sets = build_expression_sets(faces)
    Path(args.out).write_text(
        json.dumps(
            {"pose": args.pose, "faces": faces, "expression_sets": expression_sets},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"{len(faces)} faces -> {args.out}")
    core = [f for f in faces.values() if f["kind"] == "core"]
    print(f"  core {len(core)} / extra {len(faces) - len(core)}, "
          f"blush variants {sum(1 for f in faces.values() if f['has_blush'])}")
    for emotion, entries in expression_sets.items():
        ids = " ".join(f"{e['id']}:{e['strength']}" for e in entries)
        print(f"  {emotion:9s} {len(entries):3d}  {ids}")
    covered = {e["id"] for entries in expression_sets.values() for e in entries}
    orphaned = [fid for fid in faces if fid not in covered]
    print(f"  covered {len(covered)}/{len(faces)}, unused {orphaned or '-'}")

    if not args.skip_sheet:
        contact_sheet(faces, src / "gallery" / args.pose / "ウェイトレス" / "diff1", Path(args.sheet))


if __name__ == "__main__":
    main()
