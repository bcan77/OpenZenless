#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <shlobj.h>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr char kMagic[8] = {'O', 'Z', 'W', 'I', 'N', 'Z', 'I', 'P'};

#pragma pack(push, 1)
struct PayloadFooter {
    uint64_t offset;
    uint64_t size;
    char version[32];
    char magic[8];
};
#pragma pack(pop)

static bool FileExistsW(const std::wstring& path) {
    DWORD attr = GetFileAttributesW(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

static bool DirExistsW(const std::wstring& path) {
    DWORD attr = GetFileAttributesW(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

static bool EnsureDirTreeW(const std::wstring& path) {
    if (path.empty()) return false;
    if (DirExistsW(path)) return true;
    std::wstring acc;
    for (size_t i = 0; i < path.size(); ++i) {
        wchar_t c = path[i];
        acc.push_back(c);
        if ((c == L'\\' || c == L'/') && acc.size() > 3) {
            CreateDirectoryW(acc.c_str(), nullptr);
        }
    }
    CreateDirectoryW(path.c_str(), nullptr);
    return DirExistsW(path);
}

static std::wstring GetEnvW(const wchar_t* name) {
    DWORD len = GetEnvironmentVariableW(name, nullptr, 0);
    if (len == 0) return L"";
    std::wstring val(len, L'\0');
    GetEnvironmentVariableW(name, &val[0], len);
    while (!val.empty() && val.back() == L'\0') val.pop_back();
    return val;
}

static std::wstring GetExePathW() {
    std::wstring buf(32768, L'\0');
    DWORD n = GetModuleFileNameW(nullptr, &buf[0], static_cast<DWORD>(buf.size()));
    buf.resize(n);
    return buf;
}

static std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return L"";
    int needed = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), nullptr, 0);
    if (needed <= 0) return L"";
    std::wstring out(needed, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), &out[0], needed);
    return out;
}

static std::wstring QuoteArgW(const std::wstring& arg) {
    if (arg.find_first_of(L" \t\"") == std::wstring::npos && !arg.empty()) {
        return arg;
    }
    std::wstring out = L"\"";
    for (wchar_t c : arg) {
        if (c == L'"') out += L'\\';
        out += c;
    }
    out += L"\"";
    return out;
}

static void EnableHighDpiAwareness() {
    HMODULE u32 = GetModuleHandleW(L"user32.dll");
    if (u32) {
        using SetDpiCtxFn = BOOL(WINAPI*)(HANDLE);
        auto fn = reinterpret_cast<SetDpiCtxFn>(GetProcAddress(u32, "SetProcessDpiAwarenessContext"));
        if (fn) {
            // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
            if (fn(reinterpret_cast<HANDLE>(static_cast<LONG_PTR>(-4)))) {
                return;
            }
            // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = (HANDLE)-3
            if (fn(reinterpret_cast<HANDLE>(static_cast<LONG_PTR>(-3)))) {
                return;
            }
        }
    }
    SetProcessDPIAware();
}

static bool RunProcessWaitW(const std::wstring& cmdline, const std::wstring& cwd, DWORD flags, DWORD* out_exit = nullptr) {
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    std::vector<wchar_t> mutable_cmd(cmdline.begin(), cmdline.end());
    mutable_cmd.push_back(L'\0');
    BOOL ok = CreateProcessW(
        nullptr,
        mutable_cmd.data(),
        nullptr,
        nullptr,
        FALSE,
        flags,
        nullptr,
        cwd.empty() ? nullptr : cwd.c_str(),
        &si,
        &pi
    );
    if (!ok) return false;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    if (out_exit) *out_exit = code;
    return true;
}

static bool CoreFilesPresentW(const std::wstring& app_dir) {
    return FileExistsW(app_dir + L"\\openzenless.py") &&
           FileExistsW(app_dir + L"\\run.bat") &&
           FileExistsW(app_dir + L"\\requirements.txt");
}

static void WriteMarkerW(const std::wstring& path, const char* content = "ok\n") {
    HANDLE hMark = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hMark != INVALID_HANDLE_VALUE) {
        DWORD nw = 0;
        WriteFile(hMark, content, static_cast<DWORD>(std::strlen(content)), &nw, nullptr);
        CloseHandle(hMark);
    }
}

