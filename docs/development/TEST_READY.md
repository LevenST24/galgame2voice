# TEST_READY: Galgame2Voice Automated E2E Test Suite

## 1. Test Suite Status Overview

> **The authoritative status of this suite is the CI run, not this document.**
> Do not record pass rates, test counts or "green" claims here by hand: they go
> stale silently, and a reader cannot tell a real result from a remembered one.
> Run the suite (section 2) or read the latest CI job to learn the current state.

- **Framework**: `pytest` + `pytest-asyncio` + `httpx` + `aiosqlite`
- **Primary Test Artifact**: the tests collected by `python -m pytest`
- **Test Strategy Standard**: 4-Tier Opaque-Box Hierarchy (`TEST_INFRA.md`)
- **Environment-dependent tests**: tests that need real media assets, subprocess
  pipes or a writable temp directory are marked and skip with an explicit reason
  instead of failing; see "Host Capability Markers" in `TEST_INFRA.md`.

---

## 2. Test Execution Commands

### Full Automated Regression Suite Run
```powershell
python -m pytest
```

---

## 3. Former industrial hardening index

The industrial hardening file has been deleted. Names of the form `test_f*`,
`test_b*`, `test_pair_*` and `test_scenario_*` are no longer a coverage index
in this document. The behavior lives in each area's dedicated tests; this
document does not re-list them.

---

## 4. Verification Result

This section used to assert a recorded green run. It no longer does, because a
hand-written result cannot be distinguished from a stale one.

For the current outcome:

```powershell
python -m pytest -q -rs
```

`-rs` prints why anything was skipped. The suite is expected to report **zero
unexplained failures**; a `FAIL` is a defect, a `skip` with a capability reason
is this host lacking an OS facility, and a `skip` for missing assets means the
optional character media is not installed.
