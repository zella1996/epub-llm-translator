from pathlib import Path
from translator.llm_api import ollama_request
from translator.sentence_analyzer import overall_difficulty
from translator.translate import translate_epub_main


def main():
    # source_path = Path("C:/Users/zerof/Downloads/snippet.epub")
    # output_path = Path("C:/Users/zerof/Downloads/output.epub")
    # translate_epub_main(source_path)

    # result = ollama_request("glm4:9b", "Hello, world!")
    # print(result)

    # 读取hard_sentences.txt文件，遍历每个非空句子，调用overall_difficulty并打印
    with open("hard_sentences.txt", "r", encoding="utf-8") as f:
        for line in f:
            print(repr(line))  # 打印原始行内容
            sentence = line.strip().strip('"')
            if sentence:
                print(f"{sentence}\n难度: {overall_difficulty(sentence):.2f}\n")
    


if __name__ == "__main__":
    main()