static std::wstring ReadLogTailW(const std::wstring& log_path, size_t max_bytes = 1800) {
    HANDLE h = CreateFileW(log_path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return L"";
    LARGE_INTEGER sz{};
    if (!GetFileSizeEx(h, &sz) || sz.QuadPart <= 0) {
        CloseHandle(h);
        return L"";
    }
    DWORD to_read = static_cast<DWORD>(sz.QuadPart > static_cast<LONGLONG>(max_bytes) ? max_bytes : sz.QuadPart);
    LARGE_INTEGER seek{};
    seek.QuadPart = sz.QuadPart - to_read;
    SetFilePointerEx(h, seek, nullptr, FILE_BEGIN);
    std::string buf(to_read, '\0');
    DWORD nr = 0;
    BOOL ok = ReadFile(h, &buf[0], to_read, &nr, nullptr);
    CloseHandle(h);
    if (!ok || nr == 0) return L"";
    buf.resize(nr);
    return Utf8ToWide(buf);
}

static bool ExtractZipArchiveW(const std::wstring& tmp_zip, const std::wstring& out_app_dir) {
    SetEnvironmentVariableW(L"OZ_ZIP", tmp_zip.c_str());
    SetEnvironmentVariableW(L"OZ_DST", out_app_dir.c_str());

    DWORD exit_code = 1;
    std::wstring sys_dir = GetEnvW(L"SystemRoot");
    if (sys_dir.empty()) sys_dir = L"C:\\Windows";
    std::wstring tar_sys = sys_dir + L"\\System32\\tar.exe";
    std::wstring tar_bin = FileExistsW(tar_sys) ? QuoteArgW(tar_sys) : L"tar.exe";
    std::wstring tar_cmd = tar_bin + L" -xf " + QuoteArgW(tmp_zip) + L" -C " + QuoteArgW(out_app_dir);
    bool extracted = RunProcessWaitW(tar_cmd, out_app_dir, CREATE_NO_WINDOW, &exit_code) &&
                     exit_code == 0 && CoreFilesPresentW(out_app_dir);

    if (!extracted) {
        // Fallback 1: .NET ZipFile via env vars (handles apostrophes, spaces, and Unicode paths safely)
        std::wstring ps_dotnet =
            L"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
            L"\"$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
            L"Add-Type -AssemblyName System.IO.Compression.FileSystem; "
            L"$z=[System.IO.Compression.ZipFile]::OpenRead($env:OZ_ZIP); "
            L"try { foreach($e in $z.Entries) { "
            L"$dest=[System.IO.Path]::GetFullPath([System.IO.Path]::Combine($env:OZ_DST, $e.FullName)); "
            L"$root=[System.IO.Path]::GetFullPath($env:OZ_DST).TrimEnd('\\') + '\\'; "
            L"if (-not $dest.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { continue }; "
            L"if ([string]::IsNullOrEmpty($e.Name)) { [System.IO.Directory]::CreateDirectory($dest) | Out-Null; continue }; "
            L"[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($dest)) | Out-Null; "
            L"[System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true) "
            L"} } finally { $z.Dispose() }\"";
        extracted = RunProcessWaitW(ps_dotnet, out_app_dir, CREATE_NO_WINDOW, &exit_code) &&
                    exit_code == 0 && CoreFilesPresentW(out_app_dir);
    }

    if (!extracted) {
        // Fallback 2: PowerShell Expand-Archive with -LiteralPath via env vars
        std::wstring ps_expand =
            L"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
            L"\"$ProgressPreference='SilentlyContinue'; "
            L"Expand-Archive -Force -LiteralPath $env:OZ_ZIP -DestinationPath $env:OZ_DST\"";
        extracted = RunProcessWaitW(ps_expand, out_app_dir, CREATE_NO_WINDOW, &exit_code) &&
                    exit_code == 0 && CoreFilesPresentW(out_app_dir);
    }

    SetEnvironmentVariableW(L"OZ_ZIP", nullptr);
    SetEnvironmentVariableW(L"OZ_DST", nullptr);
    return extracted;
}

static bool ReadFooterAndExtractZip(
    const std::wstring& exe_path,
    const std::wstring& app_base_dir,
    bool force_repair,
    std::wstring& out_app_dir,
    std::wstring& out_version
) {
    HANDLE hIn = CreateFileW(exe_path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hIn == INVALID_HANDLE_VALUE) return false;

    LARGE_INTEGER fsize{};
    if (!GetFileSizeEx(hIn, &fsize) || fsize.QuadPart < static_cast<LONGLONG>(sizeof(PayloadFooter))) {
        CloseHandle(hIn);
        return false;
    }

    LARGE_INTEGER seek_pos{};
    seek_pos.QuadPart = fsize.QuadPart - static_cast<LONGLONG>(sizeof(PayloadFooter));
    PayloadFooter footer{};
    DWORD nread = 0;
    if (!SetFilePointerEx(hIn, seek_pos, nullptr, FILE_BEGIN) ||
        !ReadFile(hIn, &footer, sizeof(footer), &nread, nullptr) ||
        nread != sizeof(footer) ||
        std::memcmp(footer.magic, kMagic, sizeof(kMagic)) != 0) {
        CloseHandle(hIn);
        return false;
    }

    std::string ver_ascii(footer.version, strnlen(footer.version, sizeof(footer.version)));
    if (ver_ascii.empty()) ver_ascii = "dev";
    out_version = Utf8ToWide(ver_ascii);

    out_app_dir = app_base_dir + L"\\app\\" + out_version;
    std::wstring legacy_marker = out_app_dir + L"\\.ready";
    std::wstring ok_marker = out_app_dir + L"\\.oz_extracted_ok";
    if (!force_repair && (FileExistsW(ok_marker) || FileExistsW(legacy_marker)) && CoreFilesPresentW(out_app_dir)) {
        CloseHandle(hIn);
        return true;
    }

    if (force_repair) {
        DeleteFileW(ok_marker.c_str());
        DeleteFileW(legacy_marker.c_str());
    }

    EnsureDirTreeW(out_app_dir);

    std::wstring temp_dir = GetEnvW(L"TEMP");
    if (temp_dir.empty()) temp_dir = app_base_dir;
    std::wstring tmp_zip = temp_dir + L"\\oz_payload_" + std::to_wstring(GetCurrentProcessId()) + L".zip";

    HANDLE hOut = CreateFileW(tmp_zip.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hOut == INVALID_HANDLE_VALUE) {
        CloseHandle(hIn);
        return false;
    }

    LARGE_INTEGER p_off{};
    p_off.QuadPart = static_cast<LONGLONG>(footer.offset);
    if (!SetFilePointerEx(hIn, p_off, nullptr, FILE_BEGIN)) {
        CloseHandle(hOut);
        CloseHandle(hIn);
        DeleteFileW(tmp_zip.c_str());
        return false;
    }

    std::vector<char> buf(262144);
    uint64_t remaining = footer.size;
    bool copy_ok = true;
    while (remaining > 0) {
        DWORD to_read = static_cast<DWORD>(remaining > buf.size() ? buf.size() : remaining);
        DWORD nr = 0;
        if (!ReadFile(hIn, buf.data(), to_read, &nr, nullptr) || nr == 0) {
            copy_ok = false;
            break;
        }
        DWORD nw = 0;
        if (!WriteFile(hOut, buf.data(), nr, &nw, nullptr) || nw != nr) {
            copy_ok = false;
            break;
        }
        remaining -= nr;
    }
    CloseHandle(hOut);
    CloseHandle(hIn);

    if (!copy_ok) {
        DeleteFileW(tmp_zip.c_str());
        return false;
    }

    bool extracted = ExtractZipArchiveW(tmp_zip, out_app_dir);
    DeleteFileW(tmp_zip.c_str());

    if (!extracted) {
        return false;
    }

    WriteMarkerW(ok_marker, "ok\n");
    WriteMarkerW(legacy_marker, "ok\n");
    return true;
}

static bool ProbeVenvHealthW(const std::wstring& venv_py, const std::wstring& app_dir) {
    if (!FileExistsW(venv_py)) return false;
    std::wstring probe_cmd =
        QuoteArgW(venv_py) +
        L" -c \"import sys, PySide6, PIL, numpy, OpenGL, pygltflib; __import__('zstandard') if sys.version_info < (3, 14) else __import__('compression.zstd')\"";
    DWORD exit_code = 1;
    return RunProcessWaitW(probe_cmd, app_dir, CREATE_NO_WINDOW, &exit_code) && exit_code == 0;
}

static bool IsEqI(const std::wstring& a, const wchar_t* b) {
    return _wcsicmp(a.c_str(), b) == 0;
}

static bool LaunchGuiAndCheckStartupW(
    const std::wstring& exe,
    const std::wstring& main_py,
    const std::wstring& app_dir,
    bool hidden,
    bool& out_crashed_fast,
    DWORD& out_exit_code
) {
    out_crashed_fast = false;
    out_exit_code = 0;
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    std::wstring gui_cmd = QuoteArgW(exe) + L" " + QuoteArgW(main_py) + L" gui";
    std::vector<wchar_t> mutable_cmd(gui_cmd.begin(), gui_cmd.end());
    mutable_cmd.push_back(L'\0');
    DWORD flags = hidden ? CREATE_NO_WINDOW : 0;
    if (!CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, FALSE, flags, nullptr, app_dir.c_str(), &si, &pi)) {
        return false;
    }
    DWORD wait_res = WaitForSingleObject(pi.hProcess, 1500);
    if (wait_res == WAIT_OBJECT_0) {
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        out_exit_code = code;
        out_crashed_fast = (code != 0);
        return !out_crashed_fast;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    EnableHighDpiAwareness();

    std::wstring exe_path = GetExePathW();
    std::wstring local_app = GetEnvW(L"LOCALAPPDATA");
    if (local_app.empty()) {
        std::wstring up = GetEnvW(L"USERPROFILE");
        local_app = (up.empty() ? L"C:\\Users\\Public" : up) + L"\\AppData\\Local";
    }
    std::wstring data_dir = local_app + L"\\OpenZenless";
    EnsureDirTreeW(data_dir);

    std::wstring launch_log = data_dir + L"\\launch.log";
    SetEnvironmentVariableW(L"OPENZENLESS_LAUNCH_LOG", launch_log.c_str());
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");

    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    bool flag_repair = false;
    bool flag_setup_only = false;
    bool flag_console = false;
    bool flag_safe_gl = false;
    std::wstring extra_args;
    bool has_subcmd_args = false;

    if (argv != nullptr && argc > 1) {
        for (int i = 1; i < argc; ++i) {
            std::wstring arg = argv[i];
            if (IsEqI(arg, L"--repair")) {
                flag_repair = true;
            } else if (IsEqI(arg, L"--setup-only")) {
                flag_setup_only = true;
            } else if (IsEqI(arg, L"--console")) {
                flag_console = true;
            } else if (IsEqI(arg, L"--safe-gl")) {
                flag_safe_gl = true;
            } else {
                if (!extra_args.empty()) extra_args += L" ";
                extra_args += QuoteArgW(arg);
                has_subcmd_args = true;
            }
        }
    }
    if (argv) LocalFree(argv);

    if (has_subcmd_args || flag_console || flag_setup_only || flag_repair) {
        AttachConsole(ATTACH_PARENT_PROCESS);
    }

    if (flag_safe_gl) {
        SetEnvironmentVariableW(L"OPENZENLESS_SAFE_GL", L"1");
        SetEnvironmentVariableW(L"QT_OPENGL", L"software");
    }

    std::wstring app_dir;
    std::wstring app_ver = L"dev";
    if (!ReadFooterAndExtractZip(exe_path, data_dir, flag_repair, app_dir, app_ver)) {
        size_t sl = exe_path.find_last_of(L"\\/");
        std::wstring parent_dir = (sl != std::wstring::npos) ? exe_path.substr(0, sl) : L".";
        if (CoreFilesPresentW(parent_dir)) {
            app_dir = parent_dir;
        } else {
            MessageBoxW(
                nullptr,
                L"Failed to extract embedded OpenZenless runtime payload.\n"
                L"Ensure antivirus is not blocking extraction to %LOCALAPPDATA%\\OpenZenless.",
                L"OpenZenless Error",
                MB_OK | MB_ICONERROR
            );
            return 1;
        }
    }

    std::wstring venv_dir = data_dir + L"\\venv";
    std::wstring venv_py = venv_dir + L"\\Scripts\\python.exe";
    std::wstring venv_pyw = venv_dir + L"\\Scripts\\pythonw.exe";
    std::wstring venv_ready_marker = venv_dir + L"\\.oz_ready_" + app_ver;
    std::wstring run_bat = app_dir + L"\\run.bat";
    std::wstring main_py = app_dir + L"\\openzenless.py";

    if (flag_repair) {
        DeleteFileW(venv_ready_marker.c_str());
    }

    if (flag_setup_only) {
        DWORD code = 1;
        std::wstring inner = QuoteArgW(run_bat) + (flag_repair ? L" --repair --setup-only" : L" --setup-only");
        std::wstring cmd = L"cmd.exe /d /s /c \"" + inner + L"\"";
        RunProcessWaitW(cmd, app_dir, 0, &code);
        if (code == 0 && ProbeVenvHealthW(venv_py, app_dir)) {
            WriteMarkerW(venv_ready_marker, "ok\n");
        }
        return static_cast<int>(code);
    }

    if (has_subcmd_args) {
        if (!flag_repair && FileExistsW(venv_py)) {
            DWORD code = 0;
            std::wstring cmd = QuoteArgW(venv_py) + L" " + QuoteArgW(main_py) + L" " + extra_args;
            if (RunProcessWaitW(cmd, app_dir, 0, &code)) {
                return static_cast<int>(code);
            }
        }
        DWORD code = 0;
        std::wstring cmd = L"cmd.exe /d /s /c \"" + QuoteArgW(run_bat) + L" " + extra_args + L"\"";
        RunProcessWaitW(cmd, app_dir, 0, &code);
        return static_cast<int>(code);
    }

    // GUI / default mode: verify venv imports cleanly or run first-run setup
    bool venv_ok = !flag_repair && FileExistsW(venv_py) && FileExistsW(venv_pyw) && FileExistsW(venv_ready_marker);
    if (!venv_ok && !flag_repair && FileExistsW(venv_py)) {
        venv_ok = ProbeVenvHealthW(venv_py, app_dir);
        if (venv_ok) {
            EnsureDirTreeW(venv_dir);
            WriteMarkerW(venv_ready_marker, "ok\n");
        }
    }

    if (!venv_ok) {
        DWORD setup_rc = 1;
        std::wstring inner = QuoteArgW(run_bat) +
                             (flag_repair ? L" --repair --setup-only --no-pause" : L" --setup-only --no-pause");
        std::wstring setup_cmd = L"cmd.exe /d /s /c \"" + inner + L"\"";
        RunProcessWaitW(setup_cmd, app_dir, 0, &setup_rc);
        if (setup_rc == 0 && ProbeVenvHealthW(venv_py, app_dir)) {
            EnsureDirTreeW(venv_dir);
            WriteMarkerW(venv_ready_marker, "ok\n");
            venv_ok = true;
        }
    }

    if (FileExistsW(venv_pyw) && venv_ok) {
        bool crashed_fast = false;
        DWORD gui_rc = 0;
        bool use_hidden = !flag_console;
        std::wstring launch_exe = flag_console ? venv_py : venv_pyw;
        if (LaunchGuiAndCheckStartupW(launch_exe, main_py, app_dir, use_hidden, crashed_fast, gui_rc)) {
            return 0;
        }
        if (crashed_fast && !flag_safe_gl) {
            SetEnvironmentVariableW(L"OPENZENLESS_SAFE_GL", L"1");
            SetEnvironmentVariableW(L"QT_OPENGL", L"software");
            if (LaunchGuiAndCheckStartupW(launch_exe, main_py, app_dir, use_hidden, crashed_fast, gui_rc)) {
                return 0;
            }
        }
        if (crashed_fast) {
            std::wstring tail = ReadLogTailW(launch_log);
            std::wstring msg =
                L"OpenZenless failed to start (exit code " + std::to_wstring(gui_rc) + L").\n\n"
                L"You can try running with --repair or --console to inspect setup/errors.\n"
                L"Log file: " + launch_log;
            if (!tail.empty()) {
                msg += L"\n\nLast log output:\n" + tail;
            }
            MessageBoxW(nullptr, msg.c_str(), L"OpenZenless Startup Error", MB_OK | MB_ICONERROR);
            return static_cast<int>(gui_rc);
        }
    }

    // Fallback to run.bat (e.g. if user has global system python or portable venv failed)
    SetEnvironmentVariableW(L"OPENZENLESS_NO_CONSOLE", flag_console ? L"0" : L"1");
    std::wstring fallback_cmd = L"cmd.exe /d /s /c \"" + QuoteArgW(run_bat) + L" gui\"";
    DWORD code = 0;
    RunProcessWaitW(fallback_cmd, app_dir, 0, &code);
    if (code != 0) {
        std::wstring tail = ReadLogTailW(launch_log);
        std::wstring msg = L"OpenZenless launcher could not start.\nSee log: " + launch_log;
        if (!tail.empty()) {
            msg += L"\n\nLast log output:\n" + tail;
        }
        MessageBoxW(nullptr, msg.c_str(), L"OpenZenless Launch Error", MB_OK | MB_ICONERROR);
    }
    return static_cast<int>(code);
}
