"""Google OAuth login through a real Chrome/Chromium window.

Google blocks programmatic password logins, so this module drives a fresh
browser profile with DrissionPage and captures the OAuth authorization code
from the fixed CLIProxyAPI redirect URI
(``http://localhost:51121/oauth-callback``).

The tool never stores cookies or credentials. Every account gets an isolated
temporary profile that is deleted when the attempt finishes.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from .config import CHROME_ARGS, CHROME_IGNORE_DEFAULT_ARGS, TIMING
from .errors import AuthError, BrowserError, LoginRequiredError

TEMP_PREFIX = "gs2cpa_"

# DrissionPage treats a bare selector containing ``[...]`` as text, so CSS
# locators are always prefixed explicitly. Attribute values are unquoted to
# keep the 4.x parser happy.
EMAIL_FIELDS = [
    "#identifierId",
    "css:input[type=email]",
    "css:input[name=identifier]",
]
PASSWORD_FIELDS = [
    "css:input[type=password]",
    "css:input[name=Passwd]",
]
TOTP_FIELDS = [
    "css:input[name=totpPin]",
    "#totpPin",
    "css:input[autocomplete=one-time-code]",
]

NEXT_BUTTONS = [
    "tag:button@@text():Next",
    "tag:button@@text():Berikutnya",
    "tag:button@@text():Continue",
    "tag:button@@text():Lanjutkan",
    "css:button[type=submit]",
]
EMAIL_NEXT_BUTTONS = ["css:#identifierNext button", *NEXT_BUTTONS]
PASSWORD_NEXT_BUTTONS = ["css:#passwordNext button", *NEXT_BUTTONS]
TOTP_NEXT_BUTTONS = ["css:#totpNext button", *NEXT_BUTTONS]

# Text that means Google needs a human, not another selector click.
_BLOCKING_TEXT = (
    "verify it's you",
    "verify it’s you",
    "confirm your identity",
    "this browser or app may not be secure",
    "couldn't sign you in",
    "couldn’t sign you in",
    "your account has been disabled",
    "account disabled",
    "recovery email",
    "recovery phone",
    "unusual activity",
    "suspicious activity",
    "we detected unusual",
)

_ERROR_KEYWORDS = (
    "couldn't find your google account",
    "couldn’t find your google account",
    "tidak dapat menemukan akun google",
    "enter a valid email",
    "masukkan email yang valid",
    "wrong password",
    "sandi salah",
    "too many failed attempts",
    "terlalu banyak upaya",
    "account disabled",
    "akun dinonaktifkan",
    "cannot sign you in",
    "tidak dapat membuat anda login",
    "invalid verification code",
    "kode verifikasi tidak valid",
)

# Action labels on consent / speedbump / TOS pages.
_ACTION_KEYWORDS = (
    "allow",
    "izinkan",
    "continue",
    "lanjutkan",
    "sign in",
    "masuk",
    "accept",
    "terima",
    "i agree",
    "saya setuju",
    "confirm",
    "konfirmasi",
    "next",
    "berikutnya",
    "i understand",
    "saya memahami",
    "saya mengerti",
    "got it",
    "mengerti",
    "proceed",
    "go to",
    "visit",
    "weiter",
    "verstanden",
    "akzeptieren",
    "continuar",
    "aceptar",
    "continuer",
    "accepter",
    "accetta",
    "принять",
    "続行",
    "계속",
    "继续",
    "同意",
    "موافق",
)

_NEGATIVE_KEYWORDS = (
    "cancel",
    "batal",
    "back",
    "kembali",
    "use another account",
    "gunakan akun lain",
    "switch account",
    "ganti akun",
    "remove",
    "hapus",
    "deny",
    "tolak",
)

UNVERIFIED_APP_KEYWORDS = (
    "advanced",
    "lanjutan",
    "avanzado",
    "paramètres avancés",
    "erweitert",
)


@dataclass
class LoginOptions:
    """Options for one Google login attempt."""

    timing: dict[str, float] = field(default_factory=lambda: TIMING["normal"])
    timeout: float = 300.0
    browser_path: str | None = None
    proxy: str | None = None
    headless: bool = False
    expected_state: str | None = None


@dataclass(frozen=True)
class LoginResult:
    code: str
    url: str


def find_browser(explicit: str | None = None) -> str | None:
    """Return a usable Chrome/Chromium executable, or ``None``."""
    if explicit:
        expanded = os.path.expanduser(explicit)
        if os.path.isfile(expanded):
            return expanded
        return None

    names = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    candidates: list[str] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            os.path.join(program_files, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(program_files_x86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(program_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(program_files, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(program_files, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def extract_code_from_url(url: str, expected_state: str | None = None) -> str | None:
    """Extract ``code`` from an OAuth redirect URL.

    Raises ``AuthError`` when Google returns an explicit ``error`` or when the
    returned state does not match the pending CPA session.
    """
    if not url:
        return None
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    codes = params.get("code")
    if not codes:
        error = (params.get("error") or [None])[0]
        if error:
            description = (params.get("error_description") or [""])[0]
            raise AuthError(
                f"Google returned an OAuth error: {error}"
                + (f" ({description})" if description else "")
            )
        return None

    state = (params.get("state") or [None])[0]
    if expected_state and state and state != expected_state:
        raise AuthError(
            "Google returned a different OAuth state. Start a new login attempt."
        )
    return codes[0]


def _safe_url(page: object) -> str:
    try:
        return str(page.url)  # type: ignore[attr-defined]
    except Exception:
        return ""


def _check_redirect(page: object, expected_state: str | None) -> str | None:
    url = _safe_url(page)
    if not url or "code=" not in url and "error=" not in url:
        return None
    if "oauth-callback" not in url and "localhost" not in url and "127.0.0.1" not in url:
        return None
    return extract_code_from_url(url, expected_state)


def _find_element(page: object, locators: list[str], timeout: float = 3.0) -> object | None:
    for locator in locators:
        try:
            element = page.ele(locator, timeout=timeout)  # type: ignore[attr-defined]
        except Exception:
            continue
        if not element:
            continue
        try:
            if not element.run_js("return !!(this.offsetWidth || this.offsetHeight || this.getClientRects().length);"):  # type: ignore[attr-defined]
                continue
        except Exception:
            pass
        return element
    return None


def _find_and_click(page: object, locators: list[str], timeout: float = 2.0) -> bool:
    for locator in locators:
        try:
            element = page.ele(locator, timeout=timeout)  # type: ignore[attr-defined]
        except Exception:
            continue
        if not element:
            continue
        try:
            if element.run_js("return this.offsetParent !== null;"):  # type: ignore[attr-defined]
                element.scroll_into_view()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            element.click()
            return True
        except Exception:
            try:
                element.click(by_js=True)
                return True
            except Exception:
                continue
    return False


def _js_click_button(page: object, labels: list[str]) -> bool:
    """Click a visible button by its visible label using JavaScript."""
    try:
        clicked = page.run_js(  # type: ignore[attr-defined]
            """
            const wanted = JSON.parse(arguments[0]).map(value => value.toLowerCase());
            const candidates = Array.from(document.querySelectorAll(
                'button, div[role="button"], input[type="submit"], a[role="button"]'
            )).filter(el => el.offsetParent !== null && !el.disabled);
            for (const label of wanted) {
                const el = candidates.find(node => {
                    const text = (node.innerText || node.value || node.textContent || '')
                        .trim().toLowerCase();
                    return text === label;
                });
                if (el) {
                    el.scrollIntoView({block: 'center'});
                    el.focus();
                    el.click();
                    return label;
                }
            }
            return null;
            """,
            json.dumps([value.lower() for value in labels]),
        )
        return bool(clicked)
    except Exception:
        return False


def _press_enter(page: object, element: object | None = None) -> bool:
    """Submit the focused field with a real Enter keystroke."""
    if element is not None:
        with contextlib.suppress(Exception):
            element.click()  # type: ignore[attr-defined]
    try:
        from DrissionPage._units.actions import Keys

        page.actions.type(Keys.ENTER)  # type: ignore[attr-defined]
        return True
    except Exception:
        pass
    try:
        page.run_js(  # type: ignore[attr-defined]
            """
            const el = document.activeElement;
            if (!el) return false;
            for (const type of ['keydown', 'keypress', 'keyup']) {
                el.dispatchEvent(new KeyboardEvent(type, {
                    key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                    bubbles: true, cancelable: true
                }));
            }
            return true;
            """
        )
        return True
    except Exception:
        return False


def _element_value(element: object) -> str:
    try:
        value = element.attr("value")  # type: ignore[attr-defined]
        if value:
            return str(value)
    except Exception:
        pass
    try:
        return str(element.run_js("return this.value;") or "")  # type: ignore[attr-defined]
    except Exception:
        return ""


def _force_input(page: object, element: object, text: str, description: str) -> None:
    """Fill a Google field using several fallbacks.

    Google swaps between React-controlled inputs and plain inputs, and some
    pages silently ignore synthetic events.
    """
    strategies = []

    def standard() -> None:
        element.input(text, clear=True)  # type: ignore[attr-defined]
        time.sleep(0.25)
        if text in _element_value(element):
            return
        raise RuntimeError("value not set")

    def by_js() -> None:
        element.input(text, clear=True, by_js=True)  # type: ignore[attr-defined]
        time.sleep(0.25)
        if text in _element_value(element):
            return
        raise RuntimeError("value not set")

    def keyboard() -> None:
        from DrissionPage._units.actions import Keys

        element.click()  # type: ignore[attr-defined]
        time.sleep(0.15)
        page.actions.key_down(Keys.CTRL).type("a").key_up(Keys.CTRL)  # type: ignore[attr-defined]
        time.sleep(0.1)
        page.actions.type(Keys.BACKSPACE)  # type: ignore[attr-defined]
        time.sleep(0.1)
        page.actions.type(text)  # type: ignore[attr-defined]
        time.sleep(0.25)
        if text in _element_value(element):
            return
        raise RuntimeError("value not set")

    def javascript() -> None:
        element.run_js(  # type: ignore[attr-defined]
            """
            this.focus();
            this.value = arguments[0];
            this.dispatchEvent(new Event('input', {bubbles: true}));
            this.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            text,
        )
        time.sleep(0.2)
        if text in _element_value(element):
            return
        raise RuntimeError("value not set")

    strategies.extend([standard, by_js, keyboard, javascript])
    last_error: Exception | None = None
    for strategy in strategies:
        try:
            strategy()
            return
        except Exception as exc:  # noqa: BLE001 - try the next strategy
            last_error = exc
    raise BrowserError(f"Could not fill the {description}: {last_error}")


