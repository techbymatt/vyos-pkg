"""Run bounded, advisory-only Lintian scans without installing packages."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path


def stop_process_group(process: subprocess.Popen, grace: float) -> None:
    """Terminate descendants too, including those that ignore SIGTERM."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(grace)
            # Reap an exited leader before signalling any remaining descendants.
            process.poll()
    process.wait()


def report_lintian(
    packages: list[Path],
    report: Path,
    *,
    total_timeout: float = 1800,
    package_timeout: float = 300,
    kill_grace: float = 5,
    command: tuple[str, ...] = (
        "lintian",
        "--no-cfg",
        "--allow-root",
        "--fail-on",
        "error,warning",
    ),
) -> str:
    """Scan each package under per-package and overall budgets, writing the report."""
    counts = {
        "completed": 0,
        "findings": 0,
        "timed_out": 0,
        "failed": 0,
        "unscanned": 0,
    }
    deadline = time.monotonic() + total_timeout
    with report.open("w") as output:

        def log(message: str) -> None:
            """Echo progress to stdout and the report file."""
            print(message, flush=True)
            output.write(message + "\n")
            output.flush()

        log(f"Lintian advisory scan: {len(packages)} packages")
        for index, package in enumerate(packages):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                counts["unscanned"] = len(packages) - index
                log("Overall scan budget exhausted.")
                for pending in packages[index:]:
                    log(f"UNSCANNED {pending}")
                break
            started = time.monotonic()
            log(f"START {index + 1}/{len(packages)} {package}")
            try:
                process = subprocess.Popen(
                    [*command, str(package.resolve())],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                counts["failed"] += 1
                log(f"FAILED {package}: {error}")
                continue
            try:
                status = process.wait(timeout=min(package_timeout, remaining))
            except subprocess.TimeoutExpired:
                stop_process_group(process, kill_grace)
                counts["timed_out"] += 1
                result = "TIMED OUT"
            else:
                # Lintian uses 2 for policy findings and 1 for runtime errors.
                if status in (0, 2):
                    counts["completed"] += 1
                    if status == 2:
                        counts["findings"] += 1
                else:
                    counts["failed"] += 1
                result = f"exit status {status}"
            log(f"END {package}: {result}; {time.monotonic() - started:.2f}s")
        incomplete = not packages or any(
            counts[key] for key in ("timed_out", "failed", "unscanned")
        )
        summary = (
            f"Lintian scan {'INCOMPLETE' if incomplete else 'complete'} (advisory only): "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
        log(summary)
        if incomplete or counts["findings"]:
            print("::warning::" + summary, flush=True)
    return summary


def positive_seconds(value: str) -> float:
    """Argparse type rejecting non-finite or non-positive timeout values."""
    seconds = float(value)
    if not 0 < seconds < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return seconds


def main() -> None:
    """Scan --artifacts and append the summary to GITHUB_STEP_SUMMARY when set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("lintian-report.txt"))
    parser.add_argument("--total-timeout", type=positive_seconds, default=600)
    parser.add_argument("--package-timeout", type=positive_seconds, default=120)
    parser.add_argument("--kill-grace", type=positive_seconds, default=5)
    args = parser.parse_args()
    summary = report_lintian(
        sorted(args.artifacts.rglob("*.deb")),
        args.report,
        total_timeout=args.total_timeout,
        package_timeout=args.package_timeout,
        kill_grace=args.kill_grace,
    )
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(summary + "\n")


if __name__ == "__main__":
    main()
