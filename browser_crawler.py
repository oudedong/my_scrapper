from __future__ import annotations
import sys
from collections import deque
from typing import Any, override
from urllib.parse import urlparse

from .browser_session import LocatorNode, PageInfo, Page, Context

from abc import ABC, abstractmethod

class IndexedLocatorInfo:
    """LocatorNode에 인덱스를 추가한 클래스"""
    def __init__(self, locatornode: LocatorNode, frame_idx: int, locator_idx: int, frame_url: str):
        self.locatornode: LocatorNode = locatornode
        self.frame_idx: int = frame_idx
        self.locator_idx: int = locator_idx
        self.frame_url: str = frame_url
    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndexedLocatorInfo):
            return False
        return self.locatornode == other.locatornode and self.frame_url == other.frame_url
    @override
    def __hash__(self) -> int:
        return hash((self.locatornode, self.frame_url))
    
class PageSnapshot:
    """특정 시점의 페이지의 상태"""
    def __init__(self, page_info: PageInfo):
        self.page_info: PageInfo = page_info
        # URL별로 locator들을 병합하여 저장
        self.url_indexed_locators: list[IndexedLocatorInfo] = []
        for f_idx, fi in enumerate(page_info.frameInfos):
            url = fi.url
            for l_idx, ln in enumerate(fi.locator_nodes):
                self.url_indexed_locators.append(IndexedLocatorInfo(ln, f_idx, l_idx, url))
    def _get_dict(self, url_indexed_locators: set[IndexedLocatorInfo]) -> dict[int, list[int]]:
        """url_indexed_locators를 (frame_idx -> [locator_idx]) 형태로 반환"""
        elements: dict[int, list[int]] = {}
        for idx_info in url_indexed_locators:
            if idx_info.frame_idx not in elements:
                elements[idx_info.frame_idx] = []
            elements[idx_info.frame_idx].append(idx_info.locator_idx)
        return elements
    def diff(self, new_snapshot: PageSnapshot) -> dict[str, dict[int, list[int]]]:
        """
        self(before) 대비 other(after)에서 
        새로 생긴 요소('new', 새로운 페이지의 프레임기준 인덱스), 
        사라진 요소('disappeared', 이전 페이지의 프레임기준 인덱스), 
        유지된 요소('intersection', 새로운 페이지의 프레임기준 인덱스) 를 반환함
        {new: {frame_idx: [locator_idx, ...]}, disappeared: {frame_idx: [locator_idx, ...]}, intersection: {frame_idx: [locator_idx, ...]}}
        """
        diff: dict[str, dict[int, list[int]]] = {}
        set_self = set(self.url_indexed_locators)
        set_other = set(new_snapshot.url_indexed_locators)
        set_diff_new_elements = set_other - set_self
        set_diff_disappeared_elements = set_self - set_other
        set_intersection_elements = set_other & set_self
        
        diff["new"] = self._get_dict(set_diff_new_elements)
        diff["disappeared"] = self._get_dict(set_diff_disappeared_elements)
        diff["intersection"] = self._get_dict(set_intersection_elements)
        return diff
class PageSnapshotStack:
    def __init__(self, page_snapshot: PageSnapshot):
        self._snapshot: PageSnapshot = page_snapshot
        self.page_url: str = page_snapshot.page_info.url
        self._stack: deque[tuple[int, int] | None] = deque([(ii.frame_idx, ii.locator_idx) for ii in page_snapshot.url_indexed_locators])
    @property
    def stack_size(self) -> int:
        return len(self._stack)
    def pop(self) -> tuple[int, int] | None:
        return self._stack.pop()
    def check_and_add_new_elements(self, new_snapshot: PageSnapshot) -> bool:
        if new_snapshot.page_info.url != self._snapshot.page_info.url:
            raise Exception("URL이 변경되었습니다.")
        diffs = self._snapshot.diff(new_snapshot)
        new_elements = diffs["new"]
        if len(new_elements) <= 0:
            return False
        self._stack.append(None)
        for frame_idx, indices in new_elements.items():
            for idx in indices:
                self._stack.append((frame_idx, idx))
        self._snapshot = new_snapshot
        return True
        
