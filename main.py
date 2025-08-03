from pathlib import Path
from translator.translator import translate_epub_main
from translator.llm_api import ModelType


def main():
    # source_path = Path("C:/Users/zerof/Downloads/S&S - snippet.epub")
    # output_path = Path("C:/Users/zerof/Downloads/output.epub")
    source_path = Path(
        "C:/Users/zerof/Calibre 书库/Jane Austen/Sense and Sensibility (83)/Sense and Sensibility - Jane Austen.epub"
    )
    output_path = Path(
        "C:/Users/zerof/Downloads/Sense and Sensibility - Jane Austen (explained).epub"
    )

    # 匹配Sense_and_sensibility_split_009到Sense_and_sensibility_split_060的文件
    translate_epub_main(
        source_path,
        output_path,
        filename_pattern=r"^Sense_and_sensibility_split_(0(0[9-9]|[1-5][0-9]|60))$",
        model=ModelType.GEMMA3_12B,
    )
    # translate_epub_main(
    #     source_path,
    #     output_path,
    #     filename_pattern=r"^vol\d+ch\d+$",
    #     model=ModelType.GEMMA3_12B,
    # )


if __name__ == "__main__":
    main()
