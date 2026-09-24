# EPUB LLM Translator 项目说明

最后更新：2026-09-24

当前分支：`codex/japanese-book-support`

当前代码基线：S9 验收及 S10 交付见
[`docs/japanese-adaptation-progress.md`](docs/japanese-adaptation-progress.md)

本文是项目目标、当前实现、有效进度、运行基线与下一步的统一入口。代码和测试决定“已经
实现什么”；本文记录经验证的产品判断和运行事实。旧实验的价格、余额、临时授权、工作树
状态和已被后续实现取代的待办不再作为当前依据。

## 1. 项目目标

这是一个面向个人使用、本机运行的命令行工具：把无 DRM、可重排版的英文或日文 EPUB 2/3 转成
适合中文母语者在 Kindle 上持续阅读的学习版 EPUB。

首要目标不是生成练习或完整语法课，而是帮助读者在保留源文的前提下：

- 看懂当前段落并继续阅读；
- 沿源语言的信息展开理解主线、插入、限定、条件、指代和语气；
- 在需要时获得忠实、可读的中文参考和最小充分的英语阅读提示；
- 避免辅助内容误导、打断阅读或破坏原书结构。

用户允许重新评估固定字段、模型阶段和解析器，也容忍不可完全消除的模型误差。产品判断
依次看阅读是否继续、内容是否误导、辅助是否过重、运行成本是否可接受。当前支持英译中和日译中；
不加入练习、自检题、复述、迁移测试或复习任务。

## 2. 当前结论

阅读辅助重设计的 P0–P4 已完成，`reading-v1` 已通过纯文本、EPUB、设备链路和真实章节的
自动验收。目前位于“人工连续阅读”边界，而不是继续无样本地扩写 prompt 或解析器。

- CLI 默认仍是 `legacy`；新路径必须显式传 `--pipeline reading-v1`。
- 当前验证最充分的运行组合是 GLM-5.3-Flash、`glm53flash-literal-baseline` 和
  `zhipu-glm53`。`reading-v1` 当前生产路径不运行 Stanza；该预设的 Stanza 要求属于 legacy
  请求身份和旧路径。
- 当前读者界面采用 `translation-first`：正文段末只有一个普通 `›››` 入口；辅助区先显示段译，
  再按需要显示“原句 → 句译 → `•` 解析”。句卡从第二张开始有细分割线。
- 段落级跨句解析默认不生成、不渲染；需要时显式使用 `--reading-paragraph-aids`。
- `reading-v1` 的 15-word 短文本规则只适用于英文。日文使用独立的固定应答门控和可调字符保底：
  `--japanese-short-text-chars` 默认 30 个 Unicode 字符、0 关闭；不超过阈值的段落无条件跳过 LLM，
  不按文本类型设置例外。长段中的短句仍保留句译。
- 正式产物只有 EPUB。Calibre/AZW3 仅用于兼容性测试，不进入生产输出流程。

## 3. 读者体验与内容契约

### 原书与导航

- 英文或日文正文逐字保留；除定位 ID 和段末 `›››` 链接外，不改变正文内容与结构。日文 ruby
  向模型只暴露基础文字，原 DOM 保留。
- 学习内容放在独立的非线性解析 XHTML。正文到辅助区只使用普通单向链接；返回依赖 Kindle 阅读历史。
- 生成内容不使用 `noteref`、`footnote`、`aside`、上标或反向链接，因此不会被 Kindle 当成
  脚注弹窗。
- 原书已有脚注、链接、编号、样式和非目标资源保持不变。

### 阅读辅助

`reading-v1` 使用版本化的 `ParagraphAssistance` 契约：

- 每个源句恰好有一条共享句译和一个 `fluent` / `effortful` / `blocking` 难度；段译由这些
  句译按原索引拼接，不再生成第二份相互矛盾的段译。
- `phrase` aid 解释唯一、精确匹配的源短语；`sentence` aid 解释主线、附着、信息顺序、
  措辞、搭配或语气；`paragraph` aid 只处理至少两句之间的真实衔接。
- aid 必须相对译文提供可指出的英语阅读增量，不能只是复述译文、概括情节或罗列术语。
- 解释用简体中文；英文只用于精确引用源文、短标签或必要示例。
- 模型不得根据书名记忆或现实常识补写动机、关系、先行词和情节；证据范围限当前段落及
  明确允许的有界背景。
