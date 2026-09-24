# 模型与 prompt 的纯文本验证规范

当前产品方向见 [阅读辅助重设计方案](reading-comprehension-redesign-plan.md)，运行事实见
[项目说明](../PROJECT.md)。
本文管理实验隔离、外发与审计，不替代新方案的选题和验收；前一轮窄范围规则见
[调优计划](glm53flash-tuning-plan.md)。
实施新契约、单次／双阶段对照及处理部分提示失败时，先读
[技术改造方案](reading-assistance-implementation-plan.md)；旧句意专用规则仅约束对应旧实验。

## 重设计分支的适用调整（2026-08-28）

[新方案](reading-comprehension-redesign-plan.md) 优先于下文早期的固定实验规则：Stanza
可以作为有／无对照变量；单次／双阶段等系统方案比较允许改变 prompt 组织和调用数量，
但须固定任务、记录全部差异，不声称单因素因果。旧版零错误与固定分数不再作为产品
采用的前提，按实际阅读收益和错误影响判断。
纯文本仍是模型调试的优先入口；独立的展示与集成试验可以使用页面或 EPUB，不再把
这种工程隔离理解为永久产品限制。外发范围、预算、原始失败与来源追溯继续明确记录。
这些是开发侧的生成质量与技术测试，不是读者的练习或语言能力测验；产品不生成
自检题、新句练习或复习任务。本轮仅调整方案，没有授权新的付费请求、重试或章节运行。

## 适用范围

用户偏好（2026-08-27）：未特别说明时，今后新编写或修订的模型 prompt 默认使用英文。
这是指令语言约定，不改变中文句意的输出目标；既有中文基线和历史 trace 保持原样，
不为统一语言而批量改写。对照时记录语言变化，避免与规则变化混淆归因。

模型版本、system/user prompt、`thinking`、`reasoning_effort`、采样参数或 JSON
协议的研发验证，只能做**纯文本模型调用**。不得为了这类验证运行 `preview`、
`translate`、EPUB 解析/写入、生产缓存或 EPUBCheck。

Stanza 是句意质量所需的本地证据，必须保留：对每条测试文本运行
`default_accurate`，把句法 hints 随原句加入模型请求。测试工具负责固定句子编号与
候选集合；Stanza 不负责模型难度判断。每组样例解析一次，并在同轮 A/B 及预先约定的
重复运行中复用相同证据和 hash，不因更换 prompt 而重新解析。Stanza 只处理测试文本，
不读取或生成 EPUB，不使用生产缓存。质量模型仍以 `source` 为准，Stanza 仅是辅助证据。

调用前必须取得用户对外发该测试文本和目标 endpoint 的明确授权。测试文本应为用户
授权的最小样本或可公开使用的自拟句子；不要把整章或整本书作为 prompt 回归集。

## 单次测试的输入与记录

每个 case 只发送：固定版本的 system prompt、测试的 user JSON/文本（含本地 Stanza
证据）、以及所需 response JSON schema。保存以下本地审计信息：

- prompt 版本/hash、模型、endpoint 名称、Stanza 版本/包/资源身份和完整非机密请求参数；
- 用户授权的测试文本、原始响应、解析后的 JSON、错误、开始/结束时间；
- provider 返回的 input/output/reasoning token usage 与费用（如有）。

绝不记录 API key、Authorization header 或 properties 文件内容。产物放在 Git 忽略的
`.epub-llm-translator/model-tests/` 下；每次运行新建目录，不能覆盖基线。

## 对照设计

纯句意生成实验可使用独立的“冻结难度”测试协议：保持输入的 initial_difficulty，
不让模型重新分类；预先选定的 effortful/blocking 难例必须返回非空 meaning。
该协议不能改动生产难度筛选，也不能用来证明模型的难度分类能力。两组须同时采用
相同协议，实际 system 及其相对原版的差异单独留档；缺失 meaning 或改判 fluent
计为未覆盖/协议失败，不重试补齐，不把无输出当作无错误。

一次 A/B 只能改变一个变量：prompt、输入封装或 reasoning 档位三者之一。固定模型、
温度、top_p、max output tokens、schema、样例、顺序和调用次数；每个版本对每个 case
在每轮中只调用一次，失败不自动重试。稳定性验证可以预先约定多轮重复，每轮均计入
预算；不得事后只保留表现最好的一轮。必须单独报告 transport/DNS 失败，不能当作质量结果。

测试集至少覆盖：长插入/从句、否定与条件、惯用语、可还原省略、证据不足的省略、
专有名词和亲属称谓。示例 prompt 中使用的句子不得进入验收集。

按句盲评以下四项，每项 0–2：语义准确完整、英文信息推进对应、中文直接可读、边界
（保留应保留原文且不补写）。反转否定/条件、编造关系或后果是单列严重错误，不能被
平均分掩盖。文本评估不自动触发 EPUB preview；只有用户另行要求集成或阅读呈现验证时
才运行 EPUB 流程。

## GLM reasoning 档位

当前 GLM-5.3-Flash 预设使用 `thinking: enabled` 与 `reasoning_effort: max`，这不是
本项目已验证的最佳档位。2026-08-27 重新核对官方文档：GLM-5.3/Flash 不支持关闭思考；
API 和 Coding Plan 的参数映射不同。此前把 GLM-5.2 的 `medium/low → high`、
`none/minimal → 不思考` 规则套到 Flash 的说明不适用，应撤回。
[智谱深度思考文档](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)

2026-08-27 本轮重新核对：深度思考文档的 API 专属条款明确列出 GLM-5.3/Flash
仅支持 `low/high/max`；[Flash 模型页](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)
同时说明文本参数与 GLM-5.3 一致。虽然概述仍有“low 仅 GLM-5.3 支持”的不一致表述，
本轮依据更具体的 API 条款与模型页，将 high 的下一档定为 low，执行 low/max 各一次。
不发送 `medium`、`minimal` 或 `none`，不关闭思考；若接口拒绝，保存失败，不换档重试。
API 接受参数不等于证明供应商内部执行机制或稳定质量，需分开报告。

该轮已完成：low/max 均成功，返回的 reasoning tokens 分别为 10/2789（各五句一批）。
这只验证了此次 API 接受请求并返回结果；不假定供应商必然按某个固定内部 token 预算执行。

以上为 2026-08-27 的参数核对与实验记录，本轮文档评审没有重新验证供应商参数。
v6 已有整章基线使用 `max`；下一轮先固定它来隔离结构／prompt 变化，不自动降为 high。
若另开 reasoning 对照，先重新核对当时可用参数，再在同一冻结 prompt 和样例上比较。
不能以 reasoning token 更多或卡片数量更多作为质量更好的证据。

GLM-5.3-Flash 本轮的步骤、预算和可判定门槛见
[`glm53flash-tuning-plan.md`](glm53flash-tuning-plan.md)。
