"""Cut the extracted stand-CG gallery down to web sprites and index them.

Pairs with build_face_expression_map.py: every numbered face differential of a
costume is resized to the app's sprite framing and recorded in the character
package's expressions.json, which the backend serves as the portrait payload.
Everything stays inside the character package (portrait/ + portrait/<costume>/).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SPRITE_HEIGHT = 1080
WEBP_QUALITY = 90

# gallery 服装目录名 -> (角色包内 costumeId, 中文名, 图标)
COSTUMES = {
    "ウェイトレス": ("waitress", "咖啡厅服务生", "☕"),
    "私服": ("homewear", "日常便服", "👕"),
    "メイド服": ("maid", "女仆装", "🎀"),
    "チャイナ服": ("china", "旗袍", "🏮"),
    "チャウター": ("china_coat", "旗袍外套", "🐉"),
    "アウター": ("coat", "外套", "🧥"),
}


def cut_sprites(src_dir: Path, out_dir: Path, face_ids: list[str]) -> dict:
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    sprites = {}
    for face_id in face_ids:
        for variant in (face_id, f"{face_id}h"):
            src = src_dir / f"{variant}.png"
            if not src.exists():
                continue
            dest = out_dir / f"{variant}.webp"
            with Image.open(src) as im:
                w, h = im.size
                # The gallery canvas shares the app sprite's aspect ratio, so a
                # plain scale keeps every frame registered for cross-fading.
                tw = max(1, round(w * SPRITE_HEIGHT / h))
                if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
                    im.resize((tw, SPRITE_HEIGHT), Image.LANCZOS).save(
                        dest, "WEBP", quality=WEBP_QUALITY, method=6
                    )
            sprites[variant] = {"file": dest.name, "width": tw, "height": SPRITE_HEIGHT}
    return sprites


def index_faces(faces_map: dict, sprites: dict) -> dict:
    """Keep only faces this costume actually has; carry the parts for drift."""
    indexed = {}
    for face_id, meta in faces_map["faces"].items():
        if face_id not in sprites:
            continue
        indexed[face_id] = {
            "label": meta["label"],
            "jp": meta["jp"],
            "primary": meta["primary"],
            "weights": meta["weights"],
            "parts": [{"romaji": p["romaji"], "regions": p["regions"]} for p in meta["parts"]],
            "blush": f"{face_id}h" if f"{face_id}h" in sprites else None,
        }
    return indexed


def load_index(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 2, "pose": "", "costumes": {}, "faces": {}, "expression_sets": {}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faces-map", default=r"E:\extracted_fgimage\faces_map.json")
    ap.add_argument("--gallery", default=r"E:\extracted_fgimage\gallery")
    ap.add_argument("--pose", default="ナツメa")
    ap.add_argument("--diff", default="diff1")
    ap.add_argument("--costume", default="all", help=f"gallery 目录名或 all: {', '.join(COSTUMES)}")
    ap.add_argument(
        "--out",
        default=r"E:\galgame2voice\characters\四季夏目\portrait",
        help="角色包 portrait/ 目录",
    )
    ap.add_argument("--default-costume", default="waitress")
    args = ap.parse_args()

    faces_map = json.loads(Path(args.faces_map).read_text(encoding="utf-8"))
    face_ids = list(faces_map["faces"].keys())
    out_root = Path(args.out)
    index = load_index(out_root / "expressions.json")
    index["pose"] = args.pose
    index["expression_sets"] = faces_map["expression_sets"]

    wanted = COSTUMES if args.costume == "all" else {args.costume: COSTUMES[args.costume]}
    for gallery_name, (costume_id, zh_name, icon) in wanted.items():
        src_dir = Path(args.gallery) / args.pose / gallery_name / args.diff
        if not src_dir.is_dir():
            print(f"[skip] 缺目录 {src_dir}")
            continue
        sprites = cut_sprites(src_dir / "", out_root / costume_id, face_ids)
        if not sprites:
            print(f"[skip] {src_dir} 无可用表情切片")
            continue
        index["costumes"][costume_id] = {
            "id": costume_id,
            "name": zh_name,
            "icon": icon,
            "gallery_dir": gallery_name,
            "source": str(src_dir),
            "sprites": sprites,
        }
        index["faces"].update(index_faces(faces_map, sprites))
        print(f"[ok] {gallery_name} -> {costume_id}: {len(sprites)} 张")

    index["default_costume"] = args.default_costume
    (out_root / "expressions.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total_mb = sum(f.stat().st_size for f in out_root.rglob("*.webp")) / 1048576
    print(f"index -> {out_root / 'expressions.json'} | {len(index['costumes'])} 服装, "
          f"{len(index['faces'])} 表情, {total_mb:.1f} MB")


if __name__ == "__main__":
    main()
