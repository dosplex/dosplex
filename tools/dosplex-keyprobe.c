/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright 2026 RoyÅke (Roy-Åke Martinsson) */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <wayland-client.h>

#include "xdg-shell-client-protocol.h"

/* Pointer lock and relative motion are probed only where the build generated those protocol
 * headers (the compositor tests do); a build with xdg-shell alone still works without them. */
#if defined(__has_include)
#if __has_include("pointer-constraints-unstable-v1-client-protocol.h") && \
        __has_include("relative-pointer-unstable-v1-client-protocol.h")
#include "pointer-constraints-unstable-v1-client-protocol.h"
#include "relative-pointer-unstable-v1-client-protocol.h"
#define PROBE_POINTER_LOCK 1
#endif
#endif

struct app {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct wl_keyboard *keyboard;
    struct wl_pointer *pointer;
    double pointer_x, pointer_y;
    struct xdg_wm_base *wm_base;
#ifdef PROBE_POINTER_LOCK
    struct zwp_pointer_constraints_v1 *constraints;
    struct zwp_relative_pointer_manager_v1 *relative_manager;
    struct zwp_relative_pointer_v1 *relative_pointer;
    struct zwp_locked_pointer_v1 *locked;
    struct zwp_confined_pointer_v1 *confined;
#endif

    struct wl_surface *surface;
    struct xdg_surface *xdg_surface;
    struct xdg_toplevel *xdg_toplevel;

    struct wl_buffer *buffer;
    void *buffer_data;
    size_t buffer_size;
    int width;
    int height;
    int configured_width;  /* the last xdg_toplevel configure's size; 0 while the client may choose */
    int configured_height;
    unsigned frames_drawn;
    struct wl_callback *frame_callback;
    bool configured;
    bool hold_map;   /* --hold-map: acknowledge configures but attach no buffer until the 'M' command */
    bool running;
};

