"""매일 Notion에 체크박스 TODO 페이지를 자동 생성하는 스크립트

페이지 계층:
  ROOT_PAGE_ID ("할일")
    └── {YYYY} 할일  (ensure_year_page가 매년 자동 생성)
          ├── # 할 일  (사용자가 채워두는 yearly 반복 패턴)
          ├── YYYY-MM (월별 sub-page, ensure_month_page가 자동 생성)
          │     └── YYYY-MM-DD TODO (매일 페이지)

콘텐츠 소스:
1. 템플릿 페이지 (TEMPLATE_PAGE_ID) — 기본 카테고리 + 고정 할 일
2. 전날 TODO 페이지 — 미완료 항목 이월
3. {YYYY} 할일 페이지의 "# 할 일" 섹션 — 반복 패턴(매일/요일/날짜지정/빠른시일 등)
4. iCloud 캘린더 — 오늘 일정

3, 4 항목은 Claude API(Sonnet 4.6)가 템플릿 카테고리(업무/개인/공부)로 분류한다.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import anthropic
import caldav

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
ROOT_PAGE_ID = os.environ["ROOT_PAGE_ID"]
TEMPLATE_PAGE_ID = os.environ["TEMPLATE_PAGE_ID"]
APPLE_ID = os.environ.get("APPLE_ID", "")
APPLE_APP_PASSWORD = os.environ.get("APPLE_APP_PASSWORD", "")

KST = timezone(timedelta(hours=9))
API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json; charset=utf-8",
    "Notion-Version": "2022-06-28",
}

WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


# ─────────────────────────── Notion API ───────────────────────────


def api_request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, headers=HEADERS, method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_blocks(page_id: str) -> list[dict]:
    """페이지의 모든 블록을 가져온다 (paginated)."""
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        result = api_request("GET", path)
        blocks.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return blocks


def block_text(block: dict) -> str:
    btype = block.get("type")
    if btype not in block:
        return ""
    return "".join(t.get("plain_text", "") for t in block[btype].get("rich_text", []))


def find_yesterday_page(yesterday: str) -> str | None:
    """전날 TODO 페이지 ID를 검색한다."""
    body = {
        "query": f"{yesterday} TODO",
        "filter": {"value": "page", "property": "object"},
        "page_size": 5,
    }
    result = api_request("POST", "/search", body)
    for page in result.get("results", []):
        title_parts = page.get("properties", {}).get("title", {}).get("title", [])
        title = "".join(t.get("plain_text", "") for t in title_parts)
        if title == f"{yesterday} TODO":
            return page["id"]
    return None


def extract_unchecked_by_category(blocks: list[dict]) -> dict[str, list[str]]:
    """미완료(unchecked) to_do 블록을 카테고리별로 추출한다."""
    result: dict[str, list[str]] = {}
    current_category = ""
    for b in blocks:
        if b.get("type") == "heading_2":
            current_category = block_text(b)
        elif b.get("type") == "to_do" and not b["to_do"].get("checked", False):
            text = block_text(b)
            if text:
                result.setdefault(current_category, []).append(text)
    return result


def blocks_to_children(blocks: list[dict]) -> list[dict]:
    """템플릿 블록을 새 페이지용 children으로 변환한다."""
    children = []
    for b in blocks:
        btype = b.get("type")
        if btype == "heading_2":
            children.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": b["heading_2"]["rich_text"]},
            })
        elif btype == "to_do":
            children.append({
                "object": "block", "type": "to_do",
                "to_do": {
                    "rich_text": b["to_do"]["rich_text"],
                    "checked": False,
                },
            })
        elif btype == "divider":
            children.append({"object": "block", "type": "divider", "divider": {}})
    return children


def to_do_block(text: str) -> dict:
    return {
        "object": "block", "type": "to_do",
        "to_do": {
            "rich_text": [{"text": {"content": text}}],
            "checked": False,
        },
    }


def template_categories(template_children: list[dict]) -> list[str]:
    """템플릿의 heading_2 텍스트(카테고리) 목록을 순서대로 반환."""
    result = []
    for b in template_children:
        if b.get("type") == "heading_2":
            text = "".join(t.get("plain_text", "") for t in b["heading_2"]["rich_text"])
            if text:
                result.append(text)
    return result


# ─────────────────── 연간 할일 페이지의 반복 패턴 파서 ───────────────────


def heading_matches_today(heading: str, today: datetime) -> bool:
    """heading이 오늘 날짜에 해당하는 반복 패턴이면 True."""
    h = heading.strip()
    weekday_idx = today.weekday()  # Mon=0
    weekday_name = WEEKDAY_KO[weekday_idx]

    if "매일" in h:
        return True
    if weekday_name in h and "마다" in h:
        return True
    if "주말" in h and weekday_idx >= 5:
        return True
    if "평일" in h and weekday_idx < 5:
        return True

    m = re.search(r"매월\s*(\d{1,2})\s*일", h)
    if m and int(m.group(1)) == today.day:
        return True

    # YYYY-MM-DD 또는 YYYY.M.D
    m = re.search(r"(\d{4})[\-.](\d{1,2})[\-.](\d{1,2})", h)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        if (y, mo, d) == (today.year, today.month, today.day):
            return True

    return False


def is_urgent_pool_heading(heading: str) -> bool:
    return "빠른 시일" in heading or "빠른시일" in heading


def parse_recurring_todos(
    blocks: list[dict], today: datetime,
) -> tuple[list[dict], list[str]]:
    """연간 할일 페이지의 "# 할 일" 섹션 이하를 파싱.

    Returns:
        recurring: [{"section": "매일 아침 할 일", "task": "리더십 연습 두페이지 읽기"}, ...]
        urgent_pool: 빠른 시일 내 처리할 일들 (LLM이 1-2개만 선별)
    """
    recurring: list[dict] = []
    urgent_pool: list[str] = []
    in_todo_root = False  # "# 할 일" 헤딩 이하인지
    current_h2 = ""
    matched = False
    in_urgent = False

    for b in blocks:
        btype = b.get("type")
        if btype == "heading_1":
            text = block_text(b)
            in_todo_root = "할 일" in text
            current_h2 = ""
            matched = False
            in_urgent = False
            continue
        if not in_todo_root:
            continue

        if btype == "heading_2":
            current_h2 = block_text(b)
            matched = heading_matches_today(current_h2, today)
            in_urgent = is_urgent_pool_heading(current_h2)
        elif btype == "to_do":
            text = block_text(b)
            if not text:
                continue
            if b["to_do"].get("checked", False):
                continue
            if matched:
                recurring.append({"section": current_h2, "task": text})
            elif in_urgent:
                urgent_pool.append(text)

    return recurring, urgent_pool


# ─────────────────────── iCloud Calendar ───────────────────────


def collect_calendar(today: datetime) -> list[dict]:
    """오늘 KST 00:00~24:00 일정을 iCloud CalDAV로 수집."""
    if not APPLE_ID or not APPLE_APP_PASSWORD:
        print("[Calendar] APPLE_ID/APPLE_APP_PASSWORD 미설정 — 건너뜀")
        return []

    try:
        client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=APPLE_ID,
            password=APPLE_APP_PASSWORD,
        )
        calendars = client.principal().calendars()
    except Exception as e:
        print(f"[Calendar] iCloud 연결 실패: {e}")
        return []

    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    events: list[dict] = []

    for cal in calendars:
        cal_name = cal.get_display_name()
        try:
            found = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:
            print(f"[Calendar] '{cal_name}' 검색 실패: {e}")
            continue
        for ev in found:
            try:
                # caldav 2.0+에서 vobject 의존성이 제거됨 → icalendar API 사용.
                # (구 ev.vobject_instance는 vobject 미설치 시 조용히 None을 반환해
                #  모든 일정이 누락되던 버그가 있었음)
                comp = ev.icalendar_component
                summary = str(comp.get("summary", "제목 없음"))
                dtstart = comp.get("dtstart").dt
                if hasattr(dtstart, "hour"):
                    time_str = dtstart.astimezone(KST).strftime("%H:%M")
                    label = f"[{time_str}] {summary}"
                else:
                    label = f"[종일] {summary}"
                events.append({"label": label, "summary": summary})
            except Exception as e:
                print(f"[Calendar] '{cal_name}' 일정 파싱 실패: {type(e).__name__}: {e}")
                continue

    print(f"[Calendar] {len(events)}개 일정 수집")
    return events


# ─────────────────────── Claude 분류기 ───────────────────────


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["category", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def classify_items(
    categories: list[str],
    recurring: list[dict],
    calendar_events: list[dict],
    urgent_pool: list[str],
    today: datetime,
) -> dict[str, list[str]]:
    """반복 할 일 + 캘린더 + 빠른시일 추천을 카테고리별로 분류.

    Returns: {"업무": [...], "개인": [...], "공부": [...]}
    """
    if not (recurring or calendar_events or urgent_pool):
        return {c: [] for c in categories}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[Classifier] ANTHROPIC_API_KEY 미설정 — 분류 없이 첫 카테고리에 모두 추가")
        fallback = [it["task"] for it in recurring]
        fallback += [e["label"] for e in calendar_events]
        return {c: (fallback if c == categories[0] else []) for c in categories}

    weekday = WEEKDAY_KO[today.weekday()]
    recurring_lines = "\n".join(f"- ({it['section']}) {it['task']}" for it in recurring) or "(없음)"
    calendar_lines = "\n".join(f"- {e['label']}" for e in calendar_events) or "(없음)"
    urgent_lines = "\n".join(f"- {t}" for t in urgent_pool) or "(없음)"
    cat_list = ", ".join(categories)

    prompt = f"""오늘은 {today.strftime("%Y-%m-%d")} {weekday}입니다.

