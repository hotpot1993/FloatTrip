## 1. 后端删除能力

- [x] 1.1 在 `app/core/memory.py` 新增删除单条行程的操作：仅当行程属于该用户时才删除，返回是否命中
- [x] 1.2 在同一操作内先清空 `runs.result_itinerary_id` 指向该行程的引用，再删除行程本体
- [x] 1.3 在 `app/api/history_routes.py` 新增 `DELETE /api/history/{plan_id}`，用 `BEGIN IMMEDIATE` 把查所有权、清空引用与删除行程收进同一事务
- [x] 1.4 沿用既有鉴权与错误语义：未登录 401、不存在 404「行程不存在」、非本人 403「无权访问」，重复删除返回 404
- [x] 1.5 新增 pytest：删除自己的行程成功且该行消失
- [x] 1.6 新增 pytest：存在 `runs.result_itinerary_id` 引用时删除仍然成功，且该 Run 的引用被置空、Run 行与 `run_events` 均保留
- [x] 1.7 新增 pytest：删除他人行程返回 403 且行程未被删除；删除不存在的行程返回 404
- [x] 1.8 新增 pytest：删除一条被修改版派生的行程后，修改版仍可正常读取（悬空 `parent_id` 被容忍）
- [x] 1.9 新增 pytest：删除后 `messages.related_itinerary_id` 保持原值不被清理

## 2. 前端接口封装

- [x] 2.1 在 `frontend/api.js` 新增 `deleteHistoryItem(planId)`，使用 `DELETE` 与既有 `apiJson` 约定，且不吞掉错误
- [x] 2.2 在 `frontend/api.js` 的导出清单中注册该函数

## 3. 确认弹窗组件

- [x] 3.1 新建通用确认弹窗组件 `ConfirmModal`，复用 `.modal-backdrop` / `.modal-card` / `.modal-title` 样式
- [x] 3.2 确认按钮在 `danger` 时使用危险样式，默认焦点落在取消按钮，支持 Esc 关闭与点击遮罩关闭
- [x] 3.3 在 `frontend/style.css` 补充行程摘要、危险按钮与错误提示样式

## 4. 历史行程页接入

- [x] 4.1 在 `HistoryPage` 卡片右上角新增常驻删除图标，`aria-label` 包含目的地，点击时 `stopPropagation` 以免触发整卡打开
- [x] 4.2 删除图标支持键盘触发并保持可见焦点样式；整卡的 Enter 处理加上 `e.target === e.currentTarget` 守卫，避免卡内按钮的回车冒泡成打开行程
- [x] 4.3 点击删除打开确认弹窗，展示目的地、日期区间与不可恢复警告
- [x] 4.4 确认后调用删除接口，请求期间禁用两个按钮防重复提交
- [x] 4.5 成功后重新拉取列表，使卡片与「共 N 期」计数同步；失败时保留弹窗并展示可读错误
- [x] 4.6 删除最后一条行程后进入既有空状态

## 5. 文档同步

- [x] 5.1 在 `docs.md` 增加「问题十三」，记录外键未声明 `ON DELETE` 静默拦住删除的根因、三层修复与沉淀
- [x] 5.2 检查 `README.md`：其「设计亮点」为规划引擎的技术亮点清单，未描述历史行程页能力，无相关描述需要同步，故不修改

## 6. 验证

- [x] 6.1 运行后端 pytest：`tests/test_history_api.py` 16 项全部通过；全量 38 passed / 34 errors，34 个 error 全部是环境缺少 `langgraph` 与 `langchain_core` 导致的既有失败，与本变更无关
- [x] 6.2 运行既有前端测试 `node --test tests/chat-state.test.js tests/navigation-state.test.js` 26 项通过；另用 Babel standalone 离线编译全部 6 个 `.jsx` 文件确认语法可编译
- [ ] 6.3 手工验证：删除无引用行程、删除被 Run 引用过的行程、删除他人行程被拒、重复删除、删除最后一条行程（需要装齐依赖后启动服务）
- [ ] 6.4 在桌面与窄屏视口验证删除图标位置、确认弹窗布局与键盘操作（需要装齐依赖后启动服务）

> 6.3 与 6.4 未完成的原因：当前开发环境缺少 `langgraph` / `langchain_core`，`app.main` 无法导入，因此无法启动服务做浏览器验证。HTTP 层语义已由 `tests/test_history_api.py` 覆盖，未覆盖的是真实浏览器中的视觉与交互表现。
