/**
 * FreeRDP: A Remote Desktop Protocol Implementation
 * Windows Client
 *
 * Copyright 2009-2011 Jay Sorg
 * Copyright 2010-2011 Vic Lee
 * Copyright 2010-2011 Marc-Andre Moreau <marcandre.moreau@gmail.com>
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <freerdp/config.h>

#include <winpr/windows.h>

#include <winpr/crt.h>

#include <freerdp/freerdp.h>
#include <freerdp/constants.h>

#include <freerdp/client/file.h>
#include <freerdp/client/cmdline.h>
#include <freerdp/client/channels.h>
#include <freerdp/channels/channels.h>

#include "../resource/resource.h"

#include <wf_client.h>
#include <wf_defaults.h>

#include <shellapi.h>
#include <wincred.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#define STUDIO_DEFAULT_PORT 3389
#define STUDIO_HISTORY_MAX 10

static void studio_logf(const char* format, ...)
{
	char path[MAX_PATH * 2] = WINPR_C_ARRAY_INIT;
	const char* temp = getenv("TEMP");
	FILE* fp = nullptr;
	va_list args;

	if (!temp || !temp[0])
		return;
	(void)snprintf(path, sizeof(path), "%s\\freerdp-dialog.log", temp);

	fp = fopen(path, "a");
	if (!fp)
		return;

	va_start(args, format);
	vfprintf(fp, format, args);
	va_end(args);
	fputc('\n', fp);
	fclose(fp);
}
/* 读取 Windows 凭据管理器里已保存的登录信息，用于预填对话框。 */
static void studio_prefill_credentials(HWND hDlg, const char* server)
{
	WCHAR target[512] = WINPR_C_ARRAY_INIT;
	CREDENTIALW* generic = nullptr;
	CREDENTIALW* domain = nullptr;
	const WCHAR* user = nullptr;

	if (!server || !server[0])
		return;

	/* 通用条目里保存密码，域条目只有账号名，两者都取一次。 */
	(void)swprintf_s(target, ARRAYSIZE(target), L"TERMSRV/%S", server);
	(void)CredReadW(target, CRED_TYPE_GENERIC, 0, &generic);
	(void)CredReadW(target, CRED_TYPE_DOMAIN_PASSWORD, 0, &domain);

	if (generic && generic->UserName && generic->UserName[0])
		user = generic->UserName;
	else if (domain && domain->UserName && domain->UserName[0])
		user = domain->UserName;

	if (user)
		SetDlgItemTextW(hDlg, IDC_STUDIO_USER, user);

	if (generic && generic->CredentialBlob && (generic->CredentialBlobSize >= sizeof(WCHAR)))
	{
		const size_t chars = generic->CredentialBlobSize / sizeof(WCHAR);
		WCHAR* passW = calloc(chars + 1, sizeof(WCHAR));
		if (passW)
		{
			memcpy(passW, generic->CredentialBlob, generic->CredentialBlobSize);
			SetDlgItemTextW(hDlg, IDC_STUDIO_PASSWORD, passW);
			SecureZeroMemory(passW, (chars + 1) * sizeof(WCHAR));
			free(passW);
		}
	}

	if (generic)
		CredFree(generic);
	if (domain)
		CredFree(domain);
}

static void studio_history_path(char* path, size_t len)
{
	const char* base = getenv("LOCALAPPDATA");
	if (!base || !base[0])
		base = getenv("USERPROFILE");

	if (!base || !base[0])
	{
		wcsncpy_s((wchar_t*)path, len, L"", 0);
		path[0] = '\0';
		return;
	}

	(void)snprintf(path, len, "%s\\FreeRDP\\servers.txt", base);
}

static void studio_history_load(HWND combo)
{
	char path[MAX_PATH * 2] = WINPR_C_ARRAY_INIT;
	char line[512] = WINPR_C_ARRAY_INIT;
	FILE* fp = nullptr;

	studio_history_path(path, sizeof(path));
	if (path[0] == '\0')
		return;

	fp = fopen(path, "rb");
	if (!fp)
		return;

	while (fgets(line, sizeof(line), fp))
	{
		size_t n = strcspn(line, "\r\n");
		line[n] = '\0';
		if (line[0] == '\0')
			continue;
		SendMessageA(combo, CB_ADDSTRING, 0, (LPARAM)line);
	}

	fclose(fp);
	if (SendMessageA(combo, CB_GETCOUNT, 0, 0) > 0)
		SendMessageA(combo, CB_SETCURSEL, 0, 0);
}

