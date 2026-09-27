"""Tests for the node channel section of examples/bigfix_root_server_upgrade_win.py.

Handshakes run over real localhost sockets, on any OS.
"""

import asyncio
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys

import pytest

pytest.importorskip("cryptography")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT_PATH = os.path.join(ROOT, "examples", "bigfix_root_server_upgrade_win.py")

# the headers of a client masthead, with a made up serial:
MASTHEAD_HEADERS = (
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/signed; protocol="application/x-pkcs7-signature";'
    ' micalg="sha-256,sha-384"; boundary="----ABC"\r\n'
    "\r\n"
    "------ABC\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/related; boundary = "xyz";\r\n'
    "X-Fixlet-Site-Masthead-Version: 2\r\n"
    "X-Fixlet-Site-Name: actionsite\r\n"
    "X-Fixlet-Site-Gather-URL: http://bigfix.example.com:52311/cgi-bin/bfgather.exe/actionsite\r\n"
    "X-Fixlet-Site-Serial-Number: 123456789\r\n"
    "X-Fixlet-Site-Update-Frequency: Day\r\n"
    "\r\n"
    "--xyz\r\n"
    "Content-ID: Operator-List\r\n"
)


@pytest.fixture
def channel():
    """The script, for its node channel section."""
    spec = importlib.util.spec_from_file_location("upgrade_channel", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- masthead serial


def test_read_masthead_serial(channel, tmp_path):
    """Test the serial comes from the X-Fixlet-Site-Serial-Number header."""
    masthead = tmp_path / "ActionSite.afxm"
    masthead.write_bytes(MASTHEAD_HEADERS.encode() + os.urandom(4096))

    assert channel.read_masthead_serial(str(masthead)) == "123456789"


def test_read_masthead_serial_missing(channel, tmp_path):
    """Test a masthead without the header, or no file, gives None."""
    masthead = tmp_path / "ActionSite.afxm"
    masthead.write_text(MASTHEAD_HEADERS.replace("Serial-Number", "Something"))

    assert channel.read_masthead_serial(str(masthead)) is None
    assert channel.read_masthead_serial(str(tmp_path / "missing.afxm")) is None


# ---------------------------------------------------------------- secrets


def test_load_psk_precedence(channel, tmp_path):
    """Test the PSK comes from the env var, then a file, and is otherwise absent."""
    psk_file = tmp_path / "psk.txt"
    psk_file.write_text("from-a-file\n")

    assert channel.load_psk({"BIGFIX_UPGRADE_PSK": "from-env"}, str(psk_file)) == (
        b"from-env",
        "env",
    )
    assert channel.load_psk({}, str(psk_file)) == (b"from-a-file", "file")
    assert channel.load_psk({}, None) == (None, "none")


@pytest.mark.parametrize(
    "source,expected", [("none", True), ("env", False), ("file", False)]
)
def test_code_required(channel, source, expected):
    """Test a pairing code is needed exactly when there is no PSK."""
    assert channel.code_required(source) is expected


def test_derive_password(channel):
    """Test the PAKE password depends on the PSK, the serial and the code."""
    password = channel.derive_password(None, "123456789", "123456")

    assert len(password) == 32
    assert password == channel.derive_password(None, "123456789", "123456")
    assert password != channel.derive_password(None, "123456789", "123457")
    assert password != channel.derive_password(None, "987654321", "123456")
    assert password != channel.derive_password(b"psk", "123456789", "123456")
    assert channel.derive_password(b"psk", "1") != channel.derive_password(
        b"other", "1"
    )


def test_derive_password_needs_a_secret(channel):
    """Test there is no password from public inputs alone."""
    with pytest.raises(ValueError):
        channel.derive_password(None, "123456789")


def test_pairing_code_normalised(channel):
    """Test codes match whatever spaces or dashes are typed."""
    assert channel.derive_password(None, "1", "123 456") == channel.derive_password(
        None, "1", "123-456"
    )


def test_generate_pairing_code(channel):
    """Test pairing codes are 6 random digits."""
    codes = {channel.generate_pairing_code() for _ in range(20)}

    assert all(re.fullmatch(r"\d{6}", code) for code in codes)
    assert len(codes) > 1


@pytest.mark.parametrize(
    "ip,allow,expected",
    [
        ("192.168.5.40", [], True),
        ("192.168.5.40", ["192.168.5.40"], True),
        ("192.168.5.40", ["192.168.5.0/24"], True),
        ("10.0.0.1", ["192.168.5.0/24", "192.168.6.1"], False),
        ("::1", ["127.0.0.1"], False),
    ],
)
def test_peer_allowed(channel, ip, allow, expected):
    """Test the --allow list of addresses and networks."""
    assert channel.peer_allowed(ip, allow) is expected


def test_peer_allowed_rejects_bad_entries(channel):
    """Test a typo in the allow list is an error, not an open door."""
    with pytest.raises(ValueError):
        channel.peer_allowed("192.168.5.40", ["192.168.5.400"])


# ---------------------------------------------------------------- handshake


def hello(channel, node_id, serial="123456789"):
    return channel.make_hello(node_id, ["root"], serial)


async def handshake_pair(
    channel, server_password, client_password, server_serial, client_serial
):
    """Run a server and client handshake over localhost, return both outcomes."""
    results = {}

    async def on_connect(reader, writer):
        try:
            results["server"] = await channel.accept_channel(
                reader, writer, server_password, hello(channel, "hyperv", server_serial)
            )
        except Exception as err:  # pylint: disable=broad-exception-caught
            results["server"] = err

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        results["client"] = await channel.open_channel(
            "127.0.0.1", port, client_password, hello(channel, "root", client_serial)
        )
    except Exception as err:  # pylint: disable=broad-exception-caught
        results["client"] = err
    for _ in range(100):
        if "server" in results:
            break
        await asyncio.sleep(0.01)
    # NOTE: not wait_closed(), on 3.12+ it waits for the open channels too:
    server.close()
    return results


def test_handshake_and_messages(channel):
    """Test the same code connects, messages go both ways, keys differ per
    direction.
    """

    async def scenario():
        password = channel.derive_password(None, "123456789", "123456")
        results = await handshake_pair(
            channel, password, password, "123456789", "123456789"
        )
        (client, server_hello), (server, client_hello) = (
            results["client"],
            results["server"],
        )
        assert server_hello["node_id"] == "hyperv"
        assert client_hello["node_id"] == "root"
        await client.send({"type": "ping", "n": 1})
        assert await server.recv() == {"type": "ping", "n": 1}
        await server.send({"type": "pong"})
        assert await client.recv() == {"type": "pong"}
        assert client.send_key != client.recv_key
        assert client.send_key == server.recv_key
        client.close()
        server.close()

    asyncio.run(scenario())


def test_handshake_fresh_keys_per_connection(channel):
    """Test each connection negotiates its own keys, even with the same code."""

    async def scenario():
        password = channel.derive_password(None, "123456789", "123456")
        first = await handshake_pair(
            channel, password, password, "123456789", "123456789"
        )
        second = await handshake_pair(
            channel, password, password, "123456789", "123456789"
        )
        keys = [result["client"][0].send_key for result in (first, second)]
        for result in (first, second):
            result["client"][0].close()
            result["server"][0].close()
        return keys

    first_key, second_key = asyncio.run(scenario())
    assert first_key != second_key
    # the session key is negotiated, not derived from the code alone:
    assert channel.derive_password(None, "123456789", "123456") not in (
        first_key,
        second_key,
    )


def test_handshake_wrong_code_rejected_both_sides(channel):
    """Test a different code fails on both sides, as a wrong code."""

    async def scenario():
        results = await handshake_pair(
            channel,
            channel.derive_password(None, "123456789", "123456"),
            channel.derive_password(None, "123456789", "654321"),
            "123456789",
            "123456789",
        )
        for side in ("client", "server"):
            assert isinstance(results[side], channel.WrongCode), side
            assert "code" in str(results[side])

    asyncio.run(scenario())


def test_handshake_different_deployment(channel):
    """Test different masthead serials fail with a clear message on both sides."""

    async def scenario():
        results = await handshake_pair(
            channel,
            channel.derive_password(None, "123456789", "123456"),
            channel.derive_password(None, "987654321", "123456"),
            "123456789",
            "987654321",
        )
        for side in ("client", "server"):
            assert isinstance(results[side], channel.HandshakeError), side
            assert "different BigFix deployment" in str(results[side])

    asyncio.run(scenario())


def test_handshake_bad_pake_message(channel):
    """Test garbage instead of a SPAKE2 message is rejected, not a crash."""

    async def scenario():
        password = channel.derive_password(None, "123456789", "123456")
        results = {}

        async def on_connect(reader, writer):
            try:
                results["server"] = await channel.accept_channel(
                    reader, writer, password, hello(channel, "hyperv")
                )
            except Exception as err:  # pylint: disable=broad-exception-caught
                results["server"] = err

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(channel._frame(json.dumps(hello(channel, "root")).encode()))
        writer.write(channel._frame(b"not a spake2 message"))
        await writer.drain()
        for _ in range(200):
            if "server" in results:
                break
            await asyncio.sleep(0.01)
        writer.close()
        server.close()
        return results

    results = asyncio.run(scenario())
    assert isinstance(results["server"], channel.HandshakeError)


# ---------------------------------------------------------------- frames


def channel_pair(channel):
    """Two ends of a channel, without sockets, to test the framing itself."""
    a_to_b, b_to_a = os.urandom(32), os.urandom(32)
    a = channel.SecureChannel(None, None, a_to_b, b_to_a, "a->b", "b->a")
    b = channel.SecureChannel(None, None, b_to_a, a_to_b, "b->a", "a->b")
    return a, b


def test_frame_round_trip(channel):
    """Test a sealed frame opens on the other end."""
    a, b = channel_pair(channel)
    assert b.open(a.seal({"hello": "world"})) == {"hello": "world"}


def test_frame_tampered(channel):
    """Test a changed byte is detected."""
    a, b = channel_pair(channel)
    frame = bytearray(a.seal({"hello": "world"}))
    frame[-1] ^= 1

    with pytest.raises(channel.ChannelError, match="tampered"):
        b.open(bytes(frame))


def test_frame_replayed(channel):
    """Test the same frame can't be accepted twice."""
    a, b = channel_pair(channel)
    frame = a.seal({"n": 1})
    b.open(frame)

    with pytest.raises(channel.ChannelError, match="replayed or out of order"):
        b.open(frame)


def test_frame_out_of_order(channel):
    """Test frames must arrive in order."""
    a, b = channel_pair(channel)
    a.seal({"n": 1})
    second = a.seal({"n": 2})

    with pytest.raises(channel.ChannelError, match="replayed or out of order"):
        b.open(second)


def test_frame_wrong_direction(channel):
    """Test a frame can't be reflected back to its sender."""
    a, _b = channel_pair(channel)
    frame = a.seal({"n": 1})

    with pytest.raises(channel.ChannelError):
        a.open(frame)


# ---------------------------------------------------------------- discovery


def test_read_masthead_gather_host(channel, tmp_path):
    """Test the root server's host name comes from the masthead gather URL."""
    masthead = tmp_path / "ActionSite.afxm"
    masthead.write_bytes(MASTHEAD_HEADERS.encode())

    assert channel.read_masthead_gather_host(str(masthead)) == "bigfix.example.com"
    assert channel.read_masthead_gather_host(str(tmp_path / "missing")) is None


def test_discover_coordinator(channel):
    """Test a node finds the coordinator for its serial, not one for another."""

    async def scenario():
        responder = await channel.serve_discovery("123456789", 52390, "127.0.0.1", 0)
        udp_port = responder.get_extra_info("sockname")[1]
        try:
            found = await channel.discover_coordinator(
                "123456789", udp_port, targets=["127.0.0.1"], timeout=2
            )
            other = await channel.discover_coordinator(
                "987654321", udp_port, targets=["127.0.0.1"], timeout=0.5
            )
        finally:
            responder.close()
        return found, other

    found, other = asyncio.run(scenario())
    assert found == ("127.0.0.1", 52390)
    assert other is None


# coverage of existing behaviour, checked by mutation rather than red first:
def test_discovery_silent_for_other_serial(channel):
    """Test the coordinator doesn't even answer discovery for another deployment."""

    async def scenario():
        responder = await channel.serve_discovery("123456789", 52390, "127.0.0.1", 0)
        udp_port = responder.get_extra_info("sockname")[1]
        loop = asyncio.get_running_loop()
        replies = []

        class Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                replies.append(data)

        transport, _ = await loop.create_datagram_endpoint(
            Collector, local_addr=("127.0.0.1", 0)
        )
        for serial in ("987654321", "123456789"):
            message = {
                "protocol": channel.PROTOCOL,
                "type": "discover",
                "serial": serial,
            }
            transport.sendto(json.dumps(message).encode(), ("127.0.0.1", udp_port))
            await asyncio.sleep(0.2)
        transport.close()
        responder.close()
        return replies

    replies = asyncio.run(scenario())
    assert len(replies) == 1
    assert json.loads(replies[0])["serial"] == "123456789"


def test_handshake_tampered_hello_rejected(channel):
    """Test a man in the middle can't change a hello, like a console's roles."""

    async def scenario():
        password = channel.derive_password(None, "123456789", "123456")
        results = {}

        async def on_connect(reader, writer):
            try:
                results["server"] = await channel.accept_channel(
                    reader, writer, password, hello(channel, "hyperv")
                )
            except Exception as err:  # pylint: disable=broad-exception-caught
                results["server"] = err

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        server_port = server.sockets[0].getsockname()[1]

        async def relay(reader, writer, rewrite_first):
            first = True
            try:
                while True:
                    frame = await channel._read_frame(reader)
                    if first and rewrite_first:
                        message = json.loads(frame)
                        message["roles"] = ["peer"]
                        frame = json.dumps(message).encode()
                    first = False
                    writer.write(channel._frame(frame))
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                writer.close()

        async def on_proxy(client_reader, client_writer):
            server_reader, server_writer = await asyncio.open_connection(
                "127.0.0.1", server_port
            )
            await asyncio.gather(
                relay(client_reader, server_writer, True),
                relay(server_reader, client_writer, False),
            )

        proxy = await asyncio.start_server(on_proxy, "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        try:
            await channel.open_channel(
                "127.0.0.1",
                proxy_port,
                password,
                channel.make_hello("mac", ["console"], "123456789"),
            )
            results["client"] = "connected"
        except Exception as err:  # pylint: disable=broad-exception-caught
            results["client"] = err
        for _ in range(200):
            if "server" in results:
                break
            await asyncio.sleep(0.01)
        proxy.close()
        server.close()
        return results

    results = asyncio.run(scenario())
    assert isinstance(results["client"], channel.HandshakeError)
    assert isinstance(results["server"], channel.HandshakeError)


def test_optional_packages_are_lazy(tmp_path):
    """Test the script loads without cryptography and spake2, and says what's
    missing.
    """
    code = f"""
import importlib.abc, importlib.util, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("cryptography", "spake2"):
            raise ImportError(f"blocked {{name}}")
        return None

sys.meta_path.insert(0, Block())
importlib.util.find_spec = (
    lambda name, *a, _real=importlib.util.find_spec:
    None if name in ("cryptography", "spake2") else _real(name, *a)
)
spec = importlib.util.spec_from_file_location("upgrade", {SCRIPT_PATH!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.sql_major_from_version("10.50.2500.0") == "2008 R2"
try:
    module.require_session_packages()
except SystemExit as err:
    print("EXIT", err)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )
    assert "EXIT share sessions need cryptography and spake2" in result.stdout
    assert "pip install cryptography spake2" in result.stdout


# ---------------------------------------------------------------- resume


def test_handshake_password_chosen_from_hellos(channel):
    """Test the server picks its password from the client's hello, answers in
    its own hello, and the client picks its password from that answer.
    """
    secret = os.urandom(32)
    resume_password = channel.derive_resume_password(secret, "123456789")
    code_password = channel.derive_password(None, "123456789", "123456")
    seen = {}

    def server_password(peer):
        seen["server saw"] = peer.get("resume_id")
        return resume_password if peer.get("resume_id") == "ab" * 16 else code_password

    def server_hello(peer):
        own = hello(channel, "hyperv")
        own["resume"] = "accepted" if peer.get("resume_id") else "none"
        return own

    def client_password(peer):
        seen["client saw"] = peer.get("resume")
        return resume_password if peer.get("resume") == "accepted" else code_password

    async def scenario():
        results = {}

        async def on_connect(reader, writer):
            results["server"] = await channel.accept_channel(
                reader, writer, server_password, server_hello
            )

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client_hello = channel.make_hello(
            "root", ["root"], "123456789", resume_id="ab" * 16
        )
        client, peer = await channel.open_channel(
            "127.0.0.1", port, client_password, client_hello
        )
        for _ in range(100):
            if "server" in results:
                break
            await asyncio.sleep(0.01)
        await client.send({"type": "ping"})
        assert await results["server"][0].recv() == {"type": "ping"}
        client.close()
        results["server"][0].close()
        server.close()
        return peer

    peer = asyncio.run(scenario())
    assert peer["resume"] == "accepted"
    assert seen == {"server saw": "ab" * 16, "client saw": "accepted"}


def test_resume_password_differs_from_code_password(channel):
    """Test the resume password is bound to the serial and isn't the secret."""
    secret = os.urandom(32)

    first = channel.derive_resume_password(secret, "123456789")
    assert first != secret
    assert first != channel.derive_resume_password(secret, "987654321")
    assert first != channel.derive_resume_password(os.urandom(32), "123456789")


def test_make_hello_resume_id_only_when_given(channel):
    """Test the hello carries a resume id only when the node has one."""
    assert "resume_id" not in channel.make_hello("a", ["root"], "1")
    assert channel.make_hello("a", ["root"], "1", resume_id="cd" * 16)["resume_id"] == (
        "cd" * 16
    )
