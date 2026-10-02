"""Compute per-face head-region visibility deltas and store them in the index.

Same-emotion drift only reads as motion if the two frames actually differ on
screen, so the pairwise head deltas are measured from the cut sprites instead of
guessed from how many facial parts the authored labels share.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# The face-differential layers sit in this vertical band of the full-body canvas.
HEAD_TOP, HEAD_BOTTOM = 0.075, 0.165
SIGNATURE_SIZE = (64, 16)


def head_signature(path: Path):
    from PIL import Image

    with Image.open(path) as im:
        w, h = im.size
        band = im.convert("L").crop((0, int(h * HEAD_TOP), w, int(h * HEAD_BOTTOM)))
        return list(band.resize(SIGNATURE_SIZE, Image.BILINEAR).getdata())


def l1(a, b) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--index",
        default=r"E:\galgame2voice\characters\四季夏目\portrait\expressions.json",
    )
    ap.add_argument("--top", type=int, default=6, help="每个情绪池内保留几个最有视觉反差的邻居")
    args = ap.parse_args()

    root = Path(args.index).parent
    data = json.loads(Path(args.index).read_text(encoding="utf-8"))
    pools = data.get("expression_sets") or {}

    # Signatures are per costume because each outfit is a separate canvas.
    signatures: dict[str, dict[str, list]] = {}
    for costume_id, costume in (data.get("costumes") or {}).items():
        sigs = {}
        for face_id in (costume.get("sprites") or {}):
            if face_id.endswith("h"):
                continue
            path = root / costume_id / f"{face_id}.webp"
            if path.is_file():
                sigs[face_id] = head_signature(path)
        signatures[costume_id] = sigs

    # visibility[costume][emotion] = {face_id: [{id, delta}, ...]} best-first
    visibility = {}
    for costume_id, sigs in signatures.items():
        per_costume = {}
        for emotion, entries in pools.items():
            ids = [e["id"] for e in entries if e["id"] in sigs]
            per_emotion = {}
            for a in ids:
                ranked = sorted(
                    ((b, l1(sigs[a], sigs[b])) for b in ids if b != a),
                    key=lambda kv: -kv[1],
                )
                per_emotion[a] = [
                    {"id": b, "delta": round(d, 4)} for b, d in ranked[:args.top]
                ]
            per_costume[emotion] = per_emotion
        visibility[costume_id] = per_costume
        print(f"[ok] {costume_id}: {len(sigs)} 张脸已量化头部区域")

    data["visibility"] = visibility
    Path(args.index).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Spot-check the head-delta ranking just computed: first costume's tsundere pool,
    # largest vs smallest contrast neighbour.
    sample = next(iter(visibility.values()), {}).get("tsundere", {})
    for fid, neighbours in list(sample.items())[:4]:
        best = neighbours[0] if neighbours else None
        worst = neighbours[-1] if neighbours else None
        print(
            f"  tsundere {fid}: 最大反差 -> {best['id']} ({best['delta']:.1%}) | "
            f"最小反差 -> {worst['id']} ({worst['delta']:.1%})"
        )
    print(f"index -> {args.index}")


if __name__ == "__main__":
    main()