static void studio_history_save(const char* value)
{
	char path[MAX_PATH * 2] = WINPR_C_ARRAY_INIT;
	char existing[STUDIO_HISTORY_MAX][512];
	size_t count = 0;
	FILE* fp = nullptr;

	if (!value || !value[0])
		return;

	studio_history_path(path, sizeof(path));
	if (path[0] == '\0')
		return;

	if (strstr(path, "\\FreeRDP\\"))
	{
		char dir[MAX_PATH * 2] = WINPR_C_ARRAY_INIT;
		strncpy_s(dir, sizeof(dir), path, sizeof(dir) - 1);
		char* slash = strrchr(dir, '\\');
		if (slash)
		{
			*slash = '\0';
			(void)SHCreateDirectoryExA(nullptr, dir, nullptr);
		}
	}

	fp = fopen(path, "rb");
	if (fp)
	{
		char line[512] = WINPR_C_ARRAY_INIT;
		while (count < STUDIO_HISTORY_MAX && fgets(line, sizeof(line), fp))
		{
			size_t n = strcspn(line, "\r\n");
			line[n] = '\0';
			if (line[0] == '\0' || _stricmp(line, value) == 0)
				continue;
			strncpy_s(existing[count], sizeof(existing[count]), line, sizeof(existing[count]) - 1);
			count++;
		}
		fclose(fp);
	}

	fp = fopen(path, "wb");
	if (!fp)
		return;

	fprintf(fp, "%s\r\n", value);
	for (size_t x = 0; x < count; x++)
		fprintf(fp, "%s\r\n", existing[x]);

	fclose(fp);
}

typedef struct
{
	char host[256];
	UINT32 port;
	char user[256];
	char password[256];
	BOOL remember;
	BOOL useSso;
} StudioConnect;

