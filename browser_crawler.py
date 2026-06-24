import sys
from collections import deque
from typing import Callable
from urllib.parse import urlparse

from .browser_session import LocatorInfo, Frame, PageInfo, Page_Record, PageAnalyzer, FrameInfo, Page, Context
from .html_cleaner import recursive_iframe_replace, clean_html

from abc import ABC, abstractmethod
from playwright.async_api import TimeoutError, Error  # <-- 예외 객체 import
import asyncio

# ── 페이지 가져오기 ────────────────────────────────────────────────────────────
class RedirectError(Exception):
    def __init__(self, intended_url, current_url, current_page_title):
        self.intended_url = intended_url
        self.current_url = current_url
        self.current_page_title = current_page_title
    def __str__(self):
        return f"의도한 페이지로 이동되지 않았습니다. 의도한 페이지:{self.intended_url}, 현재 페이지제목:{self.current_page_title}, 현재url:{self.current_url}"

# @dataclass
# class PageInfo:
#     # 페이지의 정보를 나타냄
#     url: str
#     title: str
#     frameInfos: list[FrameInfo]

async def _recursive_dynamic_click(
        init_url: str, context_obj:Context, page_obj:Page, frame_index: int,
        global_visited, Redirected_page_urls:"Redirected_page_urls"=None, # 방문한 url집합, 리다이렉션 url집합
        new_elements_nths: list[int] = None) -> list[dict]:
    """
    프레임에서 클릭 가능한 요소들을 모두 클릭해보며 이동되는 URL을 수집합니다.
    동적으로 생성되는 요소(드롭다운 등)도 재귀적으로 탐색합니다.
    URL은 bfs로 생성되는 요소는 dfs로
    """
    rets = []
    locators_before = page_obj.get_page_info().frameInfos[frame_index].locatorInfos # 갱신이전

    # 인덱스,순서로 요소를 나타냄
    targets_to_click = new_elements_nths if new_elements_nths is not None else range(len(locators_before))

    j = 0
    while j < len(targets_to_click):
        locator_idx = targets_to_click[j]
        try:
            target_locator = locators_before[locator_idx]

            print(f"[*] 클릭 시도: tag:{target_locator.tag}, text:{target_locator.text}", file=sys.stderr)
            is_page_changed = page_obj.click_locator(frame_index, locator_idx)
            new_page_info = page_obj.get_page_info()
            after_url = new_page_info.url
            print(f"[*] 이동된 url: {after_url}", file=sys.stderr)

            # 페이지 이동이 생겼으면
            if after_url != init_url:
                # 리다이렉션 페이지인지 확인(리다이렉션 집합이 있는경우에만)
                if Redirected_page_urls != None:
                    if after_url in Redirected_page_urls:
                        # 해결시도 + 세션갱신#########################################
                        ret = await Redirected_page_urls.try_solve(after_url)
                        # 성공시
                        if not ret:
                            # 실패시 건너뜀
                            j += 1
                        continue
                # 이미 방문한 페이지인지
                if after_url not in global_visited:
                    title = await new_page_info.title
                    ret = {"url": after_url, "title": title.strip()}
                    rets.append(ret)
                    global_visited.add(ret)
                    print(f"[*] 추가한 url: {ret['url']}", file=sys.stderr)
                await page_obj.undo() # 다시 뒤로가기
                j += 1
                continue
            
            # 클릭 후 새로 생긴 요소 탐색
            if is_page_changed:
                locators_after = new_page_info.frameInfos[frame_index].locatorInfos
                after_count = len(locators_after)
                new_indices = []
                for k in range(after_count):
                    if locators_after[k] not in locators_before:
                        new_indices.append(k)
                # 새로운 요소가 있다면
                if new_indices:
                    rets += await _recursive_dynamic_click(
                        init_url, page_obj, context_obj, frame_index,
                        global_visited, Redirected_page_urls, new_indices
                    )
                    await page_obj.undo()
                j += 1
        except TimeoutError as e:
            print(f"클릭 실패", file=sys.stderr)
            j += 1

    return rets