- 专名及附带称谓保留原文；普通词和人称代词必须翻译。亲属关系不明确时使用不添加长幼、
  父系或母系信息的中性中文。

核心句译缺失、重复/越界索引、残留普通英文词、明显数字丢失或异常长度会使结果失败。
单条 aid 无效时保留有效句译并标记 `partial_aids`；整个 aids 字段不可用时标记
`translation_only`。合法的 `aids=[]` 仍是完整结果。

当前协议版本：

- 结果 schema：`reading-assistance-v1`
- 分句器：英文 `english-split-v1`；日文 `japanese-exact-v3`
- 门控：英文既有本地门控；日文 `japanese-short-text-safe-v1` + `japanese-short-text-chars-v1`
- 句法：英文按 profile；日文 `off-v1`
- 本地校验：`reading-validation-v2`
- prompt：`reading-experiment-v20`
- 定向复核策略：`fronted-inverted-conditional-v1`
- 冻结清单：`reading-frozen-manifest-v1`
- 布局：`reading-aid-layout-v1`

日文能力矩阵、真实样本证据与未验证范围见
[日文支持与已知限制](docs/japanese-support.md)。

## 4. 实现路径

```text
EPUB 安全预检与正文识别
  → 本地短文本/显式跳过
  → reading-v1 单次生成，或显式 two-stage 草稿→完整复核
  → 本地 schema、索引、源文、数字和残留英文校验
  → 必要时一次有界 core repair / directed review
  → 版本化结果缓存或冻结 manifest
  → 派生段译、句卡和可选跨句解析
  → 章末单向链接渲染
  → DOM/正文快照、内部链接、ZIP 与 EPUBCheck 验收
  → 正式 EPUB
```

关键设计：

- `single` 与 `two-stage` 都返回同一个最终契约；two-stage 的 review 返回完整替代结果，
  不是局部 patch。
- `--reading-directed-review` 允许一次完整 core repair，并只对降级结果或固定高风险目标再做
  一次定向复核，不形成无限重试。
- 前置目的/情境短语加倒装条件是确定性高风险模式，命中时进入 focused review。
- core repair 已成功而后续 review 使核心句译退化时，回退到已验证版本；普通无效 review
  仍然失败。该保护已在真实运行中触发并留下 `review_fallback` 诊断；精确段落定位见本机记录。
- `--reading-prior-context` 只发送同章前一正文段作为有界背景，不翻译、不引用，也不能替代
  当前段落证据；其 hash 进入缓存身份。
- `reading-eval` 可以接收与句表 hash 绑定的冻结 Stanza 证据做受控实验；生产
  `translate --pipeline reading-v1` 当前明确记录 `stanza=off`，不启动本地 parser。
- 缓存身份包含 pipeline、schema、prompt、模型请求策略、上下文和复核策略。
  旧 legacy 缓存不能命中 `reading-v1`，生成内容与视觉布局解耦，可零调用重渲染。
- 并发只保持 `--workers` 个在途段落。任一失败后停止提交新任务，保留已完成缓存；无法取消
  的在途请求会在进入下一阶段前停止。设置 `--max-tokens` 时，为保证逐段 usage 边界，当前
  实现会把有效 worker 数收敛为 1。
- trace 保存脱敏后的请求、响应、usage、校验与最终结果，不保存 headers、API key 或 properties
  内容。trace 含书籍正文，只能留在本机忽略目录。

## 5. 代码入口

| 领域 | 主要文件 | 责任 |
| --- | --- | --- |
| CLI 与运行配置 | `translator/cli.py` | 命令、预设解析、预算、进度、provider/client 组装 |
| 新阅读契约 | `translator/reading_assistance.py` | 数据类型、源句绑定、校验、降级、序列化 |
| 新提示词 | `translator/reading_prompts.py` | single/draft/review/repair 的公共语义与输出契约 |
| 编排 | `translator/translator.py` | 生成、复核、缓存、并发、恢复、冻结清单、预算停止 |
| EPUB | `translator/epub_processor.py` | 安全读取、正文定位、章末渲染、链接与结构保持 |
| 本地分析 | `translator/sentence_analyzer.py` | 分句、门控、Stanza/spaCy adapter 与句法缓存身份 |
| 模型传输 | `translator/llm_api.py`、`translator/openrouter_client.py` | OpenAI-compatible/OpenRouter 请求、重试、usage、trace |
| 请求预设 | `translator/tuning_profiles.py`、`translator/provider_profiles.py`、`translator/openrouter_model_profiles.py` | 模型与 provider 的版本化 wire policy |
| 缓存与追踪 | `translator/cache.py`、`translator/trace.py` | SQLite 恢复、独立 syntax cache、脱敏审计 |

