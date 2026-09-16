# planning-task-presentation Specification

## MODIFIED Requirements

### Requirement: 失败态支持恢复
失败任务 SHALL 显示经过净化的用户错误、请求是否保留及可用恢复动作。

“重试创建的是关联但独立的新 Run”由 `agent-run-lifecycle` 的 `Retry creates a new attempt` 定义；本条约束页面如何表达保留与恢复。

当失败原因是高德配额耗尽时，页面 SHALL 显示区别于通用失败文案的具体原因与额度重置时刻，使用户不必通过反复重试来试探何时可用。

#### Scenario: 可重试失败
- **WHEN** Run 以可重试错误失败
- **THEN** 页面说明原需求仍被保留并提供创建新重试 Run 的操作

#### Scenario: 重试已创建
- **WHEN** 用户重试失败任务
- **THEN** 原失败卡保持终态，新尝试作为关联但独立的任务显示

#### Scenario: 额度耗尽失败
- **WHEN** Run 因高德配额耗尽而失败
- **THEN** 页面显示耗尽的是哪个配额池与何时重置，而不是通用的“任务执行失败，请稍后重试”

#### Scenario: 需要人工处理的失败
- **WHEN** Run 因高德密钥失效或 IP 访问受限而失败
- **THEN** 页面说明该失败不会自行恢复、需要人工处理，而不是引导用户重试或等待
