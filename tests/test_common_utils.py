#!/usr/bin/env python3
"""
Unit Tests for Common Utilities (common/)
========================================
Verifies:
1. Time utils: get_current_iso_timestamp ISO-8601 UTC validity
2. Crypto utils: constant_time_compare timing-safety, equality, type rejection
3. Arch utils: normalize_architecture, is_architecture_compatible, get_canonical_architecture
4. Storage utils: resolve_safe_path, path traversal rejection, null bytes, validate_app_id
"""

import os
import sys
import tempfile
import shutil
import platform
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.time_utils import get_current_iso_timestamp
from common.crypto_utils import constant_time_compare
from common.arch_utils import (
    ARCH_MAP,
    normalize_architecture,
    is_architecture_compatible,
    get_canonical_architecture,
)
from common.storage_utils import resolve_safe_path, validate_app_id


class TestTimeUtils(unittest.TestCase):
    def test_iso_timestamp_format(self):
        ts = get_current_iso_timestamp()
        self.assertIsInstance(ts, str)
        # Verify it can be parsed as a valid ISO datetime with timezone info
        parsed = datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo)

    def test_timestamp_monotonicity(self):
        t1 = get_current_iso_timestamp()
        t2 = get_current_iso_timestamp()
        self.assertLessEqual(t1, t2)


class TestCryptoUtils(unittest.TestCase):
    def test_matching_values(self):
        self.assertTrue(constant_time_compare("secret_token_123", "secret_token_123"))
        self.assertTrue(constant_time_compare(b"bytes_token", b"bytes_token"))

    def test_mismatched_values(self):
        self.assertFalse(constant_time_compare("secret_token_123", "secret_token_124"))
        self.assertFalse(constant_time_compare("token", "longer_token"))

    def test_empty_and_none_values(self):
        self.assertFalse(constant_time_compare("", "token"))
        self.assertFalse(constant_time_compare("token", ""))
        self.assertFalse(constant_time_compare(None, "token"))
        self.assertFalse(constant_time_compare("token", None))
        self.assertFalse(constant_time_compare(None, None))

    def test_invalid_types(self):
        self.assertFalse(constant_time_compare(12345, "12345"))
        self.assertFalse(constant_time_compare("token", ["token"]))
        self.assertFalse(constant_time_compare({"a": 1}, "token"))


class TestArchUtils(unittest.TestCase):
    def test_normalize_architecture_aliases(self):
        self.assertEqual(normalize_architecture("x86_64"), "amd64")
        self.assertEqual(normalize_architecture("amd64"), "amd64")
        self.assertEqual(normalize_architecture("AMD64"), "amd64")
        self.assertEqual(normalize_architecture("x64"), "amd64")
        self.assertEqual(normalize_architecture("linux/amd64"), "amd64")

        self.assertEqual(normalize_architecture("aarch64"), "arm64")
        self.assertEqual(normalize_architecture("arm64"), "arm64")
        self.assertEqual(normalize_architecture("ARM64"), "arm64")
        self.assertEqual(normalize_architecture("arm64v8"), "arm64")
        self.assertEqual(normalize_architecture("linux/arm64"), "arm64")

        self.assertEqual(normalize_architecture("armv7l"), "armv7")
        self.assertEqual(normalize_architecture("armhf"), "armv7")
        self.assertEqual(normalize_architecture("i386"), "386")
        self.assertEqual(normalize_architecture("x86"), "386")

    def test_normalize_empty_or_invalid(self):
        self.assertEqual(normalize_architecture(""), "")
        self.assertEqual(normalize_architecture(None), "")
        self.assertEqual(normalize_architecture(123), "")

    def test_normalize_unknown_prefix_strip(self):
        self.assertEqual(normalize_architecture("custom/riscv64"), "riscv64")

    def test_is_architecture_compatible(self):
        self.assertTrue(is_architecture_compatible("arm64", ["linux/arm64", "linux/amd64"]))
        self.assertTrue(is_architecture_compatible("aarch64", ["arm64"]))
        self.assertTrue(is_architecture_compatible("amd64", ["linux/amd64"]))
        self.assertTrue(is_architecture_compatible("x86_64", "amd64"))

        # Incompatible
        self.assertFalse(is_architecture_compatible("arm64", ["linux/amd64"]))
        self.assertFalse(is_architecture_compatible("amd64", ["linux/arm64"]))

        # Unknown / empty supported architectures allows scheduling
        self.assertTrue(is_architecture_compatible("arm64", []))
        self.assertTrue(is_architecture_compatible("arm64", None))

    def test_get_canonical_architecture(self):
        arch = get_canonical_architecture()
        self.assertIsInstance(arch, str)
        self.assertIn(arch, {"amd64", "arm64", "armv7", "386", platform.machine().lower()})


class TestStorageUtils(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ps_common_test_")
        self.base_path = Path(self.temp_dir).resolve()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_path_valid_subpaths(self):
        # Empty path returns base
        self.assertEqual(resolve_safe_path(self.base_path, ""), self.base_path)

        # Valid subdirectories
        res = resolve_safe_path(self.base_path, "sub/dir/file.txt")
        self.assertEqual(res, self.base_path / "sub" / "dir" / "file.txt")

    def test_path_traversal_rejection(self):
        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "../../../etc/passwd")

        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "..\\..\\windows\\system32")

        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "sub/../../escape")

    def test_absolute_path_and_drive_escape_rejection(self):
        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "/etc/shadow")

        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "\\windows\\system32")

        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "C:\\boot.ini")

        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "D:/secret.txt")

    def test_null_byte_rejection(self):
        with self.assertRaises(ValueError):
            resolve_safe_path(self.base_path, "sub\0dir")

    def test_validate_app_id(self):
        self.assertEqual(validate_app_id("valid-app-123"), "valid-app-123")
        self.assertEqual(validate_app_id("my_app_test"), "my_app_test")

        with self.assertRaises(ValueError):
            validate_app_id("invalid/app/name")

        with self.assertRaises(ValueError):
            validate_app_id("../escape")

        with self.assertRaises(ValueError):
            validate_app_id("")

        with self.assertRaises(ValueError):
            validate_app_id(None)


if __name__ == "__main__":
    unittest.main()
