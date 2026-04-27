# social-cross-post

Cross-post a published article to the platforms an author has authorised,
via that author's chromium profile.

## Inputs

- `slug` — author slug; must resolve in `/lordship/authors/`.
- `article_url` — the live URL of the published article.
- `platforms` — list. Each must be a key under `pr.social` in the
  author yaml AND have a non-null `credEnvKey` AND that env var must
  resolve to non-empty inside the sandbox.
- `caption` — optional override; defaults to a per-platform template
  the lordship binding declares.
- `run_id` — for the run ledger.

## Behaviour

1. `web_fetch` the `article_url` to confirm it's live (HTTP 200,
   non-empty body). On failure → `gate-failure: article-unreachable`.
2. For each platform in `platforms`:
   1. Call `browser-profile-switch` with the slug.
   2. Start a browser session with the returned launch args.
   3. Navigate to the platform's compose / post UI.
   4. Compose: caption (or template), article_url as a link card,
      and the article's heroImage as the post media.
   5. Submit. Capture the resulting post id and live URL.
   6. Record `{ posted: true, post_id, url, at }` in results.
   7. End the browser session (cookies persist via the bind).
3. Failures are isolated per platform; one platform's
   `posted: false, error: ...` does not abort the others.

## Outputs

- `results` — dict keyed by platform; per-platform record (see
  outbox shape in `AGENTS.md`).
- `status` — `complete` if every platform posted, `partial` if some
  failed, `gate-failure` if no platform was reachable at all.

## Failure modes

- `gate-failure: article-unreachable` — `article_url` didn't return
  HTTP 200.
- `gate-failure: missing-cred:<env-key>` — credEnvKey unset or empty.
- `gate-failure: not-authorised:<platform>` — platform not in
  `pr.social` or `credEnvKey` is null.
- `error: login-expired:<platform>` — chromium profile lost its
  session; surfaced per-platform, run continues for others.

## Notes

- One post per platform per call. No threads, no retries within the
  skill — the lord re-dispatches if needed.
- Never invents engagement. The skill returns whatever the platform
  shows.
- Implementation lives later (Playwright + per-platform shims).
