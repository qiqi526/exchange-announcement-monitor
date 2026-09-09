import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


TZ = ZoneInfo("Asia/Shanghai")
STATE_FILE = Path("state.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/143 Safari/537.36"
)


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    url: str


@dataclass(frozen=True)
class Notice:
    site_code: str
    site_name: str
    title: str
    url: str
    date: str = ""

    @property
    def key(self) -> str:
        raw = f"{self.site_code}|{self.title}|{self.url}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]


SITES = [
    Site("SHFE", "上海期货交易所", "https://www.shfe.com.cn/publicnotice/"),
    Site("DCE", "大连商品交易所", "https://www.dce.com.cn/dalianshangpin/ywfw/jystz/index.html"),
    Site("CZCE", "郑州商品交易所", "https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/H077001003001index_1.htm"),
    Site("CFFEX-GG", "中国金融期货交易所·公告", "https://www.cffex.com.cn/cn/jysgg.html"),
    Site("CFFEX-TZ", "中国金融期货交易所·通知", "https://www.cffex.com.cn/cn/jystz.html"),
    Site("INE", "上海国际能源交易中心", "https://www.ine.cn/publicnotice/"),
    Site("GFEX-TZ", "广州期货交易所·通知公告", "https://www.gfex.com.cn/gfex/tzts/list_yw.shtml"),
    Site("GFEX-PZ", "广州期货交易所·品种公告", "https://www.gfex.com.cn/gfex/pzgg/list.shtml"),
]

KEYWORDS = ("公告", "通知", "决定", "意见", "事项", "提示", "安排", "名单")
IGNORE_EXACT = {
    "公告", "通知", "公告通知", "通知公告", "交易所公告", "交易所通知", "更多",
    "more", "首页", "上一页", "下一页", "尾页", "查看详情", "打印此页",
}
DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"),
    re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})(?!\d)"),
)


def now_cn() -> datetime:
    return datetime.now(TZ)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_date(text: str) -> str:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        parts = match.groups()
        if len(parts) == 3:
            year, month, day = parts
        else:
            year, month, day = str(now_cn().year), parts[0], parts[1]
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            return ""
    return ""


def looks_like_notice(title: str, href: str) -> bool:
    lowered = title.lower()
    if title in IGNORE_EXACT or lowered in IGNORE_EXACT:
        return False
    if len(title) < 6 or len(title) > 180:
        return False
    if href.startswith(("javascript:", "#", "mailto:")):
        return False
    path_hint = any(x in href.lower() for x in ("notice", "webinfo", "/tz", "/gg", "jyst"))
    title_hint = any(word in title for word in KEYWORDS)
    return path_hint and title_hint


def fetch(site: Site) -> list[Notice]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(site.url, headers=headers, timeout=12)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            soup = BeautifulSoup(response.text, "html.parser")
            notices: dict[str, Notice] = {}
            base_host = urlparse(site.url).netloc
            for anchor in soup.find_all("a", href=True):
                title = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))
                href = clean_text(anchor["href"])
                absolute = urljoin(site.url, href)
                if urlparse(absolute).netloc != base_host:
                    continue
                if not looks_like_notice(title, href):
                    continue
                context = anchor.parent.get_text(" ", strip=True) if anchor.parent else title
                notice = Notice(site.code, site.name, title, absolute, normalize_date(context))
                notices[notice.key] = notice
            if not notices:
                raise RuntimeError("页面可访问，但没有识别到公告；网页结构可能已改变")
            return list(notices.values())[:80]
        except Exception as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(2)
    raise RuntimeError(str(last_error))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": {}, "last_success": {}, "failures": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = {}
    state.setdefault("seen", {})
    state.setdefault("last_success", {})
    state.setdefault("failures", {})
    return state


