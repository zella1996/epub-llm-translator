from logs.logger import logger
import ollama
import os
import sys


class LLMTranslator:
    """LLM翻译器类，负责与Ollama模型交互进行文本翻译"""
    
    def __init__(self, book_name: str = None):
        """
        初始化LLM翻译器
        
        Args:
            book_name: 书籍名称，可选，会追加到system prompt中
        """
        self.system_prompt = self._load_system_prompt(book_name)
        logger.info("LLM翻译器初始化完成")
    
    def _load_system_prompt(self, book_name: str = None) -> str:
        """从项目根目录的system_prompt.txt文件加载system prompt"""
        try:
            # 获取项目根目录路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            prompt_file = os.path.join(project_root, "system_prompt.txt")
            
            with open(prompt_file, 'r', encoding='utf-8') as f:
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
    
    def ollama_request(self, model: str, prompt: str) -> str:
        """
        向Ollama模型发送请求
        
        Args:
            model: 模型名称
            prompt: 用户输入的文本
            
        Returns:
            模型的响应内容
        """
        logger.debug(f"请求模型 '{model}'，prompt: {prompt}")
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
                model=model,
                messages=messages,
                options=ollama.Options(
                    num_ctx=2048,
                    # repeat_last_n=64,
                    # repeat_penalty=1.15,  # 稍作调整，可以在1.1到1.2之间尝试
                    # temperature=0.4,  # 推荐0.3-0.6之间，这里给一个中间值
                    # seed=42,  # 任意整数，用于可复现性
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
    
    def explain_sentence(self, text: str, model: str = "gemma3:12b") -> str:
        """
        解释单个句子
        
        Args:
            text: 要解释的文本
            model: 使用的模型名称
            
        Returns:
            解释结果
        """
        return self.ollama_request(model, text)
