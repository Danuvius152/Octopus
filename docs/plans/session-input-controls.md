# 会话输入、有效配置与显式改方向

> **Status:** proposed — 让配置继承、运行中附件和显式改方向具有清楚反馈，沿原接纳与停止语义完成。

普通 Codex 文本 steer、Claude steering、输入 receipt 与原生输入证据已有实现。
本计划不从零建设输入调度。当前行为见[输入可靠性](../implemented/input-reliability.md)、
[steering](../implemented/inline-steering.md)与[模型设置](../implemented/model-reasoning.md)。

## 第一段：有效配置与附件边界

用户应分清“下一请求将继承的有效配置”和“当前运行已经捕获的配置”。显示
实际 backend/model/reasoning/speed 及来源；provider 默认值无法确认时保留未知，
不能把表单选择直接当作当前执行证据。编辑默认配置仍允许保存，旧会话按其实际
有效配置校验，不以所有旧组合都兼容作为保存前提。

附件要么由当前 backend 真正支持在活动请求追加，要么明确作为后续输入接纳。
保留原内容、所属请求及文件证据，不静默丢弃/截断。运行中附件支持需分别核
provider 与传输能力，不能从文本 steer 推导任意文件都能追加。

## 显式停止后重新定向

在确有需要时提供用户可理解的“停止当前工作并使用新指令继续”流程，复用原
Stop、receipt、资源退休和后续受理。先说明取消/退休到哪一步，新输入如何排队，
原工作结果若晚到仍保存到哪里。

取消一个 await 不证明旧执行结束；unknown 不能自动重放；新指令不覆盖旧结果。
自动返回仍按当前接纳顺序逐条处理，不借此增加返回合批或自动 steer。

只有新增入口或具体竞态证明必要时，才收拢前端输入编排。ChatView 行数不是
全面重写的理由。新发送入口沿原接纳路径，不能成为第二个派发者。

## 完成条件与后续范围

发送、排队、已接纳、拒绝和未知反馈一致；实际运行配置可核，附件内容与归属
可核。显式改方向不重跑 unknown、不重开旧 receipt，未受影响路径不增加模型
调用。覆盖等待问题、慢发送、取消与晚到结果的真实交错，按[测试政策](../testing.md)选择验证。

移动端只读快照和离线草稿在明确离线阅读需求下分别选择，不承诺离线执行 Agent。
来源：[上游 inline steering](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/inline-steering.md)、
[composer/model 目标](https://github.com/archeryue/Octopus/blob/fd18c3407a7c61fb19074f475b357871edf1dabb/docs/plans/composer-and-time.md)。
