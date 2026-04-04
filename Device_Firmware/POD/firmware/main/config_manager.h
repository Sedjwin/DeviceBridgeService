#pragma once
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * config_manager — NVS-backed configuration for POD.
 *
 * All string buffers are null-terminated. Call config_load() on startup,
 * then read fields directly from the pod_config_t struct via config_get().
 * To persist new values call the config_set_* functions (they write to NVS
 * and update the in-memory struct atomically).
 */

#define CFG_STR_LEN 128

typedef struct {
    char wifi_ssid[CFG_STR_LEN];
    char wifi_pass[CFG_STR_LEN];
    char dbs_host[CFG_STR_LEN];
    uint16_t dbs_port;
    char device_slug[64];
    char device_name[64];
    char device_key[CFG_STR_LEN];   // optional service key header
    char agent_voice[32];
    bool provisioned;                // true once wifi_ssid has been set
} pod_config_t;

/**
 * Load configuration from NVS.  Must be called after nvs_flash_init().
 * Fills in defaults for any missing keys.
 * Returns ESP_OK or an NVS error code.
 */
esp_err_t config_load(void);

/** Return a const pointer to the live config struct. */
const pod_config_t *config_get(void);

/** Persist and apply individual settings. */
esp_err_t config_set_wifi(const char *ssid, const char *pass);
esp_err_t config_set_dbs(const char *host, uint16_t port);
esp_err_t config_set_device_slug(const char *slug);
esp_err_t config_set_device_name(const char *name);
esp_err_t config_set_device_key(const char *key);
esp_err_t config_set_agent_voice(const char *voice);

/** Erase all config (triggers provisioning on next boot). */
esp_err_t config_erase_all(void);

#ifdef __cplusplus
}
#endif
