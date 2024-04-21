# encoding:utf-8

import plugins
from common.log import logger
from plugins import *
from config import conf
from .taibaiai_bot import TBAIBot
from bridge.context import ContextType

@plugins.register(
    name="taibaiai",
    desc="太白AI一键完成任务.",
    version="0.1.0",
    author="https://ai.taibaiai.com",
    desire_priority=99
)
class TaibaiAI(Plugin):
    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.config = super().load_config()
        if not self.config:
            # 未加载到配置，使用模板中的配置
            self.config = self._load_config_template()
        if self.config:
            self.tb_bot = TBAIBot(self.config.get("midjourney"))
        self.sum_config = {}
        if self.config:
            self.sum_config = self.config.get("summary")
        logger.info(f"[TaibaiAI] inited, config={self.config}")

    def _load_config_template(self):
        logger.debug("No TaibaiAI plugin config.json, use plugins/TaibaiAI/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    plugin_conf["taibaiai"]["enabled"] = False
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def on_handle_context(self, e_context: EventContext):
        """
        消息处理逻辑
        :param e_context: 消息上下文
        """
        if not self.config:
            return

        context = e_context['context']
        if context.type not in [ContextType.TEXT, ContextType.TAIBAIAI]:
            # filter content no need solve
            return

        tb_type = self.tb_bot.judge_taibai_task_type(e_context)
        if tb_type:
            # TB任务处理
            self.tb_bot.process_taibai_task(tb_type, e_context)
            return
