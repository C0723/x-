#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""X 账号新推文 -> Bark(手机弹窗) + Qmsg(QQ)，历史归档到 archive.md。

由 GitHub Actions 每 10 分钟跑一次，不需要服务器、不需要你开机。
环境变量：
    X_SCREEN_NAME  要监控的账号名，不带 @
    BARK_KEY       Bark App 里的那串 key（可选）
    QMSG_KEY       qmsg.zendee.cn 控制台的 key（可选）
    RSS_URL        可选，给一个现成 RSS 地址，脚本就改用 RSS 而不是内置抓取
    BARK_SERVER    可选，默认 https://api.day.app（自建 Bark 时改成你的域名）
    MAX_ITEMS      可选，一次最多推几条，默认 5，防止首次运行刷屏
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SCREEN_NAME = (os.environ.get("X_SCREEN_NAME") or "").strip().lstrip("@")
RSS_URL = (os.environ.get("RSS_URL") or "").strip()
BARK_KEY = (os.environ.get("BARK_KEY") or "").strip()
BARK_SERVER = (os.environ.get("BARK_SERVER") or "https://api.day.app").strip().rstrip("/")
QMSG_KEY = (os.environ.get("QMSG_KEY") or "").strip()
MAX_ITEMS = int(os.environ.get("MAX_ITEMS") or "5")

# 自建 NapCat（OneBot 11）可选配置
NAPCAT_URL = (os.environ.get("NAPCAT_URL") or "").strip().rstrip("/")
NAPCAT_TOKEN = (os.environ.get("NAPCAT_TOKEN") or "").strip()
QQ_ID = (os.environ.get("QQ_ID") or "").strip()

STATE_PATH = Path("state.json")
ARCHIVE_PATH = Path("archive.md")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- 网络

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def http_post(url, payload, timeout=30):
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def http_post_json(url, payload, headers=None, timeout=30):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 解析工具

def deep_iter(node):
    """递归遍历 JSON 的所有节点，用来对抗接口结构变化。"""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from deep_iter(value)
    elif isinstance(node, list):
        for value in node:
            yield from deep_iter(value)


def first_value(node, keys):
    for item in deep_iter(node):
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return ""


def find_images(node):
    """把所有 pbs.twimg.com/media 的图片地址抓出来，不依赖字段名。"""
    images = []
    for item in deep_iter(node):
        if isinstance(item, str) and item.startswith("https://pbs.twimg.com/media/"):
            url = item.split("?")[0]
            if url not in images:
                images.append(url)
    return images[:4]


def normalize(raw):
    tid = first_value(raw, ["id_str", "tweet_id", "id"])
    if not tid.isdigit():
        return None
    text = first_value(raw, ["full_text", "tweet_text", "text"])
    if not text:
        return None
    return {
        "id": tid,
        "text": text,
        "created": first_value(raw, ["created_at", "tweet_created_at"]),
        "images": find_images(raw),
        "url": "https://x.com/{}/status/{}".format(SCREEN_NAME, tid),
    }


# ---------------------------------------------------------------- 抓取

SYNDICATION_SOURCES = (
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/{name}",
    "https://cdn.syndication.twimg.com/timeline/profile?screen_name={name}&dnt=false",
)


def parse_payload(text):
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if match:
        text = match.group(1)
    data = json.loads(text)

    tweets, seen = [], set()
    for node in deep_iter(data):
        if isinstance(node, dict) and ("full_text" in node or "tweet_text" in node):
            tweet = normalize(node)
            if tweet and tweet["id"] not in seen:
                seen.add(tweet["id"])
                tweets.append(tweet)
    if not tweets:
        raise RuntimeError("解析到 0 条推文，结构可能变了")
    return tweets


def fetch_syndication():
    """两个端点轮着试，遇到 429 退避重试。"""
    problems = []
    for template in SYNDICATION_SOURCES:
        url = template.format(name=urllib.parse.quote(SCREEN_NAME))
        for attempt in range(3):
            try:
                return parse_payload(
                    http_get(url, headers={"Accept-Language": "en-US,en;q=0.9"})
                )
            except urllib.error.HTTPError as exc:
                problems.append("{} -> HTTP {}".format(url, exc.code))
                if exc.code in (429, 502, 503):
                    time.sleep(4 * (attempt + 1))
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                problems.append("{} -> {}".format(url, exc))
                break
    raise RuntimeError("抓取失败：" + " | ".join(problems))


def fetch_rss():
    root = ET.fromstring(http_get(RSS_URL))
    tweets = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        tid = re.sub(r"\D", "", guid) or re.sub(r"\D", "", link)
        if not tid:
            continue
        desc = item.findtext("description") or ""
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc)).strip()
        images = re.findall(r'<img[^>]+src="([^"]+)"', desc)
        tweets.append(
            {
                "id": tid,
                "text": text or (item.findtext("title") or ""),
                "created": item.findtext("pubDate") or "",
                "images": images[:4],
                "url": link or "https://x.com/{}/status/{}".format(SCREEN_NAME, tid),
            }
        )
    if not tweets:
        raise RuntimeError("RSS 里没有条目")
    return tweets


# ---------------------------------------------------------------- 推送

