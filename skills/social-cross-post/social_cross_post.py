#!/usr/bin/env python3
"""Cross-post a published article to N platforms via the author's chromium profile.

See SKILL.md and ../../AGENTS.md for the contract. One post per
platform per call. Failures isolated per-platform; one platform's
`posted: false` does not abort the others.

Implementation notes:

- The article URL is verified live (HTTP 200 + non-empty body) before
  ANY browser session starts. If the article isn't reachable, the run
  is `gate-failure: article-unreachable` and nothing is posted.
- For each platform, the chromium profile is selected via the
  `browser-profile-switch` skill (never bypass it). A persistent
  Playwright context is launched against that user-data-dir, so
  cookies / login state persist on the host bind across runs.
- Per-platform shims live in `_PLATFORMS`. Each implements a small
  protocol: navigate → compose → submit → return `(post_id, post_url)`.
  A platform without a shim returns `error: platform-not-implemented:<name>`
  and the run continues on the others.
- The skill writes a results dict + an outbox-shaped record under
  `/shared/runs/<run>/cross-post/<slug>.json` and emits the same
  payload on stdout.

Login state: every browser session uses the per-author chromium
profile. If a platform's session has expired, the per-platform shim
detects the login wall and returns `error: login-expired:<platform>`;
the skill records that, ends the session cleanly, and moves on.

Exit codes:
  0  ok (status: complete | partial)
  2  manifest gate-failure (article unreachable, no platforms passed
     gate, slug missing)
  1  unexpected error (raised before per-platform isolation kicks in)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# The shared helpers sit one level up. Add it to sys.path so the
# script can be run directly with `python ...skill.py`.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _pr_common import (  # noqa: E402
    PRError,
    call_browser_profile_switch,
    check_platform,
    compose_url,
    load_author,
    run_subdir,
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--article-url", required=True, dest="article_url")
    p.add_argument("--platform", action="append", default=[],
                   dest="platforms",
                   help="repeatable: --platform facebook --platform instagram")
    p.add_argument("--caption", default=None,
                   help="optional caption override; per-platform default applies if absent")
    p.add_argument("--authors-dir", default="/lordship/authors",
                   dest="authors_dir")
    p.add_argument("--headed", action="store_true",
                   help="show the browser (debug only)")
    return p.parse_args()


def fail(code: int, error: str, **extra) -> None:
    sys.stderr.write(f"social-cross-post: {error}\n")
    payload = {"ok": False, "error": error, **extra}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Article-live gate
# ---------------------------------------------------------------------------


def article_is_live(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """HEAD-then-GET fallback. Returns (ok, reason).

    A platform's link card unfurler will hit this URL anyway, so
    "live for the unfurler" is what we need to assert.
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "agentic-media-pr/1.0 (+article-liveness)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, f"http-{resp.status}"
            # Read at most 4KB; we only need to confirm a non-empty body.
            body = resp.read(4096)
            if not body:
                return False, "empty-body"
            return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"http-{e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"unreachable:{e.__class__.__name__}"


# ---------------------------------------------------------------------------
# Per-platform shims
# ---------------------------------------------------------------------------


def _default_caption(article_url: str, platform: str) -> str:
    return f"New piece up: {article_url}"


def _detect_login_wall(page, platform: str) -> bool:
    """Heuristic: if the page URL bounces to the platform's login route,
    or a `login` form is the only meaningful element, the session has
    expired."""
    try:
        url = page.url
    except Exception:
        return False
    url = (url or "").lower()
    if platform == "facebook":
        return "/login" in url or "login.php" in url or "checkpoint" in url
    if platform == "instagram":
        return "/accounts/login" in url
    return False