다음 할 일 항목들을 [{cat_list}] 중 하나의 카테고리로 분류해주세요.

[반복 할 일 (오늘 해당)]
{recurring_lines}

[오늘의 캘린더 일정]
{calendar_lines}

[빠른 시일 내 처리할 일들 — 이 중 오늘 추천할 만한 1-2개만 골라서 분류]
{urgent_lines}

규칙:
- [오늘의 캘린더 일정]과 [반복 할 일]의 모든 항목은 빠짐없이 분류 (드롭 금지)
- 모든 카테고리를 응답에 포함하되, 항목이 없으면 빈 배열로
- 캘린더 일정은 [HH:MM] / [종일] 시간 표기를 그대로 유지
- 반복 할 일의 (섹션) 표기는 제거하고 task 텍스트만 사용
- "1-2개만 선별"은 오직 [빠른 시일 내 처리할 일들]에만 적용 — 오늘의 요일/일정에 맞는 1-2개만 (없으면 0개)
- 각 항목은 정확히 한 카테고리에만 배치

분류 가이드 (카테고리가 업무/개인/공부일 때):
- 업무: 회사 일, 회의, 회사 스터디, 발표 준비 등
- 개인: 자기관리, 회고/일기/원고/브이로그 등 창작·기록, 집안일, 취미, 운동, 약속, 가족 관련
- 공부: 책 읽기, 어학(링글 등), 자격증 준비(데이터브릭스 등), 알고리즘, 기술 학습
"""

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        output_config={
            "format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)

    result = {c: [] for c in categories}
    for entry in data["assignments"]:
        cat = entry["category"]
        if cat in result:
            result[cat].extend(entry["items"])
        else:
            result.setdefault(categories[0], []).extend(entry["items"])

    must_include = [e["label"] for e in calendar_events] + [it["task"] for it in recurring]
    included = {x for items in result.values() for x in items}
    missing = [m for m in must_include if m not in included]
    if missing:
        print(f"[Classifier] LLM이 누락한 캘린더/반복 {len(missing)}개 → 2차 분류")
        recovered = _recover_missing_items(client, categories, missing, today)
        for cat, items in recovered.items():
            if cat in result:
                result[cat].extend(items)

    print(f"[Classifier] " + ", ".join(f"{c}:{len(v)}" for c, v in result.items()))
    return result


def _recover_missing_items(
    client: "anthropic.Anthropic",
    categories: list[str],
    missing: list[str],
    today: datetime,
) -> dict[str, list[str]]:
    """1차 분류에서 누락된 캘린더/반복 항목만 다시 분류한다.

    실패 시 첫 카테고리로 폴백 (항목이 사라지지 않도록 보장).
    """
    weekday = WEEKDAY_KO[today.weekday()]
    cat_list = ", ".join(categories)
    lines = "\n".join(f"- {m}" for m in missing)

    prompt = f"""오늘은 {today.strftime("%Y-%m-%d")} {weekday}입니다.

