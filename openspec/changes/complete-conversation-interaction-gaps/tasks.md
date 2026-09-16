## 1. 运行中新增约束的显式选择

- [ ] 1.1 决定决策由 Chat graph 发出结构化 action event，还是由独立 API 依据绑定目标创建
- [ ] 1.2 为决策事件扩展 `app/runtime/models.py` 的自定义事件白名单，并保证不含内部推理
- [ ] 1.3 在对话时间线实现决策卡：停止并按新要求重新规划 / 完成后创建修改任务 / 取消本次变更
- [ ] 1.4 复用 `frontend/style.css` 中现有的 `.change-decision` 样式，或删除该死样式
- [ ] 1.5 增加后端事件契约与前端状态投影测试

## 2. Run 卡的产品阶段进度

- [ ] 2.1 用 `ChatState.PRODUCT_STAGES` 与 `completed_product_stages` 重写 `RuntimeRunCard` 的进度区
- [ ] 2.2 默认隐藏内部节点名与 7 站循环标签，仅在可选诊断详情中保留
- [ ] 2.3 验证 `planner` / `reviewer` 多轮交替时阶段不倒退，且文案不出现内部循环
- [ ] 2.4 增加阶段推进与不倒退的前端测试

## 3. 关键状态播报包含任务名称

- [ ] 3.1 让 `waiting_user` 的 live region 播报包含任务名称与所需动作
- [ ] 3.2 保持 token streaming、heartbeat 与每个内部 stage 不触发播报

## 4. 次要差距

- [ ] 4.1 为「历史行程」「我的画像」补 continuation，使登录后回到目标页
- [ ] 4.2 失败卡读取 `error_public.retryable`，不可重试时改用说明性文案而非重试按钮
- [ ] 4.3 删除 `HomePage` 死代码与 `frontend/chat-state.js` 中后端从不产出的 `dates_or_days` 标签
- [ ] 4.4 更新或删除 `docs/conversation-entry-migration.md` 中与现状不符的描述
- [ ] 4.5 决定并引入前端 DOM 组件测试设施，为断言的字符串存在式测试补上真实交互覆盖
