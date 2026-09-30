from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, override

from .browser_session import FrameRestoreError, Page
from .locator import LocatorNode
from .urls import get_clean_url

__all__ = [
    "IndexedLocatorInfo",
    "DynamicClickExplorer",
    "UrlsProvider",
    "SimpleUrlsProvider",
    "UrlsCollector",
    "SimpleUrlsCollector",
    "DynamicURLExplorer",
    "ContentCollector",
    "SimpleContentCollector",
    "PageContentCollector",
    "Global_visit_page_url",
    "Global_visit_set_page_url",
]


class IndexedLocatorInfo:
    """LocatorNode에 인덱스를 추가한 클래스"""
    def __init__(self, locatornode: LocatorNode, frame_idx: int, locator_idx: int, frame_url: str):
        self.locatornode: LocatorNode = locatornode
        self.frame_url: str = frame_url
        self.frame_idx: int = frame_idx
        self.locator_idx: int = locator_idx

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndexedLocatorInfo):
            return False
        return self.locatornode == other.locatornode and self.frame_url == other.frame_url

    @override
    def __hash__(self) -> int:
        return hash((self.locatornode, self.frame_url))

    def is_alive(self) -> bool:
        return self.locatornode.is_alive()

class DynamicClickExplorer:
    """Page를 움직여서 탐색함"""
    def __init__(self, page: Page):
        self._page: Page = page
        self._click_candidates: deque[IndexedLocatorInfo | int]|None = None # 클릭할 요소들 (int: 0: 로케이터 변화, 1: 페이지이동)
        self._urls_queue: deque[tuple[str, int]] = deque() # (url, depth)
        self._depth: int = 0
        self._seen: set[tuple[str, IndexedLocatorInfo]] = set() # url에서 봤던 (로케이터노드, 인덱스) 쌍을 저장해서 스택에 중복으로 쌓는거를 방지

    def _mark_new(self, infos: list[IndexedLocatorInfo]) -> list[IndexedLocatorInfo]:
        """이 페이지에서 처음 보는 요소만 등록해서 반환 (이미 후보였던 건 제외)"""
        page_url = get_clean_url(self._page.page.url)
        fresh: list[IndexedLocatorInfo] = []
        for info in infos:
            key = (page_url, info)
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(info)
        return fresh

    def get_depth(self) -> int:
        return self._depth

    async def _safe_get_node_info(self, f_idx: int, l_idx: int) -> str:
        """안전하게 로케이터 정보를 문자열로 반환 (로깅용)"""
        try:
            page_info = await self._page.get_page_info()
            if f_idx < len(page_info.frameInfos):
                fi = page_info.frameInfos[f_idx]
                if l_idx < len(fi.locator_nodes):
                    return ", ".join(fi.locator_nodes[l_idx].values())
        except Exception as e:
            print(e)
        print(f_idx , len((await self._page.get_page_info()).frameInfos), l_idx, len((await self._page.get_page_info()).frameInfos[f_idx].locator_nodes))
        return f"(Frame {f_idx}, Locator {l_idx})"

    async def _get_indexed_locators(self, page: Page)->list[IndexedLocatorInfo]:
        """특정 시점의 페이지의 상태를 나타내는 indexed_locators를 반환함"""
        indexed_locators: list[IndexedLocatorInfo] = []
        page_info = await page.get_page_info()
        for f_idx, fi in enumerate(page_info.frameInfos):
            url = fi.url
            for l_idx, ln in enumerate(fi.locator_nodes):
                indexed_locators.append(IndexedLocatorInfo(ln, f_idx, l_idx, url))
        return indexed_locators

    def _diff(self, 
            indexed_locators1:list[IndexedLocatorInfo], 
            indexed_locators2: list[IndexedLocatorInfo] 
            ) -> tuple[list[IndexedLocatorInfo], list[IndexedLocatorInfo]]:
        """
        indexed_locators1(before) 대비 indexed_locators2(after)에서 
        새로 생긴 요소('new', 새로운 페이지의 프레임기준 인덱스), 
        사라진 요소('disappeared', 이전 페이지의 프레임기준 인덱스)를 반환함
        """
        set_1 = set(indexed_locators1)
        set_2 = set(indexed_locators2)
        set_diff_new_elements = set_2 - set_1
        set_diff_disappeared_elements = set_1 - set_2

        return list(set_diff_new_elements), list(set_diff_disappeared_elements)

    def _discard_recent(self, failed_steps: int) -> None:
        """오른쪽(최근)부터 failed_steps개의 separator에 해당하는 구간을 통째로 버림"""
        discarded_separators = 0
        while self._click_candidates and discarded_separators < failed_steps:
            item = self._click_candidates.pop()
            if item in (0, 1):
                if item == 1:
                    self._urls_queue.pop()  # 같이 버려지는 페이지이동이면 큐도 맞춰서 정리
                discarded_separators += 1
    async def _safe_undo(self) -> None:
        """undo 시도, 복원 실패하면 해당 구간만큼 스택에서 자동 폐기"""
        try:
            await self._page.undo()
        except FrameRestoreError as e:
            print(f"DynamicClickExplorer:복원 실패, 최근 {e.failed_steps}단계 폐기: {e}")
            self._discard_recent(e.failed_steps)

    async def next(self) -> int: # 페이지가 변했으면 1, 로케이터가 변했으면 2, 둘 다 아니면(실패) 0
        '''다음변화(페이지 이동, 요소변화)까지 page를 이동시킴'''
        init_indexed_locators = await self._get_indexed_locators(self._page)
        while True:
            candidate = None
            if self._click_candidates == None:
                # 처음이라면 스택 초기화
                # self._click_candidates = deque(self._mark_new(init_indexed_locators))   # 초기 후보도 등록
                self._click_candidates = deque(self._mark_new(init_indexed_locators)[13:14])   # 디버깅, 빠르게 몇개만 해보기
            if len(self._click_candidates) > 0:
                candidate = self._click_candidates.pop()
            else:
                if len(self._urls_queue) <= 0:
                    return 0 # 더이상 갈곳이 없음
                url, depth = self._urls_queue.popleft() # 큐에서 다음으로 탐색할 url을 꺼냄
                await self._page.goto(url)              # 이동 후 스택 초기화
                self._depth = depth
                self._click_candidates = None
                continue
            if candidate in (0,1): # 뭔가 변화가 발생한 경계이므로 되돌림
                # await self._page.undo()
                await self._safe_undo()
                if candidate == 1:
                    self._depth -= 1
                continue
            f_idx, l_idx, node = candidate.frame_idx, candidate.locator_idx, candidate.locatornode

            is_changed = False
            if not node.is_alive():
                print(f"DynamicClickExplorer:스킵: [{f_idx}][{l_idx}] {', '.join(node.values())} - 현재 페이지에는 존재하지 않음")
                continue
            try:
                print(f"DynamicClickExplorer:클릭시도: [{f_idx}][{l_idx}] {', '.join(node.values())}")
                is_changed = await self._page.click_locator(f_idx, l_idx)
            except Exception as e:
                print(f"DynamicClickExplorer:클릭실패: [{f_idx}][{l_idx}] {', '.join(node.values())} (오류: {e})")
                continue
            if not is_changed:# 변화없으면 다음꺼
                print(f"DynamicClickExplorer:아무 변화 없음")
                continue
            
            if self._page.page_changed:
                print(f"DynamicClickExplorer:페이지가 변함")
                self._depth += 1
                self._click_candidates.append(1)
                self._urls_queue.append((self._page.page.url, self._depth+1))
                return 1
        
            after_indexed_locators = await self._get_indexed_locators(self._page)
            if init_indexed_locators == None: # 디버깅용
                print("DynamicClickExplorer:err: init_indexed_locators is None")
            new, disappeared = self._diff(init_indexed_locators, after_indexed_locators)
            new = self._mark_new(new)
            if not new: # 사라지기만 했을경우 다시 되돌림(오히려 탐색할 수 있는게 줄어드므로)
                print(f"DynamicClickExplorer:사라지기만해서 되돌림")
                await self._safe_undo()
                continue
            #새로 생긴것들 스택에 추가
            print(f"DynamicClickExplorer:새로생긴 요소들({len(new)}개): ")
            for i in new:
                print(f"  [{i.frame_idx}][{i.locator_idx}] {', '.join(i.locatornode.values())}")
            self._click_candidates.append(0)
            self._click_candidates.extend(new)
            return 2

    async def abort(self)->bool:
        """이동된 페이지,상태가 맘에 안들면(예:원치않는 url로 감,팝업이 뜸,오류가 뜸, 초과깊이 등) 이거를 호출해서 해당부분을 스택에서 전부 제거하고, 되돌림"""
        while True:
            if len(self._click_candidates) == 0:
                return await self.next() > 0 # 다음 url로 그냥 감, 만약 이게 1,2이면 True, 0이면 False를 리턴
            poped = self._click_candidates.pop()
            if poped in (0,1):  # separator
                if poped  == 1:
                    self._urls_queue.pop() # 마지막에 큐에 추가한 url을 제거함
                # await self._page.undo()
                await self._safe_undo()
                self._depth -= 1
                return True

