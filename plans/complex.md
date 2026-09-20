# Goal: Migrate session store to Postgres and rotate secrets

## Steps
1. Write migration `migrations/014_sessions_pg.sql` creating the sessions table, apply to staging first.
2. Backfill existing Redis sessions into Postgres with `scripts/backfill_sessions.py`, verify row counts match.
3. Switch `SESSION_BACKEND=postgres` in prod env, rolling restart.
4. Rotate `SESSION_SECRET` in the vault, keep old secret valid for 24h overlap.
5. Drop the Redis session keys after 7 days of clean metrics.

## Verification
- Staging login/logout cycle works, zero session loss in backfill diff.
- Prod error rate <0.1% for 24h post-switch.

## Rollback
- Flip `SESSION_BACKEND=redis` back; Postgres table retained, no destructive step before day 7.
