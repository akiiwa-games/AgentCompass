"""Sign in once in a dedicated Chrome, then import the account into Grok2API or Muse2API.

A thin shell over the two gateways: nothing here touches their clones. It starts the installed Chrome on its own
profile with a loopback debugging port, opens the site's sign-in page, waits until the session cookie appears,
reads it over CDP (httpOnly cookies included) and posts it to the gateway's own admin API:

    Grok2API  POST /api/admin/v1/auth/login, then the cookie `sso` as a one-line TXT file to
              POST /api/admin/v1/accounts/web/import (an SSE stream ending in `complete` or `error`)
    Muse2API  POST /admin/accounts {"label", "cookies", "cookie_expires"} with the admin key (= its API key)

The profile persists, so a later import reuses the session without signing in again. Cookies are never written to
disk by this module; they go from the browser straight to the local gateway.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import gateways
import service_ports
from i18n import translate as _tr

PORT = service_ports.get("account_login")
PROFILE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "AgentCompass Account Login" / "chrome-profile"
CDP = f"http://127.0.0.1:{PORT}"
CREATE_NO_WINDOW = 0x08000000

SITES = {
    "grok2api": {"name": "Grok", "url": "https://grok.com/", "domain": "grok.com", "required": ("sso",)},
    "muse2api": {"name": "muse.ai", "url": "https://muse.ai/", "domain": "muse.ai",
                 "required": ("hatch_sess", "hatch_native_auth_device")},
}


class PasswordNeeded(Exception):
    """The Grok2API bootstrap password no longer works (the user changed it)."""


# ---------- the dedicated browser ----------

def _get(url: str, timeout: float = 3) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError):
        return None


def browser_running() -> bool:
    return isinstance(_get(f"{CDP}/json/version"), dict)


def open_page(url: str) -> str:
    """Show the sign-in page: start the dedicated Chrome, or open a tab in it when it already runs."""
    if browser_running():
        request = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
        urllib.request.urlopen(request, timeout=5).read()
        return _tr("已在登录窗口打开新标签")
    if not gateways.CHROME.is_file():
        raise FileNotFoundError(_tr('找不到 Chrome：{0}').format(f'{gateways.CHROME}'))
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(gateways.CHROME), f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1",
                      f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", url],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=CREATE_NO_WINDOW, close_fds=True)
    deadline = time.monotonic() + 20
    while not browser_running():
        if time.monotonic() >= deadline:
            raise RuntimeError(_tr("登录窗口没有启动（调试端口无响应）"))
        time.sleep(0.3)
    return _tr("已打开登录窗口")


def _cdp(method: str, params: dict | None = None) -> dict:
    from websockets.sync.client import connect

    version = _get(f"{CDP}/json/version")
    if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
        raise RuntimeError(_tr("登录窗口已关闭"))
    with connect(version["webSocketDebuggerUrl"], max_size=None, open_timeout=5) as socket:
        socket.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            message = json.loads(socket.recv(timeout=10))
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError(f"CDP {method}：{message['error'].get('message')}")
                return message.get("result", {})


def site_cookies(domain: str) -> list[dict]:
    """Every cookie of the domain and its subdomains, httpOnly ones included."""
    cookies = _cdp("Storage.getCookies").get("cookies", [])
    return [c for c in cookies if c.get("domain", "").lstrip(".") == domain
            or c.get("domain", "").endswith("." + domain)]


def close_browser() -> None:
    try:
        _cdp("Browser.close")
    except Exception:
        pass    # already closed by the user


def wait_for_session(plugin_id: str, timeout: float = 600, poll: float = 2) -> list[dict]:
    """Open the sign-in page and wait until the site's session cookies exist (immediately if already signed in)."""
    site = SITES[plugin_id]
    open_page(site["url"])
    deadline = time.monotonic() + timeout
    while True:
        try:
            cookies = site_cookies(site["domain"])
        except RuntimeError:
            if not browser_running():
                raise RuntimeError(_tr('登录窗口被关闭，{0} 账号未导入').format(site['name'])) from None
            cookies = []
        names = {c["name"] for c in cookies if c.get("value")}
        if all(name in names for name in site["required"]):
            return cookies
        if time.monotonic() >= deadline:
            raise TimeoutError(_tr('{0} 分钟内没有完成 {1} 登录，未导入').format(int(timeout // 60), site['name']))
        time.sleep(poll)


# ---------- the gateways' admin APIs ----------

def _call(request: urllib.request.Request, timeout: float = 120) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def _error_text(body: str) -> str:
    try:
        error = json.loads(body).get("error")
        return error.get("message") or error.get("code") if isinstance(error, dict) else str(error or body)
    except ValueError:
        return body[:200]


def grok_login(password: str | None = None) -> str:
    user, bootstrap = gateways.grok_admin()
    body = json.dumps({"username": user or "admin", "password": password or bootstrap}).encode()
    status, text = _call(urllib.request.Request(f"{gateways.GROK_URL}/api/admin/v1/auth/login", data=body,
                                                method="POST", headers={"Content-Type": "application/json"}), 30)
    if status == 401:
        raise PasswordNeeded(_tr("Grok2API 管理员密码已修改，请输入当前密码"))
    if status != 200:
        raise RuntimeError(_tr('Grok2API 管理员登录失败（{0}）：{1}').format(status, _error_text(text)))
    return json.loads(text)["data"]["tokens"]["accessToken"]


def sse_events(text: str) -> list[tuple[str, str]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, "\n".join(data)))
    return events


def grok_import_sso(sso: str, password: str | None = None) -> str:
    token = grok_login(password)
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"sso.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\n{sso}\n\r\n--{boundary}--\r\n").encode()
    status, text = _call(urllib.request.Request(
        f"{gateways.GROK_URL}/api/admin/v1/accounts/web/import", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"}), 300)
    if status != 200:
        raise RuntimeError(_tr('Grok2API 导入失败（{0}）：{1}').format(status, _error_text(text)))
    for name, data in reversed(sse_events(text)):
        if name == "complete":
            result = json.loads(data)
            return _tr('Grok Web 账号已导入：新增 {0}，更新 {1}，跳过 {2}；同步成功 {3}，失败 {4}').format(
                *(result.get(key, 0) for key in ("created", "updated", "skipped", "synced", "syncFailed")))
        if name == "error":
            raise RuntimeError(_tr('Grok2API 导入失败：{0}').format(_error_text(data)))
    raise RuntimeError(_tr("Grok2API 导入没有返回结果"))


def _mask(value: str) -> str:
    """Muse2API's own masking of cookie values in its account list (first four ... last four)."""
    return "***" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}"


def muse_import(cookies: list[dict], label: str = "") -> str:
    """Add the account, or refresh the cookies of the one already holding this session (no duplicates)."""
    key = gateways.muse_api_key()
    if not key:
        raise RuntimeError(_tr("Muse2API 还没有生成 API Key，先启动一次"))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    values = {c["name"]: c["value"] for c in cookies}
    expires = {c["name"]: int(c["expires"]) for c in cookies if c.get("expires", -1) > 0}
    status, text = _call(urllib.request.Request(f"{gateways.MUSE_URL}/admin/accounts", headers=headers), 30)
    if status != 200:
        raise RuntimeError(_tr('Muse2API 读取账号失败（{0}）：{1}').format(status, _error_text(text)))
    session = _mask(values["hatch_sess"])
    existing = next((a for a in json.loads(text).get("data", [])
                     if a.get("cookies", {}).get("hatch_sess") == session), None)
    if existing:
        request = urllib.request.Request(f"{gateways.MUSE_URL}/admin/accounts/{existing['id']}", method="PATCH",
                                         data=json.dumps({"cookies": values, "cookie_expires": expires}).encode(),
                                         headers=headers)
    else:
        label = label or f"login-{time.strftime('%Y%m%d-%H%M')}"
        request = urllib.request.Request(f"{gateways.MUSE_URL}/admin/accounts", method="POST", headers=headers,
                                         data=json.dumps({"label": label, "cookies": values,
                                                          "cookie_expires": expires}).encode())
    status, text = _call(request, 60)
    if status not in (200, 201):
        raise RuntimeError(_tr('Muse2API 导入失败（{0}）：{1}').format(status, _error_text(text)))
    if existing:
        return _tr('muse.ai 账号已存在（{0}），已刷新 cookie').format(existing.get('label') or existing['id'])
    missing = json.loads(text).get("missing_cookies") or []
    note = _tr('；缺少 {0}，可能无法生成').format(', '.join(missing)) if missing else ""
    return _tr('muse.ai 账号已导入（{0}，{1} 个 cookie）{2}').format(label, len(values), note)


def login_and_import(plugin_id: str, password: str | None = None, timeout: float = 600) -> str:
    """The panel button: sign in (or reuse the session), import, then close the login window."""
    cookies = wait_for_session(plugin_id, timeout)
    try:
        if plugin_id == "grok2api":
            sso = next(c["value"] for c in cookies if c["name"] == "sso")
            return grok_import_sso(sso, password)
        return muse_import(cookies)
    finally:
        close_browser()
