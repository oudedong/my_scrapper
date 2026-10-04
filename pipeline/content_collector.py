from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from ..core.session import Page
from ..crawler.url_crawler import UrlsProvider

__all__ = [
    "ContentCollector",
    "SimpleContentCollector",
    "PageContentCollector",
]


class ContentCollector(ABC):

    @abstractmethod
    def collect(self, url: str, title: str, content: str, content_hash: str) -> None:
        """content 수집"""
        pass

    @abstractmethod
    def get_content(self, url:str) -> dict[str, str]|None:
        """찾은 content를 반환"""
        pass


class SimpleContentCollector(ContentCollector):

    def __init__(self) -> None:
        self.contents: dict[str, dict[str, str]] = {}  # key:url, value:{title, content, content_hash}

    def collect(self, url: str, title: str, content: str, content_hash: str) -> None:
        """content 수집"""
        self.contents[url] = {"url": url, "title": title, "content": content, "content_hash": content_hash}

    def get_content(self, url:str) -> dict[str, str]|None:
        """찾은 content를 반환"""
        return self.contents[url]


class PageContentCollector:
    """html본문을 추출해 저장합니다"""

    def __init__(self, page: Page, url_provider: UrlsProvider, content_collector: ContentCollector) -> None:
        self._page: Page = page
        self._url_provider: UrlsProvider = url_provider
        self._content_collector: ContentCollector = content_collector

    async def _fetch_one(self, url: str) -> dict[str, str]:
        """url하나를 방문해서 추출함"""
        await self._page.goto(url)
        content = None
        content_hash = None
        try:
            content = await self._page.get_raw_content()
            content_hash = hashlib.sha256(content.encode()).hexdigest()
        except Exception as e:
            content = f"fail to fetch, e:{e}"
            content_hash = ""
        return {"content": content, "content_hash": content_hash}

    async def fetch_next(self) -> bool:
        """urlProvider에서 url하나를 받아와 추출함"""
        if self._url_provider.is_end():
            return False
        url_tuple = self._url_provider.next()
        content = await self._fetch_one(url_tuple[0])
        self._content_collector.collect(url_tuple[0], url_tuple[1], content["content"], content["content_hash"])
        return True

    async def fetch_all(self) -> None:
        """전부추출후 저장"""
        while await self.fetch_next():
            pass
