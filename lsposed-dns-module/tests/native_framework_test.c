/* Android integration test: mock LSPosed registration, exercise real bionic DNS.
 * No instructions or live DNS configuration are modified by this executable. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <dlfcn.h>
#include <netdb.h>
#include <arpa/inet.h>

typedef int (*HookFunType)(void *, void *, void **);
typedef void (*Callback)(const char *, void *);
typedef struct { uint32_t version; HookFunType hook; int (*unhook)(void *); } Api;
static int (*redirected)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
static struct hostent *(*redirected_name)(const char *);
static struct hostent *(*redirected_name2)(const char *, int);
static const char *observed_host;

static int observe_resolver(const char *host, const char *service,
                            const struct addrinfo *hints, struct addrinfo **result) {
    observed_host = host;
    /* A controlled resolver failure verifies CDN names are delegated unchanged
     * without making this regression test depend on public DNS availability. */
    if (!strcmp(host, "download.ys4fun.com")) return EAI_NONAME;
    return getaddrinfo(host, service, hints, result);
}

static int register_hook(void *original, void *replacement, void **backup) {
    *backup = original;
    if (original == dlsym(RTLD_DEFAULT, "getaddrinfo")) {
        redirected = replacement;
        *backup = (void *) observe_resolver;
    }
    if (original == dlsym(RTLD_DEFAULT, "gethostbyname")) redirected_name = replacement;
    if (original == dlsym(RTLD_DEFAULT, "gethostbyname2")) redirected_name2 = replacement;
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    void *library = dlopen(argv[1], RTLD_NOW);
    if (!library) { fprintf(stderr, "%s\n", dlerror()); return 2; }
    Callback (*init)(const Api *) = dlsym(library, "native_init");
    int (*configure)(const char *) = dlsym(library, "ag_configure_server");
    Api api = {1, register_hook, NULL};
    if (!init || !configure || !init(&api) || !redirected || !redirected_name || !redirected_name2) return 2;
    const char *servers[] = {"127.0.0.1", "203.0.113.42"};
    for (unsigned n = 0; n < sizeof(servers) / sizeof(servers[0]); n++) {
    const char *server = servers[n];
    if (n && !configure(server)) return 11;
    if (configure("256.1.2.3") || configure("1.2.3.4:443") || configure(NULL)) return 12;
    struct addrinfo hints = {0}, *result = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    int rc = redirected("OPEN.YS4FUN.COM.", "443", &hints, &result);
    if (rc || !result || result->ai_family != AF_INET) return 3;
    struct sockaddr_in *address = (struct sockaddr_in *) result->ai_addr;
    char ip[INET6_ADDRSTRLEN];
    inet_ntop(AF_INET, &address->sin_addr, ip, sizeof(ip));
    if (strcmp(ip, server) || ntohs(address->sin_port) != 443) return 4;
    freeaddrinfo(result);
    result = NULL;
    rc = redirected("127.0.0.1", "8102", &hints, &result);
    if (rc || !result) return 5;
    address = (struct sockaddr_in *) result->ai_addr;
    inet_ntop(AF_INET, &address->sin_addr, ip, sizeof(ip));
    if (strcmp(ip, "127.0.0.1") || ntohs(address->sin_port) != 8102) return 6;
    freeaddrinfo(result);
    result = NULL;
    hints.ai_family = AF_INET6;
    rc = redirected("prod-api-activity.ys4fun.com", "443", &hints, &result);
    if (rc || !result || result->ai_family != AF_INET6) return 7;
    struct sockaddr_in6 *v6 = (struct sockaddr_in6 *) result->ai_addr;
    if (!IN6_IS_ADDR_V4MAPPED(&v6->sin6_addr) || ntohs(v6->sin6_port) != 443) return 8;
    struct in_addr expected;
    if (inet_pton(AF_INET, server, &expected) != 1 || memcmp(v6->sin6_addr.s6_addr + 12, &expected, 4)) return 9;
    freeaddrinfo(result);
    result = NULL;
    hints.ai_family = AF_INET;
    rc = redirected("download.ys4fun.com", "443", &hints, &result);
    if (rc != EAI_NONAME || result || strcmp(observed_host, "download.ys4fun.com")) return 10;
    struct hostent *entry = redirected_name("open.ys4fun.com");
    if (!entry || entry->h_addrtype != AF_INET || memcmp(entry->h_addr_list[0], &expected, 4)) return 13;
    entry = redirected_name2("prod-api-activity.ys4fun.com", AF_INET6);
    if (!entry || entry->h_addrtype != AF_INET6 || memcmp(entry->h_addr_list[0] + 12, &expected, 4)) return 14;
    }
    puts("Android DNS: default/custom server, invalid update rejection, all three resolvers, IPv4/IPv6, ports and CDN passthrough passed");
    return 0;
}
