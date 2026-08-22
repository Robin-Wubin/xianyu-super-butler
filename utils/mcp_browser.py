"""Chrome MCP 远程浏览器支持。

背景：服务器端无头 Chromium 在弱机（1.9G 内存）上不仅启动易崩，浏览器指纹
也容易被阿里 nc 识别。当用户配置了 Chrome MCP（如 mcp-chrome，streamable
HTTP 接入本机真实 Chrome）时，人工滑块验证改为：

1. 通过 MCP 在用户本机 Chrome 新开窗口导航到惩罚页
2. 用户亲手拖滑块（真人轨迹 + 真实指纹，体验和成功率都最佳）
3. 后端轮询 ``document.cookie``，拿到 x5sec 后合并回账号 Cookie

惩罚页 URL 自带 x5secdata 令牌，与账号 Cookie 无关，因此不需要向远程
Chrome 注入 Cookie；验证产物 x5sec 通过 JS 即可读取。
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger

# 进行中/已结束的 MCP 验证会话，供前端轮询（key 为 cookie_id）
mcp_sessions: Dict[str, Dict[str, Any]] = {}


def get_mcp_config() -> Dict[str, Any]:
    """读取远程浏览器配置。未配置时 enabled=False，走原有本地无头路径。"""
    try:
        from app.db_manager import db_manager

        enabled = (db_manager.get_system_setting('mcp_browser_enabled') or 'false').strip().lower() == 'true'
        url = (db_manager.get_system_setting('mcp_browser_url') or '').strip()
    except Exception as exc:
        logger.debug(f"读取 MCP 浏览器配置失败: {exc}")
        return {'enabled': False, 'url': ''}
    return {'enabled': enabled, 'url': url}


class ChromeMCPClient:
    """极简 MCP streamable-http 客户端（JSON-RPC over HTTP/SSE）。

    只实现人工滑块验证需要的部分：initialize + tools/call。
    """

    def __init__(self, url: str):
        self.url = url
        self._session_id: Optional[str] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._next_id = 1

    async def __aenter__(self) -> "ChromeMCPClient":
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http:
            await self._http.close()

    async def _rpc(self, payload: dict) -> Optional[dict]:
        assert self._http is not None, "client not started"
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        if self._session_id:
            headers['Mcp-Session-Id'] = self._session_id

        async with self._http.post(self.url, json=payload, headers=headers) as resp:
            # initialize 响应头里下发会话 ID，后续请求必须带上
            sid = resp.headers.get('Mcp-Session-Id')
            if sid:
                self._session_id = sid
            text = await resp.text()

        # 响应是 SSE 流（data: {...}）或纯 JSON；通知类请求可能无响应体
        for line in text.splitlines():
            if line.startswith('data:'):
                data = line[5:].strip()
                if data:
                    try:
                        return json.loads(data)
                    except json.JSONDecodeError:
                        continue
        text = text.strip()
        if text.startswith('{'):
            return json.loads(text)
        return None

    async def initialize(self) -> None:
        resp = await self._rpc({
            'jsonrpc': '2.0', 'id': self._next_id,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-03-26',
                'capabilities': {},
                'clientInfo': {'name': 'xianyu-super-butler', 'version': '1.0'},
            },
        })
        self._next_id += 1
        if not resp or 'error' in resp:
            raise RuntimeError(f"MCP initialize 失败: {resp}")
        # initialized 通知（无响应体）
        await self._rpc({
            'jsonrpc': '2.0',
            'method': 'notifications/initialized',
        })

    async def call(self, name: str, arguments: Optional[dict] = None) -> str:
        resp = await self._rpc({
            'jsonrpc': '2.0', 'id': self._next_id,
            'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments or {}},
        })
        self._next_id += 1
        result = (resp or {}).get('result') or {}
        if result.get('isError') or 'error' in (resp or {}):
            raise RuntimeError(f"MCP 工具 {name} 执行失败: {resp}")
        texts = [
            c.get('text', '')
            for c in (result.get('content') or [])
            if isinstance(c, dict) and c.get('type') == 'text'
        ]
        return '\n'.join(texts)

    async def navigate(self, url: str, new_window: bool = True) -> str:
        return await self.call('chrome_navigate', {'url': url, 'newWindow': new_window})

    async def evaluate(self, code: str, timeout_ms: int = 8000) -> str:
        return await self.call('chrome_javascript', {'code': code, 'timeoutMs': timeout_ms})

    async def close_tabs_by_url(self, url: str) -> None:
        try:
            await self.call('chrome_close_tabs', {'url': url})
        except Exception as exc:
            logger.debug(f"关闭远程标签页失败（忽略）: {exc}")


# 在惩罚页里执行的探测脚本：返回 x5sec 值与滑块是否仍在
_PROBE_JS = """
const m = document.cookie.match(/(?:^|;\\s*)x5sec=([^;]+)/);
let hasCaptcha = false;
try {
  const sels = ['#nocaptcha', '#scratch-captcha-btn', '.scratch-captcha-container', '.nc-container'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.offsetParent !== null) { hasCaptcha = true; break; }
  }
} catch (e) {}
return JSON.stringify({x5sec: m ? m[1] : '', hasCaptcha});
"""


async def probe_page(client: ChromeMCPClient) -> Dict[str, Any]:
    raw = (await client.evaluate(_PROBE_JS)).strip()
    candidates = [raw]
    if raw.startswith('"') and raw.endswith('"'):
        # 工具可能把字符串结果再包一层引号
        candidates.append(raw[1:-1].replace('\\"', '"').replace('\\\\', '\\'))
    for candidate in candidates:
        try:
            d = json.loads(candidate)
            if isinstance(d, dict):
                return {'x5sec': d.get('x5sec') or '', 'hasCaptcha': bool(d.get('hasCaptcha'))}
        except Exception:
            continue
    # 解析失败按“未完成”处理，下一轮再试
    return {'x5sec': '', 'hasCaptcha': True}


async def open_manual_session_mcp(
    cookie_id: str,
    cookies_str: str,
    timeout: int = 300,
    verification_url: Optional[str] = None,
) -> Dict[str, Any]:
    """远程浏览器版人工验证：导航惩罚页 → 等用户拖 → 收割 x5sec。

    返回结构与 ``manual_captcha.open_manual_session`` 一致，调用方可直接复用
    收尾逻辑（保存 Cookie、解除熔断、同步实例）。
    """
    from utils.manual_captcha import _fetch_live_verification_url, get_verification_url

    session_id = str(cookie_id)
    result: Dict[str, Any] = {
        'success': False, 'cookies_str': cookies_str, 'message': '', 'session_id': session_id,
    }
    mcp_sessions[session_id] = {'status': 'waiting', 'message': '正在打开本机 Chrome 验证页…'}

    try:
        if not verification_url:
            verification_url = await _fetch_live_verification_url(cookie_id, cookies_str)
        if not verification_url:
            verification_url = get_verification_url(cookie_id)
        if not verification_url:
            result['message'] = '未找到该账号的滑块惩罚 URL，无法开启人工验证'
            mcp_sessions[session_id] = {'status': 'failed', 'message': result['message']}
            return result

        cfg = get_mcp_config()
        async with ChromeMCPClient(cfg['url']) as client:
            await client.initialize()
            await client.navigate(verification_url, new_window=True)
            mcp_sessions[session_id] = {
                'status': 'waiting',
                'message': '已在本机 Chrome 打开验证页，请切换过去完成滑块拖动',
            }
            logger.warning(f"【{cookie_id}】已在远程 Chrome 打开惩罚页，等待人工拖动滑块（{timeout}s）")

            deadline = time.monotonic() + timeout
            x5sec = ''
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                try:
                    state = await probe_page(client)
                except Exception as exc:
                    logger.debug(f"【{cookie_id}】探测远程页面失败: {exc}")
                    continue
                if state.get('x5sec'):
                    x5sec = state['x5sec']
                    if not state.get('hasCaptcha'):
                        break
                    # 拿到 x5sec 且滑块还在 → 再等一小会儿让页面收尾
                    await asyncio.sleep(2)
                    break

            await client.close_tabs_by_url(verification_url)

            if not x5sec:
                result['message'] = f'远程浏览器人工验证超时（{timeout} 秒内未完成）'
                mcp_sessions[session_id] = {'status': 'failed', 'message': result['message']}
                logger.warning(f"【{cookie_id}】{result['message']}")
                return result

            # 合并 x5sec 到账号 Cookie（其余 Cookie 保持原值）
            from utils.xianyu_utils import trans_cookies

            cd = trans_cookies(cookies_str)
            cd['x5sec'] = x5sec
            new_cookies = '; '.join(f'{k}={v}' for k, v in cd.items())

            result['success'] = True
            result['cookies_str'] = new_cookies
            result['message'] = '人工验证完成（远程浏览器）'
            mcp_sessions[session_id] = {'status': 'done', 'message': result['message']}
            logger.success(f"【{cookie_id}】远程浏览器人工验证完成，已收割 x5sec")
            return result
    except Exception as exc:
        result['message'] = f'远程浏览器验证异常: {exc}'
        mcp_sessions[session_id] = {'status': 'failed', 'message': result['message']}
        logger.error(f"【{cookie_id}】{result['message']}")
        return result


async def test_mcp_connection(url: str) -> Dict[str, Any]:
    """测试 MCP 连接（设置页「测试连接」按钮用）。"""
    try:
        async with ChromeMCPClient(url) as client:
            await client.initialize()
            raw = await client.call('get_windows_and_tabs')
            return {'success': True, 'message': f'连接成功：{raw[:120]}'}
    except Exception as exc:
        return {'success': False, 'message': f'连接失败: {exc}'}
