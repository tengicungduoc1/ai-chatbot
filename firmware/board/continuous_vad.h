#pragma once

#include <algorithm>
#include <cstddef>

// Sample-clock segmentation at 16 kHz. The caller owns and sends PCM.
class ContinuousVad {
public:
    enum class Event { None, Start, Audio, Stop };
    struct Result { Event event; size_t samples; };

    Result Push(bool speech, size_t samples) {
        if (finished_) return {Event::None, 0};
        if (!capturing_) {
            history_ = std::min(kPreroll, history_ + std::min(samples, kPreroll));
            onset_ = speech ? std::min(kOnset, onset_ + std::min(samples, kOnset)) : 0;
            if (onset_ < kOnset) return {Event::None, 0};
            capturing_ = true;
            total_ = history_;
            return {Event::Start, history_};
        }
        size_t accepted = std::min(samples, kLimit - total_);
        if (speech) {
            silence_ = 0;
        } else {
            accepted = std::min(accepted, kSilence - silence_);
            silence_ += accepted;
        }
        total_ += accepted;
        if (total_ == kLimit || silence_ == kSilence) {
            finished_ = true;
            return {Event::Stop, accepted};
        }
        return {Event::Audio, accepted};
    }

    void Reset() { *this = ContinuousVad{}; }

private:
    static constexpr size_t kOnset = 160 * 16;
    static constexpr size_t kPreroll = 300 * 16;
    static constexpr size_t kSilence = 500 * 16; // AFE already holds silence for 100ms.
    static constexpr size_t kLimit = 55 * 16000;
    size_t history_ = 0;
    size_t onset_ = 0;
    size_t silence_ = 0;
    size_t total_ = 0;
    bool capturing_ = false;
    bool finished_ = false;
};
