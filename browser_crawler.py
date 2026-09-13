from __future__ import annotations
import sys
from collections import deque
from typing import Any, override
from abc import ABC, abstractmethod

from .browser_session import Page, PageInfo
from .locator import LocatorNode

from .urls import get_clean_url, is_same_page_url, get_redirection_clean_url

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

    async def next(self) -> int: # 페이지가 변했으면 1, 로케이터가 변했으면 2, 둘 다 아니면(실패) 0
        '''다음변화(페이지 이동, 요소변화)까지 page를 이동시킴'''
        init_indexed_locators = None
        while True:
            candidate = None
            if self._click_candidates == None:
                # 처음이라면 초기화
                init_indexed_locators = await self._get_indexed_locators(self._page)
                self._click_candidates = deque(init_indexed_locators)
                continue
            elif len(self._click_candidates) > 0:
                candidate = self._click_candidates.pop()
            else:
                if len(self._urls_queue) <= 0:
                    return 0 # 더이상 갈곳이 없음
                url, depth = self._urls_queue.popleft() # 큐에서 다음으로 탐색할 url을 꺼냄
                await self._page.goto(url)              # 이동 후 스택 초기화
                self._depth = depth
                init_indexed_locators = None
                continue
            if candidate in (0,1): # 뭔가 변화가 발생한 경계이므로 되돌림
                await self._page.undo()
                continue
            f_idx, l_idx, node = candidate.frame_idx, candidate.locator_idx, candidate.locatornode

            is_changed = False
            if not node.is_alive():
                print(f"스킵: [{f_idx}][{l_idx}] {', '.join(node.values())} - 현재 페이지에는 존재하지 않음")
                continue
            try:
                print(f"클릭시도: [{f_idx}][{l_idx}] {', '.join(node.values())}")
                is_changed = await self._page.click_locator(f_idx, l_idx)
            except Exception as e:
                print(f"클릭실패: [{f_idx}][{l_idx}] {', '.join(node.values())} (오류: {e})")
                continue
            if not is_changed:# 변화없으면 다음꺼
                continue
            
            after_indexed_locators = await self._get_indexed_locators(self._page)
            if self._page.page_changed:
                self._click_candidates.append(1)
                self._urls_queue.append((self._page.page.url, self._depth+1))
                return 1
        
            new, disappeared = self._diff(init_indexed_locators, after_indexed_locators)
            if len(new) <= 0 and len(disappeared) > 0: # 사라지기만 하면 다시 되돌림(오히려 탐색할 수 있는게 줄어드므로)
                await self._page.undo()
                continue
            #새로 생긴것들 스택에 추가
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
                await self._page.undo()
                return True

class DynamicURLExplorer:    
    def __init__(self, page: Page,
                url_visited: Global_visit_page_url, url_redirected: Redirected_page_urls|None = None,
                max_depth: int = 2):
        self.page = page
        self._dynamic_click_explorer: DynamicClickExplorer = DynamicClickExplorer(page)
        self._url_visited: Global_visit_page_url = url_visited      # 현재 방문한 url들
        self._url_redirected: Redirected_page_urls|None = url_redirected# 리다이렉트 url 목록(이 url을 만나면 뭔가 처리해 줘야됨)
        self._max_depth = max_depth

    def _is_max_depth(self) -> bool:
        return self._dynamic_click_explorer.get_depth() >= self._max_depth

    async def do_recursivly(self):
        page_info = await self.page.get_page_info()
        url_init = get_clean_url(page_info.url)
        self._url_visited.add({"url": url_init})

        while True:
            while self._is_max_depth():
                if not await self._dynamic_click_explorer.abort():
                    return
            print("hererererer")
            ret_next = await self._dynamic_click_explorer.next()
            if ret_next == 0: # 더 이상 갈곳이 없으면 종료
                return
            print("okokok")
            page_info = await self.page.get_page_info()
            url_current = get_clean_url(page_info.url)
            if ret_next == 2:
                continue
            if self._url_redirected is not None and (url_current in self._url_redirected or self._url_redirected.clean_url(url_current) in self._url_redirected):
                if not await self._url_redirected.try_solve():
                    await self._dynamic_click_explorer.abort()
                    continue
                page_info = await self.page.get_page_info()
                url_current = get_clean_url(page_info.url)
            if url_current in self._url_visited:
                await self._dynamic_click_explorer.abort()
                continue
            self._url_visited.add({"url": url_current})

