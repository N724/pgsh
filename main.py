# plugins/pangguai_life.py

import httpx
import time
import hashlib
import json
import re
import os
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Union
from urllib.parse import urlparse, quote, unquote
from datetime import datetime, timedelta, date
from decimal import Decimal

from nonebot import on_command, on_regex, require, get_driver, logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, ArgPlainText, RegexGroup
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment, GroupMessageEvent # 假设主要使用 OneBot V11
from nonebot.permission import SUPERUSER # 用于管理员命令
from nonebot.exception import PausedException, FinishedException
from nonebot.plugin import PluginMetadata
from nonebot.config import Config as NBConfig
from pydantic import BaseModel, Field

# --- 插件元数据 ---
__plugin_meta__ = PluginMetadata(
    name="胖乖生活助手",
    description="胖乖生活账号管理、查询等功能 (移植版)",
    usage="""
    指令：
    胖乖登录 / 登录胖乖
    胖乖管理 / 管理胖乖
    胖乖查询 / 查询胖乖
    胖乖清理 / 清理胖乖 (管理员)
    胖乖授权 (管理员)
    """,
    type="application",
    homepage="https://github.com/AstrBotDevs/AstrBot", # 可以替换为你的插件仓库
    config=None, # 配置类在下面定义
    supported_adapters={"~onebot.v11"}, # 适配器示例
    extra={
        "author": "Original: linzixuan, Ported by: YourName",
        "version": "1.0.0", # 基于原版4.0移植
    }
)

# --- 配置定义 ---
class PangGuaiConfig(BaseModel):
    # 不再需要 zsm, use_ma_pay (已移除支付)
    pangguai_qinglong_config: str = Field(..., alias="pangguai_qinglong_config", description="青龙配置: Host丨ClientID丨ClientSecret")
    pangguai_osname: str = Field("pangguai", alias="pangguai_osname", description="青龙容器内胖乖的变量名")
    # 不再需要 pgVipmoney, pgcoin (已移除支付)

# --- 全局配置和状态 ---
driver = get_driver()
plugin_config = PangGuaiConfig(**driver.config.dict())
DATA_DIR = Path("data/pangguai_life") # 数据存储目录
DATA_FILE = DATA_DIR / "pangguai_data.json" # JSON 数据文件

# 确保数据目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- 数据存储 (简易 JSON 实现) ---
def load_data() -> Dict[str, Dict[str, Any]]:
    """加载 JSON 数据"""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: # 文件为空
                    return {"users": {}, "tokens": {}, "mobiles": {}, "auths": {}}
                return json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"胖乖生活: 加载数据失败: {e}, 将使用空数据")
            return {"users": {}, "tokens": {}, "mobiles": {}, "auths": {}}
    return {"users": {}, "tokens": {}, "mobiles": {}, "auths": {}}

