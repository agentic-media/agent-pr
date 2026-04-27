# dependencies/control-center.md

Consumes `agentic-media/control-center`. Relevant for the PR:

- `schemas/author.md` — canonical author yaml shape (Astro block +
  `pr:` block). Source of truth this agent's `author-yaml-write` skill
  validates against.
- `schemas/credential-reference.md` — how `credEnvKey` resolves at
  sandbox runtime; the PR never embeds raw secrets.
- `schemas/run.md` — outbox / run ledger shape.
- `principles/handoff-via-scripts.md` — outbox discipline, no ad-hoc
  writes between siblings.
- `principles/author-ids-per-site.md` — why never admin, why pick
  deliberately.
- `skills/orchestration/` — the orchestration skills the lord uses to
  dispatch PR runs.
