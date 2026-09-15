import calendar
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import time
from bs4 import BeautifulSoup
import feedparser
import requests

# ================= 配置与初始化 =================
RSS_URL = "https://www.reddit.com/r/GamingLeaksAndRumours/hot.rss"
CACHE_FILE = "sent_posts.json"
MAX_CACHE_SIZE = 150
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY")

# 设定起始时间门禁：2026-09-15 00:00:00 UTC
START_DATETIME_UTC = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)
START_TIMESTAMP = START_DATETIME_UTC.timestamp()


def sign(key, msg):
  return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def tencent_mps_translate(text, source="auto", target="zh"):
  """使用腾讯云 TC3-HMAC-SHA256 规范签名直接调用 MPS TextTranslation 接口"""
  if not text or not text.strip():
    return ""

  secret_id = TENCENT_SECRET_ID
  secret_key = TENCENT_SECRET_KEY
  host = "mps.tencentcloudapi.com"
  service = "mps"
  action = "TextTranslation"
  version = "2019-06-12"

  # 1. 组装请求 Payload
  payload_dict = {
      "SourceText": text[:1900],  # 接口限制单次低于 2000 字符
      "Source": source,
      "Target": target,
  }
  payload = json.dumps(payload_dict, ensure_ascii=False)

  # 2. 生成时间戳
  timestamp = int(time.time())
  date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

  # 3. 构造 Canonical Request
  http_request_method = "POST"
  canonical_uri = "/"
  canonical_querystring = ""
  ct = "application/json; charset=utf-8"
  canonical_headers = (
      f"content-type:{ct}\nhost:{host}\nx-tc-action:{action.lower()}\n"
  )
  signed_headers = "content-type;host;x-tc-action"
  hashed_request_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()

  canonical_request = (
      f"{http_request_method}\n"
      f"{canonical_uri}\n"
      f"{canonical_querystring}\n"
      f"{canonical_headers}\n"
      f"{signed_headers}\n"
      f"{hashed_request_payload}"
  )

  # 4. 构造 StringToSign
  algorithm = "TC3-HMAC-SHA256"
  credential_scope = f"{date}/{service}/tc3_request"
  hashed_canonical_request = hashlib.sha256(
      canonical_request.encode("utf-8")
  ).hexdigest()
  string_to_sign = (
      f"{algorithm}\n"
      f"{timestamp}\n"
      f"{credential_scope}\n"
      f"{hashed_canonical_request}"
  )

  # 5. 计算签名
  secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
  secret_service = sign(secret_date, service)
  secret_signing = sign(secret_service, "tc3_request")
  signature = hmac.new(
      secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
  ).hexdigest()

  # 6. 构造 Authorization
  authorization = (
      f"{algorithm} "
      f"Credential={secret_id}/{credential_scope}, "
      f"SignedHeaders={signed_headers}, "
      f"Signature={signature}"
  )

  headers = {
      "Authorization": authorization,
      "Content-Type": ct,
      "Host": host,
      "X-TC-Action": action,
      "X-TC-Timestamp": str(timestamp),
      "X-TC-Version": version,
  }

  try:
    resp = requests.post(
        f"https://{host}", headers=headers, data=payload.encode("utf-8"), timeout=10
    )
    res_json = resp.json()
    if (
        "Response" in res_json
        and "TargetText" in res_json["Response"]
    ):
      return res_json["Response"]["TargetText"]
    else:
      print(f"腾讯云 API 返回异常: {res_json}")
      return text
  except Exception as e:
    print(f"网络或解析异常: {e}")
    return text


def clean_reddit_summary(html_content):
  """清洗 Reddit RSS 中的 HTML 标签"""
  soup = BeautifulSoup(html_content, "html.parser")
  for tag in soup.find_all(["a", "img"]):
    tag.replace_with(tag.get_text())

  text = soup.get_text(separator="\n").strip()
  text = re.sub(r"submitted by\s+/u/\S+.*", "", text, flags=re.DOTALL)
  return text.strip()


def main():
  # 1. 提取历史记录
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

  # 2. 拉取 Reddit RSS
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36 RedditBot/1.0"
      )
  }
  resp = requests.get(RSS_URL, headers=headers, timeout=15)
  feed = feedparser.parse(resp.content)

  new_entries = []
  for entry in reversed(feed.entries):
    post_id = entry.get("id", entry.link)

    if post_id in sent_set:
      continue

    # 滤除早于 2026/09/15 的旧帖
    pub_time = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
      pub_time = calendar.timegm(entry.published_parsed)

    if pub_time and pub_time < START_TIMESTAMP:
      sent_ids.append(post_id)
      sent_set.add(post_id)
      continue

    new_entries.append(entry)

  if not new_entries:
    print("未检测到符合条件的新帖。")
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

    # 翻译
    title_zh = tencent_mps_translate(title_raw, source="auto", target="zh")
    desc_zh = (
        tencent_mps_translate(
            summary_to_translate, source="auto", target="zh"
        )
        if summary_raw
        else "（无文本内容）"
    )

    # 边界保护与截断
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

    time.sleep(1)

  save_cache(sent_ids)


def save_cache(sent_ids):
  trimmed_ids = sent_ids[-MAX_CACHE_SIZE:]
  with open(CACHE_FILE, "w", encoding="utf-8") as f:
    json.dump(trimmed_ids, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
  main()