URL_RE = re.compile(r"https?://\S+")
DIGITS_RE = re.compile(r"\d{7,}")


def tidy(text, limit):
    text = URL_RE.sub("", text)
    text = DIGITS_RE.sub("[数字]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "（纯图片或纯链接推文）")[:limit]


def push_bark(tweet):
    if not BARK_KEY:
        log("跳过 Bark：未配置 BARK_KEY")
        return
    title = "@{} 新推文".format(SCREEN_NAME)
    params = {"url": tweet["url"], "group": "X", "isArchive": "1"}
    if tweet["images"]:
        params["image"] = tweet["images"][0] + "?name=small"
    url = "{}/{}/{}/{}?{}".format(
        BARK_SERVER,
        BARK_KEY,
        urllib.parse.quote(title, safe=""),
        urllib.parse.quote(tidy(tweet["text"], 300), safe=""),
        urllib.parse.urlencode(params),
    )
    try:
        log("Bark ->", http_get(url)[:120])
    except Exception as exc:  # noqa: BLE001
        log("Bark 失败:", exc)


def push_qmsg(tweet):
    if not QMSG_KEY:
        log("跳过 Qmsg：未配置 QMSG_KEY")
        return
    msg = "[X] @{}\n{}".format(SCREEN_NAME, tidy(tweet["text"], 600))
    try:
        log("Qmsg ->", http_post(
            "https://qmsg.zendee.cn/v3/send/" + QMSG_KEY, {"msg": msg}
        )[:160])
    except Exception as exc:  # noqa: BLE001
        log("Qmsg 失败:", exc)


def push_napcat(tweet):
    """通过自建 NapCat 的 OneBot 11 HTTP 接口发 QQ 私聊，带图带链接。"""
    if not (NAPCAT_URL and QQ_ID):
        log("跳过 NapCat：未配置 NAPCAT_URL / QQ_ID")
        return
    message = [
        {
            "type": "text",
            "data": {"text": "@{} 发了新推文\n{}\n{}".format(
                SCREEN_NAME, tweet["text"], tweet["url"])},
        }
    ]
    for image in tweet["images"][:4]:
        message.append({"type": "image", "data": {"file": image + "?name=large"}})
    headers = {"Authorization": "Bearer " + NAPCAT_TOKEN} if NAPCAT_TOKEN else {}
    try:
        log("NapCat ->", http_post_json(
            NAPCAT_URL + "/send_private_msg",
            {"user_id": int(QQ_ID), "message": message},
            headers,
        )[:160])
    except Exception as exc:  # noqa: BLE001
        log("NapCat 失败:", exc)


# ---------------------------------------------------------------- 去重与归档

def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                data.setdefault("last_id", "")
                data.setdefault("seen", [])
                return data
        except Exception as exc:  # noqa: BLE001
            log("state.json 读取失败，重建:", exc)
    return {"last_id": "", "seen": []}


def save_state(state):
    state["seen"] = state["seen"][-300:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def append_archive(tweet):
    if not ARCHIVE_PATH.exists():
        ARCHIVE_PATH.write_text("# 推文归档\n\n", "utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["## " + stamp, "", tweet["text"], "", "[原推]({})".format(tweet["url"])]
    for image in tweet["images"]:
        lines += ["", "![]({}?name=small)".format(image)]
    with ARCHIVE_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n\n---\n\n")


# ---------------------------------------------------------------- 主流程

def main():
    if not SCREEN_NAME and not RSS_URL:
        log("需要 X_SCREEN_NAME 或 RSS_URL")
        sys.exit(1)

    state = load_state()
    first_run = not state.get("last_id")

    tweets = None
    try:
        tweets = fetch_rss() if RSS_URL else fetch_syndication()
    except Exception as exc:  # noqa: BLE001
        log("抓取失败:", exc)
        if "429" in str(exc) or "HTTP Error" in str(exc) or "timed out" in str(exc):
            log("属于临时限流/网络问题，这次跳过，下一轮自动重试")
            sys.exit(0)
        sys.exit(1)
    tweets.sort(key=lambda t: int(t["id"]))
    log("抓到 {} 条，最新 id={}".format(len(tweets), tweets[-1]["id"]))

    if first_run:
        log("首次运行：只记录基线，不推送（避免一次弹 20 条）")
        state["last_id"] = tweets[-1]["id"]
        state["seen"] = list({t["id"] for t in tweets})
        save_state(state)
        return

    last_id = int(state["last_id"])
    seen = set(state["seen"])
    fresh = [t for t in tweets if int(t["id"]) > last_id and t["id"] not in seen]
    fresh.sort(key=lambda t: int(t["id"]))
    log("新推 {} 条".format(len(fresh)))

    for tweet in fresh[-MAX_ITEMS:]:
        log("推送:", tweet["url"])
        push_bark(tweet)
        push_napcat(tweet)
        push_qmsg(tweet)
        append_archive(tweet)
        state["last_id"] = tweet["id"]
        state["seen"].append(tweet["id"])
        save_state(state)
        time.sleep(3.2)  # Qmsg 限流：每 3 秒一次

    if fresh:
        log("完成")
    else:
        log("没有新内容")


if __name__ == "__main__":
    main()
