#include <stdint.h>
#include <dlfcn.h>
#include <netdb.h>
#include <stdio.h>
#include <android/log.h>
#include <jni.h>
#include "policy.h"

typedef int (*HookFunType)(void *, void *, void **);
typedef int (*UnhookFunType)(void *);
typedef void (*NativeOnModuleLoaded)(const char *, void *);
typedef struct {
    uint32_t version;
    HookFunType hook_func;
    UnhookFunType unhook_func;
} NativeAPIEntries;

static int (*original_getaddrinfo)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
static struct hostent *(*original_gethostbyname)(const char *);
static struct hostent *(*original_gethostbyname2)(const char *, int);
static unsigned log_count;
static int status;
static uint32_t server_address = 0x7f000001u;

__attribute__((visibility("default"), used))
int ag_configure_server(const char *value) {
    uint32_t parsed;
    if (!ag_parse_server(value, &parsed)) return 0;
    __atomic_store_n(&server_address, parsed, __ATOMIC_RELEASE);
    return 1;
}

static const char *destination(char buffer[48], int family) {
    uint32_t address = __atomic_load_n(&server_address, __ATOMIC_ACQUIRE);
    snprintf(buffer, 48, "%s%u.%u.%u.%u", family == AF_INET6 ? "::ffff:" : "",
             (unsigned) (address >> 24), (unsigned) ((address >> 16) & 255),
             (unsigned) ((address >> 8) & 255), (unsigned) (address & 255));
    return buffer;
}

static void log_redirect(const char *host, const char *target) {
    if (__atomic_fetch_add(&log_count, 1, __ATOMIC_RELAXED) < 24)
        __android_log_print(ANDROID_LOG_INFO, "AGDnsRedirect", "Native %s -> %s", host, target);
}

static int redirected_getaddrinfo(const char *host, const char *service,
                                  const struct addrinfo *hints, struct addrinfo **result) {
    if (!original_getaddrinfo) return EAI_AGAIN;
    char buffer[48];
    if (ag_matches(host)) {
        const char *target = destination(buffer, hints ? hints->ai_family : AF_INET);
        log_redirect(host, target);
        host = target;
    }
    return original_getaddrinfo(host, service, hints, result);
}

static struct hostent *redirected_gethostbyname(const char *host) {
    if (!original_gethostbyname) return NULL;
    char buffer[48];
    if (ag_matches(host)) {
        const char *target = destination(buffer, AF_INET);
        log_redirect(host, target); host = target;
    }
    return original_gethostbyname(host);
}

static struct hostent *redirected_gethostbyname2(const char *host, int family) {
    if (!original_gethostbyname2) return NULL;
    char buffer[48];
    if (ag_matches(host)) {
        const char *target = destination(buffer, family);
        log_redirect(host, target); host = target;
    }
    return original_gethostbyname2(host, family);
}

static void on_library_loaded(const char *name, void *handle) {
    (void) name;
    (void) handle;
}

__attribute__((visibility("default"), used))
NativeOnModuleLoaded native_init(const NativeAPIEntries *api) {
    if (!api || !api->hook_func) return NULL;
    void *libc = dlopen("libc.so", RTLD_NOW | RTLD_LOCAL);
    if (!libc) return NULL;
    void *target = dlsym(libc, "getaddrinfo");
    if (target && api->hook_func(target, (void *) redirected_getaddrinfo, (void **) &original_getaddrinfo) == 0) status |= 1;
    target = dlsym(libc, "gethostbyname");
    if (target && api->hook_func(target, (void *) redirected_gethostbyname, (void **) &original_gethostbyname) == 0) status |= 2;
    target = dlsym(libc, "gethostbyname2");
    if (target && api->hook_func(target, (void *) redirected_gethostbyname2, (void **) &original_gethostbyname2) == 0) status |= 4;
    __android_log_print(ANDROID_LOG_INFO, "AGDnsRedirect", "Native API v%u; DNS hook status=%d", api->version, status);
    return on_library_loaded;
}

JNIEXPORT jint JNICALL Java_io_github_qiuyuxc_agdns_MainHook_nativeStatus(JNIEnv *env, jclass type) {
    (void) env;
    (void) type;
    return status;
}

JNIEXPORT jboolean JNICALL Java_io_github_qiuyuxc_agdns_MainHook_nativeConfigure(JNIEnv *env, jclass type, jstring value) {
    (void) type;
    if (!value) return JNI_FALSE;
    const char *address = (*env)->GetStringUTFChars(env, value, NULL);
    if (!address) return JNI_FALSE;
    int result = ag_configure_server(address);
    (*env)->ReleaseStringUTFChars(env, value, address);
    return result ? JNI_TRUE : JNI_FALSE;
}
