"""Where a tenant tool may send a request.

A tool definition is written by a tenant administrator, and its arguments by a model that reads
customer messages; neither may make the platform call something it should not (SSRF). Every
request a tenant tool sends must:

- use https on the default port, with no credentials in the URL and no placeholder in the host:
  the model fills path segments and query values, never where the request goes;
- go to a host on the tenant's egress allowlist (``tenants.tool_hosts``), which the platform
  operator sets in the tenant pack, not the tenant administrator through the API: a stolen admin
  session cannot point a tool at a host of its choosing;
- resolve only to public addresses. Every address the name resolves to is checked (one private
  address among public ones is enough to refuse), and the request is then sent to the address that
  was checked, with the name kept in the Host header and in TLS (SNI and certificate check), so a
  second lookup at connect time (DNS rebinding) cannot send it to 10.0.0.5 or 169.254.169.254.

Redirects are never followed (httpx client with follow_redirects=False), so a 302 cannot move the
request somewhere unchecked either.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class EgressError(Exception):
    """The request may not be sent. The message says why, without the request's data."""


def system_resolve(host: str) -> list[Address]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressError(f"cannot resolve {host}: {exc.strerror or exc}") from None
    return list(dict.fromkeys(ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos))


def public(address: Address) -> bool:
    """A global unicast address: not loopback, private, link-local (169.254.169.254), shared
    (100.64/10), multicast, reserved or unspecified, also when written as IPv4-mapped IPv6."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_unspecified and not address.is_loopback and not address.is_link_local)


def normal_host(host: str) -> str:
    """Lower case, no trailing dot, internationalised names as their ASCII (punycode) form: the form
    both the allowlist and a request's host are compared in."""
    host = str(host).strip().rstrip(".").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    return host


def host_allowed(host: str, hosts: tuple[str, ...] | list[str]) -> bool:
    """Exact names, or "*.harbourline.example" for its subdomains (not the bare domain)."""
    host = normal_host(host)
    for allowed in hosts:
        wildcard = str(allowed).strip().startswith("*.")
        allowed = ("*." if wildcard else "") + normal_host(str(allowed).strip()[2:] if wildcard else allowed)
        if allowed.startswith("*."):
            if host.endswith(allowed[1:]) and host != allowed[2:]:
                return True
        elif host == allowed:
            return True
    return False


class EgressPolicy:
    """One tenant's egress rules: its allowlist, and how names are resolved (tests pass their own)."""

    def __init__(self, hosts=(), resolve: Callable[[str], list[Address]] | None = None):
        self.hosts = tuple(hosts or ())
        self.resolve_fn = resolve or system_resolve

    def check_template(self, template: str) -> str:
        """The host a URL template sends to, or EgressError. Placeholders ({account_id}) may only
        stand in the path or the query."""
        if not template.lower().startswith("https://"):
            raise EgressError("only https is allowed")
        authority = _authority(template)
        if "{" in authority or "}" in authority:
            raise EgressError("the host cannot contain a placeholder")
        if "@" in authority:
            raise EgressError("no credentials in the URL (use a secret reference)")
        parts = urlsplit(template.replace("{", "").replace("}", ""))
        try:
            port = parts.port
        except ValueError:
            raise EgressError("invalid port") from None
        if port not in (None, 443):
            raise EgressError("only the default https port is allowed")
        host = normal_host(parts.hostname or "")
        if not host or not host_allowed(host, self.hosts):
            raise EgressError(f"host {host or '?'} is not on the tenant's egress allowlist")
        return host

    def resolve(self, host: str) -> Address:
        """The address to connect to: every address the name has must be public."""
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            addresses = self.resolve_fn(host)
        else:
            addresses = [literal]
        if not addresses:
            raise EgressError(f"{host} has no address")
        for address in addresses:
            if not public(address):
                raise EgressError(f"{host} resolves to a private or reserved address")
        return addresses[0]

    def pin(self, request: httpx.Request) -> httpx.Request:
        """The same request, sent to the checked address of its host."""
        host = normal_host(request.url.raw_host.decode("ascii"))
        if not host_allowed(host, self.hosts):
            raise EgressError(f"host {host} is not on the tenant's egress allowlist")
        address = self.resolve(host)
        literal = f"[{address}]" if address.version == 6 else str(address)
        headers = httpx.Headers(request.headers)
        headers["Host"] = host
        return httpx.Request(request.method, request.url.copy_with(host=literal.strip("[]")), headers=headers,
                             content=request.content,
                             extensions={**request.extensions, "sni_hostname": host})


def _authority(template: str) -> str:
    rest = template.split("://", 1)[1] if "://" in template else template
    return rest.split("/", 1)[0].split("?", 1)[0]
