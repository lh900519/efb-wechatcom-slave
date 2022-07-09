# -*- coding: utf-8 -*-
"""
@author: lh900519
"""

import asyncio
import logging
import queue
import gettext
import threading
import uuid
import html
import yaml
import json
import tempfile
import hashlib
from traceback import print_exc
from urllib.request import urlopen
import urllib.parse

from typing import Optional, Collection, BinaryIO, Dict, Any, Callable
from datetime import datetime

import websockets
from websockets import ConnectionClosed

from ehforwarderbot import MsgType, Chat, Message, Status, coordinator
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.types import MessageID, ChatID, InstanceID
from ehforwarderbot import utils as efb_utils
from ehforwarderbot.exceptions import EFBException

from .ChatMgr import ChatMgr
from .CustomTypes import EFBGroupChat, EFBPrivateChat, EFBGroupMember
from .MsgDecorator import efb_text_simple_wrapper, efb_image_wrapper
from .WechatPcMsgProcessor import MsgProcessor
from .utils import process_quote_text, download_file
from .WechatWork import WechatWork


TYPE_HANDLERS = {
    1: MsgProcessor.text_msg,
    3: MsgProcessor.image_msg,
    34: MsgProcessor.voice_msg,
    35: MsgProcessor.mail_msg,
    42: MsgProcessor.mp_card_msg,
    43: MsgProcessor.voideo_msg,
    47: MsgProcessor.emojipic_msg,
    48: MsgProcessor.location_msg,
    49: MsgProcessor.msgType49_xml_msg
}

def timestamp():
    ts = int(datetime.timestamp(datetime.now()) * 1000)
    return ts

def xor_string(data, key='awesomepassword', encode=False, decode=False):
    from itertools import cycle
    import base64
    if decode:
        data = base64.b64decode(data).decode('utf-8')
    xored = ''.join(chr(ord(c)^ord(k)) for c,k in zip(data, cycle(key)))

    if encode:
        return base64.b64encode(xored).decode('ascii')
    return xored

def get_query_param(path, key):
    query = urllib.parse.urlparse(path).query
    params = urllib.parse.parse_qs(query)
    values = params.get(key, [])
    if len(values) == 1:
        return values[0]

# 验证连接
class QueryParamProtocol(websockets.WebSocketServerProtocol):
    logger: logging.Logger = logging.getLogger("QueryParamProtocol")

    # ws://localhost:5678??app_id=1234567890ABCDEFGHIJKLMNOPQRSTUV&timestamp=1656822821181&hash=a97ae70cfafc1cf31d9cfac7e2ac17c973c849a1ae20f7d8b50deba8911e62e5
    async def process_request(self, path, headers):
        app_id = get_query_param(path, "app_id")
        ts = get_query_param(path, "timestamp")
        hash_token = get_query_param(path, "hash")
        self.logger.debug(path)
        self.logger.debug(f"app_id: {app_id}")
        self.logger.debug(f"timestamp: {ts}")
        self.logger.debug(f"hash: {hash_token}")

        if app_id is None:
            return http.HTTPStatus.UNAUTHORIZED, [], b"Missing app_id\n"
        if hash_token is None:
            return http.HTTPStatus.UNAUTHORIZED, [], b"Missing token\n"
        if abs(int(ts) - timestamp()) > 600000:
            return http.HTTPStatus.UNAUTHORIZED, [], b"Expired time\n"

        self.app_id = app_id
        self.hash = hash_token
        self.timestamp = ts


