from __future__ import annotations

from models import NewsItem
from .base import LLMError, LLMProvider
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import IntelligenceAnalysis, parse_analysis


def analyze_item(item: NewsItem, provider: LLMProvider) -> IntelligenceAnalysis:
    response = provider.generate(system=SYSTEM_PROMPT, user=build_user_prompt(title=item.title, summary=item.summary, source=item.source))
    try:
        return parse_analysis(response.content, source_url=item.url, model=response.model)
    except ValueError as exc:
        raise LLMError(f"LLM analysis rejected: {exc}") from exc
