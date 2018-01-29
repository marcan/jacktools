#include <stdio.h>
#include <errno.h>
#include <unistd.h>
#include <string.h>
#include <stdlib.h>

#include <jack/jack.h>

#include <stdint.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netdb.h>

void jack_shutdown (void *arg)
{
    exit(1);
}

void usage(char *argv0)
{
    fprintf(stderr, "usage: %s host:port [host:port]...\n", argv0);
    exit(1);
}

int main(int argc, char *argv[])
{
    jack_client_t *client;
    jack_status_t jack_status;

    int period = 10000;

    if (argc < 2)
        usage(argv[0]);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(struct addrinfo));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_DGRAM;

    int sockv4 = -1, sockv6 = -1;

    struct {
        int fd;
        socklen_t addr_len;
        struct sockaddr *addr;
    } *targets;
   
    targets = malloc(sizeof(*targets) * (argc - 1));

    for (int i = 0; i < (argc-1); i++) {
        char *name = argv[i + 1];
        char *col = strrchr(argv[i + 1], ':');
        if (!col || col == name)
            usage(argv[0]);
        *col = '\0';
        char *port = &col[1];
        if (*name == '[') {
            if (col[-1] != ']')
                usage(argv[0]);
            name++;
            col[-1] = '\0';
        }
        struct addrinfo *res;
        int ret = getaddrinfo(name, port, &hints, &res);
        if (ret) {
            fprintf(stderr, "getaddrinfo: %s\n", gai_strerror(ret));
            usage(argv[0]);
        }
        if (res[0].ai_family == AF_INET) {
            if (sockv4 == -1) {
                sockv4 = socket(AF_INET, SOCK_DGRAM, 0);
                if (sockv4 < 0) {
                    fprintf(stderr, "Error creating IPv4 socket\n");
                    exit(1);
                }
            }
            targets[i].fd = sockv4;
        } else if (res[0].ai_family == AF_INET6) {
            if (sockv6 == -1) {
                sockv6 = socket(AF_INET6, SOCK_DGRAM, 0);
                if (sockv6 < 0) {
                    fprintf(stderr, "Error creating IPv6 socket\n");
                    exit(1);
                }
            }
            targets[i].fd = sockv6;
        } else {
            fprintf(stderr, "Unknown address family\n");
            exit(1);
        }
        targets[i].addr = malloc(res[0].ai_addrlen);
        targets[i].addr_len = res[0].ai_addrlen;
        memcpy(targets[i].addr, res[0].ai_addr, res[0].ai_addrlen);
        freeaddrinfo(res);
    }

    if ((client = jack_client_open("jacktsync", JackNullOption, &jack_status)) == 0) {
        fprintf (stderr, "jack server not running?\n");
        return 1;
    }

    jack_on_shutdown (client, jack_shutdown, 0);

    if (jack_activate (client)) {
        fprintf (stderr, "cannot activate client");
        return 1;
    }

    char pkt[1024];

    while (1) {
        jack_position_t pos;
        char *state;
        switch(jack_transport_query(client, &pos)) {
            case JackTransportStopped:
                state="stopped"; break;
            case JackTransportRolling:
                state="rolling"; break;
            case JackTransportLooping:
                state="looping"; break;
            case JackTransportStarting:
                state="starting"; break;
            default:
                state="unk"; break;
        };
        snprintf(pkt, sizeof(pkt),
                 "p=%d f=%d r=%d bbt=%d:%d:%d den=%f:%f bpm=%f state=%s\n",
                 period, pos.frame, pos.frame_rate, pos.bar, pos.beat, pos.tick,
                 pos.beats_per_bar, pos.ticks_per_beat, pos.beats_per_minute,
                 state);
        for (int i = 0; i < (argc-1); i++) {
            sendto(targets[i].fd, pkt, strlen(pkt), MSG_DONTWAIT,
                   targets[i].addr, targets[i].addr_len);
        }
        struct timeval to = {
            .tv_sec = 0,
            .tv_usec = period,
        };
        select(0, NULL, NULL, NULL, &to);
    }

    jack_client_close (client);
    exit (0);
}

