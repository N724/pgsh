import aiohttp
import logging
import json
import time
import hashlib
from urllib.parse import urlparse, quote
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

# Astrbot API imports
from astrbot.api.all import AstrMessageEvent, CommandResult, Context, Plain
import astrbot.api.event.filter as filter
from astrbot.api.star import register, Star

logger = logging.getLogger("astrbot_pangguai") # Use a specific logger name

# --- Hardcoded Configuration ---
# Qinglong Panel Configuration
QL_URL = "http://ql.wzhy99.top"  # Replace with your Qinglong URL (e.g., http://192.168.1.100:5700)
QL_CLIENT_ID = "dc_kbN4Ddw2m"  # Replace with your Qinglong App Client ID
QL_CLIENT_SECRET = "RqgmCGuiuIUe8rGJT82k-z0b" # Replace with your Qinglong App Client Secret
QL_ENV_NAME = "pangguai" # Name of the environment variable in Qinglong

# PangGuai API Configuration (usually fixed)
PG_APP_SECRET = "xl8v4s/5qpBLvN+8CzFx7vVjy31NgXXcedU7G0QpOMM="
PG_API_BASE = "https://userapi.qiekj.com"
PG_USER_AGENT = "okhttp/3.14.9"
PG_VERSION = "1.57.0"
PG_CHANNEL = "android_app"
# --- End Configuration ---

