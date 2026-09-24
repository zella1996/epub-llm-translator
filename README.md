# EPUB LLM Translator

这是一个面向个人使用、本机运行的命令行工具，用来把无 DRM、可重排版的英文或日文 EPUB 转换成适合中文母语者阅读的学习版。

工具保留原文，在需要模型帮助的段落末尾加入普通 `›››` 单向链接，直接跳到同一章节末尾的完整解析。返回原文使用阅读器的阅读历史/返回功能，不生成指回原段落的反向链接，以避免阅读器把双向链接识别为脚注弹窗。原书自己的脚注保持不变。

当前目标、实现路径、有效进度、运行基线与下一步统一见 [PROJECT.md](PROJECT.md)。
长期产品边界见 [GOAL.md](GOAL.md)。
实际兼容性验证记录见 [COMPATIBILITY.md](COMPATIBILITY.md)。
逐项完成状态见 [STATUS.md](STATUS.md)。

`codex/reading-comprehension-redesign` 分支已完成 P0–P4 和第 32–52 章自动验收，详情见
[PROJECT.md](PROJECT.md)。设计理由和阶段方案仍保留在
[阅读辅助重设计方案](docs/reading-comprehension-redesign-plan.md) 与
[技术改造方案](docs/reading-assistance-implementation-plan.md)。下面的命令同时覆盖 legacy
与显式 `reading-v1` 路径。

## 当前状态

目前已经完成第一版纵向闭环：

- 安全解包和严格 XML/XHTML 解析；
- 拒绝 DRM、加密、固定版式和脚本内容；原生日文样本的 Media Overlay 资源按原样保留；
- EPUB 2/3 spine 正文段落定位；
- 按 EPUB 元数据自动选择英文或日文任务，排除标题、诗歌、代码块、表格、图片说明及显式外语段落；
- OpenAI-compatible 双模型流程；
- 忠实自然的段落译文、三级难度和困难句理解卡；
- 自动读取书名/作者、支持显式作品年代，帮助模型判断历史词义与语气；
- 版本化供应商预设，当前含 Qwen 百炼非思考结构化输出配置；
- 指定单段生成 PREVIEW EPUB；
- 全书或指定章节处理；
- 英文使用 conservative/balanced/off 本地难度门控；日文使用独立的固定短应答安全门控；
- 同章末普通单向解析链接、原书脚注隔离和 ID 防碰撞；
- 正确的 EPUB ZIP 打包；
- EPUBCheck 输入/输出差异门槛；
- 个人阅读画像校准和低/中/高辅助密度；
- token/费用/请求数/最低调用时间粗略估算、dry-run、费用与 token 上限；
- SQLite 本地缓存、有限重试、保序并发和失败后重跑。

北京百炼 Qwen 真实 API、Gutenberg EPUB、Calibre → AZW3 与 Paperwhite 12 导航方案均已验证。更广泛的出版社 EPUB/CSS 覆盖和小章节费用/恢复对账仍在路线图中。

日文首版已完成 `ja` 元数据识别、ruby 基础正文提取、精确分句、独立日译中提示与校验、
两个 pipeline、缓存恢复、竖排/RTL/Media Overlay 保真及 Kindle 上 KOReader 实机验收。
能力矩阵、三本样本证据和边界见
[日文支持与已知限制](docs/japanese-support.md)。

## 安装

需要 Python 3.10 或更高版本。

~~~bash
# Homebrew Python；不会使用 Anaconda 的解释器。
/opt/homebrew/bin/python3 -m venv .venv
source .venv/bin/activate
pip install -e .
~~~

通用调优可选装 spaCy 句法组件：

~~~bash
pip install -e '.[syntax]'
python -m spacy download en_core_web_sm
~~~

当前 meaning-only 调优预设强制使用 Stanza 的依存句法与成分句法；安装较重依赖并下载英文资源：

~~~bash
pip install -e '.[stanza]'
python -c "import stanza; stanza.download('en', package='default_accurate', processors='tokenize,pos,lemma,depparse,constituency')"
~~~

