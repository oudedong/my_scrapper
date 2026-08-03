from __future__ import annotations
import asyncio
import time, difflib
from playwright.async_api import async_playwright, Page as PlaywrightPage, Frame as PlaywrightFrame, Locator as PlaywrightLocator, Playwright, Browser, BrowserContext
import sys
from .html_cleaner import clean_html, recursive_iframe_replace
from dataclasses import dataclass,field
from typing import Any, override, cast, Generic, TypeVar
from abc import ABC, abstractmethod

"""
기본 구성 클래스들
"""
T1 = TypeVar("T1")
T2 = TypeVar("T2")
@dataclass
class Pair(Generic[T1, T2]):
    first: T1
    second: T2
@dataclass
class StabilityConfig:
    timeout: int = 5000
    stable_ms: int = 500
    interval: float = 0.2
@dataclass
class LocatorConfig:
    selector_include: list[str] = field(
        default_factory=lambda: ["a", "li", "span", "div", "p", "button", "input", "textarea", "select"]
    )
    keyword_forbidden: list[str] = field(
        default_factory=lambda: ["로그아웃", "logout", "signout", "exit", "나가기", "비밀번호 변경", "회원탈퇴", "delete account"]
    )

class LocatorManager:
    def __init__(self, 
                 stable_nodes: list[LocatorNode],
                 locator_nodes: list[LocatorNode], 
                 selector_include:list[str], 
                 keyword_forbidden:list[str],
                 timeout, 
                 stable_ms, 
                 interval):
        self._selector_include:list[str] = selector_include
        self._keyword_forbidden:list[str] = keyword_forbidden
        self.locator_nodes: list[LocatorNode] = locator_nodes
        self._stable_nodes: list[LocatorNode] = stable_nodes
        self._timeout: int = timeout
        self._stable_ms: int = stable_ms
        self._interval: float = interval
    @classmethod
    async def _extract(cls,frame:PlaywrightFrame,_selector_include,_keyword_forbidden)->list[PlaywrightLocator]:
        """프레임에서 후보 요소들을 뽑아줍니다."""
        selector_str = ", ".join(_selector_include)
        await frame.evaluate(
            """
            (args) => {
                const { sel, keywords } = args;
                const elements = document.querySelectorAll(sel);
                elements.forEach(el => {
                    const text =
                        (el.innerText || "").toLowerCase();
                    const href =
                        (el.getAttribute("href") || "")
                        .toLowerCase();
                    const isForbidden =
                        keywords.some(k =>
                            text.includes(k) ||
                            href.includes(k)
                        );
                    if (isForbidden)
                        return;
                    const hasText = Array.from(el.childNodes).some(node =>
                        node.nodeType === 3 && node.textContent.trim().length > 0
                    );
                    const isInteractive =
                        el.matches(
                            "button, input, textarea, select"
                        );
                    if (hasText || isInteractive) {
                        el.classList.add(
                            "mcp-clickable-target"
                        );
                    }
                });
            }
            """,
            {
                "sel": selector_str,
                "keywords": _keyword_forbidden,
            }
        )
        return (
            await frame
            .locator(".mcp-clickable-target")
            .filter(visible=True)
            .all()
        )
    @classmethod
    async def _extract_stable(cls,frame:PlaywrightFrame, _timeout, _stable_ms, _selector_include,_keyword_forbidden)->list[PlaywrightLocator]:
        last_locator_nodes:list[Pair] = []
        stable_start: float | None = None

        await frame.wait_for_load_state('domcontentloaded')
        last_locator_nodes = [Pair(await LocatorNode.create(l),False) for l in await cls._extract(frame,_selector_include,_keyword_forbidden)]
        start_time = time.time()

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > _timeout:
                print(f"[!] _extract_stable: 시간 초과(gap={gap}) (현재 상태로 그냥 진행)", file=sys.stderr)
                break
            try:
                current_locator_nodes = [await LocatorNode.create(l) for l in await cls._extract(frame,_selector_include,_keyword_forbidden)]
                next_locator_nodes:list[Pair] = []
                sm = difflib.SequenceMatcher(a=[p.first for p in last_locator_nodes], b=current_locator_nodes, autojunk=False)
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        for i in range(i2-i1):
                            if last_locator_nodes[i1+i].second == True: # 없어졌다가 다시 생긴 노드는 제외
                                next_locator_nodes.append(last_locator_nodes[i1+i])
                                continue
                            # 계속 있던 노드들은 그대로 추가
                            next_locator_nodes.append(Pair(current_locator_nodes[j1+i],False))
                        continue
                    if tag == "delete":
                        for ln in last_locator_nodes[i1:i2]:
                            ln.second = True # 없어졌다는 표시...
                        next_locator_nodes += last_locator_nodes[i1:i2]
                        continue
                    if tag == "insert": # 처음보는경우(생성된 경우는 일단 추가)
                        next_locator_nodes += [Pair(n,False) for n in current_locator_nodes[j1:j2]]
                        continue
                    if tag == "replace":
                        next_locator_nodes += [Pair(n,False) for n in current_locator_nodes[j1:j2]] # 새로생긴거는 추가
                        for ln in last_locator_nodes[i1:i2]:               # 없어진거는 없어진거를 표시
                            ln.second = True # 없어졌다는 표시...
                        next_locator_nodes += last_locator_nodes[i1:i2]
                        continue

                if next_locator_nodes == last_locator_nodes:
                    if stable_start is None:
                        stable_start = time.time()
                    # 2. 지정된 시간(예: 500ms) 동안 변화가 없었다면 조건 충족
                    if (time.time() - stable_start) * 1000 >= _stable_ms:
                        break  # 완전히 안정화됨
                else:
                    # 변화가 생겼다면 타이머를 초기화하고 최신 상태를 기록
                    stable_start = None
                    last_locator_nodes = next_locator_nodes
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)
        return [i.first.locator for i in last_locator_nodes if not i.second]
    @classmethod
    async def _filter_clickable(cls, locators:list[PlaywrightLocator])->list[PlaywrightLocator]:
        """html에 존재하는 요소중 실제 클릭가능한 요소만 필터링 합니다"""
        clickable:list[PlaywrightLocator] = []
        for l in locators:
            try: await l.click(trial=True, timeout=300) # 실제 클릭가능한지 시도해봄
            except: continue
            clickable.append(l)
        return clickable
    @classmethod
    async def _locators_to_locatorNodes(cls, locators:list[PlaywrightLocator]):
        return [await LocatorNode.create(l) for l in locators]
    
    async def check_frame_and_refresh(self, frame: PlaywrightFrame)->bool:
        """
        입력받은 프레임이 처음프레임과 같은지 확인함, 같다면 갱신함!
        """
        # 입력프레임에서 stable 로케이터들 추출
        new_stable_locators = await self._extract_stable(frame,self._timeout, self._stable_ms, self._selector_include, self._keyword_forbidden)      # 변하지 않는 로케이터들만 선택함
        new_stable_locator_nodes = await self._locators_to_locatorNodes(new_stable_locators)
        
        # 기존 stable 로케이터와 비교(set이용)
        old_set = set(self.locator_nodes) # type: ignore
        new_set = set(new_stable_locator_nodes)
        result = old_set <= new_set # 새 집합에 기존 로케이터들이 전부 포함되어있으면 허용
        # print(result)

        #같다면 로케이터들 갱신
        if result:
            new_map = {n:n.locator for n in new_stable_locator_nodes}
            for stable_node in self._stable_nodes:
                stable_node.locator = new_map[stable_node]
            for locator_node in self.locator_nodes:
                locator_node.locator = new_map[locator_node]
        return result
    @classmethod
    async def create(cls, 
                     frame:PlaywrightFrame, 
                     locator_configs:LocatorConfig, 
                     stable_configs:StabilityConfig):
        """
        LocatorManager을 생성,초기화
        """

        stable_nodes = await cls._extract_stable(
            frame,
            stable_configs.timeout,stable_configs.stable_ms,
            locator_configs.selector_include,locator_configs.keyword_forbidden
        )
        clickable_nodes = await cls._filter_clickable(stable_nodes)

        stable_nodes = await cls._locators_to_locatorNodes(stable_nodes)
        clickable_nodes = await cls._locators_to_locatorNodes(clickable_nodes)

        return LocatorManager(stable_nodes,clickable_nodes,
                              locator_configs.selector_include, locator_configs.keyword_forbidden, 
                              stable_configs.timeout, stable_configs.stable_ms, stable_configs.interval)
    
