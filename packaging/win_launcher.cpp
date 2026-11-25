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
        (flags & CREATE_NO_WINDOW) ? FALSE : TRUE,
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

static bool ReadFooterAndExtractZip(const std::wstring& exe_path, const std::wstring& app_base_dir, std::wstring& out_app_dir) {
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
    std::wstring ver_w = Utf8ToWide(ver_ascii);

    out_app_dir = app_base_dir + L"\\app\\" + ver_w;
    std::wstring ready_marker = out_app_dir + L"\\.ready";
    if (FileExistsW(ready_marker) && FileExistsW(out_app_dir + L"\\openzenless.py")) {
        CloseHandle(hIn);
        return true;
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

    DWORD exit_code = 1;
    std::wstring tar_cmd = L"tar.exe -xf " + QuoteArgW(tmp_zip) + L" -C " + QuoteArgW(out_app_dir);
    bool extracted = RunProcessWaitW(tar_cmd, out_app_dir, CREATE_NO_WINDOW, &exit_code) && exit_code == 0;
    if (!extracted || !FileExistsW(out_app_dir + L"\\openzenless.py")) {
        std::wstring ps_cmd =
            L"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
            L"\"Expand-Archive -Force -Path '" + tmp_zip + L"' -DestinationPath '" + out_app_dir + L"'\"";
        extracted = RunProcessWaitW(ps_cmd, out_app_dir, CREATE_NO_WINDOW, &exit_code) && exit_code == 0;
    }
    DeleteFileW(tmp_zip.c_str());

    if (!extracted || !FileExistsW(out_app_dir + L"\\openzenless.py")) {
        return false;
    }

    HANDLE hMark = CreateFileW(ready_marker.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hMark != INVALID_HANDLE_VALUE) {
        const char ok_txt[] = "ok\n";
        DWORD nw = 0;
        WriteFile(hMark, ok_txt, sizeof(ok_txt) - 1, &nw, nullptr);
        CloseHandle(hMark);
    }
    return true;
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    std::wstring exe_path = GetExePathW();
    std::wstring local_app = GetEnvW(L"LOCALAPPDATA");
    if (local_app.empty()) {
        std::wstring up = GetEnvW(L"USERPROFILE");
        local_app = (up.empty() ? L"C:\\Users\\Public" : up) + L"\\AppData\\Local";
    }
    std::wstring data_dir = local_app + L"\\OpenZenless";
    EnsureDirTreeW(data_dir);

    std::wstring app_dir;
    if (!ReadFooterAndExtractZip(exe_path, data_dir, app_dir)) {
        // Fallback if launched loose next to openzenless.py (portable zip / repo)
        size_t sl = exe_path.find_last_of(L"\\/");
        std::wstring parent_dir = (sl != std::wstring::npos) ? exe_path.substr(0, sl) : L".";
        if (FileExistsW(parent_dir + L"\\openzenless.py")) {
            app_dir = parent_dir;
        } else {
            MessageBoxW(
                nullptr,
                L"Failed to extract embedded OpenZenless runtime payload.",
                L"OpenZenless Error",
                MB_OK | MB_ICONERROR
            );
            return 1;
        }
    }

    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    std::wstring extra_args;
    bool has_cli_args = (argv != nullptr && argc > 1);
    if (has_cli_args) {
        AttachConsole(ATTACH_PARENT_PROCESS);
        for (int i = 1; i < argc; ++i) {
            if (!extra_args.empty()) extra_args += L" ";
            extra_args += QuoteArgW(argv[i]);
        }
    }
    if (argv) LocalFree(argv);

    std::wstring venv_py = data_dir + L"\\venv\\Scripts\\python.exe";
    std::wstring venv_pyw = data_dir + L"\\venv\\Scripts\\pythonw.exe";
    std::wstring run_bat = app_dir + L"\\run.bat";
    std::wstring main_py = app_dir + L"\\openzenless.py";

    if (has_cli_args) {
        if (FileExistsW(venv_py)) {
            DWORD code = 0;
            std::wstring cmd = QuoteArgW(venv_py) + L" " + QuoteArgW(main_py) + L" " + extra_args;
            if (RunProcessWaitW(cmd, app_dir, 0, &code)) {
                return static_cast<int>(code);
            }
        }
        DWORD code = 0;
        std::wstring cmd = L"cmd.exe /c " + QuoteArgW(run_bat) + L" " + extra_args;
        RunProcessWaitW(cmd, app_dir, 0, &code);
        return static_cast<int>(code);
    }

    // GUI mode: run setup batch once if venv is not yet bootstrapped
    if (!FileExistsW(venv_pyw)) {
        DWORD setup_rc = 1;
        std::wstring setup_cmd = L"cmd.exe /c " + QuoteArgW(run_bat) + L" --setup-only";
        RunProcessWaitW(setup_cmd, app_dir, 0, &setup_rc);
    }

    if (FileExistsW(venv_pyw)) {
        STARTUPINFOW si{};
        si.cb = sizeof(si);
        PROCESS_INFORMATION pi{};
        std::wstring gui_cmd = QuoteArgW(venv_pyw) + L" " + QuoteArgW(main_py) + L" gui";
        std::vector<wchar_t> mutable_cmd(gui_cmd.begin(), gui_cmd.end());
        mutable_cmd.push_back(L'\0');
        if (CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, app_dir.c_str(), &si, &pi)) {
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            return 0;
        }
    }

    // Fallback to run.bat (e.g. if user has global system python)
    SetEnvironmentVariableW(L"OPENZENLESS_NO_CONSOLE", L"1");
    std::wstring fallback_cmd = L"cmd.exe /c " + QuoteArgW(run_bat) + L" gui";
    DWORD code = 0;
    RunProcessWaitW(fallback_cmd, app_dir, 0, &code);
    return static_cast<int>(code);
}