class UrlsProvider(ABC):
    """url들을 반환하는 객체"""
    @abstractmethod
    def next(self) -> tuple[str, str]:
        pass
    @abstractmethod
    def is_end(self) -> bool:
        pass
class SimpleUrlsProvider(UrlsProvider):
    def __init__(self, urls: list[tuple[str,str]]) -> None:
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
    def __init__(self, page: Page,
                url_visited: Global_visit_page_url,
                max_depth: int = 1,
                collector: UrlsCollector|None = None, # 새로운 url방문시 페이지정보를 넘겨받을객체
                ):
        self.page = page
        self._dynamic_click_explorer: DynamicClickExplorer = DynamicClickExplorer(page)
        self._url_visited: Global_visit_page_url = url_visited # 현재 방문한 url들
        self._max_depth = max_depth
        self.collector = collector or SimpleUrlsCollector()

    def _is_max_depth(self) -> bool:
        print(f"DynamicURLExplorer:curdepth:{self._dynamic_click_explorer.get_depth()}, maxdepth:{self._max_depth}")
        return self._dynamic_click_explorer.get_depth() >= self._max_depth

    async def do_recursivly(self):
        page_info = await self.page.get_page_info()
        url_init = get_clean_url(page_info.url)
        self._url_visited.add({"url": url_init})

        while True:
            ret_next = await self._dynamic_click_explorer.next()
            if ret_next == 0: # 더 이상 갈곳이 없으면 종료
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


