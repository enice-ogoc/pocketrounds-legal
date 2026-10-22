#!/usr/bin/env python3
"""Static check for the PocketRounds share pages and preview images.

Parses each generated page, checks the Open Graph and Twitter tags, checks
each preview image, and searches the site for banned phrases. Pre-existing
hits in legacy legal/support pages are reported and do not fail the check.

    python3 tools/check_og.py
"""

from __future__ import annotations

import struct
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_share_pages import (  # noqa: E402
    DESCRIPTION,
    FALLBACK,
    PAGES,
    SITE,
    TESTING,
    image_url,
    output_path,
    page_url,
    render,
    support_url,
)

ROOT = Path(__file__).resolve().parents[1]
MAX_IMAGE_BYTES = 300 * 1024
PNG_SIG = b"\x89PNG\r\n\x1a\n"

# Phrases are joined at runtime so this file does not contain them whole.
def _phrase(*parts: str) -> str:
    return "".join(parts)


BANNED = [
    _phrase("Quiz", "Forge"),
    _phrase("enice@", "ibsx.com"),
    _phrase("Lads to ", "Leaders"),
    _phrase("Miami ", "Valley"),
]

# Files that already contained a banned phrase before these share pages.
# Only the listed phrase is ignored, and only in that file.
LEGACY = {
    "index.html": {_phrase("enice@", "ibsx.com")},
    "privacy-policy.html": {_phrase("enice@", "ibsx.com")},
    "support.html": {_phrase("enice@", "ibsx.com")},
}

REQUIRED_META = [
    "og:title",
    "og:description",
    "og:image",
    "og:image:width",
    "og:image:height",
    "og:image:type",
    "og:image:alt",
    "og:url",
    "og:type",
    "og:site_name",
    "description",
    "twitter:card",
    "twitter:title",
    "twitter:description",
    "twitter:image",
    "twitter:image:alt",
]


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.in_title = False
        self.title_parts: list[str] = []
        self.head_scripts = 0
        self.metas: list[tuple[str, str]] = []
        self.canonical: list[str] = []
        self.ids: dict[str, dict[str, str]] = {}
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "head":
            self.in_head = True
        if tag == "script" and self.in_head:
            self.head_scripts += 1
        if tag == "title" and self.in_head:
            self.in_title = True
        if tag == "meta":
            key = attr.get("property") or attr.get("name") or ""
            if key:
                self.metas.append((key, attr.get("content", "")))
        if tag == "link" and "canonical" in attr.get("rel", "").split():
            self.canonical.append(attr.get("href", ""))
        element_id = attr.get("id")
        if element_id:
            self.ids[element_id] = attr

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    def meta(self, key: str) -> list[str]:
        return [value for name, value in self.metas if name == key]


def png_info(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIG):
        raise ValueError("not a PNG")
    if data[12:16] != b"IHDR":
        raise ValueError("missing IHDR")
    length = int.from_bytes(data[8:12], "big")
    if length != 13:
        raise ValueError("unexpected IHDR length")
    width, height = struct.unpack(">II", data[16:24])
    if not data.endswith(b"IEND\xaeB`\x82"):
        raise ValueError("PNG missing IEND")
    return width, height, len(data)


