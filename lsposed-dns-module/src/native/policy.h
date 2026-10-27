#ifndef AG_DNS_POLICY_H
#define AG_DNS_POLICY_H
#include <stddef.h>
#include <string.h>
#include <stdint.h>

#define AG_SERVER "127.0.0.1"

static inline int ag_parse_server(const char *value, uint32_t *result) {
    if (!value || !result) return 0;
    uint32_t address = 0;
    for (int i = 0; i < 4; i++) {
        const char *start = value;
        unsigned octet = 0;
        while (*value >= '0' && *value <= '9') {
            octet = octet * 10 + (unsigned) (*value++ - '0');
            if (value - start > 3 || octet > 255) return 0;
        }
        if (value == start || (value - start > 1 && *start == '0')) return 0;
        address = (address << 8) | octet;
        if (i < 3) { if (*value++ != '.') return 0; }
        else if (*value) return 0;
    }
    *result = address;
    return 1;
}

static inline int ag_matches(const char *host) {
    if (!host) return 0;
    size_t length = strlen(host);
    if (!length || length > 254) return 0;
    if (host[length - 1] == '.') length--;
    if (!length || host[0] == '.' || host[length - 1] == '.') return 0;
    for (size_t i = 0; i < length; i++) {
        unsigned char c = (unsigned char) host[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9') || c == '-' || c == '.')) return 0;
        if (c == '.' && i && host[i - 1] == '.') return 0;
    }
    static const char *const domains[] = {"open.ys4fun.com", "prod-api-activity.ys4fun.com"};
    for (size_t d = 0; d < sizeof(domains) / sizeof(domains[0]); d++) {
        size_t n = strlen(domains[d]);
        if (length != n) continue;
        int equal = 1;
        for (size_t i = 0; i < n; i++) {
            char c = host[length - n + i];
            if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
            if (c != domains[d][i]) { equal = 0; break; }
        }
        if (equal) return 1;
    }
    return 0;
}
#endif
