"""
Lightweight static scanning of generated Spring controllers. Used by the
contract guardrail ("OpenAPI contract matches implementation") and by the
codebase-reasoning agent's inventory. Regex-based on purpose: no Java
toolchain needed, runs in milliseconds, unit-testable.
"""
from __future__ import annotations

import re

HTTP_METHODS = ("get", "post", "put", "delete", "patch")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

_MAPPING_RE = re.compile(
    r"@(?P<kind>Get|Post|Put|Delete|Patch|Request)Mapping\s*(?:\((?P<args>[^)]*)\))?", re.DOTALL
)
_STR_RE = re.compile(r'"([^"]*)"')
_CLASS_RE = re.compile(r"\b(?:class|interface|record)\s+\w+")


def _collapse_placeholders(path: str) -> str:
    """{shortCode} / {code:[a-z]+} / {code:[A-Za-z0-9_-]{3,32}} -> {}. Braces are balanced by depth,
    because a regex constraint may itself contain braces (a quantifier)."""
    out, depth = [], 0
    for ch in path:
        if ch == "{":
            if depth == 0:
                out.append("{")
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                out.append("}")
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def normalize_path(path: str) -> str:
    path = _collapse_placeholders(path)
    path = re.sub(r"/{2,}", "/", "/" + path.strip("/"))
    return path if path == "/" else path.rstrip("/")


def _paths_from_args(args: str | None) -> list[str]:
    if not args:
        return [""]
    # keep only the path strings: everything before method=/produces=/consumes=/params=/headers=
    path_part = re.split(r"\b(?:method|produces|consumes|params|headers|name)\s*=", args)[0]
    found = _STR_RE.findall(path_part)
    return found or [""]


def _request_method(args: str | None) -> str | None:
    if not args:
        return None
    m = re.search(r"RequestMethod\.(\w+)", args)
    return m.group(1).upper() if m else None


def extract_endpoints(files: dict[str, str]) -> set[tuple[str, str]]:
    """{(METHOD, normalized_path)} implemented by @RestController/@Controller
    classes in `files` (path -> content)."""
    endpoints: set[tuple[str, str]] = set()
    for path, content in files.items():
        if not path.endswith(".java") or "/src/test/" in path or path.startswith("src/test/"):
            continue
        if "@RestController" not in content and "@Controller" not in content:
            continue
        cls = _CLASS_RE.search(content)
        head = content[: cls.start()] if cls else ""
        body = content[cls.start():] if cls else content

        prefixes = [""]
        for m in _MAPPING_RE.finditer(head):
            if m.group("kind") == "Request":
                prefixes = _paths_from_args(m.group("args"))

        for m in _MAPPING_RE.finditer(body):
            kind = m.group("kind")
            if kind == "Request":
                method = _request_method(m.group("args"))
                if method is None:
                    continue
            else:
                method = kind.upper()
            for prefix in prefixes:
                for sub in _paths_from_args(m.group("args")):
                    endpoints.add((method, normalize_path(prefix + "/" + sub)))
    return endpoints


def contract_endpoints(openapi: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path, item in (openapi or {}).get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method.lower() in HTTP_METHODS:
                out.add((method.upper(), normalize_path(path)))
    return out


def request_body_types(files: dict[str, str]) -> set[str]:
    types: set[str] = set()
    for path, content in files.items():
        if path.endswith(".java"):
            for m in re.finditer(r"@RequestBody\s+(?:@Valid(?:ated)?\s+)?(?:final\s+)?([A-Z]\w*)", content):
                types.add(m.group(1))
    return types
