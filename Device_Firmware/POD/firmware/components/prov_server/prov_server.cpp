#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "prov_server.h"

// config_manager included via extern C linkage
extern "C" {
#include "config_manager.h"
}

static const char *TAG = "prov";

static httpd_handle_t     s_server    = NULL;
static esp_netif_t       *s_ap_netif  = NULL;
static volatile bool      s_done      = false;
static prov_complete_cb_t s_cb        = NULL;
static void              *s_cb_ctx   = NULL;

// ── HTML portal ───────────────────────────────────────────────────────────────

static const char PROV_HTML[] =
"<!DOCTYPE html><html><head>"
"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
"<title>POD Setup</title>"
"<style>"
"body{font-family:sans-serif;background:#0a0a1a;color:#e0e0ff;max-width:420px;margin:40px auto;padding:20px}"
"h1{color:#a0c0ff;text-align:center;letter-spacing:2px}h2{color:#7090c0;font-size:1em;margin-top:24px}"
"input{width:100%;box-sizing:border-box;padding:10px;margin:6px 0 14px;background:#1a1a2e;border:1px solid #3a4a7a;"
"border-radius:6px;color:#e0e0ff;font-size:1em}"
"input:focus{outline:none;border-color:#a0c0ff}"
"button{width:100%;padding:12px;background:#2040a0;color:#fff;border:none;border-radius:6px;"
"font-size:1.1em;cursor:pointer;margin-top:10px;letter-spacing:1px}"
"button:hover{background:#3060d0}.note{font-size:0.8em;color:#607090;text-align:center;margin-top:16px}"
"</style></head><body>"
"<h1>&#x25cb; POD SETUP</h1>"
"<form action='/save' method='POST'>"
"<h2>WIFI</h2>"
"<input name='ssid' placeholder='Network name (SSID)' required>"
"<input name='pass' type='password' placeholder='Password'>"
"<h2>DEVICE BRIDGE SERVICE</h2>"
"<input name='dbs_host' placeholder='DBS host (e.g. 192.168.1.100)' required>"
"<input name='dbs_port' placeholder='DBS port (default 8010)'>"
"<h2>DEVICE</h2>"
"<input name='slug' placeholder='Device slug (e.g. pod-01)' required>"
"<input name='name' placeholder='Device name (e.g. POD)'>"
"<button type='submit'>SAVE &amp; REBOOT</button>"
"</form>"
"<p class='note'>Connect to POD-SETUP &bull; pw: pod12345 &bull; then visit 192.168.4.1</p>"
"</body></html>";

static const char PROV_SAVED_HTML[] =
"<!DOCTYPE html><html><head><meta charset='utf-8'>"
"<title>POD Saved</title>"
"<style>body{font-family:sans-serif;background:#0a0a1a;color:#a0ffa0;text-align:center;padding-top:80px}"
"h1{font-size:2em;letter-spacing:3px}</style></head><body>"
"<h1>&#x2713; SAVED</h1><p>POD is rebooting&hellip;</p>"
"</body></html>";

// ── HTTP handlers ──────────────────────────────────────────────────────────────

