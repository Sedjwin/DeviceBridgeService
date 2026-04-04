#pragma once
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * dbs_client — HTTP + WebSocket client for DeviceBridgeService.
 *
 * HTTP calls:
 *   dbs_register_device()   — POST /api/devices  (idempotent)
 *   dbs_post_wake_event()   — POST /api/devices/{slug}/events
 *
 * WebSocket session:
 *   dbs_ws_connect()        — open WS to /api/embodiment/sessions/{id}/stream
 *   dbs_ws_send_chunk()     — send audio_chunk message
 *   dbs_ws_send_audio_end() — send audio_end message
 *   dbs_ws_send_ping()      — send ping
 *   dbs_ws_disconnect()     — close WS
 *
 * Incoming message callback:
 *   dbs_msg_cb_t is invoked from the WS event loop task for every
 *   message received from DBS.  Keep it short — post to a queue.
 */

// ── Incoming message types ────────────────────────────────────────────────────

typedef enum {
    DBS_MSG_AUDIO_CHUNK,     // TTS audio data (raw WAV bytes, base64-decoded)
    DBS_MSG_AUDIO_END,       // TTS finished
    DBS_MSG_EXPRESSION,      // avatar expression string
    DBS_MSG_DISPLAY_TEXT,    // overlay text
    DBS_MSG_DISPLAY_IMAGE,   // image bytes (base64-decoded)
    DBS_MSG_SETTINGS_ACK,    // settings acknowledged
    DBS_MSG_PING,            // keepalive
    DBS_MSG_WS_CONNECTED,    // WS connection established
    DBS_MSG_WS_DISCONNECTED, // WS disconnected
    DBS_MSG_WS_ERROR,        // WS error
} dbs_msg_type_t;

typedef struct {
    dbs_msg_type_t type;

    // DBS_MSG_AUDIO_CHUNK
    uint8_t *audio_data;   // heap-allocated; caller must free()
    size_t   audio_len;

    // DBS_MSG_EXPRESSION
    char expression[32];

    // DBS_MSG_DISPLAY_TEXT
    char text[512];

    // DBS_MSG_DISPLAY_IMAGE
    uint8_t *image_data;   // heap-allocated; caller must free()
    size_t   image_len;
} dbs_msg_t;

typedef void (*dbs_msg_cb_t)(const dbs_msg_t *msg, void *ctx);

// ── Configuration ─────────────────────────────────────────────────────────────

typedef struct {
    char host[128];
    uint16_t port;
    char device_slug[64];
    char device_name[64];
    char device_key[128];    // optional — sent as X-Device-Key header
    dbs_msg_cb_t on_message;
    void *cb_ctx;
} dbs_client_config_t;

// ── Lifecycle ─────────────────────────────────────────────────────────────────

esp_err_t dbs_client_init(const dbs_client_config_t *cfg);
esp_err_t dbs_client_deinit(void);

// ── HTTP calls ────────────────────────────────────────────────────────────────

/**
 * Register this device with DBS (idempotent — 409 = already registered, OK).
 * Sends the full embodiment manifest.
 */
esp_err_t dbs_register_device(void);

/**
 * POST a wake_word or button_press event.
 * On success, *out_session_id is populated with the resulting session ID
 * (or zeroed if DBS returned none — e.g. no default_agent_id configured).
 */
esp_err_t dbs_post_wake_event(const char *event_type, char *out_session_id, size_t id_buflen);

// ── WebSocket ─────────────────────────────────────────────────────────────────

/** Open WebSocket to /api/embodiment/sessions/{session_id}/stream. */
esp_err_t dbs_ws_connect(const char *session_id);

/** Send a 20ms PCM audio chunk (raw 16-bit mono samples). */
esp_err_t dbs_ws_send_chunk(const int16_t *pcm, size_t sample_count, uint32_t sample_rate);

/** Signal end of utterance. */
esp_err_t dbs_ws_send_audio_end(void);

/** Send keepalive ping. */
esp_err_t dbs_ws_send_ping(void);

/** Close the WebSocket session. */
esp_err_t dbs_ws_disconnect(void);

/** True if the WebSocket is currently connected. */
bool dbs_ws_is_connected(void);

#ifdef __cplusplus
}
#endif
