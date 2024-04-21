from enum import Enum
from config import conf
import threading
import asyncio
from common.log import logger
from bridge.reply import Reply, ReplyType
from plugins import EventContext, EventAction
from bridge.context import ContextType
from .utils import Util
import requests
import time

INVALID_REQUEST = 410

class TaskType(Enum):
    RUANZHU = "ruanzhu"
    ZHUANLI = "zhuanli"

    def __str__(self):
        return self.name


class Status(Enum):
    PENDING = "pending"
    FINISHED = "finished"
    EXPIRED = "expired"
    ABORTED = "aborted"

    def __str__(self):
        return self.name
    
class TBTask:
    def __init__(self, id, user_id: str, task_type: TaskType, raw_prompt=None, expires: int = 60 * 6,
                 status=Status.PENDING):
        self.id = id
        self.user_id = user_id
        self.task_type = task_type
        self.raw_prompt = raw_prompt
        self.send_func = None  # send_func(img_url)
        self.expiry_time = time.time() + expires
        self.status = status
        self.content = None  # url

    def __str__(self):
        return f"id={self.id}, user_id={self.user_id}, task_type={self.task_type}, status={self.status}"


# taibaiai bot
class TBAIBot:
    def __init__(self, config):
        self.base_url = 'https://auto.taibaiai.com' + "/api/v1/taskwx/"
        self.headers = {"Authorization": "Bearer " + conf().get("taibaiai_api_key")}
        self.config = config
        self.tasks = {}
        self.temp_dict = {}
        self.tasks_lock = threading.Lock()
        self.event_loop = asyncio.new_event_loop()

    def judge_taibai_task_type(self, e_context: EventContext):
        """
        判断太白AI任务的类型
        :param e_context: 上下文
        :return: 任务类型枚举
        """
        if not self.config:
            return None
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        context = e_context['context']
        if context.type == ContextType.TEXT:
            cmd_list = context.content.split(maxsplit=1)
            if not cmd_list:
                return None
            if cmd_list[0].lower() == f"{trigger_prefix}tbrz":
                return TaskType.RUANZHU
            elif cmd_list[0].lower() == f"{trigger_prefix}tbzl":
                return TaskType.ZHUANLI
            
    def process_taibai_task(self, tb_type: TaskType, e_context: EventContext):
        """
        处理太白任务
        :param tb_type: tb任务类型
        :param e_context: 对话上下文
        """
        context = e_context['context']
        session_id = context["session_id"]
        cmd = context.content.split(maxsplit=1)

        if len(cmd) == 1 and context.type == ContextType.TEXT:
            # midjourney 帮助指令
            self._set_reply_text(self.get_help_text(verbose=True), e_context, level=ReplyType.INFO)
            return

        if len(cmd) == 2 and (cmd[1] == "open" or cmd[1] == "close"):
            if not Util.is_admin(e_context):
                Util.set_reply_text("需要管理员权限执行", e_context, level=ReplyType.ERROR)
                return
            # midjourney 开关指令
            is_open = True
            tips_text = "开启"
            if cmd[1] == "close":
                tips_text = "关闭"
                is_open = False
            self.config["enabled"] = is_open
            self._set_reply_text(f"太白AI已{tips_text}", e_context, level=ReplyType.INFO)
            return

        if not self.config.get("enabled"):
            logger.warn("太白AI未开启，请查看 plugins/taibaiai/config.json 中的配置")
            self._set_reply_text(f"太白AI绘画未开启", e_context, level=ReplyType.INFO)
            return

        if not self._check_rate_limit(session_id, e_context):
            logger.warn("[TB] taibaiai task exceed rate limit")
            return

        if tb_type == TaskType.RUANZHU:
            if context.type == ContextType.TAIBAIAI:
                raw_prompt = context.content
            else:
                # 图片生成
                raw_prompt = cmd[1]
            reply = self.taibai_rz(raw_prompt, session_id, e_context)
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return
    
    def _check_rate_limit(self, user_id: str, e_context: EventContext) -> bool:
        """
        太白AI任务限流控制
        :param user_id: 用户id
        :param e_context: 对话上下文
        :return: 任务是否能够生成, True:可以生成, False: 被限流
        """
        tasks = self.find_tasks_by_user_id(user_id)
        task_count = len([t for t in tasks if t.status == Status.PENDING])
        if task_count >= self.config.get("max_tasks_per_user"):
            reply = Reply(ReplyType.INFO, "您的TaibaiAI任务数已达上限，请稍后再试")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return False
        task_count = len([t for t in self.tasks.values() if t.status == Status.PENDING])
        if task_count >= self.config.get("max_tasks"):
            reply = Reply(ReplyType.INFO, "TaibaiAI任务数已达上限，请稍后再试")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return False
        return True
    

    def find_tasks_by_user_id(self, user_id) -> list:
        result = []
        with self.tasks_lock:
            now = time.time()
            for task in self.tasks.values():
                if task.status == Status.PENDING and now > task.expiry_time:
                    task.status = Status.EXPIRED
                    logger.info(f"[TB] {task} expired")
                if task.user_id == user_id:
                    result.append(task)
        return result
    
    def _set_reply_text(self, content: str, e_context: EventContext, level: ReplyType = ReplyType.ERROR):
        """
        设置回复文本
        :param content: 回复内容
        :param e_context: 对话上下文
        :param level: 回复等级
        """
        reply = Reply(level, content)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose=False, **kwargs):
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        help_text = "🎨利用太白生成软著和专利\n\n"
        if not verbose:
            return help_text
        help_text += f" - 生成软著: {trigger_prefix}tbrz 软著简称|软著名称|作者名称|公司名称 \n - 生成专利: {trigger_prefix}tbzl 项目名称|发明人|公司名称"
        help_text += f"\n\n例如：\n\"{trigger_prefix}tbrz 太白AI智能平台|太白AI企业智能化工具平台|罗锐|深圳市真香生活科技有限公司\"\n\"{trigger_prefix}tbzl 太白AI自动拆解任务和AI一键完成任务|罗锐|深圳市真香生活科技有限公司\""
        return help_text

    def taibai_rz(self, prompt: str, user_id: str, e_context: EventContext) -> Reply:
        """
        软著生成
        :param prompt: 提示词
        :param user_id: 用户id
        :param e_context: 对话上下文
        :return: 任务ID
        """
        logger.info(f"[TB] ruanzhu generate, prompt={prompt}")
        body = {"prompt": prompt,"user_id":user_id}
        res = requests.post(url=self.base_url + "/ruanzhu", json=body, headers=self.headers, timeout=(5, 40))
        if res.status_code == 200:
            res = res.json()
            logger.debug(f"[TB] ruanzhu generate, res={res}")
            if res.get("code") == 0:
                task_id = res.get("data").get("task_id")
                time_str = "1分钟"
                content = f"🚀您的任务将在{time_str}左右完成，请耐心等待\n- - - - - - - - -\n"
                content += f"prompt: {prompt}"
                reply = Reply(ReplyType.INFO, content)
                task = TBTask(id=task_id, status=Status.PENDING, raw_prompt=prompt, user_id=user_id,
                              task_type=TaskType.RUANZHU)
                # put to memory dict
                self.tasks[task.id] = task
                # asyncio.run_coroutine_threadsafe(self.check_task(task, e_context), self.event_loop)
                self._do_check_task(task, e_context)
                return reply
        else:
            res_json = res.json()
            logger.error(f"[TB] generate error, msg={res_json.get('message')}, status_code={res.status_code}")
            if res.status_code == INVALID_REQUEST:
                reply = Reply(ReplyType.ERROR, "软著任务创建失败，请检查提示词内容")
            else:
                reply = Reply(ReplyType.ERROR, "软著任务创建失败，请稍后再试")
            return reply
        
    def taibai_zl(self, prompt: str, user_id: str, e_context: EventContext) -> Reply:
        """
        专利生成
        :param prompt: 提示词
        :param user_id: 用户id
        :param e_context: 对话上下文
        :return: 任务ID
        """
        logger.info(f"[TB] zhuanli generate, prompt={prompt}")
        body = {"prompt": prompt,"user_id":user_id}
        res = requests.post(url=self.base_url + "/zhuanli", json=body, headers=self.headers, timeout=(5, 40))
        if res.status_code == 200:
            res = res.json()
            logger.debug(f"[TB] zhuanli generate, res={res}")
            if res.get("code") == 0:
                task_id = res.get("data").get("task_id")
                time_str = "1分钟"
                content = f"🚀您的任务将在{time_str}左右完成，请耐心等待\n- - - - - - - - -\n"
                content += f"prompt: {prompt}"
                reply = Reply(ReplyType.INFO, content)
                task = TBTask(id=task_id, status=Status.PENDING, raw_prompt=prompt, user_id=user_id,
                            task_type=TaskType.RUANZHU)
                # put to memory dict
                self.tasks[task.id] = task
                # asyncio.run_coroutine_threadsafe(self.check_task(task, e_context), self.event_loop)
                self._do_check_task(task, e_context)
                return reply
        else:
            res_json = res.json()
            logger.error(f"[TB] generate error, msg={res_json.get('message')}, status_code={res.status_code}")
            if res.status_code == INVALID_REQUEST:
                reply = Reply(ReplyType.ERROR, "专利创建失败，请检查提示词内容")
            else:
                reply = Reply(ReplyType.ERROR, "专利创建失败，请稍后再试")
            return reply
    
    def _do_check_task(self, task: TBTask, e_context: EventContext):
        threading.Thread(target=self.check_task_sync, args=(task, e_context)).start()

    def check_task_sync(self, task: TBTask, e_context: EventContext):
        logger.debug(f"[TB] start check task status, {task}")
        max_retry_times = 120
        while max_retry_times > 0:
            time.sleep(10)
            url = f"{self.base_url}/getTask/{task.id}"
            try:
                res = requests.get(url, headers=self.headers, timeout=8)
                if res.status_code == 200:
                    res_json = res.json()
                    logger.debug(f"[TB] task check res sync, task_id={task.id}, status={res.status_code}, "
                                 f"data={res_json.get('data')}, thread={threading.current_thread().name}")
                    if res_json.get("data") and res_json.get("data").get("code") == 200:
                        # process success res
                        if self.tasks.get(task.id):
                            self.tasks[task.id].status = Status.FINISHED
                        self._process_success_task(task, res_json.get("data"), e_context)
                        return
                    max_retry_times -= 1
                else:
                    res_json = res.json()
                    logger.warn(f"[TB] wechat check error, status_code={res.status_code}, res={res_json}")
                    max_retry_times -= 20
            except Exception as e:
                max_retry_times -= 20
                logger.warn(e)
        logger.warn("[TB] end from poll")
        if self.tasks.get(task.id):
            self.tasks[task.id].status = Status.EXPIRED

    def _process_success_task(self, task: TBTask, res: dict, e_context: EventContext):
        """
        处理任务成功的结果
        :param task: TB任务
        :param res: 请求结果
        :param e_context: 对话上下文
        """
        # channel send img
        task.status = Status.FINISHED
        task.content = res.get("response_data")
        logger.info(f"[TB] task success, task_id={task.id}")

        # send content
        reply = Reply(ReplyType.TEXT, task.content)
        channel = e_context["channel"]
        _send(channel, reply, e_context["context"])

        self._print_tasks()
        return
    
    def _print_tasks(self):
        for id in self.tasks:
            logger.debug(f"[TB] current task: {self.tasks[id]}")

def _send(channel, reply: Reply, context, retry_cnt=0):
    try:
        channel.send(reply, context)
    except Exception as e:
        logger.error("[WX] sendMsg error: {}".format(str(e)))
        if isinstance(e, NotImplementedError):
            return
        logger.exception(e)
        if retry_cnt < 2:
            time.sleep(3 + 3 * retry_cnt)
            channel.send(reply, context, retry_cnt + 1)