def _google_error_text(page: object) -> str | None:
    try:
        result = page.run_js(  # type: ignore[attr-defined]
            """
            const keywords = JSON.parse(arguments[0]);
            const selectors = 'div[aria-live="assertive"], div[jsname="B1fAeb"], .Ekjuhf, .dEOOab, .o6cuMc, [role="alert"]';
            for (const el of document.querySelectorAll(selectors)) {
                const text = (el.innerText || el.textContent || '').trim();
                const lower = text.toLowerCase();
                for (const keyword of keywords) {
                    if (lower.includes(keyword)) return text;
                }
            }
            return null;
            """,
            json.dumps(list(_ERROR_KEYWORDS)),
        )
        return str(result).strip() if result else None
    except Exception:
        return None


def _submit(page: object, locators: list[str]) -> bool:
    if _find_and_click(page, locators, timeout=1.5):
        return True
    if _js_click_button(
        page, ["next", "berikutnya", "lanjutkan", "continue", "sign in", "masuk"]
    ):
        return True
    return _press_enter(page)


def _submit_field(page: object, element: object, locators: list[str]) -> bool:
    """Submit a field by clicking its button, falling back to Enter."""
    if _find_and_click(page, locators, timeout=1.5):
        return True
    return _press_enter(page, element)


def _handle_totp(page: object, secret: str | None) -> bool:
    url = _safe_url(page).lower()
    named_totp = "totp" in url
    element = _find_element(
        page,
        TOTP_FIELDS,
        timeout=0.8,
    )
    if element is None and named_totp:
        element = _find_element(page, ["css:input[type=tel]"], timeout=0.5)
    if element is None:
        return False
    try:
        if not element.run_js("return this.offsetParent !== null;"):  # type: ignore[attr-defined]
            return False
    except Exception:
        pass

    if not secret:
        raise LoginRequiredError(
            "Google asked for a two-factor code. Add the TOTP secret as the "
            "third field in the account line (email|password|TOTP_SECRET) "
            "or disable 2FA for these accounts."
        )
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise LoginRequiredError(
            "Two-factor authentication is required, but pyotp is not installed. "
            "Run: pip install pyotp"
        ) from exc

    try:
        code = pyotp.TOTP(secret.replace(" ", "").strip()).now()
    except Exception as exc:
        raise LoginRequiredError(f"Invalid TOTP secret: {exc}") from exc

    _force_input(page, element, code, "two-factor code")
    _submit_field(page, element, TOTP_NEXT_BUTTONS)
    time.sleep(2.0)
    error = _google_error_text(page)
    if error:
        raise AuthError(f"Google rejected the two-factor code: {error}")
    return True


