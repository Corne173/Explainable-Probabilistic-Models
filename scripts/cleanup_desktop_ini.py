from __future__ import annotations

import argparse
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    raise SystemExit("Could not find the repository root (.git directory).")


def collect_desktop_ini_files(repo_root: Path, include_worktree: bool) -> list[Path]:
    matches: list[Path] = []

    git_dir = repo_root / ".git"
    if git_dir.exists():
        matches.extend(path for path in git_dir.rglob("desktop.ini") if path.is_file())

    if include_worktree:
        for path in repo_root.rglob("desktop.ini"):
            if not path.is_file():
                continue
            if git_dir in path.parents:
                continue
            matches.append(path)

    return sorted(set(matches))


def remove_files(paths: list[Path], repo_root: Path) -> tuple[int, list[str]]:
    removed = 0
    failed: list[str] = []

    for path in paths:
        try:
            path.unlink()
            removed += 1
            print(f"Removed: {path.relative_to(repo_root)}")
        except OSError as exc:
            failed.append(f"{path.relative_to(repo_root)} -> {exc}")

    return removed, failed


def ensure_gitignore_rule(repo_root: Path) -> bool:
    gitignore_path = repo_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    lines = existing.splitlines()

    required_rules = [
        "desktop.ini",
        "**/desktop.ini",
    ]

    missing_rules = [rule for rule in required_rules if rule not in lines]
    if not missing_rules:
        return False

    content = existing
    if content and not content.endswith(("\n", "\r")):
        content += "\n"

    if content and not content.endswith("\n\n"):
        content += "\n"

    content += "# Explicitly ignore Windows shell metadata\n"
    for rule in missing_rules:
        content += f"{rule}\n"

    gitignore_path.write_text(content, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove stray desktop.ini files from this repository, including inside .git."
    )
    parser.add_argument(
        "--git-only",
        action="store_true",
        help="Only remove desktop.ini files from the .git directory.",
    )
    args = parser.parse_args()

    repo_root = find_repo_root(Path.cwd().resolve())
    include_worktree = not args.git_only

    matches = collect_desktop_ini_files(repo_root, include_worktree=include_worktree)
    if not matches:
        print("No desktop.ini files found.")
    else:
        removed, failed = remove_files(matches, repo_root)
        print(f"\nRemoved {removed} desktop.ini file(s).")
        if failed:
            print("\nCould not remove:")
            for item in failed:
                print(f"  - {item}")

    updated = ensure_gitignore_rule(repo_root)
    if updated:
        print("Updated .gitignore with desktop.ini ignore rules.")
    else:
        print(".gitignore already contains desktop.ini ignore rules.")

    print(
        "\nNote: .gitignore can prevent desktop.ini in the working tree, "
        "but it cannot stop external sync tools from writing files inside .git."
    )


if __name__ == "__main__":
    main()