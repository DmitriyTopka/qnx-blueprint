/*
 * cockpit: a small QNX 8.0 system used to demonstrate Blueprint.
 *
 * One binary, five roles, each run as its own process. The role comes from
 * the program name (the image installs the binary as gps, nav, camera,
 * logger and display, so each shows up under its own name in the trace) or
 * from the first argument:
 *
 *   gps     server "cockpit-gps"     replies with a position
 *   nav     server "cockpit-nav"     asks gps, replies with a route
 *   camera  server "cockpit-camera"  replies with a frame number
 *   logger  server "cockpit-logger"  accepts log lines
 *   display client                   every tick asks nav and camera, logs
 *
 * All communication is synchronous QNX message passing: name_attach() on the
 * server side, name_open() + MsgSend() on the client side, MsgReceive() and
 * MsgReply() in the server loop.
 *
 * The environment variable COCKPIT_FAULT selects a broken build, the way a
 * real regression would slip into one component:
 *
 *   deadlock    gps asks nav for a hint while nav is waiting on gps
 *   backdoor    display reads gps directly, bypassing nav
 *   camera-hang camera stops replying; display has no timeout and freezes
 *   camera-dies display treats a camera error as fatal and exits with it
 *   slow-reply  nav takes 200 ms to answer
 *
 * COCKPIT_FIXED=1 turns on the fix for the camera faults: display reads the
 * camera from its own thread and degrades to "camera stale" instead of
 * blocking (see camera_reader below for why a send timeout is not enough).
 */

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/dispatch.h>
#include <sys/iomsg.h>
#include <sys/neutrino.h>

#define MSG_POSITION 0x1001
#define MSG_ROUTE    0x1002
#define MSG_FRAME    0x1003
#define MSG_LOG      0x1004
#define MSG_HINT     0x1005

typedef struct {
    uint16_t type;
    uint16_t subtype;
    int32_t  seq;
    char     text[48];
} cockpit_msg_t;

static const char *fault = "";
static int fixed;

static int has_fault(const char *name) { return strcmp(fault, name) == 0; }

