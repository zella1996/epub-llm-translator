from logs.logger import logger
import ollama


def ollama_request(model: str, prompt: str):
    logger.debug(f"请求模型 '{model}'，prompt: {prompt}")
    try:
        messages = [
            {
                "role": "system",
                "content": """
                
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
                repeat_last_n=64,
                repeat_penalty=1.15,  # 稍作调整，可以在1.1到1.2之间尝试
                temperature=0.4,  # 推荐0.3-0.6之间，这里给一个中间值
                seed=42,  # 任意整数，用于可复现性
                # stop=["\n\n", "###", "无"],  # 停止序列，根据实际输出调整
                num_predict=2048,  # 预期最大Token数量
                top_k=50,  # 推荐40-60之间
                top_p=0.8,  # 推荐0.7-0.9之间
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


def translate(text: str, model="glm4:9b"):
    return ollama_request(model, text)
