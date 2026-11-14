/**
 * FreeRDP: A Remote Desktop Protocol Implementation
 *
 * Copyright 2022 Stefan Koell
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

#include <windows.h>
#include <wincred.h>
#include <stdio.h>
#include <malloc.h>
#include <freerdp/settings.h>

#include "wf_defaults.h"

static PCWSTR ValidateString(const BYTE* pb, ULONG cb)
{
	if (!pb || !cb)
		return 0;

	if (cb % sizeof(WCHAR) != 0)
		return 0;

	return (PCWSTR)pb;
}

static void AddDefaultSettings_I(rdpSettings* settings, size_t idHostname, size_t idUsername,
                                 size_t idDomain, size_t idPassword)
{
	const UINT32 port = idHostname == FreeRDP_ServerHostname ?
	                    freerdp_settings_get_uint32(settings, FreeRDP_ServerPort) : 3389;
	const char* TERMSRV = port == 3389 ? "TERMSRV/%s" : "TERMSRV/%s:%u";

	PSTR TargetName = nullptr;
	PSTR UserName = nullptr;
	PWSTR TargetNameW = nullptr;
	PWSTR ServerHostNameW = nullptr;
	PWSTR ParsedUserNameW = nullptr;
	PWSTR ParsedDomainW = nullptr;
	PWSTR PasswordNullTerminatedW = nullptr;
	PCREDENTIALW Credential = WINPR_C_ARRAY_INIT;

	PCSTR ServerHostname = freerdp_settings_get_string(settings, idHostname);

	if (!ServerHostname)
		return;

	BOOL bExistUserName = freerdp_settings_get_string(settings, idUsername) != 0;
	BOOL bExistPassword = freerdp_settings_get_string(settings, idPassword) != 0;

	if (bExistUserName && bExistPassword)
		return;

	int len = _snprintf(TargetName, 0, TERMSRV, ServerHostname, port);
	if (len < 0)
		goto fail;

	len++;
	TargetName = (PSTR)malloc(len);

	if (!TargetName)
		goto fail;

	const int rlen = _snprintf(TargetName, len, TERMSRV, ServerHostname, port);
	if (rlen < 0)
		goto fail;

	TargetName[len - 1] = 0;

	TargetNameW = ConvertUtf8ToWCharAlloc(TargetName, nullptr);
	if (!TargetNameW)
		goto fail;

	if (!CredReadW(TargetNameW, CRED_TYPE_GENERIC, 0, &Credential))
		goto fail;

	if (!bExistPassword)
	{
		const WCHAR* PasswordW =
		    ValidateString(Credential->CredentialBlob, Credential->CredentialBlobSize);

		PasswordNullTerminatedW = (PWSTR)calloc(Credential->CredentialBlobSize + 1, sizeof(WCHAR));

		if (!PasswordNullTerminatedW)
			goto fail;

		memcpy(PasswordNullTerminatedW, PasswordW, Credential->CredentialBlobSize);

		if (PasswordNullTerminatedW)
		{
			if (!freerdp_settings_set_string_from_utf16(settings, idPassword,
			                                            PasswordNullTerminatedW))
			{
				goto fail;
			}
		}
	}

	if (!bExistUserName)
	{
		const WCHAR* UserNameW = Credential->UserName;

		if (UserNameW)
		{
			ParsedUserNameW = calloc(CREDUI_MAX_USERNAME_LENGTH + 1, sizeof(WCHAR));
			if (!ParsedUserNameW)
				goto fail;

			ParsedDomainW = calloc(CREDUI_MAX_DOMAIN_TARGET_LENGTH + 1, sizeof(WCHAR));
			if (!ParsedDomainW)
				goto fail;

			DWORD ParseResult =
			    CredUIParseUserNameW(UserNameW, ParsedUserNameW, CREDUI_MAX_USERNAME_LENGTH + 1,
			                         ParsedDomainW, CREDUI_MAX_DOMAIN_TARGET_LENGTH + 1);

			if (ParseResult == NO_ERROR)
			{
				if (!freerdp_settings_set_string_from_utf16(settings, idUsername, ParsedUserNameW))
					goto fail;

				if (*ParsedDomainW != 0)
				{
					if (!freerdp_settings_set_string_from_utf16(settings, idDomain, ParsedDomainW))
						goto fail;
				}
			}
			else if (ParseResult == ERROR_INVALID_ACCOUNT_NAME)
			{
				if (!freerdp_settings_set_string_from_utf16(settings, idUsername, UserNameW))
					goto fail;
			}
		}
	}

fail:
	if (Credential)
	{
		CredFree(Credential);
	}
	free(TargetName);
	free(UserName);
	free(TargetNameW);
	free(ServerHostNameW);
	free(ParsedUserNameW);
	free(ParsedDomainW);
	free(PasswordNullTerminatedW);
	return;
}

void WINAPI AddDefaultSettings(rdpSettings* settings)
{
	AddDefaultSettings_I(settings, FreeRDP_ServerHostname, FreeRDP_Username, FreeRDP_Domain,
	                     FreeRDP_Password);
	AddDefaultSettings_I(settings, FreeRDP_GatewayHostname, FreeRDP_GatewayUsername,
	                     FreeRDP_GatewayDomain, FreeRDP_GatewayPassword);
}

BOOL SaveDefaultSettings(rdpSettings* settings)
{
	const char* host = freerdp_settings_get_string(settings, FreeRDP_ServerHostname);
	const char* user = freerdp_settings_get_string(settings, FreeRDP_Username);
	const char* domain = freerdp_settings_get_string(settings, FreeRDP_Domain);
	const char* password = freerdp_settings_get_string(settings, FreeRDP_Password);
	const UINT32 port = freerdp_settings_get_uint32(settings, FreeRDP_ServerPort);
	char target[1100] = { 0 };
	char account[1100] = { 0 };
	if (!host || !user || !password)
		return FALSE;
	int size = port == 3389 ? snprintf(target, sizeof(target), "TERMSRV/%s", host) :
	                         snprintf(target, sizeof(target), "TERMSRV/%s:%u", host, port);
	if (size < 0 || (size_t)size >= sizeof(target))
		return FALSE;
	size = domain && domain[0] ? snprintf(account, sizeof(account), "%s\\%s", domain, user) :
	                           snprintf(account, sizeof(account), "%s", user);
	if (size < 0 || (size_t)size >= sizeof(account))
		return FALSE;
	WCHAR* targetW = ConvertUtf8ToWCharAlloc(target, nullptr);
	WCHAR* accountW = ConvertUtf8ToWCharAlloc(account, nullptr);
	WCHAR* passwordW = ConvertUtf8ToWCharAlloc(password, nullptr);
	BOOL result = FALSE;
	if (targetW && accountW && passwordW)
	{
		const size_t bytes = wcslen(passwordW) * sizeof(WCHAR);
		if (bytes <= CRED_MAX_CREDENTIAL_BLOB_SIZE)
		{
			CREDENTIALW credential = { 0 };
			credential.Type = CRED_TYPE_GENERIC;
			credential.TargetName = targetW;
			credential.UserName = accountW;
			credential.CredentialBlobSize = (DWORD)bytes;
			credential.CredentialBlob = (BYTE*)passwordW;
			credential.Persist = CRED_PERSIST_LOCAL_MACHINE;
			result = CredWriteW(&credential, 0);
		}
	}
	if (passwordW)
		SecureZeroMemory(passwordW, wcslen(passwordW) * sizeof(WCHAR));
	free(passwordW);
	free(accountW);
	free(targetW);
	return result;
}
