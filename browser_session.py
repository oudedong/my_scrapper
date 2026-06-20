import asyncio
import time
from playwright.async_api import async_playwright
import sys
from playwright.async_api import TimeoutError
from .html_cleaner import *
"""
기본 구성 클래스들
"""

###################################
# 새 코드
###################################

async def default_tag_extractor(frame):
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

                const hasText =
                    (el.innerText || "").trim().length > 0;

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
            "keywords": forbidden_keywords,
        }
    )

    return (
        frame
        .locator(".mcp-clickable-target")
        .filter(visible=True)
    )

class Base:
    async def wait_dom_stable(self, target, timeout: int = 5000, stable_ms: int = 500):
        """DOM 내용과 아이프레임 개수가(페이지일 경우) 모두 멈출 때까지 대기합니다."""
        start_time = time.time()
        last_html = ""
        last_frame_count = -1
        stable_start = None
        counter = None

        if hasattr(target, "frames"):
            counter = lambda t: len(t.frames)
        else:
            counter = lambda t: len(t.child_frames)

        while True:
            gap = (time.time() - start_time) * 1000
            if gap > timeout:
                print("[!] wait_dom_stable: 시간 초과 (현재 상태로 진행)", file=sys.stderr)
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

    async def is_interactable(self,locator) -> bool:
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

    playwright = None
    browser     = None

    @classmethod
    async def create(cls, session_path:str):
        if Context.playwright == None:
            Context.playwright = await async_playwright().start()
        if Context.browser == None:    
            Context.browser = await Context.playwright.chromium.launch(headless=False)
            # Context.browser = await Context.playwright.chromium.launch(headless=True)

        # 세션 파일이 존재하나 확인
        if session_path is not None:
            try:
                with open(session_path, "rb") as f: pass
            except FileNotFoundError as e:
                # 없으면 None으로
                session_path = None

        # context 생성
        context = await Context.browser.new_context(
            storage_state=session_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
        return Context(context, session_path)
    def __init__(self, context, session_path:str):
        self.context = context           
        self.session_path = session_path # 세션경로 
        self.pages:list[Page] = []                  # 자식페이지들
    
    async def new_page(self, tag_extractor=default_tag_extractor) -> "Page":
        # page객체를 반환함
        # tag_extractor: Page에서 쓸 tag추출기
        n_page = Page(await self.context.new_page(), tag_extractor)
        self.pages.append(n_page)
        return n_page
    async def reload(self, restore_pages:bool=True):
        # 세션을 새로고침
        # restore_pages: page복원여부
        await self.context.close()
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
    async def save_session(self):
        # 세션저장
        await self.context.storage_state(path=self.session_path)

class Page(Base):
    # 페이지를 나타냄
    def __init__(self, page, tag_extractor):
        self.page          = page
        # self.cur_url       = None
        self.tag_extractor = tag_extractor
        # self.frames:list[Frame] = None           # 페이지내 프레임들 = page.frames
        self.records: list["Page_Record"] = []     # 페이지 이동기록을 나타냄, goto로 이동시 초기화됨
    async def _restore_page_state(self):
        # records로 상태를 동기화 시킴
        ## 마지막 기록을 가져옴
        last_record = self.records[-1]
        ## 먼저 마지막 페이지로 이동
        await self.page.goto(last_record.url, wait_until="domcontentloaded")
        await self.wait_dom_stable(self.page)
        ## 프레임을 갱신
        last_record.frames = [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames]
        ## 각 프레임의 상태를 redo로 복원
        if len(last_record.commands) > 1:
            for command in last_record.commands[1:]:
                await command.do(last_record.frames)
                for frame in last_record.frames:
                    await frame._update_frame_state()
    async def _update_page_state(self, command:"Command"):
        # 마지막 기록을 가져옴
        last_record = self.records[-1]
        # 페이지가 안정화 될때까지 대기
        await self.wait_dom_stable(self.page)
        # url이 바뀌었나 확인 후 바뀌었으면 업데이트
        new_url = self.page.url
        if new_url != last_record.url:
            # 새 상태 추가
            n_frames = [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames]
            self.records.append(
                Page_Record( # 새로운 프레임, url, 유발한 커맨드
                    n_frames, 
                    new_url,
                    [command]
                )
            )
            return True
        # 프레임상태 갱신
        anychange = False
        for frame in last_record.frames:
            anychange = anychange or await frame._update_frame_state()
        if anychange:
            last_record.append_command(command)
        return anychange # 아무변화라도 있는지
    async def _execute_command(self, command:"Command")->bool:
        # 커맨드를 실행하고, 유효한지검사, 유효하면 기록에 추가
        await command.do(self.records[-1].frames) 
        await self._update_page_state(command)

    async def goto(self, url:str):
        # 페이지 이동
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.wait_dom_stable(self.page)
        self.records = [] # 기록 초기화
        self.records.append(
                Page_Record( # 새로운 프레임, url, 유발한 커맨드
                    [await Frame.create(frame, self.tag_extractor) for frame in self.page.frames], 
                    url,
                    [None]
                )
            )
    async def click_locator(self, frame_idx:int, locator_idx:int):
        async def _click_command(targets):
            try:
                await targets[0].click(timeout=5000)
            except TimeoutError as e:
                print(f"클릭 실패", file=sys.stderr)
        click_command = Command(
            _click_command,
            frame_idx,locator_idx
        )
        await self._execute_command(click_command)
    async def fill_locators(
        self,
        frame_idx: int,
        locator_idxs: list[int],
        contents: list[str],
        submit: bool = False
    ):
        async def _fill_command(targets , contents, submit):
            for target, content in zip(targets, contents):
                await target.fill(content)

            if submit:
                await targets[-1].press("Enter")
        if len(locator_idxs) != len(contents):
            raise ValueError(
                "locator_idxs와 contents 길이가 다름"
            )
        fill_command = Command(
            _fill_command,
            frame_idx,
            locator_idxs,
            contents, submit
        )
        await self._execute_command(fill_command)
    def get_page_info(self) -> str:
        if not self.records:
            return "No page loaded"
        record = self.records[-1]
        result = [
            f"Page URL: {record.url}",
            f"Frame Count: {len(record.frames)}",
            ""
        ]
        for i,frame in enumerate(record.frames):
            result.append(
                f"[Frame {i}]\n{frame.get_frame_info()}"
            )
        return "\n".join(result)
    async def rollback(
        self,
        record_idx: int,
        command_idx: int | None = None
    ):
        self.records = self.records[:record_idx+1]

        if command_idx is not None:
            self.records[-1].commands = self.records[-1].commands[:command_idx+1]
        await self._restore_page_state()
    async def undo(self):
        last_record_idx = len(self.records) - 1
        last_record = self.records[-1]

        if len(last_record.commands) > 1:
            await self.rollback(
                last_record_idx,
                len(last_record.commands) - 2
            )
        else:
            await self.rollback(
                last_record_idx - 1
            )
        
class Page_Record:
    def __init__(self, frames, url, commands:list=[]):
        self.frames:list[Frame] = frames
        self.url:str            = url
        self.commands:list["Command"] = commands # 몇번인덱스의 프레임에 뭘 작업했는지를 나타냄, 뭔가 유효한 작업일때만(url변화, dom변화) 기록?
    def append_command(self,command:"Command"):
        #index는 frame의 인덱스를 나타냄
        self.commands.append(command)
    def pop_last_command(self):
        return self.commands.pop()
class Command:
    def __init__(
        self,
        command,
        frame_idx,
        locator_idxs,
        *args
    ):
        self.command = command
        self.frame_idx = frame_idx

        if isinstance(locator_idxs, int):
            locator_idxs = [locator_idxs]

        self.locator_idxs = locator_idxs
        self.args = args
    async def do(self, frames):
        frame = frames[self.frame_idx]

        targets = [
            frame.locators.nth(i)
            for i in self.locator_idxs
        ]

        await self.command(
            targets,
            *self.args
        )

class Frame(Base):
    # iframe+메인프레임을 나타냄
    @classmethod
    async def create(cls, frame, tag_extractor):
        temp = Frame(frame, tag_extractor)
        # 초기화
        temp.locators = await temp.tag_extractor(temp.frame)
        temp.locators_describe_record.append(await temp._describe_locators())
        return temp
    def __init__(self, frame, tag_extractor):
        self.frame = frame
        self.tag_extractor = tag_extractor
        self.locators      = None           # tag_extractor로 추출한 locators, 인덱스로 접근함
        self.locators_describe_record:list[list[str]] = []       # locators각각에 대한 시그니처 저장
        self.url = frame.url                # 프레임의 주소
        # self.clicked_elements_nths = []     # 작업(클릭,입력)기록, 인덱스
    async def _describe_locators(self):
        # 태그명과, 안에 텍스트를 출력함
        count = await self.locators.count()
        locators_describe = [
            await self.locators.nth(j).evaluate(
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
            for j in range(count)
        ]
        return locators_describe
    async def _update_frame_state(self)->bool:
        await self.wait_dom_stable(self.frame)
        locators_describe_old = self.locators_describe_record[-1]
        self.locators = await self.tag_extractor(self.frame) # 일단 locator들은 갱신함
        locators_describe_new = await self._describe_locators()
        new_url = self.frame.url

        if new_url != self.url: # url이 바뀌었으면
            self.url = new_url
            self.locators_describe_record.append(locators_describe_new)
            return True
        for describe in locators_describe_new: # 새로운 locator가 생겼으면
            if describe not in locators_describe_old:
                    self.locators_describe_record.append(locators_describe_new)
                    return True
        self.locators_describe_record[-1] = locators_describe_new
        return False
    def get_frame_info(self) -> str:
        result = [
            f"Frame URL: {self.url}",
            ""
        ]
        for j, desc in enumerate(self.locators_describe_record[-1]):
            result.append(
                f"  [{j}] {desc}"
            )
        result.append("")
        return "\n".join(result)

class PageAnalyzer:
    def __init__(self, page:Page):
        self.page = page
        # self.post_processor = post_processor

    async def get_raw_content(self)->str:
        return await recursive_iframe_replace(self.page.page.main_frame)
    async def get_post_processed_content(self)->str:
        return clean_html(await self.get_raw_content())
    async def get_text(self):
        ...    

    async def get_links(self):
        ...

    async def get_forms(self):
        ...