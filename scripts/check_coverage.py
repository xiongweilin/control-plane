"""Coverage decline gate (batch5 item 8).

Runs the test suite with coverage and fails when the total coverage drops
below the recorded baseline. The baseline lives in
``scripts/coverage-baseline.txt`` (a single percentage number) and was captured
from the CI portable suite (the same ``-k`` filter used in GitHub Actions).

Usage:
    uv run python scripts/check_coverage.py
    uv run python scripts/check_coverage.py --pytest-args "-k 'not windows_only'"
    uv run python scripts/check_coverage.py --baseline 75.0
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

TOTAL_RE = re.compile(r"^TOTAL\s+\d+\s+\d+\s+(\d+)%$", re.MULTILINE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=float, default=None)
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=Path(__file__).parent / "coverage-baseline.txt",
    )
    parser.add_argument("--pytest-args", default="", help="extra pytest arguments")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        help="allowed percentage-point drop below baseline",
    )
    args = parser.parse_args()

    baseline = args.baseline
    if baseline is None:
        baseline = float(args.baseline_file.read_text(encoding="ascii").strip())

    # The gate is already launched through the project's environment (the CI
    # workflow uses ``uv run python``). Reusing that interpreter avoids a
    # nested uv trampoline, which is not reliable on Windows.
    # The migration gate covers both the compatibility profile and the
    # canonical portable runtime.  Keeping both roots explicit avoids a
    # misleading green gate that exercises only legacy control-plane code.
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--cov=control_plane",
        "--cov=portable_runtime",
        "--cov-report=term",
        "-q",
    ]
    if args.pytest_args:
        command.extend(shlex.split(args.pytest_args))
    proc = subprocess.run(  # noqa: S603 - fixed command line; args come from the caller's CLI
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    match = TOTAL_RE.search(output)
    if not match:
        print(output[-3_000:])
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