아래 항목들을 [{cat_list}] 중 하나로 분류해주세요. **모든 항목을 반드시 포함**해야 합니다 (드롭 금지).

[분류 대상]
{lines}

규칙:
- 입력의 모든 항목을 응답에 포함 (입력 텍스트 그대로, 변형 금지)
- 모든 카테고리를 응답에 포함하되, 항목이 없으면 빈 배열로
- 각 항목은 정확히 한 카테고리에만 배치

분류 가이드 (카테고리가 업무/개인/공부일 때):
- 업무: 회사 일, 회의, 회사 스터디, 발표 준비 등
- 개인: 자기관리, 회고/일기/원고/브이로그 등 창작·기록, 집안일, 취미, 운동, 약속, 가족 관련
- 공부: 책 읽기, 어학(링글 등), 자격증 준비(데이터브릭스 등), 알고리즘, 기술 학습
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            output_config={
                "format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except Exception as e:
        print(f"[Classifier] 2차 분류 실패 ({e}) — 누락 {len(missing)}개를 '{categories[0]}'로 폴백")
        return {categories[0]: list(missing)}

    out: dict[str, list[str]] = {c: [] for c in categories}
    placed: set[str] = set()
    for entry in data.get("assignments", []):
        cat = entry.get("category")
        items = [x for x in entry.get("items", []) if x in set(missing) and x not in placed]
        if cat in out:
            out[cat].extend(items)
        else:
            out[categories[0]].extend(items)
        placed.update(items)

    # 2차도 누락한 항목이 있으면 첫 카테고리로 폴백 (절대 사라지지 않도록)
    still_missing = [m for m in missing if m not in placed]
    if still_missing:
        print(f"[Classifier] 2차도 누락한 {len(still_missing)}개를 '{categories[0]}'로 폴백")
        out[categories[0]].extend(still_missing)

    return out


# ─────────────────────── 페이지 빌드 ───────────────────────


def _todo_block_text(block: dict) -> str:
    """to_do 블록의 텍스트를 추출한다 (plain_text 우선, 없으면 text.content)."""
    parts = block.get("to_do", {}).get("rich_text", [])
    return "".join(
        rt.get("plain_text") or rt.get("text", {}).get("content", "") for rt in parts
    ).strip()


def build_page(
    today: str,
    parent_page_id: str,
    template_children: list[dict],
    carryover: dict[str, list[str]],
    classified: dict[str, list[str]],
) -> dict:
    """템플릿 + 이월 + LLM 분류 결과를 합쳐서 페이지 children 구성.

    이월(carryover)은 "오늘 다시 생성되지 않는 일회성 미완료 task"만 가져온다.
    매일 재생성되는 템플릿 고정 task와 반복/캘린더 분류 task는 이월 대상에서
    제외 — 이월과 재생성이 겹쳐 매일 중복 누적되던 문제 방지.
    """
    children = []
    current_category = ""
    seen: set[str] = set()  # 이미 추가한 task 텍스트 (중복 방지)

    def add_task(text: str) -> None:
        key = text.strip()
        if not key or key in seen:
            return
        seen.add(key)
        children.append(to_do_block(key))

    # 오늘 새로 생성되는 task 텍스트 (템플릿 고정 + 분류) — 이월 제외 기준
    regenerated: set[str] = set()
    for block in template_children:
        if block.get("type") == "to_do":
            t = _todo_block_text(block)
            if t:
                regenerated.add(t)
    for tasks in classified.values():
        regenerated.update(t.strip() for t in tasks if t.strip())

    # 이월은 오늘 재생성되지 않는 일회성 task만 남긴다
    carryover = {
        category: [t for t in tasks if t.strip() and t.strip() not in regenerated]
        for category, tasks in carryover.items()
    }

    for block in template_children:
        # 템플릿 고정 task도 중복 추적 대상에 포함
        if block.get("type") == "to_do":
            key = _todo_block_text(block)
            if not key or key in seen:
                continue
            seen.add(key)
            children.append(block)
            continue

        children.append(block)
        if block.get("type") == "heading_2":
            current_category = "".join(
                t.get("plain_text", "") for t in block["heading_2"]["rich_text"]
            )
            for task in carryover.pop(current_category, []):
                add_task(task)
            for task in classified.get(current_category, []):
                add_task(task)

    # 템플릿에 없는 카테고리의 이월 항목 (이미 추가된 task 제외)
    for category, tasks in carryover.items():
        fresh = [t for t in tasks if t.strip() and t.strip() not in seen]
        if not fresh:
            continue
        children.append({"object": "block", "type": "divider", "divider": {}})
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": category or "기타"}}]},
        })
        for task in fresh:
            add_task(task)

    return {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "✅"},
        "properties": {"title": [{"text": {"content": f"{today} TODO"}}]},
        "children": children,
    }


