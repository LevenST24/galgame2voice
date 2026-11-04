# Committed voice contexts

The database profile repository owns writes and their cache notifications.
VoiceManager coordinates the speech engine; it does not independently invalidate
database snapshots. Character discovery owns a complete catalog, while profile
reconciliation uses the same repository as explicit profile edits.

## Cache identity and lifetime

`VoiceProfileResolver` keys include the normalized database path, requested
profile ID and the complete trimmed character query. A cached active selection
does not create partial ID/name aliases. Queries with the same missing ID but
different fallback names remain distinct.

The LRU holds at most 256 entries. Positive snapshots expire after 300 seconds;
confirmed absence expires after one second. Read/build errors are not cached as
absence. Concurrent readers of one cold selector share a load; cancelling one
reader releases its wait without cancelling other readers. A committed edit
invalidates both cached selectors and in-progress loads for that database. If an
edit lands during the database read or reference preparation, the resolver
discards the older result and reloads it. Three consecutive invalidations end
with `VoiceContextChangedError` rather than returning an obsolete voice or
falling back to the engine's active voice. Unrelated databases do not restart
the load. Unscoped invalidation affects every in-progress load. Once a snapshot
has already been returned, this does not cancel an in-progress synthesis.

Streamed sentences retain their selected profile ID, but do not copy the turn's
possibly stale reference audio, prompt text or languages into TTS options. TTS
resolves those values and the weights together. Explicit caller overrides and
their legacy aliases remain supported; an omitted prompt for a valid custom
reference comes from the same resolved profile. When canonical `prompt_text`
is an empty string (`""`), it takes precedence over legacy `refer_text` aliases;
when omitted or `None`, alias fallback remains compatible. Existing adaptive-emotion
precedence is unchanged. A resolved profile's empty reference transcript is
kept empty in both the cache inputs and the inference payload; it does not
inherit text from the previously loaded engine voice. When no profile was resolved, existing engine-default behavior remains available.

A reference audio path belongs to an installed character package if and only if
it resolves physically inside the package folder or strictly matches a relative audio path
explicitly declared in the package manifest emotions (`clean == emo_audio`). Heuristic alias
assumptions (such as guessing `refs/{emo_audio}` from `{emo_audio}`) are discarded, and manifest
entries with directory traversal elements (`..`) or absolute paths are rejected from package ownership.
Cross-package classification (`CROSS_PACKAGE`) is strictly constrained to the catalog's set of known
installed character packages (`known_other_packages`); arbitrary or unregistered subdirectories under
`characters/` remain `EXTERNAL_OR_CUSTOM`.

When a package-owned reference audio file is missing on the application host, or when a cross-package
reference is detected, the resolver falls back to the package default (`gentle`), and if missing,
scans the manifest in order for the first alternative emotion audio file existing on disk (such as `joy`).
For cross-package references, if all emotions in the package are missing on disk, the reference is safely
reset to empty strings (`ref_audio = ""`, `prompt_text = ""`) to prevent using reference audio or transcripts
from an unrelated package. This fallback couples the backup audio with its own matching `prompt_text`
and `prompt_lang`, preventing backup audio from inheriting stale or mismatched transcripts.

User-configured external references (including non-manifested relative paths) and caller overrides are
preserved without requiring local filesystem presence when remote, ensuring parity between production
HTTP clients and test stand-ins without `is_mock_client` branching. Explicit local references may be resolved
to absolute paths, and real short audio under 3 seconds continues to trigger fallback. Legitimate empty
reference transcripts (`prompt_text = ""`) are treated as valid overrides that persist idempotently across
catalog rescans. Duration prechecks unify relative and absolute path resolution: local audio files in
unregistered directories are resolved consistently, while unknown remote paths never borrow unrelated
short files from other character packages.

When an authoritative profile context is resolved (`_profile_prompt_resolved=True`), the GPT-SoVITS
payload builder disables fuzzy cross-package package searching (`_search_character_packages_for_audio`).
Existing local files (absolute paths or canonical locations under `project_root`, `audio_dir`, or
`characters_dir`) resolve to absolute filesystem paths. Unknown or external paths (e.g.
`refs/remote_sub/gentle.wav`) are preserved verbatim in `ref_audio_path` without default-package
hijacking. An explicit empty `prompt_text=""` is passed through without inheriting prior engine text.
Legacy unpinned requests retain existing default-package fallback behavior.

In `AudioIdentity` and `SourceFile.capture`, `allow_package_search` is disabled for profile-resolved
contexts. Nonexistent external paths receive distinct canonical paths with missing stat tuples
`(canonical_path, None)`, ensuring different remote paths generate distinct cache keys to prevent cache
collision and audio crosstalk. Modifications to local default-package reference files do not falsely
invalidate cache entries for external reference paths during source validation, while modifications to
existing local audio files continue to trigger invalidation as expected.

