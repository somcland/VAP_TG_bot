from dataclasses import dataclass
from datetime import datetime


@dataclass
class Post:
    platform: str      # "tg" | "ig"
    handle: str        # username источника
    source: str        # человекочитаемое название
    date: datetime     # aware, UTC
    text: str
    url: str
    media: str = ""    # эмодзи-метка типа медиа
