const test = require("node:test");
const assert = require("node:assert/strict");

require("../frontend/chat-state.js");

test("streams assistant deltas and reconciles the durable message", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "run-1", {
    kind: "messages",
    payload: { message_id: "assistant:run-1", delta: "你" },
  });
  state = ChatState.applyEvent(state, "run-1", {
    kind: "messages",
    payload: { message_id: "assistant:run-1", delta: "好" },
  });
  assert.equal(state.messages["assistant:run-1"].content, "你好");
  state = ChatState.applyEvent(state, "run-1", {
    kind: "custom",
    sequence: 4,
    payload: {
      kind: "chat.message.completed",
      message_id: "message-1",
      content: "你好",
      sequence: 2,
      created_at: "2026-07-23T10:00:01Z",
    },
  });
  assert.equal(state.messages["assistant:run-1"], undefined);
  assert.equal(state.messages["message-1"].content, "你好");
  assert.equal(state.messages["message-1"].created_at, "2026-07-23T10:00:01Z");
});

test("keeps a just-completed historical reply above the formal planning card", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom",
    sequence: 1,
    payload: {
      kind: "chat.message.completed",
      message_id: "assistant-before-plan",
      content: "请补充日期",
      sequence: 2,
      created_at: "2026-07-23T10:00:01Z",
    },
  });
  state.runs["formal-plan"] = {
    id: "formal-plan", kind: "travel_plan", status: "queued",
    created_at: "2026-07-23T10:00:02Z",
  };
  assert.deepEqual(
    ChatState.activityItems(state).map(item => item.key),
    ["message:assistant-before-plan", "run:formal-plan"],
  );
});

test("keeps independent run cards and ignores duplicate cursors", () => {
  let state = ChatState.initialState();
  state.runs = {
    "run-a": { id: "run-a", status: "running" },
    "run-b": { id: "run-b", status: "queued" },
  };
  state = ChatState.applyEvent(state, "run-a", {
    kind: "custom",
    sequence: 3,
    payload: { kind: "run.status", status: "succeeded" },
  });
  const duplicated = ChatState.applyEvent(state, "run-a", {
    kind: "custom",
    sequence: 3,
    payload: { kind: "run.status", status: "failed" },
  });
  assert.equal(duplicated.runs["run-a"].status, "succeeded");
  assert.equal(duplicated.runs["run-b"].status, "queued");
});

test("makes a completed itinerary actionable without requiring a reload", () => {
  let state = ChatState.initialState();
  state.runs.revision = {
    id: "revision",
    kind: "revision",
    status: "running",
    request_snapshot: {},
  };
  state = ChatState.applyEvent(state, "revision", {
    kind: "custom",
    sequence: 6,
    payload: {
      kind: "planning.itinerary_created",
      itinerary_id: "itinerary-v2",
      destination: "泉州",
    },
  });
  state = ChatState.applyEvent(state, "revision", {
    kind: "custom",
    sequence: 7,
    payload: { kind: "run.status", status: "succeeded" },
  });
  assert.equal(state.runs.revision.status, "succeeded");
  assert.equal(state.runs.revision.result_itinerary_id, "itinerary-v2");
  assert.equal(state.runs.revision.request_snapshot.destination, "泉州");
});

test("stores planning briefs by id", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom",
    sequence: 1,
    payload: {
      kind: "planning_brief.ready",
      brief_id: "brief-a",
      status: "ready",
      summary: { destination: "云南", days: 5 },
      missing_fields: [],
    },
  });
  assert.equal(state.briefs["brief-a"].data.destination, "云南");
  assert.equal(state.briefs["brief-a"].status, "ready");
});

