"""A console recovery flow for users who cannot start the web interface."""

from galgame2voice.database.backups import list_database_backups, restore_database, validate_backup
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION
from galgame2voice.database.session import get_database_path
from pathlib import Path


def recover_interactively() -> int:
    database = Path(get_database_path())
    print("数据库恢复：请先关闭 Galgame2Voice 的启动窗口。")
    candidates = list_database_backups(database)
    usable = []
    for candidate in candidates:
        try:
            if validate_backup(candidate) > CURRENT_SCHEMA_VERSION:
                continue
        except Exception:
            continue
        usable.append(candidate)
        print(f"  {len(usable)}. {candidate.name}  ({candidate.stat().st_size // 1024} KB)")
    if not usable:
        print("没有可用的数据库备份。原文件未修改，请保留整个 data 目录并反馈启动日志。")
        return 1
    choice = input("请输入要恢复的备份序号，直接回车取消：").strip()
    if not choice:
        print("已取消，原文件未修改。")
        return 0
    if not choice.isdecimal() or not 1 <= int(choice) <= len(usable):
        print("序号无效，原文件未修改。请重新运行「数据恢复.bat」。")
        return 1
    print("恢复后配置和后端记录会回到该备份的时间。现有数据库及日志文件会另存，不会删除。")
    if input("确认恢复请输入「恢复」，直接回车取消：").strip() != "恢复":
        print("已取消，原文件未修改。")
        return 0
    selected = usable[int(choice) - 1]
    try:
        preserved = restore_database(database, selected, max_schema_version=CURRENT_SCHEMA_VERSION)
    except Exception as exc:
        print(f"恢复未完成：{exc}")
        print("请检查目录写入权限、磁盘空间，并关闭其他使用数据库的程序后重试。")
        return 1
    print(f"恢复完成。恢复前的原文件已保留在：{preserved}")
    print("关闭此窗口，再双击「启动.bat」即可。")
    return 0
