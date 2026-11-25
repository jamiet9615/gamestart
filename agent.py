import email.utils
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

OUT_PATH = Path("data.json")
TZ_TW = timezone(timedelta(hours=8))
VERSION_LABELS = ("當前版本", "下個版本", "下下個版本")
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

GAMES = {
    "genshin": {
        "name": "原神",
        "en_name": "Genshin Impact",
        "hoyolab_gid": 2,
        "gnn_keywords": ["原神"],
        "news_queries": [
            ("zh-TW", "原神 版本 OR 卡池 OR 前瞻 OR 角色"),
            ("en-US", "Genshin Impact leak OR banner OR version schedule"),
        ],
        "subreddit": "Genshin_Impact_Leaks",
    },
    "hsr": {
        "name": "崩壞：星穹鐵道",
        "en_name": "Honkai Star Rail",
        "hoyolab_gid": 6,
        "gnn_keywords": ["星穹鐵道", "崩壞：星穹鐵道"],
        "news_queries": [
            ("zh-TW", "星穹鐵道 版本 OR 卡池 OR 前瞻 OR 躍遷"),
            ("en-US", "Honkai Star Rail leak OR banner OR version schedule"),
        ],
        "subreddit": "HonkaiStarRail_leaks",
    },
    "zzz": {
        "name": "絕區零",
        "en_name": "Zenless Zone Zero",
        "hoyolab_gid": 8,
        "gnn_keywords": ["絕區零"],
        "news_queries": [
            ("zh-TW", "絕區零 版本 OR 卡池 OR 前瞻 OR 調頻"),
            ("en-US", "Zenless Zone Zero leak OR banner OR version schedule"),
        ],
        "subreddit": "Zenlesszonezeroleaks_",
    },
    "wuwa": {
        "name": "鳴潮",
        "en_name": "Wuthering Waves",
        "hoyolab_gid": None,
        "gnn_keywords": ["鳴潮"],
        "news_queries": [
            ("zh-TW", "鳴潮 版本 OR 卡池 OR 前瞻 OR 共鳴者"),
            ("en-US", "Wuthering Waves leak OR banner OR version schedule"),
        ],
        "subreddit": "WutheringWavesLeaks",
    },
    "endfield": {
        "name": "明日方舟：終末地",
        "en_name": "Arknights Endfield",
        "hoyolab_gid": None,
        "gnn_keywords": ["終末地"],
        "news_queries": [
            ("zh-TW", "終末地 版本 OR 卡池 OR 更新 OR 活動"),
            ("en-US", "Arknights Endfield banner OR update OR leak"),
        ],
        "subreddit": "ArknightsEndfieldLeak",
    },
    "nte": {
        "name": "異環",
        "en_name": "Neverness to Everness",
        "hoyolab_gid": None,
        "gnn_keywords": ["異環"],
        "news_queries": [
            ("zh-TW", "異環 測試 OR 公測 OR 卡池 OR 情報"),
            ("en-US", "Neverness to Everness NTE update OR banner OR leak"),
        ],
        "subreddit": "NTELeaks",
    },
    "tot": {
        "name": "時空中的繪旅人",
        "en_name": "For All Time",
        "hoyolab_gid": None,
        "gnn_keywords": ["繪旅人", "時空中的繪旅人"],
        "news_queries": [
            ("zh-TW", "時空中的繪旅人 台服 OR 活動 OR 卡池 OR 維護"),
        ],
        "subreddit": None,
    },
}


class BannerChar(BaseModel):
    name: str = Field(description="角色繁體中文名稱（可簡短附註如 免費領、聯動、上半）")
    type: Literal["new", "re"] = Field(description="new 為新角色，re 為復刻角色")
    attr: str = Field(default="", description="屬性或定位（選填，不知道請留空字串）")


class VersionInfo(BaseModel):
    label: Literal["當前版本", "下個版本", "下下個版本"]
    ver: str = Field(default="", description="版本號或版本名稱，不確定請留空字串")
    start: str = Field(default="", description="版本上線日 YYYY-MM-DD，不確定請留空字串")
    live: str = Field(default="", description="前瞻直播日 YYYY-MM-DD，不確定請留空字串")
    mid: str = Field(default="", description="下半卡池日 YYYY-MM-DD，不確定請留空字串")
    chars: list[BannerChar] = Field(default_factory=list, description="該版本卡池角色清單")


