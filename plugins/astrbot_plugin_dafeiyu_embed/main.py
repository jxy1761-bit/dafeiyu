"""大肥鱼本地记忆向量服务（手机端）

AstrBot 的 livingmemory 用 OpenAI 兼容的 embedding 接口。电脑上跑的是 Ollama
bge-m3（127.0.0.1:11434）。手机上用 llama.cpp 的 llama-server 提供同一个端点，
这样记忆库不用改配置、旧向量也继续可用。
"""

import asyncio
import os
import socket
import subprocess

from astrbot.api import logger
from astrbot.api.star import Context, Star

LLAMA = "/persistent/tools/llama.cpp/llama-server"
MODEL = "/persistent/models/bge-m3.gguf"
PORT = 11434
LOG = "/persistent/llama-embed.log"

_proc = None


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


class DafeiyuEmbed(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._task = None

    async def initialize(self):
        if self._task is None:
            self._task = asyncio.create_task(self._supervise())

    async def _supervise(self):
        global _proc
        while True:
            try:
                if not _port_open(PORT):
                    if _proc is None or _proc.poll() is not None:
                        if not (os.path.isfile(LLAMA) and os.path.isfile(MODEL)):
                            logger.warning("[dafeiyu-embed] 缺少 llama-server 或 bge-m3 模型，记忆向量不可用")
                        else:
                            logger.info("[dafeiyu-embed] 启动本地 bge-m3 向量服务 127.0.0.1:11434 ...")
                            _proc = subprocess.Popen(
                                [LLAMA, "-m", MODEL, "--embeddings",
                                 "--host", "127.0.0.1", "--port", str(PORT),
                                 "-c", "8192", "-b", "2048", "-ub", "1024", "-np", "2", "-t", "4", "-tb", "4", "--pooling", "cls"],
                                stdout=open(LOG, "ab"), stderr=subprocess.STDOUT,
                                start_new_session=True,
                            )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[dafeiyu-embed] 向量服务守护异常: {e}")
            await asyncio.sleep(10)

    async def terminate(self):
        if self._task:
            self._task.cancel()
