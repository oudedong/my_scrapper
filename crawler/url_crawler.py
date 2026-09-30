from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.session import Page
from ..utils.urls import get_clean_url
from .click_explorer import DynamicClickExplorer

__all__ = [
    "UrlsProvider",
    "SimpleUrlsProvider",
    "UrlsCollector",
    "SimpleUrlsCollector",
    "Global_visit_page_url",
    "Global_visit_set_page_url",
    "DynamicURLExplorer",
]


class UrlsProvider(ABC):
    """url들을 반환하는 객체"""

    @abstractmethod
    def next(self) -> tuple[str, str]:
        pass

    @abstractmethod
    def is_end(self) -> bool:
        pass


class SimpleUrlsProvider(UrlsProvider):

    def __init__(self, urls: list[tuple[str, str]]) -> None:
        self.urls = urls

    def next(self) -> tuple[str, str]:
        return self.urls.pop(0)

    def is_end(self) -> bool:
        return len(self.urls) == 0


class UrlsCollector(ABC):

    @abstractmethod
    def collect(self, url: str, title: str) -> None:
        """데이터 수집"""
        pass

    @abstractmethod
    def get_provider(self) -> UrlsProvider:
        """수집된 url들을 반환하는 객체를 반환"""
        pass


class SimpleUrlsCollector(UrlsCollector):

    def __init__(self) -> None:
        self.urls: list[tuple[str, str]] = []

    def collect(self, url: str, title: str) -> None:
        """데이터 수집"""
        self.urls.append((url, title))

    def get_provider(self) -> UrlsProvider:
        """수집된 url들을 반환하는 객체를 반환"""
        return SimpleUrlsProvider(self.urls)


class DynamicURLExplorer:

    def __init__(
        self,
        page: Page,
        url_visited: Global_visit_page_url,
        max_depth: int = 1,
        collector: UrlsCollector | None = None,  # 새로운 url방문시 페이지정보를 넘겨받을객체
    ):
        self.page = page
        self._dynamic_click_explorer: DynamicClickExplorer = DynamicClickExplorer(page)
        self._url_visited: Global_visit_page_url = url_visited  # 현재 방문한 url들
        self._max_depth = max_depth
        self.collector = collector or SimpleUrlsCollector()

    def _is_max_depth(self) -> bool:
        print(f"DynamicURLExplorer:curdepth:{self._dynamic_click_explorer.get_depth()}, maxdepth:{self._max_depth}")
        return self._dynamic_click_explorer.get_depth() >= self._max_depth

    async def do_recursivly(self) -> None:
        page_info = await self.page.get_page_info()
        url_init = get_clean_url(page_info.url)
        self._url_visited.add({"url": url_init})

        while True:
            ret_next = await self._dynamic_click_explorer.next()
            if ret_next == 0:  # 더 이상 갈곳이 없으면 종료
                return
            page_info = await self.page.get_page_info()
            url_current = get_clean_url(page_info.url)
            if ret_next == 2:
                continue
            if url_current in self._url_visited:
                await self._dynamic_click_explorer.abort()
                continue
            self.collector.collect(url_current, page_info.title)
            self._url_visited.add({"url": url_current})
            while self._is_max_depth():
                if not await self._dynamic_click_explorer.abort():
                    break


################# 아래는 아직 개선전..
class Global_visit_page_url(ABC):
    """방문집합 형식"""

    @abstractmethod
    def __contains__(self, url: str) -> bool:
        """집합에 대해 in연산"""
        pass

    @abstractmethod
    def add(self, data: dict[str, Any]) -> None:
        """집합 추가 연산"""
        pass


class Global_visit_set_page_url(Global_visit_page_url):
    """set을 이용한 방문집합, 디버깅용"""

    def __init__(self) -> None:
        self.set: set[str] = set()

    def __contains__(self, url: str) -> bool:
        return get_clean_url(url) in self.set

    def add(self, data: dict[str, Any]) -> None:
        self.set.add(get_clean_url(data["url"]))