def _handle_account_chooser(page: object, email: str, password: str) -> bool:
    url = _safe_url(page).lower()
    if not any(
        needle in url
        for needle in ("accountchooser", "chooseaccount", "selectaccount", "oauthchooseaccount")
    ):
        return False

    clicked = page.run_js(  # type: ignore[attr-defined]
        """
        const target = (arguments[0] || '').toLowerCase().trim();
        const username = target.split('@')[0];
        const items = Array.from(document.querySelectorAll('li, div[role="link"], div[role="button"], a'))
            .filter(el => el.offsetParent !== null);
        const found = items.find(el => {
            const text = (el.innerText || el.textContent || '').toLowerCase();
            if (text.includes('use another account') || text.includes('gunakan akun lain')) return false;
            return text.includes(target) || (username.length > 4 && text.includes(username));
        });
        if (!found) return null;
        found.scrollIntoView();
        found.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
        found.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        found.click();
        return (found.innerText || found.textContent || '').trim();
        """,
        email,
    )
    if not clicked:
        return _click_use_another_account(page)

    time.sleep(1.5)
    password_field = _find_element(page, PASSWORD_FIELDS, timeout=1.5)
    if password_field:
        _force_input(page, password_field, password, "password")
        _submit_field(
            page,
            password_field,
            PASSWORD_NEXT_BUTTONS,
        )
        time.sleep(2.0)
    return True


