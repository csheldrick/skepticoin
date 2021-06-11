
import logging
import os
import random
import selectors
import socket
import traceback
import sys
from datetime import datetime

from typing import List, Optional
from skepticoin.humans import human
from skepticoin.utils import block_filename
from skepticoin.datatypes import Block, Transaction
from skepticoin.networking.remote_peer import (
    ConnectedRemotePeer, DisconnectedRemotePeer, LISTENING_SOCKET, OUTGOING, IRRELEVANT, INCOMING
)
from skepticoin.networking.params import PORT
from skepticoin.params import DESIRED_BLOCK_TIMESPAN
from skepticoin.networking.manager import ChainManager, NetworkManager
from skepticoin.utils import calc_work
from time import time
from typing import Dict

import json

MAX_SELECTOR_SIZE_BY_PLATFORM: Dict[str, int] = {
    "win32": 64,
    "linux": 512,
}


class DiskInterface:
    """Catch-all for writing to and reading from disk, factored out to facilitate testing."""

    def save_block(self, block: Block) -> None:
        with open('chain/%s' % block_filename(block), 'wb') as f:
            f.write(block.serialize())

    def overwrite_peers(self, peers: List[ConnectedRemotePeer]) -> None:
        db = [(remote_peer.host, remote_peer.port, remote_peer.direction)
              for remote_peer in peers if remote_peer.direction == OUTGOING and remote_peer.hello_received]
        if db:
            with open("peers.json", "w") as f:
                json.dump(db, f, indent=4)
        else:
            os.remove("peers.json")

    def save_transaction_for_debugging(self, transaction: Transaction) -> None:
        with open("/tmp/%s.transaction" % human(transaction.hash()), 'wb') as f:
            f.write(transaction.serialize())


