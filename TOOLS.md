# TOOLS.md

## Skill-first contract

The PR reads inbox manifests at `/shared/inbox/pr/<run>.yaml`, opens the
target per-author yaml at `/lordship/authors/<slug>.yaml`, switches the
chromium profile to that author's `--user-data-dir`, and either updates
the yaml, posts to a platform, or captures a snapshot.

Don't inline raw `urllib`, `requests`, or platform SDKs for any task one
of the skills below covers. If a skill is missing, write a
`needs-skill: <name>` note in the run's outbox pointer and surface the
gap to the lord.

## PR-owned skills (under this repo's `skills/`)

- `author-yaml-write` — read/merge/write `/lordship/authors/<slug>.yaml`.
  Validates against `control-center/schemas/author.md`, preserves the
  Astro-collection block separately from the `pr:` block, refuses to
  embed secret values (only `credEnvKey` references), bumps
  `pr.updatedAt`.
- `social-cross-post` — given an article URL + author slug + the list
  of platforms, posts to each via the author's chromium profile. One
  post per platform per call; failures isolated per platform.
- `social-profile-snapshot` — captures bio, avatar URL, follower count,
  and last 5 posts per platform into
  `/shared/runs/<run>/snapshots/<slug>/<platform>.json`. Updates
  `pr.social.<platform>.lastSnapshotAt` in the author yaml.
- `browser-profile-switch` — thin helper; given an author slug, returns
  the chromium launch args (`--user-data-dir=/tmp/openclaw-home/<slug>`)
  and idempotently mkdirs the directory. Every browse session in this
  agent goes through this helper before the first navigation.

## Built-in tools (declared in `agent.json::tools.alsoAllow`)

- `browser` — chromium control. Per-author profile switching is
  mandatory; never call `browser` without `browser-profile-switch` first.
- `web_fetch` — read-only HTTP. Used to verify article URLs are live
  before cross-posting.
- `exec` — file ops, mkdirs, idempotent author-yaml moves. Same
  no-compound-shell rules as the other sandboxed agents.
- `image_generate` — generate avatar / OG-card images when the
  manifest asks for one. Output lands in
  `/home/node/.openclaw/media/<rest>`; copy into
  `/shared/runs/<run>/avatars/<slug>/` before referencing.
- `agents_list`, `message` — coordinate with the lord and siblings.

## Tools the PR does NOT use

- `web_search` — research is the researcher's surface; the PR works
  from manifests, not from open-ended search.
- WordPress / Astro repo write surfaces — the publisher owns those.
- Content editorial skills — the writer owns those.

## Per-author chromium profile

Every browse session selects the right `--user-data-dir` via
`browser-profile-switch`. The bind point is single
(`/lordship/shared/browser-state/pr` → `/tmp/openclaw-home`); subdirs
per-slug live underneath. Concurrency assumption: one slug per session.
If the lord wants two slugs in flight at once, it spawns two PR
sandboxes; the agent itself does not multiplex.

## Sandbox runtime contract

See `AGENTS.md` "Sandbox runtime contract" — same rules as the writer
and researcher: no compound shells in `exec`, pre-mkdir run subdirs,
skill docs at `/shared/skills/...`, generated media at
`/home/node/.openclaw/media/`.
