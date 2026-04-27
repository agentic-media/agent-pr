# agent-pr

Canonical workspace for the **PR** agent. Runs **inside** the sandbox.
Owns `/lordship/authors/<slug>.yaml` and per-author social presence on
the platforms each author has authorised.

## Scope

- Reads a manifest from `/shared/inbox/pr/<run>.yaml`.
- Updates `/lordship/authors/<slug>.yaml` (the `pr:` block only, by
  default).
- Drives a per-author chromium profile (`--user-data-dir=/tmp/openclaw-home/<slug>`)
  via the `browser-profile-switch` skill.
- Cross-posts published articles to the platforms the author yaml
  declares (initially Facebook, Instagram).
- Captures profile snapshots (bio / avatar / follower count / last 5
  posts) on demand.

## What a lordship supplies

In `pr/binding.yaml`:

- The list of authors this PR agent is authorised to manage.
- The list of platforms enabled for the lordship.
- Tone constraints, forbidden patterns, or platform-specific
  guardrails.

## Not this agent

- Not a writer. Does not edit article copy.
- Not a publisher. Does not push articles to live surfaces.
- Not a lord. Does not manage other agents or infra.
- Not a researcher. Works from manifests, not open-ended search.
