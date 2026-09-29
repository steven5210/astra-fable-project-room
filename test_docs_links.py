"""Check the documentation link graph after the docs reorganization: every
relative link (optionally with a #anchor) in every Markdown file of the
repository must resolve to an existing file or directory, a link into a
Markdown file carrying an anchor must find a heading whose GitHub-style slug
equals that anchor, docs/README.md must link to every Markdown document under
docs/ except itself, and README.md must keep every heading it had before the
reorganization so external anchors keep working.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
PRE_REORGANIZATION_README_HEADINGS = (
    "Project Room", "What each participant owns", "Prerequisites", "Install and authenticate",
    "Configure the private AO backend", "Roles, agreement and delegation", "Normal workflow on AO", "CLI fallback",
    "Operate: status, sync, usage and version checks", "Verification and acceptance versus publication",
    "Current validation limits", "DeepSeek delegate", "Legacy controller", "Qwen delegation (legacy)",
    "Deterministic change-set application", "Private state", "Development and testing")


def heading_slugs(path):
    seen = {}
    slugs = set()
    for title in HEADING.findall(path.read_text(encoding="utf-8")):
        slug = re.sub(r"[^a-z0-9_\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def markdown_files():
    return sorted(path for path in ROOT.rglob("*.md")
                  if not any(part.startswith(".") for part in path.relative_to(ROOT).parts))


class DocsLinkTests(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        problems = []
        for source in markdown_files():
            for target in LINK.findall(source.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("mailto:"):
                    continue
                name, _, anchor = target.partition("#")
                dest = (source.parent / name).resolve() if name else source
                if not dest.exists():
                    problems.append(f"{source.relative_to(ROOT)}: {target} (missing target)")
                    continue
                if anchor and dest.suffix == ".md" and anchor not in heading_slugs(dest):
                    problems.append(f"{source.relative_to(ROOT)}: {target} (missing anchor)")
        self.assertEqual(problems, [])

    def test_readme_keeps_every_pre_reorganization_heading(self):
        headings = HEADING.findall((ROOT / "README.md").read_text(encoding="utf-8"))
        for title in PRE_REORGANIZATION_README_HEADINGS:
            self.assertIn(title, headings)

    def test_docs_index_lists_every_document(self):
        index = (ROOT / "docs" / "README.md").resolve()
        documents = {p.resolve() for p in (ROOT / "docs").rglob("*.md")} - {index}
        linked = set()
        for target in LINK.findall(index.read_text(encoding="utf-8")):
            name = target.partition("#")[0]
            if name.endswith(".md"):
                linked.add((index.parent / name).resolve())
        self.assertEqual(documents, linked)


if __name__ == "__main__":
    unittest.main()
