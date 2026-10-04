"""DB 기반 URL 수집기(Collector) 및 공급기(Provider) 구현체 모듈"""
from __future__ import annotations

from typing import Any, override

from ..crawler.url_crawler import (
    Global_visit_page_url,
    UrlsCollector,
    UrlsProvider,
)
from ..utils.urls import get_clean_url
from .database import Database, ContentModel

__all__ = [
    "DatabaseUrlsProvider",
    "DatabaseUrlsCollector",
    "DatabaseVisitUrls",
]


class DatabaseUrlsProvider(UrlsProvider):
    """DB에서 처리할 URL 목록을 순서대로 읽어오는 Provider"""

    def __init__(self, db: Database, fetch_limit: int = 1000) -> None:
        self.db: Database = db
        self.queue: list[tuple[str, str]] = []
        self.fetch_limit: int = fetch_limit

    def _peek(self) -> None:
        if len(self.queue) <= 0:
            models = self.db.select_not_visited(self.fetch_limit)
            self.queue.extend((m.url, m.title) for m in models)

    @override
    def next(self) -> tuple[str, str]:
        """DB에서 다음 대상 URL (url, title)을 가져옵니다."""
        self._peek()
        if len(self.queue) <= 0:
            raise Exception("No more URLs")
        return self.queue.pop(0)

    @override
    def is_end(self) -> bool:
        """더 이상 처리할 URL이 남아있는지 확인합니다."""
        self._peek()
        return len(self.queue) <= 0


class DatabaseUrlsCollector(UrlsCollector):
    """새로 발견한 URL을 DB에 저장하는 Collector"""

    def __init__(self, db: Database):
        self.db = db

    @override
    def collect(self, url: str, title: str) -> None:
        """발견된 URL과 제목을 DB에 INSERT"""
        clean_url = get_clean_url(url)
        self.db.insert(ContentModel(clean_url, title, None, None))

    @override
    def get_provider(self) -> UrlsProvider:
        """저장된 URL들을 순회할 Provider를 반환"""
        return DatabaseUrlsProvider(self.db)


class DatabaseVisitUrls(Global_visit_page_url):
    """DB 테이블을 기반으로 방문 여부를 체크하고 기록하는 저장소"""

    def __init__(self, db: Database):
        self.db = db

    @override
    def __contains__(self, url: str) -> bool:
        """DB에 이미 방문 기록이 있는지 확인"""
        clean_url = get_clean_url(url)
        found = self.db.select_by_url(clean_url)
        return found is not None

    @override
    def add(self, data: dict[str, Any]) -> None:
        url_val = data.get("url")
        if not isinstance(url_val, str) or not url_val:
            return
        clean_url = get_clean_url(url_val)
        title_val = data.get("title", "")
        title = title_val if isinstance(title_val, str) else ""

        found = self.db.select_by_url(clean_url)
        if found is None:
            self.db.insert(ContentModel(clean_url, title, None, None))

