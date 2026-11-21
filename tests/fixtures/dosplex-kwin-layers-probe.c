/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright 2026 RoyÅke (Roy-Åke Martinsson) */
// Test client for the Desktop scale: a nested KWin 6.7's Wayland backend, as its protocol log shows it.
//
// KWin 6.7 builds each output as a tree of surfaces, all in the output's logical units:
//   - the toplevel's own surface ("main"): a wp_fractional_scale_v1 and a wp_viewport, a single-pixel buffer
//     (attached once) stretched over the output by the viewport destination, set_buffer_scale(1) and a frame
//     callback and presentation feedback on every frame;
//   - five synchronized subsurfaces, made when the toplevel is configured and stacked with place_above:
//     "primary" at the origin, an output-sized buffer in device pixels with a viewport source of the whole buffer
//     and the output as destination, and "overlay1" to "overlay4", which attach no buffer (and commit that) until
//     a plane is used; overlay4 is the Plasma panel when it is, at (0, height - 62) with a buffer of 124 device
//     pixels of height and the viewport destination 62 high at scale 2.
// The subsurfaces commit before main in every frame. A configure applies the scale last announced by the
// compositor as mode = size * scale, and the next frame lays everything out again.
// The buffers are wl_shm (a dmabuf is not at hand headless); primary and panel have an alpha channel, as KWin's do.
//
// Commands on stdin, one letter each; the client answers "ok <letter>" once the compositor has them:
//   F  a frame: the 128x128 device pixels at the top left of the primary plane change (the other planes are
//      committed again with no damage)
//   K  the same, but the panel plane (and the later surfaces) say they changed everywhere, as KWin's frames show
//      for a panel that is on a plane
//   V  a frame in which only "late" changes (a 64x64 block of it), nothing of the picture: the frame of a video on a
//      plane over a part of the output
//   G  24 frames, each committed when the feedback of the one before has come (as KWin's render loop does: one
//      frame in flight), the block of the primary plane changing in each; "paced_done" follows the last one's feedback
//   g  the same with the frames of V
//   L  "late" declares its whole surface opaque (a video plane's format has no alpha)      l  it does not
//   W  a frame with every layer damaged in full
//   P  the panel plane carries the panel (and a frame)         p  it carries nothing again
//   N  "late": a subsurface made after the toplevel mapped, at (100,200), 300x100 (and a frame)
//   n  it goes (with its child)
//   C  "nested": a subsurface of "late", at (10,20) in it, 40x30 (and a frame)
//   c  it goes
//   B  "late" and "nested" together: both made before the one frame that commits them
//   U  "cover": an opaque subsurface above "late", over all of it (and a frame)      u  it goes
//   X  a frame callback on the toplevel's own surface and nothing else (no buffer change, no feedback, no damage)
//   Y  a frame callback on "late" (a commit of "late" with no damage, then of the toplevel to apply it)
//   E  a frame callback on "overlay1", which has no buffer (the same way)
//   A  a frame callback on "primary", its buffer attached again and no damage (the same way)
//   T  a frame callback on "nested" (the same way)
//   O  the primary plane declares its whole surface opaque      o  it does not
//   Q  quit
// What it prints, one line each and flushed at once:
//   ready                      preferred_scale N      the compositor's wp_fractional_scale_v1 (120ths)
//   configure WxH              the toplevel's configure, acknowledged
//   layer NAME id=N            a surface made, with its wl_surface id (the compositor's DEV_SCENE reports it)
//   mapped scale=N output=WxH device=WxH      the first frame
//   frame K                    frame K committed (K counts from 0 for the mapping frame)
//   paced_done                 the feedback of the last frame of a G has come
//   frame_done K               the frame callback of frame K            presented K SEQ / discarded K   its feedback
//                              (each of these lines ends in t=MS, CLOCK_MONOTONIC in milliseconds when it was handled)
//   ask NAME K t=MS            a frame callback requested on a surface by X, Y, E, A or T (K counts them)
//   answered NAME K t=MS       the answer to it
//   pointer_enter NAME X Y     wl_pointer, the surface's name and the point in its own units
//   pointer_motion NAME X Y    pointer_leave NAME
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"
#include "viewporter-client-protocol.h"
#include "fractional-scale-v1-client-protocol.h"
#include "single-pixel-buffer-v1-client-protocol.h"
#include "presentation-time-client-protocol.h"

