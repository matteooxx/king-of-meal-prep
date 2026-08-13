from __future__ import annotations

import socket
import unittest
from unittest import mock

import httpx

from recipes import safe_fetch


def addrinfo(*addresses: str):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443, 0, 0) if ":" in address else (address, 443),
        )
        for address in addresses
    ]


class SafeFetchTests(unittest.TestCase):
    def test_rejects_credentials_and_non_global_addresses(self):
        with self.assertRaisesRegex(safe_fetch.SafeFetchError, "credentials"):
            safe_fetch._validate_url("https://user:pass@example.com/recipe")

        blocked = (
            "127.0.0.1",
            "10.0.0.1",
            "100.64.0.1",
            "169.254.1.1",
            "192.0.2.1",
            "::1",
            "fc00::1",
        )
        for address in blocked:
            with self.subTest(address=address), mock.patch(
                "recipes.safe_fetch.socket.getaddrinfo",
                return_value=addrinfo(address),
            ):
                with self.assertRaisesRegex(
                    safe_fetch.SafeFetchError, "private/internal"
                ):
                    safe_fetch._validate_url("https://example.com/recipe")

    def test_pins_ip_uses_string_sni_and_ignores_proxy_environment(self):
        calls = []
        real_client = httpx.Client

        def handler(request: httpx.Request):
            calls.append(request)
            return httpx.Response(200, text="<html>ok</html>")

        def client_factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            return real_client(
                transport=httpx.MockTransport(handler),
                **kwargs,
            )

        with mock.patch(
            "recipes.safe_fetch.socket.getaddrinfo",
            return_value=addrinfo("93.184.216.34"),
        ), mock.patch(
            "recipes.safe_fetch.httpx.Client", side_effect=client_factory
        ):
            body, final_url = safe_fetch.fetch("https://example.com/recipe")

        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(final_url, "https://example.com/recipe")
        self.assertEqual(calls[0].url.host, "93.184.216.34")
        self.assertEqual(calls[0].headers["host"], "example.com")
        self.assertEqual(calls[0].extensions["sni_hostname"], "example.com")
        self.assertIsInstance(calls[0].extensions["sni_hostname"], str)

    def test_tries_addresses_in_order_and_uses_fresh_clients(self):
        created = 0
        attempted = []
        real_client = httpx.Client

        def handler(request: httpx.Request):
            attempted.append(request.url.host)
            if request.url.host == "2001:4860:4860::8888":
                raise httpx.ConnectError("no IPv6 route", request=request)
            return httpx.Response(200, text="ok")

        def client_factory(**kwargs):
            nonlocal created
            created += 1
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch(
            "recipes.safe_fetch.socket.getaddrinfo",
            return_value=addrinfo("2001:4860:4860::8888", "93.184.216.34"),
        ), mock.patch(
            "recipes.safe_fetch.httpx.Client", side_effect=client_factory
        ):
            body, _ = safe_fetch.fetch("https://example.com/")

        self.assertEqual(body, "ok")
        self.assertEqual(
            attempted, ["2001:4860:4860::8888", "93.184.216.34"]
        )
        self.assertEqual(created, 2)

    def test_redirect_revalidates_dns_and_never_reuses_client(self):
        created = 0
        real_client = httpx.Client

        def handler(request: httpx.Request):
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"Location": "https://other.example/final"}
                )
            return httpx.Response(200, text="done")

        def client_factory(**kwargs):
            nonlocal created
            created += 1
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        dns = [
            addrinfo("93.184.216.34"),
            addrinfo("142.250.74.14"),
        ]
        with mock.patch(
            "recipes.safe_fetch.socket.getaddrinfo", side_effect=dns
        ) as resolver, mock.patch(
            "recipes.safe_fetch.httpx.Client", side_effect=client_factory
        ):
            body, final_url = safe_fetch.fetch(
                "https://example.com/start"
            )

        self.assertEqual(body, "done")
        self.assertEqual(final_url, "https://other.example/final")
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(created, 2)

    def test_redirect_dns_rebind_to_private_address_is_rejected(self):
        real_client = httpx.Client

        def handler(request: httpx.Request):
            return httpx.Response(302, headers={"Location": "/second"})

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch(
            "recipes.safe_fetch.socket.getaddrinfo",
            side_effect=[
                addrinfo("93.184.216.34"),
                addrinfo("127.0.0.1"),
            ],
        ), mock.patch(
            "recipes.safe_fetch.httpx.Client", side_effect=client_factory
        ):
            with self.assertRaisesRegex(
                safe_fetch.SafeFetchError, "private/internal"
            ):
                safe_fetch.fetch("https://example.com/start")


if __name__ == "__main__":
    unittest.main()
