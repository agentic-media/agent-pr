#!/usr/bin/env python3
"""Merge a yaml_patch into the `pr:` block of an author yaml.

See SKILL.md for the contract. The result is staged at
`/shared/runs/<run>/author-yaml/<slug>.yaml` (NEVER directly to
/lordship/authors/, which is read-only at runtime). The
`lordship-author-pr` skill ships the staged file via PR.

Safety guards:
- Refuses to write any value at a key matching the secret regex
  `(_)?(API_KEY|TOKEN|PASSWORD|PRIVATE_KEY|SECRET)$` (case-insensitive).
- Refuses to write any value that looks like a high-entropy literal
  (length >= 32, four+ char-classes, no whitespace).
- Atomic write: temp file in same dir, fsync, rename.
- Preserves the Astro-collection block byte-for-byte by replacing only
  the `pr:` block region. PyYAML can't round-trip comments/flow style;
  we sidestep that by re-emitting just the PR-owned region.

Exit codes:
  0  ok; staged path on stdout (JSON)
  1  io / git failure
  2  validation failure (secret-leak, schema-violation, etc.)
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


# Secret-shaped key names. Matches the AGENTS.md regex; case-insensitive
# so `Token` / `apiKey` variants are caught. The leading `_?` permits
# `OPENAI_API_KEY` and friends.
SECRET_KEY_RE = re.compile(
    r"(_)?(API_KEY|TOKEN|PASSWORD|PRIVATE_KEY|SECRET)$",
    re.IGNORECASE,
)

# Allowlist exception: `credEnvKey` is a NAME of an env var, never the
# value. The whole point of credEnvKey is to keep secrets out of yaml.
SECRET_KEY_EXCEPTIONS = {"credEnvKey"}


def _char_classes(s: str) -> int:
    """Count distinct character classes present in a string."""
    classes = 0
    if any(c.islower() for c in s):
        classes += 1
    if any(c.isupper() for c in s):
        classes += 1
    if any(c.isdigit() for c in s):
        classes += 1
    if any((not c.isalnum()) and (not c.isspace()) for c in s):
        classes += 1
    return classes


# Token-charset literal: typical PAT/JWT/base64/hex bodies. If the
# whole string consists ONLY of these characters (no spaces, no
# slashes outside `/+=`, no dots) it's almost certainly a credential.
_TOKEN_CHARSET = re.compile(r"^[A-Za-z0-9+/=_\-]+$")


def looks_like_secret_value(v: Any) -> bool:
    """Heuristic: catch credential-shaped literals while letting
    normal yaml strings (URLs, prose, slugs, ISO dates, page-ids)
    through.

    Two trip conditions, either fires:
    1. length >= 32 AND >= 4 distinct char classes AND no whitespace
       AND not URL-shaped AND not ISO-date-shaped.
    2. length >= 40 AND matches the token charset
       (alnum + `+/-_=`) — flags long base64/hex/jwt strings that
       would otherwise only have 2-3 char classes.
    """
    if not isinstance(v, str):
        return False
    s = v.strip()
    if len(s) < 32:
        return False
    if any(c.isspace() for c in s):
        return False
    # URLs / paths are long-ish but not secrets.
    if s.startswith(("http://", "https://", "/")):
        return False
    # ISO dates / datetimes (full-string match).
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ][\d:.\-+Z]+)?", s):
        return False
    # Long token-charset literals (covers base64, hex, jwt-bodies).
    if len(s) >= 40 and _TOKEN_CHARSET.fullmatch(s):
        return True
    # Multi-class long literals.
    if _char_classes(s) >= 4:
        return True
    return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--patch-file", required=True, dest="patch_file",
                   help="path to a yaml file containing the patch")
    p.add_argument("--authors-dir", default="/lordship/authors",
                   dest="authors_dir")
    p.add_argument("--staged-out-dir", default=None,
                   dest="staged_out_dir",
                   help="default: /shared/runs/<run>/author-yaml/")
    p.add_argument("--diff-out", default=None, dest="diff_out",
                   help="default: /shared/runs/<run>/author-yaml.diff")
    return p.parse_args()


def fail(code: int, error: str, **extra) -> None:
    sys.stderr.write(f"author-yaml-write: {error}\n")
    payload = {"ok": False, "error": error, **extra}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(code)


def scan_for_secrets(node: Any, path: list[str]) -> tuple[str, str] | None:
    """Walk the patch dict and return (key-path, reason) on first hit.

    `credEnvKey` keys are allowlisted on BOTH sides: the key-name
    regex skips them (the whole point of credEnvKey is it's NAMED
    like a token reference), and the value-entropy check is skipped
    on `credEnvKey: <value>` pairs (the value is a uppercase env-var
    name like `LORDSHIP_FB_TOKEN_ELENA_MORETTI` — declarative, not
    secret).
    """
    if isinstance(node, dict):
        for k, v in node.items():
            here = path + [str(k)]
            is_exception = isinstance(k, str) and k in SECRET_KEY_EXCEPTIONS
            if (
                isinstance(k, str)
                and not is_exception
                and SECRET_KEY_RE.search(k)
                and v is not None
                and v != ""
            ):
                return ".".join(here), "key-name-shaped-like-secret"
            if not is_exception and looks_like_secret_value(v):
                return ".".join(here), "high-entropy-literal"
            if not is_exception:
                inner = scan_for_secrets(v, here)
                if inner:
                    return inner
    elif isinstance(node, list):
        for i, item in enumerate(node):
            inner = scan_for_secrets(item, path + [f"[{i}]"])
            if inner:
                return inner
    else:
        if looks_like_secret_value(node):
            return ".".join(path), "high-entropy-literal"
    return None


def deep_merge(base: dict, patch: dict) -> dict:
    """Merge `patch` into `base` (returns a new dict). Lists are
    replaced wholesale; dicts are merged recursively; scalars overwrite.
    """
    out = dict(base)
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_PR_BLOCK_RE = re.compile(r"^pr:\s*$", re.MULTILINE)


def split_pr_block(text: str) -> tuple[str, str]:
    """Return (head, pr_block_text). pr: block is from its line to EOF.

    If no `pr:` block exists, head is the whole file and pr_block_text
    is empty.
    """
    m = _PR_BLOCK_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start():]


def emit_pr_block(pr_dict: dict) -> str:
    """Emit `pr:` block in canonical PyYAML block style. Always ends
    with a single trailing newline."""
    payload = yaml.safe_dump(
        {"pr": pr_dict},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )
    if not payload.endswith("\n"):
        payload += "\n"
    return payload


def atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> int:
    args = parse_args()

    src_yaml = Path(args.authors_dir) / f"{args.slug}.yaml"
    if not src_yaml.is_file():
        fail(2, "file-not-found", path=str(src_yaml))

    patch_path = Path(args.patch_file)
    if not patch_path.is_file():
        fail(2, "patch-file-missing", path=str(patch_path))

    try:
        with patch_path.open() as f:
            patch_doc = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        fail(2, "patch-unparseable", detail=str(e))

    if not isinstance(patch_doc, dict):
        fail(2, "patch-must-be-mapping")

    # The manifest's yaml_patch targets the `pr:` block by default.
    # If the caller passes a `pr:` wrapper, accept it; otherwise treat
    # the patch as the inner pr-block contents.
    if "pr" in patch_doc and isinstance(patch_doc["pr"], dict):
        pr_patch = patch_doc["pr"]
        # Check for any sibling keys at top-level — those would be
        # Astro-block writes, which require the lord's explicit
        # `target: astro` (not implemented in this version).
        astro_keys = {k for k in patch_doc.keys() if k != "pr"}
        if astro_keys:
            fail(2, "astro-block-unauthorised",
                 keys=sorted(astro_keys),
                 detail=("patch carries top-level keys that would "
                         "modify the Astro block; pass them under "
                         "`pr:` or use a separate audited path"))
    else:
        pr_patch = patch_doc

    if not isinstance(pr_patch, dict):
        fail(2, "pr-patch-must-be-mapping")

    # Reject patches that re-declare Astro-block keys at the top of
    # the inner patch (caller mistake, easy to do).
    _ASTRO_KEYS = {
        "name", "slug", "title", "bio", "bioShort", "specialization",
        "topics", "writingStyle", "avatar", "images",
    }
    intruders = sorted(_ASTRO_KEYS & set(pr_patch.keys()))
    if intruders:
        fail(2, "astro-block-unauthorised",
             keys=intruders,
             detail=("patch's pr: block must not re-declare "
                     "Astro-collection or images keys"))

    leak = scan_for_secrets(pr_patch, [])
    if leak:
        key_path, reason = leak
        fail(2, "secret-leak-attempt",
             key_path=key_path, reason=reason,
             detail=("only credEnvKey references are permitted; "
                     "raw secrets must stay in /lordship/credentials/.env"))

    # Load the existing yaml.
    src_text = src_yaml.read_text(encoding="utf-8")
    try:
        src_doc = yaml.safe_load(src_text) or {}
    except yaml.YAMLError as e:
        fail(2, "source-yaml-unparseable", path=str(src_yaml), detail=str(e))

    if src_doc.get("slug") != args.slug:
        fail(2, "slug-mismatch",
             expected=args.slug, got=src_doc.get("slug"),
             path=str(src_yaml))

    # Required fields per AGENTS.md:
    if not src_doc.get("name") or not src_doc.get("bioShort"):
        fail(2, "schema-violation",
             detail="source yaml missing name/bioShort")

    cur_pr = src_doc.get("pr") or {}
    merged_pr = deep_merge(cur_pr, pr_patch)
    merged_pr["updatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if not merged_pr.get("createdAt"):
        merged_pr["createdAt"] = merged_pr["updatedAt"]

    # Schema check on the merged pr block (per SKILL.md required set).
    required_pr = ("browserProfileDir", "authorisedDomains")
    missing = [k for k in required_pr if not merged_pr.get(k)]
    if missing:
        fail(2, "schema-violation",
             missing=missing,
             detail="merged pr: block missing required fields")

    # Reconstruct the file: keep the Astro+images head verbatim, replace
    # the pr: block region wholesale.
    head, _old_pr = split_pr_block(src_text)
    if not head.endswith("\n"):
        head += "\n"
    new_pr_text = emit_pr_block(merged_pr)
    new_text = head + new_pr_text

    # Stage to /shared/runs/<run>/author-yaml/<slug>.yaml.
    staged_dir = Path(
        args.staged_out_dir
        or f"/shared/runs/{args.run}/author-yaml"
    )
    staged_path = staged_dir / f"{args.slug}.yaml"
    try:
        atomic_write(staged_path, new_text)
    except OSError as e:
        fail(1, "atomic-write-failed", path=str(staged_path), detail=str(e))

    # Diff for the lord.
    diff_path = Path(
        args.diff_out
        or f"/shared/runs/{args.run}/author-yaml.diff"
    )
    try:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_text = "".join(difflib.unified_diff(
            src_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{args.slug}.yaml",
            tofile=f"b/{args.slug}.yaml",
        ))
        diff_path.write_text(diff_text, encoding="utf-8")
    except OSError as e:
        fail(1, "diff-write-failed", path=str(diff_path), detail=str(e))

    out = {
        "ok": True,
        "path": str(staged_path),
        "diff_path": str(diff_path),
        "astro_block_changed": False,
        "slug": args.slug,
        "run_id": args.run,
        "updated_at": merged_pr["updatedAt"],
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
