#!/usr/bin/env python3
"""对 LiteLLM 的运行时补丁。两处，补之前请先读完这段。

补丁一（Anthropic 方向，codex-on-anthropic / claude-on-openai 用）见下；
补丁二（OpenAI 方向，codex-on-openai 用）见文件后半 `fix_tool_call_adjacency`。

## 补什么

LiteLLM 1.96.0 把 Responses/Chat 的 assistant 轮转成 Anthropic content block 时，
块的顺序是 **tool_use 在前、text 在后**：

    {"role": "assistant", "content": [
        {"type": "tool_use", ...},
        {"type": "text", "text": "I'll start by reading separable.py"}]}

真实模型自己的输出永远是反过来的（先说话、再调工具）。而 Anthropic 的 API 把
assistant 消息**结尾的 text 块**当作 prefill（让模型接着这段话往下写）。于是：

    400 invalid_request_error
    "This model does not support assistant message prefill.
     The conversation must end with a user message."

## 为什么非补不可

1. 只有多轮才会炸。第一轮没有 assistant 历史，第二轮往往只有 tool_use 没有 text，
   所以冒烟 1～2 轮全绿，**跑到第三轮才崩** —— 500 条跑到一半白烧的那种崩。
2. 报错信息在撒谎。它说「对话必须以 user 消息结尾」，而实际请求确实是以
   user + tool_result 结尾的（实测抓包确认）。照着报错去查会一路查错方向。
3. 不是所有模型都会炸。支持 prefill 的模型照单全收，Opus 5 这类不支持的直接 400。
   换个模型就好了 —— 这会让人误判成「模型不行」。

实测（relay.lzbrainary.com + claude-opus-5，同一份 26k token 的真实请求体）：
    原样重放                    → 400
    把 text 块排到 tool_use 之前 → 200
    删掉 assistant 的 text 块    → 200

## 顺序规则

    thinking / redacted_thinking  →  text  →  其它（tool_use…）

thinking 必须排在最前 —— 开 extended thinking 时 Anthropic 强制要求 assistant
消息的第一个块是 thinking，否则报 "Expected thinking or redacted_thinking"。
用稳定排序，同组内部的原有相对顺序不动。

## 上游修了怎么办

`apply()` 是幂等的、只在类方法上包一层。LiteLLM 哪天自己改对了，这个补丁做的排序
就是个空操作，可以直接删掉本文件并去掉 bridge.py 里的 bootstrap。
"""
from __future__ import annotations

# 数字越小越靠前；没列出来的（tool_use、server_tool_use、image…）都排最后
_RANK = {"thinking": 0, "redacted_thinking": 0, "text": 1}


def reorder_assistant_blocks(messages: list) -> list:
    """把每条 assistant 消息里的 content block 按 _RANK 稳定排序。就地改，也返回。"""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list) or len(content) < 2:
            continue
        m["content"] = sorted(
            content,
            key=lambda b: _RANK.get(b.get("type") if isinstance(b, dict) else None, 2))
    return messages


# ---------------------------------------------------------------------------
# 补丁二：OpenAI 方向（Responses → ChatCompletions）
# ---------------------------------------------------------------------------
# LiteLLM 1.96.0 把 Codex 的 Responses input 转成 chat messages 时，会在
# 「带 tool_calls 的 assistant」和「对应的 tool 结果」之间插一条**空的 assistant
# 文本消息**（Codex 每个 function_call 旁边都跟着一个 message item，正文常常是空串）：
#
#     [4] assistant  tool_calls=[call_00_xxx]
#     [5] assistant  content=[{"type":"text","text":""}]     ← 插进来的
#     [6] tool       tool_call_id=call_00_xxx
#
# 而 OpenAI 协议要求 tool 消息**紧跟**在发起它的 assistant 之后。DeepSeek（以及
# OpenAI 官方、多数兼容端点）直接 400：
#
#     An assistant message with 'tool_calls' must be followed by tool messages
#     responding to each 'tool_call_id'. (insufficient tool messages following
#     tool_calls message)
#
# 和补丁一一样，**第一轮永远不会炸**（还没有 assistant 历史），要等 agent 真的调了
# 一次工具、进第二轮请求才崩 —— 冒烟一问一答全绿，一上手干活就废。
#
# 两步修：先删掉纯空的 assistant 消息，再把 tool 结果提到对应 assistant 后面。
# 非空的 assistant 文本不删，只是被挤到 tool 结果之后，这在协议上是合法的。

