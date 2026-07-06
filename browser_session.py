from __future__ import annotations
import asyncio
import time, difflib
from playwright.async_api import async_playwright, Page as PlaywrightPage, Frame as PlaywrightFrame, Locator as PlaywrightLocator, Playwright, Browser, BrowserContext
import sys
from .html_cleaner import clean_html, recursive_iframe_replace
from dataclasses import dataclass
from typing import Callable, Any, override
from abc import ABC, abstractmethod

"""
기본 구성 클래스들
"""
class TagExtractor:
    def __init__(self,includes:list[str]|None=None,excludes:list[str]|None=None):
        self.selectors:list[str] = includes or ["a","li","span","div","p","button","input","textarea","select"]
        self.forbidden_keywords:list[str] = excludes or ["로그아웃", "logout", "signout", "exit", "나가기","비밀번호 변경", "회원탈퇴", "delete account"]
    async def extract(self,frame:PlaywrightFrame)->list[PlaywrightLocator]:
        """프레임에서 클릭 가능한 후보 요소들을 뽑아줍니다."""
        selector_str = ", ".join(self.selectors)
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
                "keywords": self.forbidden_keywords,
            }
        )

        return (
            await frame
            .locator(".mcp-clickable-target")
            .filter(visible=True)
            .all()
        )
class NotCoveredTagExtractor(TagExtractor):
    def __init__(self,includes:list[str]|None=None,excludes:list[str]|None=None):
        super().__init__(includes,excludes)
    async def _filter_visible(self, locators:list[PlaywrightLocator])->list[PlaywrightLocator]:
        """html에 존재하는 요소중 실제 클릭가능한 요소만 필터링 합니다"""
        visibles:list[PlaywrightLocator] = []
        for l in locators:
            try: await l.click(trial=True, timeout=300) # 실제 클릭가능한지 시도해봄
            except: continue
            visibles.append(l)
        return visibles
    @override
    async def extract(self,frame:PlaywrightFrame)->list[PlaywrightLocator]:
        return await self._filter_visible(await super().extract(frame))
