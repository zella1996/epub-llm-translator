"""S0 fixtures and explicit evidence for Japanese behavior not implemented yet."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree

from translator.epub_processor import EpubBook
from translator.sentence_analyzer import split_japanese_sentences


FIXTURES = Path(__file__).parent / "fixtures"
KNOWN_MISSING = "S0 only fixes the baseline for missing Japanese behavior"


@pytest.mark.parametrize(
    ("name", "version", "package_language", "document_language"),
    [
        ("japanese-epub2.epub", "2.0", "JA", "ja"),
        ("japanese-epub3.epub", "3.0", "ja-JP", "JA-jp"),
    ],
)
def test_synthetic_japanese_fixture_contract(
    name, version, package_language, document_language
):
    with zipfile.ZipFile(FIXTURES / name) as archive:
        assert archive.testzip() is None
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        opf = etree.fromstring(archive.read("OEBPS/package.opf"))
        chapter = etree.fromstring(archive.read("OEBPS/chapter.xhtml"))
        assert opf.get("version") == version
        assert opf.xpath("string(//*[local-name()='language'])") == package_language
        assert opf.xpath("string(//*[local-name()='spine']/@page-progression-direction)") == "rtl"
        assert chapter.get("{http://www.w3.org/XML/1998/namespace}lang") == document_language
        assert chapter.xpath("count(//*[local-name()='ruby'])") == 5
        assert chapter.xpath("count(//*[local-name()='rt'])") == 5
        assert chapter.xpath("count(//*[local-name()='rp'])") == 2
        assert chapter.xpath("//*[local-name()='rb']")
        assert chapter.xpath(
            "//*[@lang='en']//*[@xml:lang='ja-JP']",
            namespaces={"xml": "http://www.w3.org/XML/1998/namespace"},
        )
        assert chapter.xpath("//*[@lang='en']")
        assert chapter.xpath(
            "//*[@xml:lang='ja-JP']",
            namespaces={"xml": "http://www.w3.org/XML/1998/namespace"},
        )
        css = archive.read("OEBPS/style.css").decode("utf-8")
        assert "writing-mode: vertical-rl" in css
        text = "".join(chapter.itertext())
        assert "！？……」" in text


def test_japanese_package_entry_is_supported():
    with EpubBook(FIXTURES / "japanese-epub3.epub") as book:
        assert book.source_language == "ja"
        assert book.original_languages == ("ja-JP",)
        assert [ref.text for ref in book.paragraphs()] == [
            "「本当！？……」彼は言った。次は何？終わり！",
            "日本と昨日。",
            "東京から明日へ。",
            "前半再び日本語後半。",
            "大小文字の言語タグ。",
            "章頭へ戻る",
        ]


def test_japanese_sentence_boundaries_are_recognized():
    text = "「本当！？……」彼は言った。次は何？終わり！"
    assert [sentence.text for sentence in split_japanese_sentences(text)] == [
        "「本当！？……」",
        "彼は言った。",
        "次は何？",
        "終わり！",
    ]


def test_ruby_reading_text_is_excluded_from_model_text():
    element = etree.fromstring(
        '<p xmlns="http://www.w3.org/1999/xhtml"><ruby>小<rt>こ</rt></ruby><ruby>柳<rt>やなぎ</rt></ruby>です。</p>'
    )
    book = object.__new__(EpubBook)
    assert book._text_without_generated(element) == "小柳です。"
