from translator.translate import translate_epub_main
from pathlib import Path


def main():
    source_path = Path("C:/Users/zerof/Downloads/snippet.epub")
    output_path = Path("C:/Users/zerof/Downloads/output.epub")
    translate_epub_main(source_path)


if __name__ == "__main__":
    main()
