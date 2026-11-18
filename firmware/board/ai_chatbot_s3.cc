// SPDX-License-Identifier: MIT
// Derived from Xiaozhi's MIT-licensed bread-compact-wifi-lcd board.
#include "wifi_board.h"
#include "application.h"
#include "button.h"
#include "codecs/no_audio_codec.h"
#include "config.h"
#include "display/lcd_display.h"
#include "assets.h"
#include "display/lvgl_display/lvgl_theme.h"
#include "settings.h"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <vector>
#include <driver/i2s_std.h>
#include <driver/gpio.h>
#include <driver/spi_common.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_vendor.h>
#include <esp_system.h>
#include <esp_heap_caps.h>
#include <freertos/semphr.h>
#include <esp_attr.h>

// MAX98357A consumes standard stereo I2S; mirror mono speech into both slots.
class StereoI2SAudioCodec : public NoAudioCodecSimplex {
    SemaphoreHandle_t dma_sent_ = xSemaphoreCreateBinary();
    bool dma_clean_ = false; // Protected by data_if_mutex_; only true while TX is disabled.
    static bool IRAM_ATTR OnSent(i2s_chan_handle_t, i2s_event_data_t*, void* context) {
        auto* self = static_cast<StereoI2SAudioCodec*>(context);
        BaseType_t woken = pdFALSE;
        xSemaphoreGiveFromISR(self->dma_sent_, &woken);
        return woken == pdTRUE;
    }
public:
    StereoI2SAudioCodec() : NoAudioCodecSimplex(
        AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
        AUDIO_I2S_SPK_GPIO_BCLK, AUDIO_I2S_SPK_GPIO_LRCK, AUDIO_I2S_SPK_GPIO_DOUT,
        I2S_STD_SLOT_BOTH,
        AUDIO_I2S_MIC_GPIO_SCK, AUDIO_I2S_MIC_GPIO_WS, AUDIO_I2S_MIC_GPIO_DIN,
        I2S_STD_SLOT_LEFT) {
        i2s_std_slot_config_t stereo_slots = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO);
        stereo_slots.slot_mask = I2S_STD_SLOT_BOTH;
        ESP_ERROR_CHECK(i2s_channel_reconfig_std_slot(tx_handle_, &stereo_slots));
        output_channels_ = 2;
        assert(dma_sent_ != nullptr);
        i2s_event_callbacks_t callbacks = {};
        callbacks.on_sent = OnSent;
        ESP_ERROR_CHECK(i2s_channel_register_event_callback(tx_handle_, &callbacks, this));
    }

    void DrainOutput() override {
        if (!output_enabled()) return;
        xSemaphoreTake(dma_sent_, 0);
        // A full DMA ring after the last write, plus one descriptor for the FIFO.
        for (int i = 0; i <= AUDIO_CODEC_DMA_DESC_NUM; ++i) {
            if (xSemaphoreTake(dma_sent_, pdMS_TO_TICKS(100)) != pdTRUE) {
                ResetOutput(); // Missing TX interrupts must never open the mic over live audio.
                return;
            }
        }
    }

    void EnableOutput(bool enable) override {
        if (!enable) {
            ResetOutput(); // The power timer must clear DMA too, not only stop TX.
            return;
        }
        std::lock_guard<std::mutex> lock(data_if_mutex_);
        if (output_enabled_) return;
        ESP_ERROR_CHECK(i2s_channel_enable(tx_handle_));
        dma_clean_ = false;
        AudioCodec::EnableOutput(true);
    }

    void ResetOutput() override {
        std::lock_guard<std::mutex> lock(data_if_mutex_);
        if (dma_clean_ && !output_enabled_) return;
        const auto result = i2s_channel_disable(tx_handle_);
        // A failed writer can observe READY before the software enabled flag catches up.
        if (result != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(result);
        AudioCodec::EnableOutput(false);
        // disable() alone retains DMA memory, which would replay on the next enable().
        std::vector<int32_t> silence(AUDIO_CODEC_DMA_DESC_NUM * AUDIO_CODEC_DMA_FRAME_NUM * 2, 0);
        size_t loaded = 0;
        ESP_ERROR_CHECK(i2s_channel_preload_data(tx_handle_, silence.data(),
                                              silence.size() * sizeof(int32_t), &loaded));
        assert(loaded == silence.size() * sizeof(int32_t));
        dma_clean_ = true;
    }

protected:
    int Write(const int16_t* data, int samples) override {
        std::vector<int16_t> stereo(static_cast<size_t>(samples) * 2);
        for (int i = 0; i < samples; ++i) {
            stereo[2 * i] = data[i];
            stereo[2 * i + 1] = data[i];
        }
        return NoAudioCodec::Write(stereo.data(), static_cast<int>(stereo.size())) / 2;
    }
};