def save_data(data: Dict[str, Dict[str, Any]]):
    """保存 JSON 数据"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"胖乖生活: 保存数据失败: {e}")

# --- 全局数据字典 ---
plugin_data = load_data()

# --- 辅助函数 (数据操作) ---
def bucket_get(bucket: str, key: str) -> Optional[Any]:
    return plugin_data.get(bucket, {}).get(str(key))

def bucket_set(bucket: str, key: str, value: Any):
    if bucket not in plugin_data:
        plugin_data[bucket] = {}
    plugin_data[bucket][str(key)] = value
    save_data(plugin_data)

def bucket_del(bucket: str, key: str):
    if bucket in plugin_data and str(key) in plugin_data[bucket]:
        del plugin_data[bucket][str(key)]
        save_data(plugin_data)

def bucket_all_keys(bucket: str) -> List[str]:
    return list(plugin_data.get(bucket, {}).keys())

# --- 青龙 API 客户端 ---
class QLClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url.rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self.http_client = httpx.AsyncClient(timeout=10.0)

    async def _get_token(self) -> Optional[str]:
        """获取或刷新 Token"""
        now = time.time()
        # 令牌有效或提前5分钟刷新
        if self._token and self._token_expires_at > now + 300:
            return self._token

        url = f"{self.base_url}/open/auth/token"
        params = {"client_id": self.client_id, "client_secret": self.client_secret}
        try:
            response = await self.http_client.get(url, params=params)
            response.raise_for_status() # Raises exception for 4xx/5xx errors
            data = response.json()
            if data.get("code") == 200 and "token" in data.get("data", {}):
                self._token = data["data"]["token"]
                # 假设令牌有效期为24小时 (青龙默认)
                self._token_expires_at = now + (data["data"].get("expiration", 86400) - 300) # 减去5分钟缓冲
                logger.info("胖乖生活: 成功获取青龙 Token")
                return self._token
            else:
                logger.error(f"胖乖生活: 获取青龙 Token 失败: {data.get('message', '未知错误')}")
                return None
        except httpx.RequestError as e:
            logger.error(f"胖乖生活: 请求青龙 Token 时网络错误: {e}")
            return None
        except Exception as e:
            logger.error(f"胖乖生活: 获取青龙 Token 时发生异常: {e}")
            return None

    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict]:
        """通用请求方法"""
        token = await self._get_token()
        if not token:
            return None

        url = f"{self.base_url}/open/{endpoint.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers["accept"] = "application/json"
        if 'json' in kwargs:
            headers["Content-Type"] = "application/json"

        try:
            response = await self.http_client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            # 处理 DELETE 请求可能返回空响应体或非 JSON 响应体
            if response.status_code == 204: # No Content
                return {"code": 200, "message": "操作成功"}
            if not response.content:
                return {"code": response.status_code, "message": "操作成功但无响应体"}

            try:
                result = response.json()
                if result.get('code') != 200:
                    logger.warning(f"胖乖生活: 青龙 API 调用失败 ({endpoint}): {result.get('message', result)}")
                return result
            except json.JSONDecodeError:
                 logger.warning(f"胖乖生活: 青龙 API 响应非 JSON ({endpoint}): {response.text[:100]}...")
                 return {"code": response.status_code, "message": "非JSON响应", "raw": response.text}

        except httpx.HTTPStatusError as e:
            logger.error(f"胖乖生活: 青龙 API 请求失败 ({endpoint}): {e.response.status_code} - {e.response.text[:100]}...")
            return {"code": e.response.status_code, "message": f"HTTP错误: {e.response.text[:100]}"}
        except httpx.RequestError as e:
            logger.error(f"胖乖生活: 青龙 API 网络错误 ({endpoint}): {e}")
            return {"code": 500, "message": f"网络错误: {e}"}
        except Exception as e:
            logger.error(f"胖乖生活: 青龙 API 请求异常 ({endpoint}): {e}")
            return {"code": 500, "message": f"未知异常: {e}"}

    async def get_envs(self, searchValue: Optional[str] = None) -> Optional[List[Dict]]:
        params = {}
        if searchValue:
            params['searchValue'] = searchValue
        result = await self._request("GET", "envs", params=params)
        return result.get("data") if result and result.get("code") == 200 else None

    async def add_env(self, name: str, value: str, remarks: str) -> Optional[Dict]:
        data = [{"name": name, "value": value, "remarks": remarks}]
        result = await self._request("POST", "envs", json=data)
        # 检查是否因为重复添加而失败 (需要根据实际青龙API返回调整)
        if result and result.get('code') != 200 and "value must be unique" in result.get('message', ''):
             logger.warning(f"胖乖生活: 添加变量 {name} 失败，可能已存在。")
             # 可以尝试查找现有变量并返回
             return None # 或者返回特定错误码
        return result['data'][0] if result and result.get("code") == 200 and result.get("data") else None


    async def update_env(self, env_id: Union[str, int], name: str, value: str, remarks: str) -> Optional[Dict]:
        data = {"name": name, "value": value, "remarks": remarks, "id": env_id}
        result = await self._request("PUT", "envs", json=data)
        return result.get("data") if result and result.get("code") == 200 else None

    async def delete_envs(self, ids: List[Union[str, int]]) -> bool:
        if not ids: return True
        # 青龙 openapi 删除环境变量需要 int 类型的 id 列表
        int_ids = []
        for i in ids:
            try:
                int_ids.append(int(i))
            except ValueError:
                logger.warning(f"胖乖生活: 无效的环境变量 ID 用于删除: {i}")
        if not int_ids: return False

        result = await self._request("DELETE", "envs", json=int_ids)
        return result is not None and result.get("code") == 200

    async def get_env_by_remark_and_name(self, name: str, account_id: str, phone: Optional[str] = None) -> Optional[Dict]:
        """通过备注中的账号ID或手机号以及变量名查找变量"""
        envs = await self.get_envs() # 获取所有变量效率可能较低，但 OpenAPI 不一定支持按 remarks 搜索
        if envs is None: return None

        target_env = None
        phone_match_env = None

        for env in envs:
            remarks = env.get('remarks', '')
            env_name = env.get('name')

            if env_name != name or not remarks:
                continue

            # 优先匹配账号ID
            if account_id in remarks:
                target_env = env
                break # 找到账号ID匹配，直接返回

            # 如果未找到账号ID匹配，再检查手机号匹配
            if phone and f'胖乖:{phone}' in remarks:
                 # 暂时记录手机号匹配的，继续查找是否有账号ID匹配的
                 phone_match_env = env

        # 如果找到账号ID匹配的，返回它；否则，如果找到手机号匹配的，返回它
        return target_env if target_env else phone_match_env


    async def add_or_update_env(self, osname: str, value: str, account: str, phone: str, user_id: str, auth_date: str) -> bool:
        """添加或更新青龙变量，优先通过 account 查找，其次 phone"""
        try:
            # 先尝试查找变量
            existing_env = await self.get_env_by_remark_and_name(osname, account, phone)
            quoted_value = quote(value) # URL 编码
            remarks = f'胖乖:{phone}丨用户:{user_id}丨授权时间:{auth_date}丨胖乖管理'

            if existing_env:
                # 更新变量
                env_id = existing_env.get('id') or existing_env.get('_id') # 兼容不同青龙版本
                if not env_id:
                    logger.error(f"胖乖生活: 找到环境变量但缺少 ID: {existing_env}")
                    return False
                logger.info(f"胖乖生活: 找到现有变量 (ID: {env_id}), 准备更新.")
                updated_env = await self.update_env(env_id, osname, quoted_value, remarks)
                if updated_env:
                    logger.info(f"胖乖生活: 成功更新青龙变量 {osname} for account {account}")
                    return True
                else:
                    logger.error(f"胖乖生活: 更新青龙变量 {osname} for account {account} 失败")
                    return False
            else:
                # 添加新变量
                logger.info(f"胖乖生活: 未找到变量 for account {account}, 准备添加新变量.")
                added_env = await self.add_env(osname, quoted_value, remarks)
                if added_env:
                    logger.info(f"胖乖生活: 成功添加青龙变量 {osname} for account {account}")
                    return True
                else:
                    # 再次检查是否是因为并发或其他原因添加失败但实际已存在
                    time.sleep(1) # 短暂等待
                    check_env = await self.get_env_by_remark_and_name(osname, account, phone)
                    if check_env:
                         logger.warning(f"胖乖生活: 添加变量 {osname} for {account} 失败，但后续检查发现已存在。")
                         return True # 认为操作成功
                    else:
                         logger.error(f"胖乖生活: 添加青龙变量 {osname} for account {account} 失败")
                         return False
        except Exception as e:
            logger.error(f"胖乖生活: 添加或更新青龙变量时出错: {e}", exc_info=True)
            return False

# --- 胖乖 API 客户端 ---
class PangGuaiClient:
    BASE_URL = "https://userapi.qiekj.com"
    APP_SECRET = "xl8v4s/5qpBLvN+8CzFx7vVjy31NgXXcedU7G0QpOMM=" # 注意：硬编码敏感信息有风险
    USER_AGENT = "okhttp/3.14.9"
    VERSION = "1.57.0"
    CHANNEL = "android_app"

    def __init__(self):
        self.http_client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=15.0)

    def _times13(self) -> int:
        """生成13位时间戳"""
        return int(time.time() * 1000)

    def _calculate_sign(self, timestamp_ms: int, token: str, url_path: str) -> str:
        """计算SHA256签名"""
        data = f'appSecret={self.APP_SECRET}&channel={self.CHANNEL}×tamp={timestamp_ms}&token={token}&version={self.VERSION}&{url_path}'
        sha256_hash = hashlib.sha256()
        sha256_hash.update(data.encode('utf-8'))
        return sha256_hash.hexdigest()

    async def _request(self, method: str, endpoint: str, token: str = "", data: Optional[Dict] = None, payload: Optional[str] = None) -> Optional[Dict]:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        timestamp_ms = self._times13()
        parsed_url = urlparse(url)
        sign = self._calculate_sign(timestamp_ms, token, parsed_url.path)

        headers = {
            'User-Agent': self.USER_AGENT,
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Content-Type': "application/x-www-form-urlencoded",
            'Authorization': f"{token}",
            'Version': self.VERSION,
            'channel': self.CHANNEL,
            'phoneBrand': "nonebot", # 可以自定义
            'timestamp': f"{timestamp_ms}",
            'sign': f"{sign}",
        }

        try:
            if payload:
                response = await self.http_client.request(method, url, headers=headers, content=payload.encode('utf-8'))
            else:
                response = await self.http_client.request(method, url, headers=headers, data=data)

            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                logger.warning(f"胖乖生活: API 调用失败 ({endpoint}): {result.get('msg', '未知错误')}")
            return result
        except httpx.HTTPStatusError as e:
            logger.error(f"胖乖生活: API 请求失败 ({endpoint}): Status {e.response.status_code} - {e.response.text[:100]}")
            return {"code": e.response.status_code, "msg": f"HTTP错误: {e.response.text[:100]}"}
        except httpx.RequestError as e:
            logger.error(f"胖乖生活: API 网络错误 ({endpoint}): {e}")
            return {"code": -1, "msg": f"网络错误: {e}"}
        except json.JSONDecodeError:
            logger.error(f"胖乖生活: API 响应非 JSON ({endpoint}): {response.text[:100]}")
            return {"code": -1, "msg": "响应解析错误"}
        except Exception as e:
            logger.error(f"胖乖生活: API 请求异常 ({endpoint}): {e}")
            return {"code": -1, "msg": f"未知异常: {e}"}

    async def verify_token(self, token: str) -> Optional[Tuple[str, str, str]]:
        """验证 Token 并获取用户信息"""
        endpoint = "user/info"
        payload = f"token={token}"
        result = await self._request("POST", endpoint, token=token, payload=payload)

        if result and result.get("code") == 0 and "data" in result:
            data = result["data"]
            phone = data.get("phone")
            account_id = str(data.get("id"))
            if phone and account_id:
                display_phone = f"{phone[:3]}****{phone[7:]}"
                return phone, account_id, display_phone
            else:
                 logger.warning(f"胖乖生活: 验证 token 成功但缺少 phone 或 id: {data}")
                 return None
        else:
            logger.warning(f"胖乖生活: 验证 Token 失败: {result.get('msg') if result else '无响应'}")
            return None # 表示 Token 失效或请求失败

    async def send_sms_code(self, phone: str) -> Tuple[bool, str]:
        """发送短信验证码"""
        endpoint = "common/sms/sendCode"
        payload = f"phone={phone}&template=reg"
        result = await self._request("POST", endpoint, payload=payload)

        if result and result.get("code") == 0 and result.get("msg") == "成功":
            return True, "验证码发送成功"
        else:
            error_msg = result.get('msg', '未知错误') if result else "请求失败"
            return False, f"获取验证码失败: {error_msg}"

    async def login_with_sms(self, phone: str, code: str) -> Optional[Tuple[str, str, str, str]]:
        """使用短信验证码登录/注册"""
        endpoint = "user/reg"
        payload = f"channel=h5&phone={phone}&verify={code}"
        result = await self._request("POST", endpoint, payload=payload)

        if result and result.get("code") == 0 and "data" in result:
            token = result["data"].get("token")
            if token:
                # 登录成功后，立即验证 token 获取完整信息
                verify_result = await self.verify_token(token)
                if verify_result:
                    r_phone, r_account, r_display_phone = verify_result
                    return r_phone, r_account, token, r_display_phone
                else:
                    logger.error("胖乖生活: 短信登录成功，但验证新 Token 失败")
                    return None
            else:
                logger.error(f"胖乖生活: 短信登录成功但未返回 Token: {result}")
                return None
        else:
            logger.warning(f"胖乖生活: 短信登录失败: {result.get('msg') if result else '无响应'}")
            return None

    async def get_account_info(self, token: str) -> Optional[Dict]:
        """查询账号余额、积分信息"""
        endpoint_balance = "user/balance"
        payload = f"token={token}"
        balance_result = await self._request("POST", endpoint_balance, token=token, payload=payload)

        if not (balance_result and balance_result.get("code") == 0 and "data" in balance_result):
            logger.warning(f"胖乖生活: 查询余额/积分失败: {balance_result.get('msg') if balance_result else '无响应'}")
            return None

        balance_data = balance_result["data"]

        # 查询积分记录（注意：此 API 可能需要调整参数或寻找替代方案）
        endpoint_integral = "integralRecord/pageList"
        # 注意：原脚本使用 files 参数，httpx 中对应 files 或 data，需确认 API 要求
        # 这里简化为 data 字典
        integral_data = {
            'page': '1',
            'pageSize': '100', # 可能需要分页处理大量记录
            'type': '100',
            'receivedStatus': '1',
            'token': token,
        }
        integral_result = await self._request("POST", endpoint_integral, token=token, data=integral_data)

        today_integral = 0
        if integral_result and integral_result.get("code") == 0 and "data" in integral_result:
            items = integral_result["data"].get("items", [])
            current_date_str = datetime.now().strftime('%Y-%m-%d')
            for item in items:
                received_time_str = item.get("receivedTime", "")
                if received_time_str and received_time_str.startswith(current_date_str):
                    try:
                        today_integral += int(item.get('amount', 0))
                    except ValueError:
                        pass #忽略无法转换的 amount
        else:
            logger.warning(f"胖乖生活: 查询积分记录失败: {integral_result.get('msg') if integral_result else '无响应'}")

        return {
            'balance': balance_data.get('balance', 0),
            'integral': balance_data.get('integral', 0),
            'today_integral': today_integral
        }

# --- 初始化客户端 ---
try:
    ql_config_parts = plugin_config.pangguai_qinglong_config.split('丨')
    if len(ql_config_parts) != 3:
        raise ValueError("青龙配置格式错误，应为 Host丨ClientID丨ClientSecret")
    ql_client = QLClient(base_url=ql_config_parts[0].strip(),
                         client_id=ql_config_parts[1].strip(),
                         client_secret=ql_config_parts[2].strip())
except ValueError as e:
    logger.error(f"胖乖生活: 初始化青龙客户端失败: {e}")
    ql_client = None
except Exception as e:
    logger.error(f"胖乖生活: 初始化青龙客户端时发生未知错误: {e}")
    ql_client = None

pg_client = PangGuaiClient()

# --- 其他辅助函数 ---
def get_today_str() -> str:
    """获取 YYYY-MM-DD 格式的今天日期字符串"""
    return str(datetime.now().date())

def empower(empowertime: Optional[str], me_as_int: int) -> str:
    """计算授权到期日期 (YYYY-MM-DD)"""
    today_dt = datetime.now().date()
    days_to_add = me_as_int * 30 # 简单按每月30天计算

    try:
        if not empowertime:
            # 没有授权时间，从今天开始计算
            target_date = today_dt + timedelta(days=days_to_add)
        else:
            # 有授权时间，判断是否已过期
            empower_date = datetime.strptime(empowertime, "%Y-%m-%d").date()
            if empower_date <= today_dt:
                # 已过期，从今天开始计算
                target_date = today_dt + timedelta(days=days_to_add)
            else:
                # 未过期，在现有授权时间基础上累加
                target_date = empower_date + timedelta(days=days_to_add)
        return str(target_date)
    except ValueError:
        logger.warning(f"胖乖生活: 解析授权时间 '{empowertime}' 失败，将从今天开始计算。")
        return str(today_dt + timedelta(days=days_to_add))
    except Exception as e:
        logger.error(f"胖乖生活: 计算授权时间出错: {e}")
        # 出错时也从今天开始算
        return str(today_dt + timedelta(days=days_to_add))

async def send_push_notification(user_id: str, account: str, message: str):
    """尝试向用户发送推送通知 (简化版，仅记录日志)"""
    # 实际推送需要知道用户的平台和具体ID，NoneBot2 中通常通过 bot.send() 实现
    # 这里仅记录日志，表示尝试推送
    phone = bucket_get('mobiles', account)
    display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID {account}"
    full_message = f"胖乖生活通知 (用户: {user_id}, 账号: {display_phone}):\n{message}"
    logger.info(full_message)
    # 实际推送示例 (需要 bot 对象和目标信息):
    # try:
    #     bot = get_bot() # 需要获取当前事件的 bot 或全局 bot
    #     # 需要用户适配器和 ID 信息，例如 event.get_user_id()
    #     # await bot.send_private_msg(user_id=int(qq_user_id), message=full_message)
    # except Exception as e:
    #     logger.error(f"胖乖生活: 推送通知失败: {e}")


# --- NoneBot2 事件处理 ---

# 登录命令
login_cmd = on_command("胖乖登录", aliases={"登录胖乖", "登陆胖乖", "胖乖登陆"}, priority=10, block=True)

@login_cmd.handle()
async def handle_login_start(matcher: Matcher, event: MessageEvent):
    await matcher.send("=======胖乖登录=====\n请输入手机号:\n------------------\n回复\"q\"退出操作\n====================")

@login_cmd.got("phone", prompt="请输入11位手机号码:")
async def handle_login_phone(matcher: Matcher, event: MessageEvent, phone: str = ArgPlainText("phone")):
    user_id = event.get_user_id()
    phone = phone.strip()

    if phone.lower() == 'q':
        await matcher.finish("✅ 已取消登录")

    if not phone.isdigit() or len(phone) != 11:
        await matcher.reject("=======格式错误=====\n❌ 请输入正确的11位手机号\n====================") # reject 会让用户重新输入 phone

    # 检查手机号是否已绑定 (并处理旧账号信息)
    existing_account_info = None
    user_accounts = eval(bucket_get('users', user_id) or '[]') # 使用 eval 有风险，确保数据来源可信

    for acc in list(user_accounts): # 遍历副本以允许修改
        acc_phone = bucket_get('mobiles', acc)
        if acc_phone == phone:
            logger.info(f"胖乖生活: 用户 {user_id} 尝试登录已存在的手机号 {phone}, 账号 {acc}，将处理旧数据。")
            existing_account_info = {
                "account_id": acc,
                "auth": bucket_get('auths', acc)
            }
            user_accounts.remove(acc)
            # 删除旧数据
            bucket_del('mobiles', acc)
            bucket_del('tokens', acc)
            # 尝试删除旧的青龙变量 (需要 ql_client)
            if ql_client:
                 envs = await ql_client.get_envs()
                 if envs:
                     ids_to_delete = []
                     for env in envs:
                         if env.get('name') == plugin_config.pangguai_osname and acc in env.get('remarks', ''):
                             env_id = env.get('id') or env.get('_id')
                             if env_id: ids_to_delete.append(env_id)
                     if ids_to_delete:
                         deleted = await ql_client.delete_envs(ids_to_delete)
                         logger.info(f"胖乖生活: 删除手机号 {phone} 的旧青龙变量 (IDs: {ids_to_delete}): {'成功' if deleted else '失败'}")

            break # 找到后停止

    # 发送验证码
    success, msg = await pg_client.send_sms_code(phone)
    if not success:
        await matcher.finish(f"=======发送失败=====\n❌ {msg}\n====================")

    matcher.state["phone"] = phone # 保存手机号到 state
    matcher.state["user_accounts"] = user_accounts # 保存更新后的账号列表
    matcher.state["existing_account_info"] = existing_account_info # 保存旧账号信息
    await matcher.send("=======验证码登录=====\n✅ 验证码已发送\n请输入收到的4位验证码:\n------------------\n回复\"q\"退出操作\n====================")


@login_cmd.got("code", prompt="请输入4位验证码:")
async def handle_login_code(matcher: Matcher, event: MessageEvent, code: str = ArgPlainText("code")):
    user_id = event.get_user_id()
    phone = matcher.state.get("phone")
    user_accounts: List[str] = matcher.state.get("user_accounts", [])
    existing_account_info: Optional[Dict] = matcher.state.get("existing_account_info")
    code = code.strip()

    if code.lower() == 'q':
        await matcher.finish("✅ 已取消登录")

    if not code.isdigit() or len(code) != 4:
        await matcher.reject("=======验证码错误=====\n❌ 请输入正确的4位验证码\n====================")

    login_result = await pg_client.login_with_sms(phone, code)

    if not login_result:
        await matcher.finish("=======登录失败=====\n❌ 验证码错误或登录请求失败\n====================")

    new_phone, new_account, new_token, new_display_phone = login_result
    logger.info(f"胖乖生活: 用户 {user_id} 通过手机号 {phone} 登录成功, 账号ID: {new_account}")

    # 保存新账号信息
    bucket_set('mobiles', new_account, new_phone)
    bucket_set('tokens', new_account, new_token)

    # 处理旧账号授权转移
    new_auth_date = None
    if existing_account_info and existing_account_info.get("auth"):
        new_auth_date = existing_account_info["auth"]
        bucket_set('auths', new_account, new_auth_date)
        logger.info(f"胖乖生活: 已将账号 {existing_account_info['account_id']} 的授权 ({new_auth_date}) 转移至新账号 {new_account}")
    else:
        # 如果没有旧授权，检查一下新账号是否已存在授权（不太可能，但做个检查）
        new_auth_date = bucket_get('auths', new_account)

    # 更新用户账号列表
    if new_account not in user_accounts:
        user_accounts.append(new_account)
    # 使用 set 去重再转回 list 保证唯一性
    unique_accounts = list(dict.fromkeys(user_accounts))
    bucket_set('users', user_id, f'{unique_accounts}') # 仍然使用字符串存储列表

    # 判断授权状态并回复
    today_str = get_today_str()
    is_authorized = False
    auth_status_msg = '⚠️ 未授权'
    next_step_msg = f'发送 "胖乖管理" 可管理账号或进行授权'

    if new_auth_date:
        try:
            auth_dt = datetime.strptime(new_auth_date, "%Y-%m-%d").date()
            today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
            if auth_dt >= today_dt:
                is_authorized = True
                auth_status_msg = f'✅ 已授权至 {new_auth_date}'
                next_step_msg = f'发送 "胖乖管理" 可管理账号'
            else:
                auth_status_msg = f'❌ 授权已于 {new_auth_date} 过期'
        except ValueError:
             logger.warning(f"胖乖生活: 账号 {new_account} 的授权日期 '{new_auth_date}' 格式无效")
             # 保持未授权状态

    # 如果已授权，尝试添加到青龙
    if is_authorized and ql_client:
        await ql_client.add_or_update_env(
            osname=plugin_config.pangguai_osname,
            value=new_token,
            account=new_account,
            phone=new_phone,
            user_id=user_id,
            auth_date=new_auth_date # 使用已存在的授权日期
        )

    reply_msg = f"""=======绑定成功=====
