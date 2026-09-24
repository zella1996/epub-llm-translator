from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

from tests.helpers import build_epub
from translator.languages import JAPANESE_SOURCE, split_source_sentences
from translator.epub_processor import (
    AID_BULLET,
    ANALYSES_PER_FILE,
    EPUB_NS,
    EpubBook,
    EpubError,
    LearningCard,
    ParagraphNote,
    ReadingAidNote,
    ReadingAssistanceNote,
    ReadingSentenceNote,
    _normalize_epubcheck_findings,
    run_epubcheck,
)


def _japanese_reading_note(source: str, quote: str | None = None) -> ReadingAssistanceNote:
    aids = () if quote is None else (ReadingAidNote("phrase", (1,), quote, "离线说明。"),)
    return ReadingAssistanceNote(
        "离线占位译文。", (ReadingSentenceNote(1, source, "离线占位译文。"),), aids
    )


@pytest.mark.parametrize("version,language_code", [("2.0", "en"), ("3.0", "ja")])
def test_detached_analyses_preserve_notes_and_source_links(tmp_path, version, language_code):
    source = build_epub(
        tmp_path / "source.epub", version=version, language_code=language_code
    )
    output = tmp_path / "output.epub"
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        before = book.chapter_structure_snapshot(1)
        book.apply_note(ref, ParagraphNote("段译。", (LearningCard("原句。", "hard", "句意。"),)))
        original = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        expected = etree.tostring(
            original.xpath("//*[@id='epubllmt-analyses']")[0], method="c14n"
        )
        book.archive(output, detach_analyses=True)
    with EpubBook(output) as generated:
        assert generated.chapter_structure_snapshot(1) == before
        assert generated.link_issues() == set()
        assert len(generated.content_spines()) == 1
        assert len(generated.non_linear_ids) == 1
        chapter = etree.parse(str(generated.root / "OEBPS/chapter.xhtml"))
        assert not chapter.xpath("//*[@id='epubllmt-analyses']")
        link = chapter.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")[0]
        notes_path = generated._resolve(
            generated.root / "OEBPS", link.get("href", "").split("#", 1)[0]
        )
        notes = etree.parse(str(notes_path))
        actual = notes.xpath("//*[@id='epubllmt-analyses']")
        assert len(actual) == 1
        assert etree.tostring(actual[0], method="c14n") == expected
        assert "段译。" in "".join(notes.getroot().itertext())
        assert "句意。" in "".join(notes.getroot().itertext())