class ContentCollector(ABC):
    @abstractmethod
    def collect(self, url: str, title: str, content: str, content_hash: str) -> None:
        """content 수집"""
        pass
    @abstractmethod
    def get_content(self, page_info_dict: dict[str,str]) -> dict[str,str]:
        """찾은 content를 반환"""
        pass
class SimpleContentCollector(ContentCollector):
    def __init__(self) -> None:
        self.contents: dict[str, dict[str, str]] = {} # key:url, value:{title, content, content_hash}
    def collect(self, url: str, title: str, content: str, content_hash: str) -> None:
        """content 수집"""
        self.contents[url]={"url": url, "title": title, "content": content, "content_hash": content_hash}
    def get_content(self, page_info_dict: dict[str,str]) -> dict[str, str]:
        """찾은 content를 반환"""
        return self.contents[page_info_dict["url"]]
class PageContentCollector:
    """html본문을 추출해 저장합니다"""
    def __init__(self, page: Page, url_provider:UrlsProvider, content_collector:ContentCollector) -> None:
        self._page:Page = page
        self._url_provider:UrlsProvider = url_provider
        self._content_collector:ContentCollector = content_collector
    async def _fetch_one(self, url:str) -> dict[str, str]:
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
    async def fetch_next(self)->bool:
        """urlProvider에서 url하나를 받아와 추출함"""
        if self._url_provider.is_end():
            return False
        url_tuple = self._url_provider.next()
        content = await self._fetch_one(url_tuple[0])
        self._content_collector.collect(url_tuple[0], url_tuple[1], content["content"], content["content_hash"])
        return True
    async def fetch_all(self):
        """전부추출후 저장"""
        while await self.fetch_next():
            pass

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
        self.set.add(get_clean_url(data['url']))