class MahiruDisplay : public SpiLcdDisplay {
    lv_obj_t* background_ = nullptr;
    lv_obj_t* dim_ = nullptr;
    std::unique_ptr<uint8_t, decltype(&heap_caps_free)> gif_bytes_{nullptr, heap_caps_free};
    std::unique_ptr<LvglGif> animation_;

    void UpdateDim() {
        DisplayLockGuard lock(this);
        if (!lock || dim_ == nullptr || content_ == nullptr) return;
        if (background_ == nullptr) return;
        lv_obj_set_style_bg_opa(dim_, lv_obj_get_child_cnt(content_) ? LV_OPA_60 : LV_OPA_TRANSP, 0);
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }

    void WarmTheme(Theme* selected) {
        auto theme = static_cast<LvglTheme*>(selected);
        theme->set_background_color(lv_color_hex(0x211824));
        theme->set_chat_background_color(lv_color_hex(0x211824));
        theme->set_text_color(lv_color_white());
        theme->set_system_text_color(lv_color_white());
        theme->set_user_bubble_color(lv_color_hex(0x624653));
        theme->set_assistant_bubble_color(lv_color_hex(0x25202A));
        theme->set_system_bubble_color(lv_color_hex(0x302530));
    }

    bool StartBackground() {
        if (background_) return true;
        void* data = nullptr;
        size_t size = 0;
        if (!Assets::GetInstance().GetAssetData("mahiru.gif", data, size) || size < 16) return false;
        // The assets partition can be unmapped during a later assets download.
        auto bytes = std::unique_ptr<uint8_t, decltype(&heap_caps_free)>(
            static_cast<uint8_t*>(heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
            heap_caps_free);
        if (!bytes) return false;
        std::memcpy(bytes.get(), data, size);
        DisplayLockGuard lock(this);
        if (!lock) return false;
        lv_img_dsc_t descriptor = {};
        descriptor.data = bytes.get();
        descriptor.data_size = size;
        auto gif = std::make_unique<LvglGif>(&descriptor);
        if (!gif->IsLoaded() || gif->width() != width_ || gif->height() != height_) return false;
        background_ = lv_image_create(lv_screen_active());
        lv_image_set_src(background_, gif->image_dsc());
        lv_obj_move_to_index(background_, 0);
        dim_ = lv_obj_create(lv_screen_active());
        lv_obj_set_size(dim_, width_, height_);
        lv_obj_align(dim_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_radius(dim_, 0, 0);
        lv_obj_set_style_border_width(dim_, 0, 0);
        lv_obj_set_style_bg_color(dim_, lv_color_black(), 0);
        lv_obj_set_style_bg_opa(dim_, lv_obj_get_child_cnt(content_) ? LV_OPA_60 : LV_OPA_TRANSP, 0);
        lv_obj_remove_flag(dim_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_move_to_index(dim_, 1);
        lv_obj_set_style_bg_opa(container_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_image_src(container_, nullptr, 0);
        lv_obj_set_style_bg_opa(content_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_pad_all(content_, 6, 0);
        lv_obj_set_style_pad_row(content_, 4, 0);
        lv_obj_set_style_text_color(content_, lv_color_white(), 0);
        lv_obj_set_style_bg_color(top_bar_, lv_color_hex(0x211824), 0);
        lv_obj_set_style_bg_opa(top_bar_, LV_OPA_60, 0);
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
        gif->SetFrameCallback([this]() {
            if (background_) lv_image_set_src(background_, animation_->image_dsc());
        });
        gif_bytes_ = std::move(bytes);
        animation_ = std::move(gif);
        animation_->Start();
        return true;
    }

public:
    using SpiLcdDisplay::SpiLcdDisplay;

    void SetupUI() override {
        WarmTheme(current_theme_);
        SpiLcdDisplay::SetupUI();
    }
    bool SetEmotion(const char* emotion) override {
        if (StartBackground()) return true;  // The character replaces the old emoji for every state.
        return SpiLcdDisplay::SetEmotion(emotion);
    }
    void SetChatMessage(const char* role, const char* text) override {
        SpiLcdDisplay::SetChatMessage(role, text);
        UpdateDim();
    }
    void RemoveChatMessage(const char* role, const char* text) override {
        SpiLcdDisplay::RemoveChatMessage(role, text);
        UpdateDim();
    }
    void ClearChatMessages() override {
        SpiLcdDisplay::ClearChatMessages();
        UpdateDim();
    }
    void SetTheme(Theme* theme) override {
        WarmTheme(theme);
        SpiLcdDisplay::SetTheme(theme);
        if (!background_) return;
        DisplayLockGuard lock(this);
        if (!lock) return;
        lv_obj_set_style_bg_image_src(container_, nullptr, 0);
        lv_obj_set_style_bg_opa(container_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_opa(content_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_color(top_bar_, lv_color_hex(0x211824), 0);
        lv_obj_set_style_bg_opa(top_bar_, LV_OPA_60, 0);
    }
};

class AiChatbotS3 : public WifiBoard {
    Button boot_{BOOT_BUTTON_GPIO};
    Button volume_up_{VOLUME_UP_BUTTON_GPIO};
    Button volume_down_{VOLUME_DOWN_BUTTON_GPIO};
    MahiruDisplay* display_ = nullptr;

    void InitializeDisplay() {
        spi_bus_config_t bus = {};
        bus.mosi_io_num = DISPLAY_MOSI_PIN;
        bus.miso_io_num = GPIO_NUM_NC;
        bus.sclk_io_num = DISPLAY_CLK_PIN;
        bus.quadwp_io_num = GPIO_NUM_NC;
        bus.quadhd_io_num = GPIO_NUM_NC;
        bus.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &bus, SPI_DMA_CH_AUTO));

        esp_lcd_panel_io_spi_config_t io = {};
        io.cs_gpio_num = DISPLAY_CS_PIN;
        io.dc_gpio_num = DISPLAY_DC_PIN;
        io.spi_mode = 0;
        io.pclk_hz = 40 * 1000 * 1000;
        io.trans_queue_depth = 10;
        io.lcd_cmd_bits = 8;
        io.lcd_param_bits = 8;
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io, &panel_io));

        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = DISPLAY_RST_PIN;
        panel_config.rgb_ele_order = DISPLAY_RGB_ORDER;
        panel_config.bits_per_pixel = 16;
        esp_lcd_panel_handle_t panel = nullptr;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(panel_io, &panel_config, &panel));
        ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
        ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
        ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel, DISPLAY_INVERT_COLOR));
        ESP_ERROR_CHECK(esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY));
        ESP_ERROR_CHECK(esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y));
        display_ = new MahiruDisplay(panel_io, panel, DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                   DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y,
                                   DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
    }

    void ChangeVolume(int delta) {
        Application::GetInstance().Schedule([this, delta]() {
            auto codec = GetAudioCodec();
            const int volume = std::clamp(codec->output_volume() + delta, 0, 100);
            codec->SetOutputVolume(volume);
            display_->ShowNotification("Am luong: " + std::to_string(volume));
        });
    }

