/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright 2026 RoyÅke (Roy-Åke Martinsson) */
/* Dev-only CUSE transport. Physical HID descriptors are inherited, never opened
 * by path. The parent policy mediates every input report. No direct read fallback.
 * Original implementation; libfuse's public CUSE API is used under its LGPL ABI.
 */
#define FUSE_USE_VERSION 31
#define _GNU_SOURCE
#include <cuse_lowlevel.h>
#include <linux/hidraw.h>
#include <pthread.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <signal.h>

#define MAX_REPORT 4096
#define QUEUE 64
#define CLIENTS 32
struct packet { size_t n; unsigned char data[MAX_REPORT]; };
struct client {
    int used; unsigned head, count;
    uid_t uid; int revoked;
    struct packet queue[QUEUE];
    fuse_req_t pending; size_t requested;
    struct fuse_pollhandle *poll;
};
static struct client clients[CLIENTS];
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t output_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t ioctl_lock=PTHREAD_MUTEX_INITIALIZER;
static int source=-1;
static uint32_t owner_uid=0; /* Root only until the parent enrolls an owner. */
static unsigned product=0x1205; /* Of the source; a relay without one is the Deck fixture. */
/* Local pipe protocol: uint32 type/flags, sequence, length; then payload.
 * Parent->relay flags: 1 flush ALL client queues, 2 request acknowledgement,
 * 4 replace owner (payload uint32 UID; revokes every existing handle).
 * Relay->parent type: 1 physical report, 2 acknowledgement. Little-endian host.
 */