static INT_PTR CALLBACK studio_connect_proc(HWND hDlg, UINT msg, WPARAM wParam, LPARAM lParam)
{
	StudioConnect* state = (StudioConnect*)GetWindowLongPtrA(hDlg, GWLP_USERDATA);

	switch (msg)
	{
		case WM_INITDIALOG:
			state = (StudioConnect*)lParam;
			SetWindowLongPtrA(hDlg, GWLP_USERDATA, (LONG_PTR)state);
			{
				HWND combo = GetDlgItem(hDlg, IDC_STUDIO_SERVER);
				studio_history_load(combo);
				if (SendMessageA(combo, CB_GETCOUNT, 0, 0) == 0)
					SetDlgItemTextA(hDlg, IDC_STUDIO_SERVER, "192.168.98.184");
				SendMessageA(combo, CB_SETEDITSEL, 0, MAKELPARAM(0, -1));
					{
					const HDC screen = GetDC(nullptr);
					const int dpi = screen ? GetDeviceCaps(screen, LOGPIXELSY) : 96;
					if (screen)
						ReleaseDC(nullptr, screen);
					const int height = -MulDiv(14, dpi, 72);
					HFONT titleFont = CreateFontA(height, 0, 0, 0, FW_BOLD, 0, 0, 0,
					                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS,
					                                CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
					                                DEFAULT_PITCH, "Microsoft YaHei UI");
					if (titleFont)
						SendDlgItemMessageA(hDlg, IDC_STUDIO_TITLE, WM_SETFONT, (WPARAM)titleFont, TRUE);
				}
				SetFocus(combo);

				/* 预填 Windows 凭据管理器里已保存的用户名与密码 */
				{
					char current[512] = WINPR_C_ARRAY_INIT;
					GetDlgItemTextA(hDlg, IDC_STUDIO_SERVER, current, sizeof(current));
					studio_prefill_credentials(hDlg, current);
				}
			}
				/* 默认勾选记住密码 */
				CheckDlgButton(hDlg, IDC_STUDIO_REMEMBER, BST_CHECKED);
			CheckDlgButton(hDlg, IDC_STUDIO_SSO, BST_CHECKED);
			return FALSE;
		case WM_ERASEBKGND:
		{
			/* 头部横幅按 mstsc 实测比例绘制：占客户区 20%，#DEE8F5 -> #FAFCFE 渐变。 */
			HDC dc = (HDC)wParam;
			RECT rc;
			RECT band;
			const int top[3] = { 222, 232, 245 };
			const int bottom[3] = { 250, 252, 254 };
			const int strips = 40;

			GetClientRect(hDlg, &rc);
			FillRect(dc, &rc, (HBRUSH)(COLOR_BTNFACE + 1));

			band = rc;
			band.bottom = (rc.bottom * 20) / 100;
			if (band.bottom < 2)
				band.bottom = 2;

			for (int i = 0; i < strips; i++)
			{
				const int t = (i * 255) / (strips - 1);
				const int r = top[0] + ((bottom[0] - top[0]) * t) / 255;
				const int g = top[1] + ((bottom[1] - top[1]) * t) / 255;
				const int b = top[2] + ((bottom[2] - top[2]) * t) / 255;
				RECT strip = band;
				HBRUSH brush = CreateSolidBrush(RGB(r, g, b));

				strip.top = band.top + ((band.bottom - band.top) * i) / strips;
				strip.bottom = band.top + ((band.bottom - band.top) * (i + 1)) / strips + 1;
				FillRect(dc, &strip, brush);
				DeleteObject(brush);
			}

			{
				RECT line = band;
				HBRUSH brush = CreateSolidBrush(RGB(218, 218, 218));

				line.top = band.bottom;
				line.bottom = band.bottom + 1;
				FillRect(dc, &line, brush);
				DeleteObject(brush);
			}
			return TRUE;
		}

		case WM_CTLCOLORSTATIC:
			SetBkMode((HDC)wParam, TRANSPARENT);
			if (GetDlgCtrlID((HWND)lParam) == IDC_STUDIO_TITLE)
				SetTextColor((HDC)wParam, RGB(0, 51, 153));
			return (LRESULT)GetStockObject(NULL_BRUSH);

		case WM_COMMAND:
			switch (LOWORD(wParam))
			{
				case IDOK:
				{
					char buffer[512] = WINPR_C_ARRAY_INIT;
					GetDlgItemTextA(hDlg, IDC_STUDIO_SERVER, buffer, sizeof(buffer));

					char* text = buffer;
					while (*text == ' ')
						text++;
					char* end = text + strlen(text);
					while (end > text && (end[-1] == ' ' || end[-1] == '\r' || end[-1] == '\n'))
						end--;
					*end = '\0';

					if (*text == '\0')
						return TRUE;

					UINT32 port = STUDIO_DEFAULT_PORT;
					char* colon = strrchr(text, ':');
					if (colon && colon[1] != '\0')
					{
						BOOL digits = TRUE;
						for (const char* p = colon + 1; *p; p++)
							if (*p < '0' || *p > '9')
								digits = FALSE;
						if (digits)
						{
							port = (UINT32)strtoul(colon + 1, nullptr, 10);
							*colon = '\0';
						}
					}

					strncpy_s(state->host, sizeof(state->host), text, sizeof(state->host) - 1);
					state->port = (port > 0 && port <= 65535) ? port : STUDIO_DEFAULT_PORT;

					GetDlgItemTextA(hDlg, IDC_STUDIO_USER, state->user, sizeof(state->user));
					GetDlgItemTextA(hDlg, IDC_STUDIO_PASSWORD, state->password,
					                sizeof(state->password));
					state->remember =
					    IsDlgButtonChecked(hDlg, IDC_STUDIO_REMEMBER) == BST_CHECKED;
					state->useSso = IsDlgButtonChecked(hDlg, IDC_STUDIO_SSO) == BST_CHECKED;

					EndDialog(hDlg, IDOK);
					return TRUE;
				}
				case IDC_STUDIO_HELP:
					MessageBoxA(hDlg, "输入要连接的计算机名称（可带端口），再填写可选凭据。地址历史会自动记录。", "FreeRDP", MB_OK | MB_ICONINFORMATION);
					return TRUE;
				case IDCANCEL:
					EndDialog(hDlg, IDCANCEL);
					return TRUE;
				default:
					break;
			}
			break;
		default:
			break;
	}
	return FALSE;
}

/* Returns TRUE when the dialog was confirmed and state holds a target. */
static BOOL studio_ask_server(StudioConnect* state)
{
	state->port = STUDIO_DEFAULT_PORT;
	INT_PTR result = DialogBoxParamA(GetModuleHandle(nullptr), MAKEINTRESOURCEA(IDD_STUDIO_CONNECT),
	                                 nullptr, studio_connect_proc, (LPARAM)state);
	return (result == IDOK) && (state->host[0] != '\0');
}

/* TRUE when the command line already names a target. */
static BOOL studio_has_target(int argc, char** argv)
{
	for (int i = 1; i < argc; i++)
	{
		const char* a = argv[i];
		if (!a)
			continue;

		if (a[0] == '/' || a[0] == '-')
		{
			const char* v = a + 1;
			if (_strnicmp(v, "v:", 2) == 0)
				return TRUE;
			if (_stricmp(v, "v") == 0)
				return TRUE;
		}
		else if (strlen(a) > 4 && _stricmp(a + strlen(a) - 4, ".rdp") == 0)
			return TRUE;
	}
	return FALSE;
}

