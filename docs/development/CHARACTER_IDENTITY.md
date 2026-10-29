# Character identity and profile reconciliation

Character lookup affects portraits, emotion audio, system prompts, TTS voice
resolution, Telegram character switching and the REST switch route. All these
consumers use `CharacterManager.get_character` and the matching rules in
`services/character_identity.py`.

## Identity rules

- Package ID is authoritative, followed by the display name and declared aliases.
- Valid character packages take precedence over invalid or portrait-only folders
  that declare the same display name or alias; an invalid package is only returned
  as a fallback when no valid package claims that identity.
- Unicode compatibility normalization, case folding and whitespace normalization
  allow full-width parentheses and spaced names without substring matching.
- A legacy bilingual label such as `安娜 (Anna)` is accepted only when its known
  parts identify the same package. Conflicting parts or shared aliases are ambiguous.
- Duplicate normalized package IDs reject the new scan. The previously published
  catalog remains available. A missing directory can publish an empty catalog.
- SQL `%` and `_` are literal name characters, never matching patterns.

## Safe synchronization

For each valid package, consider only database profiles that the same catalog
attributes to that package. Prefer an exact normalized display name, then a
declared alias or ID, then a compatible bilingual label. Equally ranked profiles
remain unchanged; the scan logs the conflicting name, and name-based switching
returns HTTP 409 with guidance to rename the profiles. Switching by numeric profile
ID remains available.

All inserts and updates from one synchronization run share the existing immediate
transaction. A failed write rolls back the run. Repeated scans avoid writes when
the resulting field values are unchanged.

Rescanning never deletes a database profile. A missing package directory, unusual
name or unavailable drive does not establish that a configuration is disposable.
Users can delete unused profiles explicitly through the existing profile API.

Model existence uses the engine's path bases, including the configured project
root, instead of the process working directory or a drive-letter blacklist.
Reference ownership uses the actual package directory, which may differ from its
ID or translated display name. Shared audio outside the character directory is
not assigned to a character merely because its filename contains a name.
File checks for an existing reference never search unrelated packages for the
same filename. Filesystem work during profile updates runs in a worker thread.
Package-relative custom audio becomes a qualified project-relative path while
retaining its transcript. Runtime legacy searches return no file when multiple
packages contain different matches, allowing the caller to use its known package
or present the existing missing-reference guidance.

## Startup responsibilities

Database initialization only makes existing absolute in-project audio paths
relative. It preserves missing paths and valid external audio. Character package
synchronization repairs missing references after a unique identity match and
updates the reference transcript/language together. Without an identified package,
the original path remains available for diagnosis and manual repair; startup must
not substitute the default character's voice.

Schema version and stored fields are unchanged. A package rename that changes its
display name should declare the old name in `aliases` to retain the existing
profile. A persistent package-to-profile binding would be a separate migration;
these rules do not claim to provide one.

## Verification

`tests/test_character_identity.py` uses synthetic packages, temporary SQLite
databases and generated silent reference WAVs. It covers similar names, literal
wildcards, alias collisions, bilingual names, duplicate IDs, preservation of user
configuration, non-project working directories, rescan idempotence, transaction
rollback, startup behavior and the switch lookup. It needs no installed character
assets, GPU, speech engine or external service. Asset-dependent tests remain a
separate check for installations that have those resources.