def _click_use_another_account(page: object) -> bool:
    return bool(
        page.run_js(  # type: ignore[attr-defined]
            """
            const labels = ['use another account', 'gunakan akun lain', 'otros usuarios', 'autre compte'];
            const candidates = Array.from(document.querySelectorAll('div[role="link"], div[role="button"], li, a'))
                .filter(el => el.offsetParent !== null);
            for (const label of labels) {
                const el = candidates.find(node => (node.innerText || node.textContent || '').toLowerCase().includes(label));
                if (el) { el.click(); return label; }
            }
            return null;
            """
        )
    )


def _handle_tos(page: object) -> bool:
    clicked = page.run_js(  # type: ignore[attr-defined]
        """
        window.scrollTo(0, document.body.scrollHeight);
        const selectors = ['#gaplustosNext button', '#gaplustosNext input', '#gaplustosNext'];
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (el && el.offsetParent !== null) { el.click(); return selector; }
        }
        const keywords = ['i understand', 'saya memahami', 'saya mengerti', 'accept', 'saya setuju', 'agree', 'setuju'];
        const candidates = Array.from(document.querySelectorAll('button, input[type="submit"], div[role="button"]'))
            .filter(el => el.offsetParent !== null);
        for (const keyword of keywords) {
            const el = candidates.find(node => (node.innerText || node.value || node.textContent || '').toLowerCase().includes(keyword));
            if (el) { el.click(); return keyword; }
        }
        return null;
        """
    )
    return bool(clicked)


def _handle_unverified_app(page: object) -> bool:
    clicked = page.run_js(  # type: ignore[attr-defined]
        """
        const keywords = JSON.parse(arguments[0]);
        const candidates = Array.from(document.querySelectorAll('button, div[role="button"], a'))
            .filter(el => el.offsetParent !== null);
        for (const keyword of keywords) {
            const el = candidates.find(node => (node.innerText || node.textContent || '').toLowerCase().trim() === keyword);
            if (el) { el.click(); return keyword; }
        }
        return null;
        """,
        json.dumps(list(UNVERIFIED_APP_KEYWORDS)),
    )
    return bool(clicked)


