# Photo Geolocator — make.fun photo-geolocator-01

An agent that guesses where a street-level photo was taken, for the
make.fun bounty **photo-geolocator-01** ($250 USDC, deadline 12 Nov 2026
12:30 WIB). One instance = 25 photos; score = 500,377 − total haversine
error (km) over the photos. Lower error wins.

## How it works (`main.py`, Python 3.13, standard library only)

1. **Reads the photos** from the env (`GET /photos`, then each JPEG),
   in fixed order p01..p25.
2. **Probes the model gateway once per vendor** on the first photo to
   learn which vendor will answer:
   - **Anthropic first** — `claude-opus-5-5` (max_tokens 768), then
     `claude-sonnet-5-5`, then `claude-haiku-5-5`. Opus is the most
     accurate documented vision model on the allowlist (median 181 km).
   - **OpenAI next** — `gpt-6.1-sol`, then `gpt-6-sol`, then
     `gpt-6-luna` (max_tokens 1024 each).
   - A 401 (no key pushed for that vendor) or a vendor that answers no
     model at all stops probing that vendor; a gateway refusal
     (budget / cap / allowlist) is final and is never retried.
3. **One model call per photo** with a structured reasoning prompt
   (country → region/city → precise place, short reasoning, then one
   JSON line `{"lat": ..., "lon": ...}`). Calls run one at a time, in
   photo order, so the live run and its replay make the same calls.
4. **Code-only fallback** if no model is available (or a photo's models
   all fail): a single fixed coordinate, lat 30.25, lon 21.03 — the
   component-wise median of the 75 public practice photos (aggregate
   only; no photo or place table is shipped).
5. **Posts all answers** in one batch, in photo order; on refusal it
   retries in smaller batches, then one by one.

Deterministic given the model's answers: fixed photo order, fixed
model order, no wall clock, no unseeded randomness.

## Files

- `main.py` — the agent (stdlib only, ~19 KB).
- `LICENSE` — MIT.
- `.makefun/owner.json` — the make.fun payout wallet for @gsruw, so
  the repo is accepted even though the GitHub login (giandisruw)
  differs from the make.fun handle.
- `test_trial.sh <trial> [port]` — local end-to-end test: starts the
  kit's env server for one practice trial, runs the agent, stops the
  server, scores with the kit's official scorer.
- `mock_gateway.py <trial> [port]` — a mock model gateway that returns
  a trial's true coordinates, so the model path can be tested without
  a real provider key.
- `photo-geolocator-01-kit/` — the bounty kit (unchanged).

## Verified locally (official scorer, kit practice trials)

| Mode | Trial 0 | Trial 1 | Trial 2 | Mean |
|---|---|---|---|---|
| Code-only fallback | score 263,999 (236,378 km) | 330,238 (170,139 km) | 359,859 (140,518 km) | **318,032 (182,345 km)** |
| Model path (mock gateway) | score **500,377** (0 km) | — | — | — |

- Code-only: 25/25 answers posted on every trial, `working 1`,
  exit 0. Mean score 318,032 matches the pre-computed median-guess
  baseline (182,342 km) to rounding.
- Model path (Anthropic and OpenAI, each tested separately against the
  mock gateway on trial-0): probe detects the vendor, coordinates are
  parsed from the model's reply and posted correctly; the official
  scorer gives the maximum 500,377 (0 km error). **This is a plumbing
  test only** — the mock returns the true coordinates, so it proves the
  request/response/posting works, not that a real model is that
  accurate. With a real key the model path should land far above the
  code-only score (Opus median ~181 km/photo would give roughly
  25 × 181 ≈ 4,500 km total, score ≈ 495,800), but that number is an
  estimate from the bounty rules, not a measured result here.

## What it still needs (before submitting)

1. **A provider key.** No Anthropic/OpenAI key is present in this
   environment, so the entry will run **model-free (code-only)** in
   grading unless a key is pushed as an entry secret first. The model
   path is implemented and tested, but it only runs when the gateway
   can swap the stand-in key for a real one. Gemini is NOT on the
   allowlist and cannot be used. Push the secret from inside the kit:
   `.env` with `ANTHROPIC_API_KEY=<key>` and/or
   `OPENAI_API_KEY=<key>`, then
   `node cli/makefun.mjs secrets push photo-geolocator-01 --env-file .env`
   (never put the key in the repo or on a command line).
2. **A GitHub repo** containing `main.py`, `LICENSE`,
   `.makefun/owner.json` (and optionally this README and the test
   scripts). Do not include the kit directory or any key.
3. **A transcripts bundle** (`makefun bundle`) and a local
   `makefun check`. Note from a sibling entry (news-summary-01,
   submitted 2026-11-12): the site **accepted the submit even though
   local `check` failed the 4 thinking checks**, so a local check
   failure is not a submission blocker — but the transcript gate may
   still matter at grading time, so bundle the session that built
   this entry.
4. **Submit** before 12 Nov 2026 12:30 WIB (no `--keypair` needed;
   this bounty is not escrowed). Max 3 submissions/day, 6 total; the
   last one before the deadline is final.

## Submission commands (from the kit directory)

```sh
# 1. provider secret (only if a real Anthropic/OpenAI key is available)
printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > .env
node cli/makefun.mjs secrets push photo-geolocator-01 --env-file .env

# 2. transcripts bundle (the session that built this entry)
node cli/makefun.mjs bundle --repo <repo-dir-or-url> --out transcripts.tar.gz

# 3. pre-submit check
node cli/makefun.mjs check --kit . --repo <repo> --transcripts transcripts.tar.gz

# 4. submit (repo must be pushed to GitHub first; use the real commit sha)
node cli/makefun.mjs submit --competition photo-geolocator-01 \
  --repo-url https://github.com/giandisruw/<repo> --commit <sha> \
  --title "Photo geolocator" --bundle transcripts.tar.gz
```
