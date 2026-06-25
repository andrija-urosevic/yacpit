#!/usr/bin/env python3

import argparse
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_TIMEOUT = 1.0
DEFAULT_COMPILER = "g++"
DEFAULT_SOURCE = "main.cpp"


@dataclass(frozen=True)
class Task:
    name: str
    student_dir: Path
    tests_dir: Path
    source_file: Path


@dataclass(frozen=True)
class TestCase:
    test_id: str
    input_file: Path
    output_file: Path


@dataclass(frozen=True)
class TestRunResult:
    status: str
    details: Optional[str] = None


@dataclass(frozen=True)
class TaskSummary:
    task_name: str
    compiled: bool
    passed: int
    total: int
    compile_error: Optional[str] = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./tester.py",
        description=(
            "Exam Tester: Compile and test student's exam solutions. "
        ),
    )
    parser.add_argument(
        "task_names",
        nargs="*",
        metavar="TASK",
        help=(
            "Optional task names or numeric prefixes to test. "
            "If omitted, all discovered tasks are tested."
        ),
    )
    parser.add_argument(
        "--student-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the student's directory. Default: current directory.",
    )
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=None,
        help="Path to the tests directory. Default: sibling 'tests' directory.",
    )
    parser.add_argument(
        "--source-name",
        default=DEFAULT_SOURCE,
        help=f"Source file name inside each task directory. Default: {DEFAULT_SOURCE}.",
    )
    parser.add_argument(
        "--compiler",
        default=DEFAULT_COMPILER,
        help=f"C++ compiler to use. Default: {DEFAULT_COMPILER}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Per-test timeout in seconds. Default: {DEFAULT_TIMEOUT}.",
    )
    parser.add_argument(
        "--strict-whitespace",
        action="store_true",
        help="Compare outputs exactly after newline normalization.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print mismatch details and compiler stderr on failures.",
    )
    return parser


def normalize_output(text: str, strict_whitespace: bool) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if strict_whitespace:
        return normalized
    return " ".join(normalized.split())


def numeric_prefix(name: str) -> str:
    match = re.match(r"^(\d+)", name)
    return match.group(1) if match else ""


def task_sort_key(name: str) -> Tuple[int, str]:
    prefix = numeric_prefix(name)
    if prefix:
        return int(prefix), name
    return 10**9, name


def resolve_tests_dir(student_dir: Path, tests_dir: Optional[Path]) -> Path:
    if tests_dir is not None:
        return tests_dir.resolve()
    return (student_dir.resolve().parent / "tests").resolve()


def discover_all_tasks(student_dir: Path, tests_dir: Path, source_name: str) -> List[Task]:
    tasks: List[Task] = []
    for path in sorted(student_dir.iterdir(), key=lambda p: task_sort_key(p.name)):
        if not path.is_dir():
            continue
        source_file = path / source_name
        task_tests_dir = tests_dir / path.name
        if not source_file.is_file():
            continue
        if not task_tests_dir.is_dir():
            continue
        tasks.append(
            Task(
                name=path.name,
                student_dir=path,
                tests_dir=task_tests_dir,
                source_file=source_file,
            )
        )
    return tasks


def select_tasks(all_tasks: Sequence[Task], requested_names: Sequence[str]) -> List[Task]:
    if not requested_names:
        return list(all_tasks)

    available_by_name = {task.name: task for task in all_tasks}
    selected: List[Task] = []
    missing: List[str] = []

    for requested in requested_names:
        task = available_by_name.get(requested)
        if task is None:
            candidates = [
                candidate
                for candidate in all_tasks
                if candidate.name == requested or numeric_prefix(candidate.name) == requested
            ]
            if len(candidates) == 1:
                task = candidates[0]
        if task is None:
            missing.append(requested)
            continue
        if task not in selected:
            selected.append(task)

    if missing:
        available = ", ".join(task.name for task in all_tasks) or "<none>"
        missing_display = ", ".join(missing)
        raise SystemExit(
            f"Unknown task selection: {missing_display}\nAvailable tasks: {available}"
        )

    return selected


def discover_tests(task: Task) -> List[TestCase]:
    inputs: Dict[str, Path] = {}
    outputs: Dict[str, Path] = {}

    for path in task.tests_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix == ".in":
            inputs[path.stem] = path
        elif path.suffix == ".out":
            outputs[path.stem] = path

    shared_ids = sorted(set(inputs) & set(outputs), key=task_sort_key)
    return [
        TestCase(test_id=test_id, input_file=inputs[test_id], output_file=outputs[test_id])
        for test_id in shared_ids
    ]


def compile_task(task: Task, build_dir: Path, compiler: str) -> Tuple[Optional[Path], str]:
    exe_path = build_dir / f"{task.name}.exe"
    compile_cmd = [
        compiler,
        "-std=c++17",
        "-O2",
        "-Wall",
        "-Wextra",
        "-pedantic",
        str(task.source_file),
        "-o",
        str(exe_path),
    ]
    try:
        proc = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            cwd=task.student_dir,
        )
    except FileNotFoundError:
        return None, "Compiler not found: {0}".format(compiler)

    stderr = proc.stderr.strip()
    stdout = proc.stdout.strip()
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if proc.returncode != 0:
        return None, combined
    return exe_path, combined


