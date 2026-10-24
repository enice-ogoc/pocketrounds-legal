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
    AFTER_INSTALL,
    APP_STORE_URL,
    DESCRIPTION,
    FALLBACK,
    PAGES,
    SITE,
    TESTFLIGHT_PUBLIC_URL,
    TESTING,
    image_url,
    install_block,
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


def check_install_slots(failures: list[str], lines: list[str]) -> None:
    """The two URL slots stay empty until someone sets them. Prove both branches."""
    lines.append("Install slots")
    flight = "https://testflight.apple.com/join/example"
    store = "https://apps.apple.com/app/example"
    empty = install_block("", "")
    flight_only = install_block(flight, "")
    store_only = install_block("", store)
    both = install_block(flight, "  " + store + "  ")
    slot_problems: list[str] = []
    if "testflight" in empty.lower() or "App Store" in empty or TESTING not in empty:
        slot_problems.append("empty slots should be the testing sentence only")
    if flight not in flight_only or "Join on TestFlight" not in flight_only or TESTING not in flight_only:
        slot_problems.append("TestFlight URL should add a link under the testing sentence")
    if (
        store not in store_only
        or "App Store" not in store_only
        or AFTER_INSTALL not in store_only
        or TESTING in store_only
    ):
        slot_problems.append("App Store URL should replace the testing sentence")
    if "testflight" in store_only.lower():
        slot_problems.append("App Store block should not include a TestFlight link")
    if store not in both or TESTING in both or "testflight" in both.lower():
        slot_problems.append("App Store URL should replace the testing block even if TestFlight is set")
    if slot_problems:
        failures.extend(slot_problems)
        for item in slot_problems:
            lines.append("  FAIL " + item)
    else:
        lines.append("  ok   empty -> testing copy, no TestFlight link, no App Store button")
        lines.append("  ok   TestFlight URL -> link under the testing copy")
        lines.append("  ok   App Store URL -> App Store button in place of the testing copy")
    lines.append("")


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
        lowered = actual.lower()
        if FALLBACK not in text:
            problems.append("missing code fallback")
        if "Open PocketRounds" not in text:
            problems.append("missing Open PocketRounds")
        store = APP_STORE_URL.strip()
        flight = TESTFLIGHT_PUBLIC_URL.strip()
        if store:
            if TESTING in text:
                problems.append("testing copy still present")
            if AFTER_INSTALL not in text:
                problems.append("missing after-install copy")
            if f'href="{store}"' not in actual or "App Store" not in text:
                problems.append("missing App Store button")
            if "testflight" in lowered:
                problems.append("TestFlight link rendered beside App Store")
        else:
            if TESTING not in text:
                problems.append("missing testing copy")
            if "app store" in lowered:
                problems.append("App Store button rendered while APP_STORE_URL is empty")
            if flight:
                if f'href="{flight}"' not in actual or "Join on TestFlight" not in text:
                    problems.append("missing TestFlight link")
            elif "testflight" in lowered:
                problems.append("TestFlight link rendered while TESTFLIGHT_PUBLIC_URL is empty")
        open_link = parser.ids.get("open", {})
        if open_link.get("href") != url or not url.endswith("/"):
            problems.append(f"open href {open_link.get('href')!r}")
        if support_url() not in actual:
            problems.append("missing support link")
        if "http-equiv" in lowered or "location.replace" in lowered or "location.href" in lowered:
            problems.append("page redirects")
        hash_at = actual.find("function accepted")
        query_at = actual.find("URLSearchParams")
        if hash_at < 0 or query_at < 0 or hash_at > query_at:
            problems.append("code reader must accept #c= and fall back to ?code=")
        if page["path"] == "/i/member/" and page["image"] != "og-member-v2.png":
            problems.append("member page is not the Team invite mirror")
        for snippet in ("fetch(", "xmlhttprequest", "mailto:", "http://", "<img", "script src"):
            if snippet in lowered:
                problems.append(f"unexpected {snippet}")
        for phrase in BANNED:
            if phrase.lower() in lowered:
                problems.append(f"banned phrase in page: {phrase}")

        image_path = ROOT / "og" / page["image"]
        width = height = size = 0
        if not page["image"].endswith("-v2.png"):
            problems.append("image is not a v2 file")
        if not image_path.is_file():
            problems.append(f"missing image og/{page['image']}")
        else:
            try:
                width, height, size = png_info(image_path)
            except (OSError, ValueError) as exc:
                problems.append(f"image {exc}")
            else:
                if (width, height) != (1200, 630):
                    problems.append(f"image {width}x{height}")
                if size >= MAX_IMAGE_BYTES:
                    problems.append(f"image {size} bytes")

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


def check_referenced_images(failures: list[str], lines: list[str]) -> None:
    """Fail when a page points at an og image that is not in the repo."""
    lines.append("")
    lines.append("Referenced images")
    og = ROOT / "og"
    v1 = sorted(og.glob("*-v1.png")) if og.is_dir() else []
    for path in v1:
        rel = path.relative_to(ROOT).as_posix()
        failures.append("unexpected " + rel)
        lines.append("  NEW  " + rel)
    missing = False
    for page in PAGES:
        rel = f"og/{page['image']}"
        path = ROOT / rel
        if path.is_file():
            lines.append(f"  ok   {page['path']}  {rel}")
            continue
        missing = True
        failures.append(f"missing {rel}")
        lines.append(f"  MISSING  {page['path']}  {rel}")
    if missing:
        lines.append("  event art is not committed; this check fails until og/og-event-v2.png lands")


def main() -> int:
    failures: list[str] = []
    lines = [
        "PocketRounds share-page check",
        f"SITE = {SITE}",
        f"TESTFLIGHT_PUBLIC_URL = {TESTFLIGHT_PUBLIC_URL!r}",
        f"APP_STORE_URL = {APP_STORE_URL!r}",
        "",
    ]
    check_install_slots(failures, lines)
    check_pages(failures, lines)
    check_banned(failures, lines)
    check_referenced_images(failures, lines)
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