static void sleep_ms(long ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* Connect to a named server, waiting for it to appear. */
static int connect_to(const char *name) {
    for (int i = 0; i < 200; i++) {
        int coid = name_open(name, 0);
        if (coid != -1) {
            return coid;
        }
        sleep_ms(20);
    }
    fprintf(stderr, "cockpit: cannot open %s: %s\n", name, strerror(errno));
    exit(2);
}

static int ask(int coid, uint16_t type, int seq, const char *text, cockpit_msg_t *reply) {
    cockpit_msg_t msg = { .type = type, .seq = seq };
    snprintf(msg.text, sizeof(msg.text), "%s", text);
    return (int)MsgSend(coid, &msg, sizeof(msg), reply, sizeof(*reply));
}

static void log_line(int logger, int seq, const char *text) {
    cockpit_msg_t reply;
    if (logger != -1) {
        ask(logger, MSG_LOG, seq, text, &reply);
    }
}

typedef int (*handler_t)(int rcvid, cockpit_msg_t *msg, cockpit_msg_t *reply);

/* Generic server loop: handles connects and pulses, passes our messages on. */
static void serve(const char *name, handler_t handle) {
    name_attach_t *att = name_attach(NULL, name, 0);
    if (att == NULL) {
        fprintf(stderr, "cockpit: name_attach %s: %s\n", name, strerror(errno));
        exit(2);
    }
    for (;;) {
        union {
            struct _pulse pulse;
            struct _io_connect connect;
            cockpit_msg_t msg;
        } in;
        int rcvid = MsgReceive(att->chid, &in, sizeof(in), NULL);
        if (rcvid == -1) {
            continue;
        }
        if (rcvid == 0) {
            if (in.pulse.code == _PULSE_CODE_DISCONNECT) {
                ConnectDetach(in.pulse.scoid);
            }
            continue;
        }
        if (in.msg.type == _IO_CONNECT) {
            MsgReply(rcvid, EOK, NULL, 0);
            continue;
        }
        if (in.msg.type >= _IO_BASE && in.msg.type <= _IO_MAX) {
            MsgError(rcvid, ENOSYS);
            continue;
        }
        cockpit_msg_t reply = { .type = in.msg.type, .seq = in.msg.seq };
        if (handle(rcvid, &in.msg, &reply) == 0) {
            MsgReply(rcvid, EOK, &reply, sizeof(reply));
        }
    }
}

/* ---- gps ---------------------------------------------------------------- */

static int gps_nav = -1;

static int gps_handle(int rcvid, cockpit_msg_t *msg, cockpit_msg_t *reply) {
    (void)rcvid;
    if (has_fault("deadlock") && msg->type == MSG_POSITION) {
        /* The regression: gps wants a route hint from nav before answering.
         * nav is blocked waiting on us, so neither side ever replies. */
        if (gps_nav == -1) {
            gps_nav = connect_to("cockpit-nav");
        }
        cockpit_msg_t hint;
        ask(gps_nav, MSG_HINT, msg->seq, "hint?", &hint);
    }
    snprintf(reply->text, sizeof(reply->text), "pos %d.%03d", 50, 450 + msg->seq % 100);
    return 0;
}

/* ---- nav ---------------------------------------------------------------- */

static int nav_gps = -1;
static int nav_logger = -1;

static int nav_handle(int rcvid, cockpit_msg_t *msg, cockpit_msg_t *reply) {
    (void)rcvid;
    if (msg->type == MSG_HINT) {
        snprintf(reply->text, sizeof(reply->text), "hint none");
        return 0;
    }
    cockpit_msg_t pos;
    ask(nav_gps, MSG_POSITION, msg->seq, "where?", &pos);
    if (has_fault("slow-reply")) {
        sleep_ms(200);
    }
    snprintf(reply->text, sizeof(reply->text), "route via %.36s", pos.text);
    log_line(nav_logger, msg->seq, "route sent");
    return 0;
}

/* ---- camera ------------------------------------------------------------- */

static int camera_frames;

static int camera_handle(int rcvid, cockpit_msg_t *msg, cockpit_msg_t *reply) {
    (void)rcvid;
    (void)msg;
    camera_frames++;
    if (has_fault("camera-hang") && camera_frames > 5) {
        /* The regression: the frame grabber wedges and never answers. */
        for (;;) {
            pause();
        }
    }
    snprintf(reply->text, sizeof(reply->text), "frame %d", camera_frames);
    return 0;
}

/* ---- logger ------------------------------------------------------------- */

static int logger_handle(int rcvid, cockpit_msg_t *msg, cockpit_msg_t *reply) {
    (void)rcvid;
    (void)msg;
    snprintf(reply->text, sizeof(reply->text), "ok");
    return 0;
}

/* ---- display ------------------------------------------------------------ */

/*
 * The fix for the camera faults. A send timeout is not enough: name_attach()
 * channels are created with _NTO_CHF_UNBLOCK, so a timed-out client stays
 * REPLY-blocked until the server handles the unblock pulse, and a wedged
 * camera never does. Instead a separate thread talks to the camera and the
 * display loop only reads the last frame it got.
 */
static pthread_mutex_t frame_lock = PTHREAD_MUTEX_INITIALIZER;
static char last_frame[48] = "no camera";
static int frame_age;

static void *camera_reader(void *arg) {
    int camera = *(int *)arg;
    for (int seq = 0;; seq++) {
        cockpit_msg_t frame;
        if (ask(camera, MSG_FRAME, seq, "frame?", &frame) == -1) {
            pthread_mutex_lock(&frame_lock);
            snprintf(last_frame, sizeof(last_frame), "no camera");
            pthread_mutex_unlock(&frame_lock);
            return NULL;
        }
        pthread_mutex_lock(&frame_lock);
        snprintf(last_frame, sizeof(last_frame), "%s", frame.text);
        frame_age = 0;
        pthread_mutex_unlock(&frame_lock);
        sleep_ms(50);
    }
}

static int run_display(int ticks) {
    int nav = connect_to("cockpit-nav");
    int camera = connect_to("cockpit-camera");
    int logger = connect_to("cockpit-logger");
    int gps = has_fault("backdoor") ? connect_to("cockpit-gps") : -1;

    if (fixed) {
        pthread_t reader;
        pthread_create(&reader, NULL, camera_reader, &camera);
    }

    for (int seq = 0; seq < ticks; seq++) {
        cockpit_msg_t route, frame, pos;
        ask(nav, MSG_ROUTE, seq, "route?", &route);
        if (gps != -1) {
            ask(gps, MSG_POSITION, seq, "where? (direct)", &pos);
        }

        if (fixed) {
            pthread_mutex_lock(&frame_lock);
            if (++frame_age > 3) {
                snprintf(frame.text, sizeof(frame.text), "camera stale");
            } else {
                snprintf(frame.text, sizeof(frame.text), "%s", last_frame);
            }
            pthread_mutex_unlock(&frame_lock);
        } else if (ask(camera, MSG_FRAME, seq, "frame?", &frame) == -1) {
            fprintf(stderr, "display: camera failed: %s, giving up\n", strerror(errno));
            return 1;
        }
        printf("display %02d: %s | %s\n", seq, route.text, frame.text);
        log_line(logger, seq, "frame drawn");
        sleep_ms(50);
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *role = strrchr(argv[0], '/') ? strrchr(argv[0], '/') + 1 : argv[0];
    int arg = 1;
    if (strcmp(role, "cockpit") == 0) {
        if (argc < 2) {
            fprintf(stderr, "usage: cockpit gps|nav|camera|logger|display [ticks]\n");
            return 2;
        }
        role = argv[1];
        arg = 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);   /* output survives a kill at the end of a run */
    const char *env = getenv("COCKPIT_FAULT");
    fault = env ? env : "";
    fixed = getenv("COCKPIT_FIXED") && strcmp(getenv("COCKPIT_FIXED"), "1") == 0;

    if (strcmp(role, "gps") == 0) {
        serve("cockpit-gps", gps_handle);
    } else if (strcmp(role, "nav") == 0) {
        nav_gps = connect_to("cockpit-gps");
        nav_logger = connect_to("cockpit-logger");
        serve("cockpit-nav", nav_handle);
    } else if (strcmp(role, "camera") == 0) {
        serve("cockpit-camera", camera_handle);
    } else if (strcmp(role, "logger") == 0) {
        serve("cockpit-logger", logger_handle);
    } else if (strcmp(role, "display") == 0) {
        return run_display(argc > arg ? atoi(argv[arg]) : 20);
    }
    fprintf(stderr, "cockpit: unknown role %s\n", role);
    return 2;
}
