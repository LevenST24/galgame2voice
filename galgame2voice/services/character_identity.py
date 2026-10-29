"""Exact character identities shared by catalog lookup and profile reconciliation.

Display names and declared aliases may be ambiguous; only an unambiguous match
can select a package. Substrings and SQL wildcard syntax are never identities.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from galgame2voice.services.character_package import CharacterPackage


def identity_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _bilingual_parts(value: str) -> tuple[str, ...]:
    match = re.fullmatch(r"([^()]+)\(([^()]+)\)", identity_key(value))
    return match.groups() if match else ()


class AmbiguousProfileName(ValueError):
    """Several stored profiles match equally well; none may be overwritten."""


class DuplicateCharacterId(ValueError):
    """A scan contains packages with the same normalized ID."""


def _profile_name(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("name", ""))
    try:
        return str(row["name"])
    except (KeyError, TypeError):
        return str(getattr(row, "name", ""))


def select_profile(rows: Iterable[Any], name: str, aliases: Iterable[str]) -> Any | None:
    """Prefer an exact name, then an exact alias, then legacy bilingual labels."""
    primary = identity_key(name)
    secondary = {identity_key(alias) for alias in aliases if alias}
    matches: list[tuple[int, Any]] = []
    for row in rows:
        row_name = _profile_name(row)
        key = identity_key(row_name)
        if key == primary:
            rank = 0
        elif key in secondary:
            rank = 1
        else:
            parts = _bilingual_parts(row_name)
            if primary in parts:
                rank = 2
            elif secondary.intersection(parts):
                rank = 3
            else:
                continue
        matches.append((rank, row))
    if not matches:
        return None
    best = min(rank for rank, _ in matches)
    candidates = [row for rank, row in matches if rank == best]
    if len(candidates) != 1:
        raise AmbiguousProfileName(f"角色“{name}”匹配到多个音色配置；请为它们设置不同名称。")
    return candidates[0]


class CharacterIndex:
    """A complete catalog snapshot. IDs outrank names, which outrank aliases."""

    def __init__(self, packages: Iterable[CharacterPackage] = ()) -> None:
        pkg_list = list(packages)
        seen_ids: set[str] = set()
        for package in pkg_list:
            key = identity_key(package.id)
            if key in seen_ids:
                raise DuplicateCharacterId(f"角色包 ID 重复：{package.id}；请修改 manifest.json 中的 id，然后重新扫描角色包。")
            seen_ids.add(key)

        self._valid_tables = self._build_tables(
            pkg for pkg in pkg_list if getattr(pkg, "is_valid", True)
        )
        self._invalid_tables = self._build_tables(
            pkg for pkg in pkg_list if not getattr(pkg, "is_valid", True)
        )

    @classmethod
    def _build_tables(
        cls,
        packages: Iterable[CharacterPackage],
    ) -> tuple[
        dict[str, CharacterPackage | None],
        dict[str, CharacterPackage | None],
        dict[str, CharacterPackage | None],
    ]:
        ids: dict[str, CharacterPackage | None] = {}
        names: dict[str, CharacterPackage | None] = {}
        aliases: dict[str, CharacterPackage | None] = {}
        for package in packages:
            ids[identity_key(package.id)] = package
            cls._register(names, package.name, package)
            for alias in package.manifest.aliases:
                cls._register(aliases, alias, package)
        return ids, names, aliases

    @staticmethod
    def _register(index: dict[str, CharacterPackage | None], value: str, package: CharacterPackage) -> None:
        key = identity_key(value)
        if not key:
            return
        if key not in index or index[key] is package:
            index[key] = package
        else:
            index[key] = None

    @staticmethod
    def _lookup_tables(
        tables: tuple[dict[str, CharacterPackage | None], ...],
        query: str,
    ) -> tuple[CharacterPackage | None, bool]:
        key = identity_key(query)
        if not key:
            return None, False
        for table in tables:
            if key in table:
                found = table[key]
                return found, found is None
        parts = _bilingual_parts(query)
        if not parts:
            return None, False
        candidates: set[CharacterPackage] = set()
        for part in parts:
            for table in tables:
                if part in table:
                    found = table[part]
                    if found is None:
                        return None, True
                    candidates.add(found)
                    break
        if len(candidates) == 1:
            return candidates.pop(), False
        return None, len(candidates) > 1

    def _lookup(self, query: str) -> tuple[CharacterPackage | None, bool]:
        pkg, conflicting = self._lookup_tables(self._valid_tables, query)
        if pkg is not None or conflicting:
            return pkg, conflicting
        return self._lookup_tables(self._invalid_tables, query)

    def is_conflicting(self, query: str) -> bool:
        _, conflicting = self._lookup(query)
        return conflicting

    def resolve(self, query: str) -> CharacterPackage | None:
        pkg, _ = self._lookup(query)
        return pkg


def resolve_profile_for_query(
    profiles: Iterable[Any],
    query: str,
    manager: Any | None = None,
) -> tuple[Any | None, CharacterPackage | None, bool]:
    """Resolves (matched_profile, package, is_conflicting) for a character query."""
    clean_query = (query or "").strip()
    if not clean_query:
        return None, None, False

    if manager is None:
        from galgame2voice.services.character_manager import get_character_manager
        manager = get_character_manager()

    def _resolve_pkg(name: str) -> CharacterPackage | None:
        if hasattr(manager, "resolve_identity"):
            return manager.resolve_identity(name)
        if hasattr(manager, "get_character"):
            return manager.get_character(name)
        return None

    def _is_conflicting(name: str) -> bool:
        if hasattr(manager, "is_conflicting_identity"):
            return manager.is_conflicting_identity(name)
        idx = getattr(manager, "_identity_index", None)
        return bool(idx and idx.is_conflicting(name))

    pkg = _resolve_pkg(clean_query)
    if pkg is None and _is_conflicting(clean_query):
        return None, None, True

    candidates = [
        p for p in profiles
        if _resolve_pkg(_profile_name(p)) is pkg
        and (pkg is not None or not _is_conflicting(_profile_name(p)))
    ]
    matched = select_profile(
        candidates,
        pkg.name if pkg else clean_query,
        [pkg.id, *pkg.manifest.aliases] if pkg else (),
    )
    return matched, pkg, False
