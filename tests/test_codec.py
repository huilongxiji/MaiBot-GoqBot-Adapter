from __future__ import annotations

import asyncio
import base64

import pytest

from conftest import load

codec = load("codec")

PNG = b"\x89PNG\r\n\x1a\nfake"


def make_inbound(images=None, names=None):
    images = images or {}
    names = names or {}
    calls = {"images": [], "names": []}

    async def load_image(url):
        calls["images"].append(url)
        return images.get(url)

    async def resolve(gid):
        calls["names"].append(gid)
        return names.get(gid, "")

    return codec.InboundCodec(load_image, resolve), calls


def group_event(**overrides):
    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "time": 1761900000,
        "self_id": 10001,
        "group_id": 2002,
        "user_id": 3003,
        "message_id": 4004,
        "raw_message": "",
        "message": [
            {"type": "at", "data": {"qq": "10001", "name": "麦麦"}},
            {"type": "text", "data": {"text": " 你好"}},
            {"type": "image", "data": {"file": "abc.image", "subType": "0", "url": "https://img/1"}},
        ],
        "sender": {"user_id": 3003, "nickname": "", "card": "", "role": "admin"},
        "user_openid": "U_OPEN",
        "group_openid": "G_OPEN",
    }
    event.update(overrides)
    return event


def test_group_message_full_conversion():
    inbound, calls = make_inbound(images={"https://img/1": PNG}, names={"2002": "测试群"})
    result = asyncio.run(inbound.build(group_event(), platform="qq", account_id="10001", download_images=True))
    msg = result.message
    assert result.external_message_id == "4004"
    assert msg["platform"] == "qq" and msg["account_id"] == "10001" and msg["scope"] is None
    assert msg["is_at"] is True and msg["is_mentioned"] is True
    info = msg["message_info"]
    # goqbot 群消息昵称常为空：回退为 user_id，保证非空
    assert info["user_info"] == {"user_id": "3003", "user_nickname": "3003", "user_cardname": None}
    assert info["group_info"] == {"group_id": "2002", "group_name": "测试群"}
    assert info["additional_config"]["platform_io_target_group_id"] == "2002"
    assert info["additional_config"]["goqbot_sender_role"] == "admin"
    types = [s["type"] for s in msg["raw_message"]]
    assert types == ["at", "text", "image"]
    image = msg["raw_message"][2]
    assert base64.b64decode(image["binary_data_base64"]) == PNG
    assert msg["is_picture"] is True
    assert msg["processed_plain_text"] == "@麦麦 你好[图片]"
    assert calls["images"] == ["https://img/1"]


def test_group_name_fallback_and_no_download():
    inbound, calls = make_inbound()
    result = asyncio.run(inbound.build(group_event(), platform="qq_official", account_id="10001", download_images=False))
    msg = result.message
    assert msg["platform"] == "qq_official"
    assert msg["message_info"]["group_info"]["group_name"] == "群2002"
    assert msg["raw_message"][2] == {"type": "text", "data": "[图片]"}
    assert calls["images"] == []


def test_private_message_and_not_at():
    inbound, _ = make_inbound()
    event = group_event(message_type="private", sub_type="friend", group_id=None,
                        message=[{"type": "text", "data": {"text": "/help"}}],
                        sender={"user_id": 3003, "nickname": "小明"})
    msg = asyncio.run(inbound.build(event, platform="qq", account_id="10001", download_images=True)).message
    assert "group_info" not in msg["message_info"]
    assert msg["message_info"]["additional_config"]["platform_io_target_user_id"] == "3003"
    assert msg["message_info"]["user_info"]["user_nickname"] == "小明"
    assert msg["is_at"] is False and msg["is_command"] is True


def test_self_message_and_missing_ids_are_dropped():
    inbound, _ = make_inbound()
    assert asyncio.run(inbound.build(group_event(user_id=10001), platform="qq", account_id="10001",
                                     download_images=False)) is None
    assert asyncio.run(inbound.build(group_event(message_id=None), platform="qq", account_id="10001",
                                     download_images=False)) is None


def test_failed_image_download_becomes_placeholder():
    inbound, _ = make_inbound(images={})
    msg = asyncio.run(inbound.build(group_event(), platform="qq", account_id="10001", download_images=True)).message
    assert msg["raw_message"][2] == {"type": "text", "data": "[图片]"}


def test_outbound_group_plan():
    plan = codec.OutboundCodec().build(
        {
            "message_info": {"group_info": {"group_id": "2002"}},
            "raw_message": [
                {"type": "reply", "data": {"target_message_id": "4004"}},
                {"type": "at", "data": {"target_user_id": "3003"}},
                {"type": "text", "data": " 收到"},
                {"type": "image", "data": "", "binary_data_base64": "QUJD"},
                {"type": "emoji", "data": "", "binary_data_base64": "REVG"},
                {"type": "reply", "data": {"target_message_id": "9"}},
                {"type": "music", "data": {}},
            ],
        }
    )
    assert plan.chat_type == "group" and plan.target_id == 2002
    assert plan.segments == [
        {"type": "reply", "data": {"id": "4004"}},
        {"type": "at", "data": {"qq": "3003"}},
        {"type": "text", "data": {"text": " 收到"}},
        {"type": "image", "data": {"file": "base64://QUJD"}},
        {"type": "image", "data": {"file": "base64://REVG"}},
    ]
    assert plan.dropped_types == ["music"]


def test_outbound_private_and_forward_flatten():
    plan = codec.OutboundCodec().build(
        {
            "message_info": {"additional_config": {"platform_io_target_user_id": "3003"}},
            "raw_message": [
                {"type": "forward", "data": [{"content": [{"type": "text", "data": "第一条"}]},
                                             {"content": "第二条"}]},
            ],
        }
    )
    assert plan.chat_type == "private" and plan.target_id == 3003
    assert [s["data"]["text"] for s in plan.segments] == ["第一条", "第二条"]


def test_outbound_errors():
    out = codec.OutboundCodec()
    with pytest.raises(ValueError, match="缺少目标"):
        out.build({"raw_message": [{"type": "text", "data": "x"}]})
    with pytest.raises(ValueError, match="可发送"):
        out.build({"message_info": {"group_info": {"group_id": "1"}},
                   "raw_message": [{"type": "reply", "data": {"target_message_id": "2"}}]})
    with pytest.raises(ValueError, match="数字 ID"):
        out.build({"message_info": {"group_info": {"group_id": "abc"}}, "raw_message": [{"type": "text", "data": "x"}]})
