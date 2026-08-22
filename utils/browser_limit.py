"""浏览器并发限流。

背景：项目里有多处需要拉起 headless Chromium（扫码登录、Cookie 刷新、
账号资料与商品详情抓取）。这些调用点原先各自独立启动浏览器，信号量也都建在
账号实例内部，于是挂几个账号就可能同时冒出几个 Chromium —— 多个账号的定时
Cookie 刷新一旦撞在同一分钟，这种情况几乎必然发生。

一个 headless Chromium 冷启动就要占满一个核数十秒。在 J1800 这类双核低压
CPU 上，两三个并存就足以把整机拖到 99%，启动变慢又导致任务继续堆积，形成雪崩。

这里提供一个进程内全局信号量，所有浏览器启动都从这里取槽位，用完自动归还。
槽位数按机器实际配置推算，弱机强制单开。
"""
import asyncio
import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

from loguru import logger

# 允许通过环境变量强制指定，方便排查问题或应对推算不准的机器
_ENV_KEY = 'MAX_CONCURRENT_BROWSERS'

# 底层用线程信号量而不是 asyncio 版本：滑块验证走的是同步 Playwright，
# 跑在独立线程里，与异步任务共用同一台机器的 CPU。两边各建一套信号量的话，
# 总数就会翻倍，限流也就失去意义。
_semaphore: Optional[threading.Semaphore] = None
_semaphore_lock = threading.Lock()
_limit: Optional[int] = None

# 等待空闲浏览器的上限。低配机器上一次浏览器任务可能要一两分钟，
# 排队本身是正常的；但等过这个时间基本说明有任务卡死了，
# 与其无限期挂着，不如报错让上层记录并重试。
_ACQUIRE_TIMEOUT = 300

# Chromium 启动阶段的超时。正常冷启动即便在弱机上也应在 1 分钟内完成；
# 超过这个时间基本是浏览器进程已崩溃而 playwright 未感知，挂等只会
# 白白占住槽位（弱机单槽位时等于拖死全部浏览器任务）。
_LAUNCH_TIMEOUT = 180


# ==================== Windows 指纹伪装 ====================
# 背景：容器里的浏览器是 Linux(aarch64) Chromium，闲鱼风控眼里
# 「Linux 桌面用户」是极小众群体，本身就有风险加权；且官方 Chrome
# 不出 ARM Linux 版，「装真 Chrome」这条路走不通。
# 因此统一伪装成 Windows Chrome，且伪装必须全套一致：
#   1. UA 版本号与实际 Chromium 大版本对齐（Sec-CH-UA 头由二进制生成，
#      版本对不上就是自相矛盾的检测信号）；
#   2. navigator.platform / userAgentData / WebGL 渲染器等 JS 指纹同步伪装；
#   3. locale/timezone 中文环境（launch 时已统一注入）。
# 关闭方式：环境变量 BROWSER_FAKE_WINDOWS_UA=false
_chromium_major_cache: Optional[int] = None


def _detect_chromium_major(playwright) -> Optional[int]:
    """探测已安装 Chromium 的大版本号（结果缓存）。"""
    global _chromium_major_cache
    if _chromium_major_cache:
        return _chromium_major_cache
    try:
        import subprocess as _sp
        exe = str(playwright.chromium.executable_path)
        # patchright 可能指向软链目录，找不到就直接查 chrome 二进制
        out = _sp.run([exe, '--version'], capture_output=True, text=True, timeout=20)
        import re as _re
        m = _re.search(r'(\d+)\.', out.stdout or '')
        if m:
            _chromium_major_cache = int(m.group(1))
            return _chromium_major_cache
    except Exception:
        pass
    return None


def _fake_windows_ua(playwright) -> Optional[str]:
    if os.environ.get('BROWSER_FAKE_WINDOWS_UA', 'true').strip().lower() == 'false':
        return None
    major = _detect_chromium_major(playwright) or 136
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


