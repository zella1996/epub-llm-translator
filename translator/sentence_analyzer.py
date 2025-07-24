import spacy
from wordfreq import zipf_frequency
import textstat

# 加载spacy模型，只需一次
nlp = spacy.load("en_core_web_sm")


def sentence_complexity(sentence: str) -> dict:
    """
    计算句子的结构复杂度，包括长度、依存树深度和从句数量。
    """
    doc = nlp(sentence)
    length = len(doc)

    def tree_depth(token):
        """递归计算依存树深度"""
        if not list(token.children):
            return 1
        return 1 + max(tree_depth(child) for child in token.children)

    # 取所有句子的根节点，计算最大深度
    depth = max(tree_depth(sent.root) for sent in doc.sents)
    # 统计从句标记和关系从句的数量
    sub_clauses = sum(1 for token in doc if token.dep_ in ("mark", "relcl"))

    return {"length": length, "depth": depth, "sub_clauses": sub_clauses}


def hard_word_ratio(sentence: str, threshold: float = 4.0) -> float:
    """
    计算生僻词比例。Zipf频率低于threshold的单词视为生僻词。
    """
    doc = nlp(sentence)
    tokens = [token for token in doc if token.is_alpha]
    if not tokens:
        return 0.0
    hard_words = [
        token for token in tokens if zipf_frequency(token.text, "en") < threshold
    ]
    return len(hard_words) / len(tokens)


def readability_score(sentence: str) -> float:
    """
    计算句子的Flesch易读性分数，分数越高越容易。
    """
    return textstat.flesch_reading_ease(sentence)


def normalize(value, min_val, max_val):
    if max_val == min_val:
        return 0
    return (value - min_val) / (max_val - min_val)


def overall_difficulty(sentence: str) -> float:
    complexity = sentence_complexity(sentence)
    hard_ratio = hard_word_ratio(sentence)
    readability = readability_score(sentence)

    # print(
    #     f"归一化前特征值：\n"
    #     f"length: {complexity['length']}\n"
    #     f"depth: {complexity['depth']}\n"
    #     f"sub_clauses: {complexity['sub_clauses']}\n"
    #     f"hard_ratio: {hard_ratio:.2f}\n"
    #     f"readability: {readability:.2f}\n"
    # )
    # 根据实际语料调整最大最小值
    length_norm = normalize(complexity["length"], 5, 80)  
    depth_norm = normalize(complexity["depth"], 1, 16)
    sub_clause_norm = normalize(complexity["sub_clauses"], 0, 5)
    hard_ratio_norm = normalize(hard_ratio, 0, 1)
    readability_norm = normalize(readability, 0, 100)

    # print(
    #     f"归一化后特征值：\n"
    #     f"句长归一化: {length_norm:.2f}（权重0.2）\n"
    #     f"句法深度归一化: {depth_norm:.2f}（权重0.3）\n"
    #     f"从句数归一化: {sub_clause_norm:.2f}（权重0.4）\n"
    #     f"生僻词比例归一化: {hard_ratio_norm:.2f}（权重0.1）\n"
    #     f"易读性归一化: {readability_norm:.2f}（权重-0.1）\n"
    # )
    score = (
        length_norm * 0.2
        + depth_norm * 0.3
        + sub_clause_norm * 0.4
        + hard_ratio_norm * 0.1
        - readability_norm * 0.1
    )
    return score * 100
