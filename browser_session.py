from __future__ import annotations
import asyncio
import time
from playwright.async_api import async_playwright, Page as PlaywrightPage, Frame as PlaywrightFrame, Locator as PlaywrightLocator, Playwright, Browser, BrowserContext
from .html_cleaner import clean_html, recursive_iframe_replace
from dataclasses import dataclass
from typing import Callable, Any, override
from abc import ABC, abstractmethod

"""
기본 구성 클래스들
"""

async def default_tag_extractor(frame: PlaywrightFrame) -> PlaywrightLocator:
    """프레임에서 클릭 가능한 후보 요소들을 뽑아줍니다."""

    forbidden_keywords = [
        "로그아웃", "logout", "signout", "exit", "나가기",
        "비밀번호 변경", "회원탈퇴", "delete account"
    ]

    selectors = [
        "a",
        "li",
        "span",
        "div",
        "p",
        "button",
        "input",
        "textarea",
        "select"
    ]

    selector_str = ", ".join(selectors)

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

                // 실제 상호작용 가능한 요소인지 판별
                const isStandardInteractive = el.matches("a, button, input, textarea, select");
                const isClickableRole = el.getAttribute("role") === "button" || el.getAttribute("onclick") !== null;
                const hasPointer = window.getComputedStyle(el).cursor === "pointer";

                if (isStandardInteractive || isClickableRole || hasPointer) {
                    el.classList.add(
                        "mcp-clickable-target"
                    );
                }
            });
        }
        """,
        {
            "sel": selector_str,
            "keywords": forbidden_keywords,
        }
    )

    return (
        frame
        .locator(".mcp-clickable-target")
        .filter(visible=True)
    )

class Base:
    async def wait_dom_stable(self, target: PlaywrightPage | PlaywrightFrame, timeout: int = 5000, stable_ms: int = 500) -> None:
        """이벤트 및 상호작용 가능한 요소들의 구조가 안정화될 때까지 대기합니다."""
        start_time = time.time()
        last_sig = ""
        stable_start: float | None = None

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > timeout:
                return
            try:
                # 상호작용 가능한 요소들의 태그, 위치, 개수 등의 시그니처만 추출하여 비교
                current_sig: str = await target.evaluate("""
                    () => {
                        const els = document.querySelectorAll("a, button, input, textarea, select, [role='button']");
                        let sig = "";
                        els.forEach(el => {
                            sig += `${el.tagName}:${el.id}:${el.className};`;
                        });
                        return sig;
                    }
                """)
                if current_sig == last_sig:
                    if stable_start is None:
                        stable_start = time.time()
                    if (time.time() - stable_start) * 1000 >= stable_ms:
                        return
                else:
                    stable_start = None
                    last_sig = current_sig
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(0.1)

    async def is_interactable(self, locator: PlaywrightLocator) -> bool:
        """요소가 화면에 실제로 보이는 크기를 가지는지 확인합니다."""
        try:
            box = await locator.bounding_box()
            if box is None:
                return False
            return box["width"] >= 3 and box["height"] >= 3
        except Exception:
            return False

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
    
    async def new_page(self, tag_extractor: Callable[[PlaywrightFrame], Any] = default_tag_extractor) -> "Page":
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

    async def save_session(self):
        # 세션저장
        await self.context.storage_state(path=self.session_path)

class Page(Base):
    # 페이지를 나타냄
    def __init__(self, page: PlaywrightPage, tag_extractor: Callable[[PlaywrightFrame], Any]):
        self.page: PlaywrightPage          = page
        self.tag_extractor: Callable[[PlaywrightFrame], Any] = tag_extractor
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
    async def _do(self, targets: list["LocatorNode"]) -> None:
        if len(targets) > 1:
            raise ValueError("can click only one element at time")
        target = targets[0]
        await target.click()

class Fill(Command):
    def __init__(self, frame_idx: int, locator_idxs: int | list[int], **kwargs: Any):
        super().__init__(frame_idx, locator_idxs, **kwargs)
        self.action = "fill"
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
    async def create(cls, frame: PlaywrightFrame, tag_extractor: Callable[[PlaywrightFrame], Any]) -> "Frame":
        temp = Frame(frame, tag_extractor)
        # 초기화
        await temp.update_locator_nodes()
        return temp
    async def update_locator_nodes(self) -> None:
        nodes = await self.tag_extractor(self.frame)
        count = await nodes.count()
        self.locator_nodes = [await LocatorNode.create(nodes.nth(i)) for i in range(count)]
    def __init__(self, frame: PlaywrightFrame, tag_extractor: Callable[[PlaywrightFrame], Any]):
        self.frame: PlaywrightFrame                     = frame
        self.tag_extractor: Callable[[PlaywrightFrame], Any]             = tag_extractor
        self.locator_nodes: list[LocatorNode]      = []         # tag_extractor로 추출한 locators, 인덱스로 접근함
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