#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifndef PACKET_IGNORE_OUTGOING
#define PACKET_IGNORE_OUTGOING 23
#endif

#define FRAME_BUFFER_SIZE 65536
#define SOCKET_BUFFER_SIZE (4 * 1024 * 1024)

struct port {
    const char *name;
    int fd;
    int ifindex;
    uint64_t frames_rx;
    uint64_t bytes_rx;
    uint64_t frames_tx;
    uint64_t bytes_tx;
    uint64_t errors;
};

static volatile sig_atomic_t running = 1;

static void handle_signal(int signo)
{
    (void)signo;
    running = 0;
}

static int get_ifindex(int fd, const char *name)
{
    struct ifreq ifr;

    memset(&ifr, 0, sizeof(ifr));
    if (strlen(name) >= sizeof(ifr.ifr_name)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    strncpy(ifr.ifr_name, name, sizeof(ifr.ifr_name) - 1);

    if (ioctl(fd, SIOCGIFINDEX, &ifr) < 0)
        return -1;

    return ifr.ifr_ifindex;
}

static int open_port(struct port *port)
{
    struct sockaddr_ll bind_addr;
    struct packet_mreq membership;
    int value;

    port->fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (port->fd < 0) {
        perror("socket(AF_PACKET)");
        return -1;
    }

    port->ifindex = get_ifindex(port->fd, port->name);
    if (port->ifindex < 0) {
        fprintf(stderr, "%s: cannot obtain interface index: %s\n",
                port->name, strerror(errno));
        close(port->fd);
        return -1;
    }

    value = SOCKET_BUFFER_SIZE;
    if (setsockopt(port->fd, SOL_SOCKET, SO_RCVBUF,
                   &value, sizeof(value)) < 0)
        fprintf(stderr, "%s: warning: cannot enlarge receive buffer: %s\n",
                port->name, strerror(errno));

    value = SOCKET_BUFFER_SIZE;
    if (setsockopt(port->fd, SOL_SOCKET, SO_SNDBUF,
                   &value, sizeof(value)) < 0)
        fprintf(stderr, "%s: warning: cannot enlarge send buffer: %s\n",
                port->name, strerror(errno));

    value = 1;
    if (setsockopt(port->fd, SOL_PACKET, PACKET_IGNORE_OUTGOING,
                   &value, sizeof(value)) < 0)
        fprintf(stderr, "%s: warning: PACKET_IGNORE_OUTGOING unavailable; "
                "falling back to packet-type filtering\n", port->name);

    memset(&bind_addr, 0, sizeof(bind_addr));
    bind_addr.sll_family = AF_PACKET;
    bind_addr.sll_protocol = htons(ETH_P_ALL);
    bind_addr.sll_ifindex = port->ifindex;
    if (bind(port->fd, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
        fprintf(stderr, "%s: bind failed: %s\n", port->name, strerror(errno));
        close(port->fd);
        return -1;
    }

    memset(&membership, 0, sizeof(membership));
    membership.mr_ifindex = port->ifindex;
    membership.mr_type = PACKET_MR_PROMISC;
    if (setsockopt(port->fd, SOL_PACKET, PACKET_ADD_MEMBERSHIP,
                   &membership, sizeof(membership)) < 0) {
        fprintf(stderr, "%s: cannot enable promiscuous reception: %s\n",
                port->name, strerror(errno));
        close(port->fd);
        return -1;
    }

    return 0;
}

static int relay_one(struct port *source, struct port *destination,
                     unsigned char *buffer)
{
    struct sockaddr_ll packet_info;
    socklen_t packet_info_length = sizeof(packet_info);
    ssize_t received;
    ssize_t sent;

    memset(&packet_info, 0, sizeof(packet_info));
    received = recvfrom(source->fd, buffer, FRAME_BUFFER_SIZE, 0,
                        (struct sockaddr *)&packet_info,
                        &packet_info_length);
    if (received < 0) {
        if (errno != EINTR && errno != EAGAIN) {
            fprintf(stderr, "%s: receive failed: %s\n",
                    source->name, strerror(errno));
            source->errors++;
        }
        return -1;
    }

    /* Never forward a frame that this host just transmitted. */
    if (packet_info.sll_pkttype == PACKET_OUTGOING)
        return 0;

    if (received < ETH_HLEN)
        return 0;

    source->frames_rx++;
    source->bytes_rx += (uint64_t)received;

    sent = send(destination->fd, buffer, (size_t)received, 0);
    if (sent != received) {
        if (sent < 0)
            fprintf(stderr, "%s -> %s: send failed for %zd-byte frame: %s\n",
                    source->name, destination->name, received, strerror(errno));
        else
            fprintf(stderr, "%s -> %s: short send: %zd of %zd bytes\n",
                    source->name, destination->name, sent, received);
        destination->errors++;
        return -1;
    }

    destination->frames_tx++;
    destination->bytes_tx += (uint64_t)sent;
    return 1;
}

static void print_statistics(const struct port *a, const struct port *b)
{
    const char *line_end = isatty(STDERR_FILENO) ? "\r" : "\n";

    fprintf(stderr,
            "%s RX=%llu/%lluB TX=%llu/%lluB ERR=%llu | "
            "%s RX=%llu/%lluB TX=%llu/%lluB ERR=%llu%s",
            a->name,
            (unsigned long long)a->frames_rx,
            (unsigned long long)a->bytes_rx,
            (unsigned long long)a->frames_tx,
            (unsigned long long)a->bytes_tx,
            (unsigned long long)a->errors,
            b->name,
            (unsigned long long)b->frames_rx,
            (unsigned long long)b->bytes_rx,
            (unsigned long long)b->frames_tx,
            (unsigned long long)b->bytes_tx,
            (unsigned long long)b->errors,
            line_end);
    fflush(stderr);
}

int main(int argc, char **argv)
{
    struct port ports[2];
    struct pollfd poll_fds[2];
    unsigned char *buffers[2] = {NULL, NULL};
    time_t last_report;
    int result = EXIT_FAILURE;

    if (argc != 3) {
        fprintf(stderr, "Usage: %s <interface-a> <interface-b>\n", argv[0]);
        return EXIT_FAILURE;
    }
    if (strcmp(argv[1], argv[2]) == 0) {
        fprintf(stderr, "The two interface names must be different.\n");
        return EXIT_FAILURE;
    }

    memset(ports, 0, sizeof(ports));
    ports[0].name = argv[1];
    ports[1].name = argv[2];
    ports[0].fd = -1;
    ports[1].fd = -1;

    buffers[0] = malloc(FRAME_BUFFER_SIZE);
    buffers[1] = malloc(FRAME_BUFFER_SIZE);
    if (buffers[0] == NULL || buffers[1] == NULL) {
        fprintf(stderr, "Cannot allocate packet buffers.\n");
        goto cleanup;
    }

    if (open_port(&ports[0]) < 0 || open_port(&ports[1]) < 0)
        goto cleanup;

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    poll_fds[0].fd = ports[0].fd;
    poll_fds[0].events = POLLIN;
    poll_fds[1].fd = ports[1].fd;
    poll_fds[1].events = POLLIN;

    fprintf(stderr, "Relaying Ethernet frames between %s and %s. "
            "Press Ctrl+C to stop.\n", ports[0].name, ports[1].name);
    last_report = time(NULL);

    while (running) {
        int ready = poll(poll_fds, 2, 1000);
        time_t now;

        if (ready < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            goto cleanup;
        }

        if (poll_fds[0].revents & POLLIN)
            relay_one(&ports[0], &ports[1], buffers[0]);
        if (poll_fds[1].revents & POLLIN)
            relay_one(&ports[1], &ports[0], buffers[1]);

        if ((poll_fds[0].revents | poll_fds[1].revents) &
            (POLLERR | POLLHUP | POLLNVAL)) {
            fprintf(stderr, "\nNetwork interface polling error.\n");
            goto cleanup;
        }

        now = time(NULL);
        if (now - last_report >= 5) {
            print_statistics(&ports[0], &ports[1]);
            last_report = now;
        }
    }

    result = EXIT_SUCCESS;

cleanup:
    if (ports[0].fd >= 0)
        close(ports[0].fd);
    if (ports[1].fd >= 0)
        close(ports[1].fd);
    free(buffers[0]);
    free(buffers[1]);
    fprintf(stderr, "\nRelay stopped.\n");
    return result;
}
