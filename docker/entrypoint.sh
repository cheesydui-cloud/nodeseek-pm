#!/bin/sh
set -eu

reject() {
  echo "$1"
  exit 1
}

case "${NS_PANEL_PASSWORD:-}" in
  ""|change-me|改成面板密码) reject "请先在 1Panel 编排里把 NS_PANEL_PASSWORD 改成你自己的面板密码，再重建容器。" ;;
esac

case "${VNC_PASSWORD:-}" in
  ""|change12|改成桌面密码) reject "请先在 1Panel 编排里把 VNC_PASSWORD 改成网页桌面密码。只用英文字母和数字，最多 8 位。" ;;
esac

case "$VNC_PASSWORD" in
  *[!A-Za-z0-9]*) reject "VNC_PASSWORD 请只用英文字母和数字，最多 8 位。" ;;
esac

if [ "${#VNC_PASSWORD}" -gt 8 ]; then
  echo "网页桌面密码只使用前 8 位。"
  VNC_PASSWORD=$(printf "%s" "$VNC_PASSWORD" | cut -c1-8)
fi

export DISPLAY=:99
export NS_RUNTIME=docker
export NS_HOST="${NS_HOST:-0.0.0.0}"
export NS_PORT="${NS_PORT:-8787}"
export NS_OPEN_BROWSER=0
export NS_BROWSER_BIN="${NS_BROWSER_BIN:-/usr/bin/chromium}"
export NS_BROWSER_DIR="${NS_BROWSER_DIR:-/browser-data}"
export NS_BROWSER_NO_SANDBOX=1
export HOME=/home/ns

mkdir -p /app/data "$NS_BROWSER_DIR" /tmp/.X11-unix
chmod 755 /app/data "$NS_BROWSER_DIR" /tmp/.X11-unix
chown -R ns:ns /app/data "$NS_BROWSER_DIR" /home/ns
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

umask 077
printf "%s\n" "$VNC_PASSWORD" > /tmp/vnc.pass
chmod 600 /tmp/vnc.pass

Xvfb :99 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
i=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  i=$((i + 1))
  if [ "$i" -gt 50 ]; then
    echo "虚拟桌面没有启动。"
    cat /tmp/xvfb.log || true
    exit 1
  fi
  sleep 0.1
done

fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -passwdfile /tmp/vnc.pass >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &

clear_browser_lock() {
  rm -f "$NS_BROWSER_DIR/SingletonLock" "$NS_BROWSER_DIR/SingletonCookie" "$NS_BROWSER_DIR/SingletonSocket"
}

start_chrome() {
  while true; do
    clear_browser_lock
    runuser -u ns -- env HOME=/home/ns DISPLAY=:99 "$NS_BROWSER_BIN" \
      --remote-debugging-port=9337 \
      --remote-debugging-address=127.0.0.1 \
      --user-data-dir="$NS_BROWSER_DIR" \
      --remote-allow-origins=* \
      --no-first-run \
      --no-default-browser-check \
      --disable-sync \
      --no-sandbox \
      --disable-dev-shm-usage \
      --disable-gpu \
      --window-size=1280,800 \
      --disable-background-timer-throttling \
      --disable-backgrounding-occluded-windows \
      --disable-renderer-backgrounding \
      --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,BackForwardCache \
      https://www.nodeseek.com/ || true
    echo "登录浏览器退出了，3 秒后重新打开。"
    sleep 3
  done
}
start_chrome &

watch_browser() {
  miss=0
  sleep 45
  while true; do
    if [ -f /tmp/ns-checking ]; then
      now=$(date +%s)
      mtime=$(stat -c %Y /tmp/ns-checking 2>/dev/null || echo 0)
      if [ $((now - mtime)) -lt 180 ]; then
        miss=0
        sleep 20
        continue
      fi
    fi
    if runuser -u ns -- python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9337/json/version', timeout=3).read()" >/dev/null 2>&1; then
      miss=0
    else
      miss=$((miss + 1))
      if [ "$miss" -ge 9 ]; then
        echo "登录浏览器调试端口连续几分钟没有响应，正在重新打开。"
        pkill -u ns -f /usr/lib/chromium/chromium || true
        clear_browser_lock
        miss=0
      fi
    fi
    sleep 20
  done
}
watch_browser &

i=0
ready=0
while [ "$i" -lt 40 ]; do
  if runuser -u ns -- python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9337/json/version', timeout=1).read()"; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "登录浏览器没有在 40 秒内打开。面板仍会启动，请根据上面的报错检查容器。"
fi

cd /app
exec runuser -u ns -- python app.py
