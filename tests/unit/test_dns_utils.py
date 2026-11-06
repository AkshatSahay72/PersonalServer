#!/usr/bin/env python3
"""
Unit Tests for Resilient DNS Resolver Fallback
==============================================
"""

import socket
import unittest
from unittest.mock import patch, MagicMock

from common.dns_utils import query_dns_a_record, make_resilient_getaddrinfo, enable_dns_fallback


class TestDNSUtils(unittest.TestCase):

    def test_01_query_dns_a_record_against_public_resolver(self):
        """query_dns_a_record parses a genuine A record response from 1.1.1.1."""
        try:
            ip = query_dns_a_record("one.one.one.one", nameserver="1.1.1.1", timeout=3.0)
            self.assertIn(ip, ("1.1.1.1", "1.0.0.1"))
        except (socket.timeout, OSError):
            self.skipTest("Network unreachable or external DNS port 53 blocked in test environment")

    def test_02_resilient_getaddrinfo_transparent_on_success(self):
        """When system getaddrinfo works, it is returned directly without querying fallback."""
        with patch("common.dns_utils._ORIG_GETADDRINFO", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]):
            resolver = make_resilient_getaddrinfo()
            res = resolver("example.com", 80)
            self.assertEqual(res[0][4][0], "93.184.216.34")

    def test_03_resilient_getaddrinfo_engages_fallback_on_gaierror(self):
        """When system getaddrinfo raises gaierror, fallback resolves host and re-calls getaddrinfo with IP."""
        mock_orig = MagicMock(side_effect=[
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"), # 1st call: hostname fails
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.207.73.82", 443))] # 2nd call: IP succeeds
        ])

        with patch("common.dns_utils._ORIG_GETADDRINFO", mock_orig), \
             patch("common.dns_utils.query_dns_a_record", return_value="20.207.73.82"):
            resolver = make_resilient_getaddrinfo()
            res = resolver("codeload.github.com", 443)
            self.assertEqual(res[0][4][0], "20.207.73.82")
            self.assertEqual(mock_orig.call_count, 2)

    def test_04_resilient_getaddrinfo_raises_if_fallback_fails(self):
        """If both system and fallback fail, gaierror is properly raised."""
        mock_orig = MagicMock(side_effect=socket.gaierror(socket.EAI_NONAME, "Name or service not known"))

        with patch("common.dns_utils._ORIG_GETADDRINFO", mock_orig), \
             patch("common.dns_utils.query_dns_a_record", side_effect=ValueError("No DNS answer")):
            resolver = make_resilient_getaddrinfo()
            with self.assertRaises(socket.gaierror):
                resolver("nonexistent.invalid.hostname", 80)


if __name__ == "__main__":
    unittest.main()
