"""实体与情绪线索提取。

只做保守的词表匹配，不调用模型；是否启用由召回层根据“翻旧账”意图决定。
"""

from __future__ import annotations

import re
from typing import Iterable

from .temporal_cue import has_recall_intent

_GENERIC_ENTITIES = {
    "主人",
    "我",
    "你",
    "他",
    "她",
    "我们",
    "你们",
    "他们",
    "聊天",
    "对话",
    "记忆",
    "测试",
    "功能",
    "设置",
    "问题",
    "日常",
    "今天",
    "昨天",
    "明天",
}

_QUERY_STOPWORDS = {
    "上次",
    "之前",
    "以前",
    "那次",
    "还记得",
    "记得",
    "还记不记得",
    "什么时候",
    "哪天",
    "什么",
    "怎么",
    "为什么",
    "有没有",
    "是不是",
    "可以",
    "然后",
    "就是",
    "因为",
    "所以",
    "但是",
    "还是",
    "已经",
    "没有",
    "一个",
    "这个",
    "那个",
    "这样",
    "那样",
    "事情",
    "东西",
    "时候",
    "一下",
    "一点",
    "感觉",
    "觉得",
    "当时",
    "现在",
    "今天",
    "昨天",
    "明天",
    "后天",
    "周末",
    "学习",
    "规定",
    "记录",
    "多少",
    "接进",
    "一次",
    "最近",
    "聊过",
    "说过",
    "提过",
}

_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-.]{1,19}")
_CHINESE_TOKEN = re.compile(r"[\u4e00-\u9fa5]{2,8}")
_MIXED_TOKEN = re.compile(r"[A-Za-z0-9]+")
_DATE_LIKE = re.compile(r"^\d{1,2}月|\d{1,2}日|^20\d{2}$")
_TOKEN_TAIL_PARTICLES = "是的了在和与有就都也还要会能把被给让对从到说问吗呢吧啊那次这"
_TOKEN_LEAD_STOPWORDS = (
    "还记得",
    "记得",
    "之前",
    "上次",
    "那次",
    "以前",
    "我",
    "你",
    "他",
    "她",
    "的",
    "和",
    "跟",
)
_INVALID_INNER_CHARS = set("被的了在和与有就都也还把给让对从到说问吗呢吧啊")

# 词表是召回线索，不是给用户贴标签。匹配到只代表“可能相关”。
EMOTION_LEXICON: dict[str, tuple[str, ...]] = {
    "开心": ("开心", "高兴", "快乐", "愉快", "开森"),
    "兴奋": ("兴奋", "激动", "雀跃"),
    "满足": ("满足", "欣慰", "踏实", "释然"),
    "难过": ("难过", "伤心", "低落", "失落", "沮丧", "emo", "哭"),
    "焦虑": ("焦虑", "紧张", "担心", "不安", "慌"),
    "生气": ("生气", "愤怒", "恼火", "火大", "气死"),
    "委屈": ("委屈", "冤枉", "憋屈"),
    "害怕": ("害怕", "恐惧", "好怕", "怕死", "怕黑", "很怕", "有点怕"),
    "孤独": ("孤独", "孤单", "寂寞"),
    "烦": ("烦", "烦躁", "烦死", "郁闷"),
    "累": ("累", "疲惫", "累死", "乏力"),
    "心疼": ("心疼", "心酸"),
}

_NEGATIVE_LABELS = {"难过", "焦虑", "生气", "委屈", "害怕", "孤独", "烦", "累", "心疼"}
_POSITIVE_LABELS = {"开心", "兴奋", "满足"}
_EXCLUDE_CONTEXTS = {
    "累": ("累积", "积累", "累计", "连累", "拖累"),
    "烦": ("麻烦", "烦请", "烦扰"),
    "焦虑": ("不用担心", "别担心", "不用担忧"),
    "心疼": ("跑腿费", "钱", "费用", "花费", "成本", "价格"),
}
_SATISFY_MARKERS = ("很", "挺", "好", "感到", "觉得", "真的", "特别", "非常", "太", "有点")