📱 账号: {new_display_phone}
🔐 状态: {auth_status_msg}
⏰ 操作: {next_step_msg}
===================="""
    await matcher.finish(reply_msg)


# 管理命令
manage_cmd = on_command("胖乖管理", aliases={"管理胖乖"}, priority=10, block=True)

@manage_cmd.handle()
async def handle_manage_start(matcher: Matcher, event: MessageEvent):
    user_id = event.get_user_id()
    user_accounts_str = bucket_get('users', user_id)

    if not user_accounts_str:
        await matcher.finish(f"""=======未绑定账号=====
❌ 未找到任何账号信息
💡 发送 "胖乖登录" 绑定
====================""")

    try:
        accounts = list(dict.fromkeys(eval(user_accounts_str))) # 去重
        if not accounts:
             await matcher.finish(f"""=======未绑定账号=====
❌ 账号列表为空
💡 发送 "胖乖登录" 绑定
====================""")
        bucket_set('users', user_id, f'{accounts}') # 保存去重后的列表
    except Exception as e:
        logger.error(f"胖乖生活: 解析用户 {user_id} 账号列表失败: {e}")
        await matcher.finish("❌ 处理账号列表时出错，请联系管理员。")
        return

    matcher.state["accounts"] = accounts
    today_str = get_today_str()
    account_list_msg = "======我的胖乖账号=====\n"
    valid_accounts_display = []

    for i, account in enumerate(accounts):
        auth_date = bucket_get('auths', account)
        phone = bucket_get('mobiles', account)
        display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID:{account[:4]}...{account[-4:]}" # 手机号可能丢失

        vip_status = '⚠️ 未授权'
        if auth_date:
            try:
                auth_dt = datetime.strptime(auth_date, "%Y-%m-%d").date()
                today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
                if auth_dt < today_dt:
                     vip_status = f'❌ 已过期({auth_date})'
                else:
                     vip_status = f'✅ {auth_date}'
            except ValueError:
                vip_status = '❓日期无效'

        account_list_msg += f"""------------------
