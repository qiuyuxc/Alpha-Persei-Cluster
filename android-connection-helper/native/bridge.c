/* Socketpair adapter for hev-socks5-tunnel. The Android VPN fd stays in Java. */
#include "bridge.h"
#include "hev-main.h"
#include <jni.h>
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

struct agconnect_tunnel {
    pthread_t worker;
    pthread_mutex_t mutex;
    pthread_cond_t initialized;
    int pair[2], ready, finished;
    char *configuration;
    atomic_int alive, stopping;
};

/* The upstream stack is a process singleton. Never start two instances. */
static pthread_mutex_t owner_mutex = PTHREAD_MUTEX_INITIALIZER;
static AgconnectTunnel *owner;

int __real_hev_socks5_tunnel_run(void);
int __wrap_hev_socks5_tunnel_run(void)
{
    AgconnectTunnel *t = owner;
    pthread_mutex_lock(&t->mutex);
    t->ready = 1; /* All upstream initialization completed before run(). */
    pthread_cond_broadcast(&t->initialized);
    pthread_mutex_unlock(&t->mutex);
    return __real_hev_socks5_tunnel_run();
}

static void *run_engine(void *argument)
{
    AgconnectTunnel *t = argument;
    hev_socks5_tunnel_main_from_str((const unsigned char *)t->configuration,
                                  (unsigned int)strlen(t->configuration), t->pair[1]);
    pthread_mutex_lock(&t->mutex);
    atomic_store(&t->alive, 0);
    t->finished = 1;
    pthread_cond_broadcast(&t->initialized);
    pthread_mutex_unlock(&t->mutex);
    return NULL;
}

AgconnectTunnel *agconnect_open(const char *configuration)
{
    if (!configuration || strlen(configuration) > 16384) { errno = EINVAL; return NULL; }
    pthread_mutex_lock(&owner_mutex);
    if (owner) { pthread_mutex_unlock(&owner_mutex); errno = EBUSY; return NULL; }
    AgconnectTunnel *t = calloc(1, sizeof(*t));
    if (!t) { pthread_mutex_unlock(&owner_mutex); return NULL; }
    t->pair[0] = t->pair[1] = -1;
    t->configuration = strdup(configuration);
    if (!t->configuration || socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, t->pair) != 0) {
        int error = errno;
        free(t->configuration); free(t);
        pthread_mutex_unlock(&owner_mutex); errno = error; return NULL;
    }
    int capacity = 262144;
    for (int i = 0; i < 2; i++) {
        setsockopt(t->pair[i], SOL_SOCKET, SO_RCVBUF, &capacity, sizeof(capacity));
        setsockopt(t->pair[i], SOL_SOCKET, SO_SNDBUF, &capacity, sizeof(capacity));
    }
    pthread_mutex_init(&t->mutex, NULL);
    pthread_cond_init(&t->initialized, NULL);
    atomic_init(&t->alive, 1);
    atomic_init(&t->stopping, 0);
    owner = t;
    int error = pthread_create(&t->worker, NULL, run_engine, t);
    if (error) {
        owner = NULL;
        close(t->pair[0]); close(t->pair[1]);
        pthread_cond_destroy(&t->initialized); pthread_mutex_destroy(&t->mutex);
        free(t->configuration); free(t);
        pthread_mutex_unlock(&owner_mutex); errno = error; return NULL;
    }
    pthread_mutex_unlock(&owner_mutex);
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;
    pthread_mutex_lock(&t->mutex);
    while (!t->ready && !t->finished) {
        if (pthread_cond_timedwait(&t->initialized, &t->mutex, &deadline) == ETIMEDOUT) break;
    }
    int initialized = t->ready && !t->finished;
    pthread_mutex_unlock(&t->mutex);
    if (!initialized) { agconnect_destroy(t); errno = EIO; return NULL; }
    return t;
}

int agconnect_alive(AgconnectTunnel *t)
{
    return t && atomic_load(&t->alive) && !atomic_load(&t->stopping);
}

