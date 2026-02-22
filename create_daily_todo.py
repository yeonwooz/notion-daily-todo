"""매일 Notion에 체크박스 TODO 페이지를 자동 생성하는 스크립트

- Notion 템플릿 페이지에서 기본 할 일을 읽어옴
- 전날 TODO 페이지에서 미완료 항목을 이월
"""

import json
import os
import urllib.request
from datetime import date, timedelta

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
PARENT_PAGE_ID = os.environ["PARENT_PAGE_ID"]
TEMPLATE_PAGE_ID = os.environ["TEMPLATE_PAGE_ID"]
API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json; charset=utf-8",
    "Notion-Version": "2022-06-28",
}


def api_request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, headers=HEADERS, method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_blocks(page_id: str) -> list[dict]:
    """페이지의 블록 목록을 가져온다."""
    result = api_request("GET", f"/blocks/{page_id}/children?page_size=100")
    return result.get("results", [])


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
            current_category = "".join(
                t.get("plain_text", "") for t in b["heading_2"].get("rich_text", [])
            )
        elif b.get("type") == "to_do" and not b["to_do"].get("checked", False):
            text = "".join(
                t.get("plain_text", "") for t in b["to_do"].get("rich_text", [])
            )
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


def build_page(today: str, template_children: list[dict], carryover: dict[str, list[str]]) -> dict:
    children = []
    current_category = ""

    for block in template_children:
        children.append(block)
        # heading_2 직후: 해당 카테고리의 이월 항목을 먼저 삽입
        if block.get("type") == "heading_2":
            current_category = "".join(
                t.get("plain_text", "") for t in block["heading_2"]["rich_text"]
            )
            for task in carryover.pop(current_category, []):
                children.append({
                    "object": "block", "type": "to_do",
                    "to_do": {
                        "rich_text": [{"text": {"content": task}}],
                        "checked": False,
                    },
                })

    # 템플릿에 없는 카테고리의 이월 항목 처리
    for category, tasks in carryover.items():
        children.append({"object": "block", "type": "divider", "divider": {}})
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": category or "기타"}}]},
        })
        for task in tasks:
            children.append({
                "object": "block", "type": "to_do",
                "to_do": {
                    "rich_text": [{"text": {"content": task}}],
                    "checked": False,
                },
            })

    return {
        "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
        "icon": {"type": "emoji", "emoji": "✅"},
        "properties": {"title": [{"text": {"content": f"{today} TODO"}}]},
        "children": children,
    }


def main():
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # 1. 템플릿 페이지에서 기본 할 일 읽기
    print("템플릿 페이지 읽는 중...")
    template_blocks = get_blocks(TEMPLATE_PAGE_ID)
    template_children = blocks_to_children(template_blocks)

    # 2. 전날 페이지에서 미완료 항목을 카테고리별로 가져오기
    carryover: dict[str, list[str]] = {}
    print(f"전날({yesterday}) 페이지 검색 중...")
    yesterday_id = find_yesterday_page(yesterday)
    if yesterday_id:
        yesterday_blocks = get_blocks(yesterday_id)
        carryover = extract_unchecked_by_category(yesterday_blocks)
        total = sum(len(v) for v in carryover.values())
        print(f"  미완료 항목 {total}개 발견 ({', '.join(f'{k}:{len(v)}' for k, v in carryover.items())})")
    else:
        print("  전날 페이지 없음")

    # 3. 오늘 TODO 페이지 생성
    body = build_page(today, template_children, carryover)
    result = api_request("POST", "/pages", body)
    url = result.get("url", "")
    print(f"[{today} TODO] 생성 완료: {url}")


if __name__ == "__main__":
    main()