def _click_consent(page: object) -> str | None:
    return page.run_js(  # type: ignore[attr-defined]
        """
        const actions = JSON.parse(arguments[0]);
        const negatives = JSON.parse(arguments[1]);

        document.querySelectorAll('input[type="checkbox"]:not(:checked)')
            .forEach(cb => cb.click());

        const priority = [
            '#submit_approve_access button', '#confirm button', '#next button',
            'input[type="submit"]', '#submit_approve_access', '#confirm', '#next'
        ];
        for (const selector of priority) {
            const el = document.querySelector(selector);
            if (el && el.offsetParent !== null) { el.click(); return 'selector:' + selector; }
        }

        const candidates = Array.from(document.querySelectorAll(
            'button, a, input[type="submit"], div[role="button"], span[role="button"]'
        )).filter(el => el.offsetParent !== null && !el.disabled);

        for (const action of actions) {
            const found = candidates.find(el => {
                const text = (el.innerText || el.value || el.textContent || '').toLowerCase().trim();
                if (!text) return false;
                if (negatives.some(word => text.includes(word))) return false;
                return text === action || text.includes(action);
            });
            if (found) { found.click(); return 'action:' + action; }
        }
        return null;
        """,
        json.dumps(list(_ACTION_KEYWORDS)),
        json.dumps(list(_NEGATIVE_KEYWORDS)),
    )


def _handle_recovery_prompt(page: object) -> bool:
    """Skip optional recovery-email/phone prompts when Google offers a skip."""
    try:
        body = str(page.run_js("return document.body ? document.body.innerText : '';") or "").lower()  # type: ignore[attr-defined]
    except Exception:
        return False
    markers = (
        "recovery email",
        "recovery phone",
        "add recovery",
        "confirm your recovery",
        "atur pemulihan",
    )
    if not any(marker in body for marker in markers):
        return False
    clicked = page.run_js(  # type: ignore[attr-defined]
        """
        const labels = ['not now', 'nanti', 'skip', 'lewati', 'remind me later', 'later', 'no thanks', 'tidak sekarang'];
        const candidates = Array.from(document.querySelectorAll('button, div[role="button"], a'))
            .filter(el => el.offsetParent !== null);
        for (const label of labels) {
            const el = candidates.find(node => (node.innerText || node.textContent || '').toLowerCase().trim() === label);
            if (el) { el.click(); return label; }
        }
        return null;
        """
    )
    if clicked:
        time.sleep(1.5)
        return True
    return False


_CAPTCHA_MARKERS = (
    "type the text you hear or see",
    "enter the characters you see",
    "please enter the characters you see",
    "verify you're not a robot",
    "verify you’re not a robot",
    "recaptcha",
)


def _detect_captcha(page: object) -> None:
    """Fail fast when Google requires an image/audio CAPTCHA.

    CAPTCHA risk is driven by IP and browser reputation; refusing to loop is
    both faster and safer than submitting the password repeatedly.
    """
    try:
        body = str(page.run_js("return document.body ? document.body.innerText : '';") or "").lower()  # type: ignore[attr-defined]
    except Exception:
        return
    if any(marker in body for marker in _CAPTCHA_MARKERS):
        raise LoginRequiredError(
            "Google is showing a CAPTCHA for this login. That is an IP/browser "
            "trust check, not a wrong password. Run the tool from a residential "
            "connection or pass a residential/mobile proxy with --browser-proxy "
            "(and --oauth-proxy for CLIProxyAPI's token exchange)."
        )