class Context:

    playwright: Playwright | None = None
    browser: Browser | None = None
    ref_cnt: int = 0

    @classmethod
    async def create(cls, session_path: str | None) -> "Context":
        if Context.playwright is None:
            Context.playwright = await async_playwright().start()
        if Context.browser is None:    
            Context.browser = await Context.playwright.chromium.launch(headless=False, args=[
                '--disable-features=Translate',
                '--disable-translate',
            ],)
            # Context.browser = await Context.playwright.chromium.launch(headless=True)

        # 세션 파일이 존재하나 확인
        if session_path is not None:
            try:
                with open(session_path, "rb") as f: pass
            except FileNotFoundError:
                # 없으면 None으로
                session_path = None

        # context 생성
        Context.ref_cnt += 1
        assert Context.browser is not None
        context = await Context.browser.new_context(
            storage_state=session_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
        return Context(context, session_path)

    def __init__(self, context: BrowserContext, session_path: str | None):
        self.context: BrowserContext = context           
        self.session_path: str | None = session_path # 세션경로 
        self.pages: list[Page] = []                  # 자식페이지들
    
    async def new_page(self) -> "Page":
        # page객체를 반환함
        n_page = Page(await self.context.new_page())
        self.pages.append(n_page)
        return n_page

    async def reload(self, restore_pages: bool = True) -> None:
        # 세션을 새로고침
        # restore_pages: page복원여부
        await self.context.close()
        assert Context.browser is not None
        self.context = await Context.browser.new_context(
            storage_state=self.session_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
        # 복원시
        if restore_pages:
            for page in self.pages:
                await page.restore(await self.context.new_page())
        # 복원x
        else:
            self.pages = []
    async def close(self) -> None:
        await self.context.close()
        Context.ref_cnt -= 1
        if Context.ref_cnt == 0:
            assert Context.browser is not None
            assert Context.playwright is not None
            await Context.browser.close()
            await Context.playwright.stop()
    async def save_session(self)->None:
        # 세션저장
        await self.context.storage_state(path=self.session_path)

class Page:
    # 페이지를 나타냄
    def __init__(self, page: PlaywrightPage):
        self.page: PlaywrightPage       = page
        self.records: list[Page_State] = []     # 페이지 이동기록을 나타냄, goto로 이동시 초기화됨
        self.command_buffer:list["Command"] = []
    def _get_current_state(self) -> "Page_State":
        """현재 상태를 반환"""
        return self.records[-1]
    def _append_command(self, command: "Command"):
        """버퍼에 나중에 실행할 명령을 추가함"""
        self.command_buffer.append(command)
    def _pop_command(self):
        """버퍼에 실행할 명령중 마지막을 제거함"""
        if len(self.command_buffer) > 0:     # 현재 record가 비어있다면
            self.command_buffer.pop()
    @classmethod
    async def _extract_stable_frames(cls,page:PlaywrightPage, _timeout, _stable_ms)->list[PlaywrightFrame]:
        """
        안정된 프레임들을 추출합니다.
        """    
        await page.wait_for_load_state('domcontentloaded')
        last_frames:list[Pair] = [Pair(id(f),False) for f in page.frames]
        refs = {f.first:f.second for f in last_frames} # 프레임객체가 gc안되게 잡아둘 딕셔너리
        start_time = time.time()
        stable_start: float | None = None

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > _timeout:
                print(f"[!] _extract_stable_frames: 시간 초과(gap={gap}) (현재 상태로 그냥 진행)", file=sys.stderr)
                break
            try:
                f_temp = page.frames
                current_frames = [id(f) for f in f_temp]
                for i in range(len(current_frames)):
                    refs[current_frames[i]] = f_temp[i]
                next_frames:list[Pair] = []
                sm = difflib.SequenceMatcher(a=[p.first for p in last_frames], b=current_frames, autojunk=False)
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        for i in range(i2-i1):
                            if last_frames[i1+i].second == True: # 없어졌다가 다시 생긴 프레임은 제외
                                next_frames.append(last_frames[i1+i])
                                continue
                            # 계속 있던 프레임들은 그대로 추가
                            next_frames.append(Pair(current_frames[j1+i],False))
                        continue
                    if tag == "delete":
                        for ln in last_frames[i1:i2]:
                            ln.second = True # 없어졌다는 표시...
                        next_frames += last_frames[i1:i2]
                        continue
                    if tag == "insert": # 처음보는경우(생성된 경우는 일단 추가)
                        next_frames += [Pair(n,False) for n in current_frames[j1:j2]]
                        continue
                    if tag == "replace":
                        next_frames += [Pair(n,False) for n in current_frames[j1:j2]] # 새로생긴거는 추가
                        for ln in last_frames[i1:i2]:               # 없어진거는 없어진거를 표시
                            ln.second = True # 없어졌다는 표시...
                        next_frames += last_frames[i1:i2]
                        continue

                if next_frames == last_frames:
                    if stable_start is None:
                        stable_start = time.time()
                    # 2. 지정된 시간(예: 500ms) 동안 변화가 없었다면 조건 충족
                    if (time.time() - stable_start) * 1000 >= _stable_ms:
                        break  # 완전히 안정화됨
                else:
                    # 변화가 생겼다면 타이머를 초기화하고 최신 상태를 기록
                    stable_start = None
                    last_frames = next_frames
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)
        return [refs[i.first] for i in last_frames if not i.second]
    async def _goto_page_state(self, record_idx: int|None, state_idx: int|None):
        """record내 특정 페이지 상태로 이동합니다, 이때 명령버퍼는 초기화 됩니다."""
        # idx범위검사
        if record_idx == None: # None이면 그냥 마지막으로 초기화
            record_idx = len(self.records)-1
        if record_idx < 0 or record_idx >= len(self.records):
            raise ValueError(f"given record_idx is out of range: given:{record_idx}, available_range:{0}~{len(self.records)}")
        if state_idx == None: # None이면 그냥 마지막으로 초기화
            state_idx = len(self.records[record_idx].command_frames)-1
        if state_idx < 0 or state_idx >= self.records[record_idx].len_commands:
            raise ValueError(f"given state_idx is out of range: given:{state_idx}, available_range:{0}~{self.records[record_idx].len_commands}")
        # idx범위로 자름
        self.records = self.records[:record_idx+1]
        cur_state = self._get_current_state()
        cur_state.cut_at_idx(state_idx)
        # 상태복원
        await self.page.goto(cur_state.page_url) # 해당 페이지 상태의 url로 이동
        configs = StabilityConfig()
        stable_frames = await Page._extract_stable_frames(self.page, configs.timeout, configs.stable_ms)
        for pair in cur_state.command_frames:
            # 먼저 해당 상태의 프레임들 복원
            for frame_idx in range(len(pair.second)):
                if not await pair.second[frame_idx].check_and_refresh(self.page.frames[frame_idx]):
                    raise Exception("Page._goto_page_state: something wrong while refreshing frames..")
            # 복원한 프레임들에서 명령실행
            if pair.first is not None:
                await pair.first.do(pair.second)
        # 복원 후 명령버퍼 초기화
        self.command_buffer = []
    async def _apply_commands_in_buffer(self):
        """커맨드 버퍼내에 명령들을 적용시킵니다. 중간에 실패하면 그 뒤의 커맨드들은 무시됩니다. 성공한 커맨드와, 상태는 record에 기록됩니다."""
        configs = StabilityConfig()
        # 현재 상태를 가져옴
        current_state = self._get_current_state()
        # 버퍼가 비어있는경우 바로 탈출
        if len(self.command_buffer) <= 0: return
        # 커맨드를 적용
        for command in self.command_buffer:
            current_frames = current_state.get_current_state().second # 현재 상태의 frame들
            await command.do(current_frames)
            # 프레임 대기
            stable_frames = await Page._extract_stable_frames(self.page, configs.timeout, configs.stable_ms)
            # url이 변한경우: 새로운 state를 리스트에 붙여줌
            if self.page.url != current_state.page_url:
                new_state = Page_State(
                    [await Frame.create(frame) for frame in stable_frames],
                    command,
                    self.page.url,
                    await self.page.title(),
                )
                self.records.append(new_state)
                current_state = self._get_current_state()
                continue
            # 페이지 내용 변화 체크
            is_changed = False
            # print(len(self.page.frames),len(current_frames))
            if len(self.page.frames)!=len(current_frames):
                # 프레임 개수가 변함
                is_changed = True
                print(f"cnt changed: {len(current_frames)}->{len(self.page.frames)}")
            else:
                # 개수는 같은데, 순서 또는 내용이 바뀜
                for frame_idx in range(len(self.page.frames)):
                    is_changed = is_changed or not await current_frames[frame_idx].check_and_refresh(self.page.frames[frame_idx])
            # 변화가 있다면
            print(f"is_changed:{is_changed}")
            if is_changed:
                print(f"frame cnt: {len(self.page.frames)}")
                current_state.append_state(command,[await Frame.create(frame) for frame in self.page.frames]) # 해당 커맨드와 결과 프레임상태를 저장
                continue
            # 아무변화도 없다면 중단
            break
        self.command_buffer = []
    async def goto(self, url: str) -> None:
        # 페이지 이동
        await self.page.goto(url, wait_until="domcontentloaded")
        configs = StabilityConfig()
        # 프레임 대기
        stable_frames = await Page._extract_stable_frames(self.page, configs.timeout, configs.stable_ms)
        self.records = [] # 기록 초기화
        self.records.append(
            Page_State(
                [await Frame.create(frame) for frame in stable_frames], 
                None,
                self.page.url,
                await self.page.title(),
            )
        )
        self.current_pos = 0
    async def click_locator(self, frame_idx: int, locator_idx: int) -> None:
        self._append_command(Click(frame_idx, [locator_idx]))
        await self._apply_commands_in_buffer()
    async def fill_locators(
        self, frame_idxs: list[int], locator_idxs: list[int], 
        contents: list[str], last_is_submit: bool = False
    ) -> None:
        require_contents_len = len(locator_idxs) - int(last_is_submit)
        if require_contents_len != len(contents):
            raise ValueError(
                f"length of [locator_idxs] and [contents] should be same: currently {require_contents_len}|{len(contents)}"
            )
        self._append_command(Fill(frame_idxs, locator_idxs, last_is_submit=last_is_submit, contents=contents))
        await self._apply_commands_in_buffer()
    async def get_page_info(self) -> "PageInfo":
        if not self.records:
            return PageInfo("", "", [])
        record = self._get_current_state()
        return PageInfo(
            record.page_url,
            await self.page.title(),
            [frame.get_frame_info() for frame in record.get_current_state().second]
        )
    async def get_raw_content(self) -> str:
        return await recursive_iframe_replace(self.page.main_frame)
    async def undo(self):
        dest_record_idx = len(self.records)-1
        dest_state_idx  = len(self._get_current_state().command_frames)-2
        if dest_state_idx < 0:
            dest_record_idx -= 1
            dest_state_idx  = None
        await self._goto_page_state(dest_record_idx, dest_state_idx)
    async def restore(self, page:PlaywrightPage):
        """새로운 페이지 객체로 교체후, 상태를 복구함"""
        self.page = page
        await self._goto_page_state(None,None)
    async def locator(self, path: str, frame_idx: int) -> "LocatorNode":
        return await self._get_current_state().get_current_state().second[frame_idx].locator(self.page.frames[frame_idx],path)
        
class Page_State:
    def __init__(self, init_frames: list["Frame"], init_command: Command|None, page_url: str, page_title: str, ):
        self.page_url: str              = page_url
        self.page_title: str            = page_title
        self.command_frames: list[Pair] = [Pair(init_command,init_frames)]# 첫번째는 command,두번째는 해당커맨드 했을때 프레임들
    def append_state(self, command: "Command", frames: list["Frame"]) -> None:
        """커맨드와 해당커맨드에대한 결과상태를 추가함"""
        self.command_frames.append(Pair(command, frames))
    def pop_last_state(self):
        """마지막 상태를 제거함"""
        if self.len_commands <= 1:
            raise Exception("제거할 상태가 없습니다.")
        self.command_frames.pop()
    def get_current_state(self)->Pair:
        """현재(마지막) 상태를 반환"""
        return self.command_frames[-1]
    def cut_at_idx(self, idx: int):
        """command_idx까지 남기고 잘라냄, 뒷부분 버림"""
        if self.len_commands <= idx:
            raise Exception("주어진 idx가 현재 최대 인덱스보다 큽니다.")
        self.command_frames = self.command_frames[:idx+1]
    @property
    def len_commands(self):
        return len(self.command_frames)
    
class Command(ABC):
    def __init__(self, frame_idxs: int | list[int], locator_idxs: int | list[int], **kwargs: Any):
        """
        frame_idxs: 각 locator가 위치한 프레임의 인덱스
        locator_idxs: 각 locator의 프레임내 인덱스
        """
        super().__init__()
        if isinstance(locator_idxs, int):
            locator_idxs = [locator_idxs]
        if isinstance(frame_idxs, int):
            frame_idxs = [frame_idxs]
        self.frame_idxs: list[int] = frame_idxs
        self.locator_idxs: list[int] = locator_idxs
        self.kwargs: dict[str, Any] = kwargs
        self.action: str | None = None
    @abstractmethod
    async def _do(self, target:"LocatorNode") -> None:
        pass
    @abstractmethod
    async def _check(self) -> bool:
            pass
    async def do(self, frames: list["Frame"]) -> None:
        if not await self._check():
            raise Exception(f"check fail at command {self.action}")
        for f_idx,l_idx in zip(self.frame_idxs,self.locator_idxs):
            frame = frames[f_idx]
            await self._do((frame.locator_manager.locator_nodes or [])[l_idx])
class Click(Command):
    def __init__(self, frame_idxs: int|list[int], locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idxs, locator_idxs, **kwargs)
        self.action = "click"
    @override
    async def _check(self) -> bool:
        if len(self.locator_idxs) != len(self.frame_idxs):
            return False
        if len(self.locator_idxs) > 1 or len(self.frame_idxs) > 1:
            return False
        return True
    @override
    async def _do(self, target:"LocatorNode") -> None:
        await target.click()
class Fill(Command):
    def __init__(self, frame_idxs: int|list[int], locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idxs, locator_idxs, **kwargs)
        self.action = "fill"
        self.cur_idx = 0
    @override
    async def _check(self) -> bool:
        if len(self.locator_idxs) != len(self.frame_idxs):
            return False
        return True
    @override
    async def _do(self, target:"LocatorNode") -> None:
        # fill또는 제출버튼 click중 선택
        cur_job = lambda : target.fill(self.kwargs['contents'][self.cur_idx]) # fill
        if self.kwargs.get('last_is_submit'):
            if self.cur_idx == len(self.locator_idxs)-1:
                cur_job = lambda : target.click() # click
        # 선택한 명령 실행
        await cur_job()
        # 인덱스 증가 또는 초기화
        self.cur_idx += 1
        if self.cur_idx >= len(self.locator_idxs):
            self.cur_idx = 0

class Frame:
    # iframe+메인프레임을 나타냄, 상태를 저장하고 있다가, frame을 받으면 복구함
    @classmethod
    async def create(cls, frame: PlaywrightFrame) -> "Frame":
        # 초기화
        locator_manager = await LocatorManager.create(frame, LocatorConfig(), StabilityConfig())
        return Frame(frame.url, frame.name, locator_manager)
    def __init__(self, init_url:str,init_name:str, locator_manager:LocatorManager):
        self.locator_manager:LocatorManager   = locator_manager
        self.init_url: str = init_url                # 프레임의 주소
        self.init_name:str = init_name
    async def check_and_refresh(self, frame: PlaywrightFrame)->bool: # 유효하면(refresh됬으면) True
        """
        frame이 처음과 동일한지 검사후, 동일하다면 갱신후 True반환함
        """
        new_url = frame.url
        if new_url != self.init_url: # url이 바뀌었으면
            return False
        return await self.locator_manager.check_frame_and_refresh(frame)
    def get_frame_info(self) -> "FrameInfo":
        return FrameInfo(self.init_url, self.init_name, self.locator_manager.locator_nodes or []) # 마지막 로케이터들 반환
    async def locator(self, frame:PlaywrightFrame, path: str) -> "LocatorNode":
        return await LocatorNode.create(frame.locator(path))
            

class LocatorNode:
    def __init__(self, tag: str, text: str, value: str, placeholder: str, href: str, locator: PlaywrightLocator):
        self.tag: str = tag
        self.text: str = text
        self.value: str = value
        self.placeholder: str = placeholder
        self.href: str = href
        self.locator: PlaywrightLocator = locator
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, LocatorNode):
            return NotImplemented
        return self.values() == other.values()
    @override
    def __hash__(self) -> int:
        return hash(str(self.values()))

    @classmethod
    async def create(cls, locator: PlaywrightLocator) -> "LocatorNode":
        infos = await locator.evaluate(
            """
            el => ({
                tag: el.tagName,
                text: (el.innerText || "").trim(),
                value: el.value || "",
                placeholder: el.placeholder || "",
                href: el.href || ""
            })
            """
        )
        return LocatorNode(**infos, locator=locator)
    async def click(self) -> None:
        await self.locator.click(timeout=5000)
    async def fill(self, content: str) -> None:
        await self.locator.fill(content)
    @classmethod
    def keys(cls) -> list[str]:
        return ["tag", "text", "value", "placeholder", "href"]
    def values(self) -> list[str]:
        return [self.tag, self.text, self.value, self.placeholder, self.href]

