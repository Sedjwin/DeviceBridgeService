#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lvgl.h"
#include "display_bsp.h"
#include "avatar.h"

static const char *TAG = "avatar";

// ── Colour palette ─────────────────────────────────────────────────────────────
#define COL_BG          lv_color_hex(0x04040f)   // near-black deep blue
#define COL_FACE        lv_color_hex(0x0d0d2a)   // face circle
#define COL_EYE_WHITE   lv_color_hex(0xd8e8ff)
#define COL_PUPIL       lv_color_hex(0x080814)
#define COL_IRIS_IDLE   lv_color_hex(0x3070e0)
#define COL_IRIS_LISTEN lv_color_hex(0x00c8ff)
#define COL_IRIS_THINK  lv_color_hex(0xf0a020)
#define COL_IRIS_SPEAK  lv_color_hex(0xa040f0)
#define COL_IRIS_HAPPY  lv_color_hex(0x40e080)
#define COL_IRIS_ERROR  lv_color_hex(0xff2020)
#define COL_MOUTH       lv_color_hex(0xd8e8ff)
#define COL_TEXT        lv_color_hex(0xe0eeff)
#define COL_SUBTEXT     lv_color_hex(0x6080b0)
#define COL_RING_IDLE   lv_color_hex(0x1a3060)
#define COL_RING_WAKE   lv_color_hex(0xffffff)
#define COL_RING_LISTEN lv_color_hex(0x00c8ff)
#define COL_RING_THINK  lv_color_hex(0xf0a020)
#define COL_RING_SPEAK  lv_color_hex(0xa040f0)
#define COL_RING_HAPPY  lv_color_hex(0x40e080)
#define COL_RING_ERROR  lv_color_hex(0xff2020)
#define COL_RING_PROV   lv_color_hex(0xffee00)
#define COL_RING_WIFI   lv_color_hex(0x404060)

// ── Layout constants (all relative to 466×466) ───────────────────────────────
#define CX              233   // centre x
#define CY              220   // centre y (slightly above centre for text room)
#define FACE_R          160   // face circle radius
#define EYE_W           46    // eye width
#define EYE_H           28    // eye height (normal open)
#define EYE_LX          (CX - 60)
#define EYE_RX          (CX + 60)
#define EYE_Y           (CY - 18)
#define PUPIL_R         10
#define MOUTH_W         80
#define MOUTH_Y         (CY + 48)
#define RING_THICK      10
#define STATUS_Y        (LCD_V_RES - 28)

// ── Internal objects ──────────────────────────────────────────────────────────
static struct {
    lv_obj_t *screen;
    lv_obj_t *bg;
    lv_obj_t *face;
    lv_obj_t *status_ring;      // outer status arc
    lv_obj_t *eye_l;            // left eye (rounded rectangle)
    lv_obj_t *eye_r;
    lv_obj_t *pupil_l;
    lv_obj_t *pupil_r;
    lv_obj_t *brow_l;           // eyebrow arc
    lv_obj_t *brow_r;
    lv_obj_t *mouth;            // arc for mouth
    lv_obj_t *text_label;       // main response text
    lv_obj_t *status_label;     // bottom status (wifi, battery)
    lv_obj_t *state_label;      // centre state hint (e.g. "Listening…")
    lv_anim_t blink_anim;
    lv_anim_t ring_anim;
} ui;

static avatar_state_t  s_state   = AVATAR_STATE_IDLE;
static bool            s_init    = false;

// ── Animation helpers ─────────────────────────────────────────────────────────

static void eye_height_anim_cb(void *obj, int32_t val)
{
    // Blink: shrink eye height to 0 then back
    int32_t h = (int32_t)lv_obj_get_height((lv_obj_t *)obj);
    lv_obj_set_height((lv_obj_t *)obj, val);
    lv_obj_set_height(ui.eye_r, val);
}

static void ring_opacity_anim_cb(void *obj, int32_t val)
{
    lv_obj_set_style_arc_opa((lv_obj_t *)obj, (lv_opa_t)val, LV_PART_INDICATOR);
}