struct frame { uint32_t kind, sequence, size; };
static int exact(int fd, void *data, size_t size, int writing) {
    char *p=data;
    while (size) {
        ssize_t n=writing ? write(fd,p,size) : read(fd,p,size);
        if (n<0 && errno==EINTR) continue;
        if (n<=0) return -1;
        size-=n; p+=n;
    }
    return 0;
}
static void send_frame(unsigned kind,unsigned sequence,const void *buf,unsigned size) {
    struct frame f={kind,sequence,size};
    pthread_mutex_lock(&output_lock);
    if(exact(1,&f,sizeof(f),1) || (size && exact(1,(void *)buf,size,1))) _exit(2);
    pthread_mutex_unlock(&output_lock);
}
static void *source_reader(void *unused) {
    (void)unused;
    struct pollfd p={source,POLLIN,0}; unsigned char buf[MAX_REPORT];
    for (;;) {
        int n=poll(&p,1,-1);
        if(n<0 && errno==EINTR) continue;
        if(n<0 || p.revents&(POLLERR|POLLHUP|POLLNVAL)) _exit(2);
        ssize_t got=read(source,buf,sizeof(buf));
        if(got<0 && (errno==EINTR || errno==EAGAIN)) continue;
        if(got<=0) _exit(2);
        send_frame(1,0,buf,(unsigned)got);
    }
    return NULL;
}
static void *policy_reader(void *unused) {
    (void)unused;
    struct frame f; unsigned char buf[MAX_REPORT];
    for (;;) {
        if(exact(0,&f,sizeof(f),0)) _exit(0);
        if(f.size>MAX_REPORT || f.kind&~7u || ((f.kind&4) && f.size!=4)) _exit(2);
        if(f.size && exact(0,buf,f.size,0)) _exit(2);
        if(f.kind&4) {
            uint32_t next; memcpy(&next,buf,4);
            if(next==UINT32_MAX) _exit(2); /* Never restore unrestricted access. */
            pthread_mutex_lock(&ioctl_lock);
            pthread_mutex_lock(&lock);
            if(next!=owner_uid) {
                owner_uid=next;
                for(unsigned i=0;i<CLIENTS;i++) {
                    struct client *c=&clients[i]; if(!c->used) continue;
                    c->revoked=1; c->head=c->count=0;
                    if(c->pending) {fuse_reply_err(c->pending,ENODEV);c->pending=NULL;}
                    if(c->poll) fuse_lowlevel_notify_poll(c->poll);
                }
            }
            pthread_mutex_unlock(&lock);
            pthread_mutex_unlock(&ioctl_lock);
            if(f.kind&2) send_frame(2,f.sequence,NULL,0);
            continue;
        }
        pthread_mutex_lock(&lock);
        for(unsigned i=0;i<CLIENTS;i++) {
            struct client *c=&clients[i]; if(!c->used || c->revoked) continue;
            if(f.kind&1) c->head=c->count=0;
            if(!f.size) continue;
            if(c->pending) {
                fuse_reply_buf(c->pending,(char *)buf,c->requested<f.size?c->requested:f.size);
                c->pending=NULL;
            } else {
                if(c->count==QUEUE) { c->head=(c->head+1)%QUEUE; c->count--; }
                struct packet *q=&c->queue[(c->head+c->count++)%QUEUE];
                q->n=f.size; memcpy(q->data,buf,f.size);
            }
            if(c->poll) fuse_lowlevel_notify_poll(c->poll);
        }
        pthread_mutex_unlock(&lock);
        if(f.kind&2) send_frame(2,f.sequence,NULL,0);
    }
    return NULL;
}
/* BEGIN firmware guard: pure code, so that native-hid-relay-guard-test.py can compile this part alone. */
/* Firmware-update commands, which the relay refuses (owner decision: safety). A side's Steam can start its controller
 * firmware updater, and its firmware check fails inside a side, so an update that went through this relay could be left
 * half done on the real pad or receiver. Only commands with evidence are listed; everything else passes unchanged.
 * The guard applies to the receiver 28de:1304 ("Proteus", the Steam Controller Puck), to the 28de:1305 puck and to their
 * pads. 1305 is carried as a 1304-family receiver (same slot reports); the guard exists for safety, so it covers every
 * receiver the relay carries from that family, although the 0x90 evidence above was gathered for 1304. The Deck's own
 * controller 28de:1205 is not covered: there is no evidence for it here.
 *
 * A command is a feature report (SET_REPORT through HIDIOCSFEATURE) or an output report written to the node whose first
 * byte is the report id (1: the pad through the receiver, 2: the receiver) and whose second byte is the command id.
 *
 * 0x90 on report 1 and on report 2: reboot into the bootloader. Source: bin/hardwareupdater/hardwareupdater.x86_64 of
 * Steam client 1788652215 (a PyInstaller bundle; its code was read, nothing was run). Its reboot_to_BL() sends the
 * feature report 02 90 to a receiver whose HID release number is 2 and 01 90 to every other receiver and to a pad, zero
 * padded to 64 bytes. It is the first step of --prep-by-serial, --update-by-serial and --update-all, which Steam's update
 * wizard runs. The erase and the programming that follow go to the bootloader's USB serial port (28de:1005, 28de:1007),
 * which this relay never carries, so this is the only step of an update that reaches the relay.
 * The id is in neither Linux hid-steam.c nor SDL's table of Steam Controller commands (0x8f is the last one below
 * 0x9f), so the updater is the only source.
 *
 * Considered and left out, because the evidence is weak or because it is not an update:
 *  - 0x95 on report 1: the updater's reboot() (--reboot-by-serial, pads only). It restarts the application; it does
 *    not enter the bootloader, erase or write anything.
 *  - 0xb7, 0xb8, 0xb9 (hid-steam.c ID_AUDIO_UPDATE_START, _DATA, _COMPLETE): names from SDL for the first Steam
 *    Controller's sound data. Nothing shows that the Puck or the new pad uses them, and no tool here sends them.
 *  - 0x86 factory reset, 0xa9 set serial number, 0xaf and 0xb0 radio records: they change settings or identity, not
 *    the firmware.
 */