#define PANEL_DEVICE_HEIGHT 124 /* the panel's buffer height in device pixels */

enum { PRIMARY, OVERLAY1, OVERLAY2, OVERLAY3, PANEL, LATE, NESTED, COVER, LAYERS };

struct layer {
    const char *name;
    struct wl_surface *surface;
    struct wl_subsurface *subsurface;
    struct wp_viewport *viewport;
    struct wl_buffer *buffer;
    int parent;            /* index of the layer it is a subsurface of; -1 for the toplevel's surface */
    int x, y, w, h;        /* in the parent's units: position and size (the viewport destination) */
    int sent_x, sent_y;    /* the position last sent; KWin sends a position only when it changes */
    int device_w, device_h;/* the buffer's size */
    int buffer_w, buffer_h;/* the buffer attached now */
    uint32_t color;
    int exists, enabled, opaque, opaque_sent;
    int opaque_w, opaque_h;/* the size of the region last sent: it follows the surface when that changes */
};

static struct wl_compositor *compositor;
static struct wl_subcompositor *subcompositor;
static struct xdg_wm_base *wm_base;
static struct wp_viewporter *viewporter;
static struct wp_fractional_scale_manager_v1 *fractional_manager;
static struct wp_single_pixel_buffer_manager_v1 *single_pixel_manager;
static struct wp_presentation *presentation;
static struct wl_shm *shm;
static struct wl_seat *seat;

static struct wl_surface *main_surface;
static struct wp_viewport *main_viewport;
static struct wl_buffer *main_buffer;
static struct layer layers[LAYERS];

static int pending_scale = 120; /* the compositor's last preferred scale, in 120ths; applied on the next configure */
static int applied_scale = 120;
static int out_w, out_h;        /* the toplevel's size: the output's logical size */
static int mapped, frame_counter;
static int paced_left;  /* frames the feedback of the one before still triggers (the G command) */
static int paced_video; /* they are the frames of V (the g command) */
static int paced_last = -1; /* the number of the last frame of a G; its feedback ends it */
#define PACED_FRAMES 24

static void say(const char *format, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *format, ...) {
    va_list args;
    va_start(args, format);
    vprintf(format, args);
    va_end(args);
    putchar('\n');
    fflush(stdout);
}

static int device(int logical) { return (logical * applied_scale + 60) / 120; }

/* CLOCK_MONOTONIC in milliseconds, with the fraction: the lines about a frame carry it as t=. */
static double now_ms(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)now.tv_sec * 1000.0 + (double)now.tv_nsec / 1000000.0;
}

static const char *label_of(struct wl_surface *surface) {
    if (surface == main_surface) return "main";
    for (int i = 0; i < LAYERS; i++) if (layers[i].exists && layers[i].surface == surface) return layers[i].name;
    return "unknown";
}

static struct wl_buffer *shm_buffer(int w, int h, uint32_t color) {
    int stride = w * 4;
    size_t size = (size_t)stride * h;
    int fd = memfd_create("kwin-layers-probe", 0);
    if (fd < 0 || ftruncate(fd, (off_t)size) < 0) { perror("buffer"); exit(1); }
    uint32_t *pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { perror("mmap"); exit(1); }
    for (size_t i = 0; i < size / 4; i++) pixels[i] = color;
    munmap(pixels, size);
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, (int32_t)size);
    struct wl_buffer *buffer = wl_shm_pool_create_buffer(pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    close(fd);
    return buffer;
}

/* --- feedback and callbacks of main -------------------------------------------------------------------------- */

static void feedback_sync_output(void *d, struct wp_presentation_feedback *f, struct wl_output *o) { (void)d; (void)f; (void)o; }
static void frame(int full, int block, int planes, int video);

/* The next frame of a G, committed on the feedback of the one before. */
static void paced_next(int answered) {
    if (answered == paced_last) {
        paced_last = -1;
        say("paced_done");
    }
    if (paced_left > 0) {
        paced_left--;
        if (paced_left == 0) paced_last = frame_counter;
        if (paced_video) frame(0, 0, 0, 1);
        else frame(0, 1, 0, 0);
    }
}