def save_state(state: dict) -> None:
    cutoff = now_cn().timestamp() - 90 * 86400
    state["seen"] = {
        key: value for key, value in state["seen"].items()
        if float(value.get("first_seen_ts", 0)) >= cutoff
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def recipients() -> list[str]:
    raw = os.environ.get("MAIL_TO", "")
    return [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]


def send_email(subject: str, text_body: str, html_body: str) -> None:
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    mail_to = recipients()
    if not username or not password or not mail_to:
        raise RuntimeError("缺少 SMTP_USERNAME、SMTP_PASSWORD 或 MAIL_TO")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"交易所公告机器人 <{username}>"
    msg["To"] = ", ".join(mail_to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=context, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)


def notice_lines(items: Iterable[Notice]) -> tuple[str, str]:
    text_parts, html_parts = [], []
    for idx, item in enumerate(items, 1):
        date = item.date or "网页未标明"
        text_parts.append(f"{idx}. [{item.site_name}] {item.title}\n发布日期：{date}\n原文：{item.url}")
        html_parts.append(
            f'<div style="margin:0 0 18px;padding:14px;border-left:4px solid #2457c5;background:#f7f9fc">'
            f'<b>{idx}. {html.escape(item.site_name)}</b><br>'
            f'<a href="{html.escape(item.url)}" style="font-size:16px">{html.escape(item.title)}</a><br>'
            f'<span style="color:#666">发布日期：{html.escape(date)}</span></div>'
        )
    return "\n\n".join(text_parts), "".join(html_parts)


def collect(state: dict) -> tuple[list[Notice], list[Notice], dict[str, str]]:
    all_items: list[Notice] = []
    new_items: list[Notice] = []
    errors: dict[str, str] = {}
    timestamp = now_cn()
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {pool.submit(fetch, site): site for site in SITES}
        for future in as_completed(futures):
            site = futures[future]
            try:
                items = future.result()
            except Exception as exc:
                errors[site.code] = str(exc)
                state["failures"][site.code] = int(state["failures"].get(site.code, 0)) + 1
                continue
            all_items.extend(items)
            state["last_success"][site.code] = timestamp.isoformat(timespec="seconds")
            state["failures"][site.code] = 0
            for item in items:
                if item.key not in state["seen"]:
                    new_items.append(item)
                    state["seen"][item.key] = {
                        "site": item.site_name,
                        "title": item.title,
                        "url": item.url,
                        "date": item.date,
                        "first_seen": timestamp.isoformat(timespec="seconds"),
                        "first_seen_ts": timestamp.timestamp(),
                    }
    return all_items, new_items, errors


def send_new(items: list[Notice]) -> None:
    text_list, html_list = notice_lines(items)
    timestamp = now_cn().strftime("%Y-%m-%d %H:%M")
    subject = f"【交易所公告提醒】发现{len(items)}条新公告"
    text_body = f"检查时间：{timestamp}\n\n{text_list}"
    html_body = (
        '<div style="font-family:Arial,Microsoft YaHei,sans-serif;max-width:760px;margin:auto">'
        f'<h2>六大期货交易所新公告</h2><p>检查时间：{timestamp}</p>{html_list}</div>'
    )
    send_email(subject, text_body, html_body)


def send_daily(state: dict, errors: dict[str, str]) -> None:
    today = now_cn().strftime("%Y-%m-%d")
    rows = []
    for value in state["seen"].values():
        if str(value.get("first_seen", "")).startswith(today):
            rows.append(Notice("", value["site"], value["title"], value["url"], value.get("date", "")))
    rows.sort(key=lambda x: (x.site_name, x.date, x.title))
    text_list, html_list = notice_lines(rows)
    status_text = "\n".join(
        f"- {site.name}：{'检查失败：' + errors[site.code] if site.code in errors else '检查成功'}"
        for site in SITES
    )
    status_html = "".join(
        f"<li>{html.escape(site.name)}：{'检查失败' if site.code in errors else '检查成功'}</li>"
        for site in SITES
    )
    if not rows:
        text_list = "今日暂未发现新公告。"
        html_list = "<p>今日暂未发现新公告。</p>"
    send_email(
        f"【交易所公告日报】{today} 共{len(rows)}条",
        f"{today} 六大期货交易所公告日报\n\n{text_list}\n\n检查状态：\n{status_text}",
        '<div style="font-family:Arial,Microsoft YaHei,sans-serif;max-width:760px;margin:auto">'
        f"<h2>{today} 六大期货交易所公告日报</h2>{html_list}<h3>检查状态</h3><ul>{status_html}</ul></div>",
    )


def send_failure_alert(state: dict, errors: dict[str, str]) -> None:
    persistent = {
        code: message for code, message in errors.items()
        if int(state["failures"].get(code, 0)) == 3
    }
    if not persistent:
        return
    details = "\n".join(f"{code}: {message}" for code, message in persistent.items())
    send_email(
        "【监控异常】部分交易所连续3次检查失败",
        details,
        f'<pre style="white-space:pre-wrap">{html.escape(details)}</pre>',
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("initialize", "monitor", "daily", "test_email"), default="monitor")
    args = parser.parse_args()
    if args.mode == "test_email":
        stamp = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        send_email("【测试成功】交易所公告提醒邮箱已连接", f"测试时间：{stamp}", f"<h2>邮箱连接成功</h2><p>测试时间：{stamp}</p>")
        print("Test email sent")
        return 0

    state = load_state()
    had_seen = bool(state["seen"])
    all_items, new_items, errors = collect(state)
    if args.mode == "initialize" or not had_seen:
        print(f"Baseline initialized with {len(all_items)} notices; no old notices sent")
    elif args.mode == "monitor" and new_items:
        send_new(new_items)
        print(f"Sent {len(new_items)} new notices")
    elif args.mode == "daily":
        send_daily(state, errors)
        print("Daily digest sent")
    else:
        print("No new notices")
    if args.mode == "monitor":
        send_failure_alert(state, errors)
    save_state(state)
    for code, message in errors.items():
        print(f"WARNING {code}: {message}", file=sys.stderr)
    return 0 if len(errors) < len(SITES) else 2


if __name__ == "__main__":
    raise SystemExit(main())
