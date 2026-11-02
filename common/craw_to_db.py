from common.schedule_db import add_schedule
from my_scrapper.scrapper import MiguScrapper     # 크롤러 인터페이스
from my_scrapper.storage import ExtractedSchedule # 크롤링된 일정
from my_scrapper.storage import FilteredUrl # url 정보

"""크롤러의 db에 수집된 일정을 통합db로 옮기는 코드"""

def import_schedules_from_crawler_db_to_common_db(scrapper: MiguScrapper, path:str)->list[int]:
    """
    크롤러 db에 추출되어 있는 일정을 통합db로 옮기는 코드
        scrapper: 크롤러 db와 연결된 크롤러
        path: 통합db의 경로
    출력:
        옮겨진 일정들의 id
    """
    # url로부터 페이지 제목 매핑해놓음
    url_list = scrapper.get_filtered_urls()
    url_title_dict = {}
    inserted_ids:list[int] = []
    for i in range(len(url_list)):
        cur_url   = url_list[i].url
        cur_title = url_list[i].title
        url_title_dict[cur_url] = cur_title
    # 스케듈을 크롤러로 부터 받아옴
    schedules = scrapper.get_schedules()
    # 각 스케듈에 대해서 add_schedule 호출
    for schedule in schedules:
        # 필드 찾기
        url = schedule.url
        title = url_title_dict[url] or url # 제목이 없으면 그냥 url사용
        content = schedule.summary
        due_at = schedule.end_date
        start_at = schedule.start_date
        source = 'crawl'
        source_id = schedule.id
        # 넣기
        inserted_ids.append(add_schedule(path, title, content, due_at, start_at, url, source, source_id))
    return inserted_ids