class LocatorManager:
    def __init__(self, selector_include:list[str]|None, keyword_forbidden:list[str]|None, timeout: int = 5000, stable_ms: int = 500, interval: float = 0.2):
        self.selector_include:list[str] = selector_include or ["a","li","span","div","p","button","input","textarea","select"]
        self.keyword_forbidden:list[str] = keyword_forbidden or ["로그아웃", "logout", "signout", "exit", "나가기","비밀번호 변경", "회원탈퇴", "delete account"]
        self.locator_nodes: list[LocatorNode]|None = None
        self.timeout: int = timeout
        self.stable_ms: int = stable_ms
        self.interval: float = interval
    async def _extract(self,frame:PlaywrightFrame)->list[PlaywrightLocator]:
        """프레임에서 후보 요소들을 뽑아줍니다."""
        selector_str = ", ".join(self.selector_include)
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
                "keywords": self.keyword_forbidden,
            }
        )
        return (
            await frame
            .locator(".mcp-clickable-target")
            .filter(visible=True)
            .all()
        )
    async def _extract_stable(self,frame:PlaywrightFrame)->list[PlaywrightLocator]:
        start_time = time.time()
        last_locator_nodes:list[LocatorNode] = []
        stable_start: float | None = None

        await frame.wait_for_load_state('domcontentloaded')
        last_locator_nodes = [await LocatorNode.create(l) for l in await self._extract(frame)]

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > self.timeout:
                print("[!] wait_dom_stable: 시간 초과 (현재 상태로 그냥 진행)", file=sys.stderr)
                break
            try:
                current_locator_nodes = [await LocatorNode.create(l) for l in await self._extract(frame)]
                next_locator_nodes:list[LocatorNode] = []
                sm = difflib.SequenceMatcher(a=last_locator_nodes, b=current_locator_nodes, autojunk=False)
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        for i in range(i2-i1):
                            if last_locator_nodes[i1+i].locator == None: # 없어졌다가 다시 생긴 노드는 제외
                                next_locator_nodes.append(last_locator_nodes[i1+i])
                                continue
                            # 계속 있던 노드들은 그대로 추가
                            next_locator_nodes.append(current_locator_nodes[j1+i])
                        continue
                    if tag == "delete":
                        for ln in last_locator_nodes[i1:i2]:
                            ln.locator = None # 없어졌다는 표시...
                        next_locator_nodes += last_locator_nodes[i1:i2]
                        continue
                    if tag == "insert": # 처음보는경우(생성된 경우는 일단 추가)
                        next_locator_nodes += current_locator_nodes[j1:j2]
                        continue
                    if tag == "replace":
                        next_locator_nodes += current_locator_nodes[j1:j2] # 새로생긴거는 추가
                        for ln in last_locator_nodes[i1:i2]:               # 없어진거는 없어진거를 표시
                            ln.locator = None # 없어졌다는 표시...
                        next_locator_nodes += last_locator_nodes[i1:i2]
                        continue

                if next_locator_nodes == last_locator_nodes:
                    if stable_start is None:
                        stable_start = time.time()
                    # 2. 지정된 시간(예: 500ms) 동안 변화가 없었다면 조건 충족
                    if (time.time() - stable_start) * 1000 >= self.stable_ms:
                        break  # 완전히 안정화됨
                else:
                    # 변화가 생겼다면 타이머를 초기화하고 최신 상태를 기록
                    stable_start = None
                    last_locator_nodes = next_locator_nodes
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)
        return [i.locator for i in last_locator_nodes if i.locator != None]
    async def _filter_clickable(self, locators:list[PlaywrightLocator])->list[PlaywrightLocator]:
        """html에 존재하는 요소중 실제 클릭가능한 요소만 필터링 합니다"""
        clickable:list[PlaywrightLocator] = []
        for l in locators:
            try: await l.click(trial=True, timeout=300) # 실제 클릭가능한지 시도해봄
            except: continue
            clickable.append(l)
        return clickable
    async def update_locators(self, frame: PlaywrightFrame):
        locators: list[PlaywrightLocator]|list[LocatorNode]
        # 처음만들때
        if self.locator_nodes == None:
            locators = await self._extract_stable(frame)      # 변하지 않는 로케이터들만 선택함
            locators = await self._filter_clickable(locators) # 처음이므로 일일히 클릭가능한지 확인해줌
            self.locator_nodes = [await LocatorNode.create(l) for l in locators]
        else:
            locators = [await LocatorNode.create(l) for l in await self._extract(frame)]
            old_set = set(self.locator_nodes) # 기존에 구해놓은거를 활용,이것만 다시 구하면됨
            new_set = set(locators)
            while not old_set.issubset(new_set):
                # 다시 추출 시도
                locators = [await LocatorNode.create(l) for l in await self._extract(frame)]
                old_set = set(self.locator_nodes)
                new_set = set(locators)
                await asyncio.sleep(self.interval)
            new_map = {node: node for node in locators}
            for old_node in self.locator_nodes:
                new_node = new_map.get(old_node)
                if new_node:
                    old_node.locator = new_node.locator
            
class Base:
    async def wait_dom_stable(self, target: PlaywrightPage | PlaywrightFrame, timeout: int = 5000, stable_ms: int = 500) -> None:
        """DOM 내용과 아이프레임 개수가(페이지일 경우) 모두 멈출 때까지 대기합니다."""
        # 수정필요!!!! 광고,시간같이 계속 변하는경우 어떻게 처리할지
        start_time = time.time()
        last_html = ""
        last_frame_count = -1
        stable_start: float | None = None
        counter: Callable[[Any], int]

        if hasattr(target, "frames"):
            counter = lambda t: len(t.frames)
        else:
            counter = lambda t: len(t.child_frames)

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > timeout:
                print("[!] wait_dom_stable: 시간 초과 (현재 상태로 그냥 진행)", file=sys.stderr)
                return
            try:
                current_html = await target.content()
                current_frame_count = counter(target)
                # 1. 메인 HTML 내용과 프레임 개수가 '모두' 이전 루프와 똑같은지 확인
                if current_html == last_html and current_frame_count == last_frame_count:
                    if stable_start is None:
                        stable_start = time.time()
                    # 2. 지정된 시간(예: 500ms) 동안 변화가 없었다면 조건 충족
                    if (time.time() - stable_start) * 1000 >= stable_ms:
                        return  # 완전히 안정화됨
                else:
                    # 변화가 생겼다면 타이머를 초기화하고 최신 상태를 기록
                    stable_start = None
                    last_html = current_html
                    last_frame_count = current_frame_count
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)

class Context:
    # context를 나타냄

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
    
    async def new_page(self, tag_extractor:TagExtractor) -> "Page":
        # page객체를 반환함
        # tag_extractor: Page에서 쓸 tag추출기
        n_page = Page(await self.context.new_page(), tag_extractor)
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
                page.page = await self.context.new_page() # page다시 넣어주고
                await page._restore_page_state() # 넣어준 page를 저장되있는 상태로 복구
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

