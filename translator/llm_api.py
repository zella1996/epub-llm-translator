from logs.logger import logger
import ollama


def ollama_request(model: str, prompt: str):
    logger.debug(f"请求模型 '{model}'，prompt: {prompt}")
    try:
        messages = [
            {
                "role": "system",
                "content": """
你是一个专业的英文文本分析助手。你的任务是精确地解析用户提供的英文句子。对于每个句子，你的输出必须严格遵循以下纯文本一行格式：

**首先，识别句子的主要结构和关键短语。然后，简洁地解释这些结构和短语的含义。最后，提供句子的整体翻译。**

请确保你的分析集中于理解句子的核心意义和复杂语法点，但避免任何Markdown格式、多行文本、或对每个单词的逐一语法标签。所有的信息都必须压缩到**一行文本**中。

示例输入：
"But, whatever might really be its limits, it was enough, when perceived by his sister, to make her uneasy, and at the same time (which was still more common) to make her uncivil."

期望的纯文本一行输出：
分析："whatever might really be its limits"（无论其界限），"it was enough"（这已足够），"when perceived by his sister"（当被他姐姐察觉时），"to make her uneasy"（让她不安），"to make her uncivil"（让她不礼貌）。整体翻译：但是，无论这种偏爱的程度到底如何，当他的姐姐察觉到时，这种偏爱就足以让她感到不安，同时（更常见的是）让她变得不礼貌。

---
请确保你的分析和翻译准确、精炼，并严格遵循单行格式。
                """,
            },
            {"role": "user", "content": prompt},
        ]
        logger.debug(f"Ollama请求prompt: {prompt}")
        response = ollama.chat(
            model=model,
            messages=messages,
            options=ollama.Options(
                num_ctx=2048,
                # repeat_last_n=64,
                # repeat_penalty=1.15,  # 稍作调整，可以在1.1到1.2之间尝试
                # temperature=0.4,  # 推荐0.3-0.6之间，这里给一个中间值
                seed=42,  # 任意整数，用于可复现性
                # stop=["\n\n", "###", "无"],  # 停止序列，根据实际输出调整
                # num_predict=2048,  # 预期最大Token数量
                # top_k=50,  # 推荐40-60之间
                # top_p=0.8,  # 推荐0.7-0.9之间
            ),
        )
        logger.debug(f"Ollama响应: {response}")
        # Ollama的chat返回对象可能是ChatResponse对象，需根据实际结构获取内容
        # 假设response有message属性，且message为dict
        result = ""
        if hasattr(response, "message"):
            result = getattr(response, "message", {}).get("content", "")
        else:
            logger.warning(f"Ollama响应格式错误: {response}")

        return result

    except Exception as e:
        logger.error(f"Ollama请求失败: {e}", exc_info=True)
        return f"Ollama请求失败: {e}"


def explain_sentence(text: str, model="glm4:9b"):
    return ollama_request(model, text)
