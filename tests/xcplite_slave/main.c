// XCP test slave on XCPlite for the zelos-extension-xcp e2e suite.
// Usage: xcp_test_slave <port> <tcp 0|1> <fast_hz>
// Writes xcp_test_slave.a2l to the working directory (atomic rename).
// Events: fast (fast_hz), medium (10 ms), slow (100 ms), all driven by one thread.
// fast: counter, sig_NNN = counter + NNN (uint32, 8-byte stride: one ODT entry each).

#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <a2l.h>
#include <xcplib.h>

#define NAME "xcp_test_slave"
#define EPK "XTS_V1.0"
#define N_SIGS 1000 // past the DAQ memory (~500 entries): selecting all must not fit
#define QUEUE_SIZE (1024 * 256)

typedef struct {
    uint32_t v;
    uint32_t pad; // keeps entries non-contiguous
} slot_t;

uint32_t counter = 0;
slot_t sigs[N_SIGS];
uint32_t medium_counter = 0;
uint16_t temperature = 0; // 0.1 degC per bit, raw
uint32_t slow_counter = 0;
uint8_t state = 0; // enum

static volatile bool running = true;
static void on_signal(int s) {
    (void)s;
    running = false;
}

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void sleep_until(uint64_t t) {
    uint64_t n = now_ns();
    if (t <= n)
        return;
    uint64_t d = t - n;
    struct timespec ts = {(time_t)(d / 1000000000ull), (long)(d % 1000000000ull)};
    nanosleep(&ts, NULL);
}

int main(int argc, char *argv[]) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <port> <tcp 0|1> <fast_hz>\n", argv[0]);
        return 2;
    }
    uint16_t port = (uint16_t)atoi(argv[1]);
    bool tcp = atoi(argv[2]) != 0;
    uint32_t fast_hz = (uint32_t)atoi(argv[3]);
    if (port == 0 || fast_hz == 0 || fast_hz > 100000) {
        fprintf(stderr, "bad arguments\n");
        return 2;
    }
    uint64_t fast_ns = 1000000000ull / fast_hz;
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    XcpSetLogLevel(1);
    if (!XcpInit(NAME, EPK, XCP_MODE_LOCAL))
        return 1;
    uint8_t addr[4] = {127, 0, 0, 1};
    if (!XcpEthServerInit(addr, port, tcp, QUEUE_SIZE))
        return 1;

    // Generate in a subdir, then rename: the cwd only ever holds a complete A2L.
    mkdir("a2l.tmp", 0755);
    if (chdir("a2l.tmp") != 0)
        return 1;
    if (!A2lInit(addr, port, tcp, A2L_MODE_WRITE_ALWAYS))
        return 1;

    tXcpEventId ev_fast = XcpCreateEvent("fast", (uint32_t)fast_ns, 0);
    tXcpEventId ev_medium = XcpCreateEvent("medium", 10000000, 0);
    tXcpEventId ev_slow = XcpCreateEvent("slow", 100000000, 0);

    A2lSetAbsoluteAddrMode_i(ev_fast);
    A2lCreateMeasurement_(NULL, "counter", A2L_TYPE_UINT32, 1, &counter, NULL, 0.0, 4294967295.0, "fast tick counter");
    char name[16];
    for (int i = 0; i < N_SIGS; i++) {
        snprintf(name, sizeof(name), "sig_%03d", i);
        A2lCreateMeasurement_(NULL, name, A2L_TYPE_UINT32, 1, &sigs[i].v, NULL, 0.0, 4294967295.0, "counter + NNN");
    }

    A2lSetAbsoluteAddrMode_i(ev_medium);
    A2lCreateMeasurement_(NULL, "medium_counter", A2L_TYPE_UINT32, 1, &medium_counter, NULL, 0.0, 4294967295.0, "medium tick counter");
    A2lCreateMeasurement_(NULL, "temperature", A2L_TYPE_UINT16, 1, &temperature, "degC", 0.0, 65535.0, "raw temperature");

    A2lSetAbsoluteAddrMode_i(ev_slow);
    A2lCreateMeasurement_(NULL, "slow_counter", A2L_TYPE_UINT32, 1, &slow_counter, NULL, 0.0, 4294967295.0, "slow tick counter");
    A2lCreateEnumConversion(state, "4 0 \"OFF\" 1 \"IDLE\" 2 \"RUN\" 3 \"FAULT\"");
    A2lCreateMeasurement_(NULL, "state", A2L_TYPE_UINT8, 1, &state, "conv.state", 0.0, 3.0, "state machine");

    if (!A2lFinalize())
        return 1;
    if (rename(NAME ".a2l", "../" NAME ".a2l") != 0)
        return 1;
    if (chdir("..") != 0)
        return 1;
    rmdir("a2l.tmp");

    uint32_t per_medium = (uint32_t)(10000000ull / fast_ns);
    uint32_t per_slow = (uint32_t)(100000000ull / fast_ns);
    if (per_medium == 0)
        per_medium = 1;
    if (per_slow == 0)
        per_slow = 1;

    uint64_t next = now_ns();
    uint64_t tick = 0;
    while (running) {
        counter++;
        for (uint32_t i = 0; i < N_SIGS; i++)
            sigs[i].v = counter + i;
        XcpEvent(ev_fast);
        if (tick % per_medium == 0) {
            medium_counter++;
            temperature = (uint16_t)(200 + (medium_counter % 100));
            XcpEvent(ev_medium);
        }
        if (tick % per_slow == 0) {
            slow_counter++;
            state = (uint8_t)((slow_counter / 10) % 4);
            XcpEvent(ev_slow);
        }
        tick++;
        next += fast_ns;
        uint64_t n = now_ns();
        if (n > next + 100000000ull) // stopped (SIGSTOP) or starved: resync, no burst
            next = n;
        sleep_until(next);
    }

    XcpDisconnect();
    XcpEthServerShutdown();
    return 0;
}