class Page(Base):
    # 페이지를 나타냄
    def __init__(self, page: PlaywrightPage, tag_extractor:TagExtractor):
        self.page: PlaywrightPage          = page
        self.tag_extractor:TagExtractor = tag_extractor
        self.records: list[Page_Record] = []     # 페이지 이동기록을 나타냄, goto로 이동시 초기화됨
        self.current_pos: int = 0                       # records[-1]에서 현재 상태 위치

    def _get_current_record(self) -> "Page_Record":
        return self.records[-1]

    def _append_command(self, command: "Command") -> None:
        self._get_current_record().append_command(command)

    def _pop_command(self) -> None:
        cur_record = self._get_current_record()
        cur_record.pop_last_command()
        if len(cur_record.commands) <= 0:
            self.records = self.records[:-1]
            self.current_pos = -1 # 현재 record가 제거됨을 알림

    def _goto_command(self, record_idx: int, command_idx: int) -> None:
        if record_idx < 0 or record_idx >= len(self.records):
            raise ValueError(f"given record_idx is out of range: given:{record_idx}")
        if command_idx < 0 or command_idx >= len(self.records[record_idx].commands):
            raise ValueError(f"given command_idx is out of range: given:{command_idx}")
        cur_record_idx = len(self.records)-1
        if record_idx < cur_record_idx:
            self.current_pos = -1
            self.records = self.records[:record_idx+1]
        last_record = self._get_current_record()
        last_record.commands = last_record.commands[:command_idx+1]
            
    async def _sync_page_state(self) -> None:
        # records와 상태를 동기화 시킴
        ## 마지막 기록을 가져옴
        last_record = self.records[-1]
        last_command_pos = len(last_record.commands)-1

        if self.current_pos == last_command_pos:
            # 변경사항이 없는경우
            return
        if self.current_pos > last_command_pos or self.current_pos < 0:
            # 더 작은경우(되돌아가야되는 경우)
            await self.page.goto(last_record.url, wait_until="domcontentloaded")  # 돌아가서 다시시작
            await self.wait_dom_stable(self.page)
            self.current_pos = 0
        ## 프레임을 갱신
        last_record.frames = [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames] 
        ## 커맨드를 적용
        commands_to_do = last_record.commands[self.current_pos+1:]
        last_record.commands = last_record.commands[:self.current_pos+1]
        for command in commands_to_do:
            if command is not None:
                await command.do(last_record.frames)
            await self.wait_dom_stable(self.page)
            if self.page.url != last_record.url:
                new_record = Page_Record(
                    [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames],
                    self.page.url,
                    await self.page.title(),
                    [command]
                )
                self.records.append(new_record)
                last_record = self._get_current_record()
                # 새로운 record이므로 command_idx초기화
                continue
            is_effective = False
            for frame in last_record.frames:
                is_effective = is_effective or await frame._update_frame_state()
            if is_effective and command is not None: # 변화를 일으키는 커맨드만 기록
                last_record.commands.append(command)
        self.current_pos = len(last_record.commands)-1

    async def _restore_page_state(self) -> None:
        if not self.records:
            return
        last_record = self.records[-1]
        await self.page.goto(last_record.url, wait_until="domcontentloaded")
        await self.wait_dom_stable(self.page)
        last_record.frames = [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames]
        for command in last_record.commands:
            if command is not None:
                await command.do(last_record.frames)
                await self.wait_dom_stable(self.page)
        self.current_pos = len(last_record.commands) - 1

    async def goto(self, url: str) -> None:
        # 페이지 이동
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.wait_dom_stable(self.page)
        self.records = [] # 기록 초기화
        self.records.append(
            Page_Record( # 새로운 프레임, url, 유발한 커맨드
                [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames], 
                url,
                await self.page.title(),
                [None]
            )
        )
        self.current_pos = 0

    async def click_locator(self, frame_idx: int, locator_idx: int) -> None:
        self._append_command(Click(frame_idx, [locator_idx]))
        await self._sync_page_state()

    async def fill_locators(
        self, frame_idx: int, locator_idxs: list[int], 
        contents: list[str], last_is_submit: bool = False
    ) -> None:
        require_contents_len = len(locator_idxs) - int(last_is_submit)
        if require_contents_len != len(contents):
            raise ValueError(
                f"length of [locator_idxs] and [contents] should be same: currently {require_contents_len}|{len(contents)}"
            )
        self._append_command(Fill(frame_idx, locator_idxs, last_is_submit=last_is_submit, contents=contents))
        await self._sync_page_state()

    async def get_page_info(self) -> "PageInfo":
        if not self.records:
            return PageInfo("", "", [])
        record = self.records[-1]
        return PageInfo(
            record.url,
            await self.page.title(),
            [frame.get_frame_info() for frame in record.frames]
        )

    async def get_raw_content(self) -> str:
        return await recursive_iframe_replace(self.page.main_frame)

    async def rollback(self, record_idx: int, command_idx: int) -> None:
        self._goto_command(record_idx, command_idx)
        await self._sync_page_state()

    async def undo(self) -> None:
        self._pop_command()
        await self._sync_page_state()

    async def locator(self, path: str, frame_idx: int) -> "LocatorNode":
        return await self.records[-1].frames[frame_idx].locator(path)
        