static int create_shm_file(size_t size) {
    const char *runtime = getenv("XDG_RUNTIME_DIR");
    if (!runtime || !runtime[0]) return -1;
    char name[4096];
    if (snprintf(name, sizeof(name), "%s/dosplex-keyprobe-XXXXXX", runtime) >= (int)sizeof(name)) return -1;
    int fd = mkstemp(name);
    if (fd < 0) {
        return -1;
    }
    unlink(name);
    if (ftruncate(fd, (off_t)size) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void draw_buffer(struct app *app) {
    if (!app->buffer_data) {
        return;
    }

    uint32_t *pixels = app->buffer_data;
    const uint32_t color = 0xff204060;
    const size_t count = app->buffer_size / sizeof(uint32_t);
    for (size_t i = 0; i < count; ++i) {
        pixels[i] = color;
    }
}

static void keyboard_keymap(void *data, struct wl_keyboard *keyboard,
        uint32_t format, int fd, uint32_t size) {
    (void)data;
    (void)keyboard;
    fprintf(stdout, "KEYBOARD_KEYMAP format=%u size=%u\n", format, size);
    fflush(stdout);
    if (fd >= 0) {
        close(fd);
    }
}

static void keyboard_enter(void *data, struct wl_keyboard *keyboard,
        uint32_t serial, struct wl_surface *surface, struct wl_array *keys) {
    (void)data;
    (void)keyboard;
    (void)surface;
    (void)keys;
    fprintf(stdout, "KEYBOARD_ENTER serial=%u\n", serial);
    fflush(stdout);
}

static void keyboard_leave(void *data, struct wl_keyboard *keyboard,
        uint32_t serial, struct wl_surface *surface) {
    (void)data;
    (void)keyboard;
    (void)surface;
    fprintf(stdout, "KEYBOARD_LEAVE serial=%u\n", serial);
    fflush(stdout);
}

static void keyboard_key(void *data, struct wl_keyboard *keyboard,
        uint32_t serial, uint32_t time, uint32_t key, uint32_t state) {
    (void)data;
    (void)keyboard;
    fprintf(stdout, "KEY serial=%u time=%u key=%u state=%u\n",
        serial, time, key, state);
    fflush(stdout);
}

static void keyboard_modifiers(void *data, struct wl_keyboard *keyboard,
        uint32_t serial, uint32_t depressed, uint32_t latched,
        uint32_t locked, uint32_t group) {
    (void)data;
    (void)keyboard;
    fprintf(stdout,
        "MODIFIERS serial=%u depressed=%u latched=%u locked=%u group=%u\n",
        serial, depressed, latched, locked, group);
    fflush(stdout);
}

static void keyboard_repeat_info(void *data, struct wl_keyboard *keyboard,
        int32_t rate, int32_t delay) {
    (void)data;
    (void)keyboard;
    fprintf(stdout, "REPEAT rate=%d delay=%d\n", rate, delay);
    fflush(stdout);
}

static const struct wl_keyboard_listener keyboard_listener = {
    .keymap = keyboard_keymap,
    .enter = keyboard_enter,
    .leave = keyboard_leave,
    .key = keyboard_key,
    .modifiers = keyboard_modifiers,
    .repeat_info = keyboard_repeat_info,
};

static void pointer_enter(void *data, struct wl_pointer *p, uint32_t serial,
        struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y) {
    struct app *app = data;
    app->pointer_x = wl_fixed_to_double(x); app->pointer_y = wl_fixed_to_double(y);
    (void)p; (void)serial; (void)surface;
    printf("POINTER_ENTER x=%.2f y=%.2f\n", wl_fixed_to_double(x), wl_fixed_to_double(y)); fflush(stdout);
}
static void pointer_leave(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *surface) {
    (void)data; (void)p; (void)serial; (void)surface;
    puts("POINTER_LEAVE"); fflush(stdout);
}
static void pointer_motion(void *data, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    struct app *app = data;
    app->pointer_x = wl_fixed_to_double(x); app->pointer_y = wl_fixed_to_double(y);
    (void)p; (void)time;
    printf("POINTER_MOTION x=%.2f y=%.2f\n", wl_fixed_to_double(x), wl_fixed_to_double(y)); fflush(stdout);
}
static void pointer_button(void *data, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
    struct app *app = data;
    (void)p; (void)serial; (void)time;
    printf("POINTER_BUTTON button=%u state=%u x=%.2f y=%.2f\n", button, state, app->pointer_x, app->pointer_y); fflush(stdout);
}
static void pointer_axis(void *data, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) {
    (void)data; (void)p; (void)time;
    printf("POINTER_AXIS axis=%u value=%.2f\n", axis, wl_fixed_to_double(value)); fflush(stdout);
}
static void pointer_frame(void *data, struct wl_pointer *p) {
    (void)data; (void)p; puts("POINTER_FRAME"); fflush(stdout);
}
static void pointer_source(void *data, struct wl_pointer *p, uint32_t source) {(void)data; (void)p; (void)source;}
static void pointer_stop(void *data, struct wl_pointer *p, uint32_t time, uint32_t axis) {(void)data; (void)p; (void)time; (void)axis;}
static void pointer_discrete(void *data, struct wl_pointer *p, uint32_t axis, int32_t discrete) {(void)data; (void)p; (void)axis; (void)discrete;}
static void pointer_value120(void *data, struct wl_pointer *p, uint32_t axis, int32_t value) {(void)data; (void)p; (void)axis; (void)value;}
static const struct wl_pointer_listener pointer_listener = {
    .enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion,
    .button = pointer_button, .axis = pointer_axis, .frame = pointer_frame,
    .axis_source = pointer_source, .axis_stop = pointer_stop,
    .axis_discrete = pointer_discrete, .axis_value120 = pointer_value120,
};

#ifdef PROBE_POINTER_LOCK
static void relative_motion(void *data, struct zwp_relative_pointer_v1 *relative,
        uint32_t time_hi, uint32_t time_lo, wl_fixed_t dx, wl_fixed_t dy,
        wl_fixed_t dx_unaccel, wl_fixed_t dy_unaccel) {
    (void)data; (void)relative; (void)time_hi; (void)time_lo;
    printf("RELATIVE dx=%.2f dy=%.2f dx_unaccel=%.2f dy_unaccel=%.2f\n",
        wl_fixed_to_double(dx), wl_fixed_to_double(dy),
        wl_fixed_to_double(dx_unaccel), wl_fixed_to_double(dy_unaccel));
    fflush(stdout);
}
static const struct zwp_relative_pointer_v1_listener relative_listener = {
    .relative_motion = relative_motion,
};

static void locked_locked(void *data, struct zwp_locked_pointer_v1 *locked) {
    (void)data; (void)locked;
    puts("POINTER_LOCKED"); fflush(stdout);
}
static void locked_unlocked(void *data, struct zwp_locked_pointer_v1 *locked) {
    (void)data; (void)locked;
    puts("POINTER_UNLOCKED"); fflush(stdout);
}
static const struct zwp_locked_pointer_v1_listener locked_listener = {
    .locked = locked_locked,
    .unlocked = locked_unlocked,
};

static void confined_confined(void *data, struct zwp_confined_pointer_v1 *confined) {
    (void)data; (void)confined;
    puts("POINTER_CONFINED"); fflush(stdout);
}
static void confined_unconfined(void *data, struct zwp_confined_pointer_v1 *confined) {
    (void)data; (void)confined;
    puts("POINTER_UNCONFINED"); fflush(stdout);
}
static const struct zwp_confined_pointer_v1_listener confined_listener = {
    .confined = confined_confined,
    .unconfined = confined_unconfined,
};

/* A relative pointer needs the manager and the wl_pointer; either may arrive first. */
static void probe_relative_pointer(struct app *app) {
    if (app->relative_pointer || !app->relative_manager || !app->pointer) return;
    app->relative_pointer = zwp_relative_pointer_manager_v1_get_relative_pointer(
        app->relative_manager, app->pointer);
    zwp_relative_pointer_v1_add_listener(app->relative_pointer, &relative_listener, app);
}

/* Test commands (see main): L and O lock the pointer to the surface (persistent, oneshot), h sets
 * the lock's cursor position hint to (200,150), l ends the lock, F confines the pointer to the
 * rectangle 200,100 120x80 and f ends the confinement. Each one waits for the compositor to
 * answer before it reports, so a test that waits for the report has seen the effect. */
static void probe_pointer_command(struct app *app, char command) {
    const char *report = NULL;
    if (!app->constraints || !app->pointer) {
        report = "NO_POINTER_CONSTRAINTS";
    } else if (command == 'L' || command == 'O') {
        if (app->locked || app->confined) {
            report = "CONSTRAINT_EXISTS";
        } else {
            app->locked = zwp_pointer_constraints_v1_lock_pointer(app->constraints, app->surface,
                app->pointer, NULL, command == 'L' ? ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT :
                ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_ONESHOT);
            zwp_locked_pointer_v1_add_listener(app->locked, &locked_listener, app);
            report = "LOCK_REQUESTED";
        }
    } else if (command == 'l') {
        if (app->locked) {
            zwp_locked_pointer_v1_destroy(app->locked);
            app->locked = NULL;
        }
        report = "LOCK_ENDED";
    } else if (command == 'h') {
        if (app->locked) {
            zwp_locked_pointer_v1_set_cursor_position_hint(app->locked,
                wl_fixed_from_int(200), wl_fixed_from_int(150));
            wl_surface_commit(app->surface);
        }
        report = "HINT_SET";
    } else if (command == 'F') {
        if (app->locked || app->confined) {
            report = "CONSTRAINT_EXISTS";
        } else {
            struct wl_region *region = wl_compositor_create_region(app->compositor);
            wl_region_add(region, 200, 100, 120, 80);
            app->confined = zwp_pointer_constraints_v1_confine_pointer(app->constraints,
                app->surface, app->pointer, region, ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT);
            wl_region_destroy(region);
            zwp_confined_pointer_v1_add_listener(app->confined, &confined_listener, app);
            report = "CONFINE_REQUESTED";
        }
    } else if (command == 'f') {
        if (app->confined) {
            zwp_confined_pointer_v1_destroy(app->confined);
            app->confined = NULL;
        }
        report = "CONFINE_ENDED";
    }
    if (report) {
        wl_display_roundtrip(app->display);
        puts(report);
        fflush(stdout);
    }
}
#endif

static void seat_capabilities(void *data, struct wl_seat *seat,
        enum wl_seat_capability caps) {
    struct app *app = data;
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !app->pointer) {
        app->pointer = wl_seat_get_pointer(seat);
        wl_pointer_add_listener(app->pointer, &pointer_listener, app);
#ifdef PROBE_POINTER_LOCK
        probe_relative_pointer(app);
#endif
    } else if (!(caps & WL_SEAT_CAPABILITY_POINTER) && app->pointer) {
        wl_pointer_destroy(app->pointer);
        app->pointer = NULL;
    }
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !app->keyboard) {
        app->keyboard = wl_seat_get_keyboard(seat);
        wl_keyboard_add_listener(app->keyboard, &keyboard_listener, app);
    } else if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD) && app->keyboard) {
        wl_keyboard_destroy(app->keyboard);
        app->keyboard = NULL;
    }
}

