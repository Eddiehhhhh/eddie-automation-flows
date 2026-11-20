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


def require_compatible_index_paths(vault: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(vault), "ls-files", "-z", "--cached"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    require(result, "inspect index paths")
    invalid_count = sum(
        any(len(component) > 255 for component in path.split(b"/"))
        for path in result.stdout.split(b"\0") if path
    )
    if invalid_count:
        print(
            f"::error::private vault has {invalid_count} indexed paths with components over 255 bytes; paths suppressed",
            file=sys.stderr,
        )
        raise SystemExit(2)


def publish_after_unrelated_updates(vault: Path, ref: str, paths: list[str]) -> None:
    source = git(vault, "rev-parse", "HEAD")
    require(source, "identify scoped commit")
    parent = git(vault, "rev-parse", "HEAD^")
    require(parent, "identify scoped commit parent")
    scope = ["--", *paths]
    target = f"origin/{ref}"
    previous_head = None
    for _ in range(3):
        require(git(vault, "fetch", "origin", ref), "fetch")
        latest = git(vault, "rev-parse", target)
        require(latest, "identify remote commit")
        head = latest.stdout.strip()
        if git(vault, "merge-base", "--is-ancestor", "HEAD", head).returncode == 0:
            return  # A prior push with an unknown outcome already reached the remote.
        if head == previous_head:
            break
        if head != parent.stdout.strip():
            require(git(vault, "diff", "--quiet", parent.stdout.strip(), head, *scope),
                    "scoped paths changed on remote")
            require(git(vault, "switch", "--detach", head), "select current remote commit")
            require(git(vault, "cherry-pick", source.stdout.strip()), "replay scoped commit")
        require(git(vault, "merge-base", "--is-ancestor", head, "HEAD"), "ancestry check")
        require_compatible_index_paths(vault)
        pushed = git(vault, "push", "origin", f"HEAD:{ref}")
        if pushed.returncode == 0:
            return
        previous_head = head
    require(pushed, "push")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    parser.add_argument("--message", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument("--all", action="store_true", help="Use git add -A for the scoped paths")
    parser.add_argument("--replay-if-scope-unchanged", action="store_true",
                        help="Retry publishing from an isolated runner if the source paths are unchanged")
    args = parser.parse_args()
    vault = Path(args.vault)

    require(git(vault, "config", "user.name", "github-actions[bot]"), "configure name")
    require(git(vault, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"), "configure email")
    if args.replay_if_scope_unchanged:
        require(git(vault, "diff", "--cached", "--quiet"), "require empty staging area")
    add_args = ["add"] + (["-A"] if args.all else []) + ["--", *args.path]
    require(git(vault, *add_args), "stage")
    require_compatible_index_paths(vault)
    staged = git(vault, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        print("private vault commit: changed=no")
        return 0
    if staged.returncode != 1:
        require(staged, "inspect staged changes")

    require(git(vault, "commit", "-m", args.message), "commit")
    if args.replay_if_scope_unchanged:
        publish_after_unrelated_updates(vault, args.ref, args.path)
    else:
        require(git(vault, "fetch", "origin", args.ref), "fetch")
        require(git(vault, "merge-base", "--is-ancestor", f"origin/{args.ref}", "HEAD"), "ancestry check")
        require_compatible_index_paths(vault)
        require(git(vault, "push", "origin", f"HEAD:{args.ref}"), "push")
    print("private vault commit: changed=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
