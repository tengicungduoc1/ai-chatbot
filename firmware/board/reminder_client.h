// SPDX-License-Identifier: MIT
#pragma once
#include <cstdint>
#include <string>

// All state is owned by Application's main task. HTTP workers return via Schedule.
class ReminderClient {
public:
    static ReminderClient& GetInstance() {
        static ReminderClient client;
        return client;
    }
    void Tick(bool idle);
    static bool IsAllowedMediaUrl(const std::string& url);
    static std::string NormalizeMediaUrl(const std::string& url);

private:
    bool request_running_ = false;
    bool playback_pending_ = false;
    int64_t next_request_us_ = 0;
    std::string ack_id_;
    std::string ack_status_;
    std::string last_played_id_;
    void Receive(const std::string& body);
    void Finished(const std::string& id, const std::string& status);
};
