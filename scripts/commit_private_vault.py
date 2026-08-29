#!/usr/bin/env python3
"""Commit a scoped private-vault update without printing private paths."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def require(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode:
        print(f"::error::private vault {label} failed; output suppressed", file=sys.stderr)
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    parser.add_argument("--message", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument("--all", action="store_true", help="Use git add -A for the scoped paths")
    args = parser.parse_args()
    vault = Path(args.vault)

    require(git(vault, "config", "user.name", "github-actions[bot]"), "configure name")
    require(git(vault, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"), "configure email")
    add_args = ["add"] + (["-A"] if args.all else []) + ["--", *args.path]
    require(git(vault, *add_args), "stage")
    staged = git(vault, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        print("private vault commit: changed=no")
        return 0
    if staged.returncode != 1:
        require(staged, "inspect staged changes")

    require(git(vault, "commit", "-m", args.message), "commit")
    require(git(vault, "fetch", "origin", args.ref), "fetch")
    require(git(vault, "merge-base", "--is-ancestor", f"origin/{args.ref}", "HEAD"), "ancestry check")
    require(git(vault, "push", "origin", f"HEAD:{args.ref}"), "push")
    print("private vault commit: changed=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
