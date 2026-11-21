#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Compile actual greeter configuration/regions/keyboard paths without accounts."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
regions = 'static const char *configured_steam_user' + source.split('static const char *configured_steam_user', 1)[1].split('static void render_side_greeter', 1)[0]
keyboard = 'static void handle_keyboard_input_for_side' + source.split('static void handle_keyboard_input_for_side', 1)[1].split('static void handle_output_destroy', 1)[0]
code = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <ctype.h>
#include <xkbcommon/xkbcommon.h>
#include <xkbcommon/xkbcommon-keysyms.h>
#define SIDE_LEFT 0
#define SIDE_GREETER 0
#define SIDE_CLAIMED 1
#define BTN_LEFT 272
#define UIelem_NONE 0
#define UIelem_USERNAME 1
#define UIelem_PASSWORD 2
#define UIelem_SIGNIN 3
#define UIelem_SIGNOUT 4
struct compositor;
struct ui_region { int type, x, y, w, h; };
struct wlr_box { int x, y, width, height; };
struct side_ctx { struct compositor *comp; int id, state, width, focused, num_regions;
    void *auth; bool launch_pending, auth_error, recovery_required; struct ui_region regions[3]; char username[64], password[64]; };
struct compositor { struct side_ctx left, right; };
static struct wlr_box greeter_box(struct side_ctx *ctx, double s, int x, int y, int w, int h) {
    return (struct wlr_box){(ctx->id ? 1920 : 0) + (int)(x*s), (int)(y*s), (int)(w*s), (int)(h*s)};
}
static void note_activity(struct compositor *c) {}
static void request_redraw(struct compositor *c) {}
static int clicks, click_x, click_y;
static void handle_pointer_click(struct compositor *c, int x, int y, bool pressed, unsigned button) {
    assert(pressed && button == BTN_LEFT); clicks++; click_x=x; click_y=y;
}
static void append_text(char *s, size_t size, const char *text) { strncat(s, text, size-strlen(s)-1); }
static void backspace_text(char *s) { if (*s) s[strlen(s)-1]=0; }
'''+ regions + keyboard + r'''
int main(void) {
    struct compositor c = {0}; c.left.comp=&c; c.right.comp=&c;
    c.left.width=c.right.width=1920; c.right.id=1;
    setenv("DOSPLEX_LEFT_USER", "deck", 1); setenv("DOSPLEX_RIGHT_USER", "guest", 1);
    unsetenv("DOSPLEX_SESSION_PAYLOAD");
    assert(!configured_steam_user(&c.left));
    build_side_regions(&c.left, 2); assert(c.left.num_regions==3);
    setenv("DOSPLEX_SESSION_PAYLOAD", "steam", 1);
    assert(!strcmp(configured_steam_user(&c.left), "deck"));
    assert(!strcmp(configured_steam_user(&c.right), "guest"));
    for (int i=0; i<2; i++) {
        struct side_ctx *s=i ? &c.right : &c.left;
        build_side_regions(s, 2);
        assert(s->num_regions==1 && s->regions[0].type==UIelem_SIGNIN);
        s->focused=UIelem_USERNAME;
        handle_keyboard_input_for_side(&c, s, XKB_KEY_a, "a");
        assert(!s->username[0] && !s->password[0] && s->focused==UIelem_SIGNIN);
        handle_keyboard_input_for_side(&c, s, XKB_KEY_Tab, "");
        assert(s->focused==UIelem_SIGNIN);
        handle_keyboard_input_for_side(&c, s, XKB_KEY_Return, "");
        assert(clicks==i+1 && click_x==i*1920+960 && click_y==444);
        s->auth=s; build_side_regions(s, 2); assert(!s->num_regions);
        handle_keyboard_input_for_side(&c, s, XKB_KEY_Return, ""); assert(clicks==i+1);
        s->auth=0;
    }
    setenv("DOSPLEX_LEFT_USER", "", 1);
    assert(!configured_steam_user(&c.left));
    build_side_regions(&c.left, 2); assert(c.left.num_regions==3);
    c.left.focused=UIelem_PASSWORD;
    handle_keyboard_input_for_side(&c, &c.left, XKB_KEY_Return, "");
    assert(clicks==3 && click_x==960 && click_y==444);
    c.right.state=SIDE_CLAIMED; build_side_regions(&c.right, 2);
    assert(c.right.num_regions==0);
    c.right.recovery_required=true; build_side_regions(&c.right, 2);
    assert(c.right.num_regions==1 && c.right.regions[0].type==UIelem_SIGNOUT);
}
'''
with tempfile.TemporaryDirectory(prefix='dosplex-native-greeter-') as directory:
    root = Path(directory)
    (root / 'test.c').write_text(code)
    subprocess.run(['cc', str(root / 'test.c'), '-lm', '-o', str(root / 'test')], check=True)
    subprocess.run([str(root / 'test')], check=True)
print('PASS: native Steam greeter hides credential fields and activates scaled buttons on both sides')
