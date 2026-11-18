// SPDX-License-Identifier: MIT
#include "reminder_client.h"
#include "device_token.h"
#include "application.h"
#include "board.h"
#include "chatbot_local_config.h"
#include "server_url.h"
#include "system_info.h"
#include <cJSON.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <memory>
#include <new>
#include <utility>

namespace {
constexpr int64_t kPollIntervalUs = 5000000;
constexpr size_t kMaxResponse = 8192;
constexpr int64_t kRequestDeadlineUs = 10000000;
const char* TAG = "ReminderClient";
struct Request {
    std::string id;
    std::string status;
};

// Exact methods verified against 78/esp-ml307 Http, used by this upstream PIN.
int Fetch(const Request& request, std::string& body) {
    auto token = DeviceToken::Get();
    if (token.empty()) return 0;
    auto http = Board::GetInstance().GetNetwork()->CreateHttp(2);
    if (!http) return 0;
    http->SetTimeout(5000);
    http->SetHeader("Authorization", "Bearer " + token);
    http->SetHeader("Accept", "application/json");
    http->SetHeader("Accept-Encoding", "identity");
    std::string url = ServerUrl::GetOrigin();
    bool ack = !request.id.empty();
    if (ack) {
        url += "/device/ack";
        auto root = cJSON_CreateObject();
        if (!root) return 0;
        cJSON_AddStringToObject(root, "id", request.id.c_str());
        cJSON_AddStringToObject(root, "status", request.status.c_str());
        char* json = cJSON_PrintUnformatted(root);
        cJSON_Delete(root);
        if (!json) return 0;
        http->SetHeader("Content-Type", "application/json");
        http->SetContent(std::string(json));
        cJSON_free(json);
    } else {
        // A MAC has only hex digits and colons; safe in this query parameter.
        url += "/device/reminders?device_id=" + SystemInfo::GetMacAddress();
    }
    auto deadline = esp_timer_get_time() + kRequestDeadlineUs;
    int code = 0;
    if (http->Open(ack ? "POST" : "GET", url)) {
        auto status = http->GetStatusCode();
        if (status) code = *status;
        // No redirects: HttpClient returns 3xx rather than following Location.
        if (code == 200 && !ack) {
            char buffer[512];
            while (true) {
                if (esp_timer_get_time() > deadline) { code = 0; break; }
                auto read = http->Read(buffer, sizeof(buffer));
                if (!read || *read < 0) { code = 0; break; }
                if (*read == 0) break;
                if (body.size() + *read > kMaxResponse) { code = 0; break; }
                body.append(buffer, *read);
            }
        }
    }
    http->Close();
    return code;
}
}  // namespace

bool ReminderClient::IsAllowedMediaUrl(const std::string& url) {
    const std::string prefix = ServerUrl::GetOrigin() + "/media/";
    if (url.size() > 512 || url.compare(0, prefix.size(), prefix) != 0 ||
        url.size() <= prefix.size() + 4 || url.substr(url.size() - 4) != ".ogg") return false;
    for (size_t i = prefix.size(); i < url.size() - 4; ++i) {
        const char c = url[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    return true;
}

std::string ReminderClient::NormalizeMediaUrl(const std::string& url) {
    const auto scheme = url.compare(0, 7, "http://") == 0 ? 7 :
                        url.compare(0, 8, "https://") == 0 ? 8 : 0;
    if (!scheme || url.size() > 512) return {};
    const auto path = url.find('/', scheme);
    if (path == std::string::npos || path == scheme || url.find_first_of("?#@\\ \t\r\n", scheme) < path) return {};
    const auto media_path = url.substr(path);
    const auto origin = ServerUrl::GetOrigin();
    if (origin.empty() || !IsAllowedMediaUrl(origin + media_path)) return {};
    return origin + media_path;
}

void ReminderClient::Tick(bool idle) {
    if (request_running_ || playback_pending_) return;
    // ACKs can finish during a conversation; a new poll may not start there.
    if (!idle) {
        if (ack_id_.empty()) return;
    }
    const auto now = esp_timer_get_time();
    if (now < next_request_us_) return;
    auto request = new (std::nothrow) Request{ack_id_, ack_status_};
    if (!request) return;
    request_running_ = true;
    next_request_us_ = now + kPollIntervalUs;
    if (xTaskCreate([](void* arg) {
            std::unique_ptr<Request> request(static_cast<Request*>(arg));
            std::string body;
            int code = Fetch(*request, body);
            Application::GetInstance().Schedule(
                [id = request->id, status = request->status, code, body = std::move(body)]() {
                    auto& client = ReminderClient::GetInstance();
                    client.request_running_ = false;
                    if (!id.empty()) {
                        // 400 means expired/invalidated issuance; do not block later reminders.
                        if (code == 200 || code == 400 || code == 409 || code == 410) {
                            client.ack_id_.clear();
                            client.ack_status_.clear();
                        }
                    } else if (code == 200) {
                        client.Receive(body);
                    }
                });
            request.reset();
            vTaskDelete(nullptr);
        }, "reminder_http", 8192, request, 2, nullptr) != pdPASS) {
        request_running_ = false;
        delete request;
        ESP_LOGW(TAG, "Cannot create reminder HTTP task");
    }
}

void ReminderClient::Receive(const std::string& body) {
    auto root = cJSON_ParseWithLength(body.c_str(), body.size());
    if (!root) return;
    auto notification = cJSON_GetObjectItemCaseSensitive(root, "notification");
    auto id = cJSON_GetObjectItemCaseSensitive(notification, "id");
    auto url = cJSON_GetObjectItemCaseSensitive(notification, "audio_url");
    auto text = cJSON_GetObjectItemCaseSensitive(notification, "text");
    if (!cJSON_IsString(id) || !id->valuestring[0] || std::string(id->valuestring).size() > 128) {
        cJSON_Delete(root);
        return;
    }
    const std::string identifier = id->valuestring;
    if (identifier == last_played_id_) {
        Finished(identifier, "played");  // Retry a lost ACK, never replay in this boot.
    } else if (!cJSON_IsString(url) || NormalizeMediaUrl(url->valuestring).empty() ||
               !cJSON_IsString(text) || std::string(text->valuestring).size() > 4096) {
        Finished(identifier, "failed");
    } else {
        playback_pending_ = true;
        // Rechecks idle on the main task; an HTTP response may race a button press.
        if (!Application::GetInstance().PlayReminder(NormalizeMediaUrl(url->valuestring), text->valuestring,
                [this, identifier](const std::string& status) { Finished(identifier, status); })) {
            Finished(identifier, "cancelled");
        }
    }
    cJSON_Delete(root);
}

void ReminderClient::Finished(const std::string& id, const std::string& status) {
    playback_pending_ = false;
    ack_id_ = id;
    ack_status_ = status;
    if (status == "played") last_played_id_ = id;
    next_request_us_ = 0;
}
