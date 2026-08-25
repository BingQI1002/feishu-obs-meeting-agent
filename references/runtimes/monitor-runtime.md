# 运行环境：持久 Monitor

## 目标

让具备持久 Monitor 的 Agent 宿主直接运行会议监听器，并把监听器 stdout 中的每个语义批次变成新的 Agent 回合。适用于 Moma 插件或其他提供“持续命令输出 → task notification → Agent 回合”能力的环境；模型名称不是判断依据。

监听器已经负责轮询、去重、合并 ASR 碎片、持久化游标并输出 `feishu_meeting_batch`。Monitor 只负责把 stdout 重新送回 Agent，不要再建立一套文件监听逻辑。

## 必须满足的能力

使用本适配器前，先确认当前宿主能够：

- 以持久模式运行一个长命令；
- 在命令产生新 stdout 时自动生成 task notification；
- 让该 notification 回到启动会议的同一 Agent 会话；
- 在用户停止或会议结束时终止并回收该 Monitor。

缺少任一项时，只能报告监听器能抓取事件，不能声称已经实现主动会议 Agent。

## 启动方式

1. 为当前 Agent 建立独立状态目录，建议使用 `<state_root>/<meeting_id>-monitor-<runtime_id>`。不要与 Codex、其他 Agent 或旧会议共用目录，也不要只用会议号命名。
2. 让 Monitor **直接**运行监听器，并启用持久模式：

   ```bash
   python3 "<skill目录>/scripts/watch_meeting_events.py" \
     --meeting-id <meeting_id> \
     --identity <bot|user> \
     --profile <profile> \
     --state-dir "<独立状态目录>" \
     --poll-seconds 5
   ```

3. 保存 Monitor task id、状态目录和会议身份到 `agent-state.md`。
4. 不给命令添加 `&`，不另起 `tail` 进程，不监视 `events.jsonl` 行数，也不启动第二个 watcher。Monitor 管进程生命周期，监听器管事件生命周期。

## 每次唤醒怎样处理

task notification 可能包含一行或多行 JSON，按 stdout 顺序逐行处理。

### `feishu_meeting_batch`

1. 把 batch 视为内部会议事件，不把 notification 原文直接回复给用户。
2. 立即进入主 Skill 的“持续判断循环”，更新会议问题、事实、分歧、决定、OBS 证据和待回流内容。
3. 不值得打断时，不在 Moma/Claude 对话或飞书发送“继续听”“保持安静”等占位内容；保存必要状态后结束当前回合，等待同一个 Monitor 的下一次唤醒。
4. 值得介入时，按已授权身份真实发送飞书私信；成功后把消息 ID、幂等键、关联事件和判断摘要写入 `agent-state.md`。
5. 判断完成后继续沿用原 Monitor，不重启监听器。

Agent 在宿主聊天框里生成了一段文字，不等于飞书已经送达。只有 `lark-cli` 返回成功消息 ID，才能记录为已发送。

### `feishu_meeting_watcher_error`

监听器自身会退避重试。相同错误没有新信息时不重复提醒用户，也不因为一次错误启动第二个 watcher；达到主 Skill 的不可恢复条件后再停止并报告。

### `feishu_meeting_ended`

1. 确认 Monitor 命令已经退出；仍在运行时停止该 task，不影响其他 Agent 的状态目录或进程。
2. 执行尾部保全、会后深读、`writeback-candidate.md` 和飞书会后小结。
3. 在 `agent-state.md` 清除活动 task id，记录结束状态和会后消息 ID。

### `feishu_meeting_watcher_stopped`

只有用户明确停止或运行环境终止时才按停止处理；保留已经取得的内容并说明覆盖范围。

## 用户中途发消息与会话恢复

用户的新消息优先处理，但除非明确停止，不结束 Monitor。处理完普通问题后继续等待同一个 task。

上下文压缩或会话恢复时，先读 `agent-state.md` 和监听器状态：

- 原 Monitor 仍有效：继续接收，不启动第二个；
- 原 Monitor 已失效但会议仍在：使用同一独立状态目录重启，依靠游标和事件 ID 去重；
- 会议已经结束：不恢复监听，直接完成会后流程。

## 安装后的最低验证

监听器或本适配器首次安装、升级后，必须在 **真实 Monitor** 中做一次回放；在普通终端看见 stdout 不算唤醒闭环。

使用现有 fixture，而不是本场 `events.jsonl`：

```bash
python3 "<skill目录>/scripts/watch_meeting_events.py" \
  --meeting-id fixture-meeting \
  --identity bot \
  --profile <profile> \
  --state-dir "<临时独立状态目录>" \
  --fixture "<skill目录>/tests/fixtures/codex_runtime_replay.json" \
  --fixture-interval 1
```

验收必须同时看到：

1. Monitor 收到普通 batch 并唤醒同一 Agent；
2. 普通内容可以保持静默，而不是输出占位回复；
3. 有价值 batch 确实进入判断循环；离线回放默认不发送真实飞书，可检查“准备发送”的判断与幂等信息；
4. `feishu_meeting_ended` 触发结束流程；
5. Monitor task 退出，没有残留 tail、watcher 或错误锁。

真实飞书送达仍需在一场获授权的最小会议中单独验证一次。fixture 只验证 Monitor 唤醒和判断链，不替代外部写操作验收。

`events.jsonl` 是监听器已经拆出的事件日志，不是 `--fixture` 所需的会议快照数组，不能直接拿来回放。

## 支持边界

本参考只解决“Skill 已被当前 Agent 加载后，如何持续接收监听器 stdout”。Skill 的安装、自动发现、模型能力和飞书身份授权仍需由对应宿主分别验证。