长期产品边界见 `GOAL.md`，用户命令见 `README.md`，兼容性矩阵见 `COMPATIBILITY.md`，重设计
理由和阶段验收方法见 `docs/reading-comprehension-redesign-plan.md`、
`docs/reading-assistance-implementation-plan.md` 与 `docs/reading-content-acceptance-cycle.md`。

## 6. 已完成进度与证据

### P0–P4

- P0：严格整数/布尔索引、非有限 confidence、数字子串误判和跨句 `as if` 误拦已修复。
- P1：新数据契约、纯文本 `reading-eval`、single/two-stage、冻结 Stanza 输入、usage/trace、
  core failure 与 aid 降级边界已完成。早期开发集据此选择 single 作为首版路径。
- P2：冻结结果可在不构造模型客户端的情况下生成 EPUB 2/3 预览；正文、原脚注、链接和 XML
  转义通过集成验证。
- P3：`translate --pipeline reading-v1`、独立缓存身份、dry-run、实际 token 停止、恢复、零调用
  重渲染和可携带冻结 manifest 已完成。
- P4：离线布局 A/B、有界前段背景、定向复核、可选正文短语链接和 reasoning effort 对照已完成。
  `max` 增加延迟/token 且未稳定改善固定样本；生产策略最终依靠 two-stage、有界修复和内容门。

### 真实章节、设备与连续章节验收

- 受控真实章节的自动内容审计全部得到 `complete`，并在 Kindle 上确认普通链接进入辅助区、
  系统返回与长内容显示正常；据此选择 `translation-first`。精确段落、引用和产物路径仅记于
  本机验收记录。
- 连续章节使用 GLM-5.3-Flash、`two-stage`、有界前段背景和 directed review 完成自动验收。
  一处供应商输出侧内容过滤按显式跳过处理；缓存完整性为 `ok`。最终 EPUB 的 ZIP、内部链接
  和 EPUBCheck 均通过，EPUBCheck 无发现项。中断请求有缺失的 usage，无法给出可靠的全流程
  token 总账。
- 既有成品生成于“跨句解析默认关闭”之前，仍含 paragraph aid。要取得当前默认界面，应保持
  同一 source、profile、最终 cache 和生成身份，换新输出路径零调用重渲染，不传
  `--reading-paragraph-aids`，并按本机记录显式跳过被过滤的段落。精确章节、备份与产物定位见
  Git 忽略的 `PROJECT_LOCAL.md`。

### 自动测试快照

基线提交的完整测试为 `330 passed, 35 skipped`，`git diff --check` 通过。跳过项需要显式启用
EPUBCheck、Calibre、localhost HTTP、真实书或本地 Stanza 资源；跳过不等于失败。

2026-09-24 在另一台 arm64 macOS 上复现当前项目环境：Python 3.14.7、同版本 Python 依赖、
EPUBCheck 5.3.0 和 Calibre 9.14。常规测试 `425 passed, 36 skipped`；另启用 EPUBCheck、
Calibre、localhost HTTP 集成测试分别为 `12 passed`、`1 passed`、`15 passed`。连接方式和
机器身份只记录在 Git 忽略的本机文档。

## 7. 供应商、模型与历史实验结论

当前代码支持 generic OpenAI-compatible、智谱 BigModel、百炼、OpenRouter 和 LM Studio；
模型参数必须来自显式、版本化的 tuning/model profile，未知 OpenRouter 模型会在联网前失败。

- **当前阅读基线：**GLM-5.3-Flash + `glm53flash-literal-baseline`。最终连续章节
  `reading-v1` 运行不执行 Stanza；同一 tuning profile 用于 legacy 时仍要求 Stanza
  `default_accurate`。
- **GLM-5.2：**智谱直连和百炼均完成过真实章节闭环；structured-meaning 证明“内部保留
  syntax、读者只看 meaning”可行，但已被新的 `reading-v1` 内容结构取代，不恢复旧 UI。
