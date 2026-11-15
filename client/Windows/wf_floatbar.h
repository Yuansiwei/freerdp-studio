/**
 * FreeRDP: A Remote Desktop Protocol Implementation
 * Windows Float Bar
 *
 * Copyright 2013 Zhang Zhaolong <zhangzl2013@126.com>
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

#ifndef FREERDP_CLIENT_WIN_FLOATBAR_H
#define FREERDP_CLIENT_WIN_FLOATBAR_H

#include <winpr/crt.h>

typedef struct s_FloatBar wfFloatBar;
typedef struct wf_context wfContext;

wfFloatBar* wf_floatbar_new(wfContext* wfc, HINSTANCE window, DWORD flags);
void wf_floatbar_free(wfFloatBar* floatbar);

BOOL wf_floatbar_toggle_fullscreen(wfFloatBar* floatbar, BOOL fullscreen);
BOOL wf_floatbar_reset_position(wfFloatBar* floatbar);
/* Resize the bar (and its controls) to a new session scale and centre it again. */
BOOL wf_floatbar_set_scale(wfFloatBar* floatbar, UINT32 zoom);

/* 屏幕坐标是否落在浮条的可拖动区域（条内、但不在按钮上）。 */
BOOL wf_floatbar_hit_drag_area(wfFloatBar* floatbar, int screenX, int screenY);
/* 把浮条移动到指定左边距（子窗口坐标），内部会夹在父窗口范围内。 */
void wf_floatbar_move_left(wfFloatBar* floatbar, LONG left);
/* 浮条的当前左边距与宽高。 */
LONG wf_floatbar_left(wfFloatBar* floatbar);
UINT32 wf_floatbar_height(wfFloatBar* floatbar);

#endif /* FREERDP_CLIENT_WIN_FLOATBAR_H */
