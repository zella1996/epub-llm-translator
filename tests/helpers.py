from __future__ import annotations

import zipfile
from pathlib import Path


def build_epub(
    path: Path,
    *,
    version: str = "3.0",
    body_type: str | None = None,
    non_linear: bool = False,
    boilerplate_header: bool = False,
    omit_language: bool = False,
    encrypted: bool = False,
    fixed_layout: bool = False,
    scripted: bool = False,
    media_overlay: bool = False,
    plain_note_target: str | None = None,
    language_code: str = "en",
    extra_non_prose: bool = False,
    generated_class_collision: bool = False,
    title: str = "Test Book",
    creator: str | None = "Test Author",
    publication_date: str | None = "2026-08-21",
    custom_paragraphs: tuple[str, ...] | None = None,
) -> Path:
    epub3 = version.startswith("3")
    package_ns = (
        "http://www.idpf.org/2007/opf"
        if epub3
        else "http://www.idpf.org/2007/opf"
    )
    nav_item = (
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        if epub3
        else '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    )
    modified = (
        '<meta property="dcterms:modified">2026-08-21T00:00:00Z</meta>'
        if epub3
        else ""
    )
    fixed = (
        '<meta property="rendition:layout">pre-paginated</meta>'
        if fixed_layout
        else ""
    )
    chapter_attributes = ""
    if scripted:
        chapter_attributes += ' properties="scripted"'
    if media_overlay:
        chapter_attributes += ' media-overlay="overlay"'
    spine_attrs = "" if epub3 else ' toc="ncx"'
    linear = ' linear="no"' if non_linear else ""
    language = (
        "" if omit_language else f"<dc:language>{language_code}</dc:language>"
    )
    creator_metadata = f"<dc:creator>{creator}</dc:creator>" if creator else ""
    date_metadata = (
        f"<dc:date>{publication_date}</dc:date>" if publication_date else ""
    )
    note_manifest = (
        '<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>'
        if plain_note_target == "cross-file"
        else ""
    )
    note_spine = (
        '<itemref idref="notes"/>' if plain_note_target == "cross-file" else ""
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="{package_ns}" version="{version}" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">test-book</dc:identifier>
    <dc:title>{title}</dc:title>
    {creator_metadata}
    {date_metadata}
    {language}
    {modified}
    {fixed}
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"{chapter_attributes}/>
    {nav_item}
    {note_manifest}
  </manifest>
  <spine{spine_attrs}><itemref idref="chapter"{linear}/>{note_spine}</spine>
</package>"""
    body_attribute = f' epub:type="{body_type}"' if body_type else ""
    epub_namespace = (
        ' xmlns:epub="http://www.idpf.org/2007/ops"' if epub3 else ""
    )
    if plain_note_target == "same-file":
        original_note = '<div><p id="author-note"><a href="#author-src">↩ a</a> Original author note.</p></div>'
    elif plain_note_target == "cross-file":
        original_note = ""
    else:
        original_note = (
            '<aside id="author-note" epub:type="footnote"><p><a href="#author-src" epub:type="backlink">↩ a</a> Original author note.</p></aside>'
            if epub3
            else '<div id="author-note" class="footnote"><p><a href="#author-src">↩ a</a> Original author note.</p></div>'
        )
    source_note_type = ' epub:type="noteref"' if epub3 else ""
    header = (
        (
            "<header><p>Release date: this is metadata, not body text.</p></header>"
            if epub3
            else '<div class="header"><p>Release date: this is metadata, not body text.</p></div>'
        )
        if boilerplate_header
        else ""
    )
    non_prose = (
        """<div class="poem"><p>A verse line.</p></div>
    <table><tr><td><p>Table cell.</p></td></tr></table>
    <figure><figcaption><p>Image caption.</p></figcaption></figure>
    <div xml:lang="fr"><p>Bonjour tout le monde.</p></div>
    <pre><code>print(&quot;code&quot;)</code></pre>"""
        if extra_non_prose
        else ""
    )
    note_href = "notes.xhtml#author-note" if plain_note_target == "cross-file" else "#author-note"
    opening_class = "epubllmt-collision" if generated_class_collision else "opening"
    source_paragraphs = (
        "\n    ".join(f"<p>{paragraph}</p>" for paragraph in custom_paragraphs)
        if custom_paragraphs is not None
        else """<p class="OPENING_CLASS">This is <em>the original</em> paragraph. Although it is long, its meaning remains testable.</p>
    <p id="second">A second paragraph has an author's note.<sup><a id="author-src" href="NOTE_HREF"SOURCE_NOTE_TYPE>a</a></sup></p>"""
    )
    chapter = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"EPUB_NAMESPACE>
  <head><title>Chapter One</title></head>
  <bodyBODY_ATTRIBUTE>
    BOILERPLATE_HEADER
    NON_PROSE
    <h1>Chapter One</h1>
    SOURCE_PARAGRAPHS
    ORIGINAL_NOTE
  </body>
</html>"""
    chapter = (
        chapter.replace("BODY_ATTRIBUTE", body_attribute)
        .replace("EPUB_NAMESPACE", epub_namespace)
        .replace("SOURCE_PARAGRAPHS", source_paragraphs)
        .replace("SOURCE_NOTE_TYPE", source_note_type)
        .replace("NOTE_HREF", note_href)
        .replace("ORIGINAL_NOTE", original_note)
        .replace("BOILERPLATE_HEADER", header)
        .replace("NON_PROSE", non_prose)
        .replace("OPENING_CLASS", opening_class)
    )
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
    nav = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml">Chapter One</a></li></ol></nav></body>
</html>"""
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="test-book"/></head>
<docTitle><text>Test Book</text></docTitle>
<navMap><navPoint id="c1" playOrder="1"><navLabel><text>Chapter One</text></navLabel><content src="chapter.xhtml"/></navPoint></navMap>
</ncx>"""
    notes = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Notes</title></head><body>
<div><p id="author-note"><a href="chapter.xhtml#author-src">↩ a</a> Original author note.</p></div>
</body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, b"application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/chapter.xhtml", chapter)
        if plain_note_target == "cross-file":
            archive.writestr("OEBPS/notes.xhtml", notes)
        if epub3:
            archive.writestr("OEBPS/nav.xhtml", nav)
        else:
            archive.writestr("OEBPS/toc.ncx", ncx)
        if encrypted:
            archive.writestr(
                "META-INF/encryption.xml",
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>',
            )
    return path