class Page_Record:
    def __init__(self, frames: list["Frame"], url: str, title: str, commands: "list[Command | None]" = []):
        self.frames: list["Frame"] = frames
        self.url: str            = url
        self.title: str          = title
        self.commands: "list[Command | None]" = list(commands)
    def append_command(self, command: "Command") -> None:
        self.commands.append(command)
    def pop_last_command(self) -> "Command | None":
        if self.commands:
            return self.commands.pop()
        return None
    
class Command(ABC):
    def __init__(self, frame_idx: int, locator_idxs: int | list[int], **kwargs: Any):  
        super().__init__()
        self.frame_idx: int = frame_idx
        if isinstance(locator_idxs, int):
            locator_idxs = [locator_idxs]
        self.locator_idxs: list[int] = locator_idxs
        self.kwargs: dict[str, Any] = kwargs
        self.action: str | None = None
    @abstractmethod
    async def _do(self, targets: list["LocatorNode"]) -> None:
        pass
    async def do(self, frames: list["Frame"]) -> None:
        frame = frames[self.frame_idx]
        targets = [
            frame.locator_nodes[i]
            for i in self.locator_idxs
        ]
        await self._do(targets)

class Click(Command):
    def __init__(self, frame_idx: int, locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idx, locator_idxs, **kwargs)
        self.action = "click"
    @override
    async def _do(self, targets: list["LocatorNode"]) -> None:
        if len(targets) > 1:
            raise ValueError("can click only one element at time")
        target = targets[0]
        await target.click()

class Fill(Command):
    def __init__(self, frame_idx: int, locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idx, locator_idxs, **kwargs)
        self.action = "fill"
    @override
    async def _do(self, targets: list["LocatorNode"]) -> None:
        submit: LocatorNode | None = None
        if self.kwargs.get('last_is_submit'):
            submit = targets[-1]
            targets = targets[:-1]
        for i in range(len(targets)):
            await targets[i].fill(self.kwargs['contents'][i])
        if submit:
            await submit.click()

class Frame(Base):
    # iframe+메인프레임을 나타냄
    @classmethod
    async def create(cls, frame: PlaywrightFrame, tag_extractor:TagExtractor) -> "Frame":
        temp = Frame(frame, tag_extractor)
        # 초기화
        await temp.update_locator_nodes()
        return temp
    async def update_locator_nodes(self) -> None:
        nodes = await self.tag_extractor.extract(self.frame)
        # count = await nodes.count()
        self.locator_nodes = [await LocatorNode.create(node) for node in nodes]
    def __init__(self, frame: PlaywrightFrame, tag_extractor:TagExtractor):
        self.frame: PlaywrightFrame           = frame
        self.tag_extractor:TagExtractor       = tag_extractor
        self.locator_nodes: list[LocatorNode] = []         # tag_extractor로 추출한 locators, 인덱스로 접근함
        self.url: str = frame.url                # 프레임의 주소
    async def _update_frame_state(self) -> bool:
        await self.wait_dom_stable(self.frame)
        locator_nodes_old = self.locator_nodes
        await self.update_locator_nodes()
        old_url = self.url
        self.url = self.frame.url

        if old_url != self.url: # url이 바뀌었으면
            return True
        if locator_nodes_old != self.locator_nodes: # 추가,삭제,변화 등을 감지함
            return True
        return False
    def get_frame_info(self) -> "FrameInfo":
        return FrameInfo(self.url, self.frame.name, self.locator_nodes) # 마지막 로케이터들 반환
    async def locator(self, path: str) -> "LocatorNode":
        return await LocatorNode.create(self.frame.locator(path))

class LocatorNode: # Locator + Command
    def __init__(self, tag: str, text: str, value: str, placeholder: str, href: str, locator: PlaywrightLocator):
        self.tag: str = tag
        self.text: str = text
        self.value: str = value
        self.placeholder: str = placeholder
        self.href: str = href
        self.locator: PlaywrightLocator|None = locator
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