static void start_blink_timer(void)
{
    lv_anim_init(&ui.blink_anim);
    lv_anim_set_var(&ui.blink_anim, ui.eye_l);
    lv_anim_set_exec_cb(&ui.blink_anim, eye_height_anim_cb);
    lv_anim_set_values(&ui.blink_anim, EYE_H, 2);
    lv_anim_set_time(&ui.blink_anim, 120);
    lv_anim_set_playback_time(&ui.blink_anim, 120);
    lv_anim_set_delay(&ui.blink_anim, 3000 + (lv_rand(0, 2000)));
    lv_anim_set_repeat_count(&ui.blink_anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_repeat_delay(&ui.blink_anim, 4000);
    lv_anim_start(&ui.blink_anim);
}

// ── Per-state appearance ──────────────────────────────────────────────────────

typedef struct {
    lv_color_t ring_col;
    lv_color_t iris_col;
    int32_t    eye_h;        // eye height px
    int32_t    mouth_start;  // arc start angle (degrees × 10)
    int32_t    mouth_end;    // arc end angle
    bool       mouth_open;   // true = concave (happy/speaking), false = flat/sad
    bool       blink;
    const char *hint;
} state_style_t;

static const state_style_t STATE_STYLES[AVATAR_STATE_COUNT] = {
    // IDLE
    { COL_RING_IDLE,   COL_IRIS_IDLE,   EYE_H, 2000, 3400, false, true,  NULL },
    // WAKING
    { COL_RING_WAKE,   COL_EYE_WHITE,   EYE_H + 10, 2500, 2900, false, false, "Hi!" },
    // LISTENING
    { COL_RING_LISTEN, COL_IRIS_LISTEN, EYE_H + 8,  2500, 2900, false, false, "Listening\xe2\x80\xa6" },
    // THINKING
    { COL_RING_THINK,  COL_IRIS_THINK,  EYE_H - 4,  2300, 3100, false, false, "Thinking\xe2\x80\xa6" },
    // SPEAKING
    { COL_RING_SPEAK,  COL_IRIS_SPEAK,  EYE_H,      1800, 3600, true,  false, NULL },
    // HAPPY
    { COL_RING_HAPPY,  COL_IRIS_HAPPY,  EYE_H - 6,  1800, 3600, true,  false, NULL },
    // SURPRISED
    { COL_RING_WAKE,   COL_EYE_WHITE,   EYE_H + 12, 2200, 3200, true,  false, NULL },
    // WIFI_WAIT
    { COL_RING_WIFI,   COL_IRIS_IDLE,   EYE_H - 10, 2600, 2800, false, false, "Connecting\xe2\x80\xa6" },
    // PROV
    { COL_RING_PROV,   COL_IRIS_IDLE,   EYE_H,      2400, 3000, false, true,  "POD-SETUP\nwifi \xe2\x96\xb6 192.168.4.1" },
    // ERROR
    { COL_RING_ERROR,  COL_IRIS_ERROR,  EYE_H - 8,  1200, 2400, false, false, "Error" },
};

static void apply_state_style(avatar_state_t state)
{
    const state_style_t *st = &STATE_STYLES[state];

    // Ring colour
    lv_obj_set_style_arc_color(ui.status_ring, st->ring_col, LV_PART_INDICATOR);

    // Iris colour
    lv_obj_set_style_bg_color(ui.pupil_l, st->iris_col, 0);
    lv_obj_set_style_bg_color(ui.pupil_r, st->iris_col, 0);

    // Eye height (immediate)
    lv_obj_set_height(ui.eye_l, st->eye_h);
    lv_obj_set_height(ui.eye_r, st->eye_h);

    // Mouth arc angles
    lv_arc_set_angles(ui.mouth,
        (uint16_t)(st->mouth_start / 10),
        (uint16_t)(st->mouth_end / 10));
    lv_obj_set_style_arc_color(ui.mouth, COL_MOUTH, LV_PART_INDICATOR);

    // Hint label
    if (st->hint) {
        lv_label_set_text(ui.state_label, st->hint);
        lv_obj_clear_flag(ui.state_label, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(ui.state_label, LV_OBJ_FLAG_HIDDEN);
    }

    // Blink animation
    lv_anim_del(ui.eye_l, eye_height_anim_cb);
    if (st->blink) start_blink_timer();

    // Eyebrow raises for thinking
    if (state == AVATAR_STATE_THINKING) {
        lv_obj_set_y(ui.brow_l, EYE_Y - FACE_R / 2 - 28);
        lv_obj_set_y(ui.brow_r, EYE_Y - FACE_R / 2 - 16);
    } else {
        lv_obj_set_y(ui.brow_l, EYE_Y - FACE_R / 2 - 20);
        lv_obj_set_y(ui.brow_r, EYE_Y - FACE_R / 2 - 20);
    }
}

// ── Init ──────────────────────────────────────────────────────────────────────

esp_err_t avatar_init(lv_obj_t *screen)
{
    ui.screen = screen;
    lv_obj_set_style_bg_color(screen, COL_BG, 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);

    // ── Status ring (outermost arc, 466×466 circle) ──────────────────────────
    ui.status_ring = lv_arc_create(screen);
    lv_obj_set_size(ui.status_ring, LCD_H_RES - 4, LCD_V_RES - 4);
    lv_obj_center(ui.status_ring);
    lv_arc_set_rotation(ui.status_ring, 270);
    lv_arc_set_bg_angles(ui.status_ring, 0, 360);
    lv_arc_set_angles(ui.status_ring, 0, 360);
    lv_obj_set_style_arc_color(ui.status_ring, COL_RING_IDLE, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(ui.status_ring, COL_BG,        LV_PART_MAIN);
    lv_obj_set_style_arc_width(ui.status_ring, RING_THICK,    LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(ui.status_ring, RING_THICK,    LV_PART_MAIN);
    lv_obj_remove_style(ui.status_ring, NULL, LV_PART_KNOB);
    lv_obj_clear_flag(ui.status_ring, LV_OBJ_FLAG_CLICKABLE);

    // ── Face circle ──────────────────────────────────────────────────────────
    ui.face = lv_obj_create(screen);
    lv_obj_set_size(ui.face, FACE_R * 2, FACE_R * 2);
    lv_obj_center(ui.face);
    lv_obj_set_y(ui.face, CY - LCD_V_RES / 2);
    lv_obj_set_style_bg_color(ui.face, COL_FACE, 0);
    lv_obj_set_style_bg_opa(ui.face, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(ui.face, FACE_R, 0);
    lv_obj_set_style_border_width(ui.face, 0, 0);
    lv_obj_set_style_pad_all(ui.face, 0, 0);

    // ── Eyes ─────────────────────────────────────────────────────────────────
    auto make_eye = [](lv_obj_t *parent, int x, int y) -> lv_obj_t * {
        lv_obj_t *e = lv_obj_create(parent);
        lv_obj_set_size(e, EYE_W, EYE_H);
        lv_obj_set_pos(e, x - EYE_W / 2, y - EYE_H / 2);
        lv_obj_set_style_bg_color(e, COL_EYE_WHITE, 0);
        lv_obj_set_style_bg_opa(e, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(e, EYE_H / 2, 0);
        lv_obj_set_style_border_width(e, 0, 0);
        lv_obj_set_style_pad_all(e, 0, 0);
        return e;
    };
    ui.eye_l = make_eye(screen, EYE_LX, EYE_Y);
    ui.eye_r = make_eye(screen, EYE_RX, EYE_Y);

    // ── Pupils / irises ───────────────────────────────────────────────────────
    auto make_pupil = [](lv_obj_t *eye, lv_color_t col) -> lv_obj_t * {
        lv_obj_t *p = lv_obj_create(eye);
        lv_obj_set_size(p, PUPIL_R * 2, PUPIL_R * 2);
        lv_obj_center(p);
        lv_obj_set_style_bg_color(p, col, 0);
        lv_obj_set_style_bg_opa(p, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(p, PUPIL_R, 0);
        lv_obj_set_style_border_width(p, 0, 0);
        return p;
    };
    ui.pupil_l = make_pupil(ui.eye_l, COL_IRIS_IDLE);
    ui.pupil_r = make_pupil(ui.eye_r, COL_IRIS_IDLE);

    // Inner pupil (dark centre)
    auto make_dark_pupil = [](lv_obj_t *iris) {
        lv_obj_t *d = lv_obj_create(iris);
        lv_obj_set_size(d, PUPIL_R, PUPIL_R);
        lv_obj_center(d);
        lv_obj_set_style_bg_color(d, COL_PUPIL, 0);
        lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(d, PUPIL_R / 2, 0);
        lv_obj_set_style_border_width(d, 0, 0);
    };
    make_dark_pupil(ui.pupil_l);
    make_dark_pupil(ui.pupil_r);

    // ── Eyebrows ──────────────────────────────────────────────────────────────
    auto make_brow = [](lv_obj_t *parent, int x, int y) -> lv_obj_t * {
        lv_obj_t *b = lv_arc_create(parent);
        lv_obj_set_size(b, 46, 20);
        lv_obj_set_pos(b, x - 23, y);
        lv_arc_set_rotation(b, 180);
        lv_arc_set_bg_angles(b, 0, 180);
        lv_arc_set_angles(b, 0, 180);
        lv_obj_set_style_arc_color(b, COL_EYE_WHITE, LV_PART_INDICATOR);
        lv_obj_set_style_arc_color(b, COL_FACE,      LV_PART_MAIN);
        lv_obj_set_style_arc_width(b, 3, LV_PART_INDICATOR);
        lv_obj_set_style_arc_width(b, 3, LV_PART_MAIN);
        lv_obj_remove_style(b, NULL, LV_PART_KNOB);
        lv_obj_clear_flag(b, LV_OBJ_FLAG_CLICKABLE);
        return b;
    };
    ui.brow_l = make_brow(screen, EYE_LX, EYE_Y - FACE_R / 2 - 20);
    ui.brow_r = make_brow(screen, EYE_RX, EYE_Y - FACE_R / 2 - 20);

    // ── Mouth arc ────────────────────────────────────────────────────────────
    ui.mouth = lv_arc_create(screen);
    lv_obj_set_size(ui.mouth, MOUTH_W, MOUTH_W / 2 + 10);
    lv_obj_set_pos(ui.mouth, CX - MOUTH_W / 2, MOUTH_Y);
    lv_arc_set_rotation(ui.mouth, 180);
    lv_arc_set_bg_angles(ui.mouth, 0, 180);
    lv_arc_set_angles(ui.mouth, 0, 180);
    lv_obj_set_style_arc_color(ui.mouth, COL_MOUTH, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(ui.mouth, COL_FACE,  LV_PART_MAIN);
    lv_obj_set_style_arc_width(ui.mouth, 4, LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(ui.mouth, 4, LV_PART_MAIN);
    lv_obj_remove_style(ui.mouth, NULL, LV_PART_KNOB);
    lv_obj_clear_flag(ui.mouth, LV_OBJ_FLAG_CLICKABLE);

    // ── State hint label (centre, above text) ─────────────────────────────────
    ui.state_label = lv_label_create(screen);
    lv_obj_set_width(ui.state_label, LCD_H_RES - 40);
    lv_obj_set_style_text_color(ui.state_label, COL_SUBTEXT, 0);
    lv_obj_set_style_text_font(ui.state_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_align(ui.state_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(ui.state_label, LV_ALIGN_CENTER, 0, 80);
    lv_label_set_text(ui.state_label, "");
    lv_obj_add_flag(ui.state_label, LV_OBJ_FLAG_HIDDEN);

    // ── Response text label ───────────────────────────────────────────────────
    ui.text_label = lv_label_create(screen);
    lv_obj_set_width(ui.text_label, LCD_H_RES - 60);
    lv_obj_set_style_text_color(ui.text_label, COL_TEXT, 0);
    lv_obj_set_style_text_font(ui.text_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_align(ui.text_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(ui.text_label, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_align(ui.text_label, LV_ALIGN_BOTTOM_MID, 0, -40);
    lv_label_set_text(ui.text_label, "");

    // ── Status bar ────────────────────────────────────────────────────────────
    ui.status_label = lv_label_create(screen);
    lv_obj_set_width(ui.status_label, LCD_H_RES - 40);
    lv_obj_set_style_text_color(ui.status_label, COL_SUBTEXT, 0);
    lv_obj_set_style_text_font(ui.status_label, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(ui.status_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(ui.status_label, LV_ALIGN_BOTTOM_MID, 0, -14);
    lv_label_set_text(ui.status_label, "");

    // ── Initial state ─────────────────────────────────────────────────────────
    apply_state_style(AVATAR_STATE_IDLE);
    start_blink_timer();

    s_init  = true;
    s_state = AVATAR_STATE_IDLE;
    ESP_LOGI(TAG, "Avatar initialised");
    return ESP_OK;
}

// ── Public API ─────────────────────────────────────────────────────────────────

void avatar_set_state(avatar_state_t state)
{
    if (state >= AVATAR_STATE_COUNT) return;

    if (display_bsp_lock(50)) {
        s_state = state;
        apply_state_style(state);
        display_bsp_unlock();
    }
}

void avatar_set_text(const char *text)
{
    if (display_bsp_lock(50)) {
        if (text && text[0]) {
            lv_label_set_text(ui.text_label, text);
            lv_obj_clear_flag(ui.text_label, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_label_set_text(ui.text_label, "");
            lv_obj_add_flag(ui.text_label, LV_OBJ_FLAG_HIDDEN);
        }
        display_bsp_unlock();
    }
}

void avatar_set_battery(int percent)
{
    if (!s_init) return;
    char buf[32];
    if (percent < 0) {
        buf[0] = '\0';
    } else {
        const char *icon = percent > 80 ? "\xef\x89\x80"   // full
                         : percent > 50 ? "\xef\x89\x81"   // 3/4
                         : percent > 20 ? "\xef\x89\x82"   // half
                                        : "\xef\x89\x83";  // low
        snprintf(buf, sizeof(buf), "%s %d%%", icon, percent);
    }
    if (display_bsp_lock(30)) {
        // Append to status label (wifi + battery)
        // Status label is rebuilt each update; store separately if needed
        // For simplicity, just show battery in status label here
        lv_label_set_text(ui.status_label, buf);
        display_bsp_unlock();
    }
}

void avatar_set_wifi_rssi(int rssi)
{
    if (!s_init) return;
    // Compose status bar: wifi icon + signal strength
    char buf[48];
    if (rssi == 0) {
        snprintf(buf, sizeof(buf), "No WiFi");
    } else if (rssi > -50) {
        snprintf(buf, sizeof(buf), "\xef\x87\xab  %d dBm", rssi);  // strong
    } else if (rssi > -70) {
        snprintf(buf, sizeof(buf), "\xef\x87\xac  %d dBm", rssi);  // medium
    } else {
        snprintf(buf, sizeof(buf), "\xef\x87\xad  %d dBm", rssi);  // weak
    }
    if (display_bsp_lock(30)) {
        lv_label_set_text(ui.status_label, buf);
        display_bsp_unlock();
    }
}

void avatar_apply_expression(const char *expression)
{
    if (!expression) return;

    avatar_state_t state = AVATAR_STATE_IDLE;

    if      (strcmp(expression, "happy")     == 0) state = AVATAR_STATE_HAPPY;
    else if (strcmp(expression, "thinking")  == 0) state = AVATAR_STATE_THINKING;
    else if (strcmp(expression, "listening") == 0) state = AVATAR_STATE_LISTENING;
    else if (strcmp(expression, "speaking")  == 0) state = AVATAR_STATE_SPEAKING;
    else if (strcmp(expression, "surprised") == 0) state = AVATAR_STATE_SURPRISED;
    else if (strcmp(expression, "neutral")   == 0) state = AVATAR_STATE_IDLE;
    else if (strcmp(expression, "sad")       == 0) state = AVATAR_STATE_ERROR;

    avatar_set_state(state);
}

avatar_state_t avatar_get_state(void)
{
    return s_state;
}
