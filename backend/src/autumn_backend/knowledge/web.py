"""公开网页读取：每次跳转重新解析，连接固定到已验证公网 IP，不带 Cookie。"""

import asyncio
import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from autumn_backend.errors import InvalidInputError
from autumn_backend.io_boundary import require_outside_uow


@dataclass(frozen=True, slots=True)
class WebPage:
    url: str
    title: str
    text: str


def validate_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 80, 443)
            or len(url) > 2048
            or any(ord(c) < 32 for c in url)
        ):
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode()
        if hostname.lower() == "localhost" or hostname.endswith(".localhost"):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as error:
        raise InvalidInputError("只接受无凭据的公开 HTTP(S) 网页链接") from error


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.hidden = 0
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)


class _HTTP(http.client.HTTPConnection):
    address: str

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _HTTPS(http.client.HTTPSConnection):
    address: str

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class SafeWebFetcher:
    async def fetch(self, url: str) -> WebPage:
        require_outside_uow()
        return await asyncio.to_thread(self._fetch, validate_url(url))

    @staticmethod
    def _fetch(url: str) -> WebPage:
        deadline = time.monotonic() + 30
        for _ in range(4):
            url = validate_url(url)
            parsed = urlsplit(url)
            assert parsed.hostname is not None
            hostname = parsed.hostname.encode("idna").decode()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = {
                str(entry[4][0])
                for entry in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            }
            if not addresses or any(
                not ipaddress.ip_address(address).is_global for address in addresses
            ):
                raise InvalidInputError("网页解析到非公网地址")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InvalidInputError("网页读取超时")
            connection = (_HTTPS if parsed.scheme == "https" else _HTTP)(
                hostname, port, timeout=min(10, remaining)
            )
            connection.address = sorted(addresses)[0]
            try:
                connection.request(
                    "GET",
                    urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                    headers={
                        "User-Agent": "AutumnPrelude/0.1",
                        "Accept": "text/html,text/plain",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise InvalidInputError("网页跳转缺少地址")
                    url = urljoin(url, location)
                    continue
                content_type = response.getheader("Content-Type", "").lower()
                if (
                    response.status != 200
                    or not content_type.startswith(("text/html", "text/plain"))
                    or response.getheader("Content-Encoding", "identity") != "identity"
                ):
                    raise InvalidInputError("网页需要登录、不可读或类型不支持")
                data = response.read(2_000_001)
                if len(data) > 2_000_000 or time.monotonic() > deadline:
                    raise InvalidInputError("网页过大或读取超时")
                charset = "utf-8"
                for part in content_type.split(";")[1:]:
                    if part.strip().startswith("charset="):
                        charset = part.strip()[8:].strip('"')
                try:
                    text = data.decode(charset, errors="replace")
                except LookupError as error:
                    raise InvalidInputError("网页编码不支持") from error
                if content_type.startswith("text/html"):
                    parser = _HTML()
                    parser.feed(text)
                    text = "\n".join(
                        line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
                    )
                    title = "".join(parser.title).strip()[:300] or hostname
                else:
                    title = hostname
                if not text.strip():
                    raise InvalidInputError("网页没有可提取正文")
                return WebPage(url, title, text)
            finally:
                connection.close()
        raise InvalidInputError("网页跳转过多")