# 与 UA 配套的 JS 层指纹伪装。只覆盖「与 UA 矛盾」的字段，
# 不碰 webdriver/plugins 等（patchright 已处理，重复修改反而露馅）。
_WIN_FINGERPRINT_JS = r"""
(() => {
  const major = (navigator.userAgent.match(/Chrome\/(\d+)/) || [])[1];
  if (!major) return;
  // navigator.* 属性挂在原型上，实例级 defineProperty 不一定生效
  const def = (obj, key, getter) => {
    for (const target of [obj, Object.getPrototypeOf(obj)]) {
      try { Object.defineProperty(target, key, {get: getter, configurable: true}); return true; } catch (e) {}
    }
    return false;
  };
  // 原生 playwright 下 navigator.webdriver 为 true，必须隐藏
  try { def(navigator, 'webdriver', () => false); } catch (e) {}
  def(navigator, 'platform', () => 'Win32');
  try {
    if (navigator.userAgentData) {
      Object.defineProperty(navigator, 'userAgentData', {
        get: () => ({
          brands: [
            {brand: 'Chromium', version: major},
            {brand: 'Not A(Brand', version: '99'},
            {brand: 'Google Chrome', version: major},
          ],
          mobile: false,
          platform: 'Windows',
          getHighEntropyValues: (hints) => Promise.resolve({
            platform: 'Windows', platformVersion: '15.0.0',
            architecture: 'x86', bitness: '64',
            uaFullVersion: major + '.0.0.0',
            model: '', fullVersionList: [
              {brand: 'Chromium', version: major + '.0.0.0'},
              {brand: 'Not A(Brand', version: '99.0.0.0'},
              {brand: 'Google Chrome', version: major + '.0.0.0'},
            ],
          }),
        }), configurable: true,
      });
    }
  } catch (e) {}
  try {
    // 37445/37446 是 WEBGL_debug_renderer_info 扩展的常量，
    // 未启用扩展时返回 None —— 直接拦 getParameter 的这两个参数即可
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Google Inc. (Intel)';
      if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return getParam.apply(this, arguments);
    };
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Google Inc. (Intel)';
      if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return getParam2.apply(this, arguments);
    };
  } catch (e) {}
  try {
    def(navigator, 'hardwareConcurrency', () => 8);
    def(navigator, 'deviceMemory', () => 8);
  } catch (e) {}
})();
"""


def _detect_total_memory_gb() -> Optional[float]:
    """物理内存（GB）。取不到时返回 None，由调用方只按 CPU 判断。"""
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return None


def get_limit() -> int:
    """允许同时运行的浏览器数量。

    浏览器是这里最重的资源消耗方，一个 headless Chromium 冷启动就要吃满一个核
    数十秒；并发拉起既拖慢彼此，也让滑块验证更容易失败。所以按机器实际配置放行，
    弱机一律单开——排队只是慢一点，挤爆了整机卡死，连正常的消息收发都会受影响。

    推算不准时可用环境变量 MAX_CONCURRENT_BROWSERS 覆盖。
    """
    global _limit
    if _limit is not None:
        return _limit

    forced = (os.getenv(_ENV_KEY) or '').strip()
    if forced:
        try:
            _limit = max(1, int(forced))
            logger.info(f"浏览器并发上限由 {_ENV_KEY} 指定为 {_limit}")
            return _limit
        except ValueError:
            logger.warning(f"{_ENV_KEY} 取值无效: {forced!r}，改用自动推算")

    cpu_count = os.cpu_count() or 2
    memory_gb = _detect_total_memory_gb()

    # 双核或 4G 以内：只能单开。这类配置多见于低价 NAS 和入门小主机，
    # 同时跑两个浏览器就会明显卡顿，甚至把整机拖到无法响应。
    if cpu_count <= 2 or (memory_gb is not None and memory_gb <= 4.5):
        _limit = 1
    elif cpu_count <= 4 or (memory_gb is not None and memory_gb <= 8.5):
        _limit = 2
    else:
        _limit = 3

    memory_desc = f"{memory_gb:.1f}G" if memory_gb is not None else "未知"
    logger.info(f"浏览器并发上限: {_limit}（CPU {cpu_count} 核，内存 {memory_desc}）")
    return _limit


