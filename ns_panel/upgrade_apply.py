"""容器外的升级收尾：停掉旧容器，用同一套配置换上新镜像。"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ns_panel.upgrade import DockerClient, apply_recreate, clear_children, publish_tree


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="")
    parser.add_argument("--image", required=True)
    parser.add_argument("--self", dest="self_id", required=True)
    parser.add_argument("--delay", type=int, default=3)
    parser.add_argument("--compose", default="")
    parser.add_argument("--publish", default="")
    args = parser.parse_args()
    time.sleep(max(0, args.delay))
    if args.publish and args.compose:
        try:
            publish_tree(Path(args.publish), Path(args.compose), args.image)
            clear_children(Path(args.publish))
        except OSError as exc:
            print(f"没能写回编排目录：{exc}", flush=True)
    names = apply_recreate(DockerClient(), args.image, args.self_id, args.project)
    print("已换上新版本：" + "、".join(names))


if __name__ == "__main__":
    main()