async def collect_page_urls(session_path:str, url: str, visited, Redirected_page_urls:"Redirected_page_urls"=None, depth: int = 1) -> list[dict]:
    """
    페이지에서 클릭 가능한 요소들을 탐색하여 이동되는 URL들을 수집합니다.
    iframe 내부도 탐색하며, depth만큼 재귀적으로 수집합니다.
    """
    context = await Context.create(session_path)
    page = await context.new_page()

    queue = deque([(url, 0)])
    if url not in visited:
        visited.add({"url": url, "title": None})
    rets = []

    try:
        while queue:
            cur_url, cur_depth = queue.popleft()
            print(f"pop됨: url:{cur_url}, cur_depth:{cur_depth}", file=sys.stderr)
            if depth > 0 and cur_depth >= depth:
                continue
            
            # 세션 갱신등으로 page가 바뀌었을 수 있으므로 갱신
            await page.goto(cur_url)
            page_info = page.get_page_info()
            # await asyncio.sleep(3) # 아이프레임 대기용?

            # 만약 처음접속한 페이지가 리다이렉션 페이지이면
            if cur_url != page_info.url:
                if Redirected_page_urls != None :
                    # 리다이렉션 정보가 있을때
                    if page_info.url in Redirected_page_urls:
                        ret = await Redirected_page_urls.try_solve(page_info.url) # 해결시도+새로고침
                        # 해결실패시
                        if not ret:
                            # 건너뜀
                            print(f"리다이렉션 해결실패 url:{cur_url}, 건너뜀", file=sys.stderr)
                            continue
                    # 없을때
                    else:
                        # 리다이렉션 정보를 db에 추가 후 건너뜀
                        print(f"db에 정보가 없는 리다이렉션 발생 url:{cur_url}, db에 추가 후 건너뜀", file=sys.stderr)
                        Redirected_page_urls.add({'redirected_url':page_info.url, 'target_url':None}) # page.url이 실제,리다이렉션된 url
                        continue
                else:
                    print(f"리다이렉션 발생 url:{cur_url}, 건너뜀", file=sys.stderr)
                    continue
            
            # print(f"get_sub_urls_by_click 초기 프레임개수: {len(page.frames)}", file=sys.stderr)
            for frame_idx in range(len(page_info.frameInfos)):
                try:
                    frame_rets = await _recursive_dynamic_click(
                        cur_url, context, page, frame_idx, visited, Redirected_page_urls
                    )
                    for ret in frame_rets:
                        rets.append(ret)
                        queue.append((ret['url'], cur_depth + 1))
                except Exception as e:
                    print(f"get_sub_urls_by_click에서 오류발생 url:{cur_url}, e:{str(e)}", file=sys.stderr)
                    continue
    finally:
        await context.close()

    return rets

class Global_visit_page_url(ABC):
    """방문집합 형식"""
    @abstractmethod
    def __contains__(self, url:str) -> bool:
        """집합에 대해 in연산"""
        pass
    @abstractmethod
    def add(self, data:dict):
        """집합 추가 연산"""
        pass
class Global_visit_set_page_url(Global_visit_page_url):
    """set을 이용한 방문집합, 디버깅용"""
    def __init__(self):
        self.set = set()
    def __contains__(self, url:str) -> bool:
        return url in self.set
    def add(self, data:dict):
        self.set.add(data['url'])

def get_clean_url(url:str)->str:
    # URL에서 쿼리스트링을 제외한 부분만 반환함
    parsed_url = urlparse(url)
    clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    return clean_url

class Redirected_page_urls(Global_visit_page_url):
    """리다이렉션 페이지로 이동했는지 확인, 해결을 시도해보는 클래스"""
    def __init__(self, solvers:list["Redirected_page_solver"]):
        self.solvers = solvers # 리다이렉션을 해결해볼 방법들
    async def try_solve(self, redirected_url:str) -> bool:
        # 리다이렉션 문제를 해결시도해봄
        t_url = None
        try:
            t_url = self._get_target_url(redirected_url)
        except Exception as e:
            print(f't_url을 얻을 수 없음: url:{redirected_url}, e:{str(e)}', file=sys.stderr)
            return False
        for solver in self.solvers:
            if await solver(t_url):
                return True
        return False
    @abstractmethod
    def _get_target_url(self, redirected_url):
        # 리다이렉션을 처리하기위해 이동해야되는 페이지를 줌(로그인페이지 등등)
        pass
    def add(self, data:dict):
        # 삽입시 쿼리스트링 땜, 아래 2가지 값이 있어야됨
        r_url = get_clean_url(data['redirected_url'])  # 리다이렉션된 url
        t_url = data['target_url']      # 처리를 할 url위치 
        try:
            self._add(r_url, t_url)
        except Exception as e:
            print(f"리다이렉션 추가 중 오류 발생: {str(e)}",file=sys.stderr)
    @abstractmethod
    def _add(self, r_url:str, t_url:str):
        # 실제 삽입부분
        pass
    def __contains__(self, url:str)->bool:
        # 쿼리스트링을 때줌, db에 때서 넣었으므로
        url = get_clean_url(url)
        return self._contains(url)
    @abstractmethod
    def _contains(self, url:str):
        # 실제 in연산 부분
        pass
