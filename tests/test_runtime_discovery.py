import builtins
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import signin


class RegistryHandle:
    def __init__(self, data, root=False):
        self.data = data
        self.root = root
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class FakeRegistry:
    HKEY_CURRENT_USER = "user"
    HKEY_LOCAL_MACHINE = "machine"
    KEY_READ = 1
    KEY_WOW64_64KEY = 256
    KEY_WOW64_32KEY = 512
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_DWORD = 4

    def __init__(self, views=None):
        self.views = views or {}
        self.handles = []
        self.root_reads = []

    def OpenKey(self, parent, name, reserved=0, access=KEY_READ):
        if isinstance(parent, RegistryHandle):
            data = parent.data[int(name)]
            if isinstance(data, OSError):
                raise data
            handle = RegistryHandle(data)
        else:
            view = access & (self.KEY_WOW64_64KEY | self.KEY_WOW64_32KEY)
            self.root_reads.append((parent, view))
            data = self.views.get((parent, view), [])
            if isinstance(data, OSError):
                raise data
            handle = RegistryHandle(data, root=True)
        self.handles.append(handle)
        return handle

    def QueryInfoKey(self, handle):
        if handle.data == "query-denied":
            raise PermissionError()
        return (len(handle.data), 0, 0)

    def EnumKey(self, handle, index):
        if handle.data[index] == "enum-deleted":
            raise FileNotFoundError()
        return str(index)

    def QueryValueEx(self, handle, field):
        value = handle.data.get(field)
        if value is None:
            raise FileNotFoundError()
        if isinstance(value, OSError):
            raise value
        return value

    def ExpandEnvironmentStrings(self, value):
        return value.replace("%TEST_APPS%", r"D:\Apps, 自定义")


def entry(name="WorkBuddy 5.6.2", location=None, icon=None):
    result = {"DisplayName": (name, FakeRegistry.REG_SZ)}
    if location is not None:
        result["InstallLocation"] = (location, FakeRegistry.REG_SZ)
    if icon is not None:
        result["DisplayIcon"] = (icon, FakeRegistry.REG_SZ)
    return result