def _post_facebook(page, article_url: str, caption: str, hero_url: Optional[str]) -> dict:
    """Post a link to a Facebook page.

    Strategy: navigate to the page; click the composer; paste the
    article URL (FB auto-attaches a link card); paste the caption;
    submit. Return the post id from the resulting permalink.

    If the page renders the "log in to post" wall, return login-expired.
    If composer never appears, return error with a description; the
    run continues on the next platform.
    """
    # The page argument has already been navigated to the page URL.
    if _detect_login_wall(page, "facebook"):
        return {"error": "login-expired:facebook"}

    try:
        # New-UI composer trigger. Multiple labels because FB A/B-tests.
        composer_selectors = [
            "div[role='button']:has-text(\"Create post\")",
            "div[role='button']:has-text(\"Crea un post\")",
            "div[role='button']:has-text(\"What's on your mind\")",
            "div[role='button']:has-text(\"Cosa stai pensando\")",
            "div[role='textbox'][contenteditable='true']",
        ]
        clicked = False
        for sel in composer_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                clicked = True
                break
        if not clicked:
            return {"error": "composer-not-found:facebook"}

        # Find the now-active textbox.
        textbox = page.locator("div[role='textbox'][contenteditable='true']").first
        textbox.wait_for(state="visible", timeout=10000)
        textbox.click()
        # Caption + URL on the same paste so FB's link-card unfurler
        # picks it up.
        page.keyboard.type(f"{caption}\n\n{article_url}")

        # Submit. Label varies by locale.
        submit_selectors = [
            "div[aria-label='Post'][role='button']",
            "div[aria-label='Pubblica'][role='button']",
            "div[role='button']:has-text(\"Post\")",
            "div[role='button']:has-text(\"Pubblica\")",
        ]
        submitted = False
        for sel in submit_selectors:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_enabled():
                btn.click(timeout=8000)
                submitted = True
                break
        if not submitted:
            return {"error": "submit-button-not-found:facebook"}

        # The composer dialog closes when the post is in flight. Wait
        # for it. Then capture the most-recent post id off the page.
        page.wait_for_timeout(4000)
        try:
            permalink = page.locator(
                "a[href*='/posts/'], a[href*='/permalink/']"
            ).first.get_attribute("href", timeout=5000)
        except Exception:
            permalink = None

        post_id = None
        post_url = None
        if permalink:
            post_url = permalink if permalink.startswith("http") else f"https://www.facebook.com{permalink}"
            # /posts/<id> or /permalink/<id>
            for marker in ("/posts/", "/permalink/"):
                if marker in permalink:
                    tail = permalink.split(marker, 1)[1]
                    post_id = tail.split("/", 1)[0].split("?", 1)[0]
                    break
        if not post_id:
            # Posted but couldn't capture the id — surface as partial.
            return {"posted": True, "post_id": None, "url": post_url,
                    "warning": "post-id-not-captured"}
        return {"posted": True, "post_id": post_id, "url": post_url}
    except Exception as e:
        return {"error": f"shim-exception:facebook:{e.__class__.__name__}",
                "detail": str(e)[:240]}


def _post_instagram(page, article_url: str, caption: str, hero_url: Optional[str]) -> dict:
    """Post to Instagram. IG web only allows posts with media — we
    rely on the link card sharing the article's hero image. Without a
    hero image URL we can't post (IG won't accept text-only posts).

    The shim is deliberately conservative: if the Create flow doesn't
    present itself, we surface a clear per-platform error rather than
    fake a success.
    """
    if _detect_login_wall(page, "instagram"):
        return {"error": "login-expired:instagram"}
    if not hero_url:
        return {"error": "instagram-needs-media",
                "detail": ("Instagram web does not allow text-only "
                           "posts; the author's images.hero or "
                           "images.og must be set")}
    try:
        # Click the "Create" entry in the sidebar.
        create_selectors = [
            "a[href='#'] >> text=Create",
            "svg[aria-label='New post']",
            "svg[aria-label='Nuovo post']",
        ]
        clicked = False
        for sel in create_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                clicked = True
                break
        if not clicked:
            return {"error": "create-button-not-found:instagram"}

        # IG's web Create-Post flow expects a file selector. Without
        # local media we cannot complete the post — surface the gap
        # rather than half-finish.
        return {"error": "instagram-upload-not-implemented",
                "detail": ("the Instagram Create flow needs a local "
                           "media file; the PR-agent doesn't yet "
                           "download images.hero into the sandbox")}
    except Exception as e:
        return {"error": f"shim-exception:instagram:{e.__class__.__name__}",
                "detail": str(e)[:240]}


