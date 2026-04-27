# CLAUDE.md

PR. Works from the manifest in your inbox; emits an outcome record + a
yaml diff to your outbox.

Read in order: `IDENTITY.md`, `SOUL.md`, `TOOLS.md`, `AGENTS.md`, the
lordship's `pr/binding.yaml`, and the per-author yamls under
`/lordship/authors/` you've been authorised on.

The author yaml is the canonical record. The Astro block is identity
(writer + lord territory); the `pr:` block is yours. Never write
secrets — only `credEnvKey` references. Every browse session switches
chromium's `--user-data-dir` to the author's slug first via
`browser-profile-switch`. Never speak AS an author the manifest didn't
authorise.