def _is_blank_assistant(m) -> bool:
    """assistant 且既没有 tool_calls 也没有任何非空文本 —— 就是被插进来的那种。"""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return False
    if m.get("tool_calls") or m.get("function_call"):
        return False
    c = m.get("content")
    if c is None:
        return True
    if isinstance(c, str):
        return not c.strip()
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict):
                if b.get("type") in (None, "text", "output_text", "input_text"):
                    if (b.get("text") or "").strip():
                        return False
                else:
                    return False  # 图片之类的非文本块，保守起见不删
            elif str(b).strip():
                return False
        return True
    return False


def _as_blocks(c) -> list:
    """把 content 归一成 block 列表，方便拼接。None → []，str → 一个 text 块。"""
    if c is None:
        return []
    if isinstance(c, str):
        return [{"type": "text", "text": c}] if c.strip() else []
    if isinstance(c, list):
        return list(c)
    return [c]


def merge_adjacent_assistants(messages: list) -> list:
    """把**连续**的 assistant 消息合并成一条（content 拼接、tool_calls 拼接）。

    为什么必须有这一步：Codex 的 Responses input 里，一轮 assistant 会被拆成两个 item
    —— 一个 `message`（说的话）和一个 `function_call`（调的工具），而且**顺序不保证**
    是「先说话后调用」。LiteLLM 原样转成两条 chat assistant 消息：

        [1] assistant  tool_calls=[call_A]
        [2] assistant  content="I'll start by examining ..."
        [3] tool       tool_call_id=call_A

    接着 fix_tool_call_adjacency 为了满足 OpenAI 的「tool 必须紧跟 assistant」，
    把 [3] 提到 [1] 后面，于是那条纯文本 assistant 被挤到了**整个数组的最后**。
    到了 Anthropic 方向，「以 assistant 结尾」就是 prefill：

        400 "This model does not support assistant message prefill.
             The conversation must end with a user message."

    补丁一（块内排序）救不了这个 —— 它只管一条消息内部的块顺序，管不了消息之间。
    合并成一条 assistant{content, tool_calls} 之后：既满足 OpenAI 的邻接要求，
    转到 Anthropic 也是一条正常的 [text, tool_use] assistant 轮，两边都合法。
    """
    if not isinstance(messages, list):
        return messages
    out: list = []
    for m in messages:
        prev = out[-1] if out else None
        if (isinstance(m, dict) and isinstance(prev, dict)
                and m.get("role") == "assistant" and prev.get("role") == "assistant"):
            blocks = _as_blocks(prev.get("content")) + _as_blocks(m.get("content"))
            merged = dict(prev)
            merged["content"] = blocks or None
            calls = (prev.get("tool_calls") or []) + (m.get("tool_calls") or [])
            if calls:
                merged["tool_calls"] = calls
            out[-1] = merged
        else:
            out.append(m)
    return out


def fix_tool_call_adjacency(messages: list) -> list:
    """删空 assistant，并让每条 tool 结果紧跟发起它的 assistant。就地重建，返回新列表。"""
    if not isinstance(messages, list):
        return messages

    kept = merge_adjacent_assistants([m for m in messages if not _is_blank_assistant(m)])

    out: list = []
    i = 0
    while i < len(kept):
        m = kept[i]
        out.append(m)
        i += 1
        tool_calls = m.get("tool_calls") if isinstance(m, dict) else None
        if not tool_calls or (isinstance(m, dict) and m.get("role") != "assistant"):
            continue
        want = [t.get("id") for t in tool_calls if isinstance(t, dict) and t.get("id")]
        if not want:
            continue
        # 从后面把匹配的 tool 结果按 tool_calls 的顺序捞上来，其余保持原相对次序
        pulled = {}
        rest = []
        for n in kept[i:]:
            tid = n.get("tool_call_id") if isinstance(n, dict) else None
            if (isinstance(n, dict) and n.get("role") == "tool"
                    and tid in want and tid not in pulled):
                pulled[tid] = n
            else:
                rest.append(n)
        if pulled:
            out.extend(pulled[t] for t in want if t in pulled)
            kept = kept[:i] + rest
    return out


