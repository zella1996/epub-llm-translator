本项目是一个用于**分析英文 EPUB 电子书并为高难度句子生成中文翻译脚注**的工具。  
它会扫描整本书的章节，对每个段落做难度分析，只为“真的很难”的句子生成详细的中文解释与翻译，并把这些解释统一写入新的“译文参考章节”，同时在原文段落后插入 `#` 锚点供跳转。

## 核心功能

- **自动获取书名**：从 EPUB 元数据中读取 `<dc:title>`，用于提示大模型当前书名和上下文。
- **智能句子筛选**：对每个英文句子进行难度打分，只给高难度句子加脚注，避免“全书机翻”。
- **本地 LLM 翻译讲解**：通过本地 `Ollama` 模型生成“分析 + 翻译 + 单词解释”等详细说明。
- **扩展章节与脚注跳转**：为每个章节自动创建一个“译文参考”扩展章节，并在原文段落后放置 `#` 链接跳过去。

## 一次 EPUB 处理的大致流程（只看流程与工具）

1. **解包并读取 EPUB**
   - 解压 EPUB，读取 `container.xml` 和 `opf`，拿到书名和章节顺序。
   - **用到的工具 / 库**：`zipfile`（标准库）、`lxml`。

2. **遍历章节文件**
   - 按 EPUB 的 `spine` 顺序依次读取每个 HTML/XHTML 章节，逐段落处理。
   - **用到的工具 / 库**：`lxml`（解析和遍历 DOM）。

3. **对段落做句子切分与难度分析**
   - 把段落文本切成句子，计算每个句子的综合难度分数，只保留“难句”。
   - **用到的工具 / 库**：
     - `spaCy` + `en_core_web_sm`：英文分词、分句、依存句法分析。
     - `wordfreq`：根据频率判断生僻词比例。
     - `textstat`：计算英文可读性分数。

4. **调用本地大模型生成解释与翻译**
   - 对每个“难句”构造 Prompt，把书名和句子一起发给本地大模型，请它输出分析、翻译和单词解释。
   - **用到的工具 / 库**：
     - `ollama`：与本地大模型交互（如 `qwen3:14b`、`gemma3:12b` 等）。
     - 项目根目录的 `system_prompt.txt`：定义翻译风格与输出格式。

5. **在原文中插入脚注锚点**
   - 对每个包含难句的段落，在段落后面插入一个 `#` 超链接，链接到后面扩展章节中对应的脚注位置。
   - **用到的工具 / 库**：`lxml`（修改 HTML 结构）。

6. **为每个章节生成“译文参考”扩展章节**
   - 自动复制一个空白章节文件，用来集中展示所有脚注；为每条脚注生成带 `id` 的标题和内容。
   - 同时更新 EPUB 的目录，让扩展章节也出现在目录中。
   - **用到的工具 / 库**：`lxml`、`zipfile`（最后重新打包）。

7. **重新打包为新的 EPUB**
   - 写回所有修改后的章节和扩展章节，打包成一个新的 EPUB 文件，并清理临时目录。
   - **用到的工具 / 库**：`zipfile`、`shutil`（标准库）。

## 使用方法

### 1. 安装依赖

```bash
pip install -r requirements.txt

# 安装 spaCy 英文模型（只需一次）
python -m spacy download en_core_web_sm
```

`requirements.txt` 中的核心依赖：

- `lxml`：解析 EPUB 结构与 HTML/XHTML。
- `spacy`：英文 NLP 分词、句法分析、句子切分。
- `wordfreq`：英文单词频率词表。
- `textstat`：文本可读性指标。
- `ollama`：与本地大模型通信。

### 2. 准备 Ollama 模型与 system prompt

- 确保本机已安装 Ollama，并已拉取需要的模型，例如：

```bash
ollama pull gemma3:12b
ollama pull qwen3:14b
```

- 在项目根目录创建并编辑 `system_prompt.txt`，定义 LLM 的角色和输出格式（例如必须包含“分析：… / 整体翻译：… / 单词解释：…” 结构）。

### 3. 运行示例脚本

在 `main.py` 中可以看到一个具体示例：

- 指定输入 EPUB 路径 `source_path` 与输出路径 `output_path`。
- 通过 `filename_pattern` 只处理指定文件名模式的章节。
- 通过 `model` 选择所用的 Ollama 模型，例如 `ModelType.GEMMA3_12B`。

命令行运行：

```bash
python main.py
```

如需灵活调用，也可以在自己的脚本中直接使用：

```python
from pathlib import Path
from translator.translator import translate_epub_main
from translator.llm_api import ModelType

translate_epub_main(
    epub_path=Path("input.epub"),
    output_path=Path("output_with_notes.epub"),
    filename_pattern=r"^ch\d+$",      # 可选：只处理匹配的章节文件名（不含后缀）
    book_name="自定义书名",            # 可选：不填则从 EPUB 元数据中读取
    model=ModelType.GEMMA3_12B,       # 选择 Ollama 模型
)
```

## 第三方依赖一览

- **EPUB 解析与操作**
  - `lxml`：解析 `opf` / `ncx` / HTML/XHTML，操作 DOM。
  - `zipfile`（标准库）：解压与重打包 EPUB。
- **自然语言处理与难度分析**
  - `spacy` + `en_core_web_sm`：分句、依存句法分析、标注。
  - `wordfreq`：根据 Zipf 频率识别生僻词。
  - `textstat`：计算文本易读性分数。
- **大模型调用**
  - `ollama`：调用本地 LLM 模型（如 `qwen3:14b`、`gemma3:12b` 等），生成句子讲解和翻译。

## 适用场景与限制

- **适用场景**
  - 精读英文经典小说、专业书籍时，希望只对真正难的句子生成详细解释和翻译，而不是整段机翻。
  - 希望在同一个 EPUB 内保留原文排版，同时多出一个“译文参考”章节，方便来回跳转。

- **当前限制**
  - 依赖本地安装的 `Ollama` 及相应大模型，显存/内存不足时可能需要选择更小的模型。
  - 难度阈值 `DIFFICULTY_THRESHOLD`、生僻词阈值等目前在代码中固定，如需更精细控制可在 `translator/sentence_analyzer.py` 与 `translator/translator.py` 中调整。

欢迎在实际使用中根据需要调整模型与参数，也欢迎提交 Issue 或 PR 一起完善本项目。