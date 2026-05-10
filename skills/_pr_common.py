"""Shared helpers for the PR-agent's browser-driven skills.

Factored out because `social-cross-post` and `social-profile-snapshot`
both need to:

- Load `/lordship/authors/<slug>.yaml` and pull the `pr.social` block.
- Validate a list of requested platforms against the per-author
  authorisation (presence in pr.social and, for posting actions, a
  resolvable credEnvKey in the sandbox env).
- Resolve the per-author chromium profile by shelling out to
  `browser-profile-switch` (the canonical surface — never bypass it).
- Launch a persistent chromium context on that profile.
- Map a platform name to the public profile URL or compose URL.

Per-platform selectors live in this module too; `facebook` and
`instagram` are the platforms the PR currently knows about. Adding
`linkedin` / `x` is a one-place change.

This is NOT itself a skill. It's importable by the skill scripts that
sit next to it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


# ---------------------------------------------------------------------------
# Author yaml loading & platform validation
# ---------------------------------------------------------------------------


class PRError(Exception):
    """Raised by helpers; carries an error code + extras for the caller's
    `fail()` to emit. Caller's exit code is its own concern."""

    def __init__(self, code: str, **extra):
        super().__init__(code)
        self.code = code
        self.extra = extra


@dataclass
class AuthorRecord:
    slug: str
    name: str
    yaml_path: Path
    pr_block: dict
    images_block: dict

    @property
    def social(self) -> dict:
        return self.pr_block.get("social") or {}


def load_author(slug: str, authors_dir: str = "/lordship/authors") -> AuthorRecord:
    """Read /<authors-dir>/<slug>.yaml and return its parsed shape.

    Raises PRError with codes the caller can map to outbox status:
    - `invalid-slug` (file missing) — gate-failure for any action.
    - `slug-mismatch` (yaml's slug doesn't match filename) — schema bug.
    """
    yaml_path = Path(authors_dir) / f"{slug}.yaml"
    if not yaml_path.is_file():
        raise PRError("invalid-slug",
                      slug=slug, yaml_path=str(yaml_path),
                      detail="no author yaml for this slug")
    try:
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PRError("author-yaml-unparseable",
                      slug=slug, yaml_path=str(yaml_path), detail=str(e))
    if doc.get("slug") != slug:
        raise PRError("slug-mismatch",
                      expected=slug, got=doc.get("slug"),
                      yaml_path=str(yaml_path))
    return AuthorRecord(
        slug=slug,
        name=doc.get("name") or slug,
        yaml_path=yaml_path,
        pr_block=doc.get("pr") or {},
        images_block=doc.get("images") or {},
    )


@dataclass
class PlatformCheck:
    platform: str
    ok: bool
    reason: Optional[str] = None  # one of the gate-failure codes when not ok
    handle: Optional[str] = None
    page_id: Optional[str] = None
    cred_env_key: Optional[str] = None
    cred_value_present: bool = False  # NEVER carry the resolved value


def check_platform(
    author: AuthorRecord,
    platform: str,
    require_cred: bool,
) -> PlatformCheck:
    """One-platform gate check.

    `require_cred=True` for posting actions (cross-post). Snapshot
    actions warn but proceed when credEnvKey is null (unauthenticated
    profile reads are a thing on most platforms).
    """
    cfg = author.social.get(platform)
    if not cfg:
        return PlatformCheck(platform=platform, ok=False,
                             reason="not-authorised")
    cred_env_key = cfg.get("credEnvKey")
    handle = cfg.get("handle")
    page_id = cfg.get("pageId")
    if require_cred:
        if not cred_env_key:
            return PlatformCheck(platform=platform, ok=False,
                                 reason="not-authorised",
                                 handle=handle, page_id=page_id)
        cred_value = os.environ.get(cred_env_key, "")
        if not cred_value:
            return PlatformCheck(platform=platform, ok=False,
                                 reason=f"missing-cred:{cred_env_key}",
                                 handle=handle, page_id=page_id,
                                 cred_env_key=cred_env_key)
        return PlatformCheck(platform=platform, ok=True,
                             handle=handle, page_id=page_id,
                             cred_env_key=cred_env_key,
                             cred_value_present=True)
    # snapshot path: cred is optional; proceed regardless.
    cred_value_present = bool(
        cred_env_key and os.environ.get(cred_env_key, "")
    )
    return PlatformCheck(platform=platform, ok=True,
                         handle=handle, page_id=page_id,
                         cred_env_key=cred_env_key,
                         cred_value_present=cred_value_present)


