"""Build one ignored, deterministic EPUB for manual Kindle navigation checks."""

from __future__ import annotations

import argparse
from pathlib import Path

from translator.epub_processor import (
    EpubBook,
    LearningCard,
    ParagraphNote,
    run_epubcheck,
)
from translator.languages import ENGLISH_TO_CHINESE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    run_epubcheck(args.source, required=True, allow_invalid=False)
    with EpubBook(args.source, reject_generated=True) as book:
        refs = book.paragraphs()
        if not refs:
            raise SystemExit("source contains no selectable body paragraph")
        if len(refs) < 2:
            raise SystemExit("source needs at least two body paragraphs")
        short_ref, long_ref = refs[:2]
        book.apply_note(
            short_ref,
            ParagraphNote(
                "【单向链接测试】确认点击后直接进入解析页，且不出现弹窗。"
            ),
        )
        sentences = ENGLISH_TO_CHINESE.split(long_ref.text)
        first_sentence = sentences[0].text if sentences else long_ref.text
        book.apply_note(
            long_ref,
            ParagraphNote(
                "【完整解析测试】点击段尾的〔析〕后应直接看到本段直译和以下分段解析。",
                (
                    LearningCard(
                        sentence=first_sentence,
                        difficulty="effortful",
                        meaning="【直接跳转测试】这里显示困难句的中文含义，并确认没有出现脚注弹窗。",
                        cues="【页面详细解析测试】这里显示理解线索。请确认换行、分段、分页和返回位置没有失效。",
                        feel="【页面详细解析测试】这里显示语气或表达特点，用于检查理解卡的第三层内容。",
                        note="【页面详细解析测试】这里显示简短提示，用于确认最后一段也没有被截断。",
                    ),
                ),
            ),
        )
        book.archive(args.output)
    run_epubcheck(args.output, required=True, allow_invalid=False)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