class WechatCOMChannel(SlaveChannel):
    channel_name: str = "Wechat Pc Slave"
    channel_emoji: str = "🖥️"
    # channel_id = "wechat.PcCOM"
    channel_id = "tedrolin.wechatPc"

    config: Dict[str, Any] = {}

    info_list = {}
    info_dict = {}

    update_friend_event = threading.Event()
    async_update_friend_event = asyncio.Event()
    update_friend_lock = threading.Lock()
    async_update_friend_lock = asyncio.Lock()

    message_queue = asyncio.Queue()

    logger: logging.Logger = logging.getLogger(
        "plugins.%s.WeChatPcChannel" % channel_id)

    supported_message_types = {MsgType.Text, MsgType.Sticker, MsgType.Image,
                               MsgType.Link, MsgType.Voice, MsgType.Animation}

    def __init__(self, instance_id: InstanceID = None):
        super().__init__(instance_id)

        self.logger.setLevel(logging.INFO)
        self.msg_handlers = {}

        # 加载配置
        self.load_config()

        self.loop = asyncio.get_event_loop()

        self.server_start_event = threading.Event()
        self.client_online_event = threading.Event()

        self.info_list['friend'] = []
        self.info_dict['friend'] = {}

        ChatMgr.slave_channel = self

        @self.add_handler('update_friend_list')
        async def update_friend_list(msg: dict):
            self.info_list['chat'] = []

            for friend in msg['data']:
                wxid = friend['wxid']
                friend_name = "" if friend.get('wxNickName') == "null" else friend.get('wxNickName')
                friend_remark = "" if friend.get('wxRemark') == "null" else friend.get('wxRemark')
                # print(f"{wxid}, name: {friend_name}, remark: {friend_remark}\n")
                if '@chatroom' in wxid:
                    # 微信群
                    new_entity = EFBGroupChat(
                        uid=wxid,
                        name=friend_name or friend_remark or wxid
                    )
                else:
                    new_entity = EFBPrivateChat(
                        uid=wxid,
                        name=friend_name or wxid,
                        alias=friend_remark
                    )

                self.info_list['chat'].append(ChatMgr.build_efb_chat_as_group(new_entity))

            # print(self.info_list['chat'])
            self.update_friend_event.set()

        def start_server():
            nonlocal self
            asyncio.set_event_loop(self.loop)

            # 启动服务
            self.log(f"启动服务1 {self.config['server_addr']}:{self.config['server_port']}")

            ws = websockets.serve(
                ws_handler=self.handler,
                host=self.config['server_addr'],
                port=self.config['server_port'],
                create_protocol=QueryParamProtocol,
                ping_interval=2,
                loop=self.loop)

            self.loop.run_until_complete(ws)
            self.server_start_event.set()
            self.loop.run_forever()

        # self.server_start()
        try:
            t = threading.Thread(target=start_server, args=())
            t.daemon = True
            t.start()
        except:
            print_exc()

        self.logger.info(f"初始化服务 0")
        self.server_start_event.wait()
        self.logger.info(f"初始化服务 1")
        self.client_online_event.wait()
        self.logger.info(f"初始化服务 2")


    def log(self, text):
        self.logger.log(99, "\x1b[0;36m %s \x1b[0m", "{}: {}".format(self.channel_id, text))

    def add_handler(self, func_name: str):
        def wrapper(func: Callable):
            if not asyncio.iscoroutinefunction(func):
                raise Exception('Handler must be a coroutine function')

            self.msg_handlers[func_name] = func
            return func
        return wrapper

    # 发送到微信
    async def send_message_to_wechat(self, ws):
        while True:
            message = await self.message_queue.get()
            if message == 'close':
                print(f"关闭 send_message_to_wechat")
                break
            print(f"发送到wechatClient的消息 {message}")

            await ws.send(xor_string(message, key=self.config['app_key'], encode=True))

    # 发送消息到Tg
    async def send_message_to_tg(self, msg):

        print(msg)

        chat = None
        author = None

        username = "" if msg['nickname'] == "null" else msg['nickname']
        remark_name = "" if msg['remark'] == "null" else msg['remark']

        if 'chatroomname' in msg:
            group_name = "" if msg['chatroomname'] == "null" else msg['chatroomname']
            group_remark_name = "" if msg['chatroomremark'] == "null" else msg['chatroomremark']

            chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                uid=msg['from'],
                name=group_name or group_remark_name or msg['from']
            ))
            author = ChatMgr.build_efb_chat_as_member(chat, EFBGroupMember(
                name=username,
                alias=remark_name,
                uid=msg['wxid']
            ))
        else:
            chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                uid=msg['wxid'],
                name=remark_name or username or msg['wxid'],
            ))
            author = chat.other

        if 'type' in msg and msg['type'] in TYPE_HANDLERS:
            efb_msgs = TYPE_HANDLERS[msg['type']](msg)
        else:
            efb_msgs = efb_text_simple_wrapper(html.escape(msg['message']))

        for efb_msg in efb_msgs:
            efb_msg.author = author
            efb_msg.chat = chat
            efb_msg.deliver_to = coordinator.master
            if msg.get('isOwner', 1) == 1:
                efb_msg.text = f"🦚You🦚:\n\n{efb_msg.text}"
            coordinator.send_message(efb_msg)

    # 接受远端发送的消息
    async def handler(self, conn):

        local_sign = hashlib.sha256(
            f"app_id={conn.app_id}&timestamp={conn.timestamp}&app_key{self.config['app_key']}".encode()) \
            .hexdigest()

        self.logger.debug(self.config['app_id'])
        self.logger.debug(self.config['app_key'])
        self.log(f"校验客户token")
        # self.logger.info(f"获取到的token {conn.hash}")
        # self.logger.info(f"生成的token   {local_sign}")
        if conn.hash != local_sign:
            await conn.close(1011, "authentication failed")
            return

        asyncio.create_task(self.send_message_to_wechat(conn))

        # 客户端连接成功
        self.client_online_event.set()
        self.log(f"客户端链接成功")

        while True:
            try:
                recv_data = await conn.recv()
                decode_data = xor_string(recv_data, key=self.config['app_key'], decode=True)

                message = json.loads(decode_data)

                if 'msg_to' not in message:
                    continue

                # 消息需要推送到tg
                if message['msg_to'] == 'tg':
                    self.logger.info(f"收到微信消息推送 {message}")
                    await self.send_message_to_tg(message)
                elif message['msg_to'] == 'fun' and 'msg_fun' in message:
                    self.logger.info(f"收到消息 {message}")
                    fun = message['msg_fun']
                    if fun in self.msg_handlers:
                        self.loop.create_task(self.msg_handlers[fun](message))
                else:
                    pass

            except json.JSONDecodeError:
                self.logger.error(f"消息解析失败")
                continue

            except ConnectionClosed as e:
                self.logger.error(f"WS ConnectionClosed, code: {e.code}")
                if e.code == 1006:
                    # self.message_queue.put_nowait('close')
                    self.logger.error(f"连接关闭, restart")
                    # break

                await asyncio.sleep(2)

    # 加载配置
    def load_config(self):
        """
        Load configuration from path specified by the framework.
        Configuration file is in YAML format.
        """
        config_path = efb_utils.get_config_path(self.channel_id)
        if not config_path.exists():
            return
        with config_path.open() as f:
            d = yaml.full_load(f)
            if not d:
                return
            self.config: Dict[str, Any] = d

        if 'server_addr' not in self.config:
            raise EFBException("server_addr not found in config")
        if 'server_port' not in self.config:
            raise EFBException("server_port not found in config")
        if 'app_id' not in self.config:
            raise EFBException("app_id not found in config")
        if 'app_key' not in self.config:
            raise EFBException("app_key not found in config")
        if 'expire' not in self.config:
            raise EFBException("expire not found in config")

    # 发送消息到微信
    def send_message(self, msg: 'Message') -> 'Message':
        chat_uid = msg.chat.uid
        print(f"send_msg {msg}")
        # self.logger.debug(f"message.vendor_specific.get('is_mp', False): {msg.vendor_specific.get('is_mp', False)}")
        if msg.vendor_specific.get('is_mp') is not None:
            msg.chat.vendor_specific['is_mp'] = msg.vendor_specific.get(
                'is_mp')

        if msg.edit:
            pass  # todo
        if msg.type in [MsgType.Text, MsgType.Link]:
            if isinstance(msg.target, Message):  # Reply to message
                max_length = 50
                tgt_text = process_quote_text(msg.target.text, max_length)
                msg.text = "%s\n\n%s" % (tgt_text, msg.text)
                # self.iot_send_text_message(chat_type, chat_uid, msg.text
                # asyncio.run_coroutine_threadsafe(self.client.at_room_member(
                #     room_id=chat_uid,
                #     wxid=msg.target.author.uid,
                #     nickname=msg.target.author.name,
                #     message=msg.text
                # ), self.loop).result()
            else:
                self.message_queue.put_nowait(json.dumps({
                    'recv_type': 'sendmsg',
                    'data': {
                        'type': 'text',
                        'wxid': msg.chat.uid,
                        'content':msg.text
                    }
                }))

            msg.uid = str(uuid.uuid4())
            self.logger.debug(
                '[%s] Sent as a text message. %s', msg.uid, msg.text)
        return msg

    def poll(self):
        pass

    def send_status(self, status: 'Status'):
        pass

    def get_chat_picture(self, chat: 'Chat') -> BinaryIO:
        # url = self.get_friend_info('headUrl', chat.uid)
        url = ""
        if not url:
            # temp workaround
            url = "https://pic2.zhimg.com/50/v2-6afa72220d29f045c15217aa6b275808_720w.jpg"
        return download_file(url)

    def get_chat(self, chat_uid: ChatID) -> 'Chat':
        if 'chat' not in self.info_list or not self.info_list['chat']:
            self.log("Chat list is empty. Fetching...")
            self.update_friend_info()
        for chat in self.info_list['chat']:
            if chat_uid == chat.uid:
                return chat
        return None

    def get_chats(self) -> Collection['Chat']:
        if 'chat' not in self.info_list or not self.info_list['chat']:
            self.log("Chat list is empty. Fetching...")
            self.update_friend_info()

        return self.info_list['chat']

    def update_friend_info(self):
        with self.update_friend_lock:
            if 'friend' in self.info_list and self.info_list['friend']:
                return
            self.log('Updating friend info...')
            self.update_friend_event.clear()

            # 获取列表
            self.message_queue.put_nowait(json.dumps({
                'recv_type': 'FriendList'
            }))

            self.update_friend_event.wait()
            self.log('Friend retrieved. Start processing...')
