"""
Safe file IO for generated code. The model proposes paths; this module
decides what is allowed to touch disk (no traversal, no .git, only the
project's source/doc/packaging locations).
"""
from __future__ import annotations

import re
from pathlib import Path

ALLOWED_PREFIXES = ("src/", "docs/")
ALLOWED_ROOT_FILES = {"pom.xml", "Dockerfile", "docker-compose.yml", ".dockerignore", "README.md", "CHANGELOG.md", ".gitignore"}
PROTECTED = {"mvnw", "mvnw.cmd"}  # scaffolded by Initializr; never model-authored
READ_SUFFIXES = (".java", ".xml", ".yml", ".yaml", ".properties", ".sql", ".md")
SKIP_DIRS = {"target", ".git", ".mvn", "node_modules"}

FILE_BLOCK_RE = re.compile(r"===FILE:\s*(?P<path>.+?)\s*===\n(?P<content>.*?)\n===END===", re.DOTALL)


class UnsafePath(ValueError):
    pass


def check_path(rel: str) -> str:
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/") or rel.startswith(".git/") or rel == ".git":
        raise UnsafePath(f"unsafe path from model: {rel!r}")
    if rel in PROTECTED:
        raise UnsafePath(f"protected file: {rel!r}")
    if rel in ALLOWED_ROOT_FILES or rel.startswith(ALLOWED_PREFIXES):
        return rel
    raise UnsafePath(f"path outside allowed locations (src/, docs/, pom.xml, Dockerfile, docker-compose.yml...): {rel!r}")


def parse_file_blocks(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse `===FILE: path=== ... ===END===` blocks. Returns
    (files, problems). A problem is reported for unsafe paths and for
    truncated output (a FILE header without a matching END)."""
    files: dict[str, str] = {}
    problems: list[str] = []
    for m in FILE_BLOCK_RE.finditer(text):
        try:
            rel = check_path(m.group("path"))
        except UnsafePath as exc:
            problems.append(str(exc))
            continue
        content = m.group("content")
        # tolerate a model wrapping the body in a markdown fence
        fence = re.match(r"^```[\w-]*\n(.*)\n```\s*$", content, re.DOTALL)
        files[rel] = (fence.group(1) if fence else content).rstrip("\n") + "\n"
    headers = len(re.findall(r"===FILE:", text))
    ends = len(re.findall(r"===END===", text))
    if headers != ends:
        done = set(files)
        incomplete = [h.strip() for h in re.findall(r"===FILE:\s*(.+?)\s*===", text) if h.strip() not in done]
        problems.append(f"output truncated or malformed: {headers} FILE headers vs {ends} END markers"
                        + (f"; incomplete file(s): {', '.join(incomplete[:5])}" if incomplete else ""))
    return files, problems


DELETE_RE = re.compile(r"===DELETE:\s*(?P<path>.+?)\s*===")


def parse_deletes(text: str) -> tuple[list[str], list[str]]:
    """`===DELETE: path===` directives (a model removing a file it created
    in an earlier attempt). Returns (paths, problems). Applied migrations
    can never be deleted."""
    paths, problems = [], []
    for m in DELETE_RE.finditer(text):
        try:
            rel = check_path(m.group("path"))
        except UnsafePath as exc:
            problems.append(str(exc))
            continue
        if "db/migration/" in rel:
            problems.append(f"refusing to delete migration {rel}")
            continue
        paths.append(rel)
    return paths, problems


def write_files(repo: Path, files: dict[str, str]) -> list[str]:
    written = []
    for rel, content in files.items():
        rel = check_path(rel)
        target = (repo / rel).resolve()
        if repo.resolve() not in target.parents:
            raise UnsafePath(f"path escapes repo: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def read_repo_files(repo: Path, include_tests: bool = True) -> dict[str, str]:
    """Relative-path -> content for source/config/doc/packaging files."""
    out: dict[str, str] = {}
    if not repo.exists():
        return out
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(repo).parts):
            continue
        rel = p.relative_to(repo).as_posix()
        if not include_tests and rel.startswith("src/test/"):
            continue
        if rel in ("Dockerfile", "docker-compose.yml") or p.suffix in READ_SUFFIXES:
            try:
                out[rel] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return out


def existing_migrations(repo: Path) -> list[str]:
    d = repo / "src/main/resources/db/migration"
    return sorted(p.name for p in d.glob("V*__*.sql")) if d.exists() else []


def max_migration_version(repo: Path) -> int:
    versions = [int(m.group(1)) for n in existing_migrations(repo) if (m := re.match(r"V(\d+)__", n))]
    return max(versions, default=0)
