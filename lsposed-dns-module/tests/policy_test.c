#include <stdio.h>
#include <stdlib.h>
#include "../src/native/policy.h"

int main(int argc, char **argv) {
    uint32_t address;
    const char *invalid[] = {NULL, "", "256.1.2.3", "1.2.3", "1.2.3.4.5", "01.2.3.4",
        "9999999999.2.3.4", "https://1.2.3.4", "1.2.3.4:443", "1.2.3.4/path", "1.2.3.4@evil.test"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++)
        if (ag_parse_server(invalid[i], &address)) return 3;
    if (!ag_parse_server(AG_SERVER, &address) || address != 0x7f000001u) return 4;
    if (!ag_parse_server("203.0.113.42", &address) || address != 0xcb00712au) return 5;
    if (!ag_parse_server("255.255.255.255", &address) || address != 0xffffffffu) return 6;
    if (argc != 2 || ag_matches(NULL)) return 2;
    FILE *f = fopen(argv[1], "r");
    if (!f) return 2;
    char line[1024];
    unsigned count = 0;
    while (fgets(line, sizeof(line), f)) {
        if (line[0] == '#') continue;
        line[strcspn(line, "\r\n")] = 0;
        if (line[1] != '\t' || ag_matches(line + 2) != (line[0] == '1')) {
            fprintf(stderr, "Unexpected domain policy: %s\n", line);
            return 1;
        }
        count++;
    }
    fclose(f);
    printf("Native domain policy: %u cases passed\n", count);
    return 0;
}
