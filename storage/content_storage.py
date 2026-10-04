"""DB 기반 본문 콘텐츠 수집기(ContentCollector) 구현체 모듈"""
from __future__ import annotations

from typing import override

from ..pipeline.content_collector import ContentCollector
from ..utils.urls import get_clean_url
from .database import Database, ContentModel

__all__ = [
    "DatabaseContentCollector",
]


class DatabaseContentCollector(ContentCollector):
    """추출된 페이지 본문(HTML/텍스트/해시)을 DB에 저장하는 Collector"""

    def __init__(self, db: Database):
        self.db = db

    @override
    def collect(self, url: str, title: str, content: str, content_hash: str) -> None:
        """수집된 본문 내용을 DB에 INSERT (해시 변경 여부 체크 등)"""
        clean_url = get_clean_url(url)
        found = self.db.select_by_url(clean_url)
        if found is None:
            model = ContentModel(clean_url, title, content, content_hash, is_visited=True)
            self.db.insert(model)
        else:
            found.title = title
            found.content = content
            found.content_hash = content_hash
            found.is_visited = True
            found.update()

    @override
    def get_content(self, url: str) -> dict[str, str] | None:
        """DB에서 특정 URL의 콘텐츠 정보를 SELECT하여 반환"""
        clean_url = get_clean_url(url)
        found = self.db.select_by_url(clean_url)
        if found is None or found.content is None:
            return None
        return {
            "url": found.url,
            "title": found.title,
            "content": found.content,
            "content_hash": found.content_hash or "",
        }
