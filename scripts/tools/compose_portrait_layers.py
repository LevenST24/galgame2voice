"""Export registered portrait sprites from original layer coordinates.

Pillow is an optional asset-authoring dependency; the running app does not need it.
The output must be a new directory so installed character artwork stays intact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in '/\\:'):
        raise ValueError(f"Invalid portrait identifier: {value!r}")
    return value


def export_portraits(coordinates: dict, plan: dict, slices: Path, output: Path, height: int = 1080) -> dict:
    """Preserve each layer's origin, then resize the completed shared frame once."""
    from PIL import Image

    if height < 1:
        raise ValueError("Sprite height must be positive")
    if output.exists():
        raise ValueError("Output already exists; use a new directory and review before installing")
    jobs = []
    provenance = {"version": 1, "height": height, "costumes": {}, "source_sha256": {}}
    loaded = {}
    seen = set()
    # Preflight every requested layer before producing any output.
    for costume in plan["costumes"]:
        cid = _identifier(costume["id"])
        if cid in seen:
            raise ValueError(f"Duplicate costume: {cid}")
        seen.add(cid)
        pose = _identifier(costume["pose"])
        layers = {entry["layer_id"]: entry for entry in coordinates[pose]["layers"]}
        frame = layers[costume["frame_layer"]]["hd"]
        width = max(1, int(frame["width"] * height / frame["height"]))
        indexed = {}
        for face, ids in costume["sprites"].items():
            _identifier(face)
            if not ids or len(ids) != len(set(ids)):
                raise ValueError(f"Empty or duplicate layer stack: {cid}/{face}")
            stack = sorted((layers[lid] for lid in ids), key=lambda entry: entry["z_order"])
            for layer in stack:
                rect = layer["hd"]
                if (rect["left"] < frame["left"] or rect["top"] < frame["top"]
                        or rect["left"] + rect["width"] > frame["left"] + frame["width"]
                        or rect["top"] + rect["height"] > frame["top"] + frame["height"]):
                    raise ValueError(f"Layer exceeds shared frame: {cid}/{face}/{layer['layer_id']}")
                key = (pose, layer["layer_id"])
                if key not in loaded:
                    path = slices / f"{pose}_{layer['layer_id']}.png"
                    if not path.resolve().is_relative_to(slices.resolve()):
                        raise ValueError("Layer path leaves source directory")
                    with Image.open(path) as image:
                        if image.size != (rect["width"], rect["height"]):
                            raise ValueError(f"Layer size differs from HD coordinates: {path.name}")
                        loaded[key] = image.convert("RGBA")
                    provenance["source_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            jobs.append((cid, face, pose, frame, width, stack))
            indexed[face] = {"file": f"{face}.png", "width": width, "height": height,
                             "layers": [{"id": entry["layer_id"], "left": entry["hd"]["left"],
                                         "top": entry["hd"]["top"]} for entry in stack]}
        provenance["costumes"][cid] = {"pose": pose, "frame": frame, "sprites": indexed}
    if not jobs:
        raise ValueError("No portrait sprites requested")

    output.mkdir(parents=True)
    for cid, face, pose, frame, width, stack in jobs:
        canvas = Image.new("RGBA", (frame["width"], frame["height"]))
        for layer in stack:
            rect = layer["hd"]
            canvas.alpha_composite(loaded[(pose, layer["layer_id"])],
                                   dest=(rect["left"] - frame["left"], rect["top"] - frame["top"]))
        folder = output / cid
        folder.mkdir(exist_ok=True)
        canvas.resize((width, height), Image.Resampling.LANCZOS).save(folder / f"{face}.png")
    (output / "composition.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return provenance


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinates", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--slices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args()
    result = export_portraits(json.loads(args.coordinates.read_text(encoding="utf-8-sig")),
                             json.loads(args.plan.read_text(encoding="utf-8-sig")),
                             args.slices, args.output, args.height)
    print(f"Exported {sum(len(c['sprites']) for c in result['costumes'].values())} registered portraits to {args.output}")


if __name__ == "__main__":
    main()
