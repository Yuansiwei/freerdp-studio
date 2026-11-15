/**
 * FreeRDP: A Remote Desktop Protocol Implementation
 * Windows Clipboard Redirection
 *
 * Copyright 2012 Jason Champion
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
#ifndef FREERDP_CLIENT_WIN_CLIPRDR_H
#define FREERDP_CLIENT_WIN_CLIPRDR_H

#include "wf_client.h"

BOOL wf_cliprdr_init(wfContext* wfc, CliprdrClientContext* cliprdr);
BOOL wf_cliprdr_uninit(wfContext* wfc, CliprdrClientContext* cliprdr);

/* 远端剪贴板当前是否带文本格式（决定 Ctrl+V 是否走原生粘贴）。 */
BOOL wf_cliprdr_remote_has_text(void);
/* 上一次复制是否发生在本机（TRUE=本机，FALSE=远端）。 */
BOOL wf_cliprdr_last_copy_was_host(void);
/* 取本机剪贴板里的 Unicode 文本；调用方负责 free()。返回 NULL 表示没有文本。 */
WCHAR* wf_cliprdr_take_host_text(size_t* length);

#endif /* FREERDP_CLIENT_WIN_CLIPRDR_H */