def _get_semaphore() -> threading.Semaphore:
    global _semaphore
    if _semaphore is None:
        with _semaphore_lock:
            if _semaphore is None:
                _semaphore = threading.Semaphore(get_limit())
    return _semaphore


class LimitedBrowser:
    """给浏览器对象套一层，close() 时把槽位还回去。

    这样调用方原有的 `finally: await browser.close()` 不用改写，
    也就不会出现「忘记归还槽位」导致后续启动被永久卡住的情况。
    其余属性一律透传，用起来与原对象无差别。
    """

    def __init__(self, browser: Any, semaphore: threading.Semaphore, purpose: str):
        self._browser = browser
        self._semaphore = semaphore
        self._purpose = purpose
        self._released = False
        self._release_lock = threading.Lock()

    def _release(self) -> None:
        with self._release_lock:
            if not self._released:
                self._released = True
                self._semaphore.release()

    async def close(self, *args, **kwargs):
        try:
            return await self._browser.close(*args, **kwargs)
        finally:
            self._release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


def profile_dir(account_id: str) -> str:
    """每个账号固定的浏览器 user-data-dir。

    每次都用临时 profile 的话，浏览器环境（Cookie、localStorage、canvas 指纹、
    登录痕迹）每次都是全新的 —— 这正是无头浏览器触发风控的典型特征。固定
    user-data-dir 让同一账号的浏览器环境跨会话保持一致，且目录放在持久化
    卷上（data/browser_profiles），容器重启也不丢。
    """
    import re

    safe = re.sub(r'[^0-9A-Za-z_-]', '', str(account_id)) or 'default'
    base = os.getenv('BROWSER_PROFILES_DIR') or os.path.join(
        os.getcwd(), 'data', 'browser_profiles'
    )
    path = os.path.join(base, f'user_{safe}')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning(f"创建浏览器 profile 目录失败（回退临时目录）: {exc}")
        import tempfile
        path = tempfile.mkdtemp(prefix='browser_profile_')
    return path


def _clean_singleton_locks(user_data_dir: str) -> None:
    """清掉 Chromium 的单例锁文件，否则上次异常退出后同一目录无法再启动。"""
    for name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        path = os.path.join(user_data_dir, name)
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass


