from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import sys
from collections import deque
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
import urllib3


URL_ATTRS = {"src", "href", "poster", "data-src", "data-href"}
SKIP_SCHEMES = {"data", "blob", "javascript", "mailto", "tel", "about"}
TEXT_TYPES = {
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/wasm",
    "image/svg+xml",
}
PAGE_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx"}
ASSET_EXTENSIONS = {
    ".css", ".js", ".mjs", ".cjs", ".json", ".webmanifest", ".xml", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".wav", ".ogg",
    ".mp4", ".webm", ".wasm", ".bin", ".map",
}
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)", re.I)
QUOTED_PATH_RE = re.compile(
    r"['\"]((?:https?:)?//[^'\"\s]+|/[A-Za-z0-9_./%+@~-]+(?:\?[A-Za-z0-9_=&.%+@~-]*)?)['\"]"
)
SOURCE_MAP_RE = re.compile(r"[#@]\s*sourceMappingURL=([^\s*]+)")
DYNAMIC_TRACK_RE = re.compile(r'\{f:"([A-Za-z0-9_-]+)",n:"[^"]+",m:"(?:prep|sell)"\}')


def clean_url(raw: str, base: str) -> str | None:
    raw = raw.strip().replace("&amp;", "&")
    if not raw or raw.startswith(("#", "{")):
        return None
    parsed_raw = urlparse(raw)
    if parsed_raw.scheme.lower() in SKIP_SCHEMES:
        return None
    absolute = urljoin(base, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


class LinkCollector(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if not value:
                continue
            name = name.lower()
            if name == "srcset":
                for item in value.split(","):
                    self._add(item.strip().split()[0])
            elif name in URL_ATTRS:
                self._add(value)
            elif name == "style":
                for match in CSS_URL_RE.finditer(value):
                    self._add(match.group(2))

    def _add(self, raw: str) -> None:
        url = clean_url(raw, self.base_url)
        if url:
            self.links.add(url)


class SiteMirror:
    def __init__(self, root_url: str, output: Path, max_files: int = 5000) -> None:
        self.root_url = root_url.rstrip("/") + "/"
        self.root = urlparse(self.root_url)
        self.output = output.resolve()
        self.max_files = max_files
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
            "Accept": "*/*",
        })
        self.queue: deque[tuple[str, str]] = deque([(self.root_url, "page")])
        self.seen: set[str] = set()
        self.saved: dict[str, str] = {}
        self.failures: list[tuple[str, str]] = []

    def normalized_key(self, url: str) -> str:
        parsed = urlparse(url)
        # Cache-busting query strings normally identify the same file. Preserve queries
        # only in the request; de-duplicate by host/path for a deterministic mirror.
        return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", "", ""))

    def local_path(self, url: str, content_type: str) -> Path:
        parsed = urlparse(url)
        path = unquote(parsed.path or "/")
        posix = PurePosixPath(path.lstrip("/"))
        parts = [p for p in posix.parts if p not in {"", ".", ".."}]
        same_origin = parsed.netloc.lower() == self.root.netloc.lower()
        base = self.output if same_origin else self.output / "_external" / self.safe_name(parsed.netloc)
        target = base.joinpath(*parts) if parts else base

        media_type = content_type.split(";", 1)[0].strip().lower()
        suffix = target.suffix.lower()
        is_html = media_type == "text/html"
        if path.endswith("/") or (is_html and suffix in PAGE_EXTENSIONS):
            target = target / "index.html"
        elif not suffix:
            extension = mimetypes.guess_extension(media_type) or ""
            if extension == ".jpe":
                extension = ".jpg"
            target = target.with_name(target.name + extension)

        if parsed.query and not same_origin:
            digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
            target = target.with_name(f"{target.stem}-{digest}{target.suffix}")
        return target

    @staticmethod
    def safe_name(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value)

    def should_fetch(self, url: str, context: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        same_origin = parsed.netloc.lower() == self.root.netloc.lower()
        if same_origin:
            if context == "page":
                return PurePosixPath(parsed.path).suffix.lower() in PAGE_EXTENSIONS
            return True
        # Only fetch external files explicitly embedded by a mirrored document.
        # Google Fonts' stylesheet endpoint intentionally has no file extension.
        suffix = PurePosixPath(parsed.path).suffix.lower()
        google_font_css = parsed.netloc.lower() == "fonts.googleapis.com" and parsed.path == "/css2"
        return context == "asset" and (suffix in ASSET_EXTENSIONS or google_font_css)

    def enqueue(self, url: str, context: str) -> None:
        if self.should_fetch(url, context) and self.normalized_key(url) not in self.seen:
            self.queue.append((url, context))

    def discover(self, text: str, base_url: str, content_type: str) -> None:
        media_type = content_type.split(";", 1)[0].strip().lower()
        candidates: set[str] = set()

        if media_type == "text/html" or "<html" in text[:1000].lower():
            parser = LinkCollector(base_url)
            try:
                parser.feed(text)
            except Exception:
                pass
            candidates.update(parser.links)

        if media_type in {"text/css", "text/html", "image/svg+xml"}:
            for match in CSS_URL_RE.finditer(text):
                url = clean_url(match.group(2), base_url)
                if url:
                    candidates.add(url)
            for match in CSS_IMPORT_RE.finditer(text):
                url = clean_url(match.group(1), base_url)
                if url:
                    candidates.add(url)

        same_origin_document = urlparse(base_url).netloc.lower() == self.root.netloc.lower()
        if same_origin_document and (media_type in TEXT_TYPES or media_type.startswith("text/")):
            for match in QUOTED_PATH_RE.finditer(text):
                url = clean_url(match.group(1), base_url)
                if url:
                    candidates.add(url)
            for match in SOURCE_MAP_RE.finditer(text):
                url = clean_url(match.group(1), base_url)
                if url:
                    candidates.add(url)
            # The game constructs music URLs at runtime as `/music/${track}.mp3`,
            # so those files do not appear as literal URLs in the bundle.
            for match in DYNAMIC_TRACK_RE.finditer(text):
                url = clean_url(f"/music/{match.group(1)}.mp3", self.root_url)
                if url:
                    candidates.add(url)

        for url in sorted(candidates):
            parsed = urlparse(url)
            suffix = PurePosixPath(parsed.path).suffix.lower()
            same_origin = parsed.netloc.lower() == self.root.netloc.lower()
            if media_type == "text/html" and same_origin and suffix in PAGE_EXTENSIONS:
                self.enqueue(url, "page")
            elif suffix in ASSET_EXTENSIONS or (
                not same_origin
                and parsed.netloc.lower() == "fonts.googleapis.com"
                and parsed.path == "/css2"
            ):
                self.enqueue(url, "asset")

    def run(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        while self.queue and len(self.seen) < self.max_files:
            url, context = self.queue.popleft()
            key = self.normalized_key(url)
            if key in self.seen:
                continue
            self.seen.add(key)
            try:
                response = self.session.get(url, timeout=40, verify=False, allow_redirects=True)
                response.raise_for_status()
            except Exception as exc:
                self.failures.append((url, str(exc)))
                print(f"FAIL {url}: {exc}")
                continue

            content_type = response.headers.get("Content-Type", "application/octet-stream")
            target = self.local_path(response.url, content_type)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
            relative = target.relative_to(self.output).as_posix()
            self.saved[url] = relative
            print(f"SAVE {response.status_code:3} {len(response.content):9}  {relative}")

            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type in TEXT_TYPES or media_type.startswith("text/"):
                response.encoding = response.encoding or "utf-8"
                self.discover(response.text, response.url, content_type)

        self.localize_external_assets()
        self.write_report()
        if self.queue:
            print(f"Stopped at max-files={self.max_files}; {len(self.queue)} queued URLs remain.")

    def localize_external_assets(self) -> None:
        replacements: dict[str, str] = {}
        for source_url, relative in self.saved.items():
            if urlparse(source_url).netloc.lower() != self.root.netloc.lower():
                replacements[source_url] = "/" + relative

        text_suffixes = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".webmanifest", ".svg"}
        for path in self.output.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            try:
                original = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            localized = original
            for source_url, local_url in replacements.items():
                localized = localized.replace(source_url, local_url)
            if localized != original:
                path.write_text(localized, encoding="utf-8", newline="\n")

    def write_report(self) -> None:
        lines = [
            "# Website mirror report",
            "",
            f"- Source: {self.root_url}",
            f"- Downloaded files: {len(self.saved)}",
            f"- Failed requests: {len(self.failures)}",
            "- Embedded external font assets localized: yes",
            "",
            "## Failed requests",
            "",
        ]
        if self.failures:
            lines.extend(f"- `{url}` — {error}" for url, error in self.failures)
        else:
            lines.append("None.")
        lines.extend([
            "",
            "## Run locally",
            "",
            "From this directory, run:",
            "",
            "```powershell",
            "python -m http.server 8080",
            "```",
            "",
            "Then open `http://localhost:8080/`.",
            "",
            "This mirror contains only resources publicly delivered to a browser. Server-side source, databases, secrets, and private APIs are not obtainable from a public URL.",
        ])
        (self.output / "MIRROR_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror the public frontend of a website.")
    parser.add_argument("url", nargs="?", default="https://aenhatrang.com/")
    parser.add_argument("--output", default="aenhatrang-source")
    parser.add_argument("--max-files", type=int, default=5000)
    args = parser.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    mirror = SiteMirror(args.url, Path(args.output), max_files=args.max_files)
    mirror.run()
    print(f"Downloaded {len(mirror.saved)} files; {len(mirror.failures)} failed.")
    return 1 if mirror.failures else 0


if __name__ == "__main__":
    sys.exit(main())