static void seat_name(void *data, struct wl_seat *seat, const char *name) {
    (void)data;
    (void)seat;
    fprintf(stdout, "SEAT name=%s\n", name);
    fflush(stdout);
}

static const struct wl_seat_listener seat_listener = {
    .capabilities = seat_capabilities,
    .name = seat_name,
};

static void xdg_wm_base_ping(void *data, struct xdg_wm_base *wm_base,
        uint32_t serial) {
    (void)data;
    xdg_wm_base_pong(wm_base, serial);
}

static const struct xdg_wm_base_listener wm_base_listener = {
    .ping = xdg_wm_base_ping,
};

static void map_probe(struct app *app) {
    draw_buffer(app);
    wl_surface_attach(app->surface, app->buffer, 0, 0);
    wl_surface_damage_buffer(app->surface, 0, 0, app->width, app->height);
    wl_surface_commit(app->surface);
    app->configured = true;
    fprintf(stdout, "CONFIGURED %dx%d\n", app->width, app->height);
    fflush(stdout);
}

static void xdg_surface_configure(void *data, struct xdg_surface *xdg_surface,
        uint32_t serial) {
    struct app *app = data;
    xdg_surface_ack_configure(xdg_surface, serial);

    if (!app->configured && !app->hold_map) {
        map_probe(app);
    }
}

