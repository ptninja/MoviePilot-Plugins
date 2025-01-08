import json
import io
import re
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
import textwrap
from ruamel.yaml import YAML, YAMLError

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils

@dataclass
class FlarumSiteConfig:
    site_name: str
    site_url: str
    cookie: str

class FlarumSignin(_PluginBase):
    # 插件名称
    plugin_name = "Flarum 论坛签到"
    # 插件描述
    plugin_desc = "Flarum 论坛签到"
    # 插件图标
    plugin_icon = "flarum.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "ptninja"
    # 作者主页
    author_url = "https://github.com/ptninja"
    # 插件配置项ID前缀
    plugin_config_prefix = "flarumsignin_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _onlyonce = False
    _notify = False
    _history_days = None
    _site_configs: List[FlarumSiteConfig] = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if not config:
            return False
        
        # 停止现有任务
        self.stop_service()

        logger.info(f"Loading config")
        self._enabled = config.get("enabled")
        self._cron = config.get("cron")
        self._notify = config.get("notify")
        self._onlyonce = config.get("onlyonce")
        self._history_days = config.get("history_days") or 30
        self._site_configs = self.__load_configs(config.get("flarum_site_configs"))

        if self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"Flarum 签到服务启动，立即运行一次")
            self._scheduler.add_job(func=self.signin_all_sites, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="Flarum 签到")
            # 关闭一次性开关
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config=config)

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def signin_all_sites(self):
        """
        逐个站点签到。可以考虑参考 sitestatistic 多线程签到
        """
        logger.info(f"所有签到: {self._site_configs}")
        for site in self._site_configs:
            self.signin(site)

    def signin(self, config: FlarumSiteConfig):
        logger.info(f"开始签到 {config.site_name} ...")

        res = RequestUtils(cookies=config.cookie).get_res(url=config.site_url)
        if not res or res.status_code != 200:
            logger.error(f"请求 {config.site_name} 错误")
            return

        # 获取csrfToken
        pattern = r'"csrfToken":"(.*?)"'
        csrfToken = re.findall(pattern, res.text)
        if not csrfToken:
            logger.error("请求csrfToken失败")
            return

        csrfToken = csrfToken[0]
        logger.info(f"获取csrfToken成功 {csrfToken}")

        # 获取userid
        pattern = r'"userId":(\d+)'
        match = re.search(pattern, res.text)

        if match:
            userId = match.group(1)
            logger.info(f"获取userid成功 {userId}")
        else:
            logger.error("未找到userId")
            return

        headers = {
            "X-CSRF-Token": csrfToken,
            "X-HTTP-Method-Override": "PATCH",
            "Cookie": config.cookie,
        }

        data = {
            "data": {
                "type": "users",
                "attributes": {
                    "canCheckin": False,
                    "totalContinuousCheckIn": 2
                },
                "id": userId
            }
        }

        # 开始签到
        res = RequestUtils(headers=headers).post(url=f"{config.site_url}/api/users/{userId}", json=data)

        if not res or res.status_code != 200:
            logger.error(f"{config.site_name} 签到失败")

            # 发送通知
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【{config.site_name} 签到任务完成】",
                    text="签到失败，请检查cookie是否失效")
            return

        sign_dict = json.loads(res.text)
        money = sign_dict['data']['attributes']['money']
        totalContinuousCheckIn = sign_dict['data']['attributes']['totalContinuousCheckIn']

        # 发送通知
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=f"【{config.site_name} 签到任务完成】",
                text=f"累计签到 {totalContinuousCheckIn} \n"
                     f"剩余积分 {money}")

        # 读取历史记录
        history = self.get_data('history') or []

        history.append({
            "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
            "siteName": config.site_name,
            "totalContinuousCheckIn": totalContinuousCheckIn,
            "money": money,
        })

        thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
        history = [record for record in history if
                   datetime.strptime(record["date"],
                                     '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
        # 保存签到历史
        self.save_data(key="history", value=history)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "FlarumSignin",
                "name": "Flarum 签到服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.signin_all_sites,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '保留历史天数'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VAceEditor',
                                        'props': {
                                            'modelvalue': 'flarum_site_configs',
                                            'lang': 'yaml',
                                            'theme': 'monokai',
                                            'style': 'height: 25rem'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '整点定时签到失败？不妨换个时间试试'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "history_days": 30,
            "cron": "0 9 * * *",
            "flarum_site_configs": self.__get_demo_config()
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        # 按照签到时间倒序
        historys = sorted(historys, key=lambda x: x.get("date") or 0, reverse=True)

        # 签到消息
        sign_msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("date")
                    },
                    {
                        'component': 'td',
                        'text': history.get("siteName")
                    },
                    {
                        'component': 'td',
                        'text': history.get("totalContinuousCheckIn")
                    },
                    {
                        'component': 'td',
                        'text': history.get("money")
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '站点'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '连续签到次数'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '剩余积分'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': sign_msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def __load_configs(self, config_str: Optional[str]) -> List[FlarumSiteConfig]:
        """加载YAML配置字符串，并构造 FlarumSiteConfig 列表。

        Args:
        config_str (str): 配置内容的字符串。

        Returns:
        List[ClassifierConfig]: 从配置字符串解析出的配置列表。
        """
        if not config_str:
            return []

        yaml = YAML(typ="safe")
        try:
            data = yaml.load(io.StringIO(config_str))
            return [FlarumSiteConfig(**item) for item in data]
        except YAMLError as e:
            self.__log_and_notify_error(f"YAML parsing error: {e}")
            return []  # 返回空列表或根据需要做进一步的错误处理
        except Exception as e:
            self.__log_and_notify_error(f"Unexpected error during YAML parsing: {e}")
            return []  # 处理任何意外的异常，返回空列表或其它适当的错误响应

    def __log_and_notify_error(self, message):
        """
        记录错误日志并发送系统通知
        """
        logger.error(message)
        self.systemmessage.put(message, title="Flarum 论坛签到")
        
    @staticmethod
    def __get_demo_config():
        """获取默认配置"""

        block = """\
            ####### 配置说明 BEGIN #######
            - site_name: invites
              site_url: https://invites.fun
              cookie: xxx
              
            - site_name: hddolby
              site_url: https://forums.orcinusorca.org
              cookie: yyy
        """
        return textwrap.dedent(block)

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))