@register(
    id="pangguai_helper",
    author="Adapted from linzixuan",
    name="胖乖生活助手",
    version="1.1.0",
    desc="通过短信登录胖乖生活并将Token同步至青龙面板"
)
class PangGuaiPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.session: aiohttp.ClientSession = self.context.get_client_session() # Use shared session
        self.ql_token: Optional[str] = None
        self.ql_token_expiry: float = 0 # Timestamp when token expires
        self.timeout = aiohttp.ClientTimeout(total=20) # Define timeout for requests

    # --- Helper Functions ---
    def _get_timestamp_ms(self) -> int:
        """Generates a 13-digit millisecond timestamp."""
        return int(time.time() * 1000)

    def _calculate_pg_sign(self, timestamp_ms: int, token: str, url_path: str) -> str:
        """Calculates the SHA256 signature for PangGuai API."""
        data = f'appSecret={PG_APP_SECRET}&channel={PG_CHANNEL}×tamp={timestamp_ms}&token={token}&version={PG_VERSION}&{url_path}'
        sha256_hash = hashlib.sha256()
        sha256_hash.update(data.encode('utf-8'))
        return sha256_hash.hexdigest()

    async def _get_pg_headers(self, token: str = "", url_path: str = "") -> Dict[str, str]:
        """Constructs headers for PangGuai API requests."""
        timestamp_ms = self._get_timestamp_ms()
        sign = self._calculate_pg_sign(timestamp_ms, token, url_path)
        return {
            'User-Agent': PG_USER_AGENT,
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Content-Type': "application/x-www-form-urlencoded",
            'Authorization': f"{token}", # Needs token even if empty string
            'Version': PG_VERSION,
            'channel': PG_CHANNEL,
            'phoneBrand': "astrbot", # Generic identifier
            'timestamp': f"{timestamp_ms}",
            'sign': f"{sign}",
        }

    # --- Qinglong API Functions ---
    async def _get_ql_token(self) -> Optional[str]:
        """Gets or refreshes the Qinglong API token."""
        if self.ql_token and time.time() < self.ql_token_expiry:
            return self.ql_token

        if not QL_URL or not QL_CLIENT_ID or not QL_CLIENT_SECRET:
            logger.error("Qinglong URL, Client ID, or Client Secret is not configured.")
            return None

        url = f"{QL_URL}/open/auth/token?client_id={QL_CLIENT_ID}&client_secret={QL_CLIENT_SECRET}"
        try:
            async with self.session.get(url, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200 and "token" in data.get("data", {}):
                        self.ql_token = data["data"]["token"]
                        # Assume token lasts reasonably long, e.g., 24 hours (adjust if needed)
                        self.ql_token_expiry = time.time() + 86000 # Cache for slightly less than 24h
                        logger.info("Successfully obtained Qinglong token.")
                        return self.ql_token
                    else:
                        logger.error(f"Failed to get Qinglong token: {data.get('message', 'Unknown error')}")
                        return None
                else:
                    logger.error(f"Qinglong token request failed with status: {resp.status}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"Network error getting Qinglong token: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting Qinglong token: {e}", exc_info=True)
            return None

    async def _find_ql_env(self, phone: str) -> Optional[str]:
        """Finds the Qinglong environment variable ID based on phone number in remarks."""
        token = await self._get_ql_token()
        if not token:
            return None

        url = f"{QL_URL}/open/envs"
        headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
        params = {"searchValue": phone} # Search directly if API supports it, otherwise filter locally

        try:
            async with self.session.get(url, headers=headers, params=params, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        envs = data.get("data", [])
                        for env in envs:
                            # Ensure it's the correct variable name and remark contains the phone
                            if env.get("name") == QL_ENV_NAME and phone in env.get("remarks", ""):
                                return env.get("id") # Found Qinglong >= 2.11 'id'
                                # For older Qinglong, it might be '_id'
                                # return env.get("id") or env.get("_id")
                        logger.debug(f"No existing env found for phone {phone} with name {QL_ENV_NAME}")
                        return None
                    else:
                        logger.error(f"Failed to list Qinglong envs: {data.get('message', 'Unknown error')}")
                        return None
                else:
                    logger.error(f"Qinglong list envs request failed: {resp.status}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"Network error finding Qinglong env: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error finding Qinglong env: {e}", exc_info=True)
            return None

    async def _update_ql_env(self, id: str, value: str, phone: str) -> bool:
        """Updates an existing Qinglong environment variable."""
        token = await self._get_ql_token()
        if not token:
            return False

        url = f"{QL_URL}/open/envs"
        headers = {
            "Authorization": f"Bearer {token}",
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = {
            "value": value,
            "name": QL_ENV_NAME,
            "remarks": f'胖乖:{phone}丨astrbot管理', # Simple remark
            "id": id # Use 'id' for Qinglong >= 2.11
            # For older Qinglong, it might be '_id'
            # "_id": id
        }
        try:
            async with self.session.put(url, headers=headers, json=payload, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        logger.info(f"Successfully updated Qinglong env for phone {phone}")
                        return True
                    else:
                        logger.error(f"Failed to update Qinglong env: {data.get('message', 'Unknown error')}")
                        return False
                else:
                    logger.error(f"Qinglong update env request failed: {resp.status}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"Network error updating Qinglong env: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating Qinglong env: {e}", exc_info=True)
            return False

    async def _add_ql_env(self, value: str, phone: str) -> bool:
        """Adds a new Qinglong environment variable."""
        token = await self._get_ql_token()
        if not token:
            return False

        url = f"{QL_URL}/open/envs"
        headers = {
            "Authorization": f"Bearer {token}",
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = [{
            "value": value,
            "name": QL_ENV_NAME,
            "remarks": f'胖乖:{phone}丨astrbot管理' # Simple remark
        }]
        try:
            async with self.session.post(url, headers=headers, json=payload, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Check for uniqueness constraint error
                    if "value must be unique" in await resp.text():
                         logger.warning(f"Qinglong env for phone {phone} likely already exists (unique constraint).")
                         # Consider trying an update or informing user
                         return False # Indicate potential issue
                    if data.get("code") == 200 and data.get("data"):
                        logger.info(f"Successfully added Qinglong env for phone {phone}")
                        return True
                    else:
                        logger.error(f"Failed to add Qinglong env: {data.get('message', 'Unknown error')}")
                        return False
                else:
                    logger.error(f"Qinglong add env request failed: {resp.status}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"Network error adding Qinglong env: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error adding Qinglong env: {e}", exc_info=True)
            return False

    async def _sync_to_ql(self, pg_token: str, phone: str) -> bool:
        """Adds or updates the PangGuai token in Qinglong."""
        env_id = await self._find_ql_env(phone)
        value_to_set = quote(pg_token) # URL Encode the token

        if env_id:
            logger.info(f"Found existing env (ID: {env_id}) for phone {phone}. Updating.")
            return await self._update_ql_env(env_id, value_to_set, phone)
        else:
            logger.info(f"No existing env found for phone {phone}. Adding new.")
            return await self._add_ql_env(value_to_set, phone)

    # --- PangGuai API Functions ---
    async def _pg_send_sms(self, phone: str) -> bool:
        """Sends SMS verification code via PangGuai API."""
        url_path = "/common/sms/sendCode"
        url = f"{PG_API_BASE}{url_path}"
        headers = await self._get_pg_headers(url_path=url_path)
        payload = f"phone={phone}&template=reg"

        try:
            async with self.session.post(url, headers=headers, data=payload, timeout=self.timeout) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get('code') == 0 and result.get('msg') == '成功':
                        logger.info(f"Successfully requested SMS code for {phone}")
                        return True
                    else:
                        error_msg = result.get('msg', 'Unknown error')
                        logger.error(f"Failed to request SMS code for {phone}: {error_msg}")
                        return False
                else:
                    logger.error(f"PangGuai SMS request failed: {resp.status}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"Network error requesting PangGuai SMS: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error requesting PangGuai SMS: {e}", exc_info=True)
            return False

    async def _pg_sms_login(self, phone: str, code: str) -> Optional[str]:
        """Logs in using phone and SMS code, returns token if successful."""
        url_path = "/user/reg"
        url = f"{PG_API_BASE}{url_path}"
        headers = await self._get_pg_headers(url_path=url_path)
        payload = f"channel=h5&phone={phone}&verify={code}"

        try:
            async with self.session.post(url, headers=headers, data=payload, timeout=self.timeout) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get('code') == 0 and 'token' in result.get('data', {}):
                        token = result['data']['token']
                        logger.info(f"Successfully logged in via SMS for {phone}")
                        # Optionally verify token immediately - good practice
                        if await self._pg_verify_token(token):
                            return token
                        else:
                            logger.warning(f"Login successful but immediate token verification failed for {phone}")
                            return None # Treat as failure if verification fails
                    else:
                        error_msg = result.get('msg', 'Login failed')
                        logger.error(f"PangGuai SMS login failed for {phone}: {error_msg}")
                        return None
                else:
                    logger.error(f"PangGuai SMS login request failed: {resp.status}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"Network error during PangGuai SMS login: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during PangGuai SMS login: {e}", exc_info=True)
            return None

    async def _pg_verify_token(self, token: str) -> bool:
        """Verifies if a PangGuai token is valid by fetching user info."""
        url_path = "/user/info"
        url = f"{PG_API_BASE}{url_path}"
        headers = await self._get_pg_headers(token=token, url_path=url_path)
        payload = f"token={token}" # API might require token in body too

        try:
            async with self.session.post(url, headers=headers, data=payload, timeout=self.timeout) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get('code') == 0 and result.get('msg') == '成功' and 'data' in result:
                         logger.debug(f"Token verified successfully for user ID: {result['data'].get('id')}")
                         return True
                    else:
                         logger.warning(f"Token verification failed: {result.get('msg', 'API error')}")
                         return False
                else:
                    logger.warning(f"Token verification request failed: {resp.status}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"Network error verifying token: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error verifying token: {e}", exc_info=True)
            return False

    async def _pg_query_info(self, token: str) -> Optional[Dict]:
        """Queries PangGuai account balance and integral info."""
        if not await self._pg_verify_token(token): # Ensure token is valid before querying
             return None

        url_path = "/user/balance"
        url = f"{PG_API_BASE}{url_path}"
        headers = await self._get_pg_headers(token=token, url_path=url_path)
        payload = f"token={token}"

        try:
            # Query balance/total integral
            async with self.session.post(url, headers=headers, data=payload, timeout=self.timeout) as resp:
                if resp.status != 200:
                     logger.error(f"PangGuai balance query failed: {resp.status}")
                     return None
                balance_data = await resp.json()
                if not (balance_data.get('code') == 0 and 'data' in balance_data):
                     logger.error(f"PangGuai balance query error: {balance_data.get('msg')}")
                     return None

                # Query integral records for today's integral (more complex)
                # Replicating the original FormData request with aiohttp requires careful construction
                # For simplicity, let's skip the "today's integral" part in this adaptation.
                # If needed, use aiohttp.FormData() and pass it to session.post(data=...)

                return {
                    'balance': balance_data['data'].get('balance', 'N/A'),
                    'integral': balance_data['data'].get('integral', 'N/A'),
                    'today_integral': 'N/A' # Skipped for simplicity
                }

        except aiohttp.ClientError as e:
            logger.error(f"Network error during PangGuai query: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during PangGuai query: {e}", exc_info=True)
            return None


    # --- Command Handlers ---
    @filter.command("胖乖登录", r"胖乖登录\s+(\d{11})\s+(\d{4,6})") # Expects /胖乖登录 <phone> <code>
    async def pangguai_login_cmd(self, event: AstrMessageEvent, match: re.Match):
        '''胖乖登录: 使用手机号和验证码登录并将Token同步至青龙。\n格式: /胖乖登录 <手机号> <验证码>'''
        phone = match.group(1)
        code = match.group(2)

        yield CommandResult().message(f"🔄 正在使用手机号 {phone[:3]}****{phone[7:]} 和验证码登录...")

        pg_token = await self._pg_sms_login(phone, code)

        if not pg_token:
            yield CommandResult().error("❌ 登录失败，请检查手机号和验证码是否正确，或查看日志。")
            return

        yield CommandResult().message("✅ 登录成功！正在将Token同步至青龙...")

        sync_success = await self._sync_to_ql(pg_token, phone)

        if sync_success:
            yield CommandResult().message(f"🎉 成功将手机号 {phone[:3]}****{phone[7:]} 的Token同步至青龙！")
        else:
            yield CommandResult().error("⚠️ 登录成功，但同步Token至青龙失败。请检查青龙配置和网络连接，或查看日志。")

    @filter.command("胖乖发码", r"胖乖发码\s+(\d{11})") # Expects /胖乖发码 <phone>
    async def pangguai_send_code_cmd(self, event: AstrMessageEvent, match: re.Match):
        '''胖乖发码: 向指定手机号发送登录验证码。\n格式: /胖乖发码 <手机号>'''
        phone = match.group(1)

        yield CommandResult().message(f"📨 正在向手机号 {phone[:3]}****{phone[7:]} 发送验证码...")

        success = await self._pg_send_sms(phone)

        if success:
            yield CommandResult().message(f"✅ 验证码已发送至 {phone[:3]}****{phone[7:]}，请查收。\n"
                                         f"收到后请使用 `/胖乖登录 {phone} <验证码>` 进行登录。")
        else:
            yield CommandResult().error("❌ 发送验证码失败，请稍后再试或检查日志。")

    @filter.command("胖乖查询", r"胖乖查询\s+(\d{11})") # Expects /胖乖查询 <phone>
    async def pangguai_query_cmd(self, event: AstrMessageEvent, match: re.Match):
        '''胖乖查询: 查询指定手机号关联账号的信息 (需要先登录同步过Token)。\n格式: /胖乖查询 <手机号>'''
        phone = match.group(1)

        yield CommandResult().message(f"🔍 正在查询手机号 {phone[:3]}****{phone[7:]} 的青龙变量...")

        # 1. Find the variable in Qinglong to get the token
        token = await self._get_ql_token()
        if not token:
             yield CommandResult().error("❌ 无法连接到青龙或获取Token，查询失败。")
             return

        url = f"{QL_URL}/open/envs"
        headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
        params = {"searchValue": phone} # Use search value

        pg_token_encoded = None
        try:
            async with self.session.get(url, headers=headers, params=params, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        envs = data.get("data", [])
                        for env in envs:
                            if env.get("name") == QL_ENV_NAME and phone in env.get("remarks", ""):
                                pg_token_encoded = env.get("value")
                                break # Found it
                        if not pg_token_encoded:
                            yield CommandResult().error(f"❌ 未在青龙中找到与手机号 {phone[:3]}****{phone[7:]} 关联的 {QL_ENV_NAME} 变量。请先登录。")
                            return
                    else:
                         yield CommandResult().error(f"❌ 查询青龙变量失败: {data.get('message', 'API Error')}")
                         return
                else:
                     yield CommandResult().error(f"❌ 查询青龙变量请求失败: HTTP {resp.status}")
                     return
        except aiohttp.ClientError as e:
             logger.error(f"Network error finding QL env for query: {e}")
             yield CommandResult().error(f"❌ 查询青龙变量时网络错误: {e}")
             return
        except Exception as e:
             logger.error(f"Unexpected error finding QL env for query: {e}", exc_info=True)
             yield CommandResult().error(f"❌ 查询青龙变量时发生意外错误: {e}")
             return

        # 2. Decode token and query PangGuai API
        try:
            from urllib.parse import unquote
            pg_token = unquote(pg_token_encoded)
        except Exception:
             yield CommandResult().error("❌ 从青龙获取的Token格式错误，无法解析。")
             return

        yield CommandResult().message(f"ℹ️ 正在使用获取到的Token查询胖乖信息...")
        info = await self._pg_query_info(pg_token)

        if info:
            display_phone = f"{phone[:3]}****{phone[7:]}"
            query_result = (
                f"=======胖乖账号信息=======\n"
                f"📱 手机号: {display_phone}\n"
                # f"💰 余额: {info.get('balance', 'N/A')}\n" # Balance might not be relevant
                f"🎯 总积分: {info.get('integral', 'N/A')}\n"
                # f"📈 今日积分: {info.get('today_integral', 'N/A')}\n" # Skipped today's integral
                f"========================"
            )
            yield CommandResult().message(query_result)
        else:
            yield CommandResult().error(f"❌ 查询胖乖信息失败。可能是Token已失效或API错误。请尝试重新登录。")


    @filter.command("胖乖帮助")
    async def pangguai_help_cmd(self, event: AstrMessageEvent):
        '''显示胖乖生活助手帮助信息'''
        help_msg = [
            "📘 胖乖生活助手 - astrbot",
            "━" * 20,
            "功能: 通过短信验证码登录胖乖生活，并将获取到的Token自动同步到你的青龙面板。",
            "",
            "指令说明:",
            "1️⃣ `/胖乖发码 <手机号>`",
            "   - 向指定手机号发送登录验证码。",
            "   - 示例: `/胖乖发码 13800138000`",
            "",
            "2️⃣ `/胖乖登录 <手机号> <验证码>`",
            "   - 使用收到的验证码完成登录，并同步Token到青龙。",
            "   - 示例: `/胖乖登录 13800138000 123456`",
            "",
            "3️⃣ `/胖乖查询 <手机号>`",
            "   - 查询已同步到青龙的账号的胖乖积分信息。",
            "   - (需要先成功执行过登录指令)",
            "   - 示例: `/胖乖查询 13800138000`",
             "",
            "4️⃣ `/胖乖帮助`",
            "   - 显示此帮助信息。",
            "━" * 20,
            "⚠️ 注意: 请确保插件代码中的青龙配置 (URL, Client ID, Client Secret, Env Name) 正确无误。"
        ]
        yield CommandResult().message("\n".join(help_msg))
