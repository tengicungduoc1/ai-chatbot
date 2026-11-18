// SPDX-License-Identifier: MIT
#pragma once

#include "chatbot_local_config.h"
#include "settings.h"
#include <string>

class ServerUrl {
public:
    static std::string GetOrigin() {
        Settings settings("wifi", false);
        auto origin = settings.GetString("ota_url");
        if (!origin.empty() && origin.back() == '/') origin.pop_back();
        // ponytail: Invalid NVS falls back to compiled LAN; changing networks still needs portal.
        if (!IsValidOrigin(origin)) origin = CHATBOT_SERVER_URL;
        return IsValidOrigin(origin) ? origin : std::string();
    }

    static std::string GetWebSocketUrl() {
        auto origin = GetOrigin();
        if (origin.empty()) return {};
        if (origin.compare(0, 7, "http://") == 0) origin.replace(0, 7, "ws://");
        else if (origin.compare(0, 8, "https://") == 0) origin.replace(0, 8, "wss://");
        return origin + "/xiaozhi/v1/";
    }

    static std::string GetCheckVersionUrl() {
        auto origin = GetOrigin();
        return origin.empty() ? std::string() : origin + "/xiaozhi/ota/";
    }

    static bool IsAllowedFirmwareUrl(const std::string& url) {
        const auto origin = GetOrigin();
        return !origin.empty() && url.size() > origin.size() + 1 &&
               url.compare(0, origin.size(), origin) == 0 && url[origin.size()] == '/' &&
               url.find_first_of("?#@\\\\ \t\r\n", origin.size()) == std::string::npos;
    }

    static bool IsValidOrigin(const std::string& value) {
        if (value.size() < 8 || value.size() > 192) return false;
        const size_t scheme = value.compare(0, 7, "http://") == 0 ? 7 :
                              value.compare(0, 8, "https://") == 0 ? 8 : 0;
        if (!scheme) return false;
        auto authority = value.substr(scheme);
        if (authority.empty() || authority.find_first_of("/?#@\\ \t\r\n") != std::string::npos) return false;

        const auto colon = authority.find(':');
        const auto host = authority.substr(0, colon);
        unsigned octets[4] = {};
        size_t count = 0, digits = 0;
        for (size_t i = 0; i < host.size(); ++i) {
            const char c = host[i];
            if (c == '.') {
                if (digits == 0 || ++count >= 4) return false;
                digits = 0;
            } else if (c >= '0' && c <= '9' && digits < 3) {
                if (digits == 0 && c == '0' && i + 1 < host.size() && host[i + 1] != '.') return false;
                octets[count] = octets[count] * 10 + static_cast<unsigned>(c - '0');
                if (octets[count] > 255) return false;
                ++digits;
            } else {
                return false;
            }
        }
        if (digits == 0 || count != 3) return false;
        if (!(octets[0] == 10 || (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31) ||
              (octets[0] == 192 && octets[1] == 168))) return false;
        if (colon != std::string::npos) {
            if (authority.find(':', colon + 1) != std::string::npos || colon + 1 == authority.size() ||
                authority.size() - colon - 1 > 5) return false;
            unsigned port = 0;
            for (size_t i = colon + 1; i < authority.size(); ++i) {
                const char c = authority[i];
                if (c < '0' || c > '9') return false;
                port = port * 10 + static_cast<unsigned>(c - '0');
                if (port > 65535) return false;
            }
            if (port == 0) return false;
        }
        return true;
    }
};