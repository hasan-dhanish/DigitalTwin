/* ==========================================================================
   QNX Neutrino RTOS - City Digital Twin Real-Time Synchronization Engine
   Problem Statement #48 - Production C Implementation
   Target Architecture: QNX 7.1 / POSIX (x86_64 / ARMv8 RPi4)
   Build Command: qcc -Vgcc_ntox86_64 -O2 qnx_twin_engine.c -o qnx_twin_engine -lrt
   Linux Build:   gcc -O2 qnx_twin_engine.c -o twin_engine -lpthread -lrt
   ========================================================================== */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <pthread.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#define NUM_STREAMS 4
#define RING_BUFFER_SIZE 256
#define MAX_SYNC_LATENCY_MS 15.0
#define WATCHDOG_TIMEOUT_MS 150.0
#define UDP_PORT 9999

typedef struct {
    char stream_id[32];
    double value;
    char unit[16];
    struct timespec timestamp;
} sensor_packet_t;

typedef struct {
    sensor_packet_t buffer[RING_BUFFER_SIZE];
    uint32_t head;
    uint32_t tail;
    pthread_mutex_t mutex;
} circular_ring_buffer_t;

typedef struct {
    char name[32];
    uint32_t period_ms;
    uint32_t priority;
    double min_val;
    double max_val;
    char unit[16];
    circular_ring_buffer_t ring_queue;
    bool fault_active;
    double freshness_ms;
    bool is_stale;
} stream_config_t;

static stream_config_t g_streams[NUM_STREAMS] = {
    {"Traffic Telemetry", 50, 150, 30.0, 90.0, "km/h", {}, false, 0.0, false},
    {"Smart Power Grid", 100, 150, 350.0, 500.0, "MW", {}, false, 0.0, false},
    {"Water Pressure Net", 500, 150, 40.0, 80.0, "PSI", {}, false, 0.0, false},
    {"Environmental AQI", 1000, 150, 15.0, 75.0, "AQI", {}, false, 0.0, false}
};

static bool g_running = true;
static double g_current_latency_ms = 0.0;
static double g_max_latency_ms = 0.0;
static uint64_t g_total_ticks = 0;
static uint64_t g_deadline_misses = 0;

/* Helper: Precise Elapsed Time in Milliseconds */
static double get_elapsed_ms(struct timespec start, struct timespec end) {
    return (end.tv_sec - start.tv_sec) * 1000.0 + (end.tv_nsec - start.tv_nsec) / 1000000.0;
}

/* Ring Buffer Functions */
static void ring_buffer_init(circular_ring_buffer_t *rb) {
    rb->head = 0;
    rb->tail = 0;
    pthread_mutex_init(&rb->mutex, NULL);
}

static void ring_buffer_push(circular_ring_buffer_t *rb, sensor_packet_t pkt) {
    pthread_mutex_lock(&rb->mutex);
    rb->buffer[rb->head] = pkt;
    rb->head = (rb->head + 1) % RING_BUFFER_SIZE;
    pthread_mutex_unlock(&rb->mutex);
}

static bool ring_buffer_get_latest(circular_ring_buffer_t *rb, sensor_packet_t *pkt_out) {
    pthread_mutex_lock(&rb->mutex);
    if (rb->head == rb->tail) {
        pthread_mutex_unlock(&rb->mutex);
        return false;
    }
    uint32_t latest_idx = (rb->head == 0) ? (RING_BUFFER_SIZE - 1) : (rb->head - 1);
    *pkt_out = rb->buffer[latest_idx];
    pthread_mutex_unlock(&rb->mutex);
    return true;
}

/* Data Acquisition Task (Priority 150) */
static void* sensor_producer_thread(void *arg) {
    stream_config_t *stream = (stream_config_t*)arg;
    struct timespec next_tick;
    clock_gettime(CLOCK_MONOTONIC, &next_tick);

    while (g_running) {
        if (!stream->fault_active) {
            sensor_packet_t pkt;
            strncpy(pkt.stream_id, stream->name, sizeof(pkt.stream_id));
            double range = stream->max_val - stream->min_val;
            pkt.value = stream->min_val + ((double)rand() / RAND_MAX) * range;
            strncpy(pkt.unit, stream->unit, sizeof(pkt.unit));
            clock_gettime(CLOCK_MONOTONIC, &pkt.timestamp);

            ring_buffer_push(&stream->ring_queue, pkt);
        }

        /* Periodic Sleep */
        next_tick.tv_nsec += stream->period_ms * 1000000L;
        while (next_tick.tv_nsec >= 1000000000L) {
            next_tick.tv_nsec -= 1000000000L;
            next_tick.tv_sec += 1;
        }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next_tick, NULL);
    }
    return NULL;
}

