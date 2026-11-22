import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from google import genai
from pydantic import BaseModel, Field

OUT_PATH = Path("data.json")
TZ_TW = timezone(timedelta(hours=8))

SUBREDDITS = {
    "genshin": ("原神", "Genshin_Impact_Leaks", ["version", "banner", "livestream", "schedule", "calendar", "phase"]),
    "hsr": ("崩壞：星穹鐵道", "HonkaiStarRail_leaks", ["version", "banner", "schedule", "livestream", "phase"]),
    "zzz": ("絕區零", "Zenlesszonezeroleaks_", ["version", "banner", "livestream", "schedule", "phase"]),
    "wuwa": ("鳴潮", "WutheringWavesLeaks", ["version", "banner", "livestream", "schedule", "phase"]),
    "endfield": ("明日方舟：終末地", "ArknightsEndfieldLeak", []),
    "nte": ("異環", "NTELeaks", []),
}


class LeakItem(BaseModel):
    title: str = Field(description="繁體中文簡短標題")
    summary: str = Field(description="繁體中文情報重點摘要（約 1-2 句）")
    confidence: str = Field(description="只能填寫：高、中、低")
    date: str = Field(description="YYYY-MM-DD 或留空字串")
    url: str = Field(description="原始貼文網址")


class GamePatch(BaseModel):
    cur: str | None = Field(default=None, description="當前版本號，例如 6.2，無法確定則為 null")
    next: str | None = Field(default=None, description="下個版本上線日 YYYY-MM-DD，無法確定則為 null")
    live: str | None = Field(default=None, description="前瞻直播日期 YYYY-MM-DD，無法確定則為 null")
    mid: str | None = Field(default=None, description="下半卡池日期 YYYY-MM-DD，無法確定則為 null")
    ban: list[list[str]] = Field(default_factory=list, description="卡池角色清單，每項為 [角色名, 'new' 或 're']")
    leaks: list[LeakItem] = Field(default_factory=list)


class AgentOutput(BaseModel):
    games: dict[str, GamePatch]


def fetch_reddit_posts(subreddit: str, keywords: list[str], limit: int = 25) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    headers = {"User-Agent": "SevenGamesInfoBot/1.0 (by local_agent)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
    except Exception as e:
        print(f"[WARN] 抓取 r/{subreddit} 失敗：{e}")
        return []

    posts = []
    for ch in children:
        d = ch.get("data", {})
        title = d.get("title") or ""
        flair = d.get("link_flair_text") or ""
        selftext = (d.get("selftext") or "")[:400]
        created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc).astimezone(TZ_TW).strftime("%Y-%m-%d")
        permalink = d.get("permalink") or ""
        full_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else (d.get("url") or "")

        hay = f"{title} {flair}".lower()
        if not keywords or any(k.lower() in hay for k in keywords):
            posts.append({
                "title": title,
                "flair": flair,
                "date": created,
                "url": full_url,
                "snippet": selftext,
            })
    return posts[:10]


def clean_date(val: str | None) -> str | None:
    if not val:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", val.strip())
    return m.group(1) if m else None


def summarize_with_gemini(raw_by_game: dict[str, list[dict]]) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[WARN] 未設定 GEMINI_API_KEY，僅輸出原始貼文未經 AI 摘要。")
        fallback = {}
        for gid, posts in raw_by_game.items():
            fallback[gid] = {
                "leaks": [
                    {
                        "title": p["title"],
                        "summary": f"標籤：{p['flair'] or '無'}",
                        "confidence": "中",
                        "date": p["date"],
                        "url": p["url"],
                    }
                    for p in posts
                ]
            }
        return fallback

    client = genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1beta"},
    )
    today_str = datetime.now(TZ_TW).strftime("%Y-%m-%d")
    game_keys = list(raw_by_game.keys())

    prompt = f"""
今天日期是 {today_str}（UTC+8）。
請根據以下各遊戲的 Reddit 內鬼版最新貼文，整理出繁體中文情報與可推估的日期欄位。
請嚴格輸出合法 JSON（不要加 markdown 三引號），結構必須為：
{{
  "games": {{
    "遊戲id": {{
      "cur": "當前版本號或 null",
      "next": "下個版本上線日 YYYY-MM-DD 或 null",
      "live": "前瞻直播日期 YYYY-MM-DD 或 null",
      "mid": "下半卡池日期 YYYY-MM-DD 或 null",
      "ban": [["角色名", "new"]],
      "leaks": [
        {{
          "title": "繁體中文簡短標題",
          "summary": "繁體中文情報重點摘要（約 1-2 句）",
          "confidence": "高",
          "date": "YYYY-MM-DD 或空字串",
          "url": "原始貼文網址"
        }}
      ]
    }}
  }}
}}

規則：
1. 可用的遊戲 id 僅限：{game_keys}。
2. 所有文字輸出必須為繁體中文。
3. confidence 只能填「高」、「中」、「低」：測試服解包或明確排程偏「高」，一般傳聞或猜測偏「中」或「低」。
4. ban 陣列中第二個元素只能是 "new" 或 "re"。
5. 若貼文沒有明確提到確切的版本號或日期（YYYY-MM-DD），該欄位請填 null，切勿猜測編造。
6. 每款遊戲最多整理 6 則最有價值的 leaks。

原始資料：
{json.dumps(raw_by_game, ensure_ascii=False, indent=2)}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
        },
    )

    text = (response.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)

    parsed_dict = json.loads(text)
    raw_games = parsed_dict.get("games", {}) if isinstance(parsed_dict, dict) else {}

    out = {}
    for gid in game_keys:
        patch = raw_games.get(gid) or {}
        clean_leaks = []
        for lk in (patch.get("leaks") or [])[:6]:
            if not isinstance(lk, dict):
                continue
            conf = lk.get("confidence") if lk.get("confidence") in ("高", "中", "低") else "中"
            clean_leaks.append({
                "title": str(lk.get("title") or "情報重點"),
                "summary": str(lk.get("summary") or ""),
                "confidence": conf,
                "date": clean_date(lk.get("date")) or str(lk.get("date") or ""),
                "url": str(lk.get("url") or ""),
            })

        item = {"leaks": clean_leaks}
        if patch.get("cur"):
            item["cur"] = str(patch["cur"])
        if clean_date(patch.get("next")):
            item["next"] = clean_date(patch.get("next"))
        if clean_date(patch.get("live")):
            item["live"] = clean_date(patch.get("live"))
        if clean_date(patch.get("mid")):
            item["mid"] = clean_date(patch.get("mid"))
        if isinstance(patch.get("ban"), list) and patch["ban"]:
            valid_ban = []
            for b in patch["ban"]:
                if isinstance(b, list) and len(b) >= 2 and b[1] in ("new", "re"):
                    valid_ban.append([str(b[0]), b[1]])
            if valid_ban:
                item["ban"] = valid_ban
        out[gid] = item
    return out


def main():
    existing = {"updated_at": "", "games": {}}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    raw_by_game = {}
    for gid, (gname, sub, kws) in SUBREDDITS.items():
        print(f"正在抓取 {gname} (r/{sub})...")
        raw_by_game[gid] = fetch_reddit_posts(sub, kws)

    patches = summarize_with_gemini(raw_by_game)
    merged_games = existing.get("games", {})
    for gid, p_data in patches.items():
        prev = merged_games.get(gid, {})
        prev.update(p_data)
        merged_games[gid] = prev

    result = {
        "updated_at": datetime.now(TZ_TW).strftime("%Y-%m-%d %H:%M:%S"),
        "games": merged_games,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成！已更新 {OUT_PATH.resolve()}")


if __name__ == "__main__":
    main()
