// Included by application.cc; all session state transitions run on the main task.
void Application::ToggleContinuousConversation() {
    const auto state = GetDeviceState();
    if (state == kDeviceStateNotifying) { StopNotification(); return; }
    if (continuous_.active) { CancelContinuousConversation(); return; }
    if (continuous_.aborting) { continuous_.Boot(); return; }
    if (state == kDeviceStateSpeaking) {
        audio_service_.ResetDecoder();
        AbortSpeaking(kAbortReasonNone);
        SetDeviceState(kDeviceStateIdle);
        return;
    }
    if (state != kDeviceStateIdle || !protocol_ || !network_connected_) return;
    continuous_.Boot();
    continuous_transport_ = true;
    ++transport_generation_;
    OpenContinuousConversation();
}

void Application::OpenContinuousConversation() {
    if (!continuous_.active || continuous_.aborting || !protocol_) return;
    const auto epoch = continuous_.epoch;
    if (!protocol_->IsAudioChannelOpened()) {
        SetDeviceState(kDeviceStateConnecting);
        Schedule([this, epoch]() {
            if (!continuous_.active || continuous_.epoch != epoch || !protocol_) return;
            Board::GetInstance().SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
            if (!protocol_->OpenAudioChannel()) { CancelContinuousConversation(true); return; }
            if (continuous_.active && continuous_.epoch == epoch) ArmContinuousCapture();
        });
    } else ArmContinuousCapture();
}

void Application::ArmContinuousCapture() {
    if (!continuous_.active || continuous_.aborting || continuous_.processing ||
        continuous_.capturing || continuous_.draining || !protocol_) return;
    if (!audio_service_.IsPlaybackIdle()) { continuous_.draining = true; return; }
    continuous_audio_open_ = false;
    conversation_pending_ = true;
    listening_mode_ = kListeningModeAutoStop;
    if (!SetDeviceState(kDeviceStateListening)) { CancelContinuousConversation(true); return; }
    audio_service_.EnableWakeWordDetection(false);
    if (!audio_service_.StartContinuousCapture(++capture_generation_)) {
        CancelContinuousConversation(true);
        Board::GetInstance().GetDisplay()->ShowNotification("Khong khoi tao duoc VAD");
    } else {
        ESP_LOGI(TAG, "Voice capture armed generation=%lu", static_cast<unsigned long>(capture_generation_));
    }
}

void Application::CancelContinuousConversation(bool close_channel) {
    continuous_audio_open_ = false;
    ++transport_generation_;
    ++capture_generation_;
    audio_service_.CancelContinuousCapture();
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.EnableWakeWordDetection(false);
    audio_service_.ResetDecoder();
    conversation_pending_ = false;
    processing_deadline_ = 0;
    if (close_channel) {
        continuous_.Disconnect();
        if (protocol_ && protocol_->IsAudioChannelOpened()) protocol_->CloseAudioChannel();
    } else if (continuous_.active) {
        continuous_.Cancel();
        abort_deadline_ = esp_timer_get_time() + 10000000;
        if (!protocol_ || !protocol_->SendContinuous("abort", nullptr, continuous_.abort_epoch,
                                                    continuous_.abort_utterance)) {
            continuous_.Disconnect();
            if (protocol_) protocol_->CloseAudioChannel();
        }
    }
    SetDeviceState(kDeviceStateIdle);
}

void Application::HandleContinuousPacket(std::unique_ptr<AudioStreamPacket> packet) {
    if (!packet || !continuous_.active || continuous_.aborting ||
        packet->capture_generation != capture_generation_ || !protocol_) return;
    switch (packet->capture_event) {
        case CaptureEvent::Start:
            ESP_LOGI(TAG, "Voice utterance start generation=%lu", static_cast<unsigned long>(capture_generation_));
            if (!continuous_.StartUtterance() ||
                !protocol_->SendContinuous("listen", "start", continuous_.epoch, continuous_.session_utterance))
                CancelContinuousConversation(true);
            break;
        case CaptureEvent::Audio:
            if (continuous_.capturing && !protocol_->SendAudio(std::move(packet)))
                CancelContinuousConversation(true);
            break;
        case CaptureEvent::Stop:
            if (!continuous_.capturing) return;
            ESP_LOGI(TAG, "Voice utterance stop generation=%lu", static_cast<unsigned long>(capture_generation_));
            audio_service_.EnableVoiceProcessing(false);
            continuous_.EndUtterance();
            processing_deadline_ = esp_timer_get_time() + 45000000;
            Board::GetInstance().GetDisplay()->SetStatus("Dang xu ly...");
            if (!protocol_->SendContinuous("listen", "stop", continuous_.epoch, continuous_.session_utterance))
                CancelContinuousConversation(true);
            break;
        case CaptureEvent::Error:
            CancelContinuousConversation();
            Board::GetInstance().GetDisplay()->ShowNotification("Loi hang doi am thanh");
            break;
    }
}

void Application::TryContinuousRearm() {
    if (!continuous_.active || !continuous_.draining || scheduled_audio_.load() != 0 ||
        !audio_service_.IsPlaybackIdle()) return;
    continuous_.PlaybackDrained();
    ESP_LOGI(TAG, "Voice playback drained; rearming capture");
    ArmContinuousCapture();
}

void Application::HandleContinuousJson(const std::string& json) {
    auto root = cJSON_Parse(json.c_str());
    if (!root) return;
    auto id = [root](const char* key) -> uint64_t {
        const auto value = cJSON_GetObjectItem(root, key);
        if (!cJSON_IsNumber(value) || !std::isfinite(value->valuedouble) ||
            value->valuedouble <= 0 || value->valuedouble > 9007199254740991.0 ||
            std::floor(value->valuedouble) != value->valuedouble) return 0;
        return static_cast<uint64_t>(value->valuedouble);
    };
    auto string = [root](const char* key) -> const char* {
        auto value = cJSON_GetObjectItem(root, key);
        return cJSON_IsString(value) && value->valuestring ? value->valuestring : "";
    };
    const auto epoch = id("conversation_epoch"), uid = id("utterance_id");
    const std::string type = string("type"), state = string("state");
    if (type == "tts" && state == "stop" && std::string(string("reason")) == "aborted") {
        if (continuous_.Acknowledge(epoch, uid)) {
            abort_deadline_ = 0;
            if (continuous_.active) OpenContinuousConversation();
        }
        cJSON_Delete(root);
        return;
    }
    if (!continuous_.Matches(epoch, uid) || !continuous_.processing) {
        cJSON_Delete(root);
        return;
    }
    auto display = Board::GetInstance().GetDisplay();
    if (type == "tts" && state == "start") {
        if (!continuous_audio_open_) {
            audio_service_.ResetDecoder();
            continuous_audio_open_ = true;
            aborted_ = false;
            SetDeviceState(kDeviceStateSpeaking);
        }
        // Bound stalled generation, not total song duration; each audio packet renews this.
        processing_deadline_ = esp_timer_get_time() + 15000000;
    } else if (type == "tts" && state == "stop") {
        continuous_audio_open_ = false;
        continuous_.Complete(epoch, uid);
        processing_deadline_ = 0;
        TryContinuousRearm();
    } else if ((type == "tts" && state == "sentence_start") || type == "stt") {
        const auto text = string("text");
        std::vector<TextGlyph> glyphs;
        uint8_t bpp = 0;
        if (TextGlyphPayload::Parse(root, glyphs, bpp)) display->AddTextGlyphs(glyphs, bpp);
        display->SetChatMessage(type == "stt" ? "user" : "assistant", text);
    }
    cJSON_Delete(root);
}
