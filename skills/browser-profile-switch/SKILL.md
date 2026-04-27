# browser-profile-switch

Thin helper. Given an author slug, returns the chromium launch args
that pin the session to that author's per-slug user-data-dir, and
idempotently creates the directory.

## Inputs

- `slug` — author slug (kebab-case). Must match
  `/lordship/authors/<slug>.yaml`.

## Behaviour

1. Validate the slug matches `^[a-z0-9][a-z0-9-]*$` and resolves to
   an existing `/lordship/authors/<slug>.yaml`. If neither, fail.
2. `mkdir -p /tmp/openclaw-home/<slug>` (idempotent; safe across
   concurrent calls thanks to the host-side bind).
3. Return the chromium launch arg list:
   ```
   [
     "--user-data-dir=/tmp/openclaw-home/<slug>",
     "--profile-directory=Default"
   ]
   ```
4. Log the switch: `{ slug, dir, ts }` to the run ledger.

## Outputs

- `args` — list of strings to pass to chromium / playwright
  `launch_options`.
- `user_data_dir` — absolute path inside the sandbox.
- `host_path` — absolute path on the host
  (`/lordship/shared/browser-state/pr/<slug>/`), recorded for the
  lord's reference but not used by the agent.

## Failure modes

- `error: invalid-slug` — slug fails the regex or doesn't resolve.
- `error: mkdir-failed` — bind isn't writable; usually means the
  renderer didn't special-case `pr` and the sandbox got the per-role
  bind instead of the shared-root one.

## Notes

- Single bind, N profiles. The lordship renderer is responsible for
  binding `/lordship/shared/browser-state/pr` (the shared root) to
  `/tmp/openclaw-home`; this skill only mkdirs the per-slug subdir.
- Idempotent. Safe to call once per browse session, even mid-run.
- Every PR-agent browse session calls this skill BEFORE the first
  `browser` tool invocation. No exceptions.
- Implementation is shell-thin (a few lines of Python), but the
  contract above is the binding one.
