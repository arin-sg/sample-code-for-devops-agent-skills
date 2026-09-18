#!/usr/bin/env python3
"""Validate the ``evals/`` layout of the skills a pull request touches.

Every skill is expected to ship the results of all three evaluation test types,
produced by the (internal) skill evaluation tool and committed as-is::

    evals/
    ├── evals.json                                       # required
    ├── structure/
    │   └── structure-tests-results-v<N>.json            # at least one
    ├── best-practices/
    │   └── v<N>/                                        # at least one complete
    │       ├── benchmark.json
    │       └── iteration-<n>/
    │           └── best-practices-tests-results.json
    └── functional/
        └── v<N>/                                        # at least one complete
            ├── benchmark.json
            ├── evals.json
            └── iteration-<n>/
                └── <scenario>/
                    ├── with_skill/functional-tests-results.json
                    └── without_skill/functional-tests-results.json

``<N>`` is one or more digits (``v1``, ``v12``, ...). A version directory counts
only when *all* of its listed contents are present, and a type passes when at
least one of its version directories is complete — an aborted run left behind
next to a good one is harmless, but a lone aborted run does not satisfy the check.

The assertions stop at that depth on purpose. Requiring anything deeper would
track a tool that is still evolving: iteration counts vary (1, 3 and 4 all appear
in the reference skill's history), scenario directory names come from the skill's
own ``evals.json``, and ``_metadata.json`` is present in all 20 recent runs but
absent from older ones — documented and left unchecked rather than pinned.

Several paths the tool writes are also excluded by ``skills/.gitignore`` and so
never reach a PR at all: the ``cli_debug/`` and ``sdk_debug/`` directories, and
``outputs/classified_output.json`` / ``outputs/metadata.json``. Of the outputs,
only ``journal_records.json`` is committed. See CONTRIBUTING.md.

Rollout is gradual, so a skill's *mode* decides whether a violation fails the
build or is only reported:

  * **enforced** — the skill directory does not exist at the base ref (a skill
    added by this PR), or it already carries the new layout at the base ref (so a
    migrated skill cannot regress). Violations exit non-zero.
  * **legacy** — a pre-existing skill still on the old flat ``evals/`` layout.
    Violations are reported as warnings only, until it is migrated.

Only skills whose files the PR touches are inspected. Skill directories deleted
by the PR, and paths under ``skills/`` that are not skill directories (such as
``skills/.gitignore``), are skipped.

A skill promotes itself from legacy to enforced as soon as its migration lands on
the base branch, so the split needs no maintenance as the backlog shrinks.
``--strict`` enforces on unmigrated skills too, which is a lever for *forcing*
that migration — every PR touching a legacy skill becomes a blocker — rather than
a cleanup step to apply after the fact, when it would be a no-op.

Exit code is 0 when no enforced skill has violations, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


STRUCTURE_RESULT_RE = re.compile(r"^structure-tests-results-v\d+\.json$")
VERSION_DIR_RE = re.compile(r"^v\d+$")

# Presence of any of these at the base ref means the skill has been migrated to
# the new layout, so it is held to it from then on.
MIGRATED_MARKERS = ("evals/structure", "evals/best-practices", "evals/functional")

DOCS_HINT = (
    'See the "Eval Results Layout" section of CONTRIBUTING.md for the expected '
    "evals/ layout."
)


@dataclass
class SkillReport:
    id: str
    enforced: bool
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _repo_root() -> Path:
    """Resolve the repository root from this script's location (.github/scripts/)."""
    return Path(__file__).resolve().parents[2]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True
    )


def _exists_at_ref(repo_root: Path, ref: str, path: str) -> bool:
    """True when ``path`` (file or directory) exists in the tree at ``ref``."""
    return _git(repo_root, "cat-file", "-e", f"{ref}:{path}").returncode == 0


def _diff_base(repo_root: Path, base_ref: str, head_ref: str) -> str:
    """Return the merge base of base_ref and head_ref, or base_ref if unavailable.

    Using the merge base means changes that landed on the base branch after the
    PR branched are not attributed to this PR.
    """
    result = _git(repo_root, "merge-base", base_ref, head_ref)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    print(
        f"warning: no merge base between {base_ref} and {head_ref}; "
        f"diffing against {base_ref} directly",
        file=sys.stderr,
    )
    return base_ref