test("keeps dynamic constraints and memory projection on planning brief events", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom", sequence: 2,
    payload: {
      kind: "planning_brief.ready", brief_id: "brief-memory", status: "ready",
      summary: {
        destination: "东京", trip_budget: "5000 元",
        trip_constraints: [{ id: "c1", category: "travel_pace", value_text: "每天最多三个景点", polarity: "require" }],
      },
      missing_fields: [],
      memory_context: {
        revision: 4, status: "succeeded",
        applied_facts: [{ fact_id: "f1", category: "dietary_requirement", value_text: "避开花生" }],
        excluded_facts: [],
      },
      effective_constraints: [{ id: "c1", value_text: "每天最多三个景点" }],
      constraint_coverage: [{ constraint_id: "c1", status: "applied" }],
    },
  });
  const brief = state.briefs["brief-memory"];
  assert.equal(brief.memory_context.applied_facts[0].fact_id, "f1");
  assert.equal(brief.effective_constraints.length, 1);
  assert.deepEqual(ChatState.briefViewModel(brief).preferences, [
    ["本次预算", "5000 元"], ["旅行节奏 · 必须", "每天最多三个景点"],
  ]);
  assert.equal(ChatState.briefViewModel(brief).usesDefaults, false);
});

test("renders a persisted planning brief with explicit and long-term constraints after reload", () => {
  const brief = {
    id: "brief-reloaded", status: "ready",
    data: {
      destination: "南京",
      trip_constraints: [{
        id: "c-hotpot", category: "food_preference",
        value_text: "吃火锅", polarity: "prefer", source: "conversation",
      }],
    },
    memory_context: {
      status: "succeeded",
      applied_facts: [{
        fact_id: "f-pace", category: "travel_pace",
        value_text: "不喜欢早起", source: "long_term_memory",
      }],
    },
  };

  const view = ChatState.briefViewModel(brief);
  assert.deepEqual(view.preferences, [["餐饮 · 偏好", "吃火锅"]]);
  assert.equal(view.usesDefaults, false);
  assert.equal(brief.memory_context.applied_facts[0].value_text, "不喜欢早起");
});

test("explains avoid memory as an exclusion instead of a destination preference", () => {
  const avoid = ChatState.memoryFactPresentation({
    category: "attraction_preference", value_text: "老门东", polarity: "avoid",
  });
  assert.deepEqual(avoid, {
    tone: "avoid", badge: "本次避开", summaryLabel: "避开",
    effect: "规划时将排除，不纳入候选行程",
    excludeAction: "本次允许安排", restoreAction: "恢复避开",
  });
  assert.deepEqual(ChatState.briefViewModel({
    status: "ready",
    data: { trip_constraints: [{ category: "attraction_preference", value_text: "老门东", polarity: "avoid" }] },
  }).preferences, [["景点 · 避开", "老门东"]]);
});

test("shows a thinking item only while a chat run is awaiting its response", () => {
  let state = ChatState.initialState();
  state.runs["chat-run"] = {
    id: "chat-run", kind: "chat", status: "queued",
    created_at: "2026-07-23T10:00:01Z",
  };
  assert.deepEqual(
    ChatState.activityItems(state).map(item => item.key),
    ["chat-thinking:chat-run"],
  );
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom", sequence: 1,
    payload: { kind: "run.status", status: "succeeded" },
  });
  assert.deepEqual(ChatState.activityItems(state), []);
});

test("adds a planning run created from a chat decision without a reload", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom",
    sequence: 1,
    payload: {
      kind: "run.created",
      run: { id: "plan-run", kind: "travel_plan", status: "queued", request_snapshot: { destination: "南京" } },
    },
  });
  assert.equal(state.runs["plan-run"].status, "queued");
  assert.equal(state.runs["plan-run"].request_snapshot.destination, "南京");
});

test("keeps a waiting interaction after the following status event", () => {
  let state = ChatState.initialState();
  state.runs["run-waiting"] = { id: "run-waiting", status: "running" };
  state = ChatState.applyEvent(state, "run-waiting", {
    kind: "custom",
    sequence: 3,
    payload: {
      kind: "run.waiting_user",
      interaction_id: "interaction-1",
      question: "请补充出行日期",
    },
  });
  state = ChatState.applyEvent(state, "run-waiting", {
    kind: "custom",
    sequence: 4,
    payload: { kind: "run.status", status: "waiting_user" },
  });
  assert.equal(state.runs["run-waiting"].status, "waiting_user");
  assert.equal(
    state.runs["run-waiting"].pending_interaction.interaction_id,
    "interaction-1",
  );
  assert.equal(
    state.runs["run-waiting"].pending_interaction.question,
    "请补充出行日期",
  );
});

