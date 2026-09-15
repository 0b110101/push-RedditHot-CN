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
  """使用标准 TC3-HMAC-SHA256 签名调用腾讯云 MPS TextTranslation 接口"""
  if not text or not text.strip():
    return ""

  if not TENCENT_SECRET_ID or not TENCENT_SECRET_KEY:
    print("错误: 未检测到 TENCENT_SECRET_ID 或 TENCENT_SECRET_KEY 环境变量！")
    return text

  host = "mps.tencentcloudapi.com"
  service = "mps"
  action = "TextTranslation"
  version = "2019-06-12"

  # 1. 组装请求 Payload (限制在 1800 字符内防止超限)
  payload_dict = {
      "SourceText": text[:1800],
      "Source": source,
      "Target": target,
  }
  payload = json.dumps(payload_dict, ensure_ascii=False)

  # 2. 准备时间参数
  timestamp = int(time.time())
  date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

  # 3. 构造 Canonical Headers 与 SignedHeaders (严格按照小写字典序排序)
  ct = "application/json; charset=utf-8"
  canonical_headers = (
      f"content-type:{ct}\n"
      f"host:{host}\n"
      f"x-tc-action:{action.lower()}\n"
      f"x-tc-timestamp:{timestamp}\n"
      f"x-tc-version:{version.lower()}\n"
  )
  signed_headers = (
      "content-type;host;x-tc-action;x-tc-timestamp;x-tc-version"
  )
  hashed_request_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()

  canonical_request = (
      f"POST\n"
      f"/\n"
      f"\n"
      f"{canonical_headers}\n"
      f"{signed_headers}\n"
      f"{hashed_request_payload}"
  )

  # 4. 拼装 StringToSign
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

  # 5. 计算派生密钥与签名
  secret_date = sign(("TC3" + TENCENT_SECRET_KEY).encode("utf-8"), date)
  secret_service = sign(secret_date, service)
  secret_signing = sign(secret_service, "tc3_request")
  signature = hmac.new(
      secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
  ).hexdigest()

  # 6. 构造 Authorization 请求头
  authorization = (
      f"{algorithm} "
      f"Credential={TENCENT_SECRET_ID}/{credential_scope}, "
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

    # 提取返回结果或输出具体报错
    if (
        "Response" in res_json
        and "TargetText" in res_json["Response"]
    ):
      return res_json["Response"]["TargetText"]
    else:
      print(f"腾讯云 API 返回失败原因: {res_json}")
      return text
  except Exception as e:
    print(f"请求腾讯云接口发生异常: {e}")
    return text


def clean_reddit_summary(html_content):
  """清洗 Reddit RSS 中的 HTML 标签并保留核心纯文本"""
  if not html_content:
    return ""
  soup = BeautifulSoup(html_content, "html.parser")
  for tag in soup.find_all(["a", "img"]):
    tag.replace_with(tag.get_text())

  text = soup.get_text(separator="\n").strip()
  text = re.sub(r"submitted by\s+/u/\S+.*", "", text, flags=re.DOTALL)
  return text.strip()


def truncate_unicode_text(text, max_len=600):
  """按 Unicode 码点数（Python 内置 len）严格限制在指定长度以内"""
  if len(text) > max_len:
    suffix = "\n\n*(正文超长已截断)*"
    allowed_len = max(0, max_len - len(suffix))
    return text[:allowed_len] + suffix
  return text


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

    # 滤除早于门禁时间的贴子
    pub_time = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
      pub_time = calendar.timegm(entry.published_parsed)

    if pub_time and pub_time < START_TIMESTAMP:
      sent_ids.append(post_id)
      sent_set.add(post_id)
      continue

    new_entries.append(entry)

  if not new_entries:
    print("未检测到符合时间要求的新帖。")
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

    print(f"正在调用翻译 API 处理: {title_raw[:30]}...")
    title_zh = tencent_mps_translate(title_raw, source="auto", target="zh")
    desc_zh = (
        tencent_mps_translate(
            summary_to_translate, source="auto", target="zh"
        )
        if summary_raw
        else "（无文本内容）"
    )

    # 标题限制在 250 字符
    title_zh = (title_zh[:247] + "...") if len(title_zh) > 250 else title_zh

    # 正文严格按 Unicode 码点数限制到 600 字符
    desc_zh = truncate_unicode_text(desc_zh, max_len=600)

    embed = {
        "title": f"🎮 {title_zh}",
        "url": entry.link,
        "description": desc_zh,
        "color": 16729344,
        "fields": [{
            "name": "📌 原文标题",
            "value": (
                (title_raw[:247] + "...")
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
      print("未配置 DISCORD_WEBHOOK_URL，仅本地记录。")
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
