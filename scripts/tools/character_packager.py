"""
Character Package Tool (validate, pack, install) for Galgame2Voice.

Usage:
    # 1. Validate a character package directory:
    python scripts/tools/character_packager.py validate characters/四季夏目

    # 2. Package a character folder into a .zip archive:
    python scripts/tools/character_packager.py pack characters/四季夏目 --output dist/natsume.zip

    # 3. Install a character package from a .zip archive:
    python scripts/tools/character_packager.py install dist/natsume.zip --target-dir characters/
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from galgame2voice.schemas.character_manifest import (
    validate_character_package,
)

IGNORED_PATTERNS = {
    "__pycache__",
    ".pytest_cache",
    ".DS_Store",
    "Thumbs.db",
    ".git",
    ".gitignore",
}


def should_ignore(path: Path) -> bool:
    for part in path.parts:
        if part in IGNORED_PATTERNS or part.endswith(".pyc") or part.endswith(".tmp"):
            return True
    return False


def cmd_validate(character_dir: Path) -> int:
    char_dir = Path(character_dir).resolve()
    print(f"[*] 正在验证角色包: {char_dir}")
    if not char_dir.exists():
        print(f"[ERROR] 路径不存在: {char_dir}", file=sys.stderr)
        return 1

    is_valid, errors = validate_character_package(char_dir)
    if is_valid:
        manifest_path = char_dir / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = data.get("name", "Unknown")
        emotions_count = len(data.get("emotions", {}))
        print(f"[OK] 角色包验证通过!")
        print(f"     角色名称: {name}")
        print(f"     情感槽位: {emotions_count} 个")
        print(f"     音频时长: 全部符合 [3.0s, 10.0s] 约束")
        return 0
    else:
        print(f"[FAIL] 角色包验证失败，发现 {len(errors)} 处问题:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1


def cmd_pack(character_dir: Path, output_zip: Path, force: bool = False) -> int:
    char_dir = Path(character_dir).resolve()
    if not force:
        ret = cmd_validate(char_dir)
        if ret != 0:
            print("[ERROR] 验证未通过，终止打包 (可加 --force 强制打包)", file=sys.stderr)
            return ret

    out_path = Path(output_zip).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[*] 正在打包到: {out_path} ...")
    file_count = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(char_dir):
            for file in files:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(char_dir)
                if should_ignore(rel_path):
                    continue
                zf.write(full_path, arcname=str(rel_path))
                file_count += 1

    print(f"[OK] 打包成功! 共包含 {file_count} 个文件 -> {out_path}")
    return 0


def cmd_install(zip_path: Path, target_dir: Path, force: bool = False) -> int:
    src_zip = Path(zip_path).resolve()
    if not src_zip.is_file():
        print(f"[ERROR] ZIP 文件不存在: {src_zip}", file=sys.stderr)
        return 1

    dest_base = Path(target_dir).resolve()
    dest_base.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        print(f"[*] 正在解压临时验证: {src_zip.name} ...")
        with zipfile.ZipFile(src_zip, "r") as zf:
            for member in zf.infolist():
                # Prevent directory traversal attacks (zip slip)
                target = tmp_path / member.filename
                if not target.resolve().is_relative_to(tmp_path.resolve()):
                    print(f"[ERROR] 非法路径解压攻击防御: {member.filename}", file=sys.stderr)
                    return 1
            zf.extractall(tmp_path)

        manifest_path = tmp_path / "manifest.json"
        if not manifest_path.is_file():
            print(f"[ERROR] 压缩包内缺少根目录 manifest.json", file=sys.stderr)
            return 1

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        char_name = data.get("name") or data.get("id") or "unnamed_character"

        if not force:
            is_valid, errors = validate_character_package(tmp_path)
            if not is_valid:
                print(f"[ERROR] 解压包验证失败，终止安装:", file=sys.stderr)
                for err in errors:
                    print(f"  - {err}", file=sys.stderr)
                return 1

        final_dest = dest_base / char_name
        if final_dest.exists():
            print(f"[*] 目标角色目录已存在，正在更新: {final_dest}")
        else:
            final_dest.mkdir(parents=True, exist_ok=True)

        for root, dirs, files in os.walk(tmp_path):
            for file in files:
                src_file = Path(root) / file
                rel_file = src_file.relative_to(tmp_path)
                dest_file = final_dest / rel_file
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)

        print(f"[OK] 角色 '{char_name}' 安装成功 -> {final_dest}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Galgame2Voice 角色包打包与验证工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    val_p = subparsers.add_parser("validate", help="验证角色包目录规范与音频完整性")
    val_p.add_argument("character_dir", type=Path, help="角色包所在目录")

    # pack
    pack_p = subparsers.add_parser("pack", help="将角色目录打包为标准 .zip 文件")
    pack_p.add_argument("character_dir", type=Path, help="角色包所在目录")
    pack_p.add_argument("-o", "--output", type=Path, required=True, help="输出 .zip 路径")
    pack_p.add_argument("--force", action="store_true", help="跳过验证强制打包")

    # install
    inst_p = subparsers.add_parser("install", help="从 .zip 安装角色包到 characters/ 目录")
    inst_p.add_argument("zip_path", type=Path, help="角色包 .zip 文件路径")
    inst_p.add_argument("-t", "--target-dir", type=Path, default=PROJECT_ROOT / "characters", help="安装目标根目录")
    inst_p.add_argument("--force", action="store_true", help="跳过验证强制安装")

    args = parser.parse_args()

    if args.command == "validate":
        return cmd_validate(args.character_dir)
    elif args.command == "pack":
        return cmd_pack(args.character_dir, args.output, force=args.force)
    elif args.command == "install":
        return cmd_install(args.zip_path, args.target_dir, force=args.force)

    return 0


if __name__ == "__main__":
    sys.exit(main())
