/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright 2026 RoyÅke (Roy-Åke Martinsson) */
// Test client for the Desktop scale: it behaves like a nested compositor's host window and prints,
// one line each and flushed at once, what the host compositor tells it.
//   preferred_scale N      wp_fractional_scale_v1, in 120ths
//   buffer_scale N         wl_surface.preferred_buffer_scale
//   output_scale N         wl_output.scale
//   logical_size WxH       zxdg_output_v1.logical_size
//   configure WxH          xdg_toplevel.configure (acknowledged)
// With --no-fractional it does not ask for a fractional scale (a client that cannot).
// With --late-fractional N it asks for it only after it has handled N configures, as a nested KWin can.
// With --map-delay MS (after --kwin) it waits that long before attaching its first buffer, as a real KWin does.
// With --kwin it also models KWin's nested backend: preferred_scale is only remembered, and it is applied
// on the next toplevel configure with a size (the first one included), as mode = size * scale. It then
// attaches a buffer of that mode size with a wp_viewporter destination of the configured size and prints
//   applied_scale N        the scale applied, in 120ths
//   buffer WxH             the attached buffer size
//   viewport WxH           its viewport destination
// With --pointer it also binds wl_seat and prints the pointer, in the surface's own units:
//   pointer_enter X Y      wl_pointer.enter, surface-local coordinates
//   pointer_motion X Y     wl_pointer.motion, surface-local coordinates
// X and Y are printed with two decimals.
// With --frames N (after --kwin) it paces frames as KWin's nested backend does: the mapping commit and every
// later frame ask for wp_presentation feedback, one frame is in flight at a time, and the next frame is committed
// only when the feedback of the one before has arrived. Each frame is a fresh buffer of the mode size (the colour
// alternates) with full damage, so the host has something to present. It prints
//   frame K                frame K committed (0 is the mapping commit)
//   presented K SEQ        its wp_presentation_feedback.presented; SEQ is the low word of the output's frame counter
//                          (the host's vblank count on an X11 backend), so two frames' distance is in host frames
//   discarded K 0          its wp_presentation_feedback.discarded
//   frames_done N          the feedback of the last frame has arrived
#define _GNU_SOURCE
#include <stdio.h>
#include <sys/mman.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <poll.h>
#include <unistd.h>
#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"
#include "fractional-scale-v1-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "viewporter-client-protocol.h"
#include "presentation-time-client-protocol.h"

static struct wl_compositor *compositor;
static struct xdg_wm_base *wm_base;
static struct wp_fractional_scale_manager_v1 *fractional_manager;
static struct zxdg_output_manager_v1 *output_manager;
static struct wl_output *output;
static struct wl_surface *surface;
static struct wl_shm *shm;
static struct wp_viewporter *viewporter;
static struct wp_viewport *viewport;
static struct wl_seat *seat;
static int want_pointer;
static int kwin;
static int map_delay_ms; /* --map-delay MS: the first buffer is attached that long after the first configure */
static int want_fractional = 1; /* 0 with --no-fractional */
static int late_fractional;     /* --late-fractional N: the fractional-scale object comes after N configures */
static int configures_handled;
static struct wp_fractional_scale_v1 *fractional_scale;
static struct wp_presentation *presentation;
static int frames_wanted;   /* --frames N: frames to commit after the mapping one */
static int frames_sent;     /* frames committed so far, the mapping commit included */
static int pending_scale = 120;
static int configured_w, configured_h;

static void say(const char *kind, int a, int b, int two) {
    if (two) printf("%s %dx%d\n", kind, a, b); else printf("%s %d\n", kind, a);
    fflush(stdout);
}

static struct wl_buffer *mode_buffer(int bw, int bh, uint32_t colour) {
    int stride = bw * 4;
    size_t size = (size_t)stride * bh;
    int fd = memfd_create("scale-probe", 0);
    if (fd < 0 || ftruncate(fd, (off_t)size) < 0) { perror("buffer"); exit(1); }
    uint32_t *pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { perror("mmap"); exit(1); }
    for (size_t i = 0; i < size / 4; i++) pixels[i] = colour;
    munmap(pixels, size);
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, (int32_t)size);
    struct wl_buffer *buffer = wl_shm_pool_create_buffer(pool, 0, bw, bh, stride, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    close(fd);
    return buffer;
}