ssize_t agconnect_read(AgconnectTunnel *t, void *buffer, size_t capacity, int timeout_ms)
{
    if (!agconnect_alive(t)) return 0;
    struct pollfd descriptor = { .fd = t->pair[0], .events = POLLIN };
    int result;
    do { result = poll(&descriptor, 1, timeout_ms); } while (result < 0 && errno == EINTR);
    if (!agconnect_alive(t)) return 0;
    if (!result) return -2; /* A timeout is distinct from EOF or an error. */
    if (result < 0) return -1;
    ssize_t size;
    do { size = recv(t->pair[0], buffer, capacity, MSG_DONTWAIT); } while (size < 0 && errno == EINTR);
    if (size < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return -2;
    return size;
}

ssize_t agconnect_write(AgconnectTunnel *t, const void *packet, size_t size)
{
    if (!agconnect_alive(t)) { errno = EPIPE; return -1; }
    ssize_t result;
    do { result = send(t->pair[0], packet, size, MSG_DONTWAIT | MSG_NOSIGNAL); }
    while (result < 0 && errno == EINTR);
    return result;
}

void agconnect_stop(AgconnectTunnel *t)
{
    if (!t || atomic_exchange(&t->stopping, 1)) return;
    if (atomic_load(&t->alive)) hev_socks5_tunnel_quit();
    shutdown(t->pair[0], SHUT_RDWR); /* Wake Java's packet reader without closing its fd. */
}

void agconnect_destroy(AgconnectTunnel *t)
{
    if (!t) return;
    agconnect_stop(t);
    pthread_join(t->worker, NULL);
    pthread_mutex_lock(&owner_mutex);
    close(t->pair[0]); close(t->pair[1]);
    pthread_cond_destroy(&t->initialized); pthread_mutex_destroy(&t->mutex);
    free(t->configuration);
    owner = NULL;
    free(t);
    pthread_mutex_unlock(&owner_mutex);
}

static void io_exception(JNIEnv *env, const char *message)
{
    jclass type = (*env)->FindClass(env, "java/io/IOException");
    if (type) { (*env)->ThrowNew(env, type, message); (*env)->DeleteLocalRef(env, type); }
}

#define TUNNEL(value) ((AgconnectTunnel *)(intptr_t)(value))
#define JNI_METHOD(name) Java_io_github_qiuyuxc_agconnect_NativeTunnel_##name

JNIEXPORT jlong JNICALL JNI_METHOD(nativeOpen)(JNIEnv *env, jclass klass, jstring configuration)
{
    (void)klass;
    if (!configuration) { io_exception(env, "Missing local TCP configuration"); return 0; }
    const char *text = (*env)->GetStringUTFChars(env, configuration, NULL);
    if (!text) return 0;
    AgconnectTunnel *t = agconnect_open(text);
    int error = errno;
    (*env)->ReleaseStringUTFChars(env, configuration, text);
    if (!t) io_exception(env, strerror(error));
    return (jlong)(intptr_t)t;
}

JNIEXPORT jbyteArray JNICALL JNI_METHOD(nativeRead)(JNIEnv *env, jclass klass, jlong handle)
{
    (void)klass;
    unsigned char packet[65535];
    ssize_t size = agconnect_read(TUNNEL(handle), packet, sizeof(packet), 250);
    if (size == -2) return (*env)->NewByteArray(env, 0);
    if (size <= 0) {
        if (size < 0 && agconnect_alive(TUNNEL(handle))) io_exception(env, strerror(errno));
        return NULL;
    }
    jbyteArray result = (*env)->NewByteArray(env, (jsize)size);
    if (result) (*env)->SetByteArrayRegion(env, result, 0, (jsize)size, (const jbyte *)packet);
    return result;
}

JNIEXPORT jboolean JNICALL JNI_METHOD(nativeWrite)(JNIEnv *env, jclass klass, jlong handle, jbyteArray packet)
{
    (void)klass;
    jsize size = (*env)->GetArrayLength(env, packet);
    if (size < 20 || size > 65535) { io_exception(env, "Invalid IP packet size"); return JNI_FALSE; }
    unsigned char bytes[65535];
    (*env)->GetByteArrayRegion(env, packet, 0, size, (jbyte *)bytes);
    if ((*env)->ExceptionCheck(env)) return JNI_FALSE;
    ssize_t result = agconnect_write(TUNNEL(handle), bytes, (size_t)size);
    if (result < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOBUFS)) return JNI_FALSE;
    if (result != size) { io_exception(env, strerror(errno)); return JNI_FALSE; }
    return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL JNI_METHOD(nativeAlive)(JNIEnv *env, jclass klass, jlong handle)
{ (void)env; (void)klass; return agconnect_alive(TUNNEL(handle)) ? JNI_TRUE : JNI_FALSE; }

JNIEXPORT void JNICALL JNI_METHOD(nativeStop)(JNIEnv *env, jclass klass, jlong handle)
{ (void)env; (void)klass; agconnect_stop(TUNNEL(handle)); }

JNIEXPORT void JNICALL JNI_METHOD(nativeDestroy)(JNIEnv *env, jclass klass, jlong handle)
{ (void)env; (void)klass; agconnect_destroy(TUNNEL(handle)); }
