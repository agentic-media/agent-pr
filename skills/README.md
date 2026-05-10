# skills/

PR-owned skills. Each skill is a directory with a `SKILL.md` documenting
the contract; implementation (Python, JS, etc.) lands later as the PR
agent comes online.

## Index

- `author-yaml-write/` — read / merge / write
  `/lordship/authors/<slug>.yaml`. Validates against
  `control-center/schemas/author.md`. Preserves the Astro block,
  edits only the `pr:` block by default. Refuses to write secrets;
  only `credEnvKey` references allowed. Implementation:
  `author_yaml_write.py`.
- `lordship-author-pr/` — opens a PR on the lordship repo carrying
  the staged author yaml + WebP asset bundle. Implementation:
  `lordship_author_pr.py`.
- `social-cross-post/` — given an article URL + author slug +
  platforms, posts to each platform via the author's chromium profile.
  Implementation: `social_cross_post.py` (Playwright; per-platform
  shims for facebook + instagram).
- `social-profile-snapshot/` — captures bio / avatar / follower count
  / last 5 posts per platform into a snapshot.json. Updates
  `pr.social.<platform>.lastSnapshotAt` via `author-yaml-write`.
  Implementation: `social_profile_snapshot.py`.
- `browser-profile-switch/` — thin helper; given an author slug,
  returns chromium launch args (`--user-data-dir=/tmp/openclaw-home/<slug>`)
  and idempotent-mkdirs. Implementation:
  `browser_profile_switch.py`.

Shared helpers (manifest validation, profile-switch invocation, per-
platform URL resolution) live in `_pr_common.py` next to the skills.
Not a skill itself; imported by `social-cross-post` and
`social-profile-snapshot`.

## Conventions

- Each skill's SKILL.md begins with a one-line summary, then
  inputs / outputs / failure modes / examples.
- Skills are stateless. Side effects go to `/lordship/authors/`,
  `/shared/runs/<run>/`, or the chromium profile dir.
- Skills never read or write secrets. Resolved env values are
  consumed in-process and never echoed.
