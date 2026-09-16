## MODIFIED Requirements

### Requirement: Structured planning brief
系统 SHALL 维护一个可编辑的 PlanningBrief，承载从对话中收集的结构化需求、其 readiness 状态与来源对话。结构化需求 SHALL 包含目的地、开始日期、结束日期，以及可选的抵达时刻与返程时刻。

#### Scenario: Update a brief from a follow-up answer
- **WHEN** 用户为一份正在收集的需求单补充了此前被询问的日期
- **THEN** 系统更新该需求单，而不是创建一份重复的需求单

#### Scenario: Optional preferences are absent
- **WHEN** 目的地与必需的日期信息完整，但可选的预算或餐饮偏好缺失
- **THEN** 需求单可以按已记录的默认处理变为可提交

#### Scenario: Duration is present without calendar dates
- **WHEN** 用户提供了目的地与旅行天数，但没有给出具体的开始与结束日期
- **THEN** 需求单保持收集状态，并指出仍缺少正式规划所需的日历日期

#### Scenario: Concrete date range completes the brief
- **WHEN** 需求单包含目的地与有效的开始、结束日期
- **THEN** 即使可选偏好缺失，需求单也变为可提交

#### Scenario: 抵达与返程时刻缺失
- **WHEN** 需求单包含目的地与有效日期范围，但没有抵达时刻或返程时刻
- **THEN** 需求单仍变为可提交，且需求摘要标明这两个时刻未提供

#### Scenario: 抵达与返程时刻随需求单冻结
- **WHEN** 用户提交一份含抵达或返程时刻的需求单
- **THEN** 这两个时刻进入不可变的 Run 请求快照，与起止日期享有同样的冻结语义