@dataclass
class FrameInfo:
    # 프레임의 정보를 나타냄
    url: str
    name: str
    locator_nodes: list[LocatorNode]
@dataclass
class PageInfo:
    # 페이지의 정보를 나타냄
    url: str
    title: str
    frameInfos: list[FrameInfo]

class PageAnalyzer:
    def __init__(self, page: Page):
        self.page: Page = page

    async def get_post_processed_content(self) -> str:
        return clean_html(await self.page.get_raw_content())
    async def print_page_info(self) -> str:
        page_info = await self.page.get_page_info()
        frame_infos = page_info.frameInfos
        result = [
            f"Page URL: {page_info.url}",
            f"Page Title: {page_info.title}",
            f"Frame Count: {len(frame_infos)}",
            f"Locator Header: {"|".join((["index"] + LocatorNode.keys()))}",
            ""
        ]
        for i, frameInfo in enumerate(frame_infos):
            result_frame = [f"Frame URL: {frameInfo.url}",]
            for j, locator_node in enumerate(frameInfo.locator_nodes):
                line = ", ".join(locator_node.values())
                result_frame.append(
                    f"  [{j}] {line}"
                )
            result_frame.append("")
            result_frame_str = "\n".join(result_frame)
            result.append(
                f"[Frame {i}]\n{result_frame_str}"
            )
        return "\n".join(result)

    async def get_text(self) -> None:
        pass    

    async def get_links(self) -> None:
        pass

    async def get_forms(self) -> None:
        pass