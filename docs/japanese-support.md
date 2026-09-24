# 日文支持与已知限制

日文支持由 EPUB package 的 `dc:language` 自动选择。`ja`、`ja-JP`、大小写及下划线变体归一
为 `ja`；缺失语言继续按历史兼容规则视为英文，未知语言和互相冲突的多个语言值会在模型调用前
报错。程序不根据假名或汉字猜源语言。

## 能力矩阵

| 能力 | 英文 | 日文 |
| --- | --- | --- |
| legacy 段译/句意卡 | 支持 | 支持，独立日译中 prompt/validator |
| `reading-v1` | 支持 | 支持；真实模型验收覆盖此路径 |
| 分句 | `english-sentence-v1` | `japanese-exact-v3`，原字符连续切片 |
| 本地门控 | 英文词数/结构启发式 | `japanese-short-text-safe-v1` 固定应答 + `japanese-short-text-chars-v1` 字符保底；不复用英文 15-word 规则 |
| 本地句法 | 可选 spaCy/Stanza，部分 profile 强制 Stanza | `off-v1`；显式请求英文分析器会失败 |
| ruby | 不适用 | 模型只见基础文字；原 ruby DOM 保留 |
| 横排 EPUB 2/3 | 自动化覆盖 | 自动化覆盖；《人間失格》为转换样本 |
| 竖排、RTL、非 ASCII 路径、字体、Media Overlay | 通用保真规则 | 《草枕》原生 EPUB 3 已完成结构金标准验收 |
| Kindle 实机 | 历史 Paperwhite/Calibre 链路 | 《草枕》章节在 Kindle 的 KOReader 中通过；不等于原生 Kindle/Send to Kindle |

两个 pipeline 都支持 inspect、dry-run、preview、按章/整书遍历、缓存恢复与写回。日文 token
估算按日文字符使用单独的保守公式，只是预算上界，不是供应商计费保证。

## 日文短文本门控

默认 `--japanese-short-text-gate auto` 对日文解析为 `safe-v1`，其他语言解析为 `off`。
`--japanese-short-text-chars N` 默认 `30`，`0` 关闭；它按门控接收的 `len(text.strip())` 计
Unicode 字符，包含标点，ruby 的 `rt/rp` 已不属于模型基础正文所以不计。字符数不超过阈值时
无条件跳过 LLM，不因问句、省略、否定、条件/转折、未完句、指代/指示、专名、ruby 或诗歌例外。
阈值以上才由 `safe-v1` 的完整固定应答白名单判断；它不做分词或形态素分析。跳过段落没有译文
或兜底内容；需要 S3–S9 的全覆盖时使用：

```bash
--japanese-short-text-gate off --japanese-short-text-chars 0
```

inspect 和 dry-run 会单列有效阈值、模型段落、“日文短文本跳过”及“日文字数保底跳过”，后两者
都带 `chapter:paragraph` 坐标。S10 对三本
离线样本审计了 822、1,094 和 2,635 个正文单元，三书拟跳过坐标均为空，因此误跳为零并启用
默认值；这不证明这些文学样本能节省调用，只证明当前极窄规则没有误跳。

gate/字符阈值选择不改变已校验的模型生成内容身份。旧缓存只要源书、段落、prompt、schema、模型与
上下文身份相同即可复用；新 manifest/run identity 会记录 gate 模式和实际字符阈值，不能把旧
`off-v1` manifest 冒充新运行。显式回放旧 manifest 时必须同时选择
`--japanese-short-text-gate off --japanese-short-text-chars 0`。

`30` 是与英语 15 个词对应的起始参考值，不是语言学等价保证。两个 pipeline、inspect 和 dry-run
使用同一有效阈值；三书离线审计应重新输出候选分布，用于量化调用量变化，而不是用文本类型否决规则。

## 验收证据边界

| 样本 | 已证明 | 未证明 |
| --- | --- | --- |
| 《人間失格》 | `reading-v1` chapter 1 共 62 段真实模型、缓存恢复、质量抽查和横排写回 | 原生 EPUB 或 Kindle 兼容；该文件由 XHTML 经 Calibre 转换 |
| 《草枕》 | 原生 EPUB 3 chapter 5 共 163 段真实模型、零调用重建、ruby/竖排/RTL/导航/CSS/字体/Media Overlay 保真；Kindle/KOReader 跳转、返回和分片性能 | 现代口语代表性、整书真实翻译、原生 Kindle/Send to Kindle、其他设备版本 |
| 住野よる边缘样本 | EPUB 2 混合标记整书离线写回；源/输出归一化 findings 差集为空 | 商业正文未发送给模型；无真实译文质量或设备结论 |

S7/S8 的真实运行只覆盖 `reading-v1`。legacy 有合成响应、完整自动化和三书离线写回证据，不能
据此声称与 `reading-v1` 有同等真实模型质量。三本书、缓存、译本、trace 和含正文审计报告都只
保存在 Git 忽略目录。

## 已知限制

- 日文没有形态素分析、GiNZA 或日文 Stanza；复杂度与辅助选择主要由模型判断。
- 默认 30 字符保底会无条件省去短段调用，因此可能跳过需要上下文才能翻译的文本；它是成本控制，
  不是语义安全判断。需要全覆盖时显式设为 0。
- 诗歌、标题、表格、代码、图片说明和原书注释仍按正文选择规则排除。
- 源 EPUBCheck error 默认阻止运行。只有确认正文仍可严格解析时才使用
  `--allow-invalid-source`；输出不得新增归一化 finding 或内部链接问题。
- 已验证设备范围是用户的 Kindle/KOReader，但型号、固件、KOReader 版本和导入方式未记录。
  不应外推到所有 Kindle、原生阅读器或 Send to Kindle。
- 不处理 DRM、加密、固定版式或脚本 EPUB；不发布书籍、缓存、trace 或生成译本。