`default_accurate` 还需要本地 Electra-large transformer 缓存。正式运行不会静默联网下载；请在明确的安装步骤中预先下载 `google/electra-large-discriminator`，并通过 `--stanza-hf-cache-dir` 指向对应的 `HF_HOME` 根目录。

之后请在已激活的 `.venv` 中运行命令，或直接使用 `.venv/bin/python`。

正式转换要求系统能够执行 epubcheck。只有开发和诊断时才应使用 --skip-epubcheck。

API key 通过环境变量提供，不作为命令行参数传入：

~~~bash
export OPENAI_API_KEY='...'
~~~

使用 OpenRouter 时，将密钥放在 `OPENROUTER_API_KEY` 环境变量或已忽略的
`secrets.properties`，并选择专用预设：

~~~bash
epub-llm-translator preview INPUT.epub OUTPUT.epub \
  --chapter 32 --paragraph 7 \
  --base-url https://openrouter.ai/api/v1 \
  --fast-model google/gemini-3.7-flash \
  --quality-model google/gemini-3.7-flash \
  --tuning-profile gemini37-openrouter-grounded
~~~

`openrouter` 预设使用专用 payload adapter，并复用公共 Chat Completions 运行时；
它要求路由到支持请求参数的 provider，并设置 `data_collection: deny`。reasoning
不属于 provider 默认参数，而由每个 OpenRouter model profile 独立声明。Gemini
3.7 Flash 的 reasoning 为必需能力，profile 将它固定为官方支持的 `low` 档。
`gemini37-openrouter-grounded` 将 Gemini 3.7 Flash 固定到当前 grounded
compact 提示词、准确 Stanza 和 4096 输出 token 上限。OpenRouter GLM-5.2
使用 `glm52-openrouter-grounded` 调优预设；对应 model profile 会发送已验证的关闭
推理配置。

OpenRouter 的统一入口不代表所有模型接受相同参数。模型专属的 structured output、
reasoning、采样和输出上限必须由显式 model profile 声明；未知模型会在联网前
fail-fast，不能继承其他模型的参数。新增模型的最小适配步骤和不可放宽的隐私边界见
[`docs/openrouter-model-adaptation.md`](docs/openrouter-model-adaptation.md)。

智谱 BigModel 直连的 GLM-5.3-Flash 使用独立预设，避免继承 GLM-5.2 的关闭思考配置：

~~~bash
epub-llm-translator preview INPUT.epub OUTPUT.epub \
  --chapter 1 --paragraph 1 \
  --base-url https://open.bigmodel.cn/api/paas/v4 \
  --fast-model glm-5.3-flash --quality-model glm-5.3-flash \
  --tuning-profile glm53flash-grounded
~~~

`glm53flash-grounded` 按官方建议开启思考并发送 `temperature:1`、`top_p:0.95`、
`reasoning_effort:max` 和 `thinking.clear_thinking:false`。它沿用 grounded compact
提示词与准确 Stanza，并将输出上限设为 8192，给强制思考和结构化翻译留出空间。

若要继续结构化“句意”实验，使用 `glm53flash-structured-meaning`。它固定同一套
GLM-5.3-Flash 参数，但改用无读者语法导览的 structured-meaning 提示词，并将每次
输出上限提高到 16384。

新版候选预设为 `glm53flash-structured-meaning-v2`：独立重写句意任务提示词，quality
输入改为纯 JSON，保留亲属称谓和专有名词原文；模型参数、16384 输出上限及卡片筛选规则
不变。旧预设保留，新版尚未通过真实模型效果验收。完整 prompt、设计依据与对照方法见
[`docs/structured-meaning-v2.md`](docs/structured-meaning-v2.md)。

用于验证最小提示是否足够的独立预设为 `glm53flash-literal-baseline`：当前 quality v6
要求完整句译、尽量保留信息展开和确定程度，逐字保留专名／附带称谓，并只保留翻译会
引入未说明关系的亲属词。Stanza 与 JSON 协议保持。已完成一次真实整章技术验收及
六卡阅读抽样，尚未证明稳定性或最佳阅读结构，未切换生产默认。
后续 GLM Flash 工作先读 [项目说明](PROJECT.md)；
精确 prompt 与历史记录见 [`docs/literal-meaning-baseline.md`](docs/literal-meaning-baseline.md)。

