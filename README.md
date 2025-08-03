本项目是一个用于处理和分析英文电子书（epub格式）的工具，支持句子难度分析、自动翻译、章节管理等功能。  

## 主要功能

- **自动书名读取**: 从epub文件的opf文件中自动读取书名（`<dc:title>`标签）
- **句子难度分析**: 使用spacy和wordfreq分析英文句子的难度
- **智能翻译**: 基于Ollama LLM模型进行智能翻译和解释
- **章节管理**: 自动创建扩展章节来存放翻译注释
- **脚注系统**: 为困难句子自动生成脚注和锚点链接

## 依赖

依赖包括：lxml、spacy、wordfreq、textstat、ollama等。  
如需使用，请先安装requirements.txt中的依赖，并根据实际需求配置Ollama模型。

## 使用方法

1. 安装依赖：`pip install -r requirements.txt`
2. 配置Ollama模型
3. 运行：`python main.py`

## 测试

运行测试脚本验证书名读取功能：
```bash
python test_title_reading.py
```

欢迎提出建议或贡献代码！