static const struct xdg_surface_listener xdg_surface_listener = {
    .configure = xdg_surface_configure,
};

static void xdg_toplevel_configure(void *data,
        struct xdg_toplevel *xdg_toplevel, int32_t width, int32_t height,
        struct wl_array *states) {
    struct app *app = data;
    (void)xdg_toplevel;
    (void)states;
    app->configured_width = width;
    app->configured_height = height;
    fprintf(stdout, "TOPLEVEL_CONFIGURE width=%d height=%d\n", width, height);
    fflush(stdout);
}

static void xdg_toplevel_close(void *data, struct xdg_toplevel *xdg_toplevel) {
    struct app *app = data;
    (void)xdg_toplevel;
    app->running = false;
}

static const struct xdg_toplevel_listener xdg_toplevel_listener = {
    .configure = xdg_toplevel_configure,
    .close = xdg_toplevel_close,
};

static void registry_global(void *data, struct wl_registry *registry,
        uint32_t name, const char *interface, uint32_t version) {
    struct app *app = data;

    if (strcmp(interface, wl_compositor_interface.name) == 0) {
        app->compositor = wl_registry_bind(registry, name,
            &wl_compositor_interface, 4);
    } else if (strcmp(interface, wl_shm_interface.name) == 0) {
        app->shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    } else if (strcmp(interface, wl_seat_interface.name) == 0) {
        app->seat = wl_registry_bind(registry, name, &wl_seat_interface,
            version < 8 ? version : 8);
        wl_seat_add_listener(app->seat, &seat_listener, app);
    } else if (strcmp(interface, xdg_wm_base_interface.name) == 0) {
        app->wm_base = wl_registry_bind(registry, name, &xdg_wm_base_interface,
            1);
        xdg_wm_base_add_listener(app->wm_base, &wm_base_listener, app);
#ifdef PROBE_POINTER_LOCK
    } else if (strcmp(interface, zwp_pointer_constraints_v1_interface.name) == 0) {
        app->constraints = wl_registry_bind(registry, name,
            &zwp_pointer_constraints_v1_interface, 1);
    } else if (strcmp(interface, zwp_relative_pointer_manager_v1_interface.name) == 0) {
        app->relative_manager = wl_registry_bind(registry, name,
            &zwp_relative_pointer_manager_v1_interface, 1);
        probe_relative_pointer(app);
#endif
    }
}

