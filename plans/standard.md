# Goal: Add `--dry-run` smoke test for validate.py

## Steps
1. Add `tests/test_validate_dryrun.py` that runs `validate.py --dry-run` on `plans/valid.md` and asserts 7 questions in the payload.
2. Add a CI step in `.github/workflows/ci.yml` running `pytest -q`.
3. Run `pytest -q` and `python3 -m py_compile validate.py route.py` locally, paste output.

## Verification
- CI green on the PR.
- No change to validator behavior (dry-run only, no network).

## Rollback
- Revert the single PR commit.