static void feedback_presented(void *d, struct wp_presentation_feedback *f, uint32_t sec_hi, uint32_t sec_lo,
        uint32_t nsec, uint32_t refresh, uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    (void)sec_hi; (void)sec_lo; (void)nsec; (void)refresh; (void)seq_hi; (void)flags;
    wp_presentation_feedback_destroy(f);
    say("presented %d %u t=%.3f", (int)(intptr_t)d, seq_lo, now_ms());
    paced_next((int)(intptr_t)d);
}
static void feedback_discarded(void *d, struct wp_presentation_feedback *f) {
    wp_presentation_feedback_destroy(f);
    say("discarded %d t=%.3f", (int)(intptr_t)d, now_ms());
    paced_next((int)(intptr_t)d);
}
static const struct wp_presentation_feedback_listener feedback_listener = {
    feedback_sync_output, feedback_presented, feedback_discarded,
};

static void frame_done(void *d, struct wl_callback *callback, uint32_t time) {
    (void)time;
    wl_callback_destroy(callback);
    say("frame_done %d t=%.3f", (int)(intptr_t)d, now_ms());
}
static const struct wl_callback_listener frame_listener = { frame_done };

/* A frame callback asked for on its own, with nothing else in the commit (X, Y, E, A, T). */
struct asked { const char *name; int number; };
static int asked_counter;
static void answered(void *d, struct wl_callback *callback, uint32_t time) {
    (void)time;
    struct asked *asked = d;
    wl_callback_destroy(callback);
    say("answered %s %d t=%.3f", asked->name, asked->number, now_ms());
    free(asked);
}
static const struct wl_callback_listener answered_listener = { answered };

static void ask_callback(struct wl_surface *surface, const char *name) {
    struct asked *asked = malloc(sizeof(*asked));
    asked->name = name;
    asked->number = asked_counter++;
    struct wl_callback *callback = wl_surface_frame(surface);
    wl_callback_add_listener(callback, &answered_listener, asked);
    say("ask %s %d t=%.3f", name, asked->number, now_ms());
}

/* --- the layers ---------------------------------------------------------------------------------------------- */

static void make_layer(int index, const char *name, int parent, int below, uint32_t color) {
    struct layer *layer = &layers[index];
    layer->name = name;
    layer->parent = parent;
    layer->color = color;
    layer->sent_x = layer->sent_y = -1;
    layer->exists = 1;
    struct wl_surface *parent_surface = parent < 0 ? main_surface : layers[parent].surface;
    layer->surface = wl_compositor_create_surface(compositor);
    layer->subsurface = wl_subcompositor_get_subsurface(subcompositor, layer->surface, parent_surface);
    layer->viewport = wp_viewporter_get_viewport(viewporter, layer->surface);
    /* Stacked above the surface made before it, the first above its parent. */
    wl_subsurface_place_above(layer->subsurface, below < 0 ? parent_surface : layers[below].surface);
    say("layer %s id=%u", name, wl_proxy_get_id((struct wl_proxy *)layer->surface));
}

static void drop_layer(int index) {
    struct layer *layer = &layers[index];
    if (!layer->exists) return;
    wl_subsurface_destroy(layer->subsurface);
    wp_viewport_destroy(layer->viewport);
    wl_surface_destroy(layer->surface);
    if (layer->buffer) wl_buffer_destroy(layer->buffer);
    memset(layer, 0, sizeof(*layer));
}

/* Where every layer goes and how large its buffer is, for the output now. */
static void lay_out(void) {
    layers[PRIMARY].x = layers[PRIMARY].y = 0;
    layers[PRIMARY].w = out_w;
    layers[PRIMARY].h = out_h;
    layers[PRIMARY].device_w = device(out_w);
    layers[PRIMARY].device_h = device(out_h);
    int panel_h = (PANEL_DEVICE_HEIGHT * 120 + applied_scale / 2) / applied_scale;
    layers[PANEL].x = 0;
    layers[PANEL].w = out_w;
    layers[PANEL].h = panel_h;
    layers[PANEL].y = out_h - panel_h;
    layers[PANEL].device_w = device(out_w);
    layers[PANEL].device_h = PANEL_DEVICE_HEIGHT;
    layers[LATE].x = 100; layers[LATE].y = 200; layers[LATE].w = 300; layers[LATE].h = 100;
    layers[LATE].device_w = device(300); layers[LATE].device_h = device(100);
    layers[NESTED].x = 10; layers[NESTED].y = 20; layers[NESTED].w = 40; layers[NESTED].h = 30;
    layers[NESTED].device_w = device(40); layers[NESTED].device_h = device(30);
    layers[COVER].x = 60; layers[COVER].y = 160; layers[COVER].w = 380; layers[COVER].h = 180;
    layers[COVER].device_w = device(380); layers[COVER].device_h = device(180);
}