class RegistryDiscoveryTests(unittest.TestCase):
    def discover(self, registry, files=(), full=False):
        existing = {ntpath.normcase(path) for path in files}
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"winreg": registry}), \
                patch.dict(os.environ, {"WORKBUDDY_EXE": ""}), \
                patch("os.path.isfile", side_effect=lambda path: ntpath.normcase(path) in existing), \
                patch.object(signin.subprocess, "Popen", side_effect=AssertionError("discovery must not execute anything")):
            if full:
                return signin.find_workbuddy_runtime()
            return list(signin._windows_registry_runtimes())

    def test_custom_install_location_and_handle_cleanup(self):
        path = r"D:\Apps, 自定义\WorkBuddy.exe"
        registry = FakeRegistry({("user", 256): [entry(location='"D:\\Apps, 自定义"')]})
        self.assertEqual(self.discover(registry, [path], full=True), path)
        self.assertTrue(registry.handles)
        self.assertTrue(all(handle.closed for handle in registry.handles))

    def test_display_icon_with_spaces_commas_quotes_and_resource_indices(self):
        path = r"D:\Apps, Tools\WorkBuddy.exe"
        for text in (path, path + ",0", path + ", -12", '"' + path + '"', '"' + path + '", -12'):
            with self.subTest(text=text):
                registry = FakeRegistry({("user", 256): [entry(icon=text)]})
                self.assertEqual(self.discover(registry, [path]), [path])

    def test_stale_directory_falls_back_to_icon(self):
        path = r"D:\Actual\WorkBuddy.exe"
        registry = FakeRegistry({("user", 256): [entry(location=r"D:\Removed", icon=path + ",0")]})
        self.assertEqual(self.discover(registry, [path]), [path])

    def test_invalid_file_does_not_mask_later_entries(self):
        path = r"D:\Valid\WorkBuddy.exe"
        registry = FakeRegistry({("user", 256): [entry(location=r"D:\Stale"), entry(icon=path)]})
        self.assertEqual(self.discover(registry, [path]), [path])

    def test_accepts_product_name_and_version_but_not_unrelated_names(self):
        path = r"D:\Apps\WorkBuddy.exe"
        for name in ("WorkBuddy", "workbuddy", "WorkBuddy 5.6.2", "WorkBuddy 5.7.0-beta.1"):
            with self.subTest(name=name):
                self.assertEqual(self.discover(FakeRegistry({("user", 256): [entry(name, icon=path)]}), [path]), [path])
        for name in ("MyWorkBuddy", "WorkBuddy Helper", "WorkBuddy Uninstaller", "WorkBuddyBackup", "WorkBuddy 5.6.2 Helper"):
            with self.subTest(name=name):
                self.assertEqual(self.discover(FakeRegistry({("user", 256): [entry(name, icon=path)]}), [path]), [])

    def test_rejects_wrong_executables_commands_and_relative_paths(self):
        paths = [r"D:\Apps\Uninstall WorkBuddy.exe", r"D:\Apps\other.exe", r"D:\Apps\WorkBuddy.ico",
                 r"D:\Apps\WorkBuddy.exe --uninstall", '"D:\\Apps\\WorkBuddy.exe" --uninstall',
                 "WorkBuddy.exe", r"D:WorkBuddy.exe", r"\Apps\WorkBuddy.exe",
                 '"D:\\Apps\\WorkBuddy.exe', "D:\\Apps\n\\WorkBuddy.exe", 42]
        for path in paths:
            with self.subTest(path=path):
                self.assertIsNone(signin._registry_runtime_path(path, icon=True))

    def test_rejects_directory_named_like_executable(self):
        registry = FakeRegistry({("user", 256): [entry(icon=r"D:\Apps\WorkBuddy.exe")]})
        # No regular file at the candidate path, even if a directory exists there.
        with patch("os.path.exists", return_value=True):
            self.assertEqual(self.discover(registry), [])

    def test_expandable_values_are_expanded_only_for_expand_sz(self):
        path = r"D:\Apps, 自定义\WorkBuddy.exe"
        expanded = entry()
        expanded["InstallLocation"] = ("%TEST_APPS%", FakeRegistry.REG_EXPAND_SZ)
        raw = entry(location="%TEST_APPS%")
        registry = FakeRegistry({("user", 256): [raw, expanded]})
        self.assertEqual(self.discover(registry, [path]), [path])

    def test_registry_value_types_and_read_errors_are_ignored(self):
        path = r"D:\Apps\WorkBuddy.exe"
        invalid_name = {"DisplayName": (12, FakeRegistry.REG_DWORD)}
        invalid_location = entry(icon=path)
        invalid_location["InstallLocation"] = (123, FakeRegistry.REG_DWORD)
        unreadable = entry(icon=path)
        unreadable["InstallLocation"] = PermissionError()
        registry = FakeRegistry({("user", 256): [invalid_name, invalid_location, unreadable]})
        self.assertEqual(self.discover(registry, [path]), [path])

    def test_user_machine_and_both_registry_views(self):
        views = [("user", 256), ("user", 512), ("machine", 256), ("machine", 512)]
        files = [r"D:\Client%d\WorkBuddy.exe" % i for i in range(4)]
        registry = FakeRegistry({view: [entry(icon=path)] for view, path in zip(views, files)})
        self.assertEqual(self.discover(registry, files), files)
        self.assertEqual(registry.root_reads, views)

    def test_denied_roots_and_deleted_entries_do_not_abort_search(self):
        path = r"D:\Apps\WorkBuddy.exe"
        registry = FakeRegistry({("user", 256): PermissionError(), ("user", 512): "query-denied",
                                 ("machine", 256): [PermissionError(), "enum-deleted", entry(icon=path)]})
        self.assertEqual(self.discover(registry, [path]), [path])
        self.assertTrue(all(handle.closed for handle in registry.handles))

    def test_duplicate_paths_are_deduplicated_case_insensitively(self):
        path = r"D:\Apps\WorkBuddy.exe"
        registry = FakeRegistry({("user", 256): [entry(icon=path)],
                                 ("machine", 512): [entry(icon=path.lower())]})
        self.assertEqual(self.discover(registry, [path]), [path])

    def test_no_registry_match_preserves_runtime_not_found(self):
        with self.assertRaises(signin.AuthError) as caught:
            self.discover(FakeRegistry(), full=True)
        self.assertEqual(caught.exception.reason, "RUNTIME_NOT_FOUND")

    def test_override_and_default_directory_skip_registry(self):
        for override in ("", r"D:\Explicit\WorkBuddy.exe"):
            with self.subTest(override=override), patch.object(sys, "platform", "win32"), \
                    patch.dict(os.environ, {"WORKBUDDY_EXE": override}), \
                    patch("os.path.isfile", return_value=True), patch("os.access", return_value=True), \
                    patch.object(signin, "_windows_registry_runtimes") as registry:
                signin.find_workbuddy_runtime()
                registry.assert_not_called()

    def test_invalid_override_is_not_replaced_by_registry_match(self):
        with patch.dict(os.environ, {"WORKBUDDY_EXE": "missing.exe"}), \
                patch("os.path.isfile", return_value=False), \
                patch.object(signin, "_windows_registry_runtimes") as registry:
            with self.assertRaises(signin.AuthError) as caught:
                signin.find_workbuddy_runtime()
        self.assertEqual(caught.exception.reason, "INVALID_RUNTIME_PATH")
        registry.assert_not_called()

    def test_non_windows_never_imports_winreg(self):
        original = builtins.__import__
        def import_module(name, *args, **kwargs):
            if name == "winreg":
                self.fail("non-Windows registry access")
            return original(name, *args, **kwargs)
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch("builtins.__import__", side_effect=import_module):
                self.assertEqual(list(signin._windows_registry_runtimes()), [])

    def test_script_import_does_not_require_winreg(self):
        result = subprocess.run([sys.executable, "-c", "import sys; sys.modules['winreg'] = None; import signin"],
                                cwd=Path(signin.__file__).parent, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
