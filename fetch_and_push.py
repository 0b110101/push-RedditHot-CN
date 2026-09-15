import calendar
from datetime import datetime, timezone
import json
import os
import re
import time
from bs4 import BeautifulSoup
import feedparser
import requests
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.tmt.v20180321 import models, tmt_client

# ================= 配置与初始化 =================
RSS_URL = "https://www.reddit.com/r/GamingLeaksAndRumours/hot.rss"
CACHE_FILE = "sent_posts.json"
MAX_CACHE_SIZE = 150  # 只保留最近 150 条已读 ID，控制文件在几 KB 以内
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY")

# 设定起始时间门禁：2026-09-15 00:00:00 UTC
# 早于此时刻发布的贴子一律忽略
START_DATETIME_UTC = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)
START_TIMESTAMP = START_DATETIME_UTC.timestamp()


def init_tmt_client():
  cred = credential.Credential(TENCENT_SECRET_ID, TENCENT_SECRET_KEY)
  httpProfile = HttpProfile()
  httpProfile.endpoint = "tmt.tencentcloudapi.com"
  clientProfile = ClientProfile()
  clientProfile.httpProfile = httpProfile
  return tmt_client.TmtClient(cred, "ap-guangzhou", clientProfile)


tmt_cli = init_tmt_client()


def translate_text(text, source="auto", target="zh"):
  """调用腾讯云 TMT 进行文本翻译"""
  if not text or not text.strip():
    return ""
  try:
    req = models.TextTranslateRequest()
    req.SourceText = text
    req.Source = source
    req.Target = target
    req.ProjectId = 0
    resp = tmt_cli.TextTranslate(req)
    return resp.TargetText
  except Exception as e:
    print(f"翻译失败: {e}，回退至原文")
    return text


def clean_reddit_summary(html_content):
  """清洗 Reddit RSS 中的正文 HTML 标签"""
  soup = BeautifulSoup(html_content, "html.parser")
  for tag in soup.find_all(["a", "img"]):
    tag.replace_with(tag.get_text())

  text = soup.get_text(separator="\n").strip()
  text = re.sub(r"submitted by\s+/u/\S+.*", "", text, flags=re.DOTALL)
  return text.strip()


def main():
  # 1. 读取历史缓存（保持列表顺序，便于做队列裁剪）
  sent_ids = []
  if os.path.exists(CACHE_FILE):
    try:
      with open(CACHE_FILE, "r", encoding="utf-8") as f:
        sent_ids = json.load(f)
        if not isinstance(sent_ids, list):
          sent_ids = list(sent_ids)
    except Exception:
      sent_ids = []

  sent_set = set(sent_ids)

  # 2. 获取 RSS
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36 RedditBot/1.0"
      )
  }
  resp = requests.get(RSS_URL, headers=headers, timeout=15)
  feed = feedparser.parse(resp.content)

  new_entries = []
  # 倒序遍历（从老到新）
  for entry in reversed(feed.entries):
    post_id = entry.get("id", entry.link)

    # 检查项 1：是否已在缓存中
    if post_id in sent_set:
      continue

    # 检查项 2：时间戳过滤（核心：滤除早于 2026/09/15 的旧帖）
    pub_time = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
      # feedparser 解析出的 struct_time 为 UTC 时间
      pub_time = calendar.timegm(entry.published_parsed)

    if pub_time and pub_time < START_TIMESTAMP:
      # 如果比设定的起始时间还要早，顺手写入缓存记录并直接跳过，防止反复解析
      sent_ids.append(post_id)
      sent_set.add(post_id)
      continue

    new_entries.append(entry)

  if not new_entries:
    print("未检测到符合时间要求的新发帖子。")
    # 写回可能新跳过的旧贴记录
    save_cache(sent_ids)
    return

  # 3. 逐条翻译并推送
  for entry in new_entries:
    post_id = entry.get("id", entry.link)
    title_raw = entry.title
    summary_raw = clean_reddit_summary(
        entry.summary if "summary" in entry else ""
    )

    summary_to_translate = (
        summary_raw[:1500] if summary_raw else "（无正文内容或为纯外链）"
    )

    title_zh = translate_text(title_raw)
    desc_zh = (
        translate_text(summary_to_translate)
        if summary_raw
        else "（无文本内容）"
    )

    title_zh = (title_zh[:250] + "...") if len(title_zh) > 250 else title_zh
    desc_zh = (
        (desc_zh[:1800] + "\n\n*(正文过长已截断)*")
        if len(desc_zh) > 1800
        else desc_zh
    )

    embed = {
        "title": f"🎮 {title_zh}",
        "url": entry.link,
        "description": desc_zh,
        "color": 16729344,
        "fields": [{
            "name": "📌 原文标题",
            "value": (
                (title_raw[:250] + "...")
                if len(title_raw) > 250
                else title_raw
            ),
            "inline": False,
        }],
        "footer": {
            "text": (
                "Reddit · r/GamingLeaksAndRumours | 发布时间:"
                f" {entry.get('published', '未知')}"
            )
        },
    }

    payload = {"embeds": [embed]}

    if DISCORD_WEBHOOK_URL:
      d_resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
      if d_resp.status_code in [200, 204]:
        print(f"推送成功: {title_zh}")
        sent_ids.append(post_id)
        sent_set.add(post_id)
      else:
        print(f"Discord 推送失败: {d_resp.status_code} - {d_resp.text}")
    else:
      print("未配置 DISCORD_WEBHOOK_URL，仅记录。")
      sent_ids.append(post_id)
      sent_set.add(post_id)

    # 避免短时间内连续发请求触发 Discord 速率限制
    time.sleep(1)

  # 4. 保存缓存
  save_cache(sent_ids)


def save_cache(sent_ids):
  """修剪并保存已读 ID，强制保留最近 MAX_CACHE_SIZE 条"""
  # 取最后 150 条，体积常年维持在 3~5 KB，绝不膨胀
  trimmed_ids = sent_ids[-MAX_CACHE_SIZE:]
  with open(CACHE_FILE, "w", encoding="utf-8") as f:
    json.dump(trimmed_ids, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
  main()
