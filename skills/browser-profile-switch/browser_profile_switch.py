#!/usr/bin/env python3
"""Resolve an author slug to a per-author chromium profile dir.

See SKILL.md for the contract. Thin helper, but enforces:

- Slug shape: `^[a-z0-9][a-z0-9-]*$`. Anything else fails with
  `invalid-slug` (no path traversal, no uppercase, no dots — the slug
  becomes a directory name under the shared host bind).
- The author yaml at `<authors-dir>/<slug>.yaml` MUST exist. The PR
  agent never browses on behalf of an author the lordship doesn't
  acknowledge.
- `mkdir -p` is idempotent and concurrency-safe (the host-side bind
  is a real directory, not a tmpfs).

Output is a JSON object on stdout with `args`, `user_data_dir`, and
`host_path`. The caller (or wrapping skill) feeds `args` into the
`browser` tool's session-start, or to `playwright.chromium.launch_persistent_context`
as `user_data_dir=`.

Exit codes:
  0  ok
  2  validation error (invalid slug, missing yaml, mkdir failed)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Defaults match the renderer-side bind documented in AGENTS.md
# (`/lordship/shared/browser-state/pr` mounted at `/tmp/openclaw-home`).
DEFAULT_PROFILE_ROOT = "/tmp/openclaw-home"
DEFAULT_HOST_ROOT = "/lordship/shared/browser-state/pr"
DEFAULT_AUTHORS_DIR = "/lordship/authors"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True)
    p.add_argument("--run", default=None,
                   help="run id; included in the ledger entry only")
    p.add_argument("--authors-dir", default=DEFAULT_AUTHORS_DIR,
                   dest="authors_dir")
    p.add_argument("--profile-root", default=DEFAULT_PROFILE_ROOT,
                   dest="profile_root",
                   help="sandbox-side root for per-slug user-data-dirs")
    p.add_argument("--host-root", default=DEFAULT_HOST_ROOT,
                   dest="host_root",
                   help="host-side root the lord can inspect (record only)")
    p.add_argument("--ledger-path", default=None, dest="ledger_path",
                   help="default: /shared/runs/<run>/browser-profile.log")
    return p.parse_args()


def fail(code: int, error: str, **extra) -> None:
    sys.stderr.write(f"browser-profile-switch: {error}\n")
    payload = {"ok": False, "error": error, **extra}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


def main() -> int:
    args = parse_args()

    slug = args.slug
    if not SLUG_RE.match(slug):
        fail(2, "invalid-slug", slug=slug,
             detail="must match ^[a-z0-9][a-z0-9-]*$")

    authors_dir = Path(args.authors_dir)
    yaml_path = authors_dir / f"{slug}.yaml"
    if not yaml_path.is_file():
        fail(2, "invalid-slug", slug=slug,
             yaml_path=str(yaml_path),
             detail="no /lordship/authors/<slug>.yaml for this slug")

    user_data_dir = Path(args.profile_root) / slug
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fail(2, "mkdir-failed",
             user_data_dir=str(user_data_dir),
             detail=(f"{e.__class__.__name__}: {e}; usually means the "
                     "renderer didn't special-case `pr` and the sandbox "
                     "got the per-role bind instead of the shared-root one"))

    chromium_args = [
        f"--user-data-dir={user_data_dir}",
        "--profile-directory=Default",
    ]

    host_path = Path(args.host_root) / slug
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # Best-effort ledger append. Failure here doesn't fail the skill —
    # the switch already happened on disk; the ledger is for after-the-fact
    # auditing.
    if args.run:
        ledger_path = Path(
            args.ledger_path
            or f"/shared/runs/{args.run}/browser-profile.log"
        )
        try:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            entry = json.dumps({
                "slug": slug,
                "user_data_dir": str(user_data_dir),
                "host_path": str(host_path),
                "ts": ts,
            }, ensure_ascii=False)
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except OSError:
            # Ledger best-effort. Don't fail the switch.
            pass

    out = {
        "ok": True,
        "slug": slug,
        "args": chromium_args,
        "user_data_dir": str(user_data_dir),
        "host_path": str(host_path),
        "ts": ts,
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