对于不需要 key 的本地 OpenAI-compatible 服务，可以不设置该变量。

LM Studio 的本地服务使用专用预设：

~~~bash
epub-llm-translator preview book.epub book-PREVIEW.epub \
  --chapter 3 \
  --paragraph 12 \
  --base-url http://127.0.0.1:8080/v1 \
  --fast-model google/gemma-4-e2b \
  --provider-profile lmstudio \
  --syntax-analyzer stanza \
  --stanza-package default_accurate \
  --stanza-model-dir C:/path/to/stanza_resources \
  --stanza-hf-cache-dir C:/path/to/huggingface
~~~

`lmstudio` 预设不包含地址、密钥或模型名；它发送 LM Studio 支持的通用 object JSON Schema，并提供 2048 token 的默认输出预算。Schema 保证响应是可解析的 JSON 对象，字段、索引和枚举仍由项目自身的严格校验负责。对于只支持 text 的其他本地服务，客户端仍保留 text 响应能力。

## 使用

### 1. 检查书籍并寻找段落编号

~~~bash
epub-llm-translator inspect book.epub --chapter 1
~~~

输出中的“章节:段落”编号用于预览，例如 3:12。

### 2. 生成单段预览

~~~bash
epub-llm-translator preview book.epub book-PREVIEW.epub \
  --chapter 3 \
  --paragraph 12 \
  --base-url https://example.com/v1 \
  --fast-model fast-model \
  --quality-model quality-model \
  --work-year 1811
~~~

开发调试时可显式记录模型调用 trace：

~~~bash
epub-llm-translator preview book.epub book-PREVIEW.epub \
  --chapter 3 \
  --paragraph 12 \
  --base-url https://example.com/v1 \
  --fast-model fast-model \
  --quality-model quality-model \
  --trace-dir .epub-llm-translator/traces
~~~

每次运行会创建独立 run 目录，并按章节、段落保存实际请求 JSON、endpoint、prompt hash、供应商完整响应 body、usage、schema 校验后的 fast/quality 结果、Stanza 证据，以及写入 EPUB 前的最终 `ParagraphLearning`。`translate` 的 run metadata 还会记录缓存 profile key。结构或 HTTP 请求失败时，已收到的响应仍会先写入 trace，然后保持原有 fail-fast 行为。请求 headers、API key 和 properties 内容不会写入 trace；即使供应商意外回显 API key，写盘前也会替换为 `<redacted>`。

trace 包含原文、读者画像和模型输出，只应写入受保护的本地目录。默认建议目录 `.epub-llm-translator/traces` 已随整个 `.epub-llm-translator/` 目录被 Git 忽略；使用其他路径时应自行加入 `.gitignore`。启用 trace 后，日志写入失败会使任务立即失败，避免生成无法审计的实验结果。

如需复核一次已冻结的 quality 输入，可从受控 preview/translate trace 中取得 `quality-replay-input.json`。replay 只读取冻结的 fast 判断和 Stanza 证据；不会运行 fast、Stanza、EPUB 写入或生产缓存，但**仍会向指定 endpoint 发起一次真实 quality 请求**，因此必须先获得对应段落和 endpoint 的授权：

~~~bash
epub-llm-translator quality-replay \
  .epub-llm-translator/traces/RUN/chapter-003/paragraph-012/quality-replay-input.json \
  --base-url https://example.com/v1 \
  --quality-model quality-model \
  --quality-payload-mode compact-v5 \
  --trace-dir .epub-llm-translator/replay-traces
~~~

每次 replay 会创建独立 trace，保存 quality 请求、响应、usage 和校验结果；它不会改写原 trace 或产生 EPUB。

