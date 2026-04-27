# social-profile-snapshot

Capture a per-platform profile snapshot for an author: bio, avatar URL,
follower count, last 5 posts.

## Inputs

- `slug` — author slug; must resolve in `/lordship/authors/`.
- `platforms` — list. Each must be a key under `pr.social` in the
  author yaml. credEnvKey may be null for platforms readable
  unauthenticated; the skill warns but proceeds.
- `run_id` — for output paths.

## Behaviour

1. For each platform in `platforms`:
   1. Call `browser-profile-switch` with the slug.
   2. Start a browser session with the returned launch args.
   3. Navigate to the public profile URL derived from
      `pr.social.<platform>.handle` (or `pageId` for Facebook).
   4. Capture:
      - `bio` — the public bio text.
      - `avatar_url` — direct URL to the platform-side avatar.
      - `follower_count` — integer; null if hidden.
      - `last_posts` — array of up to 5 entries, each
        `{ id, url, posted_at, preview }`.
   5. Write
      `/shared/runs/<run_id>/snapshots/<slug>/<platform>.json`.
   6. End the browser session.
2. Update each platform's `pr.social.<platform>.lastSnapshotAt` in
   `/lordship/authors/<slug>.yaml` via `author-yaml-write`.

## Outputs

- `snapshots_dir` — absolute path under `/shared/runs/<run_id>/snapshots/<slug>/`.
- `per_platform` — dict; per-platform path + summary counts (bio
  length, follower count, post count).
- `yaml_diff_path` — produced by `author-yaml-write` when bumping
  `lastSnapshotAt`.

## Failure modes

- `error: profile-not-found:<platform>` — handle / pageId resolves
  to a platform 404. Snapshot for that platform is skipped; others
  continue.
- `error: rate-limited:<platform>` — platform throttled; partial
  snapshot written if any data was captured before the throttle.
- `error: login-required:<platform>` — public profile gated behind
  login on this platform; needs `credEnvKey`.

## Notes

- Snapshot fields are observed values only. No smoothing, no
  rounding, no annotation.
- Avatar URL is captured as-is; whether to mirror locally is a
  separate, future skill.
- Implementation lives later (Playwright + per-platform parsers).
