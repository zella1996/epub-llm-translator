# 兼容性验证记录

## 2026-08-25：北京百炼 GLM-5.2 实际 API

项目新增 `bailian-v1` 供应商预设和 `glm52-bailian-grounded-v1` 调优预设，保留原有智谱直连配置。百炼预设读取 `DASHSCOPE_API_KEY`，发送 `enable_thinking:false`、`response_format:{"type":"json_object"}` 和 `max_tokens:2048`，不发送智谱直连的 `thinking`、`do_sample` 或 `reasoning_effort`。

使用北京按量付费共享 OpenAI-compatible endpoint 与模型 ID `glm-5.2` 完成以下实际验收：

- 多次最小 JSON Object 协议探针连续首轮成功；
- 一段公版小说的单段 PREVIEW 在 `max-retries=0`、`schema-retries=0` 下首轮完成 fast 与 quality 两阶段；
- fast 与 quality 均返回可核对的 usage；精确用量保存在本机私有记录；
- 生成 4 张理解卡，与冻结的智谱直连同段结果候选完全一致，释义只有轻微措辞差异，没有新增人物关系、亲属、指代或心理动机断言；
- PREVIEW EPUB 通过独立 `unzip -t` 与 EPUBCheck，零 fatal、零 error、零 warning；
- trace 恰有 2 个请求文件，无 schema retry；全部 10 个 trace 文件均未出现 API key。

验收产物与精确段落定位保存在本机私有目录，不进入 Git。

本次验收只覆盖关闭思考的非流式 Chat Completion、JSON Object、北京按量付费 endpoint 与单段 EPUB 闭环；不扩展为思考模式、`thinking_budget`、JSON Schema、流式、跨地域、吞吐 SLA 或整书质量认证。

## 2026-08-25：离线结构规范化

`normalize` 不加载模型、不读取 API key，也不请求网络；只使用本机 EPUBCheck 和 Calibre。

以下真实 EPUB 均由一条前置条件唯一的规则修复，并完成 EPUBCheck 与 Calibre → AZW3 验收：

- 样本 A：删除指向图片的非标准 EPUB 2 thumbnail guide 引用；
- 样本 B：移除指向存在起始页、但不存在锚点的 EPUB 2 guide fragment；
- 样本 C：将 NCX `dtb:uid` 同步为 OPF 唯一标识；
- 样本 D：将首个、仅含封面图片的 EPUB 3 非线性 cover spine 项改为线性。

四本输入各有 1 个 EPUBCheck error；规范化输出均为零 error、零 warning。每本只改动预期的 `package/content.opf` 或 `toc.ncx`，其余容器资源字节不变；Calibre 转 AZW3 均成功，解包后内部 fragment 链接均唯一可解析。对输出再次执行 `normalize --dry-run` 均没有匹配规则。

## 2026-08-21：EPUBCheck 5.3.0

合成 EPUB 2 与 EPUB 3 均完成以下验证：

- 输入 EPUB 零错误；
- 同章末追加学习注释后输出 EPUB 零错误；
- EPUB 2/3 项目生成内容均使用普通单向解析链接并通过；原书语义/普通脚注保持有效；
- 对带有既存 EPUBCheck error 的安全可解析输入，输出保留相同 finding 且不新增 finding。

可重复命令：

~~~bash
RUN_EPUBCHECK_TESTS=1 pytest -q -m epubcheck
~~~

## 2026-08-21：Calibre 9.13.0

环境：

- macOS；
- ebook-convert 9.13.0；
- calibre-debug 9.13；
- 运行时动态生成的 EPUB 3 合成书；
- 同一章节中同时包含原作者脚注和项目生成的普通学习解析链接。

验证流程：

1. 严格解析合成 EPUB；
2. 为正文段落追加偏直译和困难句理解卡；
3. 生成不含脚注语义的同章末普通单向解析链接；
4. 正确重新打包 EPUB；
5. 使用 ebook-convert 转换为 AZW3；
6. 使用 calibre-debug --explode-book 解包 AZW3；
7. 遍历转换后的 HTML，验证所有内部 fragment 目标唯一存在。

结果：

