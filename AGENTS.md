# AGENTS.md

## Startup routine

1. `IDENTITY.md`, `SOUL.md`, `TOOLS.md`.
2. From the lordship: `pr/binding.yaml` — authorised authors, authorised
   platforms, lordship-specific tone constraints (if any).
3. From the lordship: `/lordship/authors/*.yaml` — the canonical
   per-author records. The PR is the only agent that writes these.
4. `/shared/inbox/pr/<run>.yaml` — manifest the lord dropped for this run.

## Workspace layout (mounted at `/workspace/` inside the sandbox)

```
/workspace/
├── IDENTITY.md SOUL.md TOOLS.md AGENTS.md
├── skills/                      — author-yaml + social skills
├── config/mcporter.json
├── handoffs/
│   ├── inbox/<run>/             — lord-dropped manifests (mirrors /shared/inbox/pr/)
│   └── outbox/<run>/            — outcome records + snapshot dumps
├── outputs/                     — gitignored
└── memory/                      — gitignored
```

## Manifest contract (inbox)

`/shared/inbox/pr/<run>.yaml`:

```yaml
run_id: <run>
action: yaml-update | snapshot | cross-post
slug: elena-moretti                # author slug; matches /lordship/authors/<slug>.yaml
platforms:                          # required for snapshot + cross-post
  - facebook
  - instagram
article_url: https://insiemesalutetoscana.it/...   # cross-post only
yaml_patch:                         # yaml-update only; merged into pr: block
  social:
    facebook:
      handle: "elena.moretti.benessere"
      pageId: "1234567890"
      credEnvKey: "LORDSHIP_FB_TOKEN_ELENA_MORETTI"
authorised_by: lord
notes: "first cross-post for elena; FB only to start"
```

Validation: `slug` MUST resolve to an existing
`/lordship/authors/<slug>.yaml`. `platforms` MUST appear in
`pr.social.<platform>` with a non-null `credEnvKey` for the requested
action. Mismatch → outbox `status: gate-failure` with the specific gate
that failed; do not act.

## Outbox contract

`/shared/outbox/pr/<run>.yaml`:

```yaml
run_id: <run>
slug: <slug>
action: <as in manifest>
status: complete | gate-failure | partial | blocked
results:
  facebook: { posted: true, post_id: "...", url: "...", at: "2026-04-26T…Z" }
  instagram: { posted: false, error: "login expired", at: "2026-04-26T…Z" }
yaml_updated: true
yaml_diff_path: /shared/runs/<run>/author-yaml.diff   # for the lord to spot-check
snapshots_dir: /shared/runs/<run>/snapshots/<slug>/   # snapshot action only
notes: "anything worth surfacing to the lord"
```

## Per-author chromium profile

The PR multiplexes a single browser bind into N per-author profiles.
The lordship renderer special-cases `pr` so the browser-state bind is
`/lordship/shared/browser-state/pr:/tmp/openclaw-home:rw` (shared
root); per-slug subdirs are created lazily by the agent.

Every browse session — every single one — does this first:

1. Call `browser-profile-switch` with the slug. The skill returns
   chromium launch args including
   `--user-data-dir=/tmp/openclaw-home/<slug>` and ensures the directory
   exists (idempotent `mkdir -p`). Output is logged.
2. Pass those args to the `browser` tool's session-start call. Do NOT
   reuse a browser session across slugs in the same run; if the
   manifest names two slugs (it shouldn't, but guard anyway), each
   gets its own session start.
3. On session end, the cookies / login state persist in
   `/tmp/openclaw-home/<slug>/` and survive sandbox respawn (the host
   bind is `/lordship/shared/browser-state/pr/<slug>/`).

## Author yaml contract

The canonical shape is documented in
`control-center/schemas/author.md`. Two blocks live in the same file:

- The Astro-collection block (top level: `name`, `slug`, `title`,
  `bioShort`, `bio`, `specialization`, `topics`, `writingStyle`,
  `avatar`). The publisher's `astro-github` skill projects this
  verbatim into `src/content/authors/<slug>.json` (PR's `pr:` block
  stripped before projection).
- The `pr:` block (PR-owned): `browserProfileDir`, `avatarSource`,
  `avatarOverrides`, `social.<platform>` (handle, credEnvKey,
  lastSnapshotAt, etc.), `crossPostDefaults`, `authorisedDomains`,
  `createdAt`, `updatedAt`.

When `author-yaml-write` updates the file:

- Reads existing yaml.
- Merges patch into the `pr:` block only (Astro fields untouched
  unless the manifest explicitly targets them — and even then the lord
  must own the change).
- Refuses to write any value that looks like a token (regex
  `(_)?(API_KEY|TOKEN|PASSWORD|PRIVATE_KEY|SECRET)$` on the key OR a
  high-entropy literal in the value); fails with
  `error: secret-leak-attempt`.
- Bumps `pr.updatedAt` to today.
- Writes back, then emits a unified diff to
  `/shared/runs/<run>/author-yaml.diff` so the lord can audit.

## Invariants

- Author slug picked exclusively from `/lordship/authors/<slug>.yaml`.
  A manifest naming an unknown slug → gate-failure.
- Platform allowed only when `pr.social.<platform>.credEnvKey` is set
  AND that env var resolves at sandbox runtime to a non-empty value.
- Every cross-post records the live URL and the platform's post id in
  the outbox; missing either means `partial`, not `complete`.
- Avatar source of truth is the Astro block's `avatar` path. PR
  doesn't change it; if a platform requires a different crop it goes
  in `pr.avatarOverrides.<platform>` (still a path, not a binary).

## Sandbox runtime contract

Same rules as the writer and researcher (see
`agent-writer/AGENTS.md`'s "Sandbox runtime contract"):

1. **`exec` rejects compound shells.** No pipes, no heredocs, no
   `&&` chains. One direct command per call. Use `write` to create
   files, `read` to consume tool output.
2. **Pre-create your run subdirs before writing.** Before the first
   write, do
   `mkdir -p /shared/runs/<run_id>/snapshots/<slug>` and
   `mkdir -p /shared/runs/<run_id>/avatars/<slug>` if the manifest
   needs them.
3. **Skill docs are at `/shared/skills/<category>/<skill>/SKILL.md`.**
   PR-owned skill docs are at `/agent/skills/<skill>/SKILL.md` (the
   stock-repo bind).
4. **Browser state persists across runs but profile is per slug.** Never
   touch `/tmp/openclaw-home/<other-slug>/`. The renderer guarantees
   the dir exists; the agent guarantees the slug match.
5. **No secrets in stdout.** Tool output is captured into the run
   ledger; redact `credEnvKey` resolved values before any `read` on
   the env or any echo.

## Credential dereferencing

The PR reads platform creds from sandbox env at runtime. The lord
injects them at spawn time via the renderer (which reads from
`/lordship/credentials/.env`). The author yaml only carries the env
key NAME; resolution happens here.

References the PR expects (when the lordship has them wired):

- `LORDSHIP_FB_TOKEN_<SLUG_UPPER_SNAKE>` — Facebook page token.
- `LORDSHIP_IG_TOKEN_<SLUG_UPPER_SNAKE>` — Instagram graph token.
- (Future) `LORDSHIP_LI_TOKEN_<SLUG>`, `LORDSHIP_X_TOKEN_<SLUG>`, etc.

If the manifest names a platform whose env key is unset or empty, the
PR emits `gate-failure` with `error: missing-cred:<env-key>` and stops.
