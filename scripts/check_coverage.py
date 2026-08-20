"""Coverage decline gate (batch5 item 8)."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

TOTAL_RE = re.compile(r"^TOTAL\s+\d+\s+\d+\s+(\d+)%$", re.MULTILINE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=float, default=None)
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=Path(__file__).parent / "coverage-baseline.txt",
    )
    parser.add_argument("--pytest-args", default="")
    parser.add_argument("--tolerance", type=float, default=0.5)
    args = parser.parse_args()
    baseline = args.baseline
    if baseline is None:
        baseline = float(args.baseline_file.read_text(encoding="ascii").strip())
    command = ["uv", "run", "pytest", "--cov=portable_runtime", "--cov-report=term", "-q"]
    if args.pytest_args:
        command.extend(shlex.split(args.pytest_args))
    proc = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, check=False
    )
    output = proc.stdout + proc.stderr
    match = TOTAL_RE.search(output)
    if not match:
        fallback = [
            sys.executable,
            "-m",
            "pytest",
            "--cov=portable_runtime",
            "--cov-report=term",
            "-q",
        ]
        if args.pytest_args:
            fallback.extend(shlex.split(args.pytest_args))
        proc2 = subprocess.run(  # noqa: S603
            fallback, capture_output=True, text=True, check=False
        )
        output2 = proc2.stdout + proc2.stderr
        match2 = TOTAL_RE.search(output2)
        if match2:
            output = output2
            match = match2
        else:
            print(output[-3000:])
            print(output2[-3000:])
            print("coverage gate: could not parse the TOTAL line", file=sys.stderr)
            return 2
    current = float(match.group(1))
    print(f"coverage {current:.1f}% (baseline {baseline:.1f}%, tolerance {args.tolerance}%)")
    if current + args.tolerance < baseline:
        print(
            f"coverage gate FAILED: {current:.1f}% is more than {args.tolerance}% "
            f"below baseline {baseline:.1f}%",
            file=sys.stderr,
        )
        return 1
    print("coverage gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