# ─────────────────── 연/월 sub-page 확보 ───────────────────


MONTH_PAGE_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def ensure_year_page(year: str, root_id: str) -> str:
    """ROOT 안에서 '{year} 할일' 페이지를 찾고, 없으면 생성하여 page_id 반환.

    매년 1/1 첫 실행에서 자동으로 새 연도 페이지를 만든다. 'yearly 반복 패턴
    (# 할 일 섹션)'은 자동 복사하지 않으므로 사용자가 새 페이지에 직접 채워야
    매일 페이지에 반복 task가 들어간다.
    """
    title = f"{year} 할일"
    for b in get_blocks(root_id):
        if b.get("type") != "child_page":
            continue
        if b["child_page"].get("title", "") == title:
            return b["id"]

    print(f"[Year] '{title}' 페이지 신규 생성")
    result = api_request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": root_id},
        "icon": {"type": "emoji", "emoji": "🗓️"},
        "properties": {"title": [{"text": {"content": title}}]},
    })
    return result["id"]


def ensure_month_page(year_month: str, parent_id: str, existing: dict[str, str]) -> str:
    """parent_id 안에서 '{YYYY-MM}' sub-page를 찾고, 없으면 생성하여 page_id 반환."""
    if year_month in existing:
        return existing[year_month]

    print(f"[Month] '{year_month}' sub-page 신규 생성")
    result = api_request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "🗂️"},
        "properties": {"title": [{"text": {"content": year_month}}]},
    })
    page_id = result["id"]
    existing[year_month] = page_id
    return page_id