- EPUB → AZW3 转换成功；
- 中文直译内容保留；
- 原作者脚注内容保留；
- 生成解析的正文单向链接保留，且没有指回原段落的反向链接；
- 原作者脚注的往返链接保留；
- 普通跨文件原作者脚注不会被选作正文，转换后的往返链接仍唯一有效；
- 所有检查到的内部 fragment 均解析到唯一目标。

可重复命令：

~~~bash
RUN_CALIBRE_TESTS=1 pytest -q -m calibre
~~~

## 2026-08-21：真实公共领域 EPUB

输入为 Project Gutenberg 官方发布的同一公版作品的 EPUB 3 和“older e-readers” EPUB 2，下载文件仅位于临时目录；作品与文件定位保存在本机私有记录。

验证结果：

- EPUB 2/3 原书均通过 EPUBCheck 5.3.0，零错误、零警告；
- 正文识别跳过 Project Gutenberg header 元数据，首个候选段落为 Chapter I 正文；
- 只修改加入注释的目标 XHTML，其他 61 个容器资源哈希保持不变；
- 去除项目生成节点后的原正文规范化 DOM 在修改前后完全相同；
- EPUB 2/3 生成书均通过 EPUBCheck，零错误、零警告；
- 两种格式均由 Calibre 9.13.0 转 AZW3 成功；
- 解包 AZW3 后，中文直译、理解卡和内部单向 fragment 链接完整。

可重复命令：

~~~bash
REAL_EPUB_PATH=/path/to/public-domain.epub pytest -q -m real_epub
~~~

## 2026-08-21：OpenAI-compatible HTTP

localhost 协议测试覆盖：

- /v1/chat/completions 请求路径；
- Bearer API key；
- JSON response format；
- 快速模型与高质量模型的连续调用；
- HTTP 500 有限重试；
- HTTP 400 不重试；
- CLI → HTTP → 双模型解析 → PREVIEW EPUB 完整闭环。

这证明客户端协议实现和内部闭环，但不等同于特定云供应商兼容认证。

## 2026-08-21：北京百炼 Qwen 实际 API

用公共领域真实 EPUB 执行单段 PREVIEW，验证 `qwen3.7-flash-2026-07-15` 与 `qwen3.7-plus-2026-05-26` 的北京专属 OpenAI-compatible endpoint。实际鉴权、两阶段 JSON 输出、困难句复核、PREVIEW EPUB 生成及 EPUBCheck 均通过。

本次延迟样本（小样本，不代表 SLA）：

- 默认思考：单短句 Flash 23.70 秒、Plus 16.11 秒；同一超长段落 Flash→Plus 143.49 秒、Plus→Plus 140.52 秒；
- `enable_thinking:false`：单短句 Flash 6.56 秒、Plus 7.20 秒；中等句 Flash→Plus 完整两阶段 14.63 秒。

Qwen3.7 Flash/Plus 默认开启思考；项目提供不含密钥和 endpoint 的 `qwen-bailian-v1` 预设，默认发送 `enable_thinking:false`。质量校准后增加书名/作者/显式作品年代上下文、专名原文保留、中性长幼/指代策略，并要求历史亲属词或法律词不能确定时保留原词、简短提示歧义。

## 2026-08-21：完整自动化组合

同时启用合成 EPUB 2/3、EPUBCheck、Calibre、localhost OpenAI-compatible HTTP 和真实 Gutenberg EPUB 的最新结果为 `75 passed`，包含普通 `〔析〕` 直达链接、本地模型门控、个人画像阈值保护、版本化供应商预设、书籍上下文与专名缓存策略。

~~~bash
RUN_EPUBCHECK_TESTS=1 RUN_CALIBRE_TESTS=1 RUN_HTTP_TESTS=1 \
REAL_EPUB_PATH=/path/to/public-domain.epub pytest -q
~~~

## Paperwhite 手动验收产物

第一轮实机反馈：

- EPUB 3 语义脚注经 Calibre 转 AZW3 后显示为约占屏幕三分之一的弹窗；
- 弹窗不保留多段格式，会把理解卡内容压成一段；
- 弹窗提供“前往脚注”按钮，可以进入章末脚注页面；
- 第一轮后曾尝试把脚注元素缩短、将理解卡移到相邻普通 XHTML 详细区。

第二轮实机反馈：Kindle 仍把相邻详细内容全部吸入弹窗，且“前往脚注”的跳转逻辑不可靠。因此项目生成内容已彻底取消脚注语义、`aside`、`noteref` 和上标结构，改为普通基线 `〔析〕` 链接直达完整解析。原书脚注不受影响。