class LeakItem(BaseModel):
    title: str = Field(description="繁體中文簡短標題")
    summary: str = Field(description="繁體中文情報重點摘要（約 1-2 句）")
    confidence: Literal["高", "中", "低"] = Field(
        description="官方公告或測試服實錘填高；可靠內鬼填中；一般傳聞或猜測填低"
    )
    date: str = Field(default="", description="YYYY-MM-DD 或空字串")
    url: str = Field(default="", description="真實存在的來源網址，禁止編造")


class GamePatch(BaseModel):
    versions: list[VersionInfo] = Field(
        description="固定依序包含 當前版本、下個版本、下下個版本 共 3 項"
    )
    leaks: list[LeakItem] = Field(default_factory=list, description="最多 6 則最有價值的最新情報")


def strip_html(raw: str, max_len: int = 320) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def clean_date(val: str | None) -> str:
    if not val:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(val).strip())
    return m.group(1) if m else ""


def parse_rss_date(pub_date: str | None) -> str:
    if not pub_date:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(pub_date.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ_TW).strftime("%Y-%m-%d")
    except Exception:
        return clean_date(pub_date)


def is_recent(date_str: str, max_days: int = 45) -> bool:
    if not date_str:
        return True
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TZ_TW)
        return (datetime.now(TZ_TW) - d).days <= max_days
    except Exception:
        return True


