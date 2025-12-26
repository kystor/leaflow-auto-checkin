#!/usr/bin/env python3
"""
Leaflow 多账号自动签到脚本
变量名：LEAFLOW_ACCOUNTS
变量值：邮箱1:密码1,邮箱2:密码2,邮箱3:密码3

可选环境变量：
- LEAFLOW_STATE_DIR: 保存 cookies/localStorage 的目录（默认 ./leaflow_state）
"""

import os
import time
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException


# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class LeaflowAutoCheckin:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')

        if not self.email or not self.password:
            raise ValueError("邮箱和密码不能为空")

        # ====== B 方案：登录态持久化（按账号隔离）======
        self.state_dir = Path(os.getenv("LEAFLOW_STATE_DIR", "./leaflow_state"))
        self.state_dir.mkdir(parents=True, exist_ok=True)

        key = hashlib.sha256(self.email.encode("utf-8")).hexdigest()[:16]
        self.cookies_path = self.state_dir / f"cookies_{key}.json"
        self.ls_path = self.state_dir / f"localstorage_{key}.json"
        # ===========================================

        self.driver = None
        self.setup_driver()

    def setup_driver(self):
        """设置Chrome驱动选项"""
        chrome_options = Options()

        # GitHub Actions环境配置
        if os.getenv('GITHUB_ACTIONS'):
            # 更推荐 new headless
            chrome_options.add_argument('--headless=new')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')

        # 通用配置
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    # ============ B 方案：cookies + localStorage 快照 ============
    def _export_local_storage(self) -> dict:
        return self.driver.execute_script("""
            const out = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                out[k] = localStorage.getItem(k);
            }
            return out;
        """)

    def _import_local_storage(self, data: dict):
        self.driver.execute_script("""
            const data = arguments[0] || {};
            for (const k in data) {
                localStorage.setItem(k, data[k]);
            }
        """, data or {})

    def save_state(self):
        """保存 leaflow.net 与 checkin.leaflow.net 的 cookies + localStorage"""
        origins = ["https://leaflow.net", "https://checkin.leaflow.net"]

        all_cookies = []
        ls_by_origin = {}

        for origin in origins:
            self.driver.get(origin)
            time.sleep(2)

            ck = self.driver.get_cookies()
            for c in ck:
                c.pop("sameSite", None)
            all_cookies.extend(ck)

            ls_by_origin[origin] = self._export_local_storage()

        with open(self.cookies_path, "w", encoding="utf-8") as f:
            json.dump(all_cookies, f, ensure_ascii=False, indent=2)

        with open(self.ls_path, "w", encoding="utf-8") as f:
            json.dump(ls_by_origin, f, ensure_ascii=False, indent=2)

        logger.info(f"已保存登录态: {self.cookies_path.name}, {self.ls_path.name}")

    def load_state(self) -> bool:
        """尝试恢复状态；成功返回 True，否则 False"""
        if (not self.cookies_path.exists()) and (not self.ls_path.exists()):
            return False

        cookies = []
        ls_by_origin = {}
        try:
            if self.cookies_path.exists():
                with open(self.cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f) or []
            if self.ls_path.exists():
                with open(self.ls_path, "r", encoding="utf-8") as f:
                    ls_by_origin = json.load(f) or {}
        except Exception as e:
            logger.warning(f"读取登录态文件失败，将忽略并重新登录: {e}")
            return False

        # 先按 domain 分组 cookies（add_cookie 需要在对应域名页面执行）
        by_domain = {}
        host_only = []
        for c in cookies:
            domain = c.get("domain")
            if domain:
                by_domain.setdefault(domain, []).append(c)
            else:
                host_only.append(c)

        origins = ["https://leaflow.net", "https://checkin.leaflow.net"]

        try:
            for origin in origins:
                self.driver.get(origin)
                time.sleep(1)

                host = self.driver.current_url.split("/")[2]

                # 1) domain cookies
                for domain, cks in by_domain.items():
                    d = domain.lstrip(".")
                    if host == d or host.endswith("." + d):
                        for c in cks:
                            cc = dict(c)
                            cc.pop("sameSite", None)
                            if "expiry" in cc and isinstance(cc["expiry"], float):
                                cc["expiry"] = int(cc["expiry"])
                            try:
                                self.driver.add_cookie(cc)
                            except Exception:
                                pass

                # 2) host-only cookies：尽量也加上
                for c in host_only:
                    cc = dict(c)
                    cc.pop("sameSite", None)
                    if "expiry" in cc and isinstance(cc["expiry"], float):
                        cc["expiry"] = int(cc["expiry"])
                    try:
                        self.driver.add_cookie(cc)
                    except Exception:
                        pass

                # localStorage
                ls = ls_by_origin.get(origin)
                if isinstance(ls, dict) and ls:
                    self._import_local_storage(ls)

                self.driver.refresh()
                time.sleep(2)

            logger.info("已尝试恢复登录态")
            return True

        except Exception as e:
            logger.warning(f"恢复登录态失败，将重新登录: {e}")
            return False

    def is_logged_in(self) -> bool:
        """
        用访问 /dashboard 来验证是否登录：
        - 如果跳回 /login => 未登录
        - 如果页面仍有 password 输入框 => 未登录
        """
        self.driver.get("https://leaflow.net/dashboard")
        time.sleep(3)

        url = (self.driver.current_url or "").lower()
        if "login" in url:
            return False

        try:
            pwd = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
            if pwd:
                return False
        except Exception:
            pass

        return True

    def ensure_logged_in(self) -> bool:
        """
        先尝试恢复登录态；如果无效则走网页登录；成功后更新快照
        """
        self.load_state()
        if self.is_logged_in():
            logger.info("登录态仍有效（免登录成功）")
            return True

        logger.info("登录态无效，开始重新登录...")
        ok = self.login()
        if ok:
            try:
                self.driver.get("https://checkin.leaflow.net")
                time.sleep(2)
            except Exception:
                pass

            self.save_state()
            return True

        return False
    # ============================================================

    def close_popup(self):
        """关闭初始弹窗"""
        try:
            logger.info("尝试关闭初始弹窗...")
            time.sleep(3)

            try:
                actions = ActionChains(self.driver)
                actions.move_by_offset(10, 10).click().perform()
                logger.info("已成功关闭弹窗")
                time.sleep(2)
                return True
            except:
                pass
            return False

        except Exception as e:
            logger.warning(f"关闭弹窗时出错: {e}")
            return False

    def wait_for_element_clickable(self, by, value, timeout=10):
        """等待元素可点击"""
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )

    def wait_for_element_present(self, by, value, timeout=10):
        """等待元素出现"""
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def login(self):
        """执行登录流程"""
        logger.info("开始登录流程")

        self.driver.get("https://leaflow.net/login")
        time.sleep(5)

        self.close_popup()

        # 输入邮箱
        try:
            logger.info("查找邮箱输入框...")
            time.sleep(2)

            email_selectors = [
                "input[type='email']",
                "input[name='email']",
                "input[placeholder*='邮箱']",
                "input[placeholder*='邮件']",
                "input[placeholder*='email']",
                "input[name='username']",
                "input[type='text']",
            ]

            email_input = None
            for selector in email_selectors:
                try:
                    email_input = self.wait_for_element_clickable(By.CSS_SELECTOR, selector, 5)
                    logger.info("找到邮箱输入框")
                    break
                except:
                    continue

            if not email_input:
                raise Exception("找不到邮箱输入框")

            email_input.clear()
            email_input.send_keys(self.email)
            logger.info("邮箱输入完成")
            time.sleep(2)

        except Exception as e:
            logger.error(f"输入邮箱时出错: {e}")
            try:
                self.driver.execute_script(
                    "document.querySelector('input[type=\"text\"], input[type=\"email\"]').value = arguments[0];",
                    self.email
                )
                logger.info("通过JavaScript设置邮箱")
                time.sleep(2)
            except:
                raise Exception(f"无法输入邮箱: {e}")

        # 输入密码
        try:
            logger.info("查找密码输入框...")
            password_input = self.wait_for_element_clickable(By.CSS_SELECTOR, "input[type='password']", 10)
            password_input.clear()
            password_input.send_keys(self.password)
            logger.info("密码输入完成")
            time.sleep(1)

        except TimeoutException:
            raise Exception("找不到密码输入框")

        # 点击登录按钮
        try:
            logger.info("查找登录按钮...")
            login_btn_selectors = [
                "//button[contains(text(), '登录')]",
                "//button[contains(text(), 'Login')]",
                "//button[@type='submit']",
                "//input[@type='submit']",
                "button[type='submit']"
            ]

            login_btn = None
            for selector in login_btn_selectors:
                try:
                    if selector.startswith("//"):
                        login_btn = self.wait_for_element_clickable(By.XPATH, selector, 5)
                    else:
                        login_btn = self.wait_for_element_clickable(By.CSS_SELECTOR, selector, 5)
                    logger.info("找到登录按钮")
                    break
                except:
                    continue

            if not login_btn:
                raise Exception("找不到登录按钮")

            login_btn.click()
            logger.info("已点击登录按钮")

        except Exception as e:
            raise Exception(f"点击登录按钮失败: {e}")

        # ====== 等待登录完成（关键：等待期间不要 driver.get 跳页）======
        try:
            time.sleep(1)
            before_cookies = {c.get("name") for c in self.driver.get_cookies()}

            def _login_progress(driver):
                url = (driver.current_url or "").lower()

                # URL 已离开 login
                if "login" not in url:
                    return True

                # cookie 有新增（很多站点登录成功会写 session）
                try:
                    after_cookies = {c.get("name") for c in driver.get_cookies()}
                    if len(after_cookies - before_cookies) > 0:
                        return True
                except Exception:
                    pass

                # 页面出现退出/Logout（有些 SPA 不改 URL）
                try:
                    if driver.find_elements(By.XPATH, "//*[contains(text(),'退出') or contains(text(),'Logout')]"):
                        return True
                except Exception:
                    pass

                return False

            WebDriverWait(self.driver, 60, poll_frequency=1).until(_login_progress)

            # ✅ 只在这里做一次最终验证（允许跳转）
            if self.is_logged_in():
                logger.info(f"登录成功（已通过 /dashboard 验证），当前URL: {self.driver.current_url}")
                return True

            body = self.driver.find_element(By.TAG_NAME, "body").text
            raise Exception("登录后仍未处于登录态，页面提示(前300字): " + body[:300])

        except TimeoutException:
            body = ""
            try:
                body = self.driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                pass

            # 尝试抓错误框
            try:
                error_selectors = [".error", ".alert-danger", "[class*='error']", "[class*='danger']"]
                for selector in error_selectors:
                    try:
                        error_msg = self.driver.find_element(By.CSS_SELECTOR, selector)
                        if error_msg.is_displayed() and error_msg.text.strip():
                            raise Exception(f"登录失败: {error_msg.text}")
                    except:
                        continue
            except Exception as e:
                raise e

            raise Exception("登录超时，无法确认登录状态（前300字）: " + (body[:300] if body else "无法读取页面文本"))
        # ============================================================

    def get_balance(self):
        """获取当前账号的总余额"""
        try:
            logger.info("获取账号余额...")

            self.driver.get("https://leaflow.net/dashboard")
            time.sleep(3)

            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            balance_selectors = [
                "//*[contains(text(), '¥') or contains(text(), '￥') or contains(text(), '元')]",
                "//*[contains(@class, 'balance')]",
                "//*[contains(@class, 'money')]",
                "//*[contains(@class, 'amount')]",
                "//button[contains(@class, 'dollar')]",
                "//span[contains(@class, 'font-medium')]"
            ]

            for selector in balance_selectors:
                try:
                    elements = self.driver.find_elements(By.XPATH, selector)
                    for element in elements:
                        text = element.text.strip()
                        if any(char.isdigit() for char in text) and ('¥' in text or '￥' in text or '元' in text):
                            import re
                            numbers = re.findall(r'\d+\.?\d*', text)
                            if numbers:
                                balance = numbers[0]
                                logger.info(f"找到余额: {balance}元")
                                return f"{balance}元"
                except:
                    continue

            logger.warning("未找到余额信息")
            return "未知"

        except Exception as e:
            logger.warning(f"获取余额时出错: {e}")
            return "未知"

    def wait_for_checkin_page_loaded(self, max_retries=3, wait_time=20):
        """等待签到页面完全加载，支持重试"""
        for attempt in range(max_retries):
            logger.info(f"等待签到页面加载，尝试 {attempt + 1}/{max_retries}，等待 {wait_time} 秒...")
            time.sleep(wait_time)

            try:
                checkin_indicators = [
                    "button.checkin-btn",
                    "//button[contains(text(), '立即签到')]",
                    "//button[contains(text(), '已签到')]",
                    "//*[contains(text(), '每日签到')]",
                    "//*[contains(text(), '签到')]"
                ]

                for indicator in checkin_indicators:
                    try:
                        if indicator.startswith("//"):
                            element = WebDriverWait(self.driver, 10).until(
                                EC.presence_of_element_located((By.XPATH, indicator))
                            )
                        else:
                            element = WebDriverWait(self.driver, 10).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, indicator))
                            )

                        if element.is_displayed():
                            logger.info("找到签到页面元素")
                            return True
                    except:
                        continue

                logger.warning(f"第 {attempt + 1} 次尝试未找到签到按钮，继续等待...")

            except Exception as e:
                logger.warning(f"第 {attempt + 1} 次检查签到页面时出错: {e}")

        return False

    def find_and_click_checkin_button(self):
        """查找并点击签到按钮 - 处理已签到状态"""
        logger.info("查找签到按钮...")

        try:
            time.sleep(5)

            checkin_selectors = [
                "button.checkin-btn",
                "//button[contains(text(), '立即签到')]",
                "//button[contains(@class, 'checkin')]",
                "button[type='submit']",
                "button[name='checkin']"
            ]

            for selector in checkin_selectors:
                try:
                    if selector.startswith("//"):
                        checkin_btn = WebDriverWait(self.driver, 15).until(
                            EC.presence_of_element_located((By.XPATH, selector))
                        )
                    else:
                        checkin_btn = WebDriverWait(self.driver, 15).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )

                    if checkin_btn.is_displayed():
                        btn_text = checkin_btn.text.strip()
                        if "已签到" in btn_text:
                            logger.info("伙计，今日你已经签到过了！")
                            return "already_checked_in"

                        if checkin_btn.is_enabled():
                            logger.info("找到并点击立即签到按钮")
                            checkin_btn.click()
                            return True
                        else:
                            logger.info("签到按钮不可用，可能已经签到过了")
                            return "already_checked_in"

                except Exception as e:
                    logger.debug(f"选择器未找到按钮: {e}")
                    continue

            logger.error("找不到签到按钮")
            return False

        except Exception as e:
            logger.error(f"查找签到按钮时出错: {e}")
            return False

    def checkin(self):
        """执行签到流程"""
        logger.info("跳转到签到页面...")

        self.driver.get("https://checkin.leaflow.net")

        if not self.wait_for_checkin_page_loaded(max_retries=3, wait_time=20):
            raise Exception("签到页面加载失败，无法找到签到相关元素")

        checkin_result = self.find_and_click_checkin_button()

        if checkin_result == "already_checked_in":
            return "今日已签到"
        elif checkin_result is True:
            logger.info("已点击立即签到按钮")
            time.sleep(5)

            result_message = self.get_checkin_result()
            return result_message
        else:
            raise Exception("找不到立即签到按钮或按钮不可点击")

    def get_checkin_result(self):
        """获取签到结果消息"""
        try:
            time.sleep(3)

            success_selectors = [
                ".alert-success",
                ".success",
                ".message",
                "[class*='success']",
                "[class*='message']",
                ".modal-content",
                ".ant-message",
                ".el-message",
                ".toast",
                ".notification"
            ]

            for selector in success_selectors:
                try:
                    element = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if element.is_displayed():
                        text = element.text.strip()
                        if text:
                            return text
                except:
                    continue

            page_text = self.driver.find_element(By.TAG_NAME, "body").text
            important_keywords = ["成功", "签到", "获得", "恭喜", "谢谢", "感谢", "完成", "已签到", "连续签到"]

            for keyword in important_keywords:
                if keyword in page_text:
                    lines = page_text.split('\n')
                    for line in lines:
                        if keyword in line and len(line.strip()) < 100:
                            return line.strip()

            try:
                checkin_btn = self.driver.find_element(By.CSS_SELECTOR, "button.checkin-btn")
                if (not checkin_btn.is_enabled()) or ("已签到" in checkin_btn.text) or ("disabled" in checkin_btn.get_attribute("class")):
                    return "今日已签到完成"
            except:
                pass

            return "签到完成，但未找到具体结果消息"

        except Exception as e:
            return f"获取签到结果时出错: {str(e)}"

    def run(self):
        """单个账号执行流程"""
        try:
            logger.info("开始处理账号")

            if self.ensure_logged_in():
                result = self.checkin()
                balance = self.get_balance()

                logger.info(f"签到结果: {result}, 余额: {balance}")
                return True, result, balance
            else:
                raise Exception("登录失败")

        except Exception as e:
            error_msg = f"自动签到失败: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, "未知"

        finally:
            if self.driver:
                self.driver.quit()


