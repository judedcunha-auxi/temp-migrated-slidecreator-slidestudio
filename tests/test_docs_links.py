"""Every relative link in the project's Markdown resolves, including `#anchor` fragments.

A renamed file or heading breaks links nobody notices until a reader follows one.
This reads the files, needs no server, and fails with the file, line and target.
External (`http`, `mailto`) links are not fetched: a unit test must not need the network.
Also holds the documentation set the repo standards require.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

_DOC_ROOTS = ("docs", "scripts")
_TOP_LEVEL = ("README.md",)

# [text](target) and [text](<target with spaces>); images share the syntax.
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_FENCE = re.compile(r"^\s*(```|~~~)")

REQUIRED_DOCS = ("README.md", "how-it-works.md", "development.md", "deployment.md", "route-controls.md")


def _markdown_files() -> list[Path]:
    files = [ROOT / name for name in _TOP_LEVEL if (ROOT / name).exists()]
    for folder in _DOC_ROOTS:
        files += sorted((ROOT / folder).rglob("*.md"))
    return files


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower-case, markup and punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`|\*\*|\*|~~", "", heading.strip()).lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.strip().replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            slug = _slug(match.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links(path: Path) -> Iterator[tuple[int, str]]:
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _LINK.finditer(re.sub(r"`[^`]*`", "", line)):
            yield number, match.group(1)


def test_the_required_documents_exist():
    for name in REQUIRED_DOCS:
        assert (ROOT / "docs" / name).is_file(), f"docs/{name} is required by the repo standards"
    assert (ROOT / "scripts" / "README.md").is_file()


def test_the_root_readme_links_to_the_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = {target for _, target in _links(ROOT / "README.md")}
    assert readme
    for name in REQUIRED_DOCS:
        assert f"docs/{name}" in targets, f"README.md does not link docs/{name}"


def test_every_relative_link_and_anchor_resolves():
    broken = []
    for path in _markdown_files():
        for number, target in _links(path):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("//"):
                continue  # http(s), mailto and the like: not fetched here
            file_part, _, fragment = target.partition("#")
            destination = path if not file_part else (path.parent / unquote(file_part)).resolve()
            label = f"{path.relative_to(ROOT)}:{number} -> {target}"
            if not destination.exists():
                broken.append(f"{label} (no such file)")
            elif fragment and destination.suffix == ".md" and unquote(fragment).lower() not in _anchors(destination):
                broken.append(f"{label} (no such heading)")
    assert not broken, "broken documentation links:\n  " + "\n  ".join(broken)


def test_the_anchor_rule_matches_how_github_slugs_headings():
    assert _slug("Deploying to staging") == "deploying-to-staging"
    assert _slug("Redis (`slideforge-redis`)") == "redis-slideforge-redis"
    assert _slug("1. What runs, and where") == "1-what-runs-and-where"
