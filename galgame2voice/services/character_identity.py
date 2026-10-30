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


def _is_bilingual_label(value: str, known_keys: set[str]) -> bool:
    """Both parenthetical parts are identities of one package."""
    parts = _bilingual_parts(value)
    return len(parts) == 2 and all(part in known_keys for part in parts)


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
        elif _is_bilingual_label(row_name, {primary, *secondary}):
            # An unknown suffix is a different voice, not a partial label.
            rank = 2
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
        if len(parts) != 2:
            return None, False
        # Both sides must name one package. An unknown suffix is not a label,
        # even when the other side matches a package on its own.
        located: list[CharacterPackage | None] = []
        for part in parts:
            found_part: CharacterPackage | None = None
            present = False
            for table in tables:
                if part in table:
                    present = True
                    found_part = table[part]
                    break
            if not present:
                return None, False
            located.append(found_part)
        if any(item is None for item in located):
            return None, True
        if located[0] is located[1]:
            return located[0], False
        return None, True

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


def _profile_id(row: Any) -> int | None:
    if isinstance(row, dict):
        value = row.get("id")
    else:
        try:
            value = row["id"]
        except (KeyError, TypeError, IndexError):
            value = getattr(row, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same_profile(left: Any, right: Any) -> bool:
    left_id = _profile_id(left)
    right_id = _profile_id(right)
    if left_id is not None and right_id is not None:
        return left_id == right_id
    return identity_key(_profile_name(left)) == identity_key(_profile_name(right))


def _manager_resolve(manager: Any, name: str) -> Any | None:
    if hasattr(manager, "resolve_identity"):
        return manager.resolve_identity(name)
    if hasattr(manager, "get_character"):
        return manager.get_character(name)
    return None


def owned_package_for_profile(profile: Any, profiles: Iterable[Any], manager: Any) -> Any | None:
    """Return the package whose profile this row is, or None for a separate voice."""
    name = _profile_name(profile)
    if not name.strip() or manager is None:
        return None
    package = _manager_resolve(manager, name)
    if package is None:
        return None
    aliases = [package.id, *getattr(package.manifest, "aliases", ())]
    candidates = [
        row for row in profiles
        if _manager_resolve(manager, _profile_name(row)) is package
    ]
    try:
        owned = select_profile(candidates, package.name, aliases)
    except AmbiguousProfileName:
        return None
    if owned is None or not _same_profile(owned, profile):
        return None
    return package


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

    def _is_conflicting(name: str) -> bool:
        if hasattr(manager, "is_conflicting_identity"):
            return manager.is_conflicting_identity(name)
        idx = getattr(manager, "_identity_index", None)
        return bool(idx and idx.is_conflicting(name))

    rows = list(profiles)
    query_key = identity_key(clean_query)
    exact = [row for row in rows if identity_key(_profile_name(row)) == query_key]
    if len(exact) > 1:
        raise AmbiguousProfileName(f"角色“{clean_query}”匹配到多个音色配置；请为它们设置不同名称。")
    if len(exact) == 1:
        # A stored profile with this exact name is not replaced by the package.
        return exact[0], owned_package_for_profile(exact[0], rows, manager), False

    pkg = _manager_resolve(manager, clean_query)
    if pkg is None and _is_conflicting(clean_query):
        return None, None, True

    candidates = [
        row for row in rows
        if _manager_resolve(manager, _profile_name(row)) is pkg
        and (pkg is not None or not _is_conflicting(_profile_name(row)))
    ]
    matched = select_profile(
        candidates,
        pkg.name if pkg else clean_query,
        [pkg.id, *pkg.manifest.aliases] if pkg else (),
    )
    return matched, pkg, False
