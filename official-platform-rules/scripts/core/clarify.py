from __future__ import annotations

import re
from typing import Any


PLATFORM_PATTERNS = {
    "tiktok": (r"\btiktok\b", r"抖音商城", r"tik\s*tok"),
    "ozon": (r"\bozon\b", r"奥زون"),
}
TOPICS = {
    "禁限售": ("禁售", "限售", "能不能卖", "prohibited", "restricted"),
    "商品内容与上架": ("上架", "listing", "标题", "主图", "详情页", "商品信息"),
    "履约与物流": ("物流", "发货", "履约", "fbp", "realfbs", "fbo", "fbs", "shipping"),
    "退款退货": ("退款", "退货", "售后", "refund", "return"),
    "费用": ("费用", "佣金", "费率", "commission", "fee"),
    "处罚与申诉": ("处罚", "违规", "封店", "扣分", "申诉", "violation", "appeal"),
    "广告与内容": ("广告", "宣称", "达人", "affiliate", "advertising"),
    "合同": ("合同", "条款", "terms", "contract"),
}


def _first_match(text: str, mapping: dict[str, tuple[str, ...]]) -> str | None:
    lowered = text.lower()
    for key, patterns in mapping.items():
        if any(pattern.lower() in lowered for pattern in patterns):
            return key
    return None


def clarify_question(question: str) -> dict[str, Any]:
    text = question.strip()
    platform_hits = [
        platform
        for platform, patterns in PLATFORM_PATTERNS.items()
        if any(re.search(pattern, text, re.I) for pattern in patterns)
    ]
    platform = platform_hits[0] if len(platform_hits) == 1 else None
    topic = _first_match(text, TOPICS)
    facts: dict[str, str] = {}
    if platform:
        facts["platform"] = platform
    if topic:
        facts["topic"] = topic

    lowered = text.lower()
    if platform == "tiktok":
        if re.search(r"\b(us|usa|united states)\b|美国站|美区", lowered):
            facts["market"] = "US"
        if any(word in lowered for word in ("卖家", "seller", "店铺")):
            facts["identity"] = "seller"
        elif any(word in lowered for word in ("达人", "creator", "affiliate")):
            facts["identity"] = "creator"
    elif platform == "ozon":
        if any(word in lowered for word in ("中国卖家", "跨境卖家", "cross-border", "cross border")):
            facts["seller_type"] = "CN_CROSS_BORDER"
        for mode in ("realfbs", "fbp", "fbo", "fbs"):
            if mode in lowered:
                facts["fulfillment"] = mode.upper()
                break

    questions: list[str] = []
    missing: list[str] = []
    if not platform:
        missing.append("platform")
        questions.append("你问的是 TikTok Shop 还是 Ozon？")
    if platform == "tiktok" and "market" not in facts:
        missing.append("market")
        questions.append("具体是 TikTok Shop 哪个国家或站点？")
    if platform == "tiktok" and "identity" not in facts:
        missing.append("identity")
        questions.append("你的身份是卖家、达人、服务商还是消费者？")
    if platform == "ozon" and "seller_type" not in facts:
        missing.append("seller_type")
        questions.append("你是中国/跨境卖家，还是俄罗斯本土卖家？")
    if not topic:
        missing.append("topic")
        questions.append("具体想确认禁限售、上架、物流、售后、费用、处罚还是合同规则？")
    if topic in {"履约与物流", "退款退货"} and platform == "ozon" and "fulfillment" not in facts:
        missing.append("fulfillment")
        questions.append("订单使用 FBP、realFBS、FBO、FBS 还是其他履约方式？")

    return {
        "status": "needs_clarification" if missing else "ready",
        "confirmed": facts,
        "missing": missing,
        "questions": questions[:3],
        "rule": "仅询问会改变适用规则的缺失信息；补充后必须重新评估。",
    }
