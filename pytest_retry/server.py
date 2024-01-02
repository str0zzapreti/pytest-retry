import socket
import threading
from io import StringIO
from _pytest.terminal import TerminalReporter

# check if xdist installed (set min version to 1.20, don't worry about older compat)
# Set xdist hooks if installed?
# Look at pytest_configure_node
# Set up two handlers, one for server and one for clients
# server handler probably needs threading
# Set up sockets with send and recieve. Might want to look at some docs on this
# assign handler to rerun process based on server or client
# When setting reports, client should send via socket while server should just set normally
# server should also be set up to receive (via threading) and should log those entries as well


# pieces:

CONN_PORT = 0


class ReportHandler:
    def __init__(self):
        self.stream = StringIO()

    def generate_report(self, terminalreporter: TerminalReporter) -> None:
        contents = self.stream.getvalue()
        if not contents:
            return

        terminalreporter.write("\n")
        terminalreporter.section(
            "the following tests were retried", sep="=", bold=True, yellow=True
        )
        terminalreporter.write(contents)
        terminalreporter.section("end of test retry report", sep="=", bold=True, yellow=True)
        terminalreporter.write("\n")


class ReportServer:
    def __init__(self):
        self.stream = StringIO()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setblocking(True)
        self.sock.bind(("localhost", CONN_PORT))
        self.sock.listen()
        while True:
            conn, _ = self.sock.accept()
            while True:
                report_chunk = conn.recv(1024)
                if not report_chunk:
                    break
                self.stream.write(report_chunk.decode())

    def generate_report(self, terminalreporter: TerminalReporter) -> None:
        terminalreporter.write("\n")
        terminalreporter.section(
            "the following tests were retried", sep="=", bold=True, yellow=True
        )
        terminalreporter.write(self.stream.getvalue())
        terminalreporter.section("end of test retry report", sep="=", bold=True, yellow=True)
        terminalreporter.write("\n")


class ReportClient:
    def __init__(self):
        self.stream = StringIO()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(True)

    def generate_report(self, _) -> None:
        self.sock.sendall(self.stream)
