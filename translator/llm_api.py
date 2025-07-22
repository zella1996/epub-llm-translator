from translator import logger
import ollama


def ollama_request(model: str, prompt: str, **kwargs):
    """
    Send a request to the specified Ollama model and return the response.
    """
    logger.info(f"Requesting model '{model}' with prompt: {prompt}")
    try:
        response = ollama.chat(
            model=model, messages=[{"role": "user", "content": prompt}], **kwargs
        )
        logger.debug(f"Ollama response: {response}")
        result = response.get("message", {}).get("content", "")
        logger.info(f"Model '{model}' result: {result}")
        return result
    except Exception as e:
        logger.error(f"Ollama request failed: {e}", exc_info=True)
        return f"Ollama request failed: {e}"