/* One layer's part of a frame: a commit with its buffer (and the rectangle of it that changed, in device pixels) or,
 * for a layer that carries nothing, the nil buffer KWin attaches every frame. */
static void commit_layer(int index, int damage_x, int damage_y, int damage_w, int damage_h) {
    struct layer *layer = &layers[index];
    if (!layer->exists) return;
    if (!layer->enabled) {
        wl_surface_attach(layer->surface, NULL, 0, 0);
        wl_surface_commit(layer->surface);
        return;
    }
    if (!layer->buffer || layer->buffer_w != layer->device_w || layer->buffer_h != layer->device_h) {
        struct wl_buffer *old = layer->buffer;
        layer->buffer = shm_buffer(layer->device_w, layer->device_h, layer->color);
        layer->buffer_w = layer->device_w;
        layer->buffer_h = layer->device_h;
        if (old) wl_buffer_destroy(old);
        damage_x = damage_y = 0;
        damage_w = layer->device_w;
        damage_h = layer->device_h;
    }
    wp_viewport_set_source(layer->viewport, wl_fixed_from_int(0), wl_fixed_from_int(0),
        wl_fixed_from_int(layer->device_w), wl_fixed_from_int(layer->device_h));
    wp_viewport_set_destination(layer->viewport, layer->w, layer->h);
    if (layer->x != layer->sent_x || layer->y != layer->sent_y) {
        wl_subsurface_set_position(layer->subsurface, layer->x, layer->y);
        layer->sent_x = layer->x;
        layer->sent_y = layer->y;
    }
    if (layer->opaque != layer->opaque_sent || (layer->opaque && (layer->opaque_w != layer->w || layer->opaque_h != layer->h))) {
        if (layer->opaque) {
            struct wl_region *region = wl_compositor_create_region(compositor);
            wl_region_add(region, 0, 0, layer->w, layer->h);
            wl_surface_set_opaque_region(layer->surface, region);
            wl_region_destroy(region);
            layer->opaque_w = layer->w;
            layer->opaque_h = layer->h;
        } else {
            wl_surface_set_opaque_region(layer->surface, NULL);
        }
        layer->opaque_sent = layer->opaque;
    }
    wl_surface_attach(layer->surface, layer->buffer, 0, 0);
    if (damage_w > 0 && damage_h > 0) wl_surface_damage_buffer(layer->surface, damage_x, damage_y, damage_w, damage_h);
    wl_surface_commit(layer->surface);
}

/* A frame as KWin's WaylandOutput presents one: every layer commits (all of them synchronized subsurfaces, so what they
 * commit is applied with the next commit of main), then the toplevel's own surface, with its feedback and callback.
 * With `full` every layer says it changed everywhere; otherwise only a 128x128 block of the primary plane changed (with
 * `block`), and the layers above it say they changed everywhere (with `planes`) or are committed again as they are.
 * With `video` a 64x64 block of "late" changed too (the frame of a video on a plane). */
static void frame(int full, int block, int planes, int video) {
    lay_out();
    for (int i = LAYERS - 1; i > PRIMARY; i--) {
        int damaged = full || planes;
        if (video && i == LATE) commit_layer(i, 0, 0, 64, 64);
        else commit_layer(i, 0, 0, damaged ? layers[i].device_w : 0, damaged ? layers[i].device_h : 0);
    }
    if (full) commit_layer(PRIMARY, 0, 0, layers[PRIMARY].device_w, layers[PRIMARY].device_h);
    else commit_layer(PRIMARY, 0, 0, block ? 128 : 0, block ? 128 : 0);
    if (!main_buffer) {
        main_buffer = wp_single_pixel_buffer_manager_v1_create_u32_rgba_buffer(single_pixel_manager, 0, 0, 0, 0xffffffffu);
        wl_surface_attach(main_surface, main_buffer, 0, 0);
        wl_surface_damage_buffer(main_surface, 0, 0, 1, 1);
    }
    wp_viewport_set_destination(main_viewport, out_w, out_h);
    wl_surface_set_buffer_scale(main_surface, 1);
    struct wp_presentation_feedback *feedback = wp_presentation_feedback(presentation, main_surface);
    wp_presentation_feedback_add_listener(feedback, &feedback_listener, (void *)(intptr_t)frame_counter);
    struct wl_callback *callback = wl_surface_frame(main_surface);
    wl_callback_add_listener(callback, &frame_listener, (void *)(intptr_t)frame_counter);
    wl_surface_commit(main_surface);
    say("frame %d t=%.3f", frame_counter, now_ms());
    frame_counter++;
}