模型、prompt 或 reasoning 参数的研发验证不应使用 `preview`、`translate` 或 EPUB
读写。此类验证对明确授权的纯文本样例运行本地 Stanza，并将其句法证据随文本发送给
模型；以同一模型、同一参数和同一样例集比较版本。具体边界、记录字段及判定方法见
[`docs/model-prompt-text-testing.md`](docs/model-prompt-text-testing.md)。文本测试不自动触发
EPUB preview；仅在另行要求集成/阅读呈现验证时运行。GLM-5.3-Flash 的具体调优步骤与
验收条件见 [`docs/glm53flash-tuning-plan.md`](docs/glm53flash-tuning-plan.md)。

对默认开启思考的混合模型，翻译和 JSON 结构化输出通常可显式关闭思考以减少延迟与推理 Token：

~~~bash
--thinking off
~~~

对 generic/百炼/Qwen 预设，`on`/`off` 分别发送 `enable_thinking: true/false`；`default` 使用供应商或调优预设；`generic` 预设不发送扩展参数。百炼 Qwen3.7 可直接使用：

~~~bash
--provider-profile qwen-bailian
~~~

该预设带版本号、不保存 API key 或 endpoint，并默认关闭 Qwen 思考模式。显式 `--thinking on/off` 优先于预设。百炼托管的 GLM-5.2 使用独立调优预设：

~~~bash
epub-llm-translator preview INPUT.epub OUTPUT.epub \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --fast-model glm-5.2 --quality-model glm-5.2 \
  --tuning-profile glm52-bailian-grounded \
  --chapter 1 --paragraph 1 --work-year 1900
~~~

`glm52-bailian-grounded` 复用 GLM 的原句证据约束，默认发送 `enable_thinking:false`、JSON Object 和 2048 输出 token 上限，不发送智谱直连的 `thinking`、`do_sample` 或 `reasoning_effort`。百炼 Chat Completion 的思考长度参数是 `thinking_budget`；本预设首版只验收关闭思考，传入 `--reasoning-effort` 会在请求前失败。

#### 百炼网络排障：先区分执行沙箱与宿主网络

在受限执行沙箱中运行命令时，沙箱可能没有 DNS 配置，即使运行该项目的 Mac、Surge 网关和同一局域网中的 PC 均可正常解析和连接百炼。此时请求会在发起 HTTPS 连接前失败，常见错误为：

~~~text
<urlopen error [Errno 8] nodename nor servname provided, or not known>
~~~

这表示**执行环境**无法解析域名，不能据此直接修改 Surge、DHCP 或本机 DNS。应先在与真实 provider 调用相同的网络权限级别中进行只读对照：

~~~bash
python3 -c 'import socket; print(socket.getaddrinfo("dashscope.aliyuncs.com", 443, type=socket.SOCK_STREAM))'
nc -G 8 -vz dashscope.aliyuncs.com 443
~~~

若宿主环境能解析域名（Surge Fake-IP 如 `198.18.x.x` 属正常现象）且 443 可通，则应在具有宿主网络访问权限的执行环境中重跑 provider 调用；不要把它误诊为百炼、模型、API key 或 Surge 分流问题。只有宿主环境本身也无法解析或连接时，才检查 DHCP 租约、DNS 设置和 Surge 规则。所有 DNS/代理配置改动都应先明确目标客户端，避免影响整个局域网。

智谱 GLM 和百炼的 API Key 可统一保存在项目根目录的 `secrets.properties`（已被 Git 忽略）：

~~~properties
ZAI_API_KEY=你的智谱或 Z.ai-key
DASHSCOPE_API_KEY=你的百炼-key
~~~

`zhipu` 与 `zhipu-glm53` 预设会自动读取 `ZAI_API_KEY`，`bailian` 与 `qwen-bailian` 预设会自动读取 `DASHSCOPE_API_KEY`；同名环境变量优先于文件。可用 `--properties-file` 指定其他文件，或用 `--api-key-env` 指定其他键名。`zhipu` 为 GLM-5.2 发送 `thinking: {"type":"disabled"}`；`zhipu-glm53` 为 GLM-5.3 系列发送开启思考的参数；百炼预设发送 `enable_thinking:false`。