[{i+1}] 账号信息
📱 账号: {display_phone}
🔐 授权: {vip_status}\n"""
        valid_accounts_display.append({"index": i + 1, "account_id": account, "display": display_phone})

    account_list_msg += """==================
回复数字选择账号
回复"q"退出操作
=================="""

    matcher.state["valid_accounts_display"] = valid_accounts_display
    await matcher.send(account_list_msg)


@manage_cmd.got("choice", prompt="请回复数字选择账号:")
async def handle_manage_choice(matcher: Matcher, event: MessageEvent, choice: str = ArgPlainText("choice")):
    user_id = event.get_user_id()
    choice = choice.strip()
    accounts = matcher.state.get("accounts", [])
    valid_accounts_display = matcher.state.get("valid_accounts_display", [])

    if choice.lower() == 'q':
        await matcher.finish('✅ 已退出管理')

    try:
        choice_int = int(choice)
        if not (1 <= choice_int <= len(valid_accounts_display)):
            await matcher.reject('❌ 输入的序号无效，请重新输入:') # reject 会让用户重新输入 choice
        
        selected_account_info = next((acc for acc in valid_accounts_display if acc["index"] == choice_int), None)
        if not selected_account_info:
             await matcher.finish('❌ 内部错误：无法找到选择的账号信息。') # 不应该发生

        account_id = selected_account_info["account_id"]
        display_phone = selected_account_info["display"]
        token = bucket_get('tokens', account_id)
        auth_date = bucket_get('auths', account_id)

        if not token:
            await matcher.finish(f"❌ 无法找到账号 {display_phone} 的 Token 信息，可能需要重新登录。")

        # 验证 Token 有效性 (可选但推荐)
        verify_result = await pg_client.verify_token(token)
        if not verify_result:
             await matcher.send(f"⚠️ 警告：账号 {display_phone} 的 Token 似乎已失效，请考虑重新登录。")
             # 可以选择 finish，或者继续让用户管理
             # await matcher.finish(f"❌ 账号 {display_phone} 的 Token 已失效，请重新登录。")

        today_str = get_today_str()
        vip_status = '⚠️ 未授权'
        if auth_date:
            try:
                auth_dt = datetime.strptime(auth_date, "%Y-%m-%d").date()
                today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
                if auth_dt < today_dt:
                     vip_status = f'❌ 已过期({auth_date})'
                else:
                     vip_status = f'✅ {auth_date}'
            except ValueError:
                vip_status = '❓日期无效'

        account_info_msg = f"""=======账号详情======
📱 账号: {display_phone}
🔐 授权: {vip_status}
=================="""
        await matcher.send(account_info_msg)

        menu = """=======账号管理======
[1] 授权账号 (免费)
[2] 删除账号
------------------
回复数字选择功能
回复"q"退出操作
=================="""
        matcher.state["selected_account_id"] = account_id
        matcher.state["selected_token"] = token
        matcher.state["selected_auth_date"] = auth_date
        matcher.state["selected_display_phone"] = display_phone
        await matcher.send(menu)

    except ValueError:
        await matcher.reject('❌ 输入必须是数字，请重新输入:')


@manage_cmd.got("action", prompt="请回复数字选择功能:")
async def handle_manage_action(matcher: Matcher, event: MessageEvent, action: str = ArgPlainText("action")):
    user_id = event.get_user_id()
    action = action.strip()
    account_id = matcher.state.get("selected_account_id")
    token = matcher.state.get("selected_token")
    auth_date = matcher.state.get("selected_auth_date")
    display_phone = matcher.state.get("selected_display_phone")
    accounts: List[str] = matcher.state.get("accounts", []) # 获取管理开始时记录的列表

    if action.lower() == 'q':
        await matcher.finish('✅ 已退出管理')

    if action == '1':
        # 授权账号 (免费)
        matcher.state["next_action"] = "authorize"
        await matcher.send("""=======授权设置=====