# ---------------------------------------------------------------------------
# 补丁三：Anthropic 方向的 prompt cache 断点
# ---------------------------------------------------------------------------
# Anthropic 只对**显式带 cache_control 的块**做前缀缓存，而 LiteLLM 从 OpenAI/
# Responses 消息转过去时一个都不加 —— agent 每一轮都要把整段历史按未命中价重付。
#
# LiteLLM 自带的 `cache_control_injection_points` 在这条链路上**不能用**：它作用在
# chat 消息上，而 (a) role=system 的那条会被搬进 Anthropic 的顶层 `system`，
# cache_control 半路丢掉；(b) 落到 role=tool 的消息时它写的是**消息级**
# `cache_control`，转成 tool_result 块的时候同样丢掉。实测抓包：9 次请求里只有 1 次
# 断点真的到了上游，命中率卡在 65%。
#
# 所以自己在**最终的 Anthropic body** 上打断点 —— 这里 system/messages 的形状已经
# 定死，不会再被谁改一道。三个位置：
#   system —— 每轮都一样，稳定命中
#   prev   —— 上一轮的最后一条消息。**主力**：Anthropic 只在本次请求显式标了
#             cache_control 的位置上查缓存，而 last 是本轮新产生的、从没写进过缓存，
#             必然 miss；标住上一轮的落点才有得命中。
#   last   —— 把本轮新增的写进缓存，供下一轮的 prev 命中。
# Anthropic 上限 4 个断点，用 3 个，留一个余量。

# ---------------------------------------------------------------------------
# 补丁四：丢掉「无主」的 tool_result
# ---------------------------------------------------------------------------
# 中转会往请求里**注入服务端 web_search 工具**。模型一旦调用，回包里是一对
# server_tool_use(srvtoolu_…) + web_search_tool_result。这对块在
# Anthropic → Responses → Codex → Responses → Anthropic 兜一圈之后只剩下半边：
# tool_result 还在，发起它的 server_tool_use 没了。于是下一轮：
#
#   400 messages.20.content.0: unexpected `tool_use_id` found in `tool_result`
#       blocks: srvtoolu_01Sc… Each `tool_result` block must have a corresponding
#       `tool_use` block in the previous message.
#
# 2026-08-10 那轮 30 条里有 6 条（20%）死在这上面，而且是**确定性**的 ——
# 只要模型调了一次搜索，这条实例就废了。
#
# 这里把「上一条 assistant 里找不到对应 tool_use」的 tool_result 直接删掉：
# 模型损失的只是那次搜索结果（本来对解题也没用，仓库代码都在本地），
# 但对话重新合法，agent 能继续跑完。清空的消息整条丢掉，免得留下空 user 轮。

def _tool_use_ids(msg) -> set:
    """一条 assistant 消息里所有能被 tool_result 引用的 id。"""
    ids = set()
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return ids
    for b in msg.get("content") or []:
        if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"):
            if b.get("id"):
                ids.add(b["id"])
    return ids


# 服务端工具产生的块。这些是 Anthropic 自己在 assistant 轮里执行并填好的，
# **客户端不该回传**，更不该为它们造 tool_result。
_SERVER_BLOCKS = {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}


def _is_server_id(tid) -> bool:
    return isinstance(tid, str) and tid.startswith("srvtoolu_")


def drop_orphan_tool_results(messages: list) -> list:
    """清掉服务端工具的残骸 + 无主 tool_result。返回新列表。

    两条规则，缺一不可（只做第二条时 tutanota 那条仍然 400）：

    1. assistant 轮里的 server_tool_use / *_tool_result 块整个删掉。Codex round-trip
       不回去，留着只会让下一轮出现半边配对。
    2. user 轮里的 tool_result，满足任一条件就删：
       a) `tool_use_id` 是 `srvtoolu_…` —— **哪怕上一条 assistant 里真有同 id 的
          server_tool_use**。Anthropic 不接受客户端为服务端工具补 tool_result，
          配对完整也照样 400（实测：只按「无主」删，tutanota 仍报
          `messages.58.content.0: unexpected tool_use_id ... srvtoolu_01DKRjq…`）。
       b) 上一条 assistant 里找不到对应的 tool_use（普通的无主情况）。
    """
    if not isinstance(messages, list):
        return messages
    out: list = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            out.append(m)
            continue

        if m.get("role") == "assistant":
            kept = [b for b in content
                    if not (isinstance(b, dict) and b.get("type") in _SERVER_BLOCKS)]
            if len(kept) != len(content):
                m = dict(m, content=kept)
            if not kept:
                continue
            out.append(m)
            continue

        if m.get("role") != "user":
            out.append(m)
            continue

        # 只认**紧邻**的上一条 assistant —— Anthropic 的要求就是 "previous message"
        allowed = _tool_use_ids(out[-1]) if out else set()
        kept = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if _is_server_id(tid) or tid not in allowed:
                    continue
            kept.append(b)
        if not kept:
            continue                      # 整条空了就别留，否则是个空 user 轮
        if len(kept) != len(content):
            m = dict(m, content=kept)
        out.append(m)
    return out