static void commit_frame(void);

static void fb_sync_output(void *d, struct wp_presentation_feedback *f, struct wl_output *o) { (void)d; (void)f; (void)o; }
static void fb_done(void *d, struct wp_presentation_feedback *f, const char *what, int sequence) {
    int frame = (int)(intptr_t)d;
    wp_presentation_feedback_destroy(f);
    printf("%s %d %d\n", what, frame, sequence);
    fflush(stdout);
    if (frames_sent <= frames_wanted) commit_frame();
    else say("frames_done", frames_wanted, 0, 0);
}
static void fb_presented(void *d, struct wp_presentation_feedback *f, uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec, uint32_t refresh, uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    (void)tv_sec_hi; (void)tv_sec_lo; (void)tv_nsec; (void)refresh; (void)seq_hi; (void)flags;
    fb_done(d, f, "presented", (int)seq_lo);
}
static void fb_discarded(void *d, struct wp_presentation_feedback *f) { fb_done(d, f, "discarded", 0); }
static const struct wp_presentation_feedback_listener feedback_listener = { fb_sync_output, fb_presented, fb_discarded };

/* Ask for the feedback of the commit that follows; `frame` numbers it. */
static void request_feedback(int frame) {
    struct wp_presentation_feedback *feedback = wp_presentation_feedback(presentation, surface);
    wp_presentation_feedback_add_listener(feedback, &feedback_listener, (void *)(intptr_t)frame);
}

/* One more frame of the configured mode, with feedback. */
static void commit_frame(void) {
    int bw = (configured_w * pending_scale + 60) / 120, bh = (configured_h * pending_scale + 60) / 120;
    wl_surface_attach(surface, mode_buffer(bw, bh, frames_sent % 2 ? 0xff203040u : 0xffc0d0e0u), 0, 0);
    wl_surface_damage_buffer(surface, 0, 0, bw, bh);
    request_feedback(frames_sent);
    say("frame", frames_sent, 0, 0);
    frames_sent++;
    wl_surface_commit(surface);
}

static void preferred_scale(void *d, struct wp_fractional_scale_v1 *f, uint32_t scale) { (void)d; (void)f; pending_scale = (int)scale; say("preferred_scale", (int)scale, 0, 0); }
static const struct wp_fractional_scale_v1_listener fractional_listener = { preferred_scale };

static void ask_fractional_scale(void) {
    if (!want_fractional || fractional_scale) return;
    fractional_scale = wp_fractional_scale_manager_v1_get_fractional_scale(fractional_manager, surface);
    wp_fractional_scale_v1_add_listener(fractional_scale, &fractional_listener, NULL);
}

static void surface_enter(void *d, struct wl_surface *s, struct wl_output *o) { (void)d; (void)s; (void)o; }
static void surface_leave(void *d, struct wl_surface *s, struct wl_output *o) { (void)d; (void)s; (void)o; }
static void surface_buffer_scale(void *d, struct wl_surface *s, int32_t f) { (void)d; (void)s; say("buffer_scale", f, 0, 0); }
static void surface_transform(void *d, struct wl_surface *s, uint32_t t) { (void)d; (void)s; (void)t; }
static const struct wl_surface_listener surface_listener = { surface_enter, surface_leave, surface_buffer_scale, surface_transform };

static void out_geometry(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph, int32_t sp, const char *mk, const char *md, int32_t t) { (void)d; (void)o; (void)x; (void)y; (void)pw; (void)ph; (void)sp; (void)mk; (void)md; (void)t; }
static void out_mode(void *d, struct wl_output *o, uint32_t f, int32_t w, int32_t h, int32_t r) { (void)d; (void)o; (void)f; (void)w; (void)h; (void)r; }
static void out_done(void *d, struct wl_output *o) { (void)d; (void)o; }
static void out_scale(void *d, struct wl_output *o, int32_t f) { (void)d; (void)o; say("output_scale", f, 0, 0); }
static void out_name(void *d, struct wl_output *o, const char *n) { (void)d; (void)o; (void)n; }
static void out_description(void *d, struct wl_output *o, const char *n) { (void)d; (void)o; (void)n; }
static const struct wl_output_listener output_listener = { out_geometry, out_mode, out_done, out_scale, out_name, out_description };