EPUB 书名和作者会自动加入模型上下文。工具不自动把 `dc:date` 当成作品年代，因为它常是电子版日期；可用 `--work-year`、`--book-title` 和可重复的 `--book-author` 显式覆盖。

当前版本不自动识别或强制处理专名、亲属词和代词指向。提示词只要求模型不要补足原文未表达的年龄、亲属关系或指代；这部分以可用性优先，后续可根据实际阅读样本再精化。

预览命令会在终端显示原文、段译和理解卡，同时生成一份完整 EPUB 副本；只有指定段落加入学习解析链接。可以换一个段落或模型重新预览。显式预览不会被本地门控跳过。

### 3. 建立个人阅读画像（可选）

~~~bash
epub-llm-translator calibrate book.epub \
  --profile .epub-llm-translator/profile.json
~~~

默认抽取 24 个真实句子，交互标记为“顺畅、费力、阻塞”。使用画像时，在 preview 或 translate 命令加入：

~~~bash
--profile .epub-llm-translator/profile.json --density medium
~~~

低密度只为阻塞句生成理解卡；中密度为费力句生成简短顺读提示；高密度保留完整理解卡。阻塞句始终完整解析。

为某本书追加 8–12 个样本时，从长期画像派生一个独立文件：

~~~bash
epub-llm-translator calibrate book.epub \
  --samples 10 \
  --base-profile .epub-llm-translator/profile.json \
  --profile .epub-llm-translator/book-profile.json
~~~

基础画像不会被静默修改。

### 4. 先估算全书调用量

~~~bash
epub-llm-translator translate book.epub unused.epub \
  --base-url https://example.com/v1 \
  --fast-model fast-model \
  --quality-model quality-model \
  --requests-per-minute 30 \
  --dry-run
~~~

估算会显示保守的最多请求数；提供 `--requests-per-minute` 时还会显示由该速率上限决定的最低调用时间。供应商排队和实际模型延迟不在本地可知范围内。

如果提供每百万 token 的输入和输出价格，还会估算费用：

~~~bash
epub-llm-translator translate book.epub unused.epub \
  --base-url https://example.com/v1 \
  --fast-model fast-model \
  --quality-model quality-model \
  --input-price 1.0 \
  --output-price 4.0 \
  --max-cost 10 \
  --dry-run
~~~

价格单位由用户决定，但三个费用参数必须使用同一货币。

日文 EPUB 由 `dc:language` 自动识别；`ja`、`ja-JP`、大小写和下划线变体都会归一为日文，
不根据正文是否含假名猜语言。下面的 dry-run 不调用模型，并分别报告模型段落和日文短文本
跳过坐标：

~~~bash
epub-llm-translator translate japanese.epub unused.epub \
  --pipeline reading-v1 \
  --base-url https://open.bigmodel.cn/api/paas/v4 \
  --fast-model glm-5.3-flash \
  --quality-model glm-5.3-flash \
  --tuning-profile glm53flash-japanese-reading \
  --provider-profile zhipu-glm53 \
  --dry-run
~~~