test("clears a waiting interaction when the run resumes", () => {
  let state = ChatState.initialState();
  state.runs["run-waiting"] = {
    id: "run-waiting",
    status: "waiting_user",
    pending_interaction: { interaction_id: "interaction-1" },
  };
  state = ChatState.applyEvent(state, "run-waiting", {
    kind: "custom",
    sequence: 5,
    payload: { kind: "run.status", status: "running" },
  });
  assert.equal(state.runs["run-waiting"].pending_interaction, null);
});

test("labels submitted planning briefs accurately", () => {
  assert.equal(ChatState.planningBriefStatusLabel("collecting"), "信息收集中");
  assert.equal(ChatState.planningBriefStatusLabel("ready"), "等待确认");
  assert.equal(ChatState.planningBriefStatusLabel("submitted"), "已提交");
});

test("prioritizes conversation attention that needs the user", () => {
  const item = {
    status: "active",
    has_waiting_user: true,
    has_ready_brief: true,
    has_active_planning: true,
    has_unread_completed: true,
  };
  assert.deepEqual(ChatState.conversationAttention(item), {
    kind: "waiting-user", label: "待你回复", ariaLabel: "规划正在等待你的回复",
  });
  item.has_waiting_user = false;
  assert.equal(ChatState.conversationAttention(item).kind, "ready-brief");
  item.has_ready_brief = false;
  assert.equal(ChatState.conversationAttention(item).kind, "planning");
  item.has_active_planning = false;
  assert.equal(ChatState.conversationAttention(item).kind, "unread");
});

test("archive state suppresses planning and unread attention", () => {
  assert.deepEqual(ChatState.conversationAttention({
    status: "archived", has_waiting_user: true, has_unread_completed: true,
  }), { kind: "archived", label: "已归档", ariaLabel: "对话已归档" });
});

test("only polls and marks a loaded conversation while the page is visible", () => {
  assert.equal(ChatState.shouldPollConversations("visible"), true);
  assert.equal(ChatState.shouldPollConversations("hidden"), false);
  assert.equal(ChatState.shouldMarkConversationViewed("visible", "conversation-1", "conversation-1"), true);
  assert.equal(ChatState.shouldMarkConversationViewed("hidden", "conversation-1", "conversation-1"), false);
  assert.equal(ChatState.shouldMarkConversationViewed("visible", "conversation-2", "conversation-1"), false);
  assert.equal(ChatState.shouldMarkConversationViewed("visible", "", "conversation-1"), false);
});

test("projects messages, briefs, non-chat runs, and failed chat retries into one stable activity timeline", () => {
  let state = ChatState.initialState();
  state = ChatState.upsertMessage(state, {
    id: "message-1", role: "user", content: "规划云南",
    sequence: 1, created_at: "2026-07-23T10:00:00Z",
  });
  state.briefs["brief-1"] = {
    id: "brief-1", status: "ready", data: { destination: "云南" },
    answered_fields: [], declined_fields: [], question: null, collected: true,
    created_at: "2026-07-23T10:00:01Z",
  };
  state.runs["chat-run"] = {
    id: "chat-run", kind: "chat", created_at: "2026-07-23T10:00:02Z",
  };
  state.runs["failed-chat-run"] = {
    id: "failed-chat-run", kind: "chat", status: "failed",
    created_at: "2026-07-23T10:00:02.5Z",
  };
  state.runs["plan-run"] = {
    id: "plan-run", kind: "travel_plan", status: "queued",
    created_at: "2026-07-23T10:00:03Z",
  };
  assert.deepEqual(
    ChatState.activityItems(state).map(item => item.key),
    ["message:message-1", "brief:brief-1", "chat-failure:failed-chat-run", "run:plan-run"],
  );
});

