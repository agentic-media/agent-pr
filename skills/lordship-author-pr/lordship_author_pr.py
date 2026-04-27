#!/usr/bin/env python3
"""Open a lordship-repo PR for a staged author yaml + asset bundle.

See SKILL.md for the full contract. Subprocess argv arrays only,
never `shell=True`. JSON on stdout, human errors on stderr.

Exit codes:
  0  PR opened (or already-existing PR detected — idempotent re-run)
  1  git/gh failure
  2  input validation error (missing staged yaml, non-WebP asset,
     slug mismatch, etc.)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


# Image format magic-byte detection — same idiom as the publisher's
# astro-github skill so the PR-agent doesn't need Pillow at runtime.
def detect_image_format(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(32)
    except OSError:
        return "unknown"
    if not head:
        return "empty"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "avif"
    return "unknown"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--lordship-repo", required=True, dest="lordship_repo",
                   help="<owner>/<name>")
    p.add_argument("--staged-yaml-path", default=None, dest="staged_yaml_path")
    p.add_argument("--staged-assets-dir", default=None, dest="staged_assets_dir")
    p.add_argument("--pr-title", default=None, dest="pr_title")
    p.add_argument("--base-branch", default="main", dest="base_branch")
    p.add_argument("--clone-dir", default=None, dest="clone_dir")
    return p.parse_args()


def fail(code: int, msg: str, **extra) -> None:
    sys.stderr.write(f"lordship-author-pr: {msg}\n")
    payload = {"ok": False, "error": msg, **extra}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


def run(argv, cwd=None, check=True):
    return subprocess.run(
        argv, cwd=cwd, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )


def ensure_clone(clone_dir: Path, repo: str) -> None:
    """Clone if missing; if remote mismatches, wipe + re-clone."""
    git_dir = clone_dir / ".git"
    if git_dir.is_dir():
        try:
            cur = run(["git", "-C", str(clone_dir),
                       "remote", "get-url", "origin"]).stdout.strip()
        except subprocess.CalledProcessError:
            cur = ""
        wanted = (
            f"https://github.com/{repo}.git",
            f"git@github.com:{repo}.git",
            f"https://github.com/{repo}",
            f"git@github.com:{repo}",
        )
        if cur in wanted:
            run(["git", "-C", str(clone_dir), "fetch", "origin"])
            return
        sys.stderr.write(
            f"lordship-author-pr: clone-dir has remote {cur!r}; wiping\n"
        )
        shutil.rmtree(clone_dir)
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    run(["gh", "repo", "clone", repo, str(clone_dir)])


def configure_git(clone_dir: Path) -> None:
    """Per-repo gh credential helper + identity. Mirrors astro-github."""
    run(["git", "-C", str(clone_dir), "config", "--local",
         "credential.https://github.com.helper",
         "!gh auth git-credential"])
    author_name = "Lordship PR Agent"
    author_email = "pr@agentic-media.local"
    try:
        who = run(["gh", "api", "user", "--jq",
                   ".name + \"\\t\" + (.login + \"@users.noreply.github.com\")"])
        parts = who.stdout.strip().split("\t", 1)
        if len(parts) == 2 and parts[0]:
            author_name, author_email = parts[0], parts[1]
    except subprocess.CalledProcessError:
        pass
    run(["git", "-C", str(clone_dir), "config", "--local",
         "user.name", author_name])
    run(["git", "-C", str(clone_dir), "config", "--local",
         "user.email", author_email])


def collect_assets(staged_assets_dir: Path) -> tuple[list[Path], dict]:
    """Walk the staged assets dir; reject any non-WebP file.

    Returns (list of source files, manifest dict suitable for the PR body).
    """
    files: list[Path] = []
    manifest = {"avatar": None, "hero": None, "og": None, "gallery": []}

    if not staged_assets_dir.is_dir():
        return files, manifest

    for f in sorted(staged_assets_dir.rglob("*")):
        if not f.is_file():
            continue
        # Reject non-WebP. Suffix check first for speed; magic bytes
        # for authority.
        if f.suffix.lower() != ".webp":
            fail(2, "non-webp-asset",
                 path=str(f),
                 detail=("staged author assets must already be WebP — "
                         "transcode upstream before opening the PR"))
        fmt = detect_image_format(f)
        if fmt != "webp":
            fail(2, "non-webp-asset",
                 path=str(f), detected_format=fmt,
                 detail="suffix says .webp but magic bytes disagree")
        files.append(f)
        rel = f.relative_to(staged_assets_dir)
        rel_str = rel.as_posix()
        if rel_str == "avatar.webp":
            manifest["avatar"] = rel_str
        elif rel_str == "hero.webp":
            manifest["hero"] = rel_str
        elif rel_str == "og.webp":
            manifest["og"] = rel_str
        elif rel_str.startswith("gallery/"):
            manifest["gallery"].append(rel_str)

    return files, manifest


def main() -> int:
    args = parse_args()

    staged_yaml = Path(
        args.staged_yaml_path
        or f"/shared/runs/{args.run}/author-yaml/{args.slug}.yaml"
    )
    staged_assets = Path(
        args.staged_assets_dir
        or f"/shared/runs/{args.run}/author-assets/{args.slug}"
    )
    clone_dir = Path(
        args.clone_dir
        or f"/shared/runs/{args.run}/lordship-repo"
    )

    if not staged_yaml.is_file():
        fail(2, "staged-yaml-missing", path=str(staged_yaml))

    try:
        with staged_yaml.open() as f:
            doc = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        fail(2, "staged-yaml-unparseable", path=str(staged_yaml), detail=str(e))

    if doc.get("slug") != args.slug:
        fail(2, "slug-mismatch",
             expected=args.slug, got=doc.get("slug"),
             path=str(staged_yaml))

    # Asset bundle: optional. A yaml-only PR is allowed (text-only path).
    asset_files: list[Path] = []
    asset_manifest = {"avatar": None, "hero": None, "og": None, "gallery": []}
    if staged_assets.is_dir():
        asset_files, asset_manifest = collect_assets(staged_assets)
    have_assets = bool(asset_files)

    # If the bundle is present at all, avatar.webp is required.
    if have_assets and not asset_manifest["avatar"]:
        fail(2, "missing-avatar",
             staged_assets_dir=str(staged_assets),
             detail=("when an asset bundle is staged, avatar.webp at "
                     "the bundle root is required (hero/og/gallery "
                     "are optional)"))

    # Clone + branch.
    branch = f"assets/{args.slug}-{args.run}"
    try:
        ensure_clone(clone_dir, args.lordship_repo)
        configure_git(clone_dir)
        # If the branch already exists on origin (idempotent re-run),
        # base our checkout there so a no-op re-push detects "already
        # pushed" cleanly. Otherwise, branch off the base.
        try:
            run(["git", "-C", str(clone_dir),
                 "rev-parse", "--verify", f"origin/{branch}"])
            start_point = f"origin/{branch}"
        except subprocess.CalledProcessError:
            start_point = f"origin/{args.base_branch}"
        run(["git", "-C", str(clone_dir), "checkout", "-B",
             branch, start_point])
    except subprocess.CalledProcessError as e:
        fail(1, "git-prep-failed",
             stderr=(e.stderr or "").strip(),
             cmd=" ".join(e.cmd))

    # Drop yaml + assets into the working tree.
    yaml_dst = clone_dir / "authors" / f"{args.slug}.yaml"
    yaml_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(staged_yaml), str(yaml_dst))
    files_payload = {"yaml": f"authors/{args.slug}.yaml", "assets": []}

    if have_assets:
        avatars_root = clone_dir / "authors" / "avatars" / args.slug
        avatars_root.mkdir(parents=True, exist_ok=True)
        for src in asset_files:
            rel = src.relative_to(staged_assets)
            dst = avatars_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src), str(dst))
            files_payload["assets"].append(
                f"authors/avatars/{args.slug}/{rel.as_posix()}"
            )

    # Stage changes.
    add_paths = [f"authors/{args.slug}.yaml"]
    if have_assets:
        add_paths.append(f"authors/avatars/{args.slug}")
    try:
        run(["git", "-C", str(clone_dir), "add", *add_paths])
    except subprocess.CalledProcessError as e:
        fail(1, "git-add-failed", stderr=(e.stderr or "").strip())

    # If nothing changed, this is an idempotent re-run. Look up the
    # existing PR for this branch (if it exists on origin) and return
    # ok with the existing url.
    diff = run(["git", "-C", str(clone_dir),
                "diff", "--cached", "--name-only"])
    if not diff.stdout.strip():
        existing_url = ""
        existing_sha = ""
        try:
            existing_sha = run(["git", "-C", str(clone_dir),
                                "rev-parse", "HEAD"]).stdout.strip()
        except subprocess.CalledProcessError:
            pass
        try:
            view = run(["gh", "pr", "view", branch,
                        "-R", args.lordship_repo,
                        "--json", "url", "-q", ".url"], cwd=clone_dir)
            existing_url = view.stdout.strip()
        except subprocess.CalledProcessError:
            pass
        out = {
            "ok": True,
            "no_changes": True,
            "pr_url": existing_url,
            "branch": branch,
            "commit_sha": existing_sha,
            "repo": args.lordship_repo,
            "files": files_payload,
        }
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    # Commit message.
    title = args.pr_title or f"Author update: {args.slug} ({args.run})"
    body_lines = [
        f"Staged author update for `{args.slug}` from run `{args.run}`.",
        "",
        f"- yaml: `authors/{args.slug}.yaml`",
    ]
    if have_assets:
        variants = []
        if asset_manifest["avatar"]:
            variants.append("avatar")
        if asset_manifest["hero"]:
            variants.append("hero")
        if asset_manifest["og"]:
            variants.append("og")
        if asset_manifest["gallery"]:
            variants.append(f"gallery×{len(asset_manifest['gallery'])}")
        body_lines.append(f"- variants: {', '.join(variants)}")
        body_lines.append("- assets:")
        for p in files_payload["assets"]:
            body_lines.append(f"  - `{p}`")
    else:
        body_lines.append("- no asset bundle (yaml-only update)")
    body = "\n".join(body_lines)

    try:
        run(["git", "-C", str(clone_dir), "commit", "-m", title, "-m", body])
    except subprocess.CalledProcessError as e:
        fail(1, "git-commit-failed", stderr=(e.stderr or "").strip())

    try:
        run(["git", "-C", str(clone_dir), "push", "-u",
             "origin", branch])
    except subprocess.CalledProcessError as e:
        fail(1, "git-push-failed", stderr=(e.stderr or "").strip())

    sha = run(["git", "-C", str(clone_dir),
               "rev-parse", "HEAD"]).stdout.strip()

    # Open the PR. If one already exists for this head, fall through.
    pr_url = ""
    try:
        r = run(["gh", "pr", "create",
                 "-R", args.lordship_repo,
                 "--head", branch,
                 "--base", args.base_branch,
                 "--title", title,
                 "--body", body], cwd=clone_dir)
        # gh prints the URL on the last non-empty line of stdout.
        out_lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if out_lines:
            pr_url = out_lines[-1]
    except subprocess.CalledProcessError as e:
        # Likely "a pull request for branch X already exists". Look it up.
        try:
            view = run(["gh", "pr", "view", branch,
                        "-R", args.lordship_repo,
                        "--json", "url", "-q", ".url"], cwd=clone_dir)
            pr_url = view.stdout.strip()
        except subprocess.CalledProcessError:
            fail(1, "gh-pr-create-failed",
                 stderr=(e.stderr or "").strip(),
                 branch=branch)

    out = {
        "ok": True,
        "pr_url": pr_url,
        "branch": branch,
        "commit_sha": sha,
        "repo": args.lordship_repo,
        "files": files_payload,
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