def _detect_blocking_page(page: object) -> None:
    url = _safe_url(page).lower()
    if any(
        marker in url
        for marker in (
            "challenge/ipp",
            "challenge/recaptcha",
            "signin/rejected",
            "deniedsigninrejected",
            "accountdisabled",
        )
    ):
        raise LoginRequiredError(
            "Google requires manual verification for this account "
            "(device confirmation, reCAPTCHA, or a disabled account). "
            "Sign in once in a normal browser, or use an account without 2FA."
        )

    try:
        body = str(page.run_js("return document.body ? document.body.innerText : '';") or "").lower()  # type: ignore[attr-defined]
    except Exception:
        return
    if any(
        phrase in body
        for phrase in (
            "access blocked",
            "this app is blocked",
            "app is blocked",
            "org_internal",
            "has not completed the google verification process",
        )
    ):
        raise LoginRequiredError(
            "Google blocked this OAuth client for the account. A Workspace "
            "administrator may need to allow the Antigravity app, or the "
            "account is restricted by policy."
        )
    for phrase in _BLOCKING_TEXT:
        if phrase in body:
            raise LoginRequiredError(
                "Google displayed a manual verification page. "
                "This usually means 2FA/device approval or an account risk check; "
                "complete it once manually or use another account."
            )


def _consent_loop(
    page: object, options: LoginOptions, email: str, password: str, totp_secret: str | None
) -> str:
    timing = options.timing
    deadline = time.monotonic() + options.timeout
    tos_clicked = False

    for _ in range(40):
        if time.monotonic() > deadline:
            raise AuthError("Google login timed out before the OAuth redirect")

        code = _check_redirect(page, options.expected_state)
        if code:
            return code

        _detect_captcha(page)

        url = _safe_url(page)
        if not url:
            time.sleep(1)
            continue

        if "accounts.google.com" not in url and "google.com" not in url:
            # Google handed control back to the local redirect (or a dead
            # localhost page). Give the URL a moment to settle.
            for _ in range(4):
                time.sleep(0.5)
                code = _check_redirect(page, options.expected_state)
                if code:
                    return code
            raise AuthError(
                "Left Google but no OAuth code was found in the redirect URL. "
                f"Final URL: {url[:160]}"
            )

        if _handle_totp(page, totp_secret):
            continue

        if "workspacetermsofservice" in url or "speedbump" in url:
            if not tos_clicked and _handle_tos(page):
                tos_clicked = True
                time.sleep(2.0)
                code = _check_redirect(page, options.expected_state)
                if code:
                    return code
                continue
            time.sleep(timing["no_button_wait"])
            continue

        if "unknownerror" in url:
            page.run_js(  # type: ignore[attr-defined]
                """
                const el = Array.from(document.querySelectorAll('button, a, input[type="submit"], div[role="button"]'))
                    .find(node => node.offsetParent !== null);
                if (el) el.click();
                """
            )
            time.sleep(2)
            continue

        if _handle_account_chooser(page, email, password):
            code = _check_redirect(page, options.expected_state)
            if code:
                return code
            continue

        password_field = _find_element(page, PASSWORD_FIELDS, timeout=0.4)
        if password_field:
            _force_input(page, password_field, password, "password")
            _submit_field(
                page,
                password_field,
                PASSWORD_NEXT_BUTTONS,
            )
            time.sleep(timing["after_password_next"])
            continue

        email_field = _find_element(page, EMAIL_FIELDS, timeout=0.4)
        if email_field:
            _force_input(page, email_field, email, "email address")
            _submit(
                page,
                EMAIL_NEXT_BUTTONS,
            )
            time.sleep(timing["after_email_next"])
            continue

        if _handle_recovery_prompt(page):
            continue

        _detect_blocking_page(page)

        clicked = _handle_unverified_app(page)
        if not clicked:
            clicked = _click_consent(page)
        if clicked:
            time.sleep(timing["after_consent_click"])
            code = _check_redirect(page, options.expected_state)
            if code:
                return code
            continue

        time.sleep(timing["no_button_wait"])

    raise AuthError(
        "Google kept showing confirmation pages for too long. "
        "Check the account in a normal browser, then retry."
    )


