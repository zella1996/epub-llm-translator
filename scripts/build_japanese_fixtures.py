"""Build deterministic, synthetic Japanese EPUB 2/3 fixtures for tests."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="{document_language}" lang="{document_language}">
  <head>
    <title>第一章</title>
    <link rel="stylesheet" type="text/css" href="style.css"/>
  </head>
  <body>
    <h1 id="start">第一章</h1>
    <p xml:lang="ja">「本当！？……」彼は言った。次は何？終わり！</p>
    <p><ruby>日<rt>に</rt></ruby><ruby>本<rt>ほん</rt></ruby>と<ruby>昨日<rt>きのう</rt></ruby>。</p>
    <p><ruby>東京<rp>（</rp><rt>とうきょう</rt><rp>）</rp></ruby>から<ruby><rb>明日</rb><rt>あした</rt></ruby>へ。</p>
    <p>前半<span lang="en">AI<span xml:lang="ja-JP">再び<em>日本語</em></span></span>後半。</p>
    <p xml:lang="JA">大小文字の言語タグ。</p>
    <p><a href="#start">章頭へ戻る</a></p>
  </body>
</html>
"""

STYLE = """html, body { writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; }
ruby { ruby-position: over; }
"""


def _package(version: str, language: str) -> str:
    epub3 = version.startswith("3")
    nav_item = (
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        if epub3
        else '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    )
    modified = (
        '<meta property="dcterms:modified">2026-09-15T00:00:00Z</meta>'
        if epub3
        else ""
    )
    toc = "" if epub3 else ' toc="ncx"'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">synthetic-japanese-{version}</dc:identifier>
    <dc:title>合成日本語フィクスチャ</dc:title>
    <dc:language>{language}</dc:language>
    {modified}
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="style.css" media-type="text/css"/>
    {nav_item}
  </manifest>
  <spine{toc} page-progression-direction="rtl"><itemref idref="chapter"/></spine>
</package>
"""


NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja">
<head><title>目次</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml#start">第一章</a></li></ol></nav></body>
</html>
"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="synthetic-japanese-2.0"/></head>
<docTitle><text>合成日本語フィクスチャ</text></docTitle>
<navMap><navPoint id="c1" playOrder="1"><navLabel><text>第一章</text></navLabel><content src="chapter.xhtml#start"/></navPoint></navMap>
</ncx>
"""


def build_fixture(path: Path, *, version: str, language: str, document_language: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/package.opf": _package(version, language),
        "OEBPS/chapter.xhtml": CHAPTER.format(document_language=document_language),
        "OEBPS/style.css": STYLE,
        "OEBPS/nav.xhtml" if version.startswith("3") else "OEBPS/toc.ncx": (
            NAV if version.startswith("3") else NCX
        ),
    }
    with zipfile.ZipFile(path, "w") as archive:
        mimetype = zipfile.ZipInfo("mimetype", date_time=(2026, 9, 15, 0, 0, 0))
        mimetype.compress_type = zipfile.ZIP_STORED
        archive.writestr(mimetype, b"application/epub+zip")
        for name, content in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 15, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", nargs="?", type=Path, default=Path("tests/fixtures"))
    args = parser.parse_args()
    build_fixture(
        args.output_dir / "japanese-epub3.epub",
        version="3.0",
        language="ja-JP",
        document_language="JA-jp",
    )
    build_fixture(
        args.output_dir / "japanese-epub2.epub",
        version="2.0",
        language="JA",
        document_language="ja",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
