from __future__ import annotations

import logging
import os
import time
from typing import Optional


LOGIN_URL = "https://www.autodl.com/login"
PASSPORT_URL = "https://www.autodl.com/api/v1/passport"
DEFAULT_LOGIN_TIMEOUT_MS = 15000
DEFAULT_LOGIN_RETRIES = 3
DEFAULT_POST_LOGIN_WAIT_SECONDS = 8
PHONE_INPUT_SELECTORS = (
    'input[name="phone"]',
    'input[placeholder*="手机号"]',
    'input[placeholder*="手机号码"]',
    'input[type="tel"]',
    'input[type="text"]',
)
PASSWORD_INPUT_SELECTORS = (
    'input[name="password"]',
    'input[type="password"]',
    'input[placeholder*="密码"]',
)
LOGIN_BUTTON_SELECTORS = (
    'button.el-button--primary',
    'button:has-text("登录")',
    'button:has-text("立即登录")',
    'button:has-text("Sign in")',
    'button[type="submit"]',
)
CAPTCHA_SELECTORS = (
    'canvas',
    'img[alt*="验证码"]',
    'input[placeholder*="验证码"]',
    '.geetest_panel',
    '.yidun',
    '.captcha',
)
CAPTCHA_TEXT_HINTS = (
    '验证码',
    '滑块',
    '人机验证',
    '安全验证',
    '请完成验证',
    'geetest',
    'yidun',
)
LOGIN_BLOCKER_TEXT_HINTS = (
    '访问过于频繁',
    '稍后再试',
    '系统繁忙',
    '账号异常',
    '短信验证',
)

logger = logging.getLogger(__name__)


