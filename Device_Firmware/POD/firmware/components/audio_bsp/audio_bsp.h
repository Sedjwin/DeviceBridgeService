#pragma once
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * audio_bsp — ES8311 (speaker) + ES7210 (microphone) codec driver for POD.
 *
 * Provides:
 *   audio_bsp_init()              — initialise I2S + codecs
 *   audio_bsp_mic_read()          — read PCM from microphone
 *   audio_bsp_speaker_write()     — write PCM to speaker
 *   audio_bsp_speaker_mute()      — mute/unmute amplifier
 *   audio_bsp_set_volume()        — 0–100
 */

esp_err_t audio_bsp_init(void);
esp_err_t audio_bsp_deinit(void);

/**
 * Read raw PCM samples from mic into buf (16-bit signed, mono, 16kHz).
 * buf_bytes must be a multiple of 4 (I2S DMA constraint).
 * Returns ESP_OK, sets *out_bytes to number of bytes actually read.
 */
esp_err_t audio_bsp_mic_read(int16_t *buf, size_t buf_bytes, size_t *out_bytes, uint32_t timeout_ms);

/**
 * Write PCM samples to speaker (16-bit signed, mono, sample_rate Hz).
 * Blocks until all bytes are written to the I2S TX FIFO.
 * If sample_rate differs from the current codec rate, the codec is reconfigured.
 */
esp_err_t audio_bsp_speaker_write(const int16_t *buf, size_t buf_bytes, uint32_t sample_rate);

/** Write a complete WAV file to speaker (parses header, extracts PCM). */
esp_err_t audio_bsp_play_wav(const uint8_t *wav_data, size_t wav_len);

/** Mute or unmute the PA. */
esp_err_t audio_bsp_speaker_mute(bool mute);

/** Set playback volume 0–100. */
esp_err_t audio_bsp_set_volume(uint8_t vol);

/** Set mic gain 0–40 dB. */
esp_err_t audio_bsp_set_mic_gain(uint8_t db);

#ifdef __cplusplus
}
#endif