#define GUARDED_PRODUCT 0x1304
#define GUARDED_PRODUCT_2 0x1305
struct refusal { unsigned char report, command; };
static const struct refusal refusals[]={{1,0x90},{2,0x90}};
#define REFUSALS (sizeof(refusals)/sizeof(refusals[0]))
static unsigned char refusal_logged[REFUSALS];
/* The refusal that matches REPORT (SIZE bytes, report id first) for a device with this PRODUCT, or NULL. */
static const struct refusal *firmware_command(unsigned product,const unsigned char *report,size_t size) {
    if((product!=GUARDED_PRODUCT && product!=GUARDED_PRODUCT_2) || size<2) return NULL;
    for(size_t i=0;i<REFUSALS;i++) if(report[0]==refusals[i].report && report[1]==refusals[i].command) return &refusals[i];
    return NULL;
}
/* Say once per refused command, on stderr, which the parent keeps in its journal. VIA is "feature" or "output". */
static void log_refusal(const struct refusal *r,const char *via) {
    if(__atomic_exchange_n(&refusal_logged[r-refusals],1,__ATOMIC_RELAXED)) return;
    fprintf(stderr,"native-hid-relay: refused the firmware-update command 0x%02x on report %u (%s report); later ones are refused without a line\n",
            r->command,r->report,via);
}
/* END firmware guard */
static struct client *get(struct fuse_file_info *fi) {
    if(fi->fh>=CLIENTS || !clients[fi->fh].used) return NULL;
    return &clients[fi->fh];
}
static int permitted(fuse_req_t req,struct client *c) {
    return c && !c->revoked && (c->uid==owner_uid && fuse_req_ctx(req)->uid==owner_uid);
}
static void relay_open(fuse_req_t req,struct fuse_file_info *fi) {
    pthread_mutex_lock(&lock);
    if(fuse_req_ctx(req)->uid!=owner_uid) {
        pthread_mutex_unlock(&lock);fuse_reply_err(req,EACCES);return;
    }
    for(unsigned i=0;i<CLIENTS;i++) if(!clients[i].used) {
        memset(&clients[i],0,sizeof(clients[i])); clients[i].used=1; fi->fh=i;
        clients[i].uid=fuse_req_ctx(req)->uid;
        fi->direct_io=1; fi->nonseekable=1;
        fuse_reply_open(req,fi); pthread_mutex_unlock(&lock); return;
    }
    pthread_mutex_unlock(&lock); fuse_reply_err(req,EMFILE);
}
static void relay_read(fuse_req_t req,size_t size,off_t off,struct fuse_file_info *fi) {
    (void)off;
    pthread_mutex_lock(&lock); struct client *c=get(fi);
    if(!c) fuse_reply_err(req,EBADF);
    else if(!permitted(req,c)) fuse_reply_err(req,ENODEV);
    else if(!size) fuse_reply_buf(req,NULL,0);
    else if(c->count) {
        struct packet *p=&c->queue[c->head];
        fuse_reply_buf(req,(char *)p->data,size<p->n?size:p->n);
        c->head=(c->head+1)%QUEUE; c->count--;
    } else if(fi->flags&O_NONBLOCK) fuse_reply_err(req,EAGAIN);
    else if(c->pending) fuse_reply_err(req,EBUSY);
    else { c->pending=req; c->requested=size; }
    pthread_mutex_unlock(&lock);
}
static void relay_release(fuse_req_t req,struct fuse_file_info *fi) {
    pthread_mutex_lock(&lock); struct client *c=get(fi);
    if(c) {
        if(c->pending) fuse_reply_err(c->pending,ENODEV);
        if(c->poll) fuse_pollhandle_destroy(c->poll);
        memset(c,0,sizeof(*c));
    }
    pthread_mutex_unlock(&lock); fuse_reply_err(req,0);
}
static void relay_poll(fuse_req_t req,struct fuse_file_info *fi,struct fuse_pollhandle *ph) {
    pthread_mutex_lock(&lock); struct client *c=get(fi);
    if(!c) { if(ph) fuse_pollhandle_destroy(ph); fuse_reply_err(req,EBADF); }
    else {
        if(ph) { if(c->poll) fuse_pollhandle_destroy(c->poll); c->poll=ph; }
        fuse_reply_poll(req,!permitted(req,c)?POLLHUP|POLLERR:c->count?POLLIN:0);
    }
    pthread_mutex_unlock(&lock);
}
static void relay_write(fuse_req_t req,const char *buf,size_t n,off_t off,struct fuse_file_info *fi) {
    (void)off;
    if(!n || n>MAX_REPORT) { fuse_reply_err(req,EINVAL);return; }
    pthread_mutex_lock(&ioctl_lock);
    pthread_mutex_lock(&lock);int allowed=permitted(req,get(fi));pthread_mutex_unlock(&lock);
    if(!allowed) {pthread_mutex_unlock(&ioctl_lock);fuse_reply_err(req,ENODEV);return;}
    const struct refusal *refused=firmware_command(product,(const unsigned char *)buf,n);
    if(refused) {pthread_mutex_unlock(&ioctl_lock);log_refusal(refused,"output");fuse_reply_err(req,EPERM);return;}
    ssize_t result=source<0?(ssize_t)n:write(source,buf,n);int e=errno;
    pthread_mutex_unlock(&ioctl_lock);
    if(result<0) fuse_reply_err(req,e); else fuse_reply_write(req,result);
}
static void relay_ioctl(fuse_req_t req,int command,void *arg,struct fuse_file_info *fi,
                        unsigned flags,const void *input,size_t in_size,size_t out_size) {
    (void)flags;
    pthread_mutex_lock(&lock);int allowed=permitted(req,get(fi));pthread_mutex_unlock(&lock);
    if(!allowed) {fuse_reply_err(req,ENODEV);return;}
    unsigned cmd=(unsigned)command,n=_IOC_SIZE(cmd),nr=_IOC_NR(cmd);
    /* Only hidraw metadata and feature reports. Input GET ioctls must never
     * bypass the policy. HID raw structures use fixed-width fields, including
     * compat clients: no pointer-containing native structures are forwarded. */
    int readback=0,writein=0;
    if(_IOC_TYPE(cmd)!='H' || !n || n>sizeof(struct hidraw_report_descriptor)) {
        fuse_reply_err(req,ENOTTY);return;
    }
    if(cmd==HIDIOCGRDESCSIZE || cmd==HIDIOCGRAWINFO) readback=1;
    else if(cmd==HIDIOCGRDESC) readback=writein=1;
    else if((nr==4 || nr==5 || nr==8) && _IOC_DIR(cmd)==_IOC_READ) readback=1;
    else if((nr==6 || nr==7) && _IOC_DIR(cmd)==(_IOC_READ|_IOC_WRITE) && n<=MAX_REPORT) readback=writein=1;
    else { fuse_reply_err(req,ENOTTY);return; }
    struct iovec io={arg,n};
    if((writein && in_size<n) || (readback && out_size<n)) {
        fuse_reply_ioctl_retry(req,writein?&io:NULL,writein,readback?&io:NULL,readback);return;
    }
    unsigned char buffer[sizeof(struct hidraw_report_descriptor)]={0};
    if(writein) memcpy(buffer,input,n);
    if(nr==6) {
        const struct refusal *refused=firmware_command(product,buffer,n);
        if(refused) {log_refusal(refused,"feature");fuse_reply_err(req,EPERM);return;}
    }
    int result;
    if(source<0) {
        if(cmd==HIDIOCGRAWINFO) {struct hidraw_devinfo info={3,0x28de,0x1205};memcpy(buffer,&info,sizeof(info));result=0;}
        else if(cmd==HIDIOCGRDESCSIZE) {int size=0;memcpy(buffer,&size,sizeof(size));result=0;}
        else if(nr==6 || nr==7) result=n;
        else {fuse_reply_err(req,ENOTTY);return;}
    } else {
        pthread_mutex_lock(&ioctl_lock);
        pthread_mutex_lock(&lock);allowed=permitted(req,get(fi));pthread_mutex_unlock(&lock);
        if(!allowed) {pthread_mutex_unlock(&ioctl_lock);fuse_reply_err(req,ENODEV);return;}
        result=ioctl(source,cmd,buffer); int e=errno;
        pthread_mutex_unlock(&ioctl_lock);
        if(result<0) {fuse_reply_err(req,e);return;}
    }
    fuse_reply_ioctl(req,result,readback?buffer:NULL,readback?n:0);
}
/* The number that was asked for, and the name of the node. FIXED_MAJOR is 0 when the kernel picks the number. */
static const char *node_name;
static unsigned fixed_major,fixed_minor;
/* The kernel takes the reply to the INIT request and, in that same call, makes the node or refuses its number. A
 * refusal ends the connection, and libfuse then leaves its loop with status 0 and prints nothing, which looks like a
 * clean end. So with a fixed number the helper looks in sysfs when init_done runs: the device at that number has to
 * carry this node's name. If it does not, the helper says so and ends with status 3, before it starts a thread or
 * sends a frame. Status 3 is the only status that means "the kernel did not make the node at that number": a caller
 * may start the helper again without a number after 3, and after no other status. Status 2 is for what another number
 * would not change, and for a sysfs that gives no answer. A relay of the same name at the same number would pass this
 * check; nothing runs two relays of one name. */