When a selected profile has no reference path after its own emotion/default and caller override
have been considered, synthesis reports `VoiceReferenceMissingError` instead
of borrowing the engine's previous reference. Nonempty paths are not rejected
solely because the application host cannot see them: an external engine may
own the file. An internal pre-resolved request with an explicit profile must
also supply a reference; a pre-resolved request without a selector retains the
engine-default path.

A resolved profile must also supply both nonempty GPT and SoVITS model paths
after explicit overrides. Missing paths raise `VoiceModelMissingError` before
engine defaults or cache identities are bound, including for the implicit active
profile. Paths need not exist on the application host when the engine is remote;
requests without a resolved profile may still use the engine's defaults.

`VoiceManager` normalizes the legacy `profile_id` alias, but does not copy
profile fields into synthesis options. The shared resolver selects and binds
the profile once, so an active default cannot override an explicit name and
an old manager snapshot cannot be mixed with newer emotion references.
The standalone streaming API reads its first audio chunk before sending its
HTTP status, allowing selection and missing-reference errors to be returned
as errors. Failures after audio bytes have begun still terminate the stream.
Disconnects cancel and drain the owned generation and response tasks before
closing the generator, including while waiting for the first or a later chunk.

Database reads are asynchronous. Filesystem reference preparation and duration
probes run in a worker thread. Path normalization is performed before lookup;
warm-context measurements do not prove real GPU synthesis latency.

## Commit notifications

`database/commit_hooks.py` registers synchronous callbacks inside the owning
task's `immediate_transaction`. A savepoint rollback discards only callbacks
added within that savepoint. An outer rollback discards its callbacks. Equal
notification keys coalesce once when the outer transaction commits.

The existing uncancellable SQLite operation boundary settles the real commit
before publishing notifications or releasing locks, even if the writer is
cancelled while waiting. A callback failure is logged and cannot roll back a
durable write or prevent other callbacks from running. Registration outside the
current task's transaction is rejected.

Profile create/update/delete, active-profile settings and package reconciliation
use this mechanism. A rescan notifies even if SQL fields are unchanged or the
catalog becomes empty, because emotion references may have changed. Missing
packages do not imply deletion of user-created profiles.

Schema migrations still execute before startup request resolution; this change
does not add columns or raise the schema version. External processes directly
editing SQLite do not issue these callbacks; cache TTL remains their backstop.

## Explicit identity conflicts

The shared character matcher remains authoritative. Conflicting explicit package
or profile names raise `AmbiguousProfileName` during context resolution. TtsService
propagates that configuration error before scheduling synthesis; normal audio
error handling can preserve text and provide a settings/retry path. It must not
silently select the active voice for a conflict. Unspecified voice requests still
use the active/default profile. A missing ID may use an explicitly supplied,
unique fallback name; missing explicit identities never borrow an unrelated active
voice. Chat and TTS share the DB selector; chat error/recovery policy is documented
in `CHAT_SELECTION.md`.
Explicit complete reference options and pre-resolved options retain their bypass.

## Regression coverage

- `tests/test_reference_origin_classification.py`: manifest strict matching (`clean == emo_audio`),
  elimination of refs alias guessing, known-package isolation (`known_other_packages`), unregistered
  directory consistency, emotion fallback cascade, and duration precheck bounds.
- `tests/test_reference_origin_contract.py`: same-database FastAPI and TTS service wiring, HTTP
  profile CRUD parity, fresh connection persistence, idempotent rescan of empty prompt texts, and
  remote path collision counter-examples.
- `tests/test_reference_payload_origin.py`: GPT-SoVITS authoritative payload boundary, preservation
  of remote reference paths against default-package hijacking, and legacy unpinned fallback compatibility.
- `tests/test_reference_cache_origin.py`: cache key distinction for independent remote references,
  elimination of cache crosstalk, and immunity from local default package file changes.
- `tests/test_reference_recovery_contract.py`: reference audio fallback pairing contract,
  preservation of legitimate empty transcripts and valid custom references, explicit
  caller overrides, and preservation of standalone remote reference paths.
- `tests/test_voice_context_consistency.py`: database/selector isolation, CRUD and
  settings invalidation, rollback, concurrent/cancelled readers, late publication,
  TTL/capacity, worker preparation, explicit TTS conflicts and empty/metadata-only
  scans using temporary databases and synthetic packages.
- `tests/test_commit_hooks.py`: nested savepoints, notification coalescing,
  rollback, repeated cancellation before/after SQLite commits, commit failure,
  notification failure and transaction ownership.
- `tests/test_character_identity.py`: shared identity selection and ownership of
  reference audio, including the no-fallback policy for explicit conflicts and cross-package isolation.

These tests and portable smoke use generated references and fake upstreams.
Real model weight transitions, GPU load and installed portrait alignment require
separate acceptance evidence.