/* --- the toplevel -------------------------------------------------------------------------------------------- */

static void preferred_scale(void *d, struct wp_fractional_scale_v1 *f, uint32_t scale) {
    (void)d; (void)f;
    pending_scale = (int)scale;
    say("preferred_scale %u", scale);
}
static const struct wp_fractional_scale_v1_listener fractional_listener = { preferred_scale };

static void xs_configure(void *d, struct xdg_surface *xs, uint32_t serial) {
    (void)d;
    xdg_surface_ack_configure(xs, serial);
    if (out_w <= 0 || out_h <= 0) return;
    applied_scale = pending_scale;
    if (!mapped) {
        /* KWin makes its output layers after the first configure, before the toplevel's first buffer. */
        make_layer(PRIMARY, "primary", -1, -1, 0xff3050a0u);
        make_layer(OVERLAY1, "overlay1", -1, PRIMARY, 0xff606060u);
        make_layer(OVERLAY2, "overlay2", -1, OVERLAY1, 0xff606060u);
        make_layer(OVERLAY3, "overlay3", -1, OVERLAY2, 0xff606060u);
        make_layer(PANEL, "overlay4", -1, OVERLAY3, 0xffa05030u);
        layers[PRIMARY].enabled = 1;
        mapped = 1;
        lay_out();
        say("mapped scale=%d output=%dx%d device=%dx%d", applied_scale, out_w, out_h, device(out_w), device(out_h));
    }
    /* Every configure is a new output mode for KWin: all layers are laid out again and drawn in full. */
    frame(1, 0, 0, 0);
}
static const struct xdg_surface_listener xs_listener = { xs_configure };

static void tl_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h, struct wl_array *s) {
    (void)d; (void)t; (void)s;
    if (w > 0 && h > 0) { out_w = w; out_h = h; }
    say("configure %dx%d", w, h);
}
static void tl_close(void *d, struct xdg_toplevel *t) { (void)d; (void)t; exit(0); }
static void tl_bounds(void *d, struct xdg_toplevel *t, int32_t w, int32_t h) { (void)d; (void)t; (void)w; (void)h; }
static void tl_caps(void *d, struct xdg_toplevel *t, struct wl_array *c) { (void)d; (void)t; (void)c; }
static const struct xdg_toplevel_listener tl_listener = { tl_configure, tl_close, tl_bounds, tl_caps };

static void wm_ping(void *d, struct xdg_wm_base *w, uint32_t s) { (void)d; xdg_wm_base_pong(w, s); }
static const struct xdg_wm_base_listener wm_listener = { wm_ping };

/* --- the pointer --------------------------------------------------------------------------------------------- */

static struct wl_surface *pointer_focus;
static void ptr_enter(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) {
    (void)d; (void)p; (void)serial;
    pointer_focus = s;
    say("pointer_enter %s %.2f %.2f", label_of(s), wl_fixed_to_double(x), wl_fixed_to_double(y));
}
static void ptr_leave(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s) {
    (void)d; (void)p; (void)serial;
    pointer_focus = NULL;
    say("pointer_leave %s", label_of(s));
}
static void ptr_motion(void *d, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    (void)d; (void)p; (void)time;
    say("pointer_motion %s %.2f %.2f", label_of(pointer_focus), wl_fixed_to_double(x), wl_fixed_to_double(y));
}
static void ptr_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
    (void)d; (void)p; (void)serial; (void)time; (void)button; (void)state;
}
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) { (void)d; (void)p; (void)time; (void)axis; (void)value; }
static void ptr_frame(void *d, struct wl_pointer *p) { (void)d; (void)p; }
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t source) { (void)d; (void)p; (void)source; }
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t time, uint32_t axis) { (void)d; (void)p; (void)time; (void)axis; }
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t axis, int32_t discrete) { (void)d; (void)p; (void)axis; (void)discrete; }
static const struct wl_pointer_listener pointer_listener = {
    .enter = ptr_enter, .leave = ptr_leave, .motion = ptr_motion, .button = ptr_button, .axis = ptr_axis,
    .frame = ptr_frame, .axis_source = ptr_axis_source, .axis_stop = ptr_axis_stop, .axis_discrete = ptr_axis_discrete,
};