test("keeps activity order stable after entity updates and reconstruction", () => {
  const build = () => {
    let state = ChatState.initialState();
    state = ChatState.upsertMessage(state, {
      id: "m", role: "assistant", content: "已整理",
      sequence: 2, created_at: "2026-07-23T10:00:00Z",
    });
    state.briefs.b = {
      id: "b", status: "collecting", data: { destination: "云南" },
      answered_fields: ["destination"], declined_fields: [],
      question: { field: "date_range", label: "出行日期", remaining: 5, total: 6 },
      collected: false,
      created_at: "2026-07-23T10:00:01Z",
      updated_at: "2026-07-23T10:05:00Z",
    };
    state.runs.r = {
      id: "r", kind: "travel_plan", status: "queued",
      created_at: "2026-07-23T10:00:02Z",
    };
    return state;
  };
  const before = build();
  before.briefs.b = { ...before.briefs.b, status: "ready", updated_at: "2026-07-23T10:10:00Z" };
  assert.deepEqual(
    ChatState.activityItems(before).map(item => item.key),
    ChatState.activityItems(build()).map(item => item.key),
  );
});

test("uses conversation sequence for live assistant messages without timestamps", () => {
  let state = ChatState.initialState();
  state = ChatState.upsertMessage(state, {
    id: "user-1", role: "user", sequence: 1,
    created_at: "2026-07-23T10:00:00Z",
  });
  state = ChatState.upsertMessage(state, {
    id: "assistant-1", role: "assistant", sequence: 2,
  });
  state = ChatState.upsertMessage(state, {
    id: "user-2", role: "user", sequence: 3,
    created_at: "2026-07-23T10:02:00Z",
  });
  assert.deepEqual(
    ChatState.activityItems(state)
      .filter(item => item.type === "message")
      .map(item => item.entityId),
    ["user-1", "assistant-1", "user-2"],
  );
});

test("maps internal planning loops to monotonic product stages", () => {
  let state = ChatState.initialState();
  state.runs.r = { id: "r", kind: "travel_plan", status: "running" };
  const progress = (sequence, stage) => {
    state = ChatState.applyEvent(state, "r", {
      kind: "custom", sequence,
      payload: { kind: "planning_run.progress", stage, label: stage },
    });
  };
  progress(1, "planner");
  progress(2, "reviewer");
  progress(3, "time_check");
  progress(4, "planner");
  assert.equal(state.runs.r.product_stage, "compose");
  assert.equal(state.runs.r.journey_step_index, 4);
  assert.equal(state.runs.r.internal_stage, "time_check");
  assert.deepEqual(state.runs.r.completed_product_stages, ["understand", "discover"]);
  progress(5, "meal_search");
  progress(6, "reviewer");
  assert.equal(state.runs.r.product_stage, "polish");
  assert.equal(state.runs.r.journey_step_index, 5);
  assert.equal(state.runs.r.internal_stage, "meal_search");
});

test("keeps retry runs as independent associated activity items", () => {
  const state = ChatState.initialState();
  state.runs.failed = {
    id: "failed", kind: "travel_plan", status: "failed",
    created_at: "2026-07-23T10:00:00Z",
  };
  state.runs.retry = {
    id: "retry", kind: "travel_plan", status: "queued",
    retry_of_run_id: "failed", created_at: "2026-07-23T10:00:01Z",
  };
  const runs = ChatState.activityItems(state).filter(item => item.type === "run");
  assert.equal(runs.length, 2);
  assert.equal(runs[1].entity.retry_of_run_id, "failed");
});

