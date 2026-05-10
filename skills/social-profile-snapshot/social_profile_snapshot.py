#!/usr/bin/env python3
"""Capture per-platform profile snapshots for an author.

See SKILL.md and ../../AGENTS.md for the contract. For each requested
platform, navigates to the public profile via the per-author chromium
profile and captures:

- `bio`: the public bio text.
- `avatar_url`: the platform-side avatar URL (as-is, no mirroring).
- `follower_count`: integer; null if hidden / not parseable.
- `last_posts`: up to 5 entries `{ id, url, posted_at, preview }`.

Snapshots are written to
`/shared/runs/<run>/snapshots/<slug>/<platform>.json`. Per-platform
failures are isolated and surfaced as `error: ...` records that do
NOT abort the other platforms.

After a successful capture the skill bumps
`pr.social.<platform>.lastSnapshotAt` in the author yaml via a
sibling call to `author-yaml-write` (which stages the yaml under
`/shared/runs/<run>/author-yaml/<slug>.yaml` — the lord opens the PR
via `lordship-author-pr`). If `--no-yaml-bump` is passed (or no
platform succeeded), the yaml step is skipped and `yaml_diff_path`
in the output is null.

Snapshot fields are observed values only — no smoothing, no rounding,
no annotation. If a number isn't visible, it's null.

Exit codes:
  0  ok (status: complete | partial)
  2  manifest gate-failure (slug missing, no platforms passed)
  1  unexpected error before per-platform isolation
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _pr_common import (  # noqa: E402
    PRError,
    call_browser_profile_switch,
    check_platform,
    load_author,
    public_profile_url,
    run_subdir,
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--platform", action="append", default=[],
                   dest="platforms",
                   help="repeatable: --platform facebook --platform instagram")
    p.add_argument("--authors-dir", default="/lordship/authors",
                   dest="authors_dir")
    p.add_argument("--no-yaml-bump", action="store_true",
                   dest="no_yaml_bump",
                   help="skip the author-yaml-write call after capture")
    p.add_argument("--headed", action="store_true",
                   help="show the browser (debug only)")
    return p.parse_args()


def fail(code: int, error: str, **extra) -> None:
    sys.stderr.write(f"social-profile-snapshot: {error}\n")
    payload = {"ok": False, "error": error, **extra}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Number-parsing helpers
# ---------------------------------------------------------------------------


_FOLLOWER_NUM_RE = re.compile(
    r"([\d.,]+)\s*([KMBkmb])?",
)


def parse_follower_count(s: Optional[str]) -> Optional[int]:
    """`12.3K followers` → 12300. `1,234,567` → 1234567. None on failure."""
    if not s:
        return None
    m = _FOLLOWER_NUM_RE.search(s)
    if not m:
        return None
    raw, suffix = m.group(1), m.group(2)
    raw = raw.replace(",", "")
    try:
        n = float(raw)
    except ValueError:
        return None
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    if suffix:
        n *= mult.get(suffix.upper(), 1)
    return int(n)


# ---------------------------------------------------------------------------
# Per-platform snapshot shims
# ---------------------------------------------------------------------------


def _snapshot_facebook(page) -> dict:
    """Best-effort capture of an FB page header + recent posts."""
    out: dict[str, Any] = {
        "bio": None, "avatar_url": None,
        "follower_count": None, "last_posts": [],
    }
    try:
        # Page name + intro live in the page header. FB swaps DOM
        # frequently; we use multiple selector candidates.
        bio_locator = page.locator(
            "div[data-pagelet='ProfileTilesFeed_0'], "
            "div[data-pagelet='page'] div:has-text('Page · ')"
        ).first
        if bio_locator.count() > 0:
            try:
                out["bio"] = bio_locator.inner_text(timeout=4000).strip() or None
            except Exception:
                pass

        # Avatar: the first profile-picture image in the header.
        avatar = page.locator("image[xlink\\:href], svg image, img[alt*='profile']").first
        try:
            href = avatar.get_attribute("xlink:href", timeout=2000) or avatar.get_attribute("src", timeout=2000)
            if href:
                out["avatar_url"] = href
        except Exception:
            pass

        # Follower count: text node containing 'followers' (or 'follower').
        try:
            txt = page.locator("a:has-text('followers'), a:has-text('follower')").first.inner_text(timeout=4000)
            out["follower_count"] = parse_follower_count(txt)
        except Exception:
            pass

        # Recent posts: scrape the first N post permalinks in the timeline.
        try:
            anchors = page.locator("a[href*='/posts/'], a[href*='/permalink/']").all()
            seen = set()
            for a in anchors:
                href = a.get_attribute("href")
                if not href:
                    continue
                # Normalise.
                if href.startswith("/"):
                    href = f"https://www.facebook.com{href}"
                if href in seen:
                    continue
                seen.add(href)
                # Extract post id.
                pid = None
                for marker in ("/posts/", "/permalink/"):
                    if marker in href:
                        tail = href.split(marker, 1)[1]
                        pid = tail.split("/", 1)[0].split("?", 1)[0]
                        break
                # Preview: first 240 chars of the nearest article container.
                preview = None
                try:
                    art = a.locator(
                        "xpath=ancestor::div[@role='article'][1]"
                    ).first
                    if art.count() > 0:
                        preview = (art.inner_text(timeout=2000) or "")[:240].strip() or None
                except Exception:
                    pass
                out["last_posts"].append({
                    "id": pid,
                    "url": href,
                    "posted_at": None,  # FB only renders relative; not parsed.
                    "preview": preview,
                })
                if len(out["last_posts"]) >= 5:
                    break
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"shim-exception:facebook:{e.__class__.__name__}"
        out.setdefault("detail", str(e)[:240])
    return out


def _snapshot_instagram(page) -> dict:
    out: dict[str, Any] = {
        "bio": None, "avatar_url": None,
        "follower_count": None, "last_posts": [],
    }
    try:
        # The IG profile header lives in a <header> element.
        try:
            out["bio"] = page.locator("header section >> nth=2").inner_text(timeout=4000).strip() or None
        except Exception:
            try:
                out["bio"] = page.locator("header section div:has-text('')").nth(2).inner_text(timeout=2000).strip() or None
            except Exception:
                pass

        try:
            avatar = page.locator("header img").first
            href = avatar.get_attribute("src", timeout=4000)
            if href:
                out["avatar_url"] = href
        except Exception:
            pass

        try:
            # IG renders follower count in a list-item: "<n> followers"
            li = page.locator("header li:has-text('followers'), header li:has-text('follower')").first
            txt = li.inner_text(timeout=4000)
            # The exact count is in the title attribute when truncated to '12.3K'.
            try:
                title = li.locator("span[title]").first.get_attribute("title", timeout=1000)
            except Exception:
                title = None
            out["follower_count"] = parse_follower_count(title or txt)
        except Exception:
            pass

        try:
            anchors = page.locator("article a[href^='/p/']").all()
            seen = set()
            for a in anchors:
                href = a.get_attribute("href")
                if not href or href in seen:
                    continue
                seen.add(href)
                full = href if href.startswith("http") else f"https://www.instagram.com{href}"
                # /p/<shortcode>/
                pid = full.rstrip("/").split("/")[-1]
                preview = None
                try:
                    img = a.locator("img").first
                    preview = img.get_attribute("alt", timeout=1500) or None
                except Exception:
                    pass
                out["last_posts"].append({
                    "id": pid,
                    "url": full,
                    "posted_at": None,
                    "preview": preview,
                })
                if len(out["last_posts"]) >= 5:
                    break
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"shim-exception:instagram:{e.__class__.__name__}"
        out.setdefault("detail", str(e)[:240])
    return out


_PLATFORM_SHIMS = {
    "facebook": _snapshot_facebook,
    "instagram": _snapshot_instagram,
}


def _detect_login_required(page, platform: str) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if platform == "facebook":
        return "/login" in url or "login.php" in url
    if platform == "instagram":
        return "/accounts/login" in url
    return False


# ---------------------------------------------------------------------------
# author-yaml-write call
# ---------------------------------------------------------------------------


def bump_last_snapshot_at(
    slug: str,
    run_id: str,
    succeeded_platforms: list[str],
    authors_dir: str,
) -> Optional[str]:
    """Write a tiny patch that updates pr.social.<p>.lastSnapshotAt for
    each platform we successfully snapshotted. Returns the diff_path
    or None on failure (logged but non-fatal — the snapshot files are
    on disk regardless).
    """
    if not succeeded_platforms:
        return None
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    patch = {
        "social": {
            p: {"lastSnapshotAt": today} for p in succeeded_platforms
        }
    }
    skill_path = HERE.parent / "author-yaml-write" / "author_yaml_write.py"
    if not skill_path.is_file():
        return None
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8",
    ) as tmp:
        yaml.safe_dump(patch, tmp, sort_keys=False)
        patch_path = tmp.name
    try:
        proc = subprocess.run(
            [
                sys.executable, str(skill_path),
                "--slug", slug,
                "--run", run_id,
                "--patch-file", patch_path,
                "--authors-dir", authors_dir,
            ],
            check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        if not payload.get("ok"):
            return None
        return payload.get("diff_path")
    finally:
        try:
            Path(patch_path).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    run_id = args.run
    slug = args.slug

    if not args.platforms:
        fail(2, "no-platforms",
             detail="--platform is required (repeatable)")

    try:
        author = load_author(slug, authors_dir=args.authors_dir)
    except PRError as e:
        fail(2, e.code, **e.extra)

    checks = [check_platform(author, p, require_cred=False) for p in args.platforms]
    eligible = [c for c in checks if c.ok]
    if not eligible:
        out = {
            "ok": True,
            "run_id": run_id, "slug": slug,
            "action": "snapshot",
            "status": "gate-failure",
            "error": "no-eligible-platforms",
            "per_platform": {
                c.platform: {"error": f"gate-failure:{c.reason}"}
                for c in checks
            },
        }
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    try:
        profile = call_browser_profile_switch(slug, run_id,
                                              authors_dir=args.authors_dir)
    except PRError as e:
        fail(1, e.code, **e.extra)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail(1, "playwright-missing",
             detail=("install with `pip install playwright` and "
                     "`playwright install chromium` in the sandbox"))

    snapshots_dir = run_subdir(run_id, "snapshots", slug)
    user_data_dir = profile["user_data_dir"]

    per_platform: dict[str, dict] = {}
    for c in checks:
        if not c.ok:
            per_platform[c.platform] = {"error": f"gate-failure:{c.reason}"}

    succeeded: list[str] = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=not args.headed,
            args=[a for a in profile["args"] if not a.startswith("--user-data-dir")],
            viewport={"width": 1280, "height": 1100},
        )
        try:
            for c in eligible:
                platform = c.platform
                shim = _PLATFORM_SHIMS.get(platform)
                if shim is None:
                    per_platform[platform] = {
                        "error": f"platform-not-implemented:{platform}",
                    }
                    continue
                page = ctx.new_page()
                snapshot: dict[str, Any] = {}
                try:
                    try:
                        url = public_profile_url(platform, c.handle, c.page_id)
                    except PRError as e:
                        per_platform[platform] = {
                            "error": e.code, **e.extra,
                        }
                        continue
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        per_platform[platform] = {
                            "error": f"navigate-failed:{platform}",
                            "detail": str(e)[:240],
                        }
                        continue

                    if _detect_login_required(page, platform):
                        per_platform[platform] = {
                            "error": f"login-required:{platform}",
                            "detail": ("public profile gated behind login; "
                                       "set credEnvKey or warm the chromium "
                                       "profile interactively"),
                        }
                        continue

                    snapshot = shim(page)
                    snapshot.setdefault("captured_at",
                                        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
                    snapshot["platform"] = platform
                    snapshot["profile_url"] = url

                    out_path = snapshots_dir / f"{platform}.json"
                    out_path.write_text(
                        json.dumps(snapshot, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )

                    summary = {
                        "path": str(out_path),
                        "bio_length": len(snapshot.get("bio") or ""),
                        "follower_count": snapshot.get("follower_count"),
                        "post_count": len(snapshot.get("last_posts") or []),
                    }
                    if "error" in snapshot:
                        summary["error"] = snapshot["error"]
                        summary["partial"] = True
                    else:
                        succeeded.append(platform)
                    per_platform[platform] = summary
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        finally:
            ctx.close()

    yaml_diff_path = None
    if not args.no_yaml_bump:
        yaml_diff_path = bump_last_snapshot_at(
            slug, run_id, succeeded, args.authors_dir,
        )

    captured_ok = len(succeeded)
    requested = len(args.platforms)
    if captured_ok == requested:
        status = "complete"
    elif captured_ok == 0:
        status = "partial"
    else:
        status = "partial"

    out = {
        "ok": True,
        "run_id": run_id, "slug": slug,
        "action": "snapshot",
        "status": status,
        "snapshots_dir": str(snapshots_dir),
        "per_platform": per_platform,
        "yaml_diff_path": yaml_diff_path,
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
