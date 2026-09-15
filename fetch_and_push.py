import os
import json
import re
import feedparser
import requests
from bs4 import BeautifulSoup
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.tmt.v20180321 import tmt_client, models

# ================= 配置与初始化 =================
RSS_URL = "https://www.reddit.com/r/GamingLeaksAndRumours/hot.rss"
CACHE_FILE = "sent_posts.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

TENCENT_SECRET_ID = os.getenv("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY")

def init_tmt_client():
    cred = credential.Credential(TENCENT_SECRET_ID, TENCENT_SECRET_KEY)
    httpProfile = HttpProfile()
    # 使用你指定的域名，如遇地区路由限制通常对应 ap-guangzhou 或 ap-beijing
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
    """提取 Reddit RSS 中的主要正文文本并清洗标签"""
    soup = BeautifulSoup(html_content, "html.parser")
    # Reddit RSS 往往把正文或链接打包在 <table> 或 <div> 中
    # 移除多余的提交信息与通用尾注
    for tag in soup.find_all(["a", "img"]):
        # 保留纯文本，移除嵌入媒体链接干扰
        tag.replace_with(tag.get_text())
    
    text = soup.get_text(separator="\n").strip()
    # 过滤 Reddit 默认尾缀
    text = re.sub(r'submitted by\s+/u/\S+.*', '', text, flags=re.DOTALL)
    return text.strip()

def main():
    # 1. 读取已推送缓存
    sent_ids = set()
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                sent_ids = set(json.load(f))
        except Exception:
            sent_ids = set()

    # 2. 获取 RSS（伪造 UA 避免被 Reddit 屏蔽）
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 RedditBot/1.0"
    }
    resp = requests.get(RSS_URL, headers=headers, timeout=15)
    feed = feedparser.parse(resp.content)

    new_entries = []
    # 倒序遍历（从较早的贴到最新发出的贴）
    for entry in reversed(feed.entries):
        # 使用唯一帖子 ID（如 t3_xxxx）进行排重，忽略更新时间
        post_id = entry.get("id", entry.link)
        if post_id not in sent_ids:
            new_entries.append(entry)

    if not new_entries:
        print("未检测到新发布的帖子。")
        return

    # 首次运行时若缓存为空，避免一次性轰炸，仅记录不推送或仅推最新 1 条
    if not sent_ids and len(new_entries) > 3:
        print("首次初始化，记录当前帖子 ID 避免刷屏。")
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump([e.get("id", e.link) for e in feed.entries], f)
        return

    # 3. 处理并推送到 Discord
    for entry in new_entries:
        post_id = entry.get("id", entry.link)
        title_raw = entry.title
        summary_raw = clean_reddit_summary(entry.summary if "summary" in entry else "")

        # 截断输入避免超出 TMT 单次长度限制
        summary_to_translate = summary_raw[:1500] if summary_raw else "（无正文内容或为纯外链）"

        # 翻译标题与正文
        title_zh = translate_text(title_raw)
        desc_zh = translate_text(summary_to_translate) if summary_raw else "（无文本内容）"

        # Discord 字段硬限制：title <= 256, description <= 4096, 单卡总计 <= 6000
        title_zh = (title_zh[:250] + "...") if len(title_zh) > 250 else title_zh
        desc_zh = (desc_zh[:1800] + "\n\n*(正文过长已截断)*") if len(desc_zh) > 1800 else desc_zh

        # 构造 Embeds 卡片
        embed = {
            "title": f"🎮 {title_zh}",
            "url": entry.link,
            "description": desc_zh,
            "color": 16729344,  # Reddit 橙色
            "fields": [
                {
                    "name": "📌 原文标题",
                    "value": (title_raw[:250] + "...") if len(title_raw) > 250 else title_raw,
                    "inline": False
                }
            ],
            "footer": {
                "text": f"Reddit · r/GamingLeaksAndRumours | 发布时间: {entry.get('published', '未知')}"
            }
        }

        payload = {"embeds": [embed]}

        if DISCORD_WEBHOOK_URL:
            d_resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            if d_resp.status_code in [200, 204]:
                print(f"推送成功: {title_zh}")
                sent_ids.add(post_id)
            else:
                print(f"Discord 推送失败: {d_resp.status_code} - {d_resp.text}")
        else:
            print("未配置 DISCORD_WEBHOOK_URL，跳过发送。")
            sent_ids.add(post_id)

    # 4. 更新已发送记录（保留最新的 300 条）
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_ids)[-300:], f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