def changed_skill_ids(repo_root: Path, base: str, head_ref: str) -> list[str]:
    """Skill ids under skills/ touched between ``base`` and ``head_ref``.

    Only directories that still exist in the working tree and contain a SKILL.md
    are returned, which drops deleted/renamed-away skills and non-skill paths
    such as ``skills/.gitignore``.
    """
    result = _git(repo_root, "diff", "--name-only", base, head_ref)
    if result.returncode != 0:
        raise SystemExit(
            f"git diff {base} {head_ref} failed: {result.stderr.strip()}"
        )

    ids: list[str] = []
    for line in result.stdout.splitlines():
        parts = Path(line.strip()).parts
        if len(parts) < 3 or parts[0] != "skills":
            continue  # not a file inside a skill directory
        skill_id = parts[1]
        if skill_id in ids:
            continue
        skill_path = repo_root / "skills" / skill_id
        if not skill_path.is_dir():
            continue  # deleted or renamed away by this PR
        if not (skill_path / "SKILL.md").is_file():
            continue  # not a skill directory
        ids.append(skill_id)
    return sorted(ids)


def _version_dirs(parent: Path) -> list[Path]:
    """``v<N>`` subdirectories of ``parent``, ordered numerically (v2 before v10)."""
    return sorted(
        (p for p in parent.iterdir() if p.is_dir() and VERSION_DIR_RE.match(p.name)),
        key=lambda p: int(p.name[1:]),
    )


def _missing_in_best_practices_version(version: Path) -> list[str]:
    """Required contents absent from one ``evals/best-practices/v<N>/`` directory."""
    missing = []
    if not (version / "benchmark.json").is_file():
        missing.append("benchmark.json")
    if not any(version.glob("iteration-*/best-practices-tests-results.json")):
        missing.append("iteration-<n>/best-practices-tests-results.json")
    return missing


def _missing_in_functional_version(version: Path) -> list[str]:
    """Required contents absent from one ``evals/functional/v<N>/`` directory.

    The with_skill / without_skill pair has to be satisfied by the *same* scenario
    directory: a functional run is only meaningful as a comparison of the two.
    Scenario directory names come from the skill's own evals.json, so they are
    matched as a wildcard.
    """
    missing = []
    if not (version / "benchmark.json").is_file():
        missing.append("benchmark.json")
    if not (version / "evals.json").is_file():
        missing.append("evals.json")

    paired = any(
        (scenario / "with_skill" / "functional-tests-results.json").is_file()
        and (scenario / "without_skill" / "functional-tests-results.json").is_file()
        for scenario in version.glob("iteration-*/*")
        if scenario.is_dir()
    )
    if not paired:
        missing.append(
            "iteration-<n>/<scenario>/{with_skill,without_skill}"
            "/functional-tests-results.json"
        )
    return missing


def _check_versioned_type(
    skill_id: str, evals: Path, name: str, missing_fn
) -> list[str]:
    """Require at least one complete ``v<N>/`` run under ``evals/<name>/``.

    "Complete" means ``missing_fn`` reports nothing for it. When no version
    qualifies, the message explains what the highest-numbered one lacks, since
    that is almost always the run the contributor meant to commit.
    """
    target = evals / name
    rel = f"skills/{skill_id}/evals/{name}"
    if not target.is_dir():
        return [f"missing directory `{rel}/`"]

    versions = _version_dirs(target)
    if not versions:
        others = sorted(p.name for p in target.iterdir() if p.is_dir())
        detail = f" (subdirectories found: {', '.join(others[:5])})" if others else ""
        return [f"`{rel}/` has no `v<number>/` directory{detail}"]

    missing_by_version = {v.name: missing_fn(v) for v in versions}
    if any(not m for m in missing_by_version.values()):
        return []  # at least one complete run

    latest = versions[-1].name
    lacks = ", ".join(f"`{m}`" for m in missing_by_version[latest])
    inspected = ", ".join(v.name for v in versions[-5:])
    more = f" (last 5 of {len(versions)}: {inspected})" if len(versions) > 5 else f" ({inspected})"
    return [
        f"`{rel}/` has no complete `v<number>/` run{more}; the latest, `{latest}/`, "
        f"is missing {lacks}"
    ]


def validate_skill(repo_root: Path, skill_id: str) -> list[str]:
    """Return the list of evals-layout violations for one skill (empty == valid)."""
    skill_path = repo_root / "skills" / skill_id
    evals = skill_path / "evals"
    rel_evals = f"skills/{skill_id}/evals"

    if not evals.is_dir():
        return [f"missing directory `{rel_evals}/`"]

    violations: list[str] = []

    if not (evals / "evals.json").is_file():
        violations.append(f"missing file `{rel_evals}/evals.json`")

    structure = evals / "structure"
    if not structure.is_dir():
        violations.append(f"missing directory `{rel_evals}/structure/`")
    elif not any(
        p.is_file() and STRUCTURE_RESULT_RE.match(p.name) for p in structure.iterdir()
    ):
        violations.append(
            f"`{rel_evals}/structure/` has no `structure-tests-results-v<number>.json` file"
        )

    violations += _check_versioned_type(
        skill_id, evals, "best-practices", _missing_in_best_practices_version
    )
    violations += _check_versioned_type(
        skill_id, evals, "functional", _missing_in_functional_version
    )

    return violations


