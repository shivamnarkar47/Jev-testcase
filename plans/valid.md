# Goal: Add rate-limiting to MiniURL-shortener POST /shorten

## Steps
1. Add `express-rate-limit` to `package.json`, run `npm install`.
2. In `src/app.js`, mount limiter (100 req/15min/IP) on `POST /shorten` only.
3. Add test `tests/ratelimit.test.js`: 101 rapid POSTs, expect 429 on 101st.
4. Run `npm test` and `npm run lint`, paste output.
5. Rollback: revert commit `abc123` if error rate >1% in 1h.

## Verification
- `curl` 3 shortens works, 4th burst throttled in test.
- No change to `GET /:id` behavior.
