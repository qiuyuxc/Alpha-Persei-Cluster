#ifndef AGCONNECT_BRIDGE_H
#define AGCONNECT_BRIDGE_H

#include <stddef.h>
#include <sys/types.h>

#define AGCONNECT_API __attribute__((visibility("default")))
typedef struct agconnect_tunnel AgconnectTunnel;

/* Packet API also used by the no-root TCP/TLS integration test. */
AGCONNECT_API AgconnectTunnel *agconnect_open(const char *configuration);
AGCONNECT_API ssize_t agconnect_read(AgconnectTunnel *, void *, size_t, int timeout_ms);
AGCONNECT_API ssize_t agconnect_write(AgconnectTunnel *, const void *, size_t);
AGCONNECT_API int agconnect_alive(AgconnectTunnel *);
AGCONNECT_API void agconnect_stop(AgconnectTunnel *);
AGCONNECT_API void agconnect_destroy(AgconnectTunnel *);

#endif
