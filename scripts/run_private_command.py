#!/usr/bin/env python3
"""Run a command without exposing private stdout/stderr in public CI logs."""

from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--stdout-file")
    parser.add_argument("--timeout", type=float, help="seconds before stopping the private command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("command is required after --")

    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be positive")

    with tempfile.TemporaryDirectory(prefix="private-command-") as temp_dir:
        stdout_path = Path(temp_dir) / "stdout"
        stderr_path = Path(temp_dir) / "stderr"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                returncode = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                returncode = 124

        if args.stdout_file:
            destination = Path(args.stdout_file)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stdout_path, destination)
            os.chmod(destination, 0o600)

        print(
            f"{args.label}: exit={returncode} "
            f"stdout_bytes={stdout_path.stat().st_size} stderr_bytes={stderr_path.stat().st_size}"
        )
        if returncode == 124:
            print(f"::error::{args.label} timed out; private output was suppressed", file=sys.stderr)
        elif returncode:
            print(f"::error::{args.label} failed; private output was suppressed", file=sys.stderr)
        return returncode


if __name__ == "__main__":
    raise SystemExit(main())
