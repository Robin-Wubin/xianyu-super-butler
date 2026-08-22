#!/bin/sh
set -eu

# 滑块验证必须以有头模式跑：无头 Chrome 的 WebGL 渲染器、字体列表、
# navigator.plugins 与屏幕参数和有头差异巨大，会被阿里 nc 直接识破
# （见 XianyuAutoAsync.py 里 SLIDER_HEADLESS 的说明）。
#
# 但容器里没有物理显示器，Linux 上有头 Chromium 缺少 X server 会直接
# 启动失败（Missing X server or $DISPLAY），滑块链路等于从未跑起来。
# 这里用 Xvfb 提供虚拟显示补上这一环。
DISPLAY_NUM="${XVFB_DISPLAY_NUM:-99}"
SCREEN_SPEC="${XVFB_SCREEN:-1920x1080x24}"

start_xvfb() {
    command -v Xvfb >/dev/null 2>&1 || {
        echo "[entrypoint] 未安装 Xvfb，滑块将退化为无头模式（通过率显著下降）"
        return 1
    }

    Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_SPEC}" -nolisten tcp -ac >/tmp/xvfb.log 2>&1 &
    xvfb_pid=$!

    # 等显示就绪再放行，否则先启动的浏览器仍会连不上
    i=0
    while [ "$i" -lt 30 ]; do
        if ! kill -0 "$xvfb_pid" 2>/dev/null; then
            echo "[entrypoint] Xvfb 启动失败，详见 /tmp/xvfb.log"
            return 1
        fi
        if command -v xdpyinfo >/dev/null 2>&1; then
            if DISPLAY=":${DISPLAY_NUM}" xdpyinfo >/dev/null 2>&1; then
                break
            fi
        elif [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
            break
        fi
        i=$((i + 1))
        sleep 0.2
    done

    export DISPLAY=":${DISPLAY_NUM}"
    echo "[entrypoint] Xvfb 已就绪 DISPLAY=${DISPLAY} (${SCREEN_SPEC})"
    return 0
}

# 显式给了 DISPLAY 就用现成的（例如外部挂了 X server）；
# Xvfb 起不来也要让主程序照常启动 —— 滑块降级好过整个服务不可用。
if [ -n "${DISPLAY:-}" ]; then
    echo "[entrypoint] 使用已有 DISPLAY=${DISPLAY}"
elif start_xvfb; then
    :
else
    export SLIDER_HEADLESS=true
    echo "[entrypoint] 已回退 SLIDER_HEADLESS=true"
fi

# patchright 与 playwright 各自锁定不同 Chromium 版本目录（如 1234 vs
# 1223），但镜像只为 playwright 装了浏览器。patchright 的补丁在驱动层，
# 浏览器二进制与相邻版本协议兼容 —— 把它缺的版本目录软链到已安装的
# 最高版本，免去在镜像里重复下载一份 500MB 的 Chromium。
link_missing_browsers() {
    [ -d /ms-playwright ] || return 0
    for name in chromium chromium_headless_shell; do
        target=$(ls -d /ms-playwright/"${name}"-* 2>/dev/null | sort -V | tail -1)
        [ -n "$target" ] || continue
        base=${target#/ms-playwright/}
        # 找出所有「同名但版本目录不存在」的需求：从 patchright 的
        # browsers.json 读取期望 revision，逐个链接
        for pref in /opt/venv/lib/python*/site-packages/patchright/driver/package/browsers.json; do
            [ -f "$pref" ] || continue
            for wantrev in $(python - "$pref" 2>/dev/null <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for b in data.get("browsers", []):
    n = b.get("name", "").replace("-headless-shell", "_headless_shell")
    if n in ("chromium", "chromium_headless_shell"):
        print(b.get("revision"))
PY
            ); do
                link=/ms-playwright/${name}-${wantrev}
                if [ ! -e "$link" ]; then
                    ln -s "$target" "$link" && echo "[entrypoint] 浏览器兼容链接: ${name}-${wantrev} -> ${base}"
                fi
            done
        done
    done
}
link_missing_browsers

exec python Start.py