def fetch_hoyolab_news(gid: int | None) -> list[dict]:
    if not gid:
        return []
    items: list[dict] = []
    headers = {**HTTP_HEADERS, "X-Rpc-Language": "zh-tw"}
    for news_type, type_label in ((1, "官方公告"), (2, "官方活動"), (3, "官方資訊")):
        url = (
            "https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList"
            f"?gids={gid}&page_size=6&type={news_type}"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            rows = resp.json().get("data", {}).get("list", []) or []
        except Exception as e:
            print(f"[WARN] HoYoLAB gids={gid} type={news_type} 抓取失敗：{e}")
            continue

        for row in rows:
            post = row.get("post") or {}
            post_id = post.get("post_id")
            subject = (post.get("subject") or "").strip()
            if not post_id or not subject:
                continue
            created = post.get("created_at", 0)
            date_str = (
                datetime.fromtimestamp(created, tz=timezone.utc)
                .astimezone(TZ_TW)
                .strftime("%Y-%m-%d")
                if created
                else ""
            )
            if not is_recent(date_str, max_days=60):
                continue
            desc = strip_html(post.get("desc") or post.get("content") or "", max_len=280)
            extra = []
            if post.get("event_start_date"):
                extra.append(f"活動開始:{post['event_start_date']}")
            if post.get("event_end_date"):
                extra.append(f"結束:{post['event_end_date']}")
            snippet = f"{' '.join(extra)} {desc}".strip()
            items.append({
                "source": f"HoYoLAB {type_label}",
                "title": subject,
                "date": date_str,
                "url": f"https://www.hoyolab.com/article/{post_id}",
                "snippet": snippet,
                "official": True,
            })
    return items


def fetch_google_news_rss(queries: list[tuple[str, str]]) -> list[dict]:
    items: list[dict] = []
    for lang, query in queries:
        if lang == "zh-TW":
            rss_url = (
                f"https://news.google.com/rss/search?q={quote_plus(query)}"
                "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
            )
        else:
            rss_url = (
                f"https://news.google.com/rss/search?q={quote_plus(query)}"
                "&hl=en-US&gl=US&ceid=US:en"
            )
        try:
            resp = requests.get(rss_url, headers=HTTP_HEADERS, timeout=12)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            print(f"[WARN] Google News RSS ({query[:20]}) 失敗：{e}")
            continue

        for node in root.findall("./channel/item")[:10]:
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            pub_date = parse_rss_date(node.findtext("pubDate"))
            source_name = (node.findtext("source") or "Google 新聞").strip()
            desc = strip_html(node.findtext("description") or "", max_len=200)
            if not title or not is_recent(pub_date, max_days=45):
                continue
            items.append({
                "source": source_name,
                "title": title,
                "date": pub_date,
                "url": link,
                "snippet": desc,
                "official": False,
            })
    return items


def fetch_gnn_feed() -> list[dict]:
    try:
        resp = requests.get("https://gnn.gamer.com.tw/rss.xml", headers=HTTP_HEADERS, timeout=12)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"[WARN] 巴哈姆特 GNN RSS 失敗：{e}")
        return []

    items: list[dict] = []
    for node in root.findall("./channel/item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_date = parse_rss_date(node.findtext("pubDate"))
        desc = strip_html(node.findtext("description") or "", max_len=240)
        if title:
            items.append({
                "source": "巴哈姆特 GNN",
                "title": title,
                "date": pub_date,
                "url": link,
                "snippet": desc,
                "official": False,
            })
    return items


def match_gnn_items(gnn_items: list[dict], keywords: list[str]) -> list[dict]:
    matched = []
    for item in gnn_items:
        hay = f"{item['title']} {item['snippet']}"
        if any(kw in hay for kw in keywords):
            matched.append(item)
    return matched[:6]


def fetch_reddit_best_effort(subreddit: str | None) -> list[dict]:
    if not subreddit:
        return []
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=15"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        if resp.status_code in (403, 429):
            return []
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
    except Exception:
        return []

    posts = []
    for ch in children:
        d = ch.get("data", {})
        title = (d.get("title") or "").strip()
        if not title:
            continue
        flair = (d.get("link_flair_text") or "").strip()
        selftext = strip_html(d.get("selftext") or "", max_len=300)
        created = (
            datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
            .astimezone(TZ_TW)
            .strftime("%Y-%m-%d")
        )
        permalink = d.get("permalink") or ""
        full_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else (d.get("url") or "")
        posts.append({
            "source": f"r/{subreddit}" + (f" [{flair}]" if flair else ""),
            "title": title,
            "date": created,
            "url": full_url,
            "snippet": selftext,
            "official": False,
        })
    return posts[:8]


def deduplicate_items(items: list[dict], limit: int = 16) -> list[dict]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[dict] = []
    items_sorted = sorted(items, key=lambda x: (bool(x.get("official")), x.get("date") or ""), reverse=True)
    for item in items_sorted:
        url = (item.get("url") or "").strip()
        norm_title = re.sub(r"\W+", "", (item.get("title") or "").lower())
        if url and url in seen_urls:
            continue
        if norm_title and norm_title in seen_titles:
            continue
        if url:
            seen_urls.add(url)
        if norm_title:
            seen_titles.add(norm_title)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def call_gemini_with_retry(
    client: genai.Client,
    *,
    model: str,
    contents: str,
    config: types.GenerateContentConfig,
    retries: int = 3,
):
    last_err = None
    for attempt in range(retries):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            last_err = e
            wait_sec = 4 * (attempt + 1)
            print(f"[WARN] Gemini 呼叫失敗（第 {attempt + 1}/{retries} 次）：{e}，{wait_sec} 秒後重試")
            time.sleep(wait_sec)
    raise last_err  # type: ignore[misc]


def extract_grounding_sources(response) -> list[dict]:
    sources: list[dict] = []
    try:
        candidates = response.candidates or []
        if not candidates:
            return sources
        meta = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(meta, "grounding_chunks", None) or []
        for ch in chunks:
            web = getattr(ch, "web", None)
            if web and getattr(web, "uri", None):
                sources.append({
                    "title": getattr(web, "title", "") or "",
                    "url": web.uri,
                })
    except Exception:
        pass
    return sources[:12]


def normalize_patch(raw_patch: GamePatch | dict) -> dict:
    data = raw_patch.model_dump() if isinstance(raw_patch, BaseModel) else dict(raw_patch or {})

    raw_versions = data.get("versions") or []
    by_label = {v.get("label"): v for v in raw_versions if isinstance(v, dict) and v.get("label")}
    versions_out = []
    for idx, label in enumerate(VERSION_LABELS):
        src = by_label.get(label) or (raw_versions[idx] if idx < len(raw_versions) and isinstance(raw_versions[idx], dict) else {})
        chars_out = []
        for c in (src.get("chars") or [])[:10]:
            if not isinstance(c, dict):
                continue
            cname = str(c.get("name") or "").strip()
            if not cname:
                continue
            chars_out.append({
                "name": cname,
                "type": "re" if c.get("type") == "re" else "new",
                "attr": str(c.get("attr") or "").strip(),
            })
        versions_out.append({
            "label": label,
            "ver": str(src.get("ver") or "").strip(),
            "start": clean_date(src.get("start")),
            "live": clean_date(src.get("live")),
            "mid": clean_date(src.get("mid")),
            "chars": chars_out,
        })

    leaks_out = []
    for lk in (data.get("leaks") or [])[:6]:
        if not isinstance(lk, dict):
            continue
        title = str(lk.get("title") or "").strip()
        summary = str(lk.get("summary") or "").strip()
        if not title and not summary:
            continue
        conf = lk.get("confidence") if lk.get("confidence") in ("高", "中", "低") else "中"
        leaks_out.append({
            "title": title or "情報重點",
            "summary": summary,
            "confidence": conf,
            "date": clean_date(lk.get("date")) or str(lk.get("date") or "").strip(),
            "url": str(lk.get("url") or "").strip(),
        })

    return {"versions": versions_out, "leaks": leaks_out}


def build_fallback_patch(raw_items: list[dict]) -> dict:
    leaks = []
    for item in raw_items[:6]:
        leaks.append({
            "title": item["title"],
            "summary": item["snippet"] or f"來源：{item['source']}",
            "confidence": "高" if item.get("official") else "中",
            "date": item.get("date") or "",
            "url": item.get("url") or "",
        })
    return {
        "versions": [
            {"label": lbl, "ver": "", "start": "", "live": "", "mid": "", "chars": []}
            for lbl in VERSION_LABELS
        ],
        "leaks": leaks,
    }


def research_and_structure_game(
    client: genai.Client,
    gid: str,
    cfg: dict,
    raw_items: list[dict],
    today_str: str,
) -> dict:
    gname = cfg["name"]
    en_name = cfg["en_name"]

    # Stage 1: Google Search Grounding 聯網查證最新官方公告、排程與內鬼情報
    research_prompt = f"""
今天日期是 {today_str}（UTC+8）。
請為手遊《{gname}》（{en_name}）進行即時網路搜尋與情報整理。

請搜尋並確認以下資訊（務必區分官方已公布與內鬼爆料/推測，嚴禁編造未查到的日期或角色）：
1. 【當前版本】：目前正式服正在運行的版本號與版本名稱、版本上線日期（YYYY-MM-DD）、前瞻直播日期、下半卡池切換日期、以及當期 UP 卡池角色（區分新角色 new 與復刻角色 re）。
2. 【下個版本】：已公開或測試服/可靠內鬼指出的下個版本號、預計上線日期（YYYY-MM-DD）、前瞻直播日期、下半卡池日期、預計卡池角色（new / re）。
3. 【下下個版本】：若有已公開或可靠排程資訊，請簡要列出版本號、日期或可能角色；沒有就註明未知。
4. 【最新重點情報（最多 6 則）】：最近 2 至 4 週內最有價值的官方公告、版本更新、前瞻情報或測試服/內鬼爆料（如新角色技能/卡池排程/聯動/免費贈送），每則附上來源性質（官方/測試服解包/傳聞）、發布日期與真實參考網址。

以下是我們預先從官方 API 與新聞 RSS 抓到的近期參考線索（可直接引用其中的網址與事實，並透過 Google Search 補強缺少的卡池與爆料細節）：
{json.dumps(raw_items, ensure_ascii=False, indent=2)}
"""
    stage1_resp = call_gemini_with_retry(
        client,
        model="gemini-2.5-flash",
        contents=research_prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )
    research_notes = (stage1_resp.text or "").strip()
    grounding_sources = extract_grounding_sources(stage1_resp)

    # Stage 2: 依循嚴格 Pydantic Schema 轉換為前端可用的繁體中文 JSON
    structure_prompt = f"""
今天日期是 {today_str}（UTC+8）。請根據以下查證整理後的《{gname}》（{en_name}）資料，轉換成結構化繁體中文 JSON。

嚴格規則：
1. versions 必須固定包含 3 個物件，label 依序為 "當前版本"、"下個版本"、"下下個版本"。
2. 日期欄位（start、live、mid、leak.date）必須是 "YYYY-MM-DD" 或留空字串 ""，無法確定的日期請留空 ""，不可捏造。
3. chars 的 type 只能是 "new"（新角色）或 "re"（復刻角色）；若是官方贈送、聯動、上下半資訊可簡短附註在 name 括號內。
4. leaks 最多 6 則，全部使用繁體中文：
   - confidence 只能填「高」、「中」、「低」（官方公告或測試服解包填「高」，可靠爆料填「中」，推測傳聞填「低」）。
   - url 優先使用下方「預先抓取資料」中的直接原始連結，其次使用「搜尋來源清單」的連結，絕對不要編造不存在的網址。

【搜尋查證筆記】：
{research_notes}

【搜尋來源清單】：
{json.dumps(grounding_sources, ensure_ascii=False, indent=2)}

【預先抓取資料】：
{json.dumps(raw_items, ensure_ascii=False, indent=2)}
"""
    stage2_resp = call_gemini_with_retry(
        client,
        model="gemini-2.5-flash",
        contents=structure_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GamePatch,
            temperature=0.1,
        ),
    )
    parsed = getattr(stage2_resp, "parsed", None)
    if parsed is not None:
        return normalize_patch(parsed)

    text = (stage2_resp.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    return normalize_patch(json.loads(text))


def extract_ver_num(ver_str: str | None) -> str:
    if not ver_str:
        return ""
    m = re.search(r"\d+(?:\.\d+)+", str(ver_str))
    return m.group(0) if m else str(ver_str).strip()


def merge_game_patch(prev: dict | None, new_patch: dict) -> dict:
    prev = prev or {}
    prev_versions = prev.get("versions") if isinstance(prev.get("versions"), list) else []
    merged_versions = []

    for idx, label in enumerate(VERSION_LABELS):
        old_v = prev_versions[idx] if idx < len(prev_versions) and isinstance(prev_versions[idx], dict) else {}
        new_v = new_patch["versions"][idx]
        old_ver_num = extract_ver_num(old_v.get("ver"))
        new_ver_num = extract_ver_num(new_v.get("ver"))
        version_changed = bool(old_ver_num and new_ver_num and old_ver_num != new_ver_num)

        if version_changed:
            merged_v = {
                "label": label,
                "ver": new_v["ver"],
                "start": new_v["start"],
                "live": new_v["live"],
                "mid": new_v["mid"],
                "chars": new_v["chars"],
            }
        else:
            merged_v = {
                "label": label,
                "ver": new_v["ver"] or str(old_v.get("ver") or ""),
                "start": new_v["start"] or clean_date(old_v.get("start")),
                "live": new_v["live"] or clean_date(old_v.get("live")),
                "mid": new_v["mid"] or clean_date(old_v.get("mid")),
                "chars": new_v["chars"] if new_v["chars"] else (old_v.get("chars") or []),
            }
        merged_versions.append(merged_v)

    new_leaks = new_patch.get("leaks") or []
    merged_leaks = new_leaks if new_leaks else (prev.get("leaks") or [])

    return {
        "versions": merged_versions,
        "leaks": merged_leaks,
    }


def main():
    existing = {"updated_at": "", "games": {}}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    print("正在預先抓取巴哈姆特 GNN RSS...")
    gnn_items = fetch_gnn_feed()

    raw_by_game: dict[str, list[dict]] = {}
    for gid, cfg in GAMES.items():
        print(f"正在彙整 {cfg['name']} 的公開情報...")
        collected = []
        collected.extend(fetch_hoyolab_news(cfg.get("hoyolab_gid")))
        collected.extend(fetch_google_news_rss(cfg.get("news_queries") or []))
        collected.extend(match_gnn_items(gnn_items, cfg.get("gnn_keywords") or []))
        collected.extend(fetch_reddit_best_effort(cfg.get("subreddit")))
        raw_by_game[gid] = deduplicate_items(collected, limit=16)
        print(f"  -> {cfg['name']} 預先抓取到 {len(raw_by_game[gid])} 筆不重複來源")

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else None
    if not client:
        print("[WARN] 未設定 GEMINI_API_KEY，將改用預先抓取的公開來源產生基礎情報。")

    today_str = datetime.now(TZ_TW).strftime("%Y-%m-%d")
    merged_games = dict(existing.get("games") or {})

    for idx, (gid, cfg) in enumerate(GAMES.items()):
        if client:
            print(f"正在使用 Gemini Search Grounding 分析 {cfg['name']}...")
            try:
                patch = research_and_structure_game(client, gid, cfg, raw_by_game[gid], today_str)
            except Exception as e:
                print(f"[WARN] {cfg['name']} Gemini 分析失敗，改用 fallback：{e}")
                patch = build_fallback_patch(raw_by_game[gid])
            if idx < len(GAMES) - 1:
                time.sleep(2)
        else:
            patch = build_fallback_patch(raw_by_game[gid])

        merged_games[gid] = merge_game_patch(merged_games.get(gid), patch)

    result = {
        "updated_at": datetime.now(TZ_TW).strftime("%Y-%m-%d %H:%M:%S"),
        "games": merged_games,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成！已更新 {OUT_PATH.resolve()}")


if __name__ == "__main__":
    main()