def build_reports(
    repo_root: Path, skill_ids: list[str], base: str, enforce_all: bool
) -> list[SkillReport]:
    reports: list[SkillReport] = []
    for skill_id in skill_ids:
        rel_dir = f"skills/{skill_id}"
        is_new = not _exists_at_ref(repo_root, base, rel_dir)
        was_migrated = any(
            _exists_at_ref(repo_root, base, f"{rel_dir}/{marker}")
            for marker in MIGRATED_MARKERS
        )
        enforced = enforce_all or is_new or was_migrated
        reports.append(
            SkillReport(
                id=skill_id,
                enforced=enforced,
                violations=validate_skill(repo_root, skill_id),
            )
        )
    return reports


def _annotate(report: SkillReport) -> None:
    """Emit GitHub Actions annotations, one per violation.

    Deliberately emitted without a ``file=`` anchor. Every violation is about a
    path that is *absent*, and attaching the annotation to an existing file (the
    skill's SKILL.md was the only candidate present in the diff) wrongly implies
    that file is at fault. Without an anchor the annotation surfaces on the check
    and run summary instead of as an inline marker on an unrelated line.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    level = "error" if report.enforced else "warning"
    for violation in report.violations:
        title = f"Skill evals layout: {report.id}"
        # Annotations are single-line; strip any embedded newlines.
        message = violation.replace("\n", " ")
        print(f"::{level} title={title}::{message} — {DOCS_HINT}")


def _write_summary(reports: list[SkillReport], failed: list[SkillReport]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = ["## Skill evals layout check", ""]
    if not reports:
        lines.append("No skill directories touched by this PR. Nothing to check.")
    else:
        lines.append("| Skill | Mode | Result |")
        lines.append("| --- | --- | --- |")
        for r in reports:
            mode = "enforced" if r.enforced else "legacy (warn only)"
            if r.ok:
                result = "pass"
            elif r.enforced:
                result = f"**fail** — {len(r.violations)} issue(s)"
            else:
                result = f"warn — {len(r.violations)} issue(s)"
            lines.append(f"| `{r.id}` | {mode} | {result} |")
        lines.append("")
        for r in reports:
            if r.ok:
                continue
            heading = "Failures" if r.enforced else "Warnings"
            lines.append(f"### `{r.id}` — {heading}")
            lines += [f"- {v}" for v in r.violations]
            lines.append("")
        if failed:
            lines.append(DOCS_HINT)

    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Base ref of the pull request (default: origin/main).",
    )
    parser.add_argument(
        "--head-ref",
        default="HEAD",
        help="Head ref of the pull request (default: HEAD).",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=None,
        metavar="ID",
        help=(
            "Validate this skill id instead of deriving the list from the diff. "
            "Repeatable; the named skills are always enforced. Useful locally."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail on unmigrated legacy skills too, instead of only warning. Use it "
            "to force migration: every PR touching a legacy skill becomes a "
            "blocker. Once all skills are migrated this flag has no effect."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()

    if args.skill:
        reports = [
            SkillReport(id=s, enforced=True, violations=validate_skill(repo_root, s))
            for s in sorted(set(args.skill))
        ]
    else:
        base = _diff_base(repo_root, args.base_ref, args.head_ref)
        skill_ids = changed_skill_ids(repo_root, base, args.head_ref)
        reports = build_reports(repo_root, skill_ids, base, args.strict)

    if not reports:
        print("No skill directories touched by this PR; evals layout check skipped.")
        _write_summary(reports, [])
        return 0

    failed = [r for r in reports if r.enforced and not r.ok]
    warned = [r for r in reports if not r.enforced and not r.ok]

    for r in reports:
        mode = "enforced" if r.enforced else "legacy"
        if r.ok:
            print(f"PASS  {r.id} ({mode})")
        else:
            label = "FAIL" if r.enforced else "WARN"
            print(f"{label}  {r.id} ({mode})")
            for v in r.violations:
                print(f"        - {v}")
        _annotate(r)

    _write_summary(reports, failed)

    print(
        f"\n{len(reports)} skill(s) checked: "
        f"{len(reports) - len(failed) - len(warned)} pass, "
        f"{len(failed)} fail, {len(warned)} warn.",
        file=sys.stderr,
    )

    if failed:
        print(
            "\nMerging is blocked until the skill(s) above ship a complete evals/ "
            f"layout. {DOCS_HINT}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