请输入授权月数(如:1)
------------------
回复数字设置月数
回复"q"退出操作
====================")
        # 此处不暂停，等待下一个输入
        # 需要一个新的 got 来接收月数

    elif action == '2':
        # 删除账号
        matcher.state["next_action"] = "delete_confirm"
        await matcher.send(f"""=======删除警告=====
❌ 确定要删除账号 {display_phone} 吗？
------------------
此操作不可恢复！
[y] 确认删除
[n] 取消操作
====================")
        # 需要一个新的 got 来接收确认

    else:
        await matcher.reject('❌ 输入无效，请重新输入 [1] 或 [2]:')


@manage_cmd.got("confirm", prompt="请输入确认信息:")
async def handle_manage_confirm(matcher: Matcher, event: MessageEvent, confirm: str = ArgPlainText("confirm")):
    user_id = event.get_user_id()
    confirm = confirm.strip().lower()
    next_action = matcher.state.get("next_action")
    account_id = matcher.state.get("selected_account_id")
    token = matcher.state.get("selected_token")
    auth_date = matcher.state.get("selected_auth_date")
    display_phone = matcher.state.get("selected_display_phone")
    accounts: List[str] = matcher.state.get("accounts", [])

    if next_action == "authorize":
        if confirm == 'q':
            await matcher.finish("✅ 已取消授权")

        try:
            months = int(confirm)
            if months <= 0 or months > 999: # 限制一下最大月数
                await matcher.reject("❌ 请输入有效的正整数月数 (1-999):")

            # 计算新授权日期
            new_auth_date = empower(auth_date, months)
            bucket_set('auths', account_id, new_auth_date)
            logger.info(f"胖乖生活: 用户 {user_id} 为账号 {account_id} 授权 {months} 个月，新到期日: {new_auth_date}")

            # 更新青龙变量
            if ql_client:
                phone = bucket_get('mobiles', account_id)
                if phone and token:
                    await ql_client.add_or_update_env(
                        osname=plugin_config.pangguai_osname,
                        value=token,
                        account=account_id,
                        phone=phone,
                        user_id=user_id,
                        auth_date=new_auth_date # 使用新日期更新备注
                    )
                else:
                     logger.warning(f"胖乖生活: 无法为账号 {account_id} 更新青龙变量，缺少 phone 或 token。")

            result_msg = f"""=======授权成功=====
📱 账号: {display_phone}
🎉 授权: {months} 个月
📅 新到期: {new_auth_date}
===================="""
            await matcher.finish(result_msg)

        except ValueError:
            await matcher.reject("❌ 输入无效，请输入数字月数:")

    elif next_action == "delete_confirm":
        if confirm in ['y', '是']:
            # 执行删除
            if account_id in accounts:
                accounts.remove(account_id)
                if not accounts: # 如果列表空了
                    bucket_del('users', user_id)
                else:
                    bucket_set('users', user_id, f'{accounts}')

                # 删除关联数据
                bucket_del('tokens', account_id)
                bucket_del('mobiles', account_id)
                bucket_del('auths', account_id)

                # 删除青龙变量
                if ql_client:
                    envs = await ql_client.get_envs()
                    if envs:
                         ids_to_delete = []
                         for env in envs:
                             if env.get('name') == plugin_config.pangguai_osname and account_id in env.get('remarks', ''):
                                 env_id = env.get('id') or env.get('_id')
                                 if env_id: ids_to_delete.append(env_id)
                         if ids_to_delete:
                             deleted = await ql_client.delete_envs(ids_to_delete)
                             logger.info(f"胖乖生活: 删除账号 {account_id} 的青龙变量 (IDs: {ids_to_delete}): {'成功' if deleted else '失败'}")

                logger.info(f"胖乖生活: 用户 {user_id} 删除了账号 {account_id}")
                await matcher.finish('✅ 账号删除成功!')
            else:
                 await matcher.finish('❌ 错误：账号已不在列表中，可能已被删除。')
        elif confirm in ['n', '否', 'q']:
            await matcher.finish('✅ 已取消删除')
        else:
            await matcher.reject("❌ 输入无效，请输入 [y] 确认删除或 [n] 取消:")

    else: # 不应该发生
        await matcher.finish("❌ 内部状态错误，请重试。")


# 查询命令
query_cmd = on_command("胖乖查询", aliases={"查询胖乖"}, priority=10, block=True)

@query_cmd.handle()
async def handle_query(matcher: Matcher, event: MessageEvent):
    user_id = event.get_user_id()
    user_accounts_str = bucket_get('users', user_id)

    if not user_accounts_str:
        await matcher.finish(f"""=======未绑定账号=====
❌ 未找到任何账号信息
💡 发送 "胖乖登录" 绑定
====================""")

    try:
        accounts = list(dict.fromkeys(eval(user_accounts_str)))
        if not accounts:
            await matcher.finish(f"""=======未绑定账号=====
❌ 账号列表为空
💡 发送 "胖乖登录" 绑定
====================""")
        bucket_set('users', user_id, f'{accounts}')
    except Exception as e:
        logger.error(f"胖乖生活: 解析用户 {user_id} 账号列表失败: {e}")
        await matcher.finish("❌ 处理账号列表时出错，请联系管理员。")
        return

    today_str = get_today_str()
    results = []

    for account in accounts:
        token = bucket_get('tokens', account)
        auth_date = bucket_get('auths', account)
        phone = bucket_get('mobiles', account)
        display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID:{account[:4]}...{account[-4:]}"

        # 检查授权
        is_authorized = False
        auth_display = "⚠️ 未授权或已过期"
        if auth_date:
            try:
                auth_dt = datetime.strptime(auth_date, "%Y-%m-%d").date()
                today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
                if auth_dt >= today_dt:
                    is_authorized = True
                    auth_display = f"✅ {auth_date}"
                else:
                    auth_display = f"❌ {auth_date}" # 过期也显示日期
            except ValueError:
                 auth_display = "❓ 日期无效"

        if not token:
            results.append(f"""=======账号异常=====
📱 账号: {display_phone}
⚠️ 状态: 缺少 Token
💡 建议: 重新登录
====================""")
            continue

        if not is_authorized:
             results.append(f"""=======授权不足=====
📱 账号: {display_phone}
🔐 授权: {auth_display}
💡 建议: 使用 "胖乖管理" 授权
====================""")
             # 即使未授权，也尝试查询信息
             # continue # 如果不想查询未授权的账号，取消这行注释

        # 查询账号信息
        info = await pg_client.get_account_info(token)
        if not info:
            # Token 可能失效
            results.append(f"""=======查询失败=====
📱 账号: {display_phone}
❌ 状态: Token 失效或 API 异常
🔐 授权: {auth_display}
💡 建议: 重新登录
====================""")
            # 可以在这里尝试删除对应的青龙变量
            if ql_client:
                envs = await ql_client.get_envs()
                if envs:
                    ids_to_delete = []
                    for env in envs:
                        if env.get('name') == plugin_config.pangguai_osname and account in env.get('remarks', ''):
                            env_id = env.get('id') or env.get('_id')
                            if env_id: ids_to_delete.append(env_id)
                    if ids_to_delete:
                        deleted = await ql_client.delete_envs(ids_to_delete)
                        logger.info(f"胖乖生活: 查询时发现Token失效，删除账号 {account} 的青龙变量 (IDs: {ids_to_delete}): {'成功' if deleted else '失败'}")
            continue

        account_info_msg = f"""=======账号详情=====
📱 账号: {display_phone}
🎯 总积分: {info.get('integral', 'N/A')}
📈 今日积分: {info.get('today_integral', 'N/A')}
🔐 授权至: {auth_display}
===================="""
        results.append(account_info_msg)

    if not results:
        await matcher.finish("🤔 未查询到任何账号的有效信息。")
    else:
        # 发送多条消息或合并消息
        # 为了避免刷屏，可以考虑合并或使用转发消息 (如果适配器支持)
        full_reply = "\n\n".join(results)
        # 检查消息长度，过长可能需要分段发送
        max_length = 3000 # 示例长度限制，根据平台调整
        if len(full_reply) > max_length:
             await matcher.send("查询结果较多，将分条发送...")
             for res in results:
                 await matcher.send(res)
                 await asyncio.sleep(0.5) # 短暂延时避免速率限制
             await matcher.finish()
        else:
             await matcher.finish(full_reply)

# --- 管理员命令 ---

# 胖乖授权 (管理员)
auth_admin_cmd = on_command("胖乖授权", permission=SUPERUSER, priority=5, block=True)

@auth_admin_cmd.handle()
async def handle_auth_admin_start(matcher: Matcher):
    if not ql_client:
        await matcher.finish("❌ 青龙客户端未初始化，无法执行管理员操作。")

    menu = """=====胖乖授权(管理员)=====
[1] 📱 一键授权所有用户账号
[2] 👤 单独授权指定用户
[3] ⏰ 修改用户账号授权时间
------------------
⚠️ 输入q退出操作
===================="""
    await matcher.send(menu)

@auth_admin_cmd.got("choice", prompt="请选择操作 [1/2/3]:")
async def handle_auth_admin_choice(matcher: Matcher, choice: str = ArgPlainText("choice")):
    choice = choice.strip()
    if choice.lower() == 'q':
        await matcher.finish("✅ 已退出授权")

    if choice == '1':
        matcher.state["admin_action"] = "auth_all"
        await matcher.send("""=======批量授权=====
📝 请输入授权月数:
💡 示例输入: 1
⚠️ 输入q退出操作
====================""" )
    elif choice == '2':
        matcher.state["admin_action"] = "auth_single_user_select"
        await matcher.send("""=======单独授权=====
📝 请输入目标用户ID (通常是QQ号):
⚠️ 输入q退出操作
====================""" )
    elif choice == '3':
        matcher.state["admin_action"] = "modify_time_select_type"
        await matcher.send("""=====修改授权时间=====
[1] 📱 修改所有用户账号
[2] 👤 修改单独用户账号
------------------
⚠️ 输入q退出操作
====================""" )
    else:
        await matcher.reject("❌ 输入无效，请选择 [1/2/3]:")

@auth_admin_cmd.got("input_value", prompt="请输入所需信息:")
async def handle_auth_admin_input(matcher: Matcher, event: MessageEvent, input_value: str = ArgPlainText("input_value")):
    input_value = input_value.strip()
    admin_action = matcher.state.get("admin_action")

    if input_value.lower() == 'q':
        await matcher.finish("✅ 操作已取消")

    # --- 处理批量授权 ---
    if admin_action == "auth_all":
        try:
            months = int(input_value)
            if months <= 0: raise ValueError("月数必须为正")
        except ValueError:
            await matcher.reject("❌ 请输入有效的正整数月数:")
            return # 确保在此处返回，防止继续执行

        all_user_ids = bucket_all_keys('users')
        if not all_user_ids:
            await matcher.finish("ℹ️ 未找到任何已绑定的用户。")
            return

        success_count = 0
        fail_count = 0
        total_accounts = 0
        processed_accounts = set() # 防止重复处理同一账号

        await matcher.send(f"⏳ 开始为 {len(all_user_ids)} 个用户的账号授权 {months} 个月...")

        for user_id in all_user_ids:
            user_accounts_str = bucket_get('users', user_id)
            try:
                accounts = list(dict.fromkeys(eval(user_accounts_str or '[]')))
            except:
                logger.warning(f"胖乖生活: 跳过用户 {user_id}，账号列表解析失败。")
                continue

            for account_id in accounts:
                if account_id in processed_accounts: continue # 跳过已处理
                total_accounts += 1
                processed_accounts.add(account_id)

                token = bucket_get('tokens', account_id)
                if not token:
                    logger.warning(f"胖乖生活: 跳过账号 {account_id} (用户 {user_id})，缺少 Token。")
                    fail_count += 1
                    continue

                current_auth = bucket_get('auths', account_id)
                new_auth_date = empower(current_auth, months)
                bucket_set('auths', account_id, new_auth_date)

                # 更新青龙
                phone = bucket_get('mobiles', account_id)
                if ql_client and phone:
                    updated = await ql_client.add_or_update_env(
                        plugin_config.pangguai_osname, token, account_id, phone, user_id, new_auth_date
                    )
                    if updated:
                        success_count += 1
                    else:
                        logger.warning(f"胖乖生活: 更新账号 {account_id} 的青龙变量失败。")
                        fail_count += 1 # 也算作失败
                else:
                    logger.warning(f"胖乖生活: 跳过账号 {account_id} 的青龙更新 (无 QL Client, phone 或 token)。")
                    success_count += 1 # 即使没更新青龙，本地授权也算成功

        await matcher.finish(f"""=======批量授权完成=====
📊 总账号数: {total_accounts}
✅ 成功处理: {success_count} 个
❌ 处理失败: {fail_count} 个
⏰ 授权时长: {months} 月
====================""")

    # --- 处理选择单独用户 ---
    elif admin_action == "auth_single_user_select":
        target_user_id = input_value
        user_accounts_str = bucket_get('users', target_user_id)
        if not user_accounts_str:
            await matcher.finish(f"❌ 未找到用户 {target_user_id} 的账号信息。")
            return
        try:
            accounts = list(dict.fromkeys(eval(user_accounts_str)))
            if not accounts: await matcher.finish(f"❌ 用户 {target_user_id} 账号列表为空。")
        except:
            await matcher.finish(f"❌ 解析用户 {target_user_id} 账号列表失败。")
            return

        matcher.state["target_user_id"] = target_user_id
        matcher.state["target_accounts"] = accounts

        msg = f"=======用户 {target_user_id} 账号列表=====\n[0] 授权所有账号\n------------------\n"
        for i, account in enumerate(accounts, 1):
            auth_date = bucket_get('auths', account)
            phone = bucket_get('mobiles', account)
            display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID:{account[:4]}...{account[-4:]}"
            vip_status = auth_date if auth_date else '未授权'
            msg += f"[{i}] {display_phone} (授权: {vip_status})\n"
        msg += "------------------\n💡 回复序号选择账号\n⚠️ 输入q退出操作\n===================="

        matcher.state["admin_action"] = "auth_single_account_select" # 下一步是选择账号
        await matcher.send(msg)

    # --- 处理选择单独账号 ---
    elif admin_action == "auth_single_account_select":
        target_user_id = matcher.state.get("target_user_id")
        target_accounts = matcher.state.get("target_accounts", [])
        try:
            choice_int = int(input_value)
            if not (0 <= choice_int <= len(target_accounts)):
                await matcher.reject("❌ 无效的序号，请重新输入:")
                return
        except ValueError:
            await matcher.reject("❌ 请输入数字序号:")
            return

        matcher.state["selected_account_index"] = choice_int # 0 表示所有
        matcher.state["admin_action"] = "auth_single_input_months" # 下一步是输入月数
        prompt_msg = f"=======授权设置=====\n授权对象: {'所有账号' if choice_int == 0 else f'账号序号 [{choice_int}]'}\n📝 请输入授权月数:\n===================="
        await matcher.send(prompt_msg)

    # --- 处理单独授权输入月数 ---
    elif admin_action == "auth_single_input_months":
        target_user_id = matcher.state.get("target_user_id")
        target_accounts = matcher.state.get("target_accounts", [])
        selected_index = matcher.state.get("selected_account_index")

        try:
            months = int(input_value)
            if months <= 0: raise ValueError("月数必须为正")
        except ValueError:
            await matcher.reject("❌ 请输入有效的正整数月数:")
            return

        accounts_to_process = []
        if selected_index == 0:
            accounts_to_process = target_accounts
        else:
            accounts_to_process = [target_accounts[selected_index - 1]]

        success_count = 0
        processed_accounts_info = []

        for account_id in accounts_to_process:
            token = bucket_get('tokens', account_id)
            if not token:
                logger.warning(f"胖乖生活(管理员): 跳过账号 {account_id} (用户 {target_user_id})，缺少 Token。")
                continue

            current_auth = bucket_get('auths', account_id)
            new_auth_date = empower(current_auth, months)
            bucket_set('auths', account_id, new_auth_date)

            phone = bucket_get('mobiles', account_id)
            if ql_client and phone:
                await ql_client.add_or_update_env(
                    plugin_config.pangguai_osname, token, account_id, phone, target_user_id, new_auth_date
                )

            display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID:{account_id[:4]}...{account_id[-4:]}"
            processed_accounts_info.append(f"• {display_phone} -> {new_auth_date}")
            success_count += 1

        result_msg = f"""=======授权完成=====
👤 用户: {target_user_id}
✅ 成功授权 {success_count} 个账号:
{"换行".join(processed_accounts_info)}
⏰ 授权时长: {months} 月
====================""" # 换行符可能在不同平台显示不同，可能需要调整
        await matcher.finish(result_msg)

    # --- 处理修改时间 - 选择类型 ---
    elif admin_action == "modify_time_select_type":
        if input_value == '1':
            matcher.state["admin_action"] = "modify_time_all_input_days"
            await matcher.send("""=======批量修改=====
📝 请输入调整天数:
💡 正数增加, 负数减少
⚠️ 示例: 30 或 -30
====================""" )
        elif input_value == '2':
            matcher.state["admin_action"] = "modify_time_single_user_select"
            await matcher.send("""=======单独修改=====
📝 请输入目标用户ID (通常是QQ号):
⚠️ 输入q退出操作
====================""" )
        else:
             await matcher.reject("❌ 请输入 [1] 或 [2]:")

    # --- 处理修改时间 - 所有用户输入天数 ---
    elif admin_action == "modify_time_all_input_days":
        try:
            days = int(input_value)
        except ValueError:
            await matcher.reject("❌ 请输入有效的整数天数:")
            return

        all_user_ids = bucket_all_keys('users')
        if not all_user_ids: await matcher.finish("ℹ️ 未找到任何已绑定的用户。")

        success_count = 0
        fail_count = 0
        total_accounts = 0
        processed_accounts = set()
        today_dt = datetime.now().date()

        await matcher.send(f"⏳ 开始为 {len(all_user_ids)} 个用户的账号调整 {days} 天...")

        for user_id in all_user_ids:
            user_accounts_str = bucket_get('users', user_id)
            try: accounts = list(dict.fromkeys(eval(user_accounts_str or '[]')))
            except: continue

            for account_id in accounts:
                if account_id in processed_accounts: continue
                total_accounts += 1
                processed_accounts.add(account_id)

                token = bucket_get('tokens', account_id)
                current_auth = bucket_get('auths', account_id)

                try:
                    if not current_auth: current_date = today_dt # 无授权日期则从今天算
                    else: current_date = datetime.strptime(current_auth, "%Y-%m-%d").date()
                    new_date = current_date + timedelta(days=days)
                    new_auth_str = str(new_date)
                    bucket_set('auths', account_id, new_auth_str)

                    if token: # 只有存在 token 时才尝试更新青龙
                        phone = bucket_get('mobiles', account_id)
                        if ql_client and phone:
                             await ql_client.add_or_update_env(
                                plugin_config.pangguai_osname, token, account_id, phone, user_id, new_auth_str
                            )
                    success_count += 1
                except Exception as e:
                    logger.error(f"胖乖生活: 修改账号 {account_id} 时间失败: {e}")
                    fail_count += 1

        await matcher.finish(f"""=======批量修改完成=====
📊 总账号数: {total_accounts}
✅ 成功处理: {success_count} 个
❌ 处理失败: {fail_count} 个
📅 调整天数: {days} 天
====================""")

    # --- 处理修改时间 - 选择单独用户 ---
    elif admin_action == "modify_time_single_user_select":
        target_user_id = input_value
        user_accounts_str = bucket_get('users', target_user_id)
        if not user_accounts_str: await matcher.finish(f"❌ 未找到用户 {target_user_id} 的账号信息。")
        try:
            accounts = list(dict.fromkeys(eval(user_accounts_str)))
            if not accounts: await matcher.finish(f"❌ 用户 {target_user_id} 账号列表为空。")
        except: await matcher.finish(f"❌ 解析用户 {target_user_id} 账号列表失败。")

        matcher.state["target_user_id"] = target_user_id
        matcher.state["target_accounts"] = accounts

        msg = f"=======用户 {target_user_id} 账号列表=====\n[0] 修改所有账号\n------------------\n"
        for i, account in enumerate(accounts, 1):
            auth_date = bucket_get('auths', account)
            phone = bucket_get('mobiles', account)
            display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID:{account[:4]}...{account[-4:]}"
            vip_status = auth_date if auth_date else '未授权'
            msg += f"[{i}] {display_phone} (授权: {vip_status})\n"
        msg += "------------------\n💡 回复序号选择账号\n⚠️ 输入q退出操作\n===================="

        matcher.state["admin_action"] = "modify_time_single_account_select"
        await matcher.send(msg)

    # --- 处理修改时间 - 选择单独账号 ---
    elif admin_action == "modify_time_single_account_select":
        target_user_id = matcher.state.get("target_user_id")
        target_accounts = matcher.state.get("target_accounts", [])
        try:
            choice_int = int(input_value)
            if not (0 <= choice_int <= len(target_accounts)):
                await matcher.reject("❌ 无效的序号，请重新输入:")
                return
        except ValueError:
            await matcher.reject("❌ 请输入数字序号:")
            return

        matcher.state["selected_account_index"] = choice_int # 0 表示所有
        matcher.state["admin_action"] = "modify_time_single_input_days"
        prompt_msg = f"=======时间调整=====\n调整对象: {'所有账号' if choice_int == 0 else f'账号序号 [{choice_int}]'}\n📝 请输入调整天数:\n💡 正数增加, 负数减少\n===================="
        await matcher.send(prompt_msg)

    # --- 处理修改时间 - 单独用户输入天数 ---
    elif admin_action == "modify_time_single_input_days":
        target_user_id = matcher.state.get("target_user_id")
        target_accounts = matcher.state.get("target_accounts", [])
        selected_index = matcher.state.get("selected_account_index")
        today_dt = datetime.now().date()

        try:
            days = int(input_value)
        except ValueError:
            await matcher.reject("❌ 请输入有效的整数天数:")
            return

        accounts_to_process = []
        if selected_index == 0: accounts_to_process = target_accounts
        else: accounts_to_process = [target_accounts[selected_index - 1]]

        success_count = 0
        processed_accounts_info = []

        for account_id in accounts_to_process:
            token = bucket_get('tokens', account_id) # 需要 token 来更新青龙
            current_auth = bucket_get('auths', account_id)

            try:
                if not current_auth: current_date = today_dt
                else: current_date = datetime.strptime(current_auth, "%Y-%m-%d").date()
                new_date = current_date + timedelta(days=days)
                new_auth_str = str(new_date)
                bucket_set('auths', account_id, new_auth_str)

                if token:
                    phone = bucket_get('mobiles', account_id)
                    if ql_client and phone:
                         await ql_client.add_or_update_env(
                            plugin_config.pangguai_osname, token, account_id, phone, target_user_id, new_auth_str
                        )

                phone = bucket_get('mobiles', account_id) # 再次获取以显示
                display_phone = f"{phone[:3]}****{phone[7:]}" if phone else f"账号ID:{account_id[:4]}...{account_id[-4:]}"
                processed_accounts_info.append(f"• {display_phone} -> {new_auth_str}")
                success_count += 1
            except Exception as e:
                logger.error(f"胖乖生活: 修改账号 {account_id} 时间失败: {e}")

        result_msg = f"""=======修改完成=====
👤 用户: {target_user_id}
✅ 成功修改 {success_count} 个账号:
{"换行".join(processed_accounts_info)}
📅 调整天数: {days} 天
===================="""
        await matcher.finish(result_msg)

    else: # 未知状态
        await matcher.finish("❌ 未知操作状态，请重试。")


# 清理过期账号 (管理员)
clean_cmd = on_command("胖乖清理", aliases={"清理胖乖"}, permission=SUPERUSER, priority=5, block=True)

@clean_cmd.handle()
async def handle_clean_start(matcher: Matcher):
    if not ql_client:
        await matcher.finish("❌ 青龙客户端未初始化，无法执行清理操作。")

    await matcher.send("""=======清理确认=====
⚠️ 即将清理所有授权过期/未授权的账号
⚠️ 同时会删除关联的青龙变量
⚠️ 此操作不可恢复
------------------
[y] 确认清理
[n] 取消操作
====================""" )

@clean_cmd.got("confirm", prompt="请确认 [y/n]:")
async def handle_clean_confirm(matcher: Matcher, confirm: str = ArgPlainText("confirm")):
    confirm = confirm.strip().lower()
    if confirm not in ['y', '是']:
        await matcher.finish("✅ 已取消清理")

    await matcher.send("⏳ 开始清理过期账号...")

    all_user_ids = bucket_all_keys('users')
    today_str = get_today_str()
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()

    total_accounts = 0
    expired_accounts = 0
    cleaned_accounts = 0
    accounts_to_delete_env = {} # {account_id: phone or None}

    # 遍历用户和账号，识别过期账号
    for user_id in all_user_ids:
        user_accounts_str = bucket_get('users', user_id)
        valid_accounts = []
        try:
            accounts = list(dict.fromkeys(eval(user_accounts_str or '[]')))
        except: continue # 跳过解析失败的用户

        for account_id in accounts:
            total_accounts += 1
            auth_date = bucket_get('auths', account_id)
            is_expired = True # 默认未授权算过期
            if auth_date:
                try:
                    auth_dt = datetime.strptime(auth_date, "%Y-%m-%d").date()
                    if auth_dt >= today_dt:
                        is_expired = False
                except ValueError: pass # 无效日期也算过期

            if is_expired:
                expired_accounts += 1
                phone = bucket_get('mobiles', account_id)
                accounts_to_delete_env[account_id] = phone

                # 删除本地数据
                bucket_del('tokens', account_id)
                bucket_del('mobiles', account_id)
                bucket_del('auths', account_id)
                cleaned_accounts += 1
                logger.info(f"胖乖生活(清理): 标记账号 {account_id} (用户 {user_id}) 为过期并删除本地数据。")
            else:
                valid_accounts.append(account_id) # 保留未过期的

        # 更新用户账号列表
        if not valid_accounts: bucket_del('users', user_id)
        else: bucket_set('users', user_id, f'{valid_accounts}')

    # 批量删除青龙变量
    cleaned_vars = 0
    if accounts_to_delete_env and ql_client:
        logger.info(f"胖乖生活(清理): 准备删除 {len(accounts_to_delete_env)} 个过期账号的青龙变量...")
        envs = await ql_client.get_envs() # 获取所有变量
        if envs is not None: # 确保获取成功
            ids_to_delete = []
            processed_envs = set() # 防止重复添加ID

            for env in envs:
                 env_id = env.get('id') or env.get('_id')
                 if not env_id or env_id in processed_envs: continue
                 if env.get('name') != plugin_config.pangguai_osname: continue

                 remarks = env.get('remarks', '')
                 # 检查备注是否匹配任何一个待删除的账号ID或手机号
                 for acc_id, phone in accounts_to_delete_env.items():
                      if acc_id in remarks or (phone and f'胖乖:{phone}' in remarks):
                           ids_to_delete.append(env_id)
                           processed_envs.add(env_id)
                           logger.info(f"胖乖生活(清理): 找到账号 {acc_id} 对应的青龙变量 ID: {env_id}")
                           break # 找到匹配就处理下一个环境变量

            if ids_to_delete:
                deleted = await ql_client.delete_envs(ids_to_delete)
                if deleted:
                    cleaned_vars = len(ids_to_delete)
                    logger.info(f"胖乖生活(清理): 成功删除 {cleaned_vars} 个青龙变量。")
                else:
                    logger.error("胖乖生活(清理): 批量删除青龙变量失败。")
            else:
                 logger.info("胖乖生活(清理): 未找到需要删除的青龙变量。")
        else:
             logger.error("胖乖生活(清理): 获取青龙变量列表失败，无法删除变量。")


    result_msg = f"""=======清理完成=====
📊 统计信息:
• 总账号数: {total_accounts}
• 过期/未授权: {expired_accounts}
• 清理账号(本地): {cleaned_accounts}
• 清理变量(青龙): {cleaned_vars}
===================="""
    await matcher.finish(result_msg)


# --- 定时任务 ---
try:
    scheduler = require("nonebot_plugin_apscheduler").scheduler
except ImportError:
    logger.warning("胖乖生活: 未安装 nonebot_plugin_apscheduler, 定时检查任务将不会运行。")
    scheduler = None

# 原 cron: 18 8,12,16 * * * (每天 8:18, 12:18, 16:18)
@scheduler.scheduled_job("cron", hour="8,12,16", minute=18, id="pangguai_daily_check", misfire_grace_time=60)
async def scheduled_check():
    logger.info("胖乖生活: 开始执行定时检查任务...")
    if not ql_client:
        logger.warning("胖乖生活: 定时检查跳过，青龙客户端未初始化。")
        return

    all_user_ids = bucket_all_keys('users')
    today_str = get_today_str()
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    checked_accounts = set()
    all_envs = await ql_client.get_envs() # 获取一次所有变量，减少 API 调用

    for user_id in all_user_ids:
        user_accounts_str = bucket_get('users', user_id)
        try: accounts = list(dict.fromkeys(eval(user_accounts_str or '[]')))
        except: continue

        for account_id in accounts:
            if account_id in checked_accounts: continue
            checked_accounts.add(account_id)

            token = bucket_get('tokens', account_id)
            auth_date = bucket_get('auths', account_id)

            # 1. 检查 Token 有效性
            if not token:
                await send_push_notification(user_id, account_id, "Token 丢失，请重新登录。")
                continue # Token 丢失无法继续检查

            verify_result = await pg_client.verify_token(token)
            if not verify_result:
                logger.warning(f"胖乖生活(定时): 账号 {account_id} (用户 {user_id}) Token 失效。")
                await send_push_notification(user_id, account_id, "Token 已失效，请重新登录。")
                # 删除对应的青龙变量
                if all_envs is not None:
                    ids_to_delete = []
                    for env in all_envs:
                        if env.get('name') == plugin_config.pangguai_osname and account_id in env.get('remarks', ''):
                             env_id = env.get('id') or env.get('_id')
                             if env_id: ids_to_delete.append(env_id)
                    if ids_to_delete:
                        deleted = await ql_client.delete_envs(ids_to_delete)
                        logger.info(f"胖乖生活(定时): 因Token失效删除账号 {account_id} 的青龙变量 (IDs: {ids_to_delete}): {'成功' if deleted else '失败'}")
                continue # Token 失效，后续检查无意义

            # 2. 检查授权状态
            is_authorized = False
            if auth_date:
                try:
                    auth_dt = datetime.strptime(auth_date, "%Y-%m-%d").date()
                    if auth_dt >= today_dt:
                        is_authorized = True
                    else:
                         # 授权已过期
                         logger.warning(f"胖乖生活(定时): 账号 {account_id} (用户 {user_id}) 授权已于 {auth_date} 过期。")
                         await send_push_notification(user_id, account_id, f"授权已于 {auth_date} 过期，请及时续费。")
                         # 删除对应的青龙变量
                         if all_envs is not None:
                            ids_to_delete = []
                            for env in all_envs:
                                if env.get('name') == plugin_config.pangguai_osname and account_id in env.get('remarks', ''):
                                     env_id = env.get('id') or env.get('_id')
                                     if env_id: ids_to_delete.append(env_id)
                            if ids_to_delete:
                                deleted = await ql_client.delete_envs(ids_to_delete)
                                logger.info(f"胖乖生活(定时): 因授权过期删除账号 {account_id} 的青龙变量 (IDs: {ids_to_delete}): {'成功' if deleted else '失败'}")

                except ValueError:
                     logger.warning(f"胖乖生活(定时): 账号 {account_id} (用户 {user_id}) 授权日期 '{auth_date}' 格式无效。")
                     # 视为未授权处理

            if not is_authorized and auth_date: # 仅在日期有效但过期时上面已发通知，这里处理从未授权或日期无效的情况
                if not auth_date: # 从未授权过
                     logger.warning(f"胖乖生活(定时): 账号 {account_id} (用户 {user_id}) 未授权。")
                     # 可以选择性发送通知
                     # await send_push_notification(user_id, account_id, "账号尚未授权，请使用 胖乖管理 进行授权。")
                # 如果日期无效，上面已有日志，这里不再重复

            # 3. (可选) 可以在这里添加其他检查，例如调用 cx 查询积分等

    logger.info("胖乖生活: 定时检查任务完成。")

# --- 启动时检查 QL 连接 ---
@driver.on_startup
async def check_ql_on_startup():
    logger.info("胖乖生活: 正在尝试连接青龙...")
    if ql_client:
        token = await ql_client._get_token()
        if token:
            logger.info("胖乖生活: 青龙连接成功！")
        else:
            logger.error("胖乖生活: 启动时连接青龙失败，请检查配置！")
    else:
        logger.error("胖乖生活: 青龙客户端未正确初始化，请检查配置！")

# --- 导入 asyncio (如果在消息分段发送处用到) ---
import asyncio