def collect_existing_month_pages(parent_id: str) -> dict[str, str]:
    """parent_id 직속의 'YYYY-MM' 월 sub-page id를 수집."""
    archives: dict[str, str] = {}
    for b in get_blocks(parent_id):
        if b.get("type") != "child_page":
            continue
        title = b["child_page"].get("title", "")
        if MONTH_PAGE_PATTERN.match(title):
            archives[title] = b["id"]
    return archives


# ─────────────────────── main ───────────────────────


def main():
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    print(f"{current_year} 할일 페이지 확보 중...")
    year_page_id = ensure_year_page(current_year, ROOT_PAGE_ID)

    print(f"월별 sub-page 확보 중... (현재 {current_month})")
    existing_archives = collect_existing_month_pages(year_page_id)
    month_page_id = ensure_month_page(current_month, year_page_id, existing_archives)

    print("템플릿 페이지 읽는 중...")
    template_blocks = get_blocks(TEMPLATE_PAGE_ID)
    template_children = blocks_to_children(template_blocks)
    categories = template_categories(template_children)
    print(f"  카테고리: {categories}")

    carryover: dict[str, list[str]] = {}
    print(f"전날({yesterday_str}) 페이지 검색 중...")
    yesterday_id = find_yesterday_page(yesterday_str)
    if yesterday_id:
        carryover = extract_unchecked_by_category(get_blocks(yesterday_id))
        total = sum(len(v) for v in carryover.values())
        print(f"  미완료 {total}개 이월")
    else:
        print("  전날 페이지 없음")

    print(f"{current_year} 할일 페이지의 # 할 일 섹션 파싱 중...")
    recurring, urgent_pool = parse_recurring_todos(get_blocks(year_page_id), now)
    print(f"  반복 {len(recurring)}개, 빠른시일 풀 {len(urgent_pool)}개")

    calendar_events = collect_calendar(now)

    classified = classify_items(categories, recurring, calendar_events, urgent_pool, now)

    body = build_page(today_str, month_page_id, template_children, carryover, classified)
    result = api_request("POST", "/pages", body)
    print(f"[{today_str} TODO] 생성 완료: {result.get('url', '')}")


if __name__ == "__main__":
    main()