def check_pages(failures: list[str], lines: list[str]) -> None:
    lines.append("Pages")
    for page in PAGES:
        path = output_path(ROOT, page)
        rel = path.relative_to(ROOT).as_posix()
        expected = render(page)
        if not path.is_file():
            failures.append(f"{rel} is missing")
            lines.append(f"  FAIL {rel} missing")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            failures.append(f"{rel} does not match the template output")
        parser = PageParser()
        parser.feed(actual)
        title = "".join(parser.title_parts).strip()
        url = page_url(page)
        image = image_url(page)
        problems: list[str] = []

        if parser.head_scripts:
            problems.append("script in head")
        if title != page["title"]:
            problems.append(f"title {title!r}")
        if parser.canonical != [url]:
            problems.append(f"canonical {parser.canonical!r}")

        found: dict[str, list[str]] = {key: parser.meta(key) for key in REQUIRED_META}
        for key in REQUIRED_META:
            if len(found[key]) != 1:
                problems.append(f"{key} count {len(found[key])}")

        def one(key: str) -> str:
            values = found[key]
            return values[0] if len(values) == 1 else ""

        expectations = {
            "description": DESCRIPTION,
            "og:title": page["title"],
            "og:description": DESCRIPTION,
            "og:image": image,
            "og:image:width": "1200",
            "og:image:height": "630",
            "og:image:type": "image/png",
            "og:image:alt": page["alt"],
            "og:url": url,
            "og:type": "website",
            "og:site_name": "PocketRounds",
            "twitter:card": "summary_large_image",
            "twitter:title": page["title"],
            "twitter:description": DESCRIPTION,
            "twitter:image": image,
            "twitter:image:alt": page["alt"],
        }
        for key, want in expectations.items():
            if one(key) != want:
                problems.append(f"{key}={one(key)!r}")

        if not image.startswith("https://") or not url.startswith("https://"):
            problems.append("url is not absolute https")
        if "#" in url or not url.endswith("/"):
            problems.append("og:url must end with / and have no fragment")
        # CSS in the head uses #hex colors. The code must stay out of tags.
        tagged = [title, *parser.canonical]
        tagged.extend(value for _key, value in parser.metas)
        for value in tagged:
            if "#" in value or "code=" in value or "#c=" in value:
                problems.append(f"tag contains a code or fragment: {value!r}")
        if page["label"]:
            if page["label"] not in actual:
                problems.append("missing label")
        elif 'class="label"' in actual:
            problems.append("app page has a label strip")
        text = "".join(parser.text_parts)
        if FALLBACK not in text or TESTING not in text:
            problems.append("missing landing copy")
        if "Open PocketRounds" not in text:
            problems.append("missing Open PocketRounds")
        open_link = parser.ids.get("open", {})
        if open_link.get("href") != url:
            problems.append(f"open href {open_link.get('href')!r}")
        if support_url() not in actual:
            problems.append("missing support link")
        lowered = actual.lower()
        for snippet in ("fetch(", "xmlhttprequest", "mailto:", "http://", "<img", "script src"):
            if snippet in lowered:
                problems.append(f"unexpected {snippet}")
        for phrase in BANNED:
            if phrase.lower() in lowered:
                problems.append(f"banned phrase in page: {phrase}")

        image_path = ROOT / "og" / page["image"]
        try:
            width, height, size = png_info(image_path)
        except (OSError, ValueError) as exc:
            problems.append(f"image {exc}")
            width = height = size = 0
        else:
            if (width, height) != (1200, 630):
                problems.append(f"image {width}x{height}")
            if size >= MAX_IMAGE_BYTES:
                problems.append(f"image {size} bytes")
            if not page["image"].endswith("-v2.png"):
                problems.append("image is not a v2 file")

        if problems:
            failures.extend(f"{rel}: {item}" for item in problems)
            lines.append(f"  FAIL {page['path']} {'; '.join(problems)}")
        else:
            lines.append(
                f"  ok   {page['path']}  {page['title']}"
                f"  {image}  {width}x{height} PNG {size} bytes"
            )


def check_banned(failures: list[str], lines: list[str]) -> None:
    lines.append("")
    lines.append("Banned phrases")
    preexisting: list[str] = []
    for dirpath, dirnames, filenames in __import__("os").walk(ROOT):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(ROOT).as_posix()
            blob = path.read_bytes().lower()
            rel_lower = rel.lower()
            for phrase in BANNED:
                needle = phrase.lower().encode("ascii")
                count = blob.count(needle)
                name_hit = phrase.lower() in rel_lower
                if count == 0 and not name_hit:
                    continue
                allowed = phrase in LEGACY.get(rel, set())
                where = f"{rel}: {phrase}"
                if count:
                    where += f" ({count} in file)"
                if name_hit:
                    where += " (in path)"
                if allowed and not name_hit:
                    preexisting.append(where)
                else:
                    failures.append("banned " + where)
    if preexisting:
        for item in sorted(preexisting):
            lines.append("  pre-existing  " + item)
    else:
        lines.append("  pre-existing  none")
    new_hits = [item for item in failures if item.startswith("banned ")]
    if new_hits:
        for item in new_hits:
            lines.append("  NEW  " + item.removeprefix("banned "))
    else:
        lines.append("  new hits  none")


def check_no_v1(failures: list[str], lines: list[str]) -> None:
    v1 = sorted((ROOT / "og").glob("*-v1.png")) if (ROOT / "og").is_dir() else []
    lines.append("")
    lines.append("Placeholder v1 images")
    if v1:
        for path in v1:
            rel = path.relative_to(ROOT).as_posix()
            failures.append("unexpected " + rel)
            lines.append("  NEW  " + rel)
    else:
        lines.append("  none")


def main() -> int:
    failures: list[str] = []
    lines = [
        "PocketRounds share-page check",
        f"SITE = {SITE}",
        "",
    ]
    check_pages(failures, lines)
    check_banned(failures, lines)
    check_no_v1(failures, lines)
    lines.append("")
    if failures:
        lines.append(f"RESULT: FAIL ({len(failures)})")
        print("\n".join(lines))
        return 1
    lines.append("RESULT: PASS")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
