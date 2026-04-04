#pragma once
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * afe_pipeline — ESP-SR Audio Front End + WakeNet ("Hi ESP") pipeline.
 *
 * The pipeline runs a dedicated FreeRTOS task on core 0 that:
 *   1. Reads 30 ms frames from the microphone
 *   2. Passes them through the AFE (noise suppression + VAD)
 *   3. Feeds WakeNet for wake-word detection
 *   4. After wake, routes audio to a ring buffer for the session task
 *
 * Callbacks are invoked from the AFE task — keep them short (post to a queue).
 */

typedef enum {
    AFE_EVENT_WAKE_DETECTED,   // "Hi ESP" detected — start session
    AFE_EVENT_VAD_START,       // voice activity started
    AFE_EVENT_VAD_END,         // silence detected — utterance complete
} afe_event_type_t;

typedef void (*afe_event_cb_t)(afe_event_type_t event, void *ctx);

typedef struct {
    afe_event_cb_t on_event;
    void *ctx;
} afe_pipeline_config_t;

/** Initialise and start the AFE pipeline task. */
esp_err_t afe_pipeline_init(const afe_pipeline_config_t *cfg);

/** Stop the pipeline and free resources. */
esp_err_t afe_pipeline_deinit(void);

/**
 * Read post-AFE processed audio (16kHz, 16-bit, mono).
 * Returns number of bytes written into buf (0 if nothing ready).
 * Only valid while the pipeline is in ACTIVE (post-wake) mode.
 */
size_t afe_pipeline_read_audio(int16_t *buf, size_t buf_bytes);

/**
 * Switch the pipeline between WAKE_LISTENING mode (WakeNet active)
 * and ACTIVE mode (audio routed to ring buffer, WakeNet paused).
 */
void afe_pipeline_set_active(bool active);

/** True if wake word has been detected and pipeline is in ACTIVE mode. */
bool afe_pipeline_is_active(void);

#ifdef __cplusplus
}
#endif
