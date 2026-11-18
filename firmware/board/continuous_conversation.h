#pragma once
#include <cstdint>

// Main-loop owned session/utterance state; audio callbacks carry a separate generation.
struct ContinuousConversation {
    enum class Action { Open, Cancel, Wait };
    uint64_t epoch = 0, utterance = 0, session_utterance = 0;
    uint64_t abort_epoch = 0, abort_utterance = 0;
    bool active = false, capturing = false, processing = false, draining = false;
    bool aborting = false, reopen = false;

    Action Boot() {
        if (aborting) { reopen = !reopen; return Action::Wait; }
        if (active) { Cancel(); return Action::Cancel; }
        Open();
        return Action::Open;
    }
    void Open() {
        ++epoch;
        session_utterance = 0;
        active = true;
        capturing = processing = draining = reopen = false;
    }
    void Cancel() {
        abort_epoch = epoch;
        abort_utterance = session_utterance;
        aborting = true;
        active = capturing = processing = draining = reopen = false;
    }
    bool StartUtterance() {
        if (!active || aborting || capturing || processing || draining) return false;
        session_utterance = ++utterance;
        capturing = true;
        return true;
    }
    void EndUtterance() { capturing = false; processing = active; }
    bool Matches(uint64_t e, uint64_t u) const {
        return active && !aborting && session_utterance != 0 && e == epoch && u == session_utterance;
    }
    bool Complete(uint64_t e, uint64_t u) {
        if (!Matches(e, u) || !processing) return false;
        processing = false;
        draining = true;
        return true;
    }
    void PlaybackDrained() { if (active) draining = false; }
    bool Acknowledge(uint64_t e, uint64_t u) {
        if (!aborting || e != abort_epoch || u != abort_utterance) return false;
        aborting = false;
        if (reopen) Open();
        return true;
    }
    void Disconnect() { active = capturing = processing = draining = aborting = reopen = false; }
};
