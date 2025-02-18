#!/usr/bin/env python
# This file is part of tcollector.
# Copyright (C) 2011  StumbleUpon, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser
# General Public License for more details.  You should have received a copy
# of the GNU Lesser General Public License along with this program.  If not,
# see <http://www.gnu.org/licenses/>.

# Note: I spent many hours reading the Linux kernel's source code to infer the
# exact meaning of some of the obscure but useful metrics it exposes.  The
# description of the metrics are correct to the best of my knowledge, but it's
# not always to make sense of the Linux kernel's code.  Please report any
# inaccuracy you find.  -- tsuna.
"""Socket allocation and network statistics for TSDB.

Metrics from /proc/net/sockstat:
  - net.sockstat.num_sockets: Number of sockets allocated (only TCP).
  - net.sockstat.num_timewait: Number of TCP sockets currently in
    TIME_WAIT state.
  - net.sockstat.sockets_inuse: Number of sockets in use (TCP/UDP/raw).
  - net.sockstat.num_orphans: Number of orphan TCP sockets (not attached
    to any file descriptor).
  - net.sockstat.memory: Memory allocated for this socket type (in bytes).
  - net.sockstat.ipfragqueues: Number of IP flows for which there are
    currently fragments queued for reassembly.

Metrics from /proc/net/netstat (`netstat -s' command):
  - net.stat.tcp.abort: Number of connections that the kernel had to abort.
    type=memory is especially bad, the kernel had to drop a connection due to
    having too many orphaned sockets.  Other types are normal (e.g. timeout).
  - net.stat.tcp.abort.failed: Number of times the kernel failed to abort a
    connection because it didn't even have enough memory to reset it (bad).
  - net.stat.tcp.congestion.recovery: Number of times the kernel detected
    spurious retransmits and was able to recover part or all of the CWND.
  - net.stat.tcp.delayedack: Number of delayed ACKs sent of different types.
  - net.stat.tcp.failed_accept: Number of times a connection had to be dropped
    after the 3WHS.  reason=full_acceptq indicates that the application isn't
    accepting connections fast enough.  You should see SYN cookies too.
  - net.stat.tcp.invalid_sack: Number of invalid SACKs we saw of diff types.
    (requires Linux v2.6.24-rc1 or newer)
  - net.stat.tcp.memory.pressure: Number of times a socket entered the
    "memory pressure" mode (not great).
  - net.stat.tcp.memory.prune: Number of times a socket had to discard
    received data due to low memory conditions (bad).
  - net.stat.tcp.packetloss.recovery: Number of times we recovered from packet
    loss by type of recovery (e.g. fast retransmit vs SACK).
  - net.stat.tcp.receive.queue.full: Number of times a received packet had to
    be dropped because the socket's receive queue was full.
    (requires Linux v2.6.34-rc2 or newer)
  - net.stat.tcp.reording: Number of times we detected re-ordering and how.
  - net.stat.tcp.syncookies: SYN cookies (both sent & received).
"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals
import os
import pwd
import re
import resource
import sys
import time
from io import open

from collections import OrderedDict

# If we're running as root and this user exists, we'll drop privileges.
USER = "nobody"

COLLECTION_INTERVAL = 30  # seconds

# Scalyr edit:  Check environment variable for collection interval.  TODO:  See if we can centralize code, but
# difficult without requiring collectors including common module which is goes against tcollector architecture.
try:
    if "TCOLLECTOR_SAMPLE_INTERVAL" in os.environ:
        COLLECTION_INTERVAL = float(os.environ["TCOLLECTOR_SAMPLE_INTERVAL"])
except ValueError:
    pass

# Note: up until v2.6.37-rc2 most of the values were 32 bits.
# The first value is pretty useless since it accounts for some
# socket types but not others.  So we don't report it because it's
# more confusing than anything else and it's not well documented
# what type of sockets are or aren't included in this count.
REGEXP = re.compile(
    r"sockets: used \d+\n"
    r"TCP: inuse (?P<tcp_inuse>\d+) orphan (?P<orphans>\d+)"
    r" tw (?P<tw_count>\d+) alloc (?P<tcp_sockets>\d+)"
    r" mem (?P<tcp_pages>\d+)\n"
    r"UDP: inuse (?P<udp_inuse>\d+)"
    # UDP memory accounting was added in v2.6.25-rc1
    r"(?: mem (?P<udp_pages>\d+))?\n"
    # UDP-Lite (RFC 3828) was added in v2.6.20-rc2
    r"(?:UDPLITE: inuse (?P<udplite_inuse>\d+)\n)?"
    r"RAW: inuse (?P<raw_inuse>\d+)\n"
    r"FRAG: inuse (?P<ip_frag_nqueues>\d+)"
    r" memory (?P<ip_frag_mem>\d+)\n"
)


def drop_privileges():
    """Drops privileges if running as root."""
    try:
        ent = pwd.getpwnam(USER)
    except KeyError:
        return

    if os.getuid() != 0:
        return

    os.setgid(ent.pw_gid)
    os.setuid(ent.pw_uid)


def parse_and_print_metrics(f_netstat, f_sockstat, output_file_success=None,
                            output_file_error=None):
    """
    Parse /proc/net/netstat and /proc/net/sockstat and print metrics to the provided file
    handle.

    :param f_netstat: Open file handle to /proc/netstat/netstat file. That's done to avoid re-opening
    the file on each function call / main loop iteration

    :param f_sockstat: Open file handle to /proc/netstat/netstat file. That's done to avoid re-opening
    the file on each function call / main loop iteration
    """
    output_file_success = output_file_success or sys.stdout
    output_file_error = output_file_error or sys.stderr

    page_size = resource.getpagesize()

    def print_sockstat(metric, value, tags="", output_file_success=None):  # Note: tags must start with ' '
        output_file_success = output_file_success or sys.stdout

        if value is not None:
            print("net.sockstat.%s %d %s%s" % (metric, ts, value, tags), file=output_file_success)

    # If a line in /proc/net/netstat doesn't start with a word in that dict,
    # we'll ignore it.  We use the value to build the metric name.
    known_netstatstypes = OrderedDict([
        ("TcpExt:", "tcp"),
        ("IpExt:", "ip"),  # We don't collect anything from here for now.
        ("Ip:", "ip"),  # We don't collect anything from here for now.
        ("Icmp:", "icmp"),  # We don't collect anything from here for now.
        ("IcmpMsg:", "icmpmsg"),  # We don't collect anything from here for now.
        ("Tcp:", "tcp"),  # We don't collect anything from here for now.
        ("Udp:", "udp"),
        ("UdpLite:", "udplite"),  # We don't collect anything from here for now.
        ("Arista:", "arista"),  # We don't collect anything from here for now.
    ])

    # Any stat in /proc/net/netstat that doesn't appear in this dict will be
    # ignored.  If we find a match, we'll use the (metricname, tags).
    known_netstats = OrderedDict([
        # An application wasn't able to accept a connection fast enough, so
        # the kernel couldn't store an entry in the queue for this connection.
        # Instead of dropping it, it sent a cookie to the client.
        ("SyncookiesSent", ("syncookies", "type=sent")),
        # After sending a cookie, it came back to us and passed the check.
        ("SyncookiesRecv", ("syncookies", "type=received")),
        # After sending a cookie, it came back to us but looked invalid.
        ("SyncookiesFailed", ("syncookies", "type=failed")),
        # When a socket is using too much memory (rmem), the kernel will first
        # discard any out-of-order packet that has been queued (with SACK).
        ("OfoPruned", ("memory.prune", "type=drop_ofo_queue")),
        # If the kernel is really really desperate and cannot give more memory
        # to this socket even after dropping the ofo queue, it will simply
        # discard the packet it received.  This is Really Bad.
        ("RcvPruned", ("memory.prune", "type=drop_received")),
        # We waited for another packet to send an ACK, but didn't see any, so
        # a timer ended up sending a delayed ACK.
        ("DelayedACKs", ("delayedack", "type=sent")),
        # We wanted to send a delayed ACK but failed because the socket was
        # locked.  So the timer was reset.
        ("DelayedACKLocked", ("delayedack", "type=locked")),
        # We sent a delayed and duplicated ACK because the remote peer
        # retransmitted a packet, thinking that it didn't get to us.
        ("DelayedACKLost", ("delayedack", "type=lost")),
        # We completed a 3WHS but couldn't put the socket on the accept queue,
        # so we had to discard the connection.
        ("ListenOverflows", ("failed_accept", "reason=full_acceptq")),
        # We couldn't accept a connection because one of: we had no route to
        # the destination, we failed to allocate a socket, we failed to
        # allocate a new local port bind bucket.  Note: this counter
        # also include all the increments made to ListenOverflows...
        ("ListenDrops", ("failed_accept", "reason=other")),
        # A packet was lost and we recovered after a fast retransmit.
        ("TCPRenoRecovery", ("packetloss.recovery", "type=fast_retransmit")),
        # A packet was lost and we recovered by using selective
        # acknowledgements.
        ("TCPSackRecovery", ("packetloss.recovery", "type=sack")),
        # We detected re-ordering using FACK (Forward ACK -- the highest
        # sequence number known to have been received by the peer when using
        # SACK -- FACK is used during congestion control).
        ("TCPFACKReorder", ("reording", "detectedby=fack")),
        # We detected re-ordering using SACK.
        ("TCPSACKReorder", ("reording", "detectedby=sack")),
        # We detected re-ordering using fast retransmit.
        ("TCPRenoReorder", ("reording", "detectedby=fast_retransmit")),
        # We detected re-ordering using the timestamp option.
        ("TCPTSReorder", ("reording", "detectedby=timestamp")),
        # We detected some erroneous retransmits and undid our CWND reduction.
        ("TCPFullUndo", ("congestion.recovery", "type=full_undo")),
        # We detected some erroneous retransmits, a partial ACK arrived while
        # we were fast retransmitting, so we were able to partially undo some
        # of our CWND reduction.
        ("TCPPartialUndo", ("congestion.recovery", "type=hoe_heuristic")),
        # We detected some erroneous retransmits, a D-SACK arrived and ACK'ed
        # all the retransmitted data, so we undid our CWND reduction.
        ("TCPDSACKUndo", ("congestion.recovery", "type=sack")),
        # We detected some erroneous retransmits, a partial ACK arrived, so we
        # undid our CWND reduction.
        ("TCPLossUndo", ("congestion.recovery", "type=ack")),
        # We received an unexpected SYN so we sent a RST to the peer.
        ("TCPAbortOnSyn", ("abort", "type=unexpected_syn")),
        # We were in FIN_WAIT1 yet we received a data packet with a sequence
        # number that's beyond the last one for this connection, so we RST'ed.
        ("TCPAbortOnData", ("abort", "type=data_after_fin_wait1")),
        # We received data but the user has closed the socket, so we have no
        # wait of handing it to them, so we RST'ed.
        ("TCPAbortOnClose", ("abort", "type=data_after_close")),
        # This is Really Bad.  It happens when there are too many orphaned
        # sockets (not attached a FD) and the kernel has to drop a connection.
        # Sometimes it will send a reset to the peer, sometimes it wont.
        ("TCPAbortOnMemory", ("abort", "type=out_of_memory")),
        # The connection timed out really hard.
        ("TCPAbortOnTimeout", ("abort", "type=timeout")),
        # We killed a socket that was closed by the application and lingered
        # around for long enough.
        ("TCPAbortOnLinger", ("abort", "type=linger")),
        # We tried to send a reset, probably during one of teh TCPABort*
        # situations above, but we failed e.g. because we couldn't allocate
        # enough memory (very bad).
        ("TCPAbortFailed", ("abort.failed", None)),
        # Number of times a socket was put in "memory pressure" due to a non
        # fatal memory allocation failure (reduces the send buffer size etc).
        ("TCPMemoryPressures", ("memory.pressure", None)),
        # We got a completely invalid SACK block and discarded it.
        ("TCPSACKDiscard", ("invalid_sack", "type=invalid")),
        # We got a duplicate SACK while retransmitting so we discarded it.
        ("TCPDSACKIgnoredOld", ("invalid_sack", "type=retransmit")),
        # We got a duplicate SACK and discarded it.
        ("TCPDSACKIgnoredNoUndo", ("invalid_sack", "type=olddup")),
        # We received something but had to drop it because the socket's
        # receive queue was full.
        ("TCPBacklogDrop", ("receive.queue.full", None)),
        # The number of TCP segments sent containing the RST flag
        ("OutRsts", ("resets", "direction=out")),
        # The total number of segments received in error (for example, bad TCP checksums).
        ("InErrs", ("errors", "direction=in")),
    ])

    def print_netstat(statstype, metric, value, tags="", output_file_success=None):
        output_file_success = output_file_success or sys.stdout

        if tags:
            space = " "
        else:
            tags = space = ""
        print("net.stat.%s.%s %d %s%s%s" % (statstype, metric, ts, value, space, tags),
              file=output_file_success)

    statsdikt = OrderedDict()

    ts = int(time.time())

    f_sockstat.seek(0)
    f_netstat.seek(0)

    data = f_sockstat.read()
    stats = f_netstat.read()

    m = re.match(REGEXP, data)
    if not m:
        print("Cannot parse sockstat: %r" % data, file=output_file_error)
        return 13

    # The difference between the first two values is the number of
    # sockets allocated vs the number of sockets actually in use.
    print_sockstat("num_sockets", m.group("tcp_sockets"), " type=tcp", output_file_success=output_file_success)
    print_sockstat("num_timewait", m.group("tw_count"), output_file_success=output_file_success)
    print_sockstat("sockets_inuse", m.group("tcp_inuse"), " type=tcp", output_file_success=output_file_success)
    print_sockstat("sockets_inuse", m.group("udp_inuse"), " type=udp", output_file_success=output_file_success)
    print_sockstat("sockets_inuse", m.group("udplite_inuse"), " type=udplite", output_file_success=output_file_success)
    print_sockstat("sockets_inuse", m.group("raw_inuse"), " type=raw", output_file_success=output_file_success)

    print_sockstat("num_orphans", m.group("orphans"), output_file_success=output_file_success)
    print_sockstat("memory", int(m.group("tcp_pages")) * page_size, " type=tcp", output_file_success=output_file_success)
    if m.group("udp_pages") is not None:
        print_sockstat("memory", int(m.group("udp_pages")) * page_size, " type=udp", output_file_success=output_file_success)
    print_sockstat("memory", m.group("ip_frag_mem"), " type=ipfrag")
    print_sockstat("ipfragqueues", m.group("ip_frag_nqueues"), output_file_success=output_file_success)

    # /proc/net/netstat has a retarded column-oriented format.  It looks
    # like this:
    #   Header: SomeMetric OtherMetric
    #   Header: 1 2
    #   OtherHeader: ThirdMetric FooBar
    #   OtherHeader: 42 51
    # We first group all the lines for each header together:
    #   {"Header:": [["SomeMetric", "OtherHeader"], ["1", "2"]],
    #    "OtherHeader:": [["ThirdMetric", "FooBar"], ["42", "51"]]}
    # Then we'll create a dict for each type:
    #   {"SomeMetric": "1", "OtherHeader": "2"}
    for line in stats.splitlines():
        line = line.split()

        if line[0] == "MPTcpExt:":
            # Ignore metrics we don't support
            continue

        if line[0] not in known_netstatstypes:
            print(
                "Unrecoginized line in /proc/net/netstat: %r (file=%r)"
                % (line, stats),
                file=output_file_error,
            )
            continue
        statstype = line.pop(0)
        statsdikt.setdefault(known_netstatstypes[statstype], []).append(line)
    for statstype, stats in statsdikt.items():
        # stats is now:
        # [["SyncookiesSent", "SyncookiesRecv", ...], ["1", "2", ....]]
        assert len(stats) == 2, repr(statsdikt)
        stats = dict(list(zip(*stats)))
        value = stats.get("ListenDrops")
        if value is not None:  # Undo the kernel's double counting
            stats["ListenDrops"] = int(value) - int(stats.get("ListenOverflows", 0))
        for stat, (metric, tags) in known_netstats.items():
            value = stats.get(stat)
            if value is not None:
                print_netstat(statstype, metric, value, tags, output_file_success=output_file_success)

    stats.clear()
    statsdikt.clear()


def main():
    """Main loop"""
    drop_privileges()
    sys.stdin.close()

    interval = COLLECTION_INTERVAL

    try:
        f_sockstat = open("/proc/net/sockstat", encoding="utf-8")
        f_netstat = open("/proc/net/netstat", encoding="utf-8")
    except IOError as e:
        print("Failed to open /proc/net/sockstat: %s" % e, file=sys.stderr)
        return 13  # Ask tcollector to not re-start us.

    while True:
        # Scalyr edit to add in check for parent.  A ppid of 1 means our parent has died.
        if os.getppid() == 1:
            sys.exit(1)

        parse_and_print_metrics(f_netstat=f_netstat, f_sockstat=f_sockstat)

        sys.stdout.flush()
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
