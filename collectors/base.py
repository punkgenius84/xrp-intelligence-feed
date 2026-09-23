from typing import Protocol
from models import NewsItem

class Collector(Protocol):
    def collect(self) -> list[NewsItem]: ...
