"""
Zero-dependency, high-speed Pure Python MQTT 3.1.1 Micro Broker
Supports CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT
Compatible with Paho-MQTT, MQTT.js, and standard embedded clients.
"""
import socket
import threading
import sys
import time

class MicroMQTTBroker:
    def __init__(self, host="0.0.0.0", port=1883):
        self.host = host
        self.port = port
        self.running = False
        self.server_sock = None
        self.clients = []       # list of (client_sock, client_id, subscriptions)
        self.lock = threading.Lock()

    def start(self, daemon=True):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(10)
        self.running = True

        t = threading.Thread(target=self._accept_loop, daemon=daemon)
        t.start()
        print(f"[MQTT BROKER] Listening on {self.host}:{self.port}")
        return self

    def _accept_loop(self):
        while self.running:
            try:
                csock, addr = self.server_sock.accept()
                csock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._client_handler, args=(csock, addr), daemon=True).start()
            except Exception:
                break

    def _decode_rem_len(self, sock):
        multiplier = 1
        value = 0
        while True:
            b = sock.recv(1)
            if not b:
                return None
            digit = b[0]
            value += (digit & 127) * multiplier
            multiplier *= 128
            if (digit & 128) == 0:
                break
        return value

    def _encode_rem_len(self, length):
        encoded = bytearray()
        while True:
            digit = length % 128
            length //= 128
            if length > 0:
                digit |= 128
            encoded.append(digit)
            if length <= 0:
                break
        return bytes(encoded)

    def _client_handler(self, csock, addr):
        subs = []
        client_id = f"{addr[0]}:{addr[1]}"
        try:
            with self.lock:
                self.clients.append((csock, client_id, subs))

            while self.running:
                header = csock.recv(1)
                if not header:
                    break
                pkt_type = header[0] >> 4
                flags = header[0] & 0x0F
                rem_len = self._decode_rem_len(csock)
                if rem_len is None:
                    break

                body = b""
                while len(body) < rem_len:
                    chunk = csock.recv(rem_len - len(body))
                    if not chunk:
                        break
                    body += chunk

                # CONNECT -> Send CONNACK
                if pkt_type == 1:
                    connack = bytes([0x20, 0x02, 0x00, 0x00])
                    csock.sendall(connack)

                # PUBLISH -> Broadcast to subscribers
                elif pkt_type == 3:
                    topic_len = (body[0] << 8) | body[1]
                    topic = body[2:2 + topic_len].decode('utf-8', errors='ignore')
                    offset = 2 + topic_len
                    qos = (flags >> 1) & 0x03
                    if qos > 0:
                        offset += 2  # Skip packet ID
                    payload = body[offset:]
                    
                    self._broadcast(topic, payload, flags)

                # SUBSCRIBE -> Send SUBACK
                elif pkt_type == 8:
                    if len(body) >= 2:
                        pkt_id = body[:2]
                        # Parse topic filter
                        t_len = (body[2] << 8) | body[3]
                        sub_topic = body[4:4 + t_len].decode('utf-8', errors='ignore')
                        subs.append(sub_topic)
                        suback = bytes([0x90, 0x03, pkt_id[0], pkt_id[1], 0x00])
                        csock.sendall(suback)

                # PINGREQ -> Send PINGRESP
                elif pkt_type == 12:
                    csock.sendall(bytes([0xD0, 0x00]))

                # DISCONNECT
                elif pkt_type == 14:
                    break

        except Exception:
            pass
        finally:
            with self.lock:
                self.clients = [c for c in self.clients if c[0] != csock]
            try:
                csock.close()
            except Exception:
                pass

    def _broadcast(self, topic, payload, flags=0):
        # Build raw MQTT publish packet
        topic_bytes = topic.encode('utf-8')
        t_len = len(topic_bytes)
        var_header = bytes([t_len >> 8, t_len & 0xFF]) + topic_bytes
        body = var_header + payload
        rem_len_bytes = self._encode_rem_len(len(body))
        pub_pkt = bytes([0x30 | (flags & 0x0F)]) + rem_len_bytes + body

        with self.lock:
            for csock, cid, subs in self.clients:
                for s in subs:
                    # Match wildcard # or exact topic
                    if s == "#" or s == topic or (s.endswith("/#") and topic.startswith(s[:-2])):
                        try:
                            csock.sendall(pub_pkt)
                        except Exception:
                            pass
                        break

if __name__ == "__main__":
    broker = MicroMQTTBroker(port=1883)
    broker.start(daemon=False)