test("chooses safe structured controls with a text fallback", () => {
  assert.equal(ChatState.interactionInputKind({
    question: "请补充 start_date、end_date",
    input_schema: { type: "string" },
  }), "date-range");
  assert.equal(ChatState.interactionInputKind({
    question: "选择节奏",
    input_schema: { enum: ["舒缓", "紧凑"] },
  }), "single-choice");
  assert.equal(ChatState.interactionInputKind({
    question: "选择偏好",
    input_schema: { type: "array", items: { enum: ["美食", "人文"] } },
  }), "multi-choice");
  assert.equal(ChatState.interactionInputKind({
    question: "还有什么想补充？",
    input_schema: { type: "object", unsafe: "<script>" },
  }), "text");
});

test("builds a ready brief summary with explicit defaults", () => {
  const view = ChatState.briefViewModel({
    status: "ready",
    data: {
      destination: "云南",
      start_date: "2026-10-01",
      end_date: "2026-10-05",
      food_preference: "清淡",
    },
    missing_fields: [],
  });
  assert.equal(view.destination, "云南");
  assert.equal(view.dateLabel, "2026-10-01 — 2026-10-05");
  assert.equal(view.usesDefaults, true);
  assert.deepEqual(view.preferences, [["餐饮", "清淡"]]);
});

test("shows arrival and departure as the user phrased them, and marks fuzzy ones", () => {
  const view = ChatState.briefViewModel({
    status: "ready",
    data: {
      destination: "云南",
      start_date: "2026-11-07",
      end_date: "2026-11-10",
      arrival_time: "11月7日傍晚抵达",
      departure_time: "18:30",
    },
  });
  // 用户说的「傍晚」必须原样展示，同时标明不是具体钟点——后端据此采用保守下界
  assert.equal(view.arrivalLabel, "11月7日傍晚抵达（未提供具体时刻）");
  assert.equal(view.departureLabel, "18:30");
  assert.equal(view.timeWindowLabel, "抵达 11月7日傍晚抵达（未提供具体时刻） · 返程 18:30");
});

test("marks a missing arrival and departure time as not provided", () => {
  const view = ChatState.briefViewModel({
    status: "ready",
    data: { destination: "南京", start_date: "2026-11-07", end_date: "2026-11-10" },
  });
  assert.equal(view.arrivalLabel, "未提供");
  assert.equal(view.departureLabel, "未提供");
  assert.equal(view.timeWindowLabel, "未提供");
});

test("treats a bare period word as not a concrete clock time", () => {
  assert.equal(ChatState.briefViewModel({ data: { arrival_time: "傍晚" } }).arrivalLabel,
    "傍晚（未提供具体时刻）");
  assert.equal(ChatState.briefViewModel({ data: { arrival_time: "17:00" } }).arrivalLabel, "17:00");
  assert.equal(ChatState.briefViewModel({ data: { arrival_time: "下午3点" } }).arrivalLabel, "下午3点");
  // 只填了一头时，另一头仍要显式说明未提供
  assert.equal(ChatState.briefViewModel({ data: { departure_time: "15:00" } }).timeWindowLabel,
    "抵达 未提供 · 返程 15:00");
});

test("describes every run terminal and non-terminal state with an action", () => {
  for (const status of ["queued", "running", "waiting_user", "succeeded", "failed", "cancelled"]) {
    assert.ok(ChatState.RUN_PRESENTATIONS[status].label);
    assert.ok(ChatState.RUN_PRESENTATIONS[status].copy);
    assert.ok(ChatState.RUN_PRESENTATIONS[status].primaryAction);
  }
  assert.equal(ChatState.RUN_PRESENTATIONS.waiting_user.primaryAction, "resume");
  assert.equal(ChatState.RUN_PRESENTATIONS.failed.primaryAction, "retry");
});

// --- 字段提问：一条 brief 派生多个时间线条目 ---------------------------------

function collectingBrief(overrides = {}) {
  return {
    id: "brief-q", status: "collecting",
    data: { destination: "丽江" },
    answered_fields: ["destination"],
    declined_fields: [],
    question: {
      field: "date_range", label: "出行日期", question: "打算哪天出发、哪天回来？",
      kind: "date_range", options: [], optional: false, remaining: 5, total: 6,
    },
    collected: false,
    created_at: "2026-07-23T10:00:01Z",
    ...overrides,
  };
}