static esp_err_t root_get(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, PROV_HTML, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

// Minimal URL decoder (in-place)
static void url_decode(char *str)
{
    char *src = str, *dst = str;
    while (*src) {
        if (*src == '%' && src[1] && src[2]) {
            char hex[3] = {src[1], src[2], 0};
            *dst++ = (char)strtol(hex, NULL, 16);
            src += 3;
        } else if (*src == '+') {
            *dst++ = ' ';
            src++;
        } else {
            *dst++ = *src++;
        }
    }
    *dst = '\0';
}

static void extract_field(const char *body, const char *key, char *out, size_t maxlen)
{
    char search[64];
    snprintf(search, sizeof(search), "%s=", key);
    const char *p = strstr(body, search);
    if (!p) { out[0] = '\0'; return; }
    p += strlen(search);
    const char *end = strchr(p, '&');
    size_t len = end ? (size_t)(end - p) : strlen(p);
    if (len >= maxlen) len = maxlen - 1;
    strncpy(out, p, len);
    out[len] = '\0';
    url_decode(out);
}

static esp_err_t save_post(httpd_req_t *req)
{
    char body[512] = {};
    int  received  = httpd_req_recv(req, body, sizeof(body) - 1);
    if (received <= 0) { httpd_resp_send_500(req); return ESP_FAIL; }
    body[received] = '\0';

    char ssid[128], pass[128], dbs_host[128], dbs_port_s[8], slug[64], name[64];
    extract_field(body, "ssid",     ssid,     sizeof(ssid));
    extract_field(body, "pass",     pass,     sizeof(pass));
    extract_field(body, "dbs_host", dbs_host, sizeof(dbs_host));
    extract_field(body, "dbs_port", dbs_port_s, sizeof(dbs_port_s));
    extract_field(body, "slug",     slug,     sizeof(slug));
    extract_field(body, "name",     name,     sizeof(name));

    uint16_t dbs_port = (uint16_t)(dbs_port_s[0] ? atoi(dbs_port_s) : 8010);
    if (dbs_port == 0) dbs_port = 8010;

    if (ssid[0] && dbs_host[0] && slug[0]) {
        config_set_wifi(ssid, pass);
        config_set_dbs(dbs_host, dbs_port);
        config_set_device_slug(slug);
        if (name[0]) config_set_device_name(name);

        httpd_resp_set_type(req, "text/html");
        httpd_resp_send(req, PROV_SAVED_HTML, HTTPD_RESP_USE_STRLEN);

        s_done = true;
        if (s_cb) s_cb(s_cb_ctx);

        // Reboot after response is flushed
        vTaskDelay(pdMS_TO_TICKS(1500));
        esp_restart();
    } else {
        httpd_resp_set_status(req, "400 Bad Request");
        httpd_resp_sendstr(req, "Missing required fields.");
    }
    return ESP_OK;
}

// Captive portal redirect — send all unknown paths to root
static esp_err_t captive_redirect(httpd_req_t *req)
{
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

// ── Public API ────────────────────────────────────────────────────────────────

esp_err_t prov_server_start(const prov_server_config_t *cfg)
{
    if (cfg) { s_cb = cfg->on_complete; s_cb_ctx = cfg->ctx; }

    // Start SoftAP
    s_ap_netif = esp_netif_create_default_wifi_ap();

    wifi_config_t ap_cfg = {};
    strncpy((char *)ap_cfg.ap.ssid,     "POD-SETUP", sizeof(ap_cfg.ap.ssid));
    strncpy((char *)ap_cfg.ap.password, "pod12345",  sizeof(ap_cfg.ap.password));
    ap_cfg.ap.ssid_len       = strlen("POD-SETUP");
    ap_cfg.ap.channel        = 6;
    ap_cfg.ap.authmode       = WIFI_AUTH_WPA2_PSK;
    ap_cfg.ap.max_connection = 4;

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_AP), TAG, "AP mode set failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg), TAG, "AP config failed");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "WiFi AP start failed");
    ESP_LOGI(TAG, "AP started: SSID='POD-SETUP' pw='pod12345' IP=192.168.4.1");

    // Start HTTP server
    httpd_config_t hcfg = HTTPD_DEFAULT_CONFIG();
    hcfg.lru_purge_enable = true;
    hcfg.uri_match_fn     = httpd_uri_match_wildcard;

    ESP_RETURN_ON_ERROR(httpd_start(&s_server, &hcfg), TAG, "HTTP server start failed");

    httpd_uri_t root = { .uri = "/",     .method = HTTP_GET,  .handler = root_get };
    httpd_uri_t save = { .uri = "/save", .method = HTTP_POST, .handler = save_post };
    httpd_uri_t wild = { .uri = "/*",    .method = HTTP_GET,  .handler = captive_redirect };

    httpd_register_uri_handler(s_server, &root);
    httpd_register_uri_handler(s_server, &save);
    httpd_register_uri_handler(s_server, &wild);

    ESP_LOGI(TAG, "Provisioning server ready at http://192.168.4.1");
    return ESP_OK;
}

esp_err_t prov_server_stop(void)
{
    if (s_server) { httpd_stop(s_server); s_server = NULL; }
    esp_wifi_stop();
    if (s_ap_netif) { esp_netif_destroy(s_ap_netif); s_ap_netif = NULL; }
    return ESP_OK;
}

bool prov_server_is_done(void)
{
    return s_done;
}
