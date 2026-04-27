# author-yaml-write

Read / merge / write `/lordship/authors/<slug>.yaml` safely. The only
skill in the system authorised to mutate that file.

## Inputs

- `slug` — author slug (kebab-case). Must match an existing
  `/lordship/authors/<slug>.yaml`. Creating a new author requires
  `create: true` in the patch and a complete Astro block in the
  initial patch.
- `patch` — dict merged into the file. Defaults target the `pr:`
  block; top-level keys (the Astro block) only mutated when the
  caller passes `target: astro` AND the manifest's `authorised_by`
  is `lord`.
- `run_id` — for the diff path / ledger.

## Behaviour

1. Validate the file exists (or `create: true`).
2. Parse YAML preserving comments and ordering (round-trip yaml).
3. Validate the merged result against
   `control-center/schemas/author.md`. Required: `name`, `slug`
   (matches filename), `bioShort`, `pr.browserProfileDir`,
   `pr.authorisedDomains`, `pr.createdAt`.
4. Refuse to write any value that:
   - sits at a key matching
     `(_)?(API_KEY|TOKEN|PASSWORD|PRIVATE_KEY|SECRET)$`, OR
   - is a high-entropy literal (length >= 24, char-class density
     consistent with a token).
   Only `credEnvKey: "<KEY_NAME>"` references are permitted.
5. Bump `pr.updatedAt` to today (UTC date).
6. Write back atomically (write to `<file>.tmp`, fsync, rename).
7. Emit a unified diff to
   `/shared/runs/<run_id>/author-yaml.diff`.

## Outputs

- `path` — absolute path of the written yaml.
- `diff_path` — path to the unified diff for the lord.
- `astro_block_changed` — bool; `true` only if `target: astro`.

## Failure modes

- `error: file-not-found` — slug doesn't resolve and `create: false`.
- `error: schema-violation` — required field missing.
- `error: secret-leak-attempt` — patch contained a secret-shaped
  value. Skill writes nothing and surfaces the offending key path.
- `error: astro-block-unauthorised` — patch targets the Astro block
  without the lord's authorisation token in the manifest.

## Notes

- The `pr:` block is the only block this skill mutates by default.
  Touching the Astro block is a separate, audited path.
- The agent calls this skill once per `yaml-update` action; never
  partial writes across multiple skill calls.
- Implementation lives later (Python with `ruamel.yaml` for round-trip
  preservation). For now this doc is the contract.
