#!/usr/bin/env python
"""Carry SNMP (UDP) through an SSH tunnel: a TCP-to-UDP relay for ONE agent.

``ssh -L`` forwards TCP only, and the site PDUs speak SNMP over UDP 161
(docs/remote.md). Run this on a host on the site network (the MINERVA
gateway: Python 2.7, stdlib only), forward its port with ``ssh -L``, and set
``"relay": true`` in the PDU server's config (docs/recovery.md).

Each TCP connection gets its own UDP socket, connected to ``--target`` and
nowhere else. In both directions a frame is a 2-byte big-endian length and
one datagram. The relay listens on loopback only and never looks inside the
datagrams (communities pass through and are never logged).

    python snmp_relay.py --listen 127.0.0.1:7237 --target 192.168.1.37:161

Runs on Python 2.7 and 3.
"""

from __future__ import print_function

import argparse
import socket
import struct
import sys
import threading
import time

MAX_DATAGRAM = 65507


def _hostport(s):
    host, _, port = s.rpartition(":")
    return host, int(port)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _log(msg):
    sys.stderr.write("%s snmp_relay: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    sys.stderr.flush()


def _serve(conn, peer, target):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.connect(target)
    lock = threading.Lock()
    done = threading.Event()

    def replies():
        udp.settimeout(1.0)
        while not done.is_set():
            try:
                data = udp.recv(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except socket.error:
                break
            try:
                with lock:
                    conn.sendall(struct.pack(">H", len(data)) + data)
            except socket.error:
                break
        done.set()

    t = threading.Thread(target=replies)
    t.daemon = True
    t.start()
    n = 0
    try:
        while not done.is_set():
            head = _recv_exact(conn, 2)
            if head is None:
                break
            (size,) = struct.unpack(">H", head)
            if not 0 < size <= MAX_DATAGRAM:
                _log("%s: bad frame length %d; closing" % (peer, size))
                break
            data = _recv_exact(conn, size)
            if data is None:
                break
            udp.send(data)
            n += 1
    except socket.error as exc:
        _log("%s: %s" % (peer, exc))
    finally:
        done.set()
        udp.close()
        conn.close()
        _log("%s: closed after %d datagram(s)" % (peer, n))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", required=True, help="host:port on loopback")
    ap.add_argument("--target", required=True, help="the one SNMP agent, host:port")
    args = ap.parse_args(argv)
    lhost, lport = _hostport(args.listen)
    if not (lhost.startswith("127.") or lhost == "localhost"):
        ap.error("--listen must be a loopback address (reach it through ssh -L)")
    thost, tport = _hostport(args.target)
    target = (socket.gethostbyname(thost), tport)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lhost, lport))
    srv.listen(8)
    # The bound port first (a test passes port 0), then the rest on stderr.
    print("%s:%d" % srv.getsockname(), file=sys.stdout)
    sys.stdout.flush()
    _log("listening on %s:%d, relaying to %s:%d" % (srv.getsockname() + target))
    try:
        while True:
            conn, peer = srv.accept()
            peer = "%s:%d" % peer[:2]
            t = threading.Thread(target=_serve, args=(conn, peer, target))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == "__main__":
    main()
