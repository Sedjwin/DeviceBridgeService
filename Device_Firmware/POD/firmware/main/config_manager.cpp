#include <string.h>
#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "pod_config.h"
#include "config_manager.h"

static const char *TAG = "config";
static pod_config_t s_cfg = {};
static bool s_loaded = false;

// ── NVS helpers ──────────────────────────────────────────────────────────────

static esp_err_t nvs_open_rw(nvs_handle_t *h)
{
    return nvs_open(NVS_NAMESPACE, NVS_READWRITE, h);
}

static void nvs_read_str(nvs_handle_t h, const char *key, char *dst, size_t maxlen, const char *fallback)
{
    size_t len = maxlen;
    esp_err_t err = nvs_get_str(h, key, dst, &len);
    if (err != ESP_OK || len == 0) {
        strncpy(dst, fallback, maxlen - 1);
        dst[maxlen - 1] = '\0';
    }
}

static void nvs_read_u16(nvs_handle_t h, const char *key, uint16_t *dst, uint16_t fallback)
{
    esp_err_t err = nvs_get_u16(h, key, dst);
    if (err != ESP_OK) {
        *dst = fallback;
    }
}

// ── Public API ───────────────────────────────────────────────────────────────

esp_err_t config_load(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &h);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        // Namespace doesn't exist yet — use all defaults
        ESP_LOGI(TAG, "No config namespace found — using defaults");
        strncpy(s_cfg.wifi_ssid, "", sizeof(s_cfg.wifi_ssid));
        strncpy(s_cfg.wifi_pass, "", sizeof(s_cfg.wifi_pass));
        strncpy(s_cfg.dbs_host, DEFAULT_DBS_HOST, sizeof(s_cfg.dbs_host));
        s_cfg.dbs_port = DEFAULT_DBS_PORT;
        strncpy(s_cfg.device_slug, DEFAULT_DEVICE_SLUG, sizeof(s_cfg.device_slug));
        strncpy(s_cfg.device_name, DEFAULT_DEVICE_NAME, sizeof(s_cfg.device_name));
        strncpy(s_cfg.device_key, "", sizeof(s_cfg.device_key));
        strncpy(s_cfg.agent_voice, DEFAULT_AGENT_VOICE, sizeof(s_cfg.agent_voice));
        s_cfg.provisioned = false;
        s_loaded = true;
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }

    nvs_read_str(h, NVS_KEY_WIFI_SSID, s_cfg.wifi_ssid, sizeof(s_cfg.wifi_ssid), "");
    nvs_read_str(h, NVS_KEY_WIFI_PASS, s_cfg.wifi_pass, sizeof(s_cfg.wifi_pass), "");
    nvs_read_str(h, NVS_KEY_DBS_HOST,  s_cfg.dbs_host,  sizeof(s_cfg.dbs_host),  DEFAULT_DBS_HOST);
    nvs_read_u16(h, NVS_KEY_DBS_PORT,  &s_cfg.dbs_port, DEFAULT_DBS_PORT);
    nvs_read_str(h, NVS_KEY_DEVICE_SLUG, s_cfg.device_slug, sizeof(s_cfg.device_slug), DEFAULT_DEVICE_SLUG);
    nvs_read_str(h, NVS_KEY_DEVICE_NAME, s_cfg.device_name, sizeof(s_cfg.device_name), DEFAULT_DEVICE_NAME);
    nvs_read_str(h, NVS_KEY_DEVICE_KEY,  s_cfg.device_key,  sizeof(s_cfg.device_key),  "");
    nvs_read_str(h, NVS_KEY_AGENT_VOICE, s_cfg.agent_voice, sizeof(s_cfg.agent_voice),  DEFAULT_AGENT_VOICE);

    s_cfg.provisioned = (s_cfg.wifi_ssid[0] != '\0');

    nvs_close(h);
    s_loaded = true;

    ESP_LOGI(TAG, "Config loaded: ssid='%s' dbs=%s:%d slug='%s'",
             s_cfg.wifi_ssid, s_cfg.dbs_host, s_cfg.dbs_port, s_cfg.device_slug);
    return ESP_OK;
}

const pod_config_t *config_get(void)
{
    return &s_cfg;
}

esp_err_t config_set_wifi(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    nvs_set_str(h, NVS_KEY_WIFI_SSID, ssid);
    nvs_set_str(h, NVS_KEY_WIFI_PASS, pass);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        strncpy(s_cfg.wifi_ssid, ssid, sizeof(s_cfg.wifi_ssid) - 1);
        strncpy(s_cfg.wifi_pass, pass, sizeof(s_cfg.wifi_pass) - 1);
        s_cfg.provisioned = (ssid[0] != '\0');
    }
    return err;
}

esp_err_t config_set_dbs(const char *host, uint16_t port)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    nvs_set_str(h, NVS_KEY_DBS_HOST, host);
    nvs_set_u16(h, NVS_KEY_DBS_PORT, port);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        strncpy(s_cfg.dbs_host, host, sizeof(s_cfg.dbs_host) - 1);
        s_cfg.dbs_port = port;
    }
    return err;
}

esp_err_t config_set_device_slug(const char *slug)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    nvs_set_str(h, NVS_KEY_DEVICE_SLUG, slug);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        strncpy(s_cfg.device_slug, slug, sizeof(s_cfg.device_slug) - 1);
    }
    return err;
}

esp_err_t config_set_device_name(const char *name)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    nvs_set_str(h, NVS_KEY_DEVICE_NAME, name);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        strncpy(s_cfg.device_name, name, sizeof(s_cfg.device_name) - 1);
    }
    return err;
}

esp_err_t config_set_device_key(const char *key)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    nvs_set_str(h, NVS_KEY_DEVICE_KEY, key);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        strncpy(s_cfg.device_key, key, sizeof(s_cfg.device_key) - 1);
    }
    return err;
}

esp_err_t config_set_agent_voice(const char *voice)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    nvs_set_str(h, NVS_KEY_AGENT_VOICE, voice);
    esp_err_t err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        strncpy(s_cfg.agent_voice, voice, sizeof(s_cfg.agent_voice) - 1);
    }
    return err;
}

esp_err_t config_erase_all(void)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_rw(&h), TAG, "nvs open failed");
    esp_err_t err = nvs_erase_all(h);
    nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        memset(&s_cfg, 0, sizeof(s_cfg));
        strncpy(s_cfg.dbs_host, DEFAULT_DBS_HOST, sizeof(s_cfg.dbs_host));
        s_cfg.dbs_port = DEFAULT_DBS_PORT;
        strncpy(s_cfg.device_slug, DEFAULT_DEVICE_SLUG, sizeof(s_cfg.device_slug));
        strncpy(s_cfg.device_name, DEFAULT_DEVICE_NAME, sizeof(s_cfg.device_name));
        strncpy(s_cfg.agent_voice, DEFAULT_AGENT_VOICE, sizeof(s_cfg.agent_voice));
    }
    return err;
}
