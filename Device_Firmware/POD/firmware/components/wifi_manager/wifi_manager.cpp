#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "lwip/ip4_addr.h"
#include "wifi_manager.h"

static const char *TAG = "wifi_mgr";

#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1

static EventGroupHandle_t  s_wifi_eg    = NULL;
static esp_netif_t        *s_netif      = NULL;
static wifi_state_t        s_state      = WIFI_STATE_DISCONNECTED;
static wifi_state_cb_t     s_cb         = NULL;
static void               *s_cb_ctx    = NULL;
static char                s_ip[20]    = {};
static int                 s_retry     = 0;
#define MAX_RETRY 8

static void set_state(wifi_state_t st)
{
    s_state = st;
    if (s_cb) s_cb(st, s_cb_ctx);
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *event_data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "STA started — connecting");
        esp_wifi_connect();
        set_state(WIFI_STATE_CONNECTING);
    }
    else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *ev = (wifi_event_sta_disconnected_t *)event_data;
        ESP_LOGW(TAG, "Disconnected (reason %d)", ev->reason);
        if (s_retry < MAX_RETRY) {
            vTaskDelay(pdMS_TO_TICKS(1000 << s_retry));
            esp_wifi_connect();
            s_retry++;
            set_state(WIFI_STATE_CONNECTING);
        } else {
            xEventGroupSetBits(s_wifi_eg, WIFI_FAIL_BIT);
            set_state(WIFI_STATE_FAILED);
        }
    }
    else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)event_data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&ev->ip_info.ip));
        ESP_LOGI(TAG, "Got IP: %s", s_ip);
        s_retry = 0;
        xEventGroupSetBits(s_wifi_eg, WIFI_CONNECTED_BIT);
        set_state(WIFI_STATE_CONNECTED);
    }
}

esp_err_t wifi_manager_init(void)
{
    s_wifi_eg = xEventGroupCreate();
    if (!s_wifi_eg) return ESP_ERR_NO_MEM;

    esp_netif_init();
    esp_event_loop_create_default();
    s_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&cfg), TAG, "WiFi init failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "WiFi mode failed");

    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,  wifi_event_handler, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT,   IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL);

    ESP_LOGI(TAG, "WiFi manager initialised");
    return ESP_OK;
}

esp_err_t wifi_manager_connect(const char *ssid, const char *pass)
{
    wifi_config_t wc = {};
    strncpy((char *)wc.sta.ssid,     ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, pass, sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = strlen(pass) > 0 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable    = true;

    s_retry = 0;
    xEventGroupClearBits(s_wifi_eg, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);

    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wc), TAG, "WiFi config failed");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "WiFi start failed");

    ESP_LOGI(TAG, "Connecting to '%s'…", ssid);
    return ESP_OK;
}

esp_err_t wifi_manager_disconnect(void)
{
    esp_wifi_disconnect();
    esp_wifi_stop();
    set_state(WIFI_STATE_DISCONNECTED);
    return ESP_OK;
}

esp_err_t wifi_manager_wait_connected(uint32_t timeout_ms)
{
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_eg,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE,
        pdMS_TO_TICKS(timeout_ms));

    if (bits & WIFI_CONNECTED_BIT) return ESP_OK;
    if (bits & WIFI_FAIL_BIT)      return ESP_FAIL;
    return ESP_ERR_TIMEOUT;
}

wifi_state_t wifi_manager_state(void)   { return s_state; }
const char  *wifi_manager_ip_str(void)  { return s_ip; }

void wifi_manager_set_callback(wifi_state_cb_t cb, void *ctx)
{
    s_cb     = cb;
    s_cb_ctx = ctx;
}

int wifi_manager_rssi(void)
{
    wifi_ap_record_t ap_info;
    if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) return ap_info.rssi;
    return 0;
}
