# -*- coding: utf-8 -*-
"""
@author: lh900519
"""

import asyncio
import logging
import queue
import threading
import uuid
import html
import yaml
import tempfile
from traceback import print_exc
from urllib.request import urlopen

from typing import Optional, Collection, BinaryIO, Dict, Any
from datetime import datetime

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


class WechatCOMChannel(SlaveChannel):
    channel_name: str = "Wechat Pc Slave"
    channel_emoji: str = "🖥️"
    channel_id = "wechat.PcCOM"

    config: Dict[str, Any] = {}

    info_list = {}
    info_dict = {}

    update_friend_event = threading.Event()
    async_update_friend_event = asyncio.Event()
    update_friend_lock = threading.Lock()
    async_update_friend_lock = asyncio.Lock()

    message_queue = asyncio.Queue()

    update_friend_queue = queue.Queue()

    logger: logging.Logger = logging.getLogger(
        "plugins.%s.WeChatPcChannel" % channel_id)

    supported_message_types = {MsgType.Text, MsgType.Sticker, MsgType.Image,
                               MsgType.Link, MsgType.Voice, MsgType.Animation}

    def __init__(self, instance_id: InstanceID = None):
        super().__init__(instance_id)

        # 加载配置
        self.load_config()

        self.loop = asyncio.get_event_loop()

        connected_event = threading.Event()
        login_event = threading.Event()

        self.info_list['friend'] = []
        self.info_dict['friend'] = {}

        ChatMgr.slave_channel = self

    # 发送到微信客户端
    async def send_message_to_wechat(self, ws):
        while True:
            message = await self.message_queue.get()
            print(f"获取发送到wechatClient的消息 {message}")
            await ws.send(message)

    async def send_message_to_tg(self, msg):

        chat = None
        author = None

        username = "" if msg['nickname'] != "null" else msg['nickname']
        remark_name = "" if msg['remark'] != "null" else msg['remark']

        if 'chatroomname' in msg:
            group_name = "" if msg['chatroomname'] != "null" else msg['chatroomname']
            group_remark_name = "" if msg['chatroomremark'] != "null" else msg['chatroomremark']

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
            efb_msgs = efb_text_simple_wrapper(html.escape(msg['content']))

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

        self.logger.info(f"校验客户token")
        # self.logger.info(f"获取到的token {conn.hash}")
        # self.logger.info(f"生成的token   {local_sign}")
        if conn.hash != local_sign:
            await conn.close(1011, "authentication failed")
            return

        self.logger.info(f"客户端链接成功")

        asyncio.create_task(self.send_message_to_wechat(ws))

        while True:
            try:
                recv_data = await conn.recv()
                message = json.loads(recv_data)

                # 微信消息推送
                if replay in message and message['replay'] == 0:
                    # {"time": "2022-07-04 15:36:12", "type": 1, "isSendMsg": 0, "wxid": "snowfire", "from": "snowfire", "message": "i", "filepath": "", "nickname": "\u5218\u6d77\u6ce2", "alias": "null"}
                    self.logger(f"收到微信消息推送 {message}")

                # self.logger.debug(f"on recv message: {recv_data}")
                # message = input('please input:')
                # await conn.send(message)
            except json.JSONDecodeError:
                self.logger.error(f"消息解析失败")
                continue

            except ConnectionClosed as e:
                self.logger.error(f"WS ConnectionClosed, code: {e.code}")
                if e.code == 1006:
                    self.logger.error(f"连接关闭, restart")
                    await asyncio.sleep(2)
                    break

    # 启动服务
    def server_start(self):
        self.logger.info(
            f"启动服务 {self.config['server_addr']}:{self.config['server_port']}")

        ws = websockets.serve(
            ws_handler=self.handler,
            host=self.config['server_addr'],
            port=self.config['server_port'],
            create_protocol=QueryParamProtocol,
            ping_interval=2)

        asyncio.get_event_loop().run_until_complete(ws)
        asyncio.get_event_loop().run_forever()

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
                # # self.iot_send_text_message(chat_type, chat_uid, msg.text
                # asyncio.run_coroutine_threadsafe(self.client.at_room_member(
                #     room_id=chat_uid,
                #     wxid=msg.target.author.uid,
                #     nickname=msg.target.author.name,
                #     message=msg.text
                # ), self.loop).result()
            else:
                pass
                # asyncio.run_coroutine_threadsafe(self.client.send_text(
                #     wxid=msg.chat.uid,
                #     content=msg.text
                # ), self.loop).result()
            msg.uid = str(uuid.uuid4())
            self.logger.debug(
                '[%s] Sent as a text message. %s', msg.uid, msg.text)
        return msg

    def poll(self):
        pass

    def send_status(self, status: 'Status'):
        pass

    def get_chat_picture(self, chat: 'Chat') -> BinaryIO:
        url = self.get_friend_info('headUrl', chat.uid)
        if not url:
            # temp workaround
            url = "https://pic2.zhimg.com/50/v2-6afa72220d29f045c15217aa6b275808_720w.jpg"
        return download_file(url)

    def get_chat(self, chat_uid: ChatID) -> 'Chat':
        if 'chat' not in self.info_list or not self.info_list['chat']:
            self.logger.debug("Chat list is empty. Fetching...")
            self.update_friend_info()
        for chat in self.info_list['chat']:
            if chat_uid == chat.uid:
                return chat
        return None

    def get_chats(self) -> Collection['Chat']:
        if 'chat' not in self.info_list or not self.info_list['chat']:
            self.logger.debug("Chat list is empty. Fetching...")
            self.update_friend_info()
        return self.info_list['chat']

    def update_friend_info(self):
        return
        with self.update_friend_lock:
            if 'friend' in self.info_list and self.info_list['friend']:
                return
            self.logger.debug('Updating friend info...')
            self.update_friend_event.clear()
            asyncio.run_coroutine_threadsafe(
                self.client.get_friend_list(), self.loop).result()
            self.update_friend_event.wait()
            self.logger.debug('Friend retrieved. Start processing...')
            self.process_friend_info()
            self.update_friend_event.clear()