- **OpenRouter：**原生客户端、隐私路由、usage/费用/provider trace 和模型级请求策略已实现。
  Gemini 3.7 Flash 完成过真实章节 EPUB 验收；GLM-5.2 完成过单段闭环；Qwen 235B 只做过
  单请求 POC。动态 provider 路由可用，但不等于跨 provider 质量可复现。
- **Qwen/DeepSeek：**具备显式实验预设并通过覆盖协议修复，但真实文学样本曾出现专名、亲属
  关系和措辞问题，不是当前整书默认。
- **LM Studio：**本地 OpenAI-compatible structured output 链路已验证；通用 JSON Schema 只
  保证顶层对象，业务字段仍由本地代码严格校验。本地小模型没有证明能替代云端阅读基线。
- **本地句法：**spaCy 是轻量可选项；Stanza `default_accurate` 是高精度预设的内部证据来源。
  Apple MPS 在已测环境不可用，Stanza 按 CPU 运行。旧 reader-guide safe projection benchmark
  为 10/10、从句关系 benchmark 为 28/38（precision 80.0%、recall 73.7%）；这些能力保留为
  legacy/内部基础，不恢复为当前读者界面的独立语法卡。
- **quality replay：**可冻结 fast 输出和 Stanza 证据后只重放 quality，避免把 fast 随机波动
  错归因于 quality prompt。历史 compact-v5 显著降低输入 token，但单样本仍改变卡片保留，
  因此“更省”不等于“等质”。

历史模型价格和账户余额是时效性信息，不在本文固化；任何新真实调用都应重新核对当前参数、
授权范围、token/费用上限和数据策略。

## 8. 运行与安全边界

- 永不覆盖输入 EPUB 或已有输出；每次运行使用明确的新输出路径。
- 私人 EPUB、生成物、SQLite cache、trace、阅读记录、模型资源和凭据留在
  `.epub-llm-translator/` 等 Git 忽略目录，不提交 Git。
- API key 只从环境变量或被忽略的 `secrets.properties` 读取；不得打印、复制到命令、trace、
  文档或提交中。
- 当前工作区的机器连接、环境状态、具体书籍样本和文件定位见 Git 忽略的
  `PROJECT_LOCAL.md` 及其中索引的本机手册。工作区中的这些入口是指向项目同级私有目录的
  符号链接，便于 Codex 直接查阅；新克隆需在本机恢复链接。使用前先核对，不将其提交到仓库。
- 正式运行先 `inspect` 和 `--dry-run`，明确章节、模型、provider、独立 cache/trace、workers、
  RPM、token/费用边界，再发起真实调用。
- usage 缺失不能当作零；并发中无法取消的在途请求可能使本地 token 上限略微超出。
- 失败后复用同一配置与 cache 续跑，不删除已完成结果。`reading-v1` 当前拒绝
  `--prior-tokens` 和费用上限参数；`--max-tokens` 只约束本次进程可观测到的实际 usage，跨次
  续跑必须在外部单独对账。legacy 路径才支持 `--prior-tokens`。
- 正式 EPUB 必须通过正文/结构快照、内部链接、ZIP 和 EPUBCheck；验证失败的中间产物不能
  冒充成品。
- 不处理 DRM、加密、固定版式、脚本或媒体叠加 EPUB；不把本工具扩展成通用损坏 EPUB 修复器。

## 9. 下一步

当前最有价值的工作按优先级排列：

1. 使用已验收的最终 cache 零调用重渲染连续章节，取得默认不含 paragraph aid 的新 EPUB；
   保持同一 source/profile/cache，使用新输出路径并按本机记录跳过被过滤的段落。
2. 对新 EPUB 做连续阅读验收，重点记录“是否看懂并继续读、是否误导、是否打断”，并定向抽查
   `partial_aids`。不要把新的审美偏好直接写成模型硬规则；先冻结失败段落并做最小变量复验。
3. 阅读体验稳定后，再做受控的连续多章节乃至整书运行，对账实际 token、费用、耗时、缓存命中
   和中断恢复。既有 dry-run 曾明显低估真实章节 token，不可直接线性外推。
4. 逐步扩大不同出版社 EPUB/CSS/脚注样本；只在真实阅读摩擦支持时再扩展解析器、inline phrase
   或 OpenRouter Batch。

不要恢复句序号、旧四字段卡片、脚注式弹窗、反向链接、“阅读辅助”标题后缀、默认 paragraph
aid 或正式 AZW3 输出。未来若阶段状态变化，直接更新本文，不再新增会相互覆盖的 session 文件。