################# 아래는 아직 개선전..

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
        return get_clean_url(url) in self.set

    def add(self, data: dict[str, Any]) -> None:
        self.set.add(get_clean_url(data['url']))

class Redirected_page_urls(Global_visit_page_url, ABC):
    """리다이렉션 페이지로 이동했는지 확인, 해결을 시도해보는 클래스"""
    def __init__(self, page: Page, solvers: list[Redirected_page_solver], session_path: str):
        super().__init__()
        self.page: Page = page # page객체를 받아서 직접 해결함
        self.solvers: list[Redirected_page_solver] = solvers  # 리다이렉션을 해결해볼 방법들
        self.session_path: str = session_path

    def clean_url(self, url: str) -> str:
        """리다이렉션 검사용 URL 전처리 (쿼리스트링 및 프래그먼트 제거)"""
        return get_redirection_clean_url(url)

    @abstractmethod
    def _get_redirection_data(self)->dict[str,Any]: # 리다이렉션 해결에 필요한 데이터들을 반환함
        pass

    async def try_solve(self) -> bool:
        # 리다이렉션 문제를 해결시도해봄, 왠만하면 리다이렉션이 발생하자 마자 호출하기!
        cur_raw_url = (await self.page.get_page_info()).url # 요청시 page의 현재 주소 확인함
        cur_url = self.clean_url(cur_raw_url)
        if cur_url not in self and cur_raw_url not in self: # 리다이렉션 목록에 없는경우
            return False
        # 리다이렉션 처리후 다시 의도한 페이지로 이동시키기 위해서 상태를 찾아놓음
        records = self.page.records
        record_idx = None    
        last_command = None  
        for i, record in enumerate(records):
            if self.clean_url(record.page_url) == cur_url:
                record_idx = i - 1 # _goto_page_state에서 사용하기 위한 리다이렉트되기전 마지막 상태
                last_command = record.command_frames[-1].first # _goto_page_state에서 사용하기 위한 리다이렉트되기전 마지막 상태에서 쓴 커맨드(리다이렉트 해결했으면 이때 의도한 주소로 가짐)
                break
        for solver in self.solvers:
            # 먼저 해결시도
            await solver(self.page, self._get_redirection_data())
            # 그리고 페이지 복구시도, 만약에 다시 리다이렉션된 페이지로 가면 실패로 간주
            if record_idx is not None and record_idx >= 0:
                await self.page._goto_page_state(record_idx, None)
            else:
                await self.page.reset_records()
            if last_command is not None:
                self.page._append_command(last_command)
                await self.page._apply_commands_in_buffer()
            cur_raw_url = (await self.page.get_page_info()).url  # 요청시 page의 현재 주소 확인함
            cur_url = self.clean_url(cur_raw_url)
            if cur_url in self or cur_raw_url in self:  # 리다이렉션 목록에 있는경우 -> 실패로 간주, 다음 solver시도
                continue
            # await self.page.page.context.storage_state(path=self.session_path)
            return True
        return False

class Redirected_page_solver(ABC):
    """
    해결을 담당하는 클래스
    예외처리는 여기서!!
    """
    def __init__(self, session_path: str):
        self.session_path: str = session_path  # 해결완료시 세션을 저장할 경로

    async def __call__(self, page: Page, data: dict[str, Any]):
        try:
            await self._solve(page, data)
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
    def __init__(self, session_path: str):
        super().__init__(session_path)

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
        # 엔터 입력 및 페이지 로딩 대기
        await pw_field.locator.press("Enter")
        try:
            await page.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

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