def build_browser_launch_kwargs(headed: bool, executable_path: str | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {"headless": not headed}
    if executable_path:
        kwargs["executable_path"] = executable_path
    return kwargs


def _safe_is_visible(locator, timeout_ms: int = 1000) -> bool:
    try:
        return bool(locator.is_visible(timeout=timeout_ms))
    except TypeError:
        try:
            return bool(locator.is_visible())
        except Exception:
            return False
    except Exception:
        return False


def _safe_count(locator) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def _safe_text(locator) -> str:
    try:
        return str(locator.text_content() or "")
    except Exception:
        return ""


def _page_title(page) -> str:
    try:
        return str(page.title() or "")
    except Exception:
        return ""


def _page_body_text(page) -> str:
    try:
        return _safe_text(page.locator("body").first)
    except Exception:
        return ""


def find_first_visible_locator(page, selectors: tuple[str, ...], timeout_ms: int = 1000):
    for selector in selectors:
        try:
            locator = page.locator(selector)
        except Exception:
            continue
        target = getattr(locator, "first", locator)
        if _safe_count(locator) <= 0 and not _safe_is_visible(target, timeout_ms):
            continue
        if _safe_is_visible(target, timeout_ms):
            return target, selector
    return None, ""


def detect_login_blocker(page) -> str:
    body_text = _page_body_text(page)
    combined = f"{_page_title(page)}\n{body_text}".lower()
    for selector in CAPTCHA_SELECTORS:
        try:
            locator = page.locator(selector)
        except Exception:
            continue
        target = getattr(locator, "first", locator)
        if _safe_count(locator) > 0 and _safe_is_visible(target):
            return f"检测到验证码或人机验证元素: {selector}"
    if any(hint.lower() in combined for hint in CAPTCHA_TEXT_HINTS):
        return "页面提示需要验证码或人机验证，请人工处理后再重试"
    if any(hint.lower() in combined for hint in LOGIN_BLOCKER_TEXT_HINTS):
        return "登录页出现风控或异常提示，请稍后再试或改为人工登录"
    return ""


def describe_login_page(page) -> str:
    title = _page_title(page) or "-"
    url = getattr(page, "url", "") or "-"
    body = _page_body_text(page).strip().replace("\n", " ")
    excerpt = body[:120] if body else "-"
    return f"title={title}; url={url}; body_excerpt={excerpt}"


def resolve_login_form(page, timeout_ms: int, auth_error_cls: type[Exception]):
    blocker = detect_login_blocker(page)
    if blocker:
        raise auth_error_cls(blocker)
    phone_input, phone_selector = find_first_visible_locator(page, PHONE_INPUT_SELECTORS, timeout_ms)
    password_input, password_selector = find_first_visible_locator(page, PASSWORD_INPUT_SELECTORS, timeout_ms)
    login_button, login_button_selector = find_first_visible_locator(page, LOGIN_BUTTON_SELECTORS, timeout_ms)
    if phone_input is None or password_input is None or login_button is None:
        missing = []
        if phone_input is None:
            missing.append("手机号输入框")
        if password_input is None:
            missing.append("密码输入框")
        if login_button is None:
            missing.append("登录按钮")
        raise auth_error_cls(f"未找到登录表单关键元素: {', '.join(missing)}；{describe_login_page(page)}")
    return {
        "phone_input": phone_input,
        "password_input": password_input,
        "login_button": login_button,
        "phone_selector": phone_selector,
        "password_selector": password_selector,
        "login_button_selector": login_button_selector,
    }


def _fill_login_input(locator, value: str) -> None:
    locator.fill("")
    locator.type(value, delay=80)


def run_single_login_attempt(
    *,
    phone: str,
    password: str,
    headed: bool,
    timeout_ms: int,
    post_login_wait_seconds: int,
    auth_error_cls: type[Exception],
) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise auth_error_cls(
            "缺少 playwright 依赖，请先在项目虚拟环境中安装依赖并安装 Chromium。"
            "macOS/Linux: ./.venv/bin/python -m pip install -r requirements.txt && "
            "./.venv/bin/playwright install chromium；Windows PowerShell: "
            ".\\.venv\\Scripts\\python -m pip install -r requirements.txt；"
            ".\\.venv\\Scripts\\playwright install chromium"
        ) from exc

    captured: dict[str, Optional[str]] = {"token": None}
    executable_path = os.getenv("AUTODL_BROWSER_EXECUTABLE_PATH", "").strip() or None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**build_browser_launch_kwargs(headed=headed, executable_path=executable_path))
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        def handle_response(response) -> None:
            if PASSPORT_URL not in response.url:
                return
            try:
                payload = response.json()
            except Exception as exc:
                logger.warning("解析 passport 响应失败: %s", exc)
                return
            data = payload.get("data") or {}
            token = data.get("token")
            if token:
                captured["token"] = token

        page.on("response", handle_response)

        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                logger.info("登录页未达到 networkidle，继续尝试解析表单")
            form = resolve_login_form(page, timeout_ms, auth_error_cls)
            logger.info(
                "登录页元素定位成功 phone=%s password=%s button=%s",
                form["phone_selector"],
                form["password_selector"],
                form["login_button_selector"],
            )
            _fill_login_input(form["phone_input"], phone)
            _fill_login_input(form["password_input"], password)
            blocker = detect_login_blocker(page)
            if blocker:
                raise auth_error_cls(blocker)
            try:
                with page.expect_response(lambda response: PASSPORT_URL in response.url, timeout=timeout_ms):
                    form["login_button"].click()
            except PlaywrightTimeoutError as exc:
                blocker = detect_login_blocker(page)
                if blocker:
                    raise auth_error_cls(blocker) from exc
                raise auth_error_cls(f"登录提交后未收到 passport 响应，可能页面结构已变化或被风控拦截；{describe_login_page(page)}") from exc
            if captured["token"]:
                return captured["token"]
            page.wait_for_timeout(post_login_wait_seconds * 1000)
            if captured["token"]:
                return captured["token"]
            blocker = detect_login_blocker(page)
            if blocker:
                raise auth_error_cls(blocker)
            raise auth_error_cls(f"未在 passport 响应中捕获到 token；{describe_login_page(page)}")
        except PlaywrightTimeoutError as exc:
            blocker = detect_login_blocker(page)
            if blocker:
                raise auth_error_cls(blocker) from exc
            raise auth_error_cls(f"页面加载或接口等待超时；{describe_login_page(page)}") from exc
        finally:
            context.close()
            browser.close()


def fetch_token_via_playwright(
    *,
    phone: str,
    password: str,
    headed: bool,
    timeout_ms: int,
    max_retries: int,
    post_login_wait_seconds: int,
    auth_error_cls: type[Exception],
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        logger.info("开始通过 Playwright 获取 token，第 %s/%s 次尝试", attempt, max_retries)
        try:
            token = run_single_login_attempt(
                phone=phone,
                password=password,
                headed=headed,
                timeout_ms=timeout_ms,
                post_login_wait_seconds=post_login_wait_seconds,
                auth_error_cls=auth_error_cls,
            )
            logger.info("Playwright 登录成功，已捕获 token")
            return token
        except Exception as exc:
            last_error = exc
            logger.warning("Playwright 登录失败，第 %s 次尝试: %s", attempt, exc)
            time.sleep(min(attempt, 3))
    raise auth_error_cls(f"多次尝试后仍无法获取 token: {last_error}")