/* Hardware Telemetry Receiver Task (Priority 150) - UDP Port 9999 */
static void* udp_hardware_listener_thread(void *arg) {
    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) return NULL;

    struct sockaddr_in servaddr;
    memset(&servaddr, 0, sizeof(servaddr));
    servaddr.sin_family = AF_INET;
    servaddr.sin_addr.s_addr = INADDR_ANY;
    servaddr.sin_port = htons(UDP_PORT);

    if (bind(sockfd, (const struct sockaddr *)&servaddr, sizeof(servaddr)) < 0) {
        close(sockfd);
        return NULL;
    }

    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 500000;
    setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));

    char buffer[512];
    while (g_running) {
        ssize_t len = recvfrom(sockfd, buffer, sizeof(buffer) - 1, 0, NULL, NULL);
        if (len > 0) {
            buffer[len] = '\0';
            char sid[32] = {0};
            double val = 0.0;
            if (sscanf(buffer, "{\"stream\": \"%31[^\"]\", \"value\": %lf}", sid, &val) == 2) {
                for (int i = 0; i < NUM_STREAMS; i++) {
                    if (strstr(g_streams[i].name, sid) || strcmp(sid, "traffic") == 0) {
                        sensor_packet_t pkt;
                        strncpy(pkt.stream_id, g_streams[i].name, sizeof(pkt.stream_id));
                        pkt.value = val;
                        strncpy(pkt.unit, g_streams[i].unit, sizeof(pkt.unit));
                        clock_gettime(CLOCK_MONOTONIC, &pkt.timestamp);
                        ring_buffer_push(&g_streams[i].ring_queue, pkt);
                        break;
                    }
                }
            }
        }
    }
    close(sockfd);
    return NULL;
}

/* Twin Synchronizer Task (Priority Level 255 - Highest) */
static void* twin_synchronizer_thread(void *arg) {
    struct timespec start_t, end_t, now_t;

    while (g_running) {
        clock_gettime(CLOCK_MONOTONIC, &start_t);
        clock_gettime(CLOCK_MONOTONIC, &now_t);

        for (int i = 0; i < NUM_STREAMS; i++) {
            sensor_packet_t pkt;
            if (ring_buffer_get_latest(&g_streams[i].ring_queue, &pkt)) {
                double fresh = get_elapsed_ms(pkt.timestamp, now_t);
                g_streams[i].freshness_ms = fresh;
                g_streams[i].is_stale = (fresh > WATCHDOG_TIMEOUT_MS);
            } else {
                g_streams[i].is_stale = true;
            }
        }

        clock_gettime(CLOCK_MONOTONIC, &end_t);
        double latency = get_elapsed_ms(start_t, end_t);
        g_current_latency_ms = latency;
        if (latency > g_max_latency_ms) g_max_latency_ms = latency;
        if (latency > MAX_SYNC_LATENCY_MS) g_deadline_misses++;
        g_total_ticks++;

        usleep(10000); /* 10ms Sync Period */
    }
    return NULL;
}

/* CLI Renderer Output Task */
static void render_cli() {
    printf("\033[H\033[J");
    printf("========================================================================\n");
    printf("  QNX REAL-TIME CITY DIGITAL TWIN SYNCHRONIZATION ENGINE [C / POSIX]\n");
    printf("========================================================================\n");
    printf(" Sync Latency       : %.3f ms (Deadline Limit: %.1f ms)\n", g_current_latency_ms, MAX_SYNC_LATENCY_MS);
    printf(" Max Peak Latency   : %.3f ms | Total Ticks: %llu\n", g_max_latency_ms, (unsigned long long)g_total_ticks);
    printf(" Deadline Misses    : %llu\n", (unsigned long long)g_deadline_misses);
    printf("------------------------------------------------------------------------\n");

    for (int i = 0; i < NUM_STREAMS; i++) {
        printf("  * %-22s (%4d ms) | State: %-12s | Freshness: %6.1f ms\n",
            g_streams[i].name,
            g_streams[i].period_ms,
            g_streams[i].is_stale ? "STALE FAULT" : "ONLINE",
            g_streams[i].freshness_ms);
    }
    printf("========================================================================\n");
}

int main(int argc, char *argv[]) {
    printf("Initializing QNX Digital Twin Synchronization Engine...\n");

    for (int i = 0; i < NUM_STREAMS; i++) {
        ring_buffer_init(&g_streams[i].ring_queue);
    }

    pthread_t prod_threads[NUM_STREAMS];
    pthread_t sync_thread;
    pthread_t udp_thread;

    /* Create Producer Threads with Priority 150 */
    for (int i = 0; i < NUM_STREAMS; i++) {
        pthread_create(&prod_threads[i], NULL, sensor_producer_thread, &g_streams[i]);
        struct sched_param param;
        param.sched_priority = g_streams[i].priority; // Priority 150
        pthread_setschedparam(prod_threads[i], SCHED_FIFO, &param);
    }

    /* Create UDP Hardware Listener Thread with Priority 150 */
    pthread_create(&udp_thread, NULL, udp_hardware_listener_thread, NULL);
    struct sched_param udp_param;
    udp_param.sched_priority = 150;
    pthread_setschedparam(udp_thread, SCHED_FIFO, &udp_param);

    /* Create Synchronizer Core Thread with Highest Priority 255 */
    pthread_create(&sync_thread, NULL, twin_synchronizer_thread, NULL);
    struct sched_param sync_param;
    sync_param.sched_priority = 255; // QNX Highest Hard Real-Time Priority
    pthread_setschedparam(sync_thread, SCHED_FIFO, &sync_param);

    /* Run CLI Dashboard loop */
    for (int tick = 0; tick < 100; tick++) {
        render_cli();
        usleep(100000);
    }

    g_running = false;
    for (int i = 0; i < NUM_STREAMS; i++) {
        pthread_join(prod_threads[i], NULL);
    }
    pthread_join(udp_thread, NULL);
    pthread_join(sync_thread, NULL);

    printf("\n[QNX RTOS] Execution complete. Engine shut down cleanly.\n");
    return 0;
}