class DynamicClickExplorer:
    """Page를 움직여서 탐색함"""
    def __init__(self, page:Page):
        self.page: Page = page
        self._history: list[PageSnapshotStack] = [] 

    def _get_last(self)->PageSnapshotStack:
        return self._history[-1]
    async def next(self):
        '''다음변화(페이지 이동, 요소변화)까지 page를 이동"시킴!!!!!"'''
        if len(self._history) <= 0:
            # 처음에는 초기화
            page_info = await self.page.get_page_info()
            self._history.append(PageSnapshotStack(PageSnapshot(page_info)))
        while True:
            cur = self._get_last()
            candidate = None
            if cur.stack_size > 0:
                candidate = cur.pop()
            else: # cur.stack_size == 0 -> undo 해야됨 (이전페이지로 돌아가야됨)
                if len(self._history) <= 1: # 더이상 undo할꺼 없음
                    break
                await self.page.undo()
                self._history.pop()
                continue

            if candidate == None:
                await self.page.undo()
                continue
            f_idx, l_idx = candidate
            try:
                print(f"클릭시도:{self.page._get_current_record().frames[f_idx].locator_nodes[l_idx].values()}")
                await self.page.click_locator(f_idx,l_idx) # 시간초과 등등 무시하고 계속 ㄱㄱ
            except Exception as e:
                print(f"클릭실패:{self.page._get_current_record().frames[f_idx].locator_nodes[l_idx].values()}")
                continue

            new_page_info = await self.page.get_page_info()
            new_snapshot = PageSnapshot(new_page_info)
            # 먼저 url이 바뀌었는지 확인
            if new_page_info.url != cur.page_url:
                self._history.append(PageSnapshotStack(new_snapshot))
                return
            # 그다음 요소변화 확인
            if cur.check_and_add_new_elements(new_snapshot):
                return 
    async def abort(self):
        """이동된 페이지,상태가 맘에 안들면(예:원치않는 url로 감,팝업이 뜸,오류가 뜸 등) 이거를 호출해서 해당부분을 스택에서 전부 제거하고, 되돌림"""
        cur = self._get_last()
        while cur.stack_size > 0:
            poped = cur.pop()
            if poped is None: # separator
                break
        if cur.stack_size <= 0:
        # 스택크기가 0이면 현재페이지에서 변화가 없었음->되돌아가면 이전페이지로 돌아가야됨
            self._history.pop()
        await self.page.undo()
            
class RedirectError(Exception):
    def __init__(self, intended_url: str, current_url: str, current_page_title: str):
        super().__init__()
        self.intended_url: str = intended_url
        self.current_url: str = current_url
        self.current_page_title: str = current_page_title
    @override
    def __str__(self) -> str:
        return f"의도한 페이지로 이동되지 않았습니다. 의도한 페이지:{self.intended_url}, 현재 페이지제목:{self.current_page_title}, 현재url:{self.current_url}"


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
        return url in self.set
    def add(self, data: dict[str, Any]) -> None:
        self.set.add(data['url'])

def get_clean_url(url: str) -> str:
    # URL에서 쿼리스트링을 제외한 부분만 반환함
    parsed_url = urlparse(url)
    clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    return clean_url

class Redirected_page_urls(Global_visit_page_url, ABC):
    """리다이렉션 페이지로 이동했는지 확인, 해결을 시도해보는 클래스"""
    def __init__(self, solvers: list[Redirected_page_solver]):
        super().__init__()
        self.solvers: list[Redirected_page_solver] = solvers # 리다이렉션을 해결해볼 방법들
    async def try_solve(self, redirected_url: str) -> bool:
        # 리다이렉션 문제를 해결시도해봄
        t_url: str | None = None
        try:
            t_url = self._get_target_url(redirected_url)
        except Exception as e:
            print(f't_url을 얻을 수 없음: url:{redirected_url}, e:{str(e)}', file=sys.stderr)
            return False
        if t_url is None:
            return False
        for solver in self.solvers:
            if await solver(t_url):
                return True
        return False
    @abstractmethod
    def _get_target_url(self, redirected_url: str) -> str | None:
        # 리다이렉션을 처리하기위해 이동해야되는 페이지를 줌(로그인페이지 등등)
        pass
    def add(self, data: dict[str, Any]) -> None:
        # 삽입시 쿼리스트링 땜, 아래 2가지 값이 있어야됨
        r_url = get_clean_url(data['redirected_url'])  # 리다이렉션된 url
        t_url = data['target_url']      # 처리를 할 url위치 
        try:
            self._add(r_url, t_url)
        except Exception as e:
            print(f"리다이렉션 추가 중 오류 발생: {str(e)}",file=sys.stderr)
    @abstractmethod
    def _add(self, r_url: str, t_url: str | None) -> None:
        # 실제 삽입부분
        pass
    def __contains__(self, url: str) -> bool:
        # 쿼리스트링을 때줌, db에 때서 넣었으므로
        url = get_clean_url(url)
        return self._contains(url)
    @abstractmethod
    def _contains(self, url: str) -> bool:
        # 실제 in연산 부분
        pass