test("still shows the trip note before the server sends the question projection", () => {
  // 兼容性护栏：缺 collected 字段的旧载荷不能因为本次改动而整张卡消失。
  let state = ChatState.initialState();
  state.briefs.legacy = { id: "legacy", status: "ready", data: { destination: "云南" } };
  const items = ChatState.activityItems(state).filter(item => item.type === "brief");
  assert.equal(items.length, 0);

  state.briefs.legacy = { ...state.briefs.legacy, collected: true };
  assert.deepEqual(
    ChatState.activityItems(state).filter(item => item.type === "brief").map(item => item.key),
    ["brief:legacy"],
  );
});

test("derives one record per handled field plus the current question", () => {
  const state = ChatState.initialState();
  state.briefs["brief-q"] = collectingBrief({
    answered_fields: ["destination", "arrival_time", "departure_time"],
    declined_fields: ["arrival_time"],
    data: { destination: "丽江", arrival_time: "", departure_time: "15:00" },
  });
  assert.deepEqual(
    ChatState.activityItems(state).map(item => item.key),
    [
      "brief-record:brief-q:destination",
      "brief-record:brief-q:arrival_time",
      "brief-record:brief-q:departure_time",
      "brief-question:brief-q:date_range",
    ],
  );
  const records = ChatState.activityItems(state).filter(item => item.type === "brief_record");
  assert.deepEqual(records.map(item => item.entity.label), ["目的地", "抵达时刻", "返程时刻"]);
  assert.deepEqual(records.map(item => item.entity.value), ["丽江", "", "15:00"]);
  assert.deepEqual(records.map(item => item.entity.skipped), [false, true, false]);
});

test("replaces the question item in place instead of appending a new one", () => {
  const state = ChatState.initialState();
  state.briefs["brief-q"] = collectingBrief();
  const first = ChatState.activityItems(state).filter(item => item.type === "brief_question");
  assert.equal(first.length, 1);
  assert.equal(first[0].entity.remaining, 5);

  state.briefs["brief-q"] = collectingBrief({
    data: { destination: "丽江", start_date: "2026-11-07", end_date: "2026-11-11" },
    answered_fields: ["destination", "date_range"],
    question: { field: "arrival_time", label: "抵达时刻", remaining: 4, total: 6 },
  });
  const second = ChatState.activityItems(state).filter(item => item.type === "brief_question");
  assert.equal(second.length, 1);
  assert.equal(second[0].key, "brief-question:brief-q:arrival_time");
  assert.equal(second[0].entity.progress, "还差 4 项");
  assert.deepEqual(
    ChatState.activityItems(state).map(item => item.key),
    [
      "brief-record:brief-q:destination",
      "brief-record:brief-q:date_range",
      "brief-question:brief-q:arrival_time",
    ],
  );
});

test("swaps the question for the trip note once collection completes", () => {
  const state = ChatState.initialState();
  state.briefs["brief-q"] = collectingBrief({
    status: "ready",
    data: { destination: "丽江", start_date: "2026-11-07", end_date: "2026-11-11" },
    answered_fields: ["destination", "date_range", "arrival_time", "departure_time",
      "trip_budget", "preferences"],
    declined_fields: ["arrival_time", "departure_time", "trip_budget", "preferences"],
    question: null,
    collected: true,
  });
  const keys = ChatState.activityItems(state).map(item => item.key);
  assert.equal(keys.filter(key => key.startsWith("brief-question")).length, 0);
  assert.equal(keys[keys.length - 1], "brief:brief-q");
});

test("drops every derived item for a discarded brief", () => {
  const state = ChatState.initialState();
  state.briefs["brief-q"] = collectingBrief({ status: "discarded" });
  assert.deepEqual(ChatState.activityItems(state), []);
});