def test_detached_analyses_split_large_chapter_and_rewrite_all_generated_targets(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "output.epub"
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        for _ in range(ANALYSES_PER_FILE + 1):
            book.apply_reading_assistance(
                ref, _japanese_reading_note(ref.text, "original"),
                inline_phrases=True,
            )
        book.archive(output, detach_analyses=True)
    with EpubBook(output) as generated:
        assert generated.link_issues() == set()
        assert len(generated.non_linear_ids) == 2
        chapter = etree.parse(str(generated.root / "OEBPS/chapter.xhtml"))
        generated_links = chapter.xpath(
            "//*[contains(@class, 'epubllmt-analysis-ref') or "
            "contains(@class, 'epubllmt-phrase-ref')]"
        )
        target_files = {link.get("href", "").split("#", 1)[0] for link in generated_links}
        assert len(target_files) == 2
        counts = []
        for target_file in target_files:
            notes = etree.parse(str(generated.root / "OEBPS" / target_file))
            counts.append(len(notes.xpath("//*[contains(@class, 'epubllmt-analysis')]")))
        assert sorted(counts) == [1, ANALYSES_PER_FILE]


@pytest.mark.parametrize("language_code", ["en", "ja"])
def test_reading_aids_use_orientation_neutral_separation_and_readable_link(tmp_path, language_code):
    source = build_epub(tmp_path / "source.epub", language_code=language_code)
    note = ReadingAssistanceNote(
        "段译。",
        (
            ReadingSentenceNote(1, "First sentence.", "第一句。"),
            ReadingSentenceNote(2, "Second sentence.", "第二句。"),
        ),
        (
            ReadingAidNote("sentence", (1,), text="第一句提示。"),
            ReadingAidNote("sentence", (2,), text="第二句提示。"),
        ),
    )
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        before = book.chapter_text_snapshot(1)
        book.apply_reading_assistance(ref, note)
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        anchor = tree.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")[0]
        analysis = tree.xpath("//*[@id='epubllmt-analyses']")[0]
        assert anchor.text == "›››"
        assert "font-size" not in anchor.get("style", "")
        assert len(analysis.xpath(".//*[contains(@class, 'epubllmt-reading-sentence-card')]")) == 2
        assert not any(
            "border-top" in node.get("style", "")
            or "border-block-start" in node.get("style", "")
            for node in analysis.iter()
        )
        assert anchor.get("href", "")[1:] in {
            node.get("id") for node in analysis.iter() if node.get("id")
        }
        assert book.chapter_text_snapshot(1) == before
        assert book.link_issues() == set()


def test_analysis_link_text_is_configurable_plain_text(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source, analysis_link_text="<译&注>") as book:
        ref = book.paragraph(1, 1)
        book.apply_note(ref, ParagraphNote("译文。"))
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        anchor = tree.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")[0]
        assert anchor.text == "<译&注>"
        assert not anchor.xpath("./*")
        assert book.link_issues() == set()


@pytest.mark.parametrize("label", ["", " ", " 译注", "译注\n", "a" * 17])
def test_analysis_link_text_rejects_unreadable_labels(tmp_path, label):
    source = build_epub(tmp_path / "source.epub")
    with pytest.raises(EpubError, match="解析入口文字"):
        EpubBook(source, analysis_link_text=label)


@pytest.mark.parametrize("writing_mode", ["vertical-rl", "horizontal-tb"])
def test_japanese_reading_layout_preserves_both_writing_modes(tmp_path, writing_mode):
    fixture = Path("tests/fixtures/japanese-epub3.epub")
    source = tmp_path / "japanese.epub"
    with zipfile.ZipFile(fixture) as original, zipfile.ZipFile(source, "w") as copied:
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename == "OEBPS/style.css":
                content = content.replace(b"vertical-rl", writing_mode.encode("ascii"))
            copied.writestr(info, content)
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        sentences = split_source_sentences(JAPANESE_SOURCE, ref.text)
        assert len(sentences) >= 2
        note = ReadingAssistanceNote(
            "占位译文。",
            tuple(ReadingSentenceNote(item.index, item.text, "占位句译。") for item in sentences),
            tuple(ReadingAidNote("sentence", (item.index,), text="占位提示。") for item in sentences[:2]),
        )
        before = book.chapter_text_snapshot(1)
        book.apply_reading_assistance(ref, note)
        assert book.chapter_text_snapshot(1) == before
        assert book.link_issues() == set()
        css = (book.root / "OEBPS/style.css").read_text(encoding="utf-8")
        assert f"writing-mode: {writing_mode}" in css
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        anchor = tree.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")[0]
        assert anchor.text == "›››"
        assert len(tree.xpath("//*[contains(@class, 'epubllmt-reading-sentence-card')]")) == 2
        generated = tree.xpath("//*[@id='epubllmt-analyses']")[0]
        assert not any("border-" in node.get("style", "") for node in generated.iter())


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_reading_assistance_renders_scoped_aids_without_mutating_source(tmp_path, version):
    source = build_epub(tmp_path / "source.epub", version=version)
    output = tmp_path / "reading-preview.epub"
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        before_text = book.chapter_text_snapshot(1)
        before_structure = book.chapter_structure_snapshot(1)
        baseline = book.link_issues()
        note = ReadingAssistanceNote(
            "这是原段的译文。尽管它很长，意思仍可验证。",
            (
                ReadingSentenceNote(1, "This is the original paragraph.", "这是原段的译文。"),
                ReadingSentenceNote(2, "Although it is long, its meaning remains testable.", "尽管它很长，意思仍可验证。"),
            ),
            (
                ReadingAidNote("phrase", (1,), "original", "原始的 <不作为标签>。"),
                ReadingAidNote("sentence", (2,), text="让步关系。", show_translation=True),
                ReadingAidNote("paragraph", (1, 2), text="两句构成让步关系。"),
            ),
        )
        book.apply_reading_assistance(ref, note, paragraph_aids=True)
        assert book.chapter_text_snapshot(1) == before_text
        assert book.chapter_structure_snapshot(1) == before_structure
        assert book.link_issues() == baseline
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        analysis = tree.xpath("//*[starts-with(@id, 'epubllmt-analysis-')]")[0]
        heading = "".join(analysis.xpath(
            "./h:h3//text()", namespaces={"h": "http://www.w3.org/1999/xhtml"}
        ))
        assert heading == "第 1 段"
        assert "阅读辅助" not in "".join(analysis.itertext())
        assert analysis.xpath(".//*[contains(@class, 'epubllmt-reading-phrase')]")
        assert analysis.xpath(".//*[contains(@class, 'epubllmt-reading-sentence')]")
        assert analysis.xpath(".//*[contains(@class, 'epubllmt-reading-paragraph')]")
        cards = analysis.xpath("./*[contains(@class, 'epubllmt-reading-sentence-card')]")
        assert len(cards) == 2
        assert "border-top" not in cards[0].get("style", "")
        assert "border-top" not in cards[1].get("style", "")
        assert "原句：This is the original paragraph." in "".join(cards[0].itertext())
        assert "句译：这是原段的译文。" in "".join(cards[0].itertext())
        assert cards[0].xpath(".//*[contains(@class, 'epubllmt-reading-phrase')]")
        phrase = cards[0].xpath(".//*[contains(@class, 'epubllmt-reading-phrase')]")[0]
        term = phrase.xpath("./*[local-name()='strong']")[0]
        assert term.text == f"{AID_BULLET}original"
        assert term.get("style") == "white-space:nowrap"
        assert "• original" not in "".join(cards[0].itertext())
        assert f"{AID_BULLET}original：原始的 <不作为标签>。" in "".join(cards[0].itertext())
        assert not cards[0].xpath(".//*[contains(@class, 'epubllmt-reading-paragraph')]")
        assert "原句：Although it is long" in "".join(cards[1].itertext())
        assert "句译：尽管它很长，意思仍可验证。" in "".join(cards[1].itertext())
        assert cards[1].xpath(".//*[contains(@class, 'epubllmt-reading-sentence')]")
        assert f"{AID_BULLET}让步关系。" in "".join(cards[1].itertext())
        paragraph_aids = analysis.xpath("./*[contains(@class, 'epubllmt-reading-paragraph')]")
        assert len(paragraph_aids) == 1
        text = "".join(analysis.itertext())
        assert "短语：original" not in text
        assert "句译：尽管它很长，意思仍可验证。" in text
        assert "原句：This is the original paragraph." in "".join(paragraph_aids[0].itertext())
        assert "原句：Although it is long, its meaning remains testable." in "".join(paragraph_aids[0].itertext())
        assert f"{AID_BULLET}两句构成让步关系。" in text
        assert "英语阅读提示：" not in text
        assert "第 1 句" not in text and "第 2 句" not in text
        assert "<不作为标签>" in text
        assert not analysis.xpath(".//*[local-name()='不作为标签']")
        assert not analysis.xpath(".//a")
        link = tree.xpath("//*[starts-with(@id, 'epubllmt-src-')]")[0]
        assert link.get("href") == f"#{analysis.get('id')}"
        assert (link.get("aria-label") == "查看本段学习解析") == version.startswith("3")
        assert tree.xpath("//*[local-name()='em']")
        book.archive(output)
    with EpubBook(output) as generated:
        assert generated.link_issues() == set()


def test_reading_assistance_omits_paragraph_aids_by_default(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    note = ReadingAssistanceNote(
        "这是原段的译文。尽管它很长，意思仍可验证。",
        (
            ReadingSentenceNote(1, "This is the original paragraph.", "这是原段的译文。"),
            ReadingSentenceNote(2, "Although it is long, its meaning remains testable.", "尽管它很长，意思仍可验证。"),
        ),
        (
            ReadingAidNote("sentence", (2,), text="让步关系。"),
            ReadingAidNote("paragraph", (1, 2), text="两句构成让步关系。"),
        ),
    )

    with EpubBook(source) as book:
        book.apply_reading_assistance(book.paragraph(1, 1), note)
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        assert tree.xpath("//*[contains(@class, 'epubllmt-reading-sentence')]")
        assert not tree.xpath("//*[contains(@class, 'epubllmt-reading-paragraph')]")
        assert "两句构成让步关系" not in "".join(tree.getroot().itertext())


def test_reading_assistance_rejects_unknown_sentence_before_writing(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        with pytest.raises(EpubError, match="不存在的句子"):
            book.apply_reading_assistance(
                ref,
                ReadingAssistanceNote(
                    "译文。", (ReadingSentenceNote(1, "This is the original paragraph.", "译文。"),),
                    (ReadingAidNote("sentence", (2,), text="无效"),),
                ),
            )
        assert not (book.root / "OEBPS/chapter.xhtml").read_text(
            encoding="utf-8"
        ).count("epubllmt-analysis")


def test_reading_assistance_skips_cards_for_sentences_at_or_below_word_limit(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    short = "Is she still in town?"
    long = (
        "Although the unexpected message arrived before breakfast, everyone in "
        "the crowded room understood why she remained unusually silent."
    )
    note = ReadingAssistanceNote(
        "她还在城里吗？那封意外的消息早餐前便到了，屋里所有人都明白她为何异常沉默。",
        (
            ReadingSentenceNote(1, short, "她还在城里吗？"),
            ReadingSentenceNote(2, long, "那封意外的消息早餐前便到了，屋里所有人都明白她为何异常沉默。"),
        ),
        (
            ReadingAidNote("phrase", (1,), "in town", "在本地。"),
            ReadingAidNote("sentence", (2,), text="先让步，再给主句。"),
        ),
    )

    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        book.apply_reading_assistance(ref, note, short_sentence_words=15)
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        cards = tree.xpath("//*[contains(@class, 'epubllmt-reading-sentence-card')]")
        assert len(cards) == 1
        rendered = "".join(cards[0].itertext())
        assert short not in rendered
        assert long in rendered
        assert "阅读辅助" not in "".join(tree.getroot().itertext())


def test_reading_inline_phrase_link_is_exact_and_preserves_source_text(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        before = book.chapter_text_snapshot(1)
        note = ReadingAssistanceNote(
            "译文。",
            (ReadingSentenceNote(1, ref.text, "译文。"),),
            (ReadingAidNote("phrase", (1,), "original", "原来的。"),),
        )
        book.apply_reading_assistance(ref, note, inline_phrases=True)
        assert book.chapter_text_snapshot(1) == before
        assert book.link_issues() == set()
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        links = tree.xpath("//*[contains(@class, 'epubllmt-phrase-ref')]")
        assert len(links) == 1
        assert links[0].text == "original"
        assert links[0].get("href").startswith("#epubllmt-analysis-")


def test_japanese_ruby_phrase_links_only_base_text_and_falls_back_for_reading(tmp_path):
    source = Path("tests/fixtures/japanese-epub3.epub")
    with EpubBook(source) as book:
        ref = book.paragraph(1, 3)
        before = book.chapter_text_snapshot(1)
        book.apply_reading_assistance(ref, _japanese_reading_note(ref.text, "東京"), inline_phrases=True)
        assert book.chapter_text_snapshot(1) == before
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        phrase = tree.xpath("//*[contains(@class, 'epubllmt-phrase-ref')]")
        assert len(phrase) == 1 and phrase[0].text == "東京"
        assert not phrase[0].xpath(".//*[local-name()='rt' or local-name()='rp']")
        assert "とうきょう" in "".join(tree.xpath("//*[local-name()='rt']/text()"))

    # Pronunciation is deliberately invisible model text: leave the source
    # untouched and keep the generated sentence-card aid as the safe fallback.
    with EpubBook(source) as book:
        ref = book.paragraph(1, 3)
        book.apply_reading_assistance(ref, _japanese_reading_note(ref.text, "とうきょう"), inline_phrases=True)
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        assert not tree.xpath("//*[contains(@class, 'epubllmt-phrase-ref')]")
        assert "とうきょう：离线说明。" in "".join(tree.getroot().itertext())


def test_virtual_br_segment_writeback_anchors_exact_range_without_phrase_wrapping(tmp_path):
    original = tmp_path / "original.epub"
    source = tmp_path / "source.epub"
    build_epub(original, language_code="ja")
    with EpubBook(original) as prepared:
        chapter = prepared.root / "OEBPS/chapter.xhtml"
        chapter.write_text("""<?xml version='1.0' encoding='utf-8'?>
<html xmlns='http://www.w3.org/1999/xhtml'><body><div>
<ruby>東京<rt>とうきょう</rt></ruby>へ行く。<br/>同じ<ruby>東京<rt>とうきょう</rt></ruby>ではない。</div></body></html>""", encoding="utf-8")
        prepared.archive(source)
    with EpubBook(source) as book:
        refs = book.paragraphs()
        assert [ref.text for ref in refs] == ["東京へ行く。", "同じ東京ではない。"]
        before = book.chapter_text_snapshot(1)
        book.apply_reading_assistance(refs[0], _japanese_reading_note(refs[0].text, "東京"), inline_phrases=True)
        assert book.chapter_text_snapshot(1) == before
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        anchors = tree.xpath("//*[contains(@class, 'epubllmt-analysis-ref')]")
        assert len(anchors) == 1
        assert anchors[0].getnext() is not None and etree.QName(anchors[0].getnext()).localname == "br"
        assert not tree.xpath("//*[contains(@class, 'epubllmt-phrase-ref')]")
        assert "東京：离线说明。" in "".join(tree.getroot().itertext())
        assert "writing-mode:inherit" in tree.xpath("string(//*[@id='epubllmt-analyses']/@style)")


def test_reading_assistance_uses_unique_ids_and_preserves_author_footnotes(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source) as book:
        first, second = book.paragraphs()
        first_note = ReadingAssistanceNote(
            "第一段译文。",
            (ReadingSentenceNote(1, "This is the original paragraph. Although it is long, its meaning remains testable.", "第一段译文。"),),
            (ReadingAidNote("sentence", (1,), text="第一段提示。"),),
        )
        second_note = ReadingAssistanceNote(
            "第二段译文。",
            (ReadingSentenceNote(1, second.text, "第二段译文。"),),
            (ReadingAidNote("sentence", (1,), text="第二段提示。"),),
        )
        book.apply_reading_assistance(first, first_note)
        book.apply_reading_assistance(second, second_note)
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        source_ids = tree.xpath("//*[starts-with(@id, 'epubllmt-src-')]/@id")
        analysis_ids = tree.xpath("//*[starts-with(@id, 'epubllmt-analysis-')]/@id")
        assert len(set(source_ids)) == len(source_ids) == 2
        assert len(set(analysis_ids)) == len(analysis_ids) == 2
        assert tree.xpath("//*[@id='author-note']")
        assert tree.xpath("//*[@href='#author-note']")
        assert book.link_issues() == set()


def test_epub3_analysis_uses_plain_links_and_preserves_existing_footnote(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "preview.epub"
    with EpubBook(source) as book:
        refs = book.paragraphs()
        assert book.metadata_values("title") == ("Test Book",)
        assert book.metadata_values("creator") == ("Test Author",)
        assert book.metadata_values("date") == ("2026-08-21",)
        assert [(ref.chapter, ref.paragraph) for ref in refs] == [(1, 1), (1, 2)]
        before = book.chapter_text_snapshot(1)
        baseline = book.link_issues()
        book.apply_note(
            refs[0],
            ParagraphNote(
                "这是原段落的直译。",
                (
                    LearningCard(
                        sentence="Although it is long, its meaning remains testable.",
                        difficulty="effortful",
                        meaning="尽管它很长，但意思仍可验证。",
                    ),
                ),
            ),
        )
        assert book.chapter_text_snapshot(1) == before
        assert book.link_issues() == baseline
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        generated = tree.xpath("//*[@id='epubllmt-analyses']")[0]
        author_note = tree.xpath("//*[@id='author-note']")[0]
        assert list(generated.getparent()).index(generated) > list(
            author_note.getparent()
        ).index(author_note)
        assert generated.get(f"{{{EPUB_NS}}}type") is None
        assert "display:block" in generated.get("style")
        analysis = tree.xpath("//*[starts-with(@id, 'epubllmt-analysis-')]")[0]
        assert etree.QName(analysis).localname == "div"
        assert analysis.get(f"{{{EPUB_NS}}}type") is None
        assert analysis.xpath(".//*[contains(@class, 'epubllmt-card')]")
        generated_text = "".join(generated.itertext())
        assert "段译：这是原段落的直译。" in generated_text
        assert "原句：Although it is long, its meaning remains testable." in generated_text
        assert "句意：尽管它很长，但意思仍可验证。" in generated_text
        for retired_label in ("线索：", "结构提示：", "主干：", "语感：", "提示："):
            assert retired_label not in generated_text
        translation = analysis.xpath("./*[contains(@class, 'epubllmt-translation')]")[0]
        assert etree.QName(translation[0]).localname == "strong"
        assert "text-indent:0" in translation.get("style")
        assert "margin:0.8em 0 1.2em" in translation.get("style")
        card = analysis.xpath(".//*[contains(@class, 'epubllmt-card')]")[0]
        assert "border-top" not in card.get("style")
        assert "margin:1em" in card.get("style")
        field = card.xpath("./*[local-name()='p']")[0]
        assert "text-indent:0" in field.get("style")
        assert "margin:0.55em 0" in field.get("style")
        assert tree.xpath("//*[@id='author-note']")
        assert tree.xpath("//*[@href='#author-note']")
        source_links = tree.xpath("//*[starts-with(@id, 'epubllmt-src-')]")
        analysis_links = tree.xpath("//*[starts-with(@id, 'epubllmt-analysis-')]")
        assert len(source_links) == len(analysis_links) == 1
        assert etree.QName(source_links[0]).localname == "a"
        assert etree.QName(source_links[0].getparent()).localname == "p"
        assert source_links[0].text == "›››"
        assert source_links[0].get("aria-label") == "查看本段学习解析"
        assert source_links[0].get(f"{{{EPUB_NS}}}type") is None
        assert source_links[0].get("href") == f"#{analysis_links[0].get('id')}"
        assert not analysis_links[0].xpath(".//a")
        assert not tree.xpath(
            f"//a[@href='#{source_links[0].get('id')}']"
        )
        book.archive(output)

    with zipfile.ZipFile(output) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
    with EpubBook(output) as generated:
        assert generated.link_issues() == set()


def test_epub2_uses_one_way_analysis_links(tmp_path):
    source = build_epub(tmp_path / "source.epub", version="2.0")
    with EpubBook(source) as book:
        ref = book.paragraph(1, 1)
        book.apply_note(ref, ParagraphNote("直译。"))
        tree = etree.parse(str(book.root / "OEBPS/chapter.xhtml"))
        generated = tree.xpath("//*[@id='epubllmt-analyses']")[0]
        assert etree.QName(generated).localname == "div"
        assert generated.get(f"{{{EPUB_NS}}}type") is None
        source_link = tree.xpath("//*[starts-with(@id, 'epubllmt-src-')]")[0]
        assert source_link.get(f"{{{EPUB_NS}}}type") is None
        assert source_link.get("aria-label") is None
        assert not tree.xpath(f"//a[@href='#{source_link.get('id')}']")
        assert book.link_issues() == set()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("location", ["same-file", "cross-file"])
def test_plain_author_note_targets_are_not_selected_as_body_text(
    tmp_path, version, location
):
    source = build_epub(
        tmp_path / "source.epub",
        version=version,
        plain_note_target=location,
    )
    output = tmp_path / "output.epub"
    with EpubBook(source) as book:
        refs = book.paragraphs()
        assert len(refs) == 2
        assert all("Original author note." not in ref.text for ref in refs)
        assert book.link_issues() == set()
        book.apply_note(refs[0], ParagraphNote("直译。"))
        assert book.link_issues() == set()
        book.archive(output)

    with EpubBook(output) as generated:
        assert len(generated.paragraphs()) == 2
        assert generated.link_issues() == set()


def test_rejects_zip_path_traversal(tmp_path):
    source = tmp_path / "unsafe.epub"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("../escape", "bad")
    with pytest.raises(EpubError, match="不安全"):
        EpubBook(source)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"encrypted": True}, "加密"),
        ({"fixed_layout": True}, "固定版式"),
        ({"scripted": True}, "脚本"),
    ],
)
def test_rejects_unsupported_publication_features(tmp_path, options, message):
    source = build_epub(tmp_path / "unsupported.epub", **options)
    with pytest.raises(EpubError, match=message):
        EpubBook(source)


@pytest.mark.parametrize(
    "options",
    [
        {"non_linear": True},
        {"body_type": "frontmatter"},
        {"body_type": "backmatter"},
    ],
)
def test_non_body_content_is_not_selected(tmp_path, options):
    source = build_epub(tmp_path / "source.epub", **options)
    with EpubBook(source) as book:
        assert book.paragraphs() == []


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_header_boilerplate_is_not_selected_as_body_text(tmp_path, version):
    source = build_epub(
        tmp_path / "source.epub", version=version, boilerplate_header=True
    )
    with EpubBook(source) as book:
        texts = [ref.text for ref in book.paragraphs()]
    assert len(texts) == 2
    assert all("Release date" not in text for text in texts)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_poetry_tables_captions_code_and_declared_foreign_text_are_not_selected(
    tmp_path, version
):
    source = build_epub(
        tmp_path / "source.epub", version=version, extra_non_prose=True
    )
    with EpubBook(source) as book:
        texts = [ref.text for ref in book.paragraphs()]
    assert len(texts) == 2
    assert all(
        excluded not in " ".join(texts)
        for excluded in ("verse", "Table", "caption", "Bonjour", "code")
    )


def test_accepts_book_declared_as_registered_japanese(tmp_path):
    source = build_epub(tmp_path / "japanese.epub", language_code="ja")
    with EpubBook(source) as book:
        assert book.source_language == "ja"


def test_br_delimited_japanese_prose_keeps_base_ruby_and_skips_foreign_parts():
    tree = etree.ElementTree(etree.fromstring("""<html xmlns='http://www.w3.org/1999/xhtml'>
      <body xml:lang='ja'><div>見出し<div>入れ子の見出し</div><br/>
      <ruby>小<rt>こ</rt></ruby>柳<span lang='en'> foreign <em xml:lang='ja'>再び</em></span>です。<br/>
      <!-- a non-element node must not crash traversal -->次です。<br/></div></body></html>"""))
    book = object.__new__(EpubBook)
    book.source_language = "ja"
    book.note_targets = {}
    candidates = book._candidate_elements(tree)
    assert [book._text_without_generated(element).strip() for _, element in candidates] == [
        "小柳再びです。",
        "次です。",
    ]


def test_rejects_private_class_namespace_collision(tmp_path):
    source = build_epub(tmp_path / "source.epub", generated_class_collision=True)
    with pytest.raises(EpubError, match="已生成的学习注释"):
        EpubBook(source, reject_generated=True)


def test_refuses_to_overwrite_input_or_existing_output(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source) as book:
        with pytest.raises(EpubError, match="覆盖输入"):
            book.archive(source)
        existing = tmp_path / "exists.epub"
        existing.write_bytes(b"keep")
        with pytest.raises(EpubError, match="已存在"):
            book.archive(existing)
        assert existing.read_bytes() == b"keep"


def test_internal_paths_may_use_parent_segments_without_escaping_container(tmp_path):
    source = build_epub(tmp_path / "source.epub")
    with EpubBook(source) as book:
        assert book._resolve(book.root / "OEBPS", "../OEBPS/chapter.xhtml") == (
            book.root / "OEBPS" / "chapter.xhtml"
        )
        with pytest.raises(EpubError, match="内部路径逃逸"):
            book._resolve(book.root / "OEBPS", "../../outside.xhtml")


def test_epubcheck_findings_ignore_archive_name_and_line_numbers(tmp_path):
    source = tmp_path / "source.epub"
    output = tmp_path / "output.epub"
    left = _normalize_epubcheck_findings(
        f'ERROR(RSC-005) at "{source}/OEBPS/chapter.xhtml"(12,8): broken',
        source,
    )
    right = _normalize_epubcheck_findings(
        f'ERROR(RSC-005) at "{output}/OEBPS/chapter.xhtml"(99,1): broken',
        output,
    )
    assert left == right


def test_epubcheck_warnings_are_returned_for_cli_reporting(tmp_path, monkeypatch):
    monkeypatch.setattr("translator.epub_processor.shutil.which", lambda _: "epubcheck")
    monkeypatch.setattr(
        "translator.epub_processor.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="WARNING(OPF-001): harmless source warning\n",
            stderr="",
        ),
    )
    result = run_epubcheck(
        tmp_path / "source.epub", required=True, allow_invalid=False
    )
    assert result.warnings == ("WARNING(OPF-001): harmless source warning",)


def test_epubcheck_is_really_skipped_when_not_required(tmp_path, monkeypatch):
    monkeypatch.setattr("translator.epub_processor.shutil.which", lambda _: "epubcheck")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("epubcheck should not run")

    monkeypatch.setattr("translator.epub_processor.subprocess.run", unexpected_run)
    result = run_epubcheck(
        tmp_path / "source.epub", required=False, allow_invalid=False
    )
    assert not result.ran
