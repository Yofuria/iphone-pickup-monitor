#!/usr/bin/env python3
"""Apple Beijing pickup monitor. Python 3.9+, macOS/Linux, standard library only."""
import argparse
import base64
import datetime as dt
import email.utils
import fcntl
import getpass
import hashlib
import html
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parent
ENDPOINT = "/shop/retail/pickup-message"
BARK_ICON_URL = "https://www.apple.com/apple-touch-icon.png"
STOP = threading.Event()
PRINT_LOCK = threading.Lock()
LOGGER = logging.getLogger("pickup-monitor")
LOGGER.addHandler(logging.NullHandler())


def log(message):
    with PRINT_LOCK:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)
        LOGGER.info(message)


def products_for(config):
    return config.get("products") or [{key: config[key] for key in
                                      ("product_name", "part_number", "product_url")}]


def clean(value):
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


class QueryError(Exception):
    def __init__(self, message, retry_after=0, reset_session=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.reset_session = reset_session


def retry_seconds(value):
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (TypeError, ValueError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0


def backoff(failures, retry_after=0):
    return max(retry_after, min(300, 15 * 2 ** min(failures, 5)))


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("location", "city"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError("配置缺少 " + key)
    products = config.get("products")
    if products is None:
        products = [{key: config.get(key) for key in
                     ("product_name", "part_number", "product_url")}]
    if not isinstance(products, list) or not 1 <= len(products) <= 32:
        raise ValueError("products 必须包含 1～32 个型号")
    seen = set()
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("products 中每个型号必须为对象")
        for key in ("product_name", "part_number", "product_url"):
            if not isinstance(product.get(key), str) or not product[key].strip():
                raise ValueError("型号配置缺少 " + key)
        if not re.fullmatch(r"[A-Z0-9]+/A", product["part_number"]):
            raise ValueError("part_number 应为完整苹果产品编号，例如 MJT84CH/A")
        if product["part_number"] in seen:
            raise ValueError("products 包含重复的产品编号")
        seen.add(product["part_number"])
        url = urllib.parse.urlsplit(product["product_url"])
        if url.scheme != "https" or url.netloc != "www.apple.com.cn":
            raise ValueError("商品链接必须来自苹果中国大陆 HTTPS 官网")
    config["products"] = products
    # Retain normalized first-product fields for legacy single-product configs.
    config.update({key: products[0][key] for key in
                   ("product_name", "part_number", "product_url")})
    if not isinstance(config.get("stores"), dict) or not config["stores"]:
        raise ValueError("stores 必须包含需要监控的门店编号和名称")
    for key, low, high in (("interval_seconds", 30, 3600),
                           ("timeout_seconds", 1, 60),
                           ("max_cache_age_seconds", 0, 300)):
        number = float(config.get(key, {"interval_seconds": 30, "timeout_seconds": 60,
                                       "max_cache_age_seconds": 30}[key]))
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError("%s 必须介于 %s 和 %s" % (key, low, high))
        config[key] = number
    return config


def parse_stock(payload, config):
    """Only explicit product pickup availability is a positive signal."""
    if not isinstance(payload, dict):
        raise QueryError("库存响应不是对象")
    head = payload.get("head", {})
    if not isinstance(head, dict) or str(head.get("status", "200")) != "200":
        raise QueryError("库存接口业务状态异常")
    body = payload.get("body", {})
    stores = body.get("stores") if isinstance(body, dict) else None
    if not isinstance(stores, list) or not stores:
        raise QueryError("响应缺少门店列表，库存未知")
    result = {key: {"name": name, "available": None, "quote": "响应未包含此门店"}
              for key, name in config["stores"].items()}
    seen = set()
    for store in stores:
        if not isinstance(store, dict):
            raise QueryError("门店数据格式发生变化")
        key = store.get("storeNumber")
        if key not in result:
            continue
        if key in seen:
            raise QueryError("响应出现重复门店")
        seen.add(key)
        row = result[key]
        if store.get("city") not in (config["city"], config["city"] + "市"):
            row["quote"] = "门店城市不匹配"
            continue
        parts = store.get("partsAvailability", {})
        part = parts.get(config["part_number"]) if isinstance(parts, dict) else None
        if not isinstance(part, dict):
            row["quote"] = "缺少目标型号数据"
            continue
        messages = part.get("messageTypes", {})
        regular = messages.get("regular", {}) if isinstance(messages, dict) else {}
        if not isinstance(regular, dict):
            regular = {}
        title = regular.get("storePickupProductTitle", "")
        if title and re.sub(r"\s", "", title) != re.sub(r"\s", "", config["product_name"]):
            row["quote"] = "响应产品名称与配置不一致"
            continue
        display = part.get("pickupDisplay")
        if display == "available" and part.get("storePickEligible") is True:
            row["available"] = True
        elif display in ("unavailable", "ineligible"):
            row["available"] = False
        row["quote"] = clean(part.get("pickupSearchQuote") or regular.get("storePickupQuote"))
        if row["available"] is None:
            row["quote"] = "未识别的自提状态：" + str(display)
    return result


def parse_all_stock(payload, config):
    result = {}
    for product in products_for(config):
        rows = parse_stock(payload, {**config, **product})
        for store_id, row in rows.items():
            result[store_id + "|" + product["part_number"]] = {
                **row, **product, "store_id": store_id}
    return result


def stock_alerts(rows, checked_at):
    """Group stores for the same SKU; each alert links to that exact product."""
    groups = {}
    for row in rows:
        groups.setdefault(row["part_number"], []).append(row)
    for group in groups.values():
        body = group[0]["product_name"] + "\n" + "\n".join(
            row["name"] + "：" + row["quote"] for row in group)
        yield "北京 Apple 自提有货", body + "\n检测时间：" + checked_at, group[0]["product_url"]


def find_chromium():
    candidates = []
    if sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise QueryError("未找到 Google Chrome 或 Microsoft Edge，库存未知",
                     reset_session=True)


class DevToolsSocket:
    """Small RFC 6455 client for Chrome DevTools; avoids third-party dependencies."""
    MAX_MESSAGE = 8 * 1024 * 1024

    def __init__(self, url, timeout):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise QueryError("Chrome 调试地址无效", reset_session=True)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.socket.settimeout(timeout)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"
        request = (
            "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\nOrigin: http://127.0.0.1\r\n\r\n"
            % (path, parsed.hostname, parsed.port, key)
        ).encode("ascii")
        self.socket.sendall(request)
        header = self._read_header()
        first = header.split(b"\r\n", 1)[0]
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii").lower()
        headers = {}
        for line in header.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower()] = value.strip().lower()
        if b" 101 " not in first or headers.get(b"sec-websocket-accept", b"").decode() != expected:
            self.close()
            raise QueryError("Chrome 调试连接握手失败", reset_session=True)

    def _read_header(self):
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise QueryError("Chrome 调试连接提前关闭", reset_session=True)
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise QueryError("Chrome 调试握手响应过大", reset_session=True)
        marker = self.buffer.index(b"\r\n\r\n") + 4
        header = bytes(self.buffer[:marker])
        del self.buffer[:marker]
        return header

    def _read_exact(self, size):
        while len(self.buffer) < size:
            chunk = self.socket.recv(min(65536, size - len(self.buffer)))
            if not chunk:
                raise QueryError("Chrome 调试连接意外关闭", reset_session=True)
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _send_frame(self, opcode, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def send_json(self, value):
        self._send_frame(1, json.dumps(value, separators=(",", ":"), ensure_ascii=False))

    def receive_json(self):
        message = bytearray()
        started = False
        while True:
            first, second = struct.unpack("!BB", self._read_exact(2))
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read_exact(8))[0]
            if size > self.MAX_MESSAGE or len(message) + size > self.MAX_MESSAGE:
                raise QueryError("Chrome 调试响应超过安全上限", reset_session=True)
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(size)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 8:
                raise QueryError("Chrome 调试连接被关闭", reset_session=True)
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                message = bytearray(payload)
                started = True
            elif opcode == 0 and started:
                message.extend(payload)
            else:
                continue
            if final:
                try:
                    return json.loads(message.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise QueryError("Chrome 调试响应无法解析", reset_session=True) from None

    def close(self):
        sock = getattr(self, "socket", None)
        self.socket = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def browser_inventory_request(config, store_id):
    pairs = [("fae", "true"), ("pl", "true"), ("mts.0", "regular")]
    pairs.extend(("parts.%d" % index, product["part_number"])
                 for index, product in enumerate(products_for(config)))
    pairs.append(("store", store_id))
    return {"url": "https://www.apple.com.cn" + ENDPOINT,
            "pairs": pairs, "max_bytes": 4 * 1024 * 1024}


def browser_fetch_expression(request):
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    return """(async()=>{
const request=%s;
const url=new URL(request.url);
for(const [key,value] of request.pairs) url.searchParams.append(key,value);
const response=await fetch(url.toString(),{
 credentials:'same-origin',
 headers:{'Accept':'application/json, text/javascript, */*; q=0.01','X-Requested-With':'XMLHttpRequest'}
});
const body=await response.text();
const bytes=new TextEncoder().encode(body).length;
if(bytes>request.max_bytes) return {status:0,body:'response too large',age:'0',retryAfter:'0'};
return {status:response.status,body,age:response.headers.get('Age')||'0',retryAfter:response.headers.get('Retry-After')||'0'};
})()""" % encoded


def decode_browser_payload(value, config, store_id):
    if not isinstance(value, dict):
        raise QueryError("Chrome 未返回库存响应", reset_session=True)
    status = value.get("status")
    if status in (403, 541):
        raise QueryError("苹果接口 HTTP %s，库存未知" % status, reset_session=True)
    if status == 429 or isinstance(status, int) and status >= 500:
        raise QueryError("苹果接口 HTTP %s，库存未知" % status,
                         retry_seconds(value.get("retryAfter")) or 60)
    if status != 200:
        raise QueryError("苹果接口 HTTP %s，库存未知" % status, reset_session=True)
    body = value.get("body")
    if not isinstance(body, str) or not body.lstrip().startswith(("{", "[")):
        raise QueryError("苹果接口返回非 JSON，库存未知", reset_session=True)
    age = retry_seconds(value.get("age", "0"))
    if age > config["max_cache_age_seconds"]:
        raise QueryError("接口缓存已 %s 秒，拒绝据此发送有货提醒" % age)
    try:
        parsed = json.loads(body)
    except ValueError:
        raise QueryError("苹果接口 JSON 无法解析，库存未知", reset_session=True) from None
    rows = parse_all_stock(parsed, config)
    selected = {}
    for product in products_for(config):
        key = store_id + "|" + product["part_number"]
        selected[key] = rows[key]
    return selected, age


class ChromiumSession:
    START_TIMEOUT = 12
    READY_TIMEOUT = 50
    COMMAND_TIMEOUT = 30
    MIN_REQUEST_INTERVAL = 2

    def __init__(self, config):
        self.config = config
        self.process = None
        self.profile = None
        self.devtools = None
        self.command_id = 0
        self.ready = False
        self.last_request = None
        self._start()

    def _start(self):
        chrome = find_chromium()
        self.profile = tempfile.TemporaryDirectory(prefix="apple-monitor-chromium-")
        args = [chrome, "--headless=new", "--remote-debugging-port=0",
                "--remote-allow-origins=http://127.0.0.1", "--user-data-dir=" + self.profile.name,
                "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                "--disable-blink-features=AutomationControlled", "--no-first-run",
                "--no-default-browser-check", "--disable-background-networking", "--disable-sync",
                "--disable-default-apps", "--disable-extensions", "about:blank"]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.profile.name) / "DevToolsActivePort"
        deadline = time.monotonic() + self.START_TIMEOUT
        port = None
        while time.monotonic() < deadline and not STOP.is_set():
            if self.process.poll() is not None:
                self.close()
                raise QueryError("Chrome 启动后立即退出", reset_session=True)
            try:
                port = int(port_file.read_text().splitlines()[0])
                break
            except (FileNotFoundError, ValueError, IndexError):
                time.sleep(0.1)
        if port is None:
            self.close()
            raise QueryError("等待 Chrome 启动超时", reset_session=True)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/json/list")
            response = connection.getresponse()
            targets = json.loads(response.read(1048576))
        except (OSError, http.client.HTTPException, ValueError):
            self.close()
            raise QueryError("无法读取 Chrome 页面目标", reset_session=True) from None
        finally:
            connection.close()
        target = next((item for item in targets if item.get("type") == "page" and
                       item.get("webSocketDebuggerUrl")), None)
        if not target:
            self.close()
            raise QueryError("Chrome 没有可用页面目标", reset_session=True)
        self.devtools = DevToolsSocket(target["webSocketDebuggerUrl"], self.COMMAND_TIMEOUT)

    def command(self, method, params=None):
        self.command_id += 1
        command_id = self.command_id
        self.devtools.send_json({"id": command_id, "method": method, "params": params or {}})
        while True:
            response = self.devtools.receive_json()
            if response.get("id") != command_id:
                continue
            if response.get("error"):
                raise QueryError("Chrome 命令 %s 失败" % method, reset_session=True)
            return response.get("result", {})

    def evaluate(self, expression, await_promise=False):
        result = self.command("Runtime.evaluate", {
            "expression": expression, "awaitPromise": await_promise, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise QueryError("Chrome 页面执行库存查询失败", reset_session=True)
        return result.get("result", {}).get("value")

    def ensure_ready(self):
        if self.ready:
            return
        self.command("Page.navigate", {"url": "https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro"})
        deadline = time.monotonic() + self.READY_TIMEOUT
        expression = "JSON.stringify({readyState:document.readyState,cookies:document.cookie.split(';').map(x=>x.trim().split('=')[0]).filter(Boolean)})"
        while time.monotonic() < deadline and not STOP.is_set():
            state = self.evaluate(expression)
            try:
                state = json.loads(state)
            except (TypeError, ValueError):
                state = {}
            cookies = state.get("cookies", [])
            if state.get("readyState") != "loading" and "shld_bt_ck" in cookies and "as_atb" in cookies:
                self.ready = True
                log("Chrome 已完成 Apple 页面风控握手。")
                return
            time.sleep(0.25)
        raise QueryError("Apple 页面未能完成风控握手，库存未知", reset_session=True)

    def fetch_store(self, store_id):
        self.ensure_ready()
        if self.last_request is not None:
            remaining = self.MIN_REQUEST_INTERVAL - (time.monotonic() - self.last_request)
            if remaining > 0:
                wait_for_stop(remaining)
        if STOP.is_set():
            raise QueryError("监控正在停止", reset_session=True)
        self.last_request = time.monotonic()
        value = self.evaluate(browser_fetch_expression(
            browser_inventory_request(self.config, store_id)), await_promise=True)
        return decode_browser_payload(value, self.config, store_id)

    def close(self):
        if self.devtools:
            self.devtools.close()
            self.devtools = None
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                    self.process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.process = None
        if self.profile:
            self.profile.cleanup()
            self.profile = None


class AppleClient:
    def __init__(self, config):
        self.config = config
        self.session = None

    def close(self):
        if self.session:
            self.session.close()
            self.session = None

    def query(self):
        started = time.monotonic()
        if self.session is None:
            self.session = ChromiumSession(self.config)
        try:
            rows = {}
            ages = []
            for store_id in self.config["stores"]:
                store_rows, age = self.session.fetch_store(store_id)
                rows.update(store_rows)
                ages.append(age)
            expected = len(products_for(self.config)) * len(self.config["stores"])
            if len(rows) != expected:
                raise QueryError("浏览器库存响应不完整，库存未知", reset_session=True)
            return rows, round((time.monotonic() - started) * 1000), max(ages, default=0)
        except QueryError as exc:
            if exc.reset_session:
                self.close()
            raise
        except (OSError, ValueError) as exc:
            self.close()
            raise QueryError("Chrome 库存查询失败（%s），库存未知" % type(exc).__name__,
                             reset_session=True) from None


class Changes:
    def __init__(self):
        self.known = {}

    def update(self, rows):
        alerts = []
        for key, row in rows.items():
            value = row["available"]
            # Unknown does not reset a known state, preventing false restock alerts.
            if value is None:
                continue
            if value and self.known.get(key) is not True:
                alerts.append(row)
            self.known[key] = value
        return alerts


def validate_bark(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or len(parsed.path.strip("/").split("/")) != 1
            or not parsed.path.strip("/")):
        raise ValueError("Bark 地址应为 https://api.day.app/你的Key（也支持 HTTPS 自建域名）")
    return value.strip().rstrip("/")


def parse_bark_urls(value):
    urls = []
    for item in re.split(r"[,\n]+", value):
        if not item.strip():
            continue
        url = validate_bark(item)
        if url not in urls:
            urls.append(url)
    if len(urls) > 8:
        raise ValueError("Bark 地址最多配置 8 个")
    return urls


def bark_urls():
    value = (os.environ.get("BARK_URLS", "").strip()
             or os.environ.get("BARK_URL", "").strip())
    secret = ROOT / ".bark-url"
    if not value and secret.exists():
        value = secret.read_text(encoding="utf-8").strip()
    return parse_bark_urls(value)


def send_bark(url, title, body, product_url):
    parsed = urllib.parse.urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=6,
                                              context=ssl.create_default_context())
    payload = json.dumps({"title": title, "body": body, "url": product_url,
                          "group": "iPhone北京自提", "level": "timeSensitive",
                          "sound": "alarm", "icon": BARK_ICON_URL},
                         ensure_ascii=False).encode("utf-8")
    try:
        connection.request("POST", parsed.path, body=payload,
                           headers={"Content-Type": "application/json; charset=utf-8"})
        response = connection.getresponse()
        data = response.read(65536)
        if response.status != 200 or json.loads(data).get("code") != 200:
            raise RuntimeError("Bark 服务未确认接收")
    finally:
        connection.close()


def send_desktop(config, title, body, product_url):
    if config.get("sound", True):
        with PRINT_LOCK:
            print("\a", end="", flush=True)
        if sys.platform == "darwin":
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                           timeout=5, check=True, capture_output=True)
    if not config.get("desktop_notifications", True):
        return
    if sys.platform == "darwin":
        # User data is passed as argv, never interpolated into AppleScript code.
        script = ('on run argv\n'
                  'display notification (item 2 of argv) with title (item 1 of argv)\n'
                  'end run')
        subprocess.run(["osascript", "-e", script, title, body],
                       timeout=5, check=True, capture_output=True)
    else:
        subprocess.run(["notify-send", title, body], timeout=5, check=True, capture_output=True)


class Notifications:
    """Separate queues ensure Bark retries never block local alerts or polling."""
    def __init__(self, config, barks):
        self.config = config
        self.channels = []
        self.failed = threading.Event()
        if config.get("sound", True) or config.get("desktop_notifications", True):
            self.add("电脑", lambda *args: send_desktop(config, *args))
        for index, bark in enumerate(barks, 1):
            self.add("Bark %s" % index,
                     lambda *args, bark=bark: send_bark(bark, *args))

    def add(self, name, sender):
        jobs = queue.Queue(maxsize=100)
        thread = threading.Thread(target=self.worker, args=(name, sender, jobs), daemon=True)
        thread.start()
        self.channels.append((name, jobs, thread))

    def worker(self, name, sender, jobs):
        while True:
            event = jobs.get()
            try:
                if event is None:
                    return
                created, args = event
                for attempt in range(3):
                    if time.monotonic() - created > 90:
                        self.failed.set()
                        log(name + "：提醒超过 90 秒，已丢弃；请查最新库存")
                        break
                    try:
                        sender(*args)
                        log(name + "：通知已提交（实际展示取决于设备通知设置）")
                        break
                    except Exception as exc:
                        # Do not log exception messages: they may include a secret URL.
                        log("%s：通知失败 %s/3（%s）" % (name, attempt + 1, type(exc).__name__))
                        if attempt == 2:
                            self.failed.set()
                        if attempt < 2 and STOP.wait(2 ** attempt):
                            self.failed.set()
                            break
            finally:
                jobs.task_done()

    def send(self, title, body, product_url=None, include_bark=True):
        log(title + " | " + body.replace("\n", " | "))
        event = (time.monotonic(), (title, body, product_url or self.config["product_url"]))
        for name, jobs, _ in self.channels:
            if name.startswith("Bark") and not include_bark:
                continue
            try:
                jobs.put_nowait(event)
            except queue.Full:
                self.failed.set()
                log(name + "：通知队列已满，本次通知未提交，请查看终端记录")

    def finish(self):
        for _, jobs, _ in self.channels:
            jobs.put(None)
        for _, _, thread in self.channels:
            thread.join(timeout=30)
            if thread.is_alive():
                self.failed.set()
        return not self.failed.is_set()


def write_status(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def wait_for_stop(seconds):
    """Allow a local stop request during a long API cooldown without signaling unrelated PIDs."""
    deadline = time.monotonic() + seconds
    request = ROOT / "runtime" / "stop.request"
    while not STOP.is_set():
        try:
            if request.read_text().strip() == str(os.getpid()):
                STOP.set()
                break
        except FileNotFoundError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        STOP.wait(min(1, remaining))


def request_stop():
    runtime = ROOT / "runtime"
    pid_path = runtime / "monitor.pid"
    if not pid_path.exists():
        log("未找到新版监控进程的运行标记。")
        return 0
    pid = pid_path.read_text().strip()
    if not pid.isdigit():
        raise ValueError("运行标记无效")
    (runtime / "stop.request").write_text(pid, encoding="utf-8")
    for _ in range(10):
        if not pid_path.exists():
            log("监控已停止。")
            return 0
        time.sleep(1)
    log("已提交停止请求，程序可能仍在完成网络请求或通知发送。")
    return 0


def monitor(config, once=False, silent=False, count=None):
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    lock = (runtime / "monitor.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("已有监控程序运行，请勿重复启动") from None
    handler = RotatingFileHandler(runtime / "monitor.log", maxBytes=2_000_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    (runtime / "stop.request").unlink(missing_ok=True)
    (runtime / "monitor.pid").write_text(str(os.getpid()), encoding="utf-8")
    client = AppleClient(config)
    barks = bark_urls()
    notify = None if silent else Notifications(config, barks)
    if not barks and not silent:
        log("Bark 尚未配置；当前只有电脑提醒。运行 python3 monitor.py --setup-bark 配置。")
    elif len(barks) > 1 and not silent:
        log("Bark 已配置 %s 台设备。" % len(barks))
    changes = Changes()
    failures = 0
    health = "starting"
    last_success = None
    previous_rows = None
    heartbeat = 0
    loops = 0
    exit_code = 0
    product_count = len(products_for(config))
    log("监控 %s 个型号；%s 家北京门店；%s 个组合；Chrome 完整轮次间隔 %s 秒" %
        (product_count, len(config["stores"]), product_count * len(config["stores"]),
         config["interval_seconds"]))
    try:
        # A restart must not discard an active server/API cooldown.
        try:
            previous = json.loads((runtime / "status.json").read_text())
            if previous.get("health") == "error":
                due = dt.datetime.fromisoformat(previous["checked_at"]).timestamp() + previous.get("retry_seconds", 0)
                remaining = max(0, due - time.time())
                if remaining:
                    log("继续上次接口退避，%.1f 秒后查询" % remaining)
                    wait_for_stop(remaining)
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        while not STOP.is_set():
            started = time.monotonic()
            checked_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            delay = config["interval_seconds"]
            try:
                rows, elapsed_ms, age = client.query()
                unknown = [r for r in rows.values() if r["available"] is None]
                new_health = "partial" if unknown else "ok"
                if health in ("error", "partial") and new_health == "ok" and notify:
                    notify.send("库存监控已恢复", "北京全部 %s 个型号/门店组合均已成功查询。" % len(rows))
                if new_health == "partial" and health != "partial" and notify:
                    notify.send("部分库存未知", "%s 个型号/门店组合未知；详情见运行日志。" % len(unknown),
                                include_bark=False)
                health = new_health
                failures = 0
                last_success = checked_at if not unknown else last_success
                alerts = changes.update(rows)
                if alerts and notify:
                    for title, body, url in stock_alerts(alerts, checked_at):
                        notify.send(title, body, url)
                write_status(runtime / "status.json", {
                    "health": health, "checked_at": checked_at, "last_success": last_success,
                    "pid": os.getpid(), "product_count": product_count, "combination_count": len(rows),
                    "query_backend": "chromium", "request_ms": elapsed_ms,
                    "cache_age_seconds": age, "stores": rows})
                if rows != previous_rows or time.monotonic() - heartbeat >= 60 or once or count:
                    log("查询耗时 %sms，状态 %s，缓存 Age=%ss" % (elapsed_ms, health, age))
                    for product in products_for(config):
                        group = [r for r in rows.values() if r["part_number"] == product["part_number"]]
                        counts = {value: sum(r["available"] is value for r in group)
                                  for value in (True, False, None)}
                        log("  %s：%s 店可自提 / %s 店不可自提 / %s 店未知" %
                            (product["product_name"], counts[True], counts[False], counts[None]))
                        for row in group:
                            if row["available"] is not False:
                                log("    %s：%s" % (row["name"], row["quote"]))
                    heartbeat = time.monotonic()
                previous_rows = rows
                exit_code = 2 if unknown else 0
                # Start-to-start schedule, no overlapping requests and no catch-up burst.
                delay = max(0, config["interval_seconds"] - (time.monotonic() - started))
            except QueryError as exc:
                failures += 1
                delay = backoff(failures, exc.retry_after)
                log("%s；%.1f 秒后重试" % (exc, delay))
                write_status(runtime / "status.json", {
                    "health": "error", "checked_at": checked_at, "last_success": last_success,
                    "pid": os.getpid(), "product_count": product_count,
                    "query_backend": "chromium",
                    "error": str(exc), "retry_seconds": delay, "stores": {}})
                if health != "error" and notify:
                    notify.send("库存监控暂时异常", str(exc) + "；程序将自动退避重试。",
                                include_bark=False)
                health = "error"
                exit_code = 2
            loops += 1
            if once or (count is not None and loops >= count):
                break
            wait_for_stop(delay)
    finally:
        client.close()
        if notify:
            notify.finish()
        log("监控已停止。")
        (runtime / "monitor.pid").unlink(missing_ok=True)
        LOGGER.removeHandler(handler)
        handler.close()
        lock.close()
    return exit_code


def main():
    parser = argparse.ArgumentParser(description="北京 iPhone 18 Pro / Pro Max 多机型自提库存监控")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--once", action="store_true", help="查询一轮后退出")
    parser.add_argument("--count", type=int, help="查询指定轮数后退出（联调使用）")
    parser.add_argument("--silent", action="store_true", help="只记录结果，不发通知")
    parser.add_argument("--setup-bark", action="store_true", help="本机安全保存 Bark 推送地址")
    parser.add_argument("--add-bark", action="store_true", help="追加 Bark 推送地址，不覆盖已有设备")
    parser.add_argument("--test-notify", action="store_true", help="向已配置的电脑/Bark 发送测试提醒")
    parser.add_argument("--stop", action="store_true", help="请求停止本目录的新版监控服务")
    args = parser.parse_args()
    if args.count is not None and args.count < 1:
        parser.error("--count 必须大于 0")
    try:
        if args.stop:
            return request_stop()
        if args.setup_bark or args.add_bark:
            values = parse_bark_urls(getpass.getpass(
                "粘贴 Bark 基础推送地址（多台用英文逗号分隔，输入不显示）："))
            if not values:
                raise ValueError("至少需要一个 Bark 地址")
            path = ROOT / ".bark-url"
            if args.add_bark and path.exists():
                existing = parse_bark_urls(path.read_text(encoding="utf-8"))
                values = parse_bark_urls("\n".join(existing + values))
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write("\n".join(values) + "\n")
            log("Bark 配置已保存 %s 台设备。可运行 --test-notify 测试。" % len(values))
            return 0
        config = load_config(args.config)
        if args.test_notify:
            barks = bark_urls()
            if not barks:
                log("Bark 尚未配置，本次只测试电脑提醒。")
            notification = Notifications(config, barks)
            notification.send("自提监控测试（不是有货提醒）", "北京 %s 个型号的库存通知测试" % len(products_for(config)))
            return 0 if notification.finish() else 2
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: STOP.set())
        return monitor(config, args.once, args.silent, args.count)
    except (OSError, ValueError) as exc:
        log("启动/保存失败：" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
        return 2


if __name__ == "__main__":
    sys.exit(main())
