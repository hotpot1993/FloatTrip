# planning-concurrency Specification

## MODIFIED Requirements

### Requirement: Bounded provider concurrency
The runtime SHALL enforce separate configurable limits for LLM calls and AMap calls, including calls created concurrently within one planning run.

两类容量的生效位置 MUST 各自唯一：LLM 与 AMap 的并发约束由共享的提供方槽位实现，调度器 MUST NOT 持有第二份仅被声明而无人使用的 AMap 容量。

提供方并发约束的是同时在飞的请求数，而不是单位时间内的请求数；高德额度保护由 `amap-quota-guard` 定义，两者职责不同且 MUST NOT 相互替代。

#### Scenario: Multi-day meal recommendation
- **WHEN** meal recommendations for several days are eligible to execute concurrently
- **THEN** the runtime executes them asynchronously without exceeding the configured LLM-call limit

#### Scenario: Run cancellation releases capacity
- **WHEN** a running planning run is cancelled and its task settles
- **THEN** its run, LLM, and provider capacity permits are released for other eligible work

#### Scenario: 手动编辑请求共享同一并发约束
- **WHEN** 用户触发的手动编辑请求与规划任务同时发出高德调用
- **THEN** 两者受同一个 AMap 并发约束，而不是各自独立

#### Scenario: 并发约束不充当额度保护
- **WHEN** 提供方并发槽位全部空闲但某池额度已用尽
- **THEN** 该池的请求仍被拒绝，因为并发约束不负责额度