日文默认 gate 为 `japanese-short-text-safe-v1`，syntax 为 `off-v1`；不会加载 Stanza、
spaCy 或形态素分析器。另有独立版本化的字符保底 `japanese-short-text-chars-v1`：
`--japanese-short-text-chars N` 默认 `30`、`0` 关闭，按 `len(text.strip())` 计 Unicode
字符（包含标点，ruby 的 `rt/rp` 不计）。不超过阈值的文本无条件不访问 LLM，不区分问句、
否定、指代、专名、ruby 或其他类型；任何跳过都没有译文兜底。阈值以上才继续由固定应答
白名单门控判断。要恢复 S3–S9 的全覆盖范围，须同时传入
`--japanese-short-text-gate off --japanese-short-text-chars 0`。详见
[日文支持与已知限制](docs/japanese-support.md#日文短文本门控)。

### 5. 处理整本书

~~~bash
epub-llm-translator translate book.epub book-learning.epub \
  --base-url https://example.com/v1 \
  --fast-model fast-model \
  --quality-model quality-model \
  --provider-profile qwen-bailian \
  --work-year 1811 \
  --workers 2 \
  --requests-per-minute 30
~~~

可重复使用 --chapter 只处理特定章节。默认缓存位于 .epub-llm-translator/cache.sqlite3。学习结果缓存包含供应商预设版本、endpoint 标识、模型快照、思考模式、提示词版本、书名/作者/年代、阅读画像和辅助密度；独立句法缓存包含原书、段落文本、分析器 adapter、Stanza 版本、package、资源清单摘要和 Electra revision，不受云模型或 prompt 调优变化影响，也不会因 Mac/PC 的模型目录不同而失去同一身份。API key 永不进入缓存。

批量运行只维持 `--workers` 个在途段落，不会预先把整章提交给线程池。任一 Stanza、API 或结构校验失败后，程序停止提交新段落、取消尚未开始的任务，并让其他在途段落在当前不可中断调用结束后、进入下一模型阶段前停止。已经成功的学习缓存和句法缓存会保留；修复问题后运行同一命令即可续跑。正式 EPUB 仍只在全部段落完成并通过验证后创建。

当前这里的“批量运行”仍指本地保序并发，不是 OpenRouter Batch API。暂不使用 OpenRouter 的 `:batch` 模型变体：Batch API 是异步提交、轮询结果的后台任务，适合 prompt 和质量已经冻结后的整章/整书运行，不适合单段 preview、即时进度、逐请求有限重试或 fast 完成后立即触发 quality。未来若接入，需要按 fast → quality 分成至少两个批次，以 `custom_id` 对齐章节/段落，持久化 batch ID 和轮询状态，处理部分失败、过期、取消和断点续跑；同一 Google 批次还必须保持一致的 `response_format`/JSON schema。接入前应先做 3–5 段小批次验收，并记录真实 provider、token、缓存和等待时间。

正式运行默认启用控制台进度。`auto` 模式对短章节逐段显示，对整本书按约 1% 或最长 10 秒输出一次 heartbeat；每行区分所选正文、需模型处理、本地跳过、缓存和新生成段落，并显示当前章节/段落坐标、fast/quality/total API token 与耗时，不输出正文、prompt 或凭据。小范围诊断可显示每个 Stanza/fast/quality 阶段，自动化脚本也可关闭输出：

~~~bash
--progress auto --progress-interval 10
--progress verbose
--progress off
~~~

进度阶段包括 `prepare`、`syntax`、`fast`、`quality`、`retry`、`paragraph`、`render`、`epubcheck` 和 `complete`。heartbeat 重复同一快照表示进程仍在当前阶段等待，不代表计数推进或又发起了一次模型请求。`--max-tokens` 除 dry-run 估算门槛外，也会在每个可观测阶段依据供应商累计 usage 停止继续提交工作；并发运行时，已经在途的请求仍可能使最终 usage 略微超过上限。

多次续跑共用同一授权预算时，用 `--prior-tokens N` 计入之前尝试已消耗的实际 token（包括失败响应），运行剩余额度为 `--max-tokens - N`。默认 `N=0`；非零值必须小于已设置的 `--max-tokens`。该参数不改变供应商 usage 或缓存身份，dry-run 仍显示整段所选范围的估算，不扣缓存。调整 `--schema-retries` 只改变失败后的重试策略，不再使已完成的零重试缓存失效；旧的非零重试缓存不自动迁移。

英文默认使用 `conservative` 本地难度门控：综合句数、词数、从属关系、复杂标点、结构信号、古旧或疑似低频词，以及个人画像中的困难样本。只有高置信度简单段落才完全跳过模型。短简单对话和其他短简单单句的默认阈值仍为 16 词和 10 词。这些英文词数规则不会用于日文。

~~~bash
--local-gate conservative
--local-gate balanced
--local-gate off
--short-dialogue-words 20 --short-prose-words 12
--japanese-short-text-gate auto  # 日文 safe-v1，其他语言 off
--japanese-short-text-gate off
--japanese-short-text-chars 30    # 日文无条件字符保底；0 关闭
~~~

日文 gate 的运行身份与模型生成缓存身份分离：切换 `safe-v1`/`off` 时，prompt、schema、
源书和上下文身份相同的既有生成结果仍可零调用复用；冻结 manifest 记录运行 gate，旧 manifest
不会被当作新的门控运行。若要显式回放 S3–S9 的旧 `off-v1` manifest，preview 同时传
`--japanese-short-text-gate off --japanese-short-text-chars 0`。

通用调优仍可显式使用 spaCy：

~~~bash
--syntax-analyzer spacy
--spacy-model en_core_web_sm
~~~

~~~bash
--syntax-analyzer stanza
--stanza-package default_accurate
--stanza-model-dir /path/to/stanza_resources
--stanza-hf-cache-dir /path/to/huggingface
~~~

`glm52-grounded`、`glm52-bailian-grounded`、`qwen37-meaning-only` 和 `deepseek-v4-meaning-only` 会自动启用 Stanza `default_accurate`；显式传入 `--syntax-analyzer off`、`spacy` 或较低精度的 Stanza package 会在任何模型调用前失败。通用 Stanza 模式未指定 package 时也默认 `default_accurate`。Stanza 资源或 Electra-large 缓存缺失时会直接停止，不自动联网、不降级。每个没有句法缓存的段落只解析一次，段内句子使用批处理，schema 重试复用同一份结果；结果按原书、段落文本和精确模型身份独立缓存，可跨云模型与 prompt 调优复用。本地从句锚点仅作为 quality 阶段生成“句意”的内部证据，不会写入学习卡。`translate --dry-run` 只验证配置和估算模型用量，不逐段运行 Stanza。

`inspect` 会逐段显示门控决定原因；`translate --dry-run` 会分别显示正文候选、模型段落、
日文短文本跳过数/坐标、字符阈值与字符保底跳过数/坐标和扣除后的调用估算，并明确跳过无译文兜底。英文使用 `--profile` 时，
已标为费力或阻塞的低分样本会自动收紧英文门控阈值。

正式输出只有在所有段落完成、去除生成节点后的原 XHTML 规范化结构完全一致、内部链接未新增错误且 EPUBCheck 未新增错误后才会出现。验证失败的中间产物带有 INCOMPLETE 标记，不会冒充正式成品。

### EPUB 结构规范化（完全离线）

`normalize` 不调用模型、云服务或 API key；它只对少数前置条件唯一成立的包级错误生成一个新的 EPUB。不会覆盖输入或已有输出，输出必须通过 EPUBCheck、严格读取和内部链接预检后才会发布。

~~~bash
epub-llm-translator normalize source.epub normalized.epub
epub-llm-translator normalize source.epub normalized.epub --dry-run
~~~

当前只支持四条保守规则：删除指向图片的非标准 EPUB 2 thumbnail guide 引用、移除 EPUB 2 guide 中不存在的起始页 fragment、同步 EPUB 2 NCX `dtb:uid` 与 OPF 唯一标识，以及将满足严格条件的 EPUB 3 首个封面项改为线性阅读。任何多候选、正文/XHTML 修复、缺失资源或验证失败都会立即停止，不会猜测修复。

### 6. 删除一本书的缓存

~~~bash
epub-llm-translator cache-clear book.epub
~~~

## 翻译和难句策略

快速模型负责段译与逐句初判，本地规则补充疑似漏选句，高质量模型复核候选并生成理解卡。模型不得把未明确的信息补成具体年龄、亲属关系或代词指向；提示只能依据当前输入中可验证的信息，无法确定时采用最少假设的译法。

书名、作者和显式作品年代只用于判断历史词义和作者语气；模型不得调用记忆中的情节或现成译本补写当前段落。模型只返回句子索引，工具始终按索引从正文取回原句，因此学习卡中的英文原句与正文精确一致；明显遗漏数字、异常膨胀的译文或理解卡、以及缺失的候选句复核都会使整段失败并进入有限重试。

当前学习卡只展示 source-grounded 的“原句”和“句意”。`difficulty` 与本地 Stanza 证据仅用于内部选卡与生成约束，不会作为读者字段输出。当前解析器仅提取这些字段，旧附加字段不会渲染；这不等于已经严格拒绝额外 JSON 键。

段译服务于连续阅读，由 fast 返回的完整句译按索引拼接，不额外添加讲解。quality 的句意不会回写段译，当前候选输入也没有完整段落或 fast 译文。段译与句意的重叠、上下文范围和后续复用方案见 [阅读结构评审](docs/reading-design-review-2026-08-28.md)；建议尚未实现。

生成的 `›››` 是普通单向链接，不使用 `<sup>`、`noteref`、`footnote` 或 `<aside>`，解析也不反向链回原段落。`preview` 和 `translate` 可用 `--analysis-link-text '〔译〕'` 替换可见文字；只接受 1–16 个可见字符，作为纯文本写入链接，不改变模型请求或翻译缓存。生成输出把每章的段译和理解卡按每页最多 16 段放入独立的非线性 XHTML，正文只保留跳转入口，以减轻 KOReader 对正文和解析页的加载与字号重排负担；该规则共用于英文和日文。返回原位置使用阅读器的阅读历史/返回功能。解析区用标题和留白区分内容，不绘制依赖横竖排方向的分割线。这样不会改变原书自身脚注的弹窗能力。

## 安全与兼容性原则

- 永不覆盖输入文件或已有输出文件；
- 不使用宽容 HTML 恢复模式修改正文；
- 不处理原书脚注内部文本；
- 不修改原有 ID、链接、编号和样式；
- 生成内容使用 epubllmt-* 私有命名空间；
- 输出 ZIP 的第一个条目是未压缩的 mimetype；
- 输入 EPUBCheck warning 不阻止运行；
- 输入 error 默认阻止，可显式允许安全可解析的旧书继续；
- 输出不得引入新的 EPUBCheck error 或内部链接问题；
- 项目生成内容不使用 Kindle 弹窗；普通单向链接直接跳转，返回依赖 Kindle 阅读历史。

工具的正式产物只有 EPUB，不调用 Calibre，也不输出 AZW3。兼容性测试可以一次性调用
Calibre 将测试 EPUB 转为 AZW3 并检查解包链接图；侧载到 Kindle Paperwhite 12 后的分页、
导航和阅读观感仍需人工验收。

## 测试

测试在临时目录中动态构造最小 EPUB，不向仓库提交测试书籍：

~~~bash
pytest -q
~~~

当前测试覆盖 EPUB 2/3 普通解析链接、同文件及跨文件原脚注共存、英文
conservative/balanced/off 门控、日文短文本安全门控、ruby/竖排保真、个人画像阈值保护、
零模型跳过、安全解包、正确打包、正文 DOM 保持、模型结果质量门、两个 pipeline、单段预览、
失败恢复和缓存复用。

S10 的精确测试结果和可选集成测试结果记录在
[日文适配进度](docs/japanese-adaptation-progress.md)；本段不维护易过期的累计数字。

运行真实 EPUBCheck 集成：

~~~bash
RUN_EPUBCHECK_TESTS=1 pytest -q -m epubcheck
~~~

本机安装 Calibre 时可以额外验证 EPUB → AZW3 后的链接图：

~~~bash
RUN_CALIBRE_TESTS=1 pytest -q -m calibre
~~~

为 Paperwhite 手动验收生成一个不调用模型的兼容性 EPUB：

~~~bash
python -m scripts.build_compat_fixture source.epub \
  .epub-llm-translator/compat/paperwhite-check.epub
~~~

该文件只包含一条明确标记为“兼容性测试”的学习注释，不是翻译成品，也不会进入 Git。
测试时可一次性通过 Calibre 转为 AZW3 并解包检查；AZW3 不属于项目正式产物。实机验收时
仍需用户将该测试文件侧载到设备。
