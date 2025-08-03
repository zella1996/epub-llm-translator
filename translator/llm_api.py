from logs.logger import logger
import ollama
import os
import sys
from enum import Enum


class ModelType(Enum):
    """模型类型枚举"""

    QWEN3_14B = "qwen3:14b"
    MISTRAL_NEMO_12B = "mistral-nemo:12b"
    GEMMA3_12B = "gemma3:12b"
    GEMMA3_27B_IT_QAT = "gemma3:27b-it-qat"
    QWEN2_5_7B = "qwen2.5:7b"
    DEEPSEEK_R1_14B = "deepseek-r1:14b"
    MYGLM4_LATEST = "myglm4:latest"
    GLM4_9B = "glm4:9b"
    LLAMA_TRANSLATE_8B = "7shi/llama-translate:8b-q4_K_M"

    def __str__(self):
        return self.value

    @classmethod
    def get_description(cls, model_enum):
        """获取模型描述"""
        descriptions = {
            cls.QWEN3_14B: "Qwen3 14B 模型",
            cls.MISTRAL_NEMO_12B: "Mistral Nemo 12B 模型",
            cls.GEMMA3_12B: "Gemma3 12B 模型",
            cls.GEMMA3_27B_IT_QAT: "Gemma3 27B IT QAT 模型",
            cls.QWEN2_5_7B: "Qwen2.5 7B 模型",
            cls.DEEPSEEK_R1_14B: "DeepSeek R1 14B 模型",
            cls.MYGLM4_LATEST: "MyGLM4 最新版本模型",
            cls.GLM4_9B: "GLM4 9B 模型",
            cls.LLAMA_TRANSLATE_8B: "7shi/llama-translate 8B Q4_K_M 翻译模型",
        }
        return descriptions.get(model_enum, "未知模型")


class LLMTranslator:
    """LLM翻译器类，负责与Ollama模型交互进行文本翻译"""

    @classmethod
    def get_available_model_enums(cls) -> list:
        """
        获取所有可用的模型枚举列表

        Returns:
            模型枚举列表
        """
        return list(ModelType)

    def __init__(self, book_name: str = None, model: ModelType = ModelType.GLM4_9B):
        """
        初始化LLM翻译器

        Args:
            book_name: 书籍名称，可选，会追加到system prompt中
            model: 使用的模型枚举，默认使用 GLM4_9B
        """
        self.system_prompt = self._load_system_prompt(book_name)
        self.model = model.value
        logger.info(
            f"LLM翻译器初始化完成，使用模型: {model.value} ({ModelType.get_description(model)})"
        )

    def _load_system_prompt(self, book_name: str = None) -> str:
        """从项目根目录的system_prompt.txt文件加载system prompt"""
        try:
            # 获取项目根目录路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            prompt_file = os.path.join(project_root, "system_prompt.txt")

            with open(prompt_file, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()
                # 如果提供了book_name，直接追加到加载的内容中
                if book_name:
                    system_prompt += f"\n\n这句话来自：“{book_name}”"
                logger.info(f"加载的system prompt: {system_prompt}")
                return system_prompt
        except Exception as e:
            logger.error(f"加载system prompt失败: {e}")
            logger.error("程序无法继续运行，正在退出...")
            sys.exit(1)

    def ollama_request(self, prompt: str) -> str:
        """
        向Ollama模型发送请求

        Args:
            prompt: 用户输入的文本

        Returns:
            模型的响应内容
        """
        logger.debug(f"请求模型 '{self.model}'，prompt: {prompt}")
        try:
            messages = [
                {
                    "role": "system",
                    "content": self.system_prompt,
                },
                {"role": "user", "content": prompt},
            ]
            logger.debug(f"Ollama请求prompt: {prompt}")
            response = ollama.chat(
                model=self.model,
                messages=messages,
                options=ollama.Options(
                    # GPU和并行处理参数 - 平衡性能和利用率
                    num_gpu_layers=35,  # 适中的GPU层数，保持利用率
                    num_thread=12,  # 适中的线程数，避免CPU瓶颈
                    num_parallel=2,  # 适中的并行处理
                    num_batch=1536,  # 适中的批处理大小
                    
                    # 上下文窗口大小 - 翻译任务需要足够上下文
                    num_ctx=12288,  # 适中的上下文窗口
                    
                    # 重复惩罚 - 防止翻译重复内容
                    repeat_penalty=1.05,
                    repeat_last_n=64,
                    
                    # 温度控制 - 提高温度以增加翻译的灵活性
                    temperature=0.4,
                    
                    # 随机种子 - 确保翻译结果的一致性
                    seed=42,
                    
                    # 最大预测token数 - 启用以充分利用GPU
                    num_predict=1536,  # 适中的预测token数
                    
                    # 采样参数 - 平衡准确性和多样性
                    top_k=60,  # 适中的候选词数量
                    top_p=0.85,  # 适中的top_p值
                    
                    # 频率惩罚 - 减少重复词汇
                    frequency_penalty=0.05,
                    presence_penalty=0.05,
                    
                    # 微调参数 - 针对翻译任务的优化
                    mirostat=2,
                    mirostat_tau=3.0,
                    mirostat_eta=0.1,
                    
                    # 内存优化参数
                    num_gqa=8,  # 优化注意力机制
                    rope_freq_base=10000,  # 优化位置编码
                    rope_freq_scale=1.0,
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

    def explain_sentence(self, text: str) -> str:
        """
        解释单个句子

        Args:
            text: 要解释的文本

        Returns:
            解释结果
        """
        return self.ollama_request(text)
