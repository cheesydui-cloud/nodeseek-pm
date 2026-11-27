"""从 GitHub 发布页升级当前这套容器，不改端口、密码和数据。"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from ns_panel import __version__

REPO = "cheesydui-cloud/nodeseek-pm"
SOCKET = os.environ.get("NS_DOCKER_SOCK") or "/var/run/docker.sock"
SOURCE_NAMES = ("Dockerfile", "app.py", "requirements.txt", "docker", "ns_panel", "tests")
_TAG = re.compile(r"^v\d+(?:\.\d+){1,3}$")
_IMAGE_LINE = re.compile(r"""^(\s*image:\s*["']?)nodeseek-pm:[^\s"']+(["']?)\s*$""", re.M)
_HOST_KEYS = (
    "Binds",
    "PortBindings",
    "RestartPolicy",
    "ShmSize",
    "SecurityOpt",
    "Init",
    "NetworkMode",
    "Privileged",
    "CapAdd",
    "CapDrop",
    "ExtraHosts",
    "Dns",
    "ReadonlyRootfs",
    "Tmpfs",
    "Devices",
    "NanoCpus",
    "Memory",
    "MemorySwap",
    "PidsLimit",
    "Sysctls",
    "CgroupnsMode",
)


def parse_version(text: str) -> tuple[int, ...]:
    raw = (text or "").strip().lstrip("vV")
    parts: list[int] = []
    for piece in raw.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    left = parse_version(latest)
    right = parse_version(current)
    if not left or not right:
        return False
    return left > right


def valid_tag(tag: str) -> bool:
    return bool(_TAG.fullmatch(tag or ""))


def bump_compose_image(text: str, image: str) -> str:
    if not image.startswith("nodeseek-pm:"):
        return text
    return _IMAGE_LINE.sub(lambda match: f"{match.group(1)}{image}{match.group(2)}", text)


def socket_path() -> str:
    return os.environ.get("NS_DOCKER_SOCK") or SOCKET


def source_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in SOURCE_NAMES:
        path = root / name
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(
                item
                for item in path.rglob("*")
                if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
            )
    return found


def replacement_spec(info: dict, new_image: str) -> dict:
    config = info.get("Config") or {}
    host_raw = info.get("HostConfig") or {}
    host = {key: host_raw[key] for key in _HOST_KEYS if key in host_raw and host_raw[key] is not None}
    body = {
        "Image": new_image,
        "Env": list(config.get("Env") or []),
        "Cmd": config.get("Cmd"),
        "Entrypoint": config.get("Entrypoint"),
        "Labels": dict(config.get("Labels") or {}),
        "WorkingDir": config.get("WorkingDir") or "",
        "User": config.get("User") or "",
        "ExposedPorts": config.get("ExposedPorts") or {},
        "HostConfig": host,
    }
    endpoints = {}
    self_id = str(info.get("Id") or "")
    short_id = self_id[:12]
    for name, net in ((info.get("NetworkSettings") or {}).get("Networks") or {}).items():
        aliases = []
        for alias in net.get("Aliases") or []:
            if alias and alias not in (self_id, short_id):
                aliases.append(alias)
        endpoints[name] = {"Aliases": aliases} if aliases else {}
    if endpoints:
        body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return body


def socket_ready() -> bool:
    return os.path.exists(socket_path())


def ready_hint() -> str:
    if os.environ.get("NS_RUNTIME") != "docker":
        return "本机面板请在项目目录更新代码后重启。这个按钮只升级容器里的那一套。"
    return (
        "还不能从面板升级。给这个容器加上 /var/run/docker.sock:/var/run/docker.sock、"
        "./:/compose，以及环境变量 NS_COMPOSE_DIR=/compose，然后重建一次。"
        "不要改端口和密码。加上之后，以后点这里就能升级。"
    )


def fetch_latest() -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"User-Agent": "nodeseek-pm", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tag = str(payload.get("tag_name") or "")
    if not valid_tag(tag):
        raise ValueError("发布版本号无法识别")
    return {"tag": tag, "version": tag.lstrip("vV"), "url": payload.get("html_url") or ""}


def download_release(tag: str, dest: Path) -> Path:
    if not valid_tag(tag):
        raise ValueError("发布版本号无法识别")
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "release.tar.gz"
    url = f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "nodeseek-pm"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:
        raise ValueError("没能下载新版本") from exc
    with tarfile.open(archive, "r:gz") as packed:
        packed.extractall(dest, filter="data")
    roots = [item for item in dest.iterdir() if item.is_dir()]
    if len(roots) != 1:
        raise ValueError("新版本压缩包不完整")
    return roots[0]


def context_tar(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as packed:
        for path in source_files(root):
            packed.add(path, arcname=path.relative_to(root).as_posix())
    return buffer.getvalue()


def sync_source(root: Path, compose_dir: Path) -> None:
    for path in source_files(root):
        relative = path.relative_to(root)
        if relative.parts[0] == "docker-compose.yml":
            continue
        target = compose_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def compose_dir() -> Path | None:
    raw = (os.environ.get("NS_COMPOSE_DIR") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    return path


class DockerClient:
    def __init__(self, sock: str | None = None):
        self.sock = sock or socket_path()

    def available(self) -> bool:
        return os.path.exists(self.sock)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> tuple[int, bytes]:
        payload = body or b""
        header_lines = [f"{method} {path} HTTP/1.1", "Host: docker"]
        extra = dict(headers or {})
        if payload:
            extra.setdefault("Content-Type", "application/json")
        extra["Content-Length"] = str(len(payload))
        extra["Connection"] = "close"
        for key, value in extra.items():
            header_lines.append(f"{key}: {value}")
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(timeout)
        try:
            raw.connect(self.sock)
            raw.sendall(("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + payload)
            status, data = _read_http(raw)
        finally:
            raw.close()
        if status >= 400:
            detail = data.decode("utf-8", "replace")[:300]
            raise ValueError(f"Docker 返回 {status}：{detail}")
        return status, data

    def inspect(self, container: str) -> dict:
        _status, data = self.request("GET", f"/v1.41/containers/{container}/json")
        return json.loads(data.decode("utf-8"))

    def list_project(self, project: str) -> list[dict]:
        filters = json.dumps({"label": [f"com.docker.compose.project={project}"]})
        _status, data = self.request("GET", "/v1.41/containers/json?all=1&filters=" + urllib.request.quote(filters))
        return json.loads(data.decode("utf-8"))

    def build(self, context: bytes, tag: str) -> None:
        _status, data = self.request(
            "POST",
            "/v1.41/build?t=" + urllib.request.quote(tag) + "&rm=1&forcerm=1&dockerfile=Dockerfile",
            body=context,
            headers={"Content-Type": "application/x-tar"},
            timeout=900,
        )
        error = _build_error(data)
        if error:
            raise ValueError(error)

    def stop(self, container: str) -> None:
        self.request("POST", f"/v1.41/containers/{container}/stop?t=20", timeout=40)

    def rename(self, container: str, name: str) -> None:
        self.request("POST", f"/v1.41/containers/{container}/rename?name=" + urllib.request.quote(name))

    def create(self, name: str, spec: dict) -> str:
        _status, data = self.request(
            "POST",
            "/v1.41/containers/create?name=" + urllib.request.quote(name),
            body=json.dumps(spec).encode("utf-8"),
        )
        payload = json.loads(data.decode("utf-8"))
        created = str(payload.get("Id") or "")
        if not created:
            raise ValueError("没有创建出新容器")
        return created

    def start(self, container: str) -> None:
        self.request("POST", f"/v1.41/containers/{container}/start")

    def remove(self, container: str) -> None:
        self.request("DELETE", f"/v1.41/containers/{container}?force=1")

    def spawn_helper(self, image_id: str, project: str, new_image: str, self_id: str) -> str:
        spec = {
            "Image": image_id,
            "User": "0:0",
            "WorkingDir": "/app",
            "Entrypoint": ["python", "-m", "ns_panel.upgrade_apply"],
            "Cmd": ["--project", project, "--image", new_image, "--self", self_id, "--delay", "3"],
            "Env": ["NS_UPGRADE_APPLY=1"],
            "HostConfig": {
                "Binds": [f"{self.sock}:{self.sock}"],
                "AutoRemove": True,
                "NetworkMode": "none",
                "RestartPolicy": {"Name": "no"},
            },
        }
        name = "nodeseek-upgrade-" + self_id[:12]
        try:
            self.remove(name)
        except ValueError:
            pass
        created = self.create(name, spec)
        self.start(created)
        return created


def apply_recreate(docker: DockerClient, new_image: str, self_id: str, project: str = "") -> list[str]:
    own = docker.inspect(self_id)
    if project:
        listed = docker.list_project(project)
    else:
        listed = [{"Id": own.get("Id") or self_id, "Image": own.get("Config", {}).get("Image", ""), "Names": [own.get("Name") or ""]}]
    image_name = (own.get("Config") or {}).get("Image") or ""
    image_id = own.get("Image") or ""
    chosen = []
    for item in listed:
        name = _container_name(item)
        if name.startswith("nodeseek-upgrade-"):
            continue
        same = item.get("Image") in (image_name, image_id) or item.get("ImageID") == image_id
        if same or item.get("Id") == own.get("Id"):
            chosen.append(item)
    if not chosen:
        chosen = [own]
    details = []
    for item in chosen:
        info = item if "Config" in item else docker.inspect(item.get("Id") or self_id)
        details.append(info)
    details.sort(key=lambda item: (1 if _is_watch(item) else 0, _container_name(item)))
    renamed: list[tuple[str, str, str]] = []
    created_ids: list[str] = []
    try:
        for info in reversed(details):
            docker.stop(info["Id"])
        for info in details:
            old_name = _container_name(info)
            backup = old_name + "-old"
            try:
                docker.remove(backup)
            except ValueError:
                pass
            docker.rename(info["Id"], backup)
            renamed.append((info["Id"], old_name, backup))
            created = docker.create(old_name, replacement_spec(info, new_image))
            created_ids.append(created)
            docker.start(created)
        for old_id, _old_name, _backup in renamed:
            docker.remove(old_id)
    except Exception:
        for created in reversed(created_ids):
            try:
                docker.remove(created)
            except Exception:
                continue
        for old_id, old_name, _backup in reversed(renamed):
            try:
                docker.rename(old_id, old_name)
                docker.start(old_id)
            except Exception:
                continue
        raise
    return [_container_name(item) for item in details]


def own_container_ref() -> str:
    host = os.environ.get("HOSTNAME") or socket.gethostname()
    found = container_id_from_text(_cgroup_text())
    return found or host


def container_id_from_text(text: str) -> str:
    match = re.search(r"(?:docker[-/]|/containers/)([0-9a-f]{12,64})", text or "")
    if match:
        return match.group(1)
    match = re.search(r"\b([0-9a-f]{64})\b", text or "")
    return match.group(1) if match else ""


def _cgroup_text() -> str:
    chunks = []
    for path in ("/proc/self/mountinfo", "/proc/1/cgroup", "/proc/self/cgroup"):
        try:
            chunks.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _is_watch(info: dict) -> bool:
    labels = (info.get("Config") or {}).get("Labels") or info.get("Labels") or {}
    service = labels.get("com.docker.compose.service") or ""
    return "watch" in service or "watch" in _container_name(info)


def _container_name(info: dict) -> str:
    if info.get("Name"):
        return str(info["Name"]).lstrip("/")
    names = info.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return str(info.get("Id") or "")[:12]


def _build_error(payload: bytes) -> str:
    errors = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("error"):
            errors.append(str(item["error"]))
    return "\n".join(errors)


def _read_http(raw: socket.socket) -> tuple[int, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = raw.recv(4096)
        if not chunk:
            break
        data += chunk
    if b"\r\n\r\n" not in data:
        raise ValueError("Docker 没有返回完整结果")
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError("Docker 返回无法识别") from exc
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _read_chunked(rest, raw)
    elif "content-length" in headers:
        need = int(headers["content-length"])
        while len(rest) < need:
            chunk = raw.recv(65536)
            if not chunk:
                break
            rest += chunk
        body = rest[:need]
    else:
        while True:
            chunk = raw.recv(65536)
            if not chunk:
                break
            rest += chunk
        body = rest
    return status, body


def _read_chunked(initial: bytes, raw: socket.socket) -> bytes:
    buf = initial
    body = b""
    while True:
        while b"\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                return body
            buf += chunk
        line, buf = buf.split(b"\r\n", 1)
        size = int(line.split(b";", 1)[0] or b"0", 16)
        if size == 0:
            return body
        while len(buf) < size + 2:
            chunk = raw.recv(65536)
            if not chunk:
                return body
            buf += chunk
        body += buf[:size]
        buf = buf[size + 2 :]


class UpgradeService:
    def __init__(self, log=None, docker: DockerClient | None = None):
        self.log = log or (lambda _level, _text: None)
        self.docker = docker or DockerClient()
        self.lock = threading.Lock()
        self.state = "idle"
        self.message = ""
        self.latest: dict = {}

    def status(self, fetch=None) -> dict:
        lookup = fetch or fetch_latest
        current = __version__
        with self.lock:
            state = self.state
            busy = state in ("running", "restarting")
            cached = dict(self.latest)
        latest = cached.get("version") or ""
        error = ""
        if not busy:
            try:
                found = lookup()
                with self.lock:
                    self.latest = found
                latest = found["version"]
            except Exception:
                error = "暂时查不到新版本"
                latest = cached.get("version") or ""
        ready = self.docker.available() and os.environ.get("NS_RUNTIME") == "docker"
        update = bool(latest) and is_newer(latest, current)
        with self.lock:
            message = self.message
        if not message:
            if not ready:
                message = ready_hint()
            elif error and not latest:
                message = error
            elif update:
                message = f"发现新版本 {latest}。升级会重启一两分钟，不改端口、密码和登录数据。"
            else:
                message = "当前就是最新版本。"
        return {
            "ok": True,
            "current": current,
            "latest": latest,
            "update": update,
            "ready": ready,
            "state": state,
            "message": message,
            "error": "" if latest else error,
        }

    def start(self) -> tuple[dict, int]:
        if os.environ.get("NS_RUNTIME") != "docker" or not self.docker.available():
            return {"ok": False, "error": ready_hint()}, 400
        with self.lock:
            if self.state in ("running", "restarting"):
                return {"ok": False, "error": "正在升级"}, 409
            self.state = "running"
            self.message = "正在查看新版本"
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return {"ok": True, "message": "升级已开始，面板会重启一两分钟。"}, 200

    def _set(self, state: str, message: str) -> None:
        with self.lock:
            self.state = state
            self.message = message

    def _run(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="ns-upgrade-"))
        try:
            found = fetch_latest()
            self.latest = found
            if not is_newer(found["version"], __version__):
                self._set("idle", "当前就是最新版本。")
                return
            image = "nodeseek-pm:" + found["version"]
            self._set("running", f"正在下载 {found['version']}")
            self.log("info", f"开始升级到 {found['version']}")
            root = download_release(found["tag"], work)
            self._set("running", "正在构建镜像，可能要几分钟")
            self.docker.build(context_tar(root), image)
            target = compose_dir()
            if target is not None:
                sync_source(root, target)
                compose_file = target / "docker-compose.yml"
                if compose_file.is_file():
                    compose_file.write_text(
                        bump_compose_image(compose_file.read_text(encoding="utf-8"), image),
                        encoding="utf-8",
                    )
            self_id = own_container_ref()
            own = self.docker.inspect(self_id)
            project = ((own.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project") or ""
            image_id = own.get("Image") or image
            self._set("restarting", "正在重启容器，请等一两分钟后刷新")
            self.docker.spawn_helper(image_id, project, image, own.get("Id") or self_id)
        except Exception as exc:
            self._set("error", f"升级失败：{exc}")
            self.log("error", f"升级失败：{exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
