"""
刮刮乐远程控制模块
通过 WebSocket 实时传输页面截图到前端，并接收用户操作
"""

import asyncio
import base64
from typing import Optional, Dict, Any
from loguru import logger
from playwright.async_api import Page


class CaptchaRemoteController:
    """刮刮乐远程控制器"""
    
    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.websocket_connections: Dict[str, Any] = {}
    
    async def create_session(self, session_id: str, page: Page) -> Dict[str, str]:
        """
        创建远程控制会话
        
        Args:
            session_id: 会话ID（通常是用户ID）
            page: Playwright Page 对象
            
        Returns:
            包含会话信息的字典
        """
        # 初始截图：整页视口（与 screencast 帧保持一致，客户端统一按 viewport 比例映射坐标）
        screenshot_bytes = await page.screenshot(type='jpeg', quality=60, full_page=False)
        screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
        
        # 获取视口大小
        try:
            viewport = page.viewport_size
            if viewport is None:
                # 如果没有设置viewport，使用默认值或通过JS获取
                viewport = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        except:
            viewport = {'width': 1280, 'height': 720}  # 默认值
        
        # 存储会话
        self.active_sessions[session_id] = {
            'page': page,
            'screenshot': screenshot_base64,
            'captcha_info': None,
            'completed': False,
            'viewport': viewport,
            'cdp': None,
            'screencast_ok': False,
        }

        # 启动 CDP screencast 推流（浏览器主动推帧，替代逐事件截图）
        await self._start_screencast(session_id)
        
        logger.info(f"✅ 创建远程控制会话: {session_id}")
        
        return {
            'session_id': session_id,
            'screenshot': screenshot_base64,
            'captcha_info': None,
            'viewport': self.active_sessions[session_id]['viewport']
        }

    async def _start_screencast(self, session_id: str) -> bool:
        """启动 CDP screencast：浏览器主动推送压缩帧，弱机也能流畅"""
        session = self.active_sessions.get(session_id)
        if not session or not session.get('page'):
            return False
        page = session['page']
        try:
            cdp = await page.context.new_cdp_session(page)
            session['cdp'] = cdp

            def _on_frame(params):
                asyncio.ensure_future(self._handle_screencast_frame(session_id, params))

            cdp.on('Page.screencastFrame', _on_frame)
            await cdp.send('Page.enable')
            await cdp.send('Page.startScreencast', {
                'format': 'jpeg',
                'quality': 50,
                'maxWidth': 720,
                'maxHeight': 1440,
            })
            session['screencast_ok'] = True
            logger.info(f"📡 CDP screencast 已启动: {session_id}")
            return True
        except Exception as e:
            session['screencast_ok'] = False
            logger.warning(f"CDP screencast 启动失败，回退截图轮询: {e}")
            return False

    async def _handle_screencast_frame(self, session_id: str, params: dict):
        session = self.active_sessions.get(session_id)
        if not session:
            return
        cdp = session.get('cdp')
        # 必须逐帧 ack，否则浏览器停止推流
        try:
            if cdp:
                await cdp.send('Page.screencastFrameAck', {'sessionId': params.get('sessionId')})
        except Exception:
            pass
        data = params.get('data')
        if not data:
            return
        session['screenshot'] = data
        session['last_frame_ts'] = asyncio.get_event_loop().time()
        n = session.setdefault('_frame_count', 0) + 1
        session['_frame_count'] = n
        if n == 1 or n % 50 == 0:
            logger.info(f"📡 screencast 推帧中: {session_id} 已收 {n} 帧")
        await self._broadcast_screenshot(session_id, data)

    async def _broadcast_screenshot(self, session_id: str, b64: str):
        ws = self.websocket_connections.get(session_id)
        if ws is None:
            return
        try:
            await ws.send_json({
                'type': 'screenshot_update',
                'screenshot': b64,
                'viewport': self.active_sessions.get(session_id, {}).get('viewport'),
            })
        except Exception:
            pass

    def _maybe_snapshot_fallback(self, session_id: str):
        """混合兜底：推流帧超过 0.6s 没到时，主动截一张补发，保证拖动始终有反馈"""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        now = asyncio.get_event_loop().time()
        if not session.get('screencast_ok'):
            return  # 旧轮询模式由 API 层负责截图
        if now - session.get('last_frame_ts', 0.0) < 0.6:
            return
        if session.get('_snapshot_inflight'):
            return
        session['_snapshot_inflight'] = True
        asyncio.ensure_future(self._push_snapshot(session_id))

    async def _push_snapshot(self, session_id: str):
        session = self.active_sessions.get(session_id)
        try:
            if not session or not session.get('page'):
                return
            b64 = await self.update_screenshot(session_id, quality=45)
            if b64:
                await self._broadcast_screenshot(session_id, b64)
        except Exception as e:
            logger.debug(f"兜底截图失败: {e}")
        finally:
            if session:
                session['_snapshot_inflight'] = False

    def is_screencast_active(self, session_id: str) -> bool:
        session = self.active_sessions.get(session_id)
        return bool(session and session.get('screencast_ok'))
    
    async def _screenshot_captcha_area(self, page: Page, captcha_info: Dict[str, Any]) -> bytes:
        """截取整个验证码容器区域"""
        try:
            if captcha_info and 'x' in captcha_info:
                # 直接截取整个容器，稍微留一点边距
                x = max(0, captcha_info['x'] - 10)
                y = max(0, captcha_info['y'] - 10)
                width = captcha_info['width'] + 20
                height = captcha_info['height'] + 20
                
                # 截取整个验证码容器
                screenshot_bytes = await page.screenshot(
                    type='jpeg',
                    quality=80,  # 验证码区域用高质量
                    clip={
                        'x': x,
                        'y': y,
                        'width': width,
                        'height': height
                    }
                )
                logger.info(f"✅ 截取验证码容器: {width}x{height} (包含完整验证码)")
                return screenshot_bytes
            else:
                # 如果没有找到滑块，截取整个页面
                logger.warning("未找到滑块位置，截取整个页面")
                return await page.screenshot(type='jpeg', quality=75, full_page=False)
                
        except Exception as e:
            logger.warning(f"截取滑块区域失败，使用全页面: {e}")
            return await page.screenshot(type='jpeg', quality=75, full_page=False)
    
    async def _get_captcha_info(self, page: Page) -> Dict[str, Any]:
        """获取滑块验证码信息（查找整个容器）"""
        try:
            # 优先查找整个验证码容器（不是按钮）
            container_selectors = [
                '#nocaptcha',  # 完整的验证码容器
                '.scratch-captcha-container',
                '[id*="captcha"]',
                '.nc-container'
            ]
            
            # 先在主页面查找
            for selector in container_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        box = await element.bounding_box()
                        if box and box['width'] > 100 and box['height'] > 100:  # 确保找到的是容器
                            logger.info(f"✅ 在主页面找到验证码容器: {selector}, 大小: {box['width']}x{box['height']}")
                            return {
                                'selector': selector,
                                'x': box['x'],
                                'y': box['y'],
                                'width': box['width'],
                                'height': box['height'],
                                'in_iframe': False
                            }
                except Exception as e:
                    logger.debug(f"检查选择器 {selector} 失败: {e}")
                    continue
            
            # 在 iframe 中查找
            frames = page.frames
            for frame in frames:
                if frame != page.main_frame:
                    for selector in container_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                box = await element.bounding_box()
                                if box and box['width'] > 100 and box['height'] > 100:
                                    logger.info(f"✅ 在iframe找到验证码容器: {selector}, 大小: {box['width']}x{box['height']}")
                                    return {
                                        'selector': selector,
                                        'x': box['x'],
                                        'y': box['y'],
                                        'width': box['width'],
                                        'height': box['height'],
                                        'in_iframe': True
                                        # 注意：不保存 frame 对象，因为不能被 JSON 序列化
                                    }
                        except Exception as e:
                            logger.debug(f"iframe检查选择器 {selector} 失败: {e}")
                            continue
            
            logger.warning("⚠️ 未找到验证码容器")
            return None
            
        except Exception as e:
            logger.error(f"获取滑块信息失败: {e}")
            return None
    
    async def update_screenshot(self, session_id: str, quality: int = 75) -> Optional[str]:
        """更新会话的截图（截取整个验证码容器）"""
        if session_id not in self.active_sessions:
            return None
        
        try:
            page = self.active_sessions[session_id]['page']
            captcha_info = self.active_sessions[session_id].get('captcha_info')
            
            # 截取整个验证码容器
            if captcha_info and 'x' in captcha_info:
                x = max(0, captcha_info['x'] - 10)
                y = max(0, captcha_info['y'] - 10)
                width = captcha_info['width'] + 20
                height = captcha_info['height'] + 20
                
                screenshot_bytes = await page.screenshot(
                    type='jpeg',
                    quality=quality,
                    clip={'x': x, 'y': y, 'width': width, 'height': height}
                )
            else:
                # 降级方案：截取整个页面
                screenshot_bytes = await page.screenshot(
                    type='jpeg',
                    quality=quality,
                    full_page=False
                )
            
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            self.active_sessions[session_id]['screenshot'] = screenshot_base64
            return screenshot_base64
            
        except Exception as e:
            logger.error(f"更新截图失败: {e}")
            return None
    
    async def handle_mouse_event(self, session_id: str, event_type: str, x: int, y: int) -> bool:
        """
        处理鼠标事件
        
        Args:
            session_id: 会话ID
            event_type: 事件类型 (down/move/up)
            x: X坐标
            y: Y坐标
            
        Returns:
            是否成功
        """
        if session_id not in self.active_sessions:
            logger.warning(f"会话不存在: {session_id}")
            return False
        
        try:
            page = self.active_sessions[session_id]['page']
            
            if event_type == 'down':
                await page.mouse.move(x, y)
                await page.mouse.down()
                logger.info(f"🖱️ 鼠标按下: ({x}, {y})")
                self._maybe_snapshot_fallback(session_id)
                
            elif event_type == 'move':
                # 节流：移动事件极密集，转发过快反而造成事件堆积
                now = asyncio.get_event_loop().time()
                _sess = self.active_sessions[session_id]
                if now - _sess.get('_last_move', 0.0) < 0.03:
                    return True
                _sess['_last_move'] = now
                await page.mouse.move(x, y)
                self._maybe_snapshot_fallback(session_id)
                
            elif event_type == 'up':
                await page.mouse.up()
                logger.info(f"🖱️ 鼠标释放: ({x}, {y})")
                
            else:
                logger.warning(f"未知事件类型: {event_type}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"处理鼠标事件失败: {e}")
            return False
    
    async def check_completion(self, session_id: str) -> bool:
        """检查验证是否完成（更严格的判断）"""
        if session_id not in self.active_sessions:
            return False
        
        try:
            page = self.active_sessions[session_id]['page']
            
            # 多个选择器检查，确保更准确
            captcha_selectors = [
                '#nocaptcha',
                '#scratch-captcha-btn',
                '.scratch-captcha-container',
                '.scratch-captcha-slider'
            ]
            
            found_visible_captcha = False
            
            # 检查主页面
            for selector in captcha_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        is_visible = await element.is_visible()
                        if is_visible:
                            logger.debug(f"主页面发现可见滑块: {selector}")
                            found_visible_captcha = True
                            break
                except:
                    continue
            
            if found_visible_captcha:
                return False
            
            # 检查所有 iframe
            frames = page.frames
            for frame in frames:
                if frame != page.main_frame:
                    for selector in captcha_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                is_visible = await element.is_visible()
                                if is_visible:
                                    logger.debug(f"iframe中发现可见滑块: {selector}")
                                    found_visible_captcha = True
                                    break
                        except:
                            continue
                    if found_visible_captcha:
                        break
            
            if found_visible_captcha:
                return False
            
            # 额外检查：看页面内容是否还包含滑块相关文字
            try:
                page_content = await page.content()
                captcha_keywords = ['scratch-captcha', 'nocaptcha', 'slider-btn']
                
                # 如果页面中仍然有大量滑块相关内容，可能还未完成
                keyword_count = sum(1 for kw in captcha_keywords if kw in page_content)
                if keyword_count >= 2:
                    logger.debug(f"页面中仍有 {keyword_count} 个滑块关键词")
                    return False
            except:
                pass
            
            # 所有检查都通过，认为验证完成
            logger.success(f"✅ 验证完成（所有滑块元素已消失）: {session_id}")
            self.active_sessions[session_id]['completed'] = True
            return True
            
        except Exception as e:
            logger.error(f"检查完成状态失败: {e}")
            # 出错时返回 False，不要误判为成功
            return False
    
    def is_completed(self, session_id: str) -> bool:
        """检查会话是否已完成"""
        if session_id not in self.active_sessions:
            return False
        return self.active_sessions[session_id].get('completed', False)
    
    def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在"""
        return session_id in self.active_sessions
    
    async def close_session(self, session_id: str):
        """关闭会话"""
        if session_id in self.active_sessions:
            session = self.active_sessions.pop(session_id)
            cdp = session.get('cdp')
            if cdp:
                try:
                    await cdp.send('Page.stopScreencast')
                    await cdp.detach()
                except Exception:
                    pass
            logger.info(f"🔒 关闭远程控制会话: {session_id}")
    
    async def auto_refresh_screenshot(self, session_id: str, interval: float = 1.0):
        """自动刷新截图（优化版：按需更新）"""
        last_update_time = asyncio.get_event_loop().time()
        
        while session_id in self.active_sessions and not self.is_completed(session_id):
            try:
                # screencast 模式下由浏览器主动推帧，跳过轮询截图
                if self.active_sessions[session_id].get('screencast_ok'):
                    await asyncio.sleep(1)
                    continue

                current_time = asyncio.get_event_loop().time()
                
                # 使用自适应刷新：空闲时降低频率
                if current_time - last_update_time >= interval:
                    screenshot = await self.update_screenshot(session_id, quality=55)  # 降低质量提升性能
                    
                    if screenshot and session_id in self.websocket_connections:
                        try:
                            ws = self.websocket_connections[session_id]
                            await ws.send_json({
                                'type': 'screenshot_update',
                                'screenshot': screenshot
                            })
                            last_update_time = current_time
                        except:
                            # WebSocket 可能已断开
                            break
                
                # 降低检查频率，减少 CPU 使用
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"自动刷新截图失败: {e}")
                await asyncio.sleep(1)  # 出错时等待更长时间


# 全局实例
captcha_controller = CaptchaRemoteController()