# ---------------------------------------------------------------------------
# browser-profile-switch invocation (the canonical surface)
# ---------------------------------------------------------------------------


def call_browser_profile_switch(
    slug: str,
    run_id: str,
    *,
    authors_dir: str = "/lordship/authors",
    script_path: Optional[str] = None,
) -> dict:
    """Shell out to the sibling skill, return its parsed JSON.

    Defaults the sibling location to ../browser-profile-switch/ relative
    to the caller's parent dir; pass `script_path` if it's elsewhere.
    """
    if script_path is None:
        here = Path(__file__).resolve().parent
        script_path = str(
            here / "browser-profile-switch" / "browser_profile_switch.py"
        )
    proc = subprocess.run(
        [
            sys.executable, script_path,
            "--slug", slug,
            "--run", run_id,
            "--authors-dir", authors_dir,
        ],
        check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise PRError("profile-switch-failed",
                      stderr=proc.stderr.strip(),
                      stdout=proc.stdout.strip())
    if not payload.get("ok"):
        raise PRError("profile-switch-failed",
                      detail=payload.get("error"),
                      extra=payload)
    return payload


# ---------------------------------------------------------------------------
# Per-platform URL resolution
# ---------------------------------------------------------------------------


def public_profile_url(platform: str, handle: Optional[str], page_id: Optional[str]) -> str:
    """Map (platform, handle/pageId) → the public profile URL we read
    a snapshot from. Raises PRError on unknown platform."""
    if platform == "facebook":
        # FB pages are addressed by either the vanity handle or the
        # numeric pageId. Prefer pageId when set (vanity handles can
        # change; pageId is stable).
        ident = page_id or handle
        if not ident:
            raise PRError("profile-not-found:facebook",
                          detail="no handle or pageId in pr.social.facebook")
        return f"https://www.facebook.com/{ident}/"
    if platform == "instagram":
        if not handle:
            raise PRError("profile-not-found:instagram",
                          detail="no handle in pr.social.instagram")
        return f"https://www.instagram.com/{handle}/"
    if platform == "linkedin":
        if not handle:
            raise PRError(f"profile-not-found:{platform}")
        return f"https://www.linkedin.com/in/{handle}/"
    if platform in ("x", "twitter"):
        if not handle:
            raise PRError(f"profile-not-found:{platform}")
        return f"https://x.com/{handle}"
    raise PRError("platform-not-implemented",
                  platform=platform,
                  detail=("no profile-url mapping; add it in "
                          "_pr_common.public_profile_url"))


def compose_url(platform: str, handle: Optional[str], page_id: Optional[str]) -> str:
    """Map a platform to the URL the agent navigates to start a post.

    Raises PRError with `platform-not-implemented` for any platform
    whose compose flow we haven't taught the skill yet — the caller
    surfaces that as a per-platform error and keeps going on the others.
    """
    if platform == "facebook":
        ident = page_id or handle
        if not ident:
            raise PRError("not-authorised",
                          platform="facebook",
                          detail="no pageId/handle for facebook")
        return f"https://www.facebook.com/{ident}/"
    if platform == "instagram":
        # Instagram web "create post" lives behind a button on the
        # logged-in feed. We navigate there and the per-platform shim
        # clicks `Create -> Post`.
        return "https://www.instagram.com/"
    raise PRError("platform-not-implemented",
                  platform=platform,
                  detail=("no compose URL; add it in "
                          "_pr_common.compose_url before adding a shim"))


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------


def run_subdir(run_id: str, *parts: str, shared_root: str = "/shared/runs") -> Path:
    """Build <shared_root>/<run>/<parts...> and ensure it exists.

    `shared_root` defaults to the production `/shared/runs` (mounted by
    the lordship renderer); override for tests / standalone use via
    the env var `PR_SHARED_ROOT` or the explicit kwarg.
    """
    root = os.environ.get("PR_SHARED_ROOT", shared_root)
    p = Path(root) / run_id
    for part in parts:
        p = p / part
    p.mkdir(parents=True, exist_ok=True)
    return p