第三轮实机反馈：即使完全去掉脚注标签，Kindle 仍将正文↔解析的普通双向链接识别为弹窗，且第一段弹窗会吞入紧邻的第二段解析。Amazon 官方链接规范明确说明，Paperwhite 可将双向内部链接显示为脚注弹窗，非脚注内部链接应避免 A↔B。因此第四轮只移除解析→原段落的反向链接，返回使用 Kindle 阅读历史。

第四轮实机结果：`paperwhite-one-way-v4.azw3` 不再出现弹窗，两处 `〔析〕` 均能正确跳转到各自的完整解析，Kindle 自带返回功能能正常回到原阅读位置。因此已确认 A↔B 双向链接是前一版误触发脚注弹窗的原因，单向链接定为正式方案。

当前工作区已生成：

- .epub-llm-translator/compat/paperwhite-check.epub
- .epub-llm-translator/compat/paperwhite-check.azw3
- .epub-llm-translator/compat/paperwhite-popup-v2.epub
- .epub-llm-translator/compat/paperwhite-popup-v2.azw3
- .epub-llm-translator/compat/paperwhite-direct-v3.epub
- .epub-llm-translator/compat/paperwhite-direct-v3.azw3
- .epub-llm-translator/compat/paperwhite-one-way-v4.epub
- .epub-llm-translator/compat/paperwhite-one-way-v4.azw3

文件来自公共领域真实书，只包含短、长两条明确标记的兼容性测试注释，并被 Git 忽略。

第四轮使用 `paperwhite-one-way-v4.azw3` 已完成以下检查：

1. 使用 Calibre 将 paperwhite-one-way-v4.azw3 发送到设备；
2. 打开 Chapter I，确认前两个正文段落的英文内容和排版正常；
3. 点击第一段后的 `〔析〕`，确认不出现脚注弹窗，而是直接进入“单向链接测试”完整解析；
4. 使用 Kindle 的阅读历史/返回功能，确认回到第一段准确位置；
5. 点击第二段后的 `〔析〕`，确认直接看到直译以及分段的“直接跳转测试”；
6. 确认原句、句意、顺读提示、语言感觉和歧义仍分段显示且分页正常；
7. 使用 Kindle 的阅读历史/返回功能，确认回到第二段准确位置；
8. 关闭并重新打开书籍后重复一次，并记录设备型号、固件版本和 Calibre 版本。

## 2026-09-21：日文 EPUB 与 Kindle/KOReader

- 《人間失格》转换样本的 `reading-v1` chapter 1 完成 62 段真实模型运行、恢复和零调用重建；
  15 段人工抽查为严重 0、妨碍理解 0。该样本不证明原生 EPUB 或设备兼容。
- W3C/IDPF 原生 EPUB 3《草枕》的 chapter 5 完成 163 段真实模型运行、零调用重建和结构差异
  验收；ruby、竖排、RTL、导航、CSS、字体、非 ASCII 路径及 Media Overlay 无退化，
  EPUBCheck 0 errors / 0 warnings。
- 用户在 Kindle 的 KOReader 中确认竖排、解析入口、跳转/返回、圆点/冒号排版和解析分片性能
  可接受。型号、固件、KOReader 版本和导入方式未记录；不外推为原生 Kindle 或 Send to Kindle。
- 住野よる商业边缘样本只做离线占位写回，归一化 EPUBCheck findings 和链接问题均无新增；
  商业正文没有发送给模型。

详细矩阵与限制见 [日文支持与已知限制](docs/japanese-support.md)，阶段证据见
[日文适配进度](docs/japanese-adaptation-progress.md)。

## 尚未验证

- 已验证若干指定 OpenAI-compatible 供应商/profile；未列出的供应商、模型和参数组合仍需单独验收；
- 更复杂、更长的真实模型解析在 Kindle 上的跨页与分段表现尚需随实际使用继续观察；
- 尚未覆盖更多复杂出版社 CSS 和更多阅读器；普通跨文件原脚注已有合成 EPUB、EPUBCheck 与 Calibre 链接图覆盖；日文实机范围仅为上述 Kindle/KOReader。

在这些验证完成前，不能把完整项目目标标记为已完成。