static void xo_position(void *d, struct zxdg_output_v1 *x, int32_t a, int32_t b) { (void)d; (void)x; (void)a; (void)b; }
static void xo_size(void *d, struct zxdg_output_v1 *x, int32_t w, int32_t h) { (void)d; (void)x; say("logical_size", w, h, 1); }
static void xo_done(void *d, struct zxdg_output_v1 *x) { (void)d; (void)x; }
static void xo_name(void *d, struct zxdg_output_v1 *x, const char *n) { (void)d; (void)x; (void)n; }
static void xo_desc(void *d, struct zxdg_output_v1 *x, const char *n) { (void)d; (void)x; (void)n; }
static const struct zxdg_output_v1_listener xdg_output_listener = { xo_position, xo_size, xo_done, xo_name, xo_desc };

static void say_pointer(const char *kind, wl_fixed_t x, wl_fixed_t y) {
    printf("%s %.2f %.2f\n", kind, wl_fixed_to_double(x), wl_fixed_to_double(y));
    fflush(stdout);
}
static void ptr_enter(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) { (void)d; (void)p; (void)serial; (void)s; say_pointer("pointer_enter", x, y); }
static void ptr_leave(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s) { (void)d; (void)p; (void)serial; (void)s; }
static void ptr_motion(void *d, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) { (void)d; (void)p; (void)time; say_pointer("pointer_motion", x, y); }
static void ptr_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) { (void)d; (void)p; (void)serial; (void)time; (void)button; (void)state; }
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) { (void)d; (void)p; (void)time; (void)axis; (void)value; }
static void ptr_frame(void *d, struct wl_pointer *p) { (void)d; (void)p; }
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t source) { (void)d; (void)p; (void)source; }
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t time, uint32_t axis) { (void)d; (void)p; (void)time; (void)axis; }
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t axis, int32_t discrete) { (void)d; (void)p; (void)axis; (void)discrete; }
static const struct wl_pointer_listener pointer_listener = { ptr_enter, ptr_leave, ptr_motion, ptr_button, ptr_axis, ptr_frame, ptr_axis_source, ptr_axis_stop, ptr_axis_discrete };

static void seat_capabilities(void *d, struct wl_seat *s, uint32_t caps) {
    (void)d;
    if (want_pointer && (caps & WL_SEAT_CAPABILITY_POINTER)) wl_pointer_add_listener(wl_seat_get_pointer(s), &pointer_listener, NULL);
}
static void seat_name(void *d, struct wl_seat *s, const char *n) { (void)d; (void)s; (void)n; }
static const struct wl_seat_listener seat_listener = { seat_capabilities, seat_name };

static void wm_ping(void *d, struct xdg_wm_base *w, uint32_t s) { (void)d; xdg_wm_base_pong(w, s); }
static const struct xdg_wm_base_listener wm_listener = { wm_ping };

static void xs_configure(void *d, struct xdg_surface *x, uint32_t serial) {
    (void)d;
    xdg_surface_ack_configure(x, serial);
    if (kwin && configured_w > 0 && configured_h > 0) {
        if (map_delay_ms > 0) { usleep((useconds_t)map_delay_ms * 1000); map_delay_ms = 0; }
        int bw = (configured_w * pending_scale + 60) / 120, bh = (configured_h * pending_scale + 60) / 120;
        wp_viewport_set_destination(viewport, configured_w, configured_h);
        wl_surface_attach(surface, mode_buffer(bw, bh, 0xff000000u), 0, 0);
        wl_surface_damage_buffer(surface, 0, 0, bw, bh);
        say("applied_scale", pending_scale, 0, 0);
        say("buffer", bw, bh, 1);
        say("viewport", configured_w, configured_h, 1);
        if (frames_wanted > 0 && frames_sent == 0) {
            /* The mapping commit is frame 0; the feedback drives the rest. */
            request_feedback(0);
            say("frame", 0, 0, 0);
            frames_sent = 1;
        }
    }
    wl_surface_commit(surface);
    if (late_fractional && ++configures_handled >= late_fractional) ask_fractional_scale();
}
static const struct xdg_surface_listener xs_listener = { xs_configure };

