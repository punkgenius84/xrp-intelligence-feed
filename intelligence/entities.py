import re
import unicodedata
from pathlib import Path
from intelligence.configuration import read_object, validate_word_groups


def _matches(text: str, alias: str) -> bool:
    alias = unicodedata.normalize("NFKC", alias).strip()
    if not alias:
        return False
    escaped = re.escape(alias)
    # Short uppercase ticker symbols are case-sensitive. In particular, ADA
    # must not match a person's ordinary name "Ada" in unrelated reporting.
    flags = 0 if len(alias) <= 5 and alias.isupper() and alias.isalpha() else re.IGNORECASE
    # Word boundaries stop XRP/Ripple/SEC from matching inside unrelated words.
    return re.search(r"(?<!\w)" + escaped + r"(?!\w)", text, flags=flags) is not None


def detect_entities(item, config_path: str | Path = "config/entities.json") -> list[str]:
    config = validate_word_groups(read_object(config_path, "entities"),
                                  ("assets", "companies", "regulators", "government", "finance"), "entities")
    text = unicodedata.normalize("NFKC", f"{item.title} {item.summary}")
    matches: list[str] = []
    for group in config.values():
        if not isinstance(group, dict):
            continue
        for canonical, aliases in group.items():
            if isinstance(aliases, list) and any(_matches(text, str(alias)) for alias in aliases):
                if canonical not in matches:
                    matches.append(canonical)
    item.detected_entities = matches
    return matches