def emotion_word_present(
    text: str, label: str, word: str, require_first_person: bool = False
) -> bool:
    """判断情绪词是否真的在表达情绪，排除“累积/麻烦/满足要求”等动词用法。

    require_first_person=True 用于历史回填：只认“我/自己”附近的表达，
    避免把“人类害怕你们”“死亡与焦虑这些概念”这类讨论当成用户情绪。
    """
    blocked = _EXCLUDE_CONTEXTS.get(label, ())
    start = 0
    while True:
        index = text.find(word, start)
        if index < 0:
            return False
        window = text[max(0, index - 3) : index + len(word) + 4]
        if any(item in window for item in blocked):
            start = index + len(word)
            continue
        if require_first_person:
            near = text[max(0, index - 14) : index + len(word) + 14]
            if not any(marker in near for marker in ("我", "自己", "咱")):
                start = index + len(word)
                continue
        if label == "满足":
            before = text[max(0, index - 4) : index]
            after = text[index + len(word) : index + len(word) + 2]
            if not any(marker in before for marker in _SATISFY_MARKERS) and "感" not in after:
                start = index + len(word)
                continue
        return True


def extract_emotion_cues(query: str) -> list[str]:
    """返回查询中出现的情绪类别（保守：仅明确情绪词）。"""
    text = str(query or "")
    if not text:
        return []
    cues: list[str] = []
    for label, synonyms in EMOTION_LEXICON.items():
        if any(word in text for word in synonyms):
            cues.append(label)
    return cues


def emotion_sentiments(labels: Iterable[str]) -> list[str]:
    """把情绪类别映射到历史记忆的 sentiment 兜底字段。"""
    values = set(labels)
    sentiments: list[str] = []
    if values & _NEGATIVE_LABELS:
        sentiments.append("negative")
    if values & _POSITIVE_LABELS:
        sentiments.append("positive")
    return sentiments


def _query_tokens(text: str) -> list[str]:
    words: list[str] = []
    try:
        import jieba

        words.extend(str(word).strip() for word in jieba.lcut(text) if word.strip())
    except Exception:
        words.extend(_CHINESE_TOKEN.findall(text))
    words.extend(_ASCII_TOKEN.findall(text))
    # 抓“小米17 / K50至尊 / iPhone15”这类中英数混合实体：
    # 取数字/字母前后各最多 4 个汉字拼成候选，避免 jieba 把 B 站/小米17 拆开。
    for match in _MIXED_TOKEN.finditer(text):
        before = re.sub(r"[^\u4e00-\u9fa5]", "", text[max(0, match.start() - 4) : match.start()])
        after = re.sub(r"[^\u4e00-\u9fa5]", "", text[match.end() : match.end() + 4])
        if before:
            words.append(before[-4:] + match.group())
        if after:
            words.append(match.group() + after[:4])
    return words


def extract_entity_cues(
    query: str, known_entities: Iterable[str], limit: int = 3
) -> list[str]:
    """从查询里找出可能的命名实体/具体事物。

    先用 jieba 切词，再用已知图节点做加权确认；宁可少召回，也不拿
    “上次/那次/时候”这类虚词去扫库。
    """
    text = str(query or "")
    if not text:
        return []

    known = [str(item) for item in known_entities if item]
    scored: dict[str, float] = {}
    for token in _query_tokens(text):
        name = token.strip()
        while len(name) > 2 and name[-1] in _TOKEN_TAIL_PARTICLES:
            name = name[:-1]
        for prefix in _TOKEN_LEAD_STOPWORDS:
            while len(name) > 2 and name.startswith(prefix):
                name = name[len(prefix) :]
        if len(name) < 2 or len(name) > 20:
            continue
        if any(char in _INVALID_INNER_CHARS for char in name):
            continue
        if name in _GENERIC_ENTITIES or name in _QUERY_STOPWORDS:
            continue
        if name.isdigit() or _DATE_LIKE.search(name):
            continue

        score = float(len(name))
        if any(name in entity for entity in known):
            score += 3.0
        if any(char.isascii() and char.isalnum() for char in name):
            score += 1.0
        if name not in scored or score > scored[name]:
            scored[name] = score

    ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)
    names = [name for name, _ in ranked]
    # 有更具体的实体（小米17 / K50至尊）时，去掉它的短子串（小米 / K50）。
    selected = [
        name
        for name in names
        if not any(name != other and name in other for other in names)
    ]
    return selected[: max(1, limit)]



__all__ = [
    "EMOTION_LEXICON",
    "emotion_sentiments",
    "extract_emotion_cues",
    "extract_entity_cues",
    "has_recall_intent",
]