test("keeps derived keys stable when the same events replay", () => {
  const build = () => {
    let state = ChatState.initialState();
    state = ChatState.applyEvent(state, "chat-run", {
      kind: "custom", sequence: 1,
      payload: {
        kind: "planning_brief.updated", brief_id: "brief-q", status: "collecting",
        summary: { destination: "丽江" },
        missing_fields: ["start_date", "end_date"],
        answered_fields: ["destination"], declined_fields: [], collected: false,
        question: { field: "date_range", label: "出行日期", remaining: 5, total: 6 },
      },
    });
    return state;
  };
  const before = build();
  // 重放同一条事件必须幂等。
  const replayed = ChatState.applyEvent(before, "chat-run", {
    kind: "custom", sequence: 1,
    payload: {
      kind: "planning_brief.updated", brief_id: "brief-q", status: "collecting",
      summary: { destination: "丽江" },
      missing_fields: ["start_date", "end_date"],
      answered_fields: ["destination"], declined_fields: [], collected: false,
      question: { field: "date_range", label: "出行日期", remaining: 5, total: 6 },
    },
  });
  assert.deepEqual(
    ChatState.activityItems(replayed).map(item => item.key),
    ChatState.activityItems(build()).map(item => item.key),
  );
});

test("keeps the question projection when a terminal event omits it", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom", sequence: 1,
    payload: {
      kind: "planning_brief.updated", brief_id: "b", status: "collecting",
      summary: { destination: "丽江" },
      answered_fields: ["destination"], declined_fields: ["arrival_time"],
      collected: false,
      question: { field: "date_range", label: "出行日期", remaining: 5, total: 6 },
    },
  });
  // 终态事件不带提问字段：不能用空值把已有状态抹掉。
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom", sequence: 2,
    payload: {
      kind: "planning_brief.submitted", brief_id: "b", status: "submitted",
      summary: { destination: "丽江" }, missing_fields: [],
    },
  });
  const brief = state.briefs.b;
  assert.deepEqual(brief.declined_fields, ["arrival_time"]);
  assert.deepEqual(brief.answered_fields, ["destination"]);
  assert.equal(brief.question.field, "date_range");
  assert.equal(brief.collected, false);
});

test("formats each handled field the way the summary reads it", () => {
  const brief = collectingBrief({
    data: {
      destination: "丽江",
      start_date: "2026-11-07", end_date: "2026-11-11", days: 5,
      arrival_time: "傍晚", departure_time: "15:00",
      trip_budget: "6000 元",
      trip_constraints: [{ id: "c", category: "food_preference", value_text: "美食", polarity: "prefer" }],
    },
    answered_fields: ["destination", "date_range", "arrival_time", "departure_time",
      "trip_budget", "preferences"],
  });
  const values = Object.fromEntries(
    ChatState.briefFieldRecords(brief).map(record => [record.field, record.value])
  );
  assert.deepEqual(values, {
    destination: "丽江",
    date_range: "2026-11-07 — 2026-11-11",
    arrival_time: "傍晚",
    departure_time: "15:00",
    trip_budget: "6000 元",
    preferences: "美食",
  });
});

test("falls back to the raw field id for a question the frontend does not know", () => {
  const brief = collectingBrief({
    answered_fields: ["destination", "visa_note"],
    data: { destination: "丽江" },
  });
  const labels = ChatState.briefFieldRecords(brief).map(record => record.label);
  assert.deepEqual(labels, ["目的地", "visa_note"]);
});

test("reports how many questions are left", () => {
  assert.equal(ChatState.briefProgressLabel(collectingBrief()), "还差 5 项");
  assert.equal(ChatState.briefProgressLabel(collectingBrief({ collected: true, question: null })), "");
  assert.equal(ChatState.briefProgressLabel({ id: "x", data: {} }), "");
});

// --- 澄清提问与字段提问呈现一致 ---------------------------------------------

