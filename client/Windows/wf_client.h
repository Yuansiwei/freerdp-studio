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

#ifndef FREERDP_CLIENT_WIN_INTERFACE_H
#define FREERDP_CLIENT_WIN_INTERFACE_H

#include <winpr/windows.h>

#include <winpr/collections.h>

#ifdef WITH_PROGRESS_BAR
#include <shobjidl.h>
#endif

#include <freerdp/api.h>
#include <freerdp/freerdp.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/gdi/dc.h>
#include <freerdp/gdi/region.h>
#include <freerdp/codec/color.h>

#include <freerdp/client/rail.h>
#include <freerdp/channels/channels.h>
#include <freerdp/codec/rfx.h>
#include <freerdp/codec/nsc.h>
#include <freerdp/client/file.h>

/* Identifies the single window timer that injects the idle keep alive key. */
#define STUDIO_KEEPALIVE_TIMER 0x53544615
/* The keep alive key is only sent after this much time without any input.
 * 60 s, not 120 s: the remote locked within ~4 min of the last real input (2026-11-18). */
#define STUDIO_KEEPALIVE_IDLE_MS 60000
/* One shot timer that announces the initial size and scale once the display channel is up. */
#define STUDIO_SYNC_TIMER 0x53544501
#define STUDIO_SYNC_DELAY_MS 1500

#include "wf_channels.h"
#include "wf_floatbar.h"
#include "wf_event.h"
#include "wf_cliprdr.h"

#ifdef __cplusplus
extern "C"
{
#endif

// System menu constants
#define SYSCOMMAND_ID_SMARTSIZING 1000
#define SYSCOMMAND_ID_REQUEST_CONTROL 1001

	typedef struct
	{
		rdpBitmap _bitmap;
		HDC hdc;
		HBITMAP bitmap;
		HBITMAP org_bitmap;
		BYTE* pdata;
	} wfBitmap;

	typedef struct
	{
		rdpPointer pointer;
		HCURSOR cursor;
	} wfPointer;

	struct wf_context
	{
		rdpClientContext common;

		int offset_x;
		int offset_y;
		int fullscreen_toggle;
		int fullscreen;
		int percentscreen;
		WCHAR* window_title;
		int client_x;
		int client_y;
		int client_width;
		int client_height;

		HANDLE keyboardThread;

		HICON icon;
		HWND hWndParent;
		HINSTANCE hInstance;
		WNDCLASSEX wndClass;
		LPCTSTR wndClassName;
		HCURSOR hDefaultCursor;

		UINT systemMenuInsertPosition;

		HWND hwnd;
		UINT64 studioKeepAliveCount;
		BOOL studioSaveCredentials;
		/* Monitor the fullscreen session is pinned to, and whether it is valid. */
		RECT studioMonitorRect;
		BOOL studioMonitorSet;
		/* Session scale last announced to the server, in percent. */
		UINT32 studioSentScale;
		/* The session scale was derived from the display, not pinned by the user. */
		BOOL studioAutoScale;
		BOOL is_shown;
		ITaskbarList3* taskBarList;
		POINT diff;

		wfBitmap* primary;
		wfBitmap* drawing;
		HCURSOR cursor;
		HBRUSH brush;
		HBRUSH org_brush;
		RECT update_rect;
		RECT scale_update_rect;

		DWORD mainThreadId;
		DWORD keyboardThreadId;

		rdpFile* connectionRdpFile;

		BOOL disablewindowtracking;

		BOOL updating_scrollbars;
		BOOL xScrollVisible;
		int xMinScroll;
		int xCurrentScroll;
		int xMaxScroll;

		BOOL yScrollVisible;
		int yMinScroll;
		int yCurrentScroll;
		int yMaxScroll;

		void* clipboard;
		CliprdrClientContext* cliprdr;

		wfFloatBar* floatbar;

		RailClientContext* rail;
		wHashTable* railWindows;
		BOOL isConsole;

		DispClientContext* disp;
		UINT64 lastSentDate;
		BOOL wasMaximized;
	};

	/**
	 * Client Interface
	 */

	FREERDP_API int RdpClientEntry(RDP_CLIENT_ENTRY_POINTS* pEntryPoints);
	FREERDP_API int freerdp_client_set_window_size(wfContext* wfc, int width, int height);
	FREERDP_API void wf_size_scrollbars(wfContext* wfc, UINT32 client_width, UINT32 client_height);

	/* Map a monitor DPI to the closest session scale factor Windows accepts. */
	UINT32 studio_scale_from_dpi(UINT32 dpi);

	/* Snap a fullscreen session onto the display it currently overlaps. */
	void wf_snap_to_monitor(wfContext* wfc);

	/* Session scale factor that matches the display the window currently sits on. */
	UINT32 wf_window_display_scale(wfContext* wfc);

#ifdef __cplusplus
}
#endif

#endif /* FREERDP_CLIENT_WIN_INTERFACE_H */