class MultiAccountManager:
    """多账号管理器 - 简化配置版本"""

    def __init__(self):
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        self.accounts = self.load_accounts()

    def load_accounts(self):
        """从环境变量加载多账号信息，支持冒号分隔多账号和单账号"""
        accounts = []

        logger.info("开始加载账号配置...")

        accounts_str = os.getenv('LEAFLOW_ACCOUNTS', '').strip()
        if accounts_str:
            try:
                logger.info("尝试解析冒号分隔多账号配置")
                account_pairs = [pair.strip() for pair in accounts_str.split(',')]

                logger.info(f"找到 {len(account_pairs)} 个账号")

                for i, pair in enumerate(account_pairs):
                    if ':' in pair:
                        email, password = pair.split(':', 1)
                        email = email.strip()
                        password = password.strip()

                        if email and password:
                            accounts.append({'email': email, 'password': password})
                            logger.info(f"成功添加第 {i + 1} 个账号")
                        else:
                            logger.warning("账号对格式错误")
                    else:
                        logger.warning("账号对缺少冒号分隔符")

                if accounts:
                    logger.info(f"从冒号分隔格式成功加载了 {len(accounts)} 个账号")
                    return accounts
                else:
                    logger.warning("冒号分隔配置中没有找到有效的账号信息")
            except Exception as e:
                logger.error(f"解析冒号分隔账号配置失败: {e}")

        single_email = os.getenv('LEAFLOW_EMAIL', '').strip()
        single_password = os.getenv('LEAFLOW_PASSWORD', '').strip()

        if single_email and single_password:
            accounts.append({'email': single_email, 'password': single_password})
            logger.info("加载了单个账号配置")
            return accounts

        logger.error("未找到有效的账号配置")
        logger.error("请检查以下环境变量设置:")
        logger.error("1. LEAFLOW_ACCOUNTS: 冒号分隔多账号 (email1:pass1,email2:pass2)")
        logger.error("2. LEAFLOW_EMAIL 和 LEAFLOW_PASSWORD: 单账号")

        raise ValueError("未找到有效的账号配置")

    def send_notification(self, results):
        """发送汇总通知到Telegram - 按照指定模板格式"""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.info("Telegram配置未设置，跳过通知")
            return

        try:
            success_count = sum(1 for _, success, _, _ in results if success)
            total_count = len(results)
            current_date = datetime.now().strftime("%Y/%m/%d")

            message = "🎁 Leaflow自动签到通知\n"
            message += f"📊 成功: {success_count}/{total_count}\n"
            message += f"📅 签到时间：{current_date}\n\n"

            for email, success, result, balance in results:
                masked_email = email[:3] + "***" + email[email.find("@"):]

                if success:
                    message += f"账号：{masked_email}\n"
                    message += f"✅  {result}！\n"
                    message += f"💰  当前总余额：{balance}。\n\n"
                else:
                    message += f"账号：{masked_email}\n"
                    message += f"❌  {result}\n\n"

            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            data = {"chat_id": self.telegram_chat_id, "text": message, "parse_mode": "HTML"}

            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                logger.info("Telegram汇总通知发送成功")
            else:
                logger.error(f"Telegram通知发送失败: {response.text}")

        except Exception as e:
            logger.error(f"发送Telegram通知时出错: {e}")

    def run_all(self):
        """运行所有账号的签到流程"""
        logger.info(f"开始执行 {len(self.accounts)} 个账号的签到任务")

        results = []

        for i, account in enumerate(self.accounts, 1):
            logger.info(f"处理第 {i}/{len(self.accounts)} 个账号")

            try:
                auto_checkin = LeaflowAutoCheckin(account['email'], account['password'])
                success, result, balance = auto_checkin.run()
                results.append((account['email'], success, result, balance))

                if i < len(self.accounts):
                    wait_time = 5
                    logger.info(f"等待{wait_time}秒后处理下一个账号...")
                    time.sleep(wait_time)

            except Exception as e:
                error_msg = f"处理账号时发生异常: {str(e)}"
                logger.error(error_msg)
                results.append((account['email'], False, error_msg, "未知"))

        self.send_notification(results)

        success_count = sum(1 for _, success, _, _ in results if success)
        return success_count == len(self.accounts), results


def main():
    """主函数"""
    try:
        manager = MultiAccountManager()
        overall_success, detailed_results = manager.run_all()

        if overall_success:
            logger.info("✅ 所有账号签到成功")
            exit(0)
        else:
            success_count = sum(1 for _, success, _, _ in detailed_results if success)
            logger.warning(f"⚠️ 部分账号签到失败: {success_count}/{len(detailed_results)} 成功")
            exit(0)

    except Exception as e:
        logger.error(f"❌ 脚本执行出错: {e}")
        exit(1)


if __name__ == "__main__":
    main()