def login(
    auth_url: str,
    email: str,
    password: str,
    *,
    totp_secret: str | None = None,
    options: LoginOptions | None = None,
) -> LoginResult:
    """Run the Google login and return the captured OAuth code."""
    options = options or LoginOptions()
    timing = options.timing

    browser = find_browser(options.browser_path)
    if not browser:
        raise BrowserError(
            "Chrome/Chromium was not found. Install Google Chrome or pass "
            "--browser-path /path/to/chrome."
        )

    try:
        from DrissionPage import ChromiumOptions, ChromiumPage
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise BrowserError(
            "DrissionPage is not installed. Run: pip install DrissionPage"
        ) from exc

    profile_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    page = None
    try:
        chrome = ChromiumOptions()
        chrome.set_browser_path(browser)
        for arg in CHROME_ARGS:
            chrome.set_argument(arg)
        for arg in CHROME_IGNORE_DEFAULT_ARGS:
            with contextlib.suppress(Exception):
                chrome.remove_argument(arg)
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            chrome.set_argument("--no-sandbox")
        if sys.platform.startswith("linux"):
            chrome.set_argument("--disable-dev-shm-usage")
        if options.headless:
            chrome.set_argument("--headless=new")
            chrome.set_argument("--disable-gpu")
            chrome.set_argument("--window-size=1280,900")
        chrome.set_user_data_path(profile_dir)
        chrome.set_local_port(random.randint(19200, 29200))
        if options.proxy:
            chrome.set_proxy(options.proxy)

        page = ChromiumPage(chrome)
        page.get(auth_url)
        time.sleep(timing["page_load"])

        code = _check_redirect(page, options.expected_state)
        if code:
            return LoginResult(code=code, url=_safe_url(page))

        # Some sessions land directly on the password or account chooser page.
        email_field = _find_element(page, EMAIL_FIELDS, timeout=8.0)
        if email_field:
            _force_input(page, email_field, email, "email address")
            time.sleep(timing["after_email_input"])
            if not _submit(
                page,
                EMAIL_NEXT_BUTTONS,
            ):
                raise BrowserError("Google's email Next button was not found")
            time.sleep(timing["after_email_next"])

        # Password, TOTP, or an already-signed-in account chooser.
        password_field = None
        password_deadline = time.monotonic() + timing["password_timeout"]
        while time.monotonic() < password_deadline:
            code = _check_redirect(page, options.expected_state)
            if code:
                return LoginResult(code=code, url=_safe_url(page))

            if _handle_totp(page, totp_secret):
                continue
            if _handle_account_chooser(page, email, password):
                code = _check_redirect(page, options.expected_state)
                if code:
                    return LoginResult(code=code, url=_safe_url(page))
                continue

            password_field = _find_element(page, PASSWORD_FIELDS, timeout=0.5)
            if password_field:
                break

            error = _google_error_text(page)
            if error:
                raise AuthError(f"Google error: {error}")
            _detect_blocking_page(page)
            time.sleep(0.3)

        if password_field:
            _force_input(page, password_field, password, "password")
            time.sleep(timing["after_password"])
            if not _submit(
                page,
                PASSWORD_NEXT_BUTTONS,
            ):
                raise BrowserError("Google's password Next button was not found")
            time.sleep(timing["after_password_next"])
        else:
            # No password field: the consent/redirect path may still complete.
            code = _check_redirect(page, options.expected_state)
            if code:
                return LoginResult(code=code, url=_safe_url(page))

        code = _consent_loop(page, options, email, password, totp_secret)
        return LoginResult(code=code, url=_safe_url(page))
    except (AuthError, LoginRequiredError, BrowserError):
        raise
    except Exception as exc:  # noqa: BLE001 - translate DrissionPage internals
        raise BrowserError(_clean_exception(exc)) from exc
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                page.quit()
        shutil.rmtree(profile_dir, ignore_errors=True)


def _clean_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    if "页面被刷新" in message or "loading" in message.lower():
        return "The page was navigating while the browser tried to act; retry the account"
    if "没有找到" in message:
        return "A Google page element was not found"
    if "连接" in message and "断开" in message:
        return "The browser connection was lost"
    return message


def kill_zombie_browsers() -> None:
    """Clean profiles and Chrome processes left by an interrupted run."""
    for old_dir in glob.glob(os.path.join(tempfile.gettempdir(), f"{TEMP_PREFIX}*")):
        shutil.rmtree(old_dir, ignore_errors=True)
    if os.name == "nt":
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["pkill", "-f", f"chrome.*{TEMP_PREFIX}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