class Redirected_page_urls_set(Redirected_page_urls):
    """파이썬 딕셔너리를 활용하는 Redirected_page_urls"""
    def __init__(self, solvers):
        super().__init__(solvers)
        self.r_db = {}
    def _contains(self, url:str)->bool:
        ret = self.r_db.get(url)
        return not(ret == None)
    def _add(self, r_url:str, t_url:str):
        self.r_db[r_url]=t_url
    def _get_target_url(self, redirected_url):
        return self.r_db[redirected_url]
class Redirected_page_solver(ABC):
    """
    해결을 담당하는 클래스
    예외처리는 여기서!!
    """
    def __init__(self, session_path:str, data_source, headless):
        self.session_path = session_path #해결완료시 세션을 저장할 경로
        self.data_source = data_source #리다이렉션 해결에 필요한 정보들을 줄 객체!!!!! 리다이렉션된 주소를 입력으로 받아서, 해당 url에 필요한 데이터들을 줌
        self.headless = headless
    async def __call__(self, redirected_url:str)->bool:
        data = self.data_source(redirected_url)
        try:
            # solver로 해결시도
            context = await Context.create(self.session_path)
            page = await context.new_page()
            await self._solve(page, data)
            # url변화시 성공으로 간주
            page_info = page.get_page_info()
            if page_info.url != redirected_url:
                await context.save_session()
                return True
            return False
        except Exception as e:
            print(f"solver실패: 클래스명:{self.__class__.__name__}, e:{str(e)}", file=sys.stderr)
            result = False
        finally:
            # mn반환
            await context.close()
    @abstractmethod
    async def _solve(self, page:Page, data:dict):
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
    def __init__(self, session_path:str, data_source, headless=True):
        super().__init__(session_path, data_source, headless)
    async def _solve(self, page:Page, data:dict):
        """
        data는 반드시 아래 필드를 가지고 있어야됨!!!!
        login_url: 로그인을 시도할 url
        css_path_id: 아이디 입력창의 css경로
        css_path_pw: pw 입력창의 css경로
        frame_idx: 입력창들의 프레임 위치인덱스
        login_id: 로그인 id
        login_pw: 비밀번호
        """
        ############## 나중에 별도클래스로 빼주기...
        login_url = data['login_url']
        frame_idx = data['frame_idx']
        css_path_id = data['css_path_id']
        css_path_pw = data['css_path_pw']
        login_id = data['login_id']
        login_pw = data['login_pw']
        # login_btn = data['login_bnt']
        # 로그인 페이지로 이동
        await page.goto(login_url)
        # 로그인 요소들 찾기
        id_field = await page.locator(css_path_id, frame_idx)
        pw_field = await page.locator(css_path_pw, frame_idx)
        # id,비번 입력
        await id_field.fill(login_id)
        await pw_field.fill(login_pw)
        # 엔터
        await pw_field.press("Enter")
        
class Request_to_user_solver(Redirected_page_solver):
    """
    유저에게 해결을 요청하는 클래스
    아이디,비밀번호 입력창의 css경로와 아이디 비번을 활용함
    """
    def __init__(self, session_path:str, data_source):
        super().__init__(session_path, data_source, False)
    async def _solve(self, mn:Playwright_mn, data:dict):
        """
        data는 반드시 아래 필드를 가지고 있어야됨!!!!
        login_url: 로그인을 시도할 url
        유저에게 알릴방법: (미구현됨...)
        """
        login_url = data['login_url']
        # channel_to_user = data['channel_to_user']

        _request_to_user(mn, login_url)
    
class Redirection_db(ABC):
    """
    리다이렉션 해결에 필요한 정보들을 가짐
    """
    @abstractmethod
    def __call__(self, redirected_url:str)->dict:
        """
        url을 받아 해당 url에서 solver가 리다이렉션 해결에 필요한 정보들을 dict형태로 반환함
        """
        pass

async def get_sub_urls_by_click_set(session_path:str, url: str, Redirected_page_urls: Redirected_page_urls, depth: int = 1) -> list[dict]:
    """
    방문처리를 set를 이용하는 get_sub_urls_by_click
    """
    return await get_sub_urls_by_click(session_path, url, Global_visit_set_page_url(), Redirected_page_urls, depth)