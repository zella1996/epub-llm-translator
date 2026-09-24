# 句子直译：版本化 prompt 基线

本文件保留当前可复现的 prompt 契约与公开结论。真实书籍的精确段落、请求用量、
人工评分和本地 trace 路径存放在 Git 忽略的 `PROJECT_LOCAL.md` 所索引的私有归档。
当前产品状态以 [项目说明](../PROJECT.md) 为准。

## 当前版本

实验预设 `glm53flash-literal-baseline` 使用 `reading-literal-baseline` v6。
它要求按原文的信息顺序和确定程度直译中文，专名及附带称谓逐字保留；
普通词与代词翻译，亲属关系不明确时避免擅加长幼或父母系信息。
单轮样本改善不等于跨样本稳定性，不把更少 token 或更短延迟直接视为质量提升。

## Quality system 原文

```text
Provide a literal Chinese translation of each English sentence.
Follow the source's progression of ideas and information order as closely as natural Chinese allows.
Preserve the source's degree of certainty.
Copy proper names and their attached titles verbatim from the source into the Chinese translation, without translation or transliteration. Translate kinship terms unless the Chinese translation would add relationship details not stated in the source, such as relative age or paternal/maternal lineage; in that case, keep the original English term. For example, translate father, mother, son and daughter, but keep brother and sister when relative age is unspecified. Translate all other words, including common nouns, verbs, adjectives and pronouns, into Chinese.

In the input, source is the text to translate, syntax is a Stanza syntax reference, and initial_difficulty is the preliminary difficulty assessment.
Review the difficulty: fluent (directly understandable), effortful (understandable with effort), or blocking (the meaning cannot be reliably determined).

Return only a JSON object {"sentences":[...]} with each input index exactly once.
For fluent items, return only index and difficulty. For effortful or blocking items, return only index, difficulty and meaning.
```

fast system、JSON user 封装和模型参数沿用相应版本的结构化句意预设。
冻结样本和难度标签后评估句意完整性、忠实性与中文可读性；无 meaning 的
`fluent` 改判不能算作难例通过。正式运行仍需独立授权、预算和 EPUB 验收。
