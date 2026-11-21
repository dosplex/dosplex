/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright 2026 RoyÅke (Roy-Åke Martinsson) */
#define _GNU_SOURCE
#define _XOPEN_SOURCE 700

#include <assert.h>
#include <ctype.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <getopt.h>
#include <math.h>
#include <grp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <pwd.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/random.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <dirent.h>
#ifdef HAVE_LIBEI
#include <libei.h>
#endif
#include <sys/stat.h>
#include <sys/un.h>
#include <errno.h>
#include <sys/wait.h>
#include <unistd.h>
#include <xkbcommon/xkbcommon.h>
#include <linux/input.h>
#include <libinput.h>
#include <drm_fourcc.h>
#include <cairo/cairo.h>
#include <json-c/json.h>
#include <wayland-server-core.h>
#include <wayland-server.h>
#include <wlr/backend.h>
#include <wlr/backend/headless.h>
#include <wlr/backend/x11.h>
#include <wlr/backend/session.h>
#include <wlr/backend/drm.h>
#include <wlr/backend/libinput.h>
#include <wlr/backend/multi.h>
#include <wlr/render/allocator.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_buffer.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_screencopy_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_touch.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/util/log.h>
#include <wlr/util/region.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/interfaces/wlr_keyboard.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_subcompositor.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/types/wlr_xdg_activation_v1.h>
#include <wlr/xwayland/xwayland.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_fractional_scale_v1.h>
#include <wlr/types/wlr_linux_dmabuf_v1.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_pointer_constraints_v1.h>
#include <wlr/types/wlr_presentation_time.h>
#include <wlr/types/wlr_primary_selection_v1.h>
#include <wlr/types/wlr_relative_pointer_v1.h>
#include <wlr/types/wlr_viewporter.h>
#include <wlr/types/wlr_single_pixel_buffer_v1.h>
#include <wlr/types/wlr_xdg_output_v1.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <xcb/xcb.h>
#define SOCKET_NAME "dosplex-0"
#define PRIMARY_INPUT_SOCKET_NAME "dosplex-primary-input.sock"
#define HUD_IPC_SOCKET_NAME "hud-ipc.sock"
#define AUTH_HELPER_DEFAULT "/usr/local/libexec/dosplex-side-auth.py"
#define SESSION_SCRIPT_DEFAULT "/usr/local/bin/dosplex-side-supervisor.sh"
/* The power helper is $DOSPLEX_ROOT/libexec/dosplex-power-action (DOSPLEX_ROOT defaults to /usr/local). */
#define POWER_HELPER_ROOT_DEFAULT "/usr/local"
#define POWER_HELPER_RELATIVE "libexec/dosplex-power-action"
/* The panel (Help and Feedback): a per-account program the menu starts and that ends when it is closed. */
#define PANEL_HELPER_RELATIVE "libexec/dosplex-panel-launch.py"
/* The feedback form is a file of the release. A release that does not carry it has no Feedback in its menu. */
#define PANEL_FEEDBACK_FORM_RELATIVE "share/dosplex/panel/Feedback.qml"
#define PANEL_PYTHON "/usr/bin/python3"
#define PANEL_MAP_SECONDS 15.0
/* The helper's escaped unit runs at most 1020 s (UNIT_RUNTIME in dosplex-power-action) and stops this
 * compositor on its way. If this compositor is still here 60 s after that, the unit failed to stop it
 * (a failed stop and a stand-in for systemctl, say): the progress panel comes down and says so.
 * DOSPLEX_POWER_PANEL_TIMEOUT_MS shortens it, only with --test-hooks. */
#define POWER_PANEL_DEADLINE_MS (1080 * 1000)

/* === Color palette === */
static const float BG_COLOR[4]            = {0.04, 0.04, 0.07, 1.0};
static const float LABEL_LEFT_COLOR[4]    = {0.12, 0.13, 0.17, 1.0};
static const float LABEL_RIGHT_COLOR[4]   = {0.12, 0.13, 0.17, 1.0};
static const float INPUT_COLOR[4]          = {0.11, 0.11, 0.17, 1.0};
static const float BUTTON_LEFT_COLOR[4]    = {0.20, 0.46, 0.86, 1.0};
static const float BUTTON_RIGHT_COLOR[4]   = {0.20, 0.46, 0.86, 1.0};
static const float CLAIMED_COLOR[4]        = {0.08, 0.09, 0.12, 1.0};
static const float CURSOR_COLOR[4]         = {0.95, 0.95, 0.98, 0.95};
static const float MODAL_SCRIM_COLOR[4]    = {0.02, 0.02, 0.03, 0.22};
static const float MODAL_PANEL_COLOR[4]    = {0.08, 0.09, 0.12, 0.96};
static const float MODAL_BOX_COLOR[4]      = {0.12, 0.13, 0.17, 1.0};
static const float MODAL_LINE_COLOR[4]     = {0.88, 0.90, 0.96, 1.0};
static const float MODAL_ACCENT_COLOR[4]   = {0.20, 0.46, 0.86, 1.0};
#define IDLE_DIM_TIMEOUT_MS (5 * 60 * 1000)

/* Pointer buttons whose presses the compositor counts until they are released: BTN_MISC and the 63
 * codes above it, which hold the mouse buttons (BTN_LEFT to BTN_TASK) and the extras of a pointer. */
#define POINTER_BUTTON_SLOTS 64
#define BACKLIGHT_BRIGHTNESS_PATH "/sys/class/backlight/amdgpu_bl0/brightness"
#define BACKLIGHT_MAX_BRIGHTNESS_PATH "/sys/class/backlight/amdgpu_bl0/max_brightness"

static bool dosplex_file_debug_enabled(void) {
    const char *value = getenv("DOSPLEX_WAYLAND_DEBUG");
    return value && value[0] != '\0' && strcmp(value, "0") != 0 &&
        strcmp(value, "false") != 0 && strcmp(value, "no") != 0;
}

/* === Cairo text rendering === */

/* Custom wlr_buffer backed by a Cairo image surface (ARGB32) */
struct cairo_pixel_buf {
    struct wlr_buffer base;
    uint8_t *data;
    int width, height;
};

static void cpb_destroy(struct wlr_buffer *buf) {
    struct cairo_pixel_buf *cb = wl_container_of(buf, cb, base);
    free(cb->data);
    free(cb);
}

static bool cpb_begin_data_ptr_access(struct wlr_buffer *buf, uint32_t flags,
        void **data, uint32_t *format, size_t *stride) {
    (void)flags;
    struct cairo_pixel_buf *cb = wl_container_of(buf, cb, base);
    *data   = cb->data;
    *format = DRM_FORMAT_ARGB8888;  /* same layout as CAIRO_FORMAT_ARGB32 on LE */
    *stride = (size_t)cb->width * 4;
    return true;
}

static void cpb_end_data_ptr_access(struct wlr_buffer *buf) { (void)buf; }

static const struct wlr_buffer_impl cairo_pixel_buf_impl = {
    .destroy               = cpb_destroy,
    .begin_data_ptr_access = cpb_begin_data_ptr_access,
    .end_data_ptr_access   = cpb_end_data_ptr_access,
};

/*
 * Render `text` with Cairo into a new wlr_scene_buffer node positioned at
 * (x, y) in `parent`.  The node is sized to (w × h).
 * Text is centred horizontally and vertically within the node.
 * Returns the node (never NULL — if something fails the node is still created
 * with an empty/transparent surface so rendering continues).
 */
static struct wlr_scene_buffer *make_text_node(
        struct wlr_scene_tree *parent,
        int x, int y, int w, int h,
        const char *text,
        double font_size,
        double r, double g, double b, double a,
        bool bold) {
    /* Allocate pixel storage (ARGB32, premultiplied) */
    struct cairo_pixel_buf *cb = calloc(1, sizeof(*cb));
    if (!cb) goto fallback;

    cb->width  = w;
    cb->height = h;
    cb->data   = calloc(1, (size_t)w * h * 4);
    if (!cb->data) { free(cb); goto fallback; }

    wlr_buffer_init(&cb->base, &cairo_pixel_buf_impl, w, h);

    /* Draw with Cairo */
    cairo_surface_t *surf = cairo_image_surface_create_for_data(
        cb->data, CAIRO_FORMAT_ARGB32, w, h, w * 4);
    if (cairo_surface_status(surf) != CAIRO_STATUS_SUCCESS) {
        cairo_surface_destroy(surf);
        wlr_buffer_drop(&cb->base);
        goto fallback;
    }

    cairo_t *cr = cairo_create(surf);

    /* Transparent background */
    cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
    cairo_set_source_rgba(cr, 0, 0, 0, 0);
    cairo_paint(cr);

    /* Text */
    cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
    cairo_select_font_face(cr, "Sans",
        CAIRO_FONT_SLANT_NORMAL,
        bold ? CAIRO_FONT_WEIGHT_BOLD : CAIRO_FONT_WEIGHT_NORMAL);
    cairo_set_font_size(cr, font_size);
    cairo_set_source_rgba(cr, r, g, b, a);

    cairo_text_extents_t ext;
    cairo_text_extents(cr, text, &ext);
    double tx = (w - ext.width)  / 2.0 - ext.x_bearing;
    double ty = (h - ext.height) / 2.0 - ext.y_bearing;
    cairo_move_to(cr, tx, ty);
    cairo_show_text(cr, text);

    cairo_destroy(cr);
    cairo_surface_destroy(surf);

    /* Attach to scene */
    struct wlr_scene_buffer *node = wlr_scene_buffer_create(parent, &cb->base);
    wlr_buffer_drop(&cb->base);   /* scene holds its own ref */
    if (node) {
        wlr_scene_node_set_position(&node->node, x, y);
    }
    return node;

fallback:;
    /* Still create a node so callers never receive NULL */
    struct wlr_scene_buffer *node2 = wlr_scene_buffer_create(parent, NULL);
    if (node2) wlr_scene_node_set_position(&node2->node, x, y);
    return node2;
}

/* Convenience: left-aligned text, not centred */
static struct wlr_scene_buffer *make_text_node_left(
        struct wlr_scene_tree *parent,
        int x, int y, int w, int h,
        const char *text,
        double font_size,
        double r, double g, double b, double a,
        bool bold) {
    struct cairo_pixel_buf *cb = calloc(1, sizeof(*cb));
    if (!cb) return NULL;

    cb->width  = w;
    cb->height = h;
    cb->data   = calloc(1, (size_t)w * h * 4);
    if (!cb->data) { free(cb); return NULL; }

    wlr_buffer_init(&cb->base, &cairo_pixel_buf_impl, w, h);

    cairo_surface_t *surf = cairo_image_surface_create_for_data(
        cb->data, CAIRO_FORMAT_ARGB32, w, h, w * 4);
    cairo_t *cr = cairo_create(surf);

    cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
    cairo_set_source_rgba(cr, 0, 0, 0, 0);
    cairo_paint(cr);

    cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
    cairo_select_font_face(cr, "Sans",
        CAIRO_FONT_SLANT_NORMAL,
        bold ? CAIRO_FONT_WEIGHT_BOLD : CAIRO_FONT_WEIGHT_NORMAL);
    cairo_set_font_size(cr, font_size);
    cairo_set_source_rgba(cr, r, g, b, a);

    cairo_text_extents_t ext;
    cairo_text_extents(cr, text, &ext);
    double ty = (h - ext.height) / 2.0 - ext.y_bearing;
    cairo_move_to(cr, 6, ty);      /* 6px left padding */
    cairo_show_text(cr, text);

    cairo_destroy(cr);
    cairo_surface_destroy(surf);

    struct wlr_scene_buffer *node = wlr_scene_buffer_create(parent, &cb->base);
    wlr_buffer_drop(&cb->base);
    if (node) wlr_scene_node_set_position(&node->node, x, y);
    return node;
}

/* === Wrapped text ===
 *
 * make_text_node_left() draws one line and clips what is wider than its node, so a message that is longer than the
 * panel was cut off at the panel's edge. make_text_node_fit() draws what fits on a line exactly as before and
 * wraps, then shrinks, then ends in an ellipsis what does not, always inside the node. */

#define TEXT_PADDING 6.0          /* make_text_node_left()'s padding; the right edge keeps the same */
#define TEXT_LINE_HEIGHT 1.25     /* of the font size */
#define TEXT_SHRINK_LIMIT 0.72    /* a text is shrunk to this much of its size, and then ends in an ellipsis */
#define TEXT_MAX_LINES 12

struct text_line {
    size_t start, length;         /* a slice of the text; no leading or trailing blanks */
};

/* How far `length` bytes of text reach when drawn from the origin with the font of `cr`: the pen's advance or
 * the right edge of the ink, whichever is wider. */
static double text_line_width(cairo_t *cr, const char *text, size_t length) {
    char buffer[1024];
    if (length >= sizeof(buffer)) length = sizeof(buffer) - 1;
    memcpy(buffer, text, length);
    buffer[length] = '\0';
    cairo_text_extents_t ext;
    cairo_text_extents(cr, buffer, &ext);
    double ink = ext.x_bearing + ext.width;
    return ext.x_advance > ink ? ext.x_advance : ink;
}

/* The bytes of the UTF-8 character at `s` (1 for a byte that does not start one, and at the end). */
static size_t utf8_sequence_length(const char *s) {
    unsigned char c = (unsigned char)s[0];
    size_t length = c >= 0xf0 ? 4 : c >= 0xe0 ? 3 : c >= 0xc0 ? 2 : 1;
    for (size_t i = 1; i < length; i++) {
        if (((unsigned char)s[i] & 0xc0) != 0x80) return 1;
    }
    return length;
}

/* Breaks `text` at its blanks into lines no wider than `max_width` (in the font of `cr`), at most `max_lines`
 * of them, and returns how many. A word that is wider than a line alone is broken inside, between characters,
 * so that no line is wider than `max_width` unless one character is. `*complete` says whether all of the text
 * is in the lines. */
static int wrap_text_lines(cairo_t *cr, const char *text, double max_width,
        struct text_line *lines, int max_lines, bool *complete) {
    int count = 0;
    const char *p = text;
    *complete = true;
    for (;;) {
        while (*p == ' ') p++;
        if (!*p) break;
        if (count == max_lines) {
            *complete = false;
            break;
        }
        const char *start = p, *end = p, *scan = p;
        while (*scan) {
            const char *word_end = scan;
            while (*word_end && *word_end != ' ') word_end++;
            if (text_line_width(cr, start, (size_t)(word_end - start)) > max_width) break;
            end = word_end;
            scan = word_end;
            while (*scan == ' ') scan++;
        }
        if (end == start) {
            /* The first word alone does not fit: as many of its characters as do (at least one). */
            const char *q = start;
            while (*q && *q != ' ') {
                const char *next = q + utf8_sequence_length(q);
                if (q > start && text_line_width(cr, start, (size_t)(next - start)) > max_width) break;
                q = next;
            }
            end = q;
        }
        lines[count].start = (size_t)(start - text);
        lines[count].length = (size_t)(end - start);
        count++;
        p = end;
    }
    return count;
}

/* One line of `text` for drawing: `length` bytes from `start`, or, with `ellipsis`, as many of them as leave room
 * for an ellipsis within `max_width`, and that. */
static void text_line_for_drawing(cairo_t *cr, const char *text, struct text_line line, bool ellipsis,
        double max_width, char *out, size_t out_size) {
    size_t length = line.length < out_size - 4 ? line.length : out_size - 4;
    if (ellipsis) {
        static const char dots[] = "\xe2\x80\xa6";
        for (;;) {
            memcpy(out, text + line.start, length);
            memcpy(out + length, dots, sizeof(dots));
            if (length == 0 || text_line_width(cr, out, length + 3) <= max_width) return;
            do length--; while (length > 0 && ((unsigned char)text[line.start + length] & 0xc0) == 0x80);
        }
    }
    memcpy(out, text + line.start, length);
    out[length] = '\0';
}

/* Whether `text` fits on the one line of a node `box_width` pixels wide as make_text_node_left() draws it. */
static bool text_fits_one_line(cairo_t *cr, const char *text, double box_width) {
    return TEXT_PADDING + text_line_width(cr, text, strlen(text)) <= box_width;
}

/* The lines of `text` in a box of `box_width` by `box_height` pixels, in the font of `cr` at `font_size`: wrapped
 * to the box's width minus the padding at both sides, as many lines as the height holds. When they do not all fit
 * the font shrinks, down to TEXT_SHRINK_LIMIT of `font_size`, until they do. Returns the number of lines; `*size`
 * is the font size at the end (set in `cr` too) and `*complete` says whether all of the text is in the lines (it is
 * not when even the smallest font leaves text over: the last line then ends in an ellipsis). */
static int fit_text_lines(cairo_t *cr, const char *text, double font_size, double box_width, double box_height,
        struct text_line *lines, bool *complete, double *size) {
    double max_width = box_width - 2 * TEXT_PADDING;
    int count;
    *size = font_size;
    for (;;) {
        cairo_set_font_size(cr, *size);
        int fit = (int)floor(box_height / (*size * TEXT_LINE_HEIGHT));
        if (fit < 1) fit = 1;
        if (fit > TEXT_MAX_LINES) fit = TEXT_MAX_LINES;
        count = wrap_text_lines(cr, text, max_width, lines, fit, complete);
        if (*complete || *size <= font_size * TEXT_SHRINK_LIMIT + 0.01) break;
        *size = fmax(font_size * TEXT_SHRINK_LIMIT, *size * 0.94);
    }
    return count;
}

/* Text for a node of w by h pixels. What fits on one line is drawn by make_text_node_left(), as it always was.
 * What does not is wrapped to the node's width; when the lines are more than the node's height holds the font
 * shrinks (to TEXT_SHRINK_LIMIT of its size), and what still does not fit ends in an ellipsis. The block of lines
 * is centred in the node, as a single line is. */
static struct wlr_scene_buffer *make_text_node_fit(
        struct wlr_scene_tree *parent,
        int x, int y, int w, int h,
        const char *text,
        double font_size,
        double r, double g, double b, double a,
        bool bold) {
    if (!text || !text[0] || w <= 2 * TEXT_PADDING || h <= 0)
        return make_text_node_left(parent, x, y, w, h, text ? text : "", font_size, r, g, b, a, bold);
    cairo_surface_t *scratch = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, 1, 1);
    cairo_t *measure = cairo_create(scratch);
    cairo_select_font_face(measure, "Sans", CAIRO_FONT_SLANT_NORMAL,
        bold ? CAIRO_FONT_WEIGHT_BOLD : CAIRO_FONT_WEIGHT_NORMAL);
    cairo_set_font_size(measure, font_size);
    double max_width = w - 2 * TEXT_PADDING;
    if (text_fits_one_line(measure, text, w)) {
        cairo_destroy(measure);
        cairo_surface_destroy(scratch);
        return make_text_node_left(parent, x, y, w, h, text, font_size, r, g, b, a, bold);
    }

    struct text_line lines[TEXT_MAX_LINES];
    double size;
    bool complete;
    int count = fit_text_lines(measure, text, font_size, w, h, lines, &complete, &size);

    struct wlr_scene_buffer *node = NULL;
    struct cairo_pixel_buf *cb = calloc(1, sizeof(*cb));
    if (cb) {
        cb->width = w;
        cb->height = h;
        cb->data = calloc(1, (size_t)w * h * 4);
        if (!cb->data) {
            free(cb);
            cb = NULL;
        }
    }
    if (cb) {
        wlr_buffer_init(&cb->base, &cairo_pixel_buf_impl, w, h);
        cairo_surface_t *surf = cairo_image_surface_create_for_data(cb->data, CAIRO_FORMAT_ARGB32, w, h, w * 4);
        cairo_t *cr = cairo_create(surf);
        cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
        cairo_set_source_rgba(cr, 0, 0, 0, 0);
        cairo_paint(cr);
        cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
        cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL,
            bold ? CAIRO_FONT_WEIGHT_BOLD : CAIRO_FONT_WEIGHT_NORMAL);
        cairo_set_font_size(cr, size);
        cairo_set_source_rgba(cr, r, g, b, a);
        cairo_font_extents_t font;
        cairo_font_extents(cr, &font);
        double line_height = size * TEXT_LINE_HEIGHT;
        double top = (h - count * line_height) / 2.0;
        if (top < 0) top = 0;
        for (int i = 0; i < count; i++) {
            char line[1024];
            text_line_for_drawing(cr, text, lines[i], !complete && i == count - 1, max_width, line, sizeof(line));
            cairo_move_to(cr, TEXT_PADDING, top + i * line_height + (line_height - font.height) / 2.0 + font.ascent);
            cairo_show_text(cr, line);
        }
        cairo_destroy(cr);
        cairo_surface_destroy(surf);
        node = wlr_scene_buffer_create(parent, &cb->base);
        wlr_buffer_drop(&cb->base);
        if (node) wlr_scene_node_set_position(&node->node, x, y);
    }
    cairo_destroy(measure);
    cairo_surface_destroy(scratch);
    return node;
}

static const char *auth_helper_path(void) {
    const char *path = getenv("DOSPLEX_AUTH_HELPER");
    return path && path[0] ? path : AUTH_HELPER_DEFAULT;
}

static const char *session_script_path(void) {
    const char *path = getenv("DOSPLEX_SESSION_SCRIPT");
    return path && path[0] ? path : SESSION_SCRIPT_DEFAULT;
}

/* Physical side placement */
enum side_id { SIDE_LEFT, SIDE_RIGHT };

/* Logical user role */
enum side_role { SIDE_ROLE_PRIMARY, SIDE_ROLE_GUEST };

struct logical_output_contract {
    char name[32];
    char description[64];
    int logical_x;
    int logical_y;
    int logical_width;
    int logical_height;
    int scale_milli;
};

/* Side state */
enum side_state {
    SIDE_GREETER = 0,
    SIDE_CLAIMED = 1,
};

/* UI element types */
enum ui_elem_type {
    UIelem_NONE = 0,
    UIelem_USERNAME,
    UIelem_PASSWORD,
    UIelem_SIGNIN,
    UIelem_SIGNOUT,
};

enum layout_mode {
    LAYOUT_SINGLE = 0,
    LAYOUT_SPLIT_VERTICAL,
    LAYOUT_SPLIT_HORIZONTAL,
    LAYOUT_SINGLE_GUEST,
};

enum input_route_mode {
    INPUT_ROUTE_FOLLOW = 0,
    INPUT_ROUTE_LEFT,
    INPUT_ROUTE_BOTH,
    INPUT_ROUTE_RIGHT,
};

enum output_scale_mode {
    OUTPUT_SCALE_FIT = 0,
    OUTPUT_SCALE_STRETCH,
    OUTPUT_SCALE_NATIVE,
    /* Appended: Gamescope keeps its Fit scaler and the compositor crops the picture to the region's shape. */
    OUTPUT_SCALE_ZOOM,
};

enum game_resolution_choice {
    GAME_RESOLUTION_NATIVE = 0,
    GAME_RESOLUTION_16_9,
    GAME_RESOLUTION_4_3,
    GAME_RESOLUTION_1_1,
    GAME_RESOLUTION_CUSTOM,
};

/* How the side supervisor last reported a display change (session.json display_result). Deferred: a game is
 * running, so the monitor kept its old size and Gamescope scales the game's picture into the side's region. */
enum display_result {
    DISPLAY_RESULT_NONE = 0,
    DISPLAY_RESULT_APPLIED,
    DISPLAY_RESULT_FAILED,
    DISPLAY_RESULT_DEFERRED,
};

enum modal_action {
    MODAL_ACTION_NONE = 0,
    MODAL_ACTION_CLOSE,
    MODAL_ACTION_SINGLE,
    MODAL_ACTION_SPLIT_VERTICAL,
    MODAL_ACTION_SPLIT_HORIZONTAL,
    MODAL_ACTION_GUEST_LEFT,
    MODAL_ACTION_GUEST_RIGHT,
    MODAL_ACTION_SCALE_FIT,
    MODAL_ACTION_SCALE_STRETCH,
    MODAL_ACTION_SCALE_NATIVE,
    MODAL_ACTION_RESOLUTION_16_9,
    MODAL_ACTION_RESOLUTION_4_3,
    MODAL_ACTION_DISPLAY_APPLY,
    MODAL_ACTION_DISPLAY_CANCEL,
    MODAL_ACTION_TAB_DISPLAY,
    MODAL_ACTION_TAB_DEVICES,
    MODAL_ACTION_TAB_SESSION,
    MODAL_ACTION_TAB_AUDIO,
    MODAL_ACTION_AUDIO_OUTPUT,
    MODAL_ACTION_AUDIO_INPUT,
    MODAL_ACTION_AUDIO_SELECT,
    MODAL_ACTION_AUDIO_MUTE,
    MODAL_ACTION_AUDIO_PREV,
    MODAL_ACTION_AUDIO_NEXT,
    MODAL_ACTION_AUDIO_MIX_OFF,
    MODAL_ACTION_AUDIO_MIX_FULL,
    MODAL_ACTION_AUDIO_MIX_SEPARATED,
    MODAL_ACTION_AUDIO_GENTLE,
    MODAL_ACTION_AUDIO_BALANCED,
    MODAL_ACTION_AUDIO_STRONG,
    MODAL_ACTION_AUDIO_JOIN,
    MODAL_ACTION_AUDIO_LEAVE,
    MODAL_ACTION_DEVICE_PREV,
    MODAL_ACTION_DEVICE_NEXT,
    MODAL_ACTION_CONFIRM,
    MODAL_ACTION_CANCEL_ACTION,
    MODAL_ACTION_DEVICE_FOLLOW,
    MODAL_ACTION_DEVICE_LEFT,
    MODAL_ACTION_DEVICE_BOTH,
    MODAL_ACTION_DEVICE_RIGHT,
    MODAL_ACTION_POWER_SLEEP,
    MODAL_ACTION_POWER_RESTART,
    MODAL_ACTION_POWER_SHUTDOWN,
    MODAL_ACTION_POWER_EXIT,      /* Turn off Dosplex: both sessions close, stock Steam returns, the machine stays on */
    MODAL_ACTION_RESET_SIDE,
    MODAL_ACTION_RESET_ALL,
    /* Retired 2026-10-08: manual Active/Standby/Off controls. Splitting always runs
     * both sessions; the only Standby/Off is the choice Main makes for Guest when
     * switching to Full screen. The values stay reserved so published
     * overlay_confirmation_action codes keep their meaning. */
    MODAL_ACTION_RETIRED_ACTIVE,
    MODAL_ACTION_RETIRED_STANDBY,
    MODAL_ACTION_RETIRED_OFF,
    MODAL_ACTION_RETIRED_GUEST_ACTIVE,
    MODAL_ACTION_RETIRED_GUEST_STANDBY,
    MODAL_ACTION_RETIRED_GUEST_OFF,
    MODAL_ACTION_SINGLE_STANDBY,
    MODAL_ACTION_SINGLE_OFF,
    MODAL_ACTION_RETIRED_SESSION_STATES,
    MODAL_ACTION_KEEP_DEVICE,
    MODAL_ACTION_SWAP_DEVICE,
    /* Appended, not placed beside the other picture choices, so published
     * overlay_confirmation_action codes keep their meaning. */
    MODAL_ACTION_RESOLUTION_1_1,
    /* Guest's own two controller moves (T-422), appended for the same reason. */
    MODAL_ACTION_CLAIM_DEVICE,
    MODAL_ACTION_RELEASE_DEVICE,
    /* The FPS counter and performance recording of the Display tab (T-434), appended for the same reason. */
    MODAL_ACTION_FPS_OFF,
    MODAL_ACTION_FPS_ON,
    MODAL_ACTION_FPS_FRAMETIME,
    MODAL_ACTION_PERF_RECORD,
    /* Main's screen brightness on the Display tab (T-436), appended for the same reason. */
    MODAL_ACTION_BRIGHTNESS_DOWN,
    MODAL_ACTION_BRIGHTNESS_UP,
    /* The Zoom presentation, appended for the same reason. */
    MODAL_ACTION_SCALE_ZOOM,
    /* Guest full screen, appended for the same reason. */
    MODAL_ACTION_SINGLE_GUEST,
    /* Forget a controller that is not connected, appended for the same reason. */
    MODAL_ACTION_FORGET_DEVICE,
    /* Help and Feedback open the panel (ADR-026), appended for the same reason. */
    MODAL_ACTION_HELP,
    MODAL_ACTION_FEEDBACK,
};

/* Per-side UI element region */
struct ui_region {
    enum ui_elem_type type;
    int x, y, w, h;
};

struct modal_region {
    enum modal_action action;
    int x, y, w, h;
    char device_name[256];
};

/* === Swap choice === */
/* Which two controls "Swap controls" exchanges when a player may hold several (T-422).
 * This block is pure code: tests/swap-choice-test.py compiles it alone. */

/* The control a player is using is the one pressed within this many seconds. */
#define SWAP_ACTIVE_SECONDS 10.0
#define ACTIVITY_SLOTS 32

/* When each controller was last seen pressed. The broker marks a controller active for about half a
 * second after a press, so each read of the inventory notes the mark and this keeps the time. */
struct activity_table {
    struct { char id[40]; double at; } slot[ACTIVITY_SLOTS];
    int count;
};

static void activity_note(struct activity_table *table, const char *id, double now) {
    if (!table || !id || !id[0] || strlen(id) >= sizeof(table->slot[0].id)) return;
    int oldest = 0;
    for (int i = 0; i < table->count; i++) {
        if (!strcmp(table->slot[i].id, id)) { table->slot[i].at = now; return; }
        if (table->slot[i].at < table->slot[oldest].at) oldest = i;
    }
    int index = table->count < ACTIVITY_SLOTS ? table->count++ : oldest;
    snprintf(table->slot[index].id, sizeof(table->slot[index].id), "%s", id);
    table->slot[index].at = now;
}

static double activity_last(const struct activity_table *table, const char *id) {
    for (int i = 0; table && id && i < table->count; i++)
        if (!strcmp(table->slot[i].id, id)) return table->slot[i].at;
    return 0;
}

/* One connected control that can open the menu, owned by Main or by Guest. */
struct swap_control {
    const char *key;     /* "controller:<id>" or "input:<n>" */
    bool guest;          /* Guest's, otherwise Main's */
    double last_active;  /* CLOCK_MONOTONIC seconds of its last press; 0 when never seen */
};

enum swap_choice {
    SWAP_UNAVAILABLE,   /* a side has no control, or no two controls share a backend: no button */
    SWAP_NEEDS_PRESS,   /* a side holds several and none was pressed lately: the button asks for a press */
    SWAP_READY,
};

static bool swap_is_controller(const char *key) {
    return strncmp(key, "controller:", 11) == 0;
}

/* Cross-backend swaps need a broker that owns both paths, so only controls of a backend that both
 * players have are considered. A side with one such control swaps it. A side with several swaps the one
 * pressed last, if that was within SWAP_ACTIVE_SECONDS and no other shares the latest press. */
static enum swap_choice choose_swap_pair(const struct swap_control *controls, int count, double now,
        int *main_index, int *guest_index) {
    int have[2][2] = {{0, 0}, {0, 0}};   /* [is a controller][is Guest's] */
    for (int i = 0; i < count; i++) have[swap_is_controller(controls[i].key)][controls[i].guest]++;
    bool usable[2] = {have[0][0] && have[0][1], have[1][0] && have[1][1]};
    if (!usable[0] && !usable[1]) return SWAP_UNAVAILABLE;
    int chosen[2] = {-1, -1};
    for (int side = 0; side < 2; side++) {
        int members = 0, only = -1, best = -1;
        bool tie = false;
        for (int i = 0; i < count; i++) {
            if (controls[i].guest != (side == 1) || !usable[swap_is_controller(controls[i].key)]) continue;
            members++;
            only = i;
            if (controls[i].last_active <= 0 || now - controls[i].last_active > SWAP_ACTIVE_SECONDS) continue;
            if (best < 0 || controls[i].last_active > controls[best].last_active) {
                best = i;
                tie = false;
            } else if (controls[i].last_active == controls[best].last_active) {
                tie = true;
            }
        }
        chosen[side] = members == 1 ? only : tie ? -1 : best;
    }
    if (chosen[0] < 0 || chosen[1] < 0) return SWAP_NEEDS_PRESS;
    /* Both backends are usable and the last presses were on different ones. */
    if (swap_is_controller(controls[chosen[0]].key) != swap_is_controller(controls[chosen[1]].key))
        return SWAP_NEEDS_PRESS;
    *main_index = chosen[0];
    *guest_index = chosen[1];
    return SWAP_READY;
}
/* === End of swap choice === */

struct side_xdg_window {
    struct wl_list link;
    struct side_ctx *ctx;
    struct wlr_xdg_surface *xdg_surface;
    struct wlr_surface *surface;
    struct wlr_scene_tree *scene_tree;
    struct wl_listener surface_commit;
    struct wl_listener surface_map;
    struct wl_listener surface_unmap;
    struct wl_listener surface_destroy;
    struct wl_listener surface_fullscreen;
    struct wl_listener surface_client_commit; /* before wlroots checks the commit (see handle_xdg_window_client_commit) */
    bool surface_commit_armed;
    bool surface_client_commit_armed;
    bool was_mapped;                        /* the toplevel has mapped at least once */
    unsigned remaps_tolerated;              /* buffers accepted after an unmap without a new configure */
    bool surface_map_armed;
    bool surface_unmap_armed;
    bool surface_destroy_armed;
    bool surface_fullscreen_armed;
    unsigned commits;                       /* commits of this surface since it was assigned */
    struct timespec assigned;               /* CLOCK_MONOTONIC when the toplevel was assigned to the side */
    struct wl_event_source *map_watchdog;   /* fires while the toplevel has not mapped; NULL once it has, or after the last check */
    int map_watchdog_fired;                 /* times the watchdog has logged for this toplevel */
    int scale_sent_milli; /* last preferred scale sent to this surface; 0 while none */
    struct wl_event_source *scale_reconfigure; /* follow-up configure after a preferred scale; NULL while idle */
    /* The scene surface's own outputs_update listener is off its signal while the side's scale is not 1
     * (side_set_scene_scale_held): the scene would send the host output's scale on every output update. */
    bool scale_scene_listener_held;
    bool scale_kept_logged; /* the first output update of a scaled side's surface went to the log at INFO */
    /* Scene output updates and follow-up configures since scale_updates_since (CLOCK_MONOTONIC seconds), for
     * the summary line of side_scale_outputs_update; the totals since the window was made. */
    unsigned scale_updates, scale_resent, scale_updates_total, scale_resent_total;
    double scale_updates_since;
    int scale_followups_left; /* follow-up configures still allowed for the scale last sent (SCALE_FOLLOWUPS) */
    struct wlr_scene_buffer *scale_buffer; /* the surface's scene buffer whose outputs_update we follow; NULL while none */
    struct wl_listener scale_outputs_update;
    struct wl_listener scale_buffer_destroy;
    bool scale_update_logged;
    /* The surfaces of the toplevel's tree, the toplevel's own first and then every subsurface, nested ones included,
     * whose commits are handled here instead of by the scene (a side at a scale other than 1; see "The surface
     * tree"). Empty while the scene handles them. */
    struct wl_list scale_surfaces; /* struct side_scaled_surface */
    bool zoom_applied; /* the scene buffer shows the zoom rectangle (side_window_apply_zoom) */
};

struct side_xwayland_window {
    struct wl_list link;
    struct side_ctx *ctx;
    struct wlr_xwayland_surface *xwayland_handle;
    struct wlr_surface *surface;
    struct wlr_scene_tree *scene_tree;
    struct wlr_scene_surface *scene_surface;
    struct wl_listener surface_commit;
    struct wl_listener surface_unmap;
    struct wl_listener surface_destroy;
    struct wl_listener request_configure;
    bool surface_commit_armed;
    bool surface_unmap_armed;
    bool surface_destroy_armed;
    bool request_configure_armed;
};

struct input_device_ctx {
    struct wl_list link;
    struct compositor *comp;
    struct wlr_input_device *device;
    unsigned route_id; /* Unique for this connection; never match UI routes by name. */
    enum side_role target_role;
    struct wlr_keyboard *keyboard;
    bool held_keys[KEY_MAX + 1];
    unsigned held_key_count;
    /* Where a device that only reports positions (X11 backend, tablets) was last, 0..1 across the
     * area its events map to, so that its motion can be told to clients as relative motion. */
    bool abs_valid;
    double abs_x, abs_y;
    /* A re-centring of the host pointer under a lock (see "Host pointer re-centring under a lock") whose jump
     * has not been reported yet: the jump as a move of the 0..1 position, and the time of the motion that asked
     * for it. One that the start of a lock asked for has no motion to take the time from (abs_warp_untimed): the
     * time of the first position after it is taken instead. */
    bool abs_warp_pending, abs_warp_untimed, abs_warp_confirmed_said, abs_warp_unconfirmed_said;
    double abs_warp_dx, abs_warp_dy;
    uint32_t abs_warp_msec;
    bool keyboard_target_valid;
    enum side_id keyboard_target;
    bool keyboard_wait_neutral;
    bool keyboard_overlay_wait_neutral;
    double last_key_seconds; /* CLOCK_MONOTONIC time of its last key press; 0 until the first. */
    struct wl_listener key;
    struct wl_listener modifiers;
    struct wl_listener button;
    struct wl_listener motion;
    struct wl_listener motion_abs;
    struct wl_listener axis;
    struct wl_listener frame;
    struct wl_listener touch_down;
    struct wl_listener touch_motion;
    struct wl_listener touch_up;
    struct wl_listener touch_cancel;
    struct wl_listener touch_frame;
    struct wl_listener destroy;
    bool key_armed;
    bool modifiers_armed;
    bool button_armed;
    bool motion_armed;
    bool motion_abs_armed;
    bool axis_armed;
    bool frame_armed;
    bool touch_down_armed;
    bool touch_motion_armed;
    bool touch_up_armed;
    bool touch_cancel_armed;
    bool touch_frame_armed;
    bool destroy_armed;
};

/* Per-side context */
struct eis_input;
/* === Per-side performance numbers (T-434) ===
 * Every commit that carries a buffer on a side's toplevel is a frame the side delivers to Dosplex; its monotonic
 * time goes into a ring. FPS, frame times and the percentiles are computed from the ring when they are shown or
 * logged. A side's counter mode and its recording are its own. */
#define PERF_RING 2048
enum fps_mode { FPS_MODE_OFF, FPS_MODE_FPS, FPS_MODE_FRAMETIME };

struct side_perf {
    uint64_t stamp_ns[PERF_RING]; /* commit times; the newest is at head - 1 */
    unsigned head, count;
    uint64_t total;               /* frames since the compositor started */
    enum fps_mode mode;
    bool recording;
    FILE *csv;
    char csv_path[384];
};

struct side_ctx {
    enum side_id id;
    enum side_role role;
    enum side_state state;
    struct logical_output_contract logical_output;
    char owner[64];
    pid_t worker_pid;
    pid_t launcher_pid;
    bool launcher_failed;
    bool launch_pending;
    char launch_token[33], supervisor_instance[33];
    struct timespec launch_started;
    bool recovery_required;
    char wine_notice_instance[33], wine_notice[160];
    uint32_t wine_notice_seq;
    struct timespec wine_notice_started;
    bool wine_notice_timer_started;
    /* The Scaled notice shares the Wine notice's slot and its 8-second timer. It is raised once per revision (see
     * side_note_scaled_notice); wine_notice_is_scaled says that the slot holds it and not the supervisor's notice. */
    bool wine_notice_is_scaled, scaled_notice_given;
    unsigned scaled_notice_revision;
    bool auto_login_suppressed;
    int activity; /* 0 active, 1 standby, 2 off; device ownership is retained. */
    struct auth_job *auth;
    bool auth_error;
    int x, y;
    int width, height;
    /* Pointer position relative to this region, independent of the other side. */
    int pointer_x, pointer_y;
    bool pointer_seen;
    /* EIS pointer input to a nested Gamescope (see "Pointer input for a nested Gamescope"). */
    struct eis_input *eis;
    struct wlr_surface *eis_probe_surface;
    uint32_t eis_probe_msec;
    /* The lock or confinement that this side's client holds on its seat while it is active (NULL
     * otherwise), the listener that notices its end, and how many presses of each pointer button
     * went to the seat and are still held. See "Pointer constraints, relative motion and grabs". */
    struct wlr_pointer_constraint_v1 *pointer_constraint;
    struct wl_listener pointer_constraint_destroy;
    uint8_t buttons_down[POINTER_BUTTON_SLOTS];
    /* The shared pointer left this side while a press that its seat got was still held, so the seat keeps
     * its pointer focus until the release (see pointer_leave_side). */
    bool pointer_leave_pending;
    bool desktop_cursor;
    /* Scale of the Desktop payload's nested KWin, in thousandths (2000 is 200 %).
     * 1000 for every other payload. The launcher chooses it and reports it to the
     * supervisor; session.json carries it here (see side_apply_snapshot_scale). */
    int desktop_scale_milli;
    struct wlr_scene_tree *cursor_tree;
    struct wlr_scene_rect *cursor_rect;
    struct wlr_scene_buffer *cursor_buffer;
    int cursor_hotspot_x, cursor_hotspot_y;

    /* UI state */
    char username[64];
    char password[64];
    int cursor_pos;
    bool show_password;
    enum ui_elem_type focused;
    struct ui_region regions[8];
    int num_regions;

    /* App surface (Wayland or X11 client that logged in on this side) */
    struct wlr_surface  *app_surface;
    struct wlr_scene_tree  *side_tree;
    struct wlr_scene_tree  *app_scene_tree;
    struct wlr_scene_surface *app_xwayland_surface;
    bool app_scene_tree_owned;
    enum output_scale_mode scale_mode;
    enum output_scale_mode pending_scale_mode;
    bool display_pending; /* the pending picture choice differs from the applied one */
    int game_width, game_height; /* zero selects the native side size */
    int pending_game_width, pending_game_height;
    enum game_resolution_choice game_resolution_choice;
    enum game_resolution_choice pending_game_resolution_choice;
    /* The player applied a picture choice for this side. Until then the layout's default decides (see
     * default_game_resolution_choice); after it the choice stays through every layout change. */
    bool game_resolution_explicit;
    /* Counts changes of the live display contract; see side_note_display_contract. */
    unsigned display_revision;
    bool display_published;
    int published_game_width, published_game_height;
    enum output_scale_mode published_scale_mode;
    /* The side supervisor's last report on applying the display contract, read
     * from session.json by decode_session_state. It feeds the Display page, the
     * Native and Scaled words and state.json. */
    bool display_applied_known;
    unsigned display_applied_revision;
    int display_applied_width, display_applied_height; /* the monitor size in effect; 0 until reported */
    enum display_result display_result;
    char display_detail[160];
    bool desktop_payload; /* session.json mode is "desktop": a nested KWin, which has no game monitor */
    struct wl_list xdg_windows;
    struct wl_list xwayland_windows;
    struct side_xdg_window *active_xdg_window;
    struct wlr_xwayland_surface *app_xwayland_handle;
    struct wlr_xwayland_surface *pending_xwayland_surface;
    bool xdg_windows_initialized;
    bool xwayland_windows_initialized;
    bool xwayland_associate_armed;
    bool xwayland_surface_map_armed;
    bool surface_commit_armed;
    bool surface_unmap_armed;
    bool surface_destroy_armed;
    bool xwayland_request_configure_armed;

    /* Logical output (illusion of independent display) */
    struct wlr_output *wlr_output;
    struct wlr_scene_output *scene_output;

    /* Listeners for app surface lifecycle (Wayland and X11) */
    struct wl_listener surface_commit;
    struct wl_listener surface_unmap;
    struct wl_listener surface_destroy;
    struct wl_listener xwayland_associate;
    struct wl_listener xwayland_surface_map;
    struct wl_listener xwayland_request_configure;

    struct wlr_xwayland *xwayland;
    struct wl_listener new_xwayland;
    struct wl_listener xwayland_ready;

    /* Back-pointer to the compositor (needed in app event handlers) */
    struct compositor *comp;

    /* Debug: commit counter */
    int commit_count;

    struct side_perf perf;
};

/* === Runtime configuration ===
 *
 * Every runtime policy comes from this struct, filled once in main() from
 * argv. The backend (-b) only decides how the display is built and never
 * implies a policy: a nested X11 run, a headless test and a development DRM
 * run differ only in the flags their launcher passes. Nothing here depends on
 * the rest of the file, so the block can move to its own module. */
enum idle_policy {
    IDLE_POLICY_NONE,   /* never blank the output or touch the backlight */
    IDLE_POLICY_DIM,    /* switch the output off after the idle timeout */
};

/* When the compositor commits a frame to its output (handle_output_frame). */
enum frame_pacing {
    /* At every frame event, whether or not anything changed: the output's next frame is always scheduled. */
    FRAME_PACING_EVERY_FRAME,
    /* Only when something changed (the scene's damage, a frame callback, request_redraw): an X11 output whose
     * presents carry their own commit's sequence. A client that answers a present event right away is then on
     * the very next host frame, not one later. */
    FRAME_PACING_ON_DEMAND,
    /* Every frame, because the X11 backend's presents come back with the sequence of the commit before
     * (unpatched wlroots 0.17): presentation feedback completes only when the output commits again, so on
     * demand a client that waits for its feedback would wait for a commit that never comes. */
    FRAME_PACING_EVERY_FRAME_LATE_PRESENTS,
};

/* How long the frame handler may hold the next frame back with no frame event coming (release_held_frame). */
#define PACING_KICK_MS 250

/* The frame_pacing value of state.json. */
static const char *frame_pacing_name(enum frame_pacing pacing) {
    switch (pacing) {
    case FRAME_PACING_ON_DEMAND: return "on-demand";
    case FRAME_PACING_EVERY_FRAME_LATE_PRESENTS: return "every-frame-late-presents";
    case FRAME_PACING_EVERY_FRAME: break;
    }
    return "every-frame";
}

/* === Present order === */
/* The presents of an X11 output, counted against its commits.
 *
 * The X11 backend sends one present for each commit made while the output is enabled, tagged with the serial the commit
 * was sent with: the commit_seq the commit gets (the release's patched wlroots 0.17.4) or the one before it (the
 * unpatched 0.17). One present and the output's current commit_seq tell which only while a single commit is in flight,
 * and any commit outside the frame handler (a host resize, the output enabled again) puts a second one in flight,
 * which committing at every frame event then keeps there. The order of the presents does not tell it either: the
 * backend completes a commit without a buffer before an earlier one that has a buffer, and a present that was replaced
 * can be reported after the one that replaced it. Two presents settle it whatever came before them or between them:
 *  - one that carries the output's current commit_seq. A commit that has been made has a sequence the output has
 *    reached, so a backend that tags one behind cannot send it, however presents were lost, repeated or reordered;
 *  - the present of the first commit since the output was enabled, if it carries the sequence before that commit's
 *    own. A backend that tags with the commit's own sequence sends nothing below it, and what is still on its way from
 *    before the output was disabled is further below. That present comes wherever it is in the order, and with any
 *    number of commits in flight.
 * The presents are counted from the output being enabled, so that an output created or enabled again starts a count of
 * its own, and the count gives how many commits are still unanswered: one present comes back for each commit. */
enum present_verdict {
    PRESENT_UNDECIDED,
    PRESENT_OWN_SEQUENCE,  /* presents carry their own commit's sequence */
    PRESENT_ONE_BEHIND,    /* presents carry the sequence of the commit before: the unpatched backend */
};

struct present_order {
    bool counting;    /* the output is enabled and a count is running */
    uint32_t first;   /* the sequence the first commit since the output was enabled got */
    unsigned seen;    /* presents of the commits since then that have come back */
};

/* A count starts with the commit that got the sequence `first`: when the output is created and when a commit
 * enables it again. */
static void present_order_begin(struct present_order *order, uint32_t first) {
    *order = (struct present_order){.counting = true, .first = first};
}

/* The output was disabled. It sends no presents until it is enabled again, and the ones still on their way belong to
 * no count. */
static void present_order_end(struct present_order *order) {
    *order = (struct present_order){0};
}

/* A present of the output, with the serial it carries and the output's current commit_seq. */
static enum present_verdict present_order_note(struct present_order *order, uint32_t serial, uint32_t commit_seq) {
    /* A present of a commit from before the count (the output was disabled with it on its way) is no part of it. */
    bool counted = order->counting && (int32_t)(serial + 1 - order->first) >= 0;
    if (counted) order->seen++;
    if (serial == commit_seq) return PRESENT_OWN_SEQUENCE;
    return counted && serial + 1 == order->first ? PRESENT_ONE_BEHIND : PRESENT_UNDECIDED;
}

/* Whether a present is still outstanding for a commit other than the one the frame handler has just made
 * (`committed`), whose present is the one the next frame event comes with. */
static bool present_order_others_pending(const struct present_order *order, uint32_t commit_seq, bool committed) {
    if (!order->counting) return false;
    int32_t unanswered = (int32_t)(commit_seq - order->first + 1 - order->seen);
    return unanswered > (committed ? 1 : 0);
}
/* === End of present order === */

/* What a Restart or Shut down leads to, as the launcher says (--boots-into). The confirmations say it, because only
 * the launcher knows: after a retained trial or the installed `dosplex start` the machine comes back as normal
 * SteamOS (Dosplex is off until `dosplex start`), after the display-manager installation it comes back as Dosplex. */
enum boots_into {
    BOOTS_INTO_UNKNOWN,   /* not said: the confirmations say nothing about the next boot */
    BOOTS_INTO_STEAMOS,
    BOOTS_INTO_DOSPLEX,
};

static const char *boots_into_name(enum boots_into boots) {
    return boots == BOOTS_INTO_STEAMOS ? "steamos" : boots == BOOTS_INTO_DOSPLEX ? "dosplex" : "unknown";
}

struct dosplex_config {
    const char *backend;          /* -b: headless (default), x11 or drm; read only to build the backend */
    bool allow_nested_sessions;   /* -s: real side sessions, root only; off, the sides stay placeholders */
    bool test_hooks;              /* --test-hooks: DEV_FRAME, DEV_OUTPUT_*, DEV_SIDE_SCALE, DEV_SIDE_DISPLAY, DEV_STATS, DEV_SCENE and screen capture */
    bool allow_power;             /* --allow-power: Sleep, Restart and Shut down act */
    enum boots_into boots_into;   /* --boots-into=steamos|dosplex: what the next boot is, for the confirmations */
    enum idle_policy idle_policy; /* --idle-policy=none|dim */
    bool host_keyboards;          /* --host-keyboards: keyboards that are neither libinput nor X11 devices count as menu controls */
    const char *audio_router;     /* --audio-router, else DOSPLEX_AUDIO_ROUTER: the audio helper; NULL, no audio */
    const char *power_command;    /* DOSPLEX_POWER_COMMAND, only with --test-hooks: the power helper runs it instead of systemctl */
    bool power_command_ignored;   /* DOSPLEX_POWER_COMMAND was set without --test-hooks */
};

enum config_status {
    CONFIG_RUN,          /* start the compositor */
    CONFIG_EXIT_OK,      /* the request was fully served (--help) */
    CONFIG_EXIT_ERROR,   /* bad arguments; the reason is on stderr */
};

enum {
    OPT_TEST_HOOKS = 256,
    OPT_ALLOW_POWER,
    OPT_BOOTS_INTO,
    OPT_IDLE_POLICY,
    OPT_HOST_KEYBOARDS,
    OPT_AUDIO_ROUTER,
};

static const struct option dosplex_long_options[] = {
    {"test-hooks", no_argument, NULL, OPT_TEST_HOOKS},
    {"allow-power", no_argument, NULL, OPT_ALLOW_POWER},
    {"boots-into", required_argument, NULL, OPT_BOOTS_INTO},
    {"idle-policy", required_argument, NULL, OPT_IDLE_POLICY},
    {"host-keyboards", no_argument, NULL, OPT_HOST_KEYBOARDS},
    {"audio-router", required_argument, NULL, OPT_AUDIO_ROUTER},
    {"help", no_argument, NULL, 'h'},
    {NULL, 0, NULL, 0},
};

static const char *idle_policy_name(enum idle_policy policy) {
    return policy == IDLE_POLICY_DIM ? "dim" : "none";
}

static void dosplex_usage(FILE *out, const char *prog) {
    fprintf(out,
        "Usage: %s [options]\n"
        "  -b backend            headless (default), x11 or drm; builds the display and implies no policy\n"
        "  -s                    real side sessions; needs root; without it the sides are placeholders\n"
        "  --test-hooks          accept DEV_FRAME, DEV_OUTPUT_*, DEV_SIDE_SCALE, DEV_SIDE_DISPLAY, DEV_STATS, DEV_SCENE, CHORD and the\n"
        "                        R dx dy relative motion of the input socket, offer the screen-copy protocol, and\n"
        "                        pass $DOSPLEX_POWER_COMMAND to the power helper in place of systemctl (default off;\n"
        "                        the screen-copy protocol is never offered with -s)\n"
        "  --allow-power         Sleep, Restart and Shut down act (default off)\n"
        "  --boots-into=BOOT     what the machine boots into after Restart or Shut down, for the\n"
        "                        confirmations: steamos (normal SteamOS; a trial, `dosplex start`) or\n"
        "                        dosplex (a display manager starts Dosplex); unsaid, they do not say\n"
        "  --idle-policy=POLICY  none (default), or dim: switch the output off after 5 minutes\n"
        "                        without input (DOSPLEX_IDLE_DIM_TIMEOUT_MS changes the time)\n"
        "  --host-keyboards      count keyboards that are neither libinput nor X11 devices as\n"
        "                        menu controls (default off; X11 keyboards never count)\n"
        "  --audio-router=PATH   audio helper; without one audio is unavailable\n"
        "                        (default: $DOSPLEX_AUDIO_ROUTER; an empty PATH turns audio off)\n"
        "  -h, --help            show this text\n",
        prog);
}

/* Fills `config` from argv and returns what main() should do. The options win
 * over `audio_router_env` (DOSPLEX_AUDIO_ROUTER). `euid` is a parameter so the
 * root requirement of -s is decided here, in one place. */
static enum config_status dosplex_config_parse(struct dosplex_config *config,
        int argc, char *argv[], uid_t euid, const char *audio_router_env,
        const char *power_command_env) {
    *config = (struct dosplex_config){
        .backend = "headless",
        .idle_policy = IDLE_POLICY_NONE,
        .audio_router = audio_router_env && audio_router_env[0] ? audio_router_env : NULL,
    };
    int opt;
    while ((opt = getopt_long(argc, argv, "b:sh", dosplex_long_options, NULL)) != -1) {
        switch (opt) {
            case 'b': config->backend = optarg; break;
            case 's':
                if (euid != 0) {
                    fprintf(stderr, "Real nested side sessions require root\n");
                    return CONFIG_EXIT_ERROR;
                }
                config->allow_nested_sessions = true;
                break;
            case OPT_TEST_HOOKS: config->test_hooks = true; break;
            case OPT_ALLOW_POWER: config->allow_power = true; break;
            case OPT_BOOTS_INTO:
                if (strcmp(optarg, "steamos") == 0) {
                    config->boots_into = BOOTS_INTO_STEAMOS;
                } else if (strcmp(optarg, "dosplex") == 0) {
                    config->boots_into = BOOTS_INTO_DOSPLEX;
                } else {
                    fprintf(stderr, "Invalid --boots-into '%s': use steamos or dosplex\n", optarg);
                    return CONFIG_EXIT_ERROR;
                }
                break;
            case OPT_IDLE_POLICY:
                if (strcmp(optarg, "none") == 0) {
                    config->idle_policy = IDLE_POLICY_NONE;
                } else if (strcmp(optarg, "dim") == 0) {
                    config->idle_policy = IDLE_POLICY_DIM;
                } else {
                    fprintf(stderr, "Invalid --idle-policy '%s': use none or dim\n", optarg);
                    return CONFIG_EXIT_ERROR;
                }
                break;
            case OPT_HOST_KEYBOARDS: config->host_keyboards = true; break;
            case OPT_AUDIO_ROUTER: config->audio_router = optarg[0] ? optarg : NULL; break;
            case 'h': dosplex_usage(stdout, argv[0]); return CONFIG_EXIT_OK;
            default: dosplex_usage(stderr, argv[0]); return CONFIG_EXIT_ERROR;
        }
    }
    /* A stand-in for systemctl is a test hook: read once, here, and only with --test-hooks. */
    if (power_command_env && power_command_env[0]) {
        if (config->test_hooks) config->power_command = power_command_env;
        else config->power_command_ignored = true;
    }
    return CONFIG_RUN;
}

/* Main compositor state */

struct dynamic_input_route {
    struct wlr_input_device *device;
    char device_name[256]; /* For logging/display purposes */
    enum input_route_mode mode;
    struct wl_list link;
};
struct audio_device_choice {
    int kind;
    char owner[8];
    char id[256];
    char label[256];
};

struct compositor {
    struct dosplex_config config;
    struct wl_display *display;
    struct wlr_backend *backend;
    struct wlr_backend *logical_backend;
    struct wlr_renderer *renderer;
    struct wlr_allocator *allocator;
    struct wlr_scene *scene;
    struct wlr_seat *seat_primary;
    struct wlr_seat *seat_guest;

    /* DRM session (used only for DRM backend) */
    struct wlr_session *session;
    struct wlr_device *gpu_device;

    /* Side contexts */

    /* HUD overlay */
    struct wlr_surface *hud_surface;
    struct wlr_scene_tree *hud_scene_tree;
    struct wl_listener hud_surface_destroy;
    bool hud_surface_destroy_armed;
    enum side_id hud_target_side;

    /* Dynamic Input Routing */
    int hud_ipc_server_fd;
    struct wl_event_source *hud_ipc_server_source;
    /* The versioned control socket (SOCK_SEQPACKET, fixed commands, root only); see init_control_socket. */
    int control_server_fd;
    struct wl_event_source *control_server_source;
    char control_socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    struct wl_list dynamic_input_routes;

    struct side_ctx left;
    struct side_ctx right;
    enum side_id primary_side_id;
    enum side_id guest_side_id;
    enum side_id guest_physical_side;
    enum layout_mode layout;
    /* DOSPLEX_GUEST_PRELOGIN: log Guest's side in once although the layout is Full screen, so that its
     * supervisor runs and writes its session state while the side sleeps. Cleared when that login has started. */
    bool guest_prelogin;
    enum output_scale_mode output_scale_mode;
    bool management_overlay_visible;
    int overlay_tab;
    int overlay_device_page[2]; /* Devices tab page of each menu owner, by side. */
    int overlay_focus;
    int audio_kind, audio_page, audio_count;
    unsigned audio_revision;
    struct audio_device_choice audio_devices[128];
    char audio_selected[2][2][256];
    char audio_state[2][2][24];
    char audio_message[256];
    char audio_mix_mode[16], audio_mix_effective[16], audio_separation[16];
    bool audio_joined[2];
    pid_t audio_pid;
    int audio_fd, audio_ticks, audio_refresh_ticks;
    char audio_response[140000];
    size_t audio_response_length;
    struct wl_event_source *audio_timer;
    enum modal_action pending_shared_action;
    enum side_id management_overlay_owner;
    struct modal_region modal_regions[64];
    int modal_region_count;
    double modal_scale;
    int modal_origin_x, modal_origin_y;
    enum side_role remote_input_role;
    struct wl_list input_devices;
    unsigned next_input_route_id;
    bool device_move_active;
    char device_move_key[256], device_move_label[256];
    char device_move_other[256], device_message[160];
    char pending_forget_key[40], pending_forget_label[128]; /* The controller that Forget asks about. */
    struct activity_table controller_activity;
    enum input_route_mode device_move_previous, device_move_target;
    enum input_route_mode device_move_other_previous;
    struct wl_event_source *device_move_timer;

    /* Machine power. power_action is "reboot", "poweroff" or "stop" (Turn off Dosplex) from Continue until the helper's
     * escaped unit stops this compositor, or until power_panel_deadline_ms has passed; while it is
     * set a full-screen panel says so, the overlay does not open and no input reaches either side's
     * applications. power_pid is the helper's first stage, reaped by power_timer. */
    char power_helper[512];
    char power_action[16];
    char power_pid_action[16];
    char power_message[96];
    pid_t power_pid;
    int power_ticks;
    double power_started;      /* monotonic_seconds() when power_action was set */
    int power_panel_deadline_ms;
    struct wl_event_source *power_timer;

    /* The panel: Help or Feedback, a program of the menu owner's own account that exists only while it is open.
     * It is known by the connection the compositor made for it (panel_client), never by a name it gives itself.
     * panel_pid is its launcher, a process group of its own; panel_timer reaps it and follows the side's place. */
    char panel_helper[512];
    bool feedback_available;       /* the release carries the feedback form; decided once, at the start */
    char panel_mode[12];
    pid_t panel_pid;
    enum side_id panel_side;
    double panel_started;
    bool panel_closing;
    struct wl_client *panel_client;
    struct wl_listener panel_client_destroy;
    struct wlr_xdg_toplevel *panel_toplevel;
    struct wlr_surface *panel_surface;
    struct wl_listener panel_surface_destroy;
    struct wlr_scene_tree *panel_tree;
    struct wlr_scene_tree *panel_scene_tree;
    int panel_x, panel_y, panel_w, panel_h;
    struct wl_event_source *panel_timer;

    /* Cursor position */
    int cursor_x;
    int cursor_y;
    int primary_touch_id;
    enum side_id primary_touch_side;
    bool primary_touch_active;
    struct input_device_ctx *primary_touch_input;
    bool primary_touch_overlay;

    /* Auth polling */
    struct wl_event_source *auth_poll_timer;
    struct wl_event_source *auto_login_timer;
    struct wl_event_source *idle_dim_timer;
    int idle_dim_timeout_ms;
    int map_watchdog_ms;          /* how long a side's new toplevel may stay unmapped before the watchdog logs and reconfigures it */
    bool dimmed;
    bool backlight_dimmed;
    bool backlight_available;
    int saved_backlight_brightness;
    int max_backlight_brightness;
    int dim_backlight_brightness;

    /* Output */
    struct wlr_output *output;
    struct wlr_output_layout *output_layout;
    struct wlr_scene_output_layout *scene_output_layout;
    int output_width;
    int output_height;

    /* Dirty flag — rebuild scene only when UI state changes */
    bool scene_dirty;
    /* Re-entry guard — prevents nested handle_output_frame calls from double-rebuilding */
    bool in_frame_handler;
    bool frame_retry_pending;
    struct wl_event_source *frame_retry_timer;
    struct wl_event_source *pacing_kick_timer; /* release_held_frame */

    /* Debug frame counter */
    uint64_t frame_count;
    /* Scene statistics, reported by the DEV_STATS test hook: overlay rebuilds, frames that reached the
     * output, output pixels in the damage ring before each of them and the time their commits took. */
    uint64_t scene_rebuilds;
    uint64_t frames_rendered;
    uint64_t damage_pixels;
    uint64_t render_us;
    /* The overlay's full-output background rect of the last rebuild; its visible region says whether the
     * side surfaces above it are taken as opaque. */
    struct wlr_scene_rect *background_rect;

    /* Greeter overlay scene subtree — rebuilt on every dirty frame.
     * App surfaces live in the root tree above this. */
    struct wlr_scene_tree *overlay_tree;
    struct wlr_scene_tree *apps_tree;
    struct wlr_scene_tree *modal_tree;
    struct wlr_scene_tree *cursor_tree;
    /* The FPS counters (T-434): the compositor's own layer, above the sides and below the menu and the cursor. */
    struct wlr_scene_tree *perf_tree;
    struct wl_event_source *perf_timer;
    bool perf_timer_armed;
    char perf_root[256];        /* where the CSV files and the counter settings go */
    char perf_sysfs_root[256];  /* prefix of /proc and /sys for the host sensors; empty in production */
    bool perf_cpu_primed;
    uint64_t perf_cpu_total, perf_cpu_idle;

    /* The built-in screen's brightness (T-436); see screen_brightness_init. */
    bool screen_bl_available;
    char screen_bl_dir[480];       /* /sys/class/backlight/<device>, under screen_bl_sysfs_root */
    char screen_bl_sysfs_root[256]; /* prefix of /sys; empty in production */
    char screen_bl_settings[256];  /* directory of brightness.conf; empty: nothing is remembered */
    int screen_bl_max;
    int screen_bl_percent;         /* what the panel was last set to or read at, 5..100 */
    bool screen_bl_shown;          /* the menu last drawn offered the brightness control (published in the state) */
    bool screen_bl_drawn;          /* the same, while a frame is being built */

    /* Wayland protocol objects */
    struct wlr_compositor *wl_compositor;
    bool output_nested_x11;       /* the output is an X11 window (under the host Gamescope): no direct scan-out */
    struct wl_listener new_client_surface; /* every surface: a frame callback requested in a shown toplevel tree schedules a frame */
    struct wlr_xdg_shell *xdg_shell;
    struct wlr_xdg_output_manager_v1 *xdg_output_manager;
    struct wlr_linux_dmabuf_v1 *linux_dmabuf_v1;
    struct wlr_fractional_scale_manager_v1 *fractional_scale_manager_v1;
    struct wlr_relative_pointer_manager_v1 *relative_pointer_manager_v1;
    struct wlr_pointer_constraints_v1 *pointer_constraints_v1;

    /* Listeners */
    struct wlr_xcursor_manager *cursor_mgr;

    struct wl_listener new_pointer_constraint;
    struct wl_listener pointer_focus_primary;
    struct wl_listener pointer_focus_guest;
    bool pointer_listeners_armed;
    /* The side that the shared pointer, the one a device that follows the cursor carries from side to
     * side, was last over; NULL before it moves. See pointer_motion_to(). */
    struct side_ctx *shared_pointer_side;
    /* The compositor's own connection to the display of the X11 backend, opened by the first re-centring of the host
     * pointer; NULL until then. See "Host pointer re-centring under a lock". */
    struct host_pointer_warp *host_warp;
    /* The arrangement of the sides that apply_layout saw last: a resize keeps it, a new one moves
     * the regions around. */
    bool arrangement_known;
    enum layout_mode known_layout;
    enum side_id known_primary_side, known_guest_side;
    struct wl_listener new_output;
    struct wl_listener output_frame;
    struct wl_listener output_destroy;
    struct wl_listener output_request_state;
    struct wl_listener output_present;
    struct wl_listener output_commit;
    uint32_t capture_requested, capture_submitted, capture_ready, capture_commit_seq;
    bool capture_armed;
    /* Frame pacing (handle_output_frame, handle_output_present). Until a present of the output is known to carry
     * the sequence of its own commit, the compositor commits a frame at every frame event, as it always did. */
    enum frame_pacing frame_pacing;
    struct present_order present_order; /* the X11 output's commits and presents (note_present_sequence) */
    struct wlr_backend *test_output_backend;
    struct wl_listener new_input;
    struct wl_listener pointer_button;
    struct wl_listener pointer_motion;
    struct wl_listener touch_down;
    struct wl_listener touch_motion;
    struct wl_listener touch_up;
    struct wl_listener keyboard_key;
    struct wl_listener keyboard_modifiers;
    struct wl_listener keyboard_destroy;
    struct wl_listener backend_destroy;
    struct wl_listener new_xdg_surface;
    struct wlr_keyboard *keyboard;
    struct wlr_keyboard remote_keyboards[2];
    /* What a seat holds while no physical keyboard is assigned to it: [0] seat0, [1] seat1. */
    struct wlr_keyboard placeholder_keyboards[2];
    struct wlr_input_device *keyboard_device;
    struct wlr_surface *kbd_focus_surface_primary;
    struct wl_listener kbd_focus_surface_destroy_primary;
    bool kbd_focus_surface_destroy_armed_primary;
    struct wlr_surface *kbd_focus_surface_guest;
    struct wl_listener kbd_focus_surface_destroy_guest;
    bool kbd_focus_surface_destroy_armed_guest;
    bool remote_pressed_keys[KEY_MAX + 1];
    bool remote_overlay_wait_neutral;
    bool remote_pressed_buttons[KEY_MAX + 1];
    enum input_route_mode remote_input_route;
    /* Which side remote input (Input Leap) currently targets, based on cursor position */
    struct side_ctx *remote_input_target;
    bool pointer_listener_armed;
    bool touch_listener_armed;
    bool keyboard_listener_armed;
    bool keyboard_modifiers_armed;
    bool keyboard_destroy_armed;
    bool remote_keyboard_initialized;

    /* Remote primary-side input control */
    int primary_input_server_fd;
    int primary_input_client_fd;
    struct wl_event_source *primary_input_server_source;
    struct wl_event_source *primary_input_client_source;
    char primary_input_buf[2048];
    size_t primary_input_buf_len;
};

static const char *side_name(enum side_id id) {
    return id == SIDE_LEFT ? "left" : "right";
}

/* Forward declarations */
static void finish_device_move(struct compositor *comp, bool keep);
static void focus_app_surface(struct compositor *comp, struct wlr_surface *surface);
static void focus_known_side_surface(struct compositor *comp, struct side_ctx *ctx,
        struct wlr_surface *surface, struct wlr_keyboard *kb);
static void launch_side_session(struct side_ctx *ctx, const char *username);
static void configure_side_windows(struct compositor *comp, struct side_ctx *ctx);
static void side_set_desktop_scale(struct side_ctx *ctx, int scale_milli);
static bool handle_primary_input_command(struct compositor *comp, char *line);
static struct side_ctx *primary_side(struct compositor *comp);
static struct side_ctx *guest_side(struct compositor *comp);
static struct side_ctx *side_for_input_route(struct compositor *comp,
        enum input_route_mode mode);
static struct wlr_seat *seat_for_side(struct compositor *comp, enum side_id side);
static void side_ensure_app_keyboard_focus(struct compositor *comp, struct side_ctx *ctx);
static void clamp_to_side(struct side_ctx *side, int *x, int *y);
static void handle_pointer_motion(struct compositor *comp, int x, int y);
static void pointer_motion_to(struct compositor *comp, int x, int y, bool shared);
static void pointer_constraints_sync(struct compositor *comp, struct wlr_seat *seat);
static void pointer_constraints_release_all(struct compositor *comp);
static void host_pointer_recentre_at_lock(struct compositor *comp, struct side_ctx *owner);
static void pointer_release_buttons(struct compositor *comp, struct side_ctx *ctx);
static void pointer_release_remote_buttons(struct compositor *comp, struct side_ctx *preferred);
static void pointer_rehome_after_layout(struct compositor *comp);
static void eis_side_drop(struct side_ctx *side, const char *why);

static const char *side_role_name(enum side_role role) {
    return role == SIDE_ROLE_PRIMARY ? "primary" : "guest";
}

static enum side_role parse_side_role_env(const char *value, enum side_role fallback) {
    if (!value || !value[0]) {
        return fallback;
    }
    if (strcmp(value, "primary") == 0) {
        return SIDE_ROLE_PRIMARY;
    }
    if (strcmp(value, "guest") == 0) {
        return SIDE_ROLE_GUEST;
    }
    return fallback;
}

static const char *side_output_name(const struct side_ctx *ctx) {
    if (!ctx || ctx->logical_output.name[0] == '\0') {
        return "(unset)";
    }
    return ctx->logical_output.name;
}

static void init_side_logical_output(struct side_ctx *ctx,
        const char *name, const char *description,
        int logical_x, int logical_y, int logical_width, int logical_height) {
    if (!ctx) {
        return;
    }

    memset(&ctx->logical_output, 0, sizeof(ctx->logical_output));
    if (name) {
        snprintf(ctx->logical_output.name, sizeof(ctx->logical_output.name),
            "%s", name);
    }
    if (description) {
        snprintf(ctx->logical_output.description,
            sizeof(ctx->logical_output.description), "%s", description);
    }
    ctx->logical_output.logical_x = logical_x;
    ctx->logical_output.logical_y = logical_y;
    ctx->logical_output.logical_width = logical_width;
    ctx->logical_output.logical_height = logical_height;
    ctx->logical_output.scale_milli = 1000;
}

static int side_physical_origin_x(const struct side_ctx *ctx) {
    return ctx ? ctx->x : 0;
}

static int side_physical_origin_y(const struct side_ctx *ctx) {
    return ctx ? ctx->y : 0;
}

static int side_logical_width(const struct side_ctx *ctx) {
    if (!ctx || ctx->logical_output.logical_width <= 0) {
        return ctx ? ctx->width : 0;
    }
    return ctx->logical_output.logical_width;
}

static int side_logical_height(const struct side_ctx *ctx) {
    if (!ctx || ctx->logical_output.logical_height <= 0) {
        return ctx ? ctx->height : 0;
    }
    return ctx->logical_output.logical_height;
}

/* The Desktop payload's scale; 1000 for a side that runs anything else. */
static int side_scale_milli(const struct side_ctx *ctx) {
    return ctx && ctx->desktop_scale_milli >= 1000 ? ctx->desktop_scale_milli : 1000;
}

static int scale_down_milli(int pixels, int scale_milli) {
    long value = ((long)pixels * 1000 + scale_milli / 2) / scale_milli;
    return value < 1 ? 1 : (int)value;
}

/* The size a nested compositor's toplevel is configured to: the side's region
 * divided by its scale. This is the surface (logical) size; the client renders
 * region-sized buffers. Equals the region at scale 1. Pointer and touch mapping
 * keep using side_logical_width/height, which stay in region pixels. */
static int side_toplevel_width(const struct side_ctx *ctx) {
    return scale_down_milli(side_logical_width(ctx), side_scale_milli(ctx));
}

static int side_toplevel_height(const struct side_ctx *ctx) {
    return scale_down_milli(side_logical_height(ctx), side_scale_milli(ctx));
}

static void side_local_to_physical(const struct side_ctx *ctx,
        int local_x, int local_y, int *physical_x, int *physical_y) {
    if (physical_x) {
        *physical_x = side_physical_origin_x(ctx) + local_x;
    }
    if (physical_y) {
        *physical_y = side_physical_origin_y(ctx) + local_y;
    }
}

static enum side_id parse_side_id_env(const char *value, enum side_id fallback) {
    if (!value || !value[0]) {
        return fallback;
    }
    if (strcmp(value, "left") == 0) {
        return SIDE_LEFT;
    }
    if (strcmp(value, "right") == 0) {
        return SIDE_RIGHT;
    }
    return fallback;
}

static struct side_ctx *compositor_side_by_id(struct compositor *comp, enum side_id id) {
    return id == SIDE_LEFT ? &comp->left : &comp->right;
}

static struct side_ctx *primary_side(struct compositor *comp) {
    return compositor_side_by_id(comp, comp->primary_side_id);
}

static struct side_ctx *guest_side(struct compositor *comp) {
    return compositor_side_by_id(comp, comp->guest_side_id);
}

/* The one side a single layout shows: Main in Full screen, Guest in Guest full screen, NULL in a split. */
static struct side_ctx *shown_side(struct compositor *comp) {
    if (comp->layout == LAYOUT_SINGLE) return primary_side(comp);
    if (comp->layout == LAYOUT_SINGLE_GUEST) return guest_side(comp);
    return NULL;
}

/* True when the side's picture is on the screen: both sides in a split, only the shown one in a single layout. */
static bool side_is_shown(struct compositor *comp, const struct side_ctx *side) {
    struct side_ctx *shown = shown_side(comp);
    return !shown || shown == side;
}

/* Main may always ask for the overlay (it is drawn over whatever is shown); Guest only while it is on the screen. */
static bool side_may_use_overlay(struct compositor *comp, const struct side_ctx *side) {
    return side->role == SIDE_ROLE_PRIMARY || side_is_shown(comp, side);
}

/* Splitting the display always shows Guest: a Standby session resumes and an
 * Off session starts again, so Steam presents its signed-in user, onboarding
 * or sign-in screen. Off is only a full-screen choice. */
static void show_guest_for_split(struct compositor *comp) {
    struct side_ctx *guest = guest_side(comp);
    if (guest && guest->activity != 0) {
        guest->activity = 0;
        /* Its application keeps its seat's keyboard focus from here on, keyboard or not. */
        side_ensure_app_keyboard_focus(comp, guest);
    }
}

/* Any layout that shows Main brings it back from the standby Guest full screen put it in. */
static void show_main_for_layout(struct compositor *comp) {
    struct side_ctx *main_side = primary_side(comp);
    if (main_side && main_side->activity != 0) {
        main_side->activity = 0;
        side_ensure_app_keyboard_focus(comp, main_side);
    }
}

static void enter_guest_full_screen(struct compositor *comp);

static struct wlr_seat *seat_for_role(struct compositor *comp, enum side_role role) {
    return role == SIDE_ROLE_PRIMARY ? comp->seat_primary : comp->seat_guest;
}

static struct wlr_seat *seat_for_side(struct compositor *comp, enum side_id side) {
    return seat_for_role(comp, compositor_side_by_id(comp, side)->role);
}

static struct side_ctx *side_for_role(struct compositor *comp, enum side_role role) {
    return role == SIDE_ROLE_PRIMARY ? primary_side(comp) : guest_side(comp);
}

static enum input_route_mode input_route_mode_for_side(struct compositor *comp,
        enum side_id side);
static enum input_route_mode main_input_route_mode(struct compositor *comp);
static enum input_route_mode guest_input_route_mode(struct compositor *comp);
static void write_runtime_state(struct compositor *comp);
static void side_zoom_refresh(struct side_ctx *ctx);
static bool side_zoom_in_effect(const struct side_ctx *ctx);
static void position_side_cursor(struct compositor *comp, struct side_ctx *ctx, int x, int y);
static void request_redraw(struct compositor *comp);

static const char *layout_mode_name(enum layout_mode layout) {
    switch (layout) {
    case LAYOUT_SPLIT_VERTICAL:
        return "vertical";
    case LAYOUT_SPLIT_HORIZONTAL:
        return "horizontal";
    case LAYOUT_SINGLE_GUEST:
        return "guest";
    case LAYOUT_SINGLE:
    default:
        return "single";
    }
}

static const char *guest_placement_name(struct compositor *comp) {
    if (!comp) {
        return "unknown";
    }
    if (comp->layout == LAYOUT_SPLIT_HORIZONTAL) {
        return comp->guest_physical_side == SIDE_LEFT ? "top" : "bottom";
    }
    return side_name(comp->guest_physical_side);
}

static const char *output_scale_mode_name(enum output_scale_mode mode) {
    switch (mode) {
    case OUTPUT_SCALE_STRETCH:
        return "stretch";
    case OUTPUT_SCALE_NATIVE:
        return "native";
    case OUTPUT_SCALE_ZOOM:
        return "zoom";
    case OUTPUT_SCALE_FIT:
    default:
        return "fit";
    }
}

static const char *output_scale_mode_label(enum output_scale_mode mode) {
    switch (mode) {
    case OUTPUT_SCALE_STRETCH:
        return "Stretch";
    case OUTPUT_SCALE_NATIVE:
        return "1:1";
    case OUTPUT_SCALE_ZOOM:
        return "Fit";
    case OUTPUT_SCALE_FIT:
    default:
        return "Keep ratio";
    }
}

static const char *game_resolution_choice_name(enum game_resolution_choice choice) {
    switch (choice) {
    case GAME_RESOLUTION_16_9:
        return "16:9";
    case GAME_RESOLUTION_4_3:
        return "4:3";
    case GAME_RESOLUTION_1_1:
        return "1:1";
    case GAME_RESOLUTION_CUSTOM:
        return "custom";
    case GAME_RESOLUTION_NATIVE:
    default:
        return "native";
    }
}

static enum output_scale_mode parse_output_scale_mode(const char *value,
        enum output_scale_mode fallback) {
    if (!value || !value[0]) {
        return fallback;
    }
    if (strcmp(value, "fit") == 0) {
        return OUTPUT_SCALE_FIT;
    }
    if (strcmp(value, "stretch") == 0) {
        return OUTPUT_SCALE_STRETCH;
    }
    if (strcmp(value, "zoom") == 0) {
        return OUTPUT_SCALE_ZOOM;
    }
    if (strcmp(value, "native") == 0 || strcmp(value, "1:1") == 0 ||
            strcmp(value, "none") == 0 || strcmp(value, "no-scaling") == 0) {
        return OUTPUT_SCALE_NATIVE;
    }
    return fallback;
}

/* === Zoom geometry ===
 * Gamescope lays a game monitor of mon_w x mon_h out inside a region of region_w x region_h with its Fit scaler: scale
 * s = min(region_w / mon_w, region_h / mon_h), size (mon_w * s) x (mon_h * s), centred. Zoom shows the largest
 * rectangle of the region's shape, centred in that monitor, over the whole region. This is that rectangle in monitor
 * pixels. Equal shapes give the whole monitor, which is Fit. */
static void zoom_source_rect(int region_w, int region_h, int mon_w, int mon_h,
        double *x, double *y, double *w, double *h) {
    double mw = mon_w > 0 ? mon_w : 1, mh = mon_h > 0 ? mon_h : 1;
    double rw = region_w > 0 ? region_w : mw, rh = region_h > 0 ? region_h : mh;
    double sw = mw, sh = mh;
    if (mw * rh > mh * rw) {
        sw = mh * rw / rh; /* the monitor is wider than the region: left and right are cropped */
    } else {
        sh = mw * rh / rw; /* taller: top and bottom are cropped */
    }
    *x = (mw - sw) / 2.0;
    *y = (mh - sh) / 2.0;
    *w = sw;
    *h = sh;
}

/* The same rectangle in region pixels, where Gamescope's Fit puts the monitor: the box of the picture that is shown. */
static void zoom_region_box(int region_w, int region_h, int mon_w, int mon_h,
        double *x, double *y, double *w, double *h) {
    double mw = mon_w > 0 ? mon_w : 1, mh = mon_h > 0 ? mon_h : 1;
    double rw = region_w > 0 ? region_w : mw, rh = region_h > 0 ? region_h : mh;
    double scale = rw / mw < rh / mh ? rw / mw : rh / mh;
    double sx, sy, sw, sh;
    zoom_source_rect(region_w, region_h, mon_w, mon_h, &sx, &sy, &sw, &sh);
    *x = (rw - mw * scale) / 2.0 + sx * scale;
    *y = (rh - mh * scale) / 2.0 + sy * scale;
    *w = sw * scale;
    *h = sh * scale;
}

/* Whether a monitor and a region have the same shape, within a pixel of rounding. */
static bool zoom_same_shape(int region_w, int region_h, int mon_w, int mon_h) {
    if (region_w <= 0 || region_h <= 0 || mon_w <= 0 || mon_h <= 0) return true;
    long long cross = (long long)mon_w * region_h - (long long)mon_h * region_w;
    long long limit = region_w > region_h ? region_w : region_h;
    return (cross < 0 ? -cross : cross) <= limit;
}
/* The affine map of Zoom for a surface tree. Gamescope's Wayland backend draws its picture as a tree of surfaces in the
 * toplevel's units (the toplevel's own surface is a 1x1 background stretched over the region, the game monitor is a
 * subsurface that Gamescope's Fit placed, and small ones may sit anywhere). Zoom shows the box Z of that plane over the
 * whole region: a point t of the toplevel's units is drawn at (t - Z.origin) * k, with k = region / Z. A surface
 * rectangle R keeps the part I = R intersected with Z: its source box is I within R, its edges are the mapped edges
 * of I, and a surface whose I is empty is not drawn. An edge is mapped with the position clamped into Z, so the
 * position of a subsurface relative to its parent is the difference of two mapped edges, as it is for a pure scale. */
struct zoom_map {
    double x, y, w, h; /* Z, in the toplevel's units */
    double k;          /* drawn pixels for a unit of the toplevel */
};

struct zoom_surface {
    bool empty;
    double ix, iy, iw, ih;           /* I, in the toplevel's units */
    int left, top, right, bottom;    /* the edges of I as drawn (the toplevel's origin is the region's) */
};

static bool zoom_map_make(int top_w, int top_h, int mon_w, int mon_h, struct zoom_map *map) {
    if (top_w <= 0 || top_h <= 0 || mon_w <= 0 || mon_h <= 0) return false;
    zoom_region_box(top_w, top_h, mon_w, mon_h, &map->x, &map->y, &map->w, &map->h);
    if (map->w <= 0 || map->h <= 0) return false;
    map->k = top_w / map->w;
    return true;
}

/* Pixel-perfect scaling: a scale by a whole number (within 1e-6) samples nearest-neighbour, so each game pixel
 * becomes a crisp block; any other scale keeps the linear filter. */
static enum wlr_scale_filter_mode zoom_scene_filter(double k) {
    double nearest = floor(k + 0.5);
    return nearest >= 1.0 && fabs(k - nearest) < 1e-6 ? WLR_SCALE_FILTER_NEAREST : WLR_SCALE_FILTER_BILINEAR;
}

static int zoom_edge(double origin, double extent, double k, int value) {
    double clamped = value < origin ? origin : value > origin + extent ? origin + extent : value;
    return (int)floor((clamped - origin) * k + 0.5);
}

static int zoom_edge_x(const struct zoom_map *map, int x) { return zoom_edge(map->x, map->w, map->k, x); }
static int zoom_edge_y(const struct zoom_map *map, int y) { return zoom_edge(map->y, map->h, map->k, y); }

static void zoom_surface_map(const struct zoom_map *map, int ox, int oy, int w, int h, struct zoom_surface *out) {
    double x0 = ox > map->x ? ox : map->x, y0 = oy > map->y ? oy : map->y;
    double x1 = ox + w < map->x + map->w ? ox + w : map->x + map->w;
    double y1 = oy + h < map->y + map->h ? oy + h : map->y + map->h;
    out->ix = x0;
    out->iy = y0;
    out->iw = x1 - x0;
    out->ih = y1 - y0;
    out->left = zoom_edge_x(map, ox);
    out->right = zoom_edge_x(map, ox + w);
    out->top = zoom_edge_y(map, oy);
    out->bottom = zoom_edge_y(map, oy + h);
    out->empty = out->iw <= 0 || out->ih <= 0 || out->right <= out->left || out->bottom <= out->top;
}
/* === End of Zoom geometry === */

static const char *input_route_mode_name(enum input_route_mode mode) {
    switch (mode) {
    case INPUT_ROUTE_FOLLOW:
        return "follow";
    case INPUT_ROUTE_LEFT:
        return "left";
    case INPUT_ROUTE_RIGHT:
        return "right";
    case INPUT_ROUTE_BOTH:
    default:
        return "both";
    }
}

static bool input_route_mode_from_string(const char *value,
        enum input_route_mode *mode) {
    if (!value || !mode) {
        return false;
    }
    if (strcmp(value, "follow") == 0 || strcmp(value, "cursor") == 0) {
        *mode = INPUT_ROUTE_FOLLOW;
        return true;
    }
    if (strcmp(value, "left") == 0 || strcmp(value, "top") == 0 ||
            strcmp(value, "main") == 0 || strcmp(value, "primary") == 0) {
        *mode = INPUT_ROUTE_LEFT;
        return true;
    }
    if (strcmp(value, "right") == 0 || strcmp(value, "bottom") == 0 ||
            strcmp(value, "guest") == 0) {
        *mode = INPUT_ROUTE_RIGHT;
        return true;
    }
    if (strcmp(value, "both") == 0 || strcmp(value, "screen") == 0) {
        *mode = INPUT_ROUTE_BOTH;
        return true;
    }
    return false;
}

static void set_primary_side(struct compositor *comp, enum side_id primary) {
    if (!comp) {
        return;
    }

    comp->primary_side_id = primary;
    comp->guest_side_id = primary == SIDE_LEFT ? SIDE_RIGHT : SIDE_LEFT;
    comp->left.role = comp->primary_side_id == SIDE_LEFT ? SIDE_ROLE_PRIMARY : SIDE_ROLE_GUEST;
    comp->right.role = comp->primary_side_id == SIDE_RIGHT ? SIDE_ROLE_PRIMARY : SIDE_ROLE_GUEST;
}

static void set_guest_side(struct compositor *comp, enum side_id guest) {
    if (!comp) {
        return;
    }

    comp->guest_physical_side = guest;
}

static void update_side_logical_dimensions(struct side_ctx *ctx, enum output_scale_mode mode) {
    (void)mode;
    if (!ctx || !ctx->comp) return;
    /* The host Gamescope surface always occupies the side's physical region.
     * Game resolution belongs to the nested monitor inside Gamescope. */
    ctx->logical_output.logical_width = ctx->width;
    ctx->logical_output.logical_height = ctx->height;
    /* Each client sees its own monitor at origin zero. The physical placement
     * is kept separately in ctx->x/y and never becomes a client desktop offset. */
    ctx->logical_output.logical_x = 0;
    ctx->logical_output.logical_y = 0;
    ctx->logical_output.scale_milli = side_scale_milli(ctx);
    if (ctx->wlr_output && ctx->width > 0 && ctx->height > 0) {
        struct wlr_output *output = ctx->wlr_output;
        /* The output keeps the region's pixel size; its scale makes wl_output
         * report the scale and xdg_output the region divided by it. */
        float scale = (float)side_scale_milli(ctx) / 1000.0f;
        bool resize = output->width != ctx->width || output->height != ctx->height;
        bool rescale = fabsf(output->scale - scale) > 0.0005f;
        if (resize || rescale) {
            struct wlr_output_state state;
            wlr_output_state_init(&state);
            if (resize) wlr_output_state_set_custom_mode(&state, ctx->width, ctx->height, 60000);
            if (rescale) wlr_output_state_set_scale(&state, scale);
            if (!wlr_output_commit_state(output, &state)) {
                wlr_log(WLR_ERROR, "Failed to resize %s logical output", side_name(ctx->id));
            }
            wlr_output_state_finish(&state);
        }
        wlr_output_layout_add(ctx->comp->output_layout, output, 0, 0);
    }
}

static void side_set_default_scale(struct side_ctx *ctx, enum output_scale_mode mode) {
    if (!ctx) {
        return;
    }
    ctx->scale_mode = mode;
    ctx->pending_scale_mode = mode;
    ctx->display_pending = false;
    update_side_logical_dimensions(ctx, mode);
}

static enum output_scale_mode effective_side_scale_mode(const struct side_ctx *ctx) {
    if (!ctx) {
        return OUTPUT_SCALE_FIT;
    }
    return ctx->display_pending ? ctx->scale_mode : ctx->pending_scale_mode;
}

static void set_layout_default_scale_modes(struct compositor *comp) {
    if (!comp) {
        return;
    }

    side_set_default_scale(&comp->left, OUTPUT_SCALE_FIT);
    side_set_default_scale(&comp->right, OUTPUT_SCALE_FIT);
}

/* === Game picture choices === */

/* Native is every pixel of the player's region. These ratio choices are forced
 * shapes for compatibility. The table order breaks ties when two choices fill
 * a region equally. */
struct game_ratio {
    enum game_resolution_choice choice;
    enum modal_action action;
    int width, height;
};

static const struct game_ratio game_ratios[] = {
    { GAME_RESOLUTION_16_9, MODAL_ACTION_RESOLUTION_16_9, 16, 9 },
    { GAME_RESOLUTION_4_3, MODAL_ACTION_RESOLUTION_4_3, 4, 3 },
    { GAME_RESOLUTION_1_1, MODAL_ACTION_RESOLUTION_1_1, 1, 1 },
};
enum { GAME_RATIO_COUNT = sizeof(game_ratios) / sizeof(game_ratios[0]) };

/* The choice a side uses until its player makes one: Native in Full screen, the largest 16:9 that fits in
 * either split. A game then sees the TV's ratio in every layout, so going between layouts is a scale or a
 * relaunch at the new size, never a narrow portrait picture with bars. */
static enum game_resolution_choice default_game_resolution_choice(enum layout_mode layout) {
    return layout == LAYOUT_SINGLE || layout == LAYOUT_SINGLE_GUEST ? GAME_RESOLUTION_NATIVE : GAME_RESOLUTION_16_9;
}

/* The largest size of the choice's ratio that fits the region; 1:1 is the
 * largest square, min(width, height). Native advertises 0x0, the region size. */
static bool game_resolution_for_choice(int region_width, int region_height,
        enum game_resolution_choice choice, int *width, int *height) {
    if (!width || !height || region_width <= 0 || region_height <= 0) return false;
    if (choice == GAME_RESOLUTION_NATIVE) {
        *width = 0;
        *height = 0;
        return true;
    }
    const struct game_ratio *ratio = NULL;
    for (int i = 0; i < GAME_RATIO_COUNT && !ratio; i++) {
        if (game_ratios[i].choice == choice) ratio = &game_ratios[i];
    }
    if (!ratio) return false;
    int scale_x = region_width / ratio->width;
    int scale_y = region_height / ratio->height;
    int scale = scale_x < scale_y ? scale_x : scale_y;
    if (scale < 1) return false;
    *width = ratio->width * scale;
    *height = ratio->height * scale;
    return true;
}

/* The ratio choices for a region, ordered by descending fill of its pixels.
 * Equal fills keep the table order. The fill comes from the actual region, so
 * a new ratio in the table needs no ordering rule of its own. */
static void game_ratios_by_fill(int region_width, int region_height,
        const struct game_ratio *order[GAME_RATIO_COUNT]) {
    long long fill[GAME_RATIO_COUNT] = {0};
    for (int i = 0; i < GAME_RATIO_COUNT; i++) {
        int width = 0, height = 0;
        long long area = game_resolution_for_choice(region_width, region_height,
            game_ratios[i].choice, &width, &height) ? (long long)width * height : 0;
        int slot = i;
        while (slot > 0 && fill[slot - 1] < area) {
            fill[slot] = fill[slot - 1];
            order[slot] = order[slot - 1];
            slot--;
        }
        fill[slot] = area;
        order[slot] = &game_ratios[i];
    }
}

/* A ratio button's label: the ratio and the size it gives, e.g. "1:1 1920x1920". */
static void game_ratio_label(const struct game_ratio *ratio, int region_width,
        int region_height, char *label, size_t label_size) {
    int width = 0, height = 0;
    if (game_resolution_for_choice(region_width, region_height, ratio->choice, &width, &height)) {
        snprintf(label, label_size, "%s %dx%d", game_resolution_choice_name(ratio->choice),
            width, height);
    } else {
        snprintf(label, label_size, "%s", game_resolution_choice_name(ratio->choice));
    }
}

/* === End of game picture choices === */

static void refresh_side_game_resolution(struct side_ctx *ctx) {
    if (!ctx) return;
    /* A side whose player has applied no choice follows the layout's default. A choice that is selected and
     * not applied yet stays where the player put it. */
    if (ctx->comp && !ctx->game_resolution_explicit) {
        enum game_resolution_choice wanted = default_game_resolution_choice(ctx->comp->layout);
        if (ctx->pending_game_resolution_choice == ctx->game_resolution_choice) {
            ctx->pending_game_resolution_choice = wanted;
        }
        ctx->game_resolution_choice = wanted;
    }
    int width, height;
    if (game_resolution_for_choice(ctx->width, ctx->height,
            ctx->game_resolution_choice, &width, &height)) {
        ctx->game_width = width;
        ctx->game_height = height;
    }
    if (game_resolution_for_choice(ctx->width, ctx->height,
            ctx->pending_game_resolution_choice, &width, &height)) {
        ctx->pending_game_width = width;
        ctx->pending_game_height = height;
    }
    ctx->display_pending = ctx->scale_mode != ctx->pending_scale_mode ||
        ctx->game_width != ctx->pending_game_width ||
        ctx->game_height != ctx->pending_game_height ||
        ctx->game_resolution_choice != ctx->pending_game_resolution_choice;
}

static void update_display_pending(struct side_ctx *ctx) {
    ctx->display_pending = ctx->scale_mode != ctx->pending_scale_mode ||
        ctx->game_width != ctx->pending_game_width || ctx->game_height != ctx->pending_game_height ||
        ctx->game_resolution_choice != ctx->pending_game_resolution_choice;
    write_runtime_state(ctx->comp);
    side_zoom_refresh(ctx); /* a scale mode that was just applied shows at once */
    request_redraw(ctx->comp);
}

static void set_side_scale_mode(struct side_ctx *ctx, enum output_scale_mode mode) {
    if (!ctx) return;
    if (mode == OUTPUT_SCALE_NATIVE) {
        ctx->pending_game_width = ctx->pending_game_height = 0;
        ctx->pending_game_resolution_choice = GAME_RESOLUTION_NATIVE;
        ctx->pending_scale_mode = OUTPUT_SCALE_FIT;
    } else {
        ctx->pending_scale_mode = mode;
    }
    update_display_pending(ctx);
}

static void set_side_game_resolution(struct side_ctx *ctx, int width, int height) {
    if (!ctx) return;
    ctx->pending_game_resolution_choice = GAME_RESOLUTION_CUSTOM;
    ctx->pending_game_width = width;
    ctx->pending_game_height = height;
    update_display_pending(ctx);
}

static void set_side_game_aspect(struct side_ctx *ctx, enum game_resolution_choice choice) {
    if (!ctx || choice == GAME_RESOLUTION_CUSTOM) return;
    int width, height;
    if (!game_resolution_for_choice(ctx->width, ctx->height, choice, &width, &height)) return;
    ctx->pending_game_resolution_choice = choice;
    ctx->pending_game_width = width;
    ctx->pending_game_height = height;
    update_display_pending(ctx);
}

/* Apply commits the pending choice. It never restarts the side: the committed
 * values are published, and display_revision advances (once) when they change
 * the side's effective game monitor size or scaler. Applying another choice
 * than the one in use makes it the player's own, which no layout change moves. */
static void apply_side_display(struct side_ctx *ctx) {
    if (!ctx || !ctx->display_pending) return;
    if (ctx->pending_game_resolution_choice != ctx->game_resolution_choice) ctx->game_resolution_explicit = true;
    ctx->scale_mode = ctx->pending_scale_mode;
    ctx->game_width = ctx->pending_game_width;
    ctx->game_height = ctx->pending_game_height;
    ctx->game_resolution_choice = ctx->pending_game_resolution_choice;
    update_display_pending(ctx);
}

static void cancel_side_display(struct side_ctx *ctx) {
    if (!ctx) return;
    ctx->pending_scale_mode = ctx->scale_mode;
    ctx->pending_game_width = ctx->game_width;
    ctx->pending_game_height = ctx->game_height;
    ctx->pending_game_resolution_choice = ctx->game_resolution_choice;
    update_display_pending(ctx);
}

static void remote_keyboard_led_update(struct wlr_keyboard *keyboard, uint32_t leds) {
    (void)keyboard;
    (void)leds;
}

static const struct wlr_keyboard_impl remote_keyboard_impl = {
    .name = "dosplex-remote-keyboard",
    .led_update = remote_keyboard_led_update,
};

static bool init_remote_keyboard(struct compositor *comp) {
    if (!comp || comp->remote_keyboard_initialized) {
        return true;
    }

    for (size_t i = 0; i < 2; i++) {
        wlr_keyboard_init(&comp->remote_keyboards[i], &remote_keyboard_impl,
            "dosplex-remote-keyboard");
        wlr_keyboard_init(&comp->placeholder_keyboards[i], &remote_keyboard_impl,
            "dosplex-placeholder-keyboard");
    }

    struct xkb_context *xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (!xkb_context) {
        wlr_log(WLR_ERROR, "Failed to create remote keyboard xkb context");
        return false;
    }
    struct xkb_keymap *keymap = xkb_keymap_new_from_names(
        xkb_context, NULL, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (!keymap) {
        xkb_context_unref(xkb_context);
        wlr_log(WLR_ERROR, "Failed to create remote keyboard keymap");
        return false;
    }

    bool ok = true;
    for (size_t i = 0; i < 2; i++) {
        ok = wlr_keyboard_set_keymap(&comp->remote_keyboards[i], keymap) && ok;
        wlr_keyboard_set_repeat_info(&comp->remote_keyboards[i], 25, 400);
        ok = wlr_keyboard_set_keymap(&comp->placeholder_keyboards[i], keymap) && ok;
        wlr_keyboard_set_repeat_info(&comp->placeholder_keyboards[i], 25, 400);
    }
    xkb_keymap_unref(keymap);
    xkb_context_unref(xkb_context);

    comp->remote_keyboard_initialized = ok;
    if (!ok) {
        wlr_log(WLR_ERROR, "Failed to initialize remote keyboard keymap");
        return false;
    }

    wlr_log(WLR_INFO, "Initialized Dosplex remote keyboard");
    return true;
}

static struct wlr_keyboard *remote_keyboard(struct compositor *comp, struct side_ctx *ctx) {
    if (!comp || !comp->remote_keyboard_initialized) {
        return NULL;
    }
    return &comp->remote_keyboards[ctx->id];
}

static void update_remote_modifiers(struct wlr_keyboard *kb) {
    kb->modifiers.depressed = xkb_state_serialize_mods(kb->xkb_state, XKB_STATE_MODS_DEPRESSED);
    kb->modifiers.latched = xkb_state_serialize_mods(kb->xkb_state, XKB_STATE_MODS_LATCHED);
    kb->modifiers.locked = xkb_state_serialize_mods(kb->xkb_state, XKB_STATE_MODS_LOCKED);
    kb->modifiers.group = xkb_state_serialize_layout(kb->xkb_state, XKB_STATE_LAYOUT_EFFECTIVE);
}

static bool remote_keys_held(struct compositor *comp) {
    for (size_t key = 0; key <= KEY_MAX; key++) {
        if (comp->remote_pressed_keys[key]) return true;
    }
    return false;
}

static struct side_ctx *resolve_remote_input_target(struct compositor *comp);

static bool side_has_app_for_surface(struct side_ctx *ctx, struct wlr_surface *surface);
static void request_redraw(struct compositor *comp);
static void side_clear_xdg_windows(struct side_ctx *ctx);
static struct side_ctx *side_at_point(struct compositor *comp, int x, int y);
static void write_runtime_state(struct compositor *comp);

static void handle_kbd_focus_surface_destroy_primary(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, kbd_focus_surface_destroy_primary);
    wlr_log(WLR_INFO, "Primary keyboard focus surface destroyed");
    comp->kbd_focus_surface_primary = NULL;
    comp->kbd_focus_surface_destroy_armed_primary = false;
}

static void handle_kbd_focus_surface_destroy_guest(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, kbd_focus_surface_destroy_guest);
    wlr_log(WLR_INFO, "Guest keyboard focus surface destroyed");
    comp->kbd_focus_surface_guest = NULL;
    comp->kbd_focus_surface_destroy_armed_guest = false;
}

static void set_kbd_focus_surface(struct compositor *comp, struct wlr_seat *seat, struct wlr_surface *surface) {
    if (!comp || !seat) return;
    
    struct wlr_surface **focus_ptr;
    struct wl_listener *listener_ptr;
    bool *armed_ptr;
    void (*handler)(struct wl_listener *, void *);

    if (seat == comp->seat_primary) {
        focus_ptr = &comp->kbd_focus_surface_primary;
        listener_ptr = &comp->kbd_focus_surface_destroy_primary;
        armed_ptr = &comp->kbd_focus_surface_destroy_armed_primary;
        handler = handle_kbd_focus_surface_destroy_primary;
    } else {
        focus_ptr = &comp->kbd_focus_surface_guest;
        listener_ptr = &comp->kbd_focus_surface_destroy_guest;
        armed_ptr = &comp->kbd_focus_surface_destroy_armed_guest;
        handler = handle_kbd_focus_surface_destroy_guest;
    }

    if (*armed_ptr) {
        wl_list_remove(&listener_ptr->link);
        *armed_ptr = false;
    }
    *focus_ptr = surface;
    if (surface) {
        listener_ptr->notify = handler;
        wl_signal_add(&surface->events.destroy, listener_ptr);
        *armed_ptr = true;
    }
}

/* The keyboard a seat holds while no physical keyboard is assigned to it. Nothing is ever pressed on
 * it, but it gives the seat a keymap and lets its application hold keyboard focus: a nested
 * Gamescope that never gets wl_keyboard.enter leaves Steam's window unfocused, and Steam's UI then
 * loses all DOM focus (the Play button stays grey). */
static struct wlr_keyboard *seat_placeholder_keyboard(struct compositor *comp, struct wlr_seat *seat) {
    if (!comp || !seat || !comp->remote_keyboard_initialized) return NULL;
    return &comp->placeholder_keyboards[seat == comp->seat_primary ? 0 : 1];
}

static void seat_ensure_keyboard(struct compositor *comp, struct wlr_seat *seat) {
    if (seat && !wlr_seat_get_keyboard(seat)) {
        wlr_seat_set_keyboard(seat, seat_placeholder_keyboard(comp, seat));
    }
}

static struct side_ctx *side_for_seat(struct compositor *comp, struct wlr_seat *seat);
static void side_ensure_app_keyboard_focus(struct compositor *comp, struct side_ctx *ctx);

/* A physical keyboard may follow a pointer or be reassigned, but the old
 * seat must relinquish its keyboard association and application focus. It falls back to its
 * placeholder and its application takes focus again, with no keys held. */
static void assign_seat_keyboard(struct compositor *comp, struct wlr_seat *seat,
        struct wlr_keyboard *keyboard) {
    struct wlr_seat *other = seat == comp->seat_primary ? comp->seat_guest : comp->seat_primary;
    if (keyboard && other && wlr_seat_get_keyboard(other) == keyboard) {
        wlr_seat_keyboard_notify_clear_focus(other);
        set_kbd_focus_surface(comp, other, NULL);
        wlr_seat_set_keyboard(other, seat_placeholder_keyboard(comp, other));
        wlr_seat_set_keyboard(seat, keyboard);
        side_ensure_app_keyboard_focus(comp, side_for_seat(comp, other));
        return;
    }
    wlr_seat_set_keyboard(seat, keyboard ? keyboard : seat_placeholder_keyboard(comp, seat));
}

static bool side_owns_keyboard_focus(struct compositor *comp, struct side_ctx *ctx) {
    if (!comp || !ctx) return false;
    struct wlr_surface *focus = seat_for_side(comp, ctx->id) == comp->seat_primary ?
        comp->kbd_focus_surface_primary : comp->kbd_focus_surface_guest;
    return focus && side_has_app_for_surface(ctx, focus);
}

/* Lets go of what the remote input holds: its keys on `ctx` and, if asked, its buttons. A remote
 * pointer that only moves to the other side keeps its buttons down (the press stays with the seat
 * that got it until the remote releases); a closed connection or a vanished side does not. */
static void release_remote_input_for_side(struct compositor *comp,
        struct side_ctx *ctx, const char *reason, bool include_buttons) {
    if (!comp || !ctx) {
        return;
    }

    struct side_ctx *remote_target = comp->remote_input_target ? comp->remote_input_target : side_for_role(comp, comp->remote_input_role);
    if (remote_target != ctx) {
        return;
    }

    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    struct wlr_keyboard *kb = remote_keyboard(comp, ctx);
    if (include_buttons) {
        pointer_release_remote_buttons(comp, ctx);
    }
    if (seat) {
        if (kb) {
            assign_seat_keyboard(comp, seat, kb);
            for (size_t key = 0; key <= KEY_MAX; ++key) {
                if (!comp->remote_pressed_keys[key]) {
                    continue;
                }
                xkb_state_update_key(kb->xkb_state, (xkb_keycode_t)key + 8,
                    XKB_KEY_UP);
                wlr_seat_keyboard_notify_key(seat, 0,
                    (uint32_t)key, WL_KEYBOARD_KEY_STATE_RELEASED);
                comp->remote_pressed_keys[key] = false;
            }
            update_remote_modifiers(kb);
            wlr_seat_keyboard_notify_modifiers(seat, &kb->modifiers);
        } else {
            memset(comp->remote_pressed_keys, 0, sizeof(comp->remote_pressed_keys));
        }
    } else {
        memset(comp->remote_pressed_keys, 0, sizeof(comp->remote_pressed_keys));
    }

    comp->remote_overlay_wait_neutral = false;
    wlr_log(WLR_INFO, "Released remote input for %s side: %s",
        side_name(ctx->id), reason ? reason : "side lifecycle change");
}

static void clear_side_focus_on_lifecycle_change(struct compositor *comp,
        struct side_ctx *ctx, const char *reason) {
    if (!comp || !ctx) {
        return;
    }

    release_remote_input_for_side(comp, ctx, reason, true);

    /* A side lifecycle change must leave the other seat's focus intact. */
    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    if (seat) {
        struct wlr_surface *focused_surface =
            seat->pointer_state.focused_surface;
        if (focused_surface && side_has_app_for_surface(ctx, focused_surface)) {
            pointer_release_buttons(comp, ctx);
            wlr_seat_pointer_notify_clear_focus(seat);
        }
    }
    if (side_owns_keyboard_focus(comp, ctx)) {
        wlr_seat_keyboard_notify_clear_focus(seat);
        set_kbd_focus_surface(comp, seat, NULL);
    }
    side_clear_xdg_windows(ctx);
    if (ctx->active_xdg_window) {
        ctx->active_xdg_window = NULL;
    }
    ctx->app_surface = NULL;
    write_runtime_state(comp);
    request_redraw(comp);

    wlr_log(WLR_INFO, "Cleared stale focus/bookkeeping for %s side: %s",
        side_name(ctx->id), reason ? reason : "side lifecycle change");
}

static void side_init_xdg_windows(struct side_ctx *ctx) {
    if (!ctx || ctx->xdg_windows_initialized) {
        return;
    }

    wl_list_init(&ctx->xdg_windows);
    ctx->xdg_windows_initialized = true;
}

static void side_init_xwayland_windows(struct side_ctx *ctx) {
    if (!ctx || ctx->xwayland_windows_initialized) {
        return;
    }

    wl_list_init(&ctx->xwayland_windows);
    ctx->xwayland_windows_initialized = true;
}

static bool is_usable_keyboard_device(const char *name) {
    if (!name || !name[0]) {
        return false;
    }

    static const char *ignored[] = {
        "Video Bus",
        "Power Button",
        "Lid Switch",
        "Headset Jack",
    };

    for (size_t i = 0; i < sizeof(ignored) / sizeof(ignored[0]); ++i) {
        if (strstr(name, ignored[i]) != NULL) {
            return false;
        }
    }

    return true;
}

static bool env_list_matches_device(const char *env_name, const char *device_name) {
    const char *rules = getenv(env_name);
    if (!rules || !rules[0] || !device_name || !device_name[0]) {
        return false;
    }

    char *copy = strdup(rules);
    if (!copy) {
        return false;
    }

    bool matched = false;
    char *saveptr = NULL;
    for (char *token = strtok_r(copy, ",", &saveptr);
            token != NULL;
            token = strtok_r(NULL, ",", &saveptr)) {
        while (*token && isspace((unsigned char)*token)) {
            token++;
        }
        char *end = token + strlen(token);
        while (end > token && isspace((unsigned char)end[-1])) {
            *--end = '\0';
        }
        if (*token && strstr(device_name, token) != NULL) {
            matched = true;
            break;
        }
    }

    free(copy);
    return matched;
}

static enum side_role default_input_role_for_device(struct wlr_input_device *dev) {
    const char *name = dev && dev->name ? dev->name : "";

    if (env_list_matches_device("DOSPLEX_INPUT_GUEST_DEVICES", name)) {
        return SIDE_ROLE_GUEST;
    }
    if (env_list_matches_device("DOSPLEX_INPUT_PRIMARY_DEVICES", name)) {
        return SIDE_ROLE_PRIMARY;
    }

    const char *default_role = getenv("DOSPLEX_INPUT_DEFAULT_ROLE");
    if (default_role && strcmp(default_role, "guest") == 0) {
        return SIDE_ROLE_GUEST;
    }

    return SIDE_ROLE_PRIMARY;
}

/* === Child process hygiene ===
 * Forked helpers must exec with the default signal state, not the
 * compositor's. wl_event_loop_add_signal() blocks SIGINT and SIGTERM in the
 * loop thread for signalfd, and children inherited that mask (plus inherited
 * ignores) across exec, so the side supervisor's SIGTERM handler never ran
 * (CMP-001). The compositor itself ignores no signals deliberately, so the
 * default disposition for every catchable signal is the correct child state.
 * Call this first in every child branch, before any other work. Only
 * async-signal-safe calls; no allocation, no compositor-struct access. */
static void spawn_child_reset(void) {
    sigset_t empty;
    sigemptyset(&empty);
    sigprocmask(SIG_SETMASK, &empty, NULL);
    for (int sig = 1; sig < NSIG; sig++) {
        if (sig == SIGKILL || sig == SIGSTOP) continue;
        signal(sig, SIG_DFL);
    }
}

/* === Auth helper === */
#ifndef AUTH_HELPER_TIMEOUT_MS
#define AUTH_HELPER_TIMEOUT_MS 30000
#endif

struct auth_job {
    struct side_ctx *ctx;
    struct wl_event_source *timer;
    pid_t pid;
    int fd;
    bool failed, eof;
    size_t output_bytes;
    struct timespec started;
    char action[16], username[64], instance[33];
};

static void auth_finished(struct side_ctx *ctx, const char *action,
        const char *username, bool success);

static void auth_cancel(struct auth_job *job) {
    if (!job || job->failed) return;
    job->failed = true;
    kill(-job->pid, SIGKILL);
    kill(job->pid, SIGKILL);
}

static int auth_poll(void *data) {
    struct auth_job *job = data;
    struct timespec now;
    if (!job->failed) {
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0 ||
                (now.tv_sec - job->started.tv_sec) * 1000 +
                (now.tv_nsec - job->started.tv_nsec) / 1000000 >= AUTH_HELPER_TIMEOUT_MS) {
            auth_cancel(job);
        }
        /* Bound work per dispatch as well as total output. Neither a slow
         * helper nor a flood can monopolize the compositor event loop. */
        for (int i = 0; i < 16 && !job->failed && !job->eof; i++) {
            char buf[4096];
            ssize_t n = read(job->fd, buf, sizeof(buf));
            if (n > 0) {
                job->output_bytes += (size_t)n;
                explicit_bzero(buf, sizeof(buf));
                if (job->output_bytes > 65536) auth_cancel(job);
            } else if (n == 0) {
                job->eof = true;
            } else {
                if (errno != EAGAIN && errno != EINTR) auth_cancel(job);
                break;
            }
        }
    }
    siginfo_t info = {0};
    int observed = waitid(P_PID, job->pid, &info, WEXITED | WNOHANG | WNOWAIT);
    bool lost_child = observed < 0 && errno == ECHILD;
    if (lost_child || (observed == 0 && info.si_pid == job->pid && (job->eof || job->failed))) {
        int status = 0;
        pid_t waited = lost_child ? -1 : waitpid(job->pid, &status, WNOHANG);
        bool success = waited == job->pid && !job->failed &&
            WIFEXITED(status) && WEXITSTATUS(status) == 0;
        close(job->fd);
        wl_event_source_remove(job->timer);
        job->ctx->auth = NULL;
        auth_finished(job->ctx, job->action, job->username, success);
        free(job);
        return 0;
    }
    wl_event_source_timer_update(job->timer, 10);
    return 0;
}

static bool auth_spawn(struct auth_job *job, const char *action,
        const char *username, const char *password) {
    struct side_ctx *ctx = job->ctx;
    int pipefd[2];
    if (pipe2(pipefd, O_CLOEXEC) != 0) {
        wlr_log(WLR_ERROR, "Failed to create auth helper pipe");
        return false;
    }

    int secretfd[2];
    if (pipe2(secretfd, O_CLOEXEC) != 0) {
        close(pipefd[0]); close(pipefd[1]);
        return false;
    }
    size_t length = strcmp(action, "login") == 0 && password ? strlen(password) : 0;
    if (length > 4096 || (length && write(secretfd[1], password, length) != (ssize_t)length)) {
        close(secretfd[0]); close(secretfd[1]);
        close(pipefd[0]); close(pipefd[1]);
        return false;
    }
    close(secretfd[1]);

    pid_t pid = fork();
    if (pid < 0) {
        close(secretfd[0]);
        close(pipefd[0]);
        close(pipefd[1]);
        wlr_log(WLR_ERROR, "Failed to fork auth helper");
        return false;
    }

    if (pid == 0) {
        spawn_child_reset();
        /* Keep cancellation scoped to this helper and its descendants. */
        if (setpgid(0, 0) != 0) _exit(127);
        if (dup2(secretfd[0], STDIN_FILENO) < 0) _exit(127);
        close(secretfd[0]);
        dup2(pipefd[1], STDOUT_FILENO);
        dup2(pipefd[1], STDERR_FILENO);
        close(pipefd[0]);
        close(pipefd[1]);

        /* Pass the compositor's XDG_RUNTIME_DIR so the auth helper knows
         * where the Wayland socket lives (it defaults to /run/user/0 for
         * root, but we may be running with /run/user/1000). */
        const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
        if (runtime_dir) {
            setenv("DOSPLEX_RUNTIME_DIR", runtime_dir, 1);
        }

        const char *side = ctx->id == SIDE_LEFT ? "left" : "right";
        /* Logout carries the identity observed by this compositor. */
        const char *instance = strcmp(action, "logout") == 0 ? job->instance : NULL;
        /* Secrets travel only through stdin, never argv or the environment. */
        if (geteuid() == 0) {
            execl(auth_helper_path(), auth_helper_path(), action, side, username, instance, (char *)NULL);
        } else {
            execlp("sudo", "sudo", "-n", auth_helper_path(), action, side, username, instance, (char *)NULL);
        }
        _exit(127);
    }

    close(secretfd[0]);
    close(pipefd[1]);

    setpgid(pid, pid);
    job->pid = pid;
    job->fd = pipefd[0];
    if (clock_gettime(CLOCK_MONOTONIC, &job->started) != 0 ||
            fcntl(job->fd, F_SETFL, O_NONBLOCK) < 0) auth_cancel(job);
    return true;
}

/* Success means accepted, never authenticated. Only auth_finished commits UI
 * state or launches a session, using the immutable username captured here. */
static bool call_auth_helper(struct side_ctx *ctx, const char *action,
        const char *username, const char *password) {
    if (!ctx->comp->config.allow_nested_sessions || ctx->auth) return false;
    bool logout = strcmp(action, "logout") == 0;
    if (logout && (!ctx->owner[0] || !ctx->supervisor_instance[0])) {
        ctx->auth_error = true;
        return false;
    }
    struct auth_job *job = calloc(1, sizeof(*job));
    if (!job) { ctx->auth_error = true; return false; }
    job->ctx = ctx;
    snprintf(job->action, sizeof(job->action), "%s", action);
    snprintf(job->username, sizeof(job->username), "%s", logout ? ctx->owner : (username ? username : ""));
    if (logout) snprintf(job->instance, sizeof(job->instance), "%s", ctx->supervisor_instance);
    job->timer = wl_event_loop_add_timer(wl_display_get_event_loop(ctx->comp->display), auth_poll, job);
    if (!job->timer || !auth_spawn(job, action, job->username, password)) {
        if (job->timer) wl_event_source_remove(job->timer);
        free(job);
        ctx->auth_error = true;
        return false;
    }
    ctx->auth = job;
    ctx->auth_error = false;
    wl_event_source_timer_update(job->timer, 1);
    return true;
}

/* Shutdown never runs completion actions. SIGKILL is scoped to this helper;
 * reaping is nonblocking, with final orphan reaping left to init on process exit. */
static void destroy_auth(struct side_ctx *ctx) {
    struct auth_job *job = ctx->auth;
    if (!job) return;
    auth_cancel(job);
    wl_event_source_remove(job->timer);
    close(job->fd);
    waitpid(job->pid, NULL, WNOHANG);
    free(job);
    ctx->auth = NULL;
}

/* === Authentication completion === */
static void request_redraw(struct compositor *comp);
static void auth_finished(struct side_ctx *ctx, const char *action,
        const char *username, bool success) {
    ctx->auth_error = !success;
    if (success && strcmp(action, "logout") == 0) {
        ctx->auto_login_suppressed = true;
        clear_side_focus_on_lifecycle_change(ctx->comp, ctx, "signed out");
        ctx->state = SIDE_GREETER;
        ctx->recovery_required = false;
        ctx->launcher_failed = false;
        ctx->launch_pending = false;
        ctx->launch_token[0] = '\0';
        ctx->supervisor_instance[0] = '\0';
        ctx->worker_pid = 0;
        ctx->owner[0] = '\0';
        ctx->username[0] = '\0';
        explicit_bzero(ctx->password, sizeof(ctx->password));
    } else if (success) {
        ctx->auto_login_suppressed = false;
        ctx->state = SIDE_CLAIMED;
        ctx->recovery_required = false;
        snprintf(ctx->owner, sizeof(ctx->owner), "%s", username);
        launch_side_session(ctx, username);
    }
    request_redraw(ctx->comp);
}

static void auto_login_side(struct side_ctx *ctx, const char *username) {
    if (!ctx || !username || !username[0]) return;
    if (ctx->state != SIDE_GREETER || ctx->recovery_required || ctx->auth || ctx->auth_error || ctx->auto_login_suppressed) return;
    /* Admission is serialized across both sides. Defer the second automatic
     * request instead of turning normal startup contention into an error. */
    if (ctx->comp->left.auth || ctx->comp->right.auth) return;

    wlr_log(WLR_INFO, "Auto-login: attempting %s on %s side",
            username, side_name(ctx->id));
    if (call_auth_helper(ctx, "auto-login", username, "")) request_redraw(ctx->comp);
}

static int auto_login_timer_callback(void *data) {
    struct compositor *comp = data;
    if (!comp || !comp->config.allow_nested_sessions) return 0;

    /* The supervisor owns activity restart. A stale PID or a missing frame
     * must never authorize another session while cleanup is unresolved. */

    /* Auto-login primary user */
    const char *primary_user = getenv("DOSPLEX_LEFT_USER");
    if (primary_user) auto_login_side(&comp->left, primary_user);

    /* Auto-login guest user if in split mode, or once at start when Guest's side is to be ready but asleep
     * behind Main's full screen (DOSPLEX_GUEST_PRELOGIN). The supervisor keeps a Guest side in Full screen in
     * standby, so the login starts no Steam and shows nothing; the side's state file is what it gives. */
    if (comp->layout != LAYOUT_SINGLE || comp->guest_prelogin) {
        const char *guest_user = getenv("DOSPLEX_RIGHT_USER");
        if (guest_user) auto_login_side(&comp->right, guest_user);
        if (comp->right.state != SIDE_GREETER) comp->guest_prelogin = false;
    }

    if (comp->auto_login_timer) {
        wl_event_source_timer_update(comp->auto_login_timer, 5000);
    }
    return 0;
}

#ifndef SESSION_START_TIMEOUT_MS
#define SESSION_START_TIMEOUT_MS 30000
#endif
/* Reap only the child we actually forked, never a PID recovered from JSON.
 * A launcher exit is not permission to drop ownership or start another one. */
static bool monitor_session_launcher(struct side_ctx *ctx) {
    if (ctx->launcher_pid <= 0) return false;
    bool changed = false;
    if (ctx->launch_pending) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0 ||
                (now.tv_sec - ctx->launch_started.tv_sec) * 1000 +
                (now.tv_nsec - ctx->launch_started.tv_nsec) / 1000000 >= SESSION_START_TIMEOUT_MS) {
            ctx->launch_pending = false;
            ctx->launcher_failed = true;
            ctx->recovery_required = true;
            ctx->auth_error = true;
            /* This is our unreaped child, not a PID from state. Ask its
             * supervisor to stop its owned service; keep ownership on failure. */
            kill(ctx->launcher_pid, SIGTERM);
            changed = true;
        }
    }
    int status;
    pid_t done = waitpid(ctx->launcher_pid, &status, WNOHANG);
    if (done == 0 || (done < 0 && errno == EINTR)) return changed;
    if (done < 0 && errno != ECHILD) return false;
    ctx->launcher_pid = 0;
    ctx->launch_pending = false;
    if (ctx->owner[0] && !ctx->auth) {
        ctx->launcher_failed = true;
        ctx->recovery_required = true;
        ctx->auth_error = true;
    }
    return true;
}

static void launch_side_session(struct side_ctx *ctx, const char *username) {
    monitor_session_launcher(ctx);
    if (ctx->launcher_pid > 0) {
        ctx->launcher_failed = true;
        ctx->recovery_required = true;
        ctx->auth_error = true;
        return;
    }
    unsigned char random[16];
    if (getrandom(random, sizeof(random), 0) != sizeof(random) ||
            clock_gettime(CLOCK_MONOTONIC, &ctx->launch_started) != 0) {
        ctx->launcher_failed = true;
        ctx->recovery_required = true;
        ctx->auth_error = true;
        return;
    }
    for (size_t i = 0; i < sizeof(random); i++) {
        snprintf(ctx->launch_token + i * 2, 3, "%02x", random[i]);
    }
    ctx->supervisor_instance[0] = '\0';
    const char *side_str = ctx->id == SIDE_LEFT ? "left" : "right";
    pid_t pid = fork();
    if (pid < 0) {
        wlr_log(WLR_ERROR, "fork failed for session launch");
        ctx->launcher_failed = true;
        ctx->recovery_required = true;
        ctx->auth_error = true;
        return;
    }
    if (pid == 0) {
        spawn_child_reset();
        /* Child process - exec the session script */
        char env_launch_token[64];
        snprintf(env_launch_token, sizeof(env_launch_token), "DOSPLEX_LAUNCH_TOKEN=%s", ctx->launch_token);
        char env_side_role[64];
        char env_output_name[96];
        char env_output_desc[128];
        char env_output_width[64];
        char env_output_height[64];
        char env_output_scale[64];
        char env_output_phys_x[64];
        char env_output_phys_y[64];
        char env_output_logical_x[64];
        char env_output_logical_y[64];

        snprintf(env_side_role, sizeof(env_side_role), "DOSPLEX_SIDE_ROLE=%s",
            side_role_name(ctx->role));
        snprintf(env_output_name, sizeof(env_output_name), "DOSPLEX_OUTPUT_NAME=%s",
            side_output_name(ctx));
        snprintf(env_output_desc, sizeof(env_output_desc), "DOSPLEX_OUTPUT_DESCRIPTION=%s",
            ctx->logical_output.description[0] ? ctx->logical_output.description : side_str);
        snprintf(env_output_width, sizeof(env_output_width), "DOSPLEX_OUTPUT_WIDTH=%d",
            ctx->width > 0 ? ctx->width : side_logical_width(ctx));
        snprintf(env_output_height, sizeof(env_output_height), "DOSPLEX_OUTPUT_HEIGHT=%d",
            ctx->height > 0 ? ctx->height : side_logical_height(ctx));
        snprintf(env_output_scale, sizeof(env_output_scale), "DOSPLEX_OUTPUT_SCALE_MILLI=%d",
            ctx->logical_output.scale_milli);
        snprintf(env_output_phys_x, sizeof(env_output_phys_x), "DOSPLEX_OUTPUT_PHYSICAL_X=%d",
            side_physical_origin_x(ctx));
        snprintf(env_output_phys_y, sizeof(env_output_phys_y), "DOSPLEX_OUTPUT_PHYSICAL_Y=%d",
            side_physical_origin_y(ctx));
        snprintf(env_output_logical_x, sizeof(env_output_logical_x), "DOSPLEX_OUTPUT_LOGICAL_X=%d",
            ctx->logical_output.logical_x);
        snprintf(env_output_logical_y, sizeof(env_output_logical_y), "DOSPLEX_OUTPUT_LOGICAL_Y=%d",
            ctx->logical_output.logical_y);

        char env_display[64];
        if (ctx->xwayland) {
            snprintf(env_display, sizeof(env_display), "DISPLAY=%s",
                ctx->xwayland->display_name);
        } else {
            snprintf(env_display, sizeof(env_display), "DISPLAY=");
        }

        /* Production already runs as root. Avoid sudo resetting the explicit
         * supervisor configuration inherited from start-dosplex. */
        const char *launcher = geteuid() == 0 ? "/usr/bin/env" : "sudo";
        char *arguments[24];
        size_t argc = 0;
        arguments[argc++] = (char *)launcher;
        if (geteuid() != 0) arguments[argc++] = "-n";
        char *environment[] = {
            env_launch_token, env_side_role, env_display, env_output_name, env_output_desc,
            env_output_width, env_output_height, env_output_scale,
            env_output_phys_x, env_output_phys_y, env_output_logical_x,
            env_output_logical_y,
        };
        for (size_t i = 0; i < sizeof(environment) / sizeof(environment[0]); i++) {
            arguments[argc++] = environment[i];
        }
        arguments[argc++] = (char *)session_script_path();
        arguments[argc++] = (char *)side_str;
        arguments[argc++] = (char *)username;
        arguments[argc] = NULL;
        execvp(launcher, arguments);
        _exit(1);
    }
    /* Keep child identity separate from the supervisor PID read from state. */
    ctx->launcher_pid = pid;
    ctx->launcher_failed = false;
    ctx->launch_pending = true;
    ctx->worker_pid = pid;
    wlr_log(WLR_INFO, "Started side session launcher: %s as %s (pid=%d)", side_str, username, pid);
}

/* === Session state decoding === */
struct session_snapshot {
    char owner[64], lifecycle[32], instance[33], launch_token[33], wine_notice[160];
    pid_t pid;
    uint32_t wine_notice_seq;
    char mode[16];
    bool active;
    int desktop_scale_milli; /* 0 while the launcher reported none */
    /* The supervisor's last report on applying the display contract. */
    bool display_applied_known;
    unsigned display_applied_revision;
    int display_applied_width, display_applied_height; /* 0 when the report has no valid size */
    enum display_result display_result;
    char display_detail[160];
};

static enum display_result display_result_parse(const char *text) {
    if (strcmp(text, "applied") == 0) return DISPLAY_RESULT_APPLIED;
    if (strcmp(text, "failed") == 0) return DISPLAY_RESULT_FAILED;
    if (strcmp(text, "deferred") == 0) return DISPLAY_RESULT_DEFERRED;
    return DISPLAY_RESULT_NONE; /* an unknown word is no result */
}

/* The words of session.json and state.json; empty for no result. */
static const char *display_result_name(enum display_result result) {
    switch (result) {
    case DISPLAY_RESULT_APPLIED: return "applied";
    case DISPLAY_RESULT_FAILED: return "failed";
    case DISPLAY_RESULT_DEFERRED: return "deferred";
    case DISPLAY_RESULT_NONE: break;
    }
    return "";
}

/* One printable line: control characters become spaces and the text is cut at
 * a UTF-8 character boundary to fit. The JSON parser has validated the encoding. */
static void session_text_line(const char *text, size_t length, char *output, size_t capacity) {
    size_t used = length < capacity - 1 ? length : capacity - 1;
    while (used > 0 && used < length && ((unsigned char)text[used] & 0xC0) == 0x80) used--;
    for (size_t i = 0; i < used; i++) {
        unsigned char c = (unsigned char)text[i];
        output[i] = c < 0x20 || c == 0x7f ? ' ' : (char)c;
    }
    output[used] = '\0';
}

static bool session_string(struct json_object *object, const char *key,
        char *output, size_t capacity, bool required) {
    struct json_object *value;
    if (!json_object_object_get_ex(object, key, &value)) return !required;
    if (!json_object_is_type(value, json_type_string)) return false;
    const char *text = json_object_get_string(value);
    size_t length = (size_t)json_object_get_string_len(value);
    if (length >= capacity || strlen(text) != length || (required && !length)) return false;
    memcpy(output, text, length + 1);
    return true;
}

static bool decode_session_state(const char *buffer, size_t length,
        const char *side, struct session_snapshot *result) {
    struct json_tokener *parser = json_tokener_new_ex(16);
    if (!parser) return false;
    json_tokener_set_flags(parser, JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    struct json_object *object = json_tokener_parse_ex(parser, buffer, (int)length);
    bool valid = json_tokener_get_error(parser) == json_tokener_success &&
        json_tokener_get_parse_end(parser) == length &&
        json_object_is_type(object, json_type_object);
    struct session_snapshot snapshot = {0};
    char state_side[8] = "";
    struct json_object *pid = NULL, *active = NULL, *notice_seq = NULL;
    if (valid) {
        valid = session_string(object, "mode", snapshot.mode, sizeof(snapshot.mode), false) &&
            (!snapshot.mode[0] || strcmp(snapshot.mode, "gaming") == 0 || strcmp(snapshot.mode, "desktop") == 0) &&
            session_string(object, "owner", snapshot.owner, sizeof(snapshot.owner), true) &&
            session_string(object, "side", state_side, sizeof(state_side), true) &&
            strcmp(state_side, side) == 0 &&
            session_string(object, "lifecycle", snapshot.lifecycle, sizeof(snapshot.lifecycle), false) &&
            session_string(object, "instance", snapshot.instance, sizeof(snapshot.instance), false) &&
            session_string(object, "launch_token", snapshot.launch_token, sizeof(snapshot.launch_token), false) &&
            session_string(object, "wine_notice", snapshot.wine_notice, sizeof(snapshot.wine_notice), false) &&
            json_object_object_get_ex(object, "pid", &pid) && json_object_is_type(pid, json_type_int) &&
            json_object_object_get_ex(object, "active", &active) && json_object_is_type(active, json_type_boolean);
    }
    if (valid) {
        if (json_object_object_get_ex(object, "wine_notice_seq", &notice_seq)) {
            int64_t sequence = json_object_get_int64(notice_seq);
            valid = json_object_is_type(notice_seq, json_type_int) &&
                sequence >= 0 && sequence <= UINT32_MAX;
            snapshot.wine_notice_seq = valid ? (uint32_t)sequence : 0;
            if (valid && snapshot.wine_notice_seq && !snapshot.wine_notice[0]) valid = false;
        }
    }
    if (valid) {
        int64_t number = json_object_get_int64(pid);
        valid = number >= 0 && number <= INT_MAX;
        snapshot.pid = valid ? (pid_t)number : 0;
        snapshot.active = json_object_get_boolean(active);
        const char *ids[] = {snapshot.instance, snapshot.launch_token};
        struct json_object *instance_value;
        for (size_t field = 0; field < 2; field++) {
            bool present = field == 0 ? json_object_object_get_ex(object, "instance", &instance_value) : ids[field][0] != '\0';
            if (!present) continue;
            if (strlen(ids[field]) != 32) valid = false;
            for (size_t i = 0; ids[field][i]; i++) {
                if (!((ids[field][i] >= '0' && ids[field][i] <= '9') ||
                        (ids[field][i] >= 'a' && ids[field][i] <= 'f'))) valid = false;
            }
        }
        const char *known[] = {"", "starting", "running", "restarting", "stopping",
            "stopped", "sleeping", "waiting_display", "standby", "standby_failed", "off", "cleanup_failed", "exited"};
        bool known_lifecycle = false;
        for (size_t i = 0; i < sizeof(known) / sizeof(known[0]); i++) {
            if (strcmp(snapshot.lifecycle, known[i]) == 0) known_lifecycle = true;
        }
        valid = valid && known_lifecycle;
    }
    if (valid) {
        /* The display report is cosmetic and decides no ownership or recovery:
         * a missing, mistyped or out-of-range field is left out, and the rest
         * of the snapshot stands. */
        struct json_object *value;
        if (json_object_object_get_ex(object, "display_applied_revision", &value) &&
                json_object_is_type(value, json_type_int)) {
            int64_t revision = json_object_get_int64(value);
            if (revision >= 0 && revision <= UINT32_MAX) {
                snapshot.display_applied_known = true;
                snapshot.display_applied_revision = (unsigned)revision;
            }
        }
        /* The Desktop payload's scale as the launcher chose it (thousandths).
         * Anything outside 1 to 3 times is left out: the side stays at scale 1. */
        if (json_object_object_get_ex(object, "desktop_scale_milli", &value) &&
                json_object_is_type(value, json_type_int)) {
            int64_t milli = json_object_get_int64(value);
            if (milli >= 1000 && milli <= 3000) snapshot.desktop_scale_milli = (int)milli;
        }
        if (json_object_object_get_ex(object, "display_result", &value) &&
                json_object_is_type(value, json_type_string)) {
            snapshot.display_result = display_result_parse(json_object_get_string(value));
        }
        /* The monitor size in effect, [width, height]: two integers from 1 to 16384, else not reported. */
        if (json_object_object_get_ex(object, "display_applied_size", &value) &&
                json_object_is_type(value, json_type_array) && json_object_array_length(value) == 2) {
            struct json_object *width = json_object_array_get_idx(value, 0);
            struct json_object *height = json_object_array_get_idx(value, 1);
            if (json_object_is_type(width, json_type_int) && json_object_is_type(height, json_type_int)) {
                int64_t w = json_object_get_int64(width), h = json_object_get_int64(height);
                if (w >= 1 && w <= 16384 && h >= 1 && h <= 16384) {
                    snapshot.display_applied_width = (int)w;
                    snapshot.display_applied_height = (int)h;
                }
            }
        }
        if (json_object_object_get_ex(object, "display_detail", &value) &&
                json_object_is_type(value, json_type_string)) {
            session_text_line(json_object_get_string(value), (size_t)json_object_get_string_len(value),
                snapshot.display_detail, sizeof(snapshot.display_detail));
        }
    }
    if (object) json_object_put(object);
    json_tokener_free(parser);
    if (valid) *result = snapshot;
    return valid;
}

#ifndef SIDE_STATE_ROOT
#define SIDE_STATE_ROOT "/var/lib/dosplex/sides"
#endif
#ifndef SESSION_STATE_UID
#define SESSION_STATE_UID 0
#endif

static bool trusted_state_fd(int fd, bool directory) {
    struct stat status;
    if (fstat(fd, &status) != 0) return false;
    if (status.st_uid != SESSION_STATE_UID || (status.st_mode & 0022) ||
            (directory ? !S_ISDIR(status.st_mode) :
                (!S_ISREG(status.st_mode) || status.st_nlink != 1 || status.st_size >= 65536))) {
        errno = EPERM;
        return false;
    }
    return true;
}

static int open_side_state(const char *side) {
    const char *configured = getenv("DOSPLEX_SIDE_ROOT");
    const char *path = configured ? configured : SIDE_STATE_ROOT;
    if (path[0] != '/') { errno = EINVAL; return -1; }
    int root = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) return -1;
    int directory = -1, state = -1;
    if (!trusted_state_fd(root, true)) goto done;
    directory = openat(root, side, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0 || !trusted_state_fd(directory, true)) goto done;
    /* O_NONBLOCK avoids waiting for a writer if a FIFO replaced the file.
     * Validate the opened inode, not a pathname checked before opening. */
    state = openat(directory, "session.json", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (state >= 0 && !trusted_state_fd(state, false)) {
        int saved = errno;
        close(state);
        state = -1;
        errno = saved;
    }
done:;
    int saved = errno;
    if (directory >= 0) close(directory);
    close(root);
    errno = saved;
    return state;
}
/* Reset is a restart request. The supervisor restarts a side when the token in
 * <side root>/<side>/restart differs from the one it read at launch
 * (dosplex-side-supervisor.py restart_request); a contract version 2 supervisor
 * restarts only for that token, a mode change or a stop, never for a display
 * change. The directory is the supervisor's own (root, not group or other
 * writable), opened without following symlinks, and the token replaces the
 * file atomically, as request_change does. */
static bool request_side_restart(const char *side) {
    const char *configured = getenv("DOSPLEX_SIDE_ROOT");
    const char *path = configured ? configured : SIDE_STATE_ROOT;
    if (!side || path[0] != '/') { errno = EINVAL; return false; }
    int root = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) return false;
    bool ok = false;
    int directory = -1, fd = -1;
    char temp[48] = "";
    unsigned char raw[16];
    char token[sizeof(raw) * 2 + 2];
    if (!trusted_state_fd(root, true)) goto done;
    directory = openat(root, side, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0 || !trusted_state_fd(directory, true)) goto done;
    if (getrandom(raw, sizeof(raw), 0) != (ssize_t)sizeof(raw)) goto done;
    for (size_t i = 0; i < sizeof(raw); i++) snprintf(token + i * 2, 3, "%02x", raw[i]);
    token[sizeof(raw) * 2] = '\n';
    token[sizeof(raw) * 2 + 1] = '\0';
    snprintf(temp, sizeof(temp), ".restart-%ld", (long)getpid());
    unlinkat(directory, temp, 0);
    fd = openat(directory, temp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
    if (fd < 0) { temp[0] = '\0'; goto done; }
    size_t length = strlen(token);
    if (write(fd, token, length) != (ssize_t)length || fsync(fd) != 0) goto done;
    if (close(fd) != 0) { fd = -1; goto done; }
    fd = -1;
    if (renameat(directory, temp, directory, "restart") != 0) goto done;
    temp[0] = '\0';
    ok = true;
done:
    if (fd >= 0) close(fd);
    if (temp[0] && directory >= 0) unlinkat(directory, temp, 0);
    if (directory >= 0) close(directory);
    close(root);
    return ok;
}

/* The game monitor size as published in state.json: Native (zero) is the region. */
static void side_effective_game_size(const struct side_ctx *ctx, int *width, int *height) {
    *width = ctx->game_width > 0 ? ctx->game_width : ctx->width;
    *height = ctx->game_height > 0 ? ctx->game_height : ctx->height;
}

/* The supervisor's latest report is for the current revision and says deferred: a game is running, so the
 * monitor kept its old size. */
static bool side_display_deferred(const struct side_ctx *ctx) {
    return ctx->display_applied_known && !ctx->desktop_payload &&
        ctx->display_result == DISPLAY_RESULT_DEFERRED &&
        ctx->display_applied_revision == ctx->display_revision;
}

/* Whether Gamescope scales the side's picture into its region instead of showing it at the size the monitor was
 * asked for: the report on the current revision is deferred, or the monitor size in effect is not the effective
 * game size. A Desktop payload has no game monitor. A side without a report, or whose report has no size, is not
 * known to be scaled. */
static bool side_is_scaled(const struct side_ctx *ctx) {
    if (!ctx || !ctx->display_applied_known || ctx->desktop_payload) return false;
    if (side_display_deferred(ctx)) return true;
    if (ctx->display_applied_width <= 0 || ctx->display_applied_height <= 0 ||
            ctx->width <= 0 || ctx->height <= 0) return false;
    int width, height;
    side_effective_game_size(ctx, &width, &height);
    return ctx->display_applied_width != width || ctx->display_applied_height != height;
}

/* What the Display page says about a scaled side, and what the one-time notice says. */
static void side_scaled_sentence(const struct side_ctx *ctx, char *line, size_t size) {
    bool zoomed = side_zoom_in_effect(ctx);
    const char *tail = zoomed ? "Keep ratio shows the whole picture; relaunch the game to use the whole area." :
        "Fit fills the screen if the game shows black bars; relaunch the game to use the whole area.";
    if (ctx->display_applied_width > 0 && ctx->display_applied_height > 0) {
        snprintf(line, size, "%s: game at %dx%d. %s", zoomed ? "Fitted" : "Scaled",
            ctx->display_applied_width, ctx->display_applied_height, tail);
    } else {
        snprintf(line, size, "%s. %s", zoomed ? "Fitted" : "Scaled", tail);
    }
}

/* The first deferred report for a revision raises the Scaled notice, once for each side and revision. It goes into
 * the slot of the Wine notice, which the overlay shows for 8 seconds from the first time it draws it. The
 * supervisor's own notice keeps the slot until it has been shown; the Scaled notice comes at a later poll. When
 * the game ends and the monitor is applied, the report stops saying scaled, and a notice that nobody has seen
 * yet is withdrawn. */
static void side_note_scaled_notice(struct side_ctx *ctx) {
    if (!side_display_deferred(ctx)) {
        if (ctx->wine_notice_is_scaled && !side_is_scaled(ctx)) {
            ctx->wine_notice[0] = '\0';
            ctx->wine_notice_timer_started = false;
            ctx->wine_notice_is_scaled = false;
        }
        return;
    }
    if (ctx->scaled_notice_given && ctx->scaled_notice_revision == ctx->display_applied_revision) return;
    if (ctx->wine_notice[0] && !ctx->wine_notice_is_scaled) return;
    ctx->scaled_notice_given = true;
    ctx->scaled_notice_revision = ctx->display_applied_revision;
    side_scaled_sentence(ctx, ctx->wine_notice, sizeof(ctx->wine_notice));
    ctx->wine_notice_is_scaled = true;
    ctx->wine_notice_timer_started = false;
}

/* Takes over the supervisor's report from a snapshot. */
static void side_take_display_report(struct side_ctx *ctx, const struct session_snapshot *snapshot) {
    ctx->display_applied_known = snapshot->display_applied_known;
    ctx->display_applied_revision = snapshot->display_applied_revision;
    ctx->display_applied_width = snapshot->display_applied_width;
    ctx->display_applied_height = snapshot->display_applied_height;
    ctx->display_result = snapshot->display_result;
    memcpy(ctx->display_detail, snapshot->display_detail, sizeof(ctx->display_detail));
    side_note_scaled_notice(ctx);
    side_zoom_refresh(ctx);
}

/* Only an active Desktop session has a scale; every other payload, a stopped
 * session and a launcher that reported none stay at 1. */
static void side_apply_snapshot_scale(struct side_ctx *ctx, const struct session_snapshot *snapshot) {
    bool desktop = snapshot->active && strcmp(snapshot->mode, "desktop") == 0;
    side_set_desktop_scale(ctx, desktop && snapshot->desktop_scale_milli ? snapshot->desktop_scale_milli : 1000);
}

/* A nested KWin takes its scale from the preferred scale it receives before
 * its first configure, so the scale has to be current when its toplevel
 * appears, not up to one poll interval later. Reads session.json again, and
 * changes nothing but the scale. */
static void side_refresh_desktop_scale(struct side_ctx *ctx) {
    if (!ctx->comp->config.allow_nested_sessions) return;
    int state_fd = open_side_state(ctx->id == SIDE_LEFT ? "left" : "right");
    if (state_fd < 0) return;
    char buf[65537];
    ssize_t n = read(state_fd, buf, sizeof(buf) - 1);
    close(state_fd);
    struct session_snapshot snapshot;
    if (n > 0 && decode_session_state(buf, (size_t)n, ctx->id == SIDE_LEFT ? "left" : "right", &snapshot)) {
        side_apply_snapshot_scale(ctx, &snapshot);
    }
}

static void poll_side_state(struct compositor *comp, struct side_ctx *ctx) {
    if (!comp->config.allow_nested_sessions || ctx->auth) return;
    enum side_state old_state = ctx->state;
    pid_t old_pid = ctx->worker_pid;
    int state_fd = open_side_state(ctx->id == SIDE_LEFT ? "left" : "right");
    FILE *f = state_fd >= 0 ? fdopen(state_fd, "r") : NULL;
    if (!f && state_fd >= 0) {
        int saved = errno;
        close(state_fd);
        errno = saved;
    }
    if (!f) {
        if (errno != ENOENT) {
            ctx->recovery_required = true;
            ctx->state = SIDE_CLAIMED;
            return;
        }
        if (ctx->launch_pending) return;
        if (ctx->launcher_failed) {
            ctx->recovery_required = true;
            ctx->state = SIDE_CLAIMED;
            return;
        }
        ctx->recovery_required = false;
        if (ctx->state == SIDE_CLAIMED) {
            /* Logout happened externally */
            clear_side_focus_on_lifecycle_change(comp, ctx, "state file disappeared");
            ctx->state = SIDE_GREETER;
            ctx->owner[0] = '\0';
            ctx->worker_pid = 0;
            ctx->username[0] = '\0';
            ctx->password[0] = '\0';
            ctx->display_applied_known = false;
            ctx->display_applied_revision = 0;
            ctx->display_applied_width = ctx->display_applied_height = 0;
            ctx->display_result = DISPLAY_RESULT_NONE;
            ctx->display_detail[0] = '\0';
            ctx->desktop_payload = false;
            ctx->scaled_notice_given = false;
        }
        return;
    }

    char buf[65537];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = '\0';
    bool incomplete = ferror(f) || !feof(f);
    fclose(f);
    struct session_snapshot snapshot;
    if (incomplete || !decode_session_state(buf, n,
            ctx->id == SIDE_LEFT ? "left" : "right", &snapshot)) {
        ctx->recovery_required = true;
        ctx->state = SIDE_CLAIMED;
        return;
    }
    if (ctx->supervisor_instance[0] &&
            (strcmp(ctx->supervisor_instance, snapshot.instance) != 0 ||
             strcmp(ctx->owner, snapshot.owner) != 0)) {
        ctx->launch_pending = false;
        ctx->launcher_failed = true;
        ctx->recovery_required = true;
        return;
    }
    if (ctx->launch_token[0]) {
        bool matches = strcmp(ctx->launch_token, snapshot.launch_token) == 0 &&
            strcmp(ctx->owner, snapshot.owner) == 0 && snapshot.instance[0];
        if (!matches || (ctx->supervisor_instance[0] &&
                strcmp(ctx->supervisor_instance, snapshot.instance) != 0)) {
            if (!ctx->launch_pending || ctx->supervisor_instance[0]) {
                ctx->launch_pending = false;
                ctx->launcher_failed = true;
                ctx->recovery_required = true;
            }
            return;
        }
        memcpy(ctx->supervisor_instance, snapshot.instance, sizeof(ctx->supervisor_instance));
        if (ctx->launch_pending) {
            bool failed = strcmp(snapshot.lifecycle, "cleanup_failed") == 0 ||
                strcmp(snapshot.lifecycle, "stopped") == 0 || strcmp(snapshot.lifecycle, "exited") == 0;
            bool ready = snapshot.pid > 0 && kill(snapshot.pid, 0) == 0 &&
                ((strcmp(snapshot.lifecycle, "running") == 0 && snapshot.active) ||
                 strcmp(snapshot.lifecycle, "sleeping") == 0 || strcmp(snapshot.lifecycle, "off") == 0);
            if (!ready && !failed) return;
            ctx->launch_pending = false;
            if (failed) ctx->launcher_failed = true;
        }
    }
    if (!ctx->supervisor_instance[0] && snapshot.instance[0]) {
        memcpy(ctx->supervisor_instance, snapshot.instance, sizeof(ctx->supervisor_instance));
    }
    if (strcmp(ctx->wine_notice_instance, snapshot.instance) != 0) {
        memcpy(ctx->wine_notice_instance, snapshot.instance, sizeof(ctx->wine_notice_instance));
        ctx->wine_notice_seq = 0;
        ctx->wine_notice[0] = '\0';
        ctx->wine_notice_timer_started = false;
        ctx->wine_notice_is_scaled = false;
        ctx->scaled_notice_given = false;
    }
    if (snapshot.wine_notice_seq > ctx->wine_notice_seq) {
        ctx->wine_notice_seq = snapshot.wine_notice_seq;
        snprintf(ctx->wine_notice, sizeof(ctx->wine_notice), "%s", snapshot.wine_notice);
        ctx->wine_notice_timer_started = false;
        ctx->wine_notice_is_scaled = false;
    }
    memcpy(ctx->owner, snapshot.owner, sizeof(ctx->owner));
    ctx->worker_pid = snapshot.pid;
    ctx->desktop_payload = strcmp(snapshot.mode, "desktop") == 0;
    side_take_display_report(ctx, &snapshot);
    ctx->desktop_cursor = snapshot.active && strcmp(snapshot.mode, "desktop") == 0;
    side_apply_snapshot_scale(ctx, &snapshot);
    bool active = snapshot.active;
    const char *lifecycle = snapshot.lifecycle;

    /* Existing ownership survives idle, stopping and failed sessions. Only
     * acknowledged logout removes the state file and permits another claim. */
    bool live = ctx->worker_pid > 0 && kill(ctx->worker_pid, 0) == 0;
    bool waiting = strcmp(lifecycle, "sleeping") == 0 || strcmp(lifecycle, "off") == 0 ||
        strcmp(lifecycle, "waiting_display") == 0 ||
        strcmp(lifecycle, "starting") == 0 ||
        strcmp(lifecycle, "restarting") == 0 ||
        strcmp(lifecycle, "stopping") == 0;
    ctx->recovery_required = ctx->launcher_failed || strcmp(lifecycle, "standby_failed") == 0 || strcmp(lifecycle, "cleanup_failed") == 0 ||
        strcmp(lifecycle, "stopped") == 0 || strcmp(lifecycle, "exited") == 0 ||
        !ctx->owner[0] || !live || (!active && !waiting);
    ctx->state = SIDE_CLAIMED;

    if (old_state == SIDE_CLAIMED &&
            (ctx->state != SIDE_CLAIMED ||
             (old_pid > 0 && ctx->worker_pid > 0 && old_pid != ctx->worker_pid) ||
             (old_pid > 0 && ctx->worker_pid <= 0))) {
        clear_side_focus_on_lifecycle_change(comp, ctx, "side session changed");
    }
}

/* What the Display page says about a display change on a side: "Applying…"
 * while the supervisor's report still lags this compositor's display_revision,
 * the failure detail when the report says the current revision failed, or the
 * Scaled sentence when the report on the current revision is deferred or the
 * monitor in effect is not the size asked for (a report that deferred the
 * revision has acknowledged it, so it is never "Applying…"). A snapshot
 * without a revision (a supervisor that does not report) says nothing. The
 * failure detail is one line of at most max_chars characters, cut with an
 * ellipsis; the Scaled sentence is never cut. The line is empty when there is
 * nothing to say. */
enum display_status { DISPLAY_STATUS_NONE, DISPLAY_STATUS_APPLYING, DISPLAY_STATUS_FAILED, DISPLAY_STATUS_SCALED };

static enum display_status side_display_status(const struct side_ctx *ctx, size_t max_chars,
        char *line, size_t size) {
    if (!line || size < 5) return DISPLAY_STATUS_NONE;
    line[0] = '\0';
    if (!ctx || !ctx->display_applied_known) return DISPLAY_STATUS_NONE;
    if (ctx->display_revision > ctx->display_applied_revision) {
        snprintf(line, size, "Applying…");
        return DISPLAY_STATUS_APPLYING;
    }
    if (ctx->display_revision != ctx->display_applied_revision) return DISPLAY_STATUS_NONE;
    if (ctx->display_result != DISPLAY_RESULT_FAILED) {
        if (!side_is_scaled(ctx)) return DISPLAY_STATUS_NONE;
        side_scaled_sentence(ctx, line, size);
        return DISPLAY_STATUS_SCALED;
    }
    const char *text = ctx->display_detail;
    while (*text == ' ') text++;
    if (!*text) text = display_result_name(ctx->display_result);
    size_t length = strlen(text), used = 0, characters = 0;
    bool cut = false;
    while (used < length) {
        size_t step = 1;
        while (used + step < length && ((unsigned char)text[used + step] & 0xC0) == 0x80) step++;
        if (characters >= max_chars || used + step > size - 4) {
            cut = true;
            break;
        }
        used += step;
        characters++;
    }
    memcpy(line, text, used);
    while (used > 0 && line[used - 1] == ' ') used--;
    line[used] = '\0';
    if (cut) snprintf(line + used, size - used, "…");
    return DISPLAY_STATUS_FAILED;
}

/* The word for a side that shows a picture of its own, which needs an app surface and a game monitor: "native"
 * when the monitor is the size asked for, "scaled" when Gamescope scales the picture. NULL for any other side. */
static const char *side_picture_word(const struct side_ctx *ctx) {
    if (!ctx || !ctx->app_surface || ctx->desktop_payload) return NULL;
    return side_is_scaled(ctx) ? "scaled" : "native";
}

/* The header line of the overlay: the size of the side's region and the layout, and for a side with a picture of
 * its own " · Native" or " · Scaled (game at WxH)", the size being the monitor in effect. */
static void side_region_line(const struct side_ctx *ctx, const char *layout, char *line, size_t size) {
    int used = snprintf(line, size, "%dx%d screen region · %s", ctx->width, ctx->height, layout);
    if (!side_picture_word(ctx) || used < 0 || (size_t)used >= size) return;
    if (!side_is_scaled(ctx)) {
        snprintf(line + used, size - (size_t)used, " · Native");
    } else if (ctx->display_applied_width > 0 && ctx->display_applied_height > 0) {
        snprintf(line + used, size - (size_t)used, " · Scaled (game at %dx%d)",
            ctx->display_applied_width, ctx->display_applied_height);
    } else {
        snprintf(line + used, size - (size_t)used, " · Scaled");
    }
}

/* A label at the bottom of the screen in a split: "L: CLAIMED / owner", and for a side with a picture of its own
 * " / native" or " / scaled". */
static void side_debug_label(const struct side_ctx *ctx, char prefix, const char *state, char *label, size_t size) {
    int used = snprintf(label, size, "%c: %s / %s", prefix, state, ctx->owner[0] ? ctx->owner : "(none)");
    const char *word = side_picture_word(ctx);
    if (word && used >= 0 && (size_t)used < size) snprintf(label + used, size - (size_t)used, " / %s", word);
}

/* What the overlay and the labels show of the monitor behind a side's picture; they are drawn again when it changes. */
struct side_monitor {
    int width, height;
    bool scaled;
    enum display_result result;
};

static struct side_monitor side_monitor_view(const struct side_ctx *ctx) {
    return (struct side_monitor){ctx->display_applied_width, ctx->display_applied_height,
        side_is_scaled(ctx), ctx->display_result};
}

static bool side_monitor_differs(struct side_monitor a, struct side_monitor b) {
    return a.width != b.width || a.height != b.height || a.scaled != b.scaled || a.result != b.result;
}

/* === Scene rendering === */

/* The presentation a side shows now: always the player's own scale mode (Zoom is never chosen for them). */
static enum output_scale_mode side_presented_scale_mode(const struct side_ctx *ctx) {
    return ctx->scale_mode;
}

static bool side_zoom_in_effect(const struct side_ctx *ctx) {
    return ctx && !ctx->desktop_payload && side_presented_scale_mode(ctx) == OUTPUT_SCALE_ZOOM;
}

/* The game monitor Gamescope lays out in the side's region: the size the supervisor reports while it is deferred (the
 * monitor kept its old size), otherwise the effective game size. */
static void side_monitor_size(const struct side_ctx *ctx, int *width, int *height) {
    if (ctx->display_applied_known && ctx->display_applied_width > 0 && ctx->display_applied_height > 0 &&
            side_display_deferred(ctx)) {
        *width = ctx->display_applied_width;
        *height = ctx->display_applied_height;
        return;
    }
    side_effective_game_size(ctx, width, height);
}

static struct wlr_scene_rect *make_rect(struct wlr_scene_tree *parent,
        int x, int y, int w, int h, const float color[4]) {
    struct wlr_scene_rect *r = wlr_scene_rect_create(parent, w, h, color);
    wlr_scene_node_set_position(&r->node, x, y);
    return r;
}

static void rebuild_scene(struct compositor *comp);
static void request_redraw(struct compositor *comp);
static void apply_layout(struct compositor *comp);

static bool read_int_file(const char *path, int *value_out) {
    if (!path || !value_out) {
        return false;
    }

    FILE *fp = fopen(path, "r");
    if (!fp) {
        return false;
    }

    int value = 0;
    bool ok = fscanf(fp, "%d", &value) == 1;
    fclose(fp);
    if (!ok) {
        return false;
    }

    *value_out = value;
    return true;
}

static bool write_int_file(const char *path, int value) {
    if (!path) {
        return false;
    }

    FILE *fp = fopen(path, "w");
    if (!fp) {
        return false;
    }

    bool ok = fprintf(fp, "%d\n", value) > 0;
    fclose(fp);
    return ok;
}

static int default_backlight_brightness(const struct compositor *comp) {
    if (!comp || comp->max_backlight_brightness <= 0) {
        return 32767;
    }

    int fallback = comp->max_backlight_brightness / 2;
    if (fallback < 1) {
        fallback = 1;
    }
    if (fallback > comp->max_backlight_brightness) {
        fallback = comp->max_backlight_brightness;
    }
    return fallback;
}

static int min_visible_backlight_brightness(const struct compositor *comp) {
    if (!comp || comp->max_backlight_brightness <= 0) {
        return 1024;
    }
    int threshold = comp->max_backlight_brightness / 20;
    return threshold > 0 ? threshold : 1;
}

/* Idle handling follows --idle-policy and nothing else. With `none` the
 * output is never blanked and the backlight is never read or written. With
 * `dim` the output is switched off after the idle timeout; the backlight is
 * lowered as well only when the panel has one (its sysfs files exist). */
static bool idle_dim_enabled(const struct compositor *comp) {
    return comp && comp->config.idle_policy == IDLE_POLICY_DIM;
}

static bool backlight_is_off(struct compositor *comp) {
    if (!comp || !comp->backlight_available) {
        return false;
    }

    int current = 0;
    return read_int_file(BACKLIGHT_BRIGHTNESS_PATH, &current) &&
        current <= comp->dim_backlight_brightness;
}

static void restore_backlight(struct compositor *comp) {
    if (!idle_dim_enabled(comp)) {
        return;
    }

    bool should_restore = comp->backlight_dimmed || backlight_is_off(comp);
    if (!should_restore) {
        return;
    }

    if (comp->output) {
        struct wlr_output_state state;
        wlr_output_state_init(&state);
        wlr_output_state_set_enabled(&state, true);
        wlr_output_commit_state(comp->output, &state);
        wlr_output_state_finish(&state);
    }

    if (comp->backlight_available) {
        int restore_value = comp->saved_backlight_brightness;
        if (restore_value <= comp->dim_backlight_brightness ||
                restore_value < min_visible_backlight_brightness(comp)) {
            restore_value = default_backlight_brightness(comp);
        }
        if (write_int_file(BACKLIGHT_BRIGHTNESS_PATH, restore_value)) {
            wlr_log(WLR_INFO, "Display backlight restored to %d",
                restore_value);
            comp->saved_backlight_brightness = restore_value;
        } else {
            wlr_log(WLR_ERROR, "Failed to restore display backlight");
        }
    }

    comp->backlight_dimmed = false;
}

static void dim_backlight(struct compositor *comp) {
    if (!idle_dim_enabled(comp) || comp->backlight_dimmed) {
        return;
    }

    if (comp->backlight_available) {
        int current = 0;
        if (read_int_file(BACKLIGHT_BRIGHTNESS_PATH, &current)) {
            if (current >= min_visible_backlight_brightness(comp)) {
                comp->saved_backlight_brightness = current;
            } else if (comp->saved_backlight_brightness <= 0) {
                comp->saved_backlight_brightness = default_backlight_brightness(comp);
            }
            int target = comp->dim_backlight_brightness;
            if (target < 0) {
                target = 0;
            }
            if (current > target) {
                if (write_int_file(BACKLIGHT_BRIGHTNESS_PATH, target)) {
                    wlr_log(WLR_INFO, "Display backlight dimmed from %d to %d", current, target);
                } else {
                    wlr_log(WLR_ERROR, "Failed to dim display backlight");
                }
            }
        } else {
            wlr_log(WLR_ERROR, "Failed to read display backlight brightness");
        }
    }

    if (comp->output) {
        struct wlr_output_state state;
        wlr_output_state_init(&state);
        wlr_output_state_set_enabled(&state, false);
        wlr_output_commit_state(comp->output, &state);
        wlr_output_state_finish(&state);
    }

    comp->backlight_dimmed = true;
    wlr_log(WLR_INFO, "Display disabled due to idle");
}

static int idle_dim_timer_callback(void *data) {
    struct compositor *comp = data;
    if (!idle_dim_enabled(comp)) {
        return 0;
    }

    if (!comp->dimmed) {
        comp->dimmed = true;
        dim_backlight(comp);
        wlr_log(WLR_INFO, "Display dimmed after inactivity");
        request_redraw(comp);
    }

    return 0;
}

static void note_activity(struct compositor *comp) {
    if (!idle_dim_enabled(comp)) {
        return;
    }

    if (comp->dimmed || comp->backlight_dimmed || backlight_is_off(comp)) {
        comp->dimmed = false;
        restore_backlight(comp);
        wlr_log(WLR_INFO, "Display restored after activity");
        request_redraw(comp);
    }

    if (comp->idle_dim_timer) {
        wl_event_source_timer_update(comp->idle_dim_timer, comp->idle_dim_timeout_ms);
    }
}

/* === The toplevel's frame callbacks and presentation feedback ===
 * wlroots' scene decides from a node's visible region (what nothing opaque above it covers) whether a surface is on an
 * output at all: the primary output, the frame done events and the presentation sample of a surface follow from it, and
 * a surface is sampled only in a frame that renders some of its visible part, which is what the frame's damage reaches.
 * A nested KWin 6.7 asks for its frame callback and its presentation feedback on its toplevel's own surface alone, one
 * pixel stretched over the output, and draws everything in subsurfaces above it. So in its frames
 *  - a primary plane or a video plane in an opaque format, or one that declares an opaque region, covers the toplevel
 *    completely: it has no primary output and gets no frame done at all (and no sample);
 *  - an opaque video plane that covers a part of the output and is all that changes in a frame leaves the toplevel's
 *    visible part undamaged: it is not sampled, and the scene discards its feedback at its next commit.
 * KWin keeps two frames in flight and waits for the feedback of the first, which then does not come, or comes as a
 * discard for every frame, and its frames are no longer paced. The toplevel is the token of KWin's frame, and it is on the
 * screen whenever any surface of its tree is, whatever covers its own pixels and whether they changed: it gets what the
 * scene gives a surface it renders, its frame callbacks at the output's next frame and its presentation feedback with
 * the output commit that shows its tree. */

/* Whether the scene shows a part of some surface under the node, a subsurface or the toplevel's own. */
static bool scene_tree_shows_a_surface(struct wlr_scene_node *node) {
    if (!node->enabled) return false;
    if (node->type == WLR_SCENE_NODE_BUFFER) {
        return pixman_region32_not_empty(&node->visible) &&
            wlr_scene_surface_try_from_buffer(wlr_scene_buffer_from_node(node)) != NULL;
    }
    if (node->type != WLR_SCENE_NODE_TREE) return false;
    struct wlr_scene_node *child;
    wl_list_for_each(child, &wlr_scene_tree_from_node(node)->children, link) {
        if (scene_tree_shows_a_surface(child)) return true;
    }
    return false;
}

/* A mapped toplevel whose tree the scene shows a part of (its window and side are not hidden). */
static bool side_window_tree_shown(const struct side_xdg_window *window) {
    if (!window->surface || !window->surface->mapped || !window->scene_tree || !window->scale_buffer) return false;
    int lx, ly;
    if (!wlr_scene_node_coords(&window->scene_tree->node, &lx, &ly)) return false;
    return scene_tree_shows_a_surface(&window->scene_tree->node);
}

/* A toplevel whose tree the scene shows, and nothing of its own surface: its own subsurfaces cover it. */
static bool side_window_covered_by_its_tree(const struct side_xdg_window *window) {
    return side_window_tree_shown(window) && !pixman_region32_not_empty(&window->scale_buffer->node.visible);
}

static struct wlr_scene_buffer *find_surface_buffer(struct wlr_scene_node *node, struct wlr_surface *surface);

/* Every surface of a toplevel's tree, the toplevel's own and each subsurface at any depth, mapped or not (a subsurface
 * without a buffer is not mapped, and wlr_surface_for_each_surface leaves it and what is below it out; its client
 * may wait for its frame callback all the same). */
static void side_for_each_tree_surface(struct wlr_surface *surface, wlr_surface_iterator_func_t iterator, void *data) {
    struct wlr_subsurface *subsurface;
    wl_list_for_each(subsurface, &surface->current.subsurfaces_below, current.link) {
        side_for_each_tree_surface(subsurface->surface, iterator, data);
    }
    iterator(surface, 0, 0, data);
    wl_list_for_each(subsurface, &surface->current.subsurfaces_above, current.link) {
        side_for_each_tree_surface(subsurface->surface, iterator, data);
    }
}

struct tree_feedback_walk {
    struct compositor *comp;
    struct side_xdg_window *window;
    struct wlr_scene_output *scene_output;
};

static void side_queue_surface_feedback(struct wlr_surface *surface, int sx, int sy, void *data) {
    (void)sx; (void)sy;
    struct tree_feedback_walk *walk = data;
    struct wlr_scene_buffer *buffer = find_surface_buffer(&walk->window->scene_tree->node, surface);
    bool rendered = false;
    if (buffer) {
        pixman_region32_t reached;
        pixman_region32_init(&reached);
        pixman_region32_intersect(&reached, &buffer->node.visible, &walk->scene_output->damage_ring.current);
        rendered = pixman_region32_not_empty(&reached);
        pixman_region32_fini(&reached);
    }
    if (!rendered) wlr_presentation_surface_textured_on_output(walk->comp->scene->presentation, surface, walk->comp->output);
}

/* Before the output commit of a frame, if it will commit: the presentation feedback of the surfaces of the shown
 * toplevels' trees that the scene will not queue, which the commit then presents (wlroots'
 * wlr_presentation_surface_textured_on_output, as the scene calls it for what it renders). The scene queues the
 * feedback of a surface that the frame renders some of: a surface whose visible part meets the frame's damage. Where it
 * does not (the surface is covered by another surface of its tree, cropped away, without a buffer, or what changed in
 * the frame is elsewhere) this queues it, for the toplevel's own surface and for every subsurface at any depth: a
 * feedback that is not queued waits for the surface's next commit, which discards it. wlr_scene_output_commit commits
 * only when the output needs a frame or the scene has damage, and this is queued for a commit that happens. */
static void side_queue_toplevel_feedback(struct compositor *comp, struct wlr_scene_output *scene_output) {
    if (!comp->output || !comp->scene->presentation) return;
    if (!comp->output->needs_frame && !pixman_region32_not_empty(&scene_output->damage_ring.current)) return;
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        if (!sides[i]->xdg_windows_initialized) continue;
        struct side_xdg_window *window;
        wl_list_for_each(window, &sides[i]->xdg_windows, link) {
            if (!side_window_tree_shown(window)) continue;
            struct tree_feedback_walk walk = {comp, window, scene_output};
            side_for_each_tree_surface(window->surface, side_queue_surface_feedback, &walk);
        }
    }
}

static void side_answer_surface(struct wlr_surface *surface, int sx, int sy, void *data) {
    (void)sx; (void)sy;
    wlr_surface_send_frame_done(surface, data);
}

/* After the commit, with the scene's own frame done events: the frame callbacks of every surface of the shown
 * toplevels' trees, the scene's answers to the surfaces it shows left aside (a surface it answered has an empty list
 * and this does nothing for it). The scene answers the surfaces of its primary output only: a surface that is covered,
 * cropped away, without a buffer or of no size has none, and its client waits for this. A side whose tree is not shown
 * (hidden, in standby) gets no answer. */
static void side_answer_tree_callbacks(struct compositor *comp, const struct timespec *now) {
    if (!comp->output) return;
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        if (!sides[i]->xdg_windows_initialized) continue;
        struct side_xdg_window *window;
        wl_list_for_each(window, &sides[i]->xdg_windows, link) {
            if (side_window_tree_shown(window)) side_for_each_tree_surface(window->surface, side_answer_surface, (void *)now);
        }
    }
}

/* A client commit that asks for a frame callback, on any surface of a side's toplevel tree: with frames on demand no
 * frame comes unless something schedules it, and the scene does for the surfaces it shows only (the scaled path's
 * reconfigure for the same). The request schedules the frame that answers it (side_answer_tree_callbacks) while the
 * tree is shown, whatever the scene shows of the surface and whatever the commit damages. */
struct surface_frame_watch {
    struct compositor *comp;
    struct wlr_surface *surface;
    struct wl_listener commit;
    struct wl_listener destroy;
};

static void surface_frame_watch_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct surface_frame_watch *watch = wl_container_of(listener, watch, commit);
    struct compositor *comp = watch->comp;
    if (!comp->output || wl_list_empty(&watch->surface->current.frame_callback_list)) return;
    struct wlr_surface *root = wlr_surface_get_root_surface(watch->surface);
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        if (!sides[i]->xdg_windows_initialized) continue;
        struct side_xdg_window *window;
        wl_list_for_each(window, &sides[i]->xdg_windows, link) {
            if (window->surface == root && side_window_tree_shown(window)) {
                wlr_output_schedule_frame(comp->output);
                return;
            }
        }
    }
}

static void surface_frame_watch_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct surface_frame_watch *watch = wl_container_of(listener, watch, destroy);
    wl_list_remove(&watch->commit.link);
    wl_list_remove(&watch->destroy.link);
    free(watch);
}

static void handle_new_client_surface(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, new_client_surface);
    struct wlr_surface *surface = data;
    struct surface_frame_watch *watch = calloc(1, sizeof(*watch));
    if (!watch) return;
    watch->comp = comp;
    watch->surface = surface;
    watch->commit.notify = surface_frame_watch_commit;
    wl_signal_add(&surface->events.commit, &watch->commit);
    watch->destroy.notify = surface_frame_watch_destroy;
    wl_signal_add(&surface->events.destroy, &watch->destroy);
}

static int retry_output_frame(void *data) {
    struct compositor *comp = data;
    comp->frame_retry_pending = false;
    if (comp->output) wlr_output_schedule_frame(comp->output);
    return 0;
}

/* The frame handler held back the next frame while presents were outstanding (see the end of handle_output_frame)
 * and no frame event has come since. The count says that one must; if it is wrong, this is the next frame. */
static int release_held_frame(void *data) {
    struct compositor *comp = data;
    if (comp->output && comp->frame_pacing != FRAME_PACING_ON_DEMAND) {
        wlr_log(WLR_INFO, "Frame pacing: no frame event for %d ms after the next frame was held back (a present of "
            "the output may be missing); scheduling it", PACING_KICK_MS);
        wlr_output_schedule_frame(comp->output);
    }
    return 0;
}

static void handle_output_frame(struct wl_listener *listener, void *data) {
    struct wlr_output *output = data;
    struct compositor *comp = wl_container_of(listener, comp, output_frame);
    (void)output;
    if (comp->pacing_kick_timer) wl_event_source_timer_update(comp->pacing_kick_timer, 0);

    /* Re-entry guard: frame events can fire during wl_display_run's initial
     * pre-dispatch before the first iteration; this prevents double-commits. */
    if (comp->in_frame_handler || comp->frame_retry_pending) {
        return;
    }
    comp->in_frame_handler = true;

    if (!comp->output) {
        comp->in_frame_handler = false;
        return;
    }

    struct wlr_scene_output *scene_output =
        wlr_scene_get_scene_output(comp->scene, comp->output);
    if (!scene_output) {
        comp->in_frame_handler = false;
        return;
    }

    /* Rebuild the overlay/UI subtree only when compositor state changed.
     * Client surfaces are tracked by wlr_scene damage and may need commits
     * even when our own UI state is unchanged. */
    if (comp->scene_dirty) {
        rebuild_scene(comp);
    }

    /* wlr_scene_output_commit builds the state (allocs GBM buffer, renders)
     * and submits it in one step. Do NOT call wlr_scene_output_build_state
     * separately — that would allocate a buffer, throw it away, then
     * wlr_scene_output_commit would allocate a second one (the double-alloc bug). */
    if (comp->config.test_hooks && comp->capture_requested != comp->capture_ready && !comp->capture_armed) {
        comp->capture_submitted = comp->capture_requested;
        comp->capture_commit_seq = comp->output->commit_seq + 1;
        comp->capture_armed = true;
    }
    /* DEV_STATS: the output pixels the scene has damaged since the last frame and the commit's duration,
     * counted only for frames the scene commits (an undamaged frame commits nothing). */
    uint64_t damage_before = 0;
    struct timespec commit_start = {0};
    uint32_t commit_seq_before = comp->output->commit_seq;
    if (comp->config.test_hooks) {
        /* The ring starts out as everything; count what is on the output. */
        pixman_region32_t damage;
        pixman_region32_init(&damage);
        pixman_region32_intersect_rect(&damage, &scene_output->damage_ring.current,
            0, 0, comp->output->width, comp->output->height);
        int rects = 0;
        const pixman_box32_t *box = pixman_region32_rectangles(&damage, &rects);
        for (int i = 0; i < rects; i++) {
            damage_before += (uint64_t)(box[i].x2 - box[i].x1) * (uint64_t)(box[i].y2 - box[i].y1);
        }
        pixman_region32_fini(&damage);
        clock_gettime(CLOCK_MONOTONIC, &commit_start);
    }
    side_queue_toplevel_feedback(comp, scene_output);
    /* Direct scan-out hands a side's own buffer to the output when it alone covers the whole output (Main on the full
     * screen with nothing drawn over it). On the X11 output, under the host Gamescope, that buffer becomes a presented
     * pixmap, and the host gives it back only when a later present replaces it. With frames on demand no later present
     * comes while nothing changes, and a client with two buffers (the side's nested Gamescope) waits for the one the
     * host keeps: it draws nothing more, and the game, whose pictures Gamescope then never takes, stands still. Seen on
     * the Steam Machine (Baldur's Gate 3, Crimsonland, Detroit: Become Human stopped while loading, 2026-11-16 and 17;
     * a scaled Desktop session's full-screen video froze the same way on 2026-11-02): the last buffer was presented
     * and the one before it never released. The FPS counter, the overlay and the split all draw over or beside the
     * side and so never allowed scan-out, which is why each of them hid it. On the X11 output the scene composes
     * every frame into the output's own buffers, which the host returns as usual, and a side's buffer goes back to
     * its client as soon as a newer one replaces it. Elsewhere (a DRM output) a scaled Desktop session still keeps
     * it off. */
    comp->scene->direct_scanout = !comp->output_nested_x11 &&
        comp->left.desktop_scale_milli <= 1000 && comp->right.desktop_scale_milli <= 1000;
    if (!wlr_scene_output_commit(scene_output, NULL)) {
        comp->capture_armed = false;
        /* DRM rejected the commit — a prior page-flip is still in flight.
         * Keep dirty so the next frame event retries. */
        comp->scene_dirty = true;
        /* Scene damage can immediately schedule another idle frame. Rebuilding
         * on every failed commit starves backend events that release buffers. */
        if (!comp->frame_retry_timer) {
            comp->frame_retry_timer = wl_event_loop_add_timer(
                wl_display_get_event_loop(comp->display), retry_output_frame, comp);
        }
        if (comp->frame_retry_timer) {
            comp->frame_retry_pending = true;
            wl_event_source_timer_update(comp->frame_retry_timer, 16);
        }
        wlr_log(WLR_DEBUG, "scene commit rejected, deferring retry");
        comp->in_frame_handler = false;
        return;
    }

    /* CRITICAL: Send frame_done to all client surfaces rendered in this frame.
     * Without this, nested compositors (like weston) never receive frame callbacks
     * and get stuck in REPAINT_AWAITING_COMPLETION — their output never updates
     * after the initial commit. This is a separate wlroots API call, not automatic. */
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (comp->config.test_hooks && comp->output->commit_seq != commit_seq_before) {
        comp->frames_rendered++;
        comp->damage_pixels += damage_before;
        int64_t elapsed_ns = (int64_t)(now.tv_sec - commit_start.tv_sec) * 1000000000 +
            (int64_t)(now.tv_nsec - commit_start.tv_nsec);
        comp->render_us += (uint64_t)(elapsed_ns / 1000);
    }
    wlr_scene_output_send_frame_done(scene_output, &now);
    side_answer_tree_callbacks(comp, &now);

    comp->scene_dirty = false;
    comp->in_frame_handler = false;

    /* Debug: log frame count every 90 frames (~1 second) */
    comp->frame_count++;
    if (comp->frame_count % 90 == 0) {
        struct side_ctx *left = &comp->left;
        struct side_ctx *right = &comp->right;
        wlr_log(WLR_DEBUG, "frame %lu (rebuilds %lu): left state=%d owner=%s mapped=%d app_surf=%p commits=%d, right state=%d owner=%s mapped=%d app_surf=%p commits=%d",
            (unsigned long)comp->frame_count, (unsigned long)comp->scene_rebuilds,
            left->state, left->owner[0] ? left->owner : "(none)",
            left->app_surface ? left->app_surface->mapped : -1,
            left->app_surface,
            left->commit_count,
            right->state, right->owner[0] ? right->owner : "(none)",
            right->app_surface ? right->app_surface->mapped : -1,
            right->app_surface,
            right->commit_count);
    }

    /* Schedule the next frame so the rendering loop continues.
     * Without this, the DRM backend stops sending frame events
     * after the initial modeset frames.
     * On demand (an X11 output whose presents carry their own commit's sequence) the next frame is scheduled by
     * what changes: the scene's damage and frame callbacks, and request_redraw. A frame scheduled here always
     * commits (it sets the output's needs_frame), and under the X11 backend the frame event comes with the
     * present event: a client that answers its presentation feedback commits its next frame only after this
     * commit, which then waits for the next host frame, so every client frame took two host frames.
     * Otherwise every frame is scheduled, except while another present is outstanding on an X11 output. A commit
     * outside this handler (a host resize) has put a second one in flight, and every present then brings a frame
     * event that commits and schedules the next, two commits for each host frame for good. Not scheduling lets the
     * frame events of the presents still outstanding find nothing to commit, until the loop runs on with the one
     * commit that is left. Those frame events come whatever is done here (this frame's own commit has one coming
     * as well); the timer is for a count that is wrong. */
    if (comp->frame_pacing == FRAME_PACING_ON_DEMAND) return;
    bool committed = comp->output->commit_seq != commit_seq_before;
    if (!present_order_others_pending(&comp->present_order, comp->output->commit_seq, committed)) {
        wlr_output_schedule_frame(comp->output);
    } else if (!committed) {
        if (!comp->pacing_kick_timer) {
            comp->pacing_kick_timer = wl_event_loop_add_timer(
                wl_display_get_event_loop(comp->display), release_held_frame, comp);
        }
        if (comp->pacing_kick_timer) wl_event_source_timer_update(comp->pacing_kick_timer, PACING_KICK_MS);
    }
}

static struct wlr_box scaled_ui_box(int ox, int oy, double scale,
        int x, int y, int w, int h) {
    int left = (int)lround(x * scale), top = (int)lround(y * scale);
    return (struct wlr_box){ox + left, oy + top,
        (int)lround((x + w) * scale) - left,
        (int)lround((y + h) * scale) - top};
}

static struct wlr_box greeter_box(struct side_ctx *ctx, double scale,
        int x, int y, int w, int h) {
    return scaled_ui_box(ctx->x, ctx->y, scale, x, y, w, h);
}

static void greeter_rect(struct side_ctx *ctx, double scale, struct wlr_scene_tree *parent,
        int x, int y, int w, int h, const float *color) {
    struct wlr_box box = greeter_box(ctx, scale, x, y, w, h);
    make_rect(parent, box.x, box.y, box.width, box.height, color);
}

static void greeter_text(struct side_ctx *ctx, double scale, struct wlr_scene_tree *parent,
        int x, int y, int w, int h, const char *text, double font_size,
        double r, double g, double b, double a, bool bold) {
    struct wlr_box box = greeter_box(ctx, scale, x, y, w, h);
    make_text_node(parent, box.x, box.y, box.width, box.height, text,
        font_size * scale, r, g, b, a, bold);
}

static void greeter_text_left(struct side_ctx *ctx, double scale, struct wlr_scene_tree *parent,
        int x, int y, int w, int h, const char *text, double font_size,
        double r, double g, double b, double a, bool bold) {
    struct wlr_box box = greeter_box(ctx, scale, x, y, w, h);
    make_text_node_left(parent, box.x, box.y, box.width, box.height, text,
        font_size * scale, r, g, b, a, bold);
}

static const char *configured_steam_user(struct side_ctx *ctx) {
    const char *payload = getenv("DOSPLEX_SESSION_PAYLOAD");
    if (!payload || strcmp(payload, "steam") != 0) return NULL;
    const char *user = getenv(ctx->id == SIDE_LEFT ? "DOSPLEX_LEFT_USER" : "DOSPLEX_RIGHT_USER");
    return user && user[0] ? user : NULL;
}

static void build_side_regions(struct side_ctx *ctx, double scale) {
    ctx->num_regions = 0;
    if (ctx->auth || ctx->launch_pending) return;
    /* Steam owns account sign-in. A temporarily unmapped Steam window is not
     * a second login screen and must not expose an End Session hit target. */
    if (configured_steam_user(ctx) && ctx->state == SIDE_CLAIMED &&
            !ctx->auth_error && !ctx->recovery_required) return;
    int width = (int)floor(ctx->width / scale) - 60;
    struct ui_region logical[] = {
        {UIelem_USERNAME, 30, 90, width, 40},
        {UIelem_PASSWORD, 30, 145, width, 40},
        {ctx->state == SIDE_CLAIMED ? UIelem_SIGNOUT : UIelem_SIGNIN, 30, 200, width, 44},
    };
    int first = ctx->state == SIDE_CLAIMED || configured_steam_user(ctx) ? 2 : 0;
    for (int i = first; i < 3; i++) {
        struct ui_region *region = &logical[i];
        struct wlr_box box = greeter_box(ctx, scale, region->x, region->y, region->w, region->h);
        ctx->regions[ctx->num_regions++] = (struct ui_region){region->type, box.x, box.y, box.width, box.height};
    }
}

static void render_side_greeter(struct compositor *comp,
                               struct side_ctx *ctx,
                               struct wlr_scene_tree *parent) {
    double scale = fmax(1.0, fmin(comp->output_height / 1080.0,
        fmin(ctx->width / 480.0, ctx->height / 300.0)));
    int ox = 0, oy = 0;
    int pw = (int)floor(ctx->width / scale);
    int ph = (int)floor(ctx->height / scale);

    /* Build clickable regions */
    build_side_regions(ctx, scale);
    if (ctx->auth || ctx->launch_pending) {
        greeter_rect(ctx, scale, parent, ox + 20, oy + 20, pw - 40, ph - 40, MODAL_PANEL_COLOR);
        greeter_text(ctx, scale, parent, ox + 20, oy + ph / 2 - 30, pw - 40, 60,
            ctx->launch_pending ? "Starting session..." :
                (strcmp(ctx->auth->action, "logout") == 0 ? "Ending session..." :
                    (configured_steam_user(ctx) ? "Preparing Steam..." : "Signing in...")),
            22.0, 1.0, 1.0, 1.0, 1.0, true);
        return;
    }
    if (ctx->auth_error) {
        greeter_text(ctx, scale, parent, ox + 20, oy + ph - 90, pw - 40, 50,
            "Could not complete request. Try again.", 16.0, 1.0, 0.6, 0.5, 1.0, true);
    }

    if (ctx->state == SIDE_CLAIMED) {
        /* Match the management overlay during session startup/recovery. */
        greeter_rect(ctx, scale, parent, ox + 20, oy + 20, pw - 40, ph - 40,
                  (float*)CLAIMED_COLOR);

        /* Steam owns the visible account flow; this is only its fallback. */
        char welcome[96];
        if (ctx->auth_error) {
            snprintf(welcome, sizeof(welcome), "Could not complete request");
        } else if (ctx->recovery_required) {
            snprintf(welcome, sizeof(welcome), "Session needs recovery");
        } else if (ctx->activity == 2 || ctx->activity == 1) {
            /* Never present a stopped or frozen session as "waiting": nothing will start it. */
            snprintf(welcome, sizeof(welcome), "%s session is %s",
                ctx->role == SIDE_ROLE_PRIMARY ? "Main" : "Guest",
                ctx->activity == 2 ? "off" : "in standby");
        } else {
            snprintf(welcome, sizeof(welcome), "%s",
                configured_steam_user(ctx) ? "Waiting for Steam..." : "Waiting for session...");
        }
        greeter_text(ctx, scale, parent,
            ox + 20, oy + ph / 2 - 30,
            pw - 40, 60,
            welcome, 22.0,
            1.0, 1.0, 1.0, 1.0, true);
        if (!ctx->auth_error && !ctx->recovery_required && (ctx->activity == 2 || ctx->activity == 1)) {
            greeter_text(ctx, scale, parent, ox + 20, oy + ph / 2 + 30, pw - 40, 48,
                "Start it from the Dosplex menu: Session, Session states, Active.",
                14.0, .8, .83, .9, 1.0, false);
        }

        if (configured_steam_user(ctx) && !ctx->auth_error && !ctx->recovery_required) return;

        /* Sign Out button */
        const float *btn_color = ctx->id == SIDE_LEFT ? BUTTON_LEFT_COLOR : BUTTON_RIGHT_COLOR;
        greeter_rect(ctx, scale, parent, ox + 30, oy + 200, pw - 60, 44, (float*)btn_color);
        greeter_text(ctx, scale, parent, ox + 30, oy + 200, pw - 60, 44,
            configured_steam_user(ctx) ? "END SESSION" : "SIGN OUT",
            18.0, 1.0, 1.0, 1.0, 1.0, true);

    } else if (configured_steam_user(ctx)) {
        greeter_text(ctx, scale, parent, 30, 72, pw - 60, 36,
            "Steam", 26.0, 1.0, 1.0, 1.0, 1.0, true);
        greeter_text(ctx, scale, parent, 30, 118, pw - 60, 56,
            "Sign in with QR or Steam Guard in Steam.",
            16.0, 0.8, 0.8, 0.9, 1.0, false);
        const float *color = ctx->id == SIDE_LEFT ? BUTTON_LEFT_COLOR : BUTTON_RIGHT_COLOR;
        greeter_rect(ctx, scale, parent, 30, 200, pw - 60, 44, (float*)color);
        greeter_text(ctx, scale, parent, 30, 200, pw - 60, 44,
            ctx->auth_error ? "TRY AGAIN" : "START STEAM", 18.0, 1.0, 1.0, 1.0, 1.0, true);
    } else {
        /* Side label badge */
        const float *label_color = ctx->id == SIDE_LEFT ? LABEL_LEFT_COLOR : LABEL_RIGHT_COLOR;
        greeter_rect(ctx, scale, parent, ox + 20, oy + 20, 120, 40, (float*)label_color);
        greeter_text(ctx, scale, parent, ox + 20, oy + 20, 120, 40,
            ctx->id == SIDE_LEFT ? "LEFT" : "RIGHT",
            18.0, 1.0, 1.0, 1.0, 1.0, true);

        /* "Dosplex" title above inputs */
        greeter_text(ctx, scale, parent, ox + 20, oy + 62, pw - 40, 24,
            "Dosplex", 16.0, 0.7, 0.7, 0.85, 1.0, false);

        /* Username label */
        greeter_text(ctx, scale, parent, ox + 30, oy + 72, 120, 18,
            "Username", 13.0, 0.6, 0.6, 0.75, 1.0, false);

        /* Username box */
        static const float focus_color[] = {0.20, 0.20, 0.30, 1.0};
        const float *input_color = (ctx->focused == UIelem_USERNAME)
            ? focus_color : INPUT_COLOR;
        greeter_rect(ctx, scale, parent, ox + 30, oy + 90, pw - 60, 40, (float*)input_color);

        /* Username text or placeholder */
        if (ctx->username[0]) {
            greeter_text_left(ctx, scale, parent, ox + 30, oy + 90, pw - 60, 40,
                ctx->username, 16.0, 0.95, 0.95, 0.95, 1.0, false);
        } else {
            greeter_text_left(ctx, scale, parent, ox + 30, oy + 90, pw - 60, 40,
                "username", 14.0, 0.4, 0.4, 0.55, 1.0, false);
        }

        /* Password label */
        greeter_text(ctx, scale, parent, ox + 30, oy + 130, 120, 18,
            "Password", 13.0, 0.6, 0.6, 0.75, 1.0, false);

        /* Password box */
        input_color = (ctx->focused == UIelem_PASSWORD)
            ? focus_color : INPUT_COLOR;
        greeter_rect(ctx, scale, parent, ox + 30, oy + 145, pw - 60, 40, (float*)input_color);

        /* Password as asterisks */
        if (ctx->password[0]) {
            size_t plen = strlen(ctx->password);
            char stars[65];
            size_t n = plen < 64 ? plen : 64;
            memset(stars, '*', n);
            stars[n] = '\0';
            greeter_text_left(ctx, scale, parent, ox + 30, oy + 145, pw - 60, 40,
                stars, 16.0, 0.95, 0.95, 0.95, 1.0, false);
        } else {
            greeter_text_left(ctx, scale, parent, ox + 30, oy + 145, pw - 60, 40,
                "password", 14.0, 0.4, 0.4, 0.55, 1.0, false);
        }

        /* Sign In button */
        const float *btn_color = ctx->id == SIDE_LEFT ? BUTTON_LEFT_COLOR : BUTTON_RIGHT_COLOR;
        greeter_rect(ctx, scale, parent, ox + 30, oy + 200, pw - 60, 44, (float*)btn_color);
        greeter_text(ctx, scale, parent, ox + 30, oy + 200, pw - 60, 44,
            "SIGN IN", 18.0, 1.0, 1.0, 1.0, 1.0, true);
    }
}

static void add_modal_region(struct compositor *comp, enum modal_action action,
        int x, int y, int w, int h, const char *device_name) {
    if (!comp || comp->modal_region_count >= (int)(sizeof(comp->modal_regions) / sizeof(comp->modal_regions[0]))) {
        return;
    }

    struct modal_region *region = &comp->modal_regions[comp->modal_region_count++];
    memset(region, 0, sizeof(*region));
    region->action = action;
    region->x = x;
    region->y = y;
    region->w = w;
    region->h = h;
    if (device_name) {
        snprintf(region->device_name, sizeof(region->device_name), "%s", device_name);
    }
}

/* Modal layout uses logical pixels; rendering and hit regions share this map. */
static struct wlr_box modal_box(struct compositor *comp, int x, int y, int w, int h) {
    return scaled_ui_box(comp->modal_origin_x, comp->modal_origin_y,
        comp->modal_scale, x, y, w, h);
}

static void modal_rect(struct compositor *comp, struct wlr_scene_tree *parent,
        int x, int y, int w, int h, const float *color) {
    struct wlr_box box = modal_box(comp, x, y, w, h);
    make_rect(parent, box.x, box.y, box.width, box.height, color);
}

static void modal_text(struct compositor *comp, struct wlr_scene_tree *parent,
        int x, int y, int w, int h, const char *text, double font_size,
        double r, double g, double b, double a, bool bold) {
    struct wlr_box box = modal_box(comp, x, y, w, h);
    /* Text that is wider than the panel wraps inside it, whatever the layout and the panel's width. */
    make_text_node_fit(parent, box.x, box.y, box.width, box.height, text,
        font_size * comp->modal_scale, r, g, b, a, bold);
}

static void render_modal_button(struct compositor *comp, struct wlr_scene_tree *parent,
        enum modal_action action, int x, int y, int w, int h, const char *label,
        bool active) {
    if (comp->modal_region_count == comp->overlay_focus) {
        modal_rect(comp, parent, x - 2, y - 2, w + 4, h + 4, MODAL_LINE_COLOR);
    }
    modal_rect(comp, parent, x, y, w, h, active ? MODAL_ACCENT_COLOR : MODAL_BOX_COLOR);
    struct wlr_box box = modal_box(comp, x, y, w, h);
    make_text_node(parent, box.x, box.y, box.width, box.height, label, 15.0 * comp->modal_scale,
        0.96, 0.97, 1.0, 1.0, true);
    add_modal_region(comp, action, box.x, box.y, box.width, box.height, NULL);
}

/* The helper must be a regular file that root (or the user running this compositor) owns and that
 * neither group nor others can write. `why` says what is wrong otherwise. */
static bool power_helper_usable(const char *path, char *why, size_t why_size) {
    struct stat info;
    if (!path || !path[0] || lstat(path, &info) != 0) {
        snprintf(why, why_size, "missing");
        return false;
    }
    if (!S_ISREG(info.st_mode)) snprintf(why, why_size, "not a regular file");
    else if (info.st_uid != 0 && info.st_uid != geteuid()) snprintf(why, why_size, "not owned by root");
    else if (info.st_mode & 0022) snprintf(why, why_size, "writable by others");
    else if (!(info.st_mode & S_IXUSR)) snprintf(why, why_size, "not executable");
    else return true;
    return false;
}

static double monotonic_seconds(void);

static const char *power_action_label(const char *action) {
    return strcmp(action, "suspend") == 0 ? "Sleep" : strcmp(action, "reboot") == 0 ? "Restart" :
        strcmp(action, "stop") == 0 ? "Turn off Dosplex" : "Shut down";
}

/* What the confirmation of Restart or Shut down says: both sessions close, and what the machine boots into only
 * when the launcher told the compositor (--boots-into): the trial and `dosplex start` leave normal SteamOS behind,
 * the display-manager installation starts Dosplex again. Sleep's text does not depend on it. */
static const char *power_confirmation_text(enum modal_action action, enum boots_into boots) {
    switch (action) {
    case MODAL_ACTION_POWER_RESTART:
        return boots == BOOTS_INTO_STEAMOS ?
            "Close both sessions and restart? Save first. The machine starts in normal SteamOS; "
                "start Dosplex from Steam's library to bring it back." :
            boots == BOOTS_INTO_DOSPLEX ?
            "Close both sessions and restart? Save first. The machine starts Dosplex again." :
            "Close both sessions and restart? Save first.";
    case MODAL_ACTION_POWER_SHUTDOWN:
        return boots == BOOTS_INTO_STEAMOS ?
            "Close both sessions and shut down? Save first. The machine turns off. "
                "Next start is normal SteamOS." :
            boots == BOOTS_INTO_DOSPLEX ?
            "Close both sessions and shut down? Save first. The machine turns off. Next start is Dosplex." :
            "Close both sessions and shut down? Save first. The machine turns off.";
    case MODAL_ACTION_POWER_EXIT:
        /* The machine stays on and nothing about the next boot changes, so the text is the same everywhere. */
        return "Turn off Dosplex? Both players' games close. Save first. The screen returns to normal SteamOS.";
    default:
        return "";
    }
}

static void power_panel_input_off(struct compositor *comp);
static void power_panel_down(struct compositor *comp);

/* Reaps the helper's first stage. For suspend that is the whole action. For reboot and poweroff the
 * first stage starts the helper's own unit, which stops this compositor, and waits for that unit (its
 * exit status is the unit's): a unit that fails while this compositor still runs, at any point, fails
 * the first stage. A failure takes the progress panel down again, gives the input back and says so on the
 * Session tab. So does a compositor that is still running at the deadline (see POWER_PANEL_DEADLINE_MS). */
static int power_poll(void *data) {
    struct compositor *comp = data;
    if (comp->power_pid > 0) {
        int status = 0;
        pid_t done = waitpid(comp->power_pid, &status, WNOHANG);
        if (done == 0 || (done < 0 && errno == EINTR)) {
            if (++comp->power_ticks == 600)
                wlr_log(WLR_ERROR, "Power helper for %s still runs after 60 s (pid=%d)",
                    comp->power_pid_action, comp->power_pid);
            wl_event_source_timer_update(comp->power_timer, 100);
            return 0;
        }
        bool reaped = done == comp->power_pid;
        bool ok = reaped && WIFEXITED(status) && WEXITSTATUS(status) == 0;
        wlr_log(ok ? WLR_INFO : WLR_ERROR, "Power helper for %s finished: %s %d", comp->power_pid_action,
            reaped && WIFSIGNALED(status) ? "signal" : "exit status",
            !reaped ? -1 : WIFSIGNALED(status) ? WTERMSIG(status) : WEXITSTATUS(status));
        comp->power_pid = 0;
        if (!ok) {
            bool panel = comp->power_action[0] != '\0';
            snprintf(comp->power_message, sizeof(comp->power_message), "%s failed. See the log.",
                power_action_label(comp->power_pid_action));
            comp->power_action[0] = '\0';
            if (panel) power_panel_down(comp);
        }
        write_runtime_state(comp);
        request_redraw(comp);
    }
    if (!comp->power_action[0]) return 0;
    double waited = monotonic_seconds() - comp->power_started;
    if (waited * 1000.0 < comp->power_panel_deadline_ms) {
        wl_event_source_timer_update(comp->power_timer, 250);
        return 0;
    }
    wlr_log(WLR_ERROR, "%s did not stop Dosplex within %.0f s; taking the progress panel down "
        "(see journalctl -u 'dosplex-power-*')", power_action_label(comp->power_action), waited);
    snprintf(comp->power_message, sizeof(comp->power_message), "%s did not finish. See the log.",
        power_action_label(comp->power_action));
    comp->power_action[0] = '\0';
    power_panel_down(comp);
    write_runtime_state(comp);
    request_redraw(comp);
    return 0;
}

/* Runs $DOSPLEX_ROOT/libexec/dosplex-power-action ACTION. Returns false, with power_message set, when
 * nothing was started. Suspend may fall back to systemctl; reboot and poweroff never run a bare
 * systemctl, which would leave the trial half restored. */
static bool launch_power_action(struct compositor *comp, const char *action) {
    if (!comp->config.allow_power || !action || !action[0]) return false;
    bool machine = strcmp(action, "suspend") != 0;
    if (comp->power_action[0] || comp->power_pid > 0) {
        wlr_log(WLR_ERROR, "Ignoring power action %s: %s is still in progress", action,
            comp->power_action[0] ? comp->power_action : comp->power_pid_action);
        return false;
    }
    comp->power_message[0] = '\0';
    char why[64];
    bool helper = power_helper_usable(comp->power_helper, why, sizeof(why));
    if (!helper) {
        wlr_log(WLR_ERROR, "Power helper %s is not usable (%s)", comp->power_helper, why);
        /* A stand-in was asked for: never fall back to the real systemctl. */
        if (machine || comp->config.power_command) {
            snprintf(comp->power_message, sizeof(comp->power_message), "Power helper missing");
            return false;
        }
    }
    if (comp->config.power_command)
        wlr_log(WLR_INFO, "Power action %s uses DOSPLEX_POWER_COMMAND %s (test hook)", action,
            comp->config.power_command);
    if (!comp->power_timer) {
        comp->power_timer = wl_event_loop_add_timer(wl_display_get_event_loop(comp->display), power_poll, comp);
        if (!comp->power_timer) {
            snprintf(comp->power_message, sizeof(comp->power_message), "Power action failed. See the log.");
            return false;
        }
    }

    pid_t pid = fork();
    if (pid < 0) {
        wlr_log(WLR_ERROR, "Failed to fork the Dosplex power helper: %s", strerror(errno));
        snprintf(comp->power_message, sizeof(comp->power_message), "Power action failed. See the log.");
        return false;
    }
    if (pid == 0) {
        spawn_child_reset();
        setsid();
        if (chdir("/") != 0) _exit(126);
        if (helper && comp->config.power_command)
            execl(comp->power_helper, "dosplex-power-action", "--power-command",
                comp->config.power_command, action, (char *)NULL);
        else if (helper)
            execl(comp->power_helper, "dosplex-power-action", action, (char *)NULL);
        else
            execlp("systemctl", "systemctl", "suspend", (char *)NULL);
        _exit(127);
    }
    comp->power_pid = pid;
    comp->power_ticks = 0;
    snprintf(comp->power_pid_action, sizeof(comp->power_pid_action), "%s", action);
    if (machine) {
        snprintf(comp->power_action, sizeof(comp->power_action), "%s", action);
        comp->power_started = monotonic_seconds();
        wlr_log(WLR_INFO, "Power action %s: input to both sides is held until Dosplex stops", action);
        power_panel_input_off(comp);
    }
    wl_event_source_timer_update(comp->power_timer, 100);
    wlr_log(WLR_INFO, "Requested Dosplex power action: %s via %s (pid=%d)", action,
        helper ? comp->power_helper : "systemctl", pid);
    return true;
}

static enum input_route_mode default_route_mode_for_device(struct compositor *comp,
        struct input_device_ctx *input) {
    if (!comp || !input) {
        return INPUT_ROUTE_LEFT;
    }

    const char *name = input->device && input->device->name ? input->device->name : "";
    if (env_list_matches_device("DOSPLEX_INPUT_LEFT_DEVICES", name)) {
        return INPUT_ROUTE_LEFT;
    }
    if (env_list_matches_device("DOSPLEX_INPUT_RIGHT_DEVICES", name)) {
        return INPUT_ROUTE_RIGHT;
    }
    if (input->device && input->device->type != WLR_INPUT_DEVICE_KEYBOARD &&
            env_list_matches_device("DOSPLEX_INPUT_BOTH_DEVICES", name)) {
        return INPUT_ROUTE_BOTH;
    }

    const char *default_side = getenv("DOSPLEX_INPUT_DEFAULT_SIDE");
    if (default_side) {
        enum input_route_mode mode = INPUT_ROUTE_LEFT;
        if (input_route_mode_from_string(default_side, &mode)) {
            if (mode == INPUT_ROUTE_BOTH &&
                    input->device && input->device->type == WLR_INPUT_DEVICE_KEYBOARD) {
                return INPUT_ROUTE_FOLLOW;
            }
            return mode;
        }
    }
    return INPUT_ROUTE_LEFT;
}

static enum input_route_mode configured_route_mode_for_device(struct compositor *comp,
        struct input_device_ctx *input) {
    if (!comp || !input) {
        return INPUT_ROUTE_LEFT;
    }
    if (input->device) {
        struct dynamic_input_route *route = NULL;
        wl_list_for_each(route, &comp->dynamic_input_routes, link) {
            if (route->device == input->device) {
                if (input->device->type == WLR_INPUT_DEVICE_KEYBOARD &&
                        route->mode == INPUT_ROUTE_BOTH) {
                    return default_route_mode_for_device(comp, input);
                }
                return route->mode;
            }
        }
    }

    return default_route_mode_for_device(comp, input);
}

static enum input_route_mode current_route_mode_for_device(struct compositor *comp,
        struct input_device_ctx *input) {
    return comp->layout == LAYOUT_SINGLE ? main_input_route_mode(comp) :
        configured_route_mode_for_device(comp, input);
}

static bool input_device_name_is_keyboard(struct compositor *comp,
        const char *device_name) {
    if (device_name && strcmp(device_name, "__dosplex_remote_input__") == 0) {
        return true;
    }
    if (!comp || !device_name || !device_name[0]) {
        return false;
    }

    struct input_device_ctx *input = NULL;
    wl_list_for_each(input, &comp->input_devices, link) {
        if (input->device && input->device->name &&
                strcmp(input->device->name, device_name) == 0) {
            return input->device->type == WLR_INPUT_DEVICE_KEYBOARD;
        }
    }
    return false;
}

static bool input_name_contains_ci(const char *name, const char *needle) {
    if (!name || !needle || !needle[0]) {
        return false;
    }
    size_t needle_len = strlen(needle);
    for (const char *p = name; *p; p++) {
        size_t i = 0;
        while (i < needle_len && p[i] &&
                tolower((unsigned char)p[i]) ==
                tolower((unsigned char)needle[i])) {
            i++;
        }
        if (i == needle_len) {
            return true;
        }
    }
    return false;
}

static enum input_route_mode input_route_mode_for_side(struct compositor *comp,
        enum side_id side) {
    if (!comp || comp->layout != LAYOUT_SPLIT_HORIZONTAL) {
        return side == SIDE_LEFT ? INPUT_ROUTE_LEFT : INPUT_ROUTE_RIGHT;
    }
    return side == SIDE_LEFT ? INPUT_ROUTE_LEFT : INPUT_ROUTE_RIGHT;
}

static enum input_route_mode main_input_route_mode(struct compositor *comp) {
    return input_route_mode_for_side(comp, comp ? comp->primary_side_id : SIDE_LEFT);
}

static enum input_route_mode guest_input_route_mode(struct compositor *comp) {
    return input_route_mode_for_side(comp, comp ? comp->guest_side_id : SIDE_RIGHT);
}

static const char *input_route_mode_role_label(struct compositor *comp,
        enum input_route_mode mode) {
    if (mode == INPUT_ROUTE_FOLLOW) {
        return "follow";
    }
    if (mode == INPUT_ROUTE_BOTH) {
        return "both";
    }
    struct side_ctx *side = side_for_input_route(comp, mode);
    if (!side) {
        return input_route_mode_name(mode);
    }
    return side->role == SIDE_ROLE_PRIMARY ? "main" : "guest";
}

static bool is_deck_onboard_input_name(const char *name) {
    /* External Steam Controllers are not the Deck's built-in controls.
     * This is a display grouping heuristic, never an ownership boundary. */
    return name && input_name_contains_ci(name, "steam deck");
}

static bool is_external_gamepad_input_name(const char *name) {
    return name && !is_deck_onboard_input_name(name) &&
        (input_name_contains_ci(name, "controller") ||
        input_name_contains_ci(name, "gamepad") ||
        input_name_contains_ci(name, "joystick") ||
        input_name_contains_ci(name, "dualsense") ||
        input_name_contains_ci(name, "dualshock") ||
        input_name_contains_ci(name, "xbox"));
}

static void external_gamepad_base_name(const char *name, char *out, size_t out_size) {
    if (!out || out_size == 0) {
        return;
    }
    snprintf(out, out_size, "%s", name ? name : "");
    const char *routed = "Dosplex routed ";
    size_t routed_len = strlen(routed);
    if (strncmp(out, routed, routed_len) == 0) {
        char *rest = out + routed_len;
        char *space = strchr(rest, ' ');
        if (space && space[1]) {
            memmove(out, space + 1, strlen(space + 1) + 1);
        }
    }
    const char *suffixes[] = {" Touchpad", " Motion Sensors", " Accelerometer", " Gyro"};
    for (size_t i = 0; i < sizeof(suffixes) / sizeof(suffixes[0]); ++i) {
        char *suffix = strstr(out, suffixes[i]);
        if (suffix) {
            *suffix = '\0';
            return;
        }
    }
}

static bool is_builtin_touchscreen_name(const char *name) {
    return name && input_name_contains_ci(name, "FTS3528:00 2808:1015") &&
        !input_name_contains_ci(name, "UNKNOWN");
}

/* The X11 backend merges every host mouse, keyboard and touchscreen into one device each and names
 * them after itself; the menu shows what they are. */
static const char *overlay_input_label(const char *name) {
    if (!name || !name[0]) return "Unknown device";
    if (strcmp(name, "x11-pointer") == 0) return "Mouse";
    if (strcmp(name, "x11-keyboard") == 0) return "Keyboard";
    if (strcmp(name, "x11-touch") == 0) return "Touchscreen";
    return name;
}

static bool input_device_should_show_in_overlay(struct input_device_ctx *input) {
    if (!input || !input->device) {
        return false;
    }

    const char *name = input->device->name ? input->device->name : "";
    if (is_deck_onboard_input_name(name) || is_builtin_touchscreen_name(name) ||
            is_external_gamepad_input_name(name)) {
        return true;
    }

    if (input_name_contains_ci(name, "Lid Switch") ||
            input_name_contains_ci(name, "AT Translated Set 2 keyboard") ||
            input_name_contains_ci(name, "steamos-manager") ||
            input_name_contains_ci(name, "Dosplex passthrough steamos-manager") ||
            input_name_contains_ci(name, "FTS3528:00 2808:1015 UNKNOWN")) {
        return false;
    }

    switch (input->device->type) {
    case WLR_INPUT_DEVICE_KEYBOARD:
    case WLR_INPUT_DEVICE_POINTER:
    case WLR_INPUT_DEVICE_TOUCH:
        return true;
    default:
        return false;
    }
}

static bool read_first_line(const char *path, char *out, size_t out_size) {
    if (!out || out_size == 0) {
        return false;
    }
    out[0] = '\0';
    FILE *fp = fopen(path, "r");
    if (!fp) {
        return false;
    }
    bool ok = fgets(out, out_size, fp) != NULL;
    fclose(fp);
    if (!ok) {
        out[0] = '\0';
        return false;
    }
    out[strcspn(out, "\r\n")] = '\0';
    return true;
}

static void sanitize_gamepad_route_name(const char *name, char *out, size_t out_size) {
    if (!out || out_size == 0) {
        return;
    }
    size_t j = 0;
    for (size_t i = 0; name && name[i] && j + 1 < out_size; ++i) {
        unsigned char c = (unsigned char)name[i];
        if (isalnum(c) || c == '-' || c == '_') {
            out[j++] = (char)c;
        } else {
            out[j++] = '_';
        }
    }
    out[j] = '\0';
}

static enum input_route_mode current_gamepad_route_mode(struct compositor *comp,
        const char *device_name) {
    if (!comp || !device_name || !device_name[0]) {
        return main_input_route_mode(comp);
    }
    if (comp->layout == LAYOUT_SINGLE) {
        return main_input_route_mode(comp);
    }

    char base[256];
    char safe[256];
    external_gamepad_base_name(device_name, base, sizeof(base));
    sanitize_gamepad_route_name(base, safe, sizeof(safe));
    if (!safe[0]) {
        return main_input_route_mode(comp);
    }
    char path[512];
    snprintf(path, sizeof(path), "/run/dosplex/gamepad-routes/%s", safe);
    char value[32];
    if (read_first_line(path, value, sizeof(value)) && strcmp(value, "guest") == 0) {
        return guest_input_route_mode(comp);
    }
    return main_input_route_mode(comp);
}

static void write_gamepad_route_owner(const char *device_name, const char *owner) {
    char base[256];
    char safe[256];
    external_gamepad_base_name(device_name, base, sizeof(base));
    sanitize_gamepad_route_name(base, safe, sizeof(safe));
    if (!safe[0]) {
        return;
    }
    mkdir("/run/dosplex", 0755);
    mkdir("/run/dosplex/gamepad-routes", 0755);
    char path[512];
    snprintf(path, sizeof(path), "/run/dosplex/gamepad-routes/%s", safe);
    FILE *fp = fopen(path, "w");
    if (!fp) {
        wlr_log(WLR_ERROR, "Failed to write gamepad route %s: %s", path, strerror(errno));
        return;
    }
    fprintf(fp, "%s\n", owner && strcmp(owner, "guest") == 0 ? "guest" : "main");
    fclose(fp);
    wlr_log(WLR_INFO, "Gamepad route %s -> %s", base, owner);
}

static void apply_external_gamepad_route(struct compositor *comp,
        const char *device_name, enum input_route_mode mode) {
    if (!comp || !is_external_gamepad_input_name(device_name)) {
        return;
    }

    struct side_ctx *target = primary_side(comp);
    if (mode == guest_input_route_mode(comp)) {
        target = guest_side(comp);
    } else if (mode == main_input_route_mode(comp) ||
            mode == INPUT_ROUTE_FOLLOW || mode == INPUT_ROUTE_BOTH) {
        target = primary_side(comp);
    } else {
        target = side_for_input_route(comp, mode);
    }
    bool guest_owner = target && target->role == SIDE_ROLE_GUEST;
    write_gamepad_route_owner(device_name, guest_owner ? "guest" : "main");
    // The arbiter owns physical-device grabs, permissions and virtual clones.
    // A second model-name based chmod here could expose another controller or
    // undo the arbiter's physical-device lock. Route changes are requests only;
    // inventory/acknowledgment and per-controller keys are still required.

}


static const char *friendly_input_kind(struct input_device_ctx *input) {
    const char *name = input && input->device && input->device->name ?
        input->device->name : "";

    if (input_name_contains_ci(name, "gamepad") ||
            input_name_contains_ci(name, "controller") ||
            input_name_contains_ci(name, "joystick")) {
        return "Gamepad";
    }

    if (!input || !input->device) {
        return "Device";
    }
    switch (input->device->type) {
    case WLR_INPUT_DEVICE_KEYBOARD:
        return "Keyboard";
    case WLR_INPUT_DEVICE_POINTER:
        return "Mouse";
    case WLR_INPUT_DEVICE_TOUCH:
        return "Touch";
    case WLR_INPUT_DEVICE_TABLET_TOOL:
    case WLR_INPUT_DEVICE_TABLET_PAD:
        return "Tablet";
    case WLR_INPUT_DEVICE_SWITCH:
    default:
        return "Device";
    }
}

static int next_friendly_input_index(const char *kind, int *keyboard_count,
        int *mouse_count, int *gamepad_count, int *touch_count,
        int *tablet_count, int *device_count) {
    if (strcmp(kind, "Keyboard") == 0) {
        return ++(*keyboard_count);
    }
    if (strcmp(kind, "Mouse") == 0) {
        return ++(*mouse_count);
    }
    if (strcmp(kind, "Gamepad") == 0) {
        return ++(*gamepad_count);
    }
    if (strcmp(kind, "Touch") == 0) {
        return ++(*touch_count);
    }
    if (strcmp(kind, "Tablet") == 0) {
        return ++(*tablet_count);
    }
    return ++(*device_count);
}

static bool audio_decode(const char *hex, char *output, size_t capacity) {
    if (strcmp(hex, "-") == 0) { output[0] = 0; return true; }
    size_t n = strlen(hex);
    if (n % 2 || n / 2 >= capacity) return false;
    for (size_t i = 0; i < n; i += 2) {
        if (!isxdigit((unsigned char)hex[i]) || !isxdigit((unsigned char)hex[i+1])) return false;
        char pair[3] = { hex[i], hex[i+1], 0 };
        unsigned value = (unsigned)strtoul(pair, NULL, 16);
        if (value == 0 || value < 32 || value == 127) return false;
        output[i/2] = (char)value;
    }
    output[n/2] = 0;
    return true;
}

static bool audio_parse(struct compositor *comp) {
    char *body = comp->audio_response;
    bool v3 = strncmp(body, "DOSPLEX_AUDIO_3\n", 15) == 0;
    bool v2 = v3 || strncmp(body, "DOSPLEX_AUDIO_2\n", 15) == 0;
    if ((!v2 && strncmp(body, "DOSPLEX_AUDIO_1\n", 15) != 0) || !strstr(body, "\nEND\n")) return false;
    char mix[16] = "off", effective[16] = "full";
    char separation[16] = "balanced";
    bool joined[2] = {false}, mix_seen = false, separation_seen = false;
    struct audio_device_choice devices[128];
    int count = 0;
    char selected[2][2][256] = {{{0}}};
    char states[2][2][24] = {{{0}}};
    bool seen[2][2] = {{false}};
    char *save = NULL;
    for (char *line = strtok_r(body + 15, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        if (strcmp(line, "END") == 0) break;
        if (strncmp(line, "E\t", 2) == 0) {
            if (!audio_decode(line + 2, comp->audio_message, sizeof(comp->audio_message))) return false;
            return true;
        }
        char type, first[16], second[24], id[512], label[512], extra;
        if (sscanf(line, "%c\t%15s\t%23s\t%511s\t%511s %c", &type, first, second, id, label, &extra) != 5) return false;
        if (type == 'M') {
            char output[256];
            if (!v2 || mix_seen || (strcmp(first, "off") && strcmp(first, "full") && strcmp(first, "separated")) ||
                    strlen(second) != 2 || strspn(second, "01") != 2 || !audio_decode(id, output, sizeof(output)) ||
                    (strcmp(label, "full") && strcmp(label, "separated"))) return false;
            snprintf(mix, sizeof(mix), "%s", first);
            snprintf(effective, sizeof(effective), "%.15s", label);
            joined[0] = second[0] == '1'; joined[1] = second[1] == '1'; mix_seen = true;
        } else if (type == 'P') {
            if (!v3 || separation_seen || (strcmp(first, "gentle") && strcmp(first, "balanced") && strcmp(first, "strong")) ||
                    strcmp(second, "-") || strcmp(id, "-") || strcmp(label, "-")) return false;
            snprintf(separation, sizeof(separation), "%s", first);
            separation_seen = true;
        } else if (type == 'S') {
            int side = strcmp(first, "left") == 0 ? 0 : strcmp(first, "right") == 0 ? 1 : -1;
            int kind = strcmp(second, "output") == 0 ? 0 : strcmp(second, "input") == 0 ? 1 : -1;
            if (side < 0 || kind < 0 || (strcmp(id, "selected") && strcmp(id, "muted") && strcmp(id, "disconnected"))) return false;
            if (!audio_decode(label, selected[side][kind], 256)) return false;
            snprintf(states[side][kind], 24, "%s", id);
            seen[side][kind] = true;
        } else if (type == 'D') {
            if (count >= 128 || (strcmp(second, "left") && strcmp(second, "right") && strcmp(second, "free") && strcmp(second, "shared"))) return false;
            struct audio_device_choice *device = &devices[count++];
            device->kind = strcmp(first, "output") == 0 ? 0 : strcmp(first, "input") == 0 ? 1 : -1;
            if (device->kind < 0 || !audio_decode(id, device->id, 256) || !device->id[0] || !audio_decode(label, device->label, 256)) return false;
            snprintf(device->owner, sizeof(device->owner), "%s", second);
        } else return false;
    }
    if (!seen[0][0] || !seen[0][1] || !seen[1][0] || !seen[1][1]) return false;
    if (v2 && !mix_seen) return false;
    if (v3 && !separation_seen) return false;
    snprintf(comp->audio_mix_mode, sizeof(comp->audio_mix_mode), "%s", mix);
    snprintf(comp->audio_separation, sizeof(comp->audio_separation), "%s", separation);
    snprintf(comp->audio_mix_effective, sizeof(comp->audio_mix_effective), "%s", effective);
    memcpy(comp->audio_joined, joined, sizeof(joined));
    memcpy(comp->audio_devices, devices, count * sizeof(devices[0]));
    memcpy(comp->audio_selected, selected, sizeof(selected));
    memcpy(comp->audio_state, states, sizeof(states));
    comp->audio_count = count;
    return true;
}

static void audio_start(struct compositor *comp, const char *device);

static int audio_poll(void *data) {
    struct compositor *comp = data;
    if (comp->audio_pid > 0) {
        ssize_t n;
        while ((n = read(comp->audio_fd, comp->audio_response + comp->audio_response_length,
                sizeof(comp->audio_response) - comp->audio_response_length - 1)) > 0) {
            comp->audio_response_length += (size_t)n;
        }
        comp->audio_response[comp->audio_response_length] = 0;
        int status = 0;
        pid_t done = waitpid(comp->audio_pid, &status, WNOHANG);
        if (done > 0) {
            while ((n = read(comp->audio_fd, comp->audio_response + comp->audio_response_length,
                    sizeof(comp->audio_response) - comp->audio_response_length - 1)) > 0)
                comp->audio_response_length += (size_t)n;
            comp->audio_response[comp->audio_response_length] = 0;
        }
        bool overflow = comp->audio_response_length == sizeof(comp->audio_response) - 1;
        if (!done && (++comp->audio_ticks >= 150 || overflow)) {
            if (kill(-comp->audio_pid, SIGKILL) < 0) kill(comp->audio_pid, SIGKILL);
            snprintf(comp->audio_message, sizeof(comp->audio_message), "Audio request timed out. Try again.");
        }
        if (done == comp->audio_pid || done < 0) {
            close(comp->audio_fd);
            comp->audio_pid = 0;
            if (done < 0 || !WIFEXITED(status) || WEXITSTATUS(status) || !audio_parse(comp)) {
                snprintf(comp->audio_message, sizeof(comp->audio_message), "Audio service unavailable. Try again.");
            }
            comp->audio_refresh_ticks = 0;
            comp->audio_revision++;
            write_runtime_state(comp);
            request_redraw(comp);
        }
    } else if (comp->management_overlay_visible && comp->overlay_tab == 3 && ++comp->audio_refresh_ticks >= 20) {
        audio_start(comp, NULL);
    }
    wl_event_source_timer_update(comp->audio_timer, 100);
    return 0;
}

static void audio_request(struct compositor *comp, const char *device, bool mix) {
    if (comp->audio_pid > 0) return;
    /* Audio exists only where the launcher configured a router. */
    const char *helper = comp->config.audio_router;
    if (!helper || !helper[0]) {
        snprintf(comp->audio_message, sizeof(comp->audio_message), "Audio is not available here.");
        return;
    }
    int pipefd[2];
    if (pipe(pipefd) < 0) return;
    fcntl(pipefd[0], F_SETFD, FD_CLOEXEC);
    fcntl(pipefd[1], F_SETFD, FD_CLOEXEC);
    pid_t pid = fork();
    if (pid == 0) {
        spawn_child_reset();
        setsid();
        dup2(pipefd[1], STDOUT_FILENO);
        close(pipefd[0]); close(pipefd[1]);
        if (device) execlp("python3", "python3", helper, "overlay", side_name(comp->management_overlay_owner),
            mix ? "mix" : comp->audio_kind == 0 ? "output" : "input", device, (char *)NULL);
        else execlp("python3", "python3", helper, "overlay", (char *)NULL);
        _exit(127);
    }
    close(pipefd[1]);
    if (pid < 0) { close(pipefd[0]); return; }
    fcntl(pipefd[0], F_SETFL, O_NONBLOCK);
    comp->audio_fd = pipefd[0];
    comp->audio_pid = pid;
    comp->audio_ticks = 0;
    comp->audio_response_length = 0;
    if (device) comp->audio_message[0] = 0;
    if (!comp->audio_timer) comp->audio_timer = wl_event_loop_add_timer(wl_display_get_event_loop(comp->display), audio_poll, comp);
    wl_event_source_timer_update(comp->audio_timer, 100);
    write_runtime_state(comp);
    request_redraw(comp);
}

static void audio_start(struct compositor *comp, const char *device) {
    audio_request(comp, device, false);
}


/* Root-owned broker inventory; never infer controllers from display names. */
static struct json_object *controller_inventory_file(const char *filename) {
    const char *runtime = getenv("XDG_RUNTIME_DIR");
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s", runtime ? runtime : "/run/dosplex", filename);
    int fd = open(path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return NULL;
    struct stat st;
    char data[65537];
    ssize_t n = -1;
    if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_uid == geteuid() &&
            !(st.st_mode & 0022) && st.st_size < 65536) n = read(fd, data, sizeof(data)-1);
    close(fd);
    if (n < 0) return NULL;
    data[n] = 0;
    struct json_object *obj = json_tokener_parse(data), *updated, *devices;
    if (!obj || !json_object_object_get_ex(obj, "updated", &updated) ||
            difftime(time(NULL), (time_t)json_object_get_double(updated)) > 5 ||
            !json_object_object_get_ex(obj, "devices", &devices) ||
            !json_object_is_type(devices, json_type_array) || json_object_array_length(devices) > 32) {
        if (obj) json_object_put(obj);
        return NULL;
    }
    return obj;
}

static const char *controller_text(struct json_object *obj, const char *key) {
    struct json_object *value;
    return json_object_object_get_ex(obj, key, &value) && json_object_is_type(value, json_type_string)
        ? json_object_get_string(value) : "";
}

/* The number the routing service gave a controller (1-based, stable), or 0 for a row from an older service
 * that has none, or a value that cannot be a number. The menu then falls back to the controller's name. */
static int controller_index(struct json_object *obj) {
    struct json_object *value;
    if (!json_object_object_get_ex(obj, "index", &value) || !json_object_is_type(value, json_type_int)) return 0;
    int64_t number = json_object_get_int64(value);
    return number > 0 && number < 1000 ? (int)number : 0;
}

/* What a message calls a controller: "Controller 2" when it has a number, else its name as before. */
static void controller_display_name(struct json_object *obj, char *out, size_t size) {
    int number = controller_index(obj);
    if (number) snprintf(out, size, "Controller %d", number);
    else snprintf(out, size, "%s", controller_text(obj, "name"));
}

/* Native HID and generic evdev brokers publish independently. Merge only
 * fresh, trusted snapshots; native rows replace stale generic remembered IDs. */
static struct json_object *controller_inventory(void) {
    struct json_object *base = controller_inventory_file("controllers.json");
    struct json_object *native = controller_inventory_file("controllers-native.json");
    if (!base) return native;
    if (!native) return base;
    struct json_object *devices = NULL, *extra = NULL, *version = NULL;
    json_object_object_get_ex(base, "devices", &devices);
    json_object_object_get_ex(native, "devices", &extra);
    int base_version = json_object_object_get_ex(base, "assignment_preview", &version) ? json_object_get_int(version) : 0;
    int native_version = json_object_object_get_ex(native, "assignment_preview", &version) ? json_object_get_int(version) : 0;
    json_object_object_add(base, "assignment_preview", json_object_new_int(base_version == 2 && native_version == 2 ? 2 : 0));
    for (size_t i = 0; i < json_object_array_length(extra); i++) {
        struct json_object *row = json_object_array_get_idx(extra, i);
        const char *key = controller_text(row, "id");
        size_t j;
        for (j = 0; j < json_object_array_length(devices); j++)
            if (!strcmp(key, controller_text(json_object_array_get_idx(devices, j), "id"))) break;
        if (j < 32) json_object_array_put_idx(devices, j, json_object_get(row));
    }
    json_object_put(native);
    return base;
}

static bool controller_key_valid(const char *key) {
    if (strlen(key) != 32) return false;
    for (int i = 0; i < 32; i++) if (!strchr("0123456789abcdef", key[i])) return false;
    return true;
}

static int controller_directory(void) {
    const char *path = getenv("DOSPLEX_CONTROLLER_DIRECTORY");
    if (!path) path = "/var/lib/dosplex/controllers";
    int dir = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat st;
    if (dir >= 0 && (fstat(dir, &st) || st.st_uid != geteuid() || (st.st_mode & 0022))) {
        close(dir); return -1;
    }
    return dir;
}

static struct json_object *controller_record(int dir, const char *filename, bool optional) {
    int fd = openat(dir, filename, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return optional && errno == ENOENT ? json_object_new_object() : NULL;
    char data[16384]; struct stat st; ssize_t n = -1;
    if (!fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == geteuid() &&
            !(st.st_mode & 0022) && st.st_size < (off_t)sizeof(data)) n = read(fd, data, sizeof(data)-1);
    close(fd);
    if (n < 0) return NULL;
    data[n] = 0;
    struct json_object *obj = json_tokener_parse(data);
    if (obj && !json_object_is_type(obj, json_type_object)) { json_object_put(obj); obj = NULL; }
    return obj;
}

static bool read_controller_requested_route(const char *key, char target[16]) {
    if (!controller_key_valid(key)) return false;
    int dir = controller_directory();
    if (dir < 0) return false;
    char filename[48]; snprintf(filename, sizeof(filename), "%s.json", key);
    struct json_object *record = controller_record(dir, filename, false);
    struct json_object *batch = controller_record(dir, "assignments.json", true), *assignments = NULL;
    close(dir);
    const char *value = record ? controller_text(record, "target") : "";
    if (batch && json_object_object_get_ex(batch, "assignments", &assignments) &&
            strlen(controller_text(assignments, key))) value = controller_text(assignments, key);
    bool ok = record && batch && (!strcmp(value, "main") || !strcmp(value, "guest") || !strcmp(value, "unassigned"));
    if (ok) snprintf(target, 16, "%s", value);
    if (record) json_object_put(record);
    if (batch) json_object_put(batch);
    return ok;
}

/* A single atomic record carries every confirmed assignment and one optional
 * two-device preview. A swap is never published as two independent requests. */
static bool request_controller_assignment(const char *key, const char *target,
        const char *other, const char *other_target, bool preview) {
    char previous[16];
    if (!read_controller_requested_route(key, previous) ||
            (other && !read_controller_requested_route(other, previous))) return false;
    int dir = controller_directory();
    if (dir < 0) return false;
    struct json_object *obj = controller_record(dir, "assignments.json", true), *assignments = NULL;
    if (!obj) { close(dir); return false; }
    if (!json_object_object_get_ex(obj, "assignments", &assignments)) {
        assignments = json_object_new_object(); json_object_object_add(obj, "assignments", assignments);
    }
    if (!json_object_is_type(assignments, json_type_object) || json_object_object_length(assignments) > 32) {
        json_object_put(obj); close(dir); return false;
    }
    json_object_object_del(obj, "preview");
    json_object_object_del(obj, "handoff");
    if (other) {
        struct json_object *pair = json_object_new_array();
        json_object_array_add(pair, json_object_new_string(key));
        json_object_array_add(pair, json_object_new_string(other));
        json_object_object_add(obj, "handoff", pair);
    }
    struct json_object *dest = assignments;
    if (preview) {
        char boot[64] = {0};
        int boot_fd = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC);
        ssize_t count = boot_fd >= 0 ? read(boot_fd, boot, sizeof(boot)-1) : -1;
        if (boot_fd >= 0) close(boot_fd);
        if (count <= 0) { json_object_put(obj); close(dir); return false; }
        boot[strcspn(boot, "\n")] = 0;
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        struct json_object *lease = json_object_new_object();
        json_object_object_add(lease, "boot_id", json_object_new_string(boot));
        json_object_object_add(lease, "expires", json_object_new_double(now.tv_sec + now.tv_nsec / 1e9 + 15));
        dest = json_object_new_object(); json_object_object_add(lease, "assignments", dest);
        json_object_object_add(obj, "preview", lease);
    }
    json_object_object_add(dest, key, json_object_new_string(target));
    if (other) json_object_object_add(dest, other, json_object_new_string(other_target));
    int fd = openat(dir, "assignments.request", O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    const char *text = json_object_to_json_string(obj);
    bool ok = fd >= 0 && write(fd, text, strlen(text)) == (ssize_t)strlen(text) && fsync(fd) == 0;
    if (fd >= 0) close(fd);
    if (ok) ok = renameat(dir, "assignments.request", dir, "assignments.json") == 0;
    if (!ok) unlinkat(dir, "assignments.request", 0);
    json_object_put(obj); close(dir); return ok;
}

static bool request_controller_route(const char *key, const char *target) {
    return request_controller_assignment(key, target, NULL, NULL, false);
}

/* A controller that is off, and was listed by the generic pad service, may be forgotten: its record, its number and its
 * row go. Root asks the routing service, which is the one process that removes a record (under the lock of the
 * numbering), with an empty file `<id>.forget` in the controller directory; the row leaves the page on its next
 * publication. A connected controller is refused here and again by the service. Returns false with a line for the menu. */
static void say_in_devices(struct compositor *comp, const char *text);
static bool forget_controller(struct compositor *comp, const char *key) {
    if (!controller_key_valid(key)) return false;
    struct json_object *inventory = controller_inventory(), *devices = NULL, *row = NULL;
    if (inventory) json_object_object_get_ex(inventory, "devices", &devices);
    for (size_t i = 0; devices && i < json_object_array_length(devices); i++) {
        struct json_object *d = json_object_array_get_idx(devices, i);
        if (!strcmp(controller_text(d, "id"), key)) { row = d; break; }
    }
    struct json_object *connected = NULL;
    bool online = row && json_object_object_get_ex(row, "connected", &connected) && json_object_get_boolean(connected);
    bool offered = row && !online && !strcmp(controller_text(row, "backend"), "generic-pad");
    char name[128] = "";
    if (row) controller_display_name(row, name, sizeof(name));
    if (inventory) json_object_put(inventory);
    if (online) { say_in_devices(comp, "Turn it off or unplug it first."); return false; }
    if (!offered) { say_in_devices(comp, "This controller cannot be forgotten."); return false; }
    int dir = controller_directory();
    char filename[48];
    snprintf(filename, sizeof(filename), "%s.forget", key);
    int fd = dir < 0 ? -1 : openat(dir, filename, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    bool ok = fd >= 0 || (dir >= 0 && errno == EEXIST);
    if (fd >= 0) close(fd);
    if (dir >= 0) close(dir);
    char line[160];
    snprintf(line, sizeof(line), ok ? "Forgetting %s." : "Could not forget %s.", name);
    say_in_devices(comp, line);
    return ok;
}

static double monotonic_seconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec + now.tv_nsec / 1e9;
}

/* The broker marks a controller "active" for about half a second after a press. Keep the time of the
 * last mark, so that Swap can tell which of a player's controllers was used last. Every read of the
 * inventory that the Devices tab makes passes through here. */
static void note_controller_activity(struct compositor *comp, struct json_object *devices) {
    double now = monotonic_seconds();
    for (size_t i = 0; devices && i < json_object_array_length(devices); i++) {
        struct json_object *d = json_object_array_get_idx(devices, i), *active;
        const char *key = controller_text(d, "id");
        if (controller_key_valid(key) && json_object_object_get_ex(d, "active", &active) &&
                json_object_get_boolean(active)) activity_note(&comp->controller_activity, key, now);
    }
}

struct control_candidate {
    char key[256], label[128];
    enum input_route_mode route;
    double last_active; /* CLOCK_MONOTONIC seconds of its last press; 0 when never seen */
};

/* Count only connected controls that can reopen the menu. Mouse/touch alone,
 * stale broker rows and the development remote-injection socket are not a
 * recovery path. libinput power/media-only interfaces are not keyboards here. */
static int menu_controls(struct compositor *comp, struct control_candidate out[64]) {
    int count = 0;
    struct input_device_ctx *input;
    wl_list_for_each(input, &comp->input_devices, link) {
        if (count == 64 || !input->keyboard || !input_device_should_show_in_overlay(input) ||
                wlr_input_device_is_x11(input->device)) continue;
        if (wlr_input_device_is_libinput(input->device)) {
            struct libinput_device *device = wlr_libinput_get_device_handle(input->device);
            if (!libinput_device_keyboard_has_key(device, KEY_M) ||
                    !libinput_device_keyboard_has_key(device, KEY_ENTER) ||
                    !libinput_device_keyboard_has_key(device, KEY_LEFTMETA) ||
                    !libinput_device_keyboard_has_key(device, KEY_LEFTSHIFT)) continue;
        } else if (!comp->config.host_keyboards) continue; /* Test and nested inputs only. */
        snprintf(out[count].key, 256, "input:%u", input->route_id);
        snprintf(out[count].label, 128, "%s", input->device->name);
        out[count].last_active = input->last_key_seconds;
        out[count++].route = configured_route_mode_for_device(comp, input);
    }
    struct json_object *inventory = controller_inventory(), *devices = NULL, *version = NULL;
    if (inventory && json_object_object_get_ex(inventory, "assignment_preview", &version) &&
            json_object_get_int(version) == 2) json_object_object_get_ex(inventory, "devices", &devices);
    note_controller_activity(comp, devices);
    for (size_t i = 0; devices && i < json_object_array_length(devices) && count < 64; i++) {
        struct json_object *d = json_object_array_get_idx(devices, i), *connected, *status, *ready;
        const char *key = controller_text(d, "id"); char route[16];
        if (!json_object_object_get_ex(d, "connected", &connected) || !json_object_get_boolean(connected) ||
                !json_object_object_get_ex(d, "input_status", &status) ||
                !json_object_object_get_ex(status, "shortcut_ready", &ready) || !json_object_get_boolean(ready) ||
                !read_controller_requested_route(key, route) || strcmp(route, controller_text(d, "target"))) continue;
        snprintf(out[count].key, 256, "controller:%s", key);
        controller_display_name(d, out[count].label, 128);
        out[count].last_active = activity_last(&comp->controller_activity, key);
        out[count++].route = !strcmp(route, "main") ? main_input_route_mode(comp) :
            !strcmp(route, "guest") ? guest_input_route_mode(comp) : INPUT_ROUTE_FOLLOW;
    }
    if (inventory) json_object_put(inventory);
    return count;
}

static bool main_control_remains(struct compositor *comp, const char *moving) {
    struct control_candidate controls[64]; int count = menu_controls(comp, controls);
    for (int i = 0; i < count; i++)
        if (strcmp(controls[i].key, moving) && controls[i].route == main_input_route_mode(comp)) return true;
    return false;
}

static bool guest_control_remains(struct compositor *comp, const char *moving) {
    struct control_candidate controls[64]; int count = menu_controls(comp, controls);
    for (int i = 0; i < count; i++)
        if (strcmp(controls[i].key, moving) && controls[i].route == guest_input_route_mode(comp)) return true;
    return false;
}

/* The two controls Swap exchanges: pair[0] is Main's, pair[1] is Guest's. With one eligible control on
 * each side that is the pair, as before. A player may hold several: then the one pressed last is meant,
 * and when that cannot be told the button stays and asks for a press. */
static enum swap_choice control_swap_pair(struct compositor *comp, struct control_candidate pair[2]) {
    struct control_candidate controls[64];
    struct swap_control view[64];
    int origin[64], count = menu_controls(comp, controls), listed = 0, main_index = -1, guest_index = -1;
    for (int i = 0; i < count; i++) {
        bool is_main = controls[i].route == main_input_route_mode(comp);
        if (!is_main && controls[i].route != guest_input_route_mode(comp)) continue;
        view[listed] = (struct swap_control){controls[i].key, !is_main, controls[i].last_active};
        origin[listed++] = i;
    }
    enum swap_choice choice = choose_swap_pair(view, listed, monotonic_seconds(), &main_index, &guest_index);
    if (choice == SWAP_READY) {
        pair[0] = controls[origin[main_index]];
        pair[1] = controls[origin[guest_index]];
    }
    return choice;
}

/* Who is in the shared mix, for the status line of the Audio tab. A player who is out either
 * has an output of their own or none. */
static void audio_mix_members(const struct compositor *comp, char *out, size_t size) {
    bool main_in = comp->audio_joined[comp->primary_side_id];
    bool guest_in = comp->audio_joined[comp->guest_side_id];
    if (main_in == guest_in) {
        snprintf(out, size, "%s", main_in ? "Main and Guest" : "nobody has joined");
        return;
    }
    enum side_id out_side = main_in ? comp->guest_side_id : comp->primary_side_id;
    snprintf(out, size, "%s only (%s %s)", main_in ? "Main" : "Guest", main_in ? "Guest" : "Main",
        strcmp(comp->audio_state[out_side][0], "muted") == 0 ? "has no output" : "uses own output");
}

/* The page of the Devices tab that the open menu is on. Each menu owner has its own: Main's list also holds the
 * host's mouse, keyboard and touchscreen, so Main's second page is not Guest's, and a page that both menus
 * shared left Guest on an empty one after Main had been to the second. */
static int *owner_device_page(struct compositor *comp) {
    return &comp->overlay_device_page[comp->management_overlay_owner == SIDE_RIGHT ? 1 : 0];
}

/* Whether this side's applications get no input: its own player has the management overlay
 * open, or the power progress panel covers both sides. Input that Dosplex does not route itself (a
 * controller passed straight to a side) is not held here. */
static bool side_input_held(struct compositor *comp, enum side_id id) {
    if (comp->power_action[0]) return true;
    if (!comp->management_overlay_visible) return false;
    if (comp->management_overlay_owner == id) return true;
    /* Main's overlay is drawn over Guest's picture in Guest full screen: the picture it covers gets no input. */
    struct side_ctx *shown = shown_side(comp);
    return shown && shown->id == id && comp->management_overlay_owner != id;
}

/* The whole output, above both sides: Steam closes on both sides, then Dosplex stops and the
 * machine restarts or turns off. Nothing on it can be pressed, and side_input_held() keeps the
 * pointer, keyboards and touch away from the applications under it. */
static void render_power_progress(struct compositor *comp, struct wlr_scene_tree *parent) {
    int w = comp->output_width, h = comp->output_height;
    double scale = fmax(1.0, h / 1080.0);
    make_rect(parent, 0, 0, w, h, MODAL_PANEL_COLOR);
    const char *text = strcmp(comp->power_action, "poweroff") == 0 ?
        "Closing Steam on both sides, then shutting down…" :
        strcmp(comp->power_action, "stop") == 0 ?
        "Closing Steam on both sides, then returning to SteamOS…" :
        "Closing Steam on both sides, then restarting…";
    make_text_node(parent, 0, h / 2 - (int)(40 * scale), w, (int)(48 * scale), text, 26.0 * scale,
        0.96, 0.97, 1.0, 1.0, true);
    make_text_node(parent, 0, h / 2 + (int)(12 * scale), w, (int)(32 * scale),
        "Please wait. This can take a minute.", 16.0 * scale, 0.75, 0.78, 0.86, 1.0, false);
}

/* === Performance numbers: FPS counter, recording, host sensors (T-434) ===
 *
 * What is measured: the commits that carry a buffer on a side's toplevel, timed on CLOCK_MONOTONIC when the
 * compositor handles them. That is the stream of frames the side's Gamescope delivers to Dosplex, which is what the
 * player sees. A game's own frame rate can differ: Gamescope repeats a frame when the game is slower than its output,
 * and drops frames when it is faster. Nothing here is drawn into or sent to the side, so the numbers cost the side
 * nothing but the ring write.
 *
 * Files: a CSV for each side that records, under PERF_DEFAULT_ROOT (root-owned, so a side's account can neither
 * redirect nor tamper with it; the newest PERF_KEEP_FILES are kept), and fps-counter.conf in the same directory, where
 * the counter modes are remembered. DOSPLEX_PERF_ROOT and DOSPLEX_PERF_SYSFS_ROOT (a directory that stands in for
 * / when the sensors are read) are test hooks and count only with --test-hooks. */
#define PERF_DEFAULT_ROOT "/var/lib/dosplex/perf"
#define PERF_KEEP_FILES 20
#define PERF_SECOND_NS 1000000000ull
#define PERF_LONG_WINDOW_NS (5 * PERF_SECOND_NS)
#define PERF_SETTINGS_FILE "fps-counter.conf"

struct perf_stats {
    unsigned frames, intervals;
    double fps, avg_ms, p99_ms, last_ms;
};

enum perf_host_field {
    PH_CPU_LOAD, PH_GPU_BUSY, PH_CPU_TEMP, PH_GPU_TEMP, PH_POWER, PH_BATTERY, PH_SCLK, PH_CPU_MHZ, PH_COUNT
};

struct perf_host {
    bool has[PH_COUNT];
    double value[PH_COUNT];
};

static const char *const perf_csv_header =
    "timestamp_ms,side,fps,frametime_ms_avg,frametime_ms_p99,cpu_load_pct,gpu_busy_pct,cpu_temp_c,gpu_temp_c,"
    "power_w,battery_pct,gpu_sclk_mhz,cpu_mhz\n";

static uint64_t perf_now_ns(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * PERF_SECOND_NS + (uint64_t)now.tv_nsec;
}

static void perf_note_commit(struct side_ctx *ctx) {
    struct side_perf *perf = &ctx->perf;
    perf->stamp_ns[perf->head] = perf_now_ns();
    perf->head = (perf->head + 1) % PERF_RING;
    if (perf->count < PERF_RING) perf->count++;
    perf->total++;
}

static int perf_compare_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

/* The frames that arrived in the window that ends at `now`, and the intervals between each of them and the frame
 * before it (a frame that follows a stall carries the stall). p99 is the nearest rank. */
static void perf_compute(const struct side_perf *perf, uint64_t now, uint64_t window_ns, struct perf_stats *out) {
    memset(out, 0, sizeof(*out));
    double intervals[PERF_RING];
    unsigned n = 0;
    double sum = 0;
    for (unsigned i = 0; i < perf->count; i++) {
        unsigned index = (perf->head + PERF_RING - 1 - i) % PERF_RING;
        uint64_t stamp = perf->stamp_ns[index];
        if (now > stamp && now - stamp > window_ns) break;
        out->frames++;
        if (i + 1 < perf->count) {
            unsigned before = (index + PERF_RING - 1) % PERF_RING;
            double ms = (double)(stamp - perf->stamp_ns[before]) / 1e6;
            if (n == 0) out->last_ms = ms;
            intervals[n++] = ms;
            sum += ms;
        }
    }
    out->fps = (double)out->frames * (double)PERF_SECOND_NS / (double)window_ns;
    out->intervals = n;
    if (n) {
        out->avg_ms = sum / n;
        qsort(intervals, n, sizeof(intervals[0]), perf_compare_double);
        unsigned rank = (unsigned)ceil(0.99 * n);
        out->p99_ms = intervals[(rank ? rank : 1) - 1];
    }
}

static const char *perf_role_label(const struct side_ctx *side) {
    return side->role == SIDE_ROLE_PRIMARY ? "main" : "guest";
}

static const char *fps_mode_name(enum fps_mode mode) {
    return mode == FPS_MODE_FPS ? "fps" : mode == FPS_MODE_FRAMETIME ? "fps+frametime" : "off";
}

static bool fps_mode_parse(const char *text, enum fps_mode *mode) {
    if (!text) return false;
    if (!strcmp(text, "off")) *mode = FPS_MODE_OFF;
    else if (!strcmp(text, "fps")) *mode = FPS_MODE_FPS;
    else if (!strcmp(text, "fps+frametime")) *mode = FPS_MODE_FRAMETIME;
    else return false;
    return true;
}

/* The line of the counter: 1 s of FPS and frame time, the 99th percentile over 5 s. */
static void perf_text(const struct side_ctx *side, uint64_t now, char *out, size_t size) {
    struct perf_stats shortw, longw;
    perf_compute(&side->perf, now, PERF_SECOND_NS, &shortw);
    perf_compute(&side->perf, now, PERF_LONG_WINDOW_NS, &longw);
    if (side->perf.mode == FPS_MODE_FRAMETIME && shortw.intervals)
        snprintf(out, size, "%.0f FPS  %.1f ms  p99 %.1f ms", shortw.fps, shortw.avg_ms, longw.p99_ms);
    else
        snprintf(out, size, "%.0f FPS", shortw.fps);
}

/* The directory is ours: a directory, owned by whoever runs the compositor, that no one else can write. */
static bool perf_dir_trusted(const char *root) {
    struct stat info;
    return lstat(root, &info) == 0 && S_ISDIR(info.st_mode) && info.st_uid == geteuid() && !(info.st_mode & 0022);
}

static bool perf_ensure_dir(const char *root) {
    if (mkdir(root, 0755) != 0 && errno != EEXIST) return false;
    return perf_dir_trusted(root);
}

static void perf_settings_save(struct compositor *comp) {
    if (!perf_ensure_dir(comp->perf_root)) {
        wlr_log(WLR_ERROR, "FPS counter settings not saved: %s is not usable: %s", comp->perf_root, strerror(errno));
        return;
    }
    char temp[320], path[320];
    snprintf(temp, sizeof(temp), "%s/.fps-counter-XXXXXX", comp->perf_root);
    snprintf(path, sizeof(path), "%s/%s", comp->perf_root, PERF_SETTINGS_FILE);
    int fd = mkstemp(temp);
    if (fd < 0) return;
    FILE *fp = fdopen(fd, "w");
    if (!fp) { close(fd); unlink(temp); return; }
    fprintf(fp, "left=%s\nright=%s\n", fps_mode_name(comp->left.perf.mode), fps_mode_name(comp->right.perf.mode));
    bool failed = ferror(fp) != 0;
    if (fchmod(fd, 0644) != 0) failed = true;
    if (fclose(fp) != 0) failed = true;
    if (failed || rename(temp, path) != 0) unlink(temp);
}

static void perf_settings_load(struct compositor *comp) {
    if (!perf_dir_trusted(comp->perf_root)) return;
    char path[320];
    snprintf(path, sizeof(path), "%s/%s", comp->perf_root, PERF_SETTINGS_FILE);
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    FILE *fp = fd >= 0 ? fdopen(fd, "r") : NULL;
    if (!fp) { if (fd >= 0) close(fd); return; }
    char line[96];
    while (fgets(line, sizeof(line), fp)) {
        char key[16], value[32];
        enum fps_mode mode;
        if (sscanf(line, "%15[^=]=%31s", key, value) != 2 || !fps_mode_parse(value, &mode)) continue;
        if (!strcmp(key, "left")) comp->left.perf.mode = mode;
        else if (!strcmp(key, "right")) comp->right.perf.mode = mode;
    }
    fclose(fp);
}

/* --- The built-in screen's brightness (T-436) ---
 *
 * Steam's brightness slider inside a side does not reach the panel, so Main (the driver) sets it here: one control
 * in the Dosplex menu's Display tab, and the control socket. The compositor runs as root and writes the kernel's
 * backlight class directly. The device is the first of the types raw, firmware, platform that has a
 * max_brightness above 0 (the Steam Deck's is amdgpu_bl0, a raw device); among equals the smallest name wins. The
 * value never goes below 5 % of max, so the screen cannot be set to black. The last percentage is remembered in
 * brightness.conf (root-owned directory, like fps-counter.conf) and written back when the compositor starts. Nothing
 * is written when it stops: the screen stays as the player left it.
 * DOSPLEX_SCREEN_SYSFS_ROOT (stands in for / when /sys/class/backlight is read) and DOSPLEX_SCREEN_ROOT (where
 * brightness.conf goes) are test hooks and count only with --test-hooks; see screen_brightness_init for
 * which compositors use the real device. */
#define SCREEN_BL_DEFAULT_ROOT "/var/lib/dosplex/screen"
#define SCREEN_BL_FILE "brightness.conf"
#define SCREEN_BL_MIN_PERCENT 5
#define SCREEN_BL_STEP_PERCENT 5

static int screen_bl_type_rank(const char *type) {
    return !strcmp(type, "raw") ? 0 : !strcmp(type, "firmware") ? 1 : !strcmp(type, "platform") ? 2 : -1;
}

static bool screen_bl_find(struct compositor *comp) {
    char base[320];
    snprintf(base, sizeof(base), "%s/sys/class/backlight", comp->screen_bl_sysfs_root);
    DIR *handle = opendir(base);
    if (!handle) return false;
    int best_rank = 99, best_max = 0;
    char best[128] = "";
    struct dirent *entry;
    while ((entry = readdir(handle))) {
        if (entry->d_name[0] == '.' || strlen(entry->d_name) > 100) continue;
        char path[512], type[32];
        int max = 0;
        snprintf(path, sizeof(path), "%s/%s/type", base, entry->d_name);
        if (!read_first_line(path, type, sizeof(type))) continue;
        int rank = screen_bl_type_rank(type);
        snprintf(path, sizeof(path), "%s/%s/max_brightness", base, entry->d_name);
        if (rank < 0 || !read_int_file(path, &max) || max <= 0) continue;
        snprintf(path, sizeof(path), "%s/%s/brightness", base, entry->d_name);
        if (access(path, W_OK) != 0) continue;
        if (rank < best_rank || (rank == best_rank && strcmp(entry->d_name, best) < 0)) {
            best_rank = rank;
            best_max = max;
            snprintf(best, sizeof(best), "%s", entry->d_name);
        }
    }
    closedir(handle);
    if (!best[0]) return false;
    snprintf(comp->screen_bl_dir, sizeof(comp->screen_bl_dir), "%s/%s", base, best);
    comp->screen_bl_max = best_max;
    return true;
}

static int screen_bl_min_raw(const struct compositor *comp) {
    long long raw = ((long long)comp->screen_bl_max * SCREEN_BL_MIN_PERCENT + 99) / 100;
    return raw < 1 ? 1 : (int)raw;
}

static int screen_bl_percent_of(const struct compositor *comp, int raw) {
    long long percent = ((long long)raw * 100 + comp->screen_bl_max / 2) / comp->screen_bl_max;
    return percent < SCREEN_BL_MIN_PERCENT ? SCREEN_BL_MIN_PERCENT : percent > 100 ? 100 : (int)percent;
}

static void screen_bl_settings_save(struct compositor *comp) {
    if (!comp->screen_bl_settings[0]) return;
    if (!perf_ensure_dir(comp->screen_bl_settings)) {
        wlr_log(WLR_ERROR, "Brightness not remembered: %s is not usable: %s", comp->screen_bl_settings, strerror(errno));
        return;
    }
    char temp[320], path[320];
    snprintf(temp, sizeof(temp), "%s/.brightness-XXXXXX", comp->screen_bl_settings);
    snprintf(path, sizeof(path), "%s/%s", comp->screen_bl_settings, SCREEN_BL_FILE);
    int fd = mkstemp(temp);
    if (fd < 0) return;
    FILE *fp = fdopen(fd, "w");
    if (!fp) { close(fd); unlink(temp); return; }
    fprintf(fp, "percent=%d\n", comp->screen_bl_percent);
    bool failed = ferror(fp) != 0;
    if (fchmod(fd, 0644) != 0) failed = true;
    if (fclose(fp) != 0) failed = true;
    if (failed || rename(temp, path) != 0) unlink(temp);
}

static bool screen_bl_settings_load(struct compositor *comp, int *percent) {
    if (!comp->screen_bl_settings[0] || !perf_dir_trusted(comp->screen_bl_settings)) return false;
    char path[320];
    snprintf(path, sizeof(path), "%s/%s", comp->screen_bl_settings, SCREEN_BL_FILE);
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    FILE *fp = fd >= 0 ? fdopen(fd, "r") : NULL;
    if (!fp) { if (fd >= 0) close(fd); return false; }
    char line[64];
    bool found = false;
    int value;
    while (fgets(line, sizeof(line), fp)) {
        if (sscanf(line, "percent=%d", &value) == 1 && value >= 0 && value <= 100) { *percent = value; found = true; }
    }
    fclose(fp);
    return found;
}

/* What the last drawn menu offered, for the runtime state (the tests read it). */
static void screen_bl_note_shown(struct compositor *comp, bool shown) {
    if (comp->screen_bl_shown == shown) return;
    comp->screen_bl_shown = shown;
    write_runtime_state(comp);
}

/* Set the panel to `percent` (0..100, raised to the minimum). Returns the percentage now in effect, or -1. */
static int screen_brightness_set(struct compositor *comp, int percent, bool remember) {
    if (!comp->screen_bl_available || percent < 0 || percent > 100) return -1;
    long long raw = ((long long)comp->screen_bl_max * percent + 50) / 100;
    if (raw < screen_bl_min_raw(comp)) raw = screen_bl_min_raw(comp);
    if (raw > comp->screen_bl_max) raw = comp->screen_bl_max;
    char path[512];
    snprintf(path, sizeof(path), "%s/brightness", comp->screen_bl_dir);
    if (!write_int_file(path, (int)raw)) {
        wlr_log(WLR_ERROR, "Failed to set the screen brightness: %s", path);
        return -1;
    }
    comp->screen_bl_percent = screen_bl_percent_of(comp, (int)raw);
    /* The idle dimmer restores the value it saved; a change by the player while the screen is lit is the new one. */
    if (comp->screen_bl_sysfs_root[0] == '\0' && comp->backlight_available && !comp->backlight_dimmed &&
            strcmp(strrchr(comp->screen_bl_dir, '/') + 1, "amdgpu_bl0") == 0)
        comp->saved_backlight_brightness = (int)raw;
    wlr_log(WLR_INFO, "Screen brightness set to %d%% (%lld of %d)", comp->screen_bl_percent, raw, comp->screen_bl_max);
    if (remember) screen_bl_settings_save(comp);
    write_runtime_state(comp);
    request_redraw(comp);
    return comp->screen_bl_percent;
}

/* One step brighter (+1) or dimmer (-1), on the 5 % grid. */
static void screen_brightness_step(struct compositor *comp, int direction) {
    if (!comp->screen_bl_available) return;
    char path[512];
    int raw = 0;
    snprintf(path, sizeof(path), "%s/brightness", comp->screen_bl_dir);
    int current = read_int_file(path, &raw) ? screen_bl_percent_of(comp, raw) : comp->screen_bl_percent;
    int target = direction > 0 ? (current / SCREEN_BL_STEP_PERCENT + 1) * SCREEN_BL_STEP_PERCENT :
        (current + SCREEN_BL_STEP_PERCENT - 1) / SCREEN_BL_STEP_PERCENT * SCREEN_BL_STEP_PERCENT - SCREEN_BL_STEP_PERCENT;
    if (target > 100) target = 100;
    if (target < SCREEN_BL_MIN_PERCENT) target = SCREEN_BL_MIN_PERCENT;
    screen_brightness_set(comp, target, true);
}

static void screen_brightness_init(struct compositor *comp) {
    comp->screen_bl_available = false;
    comp->screen_bl_sysfs_root[0] = comp->screen_bl_settings[0] = '\0';
    const char *root = getenv("DOSPLEX_SCREEN_ROOT"), *sysfs = getenv("DOSPLEX_SCREEN_SYSFS_ROOT");
    bool hooks = comp->config.test_hooks;
    bool fake = hooks && sysfs && sysfs[0] == '/';
    bool headless = strcmp(comp->config.backend, "headless") == 0;
    if (!hooks && (root || sysfs))
        wlr_log(WLR_ERROR, "Ignoring DOSPLEX_SCREEN_ROOT and DOSPLEX_SCREEN_SYSFS_ROOT: the compositor was not started with --test-hooks");
    /* The installed product runs with --test-hooks on the x11 backend, so --test-hooks alone must not switch the real
     * backlight off. The fake roots are the test hooks; a headless compositor without them leaves the host alone;
     * anything else uses the real device, and only as root (a developer's x11 test run cannot write the panel). */
    wlr_log(WLR_INFO, "Screen brightness source: %s (test_hooks=%d backend=%s)",
        fake ? "fake-root" : headless ? "none" : "real", hooks, comp->config.backend);
    if (fake) {
        snprintf(comp->screen_bl_sysfs_root, sizeof(comp->screen_bl_sysfs_root), "%s", sysfs);
        if (root && root[0] == '/') snprintf(comp->screen_bl_settings, sizeof(comp->screen_bl_settings), "%s", root);
    } else if (headless) {
        return;
    } else if (geteuid() != 0) {
        wlr_log(WLR_INFO, "Screen brightness: not running as root, the real backlight is left alone");
        return;
    } else {
        snprintf(comp->screen_bl_settings, sizeof(comp->screen_bl_settings), "%s",
            hooks && root && root[0] == '/' ? root : SCREEN_BL_DEFAULT_ROOT);
    }
    if (!screen_bl_find(comp)) return;
    comp->screen_bl_available = true;
    char path[512];
    int raw = 0, saved = 0;
    snprintf(path, sizeof(path), "%s/brightness", comp->screen_bl_dir);
    comp->screen_bl_percent = read_int_file(path, &raw) ? screen_bl_percent_of(comp, raw) : 50;
    if (screen_bl_settings_load(comp, &saved)) screen_brightness_set(comp, saved, false);
    wlr_log(WLR_INFO, "Screen brightness: %s, max %d, now %d%%", comp->screen_bl_dir, comp->screen_bl_max,
        comp->screen_bl_percent);
}

/* --- Host sensors. Every reader gives "no value" for a file that is missing or unreadable. --- */

static void perf_sysfs_path(const struct compositor *comp, char *out, size_t size, const char *rel) {
    snprintf(out, size, "%s%s", comp->perf_sysfs_root, rel);
}

static bool perf_read_number(const char *path, double *value) {
    char text[128];
    if (!read_first_line(path, text, sizeof(text))) return false;
    char *end = NULL;
    double parsed = strtod(text, &end);
    if (end == text) return false;
    *value = parsed;
    return true;
}

/* The smallest directory name in `dir` for which `accept` holds; deterministic when several match. */
static bool perf_pick_entry(const char *dir, bool (*accept)(const char *dir, const char *name, void *arg), void *arg,
        char *out, size_t size) {
    DIR *handle = opendir(dir);
    if (!handle) return false;
    bool found = false;
    struct dirent *entry;
    while ((entry = readdir(handle))) {
        if (entry->d_name[0] == '.' || !accept(dir, entry->d_name, arg)) continue;
        if (!found || strcmp(entry->d_name, out) < 0) snprintf(out, size, "%s", entry->d_name);
        found = true;
    }
    closedir(handle);
    return found;
}

static bool perf_accept_hwmon(const char *dir, const char *name, void *arg) {
    const char *wanted = arg;
    if (strncmp(name, "hwmon", 5)) return false;
    char path[1024], text[64];
    snprintf(path, sizeof(path), "%s/%s/name", dir, name);
    if (!read_first_line(path, text, sizeof(text))) return false;
    for (const char *p = wanted; *p; p += strlen(p) + 1) if (!strcmp(text, p)) return true;
    return false;
}

/* The hwmon directory whose name file says one of the names (a double-NUL list). */
static bool perf_find_hwmon(const struct compositor *comp, const char *names, char *out, size_t size) {
    char base[512], entry[256];
    perf_sysfs_path(comp, base, sizeof(base), "/sys/class/hwmon");
    if (!perf_pick_entry(base, perf_accept_hwmon, (void *)names, entry, sizeof(entry))) return false;
    snprintf(out, size, "%s/%s", base, entry);
    return true;
}

static bool perf_accept_battery(const char *dir, const char *name, void *arg) {
    (void)arg;
    char path[1024], text[64];
    snprintf(path, sizeof(path), "%s/%s/type", dir, name);
    return read_first_line(path, text, sizeof(text)) && !strcmp(text, "Battery");
}

static bool perf_find_battery(const struct compositor *comp, char *out, size_t size) {
    char base[512], entry[256];
    perf_sysfs_path(comp, base, sizeof(base), "/sys/class/power_supply");
    if (!perf_pick_entry(base, perf_accept_battery, NULL, entry, sizeof(entry))) return false;
    snprintf(out, size, "%s/%s", base, entry);
    return true;
}

/* Busy share of the CPU since the last call, from /proc/stat. */
static bool perf_cpu_load(struct compositor *comp, double *load) {
    char path[512], line[256];
    perf_sysfs_path(comp, path, sizeof(path), "/proc/stat");
    if (!read_first_line(path, line, sizeof(line)) || strncmp(line, "cpu ", 4)) return false;
    unsigned long long field[8] = {0};
    if (sscanf(line + 4, "%llu %llu %llu %llu %llu %llu %llu %llu", &field[0], &field[1], &field[2], &field[3],
            &field[4], &field[5], &field[6], &field[7]) < 4) return false;
    uint64_t total = 0;
    for (int i = 0; i < 8; i++) total += field[i];
    uint64_t idle = field[3] + field[4];
    bool ok = comp->perf_cpu_primed && total > comp->perf_cpu_total && idle >= comp->perf_cpu_idle;
    if (ok) {
        double busy = (double)((total - comp->perf_cpu_total) - (idle - comp->perf_cpu_idle));
        *load = 100.0 * busy / (double)(total - comp->perf_cpu_total);
    }
    comp->perf_cpu_total = total;
    comp->perf_cpu_idle = idle;
    comp->perf_cpu_primed = true;
    return ok;
}

static bool perf_cpu_mhz(const struct compositor *comp, double *mhz) {
    char dir[512];
    perf_sysfs_path(comp, dir, sizeof(dir), "/sys/devices/system/cpu");
    DIR *handle = opendir(dir);
    double sum = 0;
    int cores = 0;
    if (handle) {
        struct dirent *entry;
        while ((entry = readdir(handle))) {
            if (strncmp(entry->d_name, "cpu", 3) || !isdigit((unsigned char)entry->d_name[3])) continue;
            char path[1024];
            double khz;
            snprintf(path, sizeof(path), "%s/%s/cpufreq/scaling_cur_freq", dir, entry->d_name);
            if (perf_read_number(path, &khz)) { sum += khz / 1000.0; cores++; }
        }
        closedir(handle);
    }
    if (!cores) {
        /* No cpufreq: the "cpu MHz" lines of /proc/cpuinfo. */
        char path[320];
        perf_sysfs_path(comp, path, sizeof(path), "/proc/cpuinfo");
        FILE *fp = fopen(path, "r");
        char line[256];
        while (fp && fgets(line, sizeof(line), fp)) {
            double value;
            if (!strncmp(line, "cpu MHz", 7) && strchr(line, ':') && (value = strtod(strchr(line, ':') + 1, NULL)) > 0) {
                sum += value;
                cores++;
            }
        }
        if (fp) fclose(fp);
    }
    if (!cores) return false;
    *mhz = sum / cores;
    return true;
}

static void perf_sample_host(struct compositor *comp, struct perf_host *host) {
    memset(host, 0, sizeof(*host));
    char path[1280], base[512];
    host->has[PH_CPU_LOAD] = perf_cpu_load(comp, &host->value[PH_CPU_LOAD]);
    host->has[PH_CPU_MHZ] = perf_cpu_mhz(comp, &host->value[PH_CPU_MHZ]);
    for (int card = 0; card < 8; card++) {
        snprintf(base, sizeof(base), "/sys/class/drm/card%d/device/gpu_busy_percent", card);
        perf_sysfs_path(comp, path, sizeof(path), base);
        if (!host->has[PH_GPU_BUSY]) host->has[PH_GPU_BUSY] = perf_read_number(path, &host->value[PH_GPU_BUSY]);
        snprintf(base, sizeof(base), "/sys/class/drm/card%d/device/pp_dpm_sclk", card);
        perf_sysfs_path(comp, path, sizeof(path), base);
        FILE *fp = host->has[PH_SCLK] ? NULL : fopen(path, "r");
        char line[128];
        while (fp && fgets(line, sizeof(line), fp)) {
            /* "1: 1600Mhz *": the line with the star is the clock in use. */
            char *colon = strchr(line, ':');
            if (colon && strchr(line, '*') && strtod(colon + 1, NULL) > 0) {
                host->value[PH_SCLK] = strtod(colon + 1, NULL);
                host->has[PH_SCLK] = true;
                break;
            }
        }
        if (fp) fclose(fp);
    }
    char hwmon[1024];
    if (perf_find_hwmon(comp, "k10temp\0coretemp\0zenpower\0", hwmon, sizeof(hwmon))) {
        double milli;
        snprintf(path, sizeof(path), "%s/temp1_input", hwmon);
        if (perf_read_number(path, &milli)) { host->has[PH_CPU_TEMP] = true; host->value[PH_CPU_TEMP] = milli / 1000.0; }
    }
    if (perf_find_hwmon(comp, "amdgpu\0", hwmon, sizeof(hwmon))) {
        double number;
        snprintf(path, sizeof(path), "%s/temp1_input", hwmon);
        if (perf_read_number(path, &number)) { host->has[PH_GPU_TEMP] = true; host->value[PH_GPU_TEMP] = number / 1000.0; }
        snprintf(path, sizeof(path), "%s/power1_average", hwmon);
        if (perf_read_number(path, &number)) { host->has[PH_POWER] = true; host->value[PH_POWER] = number / 1e6; }
        if (!host->has[PH_SCLK]) {
            snprintf(path, sizeof(path), "%s/freq1_input", hwmon);
            if (perf_read_number(path, &number)) { host->has[PH_SCLK] = true; host->value[PH_SCLK] = number / 1e6; }
        }
    }
    char battery[1024];
    if (perf_find_battery(comp, battery, sizeof(battery))) {
        double number;
        snprintf(path, sizeof(path), "%s/capacity", battery);
        if (perf_read_number(path, &number)) { host->has[PH_BATTERY] = true; host->value[PH_BATTERY] = number; }
        snprintf(path, sizeof(path), "%s/power_now", battery);
        if (!host->has[PH_POWER] && perf_read_number(path, &number)) {
            host->has[PH_POWER] = true;
            host->value[PH_POWER] = fabs(number) / 1e6;
        }
    }
}

/* --- Recording --- */

static int perf_csv_filter(const struct dirent *entry) {
    size_t length = strlen(entry->d_name);
    return !strncmp(entry->d_name, "perf-", 5) && length > 9 && !strcmp(entry->d_name + length - 4, ".csv");
}

/* Keep the newest PERF_KEEP_FILES files; the names start with the date, so name order is time order. */
static void perf_rotate(struct compositor *comp) {
    struct dirent **names = NULL;
    int count = scandir(comp->perf_root, &names, perf_csv_filter, alphasort);
    for (int i = 0; i < count; i++) {
        if (i < count - PERF_KEEP_FILES) {
            char path[640];
            snprintf(path, sizeof(path), "%s/%s", comp->perf_root, names[i]->d_name);
            bool open_now = !strcmp(path, comp->left.perf.csv_path) || !strcmp(path, comp->right.perf.csv_path);
            if (!open_now) unlink(path);
        }
        free(names[i]);
    }
    free(names);
}

static void perf_stop_recording(struct side_ctx *side) {
    if (side->perf.csv) fclose(side->perf.csv);
    side->perf.csv = NULL;
    side->perf.recording = false;
    side->perf.csv_path[0] = '\0';
}

static bool perf_start_recording(struct compositor *comp, struct side_ctx *side) {
    if (side->perf.recording) return true;
    if (!perf_ensure_dir(comp->perf_root)) {
        wlr_log(WLR_ERROR, "Performance recording not started: %s is not usable: %s", comp->perf_root, strerror(errno));
        return false;
    }
    time_t wall = time(NULL);
    struct tm local;
    char stamp[32], path[384];
    localtime_r(&wall, &local);
    strftime(stamp, sizeof(stamp), "%Y%m%d-%H%M%S", &local);
    int fd = -1;
    for (int attempt = 0; attempt < 100 && fd < 0; attempt++) {
        char suffix[16] = "";
        if (attempt) snprintf(suffix, sizeof(suffix), "-%d", attempt + 1);
        snprintf(path, sizeof(path), "%s/perf-%s-%s%s.csv", comp->perf_root, stamp, perf_role_label(side), suffix);
        fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
        if (fd < 0 && errno != EEXIST) break;
    }
    FILE *fp = fd >= 0 ? fdopen(fd, "w") : NULL;
    if (!fp) {
        if (fd >= 0) close(fd);
        wlr_log(WLR_ERROR, "Performance recording not started: cannot create a file in %s: %s", comp->perf_root, strerror(errno));
        return false;
    }
    fputs(perf_csv_header, fp);
    fflush(fp);
    side->perf.csv = fp;
    side->perf.recording = true;
    snprintf(side->perf.csv_path, sizeof(side->perf.csv_path), "%s", path);
    /* The first row's CPU load is the load since now. */
    struct perf_host ignored;
    perf_sample_host(comp, &ignored);
    perf_rotate(comp);
    wlr_log(WLR_INFO, "Performance recording started for the %s side: %s", perf_role_label(side), path);
    return true;
}

static void perf_field(char *out, size_t size, bool has, double value, int decimals) {
    if (has) snprintf(out, size, "%.*f", decimals, value);
    else out[0] = '\0';
}

static void perf_write_row(struct side_ctx *side, uint64_t now, const struct perf_host *host) {
    struct perf_stats stats;
    perf_compute(&side->perf, now, PERF_SECOND_NS, &stats);
    struct timespec wall;
    clock_gettime(CLOCK_REALTIME, &wall);
    char avg[24], p99[24], field[PH_COUNT][24];
    perf_field(avg, sizeof(avg), stats.intervals > 0, stats.avg_ms, 2);
    perf_field(p99, sizeof(p99), stats.intervals > 0, stats.p99_ms, 2);
    for (int i = 0; i < PH_COUNT; i++)
        perf_field(field[i], sizeof(field[i]), host->has[i], host->value[i], i == PH_SCLK || i == PH_CPU_MHZ ? 0 : 1);
    fprintf(side->perf.csv, "%lld,%s,%.1f,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
        (long long)wall.tv_sec * 1000 + wall.tv_nsec / 1000000, perf_role_label(side), stats.fps, avg, p99,
        field[PH_CPU_LOAD], field[PH_GPU_BUSY], field[PH_CPU_TEMP], field[PH_GPU_TEMP], field[PH_POWER],
        field[PH_BATTERY], field[PH_SCLK], field[PH_CPU_MHZ]);
    fflush(side->perf.csv);
}

/* --- The counter on the screen --- */

static bool perf_any_active(const struct compositor *comp) {
    return comp->left.perf.mode != FPS_MODE_OFF || comp->right.perf.mode != FPS_MODE_OFF ||
        comp->left.perf.recording || comp->right.perf.recording;
}

/* Redrawn once a second, from this file's own scene layer: a corner of each side that shows a counter. The size
 * follows the side's region, from 15 px on a 1280x800 panel to 45 px on a 3840x2160 output. */
static void perf_redraw(struct compositor *comp) {
    if (!comp->perf_tree) return;
    struct wlr_scene_node *child, *next;
    wl_list_for_each_safe(child, next, &comp->perf_tree->children, link) wlr_scene_node_destroy(child);
    uint64_t now = perf_now_ns();
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (int i = 0; i < 2; i++) {
        struct side_ctx *side = sides[i];
        if (side->perf.mode == FPS_MODE_OFF || !side->app_surface || side->width <= 0 || side->height <= 0) continue;
        if (!side_is_shown(comp, side)) continue;
        char text[96];
        perf_text(side, now, text, sizeof(text));
        double unit = fmin(3.0, fmax(1.0, fmin(side->width, side->height) / 720.0));
        double font = 15.0 * unit;
        int margin = (int)(8 * unit);
        int box_w = (int)(strlen(text) * font * 0.62 + 12 * unit), box_h = (int)(font * 1.9);
        static const float backdrop[4] = {0.f, 0.f, 0.f, 0.6f};
        make_rect(comp->perf_tree, side->x + margin, side->y + margin, box_w, box_h, backdrop);
        make_text_node_left(comp->perf_tree, side->x + margin, side->y + margin, box_w, box_h, text, font,
            1.0, 1.0, 1.0, 1.0, true);
    }
    if (comp->modal_tree) wlr_scene_node_place_below(&comp->perf_tree->node, &comp->modal_tree->node);
    if (comp->output) wlr_output_schedule_frame(comp->output);
}

static int perf_tick(void *data) {
    struct compositor *comp = data;
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    uint64_t now = perf_now_ns();
    if (comp->left.perf.recording || comp->right.perf.recording) {
        struct perf_host host;
        perf_sample_host(comp, &host);
        for (int i = 0; i < 2; i++) if (sides[i]->perf.recording) perf_write_row(sides[i], now, &host);
    }
    perf_redraw(comp);
    if (perf_any_active(comp)) wl_event_source_timer_update(comp->perf_timer, 1000);
    else comp->perf_timer_armed = false;
    return 0;
}

static void perf_update_timer(struct compositor *comp) {
    bool active = perf_any_active(comp);
    if (active && !comp->perf_timer_armed) {
        if (!comp->perf_timer && comp->display)
            comp->perf_timer = wl_event_loop_add_timer(wl_display_get_event_loop(comp->display), perf_tick, comp);
        if (comp->perf_timer && wl_event_source_timer_update(comp->perf_timer, 1000) == 0) comp->perf_timer_armed = true;
    } else if (!active && comp->perf_timer_armed) {
        wl_event_source_timer_update(comp->perf_timer, 0);
        comp->perf_timer_armed = false;
    }
}

static void perf_set_mode(struct compositor *comp, struct side_ctx *side, enum fps_mode mode) {
    if (!comp || !side) return;
    if (side->perf.mode != mode) {
        side->perf.mode = mode;
        perf_settings_save(comp);
        wlr_log(WLR_INFO, "FPS counter on the %s side: %s", perf_role_label(side), fps_mode_name(mode));
    }
    perf_update_timer(comp);
    perf_redraw(comp);
    write_runtime_state(comp);
}

static void perf_set_recording(struct compositor *comp, struct side_ctx *side, bool on) {
    if (!comp || !side) return;
    if (on) perf_start_recording(comp, side);
    else if (side->perf.recording) {
        wlr_log(WLR_INFO, "Performance recording stopped for the %s side", perf_role_label(side));
        perf_stop_recording(side);
    }
    perf_update_timer(comp);
    perf_redraw(comp);
    write_runtime_state(comp);
}

static void perf_init(struct compositor *comp) {
    snprintf(comp->perf_root, sizeof(comp->perf_root), "%s", PERF_DEFAULT_ROOT);
    comp->perf_sysfs_root[0] = '\0';
    const char *root = getenv("DOSPLEX_PERF_ROOT"), *sysfs = getenv("DOSPLEX_PERF_SYSFS_ROOT");
    if (comp->config.test_hooks) {
        if (root && root[0] == '/') snprintf(comp->perf_root, sizeof(comp->perf_root), "%s", root);
        if (sysfs && sysfs[0] == '/') snprintf(comp->perf_sysfs_root, sizeof(comp->perf_sysfs_root), "%s", sysfs);
    } else if (root || sysfs) {
        wlr_log(WLR_ERROR, "Ignoring DOSPLEX_PERF_ROOT and DOSPLEX_PERF_SYSFS_ROOT: the compositor was not started with --test-hooks");
    }
    perf_settings_load(comp);
}

static void render_management_overlay(struct compositor *comp,
        struct wlr_scene_tree *parent) {
    if (!comp || !parent) return;
    comp->modal_region_count = 0;
    comp->screen_bl_drawn = false;
    if (comp->power_action[0]) {
        render_power_progress(comp, parent);
        return;
    }
    if (!comp->management_overlay_visible) return;
    /* The panel covers its side while it is shown; the menu waits underneath, undrawn, and is back when it ends. */
    if (comp->panel_surface && !comp->panel_closing) return;
    struct side_ctx *owner = compositor_side_by_id(comp, comp->management_overlay_owner);
    if (!owner) return;
    comp->modal_scale = fmax(1.0, fmin(comp->output_height / 1080.0,
        fmin(owner->width / 744.0, owner->height / 564.0)));
    comp->modal_origin_x = owner->x;
    comp->modal_origin_y = owner->y;
    int logical_width = (int)floor(owner->width / comp->modal_scale);
    int logical_height = (int)floor(owner->height / comp->modal_scale);
    int pw = logical_width - 24;
    int ph = logical_height - 24;
    if (pw > 720) pw = 720;
    if (ph > 540) ph = 540;
    if (pw < 240 || ph < 280) return;
    int px = (logical_width - pw) / 2;
    int py = (logical_height - ph) / 2;
    int x = px + 16, y = py + 112, width = pw - 32;
    int third = (width - 16) / 3;
    int half = (width - 8) / 2;
    make_rect(parent, owner->x, owner->y, owner->width, owner->height, MODAL_SCRIM_COLOR);
    modal_rect(comp, parent, px, py, pw, ph, MODAL_PANEL_COLOR);
    modal_text(comp, parent, x, py + 12, width - 42 - 150, 28,
        owner->role == SIDE_ROLE_PRIMARY ? "Dosplex · Main" : "Dosplex · Guest",
        21, .96, .97, 1, 1, true);
    render_modal_button(comp, parent, MODAL_ACTION_CLOSE, px + pw - 44, py + 12, 28, 28, "X", false);
    char status[128];
    /* The shown side of a single layout calls it Full screen; Main, hidden by Guest full screen, names the layout. */
    side_region_line(owner, shown_side(comp) == owner ? "Full screen" :
        comp->layout == LAYOUT_SINGLE_GUEST ? "Guest full screen" :
        comp->layout == LAYOUT_SINGLE ? "Full screen" : layout_mode_name(comp->layout),
        status, sizeof(status));
    bool show_wine_notice = false;
    if (owner->wine_notice[0]) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) == 0) {
            if (!owner->wine_notice_timer_started) {
                owner->wine_notice_started = now;
                owner->wine_notice_timer_started = true;
            }
            double elapsed = (double)(now.tv_sec - owner->wine_notice_started.tv_sec) +
                (double)(now.tv_nsec - owner->wine_notice_started.tv_nsec) / 1000000000.0;
            if (elapsed < 8.0) show_wine_notice = true;
            else {
                owner->wine_notice[0] = '\0';
                owner->wine_notice_timer_started = false;
            }
        }
    }
    modal_text(comp, parent, x, py + 44, width, 18,
        show_wine_notice ? owner->wine_notice : status, 12, .72, .75, .84, 1, false);
    int tab_width = (width - 24) / 4;
    render_modal_button(comp, parent, MODAL_ACTION_TAB_DISPLAY, x, py + 70, tab_width, 30, "Display", comp->overlay_tab == 0);
    render_modal_button(comp, parent, MODAL_ACTION_TAB_DEVICES, x + tab_width + 8, py + 70, tab_width, 30, "Devices", comp->overlay_tab == 1);
    render_modal_button(comp, parent, MODAL_ACTION_TAB_AUDIO, x + 2 * (tab_width + 8), py + 70, tab_width, 30, "Audio", comp->overlay_tab == 3);
    render_modal_button(comp, parent, MODAL_ACTION_TAB_SESSION, x + 3 * (tab_width + 8), py + 70, tab_width, 30, "Session", comp->overlay_tab == 2);


    if (comp->device_move_active) {
        modal_text(comp, parent, x, y, width, 24, comp->device_move_other[0] ? "Keep swapped controls?" : "Keep this device assignment?", 17, .96, .97, 1, 1, true);
        modal_text(comp, parent, x, y + 30, width, 24, comp->device_move_label, 13, .85, .87, .92, 1, false);
        modal_text(comp, parent, x, y + 58, width, 48,
            "Confirm here using your remaining controls. If you cannot, the device returns automatically after 15 seconds.",
            12, .85, .87, .92, 1, false);
        render_modal_button(comp, parent, MODAL_ACTION_CANCEL_ACTION, x, y + 112, half, 34, "Undo", true);
        render_modal_button(comp, parent, MODAL_ACTION_KEEP_DEVICE, x + half + 8, y + 112, half, 34, "Keep assignment", false);
    } else if (comp->pending_shared_action != MODAL_ACTION_NONE) {
        modal_text(comp, parent, x, y, width, 24,
            comp->pending_shared_action == MODAL_ACTION_RESET_SIDE ?
                (comp->layout == LAYOUT_SINGLE ? "Reset Steam" : "Reset your session") : "This affects both players", 17, 1, .8, .4, 1, true);
        /* A layout or placement change restarts no side, so only the heading is shown. */
        const char *message = "";
        if (comp->pending_shared_action == MODAL_ACTION_AUDIO_MIX_OFF)
            message = "Stop shared speakers? Both players return to their own claimed outputs.";
        if (comp->pending_shared_action == MODAL_ACTION_AUDIO_MIX_FULL || comp->pending_shared_action == MODAL_ACTION_AUDIO_MIX_SEPARATED)
            message = "Share your claimed output? Guest can join from Audio. Changing the mix affects everyone who joined.";
        if (comp->pending_shared_action == MODAL_ACTION_AUDIO_MIX_SEPARATED)
            message = "Choose separation for shared speakers. Stronger favours each player's screen side. This affects everyone who joined.";
        if (comp->pending_shared_action == MODAL_ACTION_SINGLE) message = "What should happen to Guest? Standby keeps it in memory. Off closes it and loses unsaved progress.";
        if (comp->pending_shared_action == MODAL_ACTION_SINGLE_GUEST)
            message = "Give Guest the whole screen? Your session goes to standby; open your overlay any time to come back.";
        if (comp->pending_shared_action == MODAL_ACTION_SINGLE_OFF) message = "Close Guest and use the full screen? Guest unsaved progress will be lost. Devices stay claimed.";
        if (comp->pending_shared_action == MODAL_ACTION_RESET_SIDE) message = comp->layout == LAYOUT_SINGLE ?
            "Close your activity and restart Steam? Save first." : "Close your activity and restart your side? Save first.";
        if (comp->pending_shared_action == MODAL_ACTION_RESET_ALL) message = "Close both activities and reset both sides? Save first.";
        char forget_text[200];
        if (comp->pending_shared_action == MODAL_ACTION_FORGET_DEVICE) {
            snprintf(forget_text, sizeof(forget_text), "Forget %s? Its number is free for the next new controller. It comes back as a new one if you turn it on.",
                comp->pending_forget_label);
            message = forget_text;
        }
        if (comp->pending_shared_action == MODAL_ACTION_POWER_SLEEP)
            message = "Sleep this machine? Both sessions stay open and resume.";
        /* What the machine boots into after Restart or Shut down is what the launcher says it is. */
        if (comp->pending_shared_action == MODAL_ACTION_POWER_RESTART ||
                comp->pending_shared_action == MODAL_ACTION_POWER_SHUTDOWN ||
                comp->pending_shared_action == MODAL_ACTION_POWER_EXIT)
            message = power_confirmation_text(comp->pending_shared_action, comp->config.boots_into);
        bool unavailable_power = !comp->config.allow_power &&
            (comp->pending_shared_action == MODAL_ACTION_POWER_SLEEP ||
             comp->pending_shared_action == MODAL_ACTION_POWER_RESTART ||
             comp->pending_shared_action == MODAL_ACTION_POWER_SHUTDOWN ||
             comp->pending_shared_action == MODAL_ACTION_POWER_EXIT);
        if (unavailable_power) message = "Machine power is not available here.";
        modal_text(comp, parent, x, y + 34, width, 52, message, 12, .85, .87, .92, 1, false);
        render_modal_button(comp, parent, MODAL_ACTION_CANCEL_ACTION, x, y + 88, half, 34, "Cancel", true);
        if (comp->pending_shared_action == MODAL_ACTION_AUDIO_MIX_SEPARATED) {
            render_modal_button(comp, parent, MODAL_ACTION_AUDIO_GENTLE, x, y + 130, third, 34, "Gentle", strcmp(comp->audio_separation, "gentle") == 0);
            render_modal_button(comp, parent, MODAL_ACTION_AUDIO_BALANCED, x + third + 8, y + 130, third, 34, "Balanced", strcmp(comp->audio_separation, "balanced") == 0);
            render_modal_button(comp, parent, MODAL_ACTION_AUDIO_STRONG, x + 2 * (third + 8), y + 130, third, 34, "Strong", strcmp(comp->audio_separation, "strong") == 0);
        } else if (comp->pending_shared_action == MODAL_ACTION_SINGLE) {
            render_modal_button(comp, parent, MODAL_ACTION_SINGLE_STANDBY, x, y + 130, half, 34, "Standby", false);
            render_modal_button(comp, parent, MODAL_ACTION_SINGLE_OFF, x + half + 8, y + 130, half, 34, "Off...", false);
        } else if (!unavailable_power) render_modal_button(comp, parent, MODAL_ACTION_CONFIRM, x + half + 8, y + 88, half, 34, "Continue", false);
    } else if (comp->overlay_tab == 0) {
        modal_text(comp, parent, x, y, width, 18, "Virtual monitor", 13, .9, .92, .96, 1, true);
        /* Native first, then the ratio choices from the one that fills this
         * region most to the one that fills it least. The buttons are added in
         * the displayed order, so Tab follows what is on screen. */
        int quarter = (width - 24) / 4;
        render_modal_button(comp, parent, MODAL_ACTION_SCALE_NATIVE, x, y + 22, quarter, 32, "Native", owner->pending_game_resolution_choice == GAME_RESOLUTION_NATIVE);
        const struct game_ratio *ratios[GAME_RATIO_COUNT];
        game_ratios_by_fill(owner->width, owner->height, ratios);
        for (int i = 0; i < GAME_RATIO_COUNT; i++) {
            char ratio_label[32];
            game_ratio_label(ratios[i], owner->width, owner->height, ratio_label, sizeof(ratio_label));
            render_modal_button(comp, parent, ratios[i]->action, x + (i + 1) * (quarter + 8), y + 22, quarter, 32, ratio_label, owner->pending_game_resolution_choice == ratios[i]->choice);
        }
        render_modal_button(comp, parent, MODAL_ACTION_SCALE_FIT, x, y + 66, third, 32, "Keep ratio", owner->pending_scale_mode == OUTPUT_SCALE_FIT);
        render_modal_button(comp, parent, MODAL_ACTION_SCALE_STRETCH, x + third + 8, y + 66, third, 32, "Stretch", owner->pending_scale_mode == OUTPUT_SCALE_STRETCH);
        render_modal_button(comp, parent, MODAL_ACTION_SCALE_ZOOM, x + 2 * (third + 8), y + 66, third, 32, "Fit", owner->pending_scale_mode == OUTPUT_SCALE_ZOOM);
        snprintf(status, sizeof(status), "Selected: %dx%d · %s · %s",
            owner->pending_game_width ? owner->pending_game_width : owner->width,
            owner->pending_game_height ? owner->pending_game_height : owner->height,
            game_resolution_choice_name(owner->pending_game_resolution_choice),
            output_scale_mode_label(owner->pending_scale_mode));
        modal_text(comp, parent, x, y + 104, width, 20, status, 12, .8, .83, .9, 1, false);
        render_modal_button(comp, parent, MODAL_ACTION_DISPLAY_CANCEL, x, y + 132, third, 32, "Cancel", false);
        render_modal_button(comp, parent, MODAL_ACTION_DISPLAY_APPLY, x + third + 8, y + 132, width - third - 8, 32,
            owner->display_pending ? "Apply" : "Up to date", owner->display_pending);
        /* One line under the buttons: this side's display change in progress, failed
         * or scaled, otherwise the standing note about Steam's own setting. */
        char display_line[160];
        enum display_status display_status = side_display_status(owner, (size_t)(width / 7),
            display_line, sizeof(display_line));
        if (display_status == DISPLAY_STATUS_APPLYING) {
            modal_text(comp, parent, x, y + 170, width, 18, display_line, 11, .8, .83, .9, 1, false);
        } else {
            modal_text(comp, parent, x, y + 170, width, 18,
                display_status != DISPLAY_STATUS_NONE ? display_line :
                    owner->pending_scale_mode == OUTPUT_SCALE_ZOOM ?
                        "Fit: crops the picture to the screen's shape and fills it. For games that show black bars." :
                        "Steam per-game Game Resolution can override this size.",
                11, 1, .8, .4, 1, false);
        }
        /* The performance counter (T-434) takes effect at once: it needs no Apply and restarts nothing. */
        if (ph >= 408) {
            modal_text(comp, parent, x, y + 196, width, 18, "FPS counter on this side", 12, .9, .92, .96, 1, true);
            render_modal_button(comp, parent, MODAL_ACTION_FPS_OFF, x, y + 218, third, 32, "Off", owner->perf.mode == FPS_MODE_OFF);
            render_modal_button(comp, parent, MODAL_ACTION_FPS_ON, x + third + 8, y + 218, third, 32, "FPS", owner->perf.mode == FPS_MODE_FPS);
            render_modal_button(comp, parent, MODAL_ACTION_FPS_FRAMETIME, x + 2 * (third + 8), y + 218, third, 32, "FPS + frame time", owner->perf.mode == FPS_MODE_FRAMETIME);
            render_modal_button(comp, parent, MODAL_ACTION_PERF_RECORD, x, y + 258, half, 32,
                owner->perf.recording ? "Recording: stop" : "Record performance", owner->perf.recording);
            const char *file = owner->perf.recording ? strrchr(owner->perf.csv_path, '/') : NULL;
            modal_text(comp, parent, x + half + 8, y + 264, half, 20, file ? file + 1 : "CSV files in /var/lib/dosplex/perf", 11, .8, .83, .9, 1, false);
        }
        /* The screen is shared, and Main is the driver: only Main's menu has the brightness (T-436). It comes after the
         * counter in the Tab order, and takes the counter's place on a panel too short to show the counter. */
        if (comp->screen_bl_available && owner->role == SIDE_ROLE_PRIMARY && (ph >= 470 || ph < 408)) {
            int by = ph >= 470 ? y + 300 : y + 196;
            char brightness_line[48];
            comp->screen_bl_drawn = true;
            snprintf(brightness_line, sizeof(brightness_line), "Screen brightness: %d %%", comp->screen_bl_percent);
            modal_text(comp, parent, x, by, width, 18, brightness_line, 12, .9, .92, .96, 1, true);
            render_modal_button(comp, parent, MODAL_ACTION_BRIGHTNESS_DOWN, x, by + 22, half, 32, "Dimmer",
                comp->screen_bl_percent <= SCREEN_BL_MIN_PERCENT);
            render_modal_button(comp, parent, MODAL_ACTION_BRIGHTNESS_UP, x + half + 8, by + 22, half, 32, "Brighter",
                comp->screen_bl_percent >= 100);
        }
    } else if (comp->overlay_tab == 3) {
        render_modal_button(comp, parent, MODAL_ACTION_AUDIO_OUTPUT, x, y, third, 30, "Output", comp->audio_kind == 0);
        render_modal_button(comp, parent, MODAL_ACTION_AUDIO_INPUT, x + third + 8, y, third, 30, "Microphone", comp->audio_kind == 1);
        render_modal_button(comp, parent, MODAL_ACTION_AUDIO_MUTE, x + 2 * (third + 8), y, third, 30, "Release", false);
        const char *route_state = comp->audio_state[owner->id][comp->audio_kind];
        const char *message = comp->audio_pid > 0 ? "Updating audio…" : strcmp(route_state, "disconnected") == 0 ?
            "Claimed device disconnected — waiting for reconnection" : strcmp(route_state, "muted") == 0 ? "No device claimed — choose an available device" : "Release your device to let the other player claim it";
        int offset = 0;
        bool mix_enabled = comp->audio_mix_mode[0] && strcmp(comp->audio_mix_mode, "off") != 0;
        if (comp->audio_kind == 0) {
            offset = 38;
            if (owner->role == SIDE_ROLE_PRIMARY) {
                int short_mix = (width - 16) / 4;
                render_modal_button(comp, parent, MODAL_ACTION_AUDIO_MIX_OFF, x, y + 56, short_mix, 30, "Stop sharing…", !mix_enabled);
                render_modal_button(comp, parent, MODAL_ACTION_AUDIO_MIX_FULL, x + short_mix + 8, y + 56, short_mix, 30, "Full mix…", strcmp(comp->audio_mix_mode, "full") == 0);
                render_modal_button(comp, parent, MODAL_ACTION_AUDIO_MIX_SEPARATED, x + 2 * (short_mix + 8), y + 56, width - 2 * (short_mix + 8), 30, "Positioned stereo mix…", strcmp(comp->audio_mix_mode, "separated") == 0);
            } else {
                render_modal_button(comp, parent, MODAL_ACTION_AUDIO_LEAVE, x, y + 56, half, 30, "Use own output", !comp->audio_joined[owner->id]);
                render_modal_button(comp, parent, mix_enabled ? MODAL_ACTION_AUDIO_JOIN : MODAL_ACTION_NONE,
                    x + half + 8, y + 56, half, 30, "Join shared speakers", comp->audio_joined[owner->id]);
            }
            if (mix_enabled) {
                char members[64];
                audio_mix_members(comp, members, sizeof(members));
                if (strcmp(comp->audio_mix_effective, "separated") == 0) {
                    const char *strength = !strcmp(comp->audio_separation, "gentle") ? "Gentle" : !strcmp(comp->audio_separation, "strong") ? "Strong" : "Balanced";
                    snprintf(status, sizeof(status), "Positioned stereo mix · %s · %s", strength, members);
                } else {
                    snprintf(status, sizeof(status), "Full mix · %s", members);
                }
                message = status;
            }
            modal_text(comp, parent, x, y + 34, width, 18, message, 12, .8, .83, .9, 1, false);
        }
        if (comp->audio_kind != 0) modal_text(comp, parent, x, y + 34, width, 18, message, 12, .8, .83, .9, 1, false);
        int rows = (ph - 252 - offset) / 32;
        if (rows < 1) rows = 1;
        int total = 0;
        for (int i = 0; i < comp->audio_count; i++) if (comp->audio_devices[i].kind == comp->audio_kind) total++;
        int last_page = total > 0 ? (total - 1) / rows : 0;
        if (comp->audio_page > last_page) comp->audio_page = last_page;
        int index = 0, shown = 0;
        for (int i = 0; i < comp->audio_count; i++) {
            struct audio_device_choice *device = &comp->audio_devices[i];
            if (device->kind != comp->audio_kind) continue;
            if (index++ < comp->audio_page * rows || shown >= rows) continue;
            bool shared_device = strcmp(device->owner, "shared") == 0;
            bool mine = strcmp(device->owner, side_name(owner->id)) == 0 ||
                (shared_device && comp->audio_joined[owner->id]);
            char label[300];
            bool available = strcmp(device->owner, "free") == 0;
            snprintf(label, sizeof(label), "%s%s%s", available ? "Claim: " : "", device->label,
                shared_device ? (mine ? " (shared · joined)" : " (shared · join)") : mine ? " (yours)" : !available ? " (other player)" : "");
            render_modal_button(comp, parent, shared_device ? MODAL_ACTION_AUDIO_JOIN : available || mine ? MODAL_ACTION_AUDIO_SELECT : MODAL_ACTION_NONE,
                x, y + 58 + offset + shown * 32, width, 28, label, mine);
            snprintf(comp->modal_regions[comp->modal_region_count - 1].device_name, 256, "%s", device->id);
            shown++;
        }
        if (!total && !comp->audio_pid) modal_text(comp, parent, x, y + 60, width, 24,
            comp->audio_kind == 0 ? "No audio outputs detected" : "No microphones detected", 13, .8, .83, .9, 1, false);
        modal_text(comp, parent, x, py + ph - 98, width, 26, comp->audio_message, 11, 1, .8, .4, 1, false);
        if (comp->audio_page > 0) render_modal_button(comp, parent, MODAL_ACTION_AUDIO_PREV, x, py + ph - 68, half, 28, "Previous", false);
        if (comp->audio_page < last_page) render_modal_button(comp, parent, MODAL_ACTION_AUDIO_NEXT, x + half + 8, py + ph - 68, half, 28, "Next", false);
    } else if (comp->overlay_tab == 1) {
        int rows = (ph - 260) / 50;
        if (rows < 1) rows = 1;
        int skip = *owner_device_page(comp) * rows, index = 0, shown = 0;
        modal_text(comp, parent, x, y, width, 18, comp->device_message[0] ? comp->device_message :
            owner->role == SIDE_ROLE_PRIMARY ? "Main must keep a keyboard or controller for the menu" :
            "Claim a free controller for your side, or release one of yours", 12, .8, .83, .9, 1, false);
        y += 22;
        struct json_object *inventory = controller_inventory(), *devices = NULL;
        if (inventory) json_object_object_get_ex(inventory, "devices", &devices);
        struct json_object *preview_version = NULL;
        bool can_move_controllers = inventory &&
            json_object_object_get_ex(inventory, "assignment_preview", &preview_version) &&
            json_object_get_int(preview_version) == 2;
        note_controller_activity(comp, devices);
        for (size_t i = 0; devices && i < json_object_array_length(devices); i++) {
            struct json_object *d = json_object_array_get_idx(devices, i), *connected, *active;
            const char *key = controller_text(d, "id"), *target = controller_text(d, "target");
            const char *requested = controller_text(d, "requested");
            if (!controller_key_valid(key)) continue;
            bool online = json_object_object_get_ex(d, "connected", &connected) && json_object_get_boolean(connected);
            bool mine = !strcmp(requested, "guest");
            /* Guest lists its own controllers, to release them, and the free ones that are awake, to claim
             * them. Main's controllers and sleeping free ones are not Guest's to touch. */
            if (owner->role != SIDE_ROLE_PRIMARY && !mine && !(online && !strcmp(requested, "unassigned"))) continue;
            if (index++ < skip || shown >= rows) continue;
            bool pressed = json_object_object_get_ex(d, "active", &active) && json_object_get_boolean(active);
            char label[256];
            int number = controller_index(d);
            const char *status =
                !can_move_controllers ? "Routing service needs update" :
                !online ? "Disconnected" : controller_text(d, "routing_error")[0] ? "Transfer failed; controls returned" : strcmp(target, requested) ? "Assigning…" : pressed ? "Pressed" : "Connected";
            const char *suffix = owner->role == SIDE_ROLE_PRIMARY ? "" : mine ? " · Guest" : " · Unassigned";
            /* A numbered row shows the number in a column of its own, so that the names start at the same
             * place for one and two digits whatever the font's widths. A row without a number (an older
             * routing service) reads as it always did: the name, then the first characters of its id. */
            if (number) snprintf(label, sizeof(label), "%s  %s%s", controller_text(d, "name"), status, suffix);
            else snprintf(label, sizeof(label), "%s %.6s %s%s", controller_text(d, "name"), key, status, suffix);
            int row_y = y + shown * 50;
            /* Main may forget a controller that is off and that the generic pad service keeps (a receiver of the session
             * stays listed). A connected one is released to Unassigned instead. */
            bool can_forget = owner->role == SIDE_ROLE_PRIMARY && can_move_controllers && !online &&
                !strcmp(controller_text(d, "backend"), "generic-pad");
            if (number) {
                char digits[16];
                snprintf(digits, sizeof(digits), "%d", number);
                modal_text(comp, parent, x, row_y, 28, 18, digits, 12, pressed ? .4 : .9, .92, .96, 1, true);
                modal_text(comp, parent, x + 30, row_y, width - 30 - (can_forget ? 88 : 0), 18, label, 12, pressed ? .4 : .9, .92, .96, 1, false);
            } else {
                modal_text(comp, parent, x, row_y, width - (can_forget ? 88 : 0), 18, label, 12, pressed ? .4 : .9, .92, .96, 1, false);
            }
            if (can_forget) {
                /* On the line of the name, at its right end, beside the three owners below it. */
                render_modal_button(comp, parent, MODAL_ACTION_FORGET_DEVICE, x + width - 80, row_y - 2, 80, 22, "Forget", false);
                snprintf(comp->modal_regions[comp->modal_region_count-1].device_name, 256, "controller:%s", key);
            }
            if (owner->role == SIDE_ROLE_PRIMARY) {
                const char *labels[] = {"Main", "Guest", "Unassigned"};
                const char *targets[] = {"main", "guest", "unassigned"};
                enum modal_action actions[] = {MODAL_ACTION_DEVICE_LEFT, MODAL_ACTION_DEVICE_RIGHT, MODAL_ACTION_DEVICE_FOLLOW};
                for (int j = 0; j < 3; j++) {
                    render_modal_button(comp, parent, can_move_controllers ? actions[j] : MODAL_ACTION_NONE, x + j * (third + 8), row_y + 20, third, 26,
                        labels[j], strcmp(target, targets[j]) == 0 && online);
                    snprintf(comp->modal_regions[comp->modal_region_count-1].device_name, 256, "controller:%s", key);
                }
            } else {
                render_modal_button(comp, parent, !can_move_controllers ? MODAL_ACTION_NONE :
                    mine ? MODAL_ACTION_RELEASE_DEVICE : MODAL_ACTION_CLAIM_DEVICE,
                    x, row_y + 20, half, 26, mine ? "Release" : "Claim", false);
                snprintf(comp->modal_regions[comp->modal_region_count-1].device_name, 256, "controller:%s", key);
            }
            shown++;
        }
        if (inventory) json_object_put(inventory);
        struct input_device_ctx *input;
        wl_list_for_each(input, &comp->input_devices, link) {
            if (owner->role != SIDE_ROLE_PRIMARY || !input_device_should_show_in_overlay(input)) continue;
            if (index++ < skip) continue;
            if (shown >= rows) break;
            const char *name = overlay_input_label(input->device->name);
            int row_y = y + shown * 50;
            modal_text(comp, parent, x, row_y, width, 18, name, 12, .9, .92, .96, 1, false);
            enum input_route_mode route = current_route_mode_for_device(comp, input);
            /* A mouse or keyboard can also be Shared: the cursor crosses the middle of the screen, and a keyboard
             * types in the side where the cursor is. A touchscreen belongs to one side and has no such choice. */
            bool can_share = input->device->type == WLR_INPUT_DEVICE_POINTER ||
                input->device->type == WLR_INPUT_DEVICE_KEYBOARD;
            const char *labels[] = {"Main", "Guest", "Shared"};
            enum modal_action actions[] = {MODAL_ACTION_DEVICE_LEFT, MODAL_ACTION_DEVICE_RIGHT, MODAL_ACTION_DEVICE_FOLLOW};
            bool chosen[] = {route == main_input_route_mode(comp), route == guest_input_route_mode(comp),
                route == INPUT_ROUTE_FOLLOW || route == INPUT_ROUTE_BOTH};
            int buttons = can_share ? 3 : 2, button_width = can_share ? third : half;
            for (int j = 0; j < buttons; j++) {
                render_modal_button(comp, parent, actions[j], x + j * (button_width + 8), row_y + 20, button_width, 26,
                    labels[j], chosen[j]);
                struct modal_region *region = &comp->modal_regions[comp->modal_region_count - 1];
                snprintf(region->device_name, sizeof(region->device_name), "input:%u", input->route_id);
            }
            shown++;
        }
        if (!shown) modal_text(comp, parent, x, y, width, 28, "No devices on this page", 14, .8, .83, .9, 1, false);
        struct control_candidate pair[2];
        if (owner->role == SIDE_ROLE_PRIMARY && control_swap_pair(comp, pair) != SWAP_UNAVAILABLE)
            render_modal_button(comp, parent, MODAL_ACTION_SWAP_DEVICE, x, py + ph - 104, width, 28, "Swap controls", false);
        render_modal_button(comp, parent, MODAL_ACTION_DEVICE_PREV, x, py + ph - 68, half, 28, "Previous", false);
        render_modal_button(comp, parent, MODAL_ACTION_DEVICE_NEXT, x + half + 8, py + ph - 68, half, 28, "Next", false);
    } else {
        if (owner->role == SIDE_ROLE_PRIMARY) {
        modal_text(comp, parent, x, y, width, 18, "Shared display layout", 13, .9, .92, .96, 1, true);
        int layout_quarter = (width - 24) / 4;
        render_modal_button(comp, parent, MODAL_ACTION_SINGLE, x, y + 22, layout_quarter, 32, "Full screen", comp->layout == LAYOUT_SINGLE);
        render_modal_button(comp, parent, MODAL_ACTION_SPLIT_VERTICAL, x + layout_quarter + 8, y + 22, layout_quarter, 32, "Side by side", comp->layout == LAYOUT_SPLIT_VERTICAL);
        render_modal_button(comp, parent, MODAL_ACTION_SPLIT_HORIZONTAL, x + 2 * (layout_quarter + 8), y + 22, layout_quarter, 32, "Top / bottom", comp->layout == LAYOUT_SPLIT_HORIZONTAL);
        render_modal_button(comp, parent, MODAL_ACTION_SINGLE_GUEST, x + 3 * (layout_quarter + 8), y + 22, layout_quarter, 32, "Guest full screen", comp->layout == LAYOUT_SINGLE_GUEST);
        if (owner->role == SIDE_ROLE_PRIMARY) {
            render_modal_button(comp, parent, MODAL_ACTION_GUEST_RIGHT, x, y + 66, half, 32,
                comp->layout == LAYOUT_SPLIT_HORIZONTAL ? "Main top" : "Main left", comp->guest_physical_side == SIDE_RIGHT);
            render_modal_button(comp, parent, MODAL_ACTION_GUEST_LEFT, x + half + 8, y + 66, half, 32,
                comp->layout == LAYOUT_SPLIT_HORIZONTAL ? "Main bottom" : "Main right", comp->guest_physical_side == SIDE_LEFT);
        } else {
            modal_text(comp, parent, x, y + 72, width, 24,
                "Main chooses the screen positions.", 12, .8, .83, .9, 1, false);
        }
        char power_heading[160];
        snprintf(power_heading, sizeof(power_heading), "Machine power%s%s",
            comp->power_message[0] ? " · " : "", comp->power_message);
        if (comp->power_message[0])
            /* A failure message can be longer than the line: two lines, from just above the heading's row. */
            modal_text(comp, parent, x, y + 106, width, 28, power_heading, 11, 1, .8, .4, 1, true);
        else
            modal_text(comp, parent, x, y + 110, width, 18, power_heading, 13, .9, .92, .96, 1, true);
        render_modal_button(comp, parent, MODAL_ACTION_POWER_SLEEP, x, y + 136, third, 32, "Sleep", false);
        render_modal_button(comp, parent, MODAL_ACTION_POWER_RESTART, x + third + 8, y + 136, third, 32, "Restart", false);
        render_modal_button(comp, parent, MODAL_ACTION_POWER_SHUTDOWN, x + 2 * (third + 8), y + 136, third, 32, "Shut down", false);
        if (comp->layout == LAYOUT_SINGLE) {
            render_modal_button(comp, parent, MODAL_ACTION_RESET_SIDE, x, y + 174, width, 28, "Reset Steam", false);
        } else {
            render_modal_button(comp, parent, MODAL_ACTION_RESET_SIDE, x, y + 174, half, 28, "Reset my side", false);
            render_modal_button(comp, parent, MODAL_ACTION_RESET_ALL, x + half + 8, y + 174, half, 28, "Reset both sides", false);
        }
        /* A row of its own below the others, so that the rows above keep their places: both sessions close and
         * stock Steam comes back, without a restart of the machine. */
        render_modal_button(comp, parent, MODAL_ACTION_POWER_EXIT, x, y + 210, width, 30, "Turn off Dosplex", false);
        } else {
            modal_text(comp, parent, x, y, width, 18, "Your session", 13, .9, .92, .96, 1, true);
            render_modal_button(comp, parent, MODAL_ACTION_RESET_SIDE, x, y + 22, width, 32, "Reset my side", false);
            modal_text(comp, parent, x, y + 70, width, 40,
                "Shared layout and machine power are managed by Main.", 12, .8, .83, .9, 1, false);
        }
    }
    modal_text(comp, parent, x, py + ph - 28, width, 18, "D-pad / arrows: move · A / Enter: select · B / Esc: back", 12, .65, .7, .8, 1, false);
    /* Help and Feedback, in the heading's row beside Close. Added last so that every entry before them keeps its
     * place in the list (the focus the menu opens with is a number). Not while a question waits for its answer.
     * Feedback is there only when the release carries the form. */
    if (!comp->device_move_active && comp->pending_shared_action == MODAL_ACTION_NONE) {
        if (comp->feedback_available)
            render_modal_button(comp, parent, MODAL_ACTION_FEEDBACK, px + pw - 44 - 8 - 34 - 8 - 96, py + 12, 96, 28, "Feedback", false);
        render_modal_button(comp, parent, MODAL_ACTION_HELP, px + pw - 44 - 8 - 34, py + 12, 34, 28, "?", false);
    }
}

static const char *side_state_name(enum side_state state) {
    switch (state) {
    case SIDE_GREETER: return "GREETER";
    case SIDE_CLAIMED: return "CLAIMED";
    default: return "UNKNOWN";
    }
}

static void rebuild_scene(struct compositor *comp) {
    int w = comp->output_width;
    int h = comp->output_height;

    if (!comp->overlay_tree) {
        return;
    }
    comp->scene_rebuilds++;

    /* Only destroy children of the overlay subtree.
     * App surface trees (children of scene root) are persistent and
     * must NOT be destroyed — they hold active Wayland client connections. */
    struct wlr_scene_node *child, *next;
    comp->background_rect = NULL;
    wl_list_for_each_safe(child, next, &comp->overlay_tree->children, link) {
        wlr_scene_node_destroy(child);
    }
    if (comp->modal_tree) {
        wl_list_for_each_safe(child, next, &comp->modal_tree->children, link) {
            wlr_scene_node_destroy(child);
        }
    }

    /* Background fills the entire screen */
    comp->background_rect = make_rect(comp->overlay_tree, 0, 0, w, h, (float*)BG_COLOR);

    /* Left side greeter (only if no app surface is running there) */
    bool left_visible = side_is_shown(comp, &comp->left);
    bool right_visible = side_is_shown(comp, &comp->right);

    if (left_visible && (!comp->left.app_surface || comp->left.recovery_required || comp->left.auth || comp->left.auth_error || comp->left.launch_pending)) {
        render_side_greeter(comp, &comp->left, comp->overlay_tree);
    } else {
        comp->left.num_regions = 0;
    }

    /* Right side greeter (only if no app surface is running there) */
    if (right_visible && (!comp->right.app_surface || comp->right.recovery_required || comp->right.auth || comp->right.auth_error || comp->right.launch_pending)) {
        render_side_greeter(comp, &comp->right, comp->overlay_tree);
    } else {
        comp->right.num_regions = 0;
    }

    if (!shown_side(comp)) {
        char debug_left[128], debug_right[128];
        side_debug_label(&comp->left, 'L', side_state_name(comp->left.state), debug_left, sizeof(debug_left));
        side_debug_label(&comp->right, 'R', side_state_name(comp->right.state), debug_right, sizeof(debug_right));

        make_text_node_left(comp->overlay_tree, 10, h - 30, 300, 24,
            debug_left, 12.0, 1.0, 1.0, 1.0, 0.7, false);
        make_text_node_left(comp->overlay_tree, w - 310, h - 30, 300, 24,
            debug_right, 12.0, 1.0, 1.0, 1.0, 0.7, false);
    }

    if (comp->modal_tree) {
        render_management_overlay(comp, comp->modal_tree);
        screen_bl_note_shown(comp, comp->screen_bl_drawn);
        wlr_scene_node_raise_to_top(&comp->modal_tree->node);
        /* A pixel-visible token lets test captures verify the actual
         * image, including nested X11 servers whose drawable may lag present. */
        if (comp->config.test_hooks && comp->capture_requested) {
            for (int bit = 0; bit < 32; bit++) {
                float value = (comp->capture_requested & (1u << bit)) ? 1.f : 0.f;
                float color[4] = {value, value, value, 1.f};
                make_rect(comp->modal_tree, bit * 4, 0, 4, 4, color);
            }
        }
    }
    position_side_cursor(comp, &comp->left, comp->left.x + comp->left.pointer_x, comp->left.y + comp->left.pointer_y);
    position_side_cursor(comp, &comp->right, comp->right.x + comp->right.pointer_x, comp->right.y + comp->right.pointer_y);
}


static void request_redraw(struct compositor *comp) {
    comp->scene_dirty = true;
    if (comp->output) {
        wlr_output_schedule_frame(comp->output);
    }
}

static void map_touch_to_layout(struct compositor *comp, double raw_x, double raw_y,
        double *lx, double *ly) {
    if (!comp || !lx || !ly) {
        return;
    }

    /* The Deck panel is physically portrait (800x1280), but Dosplex rotates it
     * into a logical 1280x800 landscape scene. libinput touch coordinates stay
     * in panel space, so rotate them into scene space here. */
    if (comp->output && comp->output->transform == WL_OUTPUT_TRANSFORM_270) {
        *lx = raw_y * comp->output_width;
        *ly = (1.0 - raw_x) * comp->output_height;
        return;
    }

    *lx = raw_x * comp->output_width;
    *ly = raw_y * comp->output_height;
}

static void position_side_cursor(struct compositor *comp, struct side_ctx *ctx, int x, int y) {
    if (!ctx->cursor_tree) return;
    if (!ctx->cursor_buffer && comp->cursor_mgr) {
        struct wlr_xcursor *cursor = wlr_xcursor_manager_get_xcursor(comp->cursor_mgr, "left_ptr", 1.0);
        if (cursor && cursor->image_count > 0) {
            struct wlr_xcursor_image *image = cursor->images[0];
            struct cairo_pixel_buf *cb = calloc(1, sizeof(*cb));
            if (cb) {
                wlr_buffer_init(&cb->base, &cairo_pixel_buf_impl, image->width, image->height);
                cb->width = image->width;
                cb->height = image->height;
                cb->data = malloc(image->width * image->height * 4);
                if (cb->data) {
                    memcpy(cb->data, image->buffer, image->width * image->height * 4);
                    ctx->cursor_buffer = wlr_scene_buffer_create(ctx->cursor_tree, &cb->base);
                    ctx->cursor_hotspot_x = image->hotspot_x;
                    ctx->cursor_hotspot_y = image->hotspot_y;
                }
                wlr_buffer_drop(&cb->base);
            }
        }
    }
    if (ctx->cursor_buffer) {
        wlr_scene_node_set_position(&ctx->cursor_tree->node,
            x - ctx->cursor_hotspot_x, y - ctx->cursor_hotspot_y);
        wlr_scene_node_set_enabled(&ctx->cursor_rect->node, false);
    } else {
        wlr_scene_node_set_position(&ctx->cursor_tree->node, x - 9, y - 9);
    }
    /* A side's nested Gamescope (cursor_composite=2) or nested KWin draws its
     * own cursor for every pointer event Dosplex forwards, including a
     * controller's emulated mouse that Dosplex never sees. Dosplex's per-side
     * marker therefore shows only where Dosplex itself draws: an empty side, or
     * this side's own menu. Over a mapped application it was a stray arrow
     * that belonged to nobody. */
    bool visible = ctx->pointer_seen && side_is_shown(comp, ctx) &&
        (!ctx->app_surface ||
         (comp->management_overlay_visible && side_input_held(comp, ctx->id)));
    wlr_scene_node_set_enabled(&ctx->cursor_tree->node, visible);
    wlr_scene_node_raise_to_top(&comp->cursor_tree->node);
}

static void update_cursor_visual(struct compositor *comp, int x, int y) {
    struct side_ctx *ctx = side_at_point(comp, x, y);
    ctx->pointer_seen = true;
    position_side_cursor(comp, ctx, x, y);
}

static bool surface_get_uid(struct wlr_surface *surface, uid_t *uid_out) {
    if (!surface || !surface->resource) {
        return false;
    }

    struct wl_client *client = wl_resource_get_client(surface->resource);
    if (!client) {
        return false;
    }

    pid_t pid = -1;
    uid_t uid = (uid_t)-1;
    gid_t gid = (gid_t)-1;
    wl_client_get_credentials(client, &pid, &uid, &gid);
    if (uid == (uid_t)-1) {
        return false;
    }

    if (uid_out) {
        *uid_out = uid;
    }
    return true;
}

/* The account a side belongs to: the one that claimed it, else the one the launcher named for it. */
static const char *side_account_name(struct side_ctx *ctx) {
    const char *owner = NULL;
    if (!ctx) return NULL;
    if (ctx->state == SIDE_CLAIMED && ctx->owner[0]) {
        owner = ctx->owner;
    } else if (ctx->id == SIDE_LEFT) {
        owner = getenv("DOSPLEX_LEFT_USER");
        if (!owner || !owner[0]) {
            owner = "deck";
        }
    } else if (ctx->id == SIDE_RIGHT) {
        owner = getenv("DOSPLEX_RIGHT_USER");
        if (!owner || !owner[0]) {
            owner = "guest";
        }
    }
    return owner && owner[0] ? owner : NULL;
}

static bool side_matches_uid(struct side_ctx *ctx, uid_t uid) {
    const char *owner = side_account_name(ctx);
    if (!owner) {
        return false;
    }

    struct passwd *pw = getpwnam(owner);
    if (!pw) {
        return false;
    }

    return pw->pw_uid == uid;
}

static struct side_ctx *pick_side_for_surface(struct compositor *comp,
        struct wlr_surface *surface) {
    uid_t uid = 0;
    bool have_uid = surface_get_uid(surface, &uid);
    if (have_uid) {
        if (side_matches_uid(&comp->left, uid)) {
            return &comp->left;
        }
        if (side_matches_uid(&comp->right, uid)) {
            return &comp->right;
        }
    }

    /* Prefer the side that does NOT already have an app surface, so two
     * nested compositors connecting in quick succession are split evenly
     * rather than both landing on the left side. */
    bool left_open = (comp->left.state == SIDE_CLAIMED || comp->left.state == SIDE_GREETER)
                     && !comp->left.app_surface;
    bool right_open = (comp->right.state == SIDE_CLAIMED || comp->right.state == SIDE_GREETER)
                      && !comp->right.app_surface;

    if (left_open && right_open) {
        /* Both open — try UID matching again with relaxed state check,
         * or fall back to left-then-right. */
        if (have_uid && side_matches_uid(&comp->left, uid)) return &comp->left;
        if (have_uid && side_matches_uid(&comp->right, uid)) return &comp->right;
        return &comp->left;
    }
    if (left_open) return &comp->left;
    if (right_open) return &comp->right;

    return NULL;
}

/* === App surface lifecycle (xdg_shell) === */

static struct side_xdg_window *side_find_xdg_window_for_surface(
        struct side_ctx *ctx, struct wlr_surface *surface) {
    if (!ctx || !surface) {
        return NULL;
    }
    side_init_xdg_windows(ctx);

    struct wlr_surface *root = wlr_surface_get_root_surface(surface);
    struct side_xdg_window *window = NULL;
    wl_list_for_each(window, &ctx->xdg_windows, link) {
        if (!window->surface) {
            continue;
        }
        if (wlr_surface_get_root_surface(window->surface) == root) {
            return window;
        }
    }

    return NULL;
}

static void side_activate_xdg_window(struct side_ctx *ctx,
        struct side_xdg_window *window, bool raise) {
    if (!ctx || !window || !window->surface) {
        return;
    }
    side_init_xdg_windows(ctx);

    if (ctx->active_xdg_window == window &&
            ctx->app_surface == window->surface && !raise) {
        return;
    }

    struct side_xdg_window *other = NULL;
    wl_list_for_each(other, &ctx->xdg_windows, link) {
        if (!other->xdg_surface || other->xdg_surface->role != WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
            continue;
        }

        bool active = other == window;
        wlr_xdg_toplevel_set_activated(other->xdg_surface->toplevel, active);
        wlr_xdg_surface_schedule_configure(other->xdg_surface);
    }

    ctx->active_xdg_window = window;
    ctx->app_surface = window->surface;
    ctx->app_scene_tree = window->scene_tree;
    ctx->app_scene_tree_owned = false;
    if (ctx->comp) {
        write_runtime_state(ctx->comp);
    }

    if (raise && window->scene_tree) {
        wlr_scene_node_raise_to_top(&window->scene_tree->node);
        if (ctx->comp && ctx->comp->cursor_tree) {
            wlr_scene_node_raise_to_top(&ctx->comp->cursor_tree->node);
        }
    }
}

static void side_refresh_active_surface(struct side_ctx *ctx) {
    if (!ctx) {
        return;
    }
    side_init_xdg_windows(ctx);
    side_init_xwayland_windows(ctx);

    ctx->active_xdg_window = NULL;
    struct side_xdg_window *window = NULL;
    wl_list_for_each(window, &ctx->xdg_windows, link) {
        if (!window->surface) {
            continue;
        }
        if (!window->surface->mapped) {
            continue;
        }
        ctx->active_xdg_window = window;
    }

    if (ctx->active_xdg_window) {
        side_activate_xdg_window(ctx, ctx->active_xdg_window, true);
        return;
    }

    /* If no XDG window is active, check if there are any Xwayland windows */
    if (!wl_list_empty(&ctx->xwayland_windows)) {
        struct side_xwayland_window *xw = wl_container_of(ctx->xwayland_windows.next, xw, link);
        ctx->app_surface = xw->surface;
        ctx->app_xwayland_handle = xw->xwayland_handle;
        ctx->app_xwayland_surface = xw->scene_surface;
        ctx->app_scene_tree = xw->scene_tree;
        ctx->app_scene_tree_owned = false;
        if (ctx->comp) {
            write_runtime_state(ctx->comp);
        }
        return;
    }

    if (!ctx->app_xwayland_handle) {
        ctx->app_surface = NULL;
        ctx->app_scene_tree = NULL;
        ctx->app_scene_tree_owned = false;
        if (ctx->comp) {
            write_runtime_state(ctx->comp);
        }
    }
}

static void side_disarm_scale_override(struct side_xdg_window *window);

static void side_remove_xdg_window(struct side_xdg_window *window) {
    if (!window) return;

    struct side_ctx *ctx = window->ctx;

    if (window->scale_reconfigure) {
        wl_event_source_remove(window->scale_reconfigure);
        window->scale_reconfigure = NULL;
    }
    if (window->map_watchdog) {
        wl_event_source_remove(window->map_watchdog);
        window->map_watchdog = NULL;
    }
    side_disarm_scale_override(window);

    /* Unarm listeners safely */
    if (window->surface_commit_armed) {
        wl_list_remove(&window->surface_commit.link);
        window->surface_commit_armed = false;
    }
    if (window->surface_map_armed) {
        wl_list_remove(&window->surface_map.link);
        window->surface_map_armed = false;
    }
    if (window->surface_unmap_armed) {
        wl_list_remove(&window->surface_unmap.link);
        window->surface_unmap_armed = false;
    }
    if (window->surface_destroy_armed) {
        wl_list_remove(&window->surface_destroy.link);
        window->surface_destroy_armed = false;
    }
    if (window->surface_fullscreen_armed) {
        wl_list_remove(&window->surface_fullscreen.link);
        window->surface_fullscreen_armed = false;
    }
    if (window->surface_client_commit_armed) {
        wl_list_remove(&window->surface_client_commit.link);
        window->surface_client_commit_armed = false;
    }

    /* Remove from side's window list */
    if (!wl_list_empty(&window->link)) {
        wl_list_remove(&window->link);
        wl_list_init(&window->link);
    }

    if (ctx) {
        if (ctx->active_xdg_window == window) {
            ctx->active_xdg_window = NULL;
        }
        if (ctx->app_surface == window->surface) {
            ctx->app_surface = NULL;
            ctx->app_scene_tree = NULL;
        }
    }

    /* window->scene_tree is owned by wlroots (via wlr_scene_xdg_surface_create)
     * and will be destroyed automatically when the xdg_surface is destroyed.
     * We don't destroy it here. */

    free(window);

    if (ctx && ctx->comp) {
        side_refresh_active_surface(ctx);
        write_runtime_state(ctx->comp);
        request_redraw(ctx->comp);
    }
}

static void side_clear_xdg_windows(struct side_ctx *ctx) {
    if (!ctx || !ctx->xdg_windows_initialized) {
        return;
    }

    struct side_xdg_window *window = NULL;
    struct side_xdg_window *tmp = NULL;
    wl_list_for_each_safe(window, tmp, &ctx->xdg_windows, link) {
        side_remove_xdg_window(window);
    }

    ctx->active_xdg_window = NULL;
}

struct side_xwayland_window;
static void side_remove_xwayland_window(struct side_xwayland_window *window);

static void side_clear_xwayland_windows(struct side_ctx *ctx) {
    if (!ctx || !ctx->xwayland_windows_initialized) {
        return;
    }

    struct side_xwayland_window *window = NULL;
    struct side_xwayland_window *tmp = NULL;
    wl_list_for_each_safe(window, tmp, &ctx->xwayland_windows, link) {
        side_remove_xwayland_window(window);
    }
}

static void side_clear_app(struct side_ctx *ctx) {
    bool destroyed_app_tree = false;
    struct wlr_surface *root = NULL;

    if (ctx->app_surface) {
        root = wlr_surface_get_root_surface(ctx->app_surface);
    }

    side_clear_xwayland_windows(ctx);

    if (ctx->surface_commit_armed) {
        wl_list_remove(&ctx->surface_commit.link);
        ctx->surface_commit_armed = false;
    }
    if (ctx->surface_unmap_armed) {
        wl_list_remove(&ctx->surface_unmap.link);
        ctx->surface_unmap_armed = false;
    }
    if (ctx->surface_destroy_armed) {
        wl_list_remove(&ctx->surface_destroy.link);
        ctx->surface_destroy_armed = false;
    }

    if (ctx->app_surface) {
        ctx->app_surface = NULL;
    }
    struct wlr_seat *seat = seat_for_side(ctx->comp, ctx->id);
    if (root && seat && seat->keyboard_state.focused_surface == root) {
        wlr_seat_keyboard_notify_clear_focus(seat);
        set_kbd_focus_surface(ctx->comp, seat, NULL);
    }
    if (ctx->app_scene_tree) {
        if (ctx->app_scene_tree_owned) {
            wlr_scene_node_destroy(&ctx->app_scene_tree->node);
        }
        ctx->app_scene_tree = NULL;
        ctx->app_scene_tree_owned = false;
        destroyed_app_tree = true;
    }
    if (ctx->comp) {
        write_runtime_state(ctx->comp);
    }
    if (ctx->app_xwayland_surface) {
        if (!destroyed_app_tree && ctx->app_xwayland_surface->buffer) {
            wlr_scene_node_destroy(&ctx->app_xwayland_surface->buffer->node);
        }
        ctx->app_xwayland_surface = NULL;
    }
    if (ctx->app_xwayland_handle) {
        if (ctx->xwayland_request_configure_armed) {
            wl_list_remove(&ctx->xwayland_request_configure.link);
            ctx->xwayland_request_configure_armed = false;
        }
        ctx->app_xwayland_handle = NULL;
    }
    if (ctx->pending_xwayland_surface) {
        if (ctx->xwayland_associate_armed) {
            wl_list_remove(&ctx->xwayland_associate.link);
            ctx->xwayland_associate_armed = false;
        }
        if (ctx->xwayland_surface_map_armed) {
            wl_list_remove(&ctx->xwayland_surface_map.link);
        }
        ctx->xwayland_surface_map_armed = false;
        ctx->pending_xwayland_surface = NULL;
    }
    eis_side_drop(ctx, "the application went away");
    /* Without its application the side's client holds no pointer constraint. */
    pointer_constraints_sync(ctx->comp, seat_for_side(ctx->comp, ctx->id));
}

static void configure_side_xwayland_surface(struct compositor *comp,
        struct side_ctx *target, struct wlr_xwayland_surface *xsurface) {
    int target_width = side_logical_width(target);
    int target_height = side_logical_height(target);

    wlr_log(WLR_INFO, "configuring X11 host window for %s side to %dx%d+0+0",
        target->id == SIDE_LEFT ? "left" : "right",
        target_width, target_height);
    wlr_xwayland_surface_configure(xsurface, 0, 0, target_width, target_height);
    wlr_xwayland_surface_activate(xsurface, true);
}

/* === Desktop payload scale ===
 * A nested KWin sets its output scale from the preferred scale of the host's
 * wp_fractional_scale_v1 at its first configure and ignores --scale and kscreen
 * afterwards. For a Desktop side the compositor therefore sends the scale the
 * launcher chose (preferred scale and the side output's wl_output
 * scale), configures the toplevel at region / scale, and draws the surface
 * back up to the region. Other payloads never get a preferred scale. */

/* A nested KWin applies the preferred scale only on the next xdg_toplevel
 * configure it handles. When the scale reaches it after its first configure, or
 * the fractional-scale object did not exist yet when the scale was first sent,
 * it would stay at scale 1. So a scale send is followed shortly by the same
 * scale again and a fresh configure of the same logical size. */
#define SCALE_RECONFIGURE_DELAY_MS 80
/* Follow-up configures per scale sent: one for the scale itself and one for the map of a surface that got the
 * scale after its first configure (a fractional-scale object made late). A client that shows a buffer of
 * another size (a nested KWin scanning out a fullscreen video's buffer) never looks applied, and more configures
 * of the same size would only keep it busy at every scene output update. */
#define SCALE_FOLLOWUPS 2

/* Whether a surface of the toplevel's tree shows the side's toplevel at the side's scale: its logical size is the
 * toplevel size and what it shows of its buffer (its viewport's source, else the whole buffer) is that size times the
 * scale (a nested KWin draws its buffer at the scale it applied). One surface is enough: KWin 6.7's own surface is a
 * single pixel stretched over the output, and what it draws is the buffer of its primary plane, a subsurface the size
 * of the output. */
static bool side_tree_shows_scale(struct wlr_surface *surface, int width, int height, int scale) {
    if (surface->current.width == width && surface->current.height == height && surface->current.buffer_width > 0) {
        struct wlr_fbox shown;
        wlr_surface_get_buffer_source_box(surface, &shown);
        if ((int)lround(shown.width) == (int)(((long)width * scale + 500) / 1000) &&
                (int)lround(shown.height) == (int)(((long)height * scale + 500) / 1000)) return true;
    }
    struct wl_list *stacks[] = {&surface->current.subsurfaces_below, &surface->current.subsurfaces_above};
    for (size_t i = 0; i < 2; i++) {
        struct wlr_subsurface *subsurface;
        wl_list_for_each(subsurface, stacks[i], current.link) {
            if (side_tree_shows_scale(subsurface->surface, width, height, scale)) return true;
        }
    }
    return false;
}

/* Whether the client already shows the side's toplevel at the side's scale. A client in that state has nothing to
 * apply, so a configure of the same size would only cost it a frame. */
static bool side_window_scale_applied(const struct side_xdg_window *window) {
    if (!window || !window->surface) return false;
    return side_tree_shows_scale(window->surface, side_toplevel_width(window->ctx), side_toplevel_height(window->ctx),
        side_scale_milli(window->ctx));
}

static int side_scale_reconfigure(void *data) {
    struct side_xdg_window *window = data;
    if (!window->scale_reconfigure) return 0;
    wl_event_source_remove(window->scale_reconfigure);
    window->scale_reconfigure = NULL;
    if (!window->surface || !window->xdg_surface || !window->xdg_surface->toplevel ||
            !window->xdg_surface->initialized) return 0;
    int scale = side_scale_milli(window->ctx);
    if (side_window_scale_applied(window)) {
        /* The client applied the scale on its own (or had it before the timer ran): the follow-up is idempotent. */
        wlr_log(WLR_DEBUG, "No configure re-sent to the %s side's surface: it shows %dx%d at scale %d/1000 already",
            window->ctx->id == SIDE_LEFT ? "left" : "right",
            side_toplevel_width(window->ctx), side_toplevel_height(window->ctx), scale);
        return 0;
    }
    if (window->scale_followups_left <= 0) {
        wlr_log(WLR_DEBUG, "No configure re-sent to the %s side's surface: %d follow-ups were sent for scale %d/1000",
            window->ctx->id == SIDE_LEFT ? "left" : "right", SCALE_FOLLOWUPS, scale);
        return 0;
    }
    window->scale_followups_left--;
    if (scale != 1000) wlr_fractional_scale_v1_notify_scale(window->surface, (double)scale / 1000.0);
    wlr_xdg_toplevel_set_size(window->xdg_surface->toplevel,
        side_toplevel_width(window->ctx), side_toplevel_height(window->ctx));
    wlr_xdg_surface_schedule_configure(window->xdg_surface);
    window->scale_resent++;
    window->scale_resent_total++;
    wlr_log(WLR_INFO, "Re-sent configure %dx%d at scale %d/1000 to the %s side's surface",
        side_toplevel_width(window->ctx), side_toplevel_height(window->ctx), scale,
        window->ctx->id == SIDE_LEFT ? "left" : "right");
    return 0;
}

static void side_schedule_scale_reconfigure(struct side_xdg_window *window) {
    if (!window->scale_reconfigure && window->ctx->comp && window->ctx->comp->display) {
        window->scale_reconfigure = wl_event_loop_add_timer(
            wl_display_get_event_loop(window->ctx->comp->display), side_scale_reconfigure, window);
        if (window->scale_reconfigure) wl_event_source_timer_update(window->scale_reconfigure, SCALE_RECONFIGURE_DELAY_MS);
    }
}

/* The scene surface's own outputs_update listener (wlroots 0.17 types/scene/surface.c,
 * handle_scene_buffer_outputs_update) sends the surface the scale of the buffer's primary output, the host
 * output's (1), on every change of the buffer's outputs or primary output. While the side's scale is not 1 that
 * listener is off the buffer's signal: the side's scale is the compositor's to send, and a scene that sent 1 with
 * the compositor sending the side's scale right after made the client's preferred scale flap (120, then 240) on
 * every output update. The listener is the scene surface's "private state" in wlr_scene.h, laid out the same in
 * 0.17.1 and 0.17.4. Put back with resync, it runs once, so the surface has the scene's scale again. */
static void side_set_scene_scale_held(struct side_xdg_window *window, bool hold, bool resync) {
    if (!window || !window->scale_buffer || hold == window->scale_scene_listener_held) return;
    struct wlr_scene_surface *scene_surface = wlr_scene_surface_try_from_buffer(window->scale_buffer);
    /* Held back only for this window's own surface; put back whatever the window's state is now. */
    if (!scene_surface || (hold && scene_surface->surface != window->surface)) return;
    wl_list_remove(&scene_surface->outputs_update.link);
    if (hold) {
        wl_list_init(&scene_surface->outputs_update.link);
    } else {
        /* Where it was: before the compositor's own listener on the signal (side_scale_outputs_update follows it). */
        wl_list_insert(window->scale_outputs_update.link.prev, &scene_surface->outputs_update.link);
    }
    window->scale_scene_listener_held = hold;
    wlr_log(WLR_INFO, "%s the scene's own output scale updates of the %s side's surface",
        hold ? "Held back" : "Restored", window->ctx->id == SIDE_LEFT ? "left" : "right");
    if (!hold && resync) scene_surface->outputs_update.notify(&scene_surface->outputs_update, NULL);
}

/* Held back while the side's scale is not 1; idempotent. */
static void side_sync_scene_scale(struct side_xdg_window *window) {
    if (window && window->ctx) side_set_scene_scale_held(window, side_scale_milli(window->ctx) != 1000, true);
}

static void side_send_window_scale(struct side_xdg_window *window) {
    if (!window || !window->surface) return;
    side_sync_scene_scale(window);
    int scale = side_scale_milli(window->ctx);
    /* Nothing is sent to a surface that never had a scale and stays at 1. */
    if (scale == window->scale_sent_milli || (scale == 1000 && window->scale_sent_milli == 0)) return;
    window->scale_sent_milli = scale;
    window->scale_followups_left = SCALE_FOLLOWUPS;
    wlr_fractional_scale_v1_notify_scale(window->surface, (double)scale / 1000.0);
    wlr_log(WLR_INFO, "Sent preferred scale %d/1000 to the %s side's surface",
        scale, window->ctx->id == SIDE_LEFT ? "left" : "right");
    side_schedule_scale_reconfigure(window);
}

/* How often a scaled side's surface has scene output updates (each one used to cost a preferred scale flap and,
 * often, a configure): the first is logged at INFO, the rest at DEBUG, and a summary line at INFO every 5 s while
 * they keep coming, with the configures re-sent in that time, so a storm of them stays visible in the log. */
static void side_count_scale_update(struct side_xdg_window *window) {
    window->scale_updates++;
    window->scale_updates_total++;
    double now = monotonic_seconds();
    if (window->scale_updates_since <= 0) {
        window->scale_updates_since = now;
        return;
    }
    double elapsed = now - window->scale_updates_since;
    if (elapsed < 5.0) return;
    if (window->scale_updates > 1 || window->scale_resent > 0) {
        wlr_log(WLR_INFO, "Scene output updates on the %s side's surface: %u in %.1f s, %u configures re-sent "
            "(%u and %u in all)", window->ctx->id == SIDE_LEFT ? "left" : "right", window->scale_updates, elapsed,
            window->scale_resent, window->scale_updates_total, window->scale_resent_total);
    }
    window->scale_updates = 0;
    window->scale_resent = 0;
    window->scale_updates_since = now;
}

/* wlroots 0.17 types/scene/surface.c handle_scene_buffer_outputs_update calls
 * wlr_fractional_scale_v1_notify_scale() and wlr_surface_set_preferred_buffer_scale()
 * with the scale of the scene buffer's primary output whenever its output set
 * changes. That is the host output (scale 1), which would take the preferred
 * scale back from a Desktop side. For a side whose scale is not 1000 that listener
 * is held back (side_set_scene_scale_held), so the surface keeps the side's scale
 * and nothing has to be put back. Should the scene's listener still have run
 * (this one follows it: wl_signal_emit_mutable runs listeners in order), this puts
 * our scale back and has the toplevel configured again, if the client does not
 * show it at the side's scale already, so the nested KWin applies it. A side at
 * scale 1000 is left to the scene. */
static void side_scale_outputs_update(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, scale_outputs_update);
    if (!window->surface || !window->scale_buffer) return;
    int scale = side_scale_milli(window->ctx);
    const struct wlr_scene_output *primary = window->scale_buffer->primary_output;
    const char *side = window->ctx->id == SIDE_LEFT ? "left" : "right";
    if (!window->scale_update_logged) {
        window->scale_update_logged = true;
        wlr_log(WLR_INFO, "First scene output update on the %s side's surface: primary output %s scale %.3f, side scale %d/1000",
            side, primary ? primary->output->name : "(none)", primary ? (double)primary->output->scale : 0.0, scale);
    }
    if (scale == 1000) return;
    side_count_scale_update(window);
    if (!primary) return; /* the scene sent nothing */
    if ((int)(primary->output->scale * 1000.0 + 0.5) == scale) return;
    enum wlr_log_importance level = window->scale_kept_logged ? WLR_DEBUG : WLR_INFO;
    window->scale_kept_logged = true;
    /* The surface's preferred buffer scale follows the side's; a no-op once it has. */
    wlr_surface_set_preferred_buffer_scale(window->surface, (int32_t)ceil((double)scale / 1000.0));
    if (window->scale_scene_listener_held) {
        wlr_log(level, "Kept preferred scale %d/1000 after a scene output update on the %s side's surface "
            "(the scene's own scale update is held back)", scale, side);
    } else {
        wlr_fractional_scale_v1_notify_scale(window->surface, (double)scale / 1000.0);
        window->scale_sent_milli = scale;
        wlr_log(level, "Kept preferred scale %d/1000 after a scene output update on the %s side's surface",
            scale, side);
    }
    /* A client that got the scale after its first configure (its fractional-scale object came late) applies it
     * only on a configure after that: the map is the moment to give it one, within SCALE_FOLLOWUPS. */
    if (!side_window_scale_applied(window)) side_schedule_scale_reconfigure(window);
}

static void side_scale_buffer_destroyed(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, scale_buffer_destroy);
    side_disarm_scale_override(window);
}

static void side_restore_scene_commit(struct side_xdg_window *window, bool resync);

static void side_disarm_scale_override(struct side_xdg_window *window) {
    if (!window) return;
    /* The buffer is going away or the window is: the commit listeners of the scene surfaces of its tree and the
     * toplevel's outputs_update listener go back on the signals they leave with. */
    side_restore_scene_commit(window, false);
    if (!window->scale_buffer) return;
    window->zoom_applied = false;
    side_set_scene_scale_held(window, false, false);
    wl_list_remove(&window->scale_outputs_update.link);
    wl_list_remove(&window->scale_buffer_destroy.link);
    window->scale_buffer = NULL;
}

/* Walks the tree with its disabled nodes too: wlroots keeps a surface's tree
 * disabled until the surface maps, and the map enables it, which is the very
 * output update (scale 1) we have to answer. wlr_scene_node_for_each_buffer
 * skips disabled nodes, so the buffer would only be found after that update. */
static struct wlr_scene_buffer *find_surface_buffer(struct wlr_scene_node *node, struct wlr_surface *surface) {
    if (node->type == WLR_SCENE_NODE_BUFFER) {
        struct wlr_scene_buffer *buffer = wlr_scene_buffer_from_node(node);
        struct wlr_scene_surface *scene_surface = wlr_scene_surface_try_from_buffer(buffer);
        return scene_surface && scene_surface->surface == surface ? buffer : NULL;
    }
    if (node->type != WLR_SCENE_NODE_TREE) return NULL;
    struct wlr_scene_node *child;
    wl_list_for_each(child, &wlr_scene_tree_from_node(node)->children, link) {
        struct wlr_scene_buffer *buffer = find_surface_buffer(child, surface);
        if (buffer) return buffer;
    }
    return NULL;
}

/* Follow the toplevel surface's scene buffer; idempotent, so a commit retries it. */
static void side_arm_scale_override(struct side_xdg_window *window) {
    if (!window || window->scale_buffer || !window->scene_tree || !window->surface) return;
    struct wlr_scene_buffer *buffer = find_surface_buffer(&window->scene_tree->node, window->surface);
    if (!buffer) return;
    window->scale_buffer = buffer;
    window->scale_outputs_update.notify = side_scale_outputs_update;
    wl_signal_add(&buffer->events.outputs_update, &window->scale_outputs_update);
    window->scale_buffer_destroy.notify = side_scale_buffer_destroyed;
    wl_signal_add(&buffer->node.events.destroy, &window->scale_buffer_destroy);
    side_sync_scene_scale(window);
}

static int scale_up_milli(int value, int scale_milli) {
    return (int)(((long)value * scale_milli + 500) / 1000);
}

/* A position or an edge at the side's scale, rounded to the nearest pixel (a half up), a negative one the same way. */
static int scale_edge_milli(int value, int scale_milli) {
    long numerator = (long)value * scale_milli + 500;
    long quotient = numerator / 1000;
    if (numerator % 1000 < 0) quotient--;
    return (int)quotient;
}

/* The surface is region / scale in its own units; draw it region-sized. Only the size is scaled, which is all
 * there is to do for a toplevel whose surfaces are not taken over (see "The surface tree" below). */
static void scale_surface_buffer(struct wlr_scene_buffer *buffer, int sx, int sy, void *data) {
    (void)sx; (void)sy;
    const struct side_ctx *ctx = data;
    struct wlr_scene_surface *scene_surface = wlr_scene_surface_try_from_buffer(buffer);
    if (!scene_surface) return;
    int scale = side_scale_milli(ctx);
    struct wlr_surface *surface = scene_surface->surface;
    wlr_scene_buffer_set_dest_size(buffer,
        scale_up_milli(surface->current.width, scale), scale_up_milli(surface->current.height, scale));
}

/* === The surface tree ===
 * A nested KWin 6.7 builds each of its outputs as a tree of surfaces: the toplevel's own surface (a single-pixel
 * buffer stretched over the output) and a subsurface for each output layer, the primary plane's (the picture itself)
 * at the origin and the overlay planes' (a Plasma panel) where they go, in the toplevel's units and added after it
 * has mapped. The side's scale draws that tree at scale times its size: each surface at its logical size times the
 * scale, each subsurface at its logical position times the scale (relative to its parent's, so a nested one lands
 * where its parent's scaling puts it).
 *
 * wlroots 0.17's scene follows a surface tree with two listeners on the commit signal of every surface: the scene
 * surface's (types/scene/surface.c: the surface's own buffer, with its destination, source, opaque region and
 * damage) and the scene subsurface tree's (types/scene/subsurface_tree.c, private to wlroots: the stacking and the
 * positions of the surface's subsurfaces, from their current state). Both work in the toplevel's units. While the
 * side's scale is not 1 the first one leaves the signal of every surface of the toplevel's tree and the listener
 * below takes its place, which does the same at the scale. That is how the toplevel's own surface has been handled
 * since the overlay stopped being rebuilt for every commit: an unchanged size costs the scene nothing and only the
 * client's damage reaches the output.
 *
 * The second one keeps running, since the stacking is its work, but the positions it lays the subsurfaces out at are
 * the scaled ones: it reads them from the subsurfaces' current state on the commit of their parent, and for the
 * length of that commit (a listener ahead of it, and the one below, which is the last) that state holds them at the
 * scale. wlroots applies a subsurface's position when its parent commits, just before the commit signal, so what the
 * client set is what the surface's `pending` state holds then and what is put back after. Setting the scene trees
 * afterwards instead would put a subsurface that is not at its parent's origin (a panel) at its logical position
 * first, from where the scene lays it out, and back at its scaled one: the scene damages the area at both places,
 * twice for every commit of the parent, which is every frame of a KWin whose panel is on a plane (1.5 Mpx for each
 * frame at 4K with a panel and two small subsurfaces, where the client's own damage was a block of 128x128).
 * A subsurface gets its scene tree when its parent commits (wlroots adds it then, ahead of the commit signal), so
 * that commit is where it is taken over too, and a scaled side's new subsurface is never drawn at its logical size.
 *
 * Like the toplevel's, a surface's listeners are put back, and the scene's handlers run once, when the scale goes back
 * to 1 and when the surface goes. */
/* How a window's toplevel tree is mapped onto the region: by the side's desktop scale, or by Zoom's affine map. */
struct side_edge_map {
    bool zoom;
    int scale_milli;
    struct zoom_map z;
};

static void side_window_edge_map(const struct side_xdg_window *window, struct side_edge_map *map) {
    map->zoom = false;
    map->scale_milli = side_scale_milli(window->ctx);
    if (map->scale_milli != 1000 || !window->surface || !side_zoom_in_effect(window->ctx)) return;
    int monitor_w, monitor_h;
    side_monitor_size(window->ctx, &monitor_w, &monitor_h);
    map->zoom = zoom_map_make(window->surface->current.width, window->surface->current.height,
        monitor_w, monitor_h, &map->z);
}

static int side_map_edge_x(const struct side_edge_map *map, int x) {
    return map->zoom ? zoom_edge_x(&map->z, x) : scale_edge_milli(x, map->scale_milli);
}

static int side_map_edge_y(const struct side_edge_map *map, int y) {
    return map->zoom ? zoom_edge_y(&map->z, y) : scale_edge_milli(y, map->scale_milli);
}

struct side_scaled_surface {
    struct wl_list link; /* side_xdg_window.scale_surfaces */
    struct side_xdg_window *window;
    struct wlr_surface *surface;
    struct wlr_scene_surface *scene_surface; /* its commit listener is off the surface's signal while this exists */
    struct wl_listener pre_commit; /* the first on the surface's commit signal: the subsurfaces' positions at the scale */
    struct wl_listener surface_commit; /* the one that took the scene surface's place: they are put back, the buffer follows */
    struct wl_listener buffer_destroy; /* its scene buffer goes, and the scene surface with it */
    bool positions_at_scale; /* the subsurfaces' current positions are the scaled ones, from pre_commit to surface_commit */
    bool hidden; /* Zoom crops the whole surface away: its scene buffer is disabled */
};

static struct side_scaled_surface *side_scaled_surface_arm(struct side_xdg_window *window, struct wlr_surface *surface);
static void side_scaled_surface_commit(struct wl_listener *listener, void *data);

static struct side_scaled_surface *side_scaled_surface_find(const struct side_xdg_window *window,
        const struct wlr_surface *surface) {
    struct side_scaled_surface *scaled;
    wl_list_for_each(scaled, &window->scale_surfaces, link) {
        if (scaled->surface == surface) return scaled;
    }
    return NULL;
}

/* Where a surface's top-left corner is in the toplevel's units: the sum of its subsurface positions up the tree
 * (the origin for the toplevel's own surface). */
static void side_surface_origin(struct wlr_surface *surface, int *x, int *y) {
    *x = *y = 0;
    struct wlr_subsurface *subsurface;
    while ((subsurface = wlr_subsurface_try_from_wlr_surface(surface))) {
        *x += subsurface->current.x;
        *y += subsurface->current.y;
        surface = subsurface->parent;
    }
}

/* Where a surface's scene buffer is drawn from and to at the side's scale: its size (from its edges), source, opaque
 * region and transform. False when the surface has no size (no buffer). */
static bool side_scaled_surface_set_geometry(struct side_scaled_surface *scaled) {
    struct wlr_scene_buffer *scene_buffer = scaled->scene_surface->buffer;
    struct wlr_surface *surface = scaled->surface;
    struct side_edge_map map;
    side_window_edge_map(scaled->window, &map);
    const struct wlr_surface_state *state = &surface->current;
    int origin_x, origin_y;
    side_surface_origin(surface, &origin_x, &origin_y);
    int width = side_map_edge_x(&map, origin_x + state->width) - side_map_edge_x(&map, origin_x);
    int height = side_map_edge_y(&map, origin_y + state->height) - side_map_edge_y(&map, origin_y);
    if (state->width <= 0 || state->height <= 0) return false;
    struct wlr_fbox src_box;
    wlr_surface_get_buffer_source_box(surface, &src_box);
    pixman_region32_t opaque;
    pixman_region32_init(&opaque);
    if (map.zoom) {
        /* The part of the surface inside Z, from the buffer's own source box; nothing is declared opaque. */
        struct zoom_surface part;
        zoom_surface_map(&map.z, origin_x, origin_y, state->width, state->height, &part);
        if (part.empty) {
            wlr_scene_node_set_enabled(&scene_buffer->node, false);
            scaled->hidden = true;
            pixman_region32_fini(&opaque);
            return true;
        }
        if (scaled->hidden) wlr_scene_node_set_enabled(&scene_buffer->node, true);
        scaled->hidden = false;
        wlr_scene_buffer_set_filter_mode(scene_buffer, zoom_scene_filter(map.z.k));
        width = part.right - part.left;
        height = part.bottom - part.top;
        if (surface != scaled->window->surface) {
            struct wlr_fbox cropped = {
                .x = src_box.x + (part.ix - origin_x) / state->width * src_box.width,
                .y = src_box.y + (part.iy - origin_y) / state->height * src_box.height,
                .width = part.iw / state->width * src_box.width,
                .height = part.ih / state->height * src_box.height,
            };
            src_box = cropped;
        }
    } else {
        if (scaled->hidden) wlr_scene_node_set_enabled(&scene_buffer->node, true);
        scaled->hidden = false;
        wlr_scene_buffer_set_filter_mode(scene_buffer, WLR_SCALE_FILTER_BILINEAR);
        if (width <= 0 || height <= 0) {
            pixman_region32_fini(&opaque);
            return false;
        }
        wlr_region_scale(&opaque, &surface->opaque_region, (float)map.scale_milli / 1000.0f * 1.0f);
        pixman_region32_intersect_rect(&opaque, &opaque, 0, 0, width, height);
    }
    wlr_scene_buffer_set_opaque_region(scene_buffer, &opaque);
    pixman_region32_fini(&opaque);
    wlr_scene_buffer_set_source_box(scene_buffer, &src_box);
    wlr_scene_buffer_set_dest_size(scene_buffer, width, height);
    wlr_scene_buffer_set_transform(scene_buffer, state->transform);
    return true;
}

/* What the scene does on a surface commit (wlroots 0.17 types/scene/surface.c, surface_reconfigure
 * and handle_scene_surface_surface_commit), with the destination and the opaque region at the side's
 * scale. The scene's own handler sets the destination to the surface's logical size on every commit,
 * and the compositor used to set the region size again after it: two full-node updates per commit,
 * each damaging the whole side on the output. Its opaque region was the surface's, unscaled, which the
 * scene intersects with the node size: a full-screen Desktop side at scale 2 counted as opaque over
 * its top-left quarter only, and the overlay background under it stayed visible. Here an unchanged
 * size is a no-op, only the client's damage reaches the output, and the opaque region covers what the
 * client declared, scaled. The size is the surface's own (its viewport destination, or its buffer's size
 * over the buffer scale), and it is the edges of the surface that are scaled, not its size and position
 * apart: two surfaces that meet in the toplevel's units meet in the scaled ones, at a fractional scale too.
 * What a surface's scene buffer shows (its buffer and damage) changes with its own commits only; where its edges are
 * (its size at the scale, its source and opaque region) also with its position, which is applied when its parent
 * commits, after its own commit when it is synchronized. So the geometry is set again when the parent commits, ahead
 * of which a surface that moves and resizes in one commit has the size its old position gives it. */
static void side_scaled_surface_reconfigure(struct side_scaled_surface *scaled) {
    struct wlr_scene_surface *scene_surface = scaled->scene_surface;
    struct wlr_scene_buffer *scene_buffer = scene_surface->buffer;
    struct wlr_surface *surface = scaled->surface;
    const struct side_ctx *ctx = scaled->window->ctx;
    if (!wlr_box_empty(&scene_surface->clip)) {
        /* A clipped surface (never a side's toplevel or its subsurfaces): the scene's handling, then the scaled size. */
        scene_surface->surface_commit.notify(&scene_surface->surface_commit, surface);
        scale_surface_buffer(scene_buffer, 0, 0, (void *)ctx);
        return;
    }
    if (!side_scaled_surface_set_geometry(scaled)) {
        wlr_scene_buffer_set_buffer(scene_buffer, NULL);
        return;
    }
    /* The scene lets the client buffer it shows be written in place by its client (wlroots'
     * client_buffer_mark_next_can_damage and scene_buffer_unmark_client_buffer): the buffer shown so
     * far loses that mark, the one committed now gets it. */
    if (scene_buffer->buffer) {
        struct wlr_client_buffer *shown = wlr_client_buffer_get(scene_buffer->buffer);
        if (shown && shown->n_ignore_locks > 0) shown->n_ignore_locks--;
    }
    if (surface->buffer) {
        surface->buffer->n_ignore_locks++;
        wlr_scene_buffer_set_buffer_with_damage(scene_buffer, &surface->buffer->base, &surface->buffer_damage);
    } else {
        wlr_scene_buffer_set_buffer(scene_buffer, NULL);
    }
    /* A frame callback waits for the primary output's next frame, if the node is shown. */
    int lx, ly;
    bool enabled = wlr_scene_node_coords(&scene_buffer->node, &lx, &ly);
    if (!wl_list_empty(&surface->current.frame_callback_list) && scene_buffer->primary_output && enabled) {
        wlr_output_schedule_frame(scene_buffer->primary_output->output);
    }
}

/* The current positions of the surface's subsurfaces, which wlroots' scene lays their trees out at when the surface
 * commits: at the side's scale (relative to the surface's, from the positions the client set: they are applied to the
 * current state when the surface commits, ahead of the commit signal) or put back to what the client set. Only for the
 * length of a commit, between side_scaled_surface_pre_commit and side_scaled_surface_commit: whatever else reads the
 * current state at that time (the xdg surface's geometry, from the extents of its tree) sees the positions of the
 * picture there is, and the rest of the time they are the client's. */
static void side_scaled_surface_set_positions(struct side_scaled_surface *scaled, bool at_scale) {
    struct wlr_surface *surface = scaled->surface;
    struct side_edge_map map;
    side_window_edge_map(scaled->window, &map);
    int origin_x, origin_y;
    side_surface_origin(surface, &origin_x, &origin_y);
    struct wl_list *stacks[] = {&surface->current.subsurfaces_below, &surface->current.subsurfaces_above};
    for (size_t i = 0; i < 2; i++) {
        struct wlr_subsurface *subsurface;
        wl_list_for_each(subsurface, stacks[i], current.link) {
            if (at_scale) {
                subsurface->current.x = side_map_edge_x(&map, origin_x + subsurface->pending.x) -
                    side_map_edge_x(&map, origin_x);
                subsurface->current.y = side_map_edge_y(&map, origin_y + subsurface->pending.y) -
                    side_map_edge_y(&map, origin_y);
            } else {
                subsurface->current.x = subsurface->pending.x;
                subsurface->current.y = subsurface->pending.y;
            }
        }
    }
    scaled->positions_at_scale = at_scale;
}

static void side_scaled_surface_pre_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_scaled_surface *scaled = wl_container_of(listener, scaled, pre_commit);
    side_scaled_surface_set_positions(scaled, true);
}

static void side_scaled_surface_apply(struct side_scaled_surface *scaled);

/* Puts the scene trees of the surface's subsurfaces at their current positions at the scale, relative to the
 * surface's. After a commit the scene's subsurface tree has laid them out there already (and this moves nothing); it
 * is the way to the scaled positions when the scale begins or changes, and a subsurface whose scene tree this has not
 * taken over yet is taken over on the way. A subsurface's geometry is set again here, with its position applied now
 * (see side_scaled_surface_reconfigure). With `deep` every subsurface below this one is applied again as well. */
static void side_scaled_surface_place_subsurfaces(struct side_scaled_surface *scaled, bool deep) {
    struct wlr_surface *surface = scaled->surface;
    struct side_edge_map map;
    side_window_edge_map(scaled->window, &map);
    int origin_x, origin_y;
    side_surface_origin(surface, &origin_x, &origin_y);
    struct wl_list *stacks[] = {&surface->current.subsurfaces_below, &surface->current.subsurfaces_above};
    for (size_t i = 0; i < 2; i++) {
        struct wlr_subsurface *subsurface;
        wl_list_for_each(subsurface, stacks[i], current.link) {
            bool fresh = side_scaled_surface_find(scaled->window, subsurface->surface) == NULL;
            struct side_scaled_surface *child = side_scaled_surface_arm(scaled->window, subsurface->surface);
            if (!child) continue;
            struct wlr_scene_tree *tree = child->scene_surface->buffer->node.parent;
            int x = origin_x + subsurface->current.x, y = origin_y + subsurface->current.y;
            wlr_scene_node_set_position(&tree->node,
                side_map_edge_x(&map, x) - side_map_edge_x(&map, origin_x),
                side_map_edge_y(&map, y) - side_map_edge_y(&map, origin_y));
            /* A subsurface taken over now may have subsurfaces of its own that came with it: the scene makes the trees of
             * a surface and of the subsurfaces it has already together, and they are taken over with it. */
            if (deep || fresh) side_scaled_surface_apply(child);
            else side_scaled_surface_set_geometry(child);
        }
    }
}

/* The surface and everything below it, at the side's scale now. */
static void side_scaled_surface_apply(struct side_scaled_surface *scaled) {
    side_scaled_surface_reconfigure(scaled);
    side_scaled_surface_place_subsurfaces(scaled, true);
}

static void side_scaled_surface_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_scaled_surface *scaled = wl_container_of(listener, scaled, surface_commit);
    if (scaled->positions_at_scale) side_scaled_surface_set_positions(scaled, false);
    side_scaled_surface_reconfigure(scaled);
    side_scaled_surface_place_subsurfaces(scaled, false);
}

/* Ends this surface's handling: its commit listener leaves the signal and the scene surface's goes back on it. With
 * resync the scene's handler runs once, so the buffer shows at its logical size again, and a subsurface's scene tree
 * goes back to its logical position (what the scene's subsurface tree would do on the parent's next commit). Without
 * it the surface, or its scene tree, is going: it is also called from the buffer node's destroy signal, where the
 * scene surface is still whole and takes its listener down itself right after. */
static void side_scaled_surface_release(struct side_scaled_surface *scaled, bool resync) {
    struct wlr_scene_surface *scene_surface = scaled->scene_surface;
    struct wlr_surface *surface = scaled->surface;
    if (scaled->positions_at_scale) side_scaled_surface_set_positions(scaled, false);
    if (scaled->hidden) wlr_scene_node_set_enabled(&scene_surface->buffer->node, true);
    wlr_scene_buffer_set_filter_mode(scene_surface->buffer, WLR_SCALE_FILTER_BILINEAR);
    /* In the place of this listener on the signal, so the order of the surface's listeners is what it was. */
    wl_list_insert(scaled->surface_commit.link.prev, &scene_surface->surface_commit.link);
    wl_list_remove(&scaled->surface_commit.link);
    wl_list_remove(&scaled->pre_commit.link);
    wl_list_remove(&scaled->buffer_destroy.link);
    wl_list_remove(&scaled->link);
    free(scaled);
    if (!resync) return;
    scene_surface->surface_commit.notify(&scene_surface->surface_commit, surface);
    struct wlr_subsurface *subsurface = wlr_subsurface_try_from_wlr_surface(surface);
    struct wlr_scene_tree *tree = scene_surface->buffer->node.parent;
    if (subsurface && tree) wlr_scene_node_set_position(&tree->node, subsurface->current.x, subsurface->current.y);
}

static void side_scaled_surface_buffer_destroyed(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_scaled_surface *scaled = wl_container_of(listener, scaled, buffer_destroy);
    side_scaled_surface_release(scaled, false);
}

/* Take the surface's commits over from the scene: the scene surface's commit listener leaves the surface's commit
 * signal (the listener is the scene surface's "private state" in wlr_scene.h, laid out the same in 0.17.1 and
 * 0.17.4; its notify stays, for the clipped case and for putting it back) and side_scaled_surface_commit joins it,
 * with side_scaled_surface_pre_commit ahead of everything on the signal. Idempotent, and the surface is drawn at the
 * scale at once. NULL when the scene has no tree for the surface (yet). */
static struct side_scaled_surface *side_scaled_surface_arm(struct side_xdg_window *window, struct wlr_surface *surface) {
    struct side_scaled_surface *scaled = side_scaled_surface_find(window, surface);
    if (scaled) return scaled;
    if (!window->scene_tree || !surface) return NULL;
    struct wlr_scene_buffer *buffer = find_surface_buffer(&window->scene_tree->node, surface);
    struct wlr_scene_surface *scene_surface = buffer ? wlr_scene_surface_try_from_buffer(buffer) : NULL;
    if (!scene_surface || scene_surface->surface != surface) return NULL;
    scaled = calloc(1, sizeof(*scaled));
    if (!scaled) return NULL;
    scaled->window = window;
    scaled->surface = surface;
    scaled->scene_surface = scene_surface;
    wl_list_insert(window->scale_surfaces.prev, &scaled->link);
    wl_list_remove(&scene_surface->surface_commit.link);
    wl_list_init(&scene_surface->surface_commit.link);
    scaled->surface_commit.notify = side_scaled_surface_commit;
    wl_signal_add(&surface->events.commit, &scaled->surface_commit);
    /* Ahead of every listener of the signal, the scene's subsurface tree among them: wl_signal_add appends. */
    scaled->pre_commit.notify = side_scaled_surface_pre_commit;
    wl_list_insert(&surface->events.commit.listener_list, &scaled->pre_commit.link);
    scaled->buffer_destroy.notify = side_scaled_surface_buffer_destroyed;
    wl_signal_add(&buffer->node.events.destroy, &scaled->buffer_destroy);
    if (surface == window->surface) {
        wlr_log(WLR_INFO, "Scaled scene commits for the %s side's surface at %d/1000",
            window->ctx->id == SIDE_LEFT ? "left" : "right", side_scale_milli(window->ctx));
    } else {
        wlr_log(WLR_DEBUG, "Scaled scene commits for a subsurface of the %s side's surface at %d/1000",
            window->ctx->id == SIDE_LEFT ? "left" : "right", side_scale_milli(window->ctx));
    }
    side_scaled_surface_reconfigure(scaled);
    return scaled;
}

/* Hand the commits of every surface of the toplevel's tree back to the scene; with resync, its handlers run once so
 * the tree shows at its logical size and positions again. */
static void side_restore_scene_commit(struct side_xdg_window *window, bool resync) {
    if (!window || wl_list_empty(&window->scale_surfaces)) return;
    while (!wl_list_empty(&window->scale_surfaces)) {
        struct side_scaled_surface *scaled = wl_container_of(window->scale_surfaces.next, scaled, link);
        side_scaled_surface_release(scaled, resync);
    }
    wlr_log(WLR_INFO, "Scene commits for the %s side's surface are the scene's again",
        window->ctx->id == SIDE_LEFT ? "left" : "right");
}

/* The side's scale, applied to its toplevel's scene tree now and to its commits from now on: a scale
 * other than 1 handles them here, scale 1 leaves them to the scene (Game mode never leaves it). */
static void side_apply_window_scene_scale(struct side_xdg_window *window) {
    if (!window || !window->scene_tree || !window->ctx) return;
    struct side_edge_map map;
    side_window_edge_map(window, &map);
    if (map.scale_milli == 1000 && !map.zoom) {
        side_restore_scene_commit(window, true);
        return;
    }
    struct side_scaled_surface *root = side_scaled_surface_arm(window, window->surface);
    if (root) {
        side_scaled_surface_apply(root);
    } else {
        wlr_scene_node_for_each_buffer(&window->scene_tree->node, scale_surface_buffer, window->ctx);
    }
}

/* For the Zoom log: every scene buffer under the window's tree, as it is drawn now. */
static void zoom_describe_buffer(struct wlr_scene_buffer *buffer, int sx, int sy, void *data) {
    char *tree = data;
    size_t used = strlen(tree);
    if (used + 120 >= 768) return;
    struct wlr_scene_surface *scene_surface = wlr_scene_surface_try_from_buffer(buffer);
    snprintf(tree + used, 768 - used, " %s[%s src %.0fx%.0f@%.0f,%.0f dest %dx%d @%d,%d %s%s]",
        scene_surface && wlr_subsurface_try_from_wlr_surface(scene_surface->surface) ? "sub" : "top",
        scene_surface ? "surface" : "buffer", buffer->src_box.width, buffer->src_box.height,
        buffer->src_box.x, buffer->src_box.y, buffer->dst_width, buffer->dst_height, sx, sy,
        buffer->filter_mode == WLR_SCALE_FILTER_NEAREST ? "nearest" : "bilinear", buffer->node.enabled ? "" : " hidden");
}

/* Zoom: engages the transforming commit handling above for a Gamescope toplevel, with the affine map instead of a
 * scale, and puts the tree back when Zoom ends. With `force` the geometry is applied again (the mode, the monitor or
 * the region changed); without it only a change of state does anything, so a commit costs nothing here. */
static void side_window_apply_zoom(struct side_xdg_window *window, bool force) {
    if (!window || !window->ctx || !window->surface || !window->scene_tree) return;
    struct side_edge_map map;
    side_window_edge_map(window, &map);
    if (map.zoom == window->zoom_applied && !(force && map.zoom)) return;
    side_apply_window_scene_scale(window);
    bool engaged = map.zoom && !wl_list_empty(&window->scale_surfaces);
    bool first = engaged && !window->zoom_applied;
    window->zoom_applied = engaged;
    if (!first) return;
    char tree[768] = "";
    wlr_scene_node_for_each_buffer(&window->scene_tree->node, zoom_describe_buffer, tree);
    wlr_log(WLR_INFO, "Zoom on the %s side: toplevel %dx%d, zoom box %.0fx%.0f at %.0f,%.0f (k %.3f), scene buffers:%s",
        side_name(window->ctx->id), window->surface->current.width, window->surface->current.height,
        map.z.w, map.z.h, map.z.x, map.z.y, map.z.k, tree);
}

/* The side's presentation changed: apply it to the side's windows now. */
static void side_zoom_refresh(struct side_ctx *ctx) {
    if (!ctx || !ctx->xdg_windows_initialized) return;
    struct side_xdg_window *window;
    wl_list_for_each(window, &ctx->xdg_windows, link) {
        side_window_apply_zoom(window, true);
    }
    if (ctx->comp) request_redraw(ctx->comp);
}

static void side_set_desktop_scale(struct side_ctx *ctx, int scale_milli) {
    if (!ctx || !ctx->comp) return;
    if (scale_milli < 1000 || scale_milli > 3000) scale_milli = 1000;
    if (side_scale_milli(ctx) == scale_milli) return;
    wlr_log(WLR_INFO, "%s side desktop scale %d/1000 -> %d/1000",
        ctx->id == SIDE_LEFT ? "Left" : "Right", side_scale_milli(ctx), scale_milli);
    ctx->desktop_scale_milli = scale_milli;
    update_side_logical_dimensions(ctx, ctx->scale_mode);
    configure_side_windows(ctx->comp, ctx);
}

static void configure_side_windows(struct compositor *comp, struct side_ctx *ctx) {
    if (!comp || !ctx) {
        return;
    }

    int tw = side_toplevel_width(ctx);
    int th = side_toplevel_height(ctx);

    side_init_xdg_windows(ctx);
    struct side_xdg_window *xdg_window = NULL;
    wl_list_for_each(xdg_window, &ctx->xdg_windows, link) {
        if (xdg_window->scene_tree) {
            wlr_scene_node_set_position(&xdg_window->scene_tree->node, 0, 0);
        }
        if (xdg_window->xdg_surface &&
                xdg_window->xdg_surface->role == WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
            side_send_window_scale(xdg_window);
            side_apply_window_scene_scale(xdg_window);
            wlr_xdg_toplevel_set_size(xdg_window->xdg_surface->toplevel, tw, th);
            wlr_xdg_surface_schedule_configure(xdg_window->xdg_surface);
        }
    }

    side_init_xwayland_windows(ctx);
    struct side_xwayland_window *xwindow = NULL;
    wl_list_for_each(xwindow, &ctx->xwayland_windows, link) {
        if (xwindow->scene_tree) {
            wlr_scene_node_set_position(&xwindow->scene_tree->node, 0, 0);
        }
        if (xwindow->xwayland_handle && !xwindow->xwayland_handle->override_redirect) {
            configure_side_xwayland_surface(comp, ctx, xwindow->xwayland_handle);
        }
    }
}

/* state.json contract version. Version 2 never restarts a side for a display
 * change: a layout, position, scaler or Apply change only moves the values
 * below. A state.json without this field is version 1, where the supervisor
 * restarted a side whenever its size, scaler or display_revision changed. */
#define RUNTIME_STATE_CONTRACT_VERSION 2

/* display_revision counts changes of a side's live display contract: it
 * advances when the published game monitor size or the applied scaler differs
 * from what the previous state.json carried, whatever caused it (a layout,
 * position or output change under Native, Apply, an applied scaler change). A
 * pending choice, Cancel, or a change that leaves both values equal does not
 * advance it. Comparing the published values keeps every path honest, and a
 * state.json reader never sees new values under an old revision. */
static void side_note_display_contract(struct side_ctx *ctx) {
    if (!ctx || ctx->width <= 0 || ctx->height <= 0) {
        return; /* no region yet; the first real one is the baseline */
    }
    int width, height;
    side_effective_game_size(ctx, &width, &height);
    if (ctx->display_published &&
            (width != ctx->published_game_width || height != ctx->published_game_height ||
             side_presented_scale_mode(ctx) != ctx->published_scale_mode)) {
        ctx->display_revision++;
    }
    ctx->display_published = true;
    ctx->published_game_width = width;
    ctx->published_game_height = height;
    ctx->published_scale_mode = side_presented_scale_mode(ctx);
}

static void write_runtime_state(struct compositor *comp) {
    if (!comp) {
        return;
    }

    side_note_display_contract(&comp->left);
    side_note_display_contract(&comp->right);
    int left_game_width, left_game_height, right_game_width, right_game_height;
    side_effective_game_size(&comp->left, &left_game_width, &left_game_height);
    side_effective_game_size(&comp->right, &right_game_width, &right_game_height);
    /* The monitor in effect, from the supervisor's last report: its size (0 until reported), whether the
     * picture is scaled, and the report's result ("applied", "failed", "deferred", or "" before any). */
    struct side_monitor left_monitor = side_monitor_view(&comp->left);
    struct side_monitor right_monitor = side_monitor_view(&comp->right);

    const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
    if (!runtime_dir || !runtime_dir[0]) {
        runtime_dir = "/run/dosplex";
    }

    char path[256], temp_path[256];
    snprintf(path, sizeof(path), "%s/state.json", runtime_dir);
    if (snprintf(temp_path, sizeof(temp_path), "%s/.state-XXXXXX", runtime_dir)
            >= (int)sizeof(temp_path)) return;
    int state_fd = mkstemp(temp_path);
    if (state_fd < 0) return;
    FILE *fp = fdopen(state_fd, "w");
    if (!fp) {
        close(state_fd);
        unlink(temp_path);
        return;
    }

    fprintf(fp,
        "{\n"
        "  \"contract_version\": %d,\n"
        "  \"output_connected\": %s,\n"
        "  \"capture_ready\": %u,\n"
        "  \"frame_pacing\": \"%s\",\n"
        "  \"audio_busy\": %s, \"audio_revision\": %u,\n"
        "  \"layout\": \"%s\",\n"
        "  \"primary_side\": \"%s\",\n"
        "  \"guest_side\": \"%s\",\n"
        "  \"guest_physical_side\": \"%s\",\n"
        "  \"scale_mode\": \"%s\",\n"
        "  \"input_leap_route\": \"%s\",\n"
        "  \"overlay_visible\": %s,\n"
        "  \"overlay_owner\": \"%s\",\n"
        "  \"overlay_tab\": %d, \"overlay_confirmation\": %s, \"overlay_confirmation_action\": %d,\n"
        "  \"audio_page\": %d, \"audio_kind\": %d, \"device_page\": %d,\n"
        "  \"power_action\": \"%s\", \"power_message\": \"%s\",\n"
        "  \"panel\": \"%s\", \"panel_shown\": %s, \"panel_feedback\": %s,\n"
        "  \"brightness\": %d, \"brightness_controls\": %s,\n"
        "  \"perf\": {\"left\": {\"fps_counter\": \"%s\", \"recording\": %s, \"csv\": \"%s\"}, \"right\": {\"fps_counter\": \"%s\", \"recording\": %s, \"csv\": \"%s\"}},\n"
        "  \"left\": {\"role\": \"%s\", \"x\": %d, \"y\": %d, \"width\": %d, \"height\": %d, \"game_width\": %d, \"game_height\": %d, \"pending_game_width\": %d, \"pending_game_height\": %d, \"game_aspect\": \"%s\", \"pending_game_aspect\": \"%s\", \"native_resolution\": %s, \"pending_native_resolution\": %s, \"display_revision\": %u, \"scale_mode\": \"%s\", \"pending_scale_mode\": \"%s\", \"display_pending\": %s, \"has_app\": %s, \"activity\": \"%s\", \"monitor_width\": %d, \"monitor_height\": %d, \"display_scaled\": %s, \"display_result\": \"%s\"},\n"
        "  \"right\": {\"role\": \"%s\", \"x\": %d, \"y\": %d, \"width\": %d, \"height\": %d, \"game_width\": %d, \"game_height\": %d, \"pending_game_width\": %d, \"pending_game_height\": %d, \"game_aspect\": \"%s\", \"pending_game_aspect\": \"%s\", \"native_resolution\": %s, \"pending_native_resolution\": %s, \"display_revision\": %u, \"scale_mode\": \"%s\", \"pending_scale_mode\": \"%s\", \"display_pending\": %s, \"has_app\": %s, \"activity\": \"%s\", \"monitor_width\": %d, \"monitor_height\": %d, \"display_scaled\": %s, \"display_result\": \"%s\"}\n"
        "}\n",
        RUNTIME_STATE_CONTRACT_VERSION,
        comp->output ? "true" : "false",
        comp->capture_ready,
        frame_pacing_name(comp->frame_pacing),
        comp->audio_pid > 0 ? "true" : "false", comp->audio_revision,
        layout_mode_name(comp->layout),
        side_name(comp->primary_side_id),
        side_name(comp->guest_side_id),
        side_name(comp->guest_physical_side),
        output_scale_mode_name(comp->output_scale_mode),
        input_route_mode_name(comp->remote_input_route),
        comp->management_overlay_visible ? "true" : "false",
        side_name(comp->management_overlay_owner),
        comp->overlay_tab,
        comp->pending_shared_action != MODAL_ACTION_NONE ? "true" : "false",
        (int)comp->pending_shared_action,
        comp->audio_page, comp->audio_kind, *owner_device_page(comp),
        comp->power_action, comp->power_message,
        comp->panel_pid > 0 && !comp->panel_closing ? comp->panel_mode : "",
        comp->panel_surface && !comp->panel_closing ? "true" : "false",
        comp->feedback_available ? "true" : "false",
        comp->screen_bl_available ? comp->screen_bl_percent : -1,
        comp->screen_bl_shown ? "true" : "false",
        fps_mode_name(comp->left.perf.mode), comp->left.perf.recording ? "true" : "false",
        comp->left.perf.recording && strrchr(comp->left.perf.csv_path, '/') ? strrchr(comp->left.perf.csv_path, '/') + 1 : "",
        fps_mode_name(comp->right.perf.mode), comp->right.perf.recording ? "true" : "false",
        comp->right.perf.recording && strrchr(comp->right.perf.csv_path, '/') ? strrchr(comp->right.perf.csv_path, '/') + 1 : "",
        side_role_name(comp->left.role),
        comp->left.x, comp->left.y, comp->left.width, comp->left.height,
        left_game_width, left_game_height,
        comp->left.pending_game_width > 0 ? comp->left.pending_game_width : comp->left.width,
        comp->left.pending_game_height > 0 ? comp->left.pending_game_height : comp->left.height,
        game_resolution_choice_name(comp->left.game_resolution_choice),
        game_resolution_choice_name(comp->left.pending_game_resolution_choice),
        comp->left.game_width == 0 ? "true" : "false",
        comp->left.pending_game_width == 0 ? "true" : "false",
        comp->left.display_revision,
        output_scale_mode_name(side_presented_scale_mode(&comp->left)),
        output_scale_mode_name(comp->left.pending_scale_mode),
        comp->left.display_pending ? "true" : "false",
        comp->left.app_surface ? "true" : "false",
        comp->left.activity == 2 ? "off" : comp->left.activity == 1 ? "standby" : "active",
        left_monitor.width, left_monitor.height, left_monitor.scaled ? "true" : "false",
        display_result_name(left_monitor.result),
        side_role_name(comp->right.role),
        comp->right.x, comp->right.y, comp->right.width, comp->right.height,
        right_game_width, right_game_height,
        comp->right.pending_game_width > 0 ? comp->right.pending_game_width : comp->right.width,
        comp->right.pending_game_height > 0 ? comp->right.pending_game_height : comp->right.height,
        game_resolution_choice_name(comp->right.game_resolution_choice),
        game_resolution_choice_name(comp->right.pending_game_resolution_choice),
        comp->right.game_width == 0 ? "true" : "false",
        comp->right.pending_game_width == 0 ? "true" : "false",
        comp->right.display_revision,
        output_scale_mode_name(side_presented_scale_mode(&comp->right)),
        output_scale_mode_name(comp->right.pending_scale_mode),
        comp->right.display_pending ? "true" : "false",
        comp->right.app_surface ? "true" : "false",
        comp->right.activity == 2 ? "off" : comp->right.activity == 1 ? "standby" : "active",
        right_monitor.width, right_monitor.height, right_monitor.scaled ? "true" : "false",
        display_result_name(right_monitor.result));
    bool failed = ferror(fp) != 0;
    if (fchmod(state_fd, 0644) != 0) failed = true;
    if (fclose(fp) != 0) failed = true;
    if (failed || rename(temp_path, path) != 0) unlink(temp_path);
}

static void apply_layout(struct compositor *comp) {
    if (!comp || comp->output_width <= 0 || comp->output_height <= 0) {
        return;
    }

    /* A new arrangement (the layout, or which side the guest sits on) moves the regions around, so
     * the pointer locks end before they move: a lock's hint is read against the geometry it was
     * set for. A resize of the same arrangement keeps the pointer where it is. */
    bool rearranged = !comp->arrangement_known || comp->known_layout != comp->layout ||
        comp->known_primary_side != comp->primary_side_id ||
        comp->known_guest_side != comp->guest_physical_side;
    if (rearranged) pointer_constraints_release_all(comp);

    struct side_ctx *primary = primary_side(comp);
    struct side_ctx *guest = guest_side(comp);


    if (comp->layout == LAYOUT_SINGLE) {
        /* Preserve saved device assignments while the guest is hidden.
         * Input target resolution supplies the temporary full-screen target. */
        /* Layout controls presentation/activity, not authentication. The guest
         * supervisor waits in single mode, including a login finishing after
         * this change. Explicit Sign Out owns credential/access cleanup. */
    }
    if (comp->layout == LAYOUT_SPLIT_VERTICAL) {
        int half_w = comp->output_width / 2;
        struct side_ctx *left_slot = comp->guest_physical_side == SIDE_LEFT ? guest : primary;
        struct side_ctx *right_slot = comp->guest_physical_side == SIDE_LEFT ? primary : guest;
        if (left_slot) {
            left_slot->x = 0;
            left_slot->y = 0;
            left_slot->width = half_w;
            left_slot->height = comp->output_height;
        }
        if (right_slot) {
            right_slot->x = half_w;
            right_slot->y = 0;
            right_slot->width = comp->output_width - half_w;
            right_slot->height = comp->output_height;
        }
    } else if (comp->layout == LAYOUT_SPLIT_HORIZONTAL) {
        int half_h = comp->output_height / 2;
        struct side_ctx *top_slot = comp->guest_physical_side == SIDE_LEFT ? guest : primary;
        struct side_ctx *bottom_slot = comp->guest_physical_side == SIDE_LEFT ? primary : guest;
        if (top_slot) {
            top_slot->x = 0;
            top_slot->y = 0;
            top_slot->width = comp->output_width;
            top_slot->height = half_h;
        }
        if (bottom_slot) {
            bottom_slot->x = 0;
            bottom_slot->y = half_h;
            bottom_slot->width = comp->output_width;
            bottom_slot->height = comp->output_height - half_h;
        }
    } else if (primary && guest) {
        primary->x = 0;
        primary->y = 0;
        primary->width = comp->output_width;
        primary->height = comp->output_height;
        guest->x = 0;
        guest->y = 0;
        guest->width = comp->output_width;
        guest->height = comp->output_height;
    }

    refresh_side_game_resolution(&comp->left);
    refresh_side_game_resolution(&comp->right);

    init_side_logical_output(&comp->left, "dosplex-left", "Dosplex left logical output",
        comp->left.x, comp->left.y, comp->left.width, comp->left.height);
    init_side_logical_output(&comp->right, "dosplex-right", "Dosplex right logical output",
        comp->right.x, comp->right.y, comp->right.width, comp->right.height);

    /* Reapply scale_mode after layout update may have changed viewport dimensions */
    update_side_logical_dimensions(&comp->left, comp->left.scale_mode);
    update_side_logical_dimensions(&comp->right, comp->right.scale_mode);

    configure_side_windows(comp, &comp->left);
    configure_side_windows(comp, &comp->right);
    side_zoom_refresh(&comp->left);
    side_zoom_refresh(&comp->right);

    if (comp->left.side_tree) {
        wlr_scene_node_set_position(&comp->left.side_tree->node, comp->left.x, comp->left.y);
        wlr_scene_node_set_enabled(&comp->left.side_tree->node,
            side_is_shown(comp, &comp->left));
    }
    if (comp->right.side_tree) {
        wlr_scene_node_set_position(&comp->right.side_tree->node, comp->right.x, comp->right.y);
        wlr_scene_node_set_enabled(&comp->right.side_tree->node,
            side_is_shown(comp, &comp->right));
    }
    if (comp->modal_tree) {
        wlr_scene_node_raise_to_top(&comp->modal_tree->node);
    }
    if (rearranged) {
        bool first = !comp->arrangement_known;
        comp->arrangement_known = true;
        comp->known_layout = comp->layout;
        comp->known_primary_side = comp->primary_side_id;
        comp->known_guest_side = comp->guest_physical_side;
        if (!first) pointer_rehome_after_layout(comp);
    }
    struct side_ctx *cursor_sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        struct side_ctx *ctx = cursor_sides[i];
        if (ctx->pointer_x >= ctx->width) ctx->pointer_x = ctx->width > 0 ? ctx->width - 1 : 0;
        if (ctx->pointer_y >= ctx->height) ctx->pointer_y = ctx->height > 0 ? ctx->height - 1 : 0;
        position_side_cursor(comp, ctx, ctx->x + ctx->pointer_x, ctx->y + ctx->pointer_y);
    }
    if (comp->cursor_tree) {
        wlr_scene_node_raise_to_top(&comp->cursor_tree->node);
    }

    wlr_log(WLR_INFO, "Applied layout=%s primary=%s left=%dx%d+%d+%d right=%dx%d+%d+%d",
        layout_mode_name(comp->layout),
        side_name(comp->primary_side_id),
        comp->left.width, comp->left.height, comp->left.x, comp->left.y,
        comp->right.width, comp->right.height, comp->right.x, comp->right.y);
    write_runtime_state(comp);
    request_redraw(comp);
}

static void handle_app_unmap(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_ctx *ctx = wl_container_of(listener, ctx, surface_unmap);
    wlr_log(WLR_INFO, "App surface unmapped on %s side",
            ctx->id == SIDE_LEFT ? "left" : "right");
    side_clear_app(ctx);
    request_redraw(ctx->comp);
}

static void handle_app_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_ctx *ctx = wl_container_of(listener, ctx, surface_destroy);
    wlr_log(WLR_INFO, "App surface destroyed on %s side",
            ctx->id == SIDE_LEFT ? "left" : "right");
    side_clear_app(ctx);
    request_redraw(ctx->comp);
}

static void handle_app_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_ctx *ctx = wl_container_of(listener, ctx, surface_commit);
    if (ctx->app_surface) {
        wlr_log(WLR_INFO, "App surface commit on %s side: %dx%d mapped=%d",
            ctx->id == SIDE_LEFT ? "left" : "right",
            ctx->app_surface->current.width,
            ctx->app_surface->current.height,
            ctx->app_surface->mapped);
    }
}

static void side_remove_xwayland_window(struct side_xwayland_window *window) {
    if (!window) {
        return;
    }

    struct side_ctx *ctx = window->ctx;

    if (window->surface_commit_armed) {
        wl_list_remove(&window->surface_commit.link);
        window->surface_commit_armed = false;
    }
    if (window->surface_unmap_armed) {
        wl_list_remove(&window->surface_unmap.link);
        window->surface_unmap_armed = false;
    }
    if (window->surface_destroy_armed) {
        wl_list_remove(&window->surface_destroy.link);
        window->surface_destroy_armed = false;
    }
    if (window->request_configure_armed) {
        wl_list_remove(&window->request_configure.link);
        window->request_configure_armed = false;
    }

    if (!wl_list_empty(&window->link)) {
        wl_list_remove(&window->link);
        wl_list_init(&window->link);
    }

    if (ctx) {
        if (ctx->app_xwayland_handle == window->xwayland_handle) {
            ctx->app_xwayland_handle = NULL;
            ctx->app_xwayland_surface = NULL;
        }
        if (ctx->app_surface == window->surface) {
            ctx->app_surface = NULL;
            ctx->app_scene_tree = NULL;
        }
    }

    if (window->scene_tree) {
        wlr_scene_node_destroy(&window->scene_tree->node);
        window->scene_tree = NULL;
    }

    free(window);

    if (ctx && ctx->comp) {
        side_refresh_active_surface(ctx);
        request_redraw(ctx->comp);
    }
}

static void side_activate_xwayland_window(struct side_ctx *ctx,
        struct side_xwayland_window *window, bool raise) {
    if (!ctx || !window || !window->xwayland_handle) {
        return;
    }
    side_init_xwayland_windows(ctx);

    if (ctx->app_xwayland_handle == window->xwayland_handle && !raise) {
        return;
    }

    /* Update context tracking for primary surface */
    ctx->app_surface = window->surface;
    ctx->app_xwayland_handle = window->xwayland_handle;
    ctx->app_xwayland_surface = window->scene_surface;
    ctx->app_scene_tree = window->scene_tree;
    ctx->app_scene_tree_owned = false;
    if (ctx->comp) {
        write_runtime_state(ctx->comp);
    }

    if (raise && window->scene_tree) {
        wlr_scene_node_raise_to_top(&window->scene_tree->node);
    }

    /* Deactivate other windows on this side */
    struct side_xdg_window *xdg_win;
    wl_list_for_each(xdg_win, &ctx->xdg_windows, link) {
        if (xdg_win->xdg_surface && xdg_win->xdg_surface->role == WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
            wlr_xdg_toplevel_set_activated(xdg_win->xdg_surface->toplevel, false);
        }
    }

    wlr_xwayland_surface_activate(window->xwayland_handle, true);
    wlr_log(WLR_INFO, "Activated XWayland window on %s side",
        ctx->id == SIDE_LEFT ? "left" : "right");
}

static void handle_xwayland_window_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xwayland_window *window = wl_container_of(listener, window, surface_commit);
    if (!window->ctx || !window->surface) {
        return;
    }
    window->ctx->commit_count++;
    if (window->surface->buffer && (window->surface->current.committed & WLR_SURFACE_STATE_BUFFER))
        perf_note_commit(window->ctx);

    if (window->surface->mapped) {
        bool first_map = window->ctx->app_xwayland_handle != window->xwayland_handle;
        bool state_changed = window->ctx->app_surface != window->surface;
        side_activate_xwayland_window(window->ctx, window, first_map);

        if ((first_map || state_changed) && window->ctx->comp) {
            request_redraw(window->ctx->comp);
        }
    }
}

static void handle_xwayland_window_unmap(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xwayland_window *window = wl_container_of(listener, window, surface_unmap);
    wlr_log(WLR_INFO, "XWayland surface unmapped on %s side",
        window->ctx->id == SIDE_LEFT ? "left" : "right");

    if (window->scene_tree) {
        wlr_scene_node_set_enabled(&window->scene_tree->node, false);
    }

    if (window->ctx) {
        side_refresh_active_surface(window->ctx);
        write_runtime_state(window->ctx->comp);
        request_redraw(window->ctx->comp);
    }
}

static void handle_xwayland_window_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xwayland_window *window = wl_container_of(listener, window, surface_destroy);
    wlr_log(WLR_INFO, "XWayland surface destroyed on %s side",
        window->ctx->id == SIDE_LEFT ? "left" : "right");
    side_remove_xwayland_window(window);
}

static void handle_xwayland_window_request_configure(struct wl_listener *listener, void *data) {
    struct side_xwayland_window *window = wl_container_of(listener, window, request_configure);
    struct wlr_xwayland_surface_configure_event *event = data;

    if (!window->xwayland_handle || !event || event->surface != window->xwayland_handle) {
        return;
    }

    if (window->xwayland_handle->override_redirect) {
        wlr_xwayland_surface_configure(window->xwayland_handle,
            event->x, event->y, event->width, event->height);
    } else {
        configure_side_xwayland_surface(window->ctx->comp, window->ctx, event->surface);
    }
}

static void handle_xdg_window_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, surface_commit);
    if (!window->surface) {
        return;
    }

    window->ctx->commit_count++;
    window->commits++;
    if (window->surface->buffer && (window->surface->current.committed & WLR_SURFACE_STATE_BUFFER))
        perf_note_commit(window->ctx);
    side_arm_scale_override(window);
    /* A scaled side whose commits the scene still handles (its buffer was found only now): this commit's
     * destination, and the commits from the next one on. */
    if (side_scale_milli(window->ctx) != 1000 && wl_list_empty(&window->scale_surfaces)) side_apply_window_scene_scale(window);
    side_window_apply_zoom(window, false);
    /* The scene schedules the frame that answers a frame callback for a surface it shows (its primary output); a toplevel
     * that its own subsurfaces cover has none (see "The toplevel's frame callbacks and presentation feedback"). */
    if (window->ctx->comp && window->ctx->comp->output && window->scale_buffer && !window->scale_buffer->primary_output &&
            !wl_list_empty(&window->surface->current.frame_callback_list) && side_window_covered_by_its_tree(window)) {
        wlr_output_schedule_frame(window->ctx->comp->output);
    }

    /* Gamescope 3.16.30 waits for a fresh configure whenever it attaches a
     * null backing buffer, including while already unmapped. wlroots only
     * sends one for the first initial commit. Answer subsequent explicit
     * null attachments too; plain configure acknowledgements must not loop. */
    if (!window->surface->mapped && !window->surface->buffer &&
            (window->surface->current.committed & WLR_SURFACE_STATE_BUFFER) &&
            window->xdg_surface && window->xdg_surface->initialized &&
            window->xdg_surface->toplevel) {
        side_send_window_scale(window);
        wlr_xdg_toplevel_set_size(window->xdg_surface->toplevel,
            side_toplevel_width(window->ctx), side_toplevel_height(window->ctx));
        wlr_xdg_toplevel_set_activated(window->xdg_surface->toplevel, true);
        wlr_xdg_surface_schedule_configure(window->xdg_surface);
    }

    if (dosplex_file_debug_enabled()) {
        FILE *df = fopen("/tmp/dosplex-debug.log", "a");
        if (df) {
            char ts[64]; time_t now; struct tm *tb;
            now = time(NULL); tb = localtime(&now); strftime(ts, sizeof(ts), "%H:%M:%S", tb);
            fprintf(df, "[%s] COMMIT %s surface=%dx%d buf=%dx%d\n",
                ts, window->ctx->id == SIDE_LEFT ? "left" : "right",
                window->surface->current.width, window->surface->current.height,
                window->surface->buffer ? window->surface->buffer->base.width : 0,
                window->surface->buffer ? window->surface->buffer->base.height : 0);
            fclose(df);
        }
    }

    wlr_log(WLR_DEBUG, "App surface commit #%d on %s side: %dx%d mapped=%d",
        window->ctx->commit_count,
        window->ctx->id == SIDE_LEFT ? "left" : "right",
        window->surface->current.width,
        window->surface->current.height,
        window->surface->mapped);

    /* Log buffer details to diagnose DMA-BUF import issues */
    if (window->surface->buffer) {
        struct wlr_buffer *buf = &window->surface->buffer->base;
        struct wlr_dmabuf_attributes dmabuf;
        bool is_dmabuf = wlr_buffer_get_dmabuf(buf, &dmabuf);
        wlr_log(WLR_DEBUG, "  buffer: %s %dx%d",
            is_dmabuf ? "DMA-BUF" : "shm/other",
            buf->width, buf->height);
    }

    if (window->surface->mapped) {
        bool first_map = window->ctx->active_xdg_window != window;
        bool state_changed = window->ctx->app_surface != window->surface;
        side_activate_xdg_window(window->ctx, window, first_map);
        if (state_changed && window->ctx->comp) {
            write_runtime_state(window->ctx->comp);
        }
        /* The scene's damage schedules the frame for the client's content; the overlay is rebuilt
         * only when the side's state changed (a window mapped or became the side's app surface). */
        if ((first_map || state_changed) && window->ctx->comp) {
            request_redraw(window->ctx->comp);
        }
        /* A mapped, remapped or newly active application takes its seat's keyboard focus. */
        if (first_map || state_changed) {
            side_ensure_app_keyboard_focus(window->ctx->comp, window->ctx);
        }
    }
}

static void handle_xdg_window_fullscreen(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, surface_fullscreen);
    struct side_ctx *ctx = window->ctx;

    if (!window->xdg_surface || !window->xdg_surface->toplevel) {
        return;
    }

    /* Constrain fullscreen to the side's logical viewport dimensions
     * instead of letting wlroots expand to the physical output size.
     * This ensures gamescope renders within the split-screen viewport. */
    int lw = side_logical_width(ctx);
    int lh = side_logical_height(ctx);

    wlr_log(WLR_INFO, "Fullscreen requested on %s side — constraining to %dx%d",
        ctx->id == SIDE_LEFT ? "left" : "right", lw, lh);

    wlr_xdg_toplevel_set_size(window->xdg_surface->toplevel,
        side_toplevel_width(ctx), side_toplevel_height(ctx));
    wlr_xdg_surface_schedule_configure(window->xdg_surface);

    if (dosplex_file_debug_enabled()) {
        FILE *df = fopen("/tmp/dosplex-debug.log", "a");
        if (df) {
            char ts[64]; time_t now; struct tm *tb;
            now = time(NULL); tb = localtime(&now); strftime(ts, sizeof(ts), "%H:%M:%S", tb);
            fprintf(df, "[%s] FULLSCREEN %s constrained to %dx%d\n",
                ts, ctx->id == SIDE_LEFT ? "left" : "right", lw, lh);
            fclose(df);
        }
    }
}

static const char *side_window_title(const struct side_xdg_window *window) {
    return window->xdg_surface && window->xdg_surface->toplevel && window->xdg_surface->toplevel->title ?
        window->xdg_surface->toplevel->title : "(none)";
}

static int side_window_age_ms(const struct side_xdg_window *window) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
    return (int)((now.tv_sec - window->assigned.tv_sec) * 1000 + (now.tv_nsec - window->assigned.tv_nsec) / 1000000);
}

/* === Map watchdog ===
 * A side's new toplevel normally maps within seconds: the nested Gamescope's
 * first frame came 6 s after the toplevel on the Deck and 14-15 s on the Steam
 * Machine. After the Machine's third Reset of 2026-10-20 the toplevel never
 * mapped in 26 minutes, and nothing was logged. So a toplevel that has not
 * mapped after MAP_WATCHDOG_MS (DOSPLEX_MAP_WATCHDOG_MS overrides it) is logged
 * with what is known about it and, when it is the side's app surface,
 * configured and activated again, once; one more check follows after the same
 * time and only logs. */
#define MAP_WATCHDOG_MS 20000

static int side_map_watchdog(void *data) {
    struct side_xdg_window *window = data;
    struct side_ctx *ctx = window->ctx;
    if (!window->map_watchdog) return 0;
    if (!window->surface || window->surface->mapped) {
        wl_event_source_remove(window->map_watchdog);
        window->map_watchdog = NULL;
        return 0;
    }
    window->map_watchdog_fired++;
    struct wlr_xdg_surface *xdg = window->xdg_surface;
    wlr_log(WLR_ERROR, "WARN: the %s side's toplevel '%s' has not mapped %d ms after it was assigned (check %d): "
        "initial commit %s, configure %s, scheduled serial %u, %d configure(s) not acked, last acked serial %u, "
        "%u commit(s), buffer %s, surface %dx%d, %s the side's app surface",
        side_name(ctx->id), side_window_title(window), side_window_age_ms(window), window->map_watchdog_fired,
        xdg && xdg->initialized ? "made" : "not made",
        xdg && xdg->configured ? "sent" : "not sent",
        xdg ? xdg->scheduled_serial : 0,
        xdg ? wl_list_length(&xdg->configure_list) : 0,
        xdg ? xdg->current.configure_serial : 0,
        window->commits,
        window->surface->buffer ? "attached" : "none",
        window->surface->current.width, window->surface->current.height,
        ctx->app_surface == window->surface ? "it is" : "it is not");
    if (window->map_watchdog_fired > 1) {
        wl_event_source_remove(window->map_watchdog);
        window->map_watchdog = NULL;
        return 0;
    }
    if (ctx->app_surface != window->surface) {
        /* Only the side's app surface is configured again: activating another
         * toplevel could take the activation from the one the side shows. */
        wlr_log(WLR_INFO, "No configure re-sent to the %s side's toplevel '%s' from the map watchdog: "
            "it is not the side's app surface", side_name(ctx->id), side_window_title(window));
    } else if (xdg && xdg->toplevel && xdg->initialized) {
        wlr_xdg_toplevel_set_size(xdg->toplevel, side_toplevel_width(ctx), side_toplevel_height(ctx));
        wlr_xdg_toplevel_set_activated(xdg->toplevel, true);
        wlr_xdg_surface_schedule_configure(xdg);
        wlr_log(WLR_INFO, "Re-sent configure %dx%d to the %s side's toplevel '%s' from the map watchdog",
            side_toplevel_width(ctx), side_toplevel_height(ctx), side_name(ctx->id), side_window_title(window));
    } else {
        wlr_log(WLR_INFO, "No configure re-sent to the %s side's toplevel '%s' from the map watchdog: "
            "it has made no initial commit", side_name(ctx->id), side_window_title(window));
    }
    wl_event_source_timer_update(window->map_watchdog, ctx->comp ? ctx->comp->map_watchdog_ms : MAP_WATCHDOG_MS);
    return 0;
}

static void side_arm_map_watchdog(struct side_xdg_window *window) {
    struct compositor *comp = window->ctx ? window->ctx->comp : NULL;
    if (window->map_watchdog || !comp || !comp->display) return;
    window->map_watchdog = wl_event_loop_add_timer(
        wl_display_get_event_loop(comp->display), side_map_watchdog, window);
    if (window->map_watchdog) wl_event_source_timer_update(window->map_watchdog, comp->map_watchdog_ms);
}

/* Gamescope before 3.16.30 (upstream c50ddfa9, "WaylandBackend: don't attach a buffer before the toplevel is
 * reconfigured") hides its toplevel by attaching a NULL buffer whenever steamcompmgr has no focus window
 * (CWaylandConnector::SetVisible(false), every plane presents nullptr) and attaches a buffer again as soon as a
 * window appears, without the bufferless commit, configure and ack that xdg-shell requires after an unmap. wlroots
 * answers that commit with xdg_surface error 3, "xdg_surface has never been configured"; Gamescope's Wayland input
 * thread aborts on the dead connection (WaylandBackend.cpp, CWaylandInputThread::ThreadFunc) and the side restarts.
 * An installer that closes one dialog and opens the next does it (Alien Breed: Impact's UE3Redist on the Machine,
 * Gamescope 3.16.23.6). This runs before wlroots checks the commit: a toplevel that has mapped before, is unmapped
 * and unconfigured, and attaches a buffer is taken as remapping, and wlroots maps it. It is marked initialized as
 * well, so wlroots does not take the commit for an initial commit: an initial commit that maps would trip
 * assert(!surface->initial_commit) in xdg_surface_role_commit (wlr_xdg_surface.c:281, 0.17.1 and 0.17.4) at the
 * next unmap, and Gamescope unmaps again at the next focus gap. Nothing here sends a configure (the client does
 * not wait for one); the side's activation sends its usual one once the toplevel maps. A conforming client never
 * reaches this state, and a new toplevel that attaches a buffer before its first configure still gets the error. */
static void handle_xdg_window_client_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, surface_client_commit);
    struct wlr_xdg_surface *xdg = window->xdg_surface;
    struct wlr_surface *surface = window->surface;
    if (!xdg || !surface || !window->was_mapped || xdg->configured || surface->mapped ||
            xdg->role != WLR_XDG_SURFACE_ROLE_TOPLEVEL || !xdg->toplevel ||
            !(surface->pending.committed & WLR_SURFACE_STATE_BUFFER) || !surface->pending.buffer) {
        return;
    }
    window->remaps_tolerated++;
    wlr_log(WLR_INFO, "The %s side's toplevel '%s' attached a buffer after an unmap without a new configure "
        "(Gamescope before 3.16.30); accepting it as the remap (%u so far)",
        side_name(window->ctx->id), side_window_title(window), window->remaps_tolerated);
    xdg->initialized = true;
    xdg->configured = true;
}

static void handle_xdg_window_map(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, surface_map);
    window->was_mapped = true;
    wlr_log(WLR_INFO, "Side toplevel mapped on %s side: title='%s' surface %dx%d after %u commit(s), %d ms after it was assigned",
        side_name(window->ctx->id), side_window_title(window),
        window->surface ? window->surface->current.width : 0,
        window->surface ? window->surface->current.height : 0,
        window->commits, side_window_age_ms(window));
    if (window->map_watchdog) {
        wl_event_source_remove(window->map_watchdog);
        window->map_watchdog = NULL;
    }
}

static void handle_xdg_window_unmap(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, surface_unmap);
    wlr_log(WLR_INFO, "App surface unmapped on %s side: toplevel title='%s'",
        window->ctx->id == SIDE_LEFT ? "left" : "right", side_window_title(window));

    if (window->scene_tree) {
        wlr_scene_node_set_enabled(&window->scene_tree->node, false);
    }

    if (window->ctx) {
        side_refresh_active_surface(window->ctx);
        /* An unmapped toplevel does not keep the seat's keyboard focus: the client gets its leave now
         * and a new enter when it maps again, or when another window of the side takes over. */
        struct wlr_seat *seat = seat_for_side(window->ctx->comp, window->ctx->id);
        struct wlr_surface *root = window->surface ? wlr_surface_get_root_surface(window->surface) : NULL;
        if (seat && root && (seat->keyboard_state.focused_surface == root ||
                (seat == window->ctx->comp->seat_primary ? window->ctx->comp->kbd_focus_surface_primary :
                    window->ctx->comp->kbd_focus_surface_guest) == root)) {
            wlr_seat_keyboard_notify_clear_focus(seat);
            set_kbd_focus_surface(window->ctx->comp, seat, NULL);
        }
        side_ensure_app_keyboard_focus(window->ctx->comp, window->ctx);
        pointer_constraints_sync(window->ctx->comp,
            seat_for_side(window->ctx->comp, window->ctx->id));
        write_runtime_state(window->ctx->comp);
        request_redraw(window->ctx->comp);
    }
}

static void handle_xdg_window_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_xdg_window *window = wl_container_of(listener, window, surface_destroy);
    wlr_log(WLR_INFO, "App surface destroyed on %s side",
        window->ctx->id == SIDE_LEFT ? "left" : "right");
    side_remove_xdg_window(window);
}

static void handle_xwayland_request_configure(struct wl_listener *listener, void *data);

static void attach_xwayland_surface(struct compositor *comp,
        struct side_ctx *target, struct wlr_xwayland_surface *xsurface) {
    wlr_log(WLR_INFO, "attach_xwayland_surface side=%s xsurface=%p surface=%p",
        target->id == SIDE_LEFT ? "left" : "right", (void *)xsurface,
        xsurface ? (void *)xsurface->surface : NULL);
    if (!xsurface->surface) {
        wlr_log(WLR_DEBUG, "X11 surface for %s side is not yet associated",
            target->id == SIDE_LEFT ? "left" : "right");
        return;
    }

    struct side_xwayland_window *window = calloc(1, sizeof(*window));
    if (!window) {
        wlr_log(WLR_ERROR, "Failed to allocate xwayland window tracking");
        return;
    }

    window->ctx = target;
    window->xwayland_handle = xsurface;
    window->surface = xsurface->surface;

    side_init_xwayland_windows(target);
    wl_list_insert(&target->xwayland_windows, &window->link);

    if (xsurface->override_redirect) {
        wlr_xwayland_surface_configure(xsurface, xsurface->x, xsurface->y,
            xsurface->width, xsurface->height);
    } else {
        configure_side_xwayland_surface(comp, target, xsurface);
    }

    wlr_log(WLR_INFO, "attach_xwayland_surface creating scene tree at (0,0) in side_tree");
    window->scene_tree = wlr_scene_tree_create(target->side_tree);
    if (!window->scene_tree) {
        wlr_log(WLR_ERROR, "Failed to create X11 scene tree for %s side",
            target->id == SIDE_LEFT ? "left" : "right");
        wl_list_remove(&window->link);
        free(window);
        return;
    }
    wlr_scene_node_set_position(&window->scene_tree->node, 0, 0);
    wlr_scene_node_set_enabled(&window->scene_tree->node,
        side_is_shown(comp, target));

    wlr_log(WLR_INFO, "attach_xwayland_surface creating scene surface");
    struct wlr_scene_surface *scene_surface =
        wlr_scene_surface_create(window->scene_tree, xsurface->surface);
    window->scene_surface = scene_surface;

    window->surface_commit.notify = handle_xwayland_window_commit;
    window->surface_unmap.notify = handle_xwayland_window_unmap;
    window->surface_destroy.notify = handle_xwayland_window_destroy;
    window->request_configure.notify = handle_xwayland_window_request_configure;

    wl_signal_add(&xsurface->surface->events.commit, &window->surface_commit);
    window->surface_commit_armed = true;
    wl_signal_add(&xsurface->surface->events.unmap, &window->surface_unmap);
    window->surface_unmap_armed = true;
    wl_signal_add(&xsurface->events.destroy, &window->surface_destroy);
    window->surface_destroy_armed = true;
    wl_signal_add(&xsurface->events.request_configure, &window->request_configure);
    window->request_configure_armed = true;

    /* Maintain app_surface for greeter hiding and primary focus */
    target->app_surface = xsurface->surface;
    target->app_xwayland_handle = xsurface;
    target->app_xwayland_surface = scene_surface;
    target->app_scene_tree = window->scene_tree;
    target->app_scene_tree_owned = false;
    write_runtime_state(comp);

    wlr_log(WLR_INFO, "attach_xwayland_surface complete");
    request_redraw(comp);
}

static void handle_xwayland_request_configure(struct wl_listener *listener, void *data) {
    struct side_ctx *ctx = wl_container_of(listener, ctx, xwayland_request_configure);
    struct wlr_xwayland_surface_configure_event *event = data;

    if (!ctx->app_xwayland_handle || !event || event->surface != ctx->app_xwayland_handle) {
        return;
    }

    configure_side_xwayland_surface(ctx->comp, ctx, event->surface);
}

static void handle_xwayland_surface_map(struct wl_listener *listener, void *data) {
    struct side_ctx *ctx = wl_container_of(listener, ctx, xwayland_surface_map);
    struct wlr_surface *surface = data;

    if (!ctx->pending_xwayland_surface || surface != ctx->pending_xwayland_surface->surface) {
        return;
    }

    wl_list_remove(&ctx->xwayland_surface_map.link);
    ctx->xwayland_surface_map_armed = false;
    attach_xwayland_surface(ctx->comp, ctx, ctx->pending_xwayland_surface);
}

static void handle_xwayland_associate(struct wl_listener *listener, void *data) {
    struct side_ctx *ctx = wl_container_of(listener, ctx, xwayland_associate);
    struct wlr_xwayland_surface *xsurface = ctx->pending_xwayland_surface;

    wlr_log(WLR_INFO, "handle_xwayland_associate side=%s xsurface=%p surface=%p",
        ctx->id == SIDE_LEFT ? "left" : "right", (void *)xsurface,
        xsurface ? (void *)xsurface->surface : NULL);
    (void)data;
    wl_list_remove(&ctx->xwayland_associate.link);
    ctx->xwayland_associate_armed = false;
    if (!xsurface || !xsurface->surface) {
        return;
    }
    attach_xwayland_surface(ctx->comp, ctx, xsurface);
}

static void handle_xwayland_ready(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_ctx *ctx = wl_container_of(listener, ctx, xwayland_ready);
    if (!ctx->xwayland) {
        return;
    }

    struct wlr_seat *seat = seat_for_role(ctx->comp, ctx->role);
    wlr_xwayland_set_seat(ctx->xwayland, seat);
    wlr_log(WLR_INFO, "Assigned seat %s to Xwayland for side %s after ready event",
        side_role_name(ctx->role), side_name(ctx->id));
}


static void handle_hud_surface_destroy(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, hud_surface_destroy);
    comp->hud_surface = NULL;
    comp->hud_scene_tree = NULL;
    comp->hud_surface_destroy_armed = false;
}

/* === The panel: Help and Feedback ===
 *
 * The menu hands two jobs to a program of its own: reading the manual and writing feedback. That program is
 * started when asked for and is gone when closed, so nothing of it is carried while nobody uses it. It runs as
 * the menu owner's account, outside that account's session sandbox, so it is given nothing of the account's own
 * making to load: a clean environment, HOME=/, no QML cache, Python in isolated mode, and only the group the
 * account itself is (never input, audio or seat groups). Its Wayland connection is made here and handed over, so
 * its window is known by that connection. It gets keys only: the menu's own, forwarded. */
static const char *panel_title(const char *mode) {
    return mode && strcmp(mode, "feedback") == 0 ? "Feedback" : "Help";
}

static void panel_forget_surface(struct compositor *comp) {
    if (comp->panel_surface) wl_list_remove(&comp->panel_surface_destroy.link);
    comp->panel_surface = NULL;
    comp->panel_toplevel = NULL;
    comp->panel_scene_tree = NULL;
}

static void handle_panel_surface_destroy(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, panel_surface_destroy);
    (void)data;
    panel_forget_surface(comp);
    request_redraw(comp);
}

static void handle_panel_client_destroy(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, panel_client_destroy);
    (void)data;
    wl_list_remove(&comp->panel_client_destroy.link);
    comp->panel_client = NULL;
}

/* Everything of the panel is gone or going: forget it and give the menu its picture back. */
static void panel_finish(struct compositor *comp) {
    if (comp->panel_client) {
        struct wl_client *client = comp->panel_client;
        wl_list_remove(&comp->panel_client_destroy.link);
        comp->panel_client = NULL;
        wl_client_destroy(client);          /* destroys its surfaces, which forgets panel_surface */
    }
    if (comp->panel_surface) panel_forget_surface(comp);
    comp->panel_pid = 0;
    comp->panel_mode[0] = '\0';
    comp->panel_closing = false;
    write_runtime_state(comp);
    request_redraw(comp);
}

/* Ask the panel to end. Its launcher is a process group of its own: it ends qml6 and keeps what was not sent. */
static void close_panel(struct compositor *comp, const char *why) {
    if (!comp || comp->panel_pid <= 0 || comp->panel_closing) return;
    wlr_log(WLR_INFO, "Closing the %s panel: %s", comp->panel_mode, why);
    kill(-comp->panel_pid, SIGTERM);
    comp->panel_closing = true;
    comp->panel_started = monotonic_seconds();
    if (comp->panel_scene_tree) wlr_scene_node_set_enabled(&comp->panel_scene_tree->node, false);
    request_redraw(comp);
}

/* The panel covers its side exactly; called when its window appears and whenever the side has moved. */
static void panel_place(struct compositor *comp) {
    struct side_ctx *side = compositor_side_by_id(comp, comp->panel_side);
    if (!side || !comp->panel_toplevel || !comp->panel_scene_tree) return;
    if (comp->panel_x == side->x && comp->panel_y == side->y && comp->panel_w == side->width &&
            comp->panel_h == side->height) return;
    comp->panel_x = side->x; comp->panel_y = side->y; comp->panel_w = side->width; comp->panel_h = side->height;
    wlr_scene_node_set_position(&comp->panel_scene_tree->node, side->x, side->y);
    wlr_xdg_toplevel_set_size(comp->panel_toplevel, side->width, side->height);
    wlr_xdg_surface_schedule_configure(comp->panel_toplevel->base);
}

static int panel_poll(void *data) {
    struct compositor *comp = data;
    if (comp->panel_pid <= 0) return 0;
    int status = 0;
    pid_t done = waitpid(comp->panel_pid, &status, WNOHANG);
    if (done == comp->panel_pid || (done < 0 && errno != EINTR)) {
        wlr_log(WLR_INFO, "The %s panel ended (%s %d)", comp->panel_mode,
            done > 0 && WIFSIGNALED(status) ? "signal" : "exit status",
            done < 0 ? -1 : WIFSIGNALED(status) ? WTERMSIG(status) : WEXITSTATUS(status));
        panel_finish(comp);
        return 0;
    }
    double waited = monotonic_seconds() - comp->panel_started;
    if (comp->panel_closing && waited > 6.0) {
        kill(-comp->panel_pid, SIGKILL);
    } else if (!comp->panel_closing && !comp->panel_surface && waited > PANEL_MAP_SECONDS) {
        wlr_log(WLR_ERROR, "The %s panel showed no window within %.0f s", comp->panel_mode, PANEL_MAP_SECONDS);
        snprintf(comp->device_message, sizeof(comp->device_message), "%s could not open. See the log.",
            panel_title(comp->panel_mode));
        close_panel(comp, "no window");
    } else if (!comp->panel_closing && (!comp->management_overlay_visible ||
            comp->management_overlay_owner != comp->panel_side)) {
        close_panel(comp, "its menu is gone");
    } else {
        panel_place(comp);
    }
    wl_event_source_timer_update(comp->panel_timer, 200);
    return 0;
}

static bool panel_helper_usable(const char *path, char *why, size_t why_size) {
    struct stat info;
    if (!path || !path[0] || lstat(path, &info) != 0) snprintf(why, why_size, "missing");
    else if (!S_ISREG(info.st_mode)) snprintf(why, why_size, "not a regular file");
    else if (info.st_uid != 0 && info.st_uid != geteuid()) snprintf(why, why_size, "not owned by root");
    else if (info.st_mode & 0022) snprintf(why, why_size, "writable by others");
    else return true;
    return false;
}

/* Start the panel for the owner of the open menu. mode is "help" or "feedback"; page names the menu page Help
 * was asked from. Returns false, with a note for the menu, when nothing was started. */
static bool launch_panel(struct compositor *comp, struct side_ctx *owner, const char *mode, const char *page) {
    if (!comp || !owner || comp->panel_pid > 0 || comp->power_action[0]) return false;
    /* A release without the form has no Feedback: not by its button, and not by any other way in. */
    if (strcmp(mode, "feedback") == 0 && !comp->feedback_available) return false;
    const char *command = comp->config.test_hooks ? getenv("DOSPLEX_PANEL_COMMAND") : NULL;
    if (command && !command[0]) command = NULL;
    char why[64];
    if (!command && !panel_helper_usable(comp->panel_helper, why, sizeof(why))) {
        wlr_log(WLR_ERROR, "Panel launcher %s is not usable (%s)", comp->panel_helper, why);
        snprintf(comp->device_message, sizeof(comp->device_message), "%s is not installed.", panel_title(mode));
        return false;
    }
    const char *account = side_account_name(owner);
    struct passwd *pw = account ? getpwnam(account) : NULL;
    bool drop = geteuid() == 0;
    if (drop && (!pw || pw->pw_uid == 0)) {
        wlr_log(WLR_ERROR, "No ordinary account for the %s side; the panel is not started", side_name(owner->id));
        snprintf(comp->device_message, sizeof(comp->device_message), "%s could not open. See the log.", panel_title(mode));
        return false;
    }
    uid_t uid = pw ? pw->pw_uid : geteuid();
    gid_t gid = pw ? pw->pw_gid : getegid();
    char home[256], user[64];
    snprintf(home, sizeof(home), "%s", pw && pw->pw_dir ? pw->pw_dir : "/");
    snprintf(user, sizeof(user), "%s", account ? account : "player");
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) != 0) {
        wlr_log(WLR_ERROR, "socketpair for the panel failed: %s", strerror(errno));
        return false;
    }
    if (!comp->panel_timer)
        comp->panel_timer = wl_event_loop_add_timer(wl_display_get_event_loop(comp->display), panel_poll, comp);
    struct wl_client *client = comp->panel_timer ? wl_client_create(comp->display, pair[0]) : NULL;
    if (!client) {
        close(pair[0]);
        close(pair[1]);
        return false;
    }
    const char *role = owner->role == SIDE_ROLE_PRIMARY ? "main" : "guest";
    const char *layout = layout_mode_name(comp->layout);
    pid_t pid = fork();
    if (pid < 0) {
        wlr_log(WLR_ERROR, "Failed to fork the panel: %s", strerror(errno));
        wl_client_destroy(client);
        close(pair[1]);
        return false;
    }
    if (pid == 0) {
        spawn_child_reset();
        setsid();
        int fd = dup(pair[1]);              /* a copy without close-on-exec: the one descriptor that is handed on */
        if (fd < 0 || chdir("/") != 0) _exit(126);
        if (drop && (setgroups(1, &gid) != 0 || setresgid(gid, gid, gid) != 0 || setresuid(uid, uid, uid) != 0 ||
                getuid() != uid || geteuid() != uid || setuid(0) == 0)) _exit(126);
        char socket_fd[16];
        snprintf(socket_fd, sizeof(socket_fd), "%d", fd);
        if (!command) {
            /* Nothing of the account's own making is loaded: not its environment, not its Qt or Python files. */
            clearenv();
            setenv("PATH", "/usr/bin:/bin", 1);
            setenv("HOME", "/", 1);
            setenv("USER", user, 1);
            setenv("LOGNAME", user, 1);
            setenv("LANG", "C.UTF-8", 1);
            setenv("QT_QPA_PLATFORM", "wayland", 1);
            setenv("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1", 1);
            setenv("QML_DISABLE_DISK_CACHE", "1", 1);
            setenv("DBUS_SESSION_BUS_ADDRESS", "disabled:", 1);
        }
        setenv("WAYLAND_SOCKET", socket_fd, 1);
        unsetenv("WAYLAND_DISPLAY");
        if (command)
            execl(command, command, "--mode", mode, "--side", role, "--page", page, "--layout", layout, (char *)NULL);
        else
            execl(PANEL_PYTHON, PANEL_PYTHON, "-I", comp->panel_helper, "--mode", mode, "--side", role,
                "--page", page, "--layout", layout, "--home", home, (char *)NULL);
        _exit(127);
    }
    close(pair[1]);
    comp->panel_client = client;
    comp->panel_client_destroy.notify = handle_panel_client_destroy;
    wl_client_add_destroy_listener(client, &comp->panel_client_destroy);
    comp->panel_pid = pid;
    comp->panel_side = owner->id;
    comp->panel_closing = false;
    comp->panel_started = monotonic_seconds();
    comp->panel_x = comp->panel_y = comp->panel_w = comp->panel_h = -1;
    snprintf(comp->panel_mode, sizeof(comp->panel_mode), "%s", mode);
    wl_event_source_timer_update(comp->panel_timer, 200);
    wlr_log(WLR_INFO, "Started the %s panel for %s on the %s side (pid=%d, page %s)", mode, role,
        side_name(owner->id), pid, page);
    return true;
}

/* The panel's window: over its side, above the menu, with the side's keyboard focus. */
static void panel_adopt_toplevel(struct compositor *comp, struct wlr_xdg_surface *xdg_surface) {
    if (comp->panel_surface || comp->panel_closing || !comp->panel_tree) return;   /* one window, the first */
    comp->panel_scene_tree = wlr_scene_xdg_surface_create(comp->panel_tree, xdg_surface);
    if (!comp->panel_scene_tree) return;
    comp->panel_toplevel = xdg_surface->toplevel;
    comp->panel_surface = xdg_surface->surface;
    comp->panel_surface_destroy.notify = handle_panel_surface_destroy;
    wl_signal_add(&xdg_surface->surface->events.destroy, &comp->panel_surface_destroy);
    wlr_scene_node_raise_to_top(&comp->panel_tree->node);
    if (comp->cursor_tree) wlr_scene_node_raise_to_top(&comp->cursor_tree->node);
    comp->panel_x = comp->panel_y = comp->panel_w = comp->panel_h = -1;
    panel_place(comp);
    wlr_xdg_toplevel_set_activated(xdg_surface->toplevel, true);
    struct wlr_seat *seat = seat_for_side(comp, comp->panel_side);
    struct wlr_keyboard *keyboard = seat ? wlr_seat_get_keyboard(seat) : NULL;
    if (seat && keyboard) {
        wlr_seat_keyboard_notify_enter(seat, xdg_surface->surface, keyboard->keycodes, keyboard->num_keycodes,
            &keyboard->modifiers);
        set_kbd_focus_surface(comp, seat, xdg_surface->surface);
    }
    wlr_log(WLR_INFO, "The %s panel's window is up after %.0f ms", comp->panel_mode,
        (monotonic_seconds() - comp->panel_started) * 1000.0);
    write_runtime_state(comp);
    request_redraw(comp);
}

/* A key of the menu owner while the panel is open goes to the panel and nowhere else. */
static bool panel_key(struct compositor *comp, struct side_ctx *target, uint32_t keycode, uint32_t state) {
    if (comp->panel_pid <= 0 || !target || target->id != comp->panel_side) return false;
    struct wlr_seat *seat = seat_for_side(comp, target->id);
    if (seat && comp->panel_surface && !comp->panel_closing) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        wlr_seat_keyboard_notify_key(seat, (uint32_t)(now.tv_sec * 1000 + now.tv_nsec / 1000000), keycode, state);
    }
    return true;
}

static void suspend_overlay_app_input(struct compositor *comp, struct side_ctx *target) {
    if (resolve_remote_input_target(comp) == target && remote_keys_held(comp)) {
        comp->remote_overlay_wait_neutral = true;
    }
    struct wlr_seat *seat = seat_for_side(comp, target->id);
    if (!seat) return;
    wlr_seat_keyboard_notify_clear_focus(seat);
    set_kbd_focus_surface(comp, seat, NULL);
    pointer_release_buttons(comp, target);
    wlr_seat_pointer_notify_clear_focus(seat);
    struct input_device_ctx *input;
    wl_list_for_each(input, &comp->input_devices, link) {
        if (input->keyboard_target_valid && input->keyboard_target == target->id && input->held_key_count) {
            input->keyboard_overlay_wait_neutral = true;
        }
    }
}

/* Optional native-input broker barrier. The menu must not become visible until
 * the broker has neutralized every native reader and discarded queued input.
 * A missing or failed configured broker leaves the menu closed. */
static bool capture_native_overlay_input(struct side_ctx *target) {
    const char *path = getenv("DOSPLEX_NATIVE_CAPTURE_SOCKET");
    if (!path || !path[0]) return true;
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    struct stat st;
    if (strlen(path) >= sizeof(addr.sun_path) || lstat(path, &st) != 0 ||
            !S_ISSOCK(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) return false;
    strcpy(addr.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return false;
    bool ready = false;
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) goto done;
    struct ucred peer;
    socklen_t peer_len = sizeof(peer);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_len) != 0 || peer.uid != geteuid()) goto done;
    char request[32];
    int size = snprintf(request, sizeof(request), "CAPTURE %s", side_name(target->id));
    if (send(fd, request, size, MSG_NOSIGNAL) != size) goto done;
    struct pollfd event = {.fd = fd, .events = POLLIN};
    if (poll(&event, 1, 200) != 1 || !(event.revents & POLLIN)) goto done;
    char reply[16];
    ssize_t count = recv(fd, reply, sizeof(reply), 0);
    ready = count == 5 && memcmp(reply, "READY", 5) == 0;
done:
    close(fd);
    if (!ready) wlr_log(WLR_ERROR, "Native input capture failed; overlay remains closed");
    return ready;
}

/* Native gamepads do not produce a Wayland keyboard/pointer event after
 * closing the menu. Return focus explicitly to this account's application,
 * using its own neutral remote keyboard rather than borrowing the other seat's
 * physical device. Held remote keys remain gated until their normal release. */
static void refocus_side_app(struct compositor *comp, struct side_ctx *side) {
    if (side && side->app_surface && side->activity == 0 &&
            side_is_shown(comp, side) &&
            !remote_keys_held(comp)) {
        focus_known_side_surface(comp, side, side->app_surface,
            remote_keyboard(comp, side));
    }
}

static void close_management_overlay(struct compositor *comp) {
    close_panel(comp, "the menu closed");
    finish_device_move(comp, false);
    struct side_ctx *owner = compositor_side_by_id(comp, comp->management_overlay_owner);
    comp->management_overlay_visible = false;
    comp->pending_shared_action = MODAL_ACTION_NONE;
    refocus_side_app(comp, owner);
    /* With no physical keyboard the seat holds its placeholder, which only this call brings back. */
    side_ensure_app_keyboard_focus(comp, owner);
}

/* Guest full screen: Main goes to Standby as Guest does for Full screen, its overlay closes, and Guest is shown
 * and active. Only Main's own choice gets here (a shared action, or the control socket and hud-ipc). */
static void enter_guest_full_screen(struct compositor *comp) {
    struct side_ctx *main_side = primary_side(comp);
    comp->pending_shared_action = MODAL_ACTION_NONE;
    if (main_side) {
        suspend_overlay_app_input(comp, main_side);
        main_side->activity = 1;
    }
    if (comp->management_overlay_visible) close_management_overlay(comp);
    comp->layout = LAYOUT_SINGLE_GUEST;
    comp->output_scale_mode = OUTPUT_SCALE_FIT;
    show_guest_for_split(comp);
    apply_layout(comp);
    request_redraw(comp);
}

static void launch_device_hud(struct compositor *comp, struct side_ctx *target) {
    if (!comp || !target || comp->power_action[0]) {
        return;
    }

    bool opening = !comp->management_overlay_visible || comp->management_overlay_owner != target->id;
    if (!opening) {
        close_management_overlay(comp);
        write_runtime_state(comp);
        request_redraw(comp);
        return;
    }
    if (!capture_native_overlay_input(target)) return;
    if (comp->management_overlay_visible) close_management_overlay(comp);
    if (comp->management_overlay_owner != target->id) {
        /* The tab and the audio page are the previous owner's; a new owner starts on Display. */
        comp->overlay_tab = 0;
        comp->audio_page = 0;
    }
    comp->hud_target_side = target->id;
    comp->management_overlay_visible = !comp->management_overlay_visible ||
        comp->management_overlay_owner != target->id;
    comp->management_overlay_owner = target->id;
    if (comp->management_overlay_visible) suspend_overlay_app_input(comp, target);
    comp->pending_shared_action = MODAL_ACTION_NONE;
    comp->device_message[0] = 0; /* Main's last note is not Guest's, and the other way round. */
    comp->overlay_focus = 1;
    wlr_log(WLR_INFO, "Management overlay %s on %s side",
        comp->management_overlay_visible ? "shown" : "hidden",
        side_name(target->id));
    write_runtime_state(comp);
    request_redraw(comp);
}

/* A player asked for the overlay: the controller chord (MENU <side> toggle), Ctrl+Shift+M or
 * Super+Shift+M, the CHORD test hook, or the hud-ipc command OVERLAY <side>. There is one overlay, so who
 * gets it is decided here, by role (Main is SIDE_ROLE_PRIMARY, wherever it sits): Main always takes it
 * over, Guest never takes or keeps Main's, and Full screen shows only Main. The owner's own request closes
 * it when `toggle` (a chord) and leaves it open otherwise (OVERLAY <side> opens, it never closes). */
static void request_overlay(struct compositor *comp, struct side_ctx *target, const char *source, bool toggle) {
    if (!comp || !target) return;
    const char *who = target->role == SIDE_ROLE_PRIMARY ? "Main" : "Guest";
    if (comp->power_action[0]) {
        wlr_log(WLR_INFO, "Ignoring %s's overlay request (%s): %s is in progress", who, source, comp->power_action);
        return;
    }
    if (!side_may_use_overlay(comp, target)) {
        wlr_log(WLR_INFO, "Ignoring %s's overlay request (%s): %s", who, source,
            comp->layout == LAYOUT_SINGLE ? "Full screen shows only Main" : "Guest full screen shows only Guest");
        return;
    }
    struct side_ctx *owner = comp->management_overlay_visible ?
        compositor_side_by_id(comp, comp->management_overlay_owner) : NULL;
    if (owner && owner != target) {
        if (target->role != SIDE_ROLE_PRIMARY) {
            wlr_log(WLR_INFO, "Ignoring Guest's overlay request (%s): Main's overlay is open", source);
            return;
        }
        /* Closing clears Guest's pending confirmation and undoes a device move that waits. */
        close_management_overlay(comp);
        wlr_log(WLR_INFO, "Main took the overlay from Guest (%s)", source);
        launch_device_hud(comp, target);
        /* Also when Main's capture failed and the overlay stays closed. */
        write_runtime_state(comp);
        request_redraw(comp);
        return;
    }
    if (owner && !toggle) return; /* its own side's overlay is open already */
    launch_device_hud(comp, target);
}

static void request_overlay_chord(struct compositor *comp, struct side_ctx *target, const char *source) {
    request_overlay(comp, target, source, true);
}

/* The power progress panel went up. Both sides' applications lose the keyboard focus, so that what they
 * hold down ends with the focus (a client treats a leave as the release of every key); pointer buttons end;
 * and a keyboard that holds keys is not heard again until they are up. The panel's input hold drops the
 * releases that follow, so nothing is left to repeat under it. */
static void power_panel_input_off(struct compositor *comp) {
    suspend_overlay_app_input(comp, &comp->left);
    suspend_overlay_app_input(comp, &comp->right);
}

/* The power action did not take the machine down: its helper failed, or the deadline passed. The caller has
 * cleared power_action and set power_message. Input comes back (the applications get the keyboard focus as
 * when the overlay closes) and Main's overlay opens again on the Session tab, where the message says what
 * happened. Guest's overlay never opens over a power action, and nothing opens while the panel shows, so the
 * overlay that was open when Continue was pressed was Main's. */
static void power_panel_down(struct compositor *comp) {
    struct side_ctx *main_side = primary_side(comp);
    refocus_side_app(comp, guest_side(comp));
    refocus_side_app(comp, main_side);
    if (main_side) {
        bool open = comp->management_overlay_visible && comp->management_overlay_owner == main_side->id;
        if (!open) launch_device_hud(comp, main_side);
        if (comp->management_overlay_visible && comp->management_overlay_owner == main_side->id)
            comp->overlay_tab = 2;
    }
    /* Guest's overlay never opens over a power action, so its application only needs the focus back; Main's
     * is handled above, where the overlay that opens again owns the keyboard. */
    side_ensure_app_keyboard_focus(comp, guest_side(comp));
    write_runtime_state(comp);
    request_redraw(comp);
}

static void set_dynamic_device_route(struct compositor *comp, const char *device_name,
        enum input_route_mode mode) {
    if (!comp || !device_name || !device_name[0]) {
        return;
    }
    if (strncmp(device_name, "input:", 6) == 0) {
        unsigned id; char extra;
        if (sscanf(device_name + 6, "%u%c", &id, &extra) != 1 || !id) return;
        struct input_device_ctx *input;
        wl_list_for_each(input, &comp->input_devices, link) {
            if (input->route_id != id) continue;
            struct dynamic_input_route *route;
            wl_list_for_each(route, &comp->dynamic_input_routes, link) {
                if (route->device == input->device) { route->mode = mode; return; }
            }
            route = calloc(1, sizeof(*route));
            if (!route) return;
            route->device = input->device;
            snprintf(route->device_name, sizeof(route->device_name), "%s", device_name);
            route->mode = mode;
            wl_list_insert(&comp->dynamic_input_routes, &route->link);
            return;
        }
        return; /* A disconnected row must never select a replacement device. */
    }
    if (strncmp(device_name, "controller:", 11) == 0) {
        const char *target = mode == main_input_route_mode(comp) ? "main" :
            mode == guest_input_route_mode(comp) ? "guest" : "unassigned";
        if (!request_controller_route(device_name + 11, target))
            wlr_log(WLR_ERROR, "Controller assignment request failed");
        return;
    }
    if (strcmp(device_name, "__dosplex_remote_input__") == 0) {
        if (mode == INPUT_ROUTE_BOTH) {
            mode = INPUT_ROUTE_FOLLOW;
        }
        comp->remote_input_route = mode;
        if (comp->layout == LAYOUT_SINGLE) {
            comp->remote_input_target = primary_side(comp);
            comp->remote_input_role = SIDE_ROLE_PRIMARY;
        }
        wlr_log(WLR_INFO, "Updated Input Leap route to %s",
            input_route_mode_name(mode));
        write_runtime_state(comp);
        return;
    }
    if (strcmp(device_name, "__dosplex_deck_controls__") == 0) {
        set_dynamic_device_route(comp, "Steam Deck", mode);
        return;
    }
    if (mode == INPUT_ROUTE_BOTH) {
        mode = INPUT_ROUTE_FOLLOW;
    }
    apply_external_gamepad_route(comp, device_name, mode);
    if (mode == INPUT_ROUTE_BOTH &&
            input_device_name_is_keyboard(comp, device_name)) {
        wlr_log(WLR_INFO, "Ignoring screen-wide route for keyboard %s",
            device_name);
        mode = INPUT_ROUTE_FOLLOW;
    }

    /* Update existing routes matching this device name */
    bool found = false;
    struct dynamic_input_route *route = NULL;
    wl_list_for_each(route, &comp->dynamic_input_routes, link) {
        if (strcmp(route->device_name, device_name) == 0) {
            route->mode = mode;
            found = true;
        }
    }

    if (found) {
        wlr_log(WLR_INFO, "Updated dynamic routes for device name %s to %s",
            device_name, input_route_mode_name(mode));
        return;
    }

    /* Create new routes for all devices with this name */
    struct input_device_ctx *input = NULL;
    wl_list_for_each(input, &comp->input_devices, link) {
        if (input->device && input->device->name && 
                strcmp(input->device->name, device_name) == 0) {
            route = calloc(1, sizeof(*route));
            if (route) {
                route->device = input->device;
                snprintf(route->device_name, sizeof(route->device_name), "%s", device_name);
                route->mode = mode;
                wl_list_insert(&comp->dynamic_input_routes, &route->link);
            }
        }
    }
    wlr_log(WLR_INFO, "Added dynamic routes for all devices named %s to %s",
        device_name, input_route_mode_name(mode));
}

/* The line the Devices tab shows under its tabs. It is logged too, so a log tells what the menu said. */
static void say_in_devices(struct compositor *comp, const char *text) {
    snprintf(comp->device_message, sizeof(comp->device_message), "%s", text);
    wlr_log(WLR_INFO, "Devices: %s", text);
}

/* A line about one controller: "Controller 2 is already yours." when the controller has a number, the
 * unnumbered wording of an older routing service otherwise. */
static void say_about_controller(struct compositor *comp, const char *named, const char *rest, const char *plain) {
    if (!named[0]) {
        say_in_devices(comp, plain);
        return;
    }
    char text[160];
    snprintf(text, sizeof(text), "%s %s", named, rest);
    say_in_devices(comp, text);
}

static void finish_device_move(struct compositor *comp, bool keep) {
    if (!comp->device_move_active) return;
    comp->device_move_active = false;
    if (comp->device_move_timer) {
        wl_event_source_remove(comp->device_move_timer);
        comp->device_move_timer = NULL;
    }
    enum input_route_mode target = keep ? comp->device_move_target : comp->device_move_previous;
    enum input_route_mode other_target = keep ? comp->device_move_previous : comp->device_move_other_previous;
    if (!strncmp(comp->device_move_key, "controller:", 11)) {
        const char *role = target == main_input_route_mode(comp) ? "main" :
            target == guest_input_route_mode(comp) ? "guest" : "unassigned";
        if (!request_controller_assignment(comp->device_move_key + 11, role,
                comp->device_move_other[0] ? comp->device_move_other + 11 : NULL,
                other_target == main_input_route_mode(comp) ? "main" : "guest", false)) {
            say_in_devices(comp, "Assignment could not be saved. The temporary move will expire.");
        }
    } else if (!keep) {
        set_dynamic_device_route(comp, comp->device_move_key, target);
        if (comp->device_move_other[0]) set_dynamic_device_route(comp, comp->device_move_other, other_target);
    }
    comp->device_move_other[0] = 0;
    comp->pending_shared_action = MODAL_ACTION_NONE;
    write_runtime_state(comp);
    request_redraw(comp);
}

static int expire_device_move(void *data) {
    finish_device_move(data, false);
    return 0;
}

/* What the Keep/Undo preview calls the place a device is moved to. A route that follows neither side is
 * Unassigned for a controller and Shared for a mouse or keyboard, which go to the side under the cursor. */
static const char *device_move_target_name(struct compositor *comp, const char *key, enum input_route_mode mode) {
    if (mode == main_input_route_mode(comp)) return "Main";
    if (mode == guest_input_route_mode(comp)) return "Guest";
    return strncmp(key, "input:", 6) == 0 ? "Shared" : "Unassigned";
}

static void try_device_move(struct compositor *comp, const char *key, enum input_route_mode mode) {
    if (comp->device_move_active) return;
    comp->device_message[0] = 0;
    if (mode != main_input_route_mode(comp) && !main_control_remains(comp, key)) {
        say_in_devices(comp, "Keep a control for Main. Connect another or use Swap controls.");
        return;
    }
    comp->device_move_other[0] = 0;
    enum input_route_mode previous;
    const char *label = NULL;
    char controller_label[128];
    struct json_object *inventory = NULL;
    if (strncmp(key, "input:", 6) == 0) {
        unsigned id; char extra;
        if (sscanf(key + 6, "%u%c", &id, &extra) != 1) return;
        struct input_device_ctx *input;
        wl_list_for_each(input, &comp->input_devices, link) {
            if (input->route_id != id) continue;
            previous = configured_route_mode_for_device(comp, input);
            label = input->device->name ? overlay_input_label(input->device->name) : NULL;
            break;
        }
    } else if (strncmp(key, "controller:", 11) == 0) {
        inventory = controller_inventory();
        struct json_object *preview_version = NULL;
        if (inventory && (!json_object_object_get_ex(inventory, "assignment_preview", &preview_version) ||
                json_object_get_int(preview_version) != 2)) {
            json_object_put(inventory);
            return; /* Older brokers cannot guarantee recovery after a crash. */
        }
        struct json_object *devices = NULL;
        if (inventory) json_object_object_get_ex(inventory, "devices", &devices);
        for (size_t i = 0; devices && i < json_object_array_length(devices); i++) {
            struct json_object *device = json_object_array_get_idx(devices, i);
            if (strcmp(controller_text(device, "id"), key + 11)) continue;
            char requested[16];
            if (!read_controller_requested_route(key + 11, requested)) break;
            previous = !strcmp(requested, "main") ? main_input_route_mode(comp) :
                !strcmp(requested, "guest") ? guest_input_route_mode(comp) : INPUT_ROUTE_FOLLOW;
            controller_display_name(device, controller_label, sizeof(controller_label));
            label = controller_label;
            break;
        }
    }
    if (label && previous != mode) {
        snprintf(comp->device_move_key, sizeof(comp->device_move_key), "%s", key);
        snprintf(comp->device_move_label, sizeof(comp->device_move_label), "%s → %s", label,
            device_move_target_name(comp, key, mode));
        comp->device_move_previous = previous;
        comp->device_move_target = mode;
        comp->device_move_timer = wl_event_loop_add_timer(wl_display_get_event_loop(comp->display), expire_device_move, comp);
        if (comp->device_move_timer && wl_event_source_timer_update(comp->device_move_timer, 15000) == 0) {
            comp->device_move_active = true;
            comp->pending_shared_action = MODAL_ACTION_KEEP_DEVICE;
            comp->overlay_focus = 5; /* Undo first. */
            wlr_log(WLR_INFO, "Devices: %s", comp->device_move_label); /* What the confirmation asks about. */
            if (!strncmp(key, "controller:", 11)) {
                const char *target = mode == main_input_route_mode(comp) ? "main" :
                    mode == guest_input_route_mode(comp) ? "guest" : "unassigned";
                if (!request_controller_assignment(key + 11, target, NULL, NULL, true)) finish_device_move(comp, false);
            } else set_dynamic_device_route(comp, key, mode);
        } else if (comp->device_move_timer) {
            wl_event_source_remove(comp->device_move_timer);
            comp->device_move_timer = NULL;
        }
    }
    if (inventory) json_object_put(inventory);
}

/* Guest claims a free controller, or releases one of its own (T-422). Both take effect at once, through
 * the same request path as every controller move: nothing of Main's changes, and each is undone by the
 * other, so there is no preview to confirm. The request is checked against the saved assignment, not
 * against what the row showed, so a stale button cannot take a controller another player now has. */
static void claim_or_release_device(struct compositor *comp, const char *key, bool claim) {
    comp->device_message[0] = 0;
    if (strncmp(key, "controller:", 11) || !controller_key_valid(key + 11)) return;
    struct json_object *inventory = controller_inventory(), *devices = NULL, *version = NULL;
    bool supported = inventory && json_object_object_get_ex(inventory, "assignment_preview", &version) &&
        json_object_get_int(version) == 2;
    bool listed = false, awake = false;
    char named[128] = "";
    if (supported) json_object_object_get_ex(inventory, "devices", &devices);
    for (size_t i = 0; devices && i < json_object_array_length(devices); i++) {
        struct json_object *d = json_object_array_get_idx(devices, i), *connected;
        if (strcmp(controller_text(d, "id"), key + 11)) continue;
        listed = true;
        if (controller_index(d)) controller_display_name(d, named, sizeof(named));
        awake = json_object_object_get_ex(d, "connected", &connected) && json_object_get_boolean(connected);
        break;
    }
    if (inventory) json_object_put(inventory);
    char saved[16];
    if (!listed || !read_controller_requested_route(key + 11, saved)) {
        say_in_devices(comp, "Controllers cannot be changed right now.");
    } else if (claim && !strcmp(saved, "guest")) {
        say_about_controller(comp, named, "is already yours.", "You already have this controller.");
    } else if (claim && strcmp(saved, "unassigned")) {
        say_about_controller(comp, named, "belongs to another player.", "Another player has this controller.");
    } else if (!claim && strcmp(saved, "guest")) {
        say_about_controller(comp, named, "is not yours to release.", "That controller is not yours to release.");
    } else if (claim && !awake) {
        say_about_controller(comp, named, "is asleep. Wake it, then claim it.", "Wake the controller, then claim it.");
    } else {
        /* Nothing on Guest's side could operate this menu once its last control is gone, and the other
         * player's controller does not take over an open menu: the menu closes with the release. */
        bool last = !claim && !guest_control_remains(comp, key);
        if (!request_controller_route(key + 11, claim ? "guest" : "unassigned")) {
            say_about_controller(comp, named, "could not be moved: the assignment was not saved.", "Assignment could not be saved.");
        } else if (last) {
            say_about_controller(comp, named, "released. That was Guest's last control, so the menu closes.",
                "Released. That was Guest's last control, so the menu closes.");
            close_management_overlay(comp);
        } else {
            if (claim) say_about_controller(comp, named, "claimed. A game that is already running may need a restart to see it.",
                "Claimed. A game that is already running may need a restart to see it.");
            else say_about_controller(comp, named, "released.", "Released.");
        }
    }
}

static void try_control_swap(struct compositor *comp) {
    if (comp->device_move_active) return;
    struct control_candidate pair[2];
    enum swap_choice choice = control_swap_pair(comp, pair);
    if (choice == SWAP_UNAVAILABLE) {
        say_in_devices(comp, "Swap needs one connected control for each player.");
        return;
    }
    if (choice == SWAP_NEEDS_PRESS) {
        /* A player holds several controls and none was pressed lately. The button stays; the controller
         * to swap is the one pressed next. */
        say_in_devices(comp, "Press a button on the controller to swap.");
        return;
    }
    comp->device_message[0] = 0;
    snprintf(comp->device_move_key, 256, "%s", pair[0].key);
    snprintf(comp->device_move_other, 256, "%s", pair[1].key);
    snprintf(comp->device_move_label, 256, "Main: %.90s / Guest: %.90s", pair[1].label, pair[0].label);
    comp->device_move_previous = pair[0].route;
    comp->device_move_target = pair[1].route;
    comp->device_move_other_previous = pair[1].route;
    comp->device_move_timer = wl_event_loop_add_timer(wl_display_get_event_loop(comp->display), expire_device_move, comp);
    if (!comp->device_move_timer) return;
    if (wl_event_source_timer_update(comp->device_move_timer, 15000)) {
        wl_event_source_remove(comp->device_move_timer); comp->device_move_timer = NULL; return;
    }
    comp->device_move_active = true;
    comp->pending_shared_action = MODAL_ACTION_KEEP_DEVICE;
    comp->overlay_focus = 5;
    if (!strncmp(pair[0].key, "controller:", 11)) {
        if (!request_controller_assignment(pair[0].key + 11, "guest", pair[1].key + 11, "main", true))
            finish_device_move(comp, false);
    } else {
        /* Same event-loop turn; there is no intervening input dispatch. */
        set_dynamic_device_route(comp, pair[1].key, pair[0].route);
        set_dynamic_device_route(comp, pair[0].key, pair[1].route);
    }
}

static bool handle_management_overlay_click(struct compositor *comp, int x, int y) {
    if (!comp || !comp->management_overlay_visible) {
        return false;
    }
    struct side_ctx *owner = compositor_side_by_id(comp, comp->management_overlay_owner);

    for (int i = comp->modal_region_count - 1; i >= 0; --i) {
        struct modal_region *region = &comp->modal_regions[i];
        if (x < region->x || x >= region->x + region->w ||
                y < region->y || y >= region->y + region->h) {
            continue;
        }

        comp->overlay_focus = i;
        enum modal_action action = region->action;
        if (comp->device_move_active && action != MODAL_ACTION_KEEP_DEVICE &&
                action != MODAL_ACTION_CANCEL_ACTION && action != MODAL_ACTION_CLOSE) return true;
        bool confirmed = action == MODAL_ACTION_CONFIRM;
        bool strength_confirmed = comp->pending_shared_action == MODAL_ACTION_AUDIO_MIX_SEPARATED &&
            (action == MODAL_ACTION_AUDIO_GENTLE || action == MODAL_ACTION_AUDIO_BALANCED || action == MODAL_ACTION_AUDIO_STRONG);
        if (confirmed) {
            action = comp->pending_shared_action;
            comp->pending_shared_action = MODAL_ACTION_NONE;
        }
        if (strength_confirmed) {
            confirmed = true;
            comp->pending_shared_action = MODAL_ACTION_NONE;
        }
        bool shared = action == MODAL_ACTION_SINGLE || action == MODAL_ACTION_SINGLE_GUEST || action == MODAL_ACTION_SINGLE_STANDBY || action == MODAL_ACTION_SINGLE_OFF || action == MODAL_ACTION_SPLIT_VERTICAL ||
            action == MODAL_ACTION_SPLIT_HORIZONTAL || action == MODAL_ACTION_GUEST_LEFT ||
            action == MODAL_ACTION_GUEST_RIGHT || action == MODAL_ACTION_POWER_SLEEP ||
            action == MODAL_ACTION_POWER_RESTART || action == MODAL_ACTION_POWER_SHUTDOWN ||
            action == MODAL_ACTION_POWER_EXIT ||
            action == MODAL_ACTION_RESET_ALL ||
            action == MODAL_ACTION_AUDIO_MIX_OFF || action == MODAL_ACTION_AUDIO_MIX_FULL ||
            action == MODAL_ACTION_AUDIO_MIX_SEPARATED || action == MODAL_ACTION_AUDIO_GENTLE ||
            action == MODAL_ACTION_AUDIO_BALANCED || action == MODAL_ACTION_AUDIO_STRONG;
        if ((shared || action == MODAL_ACTION_DEVICE_LEFT || action == MODAL_ACTION_DEVICE_RIGHT ||
                action == MODAL_ACTION_DEVICE_FOLLOW || action == MODAL_ACTION_DEVICE_BOTH || action == MODAL_ACTION_SWAP_DEVICE ||
                action == MODAL_ACTION_FORGET_DEVICE) &&
                (!owner || owner->role != SIDE_ROLE_PRIMARY)) {
            /* Reject even a stale hit region or pending confirmation from a
             * different overlay owner. Placement belongs to the primary user. */
            comp->pending_shared_action = MODAL_ACTION_NONE;
            write_runtime_state(comp);
            request_redraw(comp);
            return true;
        }
        if ((action == MODAL_ACTION_CLAIM_DEVICE || action == MODAL_ACTION_RELEASE_DEVICE) &&
                (!owner || owner->role == SIDE_ROLE_PRIMARY)) {
            /* These two are Guest's own moves. Main moves controllers with its own buttons, so a stale
             * region from another owner's menu is refused here as well. */
            comp->pending_shared_action = MODAL_ACTION_NONE;
            write_runtime_state(comp);
            request_redraw(comp);
            return true;
        }
        if (action == MODAL_ACTION_FORGET_DEVICE && !confirmed) {
            /* Which controller the confirmation is about: the row that was pressed. */
            const char *key = strncmp(region->device_name, "controller:", 11) == 0 ? region->device_name + 11 : "";
            if (!controller_key_valid(key)) return true;
            snprintf(comp->pending_forget_key, sizeof(comp->pending_forget_key), "%s", key);
            struct json_object *inventory = controller_inventory(), *devices = NULL;
            snprintf(comp->pending_forget_label, sizeof(comp->pending_forget_label), "this controller");
            if (inventory) json_object_object_get_ex(inventory, "devices", &devices);
            for (size_t n = 0; devices && n < json_object_array_length(devices); n++) {
                struct json_object *d = json_object_array_get_idx(devices, n);
                if (!strcmp(controller_text(d, "id"), key)) controller_display_name(d, comp->pending_forget_label, sizeof(comp->pending_forget_label));
            }
            if (inventory) json_object_put(inventory);
        }
        if ((shared || action == MODAL_ACTION_RESET_SIDE || action == MODAL_ACTION_FORGET_DEVICE) && !confirmed &&
                action != MODAL_ACTION_SINGLE_STANDBY) {
            comp->pending_shared_action = action;
            comp->overlay_focus = 5; /* Cancel is the default in confirmation. */
            write_runtime_state(comp);
            request_redraw(comp);
            return true;
        }
        switch (action) {
        case MODAL_ACTION_SWAP_DEVICE:
            try_control_swap(comp);
            break;
        case MODAL_ACTION_KEEP_DEVICE:
            if (owner && owner->role == SIDE_ROLE_PRIMARY) finish_device_move(comp, true);
            break;
        case MODAL_ACTION_CANCEL_ACTION:
            finish_device_move(comp, false);
            comp->pending_shared_action = MODAL_ACTION_NONE;
            break;
        case MODAL_ACTION_TAB_DISPLAY: comp->overlay_tab = 0; comp->pending_shared_action = MODAL_ACTION_NONE; break;
        case MODAL_ACTION_TAB_DEVICES: comp->overlay_tab = 1; comp->pending_shared_action = MODAL_ACTION_NONE; comp->device_message[0] = 0; break;
        case MODAL_ACTION_TAB_SESSION: comp->overlay_tab = 2; comp->pending_shared_action = MODAL_ACTION_NONE; break;
        case MODAL_ACTION_TAB_AUDIO:
            comp->overlay_tab = 3; comp->pending_shared_action = MODAL_ACTION_NONE; audio_start(comp, NULL); break;
        case MODAL_ACTION_AUDIO_OUTPUT: comp->audio_kind = 0; comp->audio_page = 0; break;
        case MODAL_ACTION_AUDIO_INPUT: comp->audio_kind = 1; comp->audio_page = 0; break;
        case MODAL_ACTION_AUDIO_MIX_OFF: audio_request(comp, "off", true); break;
        case MODAL_ACTION_AUDIO_MIX_FULL: audio_request(comp, "full", true); break;
        case MODAL_ACTION_AUDIO_MIX_SEPARATED: audio_request(comp, "separated", true); break;
        case MODAL_ACTION_AUDIO_GENTLE: audio_request(comp, "gentle", true); break;
        case MODAL_ACTION_AUDIO_BALANCED: audio_request(comp, "balanced", true); break;
        case MODAL_ACTION_AUDIO_STRONG: audio_request(comp, "strong", true); break;
        case MODAL_ACTION_AUDIO_JOIN: audio_request(comp, "join", true); break;
        case MODAL_ACTION_AUDIO_LEAVE: audio_request(comp, "leave", true); break;
        case MODAL_ACTION_AUDIO_MUTE: audio_start(comp, "none"); break;
        case MODAL_ACTION_AUDIO_SELECT: audio_start(comp, region->device_name); break;
        case MODAL_ACTION_AUDIO_PREV: if (comp->audio_page > 0) comp->audio_page--; break;
        case MODAL_ACTION_AUDIO_NEXT: if (comp->audio_page < 127) comp->audio_page++; break;
        case MODAL_ACTION_DEVICE_PREV: {
            int *page = owner_device_page(comp);
            if (*page > 0) (*page)--;
            break;
        }
        case MODAL_ACTION_DEVICE_NEXT: {
            int *page = owner_device_page(comp);
            if (*page < 64) (*page)++;
            break;
        }
        case MODAL_ACTION_RESOLUTION_16_9: set_side_game_aspect(owner, GAME_RESOLUTION_16_9); break;
        case MODAL_ACTION_RESOLUTION_4_3: set_side_game_aspect(owner, GAME_RESOLUTION_4_3); break;
        case MODAL_ACTION_RESOLUTION_1_1: set_side_game_aspect(owner, GAME_RESOLUTION_1_1); break;
        case MODAL_ACTION_FPS_OFF: perf_set_mode(comp, owner, FPS_MODE_OFF); break;
        case MODAL_ACTION_FPS_ON: perf_set_mode(comp, owner, FPS_MODE_FPS); break;
        case MODAL_ACTION_FPS_FRAMETIME: perf_set_mode(comp, owner, FPS_MODE_FRAMETIME); break;
        case MODAL_ACTION_PERF_RECORD: if (owner) perf_set_recording(comp, owner, !owner->perf.recording); break;
        case MODAL_ACTION_BRIGHTNESS_DOWN:
        case MODAL_ACTION_BRIGHTNESS_UP:
            if (owner && owner->role == SIDE_ROLE_PRIMARY) screen_brightness_step(comp, action == MODAL_ACTION_BRIGHTNESS_UP ? 1 : -1);
            break;
        case MODAL_ACTION_DISPLAY_APPLY: apply_side_display(owner); break;
        case MODAL_ACTION_DISPLAY_CANCEL: cancel_side_display(owner); break;
        case MODAL_ACTION_CLOSE:
            close_management_overlay(comp);
            write_runtime_state(comp);
            break;
        case MODAL_ACTION_SINGLE:
        case MODAL_ACTION_SINGLE_STANDBY:
        case MODAL_ACTION_SINGLE_OFF:
            comp->pending_shared_action = MODAL_ACTION_NONE;
            show_main_for_layout(comp);
            if (guest_side(comp)) {
                suspend_overlay_app_input(comp, guest_side(comp));
                guest_side(comp)->activity = action == MODAL_ACTION_SINGLE_OFF ? 2 : 1;
            }
            comp->layout = LAYOUT_SINGLE;
            comp->output_scale_mode = OUTPUT_SCALE_FIT;
            apply_layout(comp);
            break;
        case MODAL_ACTION_SINGLE_GUEST:
            enter_guest_full_screen(comp);
            break;
        case MODAL_ACTION_SPLIT_VERTICAL:
            show_main_for_layout(comp);
            show_guest_for_split(comp);
            comp->layout = LAYOUT_SPLIT_VERTICAL;
            comp->output_scale_mode = OUTPUT_SCALE_FIT;
            apply_layout(comp);
            break;
        case MODAL_ACTION_SPLIT_HORIZONTAL:
            show_main_for_layout(comp);
            show_guest_for_split(comp);
            comp->layout = LAYOUT_SPLIT_HORIZONTAL;
            comp->output_scale_mode = OUTPUT_SCALE_FIT;
            apply_layout(comp);
            break;
        case MODAL_ACTION_GUEST_LEFT:
            set_guest_side(comp, SIDE_LEFT);
            apply_layout(comp);
            break;
        case MODAL_ACTION_GUEST_RIGHT:
            set_guest_side(comp, SIDE_RIGHT);
            apply_layout(comp);
            break;
        case MODAL_ACTION_SCALE_FIT:
            if (owner) {
                set_side_scale_mode(owner, OUTPUT_SCALE_FIT);
            }
            break;
        case MODAL_ACTION_SCALE_STRETCH:
            if (owner) {
                set_side_scale_mode(owner, OUTPUT_SCALE_STRETCH);
            }
            break;
        case MODAL_ACTION_SCALE_ZOOM:
            if (owner) {
                set_side_scale_mode(owner, OUTPUT_SCALE_ZOOM);
            }
            break;
        case MODAL_ACTION_SCALE_NATIVE:
            if (owner) {
                set_side_game_aspect(owner, GAME_RESOLUTION_NATIVE);
            }
            break;
        case MODAL_ACTION_DEVICE_FOLLOW:
            try_device_move(comp, region->device_name, INPUT_ROUTE_FOLLOW);
            break;
        case MODAL_ACTION_DEVICE_LEFT:
            try_device_move(comp, region->device_name, main_input_route_mode(comp));
            break;
        case MODAL_ACTION_DEVICE_BOTH:
            try_device_move(comp, region->device_name, INPUT_ROUTE_BOTH);
            break;
        case MODAL_ACTION_DEVICE_RIGHT:
            try_device_move(comp, region->device_name, guest_input_route_mode(comp));
            break;
        case MODAL_ACTION_FORGET_DEVICE:
            comp->pending_shared_action = MODAL_ACTION_NONE;
            if (owner && owner->role == SIDE_ROLE_PRIMARY) forget_controller(comp, comp->pending_forget_key);
            comp->pending_forget_key[0] = 0;
            break;
        case MODAL_ACTION_CLAIM_DEVICE:
        case MODAL_ACTION_RELEASE_DEVICE:
            claim_or_release_device(comp, region->device_name, action == MODAL_ACTION_CLAIM_DEVICE);
            break;
        case MODAL_ACTION_RESET_SIDE:
            /* Reset is a restart request: the restart token for a contract
             * version 2 supervisor, plus the revision bump that a version 1
             * supervisor restarts on (the one bump that is not a display
             * change). */
            if (owner) {
                if (!request_side_restart(side_name(owner->id))) {
                    wlr_log(WLR_ERROR, "Reset: restart request for %s side failed: %s",
                        side_name(owner->id), strerror(errno));
                }
                owner->display_revision++;
            }
            comp->management_overlay_visible = false;
            side_ensure_app_keyboard_focus(comp, owner);
            break;
        case MODAL_ACTION_RESET_ALL:
            if (!request_side_restart("left") || !request_side_restart("right")) {
                wlr_log(WLR_ERROR, "Reset both: restart request failed: %s", strerror(errno));
            }
            comp->left.display_revision++;
            comp->right.display_revision++;
            comp->management_overlay_visible = false;
            side_ensure_app_keyboard_focus(comp, &comp->left);
            side_ensure_app_keyboard_focus(comp, &comp->right);
            break;
        case MODAL_ACTION_POWER_SLEEP:
        case MODAL_ACTION_POWER_RESTART:
        case MODAL_ACTION_POWER_SHUTDOWN:
        case MODAL_ACTION_POWER_EXIT:
            /* Restart, Shut down and Turn off Dosplex leave power_action set: the full-screen panel
             * replaces the overlay until the helper's own unit has stopped this compositor. When
             * nothing was started, the Session tab stays open and says why. */
            if (launch_power_action(comp, action == MODAL_ACTION_POWER_SLEEP ? "suspend" :
                    action == MODAL_ACTION_POWER_RESTART ? "reboot" :
                    action == MODAL_ACTION_POWER_EXIT ? "stop" : "poweroff")) {
                close_management_overlay(comp);
            } else {
                comp->overlay_tab = 2;
            }
            break;
        case MODAL_ACTION_HELP:
            launch_panel(comp, owner, "help", comp->overlay_tab == 0 ? "display" : comp->overlay_tab == 1 ? "devices" :
                comp->overlay_tab == 3 ? "audio" : "session");
            break;
        case MODAL_ACTION_FEEDBACK:
            launch_panel(comp, owner, "feedback", "menu");
            break;
        case MODAL_ACTION_NONE:
        default:
            break;
        }

        write_runtime_state(comp);
        request_redraw(comp);
        return true;
    }

    /* Clicking panel padding must not dismiss the menu unexpectedly. */
    return true;
}

static bool handle_overlay_key(struct compositor *comp, struct side_ctx *target,
        uint32_t keycode, uint32_t state, bool shift) {
    if (!comp->management_overlay_visible || !target ||
            target->id != comp->management_overlay_owner) return false;
    if (panel_key(comp, target, keycode, state)) return true;
    if (state != WL_KEYBOARD_KEY_STATE_PRESSED) return true;
    int count = comp->modal_region_count;
    if (keycode == KEY_ESC) {
        finish_device_move(comp, false);
        if (comp->pending_shared_action != MODAL_ACTION_NONE) {
            comp->pending_shared_action = MODAL_ACTION_NONE;
            comp->overlay_focus = 3;
        } else {
            close_management_overlay(comp);
        }
        write_runtime_state(comp);
    } else if (count > 0) {
        if (comp->overlay_focus < 0 || comp->overlay_focus >= count) comp->overlay_focus = 0;
        if (keycode == KEY_TAB) {
            comp->overlay_focus = (comp->overlay_focus + (shift ? count - 1 : 1)) % count;
        } else if (keycode == KEY_LEFT || keycode == KEY_RIGHT || keycode == KEY_UP || keycode == KEY_DOWN) {
            struct modal_region *from = &comp->modal_regions[comp->overlay_focus];
            int fx = from->x + from->w / 2, fy = from->y + from->h / 2;
            long best = 0x7fffffff;
            int selected = comp->overlay_focus;
            for (int i = 0; i < count; i++) {
                struct modal_region *to = &comp->modal_regions[i];
                int dx = to->x + to->w / 2 - fx, dy = to->y + to->h / 2 - fy;
                int forward = keycode == KEY_LEFT ? -dx : keycode == KEY_RIGHT ? dx : keycode == KEY_UP ? -dy : dy;
                /* Prefer controls whose span crosses the navigation axis.
                 * Center-only distances skip wide buttons between rows of
                 * narrow buttons (notably Main placement in Session). */
                int cross = (keycode == KEY_LEFT || keycode == KEY_RIGHT) ?
                    (fy < to->y ? to->y - fy : fy > to->y + to->h ? fy - to->y - to->h : 0) :
                    (fx < to->x ? to->x - fx : fx > to->x + to->w ? fx - to->x - to->w : 0);
                if (forward <= 0) continue;
                long distance = (cross ? 100000L : 0) + forward + 4L * cross;
                if (distance < best) { best = distance; selected = i; }
            }
            comp->overlay_focus = selected;
        } else if (keycode == KEY_ENTER || keycode == KEY_KPENTER || keycode == KEY_SPACE) {
            struct modal_region region = comp->modal_regions[comp->overlay_focus];
            handle_management_overlay_click(comp, region.x + region.w / 2, region.y + region.h / 2);
        }
    }
    request_redraw(comp);
    return true;
}

static struct side_ctx *side_for_input_route(struct compositor *comp,
        enum input_route_mode mode) {
    if (!comp) return NULL;
    /* LEFT/RIGHT are stable session IDs in device assignments, matching IPC
     * state and account ownership. Presentation coordinates can change without
     * transferring an assigned keyboard/controller to the other account. */
    if (mode == INPUT_ROUTE_LEFT) return &comp->left;
    if (mode == INPUT_ROUTE_RIGHT) return &comp->right;
    return side_at_point(comp, comp->cursor_x, comp->cursor_y);
}

static struct side_ctx *resolve_input_target(struct compositor *comp, struct input_device_ctx *input) {
    if (!comp || !input) return NULL;
    if (comp->layout == LAYOUT_SINGLE) {
        return primary_side(comp);
    }

    enum input_route_mode mode = current_route_mode_for_device(comp, input);
    if (mode == INPUT_ROUTE_FOLLOW || mode == INPUT_ROUTE_BOTH) {
        struct side_ctx *target = side_at_point(comp, comp->cursor_x, comp->cursor_y);
        return target ? target : primary_side(comp);
    }
    if (mode == INPUT_ROUTE_LEFT) {
        return side_for_input_route(comp, mode);
    }
    if (mode == INPUT_ROUTE_RIGHT) {
        return side_for_input_route(comp, mode);
    }
    return side_for_role(comp, input->target_role);
}

/* DEV_SCENE <left|right> (--test-hooks): where the scene has put the side's toplevels' surfaces. One INFO line for
 * each scene buffer of a surface in the side's windows, in the order of the scene (bottom to top), then one that
 * closes the list:
 *   DEV_SCENE left surface=26 x=0 y=0 width=3840 height=2160 logical_width=1920 logical_height=1080 enabled=1
 *       buffer=1 visible_px=8294400
 * surface is the id of the client's wl_surface; x and y are the layout position of the buffer's corner and width and
 * height what the scene draws (its destination, else its buffer), logical_width and logical_height what the surface
 * says it is; enabled is whether the node and all its parents are, buffer whether it has a buffer, and visible_px
 * the area of it that nothing opaque covers. */
static void dev_scene_report_node(const struct side_ctx *side, struct wlr_scene_node *node, unsigned *count) {
    if (node->type == WLR_SCENE_NODE_TREE) {
        struct wlr_scene_node *child;
        wl_list_for_each(child, &wlr_scene_tree_from_node(node)->children, link) {
            dev_scene_report_node(side, child, count);
        }
        return;
    }
    if (node->type != WLR_SCENE_NODE_BUFFER) return;
    struct wlr_scene_buffer *buffer = wlr_scene_buffer_from_node(node);
    struct wlr_scene_surface *scene_surface = wlr_scene_surface_try_from_buffer(buffer);
    if (!scene_surface) return;
    int lx = 0, ly = 0;
    bool enabled = wlr_scene_node_coords(node, &lx, &ly);
    int width = buffer->dst_width, height = buffer->dst_height;
    if (width <= 0 || height <= 0) {
        width = buffer->buffer ? buffer->buffer->width : 0;
        height = buffer->buffer ? buffer->buffer->height : 0;
    }
    uint64_t visible = 0;
    int rects = 0;
    const pixman_box32_t *box = pixman_region32_rectangles(&node->visible, &rects);
    for (int i = 0; i < rects; i++) {
        visible += (uint64_t)(box[i].x2 - box[i].x1) * (uint64_t)(box[i].y2 - box[i].y1);
    }
    wlr_log(WLR_INFO, "DEV_SCENE %s surface=%u x=%d y=%d width=%d height=%d logical_width=%d logical_height=%d"
        " enabled=%d buffer=%d visible_px=%lu", side_name(side->id),
        (unsigned)wl_resource_get_id(scene_surface->surface->resource), lx, ly, width, height,
        scene_surface->surface->current.width, scene_surface->surface->current.height,
        enabled ? 1 : 0, buffer->buffer ? 1 : 0, (unsigned long)visible);
    (*count)++;
}

static void dev_scene_report(struct side_ctx *side) {
    unsigned count = 0;
    struct side_xdg_window *window;
    side_init_xdg_windows(side);
    wl_list_for_each(window, &side->xdg_windows, link) {
        if (window->scene_tree) dev_scene_report_node(side, &window->scene_tree->node, &count);
    }
    wlr_log(WLR_INFO, "DEV_SCENE %s end surfaces=%u", side_name(side->id), count);
}

/* Apply one of the four layouts. The LAYOUT hud-ipc command and the control socket both end here. */
static bool apply_layout_request(struct compositor *comp, enum layout_mode layout) {
    if (layout == LAYOUT_SINGLE) {
        show_main_for_layout(comp);
        comp->layout = LAYOUT_SINGLE;
    } else if (layout == LAYOUT_SINGLE_GUEST) {
        enter_guest_full_screen(comp);
        return true;
    } else if (layout == LAYOUT_SPLIT_VERTICAL || layout == LAYOUT_SPLIT_HORIZONTAL) {
        show_main_for_layout(comp);
        show_guest_for_split(comp);
        comp->layout = layout;
    } else {
        return false;
    }
    comp->output_scale_mode = OUTPUT_SCALE_FIT;
    apply_layout(comp);
    request_redraw(comp);
    return true;
}

/* The control socket: the one production way for tools (the dosplex program, and through it any UI) to
 * give the compositor an order. It is a SOCK_SEQPACKET socket whose requests are fixed 8-byte records, never
 * text, and it answers each with a fixed 8-byte record. Only the compositor's own user (root on a device)
 * is served: the socket is mode 0600 in the root-only runtime directory, and the peer credentials of every
 * connection are checked as well, so a leaked descriptor or a loosened directory does not open it.
 *
 *   request: 'D' 'P' 'X' 'C'  version  command  argument  0
 *   reply:   'D' 'P' 'X' 'R'  version  status   value     0
 *
 * version is 1. command 1 = SET_LAYOUT, argument 1 single, 2 vertical, 3 horizontal, 4 guest (Guest full screen); the reply value is
 * the layout now in effect (same numbers). status 0 ok, 1 bad request, 2 denied, 3 unsupported version.
 * command 2 = SET_BRIGHTNESS, argument 0..100 (a percent; 0 means the minimum, 5 %): the reply value is the percent
 * now in effect. command 3 = GET_BRIGHTNESS, argument 0: the reply value is the percent now. Both answer status 4
 * (unavailable) when the machine has no backlight, and for those two commands the value is not a layout number.
 * New commands get new numbers; a change of an existing record's meaning bumps the version. */
#define CONTROL_SOCKET_NAME "control.sock"
#define CONTROL_VERSION 1
#define CONTROL_CMD_SET_LAYOUT 1
#define CONTROL_CMD_SET_BRIGHTNESS 2
#define CONTROL_CMD_GET_BRIGHTNESS 3
enum { CONTROL_OK = 0, CONTROL_BAD_REQUEST = 1, CONTROL_DENIED = 2, CONTROL_BAD_VERSION = 3, CONTROL_UNAVAILABLE = 4 };

static int control_layout_number(enum layout_mode layout) {
    return layout == LAYOUT_SINGLE ? 1 : layout == LAYOUT_SPLIT_VERTICAL ? 2 :
        layout == LAYOUT_SINGLE_GUEST ? 4 : 3;
}

static int handle_control_readable(int fd, uint32_t mask, void *data) {
    struct compositor *comp = data;
    int client = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (client < 0) return 1;
    unsigned char request[16], reply[8] = {'D', 'P', 'X', 'R', CONTROL_VERSION, CONTROL_BAD_REQUEST, 0, 0};
    bool brightness_reply = false;
    struct ucred peer;
    socklen_t peer_length = sizeof(peer);
    if (getsockopt(client, SOL_SOCKET, SO_PEERCRED, &peer, &peer_length) != 0 || peer_length != sizeof(peer) ||
            peer.uid != geteuid()) {
        reply[5] = CONTROL_DENIED;
        wlr_log(WLR_ERROR, "Control socket: refused a connection from uid %d", peer_length == sizeof(peer) ? (int)peer.uid : -1);
    } else {
        /* The client sends its record right after connecting; do not wait for a slow one. */
        struct pollfd wait = {.fd = client, .events = POLLIN};
        ssize_t length = poll(&wait, 1, 200) == 1 ? recv(client, request, sizeof(request), MSG_DONTWAIT) : -1;
        if (length != 8 || memcmp(request, "DPXC", 4) != 0 || request[7] != 0) {
            /* leave BAD_REQUEST */
        } else if (request[4] != CONTROL_VERSION) {
            reply[5] = CONTROL_BAD_VERSION;
        } else if (request[5] == CONTROL_CMD_SET_LAYOUT && request[6] >= 1 && request[6] <= 4) {
            enum layout_mode layout = request[6] == 1 ? LAYOUT_SINGLE :
                request[6] == 2 ? LAYOUT_SPLIT_VERTICAL :
                request[6] == 4 ? LAYOUT_SINGLE_GUEST : LAYOUT_SPLIT_HORIZONTAL;
            if (apply_layout_request(comp, layout)) {
                reply[5] = CONTROL_OK;
                wlr_log(WLR_INFO, "Control socket: layout %s", layout_mode_name(layout));
            }
        } else if (request[5] == CONTROL_CMD_SET_BRIGHTNESS && request[6] <= 100) {
            brightness_reply = true;
            int percent = screen_brightness_set(comp, request[6], true);
            reply[5] = percent < 0 ? CONTROL_UNAVAILABLE : CONTROL_OK;
            reply[6] = percent < 0 ? 0 : (unsigned char)percent;
            wlr_log(WLR_INFO, "Control socket: brightness %d -> %d", request[6], percent);
        } else if (request[5] == CONTROL_CMD_GET_BRIGHTNESS && request[6] == 0) {
            brightness_reply = true;
            reply[5] = comp->screen_bl_available ? CONTROL_OK : CONTROL_UNAVAILABLE;
            reply[6] = comp->screen_bl_available ? (unsigned char)comp->screen_bl_percent : 0;
        }
    }
    if (!brightness_reply) reply[6] = (unsigned char)control_layout_number(comp->layout);
    (void)!send(client, reply, sizeof(reply), MSG_NOSIGNAL | MSG_DONTWAIT);
    close(client);
    return 1;
}

static void init_control_socket(struct compositor *comp) {
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) {
        wlr_log_errno(WLR_ERROR, "Failed to create the control socket");
        return;
    }
    const char *path = getenv("DOSPLEX_CONTROL_SOCK");
    char path_buf[sizeof(comp->control_socket_path)];
    if (!path || !path[0]) {
        const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
        if (!runtime_dir || !runtime_dir[0]) runtime_dir = "/run/dosplex";
        if (snprintf(path_buf, sizeof(path_buf), "%s/%s", runtime_dir, CONTROL_SOCKET_NAME) >= (int)sizeof(path_buf)) {
            close(fd);
            return;
        }
        path = path_buf;
    }
    struct sockaddr_un addr = {0};
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof(addr.sun_path)) {
        wlr_log(WLR_ERROR, "Control socket path is too long");
        close(fd);
        return;
    }
    strcpy(addr.sun_path, path);
    unlink(addr.sun_path);
    mode_t old_mask = umask(0077);          /* born 0600: no window in which another user can connect */
    int bound = bind(fd, (struct sockaddr *)&addr, sizeof(addr));
    umask(old_mask);
    if (bound < 0 || chmod(addr.sun_path, 0600) < 0 || listen(fd, 4) < 0) {
        wlr_log_errno(WLR_ERROR, "Failed to set up the control socket");
        close(fd);
        unlink(addr.sun_path);
        return;
    }
    snprintf(comp->control_socket_path, sizeof(comp->control_socket_path), "%s", addr.sun_path);
    comp->control_server_fd = fd;
    comp->control_server_source = wl_event_loop_add_fd(
        wl_display_get_event_loop(comp->display), fd, WL_EVENT_READABLE, handle_control_readable, comp);
}

static int handle_hud_ipc_readable(int fd, uint32_t mask, void *data) {
    struct compositor *comp = data;
    char buf[512];
    struct sockaddr_un peer_addr;
    socklen_t peer_addr_len = sizeof(peer_addr);

    ssize_t len = recvfrom(fd, buf, sizeof(buf) - 1, 0,
                           (struct sockaddr *)&peer_addr, &peer_addr_len);
    if (len <= 0) return 1;

    buf[len] = '\0';
    for (int i = 0; i < len; i++) {
        if (buf[i] == '\r' || buf[i] == '\n') {
            buf[i] = '\0';
            break;
        }
    }

    wlr_log(WLR_DEBUG, "HUD IPC received: %s", buf);

    if (!comp->config.test_hooks &&
            (strncmp(buf, "DEV_FRAME ", 10) == 0 || strncmp(buf, "DEV_OUTPUT_", 11) == 0 ||
             strncmp(buf, "DEV_SIDE_SCALE ", 15) == 0 || strncmp(buf, "DEV_SIDE_DISPLAY ", 17) == 0 ||
             strcmp(buf, "DEV_STATS") == 0 || strncmp(buf, "DEV_SCENE ", 10) == 0 ||
             strncmp(buf, "DEV_PERF ", 9) == 0)) {
        /* A tool that lost its hook would otherwise only time out. */
        wlr_log(WLR_ERROR, "Ignoring %.*s: the compositor was not started with --test-hooks",
            (int)strcspn(buf, " "), buf);
        return 1;
    }

    /* DEV_SIDE_SCALE <left|right> <milli>: what an active Desktop session's
     * session.json "desktop_scale_milli" does in production (1000 ends it). */
    if (comp->config.test_hooks && strncmp(buf, "DEV_SIDE_SCALE ", 15) == 0) {
        char side_text[8], extra;
        int milli;
        if (sscanf(buf + 15, "%7s %d %c", side_text, &milli, &extra) == 2 &&
                (strcmp(side_text, "left") == 0 || strcmp(side_text, "right") == 0)) {
            side_set_desktop_scale(strcmp(side_text, "left") == 0 ? &comp->left : &comp->right, milli);
            request_redraw(comp);
        }
        return 1;
    }

    /* DEV_SIDE_DISPLAY <left|right> <revision> <width> <height> <applied|failed|deferred>: what the supervisor's
     * report in session.json does in production (display_applied_revision, display_applied_size and display_result).
     * A size of 0 0 is a report that has none. */
    if (comp->config.test_hooks && strncmp(buf, "DEV_SIDE_DISPLAY ", 17) == 0) {
        char side_text[8], result_text[16], extra;
        unsigned revision;
        int width, height;
        if (sscanf(buf + 17, "%7s %u %d %d %15s %c", side_text, &revision, &width, &height, result_text, &extra) == 5 &&
                (strcmp(side_text, "left") == 0 || strcmp(side_text, "right") == 0) &&
                width >= 0 && width <= 16384 && height >= 0 && height <= 16384 &&
                display_result_parse(result_text) != DISPLAY_RESULT_NONE) {
            struct session_snapshot report = {0};
            report.display_applied_known = true;
            report.display_applied_revision = revision;
            report.display_applied_width = width;
            report.display_applied_height = height;
            report.display_result = display_result_parse(result_text);
            side_take_display_report(strcmp(side_text, "left") == 0 ? &comp->left : &comp->right, &report);
            write_runtime_state(comp);
            request_redraw(comp);
        }
        return 1;
    }

    /* DEV_STATS: one INFO line with the scene counters, for the tests that watch what a stream of
     * client commits costs: frames handled, frames the scene committed, overlay rebuilds, output pixels
     * damaged, commit time, the visible area of the overlay's background rect and the sides' commits. */
    if (comp->config.test_hooks && strcmp(buf, "DEV_STATS") == 0) {
        uint64_t background_visible = 0;
        pixman_box32_t extents = {0};
        if (comp->background_rect) {
            int rects = 0;
            const pixman_box32_t *box = pixman_region32_rectangles(&comp->background_rect->node.visible, &rects);
            for (int i = 0; i < rects; i++) {
                background_visible += (uint64_t)(box[i].x2 - box[i].x1) * (uint64_t)(box[i].y2 - box[i].y1);
            }
            if (rects > 0) extents = *pixman_region32_extents(&comp->background_rect->node.visible);
        }
        wlr_log(WLR_INFO, "DEV_STATS frames=%lu rendered=%lu rebuilds=%lu damage_px=%lu render_us=%lu"
            " background_visible_px=%lu background_visible_x1=%d background_visible_y1=%d"
            " background_visible_x2=%d background_visible_y2=%d left_commits=%d right_commits=%d",
            (unsigned long)comp->frame_count, (unsigned long)comp->frames_rendered,
            (unsigned long)comp->scene_rebuilds, (unsigned long)comp->damage_pixels,
            (unsigned long)comp->render_us, (unsigned long)background_visible,
            extents.x1, extents.y1, extents.x2, extents.y2,
            comp->left.commit_count, comp->right.commit_count);
        return 1;
    }

    /* DEV_PERF <left|right>: one INFO line with the side's frame counting and the counter's text, for the tests. */
    if (comp->config.test_hooks && strncmp(buf, "DEV_PERF ", 9) == 0) {
        struct side_ctx *side = strcmp(buf + 9, "left") == 0 ? &comp->left :
            strcmp(buf + 9, "right") == 0 ? &comp->right : NULL;
        if (side) {
            uint64_t now = perf_now_ns();
            struct perf_stats stats;
            char text[96];
            perf_compute(&side->perf, now, PERF_SECOND_NS, &stats);
            perf_text(side, now, text, sizeof(text));
            wlr_log(WLR_INFO, "DEV_PERF %s total=%lu frames=%u fps=%.2f avg_ms=%.2f p99_ms=%.2f last_ms=%.2f mode=%s"
                " drawn=%d text=%s", side_name(side->id), (unsigned long)side->perf.total, stats.frames, stats.fps,
                stats.avg_ms, stats.p99_ms, stats.last_ms, fps_mode_name(side->perf.mode),
                comp->perf_tree && !wl_list_empty(&comp->perf_tree->children), text);
        }
        return 1;
    }

    /* BRIGHTNESS <percent> (--test-hooks): what the control socket's brightness command does. */
    if (comp->config.test_hooks && strncmp(buf, "BRIGHTNESS ", 11) == 0) {
        int percent;
        char extra;
        if (sscanf(buf + 11, "%d %c", &percent, &extra) == 1 && percent >= 0 && percent <= 100)
            screen_brightness_set(comp, percent, true);
        return 1;
    }

    /* FPS <left|right> <off|fps|fps+frametime> and RECORD <left|right> <on|off>: what the Display tab's buttons do. */
    if (strncmp(buf, "FPS ", 4) == 0 || strncmp(buf, "RECORD ", 7) == 0) {
        bool fps = buf[0] == 'F';
        char side_text[16], value[16], extra;
        if (sscanf(buf + (fps ? 4 : 7), "%15s %15s %c", side_text, value, &extra) != 2) return 1;
        struct side_ctx *target = strcmp(side_text, "left") == 0 ? &comp->left :
            strcmp(side_text, "right") == 0 ? &comp->right : NULL;
        enum fps_mode mode;
        if (!target) return 1;
        if (fps && fps_mode_parse(value, &mode)) perf_set_mode(comp, target, mode);
        else if (!fps && (!strcmp(value, "on") || !strcmp(value, "off"))) perf_set_recording(comp, target, !strcmp(value, "on"));
        return 1;
    }

    if (comp->config.test_hooks && strncmp(buf, "DEV_SCENE ", 10) == 0) {
        struct side_ctx *side = strcmp(buf + 10, "left") == 0 ? &comp->left :
            strcmp(buf + 10, "right") == 0 ? &comp->right : NULL;
        if (side) dev_scene_report(side);
        return 1;
    }

    if (comp->config.test_hooks && strncmp(buf, "DEV_FRAME ", 10) == 0) {
        char *end = NULL;
        errno = 0;
        unsigned long token = strtoul(buf + 10, &end, 10);
        if (!errno && end != buf + 10 && *end == '\0' && token > 0 && token <= UINT32_MAX) {
            comp->capture_requested = (uint32_t)token;
            request_redraw(comp);
        }
        return 1;
    }

    if (comp->config.test_hooks && comp->test_output_backend && strcmp(buf, "DEV_OUTPUT_REMOVE") == 0) {
        if (comp->output) wlr_output_destroy(comp->output);
        return 1;
    }
    if (comp->config.test_hooks && comp->test_output_backend && strncmp(buf, "DEV_OUTPUT_ADD ", 15) == 0) {
        int width, height;
        char extra;
        if (!comp->output && sscanf(buf + 15, "%d %d %c", &width, &height, &extra) == 2 &&
                width >= 320 && width <= 7680 && height >= 320 && height <= 4320) {
            wlr_headless_add_output(comp->test_output_backend, width, height);
        }
        return 1;
    }

    /* CHORD <left|right>: a player's overlay chord, exactly as the controller relays send it
     * (MENU <side> toggle). OVERLAY <side> below opens (and never closes) the overlay under the same
     * precedence. */
    if (strncmp(buf, "CHORD ", 6) == 0) {
        if (!comp->config.test_hooks) {
            wlr_log(WLR_ERROR, "Ignoring CHORD: the compositor was not started with --test-hooks");
            return 1;
        }
        struct side_ctx *target = strcmp(buf + 6, "left") == 0 ? &comp->left :
            strcmp(buf + 6, "right") == 0 ? &comp->right : NULL;
        if (target) request_overlay_chord(comp, target, "CHORD test hook");
        return 1;
    }

    if (strncmp(buf, "MENU ", 5) == 0) {
        char side[16], action[16], extra;
        if (sscanf(buf + 5, "%15s %15s %c", side, action, &extra) != 2) return 1;
        struct side_ctx *target = strcmp(side, "left") == 0 ? &comp->left :
            strcmp(side, "right") == 0 ? &comp->right : NULL;
        if (!target) return 1;
        /* The chord decides who gets the overlay (Main takes it from Guest, never the reverse). */
        if (strcmp(action, "toggle") == 0) {
            request_overlay_chord(comp, target, "controller");
            return 1;
        }
        if (!side_may_use_overlay(comp, target)) return 1;
        /* Every other controller command acts only on its own player's open menu. */
        if (comp->management_overlay_visible && comp->management_overlay_owner != target->id) return 1;
        uint32_t key = 0;
        if (strcmp(action, "up") == 0) key = KEY_UP;
        else if (strcmp(action, "down") == 0) key = KEY_DOWN;
        else if (strcmp(action, "left") == 0) key = KEY_LEFT;
        else if (strcmp(action, "right") == 0) key = KEY_RIGHT;
        else if (strcmp(action, "select") == 0) key = KEY_ENTER;
        else if (strcmp(action, "back") == 0) key = KEY_ESC;
        else if (strcmp(action, "next") == 0 || strcmp(action, "previous") == 0) key = KEY_TAB;
        if (key) {
            /* A controller sends presses only. The menu acts on a press; the panel is a program and needs the
             * release too, or its key would be held and repeat. */
            bool to_panel = comp->panel_pid > 0 && comp->panel_side == target->id;
            handle_overlay_key(comp, target, key, WL_KEYBOARD_KEY_STATE_PRESSED, strcmp(action, "previous") == 0);
            if (to_panel) handle_overlay_key(comp, target, key, WL_KEYBOARD_KEY_STATE_RELEASED, false);
        }
        return 1;
    }

    if (strncmp(buf, "LAYOUT ", 7) == 0) {
        char *layout = buf + 7;
        if (strcmp(layout, "single") == 0 || strcmp(layout, "no-split") == 0) {
            apply_layout_request(comp, LAYOUT_SINGLE);
        } else if (strcmp(layout, "guest") == 0) {
            apply_layout_request(comp, LAYOUT_SINGLE_GUEST);
        } else if (strcmp(layout, "vertical") == 0) {
            apply_layout_request(comp, LAYOUT_SPLIT_VERTICAL);
        } else if (strcmp(layout, "horizontal") == 0) {
            apply_layout_request(comp, LAYOUT_SPLIT_HORIZONTAL);
        } else {
            wlr_log(WLR_ERROR, "Invalid layout in LAYOUT command: %s", layout);
            request_redraw(comp);
        }
    } else if (strncmp(buf, "GUEST ", 6) == 0) {
        char *side = buf + 6;
        if (strcmp(side, "left") == 0 || strcmp(side, "top") == 0 || strcmp(side, "up") == 0) {
            set_guest_side(comp, SIDE_LEFT);
            apply_layout(comp);
        } else if (strcmp(side, "right") == 0 || strcmp(side, "bottom") == 0 || strcmp(side, "down") == 0) {
            set_guest_side(comp, SIDE_RIGHT);
            apply_layout(comp);
        } else {
            wlr_log(WLR_ERROR, "Invalid side in GUEST command: %s", side);
        }
        request_redraw(comp);
    } else if (strncmp(buf, "RESOLUTION ", 11) == 0) {
        char side[16], preset[16], extra;
        if (sscanf(buf + 11, "%15s %15s %c", side, preset, &extra) != 2) return 1;
        struct side_ctx *target = strcmp(side, "left") == 0 ? &comp->left :
            strcmp(side, "right") == 0 ? &comp->right : NULL;
        if (!target) return 1;
        if (strcmp(preset, "native") == 0) set_side_game_aspect(target, GAME_RESOLUTION_NATIVE);
        else if (strcmp(preset, "720p") == 0) set_side_game_resolution(target, 1280, 720);
        else if (strcmp(preset, "1080p") == 0) set_side_game_resolution(target, 1920, 1080);
    } else if (strncmp(buf, "ASPECT ", 7) == 0) {
        char side[16], preset[16], extra;
        if (sscanf(buf + 7, "%15s %15s %c", side, preset, &extra) != 2) return 1;
        struct side_ctx *target = strcmp(side, "left") == 0 ? &comp->left :
            strcmp(side, "right") == 0 ? &comp->right : NULL;
        if (!target) return 1;
        if (strcmp(preset, "native") == 0) set_side_game_aspect(target, GAME_RESOLUTION_NATIVE);
        else if (strcmp(preset, "16:9") == 0) set_side_game_aspect(target, GAME_RESOLUTION_16_9);
        else if (strcmp(preset, "4:3") == 0) set_side_game_aspect(target, GAME_RESOLUTION_4_3);
        else if (strcmp(preset, "1:1") == 0) set_side_game_aspect(target, GAME_RESOLUTION_1_1);
    } else if (strncmp(buf, "DISPLAY_APPLY ", 14) == 0 ||
               strncmp(buf, "DISPLAY_CANCEL ", 15) == 0) {
        bool apply = strncmp(buf, "DISPLAY_APPLY ", 14) == 0;
        const char *side = buf + (apply ? 14 : 15);
        struct side_ctx *target = strcmp(side, "left") == 0 ? &comp->left :
            strcmp(side, "right") == 0 ? &comp->right : NULL;
        if (apply) apply_side_display(target);
        else cancel_side_display(target);
    } else if (strncmp(buf, "SCALE ", 6) == 0) {
        char *arg = buf + 6;
        char *first = strtok(arg, " ");
        char *second = strtok(NULL, " ");
        struct side_ctx *target = compositor_side_by_id(comp, comp->management_overlay_owner);
        char *mode_str = first;

        if (first && second) {
            if (strcmp(first, "left") == 0 || strcmp(first, "top") == 0) {
                target = &comp->left;
                mode_str = second;
            } else if (strcmp(first, "right") == 0 || strcmp(first, "bottom") == 0) {
                target = &comp->right;
                mode_str = second;
            }
        }

        enum output_scale_mode mode = parse_output_scale_mode(mode_str, -1);
        if ((int)mode >= 0) {
            set_side_scale_mode(target, mode);
        } else {
            wlr_log(WLR_ERROR, "Invalid scale mode in SCALE command: %s",
                mode_str ? mode_str : "(null)");
        }
        request_redraw(comp);
    } else if (strcmp(buf, "OVERLAY close") == 0 || strcmp(buf, "OVERLAY hide") == 0) {
        close_management_overlay(comp);
        write_runtime_state(comp);
        request_redraw(comp);
    } else if (strcmp(buf, "OVERLAY left") == 0 || strcmp(buf, "OVERLAY right") == 0) {
        struct side_ctx *target = strcmp(buf + 8, "left") == 0 ? &comp->left : &comp->right;
        /* The same decision as the chords: Main takes the overlay from Guest (and Guest's pending action and
         * device move end), Guest never takes or keeps Main's, Full screen shows only Main, and the power
         * progress panel stays. */
        request_overlay(comp, target, "OVERLAY command", false);
    } else if (strncmp(buf, "ROUTE ", 6) == 0) {
        char *device_name = buf + 6;
        char *space = strrchr(device_name, ' ');
        if (space) {
            *space = '\0';
            char *side_str = space + 1;

            if (strcmp(side_str, "default") == 0) {
                if (strcmp(device_name, "__dosplex_remote_input__") == 0 ||
                        strcmp(device_name, "remote") == 0 ||
                        strcmp(device_name, "input-leap") == 0) {
                    comp->remote_input_route = INPUT_ROUTE_FOLLOW;
                    wlr_log(WLR_INFO, "Reset Input Leap route to follow");
                } else {
                    struct dynamic_input_route *route, *tmp;
                    wl_list_for_each_safe(route, tmp, &comp->dynamic_input_routes, link) {
                        if (strcmp(route->device_name, device_name) == 0) {
                            wlr_log(WLR_INFO, "Removing dynamic route for %s", device_name);
                            wl_list_remove(&route->link);
                            free(route);
                            break;
                        }
                    }
                }
            } else {
                enum input_route_mode new_mode = INPUT_ROUTE_LEFT;
                if (strcmp(side_str, "main") == 0 || strcmp(side_str, "primary") == 0) {
                    new_mode = main_input_route_mode(comp);
                } else if (strcmp(side_str, "guest") == 0) {
                    new_mode = guest_input_route_mode(comp);
                } else if (!input_route_mode_from_string(side_str, &new_mode)) {
                    wlr_log(WLR_ERROR, "Invalid route in ROUTE command: %s", side_str);
                    return 1;
                }

                if (strcmp(device_name, "remote") == 0 ||
                        strcmp(device_name, "input-leap") == 0) {
                    device_name = "__dosplex_remote_input__";
                }

                if (new_mode == INPUT_ROUTE_BOTH &&
                        input_device_name_is_keyboard(comp, device_name)) {
                    wlr_log(WLR_INFO, "Ignoring IPC screen-wide route for keyboard %s",
                        device_name);
                    return 1;
                }

                set_dynamic_device_route(comp, device_name, new_mode);
            }
            request_redraw(comp);
        }
    }
    return 1;
}

static void init_hud_ipc(struct compositor *comp) {
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0) {
        wlr_log_errno(WLR_ERROR, "Failed to create HUD IPC socket");
        return;
    }

    struct sockaddr_un addr = {0};
    addr.sun_family = AF_UNIX;
    const char *ipc_path = getenv("DOSPLEX_HUD_IPC_SOCK");
    char ipc_path_buf[256];
    if (!ipc_path || !ipc_path[0]) {
        const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
        if (!runtime_dir || !runtime_dir[0]) {
            runtime_dir = "/run/dosplex";
        }
        snprintf(ipc_path_buf, sizeof(ipc_path_buf), "%s/%s",
            runtime_dir, HUD_IPC_SOCKET_NAME);
        ipc_path = ipc_path_buf;
    }
    strncpy(addr.sun_path, ipc_path, sizeof(addr.sun_path) - 1);
    unlink(addr.sun_path);

    /* Ensure directory exists */
    char *dir = strdup(addr.sun_path);
    char *last_slash = strrchr(dir, '/');
    if (last_slash) {
        *last_slash = '\0';
        mkdir(dir, 0777);
    }
    free(dir);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        wlr_log_errno(WLR_ERROR, "Failed to bind HUD IPC socket");
        close(fd);
        return;
    }

    if (chmod(addr.sun_path, 0600) < 0) {
        close(fd);
        unlink(addr.sun_path);
        return;
    }
    comp->hud_ipc_server_fd = fd;
    comp->hud_ipc_server_source = wl_event_loop_add_fd(
        wl_display_get_event_loop(comp->display),
        fd, WL_EVENT_READABLE, handle_hud_ipc_readable, comp);
}

static void handle_new_xdg_surface(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, new_xdg_surface);
    struct wlr_xdg_surface *xdg_surface = data;

    /* Only accept toplevels (not popups) */
    if (xdg_surface->role != WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
        return;
    }


    if (comp->panel_client && wl_resource_get_client(xdg_surface->resource) == comp->panel_client) {
        panel_adopt_toplevel(comp, xdg_surface);
        return;
    }

    if (xdg_surface->toplevel && xdg_surface->toplevel->app_id &&
        strcmp(xdg_surface->toplevel->app_id, "dosplex-hud") == 0) {
        wlr_log(WLR_INFO, "Mapping HUD surface");
        struct side_ctx *hud_side = compositor_side_by_id(comp, comp->hud_target_side);
        comp->hud_surface = xdg_surface->surface;
        comp->hud_scene_tree = wlr_scene_xdg_surface_create(
            comp->modal_tree ? comp->modal_tree : comp->overlay_tree, xdg_surface);
        if (comp->hud_scene_tree) {
            wlr_scene_node_set_position(&comp->hud_scene_tree->node,
                hud_side ? hud_side->x : 0, hud_side ? hud_side->y : 0);
            wlr_scene_node_raise_to_top(&comp->hud_scene_tree->node);
        }
        if (hud_side) {
            wlr_xdg_toplevel_set_size(xdg_surface->toplevel,
                hud_side->width, hud_side->height);
        }
        wlr_xdg_toplevel_set_activated(xdg_surface->toplevel, true);
        wlr_xdg_surface_schedule_configure(xdg_surface);

        if (!comp->hud_surface_destroy_armed) {
            comp->hud_surface_destroy.notify = handle_hud_surface_destroy;
            wl_signal_add(&xdg_surface->surface->events.destroy, &comp->hud_surface_destroy);
            comp->hud_surface_destroy_armed = true;
        }

        struct wlr_seat *seat = seat_for_side(comp, comp->hud_target_side);
        set_kbd_focus_surface(comp, seat, xdg_surface->surface);
        wlr_seat_pointer_notify_enter(seat, xdg_surface->surface, comp->cursor_x, comp->cursor_y);
        return;
    }

    struct side_ctx *target = pick_side_for_surface(comp, xdg_surface->surface);

    if (xdg_surface->toplevel) {
        wlr_log(WLR_INFO, "New xdg_toplevel: title='%s' app_id='%s' side=%s",
            xdg_surface->toplevel->title ? xdg_surface->toplevel->title : "(none)",
            xdg_surface->toplevel->app_id ? xdg_surface->toplevel->app_id : "(none)",
            target ? side_name(target->id) : "unknown");
    }

    if (!target) {
        wlr_log(WLR_INFO, "New xdg_toplevel but no side is waiting for an app — ignoring");
        return;
    }

    /* If a side already has an active main window and we get a new Gamescope window,
     * it might be a stale instance. Clear the side first. */
    if (target->app_surface && xdg_surface->toplevel &&
        xdg_surface->toplevel->app_id && strcmp(xdg_surface->toplevel->app_id, "gamescope") == 0) {
        wlr_log(WLR_INFO, "Side %s already has gamescope — clearing stale session", side_name(target->id));
        side_clear_xdg_windows(target);
        side_clear_app(target);
    }

    wlr_log(WLR_INFO, "Assigning new xdg_toplevel to %s side",
            target->id == SIDE_LEFT ? "left" : "right");

    /* Allow multiple windows per side. We no longer clear existing apps here. */

    /* Create scene tree — placed in the side's container tree */
    struct wlr_scene_tree *scene_tree =
        wlr_scene_xdg_surface_create(target->side_tree, xdg_surface);
    if (scene_tree) {
        wlr_scene_node_set_position(&scene_tree->node, 0, 0);
        wlr_scene_node_raise_to_top(&scene_tree->node);
    }

    /* Configure the surface to the logical output dimensions, which reflect
     * the active scale_mode. In fit/stretch modes this equals the viewport
     * size; in native mode this equals the full display resolution. */
    side_refresh_desktop_scale(target);
    int target_lw = side_toplevel_width(target);
    int target_lh = side_toplevel_height(target);
    wlr_xdg_toplevel_set_size(xdg_surface->toplevel, target_lw, target_lh);
    wlr_xdg_toplevel_set_activated(xdg_surface->toplevel, true);
    wlr_xdg_surface_schedule_configure(xdg_surface);
    wlr_log(WLR_INFO, "Configured host xdg_toplevel for %s side to %dx%d+%d+0",
        target->id == SIDE_LEFT ? "left" : "right",
        target_lw, target_lh, side_physical_origin_x(target));

    if (dosplex_file_debug_enabled()) {
        FILE *df = fopen("/tmp/dosplex-debug.log", "a");
        if (df) {
            char ts[64]; time_t now; struct tm *tb;
            now = time(NULL); tb = localtime(&now); strftime(ts, sizeof(ts), "%H:%M:%S", tb);
            fprintf(df, "[%s] set_size %s=%dx%d (logical=%dx%d physical=%dx%d)\n",
                ts, target->id == SIDE_LEFT ? "left" : "right",
                target_lw, target_lh,
                target->logical_output.logical_width, target->logical_output.logical_height,
                target->width, target->height);
            fclose(df);
        }
    }

    struct side_xdg_window *window = calloc(1, sizeof(*window));
    if (!window) {
        wlr_log(WLR_ERROR, "Failed to allocate xdg window tracking for %s side",
            target->id == SIDE_LEFT ? "left" : "right");
        return;
    }

    window->ctx = target;
    window->xdg_surface = xdg_surface;
    window->surface = xdg_surface->surface;
    window->scene_tree = scene_tree;
    wl_list_init(&window->scale_surfaces);
    side_init_xdg_windows(target);
    wl_list_insert(&target->xdg_windows, &window->link);
    /* Before the first commit: the initial configure follows the preferred scale, and a scaled side's
     * commits are handled at its scale from the first one on. */
    side_send_window_scale(window);
    side_arm_scale_override(window);
    if (side_scale_milli(target) != 1000) side_apply_window_scene_scale(window);

    window->surface_commit.notify = handle_xdg_window_commit;
    window->surface_map.notify = handle_xdg_window_map;
    window->surface_unmap.notify = handle_xdg_window_unmap;
    window->surface_destroy.notify = handle_xdg_window_destroy;
    window->surface_fullscreen.notify = handle_xdg_window_fullscreen;
    wl_signal_add(&xdg_surface->surface->events.commit, &window->surface_commit);
    window->surface_commit_armed = true;
    wl_signal_add(&xdg_surface->surface->events.map, &window->surface_map);
    window->surface_map_armed = true;
    wl_signal_add(&xdg_surface->surface->events.unmap, &window->surface_unmap);
    window->surface_unmap_armed = true;
    wl_signal_add(&xdg_surface->events.destroy, &window->surface_destroy);
    window->surface_destroy_armed = true;
    if (xdg_surface->toplevel) {
        wl_signal_add(&xdg_surface->toplevel->events.request_fullscreen, &window->surface_fullscreen);
        window->surface_fullscreen_armed = true;
    }
    window->surface_client_commit.notify = handle_xdg_window_client_commit;
    wl_signal_add(&xdg_surface->surface->events.client_commit, &window->surface_client_commit);
    window->surface_client_commit_armed = true;

    target->active_xdg_window = window;
    side_refresh_active_surface(target);

    /* Track the surface so the greeter stops rendering on this side.
     * XWayland surfaces set app_surface in attach_xwayland_surface();
     * xdg_shell surfaces need it set here. */
    if (!target->app_surface) {
        target->app_surface = xdg_surface->surface;
        wlr_log(WLR_INFO, "Set app_surface for %s side (xdg_toplevel)",
            target->id == SIDE_LEFT ? "left" : "right");
    }
    clock_gettime(CLOCK_MONOTONIC, &window->assigned);
    side_arm_map_watchdog(window);
    write_runtime_state(comp);

    request_redraw(comp);
}

static void handle_new_xwayland_surface(struct wl_listener *listener, void *data) {
    struct side_ctx *target = wl_container_of(listener, target, new_xwayland);
    struct compositor *comp = target->comp;
    struct wlr_xwayland_surface *xsurface = data;

    /* Accept all windows including popups (override_redirect) */

    wlr_log(WLR_INFO, "Assigning X11 window to %s side (window_id=%u, popup=%d)",
            target->id == SIDE_LEFT ? "left" : "right", xsurface->window_id,
            xsurface->override_redirect);

    if (!xsurface->surface) {
        wlr_log(WLR_INFO, "Waiting for X11 surface association on %s side",
            target->id == SIDE_LEFT ? "left" : "right");
        target->pending_xwayland_surface = xsurface;
        target->xwayland_associate.notify = handle_xwayland_associate;
        wl_signal_add(&xsurface->events.associate, &target->xwayland_associate);
        target->xwayland_associate_armed = true;
        return;
    }

    attach_xwayland_surface(comp, target, xsurface);
}

static struct ui_region *find_region_at(struct side_ctx *ctx, int x, int y) {
    for (int i = 0; i < ctx->num_regions; i++) {
        struct ui_region *r = &ctx->regions[i];
        if (x >= r->x && x < r->x + r->w && y >= r->y && y < r->y + r->h) {
            return r;
        }
    }
    return NULL;
}

static void handle_pointer_click(struct compositor *comp, int x, int y,
                                bool pressed, uint32_t button) {
    if (!pressed || button != BTN_LEFT) return;
    note_activity(comp);

    struct side_ctx *sides[] = {&comp->left, &comp->right};

    for (int i = 0; i < 2; i++) {
        struct side_ctx *ctx = sides[i];
        struct ui_region *region = find_region_at(ctx, x, y);

        if (!region || ctx->auth || ctx->launch_pending) continue;

        switch (region->type) {
        case UIelem_USERNAME:
            ctx->focused = UIelem_USERNAME;
            wlr_log(WLR_INFO, "Focused username field on %s",
                    ctx->id == SIDE_LEFT ? "left" : "right");
            request_redraw(comp);
            break;

        case UIelem_PASSWORD:
            ctx->focused = UIelem_PASSWORD;
            wlr_log(WLR_INFO, "Focused password field on %s",
                    ctx->id == SIDE_LEFT ? "left" : "right");
            request_redraw(comp);
            break;

        case UIelem_SIGNIN:
            if (ctx->state == SIDE_GREETER) {
                const char *steam_user = configured_steam_user(ctx);
                if (steam_user) {
                    /* Local session admission only. Steam owns account login. */
                    if (!comp->left.auth && !comp->right.auth && !ctx->recovery_required)
                        call_auth_helper(ctx, "auto-login", steam_user, "");
                    request_redraw(comp);
                    break;
                }
                wlr_log(WLR_INFO, "Sign In clicked on %s (user=%s)",
                        ctx->id == SIDE_LEFT ? "left" : "right",
                        ctx->username);
                if (ctx->username[0] && ctx->password[0]) {
                    call_auth_helper(ctx, "login", ctx->username, ctx->password);
                    explicit_bzero(ctx->password, sizeof(ctx->password));
                    request_redraw(comp);
                } else {
                    wlr_log(WLR_INFO, "Username or password empty");
                }
            }
            break;

        case UIelem_SIGNOUT:
            wlr_log(WLR_INFO, "Sign Out clicked on %s",
                    ctx->id == SIDE_LEFT ? "left" : "right");
            call_auth_helper(ctx, "logout", NULL, NULL);
            request_redraw(comp);
            break;

        default:
            ctx->focused = UIelem_NONE;
            request_redraw(comp);
            break;
        }
    }
}

/* Whether the side draws its application's toplevel at the side's scale (a Desktop payload): the toplevel's tree of
 * surfaces is in its own units there, and the scene's is scale times larger (see "The surface tree"). */
static bool side_draws_scaled(const struct side_ctx *side) {
    return side && (side_scale_milli(side) != 1000 || side_zoom_in_effect(side)) && !side->activity &&
        side->app_surface && side->app_surface->mapped;
}

/* A point of the side's region in the toplevel's units: a Desktop side's scale takes it back, Zoom's box gives it its
 * origin and takes it back by k (see "Zoom geometry"). */
static void side_region_to_toplevel(const struct side_ctx *side, double rx, double ry, double *tx, double *ty) {
    double scale = side_scale_milli(side) / 1000.0;
    *tx = rx / scale;
    *ty = ry / scale;
    if (scale != 1.0 || !side_zoom_in_effect(side) || !side->app_surface) return;
    struct wlr_surface *root = wlr_surface_get_root_surface(side->app_surface);
    int monitor_w, monitor_h;
    side_monitor_size(side, &monitor_w, &monitor_h);
    struct zoom_map map;
    if (!zoom_map_make(root->current.width, root->current.height, monitor_w, monitor_h, &map)) return;
    *tx = map.x + rx / map.k;
    *ty = map.y + ry / map.k;
}

/* The surface of a scaled side's toplevel tree that takes input at the layout point (lx, ly), the topmost one, and the
 * point in its own units; NULL when no surface does. wlroots 0.17 tests a point against a surface in drawn pixels, so
 * the scaled scene would miss most of the region and its hits would be in the wrong units: the point is taken back to
 * the toplevel's units here, and the tree is searched as it is there, which is where it is the picture too (the
 * surfaces and subsurfaces are drawn at their scaled positions and sizes, stacked as they are). */
static struct wlr_surface *side_scaled_surface_at(const struct side_ctx *side, double lx, double ly,
        double *sx, double *sy) {
    struct wlr_surface *root = wlr_surface_get_root_surface(side->app_surface);
    double tx, ty;
    side_region_to_toplevel(side, lx - side->x, ly - side->y, &tx, &ty);
    return wlr_surface_surface_at(root, tx, ty, sx, sy);
}

static struct wlr_surface *surface_at(struct compositor *comp, double lx, double ly,
        double *sx, double *sy) {
    if (!comp || !comp->scene) {
        return NULL;
    }

    /* A Desktop side's pointer goes to the surface of its toplevel's tree that is under it, in that surface's units. */
    struct side_ctx *scaled = side_at_point(comp, (int)lx, (int)ly);
    if (side_draws_scaled(scaled)) {
        return side_scaled_surface_at(scaled, lx, ly, sx, sy);
    }

    /* Temporarily disable cursor tree so it doesn't block surface picking */
    bool cursor_enabled = false;
    if (comp->cursor_tree) {
        cursor_enabled = comp->cursor_tree->node.enabled;
        wlr_scene_node_set_enabled(&comp->cursor_tree->node, false);
    }

    struct wlr_scene_node *node = wlr_scene_node_at(&comp->scene->tree.node, lx, ly, sx, sy);

    /* Re-enable cursor tree if it was enabled */
    if (comp->cursor_tree && cursor_enabled) {
        wlr_scene_node_set_enabled(&comp->cursor_tree->node, true);
    }

    if (!node || node->type != WLR_SCENE_NODE_BUFFER) {
        return NULL;
    }

    struct wlr_scene_buffer *scene_buffer = wlr_scene_buffer_from_node(node);
    struct wlr_scene_surface *scene_surface =
        wlr_scene_surface_try_from_buffer(scene_buffer);
    if (!scene_surface) {
        return NULL;
    }

    struct wlr_surface *root = wlr_surface_get_root_surface(scene_surface->surface);
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (int i = 0; i < 2; i++)
        if (sides[i]->activity && sides[i]->app_surface &&
                wlr_surface_get_root_surface(sides[i]->app_surface) == root) return NULL;
    return scene_surface->surface;
}

static bool side_has_app_for_surface(struct side_ctx *ctx, struct wlr_surface *surface) {
    if (!ctx || !surface) {
        return false;
    }
    side_init_xdg_windows(ctx);

    struct side_xdg_window *window = NULL;
    wl_list_for_each(window, &ctx->xdg_windows, link) {
        if (!window->surface) {
            continue;
        }
        if (wlr_surface_get_root_surface(window->surface) ==
                wlr_surface_get_root_surface(surface)) {
            return true;
        }
    }

    side_init_xwayland_windows(ctx);
    struct side_xwayland_window *xwindow = NULL;
    wl_list_for_each(xwindow, &ctx->xwayland_windows, link) {
        if (!xwindow->surface) {
            continue;
        }
        if (wlr_surface_get_root_surface(xwindow->surface) ==
                wlr_surface_get_root_surface(surface)) {
            return true;
        }
    }

    uid_t uid;
    if (surface_get_uid(surface, &uid) && side_matches_uid(ctx, uid)) {
        return true;
    }

    if (!ctx->app_surface) {
        return false;
    }

    return wlr_surface_get_root_surface(ctx->app_surface) ==
        wlr_surface_get_root_surface(surface);
}

static struct side_ctx *side_at_point(struct compositor *comp, int x, int y) {
    if (!comp) {
        return NULL;
    }

    if (shown_side(comp)) {
        return shown_side(comp);
    }

    if (x >= comp->left.x && x < comp->left.x + comp->left.width &&
            y >= comp->left.y && y < comp->left.y + comp->left.height) {
        return &comp->left;
    }
    if (x >= comp->right.x && x < comp->right.x + comp->right.width &&
            y >= comp->right.y && y < comp->right.y + comp->right.height) {
        return &comp->right;
    }

    if (comp->layout == LAYOUT_SPLIT_HORIZONTAL) {
        return y < comp->output_height / 2 ? &comp->left : &comp->right;
    }
    if (x < comp->output_width / 2) {
        return &comp->left;
    }
    return &comp->right;
}

static struct side_ctx *side_at(struct compositor *comp, int x) {
    return side_at_point(comp, x, comp ? comp->cursor_y : 0);
}

static bool keyboard_blocked_for_app(struct compositor *comp, struct side_ctx *ctx,
        struct wlr_keyboard *keyboard) {
    if (ctx->activity != 0) return true;
    if (side_input_held(comp, ctx->id)) return true;
    if (keyboard == remote_keyboard(comp, ctx) && comp->remote_overlay_wait_neutral) return true;
    struct input_device_ctx *input;
    wl_list_for_each(input, &comp->input_devices, link) {
        if (input->keyboard == keyboard && (input->keyboard_wait_neutral || input->keyboard_overlay_wait_neutral)) return true;
    }
    return false;
}

static void focus_app_surface_with_keyboard(struct compositor *comp,
        struct wlr_seat *seat, struct wlr_surface *surface, struct wlr_keyboard *kb) {
    if (!seat || !kb || !surface) {
        return;
    }

    struct side_ctx *ctx = NULL;
    if (side_has_app_for_surface(&comp->left, surface)) {
        ctx = &comp->left;
    } else if (side_has_app_for_surface(&comp->right, surface)) {
        ctx = &comp->right;
    }
    if (!ctx || keyboard_blocked_for_app(comp, ctx, kb)) return;

    struct side_xdg_window *window = side_find_xdg_window_for_surface(ctx, surface);
    if (window) {
        side_activate_xdg_window(ctx, window, true);
        request_redraw(comp);
    }

    struct wlr_surface *root = wlr_surface_get_root_surface(surface);
    struct wlr_surface *current_focus = (seat == comp->seat_primary) ?
        comp->kbd_focus_surface_primary : comp->kbd_focus_surface_guest;

    if (current_focus == root) {
        return;
    }

    assign_seat_keyboard(comp, seat, kb);
    wlr_seat_keyboard_notify_enter(seat, root,
        kb->keycodes, kb->num_keycodes, &kb->modifiers);
    set_kbd_focus_surface(comp, seat, root);
}

static void focus_known_side_surface(struct compositor *comp, struct side_ctx *ctx,
        struct wlr_surface *surface, struct wlr_keyboard *kb) {
    if (!comp || !ctx || !kb || !surface || keyboard_blocked_for_app(comp, ctx, kb)) {
        return;
    }

    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    if (!seat) return;

    struct side_xdg_window *window = side_find_xdg_window_for_surface(ctx, surface);
    if (window) {
        side_activate_xdg_window(ctx, window, true);
        request_redraw(comp);
    }

    struct wlr_surface *root = wlr_surface_get_root_surface(surface);
    struct wlr_surface *current_focus = (seat == comp->seat_primary) ?
        comp->kbd_focus_surface_primary : comp->kbd_focus_surface_guest;

    if (!root || current_focus == root) {
        return;
    }

    assign_seat_keyboard(comp, seat, kb);
    wlr_seat_keyboard_notify_enter(seat, root,
        kb->keycodes, kb->num_keycodes, &kb->modifiers);
    set_kbd_focus_surface(comp, seat, root);
}

/* A side's application holds its seat's keyboard focus whether or not a keyboard is routed to it,
 * as on SteamOS, where Gamescope's Steam always has it. Called when the application maps, remaps or
 * becomes the side's active window; sends wl_keyboard.enter with the seat's current keyboard (the
 * placeholder holds no keys) unless the application already has focus or the side's input is held
 * back (menu open, power panel, a keyboard waiting for neutral). */
static void side_ensure_app_keyboard_focus(struct compositor *comp, struct side_ctx *ctx) {
    if (!comp || !ctx || !ctx->app_surface || !ctx->app_surface->mapped) return;
    if (comp->management_overlay_visible && comp->management_overlay_owner == ctx->id) return;
    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    if (!seat) return;
    seat_ensure_keyboard(comp, seat);
    focus_known_side_surface(comp, ctx, ctx->app_surface, wlr_seat_get_keyboard(seat));
}

static void focus_app_surface(struct compositor *comp, struct wlr_surface *surface) {
    if (!comp || !surface) return;
    struct side_ctx *ctx = NULL;
    if (side_has_app_for_surface(&comp->left, surface)) {
        ctx = &comp->left;
    } else if (side_has_app_for_surface(&comp->right, surface)) {
        ctx = &comp->right;
    }
    if (!ctx) return;

    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    struct wlr_keyboard *kb = wlr_seat_get_keyboard(seat);
    if (!kb) kb = remote_keyboard(comp, ctx);

    focus_app_surface_with_keyboard(comp, seat, surface, kb);
}

/* === Pointer constraints, relative motion and grabs ===
 *
 * A nested Gamescope whose game has hidden and locked the pointer asks for a pointer lock and
 * from then on wants relative motion, not positions. Each seat belongs to one side, and what is
 * kept here hangs off that side (the constraint it holds, the buttons it has pressed), so one
 * player's locked mouse never stops the other player's pointer.
 *
 * A constraint is active only while the seat's pointer is on the constrained application
 * surface, the side is shown and active, and its menu is closed. pointer_constraints_sync()
 * decides that again whenever one of those can have changed. */

static uint32_t pointer_now_msec(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint32_t)(now.tv_sec * 1000 + now.tv_nsec / 1000000);
}

static struct side_ctx *side_for_seat(struct compositor *comp, struct wlr_seat *seat) {
    if (!comp || !seat) return NULL;
    if (seat == comp->seat_primary) return primary_side(comp);
    if (seat == comp->seat_guest) return guest_side(comp);
    return NULL;
}

static bool side_pointer_locked(const struct side_ctx *side) {
    return side && side->pointer_constraint &&
        side->pointer_constraint->type == WLR_POINTER_CONSTRAINT_V1_LOCKED;
}

/* Pointer input for a nested Gamescope through its EIS socket.
 *
 * Gamescope's Wayland backend turns every host wl_pointer.motion into a touch motion, and Steam's
 * touch click mode (Passthrough) drops a touch motion that has no touch-down, so a host mouse never
 * moves Gamescope's cursor. Gamescope also runs an EIS server; a libei sender client there moves the
 * cursor, clicks and scrolls as a real mouse would. For a side whose application is a nested
 * Gamescope, motion, buttons and scroll go over that socket; enter, leave, focus and pointer
 * constraints stay on the Wayland path. So do relative pointer events, but a pointer lock's
 * relative motion also goes over the socket (eis_side_relative). Anything that is not ready
 * (no libei, no socket, device not resumed) falls back to wl_pointer, so nothing regresses. */

/* Surface-local position in a side's region to a nested-game pixel, as Gamescope lays it out.
 * Fit keeps the proportions (letterbox or pillarbox, centred); Stretch scales each axis; Native is
 * 1:1, centred when the game monitor is smaller than the region. Zoom shows the centred rectangle of the
 * monitor that has the region's shape (zoom_source_rect), so a region point is a point of that rectangle.
 * Gamescope's Fill scaler is not
 * offered by Dosplex, so it is not modelled. The result is clamped into the game monitor. */
static void __attribute__((unused)) eis_map_point(enum output_scale_mode mode, int region_w,
        int region_h, int game_w, int game_h, double sx, double sy, double *nx, double *ny) {
    double w = game_w > 0 ? game_w : 1, h = game_h > 0 ? game_h : 1;
    double rw = region_w > 0 ? region_w : w, rh = region_h > 0 ? region_h : h;
    double x, y;
    if (mode == OUTPUT_SCALE_STRETCH) {
        x = sx * w / rw;
        y = sy * h / rh;
    } else if (mode == OUTPUT_SCALE_ZOOM) {
        /* The region shows the zoom rectangle of the monitor: a point of it is that rectangle's point. */
        double zx, zy, zw, zh;
        zoom_source_rect(region_w, region_h, game_w, game_h, &zx, &zy, &zw, &zh);
        x = zx + sx * zw / rw;
        y = zy + sy * zh / rh;
    } else {
        double scale = 1.0;
        if (mode != OUTPUT_SCALE_NATIVE) {
            scale = rw / w < rh / h ? rw / w : rh / h;
        }
        x = (sx - (rw - w * scale) / 2.0) / scale;
        y = (sy - (rh - h * scale) / 2.0) / scale;
    }
    if (x > w - 1) x = w - 1;
    if (x < 0) x = 0;
    if (y > h - 1) y = h - 1;
    if (y < 0) y = 0;
    *nx = x;
    *ny = y;
}

/* A relative motion in a side's region to the same motion in the nested game's pixels: what two
 * positions that eis_map_point() maps differ by. Fit has one scale for both axes, Stretch one per
 * axis, Native none. A delta has no offset, and it is not clamped: a game that looks around takes
 * every pixel of it. */
static void __attribute__((unused)) eis_map_delta(enum output_scale_mode mode, int region_w,
        int region_h, int game_w, int game_h, double dx, double dy, double *ndx, double *ndy) {
    double w = game_w > 0 ? game_w : 1, h = game_h > 0 ? game_h : 1;
    double rw = region_w > 0 ? region_w : w, rh = region_h > 0 ? region_h : h;
    if (mode == OUTPUT_SCALE_STRETCH) {
        *ndx = dx * w / rw;
        *ndy = dy * h / rh;
    } else if (mode == OUTPUT_SCALE_ZOOM) {
        double zx, zy, zw, zh;
        zoom_source_rect(region_w, region_h, game_w, game_h, &zx, &zy, &zw, &zh);
        *ndx = dx * zw / rw;
        *ndy = dy * zh / rh;
    } else {
        double scale = 1.0;
        if (mode != OUTPUT_SCALE_NATIVE) {
            scale = rw / w < rh / h ? rw / w : rh / h;
        }
        *ndx = dx / scale;
        *ndy = dy / scale;
    }
}

#ifdef HAVE_LIBEI
struct eis_input {
    struct side_ctx *side;
    struct wlr_surface *surface;
    struct ei *ei;
    struct wl_event_source *source;
    struct ei_seat *seat;
    struct ei_device *device;
    bool emulating;
    bool last_valid;
    double last_x, last_y;
    uint32_t sequence;
    char path[PATH_MAX];
};

static bool eis_socket_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0 && S_ISSOCK(st.st_mode);
}

/* Connect to the EIS socket at `path`, and keep the connection only when the process listening there runs as
 * `owner` (CMP-B-01). The path comes from the client's own environment, which the client's account writes: a
 * Guest program could name Main's Gamescope there, and this compositor, as root, would then hand Guest's pointer
 * to Main's session. The kernel says who listens on the socket that was actually reached, so a path swapped
 * between a check and the connect changes nothing. Never blocks: a listener that does not take the connection at
 * once is not used. Returns the connected descriptor, or -1. */
static int __attribute__((unused)) eis_open_owned(const char *path, uid_t owner) {
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (!path || strlen(path) >= sizeof(addr.sun_path)) return -1;
    strcpy(addr.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return -1;
    struct ucred peer;
    socklen_t length = sizeof(peer);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
            getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &length) != 0 || peer.uid != owner) {
        close(fd);
        return -1;
    }
    return fd;
}

/* The value of `name` in a process's environment, or false. */
static bool eis_proc_env(pid_t pid, const char *name, char *out, size_t size) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/environ", (int)pid);
    FILE *file = fopen(path, "r");
    if (!file) return false;
    size_t name_len = strlen(name);
    char *line = NULL;
    size_t cap = 0;
    bool found = false;
    ssize_t n;
    while (!found && (n = getdelim(&line, &cap, '\0', file)) > 0) {
        if ((size_t)n > name_len + 1 && strncmp(line, name, name_len) == 0 && line[name_len] == '=') {
            snprintf(out, size, "%s", line + name_len + 1);
            found = out[0] != '\0';
        }
    }
    free(line);
    fclose(file);
    return found;
}

/* The EIS socket of the Gamescope process `pid`, or false when it is not Gamescope or has none. */
static bool eis_find_socket(pid_t pid, char *out, size_t size) {
    char path[PATH_MAX], exe[PATH_MAX];
    snprintf(path, sizeof(path), "/proc/%d/exe", (int)pid);
    ssize_t n = readlink(path, exe, sizeof(exe) - 1);
    if (n <= 0) return false;
    exe[n] = '\0';
    const char *base = strrchr(exe, '/');
    base = base ? base + 1 : exe;
    if (strncmp(base, "gamescope", 9) != 0) return false;

    char runtime[PATH_MAX], display[128];
    if (!eis_proc_env(pid, "XDG_RUNTIME_DIR", runtime, sizeof(runtime))) return false;
    if (eis_proc_env(pid, "GAMESCOPE_WAYLAND_DISPLAY", display, sizeof(display))) {
        snprintf(path, sizeof(path), "%s/%s-ei", runtime, display);
        if (eis_socket_exists(path)) { snprintf(out, size, "%s", path); return true; }
    }
    snprintf(path, sizeof(path), "%s/gamescope-0-ei", runtime);
    if (eis_socket_exists(path)) { snprintf(out, size, "%s", path); return true; }

    DIR *dir = opendir(runtime);
    if (!dir) return false;
    int count = 0;
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        size_t len = strlen(entry->d_name);
        if (strncmp(entry->d_name, "gamescope-", 10) != 0 || len < 14 ||
                strcmp(entry->d_name + len - 3, "-ei") != 0) continue;
        snprintf(path, sizeof(path), "%s/%s", runtime, entry->d_name);
        if (!eis_socket_exists(path)) continue;
        snprintf(out, size, "%s", path);
        count++;
    }
    closedir(dir);
    return count == 1;
}

static void eis_side_drop(struct side_ctx *side, const char *why) {
    struct eis_input *eis = side ? side->eis : NULL;
    if (!eis) return;
    side->eis = NULL;
    wlr_log(WLR_INFO, "EIS input of the %s side dropped (%s); pointer input uses Wayland again",
        side_name(side->id), why);
    if (eis->source) wl_event_source_remove(eis->source);
    if (eis->device) ei_device_unref(eis->device);
    if (eis->seat) ei_seat_unref(eis->seat);
    if (eis->ei) ei_unref(eis->ei);
    free(eis);
}

static void eis_handle_events(struct eis_input *eis) {
    struct ei_event *event;
    while ((event = ei_get_event(eis->ei))) {
        switch (ei_event_get_type(event)) {
        case EI_EVENT_SEAT_ADDED:
            if (!eis->seat) {
                eis->seat = ei_seat_ref(ei_event_get_seat(event));
                ei_seat_bind_capabilities(eis->seat, EI_DEVICE_CAP_POINTER,
                    EI_DEVICE_CAP_POINTER_ABSOLUTE, EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_SCROLL, NULL);
            }
            break;
        case EI_EVENT_DEVICE_ADDED: {
            struct ei_device *device = ei_event_get_device(event);
            if (!eis->device && ei_device_has_capability(device, EI_DEVICE_CAP_POINTER) &&
                    ei_device_has_capability(device, EI_DEVICE_CAP_BUTTON)) {
                eis->device = ei_device_ref(device);
            }
            break;
        }
        case EI_EVENT_DEVICE_RESUMED:
            if (eis->device && ei_event_get_device(event) == eis->device && !eis->emulating) {
                ei_device_start_emulating(eis->device, ++eis->sequence);
                eis->emulating = true;
                eis->last_valid = false;
                wlr_log(WLR_INFO, "EIS input of the %s side connected: %s",
                    side_name(eis->side->id), eis->path);
            }
            break;
        case EI_EVENT_DEVICE_PAUSED:
            if (eis->device && ei_event_get_device(event) == eis->device) {
                eis->emulating = false;
                eis->last_valid = false;
            }
            break;
        case EI_EVENT_DEVICE_REMOVED:
            if (eis->device && ei_event_get_device(event) == eis->device) {
                ei_device_unref(eis->device);
                eis->device = NULL;
                eis->emulating = false;
            }
            break;
        case EI_EVENT_DISCONNECT:
            ei_event_unref(event);
            eis_side_drop(eis->side, "the EIS server disconnected");
            return;
        default:
            break;
        }
        ei_event_unref(event);
    }
}

static int eis_fd_ready(int fd, uint32_t mask, void *data) {
    struct eis_input *eis = data;
    (void)fd;
    if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) {
        eis_side_drop(eis->side, "the EIS socket closed");
        return 0;
    }
    ei_dispatch(eis->ei);
    eis_handle_events(eis);
    return 0;
}

static struct eis_input *eis_connect(struct side_ctx *side, struct wlr_surface *surface) {
    if (!surface->resource) return NULL;
    pid_t pid = 0;
    uid_t uid;
    gid_t gid;
    wl_client_get_credentials(wl_resource_get_client(surface->resource), &pid, &uid, &gid);
    if (pid <= 0) return NULL;
    struct eis_input *eis = calloc(1, sizeof(*eis));
    if (!eis) return NULL;
    eis->side = side;
    eis->surface = surface;
    if (!eis_find_socket(pid, eis->path, sizeof(eis->path))) goto fail;
    /* Only a socket that the side's own account listens on is this side's Gamescope. */
    int fd = eis_open_owned(eis->path, uid);
    if (fd < 0) {
        wlr_log(WLR_ERROR, "EIS socket %s is not one the %s side's account (uid %d) listens on; not used",
            eis->path, side_name(side->id), (int)uid);
        goto fail;
    }
    eis->ei = ei_new_sender(NULL);
    if (!eis->ei) {
        close(fd);
        goto fail;
    }
    ei_configure_name(eis->ei, "dosplex-compositor");
    if (ei_setup_backend_fd(eis->ei, fd) != 0) goto fail;
    eis->source = wl_event_loop_add_fd(wl_display_get_event_loop(side->comp->display),
        ei_get_fd(eis->ei), WL_EVENT_READABLE, eis_fd_ready, eis);
    if (!eis->source) goto fail;
    return eis;
fail:
    if (eis->ei) ei_unref(eis->ei);
    free(eis);
    return NULL;
}

/* The side's EIS connection when pointer input should use it now, else NULL. Detects again when the
 * application surface changed, and drops the connection when the application went away. */
static struct eis_input *eis_side_ready(struct side_ctx *side) {
    if (!side) return NULL;
    struct wlr_surface *surface = side->app_surface;
    if (side->eis && side->eis->surface != surface) eis_side_drop(side, "the application changed");
    if (!surface) {
        side->eis_probe_surface = NULL;
        return NULL;
    }
    if (!side->eis) {
        uint32_t now = pointer_now_msec();
        /* A surface that is not a Gamescope, or whose socket is not there yet, is probed again
         * after a while, not on every event. */
        if (side->eis_probe_surface == surface && now - side->eis_probe_msec < 2000) return NULL;
        side->eis_probe_surface = surface;
        side->eis_probe_msec = now;
        side->eis = eis_connect(side, surface);
        if (!side->eis) return NULL;
        wlr_log(WLR_INFO, "EIS input of the %s side connecting: %s", side_name(side->id),
            side->eis->path);
    }
    struct eis_input *eis = side->eis;
    return eis->device && eis->emulating ? eis : NULL;
}

static void eis_game_point(const struct side_ctx *side, double *x, double *y) {
    int game_w, game_h;
    side_monitor_size(side, &game_w, &game_h);
    eis_map_point(side_presented_scale_mode(side), side->width, side->height, game_w, game_h,
        side->pointer_x, side->pointer_y, x, y);
}

static void eis_flush(struct eis_input *eis) {
    ei_device_frame(eis->device, ei_now(eis->ei));
}

/* Puts Gamescope's cursor on the side's pointer. It is synthetic: it does not unhide the cursor. */
static void eis_side_warp(struct side_ctx *side) {
    struct eis_input *eis = eis_side_ready(side);
    if (!eis) return;
    double x, y;
    eis_game_point(side, &x, &y);
    ei_device_pointer_motion_absolute(eis->device, x, y);
    eis_flush(eis);
    eis->last_x = x;
    eis->last_y = y;
    eis->last_valid = true;
}

/* Moves Gamescope's cursor by the change of the mapped position since the last one sent. The first
 * position after a (re)connect is a warp. */
static void eis_side_motion(struct side_ctx *side) {
    struct eis_input *eis = eis_side_ready(side);
    if (!eis) return;
    if (!eis->last_valid) {
        eis_side_warp(side);
        return;
    }
    double x, y;
    eis_game_point(side, &x, &y);
    double dx = x - eis->last_x, dy = y - eis->last_y;
    if (dx == 0.0 && dy == 0.0) return;
    ei_device_pointer_motion(eis->device, dx, dy);
    eis_flush(eis);
    eis->last_x = x;
    eis->last_y = y;
}

/* The host mouse's relative motion for a game that holds a pointer lock. A lock takes no positions;
 * the game turns its camera on relative pointer events. Gamescope's Wayland backend forwards the
 * host's relative motion only while it holds its own host lock and its surface has keyboard focus;
 * its EIS server turns a relative motion event straight into the relative pointer events of its
 * focused game and applies the game's lock itself. The delta is in the side's region, so it is
 * scaled into the game's pixels the way a position is. Where the cursor is under a lock is not
 * known: last_x and last_y are left alone and the first position after the lock is a warp. */
static void eis_side_relative(struct side_ctx *side, double dx_unaccel, double dy_unaccel) {
    struct eis_input *eis = eis_side_ready(side);
    if (!eis || (dx_unaccel == 0.0 && dy_unaccel == 0.0)) return;
    int game_w, game_h;
    side_monitor_size(side, &game_w, &game_h);
    double dx, dy;
    eis_map_delta(side_presented_scale_mode(side), side->width, side->height, game_w, game_h,
        dx_unaccel, dy_unaccel, &dx, &dy);
    ei_device_pointer_motion(eis->device, dx, dy);
    eis_flush(eis);
    eis->last_valid = false;
}

static void eis_side_button(struct side_ctx *side, uint32_t button, bool pressed, bool warp) {
    struct eis_input *eis = eis_side_ready(side);
    if (!eis) return;
    if (warp) {
        double x, y;
        eis_game_point(side, &x, &y);
        ei_device_pointer_motion_absolute(eis->device, x, y);
        eis->last_x = x;
        eis->last_y = y;
        eis->last_valid = true;
    }
    ei_device_button_button(eis->device, button, pressed);
    eis_flush(eis);
}

static void eis_side_scroll(struct side_ctx *side, bool vertical, double delta, int32_t discrete) {
    struct eis_input *eis = eis_side_ready(side);
    if (!eis) return;
    if (discrete != 0) {
        int32_t steps = discrete * 120; /* libei counts a wheel notch as 120 */
        ei_device_scroll_discrete(eis->device, vertical ? 0 : steps, vertical ? steps : 0);
    } else {
        ei_device_scroll_delta(eis->device, vertical ? 0.0 : delta, vertical ? delta : 0.0);
    }
    eis_flush(eis);
}

/* The pointer moved without EIS (a lock): the next position is a warp, not a delta. */
static void eis_side_unsync(struct side_ctx *side) {
    if (side && side->eis) side->eis->last_valid = false;
}

/* Whether the side's pointer input goes over EIS right now. */
static bool eis_side_active(struct side_ctx *side) {
    return eis_side_ready(side) != NULL;
}
#else
static void eis_side_drop(struct side_ctx *side, const char *why) { (void)side; (void)why; }
static void eis_side_warp(struct side_ctx *side) { (void)side; }
static void eis_side_motion(struct side_ctx *side) { (void)side; }
static void eis_side_relative(struct side_ctx *side, double dx_unaccel, double dy_unaccel) {
    (void)side; (void)dx_unaccel; (void)dy_unaccel;
}
static void eis_side_button(struct side_ctx *side, uint32_t button, bool pressed, bool warp) {
    (void)side; (void)button; (void)pressed; (void)warp;
}
static void eis_side_scroll(struct side_ctx *side, bool vertical, double delta, int32_t discrete) {
    (void)side; (void)vertical; (void)delta; (void)discrete;
}
static void eis_side_unsync(struct side_ctx *side) { (void)side; }
static bool eis_side_active(struct side_ctx *side) { (void)side; return false; }
#endif

static const char *pointer_constraint_name(const struct wlr_pointer_constraint_v1 *constraint) {
    return constraint->type == WLR_POINTER_CONSTRAINT_V1_LOCKED ? "lock" : "confinement";
}

/* Buttons: each seat counts the presses it passed on, so that a release finds the seat that got
 * the press even after the pointer has moved over the other side. */

static int pointer_button_slot(uint32_t button) {
    return button >= BTN_MISC && button < BTN_MISC + POINTER_BUTTON_SLOTS ?
        (int)(button - BTN_MISC) : -1;
}

static void pointer_note_press(struct side_ctx *side, uint32_t button) {
    int slot = pointer_button_slot(button);
    if (slot >= 0 && side->buttons_down[slot] < UINT8_MAX) side->buttons_down[slot]++;
}

static void pointer_forget_press(struct side_ctx *side, uint32_t button) {
    int slot = pointer_button_slot(button);
    if (slot >= 0 && side->buttons_down[slot] > 0) side->buttons_down[slot]--;
}

/* The side whose seat got a press of `button` that is still held; `preferred` wins a tie. */
static struct side_ctx *pointer_press_side(struct compositor *comp, struct side_ctx *preferred,
        uint32_t button) {
    int slot = pointer_button_slot(button);
    if (slot < 0) return NULL;
    if (preferred && preferred->buttons_down[slot]) return preferred;
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        if (sides[i] != preferred && sides[i]->buttons_down[slot]) return sides[i];
    }
    return NULL;
}

/* Whether a press that this side's seat got is still held. */
static bool pointer_buttons_held(const struct side_ctx *side) {
    for (int slot = 0; slot < POINTER_BUTTON_SLOTS; slot++) {
        if (side->buttons_down[slot] > 0) return true;
    }
    return false;
}

/* The shared pointer has gone over the other side. A seat is entered when the pointer comes into its
 * application and keeps that focus until something clears it, so a seat that the pointer left would go
 * on believing that it is inside its surface: its client never sees the pointer leave, and on the way
 * back it gets plain motion instead of an enter, and its nested Gamescope no warp. Gamescope's cursor is
 * moved by the change from the position it was last given, which is wrong once the cursor has been
 * moved or parked since (Gamescope parks a cursor that it hides). So the seat's pointer focus ends and the
 * next position in its Gamescope is a warp; when the pointer returns, the seat is entered afresh and the
 * Gamescope warped (pointer_motion_to).
 *
 * Two things keep a seat as it is. A lock or a confinement that its client holds: the cursor cannot leave
 * such a side, and the rules of the constraint are not touched here. And a press that the seat still
 * holds: the release belongs to the seat that got the press, so the focus stays until the release, which
 * ends it (pointer_complete_leave). */
static void pointer_leave_side(struct compositor *comp, struct side_ctx *side) {
    struct wlr_seat *seat = seat_for_side(comp, side->id);
    if (!seat || side->pointer_constraint) return;
    eis_side_unsync(side);
    if (!seat->pointer_state.focused_surface) return;
    if (pointer_buttons_held(side)) {
        side->pointer_leave_pending = true;
        return;
    }
    wlr_log(WLR_INFO, "Pointer left app: side=%s", side_name(side->id));
    wlr_seat_pointer_notify_clear_focus(seat);
}

/* A release ended the last press that kept a side's focus after the shared pointer left it. */
static void pointer_complete_leave(struct compositor *comp, struct side_ctx *side) {
    if (!side->pointer_leave_pending || pointer_buttons_held(side)) return;
    side->pointer_leave_pending = false;
    if (comp->shared_pointer_side != side) pointer_leave_side(comp, side);
}

/* Lets go of every button this seat still holds. A client that loses its pointer (the menu opens,
 * the layout changes, the application goes away) must not keep a button down, and the physical
 * release that follows finds nothing pressed and is dropped. */
static void pointer_release_buttons(struct compositor *comp, struct side_ctx *ctx) {
    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    uint32_t time_msec = pointer_now_msec();
    bool sent = false;
    for (int slot = 0; slot < POINTER_BUTTON_SLOTS; slot++) {
        while (ctx->buttons_down[slot] > 0) {
            ctx->buttons_down[slot]--;
            if (eis_side_active(ctx)) {
                eis_side_button(ctx, BTN_MISC + slot, false, false);
            } else if (seat && seat->pointer_state.focused_surface) {
                wlr_seat_pointer_notify_button(seat, time_msec, BTN_MISC + slot,
                    WLR_BUTTON_RELEASED);
                sent = true;
            }
        }
    }
    if (sent) wlr_seat_pointer_notify_frame(seat);
}

/* The remote input's held buttons end with its connection or when its side goes away. Each one
 * goes back to the seat that got its press, which is not always `preferred`. */
static void pointer_release_remote_buttons(struct compositor *comp, struct side_ctx *preferred) {
    uint32_t time_msec = pointer_now_msec();
    for (size_t button = 0; button <= KEY_MAX; ++button) {
        if (!comp->remote_pressed_buttons[button]) continue;
        comp->remote_pressed_buttons[button] = false;
        struct side_ctx *pressed = pointer_press_side(comp, preferred, (uint32_t)button);
        struct wlr_seat *seat = pressed ? seat_for_side(comp, pressed->id) : NULL;
        if (!seat) continue;
        pointer_forget_press(pressed, (uint32_t)button);
        if (eis_side_active(pressed)) {
            eis_side_button(pressed, (uint32_t)button, false, false);
            pointer_complete_leave(comp, pressed);
            continue;
        }
        wlr_seat_pointer_notify_button(seat, time_msec, (uint32_t)button, WLR_BUTTON_RELEASED);
        wlr_seat_pointer_notify_frame(seat);
        pointer_complete_leave(comp, pressed);
    }
}

/* A lock's cursor position hint says where the client left its own cursor, in the coordinates of
 * its surface. Honour it when the lock ends: the pointer continues from there. The client learns
 * the position from the next motion it gets, as with any other warp. */
static void pointer_apply_cursor_hint(struct compositor *comp, struct side_ctx *side,
        double hint_x, double hint_y) {
    int x = side->x + (int)floor(hint_x);
    int y = side->y + (int)floor(hint_y);
    clamp_to_side(side, &x, &y);
    side->pointer_x = x - side->x;
    side->pointer_y = y - side->y;
    if (side_at_point(comp, comp->cursor_x, comp->cursor_y) == side) {
        comp->cursor_x = x;
        comp->cursor_y = y;
        update_cursor_visual(comp, x, y);
    }
    struct wlr_seat *seat = seat_for_side(comp, side->id);
    if (seat) wlr_seat_pointer_warp(seat, side->pointer_x, side->pointer_y);
    wlr_log(WLR_INFO, "Pointer of the %s side continues at its client's hint (%d,%d)",
        side_name(side->id), side->pointer_x, side->pointer_y);
}

static void handle_pointer_constraint_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct side_ctx *side = wl_container_of(listener, side, pointer_constraint_destroy);
    struct wlr_pointer_constraint_v1 *constraint = side->pointer_constraint;
    wl_list_remove(&side->pointer_constraint_destroy.link);
    side->pointer_constraint = NULL;
    if (!constraint) return;
    wlr_log(WLR_INFO, "Pointer %s of the %s side ended by its client",
        pointer_constraint_name(constraint), side_name(side->id));
    if (side->comp && constraint->type == WLR_POINTER_CONSTRAINT_V1_LOCKED &&
            (constraint->current.committed & WLR_POINTER_CONSTRAINT_V1_STATE_CURSOR_HINT)) {
        pointer_apply_cursor_hint(side->comp, side, constraint->current.cursor_hint.x,
            constraint->current.cursor_hint.y);
    }
}

static void pointer_constraint_activate(struct compositor *comp, struct side_ctx *side,
        struct wlr_pointer_constraint_v1 *constraint) {
    side->pointer_constraint = constraint;
    side->pointer_constraint_destroy.notify = handle_pointer_constraint_destroy;
    wl_signal_add(&constraint->events.destroy, &side->pointer_constraint_destroy);
    wlr_log(WLR_INFO, "Pointer %s active on the %s side", pointer_constraint_name(constraint),
        side_name(side->id));
    /* A lock takes relative motion only, which the host pointer of the X11 backend can give only while it is away
     * from the edges of the screen: it is brought to the middle now, not when it has moved. */
    host_pointer_recentre_at_lock(comp, side);
    wlr_pointer_constraint_v1_send_activated(constraint);
}

static void pointer_constraint_deactivate(struct compositor *comp, struct side_ctx *side,
        const char *reason) {
    struct wlr_pointer_constraint_v1 *constraint = side->pointer_constraint;
    if (!constraint) return;
    wl_list_remove(&side->pointer_constraint_destroy.link);
    side->pointer_constraint = NULL;
    bool hint = constraint->type == WLR_POINTER_CONSTRAINT_V1_LOCKED &&
        (constraint->current.committed & WLR_POINTER_CONSTRAINT_V1_STATE_CURSOR_HINT);
    double hint_x = constraint->current.cursor_hint.x;
    double hint_y = constraint->current.cursor_hint.y;
    wlr_log(WLR_INFO, "Pointer %s of the %s side ended: %s", pointer_constraint_name(constraint),
        side_name(side->id), reason);
    /* A oneshot constraint ends with this, and so does the memory the hint was read from. */
    wlr_pointer_constraint_v1_send_deactivated(constraint);
    if (hint) pointer_apply_cursor_hint(comp, side, hint_x, hint_y);
}

/* The constraint that may be active for this side's seat now, if any: one on the application
 * surface the seat's pointer is on (the constraint may sit on the toplevel while a subsurface
 * has the pointer). */
static struct wlr_pointer_constraint_v1 *pointer_constraint_wanted(struct compositor *comp,
        struct side_ctx *side, struct wlr_seat *seat) {
    struct wlr_surface *focus = seat->pointer_state.focused_surface;
    if (!focus || !focus->mapped || !side->app_surface || side->activity != 0 ||
            !side_is_shown(comp, side) ||
            side_input_held(comp, side->id) ||
            !side_has_app_for_surface(side, focus)) {
        return NULL;
    }
    struct wlr_surface *root = wlr_surface_get_root_surface(focus);
    struct wlr_pointer_constraint_v1 *found = NULL, *constraint;
    wl_list_for_each(constraint, &comp->pointer_constraints_v1->constraints, link) {
        if (constraint->seat != seat || !constraint->surface || !constraint->surface->mapped ||
                wlr_surface_get_root_surface(constraint->surface) != root) {
            continue;
        }
        if (constraint->surface == focus) return constraint;
        if (!found) found = constraint;
    }
    return found;
}

static void pointer_constraints_sync(struct compositor *comp, struct wlr_seat *seat) {
    struct side_ctx *side = side_for_seat(comp, seat);
    if (!side || !comp->pointer_constraints_v1) return;
    struct wlr_pointer_constraint_v1 *wanted = pointer_constraint_wanted(comp, side, seat);
    if (wanted == side->pointer_constraint) return;
    pointer_constraint_deactivate(comp, side, "the pointer, the application or its menu changed");
    if (wanted) pointer_constraint_activate(comp, side, wanted);
}

static void pointer_constraints_release_all(struct compositor *comp) {
    pointer_constraint_deactivate(comp, &comp->left, "the layout changed");
    pointer_constraint_deactivate(comp, &comp->right, "the layout changed");
}

static void handle_new_pointer_constraint(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, new_pointer_constraint);
    struct wlr_pointer_constraint_v1 *constraint = data;
    wlr_log(WLR_INFO, "New pointer %s requested on seat %s", pointer_constraint_name(constraint),
        constraint->seat ? constraint->seat->name : "(none)");
    pointer_constraints_sync(comp, constraint->seat);
}

/* Pointer focus is where constraints start and end. A press belongs to the client that got it, so
 * the count of held buttons restarts when the focus leaves that client. */
static void pointer_focus_changed(struct compositor *comp,
        struct wlr_seat_pointer_focus_change_event *event) {
    struct side_ctx *side = side_for_seat(comp, event->seat);
    /* A leave that waited for a release does not wait for it once the focus is something else. */
    if (side) side->pointer_leave_pending = false;
    if (side && event->old_surface && (!event->new_surface ||
            wl_resource_get_client(event->old_surface->resource) !=
            wl_resource_get_client(event->new_surface->resource))) {
        memset(side->buttons_down, 0, sizeof(side->buttons_down));
    }
    pointer_constraints_sync(comp, event->seat);
}

static void handle_primary_pointer_focus(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, pointer_focus_primary);
    pointer_focus_changed(comp, data);
}

static void handle_guest_pointer_focus(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, pointer_focus_guest);
    pointer_focus_changed(comp, data);
}

static void pointer_listeners_add(struct compositor *comp) {
    if (comp->pointer_constraints_v1) {
        comp->new_pointer_constraint.notify = handle_new_pointer_constraint;
        wl_signal_add(&comp->pointer_constraints_v1->events.new_constraint,
            &comp->new_pointer_constraint);
    }
    comp->pointer_focus_primary.notify = handle_primary_pointer_focus;
    wl_signal_add(&comp->seat_primary->pointer_state.events.focus_change,
        &comp->pointer_focus_primary);
    comp->pointer_focus_guest.notify = handle_guest_pointer_focus;
    wl_signal_add(&comp->seat_guest->pointer_state.events.focus_change,
        &comp->pointer_focus_guest);
    comp->pointer_listeners_armed = true;
}

/* Before the display goes away, so that nothing here runs while the seats and the scene are
 * taken down. */
static void pointer_listeners_remove(struct compositor *comp) {
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        eis_side_drop(sides[i], "shutting down");
        if (sides[i]->pointer_constraint) {
            wl_list_remove(&sides[i]->pointer_constraint_destroy.link);
            sides[i]->pointer_constraint = NULL;
        }
    }
    if (!comp->pointer_listeners_armed) return;
    comp->pointer_listeners_armed = false;
    if (comp->pointer_constraints_v1) wl_list_remove(&comp->new_pointer_constraint.link);
    wl_list_remove(&comp->pointer_focus_primary.link);
    wl_list_remove(&comp->pointer_focus_guest.link);
}

/* A confinement keeps the cursor inside its surface and inside the region the client asked for. */
static void pointer_confine_point(struct side_ctx *side, int *x, int *y) {
    struct wlr_pointer_constraint_v1 *constraint = side->pointer_constraint;
    if (!constraint || constraint->type != WLR_POINTER_CONSTRAINT_V1_CONFINED ||
            !constraint->surface) {
        return;
    }
    double sx = *x - side->x, sy = *y - side->y;
    if (constraint->surface->current.width > 0 && constraint->surface->current.height > 0) {
        sx = fmin(fmax(sx, 0.0), constraint->surface->current.width - 1.0);
        sy = fmin(fmax(sy, 0.0), constraint->surface->current.height - 1.0);
    }
    const pixman_box32_t *box = &constraint->region.extents;
    if (box->x2 > box->x1 && box->y2 > box->y1) {
        double confined_x, confined_y;
        if (wlr_region_confine(&constraint->region, side->pointer_x, side->pointer_y, sx, sy,
                &confined_x, &confined_y)) {
            sx = confined_x;
            sy = confined_y;
        } else {
            sx = fmin(fmax(sx, box->x1), box->x2 - 1.0);
            sy = fmin(fmax(sy, box->y1), box->y2 - 1.0);
        }
    }
    *x = side->x + (int)floor(sx);
    *y = side->y + (int)floor(sy);
}

/* Whether a motion that belongs to `owner` may move the cursor to (*x, *y). A pointer lock holds
 * the cursor where it is, and its client gets relative motion only; a confinement keeps it
 * inside its region. */
static bool pointer_motion_allowed(struct side_ctx *owner, int *x, int *y) {
    if (side_pointer_locked(owner)) return false;
    pointer_confine_point(owner, x, y);
    return true;
}

/* Relative motion goes to the seat that owns the motion and to its focused client only. */
static void pointer_send_relative(struct compositor *comp, struct side_ctx *owner,
        uint32_t time_msec, double dx, double dy, double dx_unaccel, double dy_unaccel) {
    struct wlr_seat *seat = seat_for_side(comp, owner->id);
    if (!seat || side_input_held(comp, owner->id) || !comp->relative_pointer_manager_v1 ||
            !seat->pointer_state.focused_surface ||
            (dx == 0.0 && dy == 0.0 && dx_unaccel == 0.0 && dy_unaccel == 0.0)) {
        return;
    }
    wlr_relative_pointer_manager_v1_send_relative_motion(comp->relative_pointer_manager_v1,
        seat, (uint64_t)time_msec * 1000, dx, dy, dx_unaccel, dy_unaccel);
    /* A locked pointer takes no positions, only relative motion. A nested Gamescope takes it from
     * the Wayland path only under conditions of its own (see eis_side_relative), so it goes over
     * the side's EIS connection as well. Without EIS this does nothing. */
    if (side_pointer_locked(owner)) eis_side_relative(owner, dx_unaccel, dy_unaccel);
}

/* One relative motion that belongs to `owner`. `follow` says that it moves the shared cursor (a
 * device that follows the pointer while both sides are shown); otherwise it moves the owner's
 * own pointer inside its region. */
static void pointer_motion_relative(struct compositor *comp, struct side_ctx *owner, bool follow,
        uint32_t time_msec, double dx, double dy, double dx_unaccel, double dy_unaccel) {
    note_activity(comp);
    pointer_send_relative(comp, owner, time_msec, dx, dy, dx_unaccel, dy_unaccel);

    int x, y;
    if (follow) {
        x = comp->cursor_x + (int)dx;
        y = comp->cursor_y + (int)dy;
        if (comp->output_width > 0) {
            if (x < 0) x = 0;
            if (x >= comp->output_width) x = comp->output_width - 1;
        }
        if (comp->output_height > 0) {
            if (y < 0) y = 0;
            if (y >= comp->output_height) y = comp->output_height - 1;
        }
    } else {
        x = owner->x + owner->pointer_x + (int)dx;
        y = owner->y + owner->pointer_y + (int)dy;
        clamp_to_side(owner, &x, &y);
    }
    if (pointer_motion_allowed(owner, &x, &y)) pointer_motion_to(comp, x, y, follow);
}

/* Enters the side's application surface at the layout point (x, y) and sends a motion there. */
static void pointer_enter_side(struct compositor *comp, struct side_ctx *side, int x, int y) {
    struct wlr_seat *seat = seat_for_side(comp, side->id);
    struct wlr_surface *surface = side->app_surface;
    if (!seat || !surface || !surface->mapped || side->activity != 0 ||
            !side_is_shown(comp, side) ||
            side_input_held(comp, side->id)) {
        return;
    }
    struct wlr_surface *root = wlr_surface_get_root_surface(surface);
    struct wlr_surface *target = root;
    double sx = x - side->x, sy = y - side->y;
    if (side_draws_scaled(side)) {
        /* The toplevel is in its own units, scale times smaller than the region, and the surface under the point of its
         * tree gets the pointer, as it does when the pointer moves in. */
        side_region_to_toplevel(side, sx, sy, &sx, &sy);
        double hit_x, hit_y;
        struct wlr_surface *hit = side_scaled_surface_at(side, x, y, &hit_x, &hit_y);
        if (hit) {
            target = hit;
            sx = hit_x;
            sy = hit_y;
        }
    }
    focus_app_surface(comp, root);
    wlr_seat_pointer_notify_enter(seat, target, sx, sy);
    /* A persistent lock may have become active with the enter; it takes no positions. */
    if (!side_pointer_locked(side)) {
        if (eis_side_active(side)) {
            eis_side_warp(side);
        } else {
            wlr_seat_pointer_notify_motion(seat, pointer_now_msec(), sx, sy);
            wlr_seat_pointer_notify_frame(seat);
        }
    }
}

/* A layout change moves, resizes or hides the regions, so every position, grab and focus from
 * before it describes geometry that no longer exists. Put the cursor inside Main's region,
 * release the held buttons, drop both seats' pointer focus and the remote input's side, and enter
 * Main's application afresh. The constraints ended before the geometry changed. */
static void pointer_rehome_after_layout(struct compositor *comp) {
    struct side_ctx *primary = primary_side(comp);
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    for (size_t i = 0; i < 2; i++) {
        struct wlr_seat *seat = seat_for_side(comp, sides[i]->id);
        pointer_release_buttons(comp, sides[i]);
        sides[i]->pointer_leave_pending = false;
        if (seat && seat->pointer_state.focused_surface) {
            wlr_seat_pointer_notify_clear_focus(seat);
        }
    }
    memset(comp->remote_pressed_buttons, 0, sizeof(comp->remote_pressed_buttons));
    struct side_ctx *remote = comp->remote_input_target;
    if (remote && remote != primary) {
        release_remote_input_for_side(comp, remote, "layout changed", true);
    }
    comp->remote_input_target = NULL;
    comp->shared_pointer_side = primary;

    int x = comp->cursor_x, y = comp->cursor_y;
    clamp_to_side(primary, &x, &y);
    comp->cursor_x = x;
    comp->cursor_y = y;
    primary->pointer_x = x - primary->x;
    primary->pointer_y = y - primary->y;
    pointer_enter_side(comp, primary, x, y);
    wlr_log(WLR_INFO, "Pointer re-homed to (%d,%d) in the %s side after the layout change",
        x, y, side_name(primary->id));
}

/* The shared pointer is over `side` now. A device assigned to one side has a pointer of its own that
 * stays in that side's region and is not followed here; a device that follows the cursor carries one
 * pointer from side to side, and the side it comes from stops having it. */
static void pointer_shared_arrive(struct compositor *comp, struct side_ctx *side) {
    struct side_ctx *from = comp->shared_pointer_side;
    comp->shared_pointer_side = side;
    side->pointer_leave_pending = false;
    if (from && from != side) pointer_leave_side(comp, from);
}

/* Moves the cursor to the layout point (x, y) and tells the seat of the side under it. `shared` says that
 * the motion is the shared pointer's, a device that follows the cursor across the sides, not a pointer
 * that is assigned to one side (handle_pointer_motion): only the shared pointer ever leaves a side. */
static void pointer_motion_to(struct compositor *comp, int x, int y, bool shared) {
    note_activity(comp);

    if (comp->output_width > 0) {
        if (x < 0) x = 0;
        else if (x >= comp->output_width) x = comp->output_width - 1;
    }
    if (comp->output_height > 0) {
        if (y < 0) y = 0;
        else if (y >= comp->output_height) y = comp->output_height - 1;
    }

    comp->cursor_x = x;
    comp->cursor_y = y;
    update_cursor_visual(comp, x, y);

    struct side_ctx *ctx = side_at_point(comp, x, y);
    if (shared) pointer_shared_arrive(comp, ctx);
    ctx->pointer_x = x - ctx->x;
    ctx->pointer_y = y - ctx->y;
    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    if (!seat) return;
    if (side_input_held(comp, ctx->id)) {
        wlr_seat_pointer_notify_clear_focus(seat);
        return;
    }

    double sx = 0.0, sy = 0.0;
    struct wlr_surface *surface = surface_at(comp, (double)x, (double)y, &sx, &sy);
    uint32_t time_msec = pointer_now_msec();

    if (surface && side_has_app_for_surface(ctx, surface)) {
        if (seat->pointer_state.focused_surface != surface) {
            wlr_log(WLR_INFO, "Pointer entered app: x=%d y=%d side=%s surface=%p",
                    x, y, side_name(ctx->id), (void *)surface);
            focus_app_surface(comp, surface);
            wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
            if (eis_side_active(ctx)) eis_side_warp(ctx);
        } else if (side_pointer_locked(ctx)) {
            eis_side_unsync(ctx);
        } else if (eis_side_active(ctx)) {
            eis_side_motion(ctx);
            return;
        }

        /* A locked pointer takes no positions: its client gets relative motion only. */
        if (!side_pointer_locked(ctx) && !eis_side_active(ctx)) {
            wlr_seat_pointer_notify_motion(seat, time_msec, sx, sy);
            wlr_seat_pointer_notify_frame(seat);
        }
    } else {
        if (seat->pointer_state.focused_surface) {
            wlr_log(WLR_DEBUG, "Pointer cleared focus: x=%d y=%d", x, y);
            wlr_seat_pointer_notify_clear_focus(seat);
        }
    }
}

static void handle_pointer_motion(struct compositor *comp, int x, int y) {
    pointer_motion_to(comp, x, y, false);
}

static void handle_pointer_button(struct compositor *comp, uint32_t time_msec,
        uint32_t button, uint32_t state) {
    struct side_ctx *cursor_side = side_at_point(comp, comp->cursor_x, comp->cursor_y);

    if (state == WLR_BUTTON_RELEASED) {
        /* A release belongs to the seat that got the press, wherever the pointer is now. A
         * release of a button nothing holds has no press to end and is not passed on. */
        struct side_ctx *pressed = pointer_press_side(comp, cursor_side, button);
        if (pressed) {
            struct wlr_seat *seat = seat_for_side(comp, pressed->id);
            pointer_forget_press(pressed, button);
            if (!seat) return;
            if (eis_side_active(pressed)) {
                /* A locked pointer is not warped: its game owns the cursor position. */
                eis_side_button(pressed, button, false,
                    pressed == cursor_side && !side_pointer_locked(pressed));
                pointer_complete_leave(comp, pressed);
                return;
            }
            if (pressed == cursor_side && !side_pointer_locked(pressed)) {
                double sx = 0.0, sy = 0.0;
                struct wlr_surface *surface = surface_at(comp, (double)comp->cursor_x,
                    (double)comp->cursor_y, &sx, &sy);
                if (surface && surface == seat->pointer_state.focused_surface) {
                    wlr_seat_pointer_notify_motion(seat, time_msec, sx, sy);
                }
            }
            wlr_seat_pointer_notify_button(seat, time_msec, button, state);
            wlr_seat_pointer_notify_frame(seat);
            pointer_complete_leave(comp, pressed);
            return;
        }
        if (pointer_button_slot(button) >= 0) return;
    }

    if (side_input_held(comp, cursor_side->id)) {
        if (button == BTN_LEFT && state == WLR_BUTTON_PRESSED) {
            handle_management_overlay_click(comp, comp->cursor_x, comp->cursor_y);
        }
        return;
    }

    double sx = 0.0, sy = 0.0;
    struct wlr_surface *surface = surface_at(comp, (double)comp->cursor_x,
        (double)comp->cursor_y, &sx, &sy);
    struct side_ctx *ctx = cursor_side;
    struct wlr_seat *seat = seat_for_side(comp, ctx->id);
    if (!seat) return;

    if (surface && side_has_app_for_surface(ctx, surface)) {
        if (state == WLR_BUTTON_PRESSED) {
            if (seat->pointer_state.focused_surface != surface) {
                wlr_log(WLR_INFO, "Pointer button forcing focus: side=%s surface=%p",
                        side_name(ctx->id), (void *)surface);
                focus_app_surface(comp, surface);
                wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
            }
        }

        /* Keep nested gamescope/Steam's internal pointer coordinates fresh.
         * The motion path already sends this during cursor movement, but sending
         * the current local coordinates immediately before a button event avoids
         * clicks being interpreted at a stale inner position after focus/lifecycle
         * churn. A locked pointer takes no positions, so it gets the button alone. */
        if (eis_side_active(ctx)) {
            /* The warp restores the real position after Gamescope hid and parked its cursor. A game
             * that holds a pointer lock owns the cursor position, so it gets the button alone. */
            eis_side_button(ctx, button, state == WLR_BUTTON_PRESSED, !side_pointer_locked(ctx));
            if (state == WLR_BUTTON_PRESSED) pointer_note_press(ctx, button);
            return;
        }
        if (!side_pointer_locked(ctx)) {
            wlr_seat_pointer_notify_motion(seat, time_msec, sx, sy);
        }
        uint32_t serial = wlr_seat_pointer_notify_button(seat, time_msec, button, state);
        if (serial && state == WLR_BUTTON_PRESSED) pointer_note_press(ctx, button);
        wlr_seat_pointer_notify_frame(seat);
        return;
    }

    if (button == BTN_LEFT && state == WLR_BUTTON_PRESSED) {
        handle_pointer_click(comp, comp->cursor_x, comp->cursor_y, true, button);
    }
}
static void append_text(char *dst, size_t dst_size, const char *text) {
    size_t len = strlen(dst);
    if (len >= dst_size - 1) {
        return;
    }

    while (*text && len < dst_size - 1) {
        dst[len++] = *text++;
    }
    dst[len] = '\0';
}

static void backspace_text(char *dst) {
    size_t len = strlen(dst);
    if (len > 0) {
        dst[len - 1] = '\0';
    }
}

static void handle_keyboard_input_for_side(struct compositor *comp,
        struct side_ctx *ctx, xkb_keysym_t sym, const char *utf8) {
    note_activity(comp);

    if (!ctx) {
        return;
    }

    if (ctx->id == SIDE_LEFT) {
        comp->right.focused = UIelem_NONE;
    } else {
        comp->left.focused = UIelem_NONE;
    }

    if (ctx->auth || ctx->launch_pending) return;
    if (configured_steam_user(ctx) && ctx->state == SIDE_GREETER) {
        ctx->focused = UIelem_SIGNIN;
        /* No Dosplex credential fields in the native Steam flow. */
        if (sym != XKB_KEY_Return && sym != XKB_KEY_KP_Enter) {
            request_redraw(comp);
            return;
        }
    }
    if (ctx->focused == UIelem_NONE) {
        ctx->focused = UIelem_USERNAME;
    }

    if (sym == XKB_KEY_Tab) {
        if (ctx->focused == UIelem_USERNAME) {
            ctx->focused = UIelem_PASSWORD;
        } else if (ctx->focused == UIelem_PASSWORD) {
            ctx->focused = UIelem_SIGNIN;
        } else {
            ctx->focused = UIelem_USERNAME;
        }
        request_redraw(comp);
        return;
    }

    if (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter) {
        if (ctx->focused == UIelem_SIGNIN || ctx->focused == UIelem_PASSWORD) {
            /* Regions already include output scaling and side origin. */
            for (int i = 0; i < ctx->num_regions; i++) {
                struct ui_region *region = &ctx->regions[i];
                if (region->type == UIelem_SIGNIN) {
                    handle_pointer_click(comp, region->x + region->w / 2,
                        region->y + region->h / 2, true, BTN_LEFT);
                    break;
                }
            }
        }
        return;
    }

    if (sym == XKB_KEY_BackSpace) {
        if (ctx->focused == UIelem_USERNAME) {
            backspace_text(ctx->username);
            request_redraw(comp);
        } else if (ctx->focused == UIelem_PASSWORD) {
            backspace_text(ctx->password);
            request_redraw(comp);
        }
        return;
    }

    if (!utf8 || utf8[0] == '\0' || !isprint((unsigned char)utf8[0])) {
        return;
    }

    if (ctx->focused == UIelem_USERNAME) {
        append_text(ctx->username, sizeof(ctx->username), utf8);
        request_redraw(comp);
    } else if (ctx->focused == UIelem_PASSWORD) {
        append_text(ctx->password, sizeof(ctx->password), utf8);
        request_redraw(comp);
    }
}

static void handle_output_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct compositor *comp = wl_container_of(listener, comp, output_destroy);
    wl_list_remove(&comp->output_frame.link);
    wl_list_remove(&comp->output_destroy.link);
    wl_list_remove(&comp->output_request_state.link);
    wl_list_remove(&comp->output_present.link);
    wl_list_remove(&comp->output_commit.link);
    comp->capture_armed = false;
    comp->capture_ready = 0;
    comp->frame_pacing = FRAME_PACING_EVERY_FRAME; /* the next output proves its presents again */
    present_order_end(&comp->present_order);       /* and counts them from its own first commit */
    comp->output = NULL;
    comp->scene_dirty = true;
    /* Keep virtual monitors, windows, ownership and display selections intact.
     * A temporarily unplugged TV must not log either player out. */
    write_runtime_state(comp);
    wlr_log(WLR_INFO, "Physical output disconnected; preserving sessions");
}

/* Decide the frame pacing from the X11 output's presents (see "Present order"): a present that carries the output's
 * current commit_seq settles that the backend tags a present with the sequence its commit gets (the release's patched
 * 0.17.4), the present of the output's first commit with the sequence before its own settles the unpatched case. That
 * first present has to come back for the second, and an unpatched backend never sends the first. Other backends keep
 * the every-frame pacing. */
static void note_present_sequence(struct compositor *comp, const struct wlr_output_event_present *event) {
    if (comp->frame_pacing == FRAME_PACING_ON_DEMAND || !comp->output || event->output != comp->output ||
            !wlr_output_is_x11(comp->output)) return;
    unsigned counted_before = comp->present_order.seen;
    enum present_verdict verdict = present_order_note(&comp->present_order, event->commit_seq,
        comp->output->commit_seq);
    if (comp->frame_pacing != FRAME_PACING_EVERY_FRAME) return; /* settled; the count goes on for the frame handler */
    if (verdict == PRESENT_OWN_SEQUENCE) {
        comp->frame_pacing = FRAME_PACING_ON_DEMAND;
        wlr_log(WLR_INFO, "Frame pacing: on demand (the X11 output's present %u is its own commit's, after %u "
            "counted)", event->commit_seq, counted_before);
        present_order_end(&comp->present_order);
        write_runtime_state(comp);
    } else if (verdict == PRESENT_ONE_BEHIND) {
        comp->frame_pacing = FRAME_PACING_EVERY_FRAME_LATE_PRESENTS;
        wlr_log(WLR_INFO, "Frame pacing: every host frame. The X11 backend's presents carry the sequence of the "
            "commit before their own (a wlroots without the release's X11 presentation patch): a client that paces "
            "frames by presentation feedback runs a host frame behind");
        write_runtime_state(comp);
    }
}

/* A commit that enables or disables the output ends the count of its presents and, if it enables, starts the next
 * one with that commit. The commits in between leave the output as it is and are counted by their sequence. */
static void handle_output_commit(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, output_commit);
    struct wlr_output_event_commit *event = data;
    if (comp->frame_pacing == FRAME_PACING_ON_DEMAND || event->output != comp->output ||
            !wlr_output_is_x11(event->output) || !(event->state->committed & WLR_OUTPUT_STATE_ENABLED)) return;
    if (event->output->enabled) present_order_begin(&comp->present_order, event->output->commit_seq);
    else present_order_end(&comp->present_order);
}

static void handle_output_present(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, output_present);
    struct wlr_output_event_present *event = data;
    note_present_sequence(comp, event);
    if (!comp->capture_armed || event->commit_seq != comp->capture_commit_seq) return;
    comp->capture_armed = false;
    if (event->presented) {
        comp->capture_ready = comp->capture_submitted;
        write_runtime_state(comp);
    } else {
        request_redraw(comp);
    }
}

static void handle_output_request_state(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, output_request_state);
    struct wlr_output_event_request_state *event = data;
    if (event->output != comp->output) return;
    if (!wlr_output_commit_state(event->output, event->state)) {
        wlr_log(WLR_ERROR, "Failed to apply requested output state");
        return;
    }
    wlr_output_effective_resolution(event->output, &comp->output_width, &comp->output_height);
    apply_layout(comp);
    request_redraw(comp);
}

static void setup_output(struct compositor *comp, struct wlr_output *output) {
    /* Scene trees belong to sessions, not the lifetime of a physical monitor. */
    if (!comp->overlay_tree) comp->overlay_tree = wlr_scene_tree_create(&comp->scene->tree);
    if (!comp->apps_tree) comp->apps_tree = wlr_scene_tree_create(&comp->scene->tree);
    if (!comp->modal_tree) comp->modal_tree = wlr_scene_tree_create(&comp->scene->tree);
    /* Above the menu, which is emptied and drawn again at every change: the panel's window must outlive that. */
    if (!comp->panel_tree) comp->panel_tree = wlr_scene_tree_create(&comp->scene->tree);
    if (!comp->perf_tree) comp->perf_tree = wlr_scene_tree_create(&comp->scene->tree);
    if (!comp->cursor_tree) {
        comp->cursor_tree = wlr_scene_tree_create(&comp->scene->tree);
        if (comp->cursor_tree) {
            struct side_ctx *sides[] = {&comp->left, &comp->right};
            for (size_t i = 0; i < 2; i++) {
                sides[i]->cursor_tree = wlr_scene_tree_create(comp->cursor_tree);
                if (!sides[i]->cursor_tree) continue;
                sides[i]->cursor_rect = wlr_scene_rect_create(sides[i]->cursor_tree, 18, 18, (float *)CURSOR_COLOR);
                wlr_scene_node_set_enabled(&sides[i]->cursor_tree->node, false);
            }
        }
    }
    if (!comp->overlay_tree || !comp->apps_tree || !comp->modal_tree || !comp->perf_tree || !comp->cursor_tree ||
            !comp->left.cursor_rect || !comp->right.cursor_rect) {
        wlr_log(WLR_ERROR, "Failed to allocate scene layers");
        comp->output = NULL;
        return;
    }

    /* Initialize output rendering with allocator and renderer */
    if (!wlr_output_init_render(output, comp->allocator, comp->renderer)) {
        wlr_log(WLR_ERROR, "Failed to initialize output render");
        comp->output = NULL;
        return;
    }

    /* Enable output first - must be done before creating scene output */
    struct wlr_output_state state;
    wlr_output_state_init(&state);
    wlr_output_state_set_enabled(&state, true);

    struct wlr_output_mode *mode = wlr_output_preferred_mode(output);
    if (mode == NULL && !wl_list_empty(&output->modes)) {
        mode = wl_container_of(output->modes.next, mode, link);
    }

    if (mode != NULL) {
        wlr_output_state_set_mode(&state, mode);
        wlr_log(WLR_INFO, "Setting mode: %dx%d @ %dmHz",
            mode->width, mode->height, mode->refresh);
    } else {
        int width = output->width > 0 ? output->width : 1280;
        int height = output->height > 0 ? output->height : 800;
        /* X11 presentation is paced by the host compositor. wlroots rejects
         * an explicit refresh rate for nested X11 outputs. */
        int refresh = wlr_output_is_x11(output) ? 0 : 60000;
        wlr_output_state_set_custom_mode(&state, width, height, refresh);
    }

    /* Rotate 270° (clockwise) for the Deck's native portrait panel. */
    if (strncmp(output->name, "eDP-", 4) == 0 &&
        ((output->width == 800 && output->height == 1280) ||
        (mode && mode->width == 800 && mode->height == 1280))) {
        wlr_output_state_set_transform(&state, WL_OUTPUT_TRANSFORM_270);
        wlr_log(WLR_INFO, "Applying 270 deg transform for Steam Deck panel");
    }

    uint32_t first_commit_seq = output->commit_seq + 1;
    if (!wlr_output_commit_state(output, &state)) {
        wlr_log(WLR_ERROR, "Failed to enable output (commit failed)");
        wlr_output_state_finish(&state);
        comp->output = NULL;
        return;
    }
    wlr_output_state_finish(&state);
    /* The count of the X11 output's presents starts with this commit; its present is the first to come back. */
    if (wlr_output_is_x11(output)) present_order_begin(&comp->present_order, first_commit_seq);
    comp->output_nested_x11 = wlr_output_is_x11(output);
    wlr_log(WLR_INFO, "Direct scan-out of a side's buffer: %s", comp->output_nested_x11 ? "off (X11 output)" : "allowed");

    /* Derive scene geometry from the committed mode and transform. */
    wlr_output_effective_resolution(output, &comp->output_width, &comp->output_height);

    wlr_output_create_global(output);

    if (comp->output_layout) {
        wlr_output_layout_add(comp->output_layout, output, 0, 0);
    }

    /* Initialize session identity once. Reconnection only updates geometry. */
    struct side_ctx *sides[] = { &comp->left, &comp->right };
    for (int i = 0; i < 2; i++) {
        struct side_ctx *side = sides[i];
        if (side->side_tree) continue;
        side->comp = comp;
        side->id = i == 0 ? SIDE_LEFT : SIDE_RIGHT;
        side->role = comp->primary_side_id == side->id ? SIDE_ROLE_PRIMARY : SIDE_ROLE_GUEST;
        side->side_tree = wlr_scene_tree_create(&comp->scene->tree);
        side->width = comp->output_width;
        side->height = comp->output_height;
        side_set_default_scale(side, OUTPUT_SCALE_FIT);
        side->focused = i == 0 ? UIelem_USERNAME : UIelem_NONE;
        side_init_xdg_windows(side);
        poll_side_state(comp, side);
    }

    /* Create scene output */
    struct wlr_scene_output *scene_output = wlr_scene_output_create(comp->scene, output);
    if (!scene_output) {
        wlr_log(WLR_ERROR, "Failed to create scene output");
        comp->output = NULL;
        return;
    }
    comp->output = output;
    apply_layout(comp);
    wlr_log(WLR_INFO, "scene output created");
    wlr_log(WLR_INFO, "Side policy: %s=%s %s=%s",
        side_role_name(SIDE_ROLE_PRIMARY), side_name(comp->primary_side_id),
        side_role_name(SIDE_ROLE_GUEST), side_name(comp->guest_side_id));
    wlr_log(WLR_INFO, "Remote input policy: role=%s side=%s",
        side_role_name(comp->remote_input_role),
        side_name(side_for_role(comp, comp->remote_input_role)->id));
    wlr_log(WLR_INFO, "Logical outputs: LEFT=%dx%d (wlr=%s) RIGHT=%dx%d (wlr=%s)",
        comp->left.width, comp->left.height,
        comp->left.wlr_output ? comp->left.wlr_output->name : "(none)",
        comp->right.width, comp->right.height,
        comp->right.wlr_output ? comp->right.wlr_output->name : "(none)");

    /* Listen for frame events so we only commit when the display is ready */
    comp->output_frame.notify = handle_output_frame;
    wl_signal_add(&output->events.frame, &comp->output_frame);
    comp->output_destroy.notify = handle_output_destroy;
    wl_signal_add(&output->events.destroy, &comp->output_destroy);
    comp->output_request_state.notify = handle_output_request_state;
    wl_signal_add(&output->events.request_state, &comp->output_request_state);
    comp->output_present.notify = handle_output_present;
    wl_signal_add(&output->events.present, &comp->output_present);
    comp->output_commit.notify = handle_output_commit;
    wl_signal_add(&output->events.commit, &comp->output_commit);

    /* Mark dirty — the first frame event from wl_display_run will commit */
    comp->scene_dirty = true;

    wlr_log(WLR_INFO, "Dosplex output ready: %dx%d", output->width, output->height);
}

static void handle_new_output(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, new_output);
    struct wlr_output *output = data;

    wlr_log(WLR_INFO, "New output detected: %s (%dx%d) backend=%p",
        output->name, output->width, output->height, output->backend);

    if (output->backend == comp->logical_backend) {
        wlr_log(WLR_INFO, "Logical output detected: %s", output->name);
        if (!wlr_output_init_render(output, comp->allocator, comp->renderer)) {
            wlr_log(WLR_ERROR, "Failed to initialize logical output render");
            return;
        }
        struct wlr_output_state state;
        wlr_output_state_init(&state);
        wlr_output_state_set_enabled(&state, true);
        wlr_output_commit_state(output, &state);
        wlr_output_state_finish(&state);

        struct side_ctx *side = !comp->left.wlr_output ? &comp->left : &comp->right;
        if (side->wlr_output) {
            wlr_log(WLR_ERROR, "Unexpected extra logical output");
            return;
        }
        side->wlr_output = output;
        wlr_output_set_name(output, side == &comp->left ? "dosplex-left" : "dosplex-right");
        wlr_output_set_description(output, side == &comp->left ? "Dosplex main monitor" : "Dosplex guest monitor");
        /* Assign before publishing: the global filter needs the side identity. */
        wlr_output_layout_add(comp->output_layout, output, 0, 0);
        update_side_logical_dimensions(side, side->scale_mode);
        return;
    }

    if (comp->output == output) {
        return;
    }

    const char *selected_output = getenv("DOSPLEX_OUTPUT");
    if (selected_output && selected_output[0] && strcmp(output->name, selected_output) != 0) {
        wlr_log(WLR_INFO, "Ignoring output %s; selected connector is %s", output->name, selected_output);
        return;
    }

    if (comp->output != NULL) {
        wlr_log(WLR_INFO, "Ignoring additional physical output %s", output->name);
        return;
    }

    comp->output = output;
    setup_output(comp, output);
}

static void handle_touch_down(struct wl_listener *listener, void *data) {
    struct wlr_touch_down_event *event = data;
    struct compositor *comp = wl_container_of(listener, comp, touch_down);
    if (!comp->seat_primary || comp->output_width <= 0 || comp->output_height <= 0) {
        return;
    }

    note_activity(comp);

    double lx = 0.0, ly = 0.0;
    map_touch_to_layout(comp, event->x, event->y, &lx, &ly);

    if (comp->primary_touch_active) return;
    if (!comp->primary_touch_active) {
        comp->primary_touch_id = event->touch_id;
        comp->primary_touch_active = true;
        comp->primary_touch_side = side_at_point(comp, (int)lx, (int)ly)->id;
    }

    if (event->touch_id != comp->primary_touch_id) return;

    double sx = 0.0, sy = 0.0;
    struct wlr_surface *surface = surface_at(comp, lx, ly, &sx, &sy);
    struct side_ctx *ctx = side_at_point(comp, (int)lx, (int)ly);
    struct wlr_seat *seat = seat_for_side(comp, comp->primary_touch_side);
    comp->primary_touch_overlay = side_input_held(comp, ctx->id);
    if (comp->primary_touch_overlay) {
        handle_management_overlay_click(comp, (int)lx, (int)ly);
        return;
    }

    if (surface && side_has_app_for_surface(ctx, surface)) {
        wlr_log(WLR_INFO, "Touch down on app: x=%d y=%d side=%s surface=%p",
                (int)lx, (int)ly, side_name(ctx->id), (void *)surface);
        handle_pointer_motion(comp, (int)lx, (int)ly);
        focus_app_surface(comp, surface);
        wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
        wlr_seat_pointer_notify_button(seat, event->time_msec,
            BTN_LEFT, WLR_BUTTON_PRESSED);
        wlr_seat_touch_notify_down(seat, surface, event->time_msec, event->touch_id, sx, sy);
    } else {
        handle_pointer_motion(comp, (int)lx, (int)ly);
        handle_pointer_click(comp, (int)lx, (int)ly, true, BTN_LEFT);
    }

    wlr_seat_touch_notify_frame(seat);
}

static void handle_touch_motion(struct wl_listener *listener, void *data) {
    struct wlr_touch_motion_event *event = data;
    struct compositor *comp = wl_container_of(listener, comp, touch_motion);

    if (!comp->primary_touch_active || event->touch_id != comp->primary_touch_id ||
            comp->output_width <= 0 || comp->output_height <= 0) return;

    note_activity(comp);

    double lx = 0.0, ly = 0.0;
    map_touch_to_layout(comp, event->x, event->y, &lx, &ly);

    double sx = 0.0, sy = 0.0;
    struct wlr_surface *surface = surface_at(comp, lx, ly, &sx, &sy);
    struct side_ctx *ctx = side_at_point(comp, (int)lx, (int)ly);
    struct wlr_seat *seat = seat_for_side(comp, comp->primary_touch_side);

    if (!seat || comp->primary_touch_overlay || side_input_held(comp, comp->primary_touch_side)) return;

    /* Keep a touch sequence on the seat where it began. */
    if (ctx->id != comp->primary_touch_side) return;
    bool surface_matches_side = surface && side_has_app_for_surface(ctx, surface);

    if (event->touch_id != comp->primary_touch_id) {
        wlr_seat_touch_notify_frame(seat);
        return;
    }

    if (surface_matches_side) {
        handle_pointer_motion(comp, (int)lx, (int)ly);
        wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
        wlr_seat_pointer_notify_motion(seat, event->time_msec, sx, sy);
        wlr_seat_touch_notify_motion(seat, event->time_msec, event->touch_id, sx, sy);
    } else if (comp->primary_touch_active) {
        handle_pointer_motion(comp, (int)lx, (int)ly);
    }
    wlr_seat_touch_notify_frame(seat);
}

static void handle_touch_up(struct wl_listener *listener, void *data) {
    struct wlr_touch_up_event *event = data;
    struct compositor *comp = wl_container_of(listener, comp, touch_up);

    note_activity(comp);
    if (!comp->primary_touch_active || event->touch_id != comp->primary_touch_id) return;

    struct wlr_seat *seat = seat_for_side(comp, comp->primary_touch_side);

    if (comp->primary_touch_overlay) {
        comp->primary_touch_active = false;
        comp->primary_touch_id = -1;
        comp->primary_touch_input = NULL;
        comp->primary_touch_overlay = false;
        return;
    }

    if (comp->primary_touch_active && event->touch_id == comp->primary_touch_id) {
        if (seat) {
            wlr_seat_pointer_notify_button(seat, event->time_msec,
                BTN_LEFT, WLR_BUTTON_RELEASED);
        }
        comp->primary_touch_active = false;
        comp->primary_touch_id = -1;
        comp->primary_touch_input = NULL;
    }

    if (seat) {
        wlr_seat_touch_notify_up(seat, event->time_msec, event->touch_id);
        wlr_seat_touch_notify_frame(seat);
    }
}

static void clamp_to_side(struct side_ctx *side, int *x, int *y) {
    if (!side || !x || !y) {
        return;
    }

    if (*x < side->x) {
        *x = side->x;
    } else if (*x >= side->x + side->width) {
        *x = side->x + side->width - 1;
    }

    if (*y < side->y) {
        *y = side->y;
    } else if (*y >= side->y + side->height) {
        *y = side->y + side->height - 1;
    }
}

static void restore_side_pointer(struct compositor *comp, struct side_ctx *target) {
    int x = target->x + target->pointer_x;
    int y = target->y + target->pointer_y;
    clamp_to_side(target, &x, &y);
    handle_pointer_motion(comp, x, y);
}

/* A routed pointer's relative motion: the owner is the side the device is assigned to, or the side
 * under the shared cursor for a device that follows the pointer. */
static void handle_routed_pointer_motion(struct input_device_ctx *input, uint32_t time_msec,
        double dx, double dy, double dx_unaccel, double dy_unaccel) {
    struct compositor *comp = input->comp;
    enum input_route_mode mode = current_route_mode_for_device(comp, input);
    struct side_ctx *owner = resolve_input_target(comp, input);
    if (!owner) return;
    bool follow = (mode == INPUT_ROUTE_FOLLOW || mode == INPUT_ROUTE_BOTH) &&
        comp->layout != LAYOUT_SINGLE;
    pointer_motion_relative(comp, owner, follow, time_msec, dx, dy, dx_unaccel, dy_unaccel);
}

static void handle_routed_keyboard_modifiers(struct wl_listener *listener, void *data) {
    (void)data;
    struct input_device_ctx *input = wl_container_of(listener, input, modifiers);
    struct compositor *comp = input->comp;

    if (!comp->seat_primary || !input->keyboard || input->keyboard_wait_neutral) {
        return;
    }

    struct side_ctx *target = resolve_input_target(comp, input);
    if (!target) return;
    if (input->keyboard_overlay_wait_neutral || side_input_held(comp, target->id)) return;
    struct wlr_seat *seat = seat_for_side(comp, target->id);
    assign_seat_keyboard(comp, seat, input->keyboard);
    wlr_seat_keyboard_notify_modifiers(seat, &input->keyboard->modifiers);
}

static void handle_routed_keyboard_key(struct wl_listener *listener, void *data) {
    struct wlr_keyboard_key_event *event = data;
    struct input_device_ctx *input = wl_container_of(listener, input, key);
    struct compositor *comp = input->comp;
    struct side_ctx *target = resolve_input_target(comp, input);

    if (!target || !input->keyboard) {
        return;
    }

    /* Reassignment never carries held keys/modifiers into the new activity.
     * Revoke old focus, then ignore this device until every held key is up. */
    if (input->keyboard_target_valid && input->keyboard_target != target->id &&
            input->held_key_count > 0) {
        struct wlr_seat *old = seat_for_side(comp, input->keyboard_target);
        if (wlr_seat_get_keyboard(old) == input->keyboard) {
            wlr_seat_keyboard_notify_clear_focus(old);
            set_kbd_focus_surface(comp, old, NULL);
            wlr_seat_set_keyboard(old, seat_placeholder_keyboard(comp, old));
            side_ensure_app_keyboard_focus(comp, side_for_seat(comp, old));
        }
        input->keyboard_wait_neutral = true;
    }
    input->keyboard_target = target->id;
    input->keyboard_target_valid = true;
    if (event->keycode > KEY_MAX) return;
    bool pressed = event->state == WL_KEYBOARD_KEY_STATE_PRESSED;
    bool was_pressed = input->held_keys[event->keycode];
    if (pressed && !was_pressed) input->held_key_count++;
    if (!pressed && was_pressed) input->held_key_count--;
    input->held_keys[event->keycode] = pressed;
    if (pressed && !was_pressed) input->last_key_seconds = monotonic_seconds(); /* Swap meant this one last. */
    if (input->keyboard_wait_neutral) {
        if (input->held_key_count == 0) {
            input->keyboard_wait_neutral = false;
            input->keyboard_overlay_wait_neutral = false;
        }
        return;
    }
    if (!pressed && !was_pressed) return;
    bool menu_owns_keyboard = comp->management_overlay_visible && comp->management_overlay_owner == target->id;
    if (input->keyboard_overlay_wait_neutral && !menu_owns_keyboard) {
        if (input->held_key_count == 0) input->keyboard_overlay_wait_neutral = false;
        return;
    }
    if (menu_owns_keyboard) input->keyboard_overlay_wait_neutral = input->held_key_count > 0;

    assign_seat_keyboard(comp, seat_for_side(comp, target->id), input->keyboard);
    note_activity(comp);
    comp->keyboard = input->keyboard;
    comp->keyboard_device = input->device;

    wlr_log(WLR_DEBUG, "Routed key device=%s target=%s keycode=%u state=%u app=%p",
        input->device && input->device->name ? input->device->name : "(unknown)",
        side_name(target->id), event->keycode, event->state, (void *)target->app_surface);

    /* The overlay is the only compositor keyboard shortcut. */
    if (event->state == WL_KEYBOARD_KEY_STATE_PRESSED) {
        uint32_t modifiers = wlr_keyboard_get_modifiers(input->keyboard);
        bool super = (modifiers & WLR_MODIFIER_LOGO);
        bool ctrl = (modifiers & WLR_MODIFIER_CTRL);
        bool shift = (modifiers & WLR_MODIFIER_SHIFT);
        if ((super || ctrl) && shift) {
            xkb_keycode_t keycode = event->keycode + 8;
            const xkb_keysym_t *syms = NULL;
            int nsyms = xkb_state_key_get_syms(input->keyboard->xkb_state, keycode, &syms);
            if (nsyms > 0) {
                if (syms[0] == XKB_KEY_M || syms[0] == XKB_KEY_m) {
                    wlr_log(WLR_INFO, "Device HUD hotkey triggered on %s side", side_name(target->id));
                    request_overlay_chord(comp, target, "keyboard");
                    return;
                }
            }
        }
    }

    if (handle_overlay_key(comp, target, event->keycode, event->state,
            (wlr_keyboard_get_modifiers(input->keyboard) & WLR_MODIFIER_SHIFT) != 0)) return;
    if (side_input_held(comp, target->id)) return; /* the power progress panel */

    struct wlr_seat *seat = seat_for_side(comp, target->id);
    if (target->app_surface && target->activity == 0) {
        focus_known_side_surface(comp, target, target->app_surface, input->keyboard);
        assign_seat_keyboard(comp, seat, input->keyboard);
        wlr_seat_keyboard_notify_key(seat, event->time_msec,
            event->keycode, event->state);
        wlr_seat_keyboard_notify_modifiers(seat, &input->keyboard->modifiers);
        return;
    }

    if (event->state != WL_KEYBOARD_KEY_STATE_PRESSED) {
        return;
    }

    /* Greeter input handling */
    xkb_keycode_t keycode = event->keycode + 8;
    const xkb_keysym_t *syms = NULL;
    int nsyms = xkb_state_key_get_syms(input->keyboard->xkb_state, keycode, &syms);
    if (nsyms <= 0) {
        return;
    }

    char utf8[64] = "";
    if (xkb_state_key_get_utf8(input->keyboard->xkb_state, keycode,
            utf8, sizeof(utf8)) < 0) {
        utf8[0] = '\0';
    }
    handle_keyboard_input_for_side(comp, target, syms[0], utf8);
}

static void handle_routed_pointer_button(struct wl_listener *listener, void *data) {
    struct wlr_pointer_button_event *event = data;
    struct input_device_ctx *input = wl_container_of(listener, input, button);
    struct compositor *comp = input->comp;
    enum input_route_mode mode = current_route_mode_for_device(comp, input);
    struct side_ctx *target = resolve_input_target(comp, input);

    if (!target) {
        return;
    }

    if (mode != INPUT_ROUTE_FOLLOW && mode != INPUT_ROUTE_BOTH) {
        restore_side_pointer(comp, target);
    }

    wlr_log(WLR_DEBUG, "Routed pointer button device=%s target=%s button=%u state=%u x=%d y=%d",
        input->device && input->device->name ? input->device->name : "(unknown)",
        side_name(target->id),
        event->button, event->state, input->comp->cursor_x, input->comp->cursor_y);

    /* Ensure seat pointer focus is correct for the target side before clicking */
    if (target && target->app_surface && target->activity == 0) {
        focus_app_surface(input->comp, target->app_surface);
    }
    handle_pointer_button(input->comp, event->time_msec, event->button, event->state);
}

static void handle_routed_pointer_motion_event(struct wl_listener *listener, void *data) {
    struct wlr_pointer_motion_event *event = data;
    struct input_device_ctx *input = wl_container_of(listener, input, motion);
    handle_routed_pointer_motion(input, event->time_msec, event->delta_x, event->delta_y,
        event->unaccel_dx, event->unaccel_dy);
}

/* === Host pointer re-centring under a lock (T-423) ===
 *
 * The X11 backend, which is how Dosplex runs as a client of SteamOS's own Gamescope, has a pointer that only reports
 * where it is. A client that holds a pointer lock takes relative motion only, so handle_routed_pointer_absolute()
 * works that motion out from consecutive positions. The host pointer is a real X pointer with screen edges: once it
 * reaches one, its position stops changing, the motion worked out from it is zero, and the camera of the game stops
 * turning that way.
 *
 * While the side that owns a motion holds a lock, the host pointer is therefore moved back to the middle of the
 * output whenever it is more than a quarter of the output away from it, after the motion that carried it there has
 * been sent, so that it never gets near an edge. The move is a relative warp on a connection of the compositor's
 * own to the display that the backend uses (wlroots 0.17 does not hand out the backend's). It needs no knowledge of
 * where the output's window is on the screen, and it applies to the pointer wherever the display has it by then.
 * Other backends give true relative motion (libinput) and have no such pointer. A free pointer or a confinement
 * needs the real position of the host pointer, so without a lock nothing is moved.
 *
 * A lock can begin with the host pointer already at an edge, when the player's mouse rests there as the game grabs the
 * pointer. Pushed further, its position does not change and the display may report none, so no motion would be worked
 * out and nothing would bring it back until the mouse went another way: the first motion in that direction would be
 * lost. The host pointer is therefore also moved to the middle when a lock becomes active, and again whenever a lock
 * is taken up again, from where it was last seen. That needs a position that the pointer has reported: before one
 * there is no knowing where it is, and wlroots does not hand out the window to move it in. No motion gives the time
 * of that request, so the wait for its jump starts with the first position that arrives after it.
 *
 * The jump that the warp makes arrives like any other position, some events later. It is not motion: the
 * bookkeeping of the derived motion (abs_x, abs_y of the device) is set to where the pointer is, and no relative
 * motion is sent for it. Positions that were already on their way when the warp was asked for are still motion, and
 * are worked out from the positions before them. That is why the jump is recognized by its size, the move that was
 * asked for, and not by the place it ends at. The compositor's own cursor is not touched: a lock holds it.
 *
 * The connection is opened by the first warp. Failing to open or to use it is said once and is never fatal;
 * nothing is moved then, which is how the pointer behaved before. */

/* How far from the middle of the output, as a fraction of it on each axis, the host pointer may be before it is
 * brought back. */
#define HOST_POINTER_RECENTRE_FRACTION 0.25
/* How long, in the time of the pointer's own events, the jump of a warp is waited for, and by how many pixels its size
 * may differ from the move that was asked for (a position is whole pixels, a warp moves whole pixels). */
#define HOST_POINTER_JUMP_WAIT_MSEC 250
#define HOST_POINTER_JUMP_TOLERANCE_PX 2.0

struct host_pointer_warp {
    xcb_connection_t *connection;
    bool unavailable; /* the connection could not be opened or broke: said once, not tried again */
    bool warped_said, error_said;
};

/* Moves the host pointer by (dx, dy) pixels from wherever the display has it now, on the compositor's own
 * connection. Returns whether the request was sent. */
static bool host_pointer_warp_by(struct compositor *comp, int dx, int dy) {
    struct host_pointer_warp *warp = comp->host_warp;
    if (!warp) {
        warp = comp->host_warp = calloc(1, sizeof(*warp));
        if (!warp) return false;
    }
    if (warp->unavailable) return false;
    if (!warp->connection) {
        /* The display of the backend: it opened its own connection from the same environment. */
        xcb_connection_t *connection = xcb_connect(NULL, NULL);
        int error = xcb_connection_has_error(connection);
        if (error) {
            wlr_log(WLR_ERROR, "Host pointer re-centring under a lock is off: cannot open the X display "
                "(xcb error %d)", error);
            xcb_disconnect(connection);
            warp->unavailable = true;
            return false;
        }
        /* Never inherited by the sessions that the compositor starts. */
        int fd = xcb_get_file_descriptor(connection);
        if (fd >= 0) fcntl(fd, F_SETFD, FD_CLOEXEC);
        warp->connection = connection;
    }
    xcb_connection_t *connection = warp->connection;
    if (dx < INT16_MIN) dx = INT16_MIN;
    if (dx > INT16_MAX) dx = INT16_MAX;
    if (dy < INT16_MIN) dy = INT16_MIN;
    if (dy > INT16_MAX) dy = INT16_MAX;
    /* Neither a source nor a destination window: the pointer moves by (dx, dy) from where it is. */
    xcb_warp_pointer(connection, XCB_NONE, XCB_NONE, 0, 0, 0, 0, (int16_t)dx, (int16_t)dy);
    if (xcb_flush(connection) <= 0 || xcb_connection_has_error(connection)) {
        wlr_log(WLR_ERROR, "Host pointer re-centring under a lock is off: the X connection broke");
        xcb_disconnect(connection);
        warp->connection = NULL;
        warp->unavailable = true;
        return false;
    }
    /* Nothing is read from the connection but what the display answered with an error. */
    for (xcb_generic_event_t *event; (event = xcb_poll_for_event(connection)); free(event)) {
        if (event->response_type == 0 && !warp->error_said) {
            const xcb_generic_error_t *answer = (const xcb_generic_error_t *)event;
            warp->error_said = true;
            wlr_log(WLR_ERROR, "The display answered a host pointer warp with error %u (request %u)",
                answer->error_code, answer->major_code);
        }
    }
    if (!warp->warped_said) {
        warp->warped_said = true;
        wlr_log(WLR_INFO, "Host pointer brought back to the middle of the output under a pointer lock "
            "(first move %d,%d on %dx%d)", dx, dy, comp->output_width, comp->output_height);
    }
    return true;
}

static void host_pointer_recentre_finish(struct compositor *comp) {
    if (!comp->host_warp) return;
    if (comp->host_warp->connection) xcb_disconnect(comp->host_warp->connection);
    free(comp->host_warp);
    comp->host_warp = NULL;
}

/* Whether `input` is the pointer of the X11 backend, whose host pointer has screen edges. */
static bool host_pointer_is_x11(const struct input_device_ctx *input) {
    return input->device && wlr_input_device_is_x11(input->device);
}

/* Moves the host pointer to the middle of the output, by the distance from where `input` last saw it (abs_x, abs_y),
 * and awaits the jump of that move. Returns whether the move was sent. What the wait for the jump is counted from is
 * for the caller to say (abs_warp_msec, or abs_warp_untimed). */
static bool host_pointer_warp_to_middle(struct compositor *comp, struct input_device_ctx *input) {
    int dx = (int)lround((0.5 - input->abs_x) * comp->output_width);
    int dy = (int)lround((0.5 - input->abs_y) * comp->output_height);
    if (!host_pointer_warp_by(comp, dx, dy)) return false;
    input->abs_warp_pending = true;
    input->abs_warp_dx = (double)dx / comp->output_width;
    input->abs_warp_dy = (double)dy / comp->output_height;
    return true;
}

/* Whether the position (x, y) that `input` reports is the jump of the warp that host_pointer_recentre() or
 * host_pointer_recentre_at_lock() asked for: the pointer moved by the size that was asked for, from where it was last
 * seen. Then it is no motion. A warp whose jump does not come in time is given up, and said once: the display may not
 * honour warps. */
static bool host_pointer_jump_expected(struct compositor *comp, struct input_device_ctx *input,
        uint32_t time_msec, double x, double y) {
    if (!input->abs_warp_pending) return false;
    if (input->abs_warp_untimed) {
        /* Asked for as a lock began, so that no motion gave the time: the wait starts with the first position after
         * the request. That is the jump itself, unless positions were on their way. */
        input->abs_warp_untimed = false;
        input->abs_warp_msec = time_msec;
    }
    if ((uint32_t)(time_msec - input->abs_warp_msec) > HOST_POINTER_JUMP_WAIT_MSEC) {
        input->abs_warp_pending = false;
        if (!input->abs_warp_unconfirmed_said) {
            input->abs_warp_unconfirmed_said = true;
            wlr_log(WLR_INFO, "Host pointer re-centring: the pointer reported no jump of %.0f,%.0f pixels within "
                "%d ms; the display may not honour warps", input->abs_warp_dx * comp->output_width,
                input->abs_warp_dy * comp->output_height, HOST_POINTER_JUMP_WAIT_MSEC);
        }
        return false;
    }
    if (fabs((x - input->abs_x) - input->abs_warp_dx) > HOST_POINTER_JUMP_TOLERANCE_PX / comp->output_width ||
            fabs((y - input->abs_y) - input->abs_warp_dy) > HOST_POINTER_JUMP_TOLERANCE_PX / comp->output_height) {
        return false;
    }
    input->abs_warp_pending = false;
    if (!input->abs_warp_confirmed_said) {
        input->abs_warp_confirmed_said = true;
        wlr_log(WLR_INFO, "Host pointer re-centring: the pointer reported the jump of %.0f,%.0f pixels %u ms after "
            "the wait for it began", input->abs_warp_dx * comp->output_width,
            input->abs_warp_dy * comp->output_height, (uint32_t)(time_msec - input->abs_warp_msec));
    }
    return true;
}

/* After the motion of `input`, which `owner` has, has been consumed (input->abs_x and abs_y are its position now):
 * while `owner` holds a pointer lock, brings the host pointer back to the middle of the output if it is more than a
 * quarter of the output away from it. One warp at a time: the next waits for the jump of this one. */
static void host_pointer_recentre(struct compositor *comp, struct input_device_ctx *input,
        struct side_ctx *owner, uint32_t time_msec) {
    if (input->abs_warp_pending || !side_pointer_locked(owner) || !host_pointer_is_x11(input) ||
            comp->output_width <= 0 || comp->output_height <= 0) {
        return;
    }
    double away_x = input->abs_x - 0.5, away_y = input->abs_y - 0.5;
    if (fabs(away_x) <= HOST_POINTER_RECENTRE_FRACTION && fabs(away_y) <= HOST_POINTER_RECENTRE_FRACTION) return;
    if (!host_pointer_warp_to_middle(comp, input)) return;
    input->abs_warp_untimed = false;
    input->abs_warp_msec = time_msec;
}

/* A lock has just become active for `owner`: brings the host pointer of the X11 backend, if `owner` has its motion,
 * to the middle of the output at once, wherever it is, so that the first motion after the lock is not lost at an
 * edge that it rests at. Not for a confinement, which needs the real position. The pointer has to have reported a
 * position, and no move of it may be awaited already (that one is on its way to the middle). Nothing to move when
 * it is in the middle already. */
static void host_pointer_recentre_at_lock(struct compositor *comp, struct side_ctx *owner) {
    if (!side_pointer_locked(owner) || comp->output_width <= 0 || comp->output_height <= 0) return;
    struct input_device_ctx *input;
    wl_list_for_each(input, &comp->input_devices, link) {
        if (!input->abs_valid || input->abs_warp_pending || !host_pointer_is_x11(input) ||
                resolve_input_target(comp, input) != owner) {
            continue;
        }
        if (fabs(0.5 - input->abs_x) * comp->output_width <= HOST_POINTER_JUMP_TOLERANCE_PX &&
                fabs(0.5 - input->abs_y) * comp->output_height <= HOST_POINTER_JUMP_TOLERANCE_PX) {
            continue;
        }
        if (!host_pointer_warp_to_middle(comp, input)) return;
        input->abs_warp_untimed = true;
        wlr_log(WLR_INFO, "Host pointer brought to the middle of the output as the pointer lock of the %s side "
            "began (move %.0f,%.0f pixels)", side_name(owner->id), input->abs_warp_dx * comp->output_width,
            input->abs_warp_dy * comp->output_height);
        return; /* one host pointer, one move */
    }
}
/* === end of host pointer re-centring under a lock === */

static void handle_routed_pointer_absolute(struct wl_listener *listener, void *data) {
    struct wlr_pointer_motion_absolute_event *event = data;
    struct input_device_ctx *input = wl_container_of(listener, input, motion_abs);
    struct compositor *comp = input->comp;
    enum input_route_mode mode = current_route_mode_for_device(comp, input);
    struct side_ctx *target = resolve_input_target(comp, input);
    if (!target || comp->output_width <= 0 || comp->output_height <= 0) return;
    note_activity(comp);
    int x, y;
    double width, height; /* the area that 0..1 maps to */
    bool follow = mode == INPUT_ROUTE_FOLLOW || mode == INPUT_ROUTE_BOTH;
    if (follow) {
        width = comp->output_width;
        height = comp->output_height;
        x = event->x * width;
        y = event->y * height;
    } else {
        width = target->width;
        height = target->height;
        x = target->x + event->x * width;
        y = target->y + event->y * height;
        clamp_to_side(target, &x, &y);
    }
    /* A device that only reports where it is (the X11 backend's pointer, a tablet) cannot send
     * relative motion; a client that holds a locked pointer still needs it, so it is worked out
     * from the positions. The jump of a re-centred host pointer is not motion: the position is
     * taken as the new starting point. */
    bool recentred = host_pointer_jump_expected(comp, input, event->time_msec, event->x, event->y);
    if (input->abs_valid && !recentred) {
        double dx = (event->x - input->abs_x) * width;
        double dy = (event->y - input->abs_y) * height;
        pointer_send_relative(comp, target, event->time_msec, dx, dy, dx, dy);
    }
    input->abs_valid = true;
    input->abs_x = event->x;
    input->abs_y = event->y;
    if (pointer_motion_allowed(target, &x, &y)) pointer_motion_to(comp, x, y, follow);
    host_pointer_recentre(comp, input, target, event->time_msec);
}

static void handle_routed_pointer_axis(struct wl_listener *listener, void *data) {
    struct wlr_pointer_axis_event *event = data;
    struct input_device_ctx *input = wl_container_of(listener, input, axis);
    struct compositor *comp = input->comp;
    struct side_ctx *target = resolve_input_target(comp, input);
    if (!target) return;
    enum input_route_mode mode = current_route_mode_for_device(comp, input);
    if (mode != INPUT_ROUTE_FOLLOW && mode != INPUT_ROUTE_BOTH) restore_side_pointer(comp, target);
    note_activity(comp);
    if (side_input_held(comp, target->id)) return;
    struct wlr_seat *seat = seat_for_side(comp, target->id);
    if (!seat) return;
    if (eis_side_active(target)) {
        eis_side_scroll(target, event->orientation == WLR_AXIS_ORIENTATION_VERTICAL,
            event->delta, event->delta_discrete);
        return;
    }
    wlr_seat_pointer_notify_axis(seat, event->time_msec, event->orientation,
        event->delta, event->delta_discrete, event->source);
    wlr_seat_pointer_notify_frame(seat);
}

static void handle_routed_touch_down(struct wl_listener *listener, void *data) {
    struct input_device_ctx *input = wl_container_of(listener, input, touch_down);
    struct compositor *comp = input->comp;
    struct wlr_touch_down_event *event = data;
    if (comp->primary_touch_active) return;
    double x = 0, y = 0;
    map_touch_to_layout(comp, event->x, event->y, &x, &y);
    enum input_route_mode mode = current_route_mode_for_device(comp, input);
    struct side_ctx *target = (mode == INPUT_ROUTE_FOLLOW || mode == INPUT_ROUTE_BOTH)
        ? primary_side(comp) : resolve_input_target(comp, input);
    /* Touch follows its assigned account, never the last shared cursor position.
     * Ignore contacts outside that account's visible region; do not redirect
     * a tap intended for Guest into a different Main-side control. */
    if (!target || side_at_point(comp, (int)x, (int)y) != target) return;
    handle_touch_down(&comp->touch_down, data);
    if (comp->primary_touch_active) comp->primary_touch_input = input;
}

static void handle_routed_touch_motion(struct wl_listener *listener, void *data) {
    struct input_device_ctx *input = wl_container_of(listener, input, touch_motion);
    if (input->comp->primary_touch_input != input) return;
    handle_touch_motion(&input->comp->touch_motion, data);
}

static void handle_routed_touch_up(struct wl_listener *listener, void *data) {
    struct input_device_ctx *input = wl_container_of(listener, input, touch_up);
    if (input->comp->primary_touch_input != input) return;
    handle_touch_up(&input->comp->touch_up, data);
}

static void destroy_input_device_ctx(struct input_device_ctx *input) {
    if (!input) {
        return;
    }

    if (input->comp->primary_touch_input == input) {
        struct wlr_touch_up_event up = {.touch_id = input->comp->primary_touch_id};
        handle_touch_up(&input->comp->touch_up, &up);
    }

    if (input->key_armed) wl_list_remove(&input->key.link);
    if (input->modifiers_armed) wl_list_remove(&input->modifiers.link);
    if (input->button_armed) wl_list_remove(&input->button.link);
    if (input->motion_armed) wl_list_remove(&input->motion.link);
    if (input->motion_abs_armed) wl_list_remove(&input->motion_abs.link);
    if (input->axis_armed) wl_list_remove(&input->axis.link);
    if (input->frame_armed) wl_list_remove(&input->frame.link);
    if (input->touch_down_armed) wl_list_remove(&input->touch_down.link);
    if (input->touch_motion_armed) wl_list_remove(&input->touch_motion.link);
    if (input->touch_up_armed) wl_list_remove(&input->touch_up.link);
    if (input->touch_cancel_armed) wl_list_remove(&input->touch_cancel.link);
    if (input->touch_frame_armed) wl_list_remove(&input->touch_frame.link);
    if (input->destroy_armed) wl_list_remove(&input->destroy.link);

    if (input->comp && input->comp->keyboard_device == input->device) {
        input->comp->keyboard = NULL;
        input->comp->keyboard_device = NULL;
    }
    /* Device removal must not discard another seat's keyboard focus. The seat that held the keyboard
     * falls back to its placeholder before wlroots would leave it with none. */
    if (input->comp && input->keyboard) {
        struct wlr_seat *seats[] = {input->comp->seat_primary, input->comp->seat_guest};
        for (size_t i = 0; i < 2; i++) {
            if (seats[i] && wlr_seat_get_keyboard(seats[i]) == input->keyboard) {
                wlr_seat_keyboard_notify_clear_focus(seats[i]);
                set_kbd_focus_surface(input->comp, seats[i], NULL);
                wlr_seat_set_keyboard(seats[i], seat_placeholder_keyboard(input->comp, seats[i]));
                side_ensure_app_keyboard_focus(input->comp, side_for_seat(input->comp, seats[i]));
            }
        }
    }

    struct dynamic_input_route *route, *route_tmp;
    wl_list_for_each_safe(route, route_tmp, &input->comp->dynamic_input_routes, link) {
        if (route->device == input->device) {
            wl_list_remove(&route->link);
            free(route);
        }
    }
    wl_list_remove(&input->link);
    free(input);
}

static void handle_routed_input_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct input_device_ctx *input = wl_container_of(listener, input, destroy);
    wlr_log(WLR_INFO, "Input device removed device=%s role=%s",
        input->device && input->device->name ? input->device->name : "(unknown)",
        side_role_name(input->target_role));
    destroy_input_device_ctx(input);
}

static struct side_ctx *resolve_remote_input_target(struct compositor *comp) {
    if (!comp) {
        return NULL;
    }
    if (comp->layout == LAYOUT_SINGLE) {
        return primary_side(comp);
    }
    if (comp->remote_input_route == INPUT_ROUTE_LEFT ||
            comp->remote_input_route == INPUT_ROUTE_RIGHT) {
        return side_for_input_route(comp, comp->remote_input_route);
    }
    struct side_ctx *target = comp->remote_input_target;
    if (!target) {
        target = side_at_point(comp, comp->cursor_x, comp->cursor_y);
    }
    if (!target) {
        target = side_for_role(comp, comp->remote_input_role);
    }
    return target ? target : primary_side(comp);
}

/* The remote pointer moved onto `target`: its keys end on the side it left (its buttons stay
 * with the seat that got the press until the remote releases them) and its keyboard focus
 * follows. */
static void remote_input_retarget(struct compositor *comp, struct side_ctx *target) {
    if (comp->remote_input_target == target) return;
    if (comp->remote_input_target) {
        release_remote_input_for_side(comp, comp->remote_input_target,
            "remote pointer changed side", false);
    }
    comp->remote_input_target = target;
    if (target->app_surface && target->app_surface->mapped) {
        struct wlr_keyboard *kb = remote_keyboard(comp, target);
        focus_known_side_surface(comp, target, target->app_surface, kb);
    }
}

static bool inject_primary_key(struct compositor *comp, uint32_t keycode,
        uint32_t state) {
    struct side_ctx *target = resolve_remote_input_target(comp);
    if (!target) return false;
    struct wlr_keyboard *kb = remote_keyboard(comp, target);
    if (!kb) {
        wlr_log(WLR_ERROR, "Remote key dropped: remote keyboard is not initialized");
        return false;
    }

    if (keycode > KEY_MAX) return false;
    bool pressed = state == WL_KEYBOARD_KEY_STATE_PRESSED;
    bool was_pressed = comp->remote_pressed_keys[keycode];
    comp->remote_pressed_keys[keycode] = pressed;
    xkb_state_update_key(kb->xkb_state, keycode + 8, pressed ? XKB_KEY_DOWN : XKB_KEY_UP);
    update_remote_modifiers(kb);
    if (!pressed && !was_pressed) return true;
    bool menu_owns_keyboard = comp->management_overlay_visible && comp->management_overlay_owner == target->id;
    if (comp->remote_overlay_wait_neutral && !menu_owns_keyboard) {
        if (!remote_keys_held(comp)) comp->remote_overlay_wait_neutral = false;
        return true;
    }
    if (menu_owns_keyboard) comp->remote_overlay_wait_neutral = remote_keys_held(comp);

    wlr_log(WLR_DEBUG, "Remote key event target=%s keycode=%u state=%u app=%p",
        side_name(target->id),
        keycode, state, (void *)target->app_surface);

    /* Match the backend keyboard: only the overlay shortcut is reserved. */
    if (state == WL_KEYBOARD_KEY_STATE_PRESSED) {
        bool super = comp->remote_pressed_keys[KEY_LEFTMETA] || comp->remote_pressed_keys[KEY_RIGHTMETA];
        bool ctrl = comp->remote_pressed_keys[KEY_LEFTCTRL] || comp->remote_pressed_keys[KEY_RIGHTCTRL];
        bool shift = comp->remote_pressed_keys[KEY_LEFTSHIFT] || comp->remote_pressed_keys[KEY_RIGHTSHIFT];

        if ((super || ctrl) && shift) {
            xkb_keycode_t xkb_code = keycode + 8;
            const xkb_keysym_t *syms = NULL;
            int nsyms = xkb_state_key_get_syms(kb->xkb_state, xkb_code, &syms);
            if (nsyms > 0) {
                if (syms[0] == XKB_KEY_M || syms[0] == XKB_KEY_m) {
                    wlr_log(WLR_INFO, "Device HUD (remote) hotkey triggered on %s side", side_name(target->id));
                    request_overlay_chord(comp, target, "remote keyboard");
                    return true;
                }
            }
        }
    }

    if (handle_overlay_key(comp, target, keycode, state,
            comp->remote_pressed_keys[KEY_LEFTSHIFT] || comp->remote_pressed_keys[KEY_RIGHTSHIFT])) return true;
    if (side_input_held(comp, target->id)) return true; /* the power progress panel */

    if (target->app_surface && target->activity == 0) {
        focus_known_side_surface(comp, target, target->app_surface, kb);
        struct wlr_seat *seat = seat_for_side(comp, target->id);
        assign_seat_keyboard(comp, seat, kb);
        wlr_seat_keyboard_notify_key(seat, 0, keycode, state);
        wlr_seat_keyboard_notify_modifiers(seat, &kb->modifiers);
        return true;
    }

    if (state != WL_KEYBOARD_KEY_STATE_PRESSED) {
        return true;
    }

    target->focused = target->focused == UIelem_NONE ? UIelem_USERNAME : target->focused;
    if (target->id == SIDE_LEFT) {
        comp->right.focused = UIelem_NONE;
    } else {
        comp->left.focused = UIelem_NONE;
    }

    xkb_keycode_t xkb_code = keycode + 8;
    const xkb_keysym_t *syms = NULL;
    int nsyms = xkb_state_key_get_syms(kb->xkb_state, xkb_code, &syms);
    char utf8[64];
    if (xkb_state_key_get_utf8(kb->xkb_state, xkb_code, utf8,
            sizeof(utf8)) < 0) {
        utf8[0] = '\0';
    }
    if (nsyms > 0) {
        handle_keyboard_input_for_side(comp, target, syms[0], utf8);
    }
    return true;
}

/* One line of the remote input socket: "M x y" moves the pointer to a position of the screen,
 * "B code state" presses or releases a pointer button, "K code state" a key, "W h v" scrolls and
 * "R dx dy" moves the pointer by that much, as a mouse does (a test hook: --test-hooks). A line
 * that does not parse ends the connection. */
static bool handle_primary_input_command(struct compositor *comp, char *line) {
    char cmd, first[24], second[24], extra;
    if (sscanf(line, " %c %23s %23s %c", &cmd, first, second, &extra) != 3) return false;
    char *end;
    errno = 0;
    long value_a = strtol(first, &end, 10);
    if (errno || *end || value_a < INT_MIN || value_a > INT_MAX) return false;
    long value_b = strtol(second, &end, 10);
    if (errno || *end || value_b < INT_MIN || value_b > INT_MAX) return false;
    int a = (int)value_a, b = (int)value_b;
    if (cmd == 'K' && (a < 0 || a > KEY_MAX || (b != 0 && b != 1))) return false;
    if (cmd == 'B' && (a < BTN_LEFT || a > BTN_TASK || (b != 0 && b != 1))) return false;
    if (cmd == 'W' && (a < -1200 || a > 1200 || b < -1200 || b > 1200)) return false;
    if (cmd == 'M' && (a < -131072 || a > 131072 || b < -131072 || b > 131072)) return false;
    if (cmd == 'R' && (a < -131072 || a > 131072 || b < -131072 || b > 131072)) return false;

    /* Throttled debug log: only log every 90th input command to avoid flooding */
    static int input_cmd_count = 0;
    input_cmd_count++;
    if (input_cmd_count % 90 == 1) {
        wlr_log(WLR_DEBUG, "Primary input cmd=%c a=%d b=%d (cmd#%d)",
            cmd, a, b, input_cmd_count);
    }

    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint32_t time_msec = (uint32_t)(now.tv_sec * 1000 + now.tv_nsec / 1000000);

    switch (cmd) {
    case 'M': {
        note_activity(comp);
        /* Accept full-screen coordinates and resolve them against the current
         * dynamic layout, not the old fixed 50/50 split. */
        int full_x = a;
        int full_y = b;
        if (full_x < 0) full_x = 0;
        if (full_y < 0) full_y = 0;
        if (full_x >= comp->output_width) full_x = comp->output_width - 1;
        if (full_y >= comp->output_height) full_y = comp->output_height - 1;
        struct side_ctx *target = NULL;
        bool follow = false; /* the remote pointer is the shared pointer, not one assigned to a side */
        if (comp->layout != LAYOUT_SINGLE &&
                (comp->remote_input_route == INPUT_ROUTE_LEFT ||
                 comp->remote_input_route == INPUT_ROUTE_RIGHT)) {
            target = side_for_input_route(comp, comp->remote_input_route);
            clamp_to_side(target, &full_x, &full_y);
        } else {
            target = side_at_point(comp, full_x, full_y);
            follow = true;
        }
        if (!target) {
            target = primary_side(comp);
        }
        int local_x = full_x - target->x;
        int local_y = full_y - target->y;
        if (local_x < 0) local_x = 0;
        if (local_y < 0) local_y = 0;
        int target_width = side_logical_width(target);
        int target_height = side_logical_height(target);
        if (local_x >= target_width) local_x = target_width - 1;
        if (local_y >= target_height) local_y = target_height - 1;

        int physical_x = 0;
        int physical_y = 0;
        side_local_to_physical(target, local_x, local_y, &physical_x, &physical_y);
        /* A pointer lock holds the cursor where it is: positions do not move it, and the
         * remote cannot enter a locked side. A confinement keeps the cursor in its region. */
        if (!pointer_motion_allowed(target, &physical_x, &physical_y)) return true;

        /* If remote input target side changed, update focus */
        remote_input_retarget(comp, target);
        pointer_motion_to(comp, physical_x, physical_y, follow);
        return true;
    }
    case 'R': {
        /* Relative motion, as a libinput mouse sends it. A test hook: the suites use it to
         * exercise a locked pointer without a physical mouse. */
        if (!comp->config.test_hooks) {
            wlr_log(WLR_ERROR, "Ignoring R: the compositor was not started with --test-hooks");
            return true;
        }
        bool follow = comp->layout != LAYOUT_SINGLE &&
            comp->remote_input_route != INPUT_ROUTE_LEFT &&
            comp->remote_input_route != INPUT_ROUTE_RIGHT;
        struct side_ctx *owner = follow ? side_at_point(comp, comp->cursor_x, comp->cursor_y) :
            resolve_remote_input_target(comp);
        pointer_motion_relative(comp, owner, follow, time_msec, a, b, a, b);
        if (follow) {
            remote_input_retarget(comp, side_at_point(comp, comp->cursor_x, comp->cursor_y));
        }
        return true;
    }
    case 'B':
        restore_side_pointer(comp, resolve_remote_input_target(comp));
        note_activity(comp);
        handle_pointer_button(comp, time_msec, (uint32_t)a, (uint32_t)b);
        if (a >= 0 && a <= KEY_MAX) {
            comp->remote_pressed_buttons[a] = b != 0;
        }
        return true;
    case 'K':
        note_activity(comp);
        return inject_primary_key(comp, (uint32_t)a, (uint32_t)b);
    case 'W': {
        note_activity(comp);
        struct side_ctx *target = resolve_remote_input_target(comp);
        if (side_input_held(comp, target->id)) return true;
        struct wlr_seat *seat = seat_for_side(comp, target->id);
        if (!seat) {
            return false;
        }
        if (eis_side_active(target)) {
            if (a != 0) eis_side_scroll(target, false, (double)a, a);
            if (b != 0) eis_side_scroll(target, true, (double)b, b);
            return true;
        }
        if (a != 0) {
            wlr_seat_pointer_notify_axis(seat, time_msec,
                WL_POINTER_AXIS_HORIZONTAL_SCROLL, (double)a, a,
                WL_POINTER_AXIS_SOURCE_WHEEL);
        }
        if (b != 0) {
            wlr_seat_pointer_notify_axis(seat, time_msec,
                WL_POINTER_AXIS_VERTICAL_SCROLL, (double)b, b,
                WL_POINTER_AXIS_SOURCE_WHEEL);
        }
        wlr_seat_pointer_notify_frame(seat);
        return true;
    }
    default:
        return false;
    }
}

static void disconnect_primary_input(struct compositor *comp) {
    struct side_ctx *target = comp->remote_input_target ? comp->remote_input_target : side_for_role(comp, comp->remote_input_role);
    release_remote_input_for_side(comp, target, "input connection closed", true);
    if (comp->primary_input_client_source) {
        wl_event_source_remove(comp->primary_input_client_source);
        comp->primary_input_client_source = NULL;
    }
    if (comp->primary_input_client_fd >= 0) close(comp->primary_input_client_fd);
    comp->primary_input_client_fd = -1;
    comp->primary_input_buf_len = 0;
}

static int primary_input_client_readable(int fd, uint32_t mask, void *data) {
    (void)fd;
    struct compositor *comp = data;

    if (!(mask & WL_EVENT_READABLE) || comp->primary_input_client_fd < 0) {
        return 0;
    }

    ssize_t n = read(comp->primary_input_client_fd,
        comp->primary_input_buf + comp->primary_input_buf_len,
        sizeof(comp->primary_input_buf) - comp->primary_input_buf_len - 1);
    if (n <= 0) {
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
        disconnect_primary_input(comp);
        return 0;
    }
    if (memchr(comp->primary_input_buf + comp->primary_input_buf_len, 0, (size_t)n)) {
        disconnect_primary_input(comp);
        return 0;
    }

    comp->primary_input_buf_len += (size_t)n;
    comp->primary_input_buf[comp->primary_input_buf_len] = '\0';

    char *start = comp->primary_input_buf;
    char *newline = NULL;
    while ((newline = strchr(start, '\n')) != NULL) {
        *newline = '\0';
        if (!handle_primary_input_command(comp, start)) {
            disconnect_primary_input(comp);
            return 0;
        }
        start = newline + 1;
    }

    size_t remaining = strlen(start);
    memmove(comp->primary_input_buf, start, remaining + 1);
    comp->primary_input_buf_len = remaining;
    if (remaining > 128) disconnect_primary_input(comp);
    return 0;
}

static int primary_input_server_readable(int fd, uint32_t mask, void *data) {
    struct compositor *comp = data;
    if (!(mask & WL_EVENT_READABLE)) {
        return 0;
    }

    int client_fd = accept(fd, NULL, NULL);
    if (client_fd < 0) {
        return 0;
    }

    struct ucred peer;
    socklen_t peer_size = sizeof(peer);
    if (getsockopt(client_fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_size) < 0 || peer.uid != geteuid()) {
        close(client_fd);
        return 0;
    }
    fcntl(client_fd, F_SETFD, FD_CLOEXEC);
    fcntl(client_fd, F_SETFL, O_NONBLOCK);
    if (comp->primary_input_client_fd >= 0) disconnect_primary_input(comp);

    comp->primary_input_client_fd = client_fd;
    comp->primary_input_buf_len = 0;
    comp->primary_input_client_source = wl_event_loop_add_fd(
        wl_display_get_event_loop(comp->display), client_fd,
        WL_EVENT_READABLE, primary_input_client_readable, comp);
    return 0;
}

static void handle_new_input(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, new_input);
    struct wlr_input_device *dev = data;

    wlr_log(WLR_INFO, "New input device type=%d name=%s", dev->type, dev->name);

    if (!comp->seat_primary) {
        wlr_log(WLR_ERROR, "Input arrived before seat creation");
        return;
    }

    struct input_device_ctx *input = calloc(1, sizeof(*input));
    if (!input) {
        wlr_log(WLR_ERROR, "Failed to allocate input device context");
        return;
    }
    input->comp = comp;
    input->device = dev;
    input->route_id = ++comp->next_input_route_id;
    input->target_role = default_input_role_for_device(dev);
    wl_list_insert(&comp->input_devices, &input->link);
    input->destroy.notify = handle_routed_input_destroy;
    wl_signal_add(&dev->events.destroy, &input->destroy);
    input->destroy_armed = true;

    if (dev->type == WLR_INPUT_DEVICE_KEYBOARD) {
        if (!is_usable_keyboard_device(dev->name)) {
            wlr_log(WLR_INFO, "Ignoring pseudo keyboard device: %s", dev->name);
            destroy_input_device_ctx(input);
            return;
        }
        struct wlr_keyboard *kb = wlr_keyboard_from_input_device(dev);
        if (kb) {
            struct xkb_context *xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
            struct xkb_keymap *keymap = xkb_keymap_new_from_names(
                xkb_context, NULL, XKB_KEYMAP_COMPILE_NO_FLAGS);
            wlr_keyboard_set_keymap(kb, keymap);
            wlr_keyboard_set_repeat_info(kb, 25, 400);
            xkb_keymap_unref(keymap);
            xkb_context_unref(xkb_context);

            input->keyboard = kb;
            input->key.notify = handle_routed_keyboard_key;
            wl_signal_add(&kb->events.key, &input->key);
            input->key_armed = true;
            input->modifiers.notify = handle_routed_keyboard_modifiers;
            wl_signal_add(&kb->events.modifiers, &input->modifiers);
            input->modifiers_armed = true;

            wlr_log(WLR_INFO, "Attached routed keyboard device=%s role=%s",
                dev->name, side_role_name(input->target_role));

            struct side_ctx *target = resolve_input_target(comp, input);
            if (target && target->app_surface && target->activity == 0) {
                struct wlr_seat *seat = seat_for_side(comp, target->id);
                comp->keyboard = kb;
                comp->keyboard_device = dev;
                focus_app_surface_with_keyboard(comp, seat, target->app_surface, kb);
                wlr_log(WLR_INFO, "Focused %s side after keyboard attach: %s seat=%s",
                    side_role_name(input->target_role), side_name(target->id),
                    (seat == comp->seat_primary) ? "seat0" : "seat1");
            }
        } else {
            destroy_input_device_ctx(input);
        }
    } else if (dev->type == WLR_INPUT_DEVICE_POINTER) {
        struct wlr_pointer *pointer = wlr_pointer_from_input_device(dev);
        if (pointer) {
            input->button.notify = handle_routed_pointer_button;
            wl_signal_add(&pointer->events.button, &input->button);
            input->button_armed = true;
            input->motion.notify = handle_routed_pointer_motion_event;
            wl_signal_add(&pointer->events.motion, &input->motion);
            input->motion_armed = true;
            input->motion_abs.notify = handle_routed_pointer_absolute;
            wl_signal_add(&pointer->events.motion_absolute, &input->motion_abs);
            input->motion_abs_armed = true;
            input->axis.notify = handle_routed_pointer_axis;
            wl_signal_add(&pointer->events.axis, &input->axis);
            input->axis_armed = true;
            wlr_log(WLR_INFO, "Attached routed pointer device=%s role=%s",
                dev->name, side_role_name(input->target_role));
        } else {
            destroy_input_device_ctx(input);
        }
    } else if (dev->type == WLR_INPUT_DEVICE_TOUCH) {
        struct wlr_touch *touch = wlr_touch_from_input_device(dev);
        if (touch) {
            input->touch_down.notify = handle_routed_touch_down;
            wl_signal_add(&touch->events.down, &input->touch_down);
            input->touch_down_armed = true;
            input->touch_motion.notify = handle_routed_touch_motion;
            wl_signal_add(&touch->events.motion, &input->touch_motion);
            input->touch_motion_armed = true;
            input->touch_up.notify = handle_routed_touch_up;
            wl_signal_add(&touch->events.up, &input->touch_up);
            input->touch_up_armed = true;
            wlr_log(WLR_INFO, "Attached routed touch device=%s role=%s",
                dev->name, side_role_name(input->target_role));
        } else {
            destroy_input_device_ctx(input);
        }
    } else {
        wlr_log(WLR_INFO, "Tracking unhandled input device type=%d name=%s role=%s",
            dev->type, dev->name ? dev->name : "(unknown)",
            side_role_name(input->target_role));
    }
}

static int auth_timer_callback(void *data) {
    struct compositor *comp = data;
    if (!comp->config.allow_nested_sessions) return 0;
    struct side_ctx *sides[] = {&comp->left, &comp->right};
    bool state_changed = false, monitor_changed = false;

    for (int i = 0; i < 2; i++) {
        struct side_ctx *ctx = sides[i];
        enum side_state old_state = ctx->state;
        bool old_recovery = ctx->recovery_required;
        bool old_pending = ctx->launch_pending;
        bool old_desktop_cursor = ctx->desktop_cursor;
        char old_display_line[160], display_line[160];
        enum display_status old_display_status = side_display_status(ctx, 160, old_display_line, sizeof(old_display_line));
        struct side_monitor old_monitor = side_monitor_view(ctx);
        if (monitor_session_launcher(ctx)) state_changed = true;
        poll_side_state(comp, ctx);
        if (old_desktop_cursor != ctx->desktop_cursor || old_state != ctx->state || old_recovery != ctx->recovery_required || old_pending != ctx->launch_pending) {
            state_changed = true;
        }
        /* The Display page shows the supervisor's report on a display change, and the overlay and the
         * labels say from it whether the picture is native or scaled; state.json carries the monitor. */
        bool side_monitor_moved = side_monitor_differs(old_monitor, side_monitor_view(ctx));
        if (side_monitor_moved) monitor_changed = true;
        if (old_display_status != side_display_status(ctx, 160, display_line, sizeof(display_line)) ||
                strcmp(old_display_line, display_line) != 0 || side_monitor_moved) {
            state_changed = true;
        }
    }

    if (monitor_changed) write_runtime_state(comp);
    /* What the open overlay shows that changes with time alone: on the Devices tab the controllers the broker
     * marks active (for about half a second after a press, so the tab reads them every 250 ms), and in its header
     * the Wine or Scaled notice until it expires. A client's commits do not rebuild the overlay. */
    struct side_ctx *overlay_owner = comp->management_overlay_visible ?
        compositor_side_by_id(comp, comp->management_overlay_owner) : NULL;
    bool devices_open = overlay_owner && comp->overlay_tab == 1;
    bool notice_open = overlay_owner && overlay_owner->wine_notice[0];
    if (state_changed || devices_open || notice_open) {
        request_redraw(comp);
    }

    /* libwayland timers are one-shot — re-arm for the next poll interval */
    if (comp->auth_poll_timer) {
        wl_event_source_timer_update(comp->auth_poll_timer, devices_open ? 250 : 500);
    }

    return 0;
}

static void handle_backend_destroy(struct wl_listener *listener, void *data) {
    struct compositor *comp = wl_container_of(listener, comp, backend_destroy);
    restore_backlight(comp);
    if (comp->idle_dim_timer) {
        wl_event_source_remove(comp->idle_dim_timer);
        comp->idle_dim_timer = NULL;
    }
    if (comp->primary_input_client_source) {
        wl_event_source_remove(comp->primary_input_client_source);
        comp->primary_input_client_source = NULL;
    }

    if (comp->hud_ipc_server_source) {
        wl_event_source_remove(comp->hud_ipc_server_source);
    }
    if (comp->hud_ipc_server_fd >= 0) {
        close(comp->hud_ipc_server_fd);
    }
    if (comp->control_server_source) {
        wl_event_source_remove(comp->control_server_source);
        comp->control_server_source = NULL;
    }
    if (comp->control_server_fd >= 0) {
        close(comp->control_server_fd);
        comp->control_server_fd = -1;
        unlink(comp->control_socket_path);
    }
    struct dynamic_input_route *route, *tmp_route;
    wl_list_for_each_safe(route, tmp_route, &comp->dynamic_input_routes, link) {
        wl_list_remove(&route->link);
        free(route);
    }

    if (comp->primary_input_server_source) {
        wl_event_source_remove(comp->primary_input_server_source);
        comp->primary_input_server_source = NULL;
    }
    if (comp->primary_input_client_fd >= 0) {
        close(comp->primary_input_client_fd);
        comp->primary_input_client_fd = -1;
    }
    if (comp->primary_input_server_fd >= 0) {
        close(comp->primary_input_server_fd);
        comp->primary_input_server_fd = -1;
    }
    if (!wl_list_empty(&comp->input_devices)) {
        struct input_device_ctx *input = NULL;
        struct input_device_ctx *tmp = NULL;
        wl_list_for_each_safe(input, tmp, &comp->input_devices, link) {
            destroy_input_device_ctx(input);
        }
    }
    if (comp->remote_keyboard_initialized) {
        for (size_t i = 0; i < 2; i++) {
            wlr_keyboard_finish(&comp->remote_keyboards[i]);
            wlr_keyboard_finish(&comp->placeholder_keyboards[i]);
        }
        comp->remote_keyboard_initialized = false;
    }
    /* The DRM backend owns the GPU device and closes it when the session is destroyed; closing it here as
     * well freed it twice (a crash on every stop in DRM mode, CMP-003). Only the pointer is forgotten. */
    comp->gpu_device = NULL;
    if (comp->session) {
        wlr_session_destroy(comp->session);
        comp->session = NULL;
    }
    wl_display_terminate(comp->display);
}

static int handle_termination_signal(int signal_number, void *data) {
    (void)signal_number;
    struct compositor *comp = data;
    wl_display_terminate(comp->display);
    return 0;
}

static bool compositor_global_filter(const struct wl_client *client,
        const struct wl_global *global, void *data) {
    struct compositor *comp = data;
    const struct wl_interface *iface = wl_global_get_interface(global);

    /* The panel is known by the connection made for it, not by an account: that connection is a socket pair of
     * this process, so its credentials are the compositor's own. It sees its side's seat and its side's output. */
    if (comp->panel_client && client == comp->panel_client) {
        struct side_ctx *side = compositor_side_by_id(comp, comp->panel_side);
        if (strcmp(iface->name, "wl_seat") == 0)
            return wl_global_get_user_data(global) == seat_for_side(comp, comp->panel_side);
        if (strcmp(iface->name, "wl_output") == 0)
            return side && wl_global_get_user_data(global) == side->wlr_output;
        return true;
    }

    if (strcmp(iface->name, "wl_seat") == 0) {
        uid_t uid;
        wl_client_get_credentials((struct wl_client *)client, NULL, &uid, NULL);
        struct wlr_seat *seat = wl_global_get_user_data(global);
        return (seat == comp->seat_primary && side_matches_uid(primary_side(comp), uid)) ||
            (seat == comp->seat_guest && side_matches_uid(guest_side(comp), uid));
    }

    if (strcmp(iface->name, "wl_output") != 0) {
        return true;
    }

    /* It's an output global. Check which one it is. */
    struct wlr_output *output = wl_global_get_user_data(global);
    if (!output) {
        return true;
    }

    uid_t uid;
    wl_client_get_credentials((struct wl_client *)client, NULL, &uid, NULL);

    /* Side matching logic */
    bool is_left_user = side_matches_uid(&comp->left, uid);
    bool is_right_user = side_matches_uid(&comp->right, uid);

    if (output == comp->left.wlr_output) {
        return is_left_user;
    }
    if (output == comp->right.wlr_output) {
        return is_right_user;
    }

    /* Physical output: hide from side users to enforce isolation */
    if (output == comp->output) {
        if (is_left_user || is_right_user) {
            return false;
        }
        return true;
    }

    return true;
}

int main(int argc, char *argv[]) {
    wlr_log_init(dosplex_file_debug_enabled() ? WLR_DEBUG : WLR_INFO, NULL);

    /* The one place the command line is read. Everything below follows the
     * configuration; only backend construction looks at the backend name. */
    struct dosplex_config config;
    switch (dosplex_config_parse(&config, argc, argv, geteuid(), getenv("DOSPLEX_AUDIO_ROUTER"),
            getenv("DOSPLEX_POWER_COMMAND"))) {
        case CONFIG_RUN: break;
        case CONFIG_EXIT_OK: return 0;
        default: return 1;
    }
    /* Keep a copy, then take the variable out of the environment every child inherits: the power
     * helper gets the command only as its argument, and only from here. */
    if (config.power_command) config.power_command = strdup(config.power_command);
    if (config.power_command_ignored)
        wlr_log(WLR_ERROR, "Ignoring DOSPLEX_POWER_COMMAND: the compositor was not started with --test-hooks");
    unsetenv("DOSPLEX_POWER_COMMAND");
    wlr_log(WLR_INFO, "Runtime policy: backend=%s real_sessions=%d test_hooks=%d allow_power=%d "
        "idle_policy=%s host_keyboards=%d power_command=%s boots_into=%s audio_router=%s",
        config.backend, config.allow_nested_sessions, config.test_hooks, config.allow_power,
        idle_policy_name(config.idle_policy), config.host_keyboards,
        config.power_command ? config.power_command : "none", boots_into_name(config.boots_into),
        config.audio_router ? config.audio_router : "none");

    struct compositor *comp = calloc(1, sizeof(*comp));
    if (!comp) return 1;
    comp->config = config;
    const char *dosplex_root = getenv("DOSPLEX_ROOT");
    if (!dosplex_root || !dosplex_root[0]) dosplex_root = POWER_HELPER_ROOT_DEFAULT;
    if (dosplex_root[0] == '/') {
        snprintf(comp->power_helper, sizeof(comp->power_helper), "%s/%s", dosplex_root, POWER_HELPER_RELATIVE);
        snprintf(comp->panel_helper, sizeof(comp->panel_helper), "%s/%s", dosplex_root, PANEL_HELPER_RELATIVE);
        char form[512], why[64];
        snprintf(form, sizeof(form), "%s/%s", dosplex_root, PANEL_FEEDBACK_FORM_RELATIVE);
        comp->feedback_available = panel_helper_usable(form, why, sizeof(why));
    }
    comp->primary_touch_id = -1;
    comp->primary_input_server_fd = -1;
    comp->primary_input_client_fd = -1;
    comp->hud_ipc_server_fd = -1;
    comp->control_server_fd = -1;
    comp->hud_target_side = SIDE_LEFT;
    comp->saved_backlight_brightness = -1;
    comp->max_backlight_brightness = -1;
    comp->dim_backlight_brightness = 0;
    wl_list_init(&comp->input_devices);
    wl_list_init(&comp->dynamic_input_routes);
    comp->idle_dim_timeout_ms = IDLE_DIM_TIMEOUT_MS;
    comp->power_panel_deadline_ms = POWER_PANEL_DEADLINE_MS;
    const char *power_panel_env = getenv("DOSPLEX_POWER_PANEL_TIMEOUT_MS");
    if (power_panel_env && power_panel_env[0]) {
        char *end = NULL;
        long parsed = strtol(power_panel_env, &end, 10);
        if (!comp->config.test_hooks)
            wlr_log(WLR_ERROR, "Ignoring DOSPLEX_POWER_PANEL_TIMEOUT_MS: the compositor was not started with --test-hooks");
        else if (end && *end == '\0' && parsed >= 500 && parsed <= POWER_PANEL_DEADLINE_MS)
            comp->power_panel_deadline_ms = (int)parsed;
    }
    const char *idle_dim_timeout_env = getenv("DOSPLEX_IDLE_DIM_TIMEOUT_MS");
    if (idle_dim_timeout_env && idle_dim_timeout_env[0]) {
        char *end = NULL;
        long parsed = strtol(idle_dim_timeout_env, &end, 10);
        if (end && *end == '\0' && parsed >= 1000 && parsed <= 24L * 60L * 60L * 1000L) {
            comp->idle_dim_timeout_ms = (int)parsed;
        }
    }
    comp->map_watchdog_ms = MAP_WATCHDOG_MS;
    const char *map_watchdog_env = getenv("DOSPLEX_MAP_WATCHDOG_MS");
    if (map_watchdog_env && map_watchdog_env[0]) {
        char *end = NULL;
        long parsed = strtol(map_watchdog_env, &end, 10);
        if (end && *end == '\0' && parsed >= 100 && parsed <= 10L * 60L * 1000L) {
            comp->map_watchdog_ms = (int)parsed;
        }
    }
    /* The backlight is touched only under the dim policy, and only when this
     * machine has the panel backlight at all. */
    comp->backlight_available = config.idle_policy == IDLE_POLICY_DIM &&
        access(BACKLIGHT_BRIGHTNESS_PATH, F_OK) == 0 &&
        access(BACKLIGHT_MAX_BRIGHTNESS_PATH, F_OK) == 0;
    if (comp->backlight_available && read_int_file(BACKLIGHT_MAX_BRIGHTNESS_PATH, &comp->max_backlight_brightness) &&
            comp->max_backlight_brightness > 0) {
        comp->dim_backlight_brightness = 0;
    }
    if (comp->backlight_available && (!read_int_file(BACKLIGHT_BRIGHTNESS_PATH, &comp->saved_backlight_brightness) ||
            comp->saved_backlight_brightness < min_visible_backlight_brightness(comp))) {
        comp->saved_backlight_brightness = default_backlight_brightness(comp);
        if (write_int_file(BACKLIGHT_BRIGHTNESS_PATH, comp->saved_backlight_brightness)) {
            wlr_log(WLR_INFO, "Display backlight initialized to %d",
                comp->saved_backlight_brightness);
        }
    }
    comp->primary_side_id = parse_side_id_env(getenv("DOSPLEX_PRIMARY_SIDE"), SIDE_LEFT);
    comp->guest_side_id = parse_side_id_env(getenv("DOSPLEX_GUEST_SIDE"),
        comp->primary_side_id == SIDE_LEFT ? SIDE_RIGHT : SIDE_LEFT);
    if (comp->guest_side_id == comp->primary_side_id) {
        comp->guest_side_id = comp->primary_side_id == SIDE_LEFT ? SIDE_RIGHT : SIDE_LEFT;
    }
    comp->guest_physical_side = parse_side_id_env(getenv("DOSPLEX_GUEST_PHYSICAL_SIDE"),
        comp->guest_side_id);
    /* Main alone on the full screen is the start. "single" says so explicitly; anything that is not one of the
     * three names, a typing mistake included, also gives Full screen rather than a split nobody asked for. */
    comp->layout = LAYOUT_SINGLE;
    const char *layout_env = getenv("DOSPLEX_INITIAL_LAYOUT");
    if (layout_env && strcmp(layout_env, "vertical") == 0) {
        comp->layout = LAYOUT_SPLIT_VERTICAL;
    } else if (layout_env && strcmp(layout_env, "horizontal") == 0) {
        comp->layout = LAYOUT_SPLIT_HORIZONTAL;
    } else if (layout_env && strcmp(layout_env, "single") == 0) {
        comp->layout = LAYOUT_SINGLE;
    }
    const char *prelogin_env = getenv("DOSPLEX_GUEST_PRELOGIN");
    comp->guest_prelogin = prelogin_env && strcmp(prelogin_env, "1") == 0;
    comp->output_scale_mode = OUTPUT_SCALE_FIT;
    const char *scale_env = getenv("DOSPLEX_OUTPUT_SCALE_MODE");
    comp->output_scale_mode = parse_output_scale_mode(scale_env, OUTPUT_SCALE_FIT);
    comp->management_overlay_visible = false;
    comp->management_overlay_owner = comp->primary_side_id;
    comp->remote_input_role = parse_side_role_env(getenv("DOSPLEX_REMOTE_INPUT_ROLE"),
        SIDE_ROLE_GUEST);
    comp->remote_input_route = INPUT_ROUTE_FOLLOW;

    comp->display = wl_display_create();
    if (!comp->display) {
        wlr_log(WLR_ERROR, "Failed to create display");
        return 1;
    }
    wl_display_set_global_filter(comp->display, compositor_global_filter, comp);

    init_hud_ipc(comp);
    perf_init(comp);
    screen_brightness_init(comp);
    init_control_socket(comp);

    if (config.idle_policy == IDLE_POLICY_DIM) {
        comp->idle_dim_timer = wl_event_loop_add_timer(
            wl_display_get_event_loop(comp->display),
            idle_dim_timer_callback, comp);
        if (!comp->idle_dim_timer) {
            wlr_log(WLR_ERROR, "Failed to create idle dim timer");
            return 1;
        }
    }
    note_activity(comp);

    /* Create multi-backend to hold both physical and logical backends */
    struct wlr_backend *multi_backend = wlr_multi_backend_create(comp->display);
    if (!multi_backend) {
        wlr_log(WLR_ERROR, "Failed to create multi backend");
        return 1;
    }
    comp->backend = multi_backend;

    /* Primary backend (physical or container). This is the only code that
     * reads the backend name. */
    struct wlr_backend *primary_backend = NULL;
    if (strcmp(config.backend, "headless") == 0) {
        primary_backend = wlr_headless_backend_create(comp->display);
    } else if (strcmp(config.backend, "x11") == 0) {
        primary_backend = wlr_x11_backend_create(comp->display, NULL);
    } else if (strcmp(config.backend, "drm") == 0) {
        /* DRM backend — requires logind seat and direct /dev/dri access. */
        wlr_log(WLR_INFO, "Initializing DRM session...");
        comp->session = wlr_session_create(comp->display);
        if (!comp->session) {
            wlr_log(WLR_ERROR, "Failed to create logind session");
            return 1;
        }

        struct wlr_device *gpu_dev = NULL;
        ssize_t n = wlr_session_find_gpus(comp->session, 1, &gpu_dev);
        if (n < 1) {
            wlr_log(WLR_ERROR, "No DRM devices found");
            return 1;
        }
        comp->gpu_device = gpu_dev;

        primary_backend = wlr_drm_backend_create(comp->display, comp->session,
            comp->gpu_device, NULL);

        struct wlr_backend *libinput = wlr_libinput_backend_create(comp->display, comp->session);
        if (libinput) wlr_multi_backend_add(multi_backend, libinput);
    }

    if (primary_backend) {
        wlr_multi_backend_add(multi_backend, primary_backend);
    } else if (strcmp(config.backend, "drm") != 0) {
        wlr_log(WLR_ERROR, "Failed to create primary backend: %s", config.backend);
        return 1;
    }

    /* Logical backend for virtual side-local outputs */
    comp->logical_backend = wlr_headless_backend_create(comp->display);
    if (comp->logical_backend) {
        wlr_multi_backend_add(multi_backend, comp->logical_backend);
        wlr_log(WLR_INFO, "Logical backend (headless) added for virtual outputs");

        /* Create two virtual outputs, one for each side.
         * These will be detected by handle_new_output and assigned to sides. */
        wlr_headless_add_output(comp->logical_backend, 640, 800);
        wlr_headless_add_output(comp->logical_backend, 640, 800);
    }

    if (!comp->backend) {
        wlr_log(WLR_ERROR, "Failed to create backend");
        return 1;
    }

    /* Renderer + buffer factory protocols for nested Wayland clients */
    comp->renderer = wlr_renderer_autocreate(comp->backend);
    if (!comp->renderer) {
        wlr_log(WLR_ERROR, "Failed to create renderer");
        return 1;
    }
    if (!wlr_renderer_init_wl_display(comp->renderer, comp->display)) {
        wlr_log(WLR_ERROR, "Failed to initialize renderer wl_display support");
        return 1;
    }

    /* Allocator for buffer allocation */
    comp->allocator = wlr_allocator_autocreate(comp->backend, comp->renderer);
    if (!comp->allocator) {
        wlr_log(WLR_ERROR, "Failed to create allocator");
        return 1;
    }

    /* Scene */
    comp->scene = wlr_scene_create();
    if (!comp->scene) {
        wlr_log(WLR_ERROR, "Failed to create scene");
        return 1;
    }
    comp->output_layout = wlr_output_layout_create();
    if (!comp->output_layout) {
        wlr_log(WLR_ERROR, "Failed to create output layout");
        return 1;
    }
    comp->scene_output_layout = wlr_scene_attach_output_layout(comp->scene,
        comp->output_layout);
    if (!comp->scene_output_layout) {
        wlr_log(WLR_ERROR, "Failed to attach scene to output layout");
        return 1;
    }

    comp->linux_dmabuf_v1 = wlr_linux_dmabuf_v1_create_with_renderer(
        comp->display, 4, comp->renderer);
    if (!comp->linux_dmabuf_v1) {
        wlr_log(WLR_INFO, "Failed to create linux-dmabuf global (non-fatal, likely due to pixman renderer)");
    } else {
        wlr_scene_set_linux_dmabuf_v1(comp->scene, comp->linux_dmabuf_v1);
        wlr_log(WLR_INFO, "linux-dmabuf support enabled");
    }

    /* Enable presentation-time feedback — required by gamescope and other
     * latency-sensitive clients. wlroots handles all the protocol machinery. */
    struct wlr_presentation *presentation =
        wlr_presentation_create(comp->display, comp->backend);
    if (presentation) {
        wlr_scene_set_presentation(comp->scene, presentation);
        wlr_log(WLR_INFO, "Presentation-time support enabled");
    } else {
        wlr_log(WLR_DEBUG, "Failed to create presentation-time global");
    }

    /* Wayland protocols — clients need these to create surfaces and windows */
    comp->seat_primary = wlr_seat_create(comp->display, "seat0");
    comp->seat_guest = wlr_seat_create(comp->display, "seat1");
    if (!comp->seat_primary || !comp->seat_guest) {
        wlr_log(WLR_ERROR, "Failed to create seat");
        return 1;
    }
    wlr_seat_set_capabilities(comp->seat_primary,
        WL_SEAT_CAPABILITY_POINTER | WL_SEAT_CAPABILITY_KEYBOARD | WL_SEAT_CAPABILITY_TOUCH);
    wlr_seat_set_capabilities(comp->seat_guest,
        WL_SEAT_CAPABILITY_POINTER | WL_SEAT_CAPABILITY_KEYBOARD | WL_SEAT_CAPABILITY_TOUCH);
    if (!init_remote_keyboard(comp)) {
        return 1;
    }
    seat_ensure_keyboard(comp, comp->seat_primary);
    seat_ensure_keyboard(comp, comp->seat_guest);

    wlr_xdg_activation_v1_create(comp->display);
    wlr_log(WLR_INFO, "xdg-activation-v1 support enabled");

    /* Auto-login timer */
    comp->auto_login_timer = wl_event_loop_add_timer(
        wl_display_get_event_loop(comp->display), auto_login_timer_callback, comp);
    if (comp->auto_login_timer) {
        wl_event_source_timer_update(comp->auto_login_timer, 3000);
    }

    comp->wl_compositor = wlr_compositor_create(comp->display, 5, comp->renderer);
    if (!comp->wl_compositor) {
        wlr_log(WLR_ERROR, "Failed to create wl_compositor");
        return 1;
    }
    comp->new_client_surface.notify = handle_new_client_surface;
    wl_signal_add(&comp->wl_compositor->events.new_surface, &comp->new_client_surface);
    wlr_subcompositor_create(comp->display);
    /* Screen capture is a test hook, and real side sessions must never expose
     * cross-user screen capture: -s overrides --test-hooks. Do not loosen. */
    if (comp->config.test_hooks && !comp->config.allow_nested_sessions) {
        wlr_screencopy_manager_v1_create(comp->display);
    }
    wlr_viewporter_create(comp->display);
    wlr_single_pixel_buffer_manager_v1_create(comp->display);
    wlr_data_device_manager_create(comp->display);
    wlr_primary_selection_v1_device_manager_create(comp->display);
    comp->fractional_scale_manager_v1 =
        wlr_fractional_scale_manager_v1_create(comp->display, 1);
    if (comp->fractional_scale_manager_v1) {
        wlr_log(WLR_INFO, "Fractional-scale support enabled");
    }
    comp->relative_pointer_manager_v1 =
        wlr_relative_pointer_manager_v1_create(comp->display);
    if (comp->relative_pointer_manager_v1) {
        wlr_log(WLR_INFO, "Relative-pointer support enabled");
    }
    comp->pointer_constraints_v1 =
        wlr_pointer_constraints_v1_create(comp->display);
    if (comp->pointer_constraints_v1) {
        wlr_log(WLR_INFO, "Pointer-constraints support enabled");
    }
    pointer_listeners_add(comp);
    comp->xdg_output_manager = wlr_xdg_output_manager_v1_create(comp->display,
        comp->output_layout);
    if (!comp->xdg_output_manager) {
        wlr_log(WLR_ERROR, "Failed to create xdg_output manager");
        return 1;
    }

    comp->xdg_shell = wlr_xdg_shell_create(comp->display, 3);
    if (!comp->xdg_shell) {
        wlr_log(WLR_ERROR, "Failed to create xdg_shell");
        return 1;
    }
    comp->new_xdg_surface.notify = handle_new_xdg_surface;
    wl_signal_add(&comp->xdg_shell->events.new_surface, &comp->new_xdg_surface);

    /* XWayland — allow X11 clients to run inside Dosplex. 
     * We spawn TWO separate XWayland servers, one for each side. */
    /*
    struct side_ctx *sides[2] = {&comp->left, &comp->right};
    for (int i = 0; i < 2; i++) {
        struct side_ctx *s = sides[i];
        s->xwayland = wlr_xwayland_create(comp->display, comp->wl_compositor, false);
        if (!s->xwayland) {
            wlr_log(WLR_ERROR, "Failed to create XWayland for %s side", side_name(s->id));
        } else {
            s->new_xwayland.notify = handle_new_xwayland_surface;
            wl_signal_add(&s->xwayland->events.new_surface, &s->new_xwayland);
            s->xwayland_ready.notify = handle_xwayland_ready;
            wl_signal_add(&s->xwayland->events.ready, &s->xwayland_ready);
            
            struct wlr_seat *seat = seat_for_role(comp, s->role);
            if (seat) {
                wlr_xwayland_set_seat(s->xwayland, seat);
            }
            wlr_log(WLR_INFO, "XWayland for %s side ready (DISPLAY=%s)", 
                side_name(s->id), s->xwayland->display_name);
        }
    }
    */

    comp->cursor_mgr = wlr_xcursor_manager_create(NULL, 24);
    if (!comp->cursor_mgr) {
        wlr_log(WLR_ERROR, "Failed to create xcursor manager");
        return 1;
    }
    wlr_xcursor_manager_load(comp->cursor_mgr, 1.0);

    /* Listeners */
    comp->new_output.notify = handle_new_output;
    wl_signal_add(&comp->backend->events.new_output, &comp->new_output);

    comp->new_input.notify = handle_new_input;
    wl_signal_add(&comp->backend->events.new_input, &comp->new_input);

    comp->backend_destroy.notify = handle_backend_destroy;
    wl_signal_add(&comp->backend->events.destroy, &comp->backend_destroy);

    /* Start backend */
    if (!wlr_backend_start(comp->backend)) {
        wlr_log(WLR_ERROR, "Failed to start backend");
        return 1;
    }

    /* Add a stable socket so helpers and user-side sessions agree on it.
     * Use a shared runtime dir so all users can connect.
     * We create the socket manually with world-read/writable perms, then
     * hand the fd to wl_display_add_socket_fd so wlroots doesn't chmod it. */
    char socket_path[256];
    const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
    snprintf(socket_path, sizeof(socket_path), "%s/%s",
             (runtime_dir && runtime_dir[0]) ? runtime_dir : "/run/dosplex",
             SOCKET_NAME);

    /* Create the Unix socket, bind it, chmod to 0777, hand to wlroots */
    int sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock_fd < 0) {
        wlr_log(WLR_ERROR, "Failed to create socket: %s", strerror(errno));
        return 1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    /* Remove any stale socket at this path */
    unlink(socket_path);

    if (bind(sock_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        wlr_log(WLR_ERROR, "Failed to bind socket at %s: %s", socket_path, strerror(errno));
        close(sock_fd);
        return 1;
    }
    if (chmod(socket_path, 0777) != 0) {
        wlr_log(WLR_DEBUG, "chmod socket failed (non-fatal): %s", strerror(errno));
    }
    if (listen(sock_fd, 16) != 0) {
        wlr_log(WLR_ERROR, "Failed to listen on socket: %s", strerror(errno));
        close(sock_fd);
        return 1;
    }

    if (wl_display_add_socket_fd(comp->display, sock_fd) != 0) {
        wlr_log(WLR_ERROR, "Failed to add socket fd to display");
        close(sock_fd);
        return 1;
    }
    /* wl_display_add_socket_fd takes ownership of sock_fd — do not close it */

    setenv("WAYLAND_DISPLAY", SOCKET_NAME, 1);
    wlr_log(WLR_INFO, "Wayland socket: %s (world-accessible)", socket_path);

    char primary_input_socket_path[256];
    snprintf(primary_input_socket_path, sizeof(primary_input_socket_path),
        "%s/%s", (runtime_dir && runtime_dir[0]) ? runtime_dir : "/run/dosplex",
        PRIMARY_INPUT_SOCKET_NAME);
    comp->primary_input_server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (comp->primary_input_server_fd >= 0) {
        struct sockaddr_un primary_addr;
        memset(&primary_addr, 0, sizeof(primary_addr));
        primary_addr.sun_family = AF_UNIX;
        strncpy(primary_addr.sun_path, primary_input_socket_path,
            sizeof(primary_addr.sun_path) - 1);
        unlink(primary_input_socket_path);
        if (bind(comp->primary_input_server_fd,
                (struct sockaddr *)&primary_addr, sizeof(primary_addr)) == 0 &&
                chmod(primary_input_socket_path, 0600) == 0 &&
                listen(comp->primary_input_server_fd, 4) == 0) {
            comp->primary_input_server_source = wl_event_loop_add_fd(
                wl_display_get_event_loop(comp->display),
                comp->primary_input_server_fd, WL_EVENT_READABLE,
                primary_input_server_readable, comp);
            wlr_log(WLR_INFO, "Primary input socket: %s", primary_input_socket_path);
        } else {
            wlr_log(WLR_ERROR, "Failed to set up primary input socket at %s: %s",
                primary_input_socket_path, strerror(errno));
            close(comp->primary_input_server_fd);
            comp->primary_input_server_fd = -1;
        }
    }

    /* The headless and X11 backends have no display to enumerate, so they get
     * their one output here. This looks at the backend object, not its name. */
    if (primary_backend && wlr_backend_is_headless(primary_backend)) {
        /* The headless backend is also what the DEV_OUTPUT_* test hooks add
         * outputs to and remove them from. */
        comp->test_output_backend = primary_backend;
        wlr_headless_add_output(primary_backend, 1280, 720);
    } else if (primary_backend && wlr_backend_is_x11(primary_backend)) {
        struct wlr_output *x11_output = wlr_x11_output_create(primary_backend);
        if (!x11_output) {
            wlr_log(WLR_ERROR, "Failed to create X11 output");
            return 1;
        }
        /* create emits new_output synchronously; that handler publishes the
         * output global. Its protocol name can no longer be changed here. */
        wlr_x11_output_set_title(x11_output, "Dosplex");
        wlr_log(WLR_INFO, "Created X11 output, waiting for new_output event");
    }

    /* Auth polling timer every 500ms */
    struct wl_event_loop *loop = wl_display_get_event_loop(comp->display);
    comp->auth_poll_timer = wl_event_loop_add_timer(loop, auth_timer_callback, comp);
    if (comp->auth_poll_timer) {
        wl_event_source_timer_update(comp->auth_poll_timer, 500);
    }

    wlr_log(WLR_INFO, "Dosplex compositor running on %s (backend=%s)",
        SOCKET_NAME, config.backend);

    struct wl_event_source *term_signal = wl_event_loop_add_signal(loop, SIGTERM, handle_termination_signal, comp);
    struct wl_event_source *int_signal = wl_event_loop_add_signal(loop, SIGINT, handle_termination_signal, comp);
    wl_display_run(comp->display);
    if (term_signal) wl_event_source_remove(term_signal);
    if (int_signal) wl_event_source_remove(int_signal);

    pointer_listeners_remove(comp);
    host_pointer_recentre_finish(comp);
    finish_device_move(comp, false);
    destroy_auth(&comp->left);
    destroy_auth(&comp->right);

    /* Cleanup DRM session if used. The DRM backend owns the device and closes it when the session is
     * destroyed; closing it here as well was a double close (a crash on every stop, CMP-003). */
    comp->gpu_device = NULL;
    if (comp->session) {
        wlr_session_destroy(comp->session);
        comp->session = NULL;
    }

    if (comp->audio_timer) wl_event_source_remove(comp->audio_timer);
    if (comp->audio_pid > 0) {
        if (kill(-comp->audio_pid, SIGKILL) < 0) kill(comp->audio_pid, SIGKILL);
        waitpid(comp->audio_pid, NULL, 0);
        close(comp->audio_fd);
    }
    wl_display_destroy(comp->display);
    free(comp);
    return 0;
}