INT WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPSTR lpCmdLine, int nCmdShow)
{
	/* Use physical pixels on high-DPI displays and when moving between monitors. */
	typedef BOOL(WINAPI* StudioSetDpiContext)(HANDLE);
	HMODULE studioUser32 = GetModuleHandleW(L"user32.dll");
	StudioSetDpiContext studioSetDpi = (StudioSetDpiContext)GetProcAddress(studioUser32, "SetProcessDpiAwarenessContext");
	if (studioSetDpi)
		studioSetDpi((HANDLE)(LONG_PTR)-4);
	else
		SetProcessDPIAware();
	int status;
	HANDLE thread;
	wfContext* wfc;
	DWORD dwExitCode;
	rdpContext* context;
	rdpSettings* settings;
	LPWSTR cmd;
	char** argv = nullptr;
	RDP_CLIENT_ENTRY_POINTS clientEntryPoints = WINPR_C_ARRAY_INIT;
	int ret = 1;
	int argc = 0;
	LPWSTR* args = nullptr;
	char** effectiveArgv = nullptr;
	int effectiveArgc = 0;
	char* injected = nullptr;
	StudioConnect target = WINPR_C_ARRAY_INIT;

	WINPR_UNUSED(hInstance);
	WINPR_UNUSED(hPrevInstance);
	WINPR_UNUSED(lpCmdLine);
	WINPR_UNUSED(nCmdShow);

	RdpClientEntry(&clientEntryPoints);
	context = freerdp_client_context_new(&clientEntryPoints);

	if (!context)
		return -1;

	cmd = GetCommandLineW();

	if (!cmd)
		goto out;

	args = CommandLineToArgvW(cmd, &argc);

	if (!args || (argc <= 0))
		goto out;

	argv = calloc((size_t)argc, sizeof(char*));

	if (!argv)
		goto out;

	for (int i = 0; i < argc; i++)
	{
		int size = WideCharToMultiByte(CP_UTF8, 0, args[i], -1, nullptr, 0, nullptr, nullptr);
		if (size <= 0)
			goto out;
		argv[i] = calloc((size_t)size, sizeof(char));

		if (!argv[i])
			goto out;

		if (WideCharToMultiByte(CP_UTF8, 0, args[i], -1, argv[i], size, nullptr, nullptr) != size)
			goto out;
	}

	freerdp_client_warn_deprecated(argc, argv);

	settings = context->settings;
	wfc = (wfContext*)context;

	if (!settings || !wfc)
		goto out;

	/* Ask for a target when the command line does not name one. */
	effectiveArgv = argv;
	effectiveArgc = argc;

	if (!studio_has_target(argc, argv))
	{
		char entry[512] = WINPR_C_ARRAY_INIT;
		char history[512] = WINPR_C_ARRAY_INIT;

		if (!studio_ask_server(&target))
		{
			ret = 0;
			goto out;
		}

		if (target.port == STUDIO_DEFAULT_PORT)
		{
			(void)snprintf(entry, sizeof(entry), "/v:%s", target.host);
			(void)snprintf(history, sizeof(history), "%s", target.host);
		}
		else
		{
			(void)snprintf(entry, sizeof(entry), "/v:%s:%u", target.host, target.port);
			(void)snprintf(history, sizeof(history), "%s:%u", target.host, target.port);
		}

		injected = _strdup(entry);
		if (!injected)
			goto out;

		studio_history_save(history);

		/* 对话框启动时补上默认参数，避免退化成 1024x768 且没有动态分辨率。 */
		{
			static const char* const defaults[] = { "/f",             "+toggle-fullscreen",
			                                        "+dynamic-resolution", "+auto-reconnect",
			                                        "/gdi:sw",        "/bpp:32",
			                                        "/kbd:unicode:on", "/cert:ignore",
			                                        "/log-level:WARN" };
			const size_t extra = ARRAYSIZE(defaults);
			size_t added = 0;

			effectiveArgv = calloc((size_t)argc + 1 + extra, sizeof(char*));
			if (!effectiveArgv)
				goto out;

			for (int i = 0; i < argc; i++)
				effectiveArgv[i] = argv[i];

			effectiveArgv[argc] = injected;
			added = 1;

			for (size_t d = 0; d < extra; d++)
			{
				effectiveArgv[argc + 1 + d] = _strdup(defaults[d]);
				if (!effectiveArgv[argc + 1 + d])
					break;
				added++;
			}

			effectiveArgc = argc + (int)added;
		}
	}

	status = freerdp_client_settings_parse_command_line(settings, effectiveArgc, effectiveArgv, FALSE);
	studio_logf("parse argc=%d status=%d argv[last]=[%s]", effectiveArgc, status,
	             (effectiveArgv && effectiveArgc > 0) ? effectiveArgv[effectiveArgc - 1] : "(none)");
	if (status)
	{
		ret = freerdp_client_settings_command_line_status_print(settings, status, argc, argv);
		goto out;
	}

	AddDefaultSettings(settings);

	/* 勾选「使用当前 Windows 凭据」时不发送任何凭据，交给 NLA/SSO。
	 * 否则把 domain\user 拆成 Domain + Username 两个字段再发送。 */
	if (!target.useSso)
	{
		if (target.user[0])
		{
			char domain[256] = WINPR_C_ARRAY_INIT;
			const char* account = target.user;
			const char* split = strchr(target.user, '\\');

			if (split && ((size_t)(split - target.user) < sizeof(domain)))
			{
				const size_t length = (size_t)(split - target.user);
				memcpy(domain, target.user, length);
				domain[length] = '\0';
				account = split + 1;

				if (!freerdp_settings_set_string(settings, FreeRDP_Domain, domain))
					goto out;
			}

			if (!freerdp_settings_set_string(settings, FreeRDP_Username, account))
				goto out;
		}

		if (target.password[0])
		{
			if (!freerdp_settings_set_string(settings, FreeRDP_Password, target.password))
				goto out;
			wfc->studioSaveCredentials = target.remember;
		}
	}

	/* Name the window and its input endpoint after the host only. */
	const char* inputHost = freerdp_settings_get_string(settings, FreeRDP_ServerHostname);
	if (!inputHost || !freerdp_settings_set_string(settings, FreeRDP_WindowTitle, inputHost))
		goto out;

	studio_logf("before start: host=[%s] port=%u user=[%s]",
	             freerdp_settings_get_string(settings, FreeRDP_ServerHostname),
	             freerdp_settings_get_uint32(settings, FreeRDP_ServerPort),
	             freerdp_settings_get_string(settings, FreeRDP_Username) ? freerdp_settings_get_string(settings, FreeRDP_Username) : "(null)");
	const int startRc = freerdp_client_start(context);
	studio_logf("start returned %d", startRc);
	if (startRc != 0)
		goto out;

	thread = freerdp_client_get_thread(context);

	if (thread)
	{
		if (WaitForSingleObject(thread, INFINITE) == WAIT_OBJECT_0)
		{
			GetExitCodeThread(thread, &dwExitCode);
			ret = (int)dwExitCode;
		}
	}

	if (ret != 0)
	{
		/* 把静默失败变成可读的错误提示。 */
		const UINT32 code = freerdp_get_last_error(context);
		const char* name = freerdp_get_last_error_name(code);
		const char* text = freerdp_get_last_error_string(code);
		WCHAR message[1024] = WINPR_C_ARRAY_INIT;
		const WCHAR* hint = L"";

		if (code == FREERDP_ERROR_AUTHENTICATION_FAILED ||
		    code == FREERDP_ERROR_CONNECT_LOGON_FAILURE)
			hint = L"\n\n提示：凭据可能已失效。可在对话框中重新输入，或删除 Windows 凭据管理器里的 TERMSRV 条目。";
		else if (code == FREERDP_ERROR_CONNECT_ACTIVATION_TIMEOUT)
			hint = L"\n\n提示：远端会话可能仍在释放中，稍等几十秒再试。";

		(void)_snwprintf_s(message, ARRAYSIZE(message), _TRUNCATE,
		                  L"无法建立远程桌面连接。\n\n错误：%S (%S)\n代码：0x%08X%ls",
		                  text ? text : "未知", name ? name : "unknown", code, hint);

		studio_logf("connect failed: 0x%08X %s", code, name ? name : "unknown");
		MessageBoxW(nullptr, message, L"FreeRDP", MB_OK | MB_ICONERROR);
	}

	if (freerdp_client_stop(context) != 0)
		goto out;

out:
	studio_logf("reached out: ret=%d", ret);
	freerdp_client_context_free(context);

	if (effectiveArgv && (effectiveArgv != argv))
		free(effectiveArgv);

	free(injected);

	if (argv)
	{
		for (int i = 0; i < argc; i++)
			free(argv[i]);

		free(argv);
	}

	LocalFree(args);
	return ret;
}

#ifdef WITH_WIN_CONSOLE
int main()
{
	return WinMain(nullptr, nullptr, nullptr, 0);
}
#endif
