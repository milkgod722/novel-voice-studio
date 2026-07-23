from __future__ import annotations

import re

EMOTIONS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm")

_CUES = {
    "happy": ("笑", "喜", "开心", "幸福", "兴奋", "甜蜜", "哈哈"),
    "angry": ("怒", "吼", "混蛋", "可恶", "愤怒", "咬牙"),
    "sad": ("哭", "泪", "悲", "痛苦", "对不起", "失去"),
    "afraid": ("怕", "恐惧", "颤抖", "快跑", "救命", "危险"),
    "disgusted": ("恶心", "厌恶", "嫌弃", "肮脏"),
    "melancholic": ("孤独", "寂寞", "惆怅", "回忆", "叹", "遗憾"),
    "surprised": ("竟然", "居然", "没想到", "什么", "天哪", "惊"),
}


def plan_emotion(text: str, strength: float = 0.65) -> list[float]:
    strength = max(0.0, min(1.0, strength))
    scores = {name: 0.0 for name in EMOTIONS}
    scores["calm"] = 0.28
    for emotion, cues in _CUES.items():
        scores[emotion] += sum(text.count(cue) for cue in cues) * 0.32
    scores["surprised"] += min(2, text.count("！") + text.count("!")) * 0.12
    scores["afraid"] += min(2, text.count("？") + text.count("?")) * 0.06
    if re.search(r"[“\"](.+?)[”\"]", text):
        scores["calm"] -= 0.06
    non_calm = max((name for name in EMOTIONS if name != "calm"), key=scores.get)
    if scores[non_calm] > 0:
        scores[non_calm] = min(1.0, 0.32 + scores[non_calm]) * strength
        scores["calm"] = max(0.08, (1.0 - strength) * 0.45)
    total = sum(max(0.0, value) for value in scores.values()) or 1.0
    return [round(max(0.0, scores[name]) / total, 4) for name in EMOTIONS]