class Redirected_page_urls_set(Redirected_page_urls):
    """파이썬 딕셔너리를 활용하는 Redirected_page_urls"""
    def __init__(self, solvers: list[Redirected_page_solver]):
        super().__init__(solvers)
        self.r_db: dict[str, str | None] = {}
    def _contains(self, url: str) -> bool:
        ret = self.r_db.get(url)
        return not(ret is None)
    def _add(self, r_url: str, t_url: str | None) -> None:
        self.r_db[r_url] = t_url
    def _get_target_url(self, redirected_url: str) -> str | None:
        return self.r_db[redirected_url]

class Redirected_page_solver(ABC):
    """
    해결을 담당하는 클래스
    예외처리는 여기서!!
    """
    def __init__(self, session_path: str, data_source: Redirection_db, headless: bool):
        self.session_path: str = session_path #해결완료시 세션을 저장할 경로
        self.data_source: Redirection_db = data_source #리다이렉션 해결에 필요한 정보들을 줄 객체
        self.headless: bool = headless
    async def __call__(self, redirected_url: str) -> bool:
        data = self.data_source(redirected_url)
        try:
            # solver로 해결시도
            context = await Context.create(self.session_path)
            page = await context.new_page()
            await self._solve(page, data)
            # url변화시 성공으로 간주
            page_info = await page.get_page_info()
            if page_info.url != redirected_url:
                await context.save_session()
                await context.close()
                return True
            await context.close()
            return False
        except Exception as e:
            print(f"solver실패: 클래스명:{self.__class__.__name__}, e:{str(e)}", file=sys.stderr)
            return False
    @abstractmethod
    async def _solve(self, page: Page, data: dict[str, Any]) -> None:
        """
        실제 해결하는 부분
        page: Page객체,
        data: 리다이렉션 해결에 필요한 데이터
        """
        pass

class Try_login_solver(Redirected_page_solver):
    """
    로그인 페이지에서 로그인을 시도해보는 클래스
    아이디,비밀번호 입력창의 css경로와 아이디 비번을 활용함
    """
    def __init__(self, session_path: str, data_source: Redirection_db, headless: bool = True):
        super().__init__(session_path, data_source, headless)
    async def _solve(self, page: Page, data: dict[str, Any]) -> None:
        """
        data는 반드시 아래 필드를 가지고 있어야됨!!!!
        login_url: 로그인을 시도할 url
        css_path_id: 아이디 입력창의 css경로
        css_path_pw: pw 입력창의 css경로
        frame_idx: 입력창들의 프레임 위치인덱스
        login_id: 로그인 id
        login_pw: 비밀번호
        """
        login_url: str = data['login_url']
        frame_idx: int = data['frame_idx']
        css_path_id: str = data['css_path_id']
        css_path_pw: str = data['css_path_pw']
        login_id: str = data['login_id']
        login_pw: str = data['login_pw']
        # 로그인 페이지로 이동
        await page.goto(login_url)
        # 로그인 요소들 찾기
        id_field = await page.locator(css_path_id, frame_idx)
        pw_field = await page.locator(css_path_pw, frame_idx)
        # id,비번 입력
        await id_field.fill(login_id)
        await pw_field.fill(login_pw)
        # 엔터
        await pw_field.locator.press("Enter")
        
class Redirection_db(ABC):
    """
    리다이렉션 해결에 필요한 정보들을 가짐
    """
    @abstractmethod
    def __call__(self, redirected_url: str) -> dict[str, Any]:
        """
        url을 받아 해당 url에서 solver가 리다이렉션 해결에 필요한 정보들을 dict형태로 반환함
        """
        pass