static void check_number(void) {
    char path[48],target[256];
    snprintf(path,sizeof(path),"/sys/dev/char/%u:%u",fixed_major,fixed_minor);
    ssize_t length=readlink(path,target,sizeof(target)-1);
    /* ENOENT says that nothing is registered at the number only if the directory is there to say it. */
    if(length<0 && (errno!=ENOENT || access("/sys/dev/char",X_OK))) {
        fprintf(stderr,"native-hid-relay: cannot tell whether number %u:%u was granted: /sys/dev/char: %s\n",fixed_major,fixed_minor,strerror(errno));_exit(2);
    }
    if(length<0) {
        fprintf(stderr,"native-hid-relay: number %u:%u refused by the kernel\n",fixed_major,fixed_minor);_exit(3);
    }
    target[length]=0;
    const char *holder=strrchr(target,'/');holder=holder?holder+1:target;
    if(strcmp(holder,node_name)) {
        fprintf(stderr,"native-hid-relay: number %u:%u refused by the kernel: %.60s holds it\n",fixed_major,fixed_minor,holder);_exit(3);
    }
}
static void initialized(void *unused) {
    (void)unused;pthread_t thread;
    if(fixed_major) check_number();
    if(pthread_create(&thread,NULL,policy_reader,NULL)) _exit(2);
    pthread_detach(thread);
    if(source>=0) {if(pthread_create(&thread,NULL,source_reader,NULL)) _exit(2);pthread_detach(thread);}
}
/* One decimal field of MAJOR:MINOR: digits only, no sign, no space, no leading zero, at most MAX,
 * ended by STOP. Returns 0 and moves *TEXT past STOP, or -1. */