public:
    AiChatbotS3() {
        InitializeDisplay();
        boot_.OnClick([]() {
            // Evaluate state on the main loop, not the button driver's task.
            Application::GetInstance().Schedule([]() {
                auto& app = Application::GetInstance();
                if (app.GetDeviceState() == kDeviceStateAudioTesting) app.StopListening();
                else app.ToggleContinuousConversation();
            });
        });
        // Long press is deliberately separate from recording; recovery without touch.
        boot_.OnLongPress([this]() {
            if (IsInWifiConfigMode()) return;
            if (Application::GetInstance().GetDeviceState() == kDeviceStateActivating) {
                // The OTA retry task blocks activation; request config on next boot instead.
                {
                    Settings settings("wifi", true);
                    settings.SetBool("force_config", true);
                }
                // BUTTON_LONG_PRESS_START fires while GPIO0 is still low. Rebooting
                // before release would select the ROM download boot mode.
                if (xTaskCreate([](void*) {
                    while (gpio_get_level(BOOT_BUTTON_GPIO) == 0) {
                        vTaskDelay(pdMS_TO_TICKS(20));
                    }
                    vTaskDelay(pdMS_TO_TICKS(100));
                    esp_restart();
                }, "wifi_cfg_reboot", 2048, nullptr, 2, nullptr) != pdPASS) {
                    ESP_LOGE("AiChatbotS3", "Could not schedule WiFi config reboot");
                }
                return;
            }
            EnterWifiConfigMode();
        });
        volume_up_.OnClick([this]() { ChangeVolume(5); });
        volume_down_.OnClick([this]() { ChangeVolume(-5); });
        GetBacklight()->RestoreBrightness();
    }

    AudioCodec* GetAudioCodec() override {
        // MAX98357A stereo I2S: DIN=7, BCLK=15, LRC=16. INMP441 selects LEFT.
        static StereoI2SAudioCodec codec;
        return &codec;
    }
    Display* GetDisplay() override { return display_; }
    Backlight* GetBacklight() override {
        static PwmBacklight backlight(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT);
        return &backlight;
    }
};

DECLARE_BOARD(AiChatbotS3);
