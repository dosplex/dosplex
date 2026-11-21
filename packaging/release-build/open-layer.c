/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright 2026 RoyÅke (Roy-Åke Martinsson) */
/* Opens a Vulkan layer the way the Vulkan loader does, and asks it for the entry point that the loader negotiates
 * with. payload-build.sh builds this program for the architecture of each layer (cc -m64, cc -m32) and runs it on the
 * layer of the payload; the exit status 0 means that the layer and all it needs open in this image. */
#include <dlfcn.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s LAYER.so\n", argv[0]);
        return 2;
    }
    void *layer = dlopen(argv[1], RTLD_LAZY | RTLD_LOCAL);
    if (!layer) {
        fprintf(stderr, "%s\n", dlerror());
        return 1;
    }
    if (!dlsym(layer, "vkNegotiateLoaderLayerInterfaceVersion")) {
        fprintf(stderr, "%s: no vkNegotiateLoaderLayerInterfaceVersion\n", argv[1]);
        return 1;
    }
    printf("%s opens and exports vkNegotiateLoaderLayerInterfaceVersion\n", argv[1]);
    return 0;
}
