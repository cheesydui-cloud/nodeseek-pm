"""容器外的升级收尾：停掉旧容器，用同一套配置换上新镜像。"""

from __future__ import annotations

import argparse
import time

from ns_panel.upgrade import DockerClient, apply_recreate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="")
    parser.add_argument("--image", required=True)
    parser.add_argument("--self", dest="self_id", required=True)
    parser.add_argument("--delay", type=int, default=3)
    args = parser.parse_args()
    time.sleep(max(0, args.delay))
    names = apply_recreate(DockerClient(), args.image, args.self_id, args.project)
    print("已换上新版本：" + "、".join(names))


if __name__ == "__main__":
    main()