_CACHE_CONTROL = {"type": "ephemeral"}

# 允许挂 cache_control 的块类型。thinking 块不在其列（挂上去 Anthropic 会 400）。
_CACHEABLE = {"text", "image", "document", "tool_use", "tool_result"}


def _mark_last_block(blocks) -> bool:
    """给块列表的最后一个可缓存块打断点。返回是否真打上了。"""
    if not isinstance(blocks, list):
        return False
    for b in reversed(blocks):
        if isinstance(b, dict) and b.get("type") in _CACHEABLE:
            if b.get("cache_control") is None:
                b["cache_control"] = dict(_CACHE_CONTROL)
            return True
        return False  # 最后一块不可缓存（thinking 等）就不往前找了，位置不对
    return False


def inject_cache_control(body: dict, points) -> dict:
    """在 Anthropic 请求体上打 prompt cache 断点。就地改，也返回。"""
    if not isinstance(body, dict) or not points:
        return body
    if "system" in points:
        _mark_last_block(body.get("system"))
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for name, idx in (("prev", -3), ("last", -1)):
            if name in points and len(msgs) >= -idx and isinstance(msgs[idx], dict):
                _mark_last_block(msgs[idx].get("content"))
    return body


def _cache_points() -> set:
    """断点集合由 bridge.py 通过环境变量下发（补丁拿不到命令行参数）。"""
    import os
    raw = os.environ.get("BRIDGE_CACHE_POINTS")
    if raw is None:
        raw = "system,prev,last"
    return {p.strip() for p in raw.split(",") if p.strip()}


def apply() -> bool:
    """给两个 transform 各包一层。幂等；返回是否真的打上了（任一打上即 True）。"""
    return _apply_anthropic_block_order() | _apply_openai_tool_adjacency()


def _apply_anthropic_block_order() -> bool:
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    orig = AnthropicConfig.transform_request
    if getattr(orig, "_bridge_patched", False):
        return False
    points = _cache_points()

    def transform_request(self, *args, **kwargs):
        body = orig(self, *args, **kwargs)
        if isinstance(body, dict):
            # 顺序有讲究：先删无主 tool_result（可能整条消息消失），再排块序，
            # 最后才打 cache 断点 —— 断点要落在**最终**的 messages[-1]/[-3] 上。
            if isinstance(body.get("messages"), list):
                body["messages"] = drop_orphan_tool_results(body["messages"])
            reorder_assistant_blocks(body.get("messages"))
            inject_cache_control(body, points)
        return body

    transform_request._bridge_patched = True
    AnthropicConfig.transform_request = transform_request
    return True


def _apply_openai_tool_adjacency() -> bool:
    from litellm.responses.litellm_completion_transformation.transformation import (
        LiteLLMCompletionResponsesConfig,
    )

    name = "transform_responses_api_request_to_chat_completion_request"
    orig = getattr(LiteLLMCompletionResponsesConfig, name)
    if getattr(orig, "_bridge_patched", False):
        return False

    def wrapper(*args, **kwargs):
        body = orig(*args, **kwargs)
        if isinstance(body, dict) and isinstance(body.get("messages"), list):
            body["messages"] = fix_tool_call_adjacency(body["messages"])
        return body

    wrapper._bridge_patched = True
    setattr(LiteLLMCompletionResponsesConfig, name, staticmethod(wrapper))
    return True