static int number_field(const char **text,char stop,unsigned max,unsigned *out) {
    const char *p=*text;unsigned value=0;
    if(*p<'0' || *p>'9' || (*p=='0' && p[1]>='0' && p[1]<='9')) return -1;
    for(;*p>='0' && *p<='9';p++) {value=value*10+(unsigned)(*p-'0');if(value>max) return -1;}
    if(*p!=stop) return -1;
    *out=value;*text=stop?p+1:p;
    return 0;
}
/* The optional device number of the CUSE node. The major is one of the kernel's local and
 * experimental majors, 120-127 or 60-63: dynamic allocation never hands those out, so a number
 * that is taken belongs to something that was started on purpose. The minor is the number of
 * the hidraw node that is served, 0-63. */
static int device_number(const char *text,unsigned *want_major,unsigned *want_minor) {
    const char *p=text;
    if(number_field(&p,':',999,want_major) || number_field(&p,'\0',99,want_minor)) return -1;
    if(!((*want_major>=120 && *want_major<=127) || (*want_major>=60 && *want_major<=63)) || *want_minor>63) return -1;
    return 0;
}
/* What a file is, for the message that refuses a source: its type and, for a device, its number. */
static void describe(const struct stat *st,char *out,size_t size) {
    const char *kind;
    switch(st->st_mode&S_IFMT) {
    case S_IFCHR: kind="character device";break;
    case S_IFBLK: kind="block device";break;
    case S_IFREG: kind="regular file";break;
    case S_IFDIR: kind="directory";break;
    case S_IFIFO: kind="pipe";break;
    case S_IFSOCK: kind="socket";break;
    case S_IFLNK: kind="symbolic link";break;
    default: kind="file of an unknown type";
    }
    if(S_ISCHR(st->st_mode) || S_ISBLK(st->st_mode)) snprintf(out,size,"a %s %u:%u",kind,major(st->st_rdev),minor(st->st_rdev));
    else snprintf(out,size,"a %s",kind);
}
int main(int argc,char **argv) {
    if((argc!=3 && argc!=4) || strncmp(argv[1],"dosplex-hid-",11) || strspn(argv[1],"abcdefghijklmnopqrstuvwxyz0123456789-")!=strlen(argv[1]) || strlen(argv[1])>80) {
        fprintf(stderr,"usage: native-hid-relay dosplex-hid-NAME inherited-source-fd|-1 [MAJOR:MINOR]\n");return 2;
    }
    char *end;long value=strtol(argv[2],&end,10);
    if(*end || value< -1 || value>1048576) return 2;
    source=value;
    node_name=argv[1];
    /* Without MAJOR:MINOR the kernel picks a dynamic number, as it always did. */
    if(argc==4) {
        if(device_number(argv[3],&fixed_major,&fixed_minor)) {
            fprintf(stderr,"native-hid-relay: %.40s is not a device number: decimal MAJOR:MINOR, MAJOR 120-127 or 60-63, MINOR 0-63\n",argv[3]);return 2;
        }
        if(source>=0) {
            struct stat st;char found[64];
            if(fstat(source,&st)) {
                fprintf(stderr,"native-hid-relay: number %u:%u refused: source descriptor %d cannot be examined: %s\n",fixed_major,fixed_minor,source,strerror(errno));return 2;
            }
            if(!S_ISCHR(st.st_mode) || minor(st.st_rdev)!=fixed_minor) {
                describe(&st,found,sizeof(found));
                fprintf(stderr,"native-hid-relay: number %u:%u refused: the source is %s, not a character device with minor %u\n",fixed_major,fixed_minor,found,fixed_minor);return 2;
            }
        }
    }
    if(source>=0) {struct hidraw_devinfo info;if(ioctl(source,HIDIOCGRAWINFO,&info) || info.vendor!=0x28de || (info.product!=0x1205 && info.product!=0x1304 && info.product!=0x1305)) return 2;product=info.product;}
    signal(SIGPIPE,SIG_IGN);
    char name[100];snprintf(name,sizeof(name),"DEVNAME=%s",argv[1]);const char *info[]={name};
    struct cuse_info ci={.dev_major=fixed_major,.dev_minor=fixed_minor,.dev_info_argc=1,.dev_info_argv=info,.flags=CUSE_UNRESTRICTED_IOCTL};
    struct cuse_lowlevel_ops ops={.init_done=initialized,.open=relay_open,.read=relay_read,.write=relay_write,.release=relay_release,.ioctl=relay_ioctl,.poll=relay_poll};
    char *args[]={argv[0],"-f","-s",NULL};
    return cuse_lowlevel_main(3,args,&ci,&ops,NULL);
}