def run_test_case(
    exe_path: Path,
    test_case: TestCase,
    timeout: float,
    strict_whitespace: bool,
) -> TestRunResult:
    with test_case.input_file.open("rb") as fin:
        try:
            proc = subprocess.run(
                [str(exe_path)],
                stdin=fin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return TestRunResult(status="TLE", details=f"exceeded {timeout:.2f}s")

    if proc.returncode != 0:
        details = f"exit code {proc.returncode}"
        stderr = proc.stderr.strip()
        if stderr:
            details = f"{details}; stderr:\n{stderr}"
        return TestRunResult(status="RTE", details=details)

    expected = test_case.output_file.read_text(encoding="utf-8")
    actual = proc.stdout
    if normalize_output(actual, strict_whitespace) == normalize_output(expected, strict_whitespace):
        return TestRunResult(status="OK")

    details = format_mismatch_details(expected, actual)
    stderr = proc.stderr.strip()
    if stderr:
        details = f"{details}\nstderr:\n{stderr}"
    return TestRunResult(status="WA", details=details)


def format_mismatch_details(expected: str, actual: str) -> str:
    expected_preview = preview_text(expected)
    actual_preview = preview_text(actual)
    return f"expected:\n{expected_preview}\nactual:\n{actual_preview}"


def preview_text(text: str, limit: int = 400) -> str:
    if not text:
        return "<empty>"
    snippet = text[:limit]
    if len(text) > limit:
        snippet += "\n...<truncated>"
    return repr(snippet)


def print_task_header(task_name: str) -> None:
    print(task_name)
    print("-" * len(task_name))


def run_task(
    task: Task,
    build_dir: Path,
    compiler: str,
    timeout: float,
    strict_whitespace: bool,
    verbose: bool,
) -> TaskSummary:
    tests = discover_tests(task)
    print_task_header(task.name)

    if not tests:
        print("No matching .in/.out test pairs found.\n")
        return TaskSummary(task_name=task.name, compiled=False, passed=0, total=0)

    exe_path, compiler_output = compile_task(task, build_dir, compiler)
    if exe_path is None:
        print("Compilation: FAILED")
        if compiler_output and verbose:
            print(compiler_output)
        print()
        return TaskSummary(
            task_name=task.name,
            compiled=False,
            passed=0,
            total=len(tests),
            compile_error=compiler_output or "Compilation failed.",
        )

    print("Compilation: OK")
    passed = 0
    for test_case in tests:
        result = run_test_case(
            exe_path=exe_path,
            test_case=test_case,
            timeout=timeout,
            strict_whitespace=strict_whitespace,
        )
        print(f"Test {test_case.test_id}: {result.status}")
        if result.status == "OK":
            passed += 1
        elif verbose and result.details:
            print(result.details)

    print(f"Summary: {passed}/{len(tests)} passed\n")
    return TaskSummary(task_name=task.name, compiled=True, passed=passed, total=len(tests))


def print_overall_summary(summaries: Sequence[TaskSummary]) -> None:
    total_tasks = len(summaries)
    compiled_tasks = sum(1 for summary in summaries if summary.compiled)
    compile_failures = sum(1 for summary in summaries if summary.compile_error is not None)
    passed_tests = sum(summary.passed for summary in summaries)
    total_tests = sum(summary.total for summary in summaries)
    fully_passed_tasks = sum(
        1
        for summary in summaries
        if summary.compiled and summary.total > 0 and summary.passed == summary.total
    )

    print("Overall")
    print("-------")
    print(f"Tasks tested: {total_tasks}")
    print(f"Tasks compiled: {compiled_tasks}")
    print(f"Compile failures: {compile_failures}")
    print(f"Task score: {fully_passed_tasks}/{total_tasks}")
    print(f"Test score: {passed_tests}/{total_tests}")


def validate_roots(student_dir: Path, tests_dir: Path) -> None:
    if not student_dir.is_dir():
        raise SystemExit(f"Student directory not found: {student_dir}")
    if not tests_dir.is_dir():
        raise SystemExit(f"Tests directory not found: {tests_dir}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    student_dir = args.student_dir.resolve()
    tests_dir = resolve_tests_dir(student_dir, args.tests_dir)
    validate_roots(student_dir, tests_dir)

    all_tasks = discover_all_tasks(student_dir, tests_dir, args.source_name)
    if not all_tasks:
        print(
            "No tasks discovered. Expected directories like "
            f"'{student_dir}/01_task_name/{args.source_name}' and "
            f"'{tests_dir}/01_task_name/01.in'."
        )
        return 1

    selected_tasks = select_tasks(all_tasks, args.task_names)
    if not selected_tasks:
        print("No tasks selected.")
        return 1

    with tempfile.TemporaryDirectory(prefix="exam_tester_") as tmpdir:
        build_dir = Path(tmpdir)
        summaries = [
            run_task(
                task=task,
                build_dir=build_dir,
                compiler=args.compiler,
                timeout=args.timeout,
                strict_whitespace=args.strict_whitespace,
                verbose=args.verbose,
            )
            for task in selected_tasks
        ]

    print_overall_summary(summaries)
    return 0 if all(summary.compiled and summary.passed == summary.total for summary in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
