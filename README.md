# Jev Plan Validator

Validate free-text (`.md`) or structured (`.json`) execution plans with [TypeSafe Jev](https://docs.typesafe.ai) (`POST /v1/systemone`) using raw `curl` + `jq` (`validate.sh`) or stdlib-only Python (`validate.py`). No SDK.

Jev judges, bash decides. One API call asks 7–9 parallel questions, then confidence-gated routing in code emits `READY` / `NEEDS_REVISION` / `BLOCKED` / `REVIEW`.

## Questions

| # | Name | Type | Meaning |
|---|------|------|---------|
| 1 | `is_executable` | noul | Concrete ordered steps, no missing deps or imaginary tools |
| 2 | `is_complete` | noul | Covers goal, testable, no major gaps |
| 3 | `is_safe` | noul | No destructive / irreversible / policy-violating actions without safeguards |
| 4 | `completeness` | score 0–4 | Vague → complete + rollback + verification |
| 5 | `risk` | score 0–4 | Safe → critical |
| 6 | `clarity` | score 0–4 | Incoherent → exact commands + expected outputs |
| 7 | `readiness` | choice | `ready` / `needs_revision` / `blocked` + confidence |
| 8 | `tools_grounded` | noul | JSON-only: every step uses allowed tools / real paths |
| 9 | `deps_sound` | noul | JSON-only: deps exist, ordered, no cycles |

`.md` plans send `state` as string (7 Qs). `.json` plans send `state` as object (9 Qs).

## Usage

```bash
export TYPESAFE_API_KEY=...
./validate.sh plans/valid.md
./validate.sh --strict plans/valid.json
./validate.sh --lenient plans/invalid.md

# tuning (env overrides beat presets)
SAFE_MIN=0.95 ./validate.sh plans/invalid.md
JEV_MODEL=jev-latest ./validate.sh plans/valid.md
```

Python port (`validate.py`, stdlib only) mirrors the shell script:

```bash
python3 validate.py plans/valid.md
python3 validate.py --preset strict plans/valid.json
python3 validate.py --dry-run plans/valid.md  # print payload, no network
```

Exit codes (Python): `0` = READY, `1` = NEEDS_REVISION/REVIEW, `2` = BLOCKED.

## Presets

| Preset | `SAFE_MIN` | `EXE/COMP_MIN` | `RISK_BLOCK` | `RISK_READY` | `CONF_MIN` |
|--------|-----------|----------------|--------------|--------------|------------|
| `--strict` | 0.85 | 0.75 | 2.5 | 1.5 | 0.75 |
| `--normal` (default) | 0.7 | 0.6 | 3.0 | 2.0 | 0.6 |
| `--lenient` | 0.5 | 0.4 | 3.5 | 2.5 | 0.4 |

Full overrides: `SAFE_MIN EXE_MIN COMP_MIN CS_MIN CLS_MIN RISK_BLOCK RISK_READY SAFE_READY CONF_MIN`.

## Verdict logic

```
safe < SAFE_MIN or risk >= RISK_BLOCK       -> BLOCKED: unsafe
tools_grounded < EXE_MIN (json)             -> NEEDS_REVISION: hallucinated tools/paths
deps_sound < EXE_MIN (json)                 -> NEEDS_REVISION: broken dependencies
exe/comp < min or completeness/clarity low  -> NEEDS_REVISION: incomplete or unclear
readiness == blocked                        -> BLOCKED: model says blocked
readiness == needs_revision or conf low     -> NEEDS_REVISION: low confidence
readiness == ready + safe high + risk low   -> READY: execute
else                                        -> REVIEW: human check
```

## Examples

* `plans/valid.md` / `plans/valid.json` — rate-limiting plan with tests, verification, rollback. Expect `READY`.
* `plans/invalid.md` / `plans/invalid.json` — vague DB tweak, `DROP TABLE users`, Friday prod deploy, hallucinated tool `magic-optimizer-9000`, dep on `s9`. Expect `BLOCKED` / `NEEDS_REVISION`.

## Requirements

`validate.sh`: `bash`, `curl`, `jq`. `validate.py`: `python3` stdlib only.
