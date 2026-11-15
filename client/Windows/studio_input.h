/* Local, explicit input endpoint for this FreeRDP connection. */
#include <freerdp/input.h>
#include <freerdp/settings.h>
#include <string.h>

/* 用扫描码敲一个字符（与物理键盘完全一致）。返回 FALSE 表示该字符无法映射。 */
static BOOL studio_type_char(rdpContext* context, WCHAR ch)
{
	SHORT vkState = 0;
	BYTE vk = 0;
	BOOL shift = FALSE;
	UINT32 scan = 0;

	if (!context || !context->input)
		return FALSE;

	if (ch == L'\r')
		return TRUE;
	if (ch == L'\n')
	{
		freerdp_input_send_keyboard_event_ex(context->input, TRUE, FALSE, 0x1C);
		freerdp_input_send_keyboard_event_ex(context->input, FALSE, FALSE, 0x1C);
		return TRUE;
	}
	if (ch == L'\t')
	{
		freerdp_input_send_keyboard_event_ex(context->input, TRUE, FALSE, 0x0F);
		freerdp_input_send_keyboard_event_ex(context->input, FALSE, FALSE, 0x0F);
		return TRUE;
	}

	vkState = VkKeyScanW(ch);
	if (vkState == -1)
		return FALSE;

	vk = (BYTE)(vkState & 0xFF);
	shift = (vkState & 0x0100) != 0;
	scan = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
	if (!scan)
		return FALSE;

	if (shift)
		freerdp_input_send_keyboard_event_ex(context->input, TRUE, FALSE, 0x2A);

	freerdp_input_send_keyboard_event_ex(context->input, TRUE, FALSE, scan);
	freerdp_input_send_keyboard_event_ex(context->input, FALSE, FALSE, scan);

	if (shift)
		freerdp_input_send_keyboard_event_ex(context->input, FALSE, FALSE, 0x2A);

	return TRUE;
}

/* 发送一段文本的键盘事件。节流由调用方（定时器）负责，本函数不做任何 Sleep。 */
static BOOL studio_send_text_events(rdpContext* context, const WCHAR* chars, size_t count)
{
	BOOL success = TRUE;
	size_t failed = 0;

	if (!context || !context->input || !chars)
		return FALSE;

	for (size_t i = 0; i < count; i++)
	{
		/* CRLF 只产生一次换行：跳过 \r，保留 \n */
		if (chars[i] == L'\r')
			continue;

		const BOOL down = freerdp_input_send_unicode_keyboard_event(context->input, 0, chars[i]);
		const BOOL up =
		    freerdp_input_send_unicode_keyboard_event(context->input, KBD_FLAGS_RELEASE, chars[i]);
		const BOOL ok = down && up;

		if (!ok)
			failed++;
	}

	success = (failed == 0);
	if (!success)
	{
		const char* temp = getenv("TEMP");
		if (temp && temp[0])
		{
			char logPath[MAX_PATH * 2] = WINPR_C_ARRAY_INIT;
			FILE* fp = NULL;
			(void)snprintf(logPath, sizeof(logPath), "%s\\studio-paste.log", temp);
			fp = fopen(logPath, "a");
			if (fp)
			{
				(void)fprintf(fp, "BATCH_FAILED: %zu of %zu events rejected\n", failed, count);
				fclose(fp);
			}
		}
	}

	return success;
}
static LRESULT studio_send_input(rdpContext* context, const COPYDATASTRUCT* message)
{
	if (!message || !message->lpData || message->cbData == 0 || message->cbData > 8192)
		return 0;
	const BOOL text = message->dwData == 0x46524431;
	const BOOL keys = message->dwData == 0x46524432;
	const BOOL wheel = message->dwData == 0x46524434;
	if (message->dwData == 0x46524430)
		return 1000 + (freerdp_is_active_state(context) ? 1 : 0) +
		       (freerdp_settings_get_bool(context->settings, FreeRDP_UnicodeInput) ? 2 : 0) +
		       (freerdp_settings_get_bool(context->settings, FreeRDP_SuspendInput) ? 4 : 0);
	if (wheel)
	{
		/* 载荷是一个 int32 滚轮格数：正数向上，负数向下。 */
		if (message->cbData != sizeof(INT32) || !freerdp_is_active_state(context) ||
		    freerdp_shall_disconnect_context(context))
			return 0;

		const INT32 notches = *(const INT32*)message->lpData;
		const INT32 count = (notches < 0) ? -notches : notches;
		const BOOL suspended = freerdp_settings_get_bool(context->settings, FreeRDP_SuspendInput);
		if (!freerdp_settings_set_bool(context->settings, FreeRDP_SuspendInput, FALSE))
			return 0;

		BOOL sent = TRUE;
		const UINT16 up = PTR_FLAGS_WHEEL | 0x0078;
		const UINT16 down = PTR_FLAGS_WHEEL | PTR_FLAGS_WHEEL_NEGATIVE | 0x0088;
		for (INT32 i = 0; i < count && i < 50 && sent; i++)
			sent = freerdp_input_send_mouse_event(context->input, (notches > 0) ? up : down, 0, 0);

		if (!freerdp_settings_set_bool(context->settings, FreeRDP_SuspendInput, suspended))
			return 0;
		return sent ? 1 : 0;
	}
	if ((!text && !keys) || !freerdp_is_active_state(context) ||
	    freerdp_shall_disconnect_context(context))
		return 0;
	if (text && (message->cbData % sizeof(WCHAR)) != 0)
		return 0;
	if (text && !freerdp_settings_get_bool(context->settings, FreeRDP_UnicodeInput))
		return 2;
	if (keys && (message->cbData % sizeof(UINT32)) != 0)
		return 0;
	const BOOL suspended = freerdp_settings_get_bool(context->settings, FreeRDP_SuspendInput);
	if (!freerdp_settings_set_bool(context->settings, FreeRDP_SuspendInput, FALSE))
		return 0;
	BOOL success = TRUE;
	if (text)
	{
		const WCHAR* chars = (const WCHAR*)message->lpData;
		const size_t count = message->cbData / sizeof(WCHAR);
		success = studio_send_text_events(context, chars, count);
	}
	else
	{
		const UINT32* events = (const UINT32*)message->lpData;
		const size_t count = message->cbData / sizeof(UINT32);
		/* Validate the entire request before emitting any key. */
		for (size_t i = 0; i < count; i++)
			if (events[i] & ~0x830000FFU)
				success = FALSE;
		if (success)
			for (size_t i = 0; i < count; i++)
			{
				const BOOL sent = freerdp_input_send_keyboard_event(context->input, (UINT16)(events[i] >> 16), (BYTE)events[i]);
				success = sent && success;
			}
	}
	if (!freerdp_settings_set_bool(context->settings, FreeRDP_SuspendInput, suspended))
		return 0;
	return success ? 1 : 0;
}
