import socket
import threading
from io import StringIO
from _pytest.terminal import TerminalReporter


class ReportHandler:
    def __init__(self) -> None:
        self.stream = StringIO()

    def build_retry_report(self, terminalreporter: TerminalReporter) -> None:
        pass

    def record_attempt(self, lines: list[str]) -> None:
        pass


class OfflineReporter(ReportHandler):
    def __init__(self) -> None:
        super().__init__()

    def record_attempt(self, lines: list[str]) -> None:
        self.stream.writelines(lines)


class ReportServer(ReportHandler):
    def __init__(self) -> None:
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(True)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def initialize_server(self) -> int:
        self.sock.bind(("localhost", 0))
        t = threading.Thread(target=self.run_server, daemon=True)
        t.start()
        return self.sock.getsockname()[-1]

    def run_server(self) -> None:
        self.sock.listen()
        while True:
            conn, _ = self.sock.accept()

            while True:
                chunk = conn.recv(128)
                if not chunk:
                    break
                self.stream.write(chunk.decode("utf-8"))


class ClientReporter(ReportHandler):
    def __init__(self, port: int) -> None:
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(True)
        self.sock.connect(("localhost", port))

    def record_attempt(self, lines: list[str]) -> None:
        self.stream.writelines(lines)
        # Group reports for each item together before sending and resetting stream
        if not lines[1].endswith("Retrying!\n\t"):
            self.sock.sendall(self.stream.getvalue().encode("utf-8"))
            self.stream = StringIO()