async def launch_browser(
    playwright: Any,
    launch_options: Optional[Dict[str, Any]] = None,
    purpose: str = '浏览器任务',
    user_data_dir: Optional[str] = None,
) -> LimitedBrowser:
    """取到槽位后再启动浏览器；没有空位就等着。

    调用方必须像以前一样在 finally 里 close()，槽位随之归还。
    传 ``user_data_dir`` 时改用 ``launch_persistent_context``（同一账号
    固定目录，环境跨会话一致）；返回的对象上 ``new_page()``、
    ``add_cookies()``、``cookies()`` 等用法与 context 一致。
    """
    semaphore = _get_semaphore()

    # 在线程里等待，避免占着事件循环不放导致消息收发一起卡住
    acquired = await asyncio.to_thread(semaphore.acquire, True, _ACQUIRE_TIMEOUT)
    if not acquired:
        raise TimeoutError(
            f"{purpose}: 等待浏览器空闲超过 {_ACQUIRE_TIMEOUT} 秒。"
            "可能有浏览器任务卡住未退出，或机器性能不足以支撑当前账号数量。"
        )

    # 启动最多重试 3 次：弱机内存紧张时 Chromium 偶发启动即段错误
    # （SIGSEGV / crashpad 报错），等几秒让内核回收内存后重试通常就能成功
    last_err: Optional[BaseException] = None
    for attempt in range(3):
        try:
            opts = dict(launch_options or {})
            # 统一中文环境：容器里没有 locale 设置时 Chromium 默认 en-US，
            # 打开闲鱼/淘宝会重定向到英文版页面，还会影响风控指纹。
            opts['locale'] = 'zh-CN'
            opts['timezone_id'] = 'Asia/Shanghai'
            args = list(opts.get('args') or [])
            if '--lang=zh-CN' not in args:
                args.append('--lang=zh-CN')
            # Chrome 137+ 无 GPU 环境默认禁用 WebGL（防指纹），会导致
            # 「正常电脑必有 WebGL」的检测露馅；显式启用软件渲染
            if '--enable-unsafe-swiftshader' not in args:
                args.append('--enable-unsafe-swiftshader')
            # 隐藏 navigator.webdriver 自动化标志
            if '--disable-blink-features=AutomationControlled' not in args:
                args.append('--disable-blink-features=AutomationControlled')
            opts['args'] = args
            # Windows 指纹伪装（调用方未显式指定 UA 时）
            _inject_fp = False
            if not opts.get('user_agent'):
                _ua = _fake_windows_ua(playwright)
                if _ua:
                    opts['user_agent'] = _ua
                    _inject_fp = True
            if user_data_dir:
                _clean_singleton_locks(user_data_dir)
                opts['user_data_dir'] = user_data_dir
                browser = await asyncio.wait_for(
                    playwright.chromium.launch_persistent_context(**opts),
                    timeout=_LAUNCH_TIMEOUT,
                )
            else:
                # 启动阶段单独限时：Chromium 崩溃（如 OOM）时 playwright 的 launch()
                # 可能永久挂起且不抛异常，槽位会被无限期占住，拖死后续所有浏览器任务
                browser = await asyncio.wait_for(
                    playwright.chromium.launch(**opts),
                    timeout=_LAUNCH_TIMEOUT,
                )
            # JS 层指纹注入：persistent context 直接支持；普通 Browser
            # 对象没有该方法（由各 context 自行注入），忽略即可。
            # 注意 persistent context 自带的初始页面早于 add_init_script
            # 存在，要立即对已有页面补一次注入。
            if _inject_fp:
                # 原生 playwright 的 add_init_script 可用：自动覆盖所有
                # frame（含阿里滑块 iframe）。（patchright 曾把它阉割，已弃用；
                # 旧的逐 frame evaluate 保留在 apply_windows_fingerprint 兜底）
                try:
                    await browser.add_init_script(_WIN_FINGERPRINT_JS)
                except Exception:
                    pass
                # navigator.platform 是 LegacyUnforgeable 属性，JS 层覆盖不了，
                # 必须走 CDP Emulation.setUserAgentOverride（Playwright 自己
                # 设 UA 就是这个通道，不会引入额外检测面）
                _ua_str = opts.get('user_agent')
                if _ua_str:
                    async def _apply_platform(pg):
                        try:
                            cdp = await pg.context.new_cdp_session(pg)
                            _major = _ua_str.split('Chrome/')[1].split('.')[0]
                            await cdp.send('Emulation.setUserAgentOverride', {
                                'userAgent': _ua_str,
                                'platform': 'Windows',
                                'acceptLanguage': 'zh-CN,zh;q=0.9',
                                # 不带 userAgentMetadata 会把 navigator.userAgentData
                                # 清空，必须与 UA 品牌一致地补上
                                'userAgentMetadata': {
                                    'brands': [
                                        {'brand': 'Chromium', 'version': _major},
                                        {'brand': 'Not A(Brand', 'version': '99'},
                                        {'brand': 'Google Chrome', 'version': _major},
                                    ],
                                    'fullVersionList': [
                                        {'brand': 'Chromium', 'version': _major + '.0.0.0'},
                                        {'brand': 'Not A(Brand', 'version': '99.0.0.0'},
                                        {'brand': 'Google Chrome', 'version': _major + '.0.0.0'},
                                    ],
                                    'fullVersion': _major + '.0.0.0',
                                    'platform': 'Windows',
                                    'platformVersion': '15.0.0',
                                    'architecture': 'x86',
                                    'model': '',
                                    'mobile': False,
                                    'bitness': '64',
                                    'wow64': False,
                                },
                            })
                        except Exception:
                            pass
                    try:
                        for _pg in (getattr(browser, 'pages', None) or []):
                            await _apply_platform(_pg)
                        # 之后新建的页面同样补 CDP 覆盖
                        browser.on('page', lambda pg: asyncio.create_task(_apply_platform(pg)))
                    except Exception:
                        pass
            if attempt:
                logger.info(f"{purpose}: 第 {attempt + 1} 次尝试启动成功")
            return LimitedBrowser(browser, semaphore, purpose)
        except BaseException as e:
            last_err = e
            logger.warning(
                f"{purpose}: 浏览器启动失败（第 {attempt + 1}/3 次，"
                f"{type(e).__name__}），{'稍后重试' if attempt < 2 else '放弃'}"
            )
            if attempt < 2:
                await asyncio.sleep(3 + attempt * 5)  # 给内核留回收内存的时间
                continue
            # 启动失败要立刻归还，否则槽位会被永久占住
            semaphore.release()
            raise last_err

    # 理论上到不了这里，防御性处理
    semaphore.release()
    raise last_err or RuntimeError(f"{purpose}: 浏览器启动失败")

    return LimitedBrowser(browser, semaphore, purpose)