_PLATFORM_SHIMS = {
    "facebook": _post_facebook,
    "instagram": _post_instagram,
}


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

    # Article liveness gate before any browser session.
    live, reason = article_is_live(args.article_url)
    if not live:
        results_dir = run_subdir(run_id, "cross-post")
        out = {
            "ok": True,
            "run_id": run_id, "slug": slug,
            "action": "cross-post",
            "status": "gate-failure",
            "error": "article-unreachable",
            "detail": reason,
            "results": {},
        }
        out_path = results_dir / f"{slug}.json"
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    # Author + per-platform gating.
    try:
        author = load_author(slug, authors_dir=args.authors_dir)
    except PRError as e:
        fail(2, e.code, **e.extra)

    checks = [check_platform(author, p, require_cred=True) for p in args.platforms]
    eligible = [c for c in checks if c.ok]

    if not eligible:
        # Every platform failed gate — single gate-failure for the run.
        results = {
            c.platform: {"posted": False, "error": f"gate-failure:{c.reason}"}
            for c in checks
        }
        out = {
            "ok": True,
            "run_id": run_id, "slug": slug,
            "action": "cross-post",
            "status": "gate-failure",
            "error": "no-eligible-platforms",
            "results": results,
        }
        results_dir = run_subdir(run_id, "cross-post")
        (results_dir / f"{slug}.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False)
        )
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    # Profile switch (idempotent).
    try:
        profile = call_browser_profile_switch(slug, run_id,
                                              authors_dir=args.authors_dir)
    except PRError as e:
        fail(1, e.code, **e.extra)

    # Lazy-import playwright so the skill can at least gate-check
    # without it. If playwright isn't installed and we have eligible
    # platforms, that's an honest error.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail(1, "playwright-missing",
             detail=("install with `pip install playwright` and "
                     "`playwright install chromium` in the sandbox"))

    hero_url = author.images_block.get("hero") or author.images_block.get("og")

    results: dict[str, dict] = {}
    # Pre-fill skip results for any gate-failed platform so the outbox
    # carries the full requested list, not just the eligible subset.
    for c in checks:
        if not c.ok:
            results[c.platform] = {
                "posted": False,
                "error": f"gate-failure:{c.reason}",
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            }

    user_data_dir = profile["user_data_dir"]

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=not args.headed,
            args=[a for a in profile["args"] if not a.startswith("--user-data-dir")],
            viewport={"width": 1280, "height": 900},
        )
        try:
            for c in eligible:
                platform = c.platform
                shim = _PLATFORM_SHIMS.get(platform)
                if shim is None:
                    results[platform] = {
                        "posted": False,
                        "error": f"platform-not-implemented:{platform}",
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    }
                    continue

                page = ctx.new_page()
                try:
                    target = compose_url(platform, c.handle, c.page_id)
                    page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    caption = args.caption or _default_caption(args.article_url, platform)
                    res = shim(page, args.article_url, caption, hero_url)
                except PRError as e:
                    res = {"error": e.code, **e.extra}
                except Exception as e:
                    res = {"error": f"navigate-failed:{platform}",
                           "detail": str(e)[:240]}
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                res.setdefault(
                    "at",
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                )
                if "posted" not in res:
                    res["posted"] = False
                results[platform] = res
        finally:
            ctx.close()

    posted_ok = sum(1 for r in results.values() if r.get("posted"))
    total = len(results)
    if posted_ok == total:
        status = "complete"
    elif posted_ok == 0:
        status = "partial"  # gate-failure already covered above
    else:
        status = "partial"

    out = {
        "ok": True,
        "run_id": run_id, "slug": slug,
        "action": "cross-post",
        "status": status,
        "results": results,
    }
    results_dir = run_subdir(run_id, "cross-post")
    (results_dir / f"{slug}.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False)
    )
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
