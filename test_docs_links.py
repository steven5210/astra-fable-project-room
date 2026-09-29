"""Check the documentation link graph after the docs reorganization: every
relative link ending in .md (optionally with a #anchor) in every Markdown file of
the repository must resolve to an existing file, and a link carrying an
anchor must find a heading in the target file whose GitHub-style slug equals
that anchor; a second test requires docs/README.md to link to every Markdown
document under docs/ except itself.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


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
    def test_relative_markdown_links_and_anchors_resolve(self):
        problems = []
        for source in markdown_files():
            for target in LINK.findall(source.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("mailto:"):
                    continue
                name, _, anchor = target.partition("#")
                if name and not name.endswith(".md"):
                    continue
                dest = (source.parent / name).resolve() if name else source
                if not dest.is_file():
                    problems.append(f"{source.relative_to(ROOT)}: {target} (missing file)")
                    continue
                if anchor and anchor not in heading_slugs(dest):
                    problems.append(f"{source.relative_to(ROOT)}: {target} (missing anchor)")
        self.assertEqual(problems, [])

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