if __name__ == "__main__":
    # 自测：不起服务，纯函数验两个补丁
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "shell", "input": {}},
            {"type": "text", "text": "looking"},
            {"type": "thinking", "thinking": "hmm"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
    ]
    got = [b["type"] for b in reorder_assistant_blocks(msgs)[1]["content"]]
    assert got == ["thinking", "text", "tool_use"], got
    print("ok block order:", got)

    # 补丁二：这就是 logs/rec/002-req.json 抓到的真实序列
    seq = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "do it"}]},
        {"role": "assistant", "tool_calls": [
            {"id": "call_A", "type": "function",
             "function": {"name": "exec_command", "arguments": "{}"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        {"role": "tool", "tool_call_id": "call_A", "content": "SWEPRO"},
    ]
    fixed = fix_tool_call_adjacency(seq)
    shape = [(m["role"], m.get("tool_call_id") or bool(m.get("tool_calls"))) for m in fixed]
    assert shape == [("system", False), ("user", False),
                     ("assistant", True), ("tool", "call_A")], shape
    print("ok adjacency:", shape)

    # 非空的 assistant 文本不能被删，只能被挤到 tool 之后
    seq2 = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "assistant", "content": "thinking out loud"},
        {"role": "tool", "tool_call_id": "c2", "content": "second"},
        {"role": "tool", "tool_call_id": "c1", "content": "first"},
    ]
    got2 = [(m["role"], m.get("tool_call_id")) for m in fix_tool_call_adjacency(seq2)]
    assert got2 == [("assistant", None), ("tool", "c1"), ("tool", "c2")], got2
    print("ok multi-call:", got2)

    # 补丁三：logs/rec2/002-req.json 抓到的真实序列 —— Codex 把一轮 assistant 拆成
    # function_call + message 两个 item，合并前会被挤成「以 assistant 结尾」→ 400
    seq3 = [
        {"role": "user", "content": [{"type": "text", "text": "do it"}]},
        {"role": "assistant", "tool_calls": [
            {"id": "call_A", "type": "function",
             "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "assistant", "content": "I'll start by examining the source."},
        {"role": "tool", "tool_call_id": "call_A", "content": "..."},
    ]
    fixed3 = fix_tool_call_adjacency(seq3)
    roles3 = [m["role"] for m in fixed3]
    assert roles3 == ["user", "assistant", "tool"], roles3
    assert fixed3[-1]["role"] != "assistant", "以 assistant 结尾 = Anthropic prefill 400"
    assert fixed3[1]["tool_calls"] and fixed3[1]["content"], fixed3[1]
    print("ok merge+no-prefill:", roles3)

    # 补丁三：断点必须落在 system 末块、倒数第三条和最后一条消息的末块上
    body = {
        "system": [{"type": "text", "text": "sys A"}, {"type": "text", "text": "sys B"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t2"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2"}]},
        ],
    }
    inject_cache_control(body, {"system", "prev", "last"})
    marked = [("system", 1)] if body["system"][1].get("cache_control") else []
    marked += [i for i, m in enumerate(body["messages"])
               if any(b.get("cache_control") for b in m["content"])]
    assert marked == [("system", 1), 2, 4], marked
    assert body["system"][0].get("cache_control") is None
    assert len([1 for m in body["messages"]
                for b in m["content"] if b.get("cache_control")]) == 2
    print("ok cache points:", marked)

    # thinking 块不能挂 cache_control（Anthropic 会 400），且不往前找
    b2 = {"messages": [{"role": "assistant",
                        "content": [{"type": "text", "text": "x"},
                                    {"type": "thinking", "thinking": "y"}]}]}
    inject_cache_control(b2, {"last"})
    assert all(b.get("cache_control") is None for b in b2["messages"][0]["content"]), b2
    print("ok cache skips thinking")

    # 补丁四：中转注入 web_search 之后的真实形状 —— tool_result 引用的
    # srvtoolu_ 在上一条 assistant 里找不到，必须删掉这个块
    msgs4 = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "shell"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "srvtoolu_ORPHAN", "content": "web"},
        ]},
        # 整条都是无主 tool_result → 这条 user 消息应该整个消失
        {"role": "assistant", "content": [{"type": "text", "text": "hm"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "srvtoolu_ORPHAN2", "content": "web"}]},
    ]
    got4 = drop_orphan_tool_results(msgs4)
    assert len(got4) == 4, [m["role"] for m in got4]
    ids4 = [b.get("tool_use_id") for b in got4[2]["content"]]
    assert ids4 == ["t1"], ids4
    assert got4[-1]["role"] == "assistant", got4[-1]
    print("ok orphan tool_result dropped:", ids4)

    # 配对完整的 server tool 也必须清掉 —— 这正是 tutanota 那条第二次仍然 400 的原因
    msgs5 = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "searching"},
            {"type": "server_tool_use", "id": "srvtoolu_OK", "name": "web_search"},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_OK"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "srvtoolu_OK", "content": "r"}]},
    ]
    got5 = drop_orphan_tool_results(msgs5)
    assert [m["role"] for m in got5] == ["user", "assistant"], [m["role"] for m in got5]
    assert [b["type"] for b in got5[1]["content"]] == ["text"], got5[1]["content"]
    print("ok server-tool artifacts stripped even when paired")