static void registry_global_remove(void *data, struct wl_registry *registry,
        uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

/* Test commands (see main): D draws one more frame, the 64x64 top-left block in a new colour, with that
 * block damaged and a frame callback, and reports FRAME_DONE when the compositor answers the callback.
 * P makes the buffer the size the compositor last configured, in a format with alpha, declares the whole
 * surface opaque and reports OPAQUE WxH once the commit is in. */
static void frame_done(void *data, struct wl_callback *callback, uint32_t time) {
    (void)time;
    struct app *app = data;
    wl_callback_destroy(callback);
    if (app->frame_callback == callback) app->frame_callback = NULL;
    puts("FRAME_DONE");
    fflush(stdout);
}

static const struct wl_callback_listener frame_listener = {
    .done = frame_done,
};

static void probe_draw_frame(struct app *app) {
    if (!app->buffer_data || !app->buffer) return;
    app->frames_drawn++;
    uint32_t *pixels = app->buffer_data;
    uint32_t color = 0xff000000 | ((app->frames_drawn * 0x010305u) & 0x00ffffffu);
    int block_w = app->width < 64 ? app->width : 64;
    int block_h = app->height < 64 ? app->height : 64;
    for (int y = 0; y < block_h; y++) {
        for (int x = 0; x < block_w; x++) pixels[(size_t)y * app->width + x] = color;
    }
    wl_surface_attach(app->surface, app->buffer, 0, 0);
    wl_surface_damage_buffer(app->surface, 0, 0, block_w, block_h);
    if (!app->frame_callback) {
        app->frame_callback = wl_surface_frame(app->surface);
        wl_callback_add_listener(app->frame_callback, &frame_listener, app);
    }
    wl_surface_commit(app->surface);
}

static int create_buffer(struct app *app, int width, int height, uint32_t format);

static void probe_opaque_buffer(struct app *app) {
    int width = app->configured_width > 0 ? app->configured_width : app->width;
    int height = app->configured_height > 0 ? app->configured_height : app->height;
    /* The buffer in use stays with the compositor until the probe exits; the new one replaces it. */
    if (create_buffer(app, width, height, WL_SHM_FORMAT_ARGB8888) != 0) {
        puts("OPAQUE_FAILED");
        fflush(stdout);
        return;
    }
    draw_buffer(app);
    struct wl_region *region = wl_compositor_create_region(app->compositor);
    wl_region_add(region, 0, 0, width, height);
    wl_surface_set_opaque_region(app->surface, region);
    wl_region_destroy(region);
    wl_surface_attach(app->surface, app->buffer, 0, 0);
    wl_surface_damage_buffer(app->surface, 0, 0, width, height);
    wl_surface_commit(app->surface);
    wl_display_roundtrip(app->display);
    printf("OPAQUE %dx%d\n", width, height);
    fflush(stdout);
}

static int create_buffer(struct app *app, int width, int height, uint32_t format) {
    app->width = width;
    app->height = height;
    app->buffer_size = (size_t)app->width * app->height * 4;

    int fd = create_shm_file(app->buffer_size);
    if (fd < 0) {
        fprintf(stderr, "failed to create shm file: %s\n", strerror(errno));
        return -1;
    }

    app->buffer_data = mmap(NULL, app->buffer_size, PROT_READ | PROT_WRITE,
        MAP_SHARED, fd, 0);
    if (app->buffer_data == MAP_FAILED) {
        fprintf(stderr, "mmap failed: %s\n", strerror(errno));
        close(fd);
        return -1;
    }

    struct wl_shm_pool *pool = wl_shm_create_pool(app->shm, fd,
        (int)app->buffer_size);
    app->buffer = wl_shm_pool_create_buffer(pool, 0, app->width, app->height,
        app->width * 4, format);
    wl_shm_pool_destroy(pool);
    close(fd);

    return app->buffer ? 0 : -1;
}

static int init_buffer(struct app *app) {
    return create_buffer(app, 640, 800, WL_SHM_FORMAT_XRGB8888);
}

int main(int argc, char **argv) {
    /* --test-control reads single-letter commands on stdin; with --hold-map as
     * well the window acknowledges its configures but maps only on 'M'. */
    bool test_control = argc >= 2 && argc <= 3 && strcmp(argv[1], "--test-control") == 0;
    bool hold_map = test_control && argc == 3 && strcmp(argv[2], "--hold-map") == 0;
    if ((argc > 1 && !test_control) || (argc == 3 && !hold_map)) return 2;
    struct app app = {0};
    app.running = true;
    app.hold_map = hold_map;

    app.display = wl_display_connect(NULL);
    if (!app.display) {
        fprintf(stderr, "failed to connect to Wayland display\n");
        return 1;
    }

    app.registry = wl_display_get_registry(app.display);
    wl_registry_add_listener(app.registry, &registry_listener, &app);
    wl_display_roundtrip(app.display);
    wl_display_roundtrip(app.display);

    if (!app.compositor || !app.shm || !app.wm_base) {
        fprintf(stderr, "missing required globals\n");
        return 1;
    }

    if (init_buffer(&app) != 0) {
        return 1;
    }

    app.surface = wl_compositor_create_surface(app.compositor);
    app.xdg_surface = xdg_wm_base_get_xdg_surface(app.wm_base, app.surface);
    xdg_surface_add_listener(app.xdg_surface, &xdg_surface_listener, &app);
    app.xdg_toplevel = xdg_surface_get_toplevel(app.xdg_surface);
    xdg_toplevel_add_listener(app.xdg_toplevel, &xdg_toplevel_listener, &app);
    xdg_toplevel_set_title(app.xdg_toplevel, "Dosplex Key Probe");

    wl_surface_commit(app.surface);

    while (app.running) {
        if (!test_control) {
            if (wl_display_dispatch(app.display) < 0) break;
            continue;
        }
        /* Test-only commands: U/N attach null without remapping, R sends a
         * fresh initial commit, C attaches null and waits for a configure,
         * D draws a frame, P commits an opaque buffer of the configured size and
         * M maps a window started with --hold-map. */
        if (wl_display_dispatch_pending(app.display) < 0 ||
                wl_display_flush(app.display) < 0) break;
        struct pollfd fds[] = {
            {.fd = wl_display_get_fd(app.display), .events = POLLIN},
            {.fd = STDIN_FILENO, .events = POLLIN},
        };
        if (poll(fds, 2, -1) < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (fds[0].revents & (POLLERR | POLLHUP)) break;
        if (fds[0].revents & POLLIN) {
            if (wl_display_dispatch(app.display) < 0) break;
        }
        if (fds[1].revents & (POLLIN | POLLHUP)) {
            char command;
            if (read(STDIN_FILENO, &command, 1) != 1) break;
            if (command == 'U' || command == 'N' || command == 'C') {
                /* Complete any activation configure before unmapping so
                 * its old serial is not acknowledged after the reset. */
                if (command == 'U' && wl_display_roundtrip(app.display) < 0) break;
                if (command == 'C') app.configured = false;
                wl_surface_attach(app.surface, NULL, 0, 0);
                wl_surface_commit(app.surface);
            } else if (command == 'R') {
                app.configured = false;
                xdg_toplevel_set_title(app.xdg_toplevel, "Dosplex Key Probe");
                wl_surface_commit(app.surface);
            } else if (command == 'D') {
                probe_draw_frame(&app);
            } else if (command == 'P') {
                probe_opaque_buffer(&app);
            } else if (command == 'M' && app.hold_map && !app.configured) {
                app.hold_map = false;
                map_probe(&app);
            }
#ifdef PROBE_POINTER_LOCK
            else if (command && strchr("LOlhFf", command)) {
                probe_pointer_command(&app, command);
            }
#endif
        }
    }

    return 0;
}