static void tl_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h, struct wl_array *s) { (void)d; (void)t; (void)s; configured_w = w; configured_h = h; say("configure", w, h, 1); }
static void tl_close(void *d, struct xdg_toplevel *t) { (void)d; (void)t; exit(0); }
static void tl_bounds(void *d, struct xdg_toplevel *t, int32_t w, int32_t h) { (void)d; (void)t; (void)w; (void)h; }
static void tl_caps(void *d, struct xdg_toplevel *t, struct wl_array *c) { (void)d; (void)t; (void)c; }
static const struct xdg_toplevel_listener tl_listener = { tl_configure, tl_close, tl_bounds, tl_caps };

static void global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version) {
    (void)d;
    if (!strcmp(iface, "wl_compositor")) compositor = wl_registry_bind(r, name, &wl_compositor_interface, version < 6 ? version : 6);
    else if (!strcmp(iface, "xdg_wm_base")) wm_base = wl_registry_bind(r, name, &xdg_wm_base_interface, 1);
    else if (!strcmp(iface, "wp_fractional_scale_manager_v1")) fractional_manager = wl_registry_bind(r, name, &wp_fractional_scale_manager_v1_interface, 1);
    else if (!strcmp(iface, "zxdg_output_manager_v1")) output_manager = wl_registry_bind(r, name, &zxdg_output_manager_v1_interface, 2);
    else if (!strcmp(iface, "wl_shm")) shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
    else if (!strcmp(iface, "wp_viewporter")) viewporter = wl_registry_bind(r, name, &wp_viewporter_interface, 1);
    else if (!strcmp(iface, "wp_presentation")) presentation = wl_registry_bind(r, name, &wp_presentation_interface, 1);
    else if (!strcmp(iface, "wl_seat") && !seat && want_pointer) { seat = wl_registry_bind(r, name, &wl_seat_interface, 5); wl_seat_add_listener(seat, &seat_listener, NULL); }
    else if (!strcmp(iface, "wl_output") && !output) output = wl_registry_bind(r, name, &wl_output_interface, 4);
}
static void global_remove(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = { global, global_remove };

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--no-fractional")) want_fractional = 0;
        else if (!strcmp(argv[i], "--late-fractional") && i + 1 < argc) late_fractional = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--kwin")) kwin = 1;
        else if (!strcmp(argv[i], "--pointer")) want_pointer = 1;
        else if (!strcmp(argv[i], "--map-delay") && i + 1 < argc) map_delay_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--frames") && i + 1 < argc) frames_wanted = atoi(argv[++i]);
    }
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) { fprintf(stderr, "no display\n"); return 1; }
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(display);
    if (!compositor || !wm_base || !output || !output_manager || !fractional_manager) { fprintf(stderr, "missing globals\n"); return 1; }
    xdg_wm_base_add_listener(wm_base, &wm_listener, NULL);
    wl_output_add_listener(output, &output_listener, NULL);
    struct zxdg_output_v1 *xdg_output = zxdg_output_manager_v1_get_xdg_output(output_manager, output);
    zxdg_output_v1_add_listener(xdg_output, &xdg_output_listener, NULL);
    surface = wl_compositor_create_surface(compositor);
    wl_surface_add_listener(surface, &surface_listener, NULL);
    if (kwin) {
        if (!shm || !viewporter) { fprintf(stderr, "missing shm or viewporter\n"); return 1; }
        if (frames_wanted > 0 && !presentation) { fprintf(stderr, "missing wp_presentation\n"); return 1; }
        viewport = wp_viewporter_get_viewport(viewporter, surface);
    }
    if (!late_fractional) ask_fractional_scale();
    struct xdg_surface *xs = xdg_wm_base_get_xdg_surface(wm_base, surface);
    xdg_surface_add_listener(xs, &xs_listener, NULL);
    struct xdg_toplevel *tl = xdg_surface_get_toplevel(xs);
    xdg_toplevel_add_listener(tl, &tl_listener, NULL);
    xdg_toplevel_set_app_id(tl, "kwin_wayland");
    wl_surface_commit(surface);
    puts("ready");
    fflush(stdout);
    while (1) {
        while (wl_display_prepare_read(display) != 0) wl_display_dispatch_pending(display);
        wl_display_flush(display);
        struct pollfd fd = { wl_display_get_fd(display), POLLIN, 0 };
        if (poll(&fd, 1, -1) < 0) { wl_display_cancel_read(display); break; }
        wl_display_read_events(display);
        if (wl_display_dispatch_pending(display) < 0) break;
    }
    return 0;
}
