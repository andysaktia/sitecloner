"""Jalankan SiteCloner: python run.py"""
import os
import socket
import threading
import webbrowser

import uvicorn

from app.main import app


def free_port(start, host="127.0.0.1"):
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:  # tidak ada yang mendengarkan
                return port
    raise RuntimeError("Tidak ada port kosong di rentang yang dicoba")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = free_port(int(os.environ.get("PORT", 8000)))
    url = f"http://{host}:{port}"
    print(f"SiteCloner berjalan di {url}  (Ctrl+C untuk berhenti)")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")