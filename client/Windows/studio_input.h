/* Local, explicit input endpoint for this FreeRDP connection. */
#include <freerdp/input.h>
#include <freerdp/settings.h>
#include <string.h>

static LRESULT studio_send_input(rdpContext* context, const COPYDATASTRUCT* message)
{
	if (!message || !message->lpData || message->cbData == 0 || message->cbData > 8192)
		return 0;
	const BOOL text = message->dwData == 0x46524431;
	const BOOL keys = message->dwData == 0x46524432;
	if (message->dwData == 0x46524430)
		return 1000 + (freerdp_is_active_state(context) ? 1 : 0) +
		       (freerdp_settings_get_bool(context->settings, FreeRDP_UnicodeInput) ? 2 : 0) +
		       (freerdp_settings_get_bool(context->settings, FreeRDP_SuspendInput) ? 4 : 0);
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
		for (size_t i = 0; i < count && success; i++)
		{
			const BOOL down = freerdp_input_send_unicode_keyboard_event(context->input, 0, chars[i]);
			const BOOL up = freerdp_input_send_unicode_keyboard_event(context->input, KBD_FLAGS_RELEASE, chars[i]);
			success = down && up;
		}
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
