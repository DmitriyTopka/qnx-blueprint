#!/bin/sh
# Runs one cockpit scenario on the target under a wide kernel trace.
#   cockpit-run.sh <scenario> [fixed]
# Scenarios: ok deadlock backdoor camera-hang camera-dies slow-reply prio-inversion
# LATE=1 starts the trace after the system is up (no ChannelCreate/ConnectAttach in it).
# SECPOL=1 starts each process with its security type (on -T <name>_t), for a
# system booted with a policy from `blueprint secpol`.
# Writes /tmp/<scenario>[-fixed].kev, .pidin and .out
SC=${1:-ok}
NAME=$SC
if [ "$2" = fixed ]; then
    export COCKPIT_FIXED=1
    NAME=$SC-fixed
fi
export COCKPIT_FAULT=$SC
# Start a role, with its security type when SECPOL=1.
run() {
    if [ -n "$SECPOL" ]; then
        on -T "${1}_t" "$@"
    else
        "$@"
    fi
}
TRACE=/tmp/$NAME.kev
OUT=/tmp/$NAME.out

slay -Q -s KILL display gps nav camera logger media >/dev/null 2>&1
rm -f /tmp/cockpit-logger.chan
# media keeps both CPUs busy at priority 20; tracelogger runs above it so the
# trace buffers still get written.
TLPRIO=
if [ "$SC" = prio-inversion ]; then
    TLPRIO="on -p 40"
fi

if [ -n "$LATE" ]; then
    # Start the trace on a system that is already running, the way it is in
    # the field: every connection was made before the first traced event.
    NAME=$NAME-late
    TRACE=/tmp/$NAME.kev
    OUT=/tmp/$NAME.out
    run logger & run gps & run camera & run nav &
    sleep 1
    run display 80 >$OUT 2>&1 &
    sleep 1
    tracelogger -w -s 3 -f $TRACE >/dev/null 2>&1 &
    TL=$!
else
    # Start the trace first so every ChannelCreate and ConnectAttach is recorded.
    $TLPRIO tracelogger -w -s 6 -f $TRACE >/dev/null 2>&1 &
    TL=$!
    sleep 1
    run logger & run gps & run camera & run nav &
    sleep 1
    if [ "$SC" = prio-inversion ]; then
        media &
        sleep 0.5
    fi
    run display 50 >$OUT 2>&1 &
fi
if [ "$SC" = camera-dies ]; then
    sleep 1
    slay -Q -s KILL camera
fi
wait $TL
# Thread states at the end of the trace, for threads that were blocked all along.
pidin >${TRACE%.kev}.pidin 2>&1
echo "exit: $(pidin -p display -f a 2>/dev/null | tail -n +2 | grep -q . && echo still-running || echo finished)" >>$OUT
slay -Q -s KILL display gps nav camera logger media >/dev/null 2>&1
ls -l $TRACE