@contextmanager
def browser_slot(purpose: str = '浏览器任务'):
    """同步版槽位。供滑块验证这类跑在线程里的同步 Playwright 使用。

    用法::

        with browser_slot("滑块验证"):
            browser = playwright.chromium.launch(...)
            ...
    """
    semaphore = _get_semaphore()
    acquired = semaphore.acquire(True, _ACQUIRE_TIMEOUT)
    if not acquired:
        raise TimeoutError(
            f"{purpose}: 等待浏览器空闲超过 {_ACQUIRE_TIMEOUT} 秒。"
            "可能有浏览器任务卡住未退出，或机器性能不足以支撑当前账号数量。"
        )
    try:
        yield
    finally:
        semaphore.release()


async def apply_windows_fingerprint(page) -> int:
    """在页面所有 frame 里执行 Windows 指纹伪装脚本，返回成功 frame 数。

    patchright 阉割了 add_init_script，route.fulfill 的脚本也不执行，
    只能由调用方在页面/iframe 就绪后显式调用（跨域 iframe 也可 evaluate）。
    iframe 晚加载的场景建议周期性重刷。
    """
    ok = 0
    for fr in page.frames:
        try:
            await fr.evaluate(_WIN_FINGERPRINT_JS)
            ok += 1
        except Exception:
            continue
    return ok


def acquire_slot(purpose: str = '浏览器任务') -> None:
    """同步取槽位。用于滑块验证这类跑在线程里、启动与关闭分处两个方法的场景。

    取到之后必须调用 release_slot 归还，否则后续任务会一直排队。
    """
    semaphore = _get_semaphore()
    if not semaphore.acquire(True, _ACQUIRE_TIMEOUT):
        raise TimeoutError(
            f"{purpose}: 等待浏览器空闲超过 {_ACQUIRE_TIMEOUT} 秒。"
            "可能有浏览器任务卡住未退出，或机器性能不足以支撑当前账号数量。"
        )


def release_slot(purpose: str = '浏览器任务') -> None:
    """归还 acquire_slot 取得的槽位。重复调用会被忽略调用方自行保证配对。"""
    try:
        _get_semaphore().release()
    except ValueError:
        # 归还次数多于取用次数，说明配对有误；记录但不影响主流程
        logger.warning(f"{purpose}: 浏览器槽位重复归还，已忽略")
