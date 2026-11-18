#pragma once
#include "settings.h"
#include <string>

namespace DeviceToken {
inline std::string Get() {
    Settings settings("wifi", false);
    auto token = settings.GetString("pairing_token");
    if (token.size() < 24 || token.size() > 256) return {};
    for (unsigned char c : token) {
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) return {};
    }
    return token;
}
}