test("attaches live clarification options to the assistant message", () => {
  let state = ChatState.initialState();
  state.runs["chat-run"] = { id: "chat-run", kind: "chat", created_at: "2026-07-23T10:00:00Z" };
  state = ChatState.upsertMessage(state, {
    id: "assistant-clarify", role: "assistant",
    content: "我找到了多个可能的行程，请先选择要修改的那一份。",
    sequence: 2, created_at: "2026-07-23T10:00:02Z", related_run_id: "chat-run",
  });
  state = ChatState.applyEvent(state, "chat-run", {
    kind: "custom", sequence: 1,
    payload: {
      kind: "chat.clarification", field: "itinerary_id",
      question: "我找到了多个可能的行程，请先选择要修改的那一份。",
      options: ["南京三日游", "苏州两日游"],
    },
  });
  const item = ChatState.activityItems(state).find(entry => entry.type === "message");
  assert.deepEqual(item.clarification.options, ["南京三日游", "苏州两日游"]);
});

test("drops clarification options on reload but keeps the question text", () => {
  // 刷新后 runs 来自 listRuns，不带 clarification：卡片退化成普通气泡，
  // 但问题文本本身是持久化的助手消息，信息不会丢。
  let state = ChatState.initialState();
  state.runs["chat-run"] = { id: "chat-run", kind: "chat", status: "succeeded" };
  state = ChatState.upsertMessage(state, {
    id: "assistant-clarify", role: "assistant",
    content: "我找到了多个可能的行程，请先选择要修改的那一份。",
    sequence: 2, related_run_id: "chat-run",
  });
  const item = ChatState.activityItems(state).find(entry => entry.type === "message");
  assert.equal(item.clarification, null);
  assert.match(item.entity.content, /多个可能的行程/);
});

test("never attaches clarification options to a user message", () => {
  let state = ChatState.initialState();
  state.runs["chat-run"] = {
    id: "chat-run", kind: "chat",
    clarification: { kind: "chat.clarification", options: ["A"] },
  };
  state = ChatState.upsertMessage(state, {
    id: "user-1", role: "user", content: "改一下", sequence: 1, related_run_id: "chat-run",
  });
  const item = ChatState.activityItems(state).find(entry => entry.type === "message");
  assert.equal(item.clarification, null);
});

test("surfaces an amap quota warning as its own timeline item", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "plan-1", {
    kind: "custom", sequence: 1,
    payload: {
      kind: "amap.quota_warning", bucket: "search",
      used: 3800, limit: 4750, remaining: 950,
      message: "高德「基础搜索服务」的本月调用额度已用 3800/4750，接近上限。",
    },
  });
  const items = ChatState.activityItems(state);
  const warning = items.find(entry => entry.type === "quota_warning");
  const card = items.find(entry => entry.type === "run");
  assert.equal(warning.entityId, "plan-1");
  assert.equal(warning.entity.quota_warning.used, 3800);
  assert.match(warning.entity.quota_warning.message, /基础搜索服务/);
  // 预警属于这条 Run 的旁路信息，必须排在任务卡之前
  assert.ok(
    items.indexOf(warning) < items.indexOf(card),
    "配额预警应排在同一条 Run 的任务卡之前",
  );
});

test("keeps only one quota warning per run and drops it when absent", () => {
  let state = ChatState.initialState();
  state = ChatState.applyEvent(state, "plan-1", {
    kind: "custom", sequence: 1,
    payload: { kind: "amap.quota_warning", bucket: "search", used: 3800, limit: 4750 },
  });
  state = ChatState.applyEvent(state, "plan-1", {
    kind: "custom", sequence: 2,
    payload: { kind: "amap.quota_warning", bucket: "weather", used: 4000, limit: 4750 },
  });
  const warnings = ChatState.activityItems(state).filter(e => e.type === "quota_warning");
  assert.equal(warnings.length, 1, "预警不堆叠历史，只保留最后一条");
  assert.equal(warnings[0].entity.quota_warning.bucket, "weather");

  // 没有预警的 Run 不产生任何额外条目
  let clean = ChatState.initialState();
  clean.runs["plan-2"] = { id: "plan-2", kind: "travel_plan", status: "running" };
  assert.equal(
    ChatState.activityItems(clean).filter(e => e.type === "quota_warning").length,
    0,
  );
});

