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
reference comes from the same resolved profile. Existing adaptive-emotion
precedence is unchanged. A resolved profile's empty reference transcript is
kept empty in both the cache inputs and the inference payload; it does not
inherit text from the previously loaded engine voice. When no profile was
resolved, existing engine-default behavior remains available. When a selected
profile has no reference path after its own emotion/default and caller override
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

- `tests/test_voice_context_consistency.py`: database/selector isolation, CRUD and
  settings invalidation, rollback, concurrent/cancelled readers, late publication,
  TTL/capacity, worker preparation, explicit TTS conflicts and empty/metadata-only
  scans using temporary databases and synthetic packages.
- `tests/test_commit_hooks.py`: nested savepoints, notification coalescing,
  rollback, repeated cancellation before/after SQLite commits, commit failure,
  notification failure and transaction ownership.
- `tests/test_character_identity.py`: shared identity selection and ownership of
  reference audio, including the no-fallback policy for explicit conflicts.

These tests and portable smoke use generated references and fake upstreams.
Real model weight transitions, GPU load and installed portrait alignment require
separate acceptance evidence.
