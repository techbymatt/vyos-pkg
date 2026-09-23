"""Run bounded, advisory-only Lintian scans without installing packages."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    **kwargs: object,
) -> str:
    """Scan packages and return only the summary; see scan_packages."""
    summary, _ = scan_packages(packages, report, **kwargs)
    return summary


def scan_packages(
    packages: list[Path],
    report: Path,
    *,
    total_timeout: float = 600,
    package_timeout: float = 120,
    kill_grace: float = 5,
    jobs: int | None = None,
    command: tuple[str, ...] = (
        "lintian",
        "--no-cfg",
        "--allow-root",
        "--fail-on",
        "error,warning",
    ),
) -> tuple[str, bool]:
    """Scan each package under per-package and overall budgets, writing the report."""
    counts = {
        "completed": 0,
        "findings": 0,
        "timed_out": 0,
        "failed": 0,
        "unscanned": 0,
    }
    width = max(1, (os.cpu_count() or 2) if jobs is None else jobs)
    deadline = time.monotonic() + total_timeout
    console = threading.Lock()
    with report.open("w") as output:

        def log(message: str) -> None:
            """Echo progress to stdout and the report file."""
            with console:
                print(message, flush=True)
            output.write(message + "\n")
            output.flush()

        def announce(message: str) -> None:
            """Echo live scan progress to stdout; the report keeps input order."""
            with console:
                print(message, flush=True)

        log(f"Lintian advisory scan: {len(packages)} packages ({width} jobs)")

        def scan_one(index: int, package: Path) -> tuple[int, str, str]:
            """Run one scan and return its index, outcome, and report section."""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return index, "unscanned", ""
            start_line = f"START {index + 1}/{len(packages)} {package}"
            started = time.monotonic()
            announce(start_line)
            with tempfile.TemporaryFile() as capture:
                try:
                    process = subprocess.Popen(
                        [*command, str(package.resolve())],
                        stdout=capture,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as error:
                    failed_line = f"FAILED {package}: {error}"
                    announce(failed_line)
                    return index, "failed", f"{start_line}\n{failed_line}\n"
                try:
                    status = process.wait(timeout=min(package_timeout, remaining))
                except subprocess.TimeoutExpired:
                    stop_process_group(process, kill_grace)
                    outcome = "timed_out"
                    result = "TIMED OUT"
                else:
                    # Lintian uses 2 for policy findings and 1 for runtime errors.
                    if status in (0, 2):
                        outcome = "findings" if status == 2 else "ok"
                    else:
                        outcome = "failed"
                    result = f"exit status {status}"
                capture.seek(0)
                captured = capture.read().decode(errors="replace")
            end_line = f"END {package}: {result}; {time.monotonic() - started:.2f}s"
            announce(end_line)
            return index, outcome, f"{start_line}\n{captured}{end_line}\n"

        pending: dict[int, tuple[str, str]] = {}
        next_index = 0
        budget_logged = False

        def emit(index: int, outcome: str, section: str) -> None:
            """Append finished sections to the report in package order."""
            nonlocal next_index, budget_logged
            pending[index] = (outcome, section)
            while next_index in pending:
                pending_outcome, text = pending.pop(next_index)
                if pending_outcome == "unscanned":
                    if not budget_logged:
                        budget_logged = True
                        log("Overall scan budget exhausted.")
                    log(f"UNSCANNED {packages[next_index]}")
                else:
                    output.write(text)
                    output.flush()
                next_index += 1

        with ThreadPoolExecutor(max_workers=width) as executor:
            futures = [
                executor.submit(scan_one, index, package)
                for index, package in enumerate(packages)
            ]
            for future in as_completed(futures):
                index, outcome, section = future.result()
                if outcome == "ok":
                    counts["completed"] += 1
                elif outcome == "findings":
                    counts["completed"] += 1
                    counts["findings"] += 1
                else:
                    counts[outcome] += 1
                emit(index, outcome, section)

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
    return summary, incomplete


def positive_seconds(value: str) -> float:
    """Argparse type rejecting non-finite or non-positive timeout values."""
    seconds = float(value)
    if not 0 < seconds < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return seconds


def positive_int(value: str) -> int:
    """Argparse type rejecting non-positive job counts."""
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("jobs must be positive")
    return count


def main(argv: list[str] | None = None) -> int:
    """Scan --artifacts, append the summary to GITHUB_STEP_SUMMARY when set.

    Status: 0 complete scan, 1 incomplete (the scan must not silently degrade to
    zero coverage); lintian findings remain advisory and never fail the run.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("lintian-report.txt"))
    parser.add_argument("--total-timeout", type=positive_seconds, default=600)
    parser.add_argument("--package-timeout", type=positive_seconds, default=120)
    parser.add_argument("--kill-grace", type=positive_seconds, default=5)
    parser.add_argument("--jobs", type=positive_int, default=None)
    args = parser.parse_args(argv)
    summary, incomplete = scan_packages(
        sorted(args.artifacts.rglob("*.deb")),
        args.report,
        total_timeout=args.total_timeout,
        package_timeout=args.package_timeout,
        kill_grace=args.kill_grace,
        jobs=args.jobs,
    )
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(summary + "\n")
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
