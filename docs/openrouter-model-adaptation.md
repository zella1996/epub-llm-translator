# OpenRouter 模型请求适配边界

## 为什么必须有这一层

OpenRouter 提供统一的 HTTP 入口，但不会保证每个“模型 × 上游 provider”端点接受
相同参数。`response_format`、`reasoning`、采样参数和输出上限都可能因模型或端点而异。
当 `provider.require_parameters=true` 时，如果没有端点同时支持请求中的全部参数，
OpenRouter 会返回 `404 No endpoints found that can handle the requested parameters`。

因此，禁止把某个已验证模型的参数放进 OpenRouter provider 默认配置，并发送给所有
模型。2026-08-26 的 Qwen POC 曾因通用配置注入 `reasoning` 而触发这一风险；
`qwen/qwen3-235b-a22b-07-25` 不使用 thinking/reasoning，请求中不应包含该字段。

OpenRouter 官方参考：

- [Provider routing 与 require_parameters](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Models API 的 supported_parameters](https://openrouter.ai/docs/guides/overview/models)

## 架构职责

```text
TaskPrompt + PromptVariant
        │
        ▼
OpenRouterModelProfile       ProviderProfile
模型允许的最小参数           隐私与路由约束
        │                         │
        └──────────┬──────────────┘
                   ▼
          OpenRouterClient
   HTTP / retry / trace / usage / JSON parse
```

- `OpenRouterClient` 只负责通用运行时行为，并把模型 ID 交给 profile 注册表解析；
  client 内部不得出现按模型名称分支的参数逻辑。
- `ProviderProfile("openrouter")` 只声明服务级策略，例如
  `data_collection: deny` 和 `require_parameters: true`。
- `OpenRouterModelProfile` 是模型参数的唯一来源，明确声明：
  structured output 模式、是否发送 temperature、是否发送 max tokens、reasoning 形状。
- Task Prompt 与模型请求参数相互独立；新增模型不得复制或替换核心阅读卡 Prompt。
- 未登记模型必须在网络请求前 fail-fast，不能使用“看起来兼容”的通用参数猜测。

实现位置：

- `translator/openrouter_model_profiles.py`：模型策略与注册表。
- `translator/openrouter_client.py`：根据模型策略编译 payload。
- `translator/provider_profiles.py`：OpenRouter 隐私与路由策略。

## 接入新模型的最小步骤

1. 从 OpenRouter 模型页或 Models API 取得精确 model ID 和 `supported_parameters`。
2. 在 `OPENROUTER_MODEL_PROFILES` 增加一个 `OpenRouterModelProfile`；只声明已确认支持、
   且当前任务确实需要的参数。
   少量额外参数可放入该 profile 的 `static_parameters`，但不能覆盖 model、messages、
   provider、structured output、temperature、max tokens 或 reasoning 等受控字段。
3. reasoning 不得按厂商或模型家族推断：必需、可选、禁止发送必须分别声明。
4. 如果模型不支持 JSON Schema，只能选择已验证的 `json_object` 或 `prompt_only`；
   仍由本地 JSON 解析和业务 schema 校验把关。
5. 增加 payload 契约测试，精确断言应出现与不应出现的字段。
6. 先做一次无 EPUB、无 Stanza 的单请求 POC，再授权单段 preview。
7. 模型策略 fingerprint 必须进入学习缓存身份；策略变化后不得复用旧模型结果。

通常接入一个 OpenRouter 模型只需要增加一个 profile 和一组 payload 测试，不应修改
通用 client、翻译流程或核心 Prompt。

## 不可静默放宽的边界

- 不得为跑通请求自动移除 `data_collection: deny` 或改成允许收集数据。
- 不得把 `require_parameters` 从 true 静默改为 false；否则 provider 可能忽略结构化输出
  等关键参数。
- 不得在 404 后自动删参数重试生产请求。诊断必须一次只改变一个参数，并由用户明确
  授权真实 API 调用。
- 不得通过自由 `request_extras` 绕过模型 profile。服务级 provider preferences 与
  模型参数必须保持不同命名空间。

## 验收标准

- Gemini profile 发送低档 reasoning 和 JSON Schema。
- GLM profile 发送显式关闭 reasoning 和 JSON Schema。
- Qwen 235B 07-25 profile 发送 JSON Schema，但不发送 reasoning。
- minimal profile 能省略 response format、temperature、max tokens 和 reasoning。
- 未登记模型在 transport 调用前失败。
- profile 内容变化会改变 fingerprint，并使生产缓存身份变化。
- 并发请求的 trace 和 usage 保持线程隔离。
