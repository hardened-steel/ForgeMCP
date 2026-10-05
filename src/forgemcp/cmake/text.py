"""Plain-text CMake settings, execution outcomes, and complete CTest cases."""

from typing import Literal

from forgemcp.toolchain.tools import ctest

from .models import (
    CMakeProfile,
    CMakeConfigureResult,
    CMakeBuildResult,
    CMakeTestResult,
)


def render_profiles(profiles: list[CMakeProfile]) -> str:
    """Show manual settings and independent preset selections for every profile."""
    lines = [f"Available CMake profiles: {len(profiles)}."]
    for profile in profiles:
        lines.extend(
            [
                "",
                profile.name,
                f"  Toolset: {profile.toolset_id}",
                f"  Mode: {profile.mode}",
                f"  Configuration: {profile.configuration or 'Not specified'}",
                f"  Generator: {profile.generator or 'Not specified'}",
                f"  Build directory: {profile.build_directory or 'Not specified'}",
                f"  Configure presets: {', '.join(profile.configure_presets) or 'None'}",
                f"  Build presets: {', '.join(profile.build_presets) or 'None'}",
                f"  Test presets: {', '.join(profile.test_presets) or 'None'}",
            ]
        )
    return "\n".join(lines)


def render_executions(
    results: list[CMakeConfigureResult] | list[CMakeBuildResult] | list[CMakeTestResult],
    operation: Literal["Configure", "Build", "CTest"],
) -> str:
    """Report each execution independently, preserving errors and command-log references."""
    failed = sum(result.error is not None for result in results)
    lines = [
        f"{operation} results: {len(results) - failed} executions succeeded; {failed} failed."
    ]
    for result in results:
        label = result.profile
        if result.preset is not None:
            label += f" (preset: {result.preset})"
        lines.extend(["", f"{label}: {'Succeeded' if result.error is None else 'Failed'}"])
        if result.error is not None:
            lines.append("  Error: " + result.error.replace("\n", "\n    "))
        if result.process_id is not None:
            lines.append(f"  Command output: process_get(process_id={result.process_id})")
        else:
            lines.append("  Process: not available")
        if isinstance(result, CMakeConfigureResult):
            lines.extend(
                [
                    f"  Build directory: {result.build_directory or 'Unavailable'}",
                    f"  Compilation database: {result.compilation_database or 'Unavailable'}",
                ]
            )
        else:
            lines.append(f"  Configuration: {result.configuration or 'Not specified'}")
            if isinstance(result, CMakeBuildResult):
                completed = result.completed_steps
                total = result.total_steps
                lines.append(
                    f"  Steps: {completed if completed is not None else 'Unknown'} completed; "
                    f"{total if total is not None else 'unknown'} total"
                )
            else:
                lines.extend(render_test_cases(result.tests))
    return "\n".join(lines)


def render_test_cases(tests: list[ctest.TestCase]) -> list[str]:
    """Keep all CTest cases and explain failures, skips, and unknown durations."""
    counts = {
        status: sum(test.status == status for test in tests)
        for status in ("passed", "failed", "skipped", "not_run")
    }
    lines = [
        f"  Tests: {counts['passed']} passed; {counts['failed']} failed; "
        f"{counts['skipped']} skipped; {counts['not_run']} not run."
    ]
    if not tests:
        lines.append("  No test cases reported.")
    for test in tests:
        duration = (
            f"{test.duration_seconds} s"
            if test.duration_seconds is not None
            else "duration unavailable"
        )
        lines.append(f"    {test.status.replace('_', ' ')}: {test.name} ({duration})")
        if test.message is not None:
            lines.append("      " + test.message.replace("\n", "\n      "))
    return lines