static void seat_capabilities(void *d, struct wl_seat *s, uint32_t caps) {
    (void)d;
    if (caps & WL_SEAT_CAPABILITY_POINTER) wl_pointer_add_listener(wl_seat_get_pointer(s), &pointer_listener, NULL);
}
static void seat_name(void *d, struct wl_seat *s, const char *n) { (void)d; (void)s; (void)n; }
static const struct wl_seat_listener seat_listener = { seat_capabilities, seat_name };

static void global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version) {
    (void)d;
    if (!strcmp(iface, "wl_compositor")) compositor = wl_registry_bind(r, name, &wl_compositor_interface, version < 4 ? version : 4);
    else if (!strcmp(iface, "wl_subcompositor")) subcompositor = wl_registry_bind(r, name, &wl_subcompositor_interface, 1);
    else if (!strcmp(iface, "xdg_wm_base")) wm_base = wl_registry_bind(r, name, &xdg_wm_base_interface, 1);
    else if (!strcmp(iface, "wp_viewporter")) viewporter = wl_registry_bind(r, name, &wp_viewporter_interface, 1);
    else if (!strcmp(iface, "wp_fractional_scale_manager_v1")) fractional_manager = wl_registry_bind(r, name, &wp_fractional_scale_manager_v1_interface, 1);
    else if (!strcmp(iface, "wp_single_pixel_buffer_manager_v1")) single_pixel_manager = wl_registry_bind(r, name, &wp_single_pixel_buffer_manager_v1_interface, 1);
    else if (!strcmp(iface, "wp_presentation")) presentation = wl_registry_bind(r, name, &wp_presentation_interface, 1);
    else if (!strcmp(iface, "wl_shm")) shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
    else if (!strcmp(iface, "wl_seat") && !seat) {
        seat = wl_registry_bind(r, name, &wl_seat_interface, version < 5 ? version : 5);
        wl_seat_add_listener(seat, &seat_listener, NULL);
    }
}
static void global_remove(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = { global, global_remove };

/* --- commands ------------------------------------------------------------------------------------------------ */

static void command(struct wl_display *display, char letter) {
    if (!mapped && letter != 'Q') { say("ok %c not mapped", letter); return; }
    switch (letter) {
    case 'F': frame(0, 1, 0, 0); break;
    case 'K': frame(0, 1, 1, 0); break;
    case 'V': frame(0, 0, 0, 1); break;
    case 'G':
    case 'g':
        paced_video = letter == 'g';
        paced_left = PACED_FRAMES;
        paced_next(-2);
        break;
    case 'L':
        layers[LATE].opaque = 1;
        frame(1, 0, 0, 0);
        break;
    case 'l':
        layers[LATE].opaque = 0;
        frame(1, 0, 0, 0);
        break;
    case 'W': frame(1, 0, 0, 0); break;
    case 'P':
        layers[PANEL].enabled = 1;
        frame(1, 0, 0, 0);
        break;
    case 'p':
        layers[PANEL].enabled = 0;
        frame(1, 0, 0, 0);
        break;
    case 'N':
        if (!layers[LATE].exists) make_layer(LATE, "late", -1, PANEL, 0xff40a040u);
        layers[LATE].enabled = 1;
        frame(1, 0, 0, 0);
        break;
    case 'n':
        drop_layer(NESTED);
        drop_layer(LATE);
        frame(1, 0, 0, 0);
        break;
    case 'C':
        if (layers[LATE].exists && !layers[NESTED].exists) make_layer(NESTED, "nested", LATE, -1, 0xffc0c040u);
        if (layers[NESTED].exists) layers[NESTED].enabled = 1;
        frame(1, 0, 0, 0);
        break;
    case 'B':
        if (!layers[LATE].exists) make_layer(LATE, "late", -1, PANEL, 0xff40a040u);
        if (!layers[NESTED].exists) make_layer(NESTED, "nested", LATE, -1, 0xffc0c040u);
        layers[LATE].enabled = layers[NESTED].enabled = 1;
        frame(1, 0, 0, 0);
        break;
    case 'c':
        drop_layer(NESTED);
        frame(1, 0, 0, 0);
        break;
    case 'O':
        layers[PRIMARY].opaque = 1;
        frame(1, 0, 0, 0);
        break;
    case 'o':
        layers[PRIMARY].opaque = 0;
        frame(1, 0, 0, 0);
        break;
    case 'U':
        if (!layers[COVER].exists) make_layer(COVER, "cover", -1, LATE, 0xff202020u);
        layers[COVER].enabled = layers[COVER].opaque = 1;
        frame(1, 0, 0, 0);
        break;
    case 'u':
        drop_layer(COVER);
        frame(1, 0, 0, 0);
        break;
    case 'X':
        ask_callback(main_surface, "main");
        wl_surface_commit(main_surface);
        break;
    case 'Y':
    case 'E':
    case 'A':
    case 'T': {
        int index = letter == 'Y' ? LATE : letter == 'E' ? OVERLAY1 : letter == 'A' ? PRIMARY : NESTED;
        if (!layers[index].exists) { say("ok %c no such layer", letter); return; }
        ask_callback(layers[index].surface, layers[index].name);
        if (letter == 'A') wl_surface_attach(layers[index].surface, layers[index].buffer, 0, 0);
        wl_surface_commit(layers[index].surface);
        /* A subsurface is synchronized: its commit is applied with the next commit of its parent. */
        wl_surface_commit(layers[index].parent < 0 ? main_surface : layers[layers[index].parent].surface);
        if (layers[index].parent >= 0) wl_surface_commit(main_surface);
        break;
    }
    case 'Q': exit(0);
    default: break;
    }
    wl_display_roundtrip(display);
    say("ok %c", letter);
}

int main(void) {
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) { fprintf(stderr, "no display\n"); return 1; }
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(display);
    if (!compositor || !subcompositor || !wm_base || !viewporter || !fractional_manager || !single_pixel_manager ||
            !presentation || !shm) {
        fprintf(stderr, "missing globals\n");
        return 1;
    }
    xdg_wm_base_add_listener(wm_base, &wm_listener, NULL);
    main_surface = wl_compositor_create_surface(compositor);
    struct xdg_surface *xs = xdg_wm_base_get_xdg_surface(wm_base, main_surface);
    xdg_surface_add_listener(xs, &xs_listener, NULL);
    struct xdg_toplevel *tl = xdg_surface_get_toplevel(xs);
    xdg_toplevel_add_listener(tl, &tl_listener, NULL);
    struct wp_fractional_scale_v1 *fractional = wp_fractional_scale_manager_v1_get_fractional_scale(fractional_manager, main_surface);
    wp_fractional_scale_v1_add_listener(fractional, &fractional_listener, NULL);
    main_viewport = wp_viewporter_get_viewport(viewporter, main_surface);
    xdg_toplevel_set_title(tl, "KDE Wayland Compositor WL-0- Output disabled");
    xdg_toplevel_set_app_id(tl, "kwin_wayland");
    xdg_toplevel_set_fullscreen(tl, NULL);
    wl_surface_commit(main_surface);
    say("layer main id=%u", wl_proxy_get_id((struct wl_proxy *)main_surface));
    say("ready");
    while (1) {
        while (wl_display_prepare_read(display) != 0) wl_display_dispatch_pending(display);
        wl_display_flush(display);
        struct pollfd fds[] = {{wl_display_get_fd(display), POLLIN, 0}, {STDIN_FILENO, POLLIN, 0}};
        if (poll(fds, 2, -1) < 0) { wl_display_cancel_read(display); if (errno == EINTR) continue; break; }
        if (fds[0].revents & POLLIN) wl_display_read_events(display); else wl_display_cancel_read(display);
        if (wl_display_dispatch_pending(display) < 0) break;
        if (fds[1].revents & (POLLIN | POLLHUP)) {
            char letter;
            if (read(STDIN_FILENO, &letter, 1) != 1) break;
            if (letter != '\n' && letter != ' ') command(display, letter);
        }
    }
    return 0;
}