class LocalPeer:

    def __init__(self, disk_interface: DiskInterface = DiskInterface()):
        self.disk_interface = disk_interface
        self.port: Optional[
            int
        ] = None  # TODO perhaps just push this into the signature here?
        self.nonce = random.randrange(pow(2, 32))
        self.selector = selectors.DefaultSelector()
        self.network_manager = NetworkManager(self)
        self.chain_manager = ChainManager(self, int(time()))
        self.managers = [
            self.network_manager,
            self.chain_manager,
        ]

        self.logger = logging.getLogger("skepticoin.networking.%s" % self.nonce)
        self.last_stats_output: str = ""

    def start_listening(self, port: int = PORT) -> None:
        self.port = port
        self.logger.info("%15s LocalPeer.start_listening(%s)" % ("", port))
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # https://stackoverflow.com/questions/4465959/python-errno-98-address-already-in-use/4466035#4466035
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        lsock.bind(("", port))
        lsock.listen()
        lsock.setblocking(False)
        self.selector.register(lsock, selectors.EVENT_READ, data=LISTENING_SOCKET)

    def handle_incoming_connection(self, sock: socket.socket) -> None:
        self.logger.info("%15s LocalPeer.handle_incoming_connection()" % "")
        # TODO only accept a single (incoming, outgoing) connection from each peer
        conn, addr = sock.accept()
        conn.setblocking(False)
        events = selectors.EVENT_READ

        remote_host = conn.getpeername()[0]
        remote_port = conn.getpeername()[1]
        remote_peer = ConnectedRemotePeer(self, remote_host, remote_port, INCOMING, None, conn)
        self.selector.register(conn, events, data=remote_peer)
        self.network_manager.handle_peer_connected(remote_peer)

    def handle_remote_peer_selector_event(
        self, key: selectors.SelectorKey, mask: int
    ) -> None:
        # self.logger.info("LocalPeer.handle_remote_peer_selector_event()")

        sock: socket.socket = key.fileobj  # type: ignore
        remote_peer = key.data
        assert isinstance(remote_peer, ConnectedRemotePeer)

        try:
            if mask & selectors.EVENT_READ:
                recv_data = sock.recv(1024)

                if recv_data:
                    remote_peer.handle_receive_data(recv_data)
                else:
                    self.disconnect(remote_peer, "connection closed remotely")  # is this so?

            if mask & selectors.EVENT_WRITE:
                remote_peer.handle_can_send(sock)

        except OSError as e:  # e.g. ConnectionRefusedError, "Bad file descriptor"
            # no print-to-screen for this one
            self.logger.info("%15s Disconnecting remote peer %s" % (remote_peer.host, e))
            self.disconnect(remote_peer, "OS error")

        except Exception as e:
            # We take the position that any exception caused is reason to disconnect. This allows the code that talks to
            # peers to not have special cases for exceptions since they will all be caught by this catch-all.
            self.logger.info("%15s Disconnecting remote peer %s" % (remote_peer.host, e))

            if "ValueError: Invalid file descriptor: " not in str(e):
                self.logger.warning(traceback.format_exc())  # be loud... this is likely a programming error.

            self.disconnect(remote_peer, "Exception")

    def disconnect(self, remote_peer: ConnectedRemotePeer, reason: str = "") -> None:
        self.logger.info("%15s LocalPeer.disconnect(%s)" % (remote_peer.host, reason))

        try:
            self.selector.unregister(remote_peer.sock)
            remote_peer.sock.close()
            self.network_manager.handle_peer_disconnected(remote_peer)
        except Exception as e:
            # yes yes... sweeping things under the carpet here. until I actually RTFM and think this through
            # (i.e. the whole business of unregistering things that are already in some half-baked state)
            # at least one path how you might end up here: a EVENT_WRITE is reached for a socket that was just closed
            # as a consequence of something that was read.
            self.logger.info("%15s Error while disconnecting %s" % ("", e))

    def start_outgoing_connection(self, disconnected_peer: DisconnectedRemotePeer) -> None:
        self.logger.info("%15s LocalPeer.start_outgoing_connection()" % disconnected_peer.host)

        max_selector_map_size = MAX_SELECTOR_SIZE_BY_PLATFORM.get(sys.platform, 64)

        if len(self.selector.get_map()) >= max_selector_map_size:
            # We hit the platform-dependent limit of connected peers
            # TODO this is actually a hack, find a proper solution
            return

        server_addr = (disconnected_peer.host, disconnected_peer.port)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.connect_ex(server_addr)
        events = selectors.EVENT_READ

        remote_peer = disconnected_peer.as_connected(self, sock)
        self.selector.register(sock, events, data=remote_peer)
        self.network_manager.handle_peer_connected(remote_peer)

    def step_managers(self, current_time: int) -> None:
        for manager in self.managers:
            if not self.running:
                break

            manager.step(current_time)

    def handle_selector_events(self) -> None:
        events = self.selector.select(timeout=1)  # TODO this is for the managers to do something... tune it though
        for key, mask in events:
            if not self.running:
                break

            if key.data is LISTENING_SOCKET:
                self.handle_incoming_connection(key.fileobj)  # type: ignore
            else:
                self.handle_remote_peer_selector_event(key, mask)

    def run(self) -> None:
        self.running = True
        try:
            while self.running:
                current_time = int(time())
                self.step_managers(current_time)
                self.handle_selector_events()
        except Exception:
            self.logger.error("Uncaught exception in LocalPeer.run()")
            self.logger.error(traceback.format_exc())
        finally:
            self.logger.info("%15s LocalPeer selector close" % "")
            self.selector.close()
            self.logger.info("%15s LocalPeer selector closed" % "")

    def stop(self) -> None:
        self.logger.info("%15s LocalPeer.stop()" % "")
        self.running = False

    def show_stats(self) -> None:
        coinstate = self.chain_manager.coinstate
        assert coinstate

        out = "NETWORK - %d connected peers: \n" % len(self.network_manager.get_active_peers())
        for p in self.network_manager.get_active_peers():
            # TODO: Fix inconsistent usage of datatypes for PORT. int or str, pick one!
            out += "  %15s:%s %s,\n" % (p.host, p.port if p.port != IRRELEVANT else "....", p.direction)  # type: ignore

        out += "CHAIN - "
        for (head, lca) in coinstate.forks():
            if head.height < coinstate.head().height - 10:
                continue  # don't show forks which are out-ran by more than 10 blocks

            out += "Height = %s, " % head.height
            out += "Date/time = %s\n" % datetime.fromtimestamp(head.timestamp).isoformat()
            if head.height != lca.height:
                out += "  diverges for %s blocks\n" % (head.height - lca.height)
            out += "\n"

        if out != self.last_stats_output:
            print(out)
            self.last_stats_output = out

    def show_network_stats(self) -> None:
        print("NETWORK")
        print("Nr. of connected peers:", len(self.network_manager.get_active_peers()))
        print("Nr. of unique hosts   :", len(set(p.host for p in self.network_manager.get_active_peers())))
        print("Nr. of listening hosts:",
              len([p for p in self.network_manager.get_active_peers() if p.direction == OUTGOING]))

        per_host = {}

        for p in self.network_manager.get_active_peers():
            if p.host not in per_host:
                per_host[p.host] = (0, 0)

            incoming, outgoing = per_host[p.host]
            if p.direction == INCOMING:
                per_host[p.host] = incoming + 1, outgoing
            else:
                per_host[p.host] = incoming, outgoing + 1

        print("\ndetails:")
        for host, (incoming, outgoing) in per_host.items():
            print("%15s: %2d incoming, %2d outgoing" % (host, incoming, outgoing))

    def show_chain_stats(self) -> None:
        coinstate = self.chain_manager.coinstate
        assert coinstate

        def get_block_timespan_factor(n: int) -> float:
            # Current block duration over past n block as a factor of DESIRED_BLOCK_TIMESPAN, e.g. 0.5 for twice desired
            # speed
            assert coinstate
            diff = coinstate.head().timestamp - coinstate.at_head.block_by_height[coinstate.head().height - n].timestamp
            return diff / (DESIRED_BLOCK_TIMESPAN * n)  # type: ignore

        def get_network_hash_rate(n: int) -> float:
            assert coinstate
            total_over_blocks = sum(
                calc_work(coinstate.at_head.block_by_height[coinstate.head().height - i].target) for i in range(n))

            diff = coinstate.head().timestamp - coinstate.at_head.block_by_height[coinstate.head().height - n].timestamp

            return total_over_blocks / diff  # type: ignore

        print("WASTELAND STATS")
        print("Current target: ", human(coinstate.head().target))
        print("Current work:   ", calc_work(coinstate.head().target))
        print("Timespan factor:", get_block_timespan_factor(100))
        print("Hash rate:      ", get_network_hash_rate(100))