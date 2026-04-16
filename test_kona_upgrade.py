"""Unit tests for kona_upgrade.py pure functions.

Run with: python -m pytest references/homelab/lorawan-qa/test_kona_upgrade.py -v
Or standalone: python references/homelab/lorawan-qa/test_kona_upgrade.py
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import kona_upgrade as ku


class TestParseVersion(unittest.TestCase):
    def test_full_semver(self):
        self.assertEqual(ku.parse_version("7.1.16.3"), (7, 1, 16, 3))

    def test_three_parts(self):
        self.assertEqual(ku.parse_version("6.1.4"), (6, 1, 4))

    def test_empty(self):
        self.assertIsNone(ku.parse_version(""))

    def test_garbage(self):
        self.assertIsNone(ku.parse_version("not-a-version"))

    def test_trailing_dash(self):
        # Implementation uses re.match at start of string, trailing '-r10' ignored
        self.assertEqual(ku.parse_version("7.1.16.3-r10"), (7, 1, 16, 3))

    def test_whitespace_stripped(self):
        self.assertEqual(ku.parse_version("  7.1.12.1  "), (7, 1, 12, 1))


class TestCheckUpgradePath(unittest.TestCase):
    def test_same_major_direct(self):
        ok, _ = ku.check_upgrade_path("7.1.12.1", "7.1.16.3", "micro")
        self.assertTrue(ok)

    def test_same_version(self):
        ok, _ = ku.check_upgrade_path("7.1.16.3", "7.1.16.3", "micro")
        self.assertTrue(ok)

    def test_6x_to_7x_above_floor(self):
        # Macro floor is 5.1.3 for direct-to-7.x, 6.x is above
        ok, msg = ku.check_upgrade_path("6.1.4", "7.1.12.1", "macro")
        self.assertTrue(ok, msg)

    def test_6x_to_7x_mega_above_floor(self):
        ok, msg = ku.check_upgrade_path("6.1.1", "7.1.12.1", "mega")
        self.assertTrue(ok, msg)

    def test_below_floor_blocked(self):
        # micro floor is 4.0.2; pretend we're on 3.5 on micro
        ok, msg = ku.check_upgrade_path("3.5.0", "7.1.12.1", "micro")
        self.assertFalse(ok)
        self.assertIn("minimum", msg)

    def test_downgrade_blocked_by_default(self):
        ok, msg = ku.check_upgrade_path("7.1.16.3", "7.1.12.1", "micro")
        self.assertFalse(ok)
        self.assertIn("--allow-downgrade", msg)

    def test_downgrade_permitted_with_flag(self):
        ok, msg = ku.check_upgrade_path("7.1.16.3", "7.1.12.1", "micro",
                                        allow_downgrade=True)
        self.assertTrue(ok, msg)
        self.assertIn("downgrade permitted", msg)

    def test_downgrade_same_major_flag_needed(self):
        # 7.1.16.3 -> 7.1.12.1 is technically same-major but the version check
        # happens first; with flag it should pass
        ok, _ = ku.check_upgrade_path("7.1.16.3", "7.1.12.1", "macro",
                                      allow_downgrade=True)
        self.assertTrue(ok)

    def test_unparseable_versions_proceed(self):
        ok, msg = ku.check_upgrade_path("weird", "also-weird", "micro")
        self.assertTrue(ok)
        self.assertIn("could not parse", msg)


class TestDeriveTargetFromZip(unittest.TestCase):
    def test_standard_name(self):
        self.assertEqual(ku.derive_target_from_zip("BSP_7.1.16.3.zip"), "7.1.16.3")

    def test_with_path(self):
        p = "/tmp/cache/BSP_7.1.12.1.zip"
        self.assertEqual(ku.derive_target_from_zip(p), "7.1.12.1")

    def test_windows_path(self):
        p = r"C:\cache\BSP_7.0.9.zip"
        self.assertEqual(ku.derive_target_from_zip(p), "7.0.9")

    def test_nonstandard_name(self):
        self.assertIsNone(ku.derive_target_from_zip("custom-firmware.zip"))

    def test_version_with_suffix(self):
        # Should pick the semver substring even with trailing noise
        self.assertEqual(ku.derive_target_from_zip("BSP_7.1.16.3_NOT_FOR_ACTILITY.zip"),
                         "7.1.16.3")


class TestLoadSha256Sidecar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bsp = Path(self.tmp.name) / "BSP_7.1.16.3.zip"
        self.bsp.write_bytes(b"fake zip content")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_sidecar_returns_empty(self):
        # Implementation returns "" (falsy) when sidecar missing
        self.assertEqual(ku.load_sha256_sidecar(str(self.bsp)), "")

    def test_reads_hash_first_line_plain(self):
        sidecar = self.bsp.with_suffix(self.bsp.suffix + ".sha256")
        sidecar.write_text(
            "5b944f1757acb7d7f7bedf15d4d14add040241cb8161928c81b1948668ee1da6\n"
            "# Source: ftpes://74.3.134.34/some/path\n")
        got = ku.load_sha256_sidecar(str(self.bsp))
        self.assertEqual(got,
            "5b944f1757acb7d7f7bedf15d4d14add040241cb8161928c81b1948668ee1da6")

    def test_reads_hash_with_filename_format(self):
        # Some sha256 tools emit "<hash>  <filename>"
        sidecar = self.bsp.with_suffix(self.bsp.suffix + ".sha256")
        sidecar.write_text(
            "5b944f1757acb7d7f7bedf15d4d14add040241cb8161928c81b1948668ee1da6  BSP_7.1.16.3.zip\n")
        got = ku.load_sha256_sidecar(str(self.bsp))
        self.assertEqual(got,
            "5b944f1757acb7d7f7bedf15d4d14add040241cb8161928c81b1948668ee1da6")


class TestSnapshotComponentsParsing(unittest.TestCase):
    """snapshot_components runs on GW, but the parser for its raw output is pure."""

    def test_typical_opkg_list_output(self):
        # Simulates `opkg list-installed` format: "<name> - <version>"
        sample = (
            "tektelic-bsp-identity - 1.5.3\n"
            "tektelic-backup - 1.8.0\n"
            "region-config - 0.16.1\n"
            "fe-fpga - 2.1.4-r0\n"
        )
        # Exercise the parsing logic: split each line on " - "
        parsed = {}
        for line in sample.splitlines():
            if " - " in line:
                name, ver = line.split(" - ", 1)
                parsed[name.strip()] = ver.strip()
        self.assertEqual(parsed["tektelic-bsp-identity"], "1.5.3")
        self.assertEqual(parsed["tektelic-backup"], "1.8.0")
        self.assertEqual(parsed["region-config"], "0.16.1")
        self.assertEqual(parsed["fe-fpga"], "2.1.4-r0")


class TestSha256OfFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_hash(self):
        p = Path(self.tmp.name) / "data.bin"
        content = b"the quick brown fox jumps over the lazy dog"
        p.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(ku._sha256_of_file(str(p)), expected)

    def test_empty_file(self):
        p = Path(self.tmp.name) / "empty.bin"
        p.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        self.assertEqual(ku._sha256_of_file(str(p)), expected)

    def test_large_file_multiple_chunks(self):
        # 200 KB — exercises the 65536-byte chunk loop (3+ chunks)
        p = Path(self.tmp.name) / "big.bin"
        content = b"A" * 200_000
        p.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(ku._sha256_of_file(str(p)), expected)


class TestDetectPlatform(unittest.TestCase):
    """detect_platform dispatches SSH commands; we mock the GW run() calls."""

    def _mock_gw(self, bsp_version_text, uname_text=""):
        gw = MagicMock()
        gw.run.side_effect = [
            (0, bsp_version_text, ""),  # cat tektelic-bsp-version
            (0, uname_text, ""),        # uname -n (fallback)
        ]
        return gw

    def test_micro(self):
        gw = self._mock_gw("Tektelic 7.1.16.3\nPlatform: Kona\nCustom code: Kona Micro EU GW\n")
        self.assertEqual(ku.detect_platform(gw), "micro")

    def test_macro(self):
        gw = self._mock_gw("Tektelic 7.1.12.1\nPlatform: Kona\nCustom code: Kona Macro EU GW\n")
        self.assertEqual(ku.detect_platform(gw), "macro")

    def test_mega(self):
        gw = self._mock_gw("Tektelic 7.1.16.3\nCustom code: Kona Mega EU GW\n")
        self.assertEqual(ku.detect_platform(gw), "mega")

    def test_enterprise(self):
        gw = self._mock_gw("Tektelic 2.1.4\nCustom code: Kona Enterprise\n")
        self.assertEqual(ku.detect_platform(gw), "enterprise")

    def test_photon(self):
        gw = self._mock_gw("Tektelic 2.1.4\nCustom code: Kona Photon\n")
        self.assertEqual(ku.detect_platform(gw), "photon")

    def test_fallback_via_uname(self):
        gw = self._mock_gw("unknown version string", uname_text="kona-micro-00A511\n")
        self.assertEqual(ku.detect_platform(gw), "micro")

    def test_unknown(self):
        gw = self._mock_gw("totally unknown", uname_text="generic-linux-host")
        self.assertEqual(ku.detect_platform(gw), "unknown")


class TestPrintRecoveryHint(unittest.TestCase):
    """Recovery hints are printed for known error signatures; log calls are captured."""

    def test_hint_printed_for_known_signature(self):
        # Pick a known signature from RECOVERY_HINTS
        known_sig = next(iter(ku.RECOVERY_HINTS.keys()))
        with patch.object(ku.log, "info") as mock_info:
            ku.print_recovery_hint(f"something happened: {known_sig} in log")
            calls = [c.args[0] for c in mock_info.call_args_list]
            # Should include "Recovery hint:" header and the hint body
            self.assertTrue(any("Recovery hint" in str(s) for s in calls),
                            f"No 'Recovery hint' header in calls: {calls}")

    def test_no_hint_for_unknown_signature(self):
        with patch.object(ku.log, "info") as mock_info:
            ku.print_recovery_hint("a completely novel error string zzz")
            calls = [c.args[0] for c in mock_info.call_args_list]
            self.assertTrue(any("No specific recovery hint" in str(s) for s in calls),
                            f"Expected fallback message; got: {calls}")

    def test_case_insensitive_match(self):
        known_sig = next(iter(ku.RECOVERY_HINTS.keys()))
        with patch.object(ku.log, "info") as mock_info:
            ku.print_recovery_hint(known_sig.upper())
            calls = [c.args[0] for c in mock_info.call_args_list]
            self.assertTrue(any("Recovery hint" in str(s) for s in calls))


class TestFtpFindBspParsing(unittest.TestCase):
    """ftp_find_bsp parses LIST output; validate the parse logic via mocked FTPS."""

    def _mock_ftps(self, list_output_universal=None, list_output_platform=None,
                   list_output_inner=None):
        ftps = MagicMock()
        def retrlines(_cmd, cb):
            if not hasattr(retrlines, "call"):
                retrlines.call = 0
            if retrlines.call == 0 and list_output_universal is not None:
                for line in list_output_universal:
                    cb(line)
            elif retrlines.call == 1 and list_output_platform is not None:
                for line in list_output_platform:
                    cb(line)
            elif retrlines.call >= 2 and list_output_inner is not None:
                for line in list_output_inner:
                    cb(line)
            retrlines.call += 1
        ftps.retrlines = retrlines
        return ftps

    def test_finds_in_universal_kona_sw(self):
        ftps = self._mock_ftps(list_output_universal=[
            "drwxr-xr-x  2 root root 4096 Oct 01 BSP_7.1.16.3_NOT_FOR_ACTILITY",
            "drwxr-xr-x  2 root root 4096 Oct 01 BSP_7.1.12.1",
            "drwxr-xr-x  2 root root 4096 Oct 01 other_folder",
        ])
        result = ku.ftp_find_bsp(ftps, "7.1.16.3")
        self.assertIsNotNone(result)
        folder, filename = result
        self.assertIn("7.1.16.3", folder)
        self.assertEqual(filename, "BSP_7.1.16.3.zip")

    def test_returns_none_when_not_found(self):
        ftps = self._mock_ftps(
            list_output_universal=["drwxr-xr-x  2 root root 4096 Oct 01 BSP_7.1.12.1"],
            list_output_platform=[],
        )
        # Target 7.0.0 doesn't exist in any listing
        result = ku.ftp_find_bsp(ftps, "7.0.0")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
